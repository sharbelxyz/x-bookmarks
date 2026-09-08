#!/usr/bin/env python3
"""Let a Telegram tap record a verdict, without opening the Mac to the internet.

The problem this solves: the dashboard is loopback-only, so decisions can only
be made at the Mac. Telegram already reaches the phone, but only one way.

The shape, and why it is this shape:

* **Only one process may poll a Telegram bot token.** The Atlas bot on the VPS
  already polls with ``callback_query`` enabled, so the Mac cannot also poll.
  The VPS therefore records the tap; the Mac collects it on its next pass.
* **``callback_data`` is capped at 64 bytes.** A tool key like
  ``github.com/aldinokemal/go-whatsapp-web-multidevice`` is nearly double that
  on its own, so the key never travels. Each offered decision gets a short id;
  the id→key map stays here, on the Mac, in ``pending-decisions.json``.
* **One write path.** A pulled decision is applied through exactly the same
  validation the HTTP endpoint uses (``radar_server.record_verdict``, which
  also resolves the resource type server-side), so the two can never drift.

Recovery model (the part that used to lose taps):

* The checkpoint is **log identity + consumed records**, not a bare byte
  offset. Identity = the remote file's inode plus a hash of its first bytes;
  rotation, truncation and same-size replacement are detected before anything
  is consumed from the old offset, and trigger a re-read from the start.
* Only **complete, newline-terminated lines** are consumed; a partial append
  (including a cut UTF-8 sequence) stays unconsumed until the producer
  finishes it. The offset only ever advances over bytes that were parsed.
* Every fetched record is journaled to ``telegram-received.jsonl`` and every
  rejected/unknown one to ``telegram-rejected.jsonl`` **before** the
  checkpoint advances past it: an acknowledged tap is never reduced to a
  counter.
* Application is **idempotent**: each record has a stable event id (its raw
  bytes hashed, or an explicit ``event_id`` field); ids live in the
  checkpoint's ``consumed_ids`` and on the written verdict itself
  (``source_event``), so replays and crash-window retries never double-apply.

Known limitation, stated rather than papered over: if the VPS producer
truncates or rotates the log *destructively* before a pull ever sees those
bytes, the taps in the destroyed region are unrecoverable from here. The
smallest producer-side fix is proposed in the lane package (monotonic ``seq``
per line + keep one rotated generation); until then this consumer detects the
gap and reports it instead of silently skipping.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


os.umask(0o077)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import vps_config  # noqa: E402  (needs the sys.path insert above)

DATA_DIR = ROOT / "data" / "group-monitor"
PENDING_PATH = DATA_DIR / "pending-decisions.json"
OFFSET_PATH = DATA_DIR / "telegram-offset.json"

# Deployment coordinates come from config/vps.json (git-ignored) or
# RADAR_VPS_* — never from source, so this file is safe to publish. Module
# level names are kept so existing tests can patch them directly.
_VPS = vps_config.load()
VPS_HOST = _VPS["host"]
VPS_KEY = _VPS["ssh_key"] or Path("/nonexistent/vps-key-not-configured")
REMOTE_LOG = _VPS["remote_base"] + "/data/radar-decisions.jsonl"
SSH_TIMEOUT = 30

CALLBACK_PREFIX = "rdr"
CALLBACK_LIMIT = 64          # Telegram's hard limit on callback_data
ID_LENGTH = 8
ACTION_TO_VERDICT = {"y": "must_try", "n": "excluded"}
PENDING_RETENTION_DAYS = 30

EVENT_ID_LENGTH = 16
CONSUMED_IDS_KEEP = 500
DEFAULT_PREFIX_LEN = 256
CHECKPOINT_VERSION = 2

sys.path.insert(0, str(ROOT / "scripts"))



def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def short_id(tool_key: str) -> str:
    """Stable short handle for a tool, small enough to fit in callback_data."""
    return hashlib.sha1(tool_key.encode("utf-8")).hexdigest()[:ID_LENGTH]


def callback_data(tool_key: str, action: str) -> str:
    payload = "{}:{}:{}".format(CALLBACK_PREFIX, short_id(tool_key), action)
    if len(payload.encode("utf-8")) > CALLBACK_LIMIT:
        raise ValueError("callback payload exceeds Telegram's 64-byte limit")
    return payload


def event_id_for(record: Any, raw: Optional[bytes] = None) -> str:
    """Stable identity for one decision event, for idempotent application.

    An explicit ``event_id`` from the producer wins; otherwise the exact raw
    line bytes (or a canonical dump for records handed to us as dicts) are
    hashed, so the same delivered line always maps to the same identity.
    """
    if isinstance(record, dict):
        explicit = str(record.get("event_id") or "").strip()
        if explicit:
            return explicit[:64]
    if raw is None:
        raw = json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:EVENT_ID_LENGTH]


def load_json(path: Path, default: Any) -> Any:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _quarantine_corrupt(path: Path) -> Optional[str]:
    """Copy corrupt bytes aside before a rewrite, so nothing is destroyed."""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        json.loads(raw.decode("utf-8"))
        return None  # not corrupt; nothing to preserve
    except (ValueError, UnicodeDecodeError):
        pass
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
    target = path.with_name("{}.corrupt-{}".format(path.name, stamp))
    try:
        target.write_bytes(raw)
        target.chmod(0o600)
        return str(target)
    except OSError:
        return None


def write_json(path: Path, payload: Any) -> None:
    """Unique temp + fsync + atomic replace: durable and collision-free even
    if two processes ever write the same sidecar concurrently."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=1))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    path.chmod(0o600)


