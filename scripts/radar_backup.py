#!/usr/bin/env python3
"""Consistent backup and safe restore for the Group Radar recovery set.

Why this exists (audit A12): the ledger, the authored decisions and the
sync checkpoints lived with no demonstrated recovery path. This tool captures
the C3 storage-manifest recovery set as one logical revision and restores it
into a NEW directory with scanning and notifications disabled.

Consistency protocol (contract C3, revision c1):
* take `worker.lock` first (bounded wait) — the scanner and every SQLite
  writer holds it for their whole run, so holding it quiesces the ledger;
* snapshot SQLite with `VACUUM INTO` (the supported backup path — never a
  file copy of a live database, never the -wal/-shm sidecars);
* copy each JSON store under its `<name>.json.lock` flock sentinel in the
  frozen lock order (verdicts → outcomes → profile → checkpoints), then
  re-hash the originals after all copies: the viewer writes decisions
  WITHOUT worker.lock, so any file that changed mid-window is re-copied
  (bounded retries) or the backup fails honestly;
* stage everything in a hidden directory and atomically rename into place —
  a failed backup can never look like a recovery point or damage the
  previous one.

Restore defaults: NEW/empty target only, `restore-state.json` marks scanning
and notifications disabled (group_filter_loop refuses to run until an
operator removes it), no schedule is installed, nothing is written to the
live tree. Credentials/cookies are never part of the recovery set.

Limits stated plainly: a backup under the default destination lives on the
same disk as the source — that is crash/corruption recovery, NOT disaster
recovery. Off-device/encrypted destinations are an explicit operator
enrollment step (see `--dest`).
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional


def _readonly_uri(path: Path) -> str:
    """URI form so spaces/#/? in paths cannot corrupt the mode=ro query."""
    return "file:{}?mode=ro".format(urllib.parse.quote(str(path)))

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
import run_health  # noqa: E402

os.umask(0o077)

DEFAULT_ROOT = SCRIPTS_DIR.parent
MANIFEST_NAME = "backup-manifest.json"
MANIFEST_VERSION = 1
CONTRACT = "C3/c1"
LATEST_NAME = "LATEST"
RESTORE_README = "RESTORE-README.md"
WORKER_LOCK_WAIT_SECONDS = 30
COPY_RETRIES = 3
SQLITE_TABLES = ("metadata", "messages", "senders", "resources",
                 "message_resources", "runs")

# The C3 c1 recovery set, in the frozen lock order, plus lane-03 operational
# state (additive). Everything else under data/ is regenerable or excluded.
RECOVERY_SET: List[Dict[str, Any]] = [
    {"path": "data/group-monitor/group-monitor.sqlite3", "kind": "sqlite",
     "required": True},
    {"path": "config/verdicts.json", "kind": "authored-json", "required": False},
    {"path": "config/outcomes.json", "kind": "authored-json", "required": False},
    {"path": "config/group-filter-profile.json", "kind": "authored-json",
     "required": True},
    {"path": "data/group-monitor/telegram-offset.json", "kind": "checkpoint",
     "required": False},
    {"path": "data/group-monitor/pending-decisions.json", "kind": "checkpoint",
     "required": False},
    {"path": "data/group-monitor/autonomous-runs.jsonl", "kind": "append-journal",
     "required": False},
    # Lane 02 (02-pkg-002) decision-sync journals: rejects and received events
    # are the durable record behind the Telegram checkpoint, so a restore that
    # dropped them would lose decisions the checkpoint claims were consumed.
    {"path": "data/group-monitor/telegram-received.jsonl", "kind": "append-journal",
     "required": False},
    {"path": "data/group-monitor/telegram-rejected.jsonl", "kind": "append-journal",
     "required": False},
    # Lane 02 (02-pkg-001) bounded authored-document history: the documented
    # recovery path for a corrupt verdicts/outcomes/profile file.
    {"path": "config/_history/verdicts", "kind": "history-dir", "required": False},
    {"path": "config/_history/outcomes", "kind": "history-dir", "required": False},
    {"path": "config/_history/group-filter-profile", "kind": "history-dir",
     "required": False},
    {"path": "data/group-monitor/stage-health.json", "kind": "extra-state",
     "required": False},
    {"path": "data/group-monitor/backoff-state.json", "kind": "extra-state",
     "required": False},
    {"path": "data/group-monitor/health-events.jsonl", "kind": "extra-state",
     "required": False},
]
# Never allowed anywhere near a backup, restore, or manifest.
FORBIDDEN_NAMES = {"accounts.json", "dm_config.json", "cookies", ".env"}
# Transient sidecars: flock sentinels are empty and recreated on demand, and
# in-flight temp files are by definition not a committed revision (C3
# additions from 02-pkg-001).
EXCLUDED_SUFFIXES = (".lock", ".tmp")
MAX_HISTORY_FILES_PER_STORE = 20


