#!/usr/bin/env python3
"""Truthful per-stage health, bounded persisted backoff, and health events.

Why this exists (audit A03): the viewer's /api/health said "ok" whenever the
dashboard file existed and status.json was fresh — a reachable page, not a
successful pipeline. This module gives the loop, the viewer and the backup
tool one shared, durable record of what each stage actually did last, plus a
persisted backoff/circuit state so failures neither storm nor hide.

Contract: C4 (provider 03 → consumer 06), revision c1. The additive health
block composed here is frozen in contracts/fixtures/c4-health-extended.json:
top-level `stages`, `last_run_outcome`, `last_run_at`,
`last_semantic_success_at`, `auth_required`, `backlog_age_seconds`,
`backoff {active, until, reason}`. Stage names and states are the c1 enums.

Design rules encoded here, not just documented:
* An old passing gate must never overwrite a newer failure: stage updates are
  monotonic on their observation timestamp, and read-time staleness overlays
  only ever downgrade, never upgrade.
* Viewer liveness stays independent: nothing here touches the frozen `ok`.
* Auth circuits never self-close: an operator must `reset` or arm exactly one
  `probe`. Non-auth circuits half-open on a capped schedule, so a transient
  outage can always recover without a human and can never retry-storm.
* No credentials or private content: every detail string is scrubbed and
  truncated before it is persisted or served.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional


STAGE_NAMES = (
    "capture",
    "hydration",
    "semantic_review",
    "decision_sync",
    "notification",
    "backup",
    "export",
)
STAGE_STATES = ("ok", "degraded", "failed", "auth_required", "recovering", "unknown")

# Failure classes drive backoff policy. Classes mirror what the run journal
# has actually recorded historically (auth 39, dns 15, hydration 9, timeout 1
# of 60 errors over 365 runs measured 2026-09-06).
FAILURE_CLASSES = ("auth", "rate_limit", "dns", "network", "timeout", "transient",
                   "capacity")
# Classes that must NEVER open a circuit. `capacity` means "there was more work
# than one bounded run could do" (lane 01 reports a page cap as "capture did not
# reach the durable checkpoint"). Skipping capture would remove the only way the
# backlog ever drains, so it is counted and shown, never circuit-broken.
NO_CIRCUIT_CLASSES = frozenset({"capacity"})
BACKOFF_BASE_SECONDS = 30 * 60  # one cron slot
BACKOFF_CAP_SECONDS = {
    "auth": 6 * 3600,
    "rate_limit": 4 * 3600,
    "dns": 2 * 3600,
    "network": 2 * 3600,
    "timeout": 2 * 3600,
    "transient": 2 * 3600,
    "capacity": 2 * 3600,  # unused: capacity never opens a circuit
}
# Consecutive classified failures before the circuit opens. Below the
# threshold the normal cron cadence is the only retry pacing.
OPEN_THRESHOLD = {"auth": 2}
OPEN_THRESHOLD_DEFAULT = 3
ALERT_MIN_INTERVAL_SECONDS = 6 * 3600  # per (domain, kind) rate limit
MAX_DETAIL_CHARS = 300
JOURNAL_TAIL_BYTES = 96 * 1024
STALE_AFTER_SECONDS = 90 * 60  # matches radar_server.STALE_AFTER_SECONDS

STAGE_FILE = "stage-health.json"
BACKOFF_FILE = "backoff-state.json"
EVENTS_FILE = "health-events.jsonl"
ALERTS_FILE = "health-alerts.jsonl"
RESTORE_STATE_FILE = "restore-state.json"

_SECRET_PATTERNS = [
    re.compile(r"(?i)bearer\s+[^\s\"']+"),
    re.compile(r"(?i)(token|key|secret|password|authorization|cookie)\s*[=:]\s*[^\s\"'&;]+"),
    re.compile(r"\b(sk|pk|ghp|gho|ghs|xoxb|xoxp|AKIA)[A-Za-z0-9_\-]{8,}\b"),
    re.compile(r"\b[A-Fa-f0-9]{32,}\b"),
    re.compile(r"\b[A-Za-z0-9+/=_\-]{40,}\b"),
]


def utc_now_iso(now: Optional[dt.datetime] = None) -> str:
    now = now or dt.datetime.now(dt.timezone.utc)
    return now.isoformat(timespec="seconds")


def parse_iso(value: Any) -> Optional[dt.datetime]:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def scrub_detail(text: Any) -> str:
    """Make an error string safe to persist/serve: no secrets, bounded size."""
    cleaned = str(text or "").replace("\n", " ").strip()
    for pattern in _SECRET_PATTERNS:
        cleaned = pattern.sub("[redacted]", cleaned)
    return cleaned[:MAX_DETAIL_CHARS]


def classify_failure(text: Any) -> str:
    """Map an exception/stderr string to a backoff failure class."""
    lowered = str(text or "").lower()
    if any(marker in lowered for marker in (
        "401", "unauthorized", "forbidden", "sign in", "signed out", "login",
        "authentication", "not logged in", "credential", "session expired",
    )):
        return "auth"
    if any(marker in lowered for marker in ("429", "rate limit", "too many requests")):
        return "rate_limit"
    if any(marker in lowered for marker in (
        "page cap", "did not reach the durable checkpoint",
        "before durable checkpoint",
    )):
        return "capacity"
    if any(marker in lowered for marker in (
        "dns", "getaddrinfo", "nodename nor servname", "name resolution",
        "no address associated",
    )):
        return "dns"
    if any(marker in lowered for marker in ("timed out", "timeout", "deadline")):
        return "timeout"
    if any(marker in lowered for marker in (
        "connection", "network is unreachable", "unreachable", "reset by peer",
        "broken pipe", "temporary failure",
    )):
        return "network"
    return "transient"


def _atomic_write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


class _LockedJsonFile:
    """flock-per-write JSON document, same sentinel convention as json_filelock."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    def read(self) -> Dict[str, Any]:
        try:
            with self.path.open(encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def mutate(self, fn) -> Dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path.touch(exist_ok=True)
        with self.lock_path.open() as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                data = self.read()
                data = fn(data)
                _atomic_write_private(
                    self.path, json.dumps(data, ensure_ascii=False, indent=2) + "\n"
                )
                return data
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def append_event(data_dir: Path, event: Dict[str, Any]) -> None:
    """Durable, append-only record of every failure/recovery transition."""
    path = Path(data_dir) / EVENTS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"at": event.get("at") or utc_now_iso(), **event}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass


