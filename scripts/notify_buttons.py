#!/usr/bin/env python3
"""Send one Telegram message carrying decision buttons.

The existing notifier (`~/assistant/scripts/telegram-notify.sh`) is shared with
Atlas and sends plain text. Rather than change its behaviour for every caller,
this sends the one message shape that needs a keyboard, over the same SSH path
and the same bot token.

Everything is bounded: SSH has a connect timeout and an overall timeout, the
payload is size-checked before it leaves, and any failure returns a reason
instead of raising, because a notification problem must never fail a good run.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
from pathlib import Path

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent))
import vps_config
from typing import Any, Dict, List, Optional


os.umask(0o077)

# See scripts/vps_config.py: deployment coordinates live in git-ignored
# config, so publishing this file exposes no server.
_VPS = vps_config.load()
VPS_HOST = _VPS["host"]
VPS_KEY = _VPS["ssh_key"] or Path("/nonexistent/vps-key-not-configured")
REMOTE_BASE = _VPS["remote_base"]
SSH_TIMEOUT = 45
MAX_TEXT_CHARS = 3500          # Telegram's limit is 4096; leave headroom

# Reads the token and owner chat id the Atlas bot already uses, so there is no
# second credential to manage and nothing secret is ever written to disk here.
REMOTE_SEND_TEMPLATE = r"""
set -euo pipefail
PAYLOAD=$(printf '%s' "$1" | base64 -d)
source {remote_base}/.env
CHAT_ID=$(python3 -c "import json;print(json.load(open('{remote_base}/telegram-bot/.state.json'))['ownerChatId'])")
printf '%s' "$PAYLOAD" | python3 -c "
import json,sys,urllib.request,urllib.parse,os
body = json.load(sys.stdin)
body['chat_id'] = os.environ['CHAT_ID']
data = json.dumps(body).encode()
req = urllib.request.Request(
    'https://api.telegram.org/bot' + os.environ['TELEGRAM_BOT_TOKEN'] + '/sendMessage',
    data=data, headers={'Content-Type': 'application/json'})
print(urllib.request.urlopen(req, timeout=20).status)
"
"""

# Plain substitution, not str.format: the script embeds literal { } in the
# Python dict below, which format() would try to interpret as fields.
REMOTE_SEND = REMOTE_SEND_TEMPLATE.replace("{remote_base}", REMOTE_BASE)


def send(text: str, reply_markup: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Send one message. Returns a result dict; never raises."""
    reason = vps_config.unconfigured_reason(_VPS)
    if reason:
        return {"sent": False, "reason": reason}
    if not VPS_KEY.exists():
        return {"sent": False, "reason": "ssh key is missing"}
    if len(text) > MAX_TEXT_CHARS:
        text = text[: MAX_TEXT_CHARS - 1] + "…"
    body: Dict[str, Any] = {
        "text": text,
        "disable_web_page_preview": True,
    }
    if reply_markup:
        body["reply_markup"] = reply_markup
    encoded = base64.b64encode(json.dumps(body, ensure_ascii=False).encode("utf-8")).decode("ascii")
    command = [
        "ssh", "-i", str(VPS_KEY),
        "-o", "ConnectTimeout=8", "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "root@{}".format(VPS_HOST),
        "bash -s", encoded,
    ]
    try:
        result = subprocess.run(
            command, input=REMOTE_SEND, capture_output=True, text=True,
            timeout=SSH_TIMEOUT, check=False,
        )
    except subprocess.TimeoutExpired:
        return {"sent": False, "reason": "ssh timed out"}
    except OSError as exc:
        return {"sent": False, "reason": "ssh failed: {}".format(exc)[:150]}
    if result.returncode != 0:
        return {"sent": False, "reason": (result.stderr or "ssh returned non-zero").strip()[-200:]}
    return {"sent": True, "response": result.stdout.strip()[-40:]}