class BackupError(RuntimeError):
    pass


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ensure_inside(root: Path, relative: str) -> Path:
    """Resolve a recovery-set member and refuse symlink escapes."""
    if Path(relative).name in FORBIDDEN_NAMES:
        raise BackupError("forbidden file in recovery set: " + relative)
    candidate = (root / relative).resolve()
    if not str(candidate).startswith(str(root.resolve()) + os.sep):
        raise BackupError("recovery member escapes the root: " + relative)
    return candidate


def _expand_recovery_set(root: Path) -> List[Dict[str, Any]]:
    """RECOVERY_SET with history directories expanded to concrete files.

    Keeping the manifest a flat file list means verify/restore need no special
    cases, and every captured byte is individually hashed.
    """
    expanded: List[Dict[str, Any]] = []
    for member in RECOVERY_SET:
        if member["kind"] != "history-dir":
            expanded.append(member)
            continue
        directory = root / member["path"]
        if not directory.is_dir():
            continue
        versions = sorted(
            (item for item in directory.iterdir()
             if item.is_file() and not item.name.endswith(EXCLUDED_SUFFIXES)),
            key=lambda item: item.name,
        )[-MAX_HISTORY_FILES_PER_STORE:]
        for version in versions:
            expanded.append({
                "path": "{}/{}".format(member["path"], version.name),
                "kind": "history-file",
                "required": False,
            })
    return expanded


def _hold_worker_lock(root: Path, wait_seconds: float):
    """Bounded-wait exclusive flock on the scanner's worker.lock."""
    lock_dir = root / "data" / "group-monitor"
    lock_dir.mkdir(parents=True, exist_ok=True)
    handle = (lock_dir / "worker.lock").open("a+")
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except BlockingIOError:
            if time.monotonic() >= deadline:
                handle.close()
                raise BackupError(
                    "scanner is running (worker.lock held); retry after the "
                    "current run or raise --wait-seconds"
                )
            time.sleep(0.5)


def _copy_locked_json(source: Path, target: Path) -> str:
    """Copy one JSON store under its flock sentinel; returns the copy's hash."""
    lock_path = source.with_suffix(source.suffix + ".lock")
    lock_path.touch(exist_ok=True)
    with lock_path.open() as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            data = source.read_bytes()
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    target.chmod(0o600)
    return hashlib.sha256(data).hexdigest()


def _copy_plain(source: Path, target: Path) -> str:
    data = source.read_bytes()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    target.chmod(0o600)
    return hashlib.sha256(data).hexdigest()


def _sqlite_snapshot(source_db: Path, target_db: Path) -> Dict[str, Any]:
    """Consistent SQLite snapshot via VACUUM INTO, then self-check the copy."""
    target_db.parent.mkdir(parents=True, exist_ok=True)
    if target_db.exists():
        raise BackupError("snapshot target already exists: " + str(target_db))
    conn = sqlite3.connect(str(source_db), timeout=30)
    try:
        counts = {
            table: conn.execute(
                "SELECT COUNT(*) FROM {}".format(table)
            ).fetchone()[0]
            for table in SQLITE_TABLES
        }
        conn.execute("VACUUM INTO ?", (str(target_db),))
    finally:
        conn.close()
    target_db.chmod(0o600)
    copy = sqlite3.connect(str(target_db), timeout=30)
    try:
        integrity = copy.execute("PRAGMA integrity_check").fetchone()[0]
        copy_counts = {
            table: copy.execute(
                "SELECT COUNT(*) FROM {}".format(table)
            ).fetchone()[0]
            for table in SQLITE_TABLES
        }
    finally:
        copy.close()
    if integrity != "ok":
        raise BackupError("snapshot failed integrity_check: " + integrity)
    if copy_counts != counts:
        raise BackupError(
            "snapshot row counts diverge from source under worker.lock: "
            "{} vs {}".format(copy_counts, counts)
        )
    return {"integrity": integrity, "counts": counts}