class StageHealthStore:
    """Durable per-stage observations plus run-level summary fields."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.doc = _LockedJsonFile(self.data_dir / STAGE_FILE)

    def update_stage(
        self,
        name: str,
        state: str,
        at: Optional[str] = None,
        detail: Optional[str] = None,
        emit_event: bool = True,
    ) -> None:
        if name not in STAGE_NAMES:
            raise ValueError("unknown stage: {}".format(name))
        if state not in STAGE_STATES:
            raise ValueError("unknown stage state: {}".format(state))
        at = at or utc_now_iso()
        detail = scrub_detail(detail) if detail else None
        previous_state = {"value": None}

        def apply(data: Dict[str, Any]) -> Dict[str, Any]:
            stages = data.setdefault("stages", {})
            current = stages.get(name) or {}
            previous_state["value"] = current.get("state")
            current_at = parse_iso(current.get("at"))
            update_at = parse_iso(at)
            # Monotonic guard: an observation older than what we already have
            # must not overwrite it — an old passing gate can never replace a
            # newer failure (or vice versa).
            if current_at and update_at and update_at < current_at:
                return data
            record = {"state": state, "at": at}
            if detail:
                record["detail"] = detail
            stages[name] = record
            data["version"] = 1
            data["updated_at"] = utc_now_iso()
            return data

        self.doc.mutate(apply)
        if emit_event and previous_state["value"] != state:
            append_event(
                self.data_dir,
                {
                    "at": at,
                    "kind": "stage_transition",
                    "stage": name,
                    "from": previous_state["value"] or "unknown",
                    "to": state,
                    **({"detail": detail} if detail else {}),
                },
            )

    def record_run_result(
        self,
        outcome: str,
        started_at: str,
        finished_at: str,
        semantic_success_at: Optional[str] = None,
        backlog_oldest_pending_at: Optional[str] = "__unchanged__",
    ) -> None:
        def apply(data: Dict[str, Any]) -> Dict[str, Any]:
            existing_finished = parse_iso(data.get("last_run_at"))
            update_finished = parse_iso(finished_at)
            if existing_finished and update_finished and update_finished < existing_finished:
                return data  # never let an older run overwrite a newer one
            data["version"] = 1
            data["last_run_outcome"] = outcome
            data["last_run_started_at"] = started_at
            data["last_run_at"] = finished_at
            if semantic_success_at:
                data["last_semantic_success_at"] = semantic_success_at
            if backlog_oldest_pending_at != "__unchanged__":
                data["backlog_oldest_pending_at"] = backlog_oldest_pending_at
            data["updated_at"] = utc_now_iso()
            return data

        self.doc.mutate(apply)

    def read(self) -> Dict[str, Any]:
        return self.doc.read()


class BackoffStore:
    """Persisted bounded backoff/circuit state per failure domain.

    Domains are pipeline units that hit an external dependency and are worth
    skipping while broken: `capture` (X via bird) and `semantic_review`
    (Codex). One cheap bounded call per run (Telegram pull, enrichment) is
    already storm-proof by construction and gets stage states only.
    """

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.doc = _LockedJsonFile(self.data_dir / BACKOFF_FILE)

    # -- queries ---------------------------------------------------------
    def domain(self, name: str) -> Dict[str, Any]:
        return (self.doc.read().get("domains") or {}).get(name) or {}

    def check(self, name: str, now: Optional[dt.datetime] = None) -> Dict[str, Any]:
        """May this domain run now? Returns {allowed, probe, until, reason, state}."""
        now = now or dt.datetime.now(dt.timezone.utc)
        entry = self.domain(name)
        state = entry.get("state", "closed")
        failure_class = entry.get("failure_class")
        if state == "closed":
            return {"allowed": True, "probe": False, "until": None, "reason": None,
                    "state": "closed", "failure_class": None}
        until = parse_iso(entry.get("until"))
        reason = entry.get("reason")
        if state == "half_open":
            # A probe was armed (auth: by the operator; others: by schedule)
            # and has not resolved yet — allow exactly this one attempt.
            return {"allowed": True, "probe": True, "until": entry.get("until"),
                    "reason": reason, "state": "half_open",
                    "failure_class": failure_class}
        # state == "open"
        if failure_class == "auth":
            # Auth never self-closes: reauthentication is a human gate.
            return {"allowed": False, "probe": False, "until": entry.get("until"),
                    "reason": reason, "state": "open", "failure_class": failure_class}
        if until and now < until:
            return {"allowed": False, "probe": False, "until": entry.get("until"),
                    "reason": reason, "state": "open", "failure_class": failure_class}
        return {"allowed": True, "probe": True, "until": entry.get("until"),
                "reason": reason, "state": "open", "failure_class": failure_class}

    # -- transitions -----------------------------------------------------
    def record_failure(
        self, name: str, failure_class: str, detail: Any = None,
        now: Optional[dt.datetime] = None,
    ) -> Dict[str, Any]:
        if failure_class not in FAILURE_CLASSES:
            failure_class = "transient"
        now = now or dt.datetime.now(dt.timezone.utc)
        now_iso = utc_now_iso(now)
        detail_clean = scrub_detail(detail) if detail else None
        result: Dict[str, Any] = {}

        def apply(data: Dict[str, Any]) -> Dict[str, Any]:
            domains = data.setdefault("domains", {})
            entry = domains.get(name) or {}
            same_class = entry.get("failure_class") == failure_class
            consecutive = int(entry.get("consecutive_failures") or 0) + 1 if same_class else 1
            threshold = OPEN_THRESHOLD.get(failure_class, OPEN_THRESHOLD_DEFAULT)
            cap = BACKOFF_CAP_SECONDS[failure_class]
            was_open = entry.get("state") in ("open", "half_open")
            opens = was_open or consecutive >= threshold
            if failure_class in NO_CIRCUIT_CLASSES:
                opens = False
            delay = min(BACKOFF_BASE_SECONDS * (2 ** max(0, consecutive - threshold)), cap)
            if same_class and entry.get("first_failure_at"):
                first_failure_at = entry["first_failure_at"]
            else:
                first_failure_at = now_iso
            new_entry = {
                "failure_class": failure_class,
                "consecutive_failures": consecutive,
                "first_failure_at": first_failure_at,
                "last_failure_at": now_iso,
                "state": "open" if opens else "closed",
                "reason": "{} x{}".format(failure_class, consecutive)
                + (": " + detail_clean if detail_clean else ""),
            }
            if opens:
                new_entry["until"] = utc_now_iso(now + dt.timedelta(seconds=delay))
                new_entry["opened_at"] = entry.get("opened_at") or now_iso
            data["version"] = 1
            data["updated_at"] = now_iso
            domains[name] = new_entry
            result.update(new_entry)
            result["just_opened"] = opens and not was_open
            return data

        self.doc.mutate(apply)
        append_event(self.data_dir, {
            "at": now_iso, "kind": "backoff_failure", "domain": name,
            "failure_class": failure_class,
            "consecutive": result.get("consecutive_failures"),
            "state": result.get("state"),
            **({"until": result["until"]} if result.get("until") else {}),
            **({"detail": detail_clean} if detail_clean else {}),
        })
        return result

    def record_success(self, name: str, now: Optional[dt.datetime] = None) -> Dict[str, Any]:
        now_iso = utc_now_iso(now)
        outcome: Dict[str, Any] = {"recovered": False}

        def apply(data: Dict[str, Any]) -> Dict[str, Any]:
            domains = data.setdefault("domains", {})
            entry = domains.get(name) or {}
            if entry.get("state") in ("open", "half_open") or entry.get("consecutive_failures"):
                outcome["recovered"] = entry.get("state") in ("open", "half_open")
            domains[name] = {
                "state": "closed",
                "consecutive_failures": 0,
                "last_success_at": now_iso,
                **({"recovered_at": now_iso} if outcome["recovered"] else {}),
            }
            data["version"] = 1
            data["updated_at"] = now_iso
            return data

        self.doc.mutate(apply)
        if outcome["recovered"]:
            append_event(self.data_dir, {
                "at": now_iso, "kind": "backoff_recovered", "domain": name,
            })
        return outcome

    def arm_probe(self, name: str, now: Optional[dt.datetime] = None) -> bool:
        """Arm exactly one attempt on an open circuit (operator action for auth)."""
        now_iso = utc_now_iso(now)
        armed = {"value": False}

        def apply(data: Dict[str, Any]) -> Dict[str, Any]:
            domains = data.setdefault("domains", {})
            entry = domains.get(name) or {}
            if entry.get("state") == "open":
                entry["state"] = "half_open"
                entry["probe_armed_at"] = now_iso
                domains[name] = entry
                data["updated_at"] = now_iso
                armed["value"] = True
            return data

        self.doc.mutate(apply)
        if armed["value"]:
            append_event(self.data_dir, {
                "at": now_iso, "kind": "probe_armed", "domain": name, "by": "operator",
            })
        return armed["value"]

    def reset(self, name: str, reason: str = "", now: Optional[dt.datetime] = None) -> None:
        """Explicit operator reset: close the circuit immediately."""
        now_iso = utc_now_iso(now)

        def apply(data: Dict[str, Any]) -> Dict[str, Any]:
            domains = data.setdefault("domains", {})
            domains[name] = {
                "state": "closed",
                "consecutive_failures": 0,
                "reset_at": now_iso,
                "recovered_at": now_iso,
            }
            data["version"] = 1
            data["updated_at"] = now_iso
            return data

        self.doc.mutate(apply)
        append_event(self.data_dir, {
            "at": now_iso, "kind": "operator_reset", "domain": name,
            **({"reason": scrub_detail(reason)} if reason else {}),
        })

    def read(self) -> Dict[str, Any]:
        return self.doc.read()


def prepare_alert(
    data_dir: Path,
    kind: str,
    domain: str,
    message: str,
    now: Optional[dt.datetime] = None,
    min_interval_seconds: int = ALERT_MIN_INTERVAL_SECONDS,
) -> bool:
    """Queue a deduplicated, rate-limited alert into the dry-run sink.

    This NEVER sends anything. It appends to health-alerts.jsonl; actual
    delivery stays wired to the existing telegram-notify integration and is a
    separate, explicitly-enabled step outside this run (worker lanes must not
    activate alerts). Returns True when the alert was queued, False when the
    per-(kind, domain) rate limit suppressed it.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    path = Path(data_dir) / ALERTS_FILE
    last_at: Optional[dt.datetime] = None
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("kind") == kind and record.get("domain") == domain:
                    candidate = parse_iso(record.get("at"))
                    if candidate and (last_at is None or candidate > last_at):
                        last_at = candidate
    except (FileNotFoundError, OSError):
        pass
    if last_at and (now - last_at).total_seconds() < min_interval_seconds:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "at": utc_now_iso(now), "kind": kind, "domain": domain,
            "message": scrub_detail(message), "delivery": "dry_run",
        }, ensure_ascii=False) + "\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return True


