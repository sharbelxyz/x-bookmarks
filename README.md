# x-bookmarks

Turn X/Twitter bookmarks from a graveyard of good intentions into actionable work.

## What it does

- Fetches your X bookmarks via **bird CLI** or **X API v2** (auto-detects)
- Categorizes them by topic
- Proposes specific actions your AI agent can execute
- Supports scheduled digests via cron
- Pattern detection across bookmark history

## Quick Start

### Option 1: bird CLI (easiest)

```bash
npm install -g bird-cli
# Log into x.com in Chrome, then:
bird --chrome-profile "Default" bookmarks --json
```

### Option 2: X API v2 (no bird needed)

```bash
# One-time: create app at https://developer.x.com, then:
python3 scripts/x_api_auth.py --client-id "YOUR_CLIENT_ID"

# Fetch bookmarks
python3 scripts/fetch_bookmarks_api.py -n 20
```

Both backends output the same JSON format — all workflows work with either.

## Files

```
SKILL.md              — Agent instructions (the skill itself)
scripts/
  fetch_bookmarks.sh  — bird CLI wrapper
  fetch_bookmarks_api.py  — X API v2 fetcher
  x_api_auth.py       — OAuth 2.0 PKCE auth helper
references/
  auth-setup.md       — Detailed setup guide for both backends
```

## Requirements

**bird CLI path:** Node.js, npm, bird-cli, browser with X login
**X API path:** Python 3.10+, X Developer account, OAuth 2.0 app

## Install as OpenClaw Skill

Copy this folder to your OpenClaw skills directory, or:

```bash
# If published to ClawhHub
openclaw skill install x-bookmarks
```

## License

MIT
