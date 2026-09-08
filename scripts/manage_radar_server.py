#!/usr/bin/env python3
"""Install, remove, inspect, or open the live Group Resource Radar server.

The server itself is ``scripts/radar_server.py``. This manager owns exactly
one launchd LaunchAgent (``com.mshrmnsr.group-radar-server``) so the dashboard
is reachable at http://127.0.0.1:8765/ whenever the Mac is awake.

    python3 scripts/manage_radar_server.py install    # write plist + start
    python3 scripts/manage_radar_server.py status     # plist / process / health
    python3 scripts/manage_radar_server.py open       # open the dashboard URL
    python3 scripts/manage_radar_server.py restart    # kickstart after code changes
    python3 scripts/manage_radar_server.py uninstall  # kill switch

``uninstall`` is the kill switch: it stops the job and deletes the plist.
Nothing here touches the cron-based monitor loop.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional


ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data" / "group-monitor"
SERVER = ROOT / "scripts" / "radar_server.py"
LABEL = "com.mshrmnsr.group-radar-server"
PLIST = Path.home() / "Library" / "LaunchAgents" / (LABEL + ".plist")
LOG = DATA_DIR / "server.log"
PORT = 8765
URL = "http://127.0.0.1:{}/".format(PORT)
HEALTH_URL = URL + "api/health"
PYTHON = "/usr/bin/python3"
MAX_LOG_BYTES = 5 * 1024 * 1024


def domain() -> str:
    return "gui/{}".format(os.getuid())


def launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, check=False)


def plist_document() -> Dict[str, Any]:
    return {
        "Label": LABEL,
        "ProgramArguments": [PYTHON, str(SERVER), "--port", str(PORT)],
        "WorkingDirectory": str(ROOT),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 30,
        "ProcessType": "Background",
        "StandardOutPath": str(LOG),
        "StandardErrorPath": str(LOG),
        "EnvironmentVariables": {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8",
        },
    }


def fetch_health(timeout: float = 2.0) -> Optional[Dict[str, Any]]:
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def wait_for_health(seconds: float = 10.0) -> Optional[Dict[str, Any]]:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        payload = fetch_health()
        if payload and payload.get("service") == "group-radar":
            return payload
        time.sleep(0.4)
    return None


def loaded_state() -> Dict[str, Any]:
    result = launchctl("print", "{}/{}".format(domain(), LABEL))
    if result.returncode != 0:
        return {"loaded": False}
    pid = None
    state = None
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("pid = "):
            try:
                pid = int(stripped.split("=", 1)[1].strip())
            except ValueError:
                pid = None
        elif stripped.startswith("state = "):
            state = stripped.split("=", 1)[1].strip()
    return {"loaded": True, "pid": pid, "state": state}


def trim_log() -> None:
    try:
        if LOG.exists() and LOG.stat().st_size > MAX_LOG_BYTES:
            tail = LOG.read_bytes()[-MAX_LOG_BYTES // 4 :]
            LOG.write_bytes(tail)
    except OSError:
        pass


def install() -> int:
    if not SERVER.exists():
        print("server script is missing: {}".format(SERVER), file=sys.stderr)
        return 1
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.chmod(0o700)
    LOG.touch(mode=0o600, exist_ok=True)
    LOG.chmod(0o600)
    trim_log()
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    if loaded_state()["loaded"]:
        launchctl("bootout", "{}/{}".format(domain(), LABEL))
        time.sleep(0.5)
    with PLIST.open("wb") as handle:
        plistlib.dump(plist_document(), handle, sort_keys=True)
    PLIST.chmod(0o644)
    result = launchctl("bootstrap", domain(), str(PLIST))
    if result.returncode != 0:
        # Older macOS releases only understand load/unload.
        result = launchctl("load", "-w", str(PLIST))
        if result.returncode != 0:
            print("launchctl could not start the job: {}".format((result.stderr or result.stdout).strip()), file=sys.stderr)
            return 1
    health = wait_for_health()
    if not health:
        print("job started but the server is not answering yet; check {}".format(LOG), file=sys.stderr)
        return 1
    print("installed {}; dashboard: {} (pid {})".format(LABEL, URL, health.get("pid")))
    return 0


def uninstall() -> int:
    state = loaded_state()
    if state["loaded"]:
        launchctl("bootout", "{}/{}".format(domain(), LABEL))
    if PLIST.exists():
        PLIST.unlink()
    print("removed {}; the dashboard file still works via file://{}".format(LABEL, DATA_DIR / "dashboard.html"))
    return 0


def restart() -> int:
    if not PLIST.exists():
        print("not installed; run install first", file=sys.stderr)
        return 1
    result = launchctl("kickstart", "-k", "{}/{}".format(domain(), LABEL))
    if result.returncode != 0:
        print("kickstart failed: {}".format((result.stderr or result.stdout).strip()), file=sys.stderr)
        return 1
    health = wait_for_health()
    if not health:
        print("restarted but the server is not answering yet; check {}".format(LOG), file=sys.stderr)
        return 1
    print("restarted {} (pid {})".format(LABEL, health.get("pid")))
    return 0


def status() -> int:
    state = loaded_state()
    health = fetch_health()
    summary = {
        "plist": str(PLIST),
        "plist_present": PLIST.exists(),
        "launchd_loaded": state.get("loaded", False),
        "launchd_pid": state.get("pid"),
        "launchd_state": state.get("state"),
        "url": URL,
        "healthy": bool(health and health.get("ok")),
        "health": health,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["healthy"] else 1


def open_dashboard() -> int:
    health = fetch_health()
    if not health:
        print("server is not running; opening the static file instead (install for live updates)", file=sys.stderr)
        target = "file://{}".format(DATA_DIR / "dashboard.html")
    else:
        target = URL
    subprocess.run(["open", target], check=False)
    print(target)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "uninstall", "status", "open", "restart"))
    args = parser.parse_args()
    return {
        "install": install,
        "uninstall": uninstall,
        "status": status,
        "open": open_dashboard,
        "restart": restart,
    }[args.action]()


if __name__ == "__main__":
    raise SystemExit(main())