def restore_block(data_dir: Path) -> Optional[str]:
    """If this data dir was produced by a restore with scanning disabled,
    return a human-readable refusal message, else None."""
    try:
        with (Path(data_dir) / RESTORE_STATE_FILE).open(encoding="utf-8") as handle:
            state = json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if isinstance(state, dict) and state.get("scanning") == "disabled":
        return (
            "this data directory was restored from backup {} with scanning "
            "disabled; review it, then delete {} to re-enable runs".format(
                state.get("source_backup", "<unknown>"), RESTORE_STATE_FILE
            )
        )
    return None


def _read_journal_tail(data_dir: Path) -> Dict[str, Any]:
    """Last parseable run-journal entry, reading a bounded tail only."""
    path = Path(data_dir) / "autonomous-runs.jsonl"
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - JOURNAL_TAIL_BYTES))
            chunk = handle.read().decode("utf-8", errors="replace")
    except (FileNotFoundError, OSError):
        return {}
    for line in reversed(chunk.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            return entry
    return {}


def compose_health_extension(
    data_dir: Path, now: Optional[dt.datetime] = None
) -> Dict[str, Any]:
    """The C4 additive health block. Read-only; never raises for missing files."""
    data_dir = Path(data_dir)
    now = now or dt.datetime.now(dt.timezone.utc)
    stage_doc = StageHealthStore(data_dir).read()
    backoff_doc = BackoffStore(data_dir).read()
    journal = _read_journal_tail(data_dir)

    # Run summary: the stage store is authoritative, the journal is the
    # fallback; whichever observation is NEWER wins, never the older one.
    last_run_outcome = stage_doc.get("last_run_outcome")
    last_run_at = stage_doc.get("last_run_at")
    journal_finished = parse_iso(journal.get("finished_at"))
    store_finished = parse_iso(last_run_at)
    if journal_finished and (store_finished is None or journal_finished > store_finished):
        last_run_outcome = journal.get("outcome")
        last_run_at = journal.get("finished_at")

    stages: Dict[str, Any] = {}
    for name, record in (stage_doc.get("stages") or {}).items():
        if name in STAGE_NAMES and isinstance(record, dict):
            stages[name] = {
                "state": record.get("state", "unknown"),
                "at": record.get("at"),
                **({"detail": record["detail"]} if record.get("detail") else {}),
            }

    # Read-time staleness overlays: computed against `now`, so a newer failure
    # observation always beats an old pass, and an old pass degrades honestly.
    # Overlays only ever downgrade ok → degraded; they never upgrade.
    def observed_age(value: Any) -> Optional[float]:
        parsed = parse_iso(value)
        return None if parsed is None else (now - parsed).total_seconds()

    verification_at = None
    try:
        with (data_dir / "verification.json").open(encoding="utf-8") as handle:
            verification_at = json.load(handle).get("verified_at")
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    export_entry = stages.get("export")
    verification_age = observed_age(verification_at)
    if export_entry and export_entry.get("state") == "ok":
        export_age = observed_age(export_entry.get("at"))
        stale_ref = min(
            (age for age in (export_age, verification_age) if age is not None),
            default=None,
        )
        if stale_ref is not None and stale_ref > STALE_AFTER_SECONDS:
            export_entry["state"] = "degraded"
            export_entry["detail"] = "stale: last verified export is {}s old".format(
                int(stale_ref)
            )
    capture_entry = stages.get("capture")
    if capture_entry and capture_entry.get("state") == "ok":
        capture_age = observed_age(capture_entry.get("at"))
        if capture_age is not None and capture_age > STALE_AFTER_SECONDS:
            capture_entry["state"] = "degraded"
            capture_entry["detail"] = "stale: last capture checkpoint is {}s old".format(
                int(capture_age)
            )

    domains = backoff_doc.get("domains") or {}
    open_domains = {
        name: entry for name, entry in domains.items()
        if isinstance(entry, dict) and entry.get("state") in ("open", "half_open")
    }
    auth_required = any(
        entry.get("failure_class") == "auth" for entry in open_domains.values()
    )
    untils = [parse_iso(entry.get("until")) for entry in open_domains.values()]
    untils = [value for value in untils if value is not None]
    backoff_block = {
        "active": bool(open_domains),
        "until": utc_now_iso(max(untils)) if untils else None,
        "reason": "; ".join(
            sorted(
                "{}: {}".format(name, entry.get("reason") or entry.get("failure_class"))
                for name, entry in open_domains.items()
            )
        ) or None,
        # additive detail beyond the frozen trio, allowed at c1
        "domains": {
            name: {
                "state": entry.get("state"),
                "failure_class": entry.get("failure_class"),
                "consecutive_failures": entry.get("consecutive_failures"),
                "until": entry.get("until"),
            }
            for name, entry in open_domains.items()
        },
    }

    backlog_oldest = stage_doc.get("backlog_oldest_pending_at")
    backlog_age = observed_age(backlog_oldest)

    return {
        "stages": stages,
        "last_run_outcome": last_run_outcome,
        "last_run_at": last_run_at,
        "last_semantic_success_at": stage_doc.get("last_semantic_success_at"),
        "auth_required": auth_required,
        "backlog_age_seconds": None if backlog_age is None else max(0, int(backlog_age)),
        "backoff": backoff_block,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=None,
                        help="data directory (default: ../data/group-monitor)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="print the composed C4 health extension")
    reset_parser = sub.add_parser("reset", help="operator reset of a backoff circuit")
    reset_parser.add_argument("--domain", required=True)
    reset_parser.add_argument("--reason", default="")
    probe_parser = sub.add_parser(
        "probe", help="arm exactly one probe attempt on an open circuit"
    )
    probe_parser.add_argument("--domain", required=True)
    events_parser = sub.add_parser("events", help="print recent health events")
    events_parser.add_argument("--tail", type=int, default=20)
    args = parser.parse_args()

    data_dir = Path(args.data_dir) if args.data_dir else (
        Path(__file__).resolve().parent.parent / "data" / "group-monitor"
    )
    if args.command == "status":
        print(json.dumps(compose_health_extension(data_dir), ensure_ascii=False, indent=2))
        return 0
    if args.command == "reset":
        BackoffStore(data_dir).reset(args.domain, reason=args.reason)
        StageHealthStore(data_dir).update_stage(
            args.domain if args.domain in STAGE_NAMES else "semantic_review",
            "recovering", detail="operator reset",
        )
        print("circuit '{}' reset; next run may attempt it".format(args.domain))
        return 0
    if args.command == "probe":
        if BackoffStore(data_dir).arm_probe(args.domain):
            print("one probe armed for '{}'".format(args.domain))
            return 0
        print("no open circuit for '{}'; nothing to arm".format(args.domain))
        return 1
    if args.command == "events":
        path = data_dir / EVENTS_FILE
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (FileNotFoundError, OSError):
            lines = []
        for line in lines[-args.tail:]:
            print(line)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
