#!/usr/bin/env python3
"""
X Bookmark Manager — Sync Scheduler

Pure Python replacement for sync_scheduler.sh.
Runs sync 1-2x/day at random intervals (1-10 hours).

Designed to run as a launchd daemon (KeepAlive: true).
"""

import json
import os
import random
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# ─── Paths ────────────────────────────────────────────────────────────────────

SERVICE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = SERVICE_DIR / "data"
SCRIPTS_DIR = SERVICE_DIR / "scripts"
SYNC_SCRIPT = SCRIPTS_DIR / "service.py"
STATE_FILE = DATA_DIR / "sync_scheduler_state.json"
LOG_FILE = DATA_DIR / "sync_stdout.log"

# Add scripts dir to path for filelock import
sys.path.insert(0, str(SCRIPTS_DIR))
from json_filelock import locked_json_read, locked_json_write

# ─── Constants ────────────────────────────────────────────────────────────────

MAX_RUNS_PER_DAY = 2
MIN_SLEEP_SECS = 3600      # 1 hour
MAX_SLEEP_SECS = 36000     # 10 hours
STATE_RETENTION_DAYS = 7
LOG_MAX_BYTES = 1_000_000  # 1 MB
SYNC_TIMEOUT_SECS = 1800   # 30 minutes


# ─── Graceful Shutdown ────────────────────────────────────────────────────────

_shutdown = False


def _handle_signal(signum, frame):
    global _shutdown
    _shutdown = True
    log(f"Received signal {signum}, shutting down gracefully...")


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


# ─── Logging ──────────────────────────────────────────────────────────────────

def log(msg):
    """Log with timestamp, matching the old bash scheduler format."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[scheduler {timestamp}] {msg}"
    print(line, flush=True)
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def rotate_log():
    """Rotate log if it exceeds LOG_MAX_BYTES (keep last half)."""
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > LOG_MAX_BYTES:
            with open(LOG_FILE) as f:
                lines = f.readlines()
            keep = lines[len(lines) // 2:]
            with open(LOG_FILE, "w") as f:
                f.writelines(keep)
            log("Log file rotated")
    except Exception:
        pass


# ─── State Management ────────────────────────────────────────────────────────

def load_state():
    return locked_json_read(STATE_FILE, default={"runs": {}})


def save_state(state):
    locked_json_write(STATE_FILE, state, indent=2)


def get_runs_today(state):
    today = datetime.now().strftime("%Y-%m-%d")
    return state.get("runs", {}).get(today, 0)


def record_run(state):
    """Increment today's run count and prune old entries."""
    today = datetime.now().strftime("%Y-%m-%d")
    runs = state.get("runs", {})
    runs[today] = runs.get(today, 0) + 1

    # Prune entries older than STATE_RETENTION_DAYS
    cutoff = (datetime.now() - timedelta(days=STATE_RETENTION_DAYS)).strftime("%Y-%m-%d")
    runs = {k: v for k, v in runs.items() if k >= cutoff}
    state["runs"] = runs
    save_state(state)
    return state


# ─── Sleep with shutdown check ────────────────────────────────────────────────

def interruptible_sleep(seconds):
    """Sleep in 10-second increments, checking for shutdown signal."""
    end_time = time.monotonic() + seconds
    while time.monotonic() < end_time:
        if _shutdown:
            return False  # Interrupted
        remaining = end_time - time.monotonic()
        time.sleep(min(10, max(0, remaining)))
    return True  # Completed naturally


# ─── Main Loop ────────────────────────────────────────────────────────────────

def main():
    log("Scheduler started (Python)")
    rotate_log()

    while not _shutdown:
        state = load_state()
        runs_today = get_runs_today(state)

        if runs_today >= MAX_RUNS_PER_DAY:
            # Already hit the daily limit — sleep until tomorrow
            now = datetime.now()
            tomorrow = (now + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            secs_left = int((tomorrow - now).total_seconds()) + 60
            log(f"Already ran {runs_today}x today, sleeping until tomorrow ({secs_left}s)")
            if not interruptible_sleep(secs_left):
                break
            continue

        # Run sync
        log(f"Running sync (run #{runs_today + 1} today)...")
        try:
            result = subprocess.run(
                [sys.executable, str(SYNC_SCRIPT), "sync"],
                capture_output=False,  # Let output go to stdout/log
                timeout=SYNC_TIMEOUT_SECS,
                env={
                    **os.environ,
                    "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
                },
            )
            if result.returncode == 0:
                state = record_run(state)
                log("Sync completed successfully")
            else:
                log(f"Sync failed with exit code {result.returncode}")
        except subprocess.TimeoutExpired:
            log(f"Sync timed out after {SYNC_TIMEOUT_SECS}s")
        except Exception as e:
            log(f"Sync error: {e}")

        # Random wait before next run
        sleep_secs = random.randint(MIN_SLEEP_SECS, MAX_SLEEP_SECS)
        sleep_hrs = round(sleep_secs / 3600, 1)
        log(f"Sleeping {sleep_hrs}h ({sleep_secs}s) until next sync")
        if not interruptible_sleep(sleep_secs):
            break

    log("Scheduler stopped")


if __name__ == "__main__":
    main()
