#!/usr/bin/env python3
"""Process-safe persistence for the radar's authored decision files.

Why this exists: ``config/verdicts.json`` and ``config/outcomes.json`` are the
only files a human authors through the radar, and they were protected only by
``threading.Lock`` — real for one process, imaginary across the dashboard
server, the Telegram pull cron and any agent session, which are separate OS
processes. Concurrent read-modify-write lost decisions, and a corrupt file was
silently replaced with an empty document on the next save.

The rules this module enforces, in one place, for every writer:

* **Inter-process exclusion.** A sidecar ``<name>.json.lock`` file is held
  with ``fcntl.flock`` around the whole read-validate-write cycle. The lock
  file is never deleted (deleting lock files is how flock races start). A
  crashed holder releases the lock automatically with its file descriptors.
  Waiting is bounded: a stuck writer produces a visible timeout error, never
  a silent hang.
* **Absence is not corruption.** A missing file yields a fresh empty
  document; anything else that fails to parse or has the wrong shape raises
  ``StoreError`` and the caller must refuse the write. The corrupt bytes are
  left byte-identical on disk for recovery.
* **Atomic durable publication.** Writes go to a unique ``mkstemp`` file in
  the same directory (two processes can never collide on a shared ``.tmp``
  name), are fsynced, then ``os.replace``d over the target, then the
  directory entry is fsynced. Readers see the old or the new document, never
  a torn one.
* **Recoverable history.** Before each replace, the previous raw bytes are
  archived under ``<parent>/_history/<stem>/<utcstamp>-r<rev>.json`` (bounded
  retention), so a bad edit or later corruption can be repaired from the
  last-known-good version instead of guesswork.
* **Revisions.** Each successful publish bumps an integer ``revision`` field
  inside the document (additive; readers that ignore it keep working). The
  revision a mutation returns is the one that is durably on disk, which is
  what makes honest read-back and optimistic conflict answers possible.

Lock order (contract C3): worker.lock -> SQLite -> verdicts.json ->
outcomes.json -> profile -> checkpoint files. Callers here take exactly one
document lock at a time; never take an earlier lock while holding a later one.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import tempfile
import time
import datetime as dt
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


HISTORY_DIR_NAME = "_history"
HISTORY_KEEP = 20
LOCK_TIMEOUT_SECONDS = 10.0
LOCK_POLL_SECONDS = 0.03
STALE_TMP_SECONDS = 3600
MAX_DOCUMENT_BYTES = 16 * 1024 * 1024


class StoreError(Exception):
    """A refusal with an HTTP-shaped status and payload; never partial state."""

    def __init__(self, status: int, payload: Dict[str, Any]) -> None:
        super().__init__(payload.get("error", "store error"))
        self.status = int(status)
        self.payload = payload


def _fresh_document(container_key: str) -> Dict[str, Any]:
    return {"version": 1, container_key: [], "revision": 0}


def _lock_path(path: Path) -> Path:
    return path.with_name(path.name + ".lock")


def _acquire_lock(path: Path, timeout: float):
    """Open and flock the sidecar lock file, bounded by ``timeout`` seconds."""
    lock_file = _lock_path(path)
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        handle = lock_file.open("a+b")
    except OSError as exc:
        raise StoreError(503, {"error": "cannot open lock for {}: {}".format(path.name, exc)})
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except OSError as exc:
            if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EINTR):
                handle.close()
                raise StoreError(503, {"error": "cannot lock {}: {}".format(path.name, exc)})
            if time.monotonic() >= deadline:
                handle.close()
                raise StoreError(503, {
                    "error": "could not lock {} within {:.0f}s; another writer may be stuck".format(
                        path.name, timeout
                    )
                })
            time.sleep(LOCK_POLL_SECONDS)


def _read_raw(path: Path) -> Optional[bytes]:
    """Current bytes, or None when the file does not exist (a valid state)."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise StoreError(503, {"error": "{} is unreadable: {}".format(path.name, exc)})
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise StoreError(500, {"error": "{} is implausibly large; refusing to touch it".format(path.name)})
    return raw


def _parse_document(raw: bytes, path: Path, container_key: Optional[str]) -> Dict[str, Any]:
    """Parse and shape-check; corruption fails closed without touching bytes."""
    recovery = "; refusing to overwrite. Recover from {}/{}/ or repair by hand".format(
        HISTORY_DIR_NAME, path.stem
    )
    try:
        document = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise StoreError(500, {"error": "{} is corrupt ({}){}".format(path.name, exc, recovery)})
    if not isinstance(document, dict):
        raise StoreError(500, {"error": "{} is structurally invalid (top level must be an object){}".format(
            path.name, recovery)})
    if container_key is not None and not isinstance(document.get(container_key), list):
        raise StoreError(500, {"error": "{} is structurally invalid ('{}' must be a list){}".format(
            path.name, container_key, recovery)})
    return document