def _append_jsonl(path: Path, record: Dict[str, Any]) -> None:
    """Durable append of one journal line; never raises (journaling must not
    break a pull, but a failed persist is surfaced by the caller's flow)."""
    line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as handle:
        handle.write(line.encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())
    path.chmod(0o600)


def _received_path() -> Path:
    return DATA_DIR / "telegram-received.jsonl"


def _rejected_path() -> Path:
    return DATA_DIR / "telegram-rejected.jsonl"


def retain_rejected(reason: str, record: Any, event_id: str, raw: Optional[bytes] = None) -> bool:
    """A decision we cannot apply is still a decision someone made: keep it,
    with the reason, durably — never just a counter."""
    entry: Dict[str, Any] = {
        "at": utc_now(),
        "event_id": event_id,
        "reason": str(reason)[:300],
    }
    if isinstance(record, dict):
        entry["decision"] = record
    if raw is not None:
        entry["raw_base64"] = base64.b64encode(raw).decode("ascii")
    try:
        _append_jsonl(_rejected_path(), entry)
        return True
    except OSError as exc:
        print("telegram_decisions: could not retain rejected event {}: {}".format(event_id, exc),
              file=sys.stderr)
        return False


def register_pending(entries: List[Tuple[str, str]]) -> Dict[str, str]:
    """Remember which short id means which tool, and prune anything stale."""
    pending = load_json(PENDING_PATH, None)
    if pending is None and PENDING_PATH.exists():
        quarantined = _quarantine_corrupt(PENDING_PATH)
        if quarantined:
            print("telegram_decisions: pending-decisions.json was corrupt; preserved at {}".format(
                quarantined), file=sys.stderr)
    if not isinstance(pending, dict):
        pending = {}
    now = dt.datetime.now(dt.timezone.utc)
    fresh = {}
    for key, value in pending.items():
        try:
            offered = dt.datetime.fromisoformat(str(value.get("offered_at")))
        except (TypeError, ValueError):
            continue
        if (now - offered).days <= PENDING_RETENTION_DAYS:
            fresh[key] = value
    added = {}
    for tool_key, name in entries:
        ident = short_id(tool_key)
        fresh[ident] = {"key": tool_key, "name": name, "offered_at": utc_now()}
        added[ident] = tool_key
    write_json(PENDING_PATH, fresh)
    return added


def build_keyboard(tool_key: str, url: str = "") -> Dict[str, Any]:
    row = [
        {"text": "✅ Must try", "callback_data": callback_data(tool_key, "y")},
        {"text": "🚫 Not for me", "callback_data": callback_data(tool_key, "n")},
    ]
    if url.startswith("https://"):
        row.append({"text": "↗ Open", "url": url})
    return {"inline_keyboard": [row]}


# ---------------------------------------------------------------------------
# Remote log consumption
# ---------------------------------------------------------------------------

_FRAME_MARKER = b"payload\n"

# The remote side reports identity (inode + hash of the first N bytes) and
# size BEFORE the payload, in labeled lines, so the consumer can tell "same
# log, appended" from "different or rewritten log" without a second guess.
_REMOTE_SCRIPT = (
    "if [ -f {log} ]; then "
    "printf 'ident %s\\n' \"$(stat -c %i {log} 2>/dev/null || echo unknown)\"; "
    "printf 'prefix %s\\n' \"$(head -c {prefix_len} {log} | sha256sum | cut -d' ' -f1)\"; "
    "printf 'size %s\\n' \"$(wc -c < {log})\"; "
    "printf 'payload\\n'; "
    "tail -c +{start} {log}; "
    "else printf 'absent\\n'; fi"
)