def create_backup(
    root: Path,
    dest: Optional[Path] = None,
    wait_seconds: float = WORKER_LOCK_WAIT_SECONDS,
    update_health: bool = True,
) -> Dict[str, Any]:
    root = Path(root).resolve()
    dest = Path(dest) if dest else root / "data" / "group-monitor" / "backups"
    dest.mkdir(parents=True, exist_ok=True)
    dest.chmod(0o700)
    # Microsecond suffix: two backups inside one second must not collide, and
    # the id stays lexicographically sortable by time.
    backup_id = "backup-" + dt.datetime.now(dt.timezone.utc).strftime(
        "%Y%m%d-%H%M%S-%f"
    )
    staging = dest / (".staging-" + backup_id)
    if staging.exists():
        shutil.rmtree(staging)
    data_dir = root / "data" / "group-monitor"

    previous_id = None
    latest_path = dest / LATEST_NAME
    if latest_path.exists():
        previous_id = latest_path.read_text(encoding="utf-8").strip() or None

    lock_handle = _hold_worker_lock(root, wait_seconds)
    files: List[Dict[str, Any]] = []
    retries_used = 0
    try:
        staging.mkdir(parents=True)
        staging.chmod(0o700)
        sqlite_report = None
        for member in _expand_recovery_set(root):
            source = _ensure_inside(root, member["path"])
            target = staging / member["path"]
            entry = {"path": member["path"], "kind": member["kind"],
                     "present": source.is_file()}
            if not source.is_file():
                if member["required"]:
                    raise BackupError("required store missing: " + member["path"])
                files.append(entry)
                continue
            if source.is_symlink():
                raise BackupError("refusing symlinked store: " + member["path"])
            if member["kind"] == "sqlite":
                sqlite_report = _sqlite_snapshot(source, target)
                entry["sha256"] = sha256_file(target)
            elif member["kind"] in ("authored-json", "checkpoint"):
                entry["sha256"] = _copy_locked_json(source, target)
            else:
                entry["sha256"] = _copy_plain(source, target)
            entry["bytes"] = target.stat().st_size
            files.append(entry)

        # Post-copy re-hash: the viewer writes decisions without worker.lock,
        # so any store that changed during the window is re-copied. A store
        # that will not settle within the retry budget fails the backup —
        # honestly — rather than shipping a torn logical revision.
        for entry in files:
            if not entry.get("present") or entry["kind"] == "sqlite":
                continue
            source = root / entry["path"]
            for _attempt in range(COPY_RETRIES):
                if not source.is_file():
                    if entry["kind"] == "history-file":
                        # 02's authored history is bounded and rotates: a
                        # version pruned mid-backup is normal, and it is not
                        # needed for restore (the main document is
                        # authoritative). Drop it instead of failing.
                        (staging / entry["path"]).unlink(missing_ok=True)
                        entry["present"] = False
                        entry["note"] = "rotated out during backup"
                        entry.pop("sha256", None)
                        entry.pop("bytes", None)
                        break
                    raise BackupError(
                        "store disappeared during backup: " + entry["path"]
                    )
                current = sha256_file(source)
                if current == entry["sha256"]:
                    break
                retries_used += 1
                try:
                    if entry["kind"] in ("authored-json", "checkpoint"):
                        entry["sha256"] = _copy_locked_json(
                            source, staging / entry["path"])
                    else:
                        entry["sha256"] = _copy_plain(source, staging / entry["path"])
                except FileNotFoundError:
                    continue  # re-evaluated as a vanished source next iteration
                entry["bytes"] = (staging / entry["path"]).stat().st_size
            else:
                raise BackupError(
                    "store kept changing during backup: " + entry["path"]
                )

        document_revisions = {}
        for entry in files:
            if entry["kind"] != "authored-json" or not entry.get("present"):
                continue
            try:
                document = json.loads(
                    (staging / entry["path"]).read_text(encoding="utf-8")
                )
                document_revisions[entry["path"]] = (
                    document.get("revision", 0) if isinstance(document, dict) else None
                )
            except (json.JSONDecodeError, OSError):
                document_revisions[entry["path"]] = None

        logical_revision = hashlib.sha256(
            "\n".join(
                "{}:{}".format(entry["path"], entry.get("sha256", "absent"))
                for entry in sorted(files, key=lambda item: item["path"])
            ).encode("utf-8")
        ).hexdigest()
        manifest = {
            "manifest_version": MANIFEST_VERSION,
            "contract": CONTRACT,
            "backup_id": backup_id,
            "created_at": utc_now_iso(),
            "tool": "radar_backup.py",
            "source_root": str(root),
            "logical_revision": logical_revision,
            "document_revisions": document_revisions,
            "files": files,
            "sqlite": sqlite_report,
            "consistency": {
                "method": "worker.lock + VACUUM INTO + locked JSON copies + "
                          "post-copy re-hash",
                "worker_lock_held": True,
                "rehash_retries_used": retries_used,
            },
            "previous_backup_id": previous_id,
            "offsite": False,
            "credentials_excluded": True,
            "disclaimers": [
                "same-disk snapshot: crash/corruption recovery only, not "
                "disaster recovery; enroll an off-device encrypted "
                "destination for that",
            ],
        }
        manifest_path = staging / MANIFEST_NAME
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        manifest_path.chmod(0o600)
        final_dir = dest / backup_id
        if final_dir.exists():
            raise BackupError("backup id collision: " + backup_id)
        os.rename(staging, final_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        if update_health:
            _record_backup_health(data_dir, ok=False,
                                  detail=str(sys.exc_info()[1]))
        raise
    finally:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        lock_handle.close()

    # LATEST moves only after the backup directory is fully in place, so a
    # failure can never erase or repoint the previous recovery point.
    tmp_latest = dest / (LATEST_NAME + ".tmp")
    tmp_latest.write_text(backup_id + "\n", encoding="utf-8")
    os.replace(tmp_latest, latest_path)
    if update_health:
        _record_backup_health(data_dir, ok=True, detail=backup_id)
    return {"backup_id": backup_id, "path": str(dest / backup_id),
            "logical_revision": logical_revision,
            "files": len([f for f in files if f.get("present")]),
            "previous_backup_id": previous_id}


def _record_backup_health(data_dir: Path, ok: bool, detail: str) -> None:
    try:
        run_health.StageHealthStore(data_dir).update_stage(
            "backup", "ok" if ok else "failed", detail=detail
        )
        if not ok:
            run_health.prepare_alert(data_dir, "backup_failed", "backup", detail)
    except Exception:  # noqa: BLE001 - health recording never blocks backup
        pass


def verify_backup(backup_dir: Path) -> Dict[str, Any]:
    backup_dir = Path(backup_dir)
    problems: List[str] = []
    manifest: Dict[str, Any] = {}
    try:
        manifest = json.loads(
            (backup_dir / MANIFEST_NAME).read_text(encoding="utf-8")
        )
    except FileNotFoundError:
        problems.append("backup manifest missing")
    except (json.JSONDecodeError, OSError) as exc:
        problems.append("backup manifest unreadable: {}".format(exc))
    if manifest:
        if manifest.get("manifest_version") != MANIFEST_VERSION:
            problems.append(
                "unsupported manifest_version: {!r}".format(
                    manifest.get("manifest_version")
                )
            )
        for entry in manifest.get("files", []):
            if not entry.get("present"):
                continue
            member = backup_dir / entry["path"]
            if not member.is_file():
                problems.append("missing member: " + entry["path"])
                continue
            actual = sha256_file(member)
            if actual != entry.get("sha256"):
                problems.append("hash mismatch: " + entry["path"])
        recomputed = hashlib.sha256(
            "\n".join(
                "{}:{}".format(entry["path"], entry.get("sha256", "absent"))
                for entry in sorted(
                    manifest.get("files", []), key=lambda item: item["path"]
                )
            ).encode("utf-8")
        ).hexdigest()
        if manifest.get("logical_revision") != recomputed:
            problems.append("logical_revision mismatch")
        db_path = backup_dir / "data/group-monitor/group-monitor.sqlite3"
        if db_path.is_file():
            conn = sqlite3.connect(_readonly_uri(db_path), uri=True)
            try:
                integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
                if integrity != "ok":
                    problems.append("sqlite integrity: " + integrity)
                expected_counts = (manifest.get("sqlite") or {}).get("counts") or {}
                for table, expected in expected_counts.items():
                    actual = conn.execute(
                        "SELECT COUNT(*) FROM {}".format(table)
                    ).fetchone()[0]
                    if actual != expected:
                        problems.append(
                            "count mismatch {}: {} != {}".format(
                                table, actual, expected
                            )
                        )
            finally:
                conn.close()
    return {"pass": not problems, "problems": problems, "manifest": manifest}


def restore_backup(
    backup_dir: Path, target: Path, verify_only: bool = False
) -> Dict[str, Any]:
    backup_dir = Path(backup_dir)
    verification = verify_backup(backup_dir)
    if not verification["pass"]:
        raise BackupError(
            "refusing restore, backup failed verification: "
            + "; ".join(verification["problems"])
        )
    if verify_only:
        return {"verified": True, "restored": False,
                "backup_id": verification["manifest"].get("backup_id")}
    target = Path(target)
    if target.exists() and any(target.iterdir()):
        raise BackupError(
            "restore target must be a new or empty directory: " + str(target)
        )
    manifest = verification["manifest"]
    target.mkdir(parents=True, exist_ok=True)
    target.chmod(0o700)
    restored = []
    for entry in manifest.get("files", []):
        if not entry.get("present"):
            continue
        source = backup_dir / entry["path"]
        destination = target / entry["path"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        destination.chmod(0o600)
        restored.append(entry["path"])
    for parent in {str(Path(path).parent) for path in restored}:
        directory = target / parent
        while directory != target:
            directory.chmod(0o700)
            directory = directory.parent
    data_dir = target / "data" / "group-monitor"
    data_dir.mkdir(parents=True, exist_ok=True)
    restore_state = {
        "restored_at": utc_now_iso(),
        "source_backup": manifest.get("backup_id"),
        "source_logical_revision": manifest.get("logical_revision"),
        "notifications": "disabled",
        "scanning": "disabled",
    }
    state_path = data_dir / run_health.RESTORE_STATE_FILE
    state_path.write_text(
        json.dumps(restore_state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    state_path.chmod(0o600)
    (target / RESTORE_README).write_text(
        "# Restored Group Radar data — REVIEW BEFORE USE\n\n"
        "Restored from backup `{backup}` (logical revision `{rev}`) at {at}.\n\n"
        "* Scanning and notifications are DISABLED: `data/group-monitor/{marker}`"
        " makes group_filter_loop refuse to run. Delete that file only after"
        " reviewing this tree.\n"
        "* No cron/launchd schedule was installed by this restore.\n"
        "* Credentials/cookies were never part of the backup; reauthentication"
        " is a separate human step.\n"
        "* Exports (dashboard.html, status.json, CSV/JSONL) are regenerable:"
        " run the exporter after review.\n".format(
            backup=manifest.get("backup_id"),
            rev=(manifest.get("logical_revision") or "")[:16],
            at=restore_state["restored_at"],
            marker=run_health.RESTORE_STATE_FILE,
        ),
        encoding="utf-8",
    )
    post = verify_restored_tree(target, manifest)
    if not post["pass"]:
        raise BackupError(
            "restore self-check failed: " + "; ".join(post["problems"])
        )
    return {"verified": True, "restored": True,
            "backup_id": manifest.get("backup_id"),
            "target": str(target), "files": len(restored),
            "post_check": post}


def verify_restored_tree(target: Path, manifest: Dict[str, Any]) -> Dict[str, Any]:
    problems: List[str] = []
    for entry in manifest.get("files", []):
        if not entry.get("present"):
            continue
        member = Path(target) / entry["path"]
        if not member.is_file():
            problems.append("missing after restore: " + entry["path"])
        elif sha256_file(member) != entry.get("sha256"):
            problems.append("hash mismatch after restore: " + entry["path"])
    db_path = Path(target) / "data/group-monitor/group-monitor.sqlite3"
    counts: Dict[str, int] = {}
    if db_path.is_file():
        conn = sqlite3.connect(_readonly_uri(db_path), uri=True)
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                problems.append("restored sqlite integrity: " + integrity)
            for table in SQLITE_TABLES:
                counts[table] = conn.execute(
                    "SELECT COUNT(*) FROM {}".format(table)
                ).fetchone()[0]
            expected = (manifest.get("sqlite") or {}).get("counts") or {}
            if expected and counts != expected:
                problems.append(
                    "restored counts diverge: {} != {}".format(counts, expected)
                )
        finally:
            conn.close()
    return {"pass": not problems, "problems": problems, "counts": counts}


def restore_check(backup_dir: Path, scratch: Optional[Path] = None) -> Dict[str, Any]:
    """Prove the backup restores: verify + full restore into a throwaway dir."""
    import tempfile

    with tempfile.TemporaryDirectory(dir=str(scratch) if scratch else None) as tmp:
        result = restore_backup(Path(backup_dir), Path(tmp) / "restore-check")
    return {"pass": True, "backup_id": result["backup_id"],
            "files": result["files"], "counts": result["post_check"]["counts"]}


def prune_backups(dest: Path, keep: int, yes_delete: bool = False) -> Dict[str, Any]:
    dest = Path(dest)
    if keep < 1:
        raise BackupError("keep must be >= 1; retention never deletes everything")
    backups = sorted(
        entry.name for entry in dest.iterdir()
        if entry.is_dir() and entry.name.startswith("backup-")
    )
    latest = None
    latest_path = dest / LATEST_NAME
    if latest_path.exists():
        latest = latest_path.read_text(encoding="utf-8").strip()
    doomed = [name for name in backups[:-keep] if name != latest]
    if not yes_delete:
        return {"deleted": [], "would_delete": doomed, "kept": backups[-keep:],
                "note": "dry run; pass --yes-delete to actually delete"}
    for name in doomed:
        shutil.rmtree(dest / name)
    return {"deleted": doomed, "kept": [n for n in backups if n not in doomed]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(DEFAULT_ROOT),
                        help="project root holding config/ and data/")
    sub = parser.add_subparsers(dest="command", required=True)

    create_parser = sub.add_parser("create", help="create a consistent backup")
    create_parser.add_argument("--dest", default=None,
                               help="backup destination directory "
                                    "(default: data/group-monitor/backups; "
                                    "same-disk = not disaster recovery)")
    create_parser.add_argument("--wait-seconds", type=float,
                               default=WORKER_LOCK_WAIT_SECONDS)

    verify_parser = sub.add_parser("verify", help="verify a backup in place")
    verify_parser.add_argument("backup_dir")

    restore_parser = sub.add_parser(
        "restore", help="restore into a NEW directory (scanning disabled)"
    )
    restore_parser.add_argument("backup_dir")
    restore_parser.add_argument("--to", required=True,
                                help="new/empty target directory")
    restore_parser.add_argument("--verify-only", action="store_true")

    check_parser = sub.add_parser(
        "check", help="verify + trial restore into a throwaway directory"
    )
    check_parser.add_argument("backup_dir")

    prune_parser = sub.add_parser(
        "prune", help="delete old backups (refuses without --yes-delete)"
    )
    prune_parser.add_argument("--dest", required=True)
    prune_parser.add_argument("--keep", type=int, default=5)
    prune_parser.add_argument("--yes-delete", action="store_true")

    args = parser.parse_args()
    try:
        if args.command == "create":
            result = create_backup(
                Path(args.root),
                dest=Path(args.dest) if args.dest else None,
                wait_seconds=args.wait_seconds,
            )
        elif args.command == "verify":
            result = verify_backup(Path(args.backup_dir))
            result.pop("manifest", None)
        elif args.command == "restore":
            result = restore_backup(
                Path(args.backup_dir), Path(args.to),
                verify_only=args.verify_only,
            )
        elif args.command == "check":
            result = restore_check(Path(args.backup_dir))
        else:
            result = prune_backups(Path(args.dest), args.keep, args.yes_delete)
    except BackupError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    ok = bool(result.get("pass", True))
    print(json.dumps({"ok": ok, **result}, ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