def _archive_previous(path: Path, previous_raw: bytes, previous_revision: int) -> None:
    """Keep the outgoing version recoverable; bounded, best-effort on prune."""
    history = path.parent / HISTORY_DIR_NAME / path.stem
    history.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    target = history / "{}-r{}.json".format(stamp, previous_revision)
    fd, tmp_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=str(history))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(previous_raw)
        os.replace(tmp_name, target)
        target.chmod(0o600)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    versions = sorted(entry for entry in history.iterdir() if entry.suffix == ".json")
    for stale in versions[:-HISTORY_KEEP]:
        try:
            stale.unlink()
        except OSError:
            pass


def _sweep_stale_tmp(path: Path) -> None:
    """Remove abandoned temp files from killed writers (safe: we hold the lock)."""
    now = time.time()
    try:
        candidates = list(path.parent.glob(path.name + ".*.tmp"))
    except OSError:
        return
    for candidate in candidates:
        try:
            if now - candidate.stat().st_mtime > STALE_TMP_SECONDS:
                candidate.unlink()
        except OSError:
            pass


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass  # some filesystems refuse; the file itself is already fsynced
    finally:
        os.close(fd)


def _publish(path: Path, document: Dict[str, Any]) -> None:
    """Unique temp file + fsync + atomic replace + directory fsync + 0600."""
    serialized = json.dumps(document, ensure_ascii=False, indent=2)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    path.chmod(0o600)
    _fsync_directory(path.parent)


class LockedDocument:
    """One locked read-validate-mutate-publish cycle for a JSON document."""

    def __init__(self, path: Path, container_key: Optional[str],
                 require_existing: bool, bump_revision: bool, timeout: float) -> None:
        self._path = Path(path)
        self._container_key = container_key
        self._require_existing = require_existing
        self._bump_revision = bump_revision
        self._timeout = timeout
        self._lock_handle = None
        self._previous_raw: Optional[bytes] = None
        self.document: Dict[str, Any] = {}
        self.revision = 0
        self.committed_revision: Optional[int] = None

    def __enter__(self) -> "LockedDocument":
        self._lock_handle = _acquire_lock(self._path, self._timeout)
        try:
            self._previous_raw = _read_raw(self._path)
            if self._previous_raw is None:
                if self._require_existing:
                    raise StoreError(500, {"error": "{} is missing".format(self._path.name)})
                self.document = _fresh_document(self._container_key or "entries")
                if self._container_key is None:
                    self.document = {}
            else:
                self.document = _parse_document(self._previous_raw, self._path, self._container_key)
            revision = self.document.get("revision")
            self.revision = revision if isinstance(revision, int) and revision >= 0 else 0
        except BaseException:
            self._release()
            raise
        return self

    def commit(self) -> int:
        """Publish the mutated document; returns the durable new revision."""
        if self._lock_handle is None:
            raise StoreError(500, {"error": "commit outside the lock"})
        _sweep_stale_tmp(self._path)
        if self._previous_raw is not None:
            _archive_previous(self._path, self._previous_raw, self.revision)
        if self._bump_revision:
            self.document["revision"] = self.revision + 1
        _publish(self._path, self.document)
        self.committed_revision = self.document.get("revision", self.revision)
        return self.committed_revision if isinstance(self.committed_revision, int) else self.revision

    def _release(self) -> None:
        if self._lock_handle is not None:
            try:
                fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            self._lock_handle.close()
            self._lock_handle = None

    def __exit__(self, exc_type, exc, tb) -> None:
        self._release()


def locked_document(path: Path, container_key: Optional[str] = None, *,
                    require_existing: bool = False, bump_revision: bool = True,
                    timeout: Optional[float] = None) -> LockedDocument:
    """Context manager: hold the file's inter-process lock for read+write.

    ``container_key`` asserts the document is ``{..., container_key: [...]}``;
    ``None`` skips container validation (used for the filter profile).
    Nothing is written unless the caller invokes ``commit()``. ``timeout``
    defaults to the module's LOCK_TIMEOUT_SECONDS at call time, so tests and
    operators can tighten it without re-importing.
    """
    effective = LOCK_TIMEOUT_SECONDS if timeout is None else timeout
    return LockedDocument(path, container_key, require_existing, bump_revision, effective)


def read_document(path: Path, container_key: Optional[str]) -> Tuple[Optional[Dict[str, Any]], int, str]:
    """Lock-free consistent read: (document, revision, error).

    Publication is atomic, so a plain read never sees a torn file. A missing
    file is a valid empty state (fresh document, revision 0, no error); a
    corrupt or misshapen file returns ``(None, 0, reason)`` so callers can be
    honest instead of showing an empty page over broken history.
    """
    try:
        raw = _read_raw(path)
        if raw is None:
            return _fresh_document(container_key or "entries"), 0, ""
        document = _parse_document(raw, path, container_key)
    except StoreError as exc:
        return None, 0, str(exc.payload.get("error", "unreadable"))
    revision = document.get("revision")
    return document, (revision if isinstance(revision, int) and revision >= 0 else 0), ""
