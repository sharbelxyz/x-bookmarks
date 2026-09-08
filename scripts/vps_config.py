#!/usr/bin/env python3
"""Where the Telegram integration's VPS lives — deployment detail, not code.

The radar reaches Telegram through a personal VPS: the Atlas bot already polls
that token, so this Mac must not poll it too (see telegram_decisions). Its
address, SSH key and remote paths are **deployment coordinates**. Hard-coding
them in source meant publishing a hostname, a root login target and the exact
remote paths to anyone reading the repository — a free target list.

They now load, in order of precedence, from:

1. ``RADAR_VPS_HOST`` / ``RADAR_VPS_KEY`` / ``RADAR_VPS_REMOTE_BASE`` in the
   environment, for one-off overrides;
2. ``config/vps.json`` (git-ignored), the normal place;
3. nothing — in which case ``host`` is empty and every caller degrades
   gracefully. Telegram delivery is best-effort by design: a notification
   problem must never fail an otherwise-good scan run.

Keeping this in one module means the two callers cannot drift apart, and the
repository stays publishable.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "vps.json"

# Fallback only; the real base normally comes from config/vps.json.
DEFAULT_REMOTE_BASE = "/opt/autonomous-loop"


def _read_config(path: Path = CONFIG_PATH) -> Dict[str, Any]:
    """Missing or unreadable config is a normal unconfigured state, not an error.

    A malformed file is reported by the caller's degraded path rather than
    raised, because this module is imported at notification time inside an
    already-bounded best-effort block.
    """
    try:
        with path.open(encoding="utf-8") as handle:
            document = json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return document if isinstance(document, dict) else {}


def load(path: Path = CONFIG_PATH) -> Dict[str, Any]:
    document = _read_config(path)
    host = str(os.environ.get("RADAR_VPS_HOST") or document.get("host") or "").strip()
    key = str(os.environ.get("RADAR_VPS_KEY") or document.get("ssh_key") or "").strip()
    base = str(
        os.environ.get("RADAR_VPS_REMOTE_BASE")
        or document.get("remote_base")
        or DEFAULT_REMOTE_BASE
    ).strip()
    return {
        "host": host,
        "ssh_key": Path(key).expanduser() if key else None,
        "remote_base": base.rstrip("/"),
        "configured": bool(host and key),
    }


def unconfigured_reason(settings: Dict[str, Any]) -> str:
    """One human sentence naming what is missing, for the caller's result dict."""
    if not settings.get("host"):
        return "VPS host is not configured (set config/vps.json or RADAR_VPS_HOST)"
    if not settings.get("ssh_key"):
        return "VPS ssh key is not configured (set config/vps.json or RADAR_VPS_KEY)"
    return ""
