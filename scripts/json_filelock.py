#!/usr/bin/env python3
"""
Atomic JSON file I/O with flock-based concurrency control.

Prevents data corruption when multiple processes (scheduler, dashboard, CLI)
access the same JSON files simultaneously.

Usage:
    from json_filelock import atomic_json_write, locked_json_read, locked_json_write

    data = locked_json_read(path)          # shared lock (concurrent reads OK)
    locked_json_write(path, data)          # exclusive lock + atomic write
    atomic_json_write(path, data)          # atomic write without lock
"""

import fcntl
import json
import os
import tempfile
from pathlib import Path


def atomic_json_write(path, data, ensure_ascii=False, indent=None, separators=None):
    """
    Write JSON atomically: write to a temp file in the same directory,
    then os.replace() to swap it in. This prevents partial writes from
    corrupting the file if the process is killed mid-write.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent),
        prefix="." + path.stem + "_",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, ensure_ascii=ensure_ascii, indent=indent,
                      separators=separators)
        os.replace(tmp_path, str(path))
    except BaseException:
        # Clean up temp file on any error (including KeyboardInterrupt)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def locked_json_read(path, default=None):
    """
    Read JSON with a shared (LOCK_SH) flock.
    Multiple readers can hold the lock simultaneously.
    Returns `default` (or empty dict) if the file doesn't exist.
    """
    path = Path(path)
    if not path.exists():
        return default if default is not None else {}

    with open(path) as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_SH)
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return default if default is not None else {}
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def locked_json_write(path, data, ensure_ascii=False, indent=None, separators=None):
    """
    Acquire an exclusive flock, then atomically write the JSON data.

    Uses a separate .lock sentinel file so we can atomically replace
    the data file without truncating it while readers hold a shared lock.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.touch(exist_ok=True)

    with open(lock_path) as lock_f:
        fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
        try:
            atomic_json_write(path, data, ensure_ascii=ensure_ascii,
                              indent=indent, separators=separators)
        finally:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)


def locked_read_modify_write(path, modify_fn, ensure_ascii=False, indent=None,
                              separators=None, default=None):
    """
    Convenience: acquire exclusive lock, read JSON, apply modify_fn,
    write back atomically. Prevents TOCTOU races on read-modify-write cycles.

    Usage:
        def add_tweet(data):
            data.append(new_tweet)
            return data

        locked_read_modify_write(master_file, add_tweet, default=[])
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.touch(exist_ok=True)

    with open(lock_path) as lock_f:
        fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
        try:
            # Read current data
            if path.exists():
                try:
                    with open(path) as f:
                        data = json.load(f)
                except json.JSONDecodeError:
                    data = default if default is not None else {}
            else:
                data = default if default is not None else {}

            # Apply modification
            data = modify_fn(data)

            # Write back atomically
            atomic_json_write(path, data, ensure_ascii=ensure_ascii,
                              indent=indent, separators=separators)

            return data
        finally:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