def _run_remote(start_offset: int, prefix_len: int) -> Tuple[Optional[bytes], str]:
    """One bounded, read-only SSH call. Returns (stdout bytes, error)."""
    reason = vps_config.unconfigured_reason(_VPS)
    if reason:
        return [], offset, reason
    if not VPS_KEY.exists():
        return None, "ssh key is missing"
    command = [
        "ssh", "-i", str(VPS_KEY),
        "-o", "ConnectTimeout=8", "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "root@{}".format(VPS_HOST),
        _REMOTE_SCRIPT.format(log=REMOTE_LOG, prefix_len=max(1, int(prefix_len)),
                              start=int(start_offset) + 1),
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, timeout=SSH_TIMEOUT, check=False
        )
    except subprocess.TimeoutExpired:
        return None, "ssh timed out"
    except OSError as exc:
        return None, "ssh failed: {}".format(exc)[:150]
    if result.returncode != 0:
        stderr = result.stderr
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        return None, (stderr or "ssh returned {}".format(result.returncode)).strip()[:150]
    stdout = result.stdout
    if isinstance(stdout, str):  # tolerate text-mode stubs/mocks
        stdout = stdout.encode("utf-8", "replace")
    return stdout, ""


def _parse_frames(stdout: bytes) -> Tuple[Optional[Dict[str, Any]], str]:
    """Split the labeled header from the raw payload; malformed responses are
    an explicit error, never a guessed offset."""
    if stdout.strip() == b"absent" or stdout.startswith(b"absent\n"):
        return {"absent": True}, ""
    marker = stdout.find(_FRAME_MARKER)
    if marker < 0:
        return None, "unexpected response from the decision log (no payload frame)"
    header: Dict[str, str] = {}
    for line in stdout[:marker].decode("utf-8", "replace").splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2:
            header[parts[0]] = parts[1].strip()
    try:
        size = int(header["size"])
    except (KeyError, ValueError):
        return None, "unexpected response from the decision log (no size)"
    return {
        "absent": False,
        "inode": header.get("ident", "unknown"),
        "prefix_sha256": header.get("prefix", ""),
        "size": size,
        "payload": stdout[marker + len(_FRAME_MARKER):],
    }, ""


def _complete_lines(payload: bytes) -> Tuple[List[Tuple[bytes, str]], int]:
    """(raw line, event id) for each COMPLETE line; plus consumed byte count.

    A trailing fragment without a newline — a producer mid-append, possibly
    mid-UTF-8-sequence — is left for the next pull; its bytes do not count as
    consumed, so the checkpoint can never advance past half a record.
    """
    boundary = payload.rfind(b"\n")
    if boundary < 0:
        return [], 0
    complete = payload[:boundary + 1]
    lines = []
    for raw_line in complete.split(b"\n"):
        if raw_line.strip():
            lines.append((raw_line, hashlib.sha256(raw_line).hexdigest()[:EVENT_ID_LENGTH]))
    return lines, len(complete)


def _identity_from(frame: Dict[str, Any], prefix_len: int) -> Dict[str, Any]:
    return {
        "inode": frame.get("inode", "unknown"),
        "prefix_len": min(int(prefix_len), int(frame.get("size", 0))),
        "prefix_sha256": frame.get("prefix_sha256", ""),
    }


def _identity_matches(stored: Optional[Dict[str, Any]], frame: Dict[str, Any]) -> bool:
    if not stored:
        return False
    if str(stored.get("inode")) != str(frame.get("inode")):
        return False
    # The stored hash covered the first prefix_len bytes; the remote hashed
    # exactly that many again, so append-only growth keeps it stable.
    if int(stored.get("prefix_len") or 0) > 0 and stored.get("prefix_sha256") != frame.get("prefix_sha256"):
        return False
    return True


def fetch_remote_v2(offset: int, identity: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Read new complete records from the VPS decision log, rotation-safe.

    Returns a dict with ``records`` ([(raw bytes, event id, parsed-or-None,
    parse_error)]), ``new_offset``, ``identity``, ``reread`` (bool: the log
    changed identity/shrank and was re-read from the start), ``absent`` and
    ``error``. On any error nothing is considered consumed.
    """
    result = {"records": [], "new_offset": int(offset), "identity": identity,
              "reread": False, "absent": False, "error": ""}
    prefix_len = int((identity or {}).get("prefix_len") or 0) or DEFAULT_PREFIX_LEN
    stdout, error = _run_remote(offset, prefix_len)
    if error:
        result["error"] = error
        return result
    frame, error = _parse_frames(stdout)
    if error:
        result["error"] = error
        return result
    if frame["absent"]:
        result["absent"] = True
        result["error"] = "remote decision log is missing; nothing consumed"
        return result

    rotated = (identity is not None and not _identity_matches(identity, frame))
    truncated = frame["size"] < offset
    if rotated or truncated:
        # The old offset is meaningless against this content: discard the
        # fetched slice and re-read the whole current log. consumed_ids and
        # source_event stamps make any replayed records idempotent.
        stdout, error = _run_remote(0, DEFAULT_PREFIX_LEN)
        if error:
            result["error"] = error
            return result
        frame, error = _parse_frames(stdout)
        if error:
            result["error"] = error
            return result
        if frame["absent"]:
            result["absent"] = True
            result["error"] = "remote decision log vanished mid-pull; nothing consumed"
            return result
        offset = 0
        prefix_len = DEFAULT_PREFIX_LEN
        result["reread"] = True

    lines, consumed = _complete_lines(frame["payload"])
    records = []
    for raw_line, event in lines:
        parsed, parse_error = None, ""
        try:
            parsed = json.loads(raw_line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            parse_error = "undecodable record: {}".format(exc)[:200]
        if parsed is not None and not isinstance(parsed, dict):
            parsed, parse_error = None, "record is not a JSON object"
        records.append({"raw": raw_line, "event_id": event_id_for(parsed, raw_line) if parsed else event,
                        "decision": parsed, "parse_error": parse_error})
    result["records"] = records
    result["new_offset"] = offset + consumed
    result["identity"] = _identity_from(frame, prefix_len)
    return result


def fetch_remote(offset: int, identity: Optional[Dict[str, Any]] = None
                 ) -> Tuple[List[Dict[str, Any]], int, str]:
    """Compatibility wrapper: (decisions, new_offset, error).

    Kept for existing callers/tests; ``pull()`` uses the richer
    ``fetch_remote_v2``. On error (including a malformed or shrunken remote
    response) the offset comes back unchanged — never silently advanced.
    """
    outcome = fetch_remote_v2(offset, identity)
    if outcome["error"]:
        return [], int(offset), outcome["error"]
    decisions = [r["decision"] for r in outcome["records"] if r["decision"] is not None]
    return decisions, outcome["new_offset"], ""


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


def _already_applied(event_id: str) -> bool:
    """True when a verdict written earlier carries this exact event identity
    (crash-window replays: applied but checkpoint not yet advanced)."""
    import decision_store
    import radar_server

    document, _revision, error = decision_store.read_document(radar_server.VERDICTS_PATH, "verdicts")
    if error or not document:
        return False
    for entry in document.get("verdicts", []):
        if isinstance(entry, dict) and entry.get("source_event") == event_id:
            return True
    return False


def _apply_decisions(records: List[Dict[str, Any]],
                     consumed_ids: Tuple[str, ...] = ()) -> Dict[str, Any]:
    """Apply through the server's own validated write path — never a second one.

    Every record ends in exactly one durable place: the verdicts file
    (applied), the rejected journal (unknown/invalid/refused), or is skipped
    as an exact duplicate of something already durably applied.
    """
    import radar_server

    pending = load_json(PENDING_PATH, {})
    counters = {"applied": 0, "unknown": 0, "rejected": 0,
                "duplicates": 0, "invalid": 0, "retain_failures": 0}
    processed_ids: List[str] = []
    known = set(consumed_ids)
    for record in records:
        decision = record.get("decision")
        event = str(record.get("event_id") or event_id_for(decision, record.get("raw")))
        raw = record.get("raw")
        if event in known:
            counters["duplicates"] += 1
            continue
        processed_ids.append(event)
        known.add(event)
        if record.get("parse_error"):
            counters["invalid"] += 1
            if not retain_rejected(record["parse_error"], None, event, raw):
                counters["retain_failures"] += 1
            continue
        ident = str(decision.get("id") or "")
        action = str(decision.get("action") or "")
        verdict = ACTION_TO_VERDICT.get(action)
        entry = pending.get(ident) if isinstance(pending, dict) else None
        if not entry or not verdict:
            counters["unknown"] += 1
            reason = ("unsupported action '{}'".format(action) if not verdict
                      else "unknown id '{}' (expired after {} days, or never offered)".format(
                          ident, PENDING_RETENTION_DAYS))
            if not retain_rejected(reason, decision, event, raw):
                counters["retain_failures"] += 1
            continue
        if _already_applied(event):
            counters["duplicates"] += 1
            continue
        actor = str(decision.get("from") or decision.get("actor") or "")
        code, payload = radar_server.record_verdict(
            {"key": entry["key"], "name": entry.get("name") or entry["key"], "verdict": verdict},
            source="telegram", source_event=event, actor=actor,
        )
        if int(code) == 200:
            counters["applied"] += 1
        else:
            counters["rejected"] += 1
            reason = "server refused ({}): {}".format(code, payload.get("error", ""))[:300]
            if not retain_rejected(reason, decision, event, raw):
                counters["retain_failures"] += 1
    counters["processed_ids"] = processed_ids
    return counters


def apply_decisions(decisions: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Frozen-surface wrapper: {'applied', 'unknown', 'rejected'} exactly.

    Rejected/unknown decisions are still durably retained as a side effect;
    the richer counters live on ``_apply_decisions`` for ``pull()``.
    """
    records = [{"decision": d, "event_id": event_id_for(d), "raw": None, "parse_error": ""}
               for d in decisions]
    detailed = _apply_decisions(records)
    return {k: detailed[k] for k in ("applied", "unknown", "rejected")}


def load_checkpoint() -> Dict[str, Any]:
    """Current checkpoint; legacy {'offset': N} files stay readable, corrupt
    ones are preserved aside and treated as a restart from zero (idempotent)."""
    state = load_json(OFFSET_PATH, None)
    if state is None and OFFSET_PATH.exists():
        quarantined = _quarantine_corrupt(OFFSET_PATH)
        if quarantined:
            print("telegram_decisions: telegram-offset.json was corrupt; preserved at {}".format(
                quarantined), file=sys.stderr)
        state = {}
    if not isinstance(state, dict):
        state = {}
    identity = state.get("log_identity")
    return {
        "offset": int(state.get("offset") or 0),
        "log_identity": identity if isinstance(identity, dict) else None,
        "consumed_ids": [str(i) for i in state.get("consumed_ids") or [] if i],
    }


def pull() -> Dict[str, Any]:
    checkpoint = load_checkpoint()
    outcome = fetch_remote_v2(checkpoint["offset"], checkpoint["log_identity"])
    if outcome["error"]:
        return {"pulled": 0, "error": outcome["error"], "offset": checkpoint["offset"],
                "absent": outcome["absent"]}

    # Journal what arrived BEFORE applying or advancing: a crash after this
    # point can replay safely (idempotent), but can never lose the events.
    journal_error = ""
    for record in outcome["records"]:
        entry = {"at": utc_now(), "event_id": record["event_id"],
                 "decision": record["decision"]}
        if record["parse_error"]:
            entry["parse_error"] = record["parse_error"]
            entry["raw_base64"] = base64.b64encode(record["raw"]).decode("ascii")
        try:
            _append_jsonl(_received_path(), entry)
        except OSError as exc:
            journal_error = "could not journal received events: {}".format(exc)[:150]
            break
    if journal_error:
        # Do not consume what we could not journal; next pull re-reads it.
        return {"pulled": 0, "error": journal_error, "offset": checkpoint["offset"]}

    result = _apply_decisions(outcome["records"], tuple(checkpoint["consumed_ids"]))
    if result.pop("retain_failures", 0):
        # A reject we failed to retain durably must not be skipped past.
        return {"pulled": len(outcome["records"]), "error": "could not retain a rejected decision; checkpoint not advanced",
                "offset": checkpoint["offset"], **{k: result[k] for k in ("applied", "unknown", "rejected")}}
    consumed_ids = (checkpoint["consumed_ids"] + result.pop("processed_ids"))[-CONSUMED_IDS_KEEP:]
    write_json(OFFSET_PATH, {
        "version": CHECKPOINT_VERSION,
        "offset": outcome["new_offset"],
        "log_identity": outcome["identity"],
        "consumed_ids": consumed_ids,
        "checked_at": utc_now(),
    })
    result.update({"pulled": len(outcome["records"]), "offset": outcome["new_offset"],
                   "reread": outcome["reread"]})
    return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pull", action="store_true", help="collect and apply taps from the VPS")
    parser.add_argument("--self-test", action="store_true", help="check payload sizes and id stability")
    args = parser.parse_args(argv)

    if args.self_test:
        longest = "github.com/aldinokemal/go-whatsapp-web-multidevice"
        payload = callback_data(longest, "y")
        print(json.dumps({
            "longest_key": longest,
            "callback_data": payload,
            "bytes": len(payload.encode("utf-8")),
            "limit": CALLBACK_LIMIT,
            "id_is_stable": short_id(longest) == short_id(longest),
        }, indent=2))
        return 0

    print(json.dumps(pull() if args.pull else {"error": "choose --pull or --self-test"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
