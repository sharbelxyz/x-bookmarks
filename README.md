# x-bookmarks

Turn X/Twitter bookmarks from a graveyard of good intentions into actionable work.

## How This Works

Once installed, just tell your AI agent:

> "check my bookmarks"

That's it. Your agent will:

1. **Fetch** your latest X bookmarks (auto-detects bird CLI or X API v2)
2. **Categorize** them by topic (crypto, AI, marketing, tools, etc.)
3. **Propose actions** for each one — not just summaries, but things your agent can actually do:

```
📂 AI TOOLS (3)
• @someone shared a repo for automating video edits
  → 🤖 I CAN: Clone it, test it, and set it up for you

📂 TRADING (2)  
• @trader posted a new momentum strategy with backtest data
  → 🤖 I CAN: Compare this against your current strategy and report differences
```

You can also say:
- **"bookmark digest"** — get a categorized summary of recent saves
- **"what did I bookmark this week?"** — filtered by time
- **"find patterns in my bookmarks"** — clusters topics you keep saving
- **"clean up old bookmarks"** — flags stale saves with TL;DRs

### Scheduled Digests

Set up a daily or weekly cron job and your agent will automatically check for new bookmarks, categorize them, and deliver a digest to you.

### Private Group Share Filter

The durable group monitor resumes after the last previously audited group message,
captures resources from every account in the group, and keeps failed X reads in a
retry queue. Its self-contained HTML dashboard refreshes on every run; served by
`scripts/manage_radar_server.py install` at http://127.0.0.1:8765/ it updates in
place and shows what is new since your last visit, top picks, and software /
practice / research lanes. See [GROUP_FILTER.md](GROUP_FILTER.md) for the
schedule, outputs, selection profile, dashboard, and kill switches.

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

## Auto-Detection

You don't need to pick a backend. The skill automatically:

1. Tries `bird whoami` — if it works, uses bird CLI
2. If not, checks for X API tokens in `~/.config/x-bookmarks/`
3. If neither, walks you through setup (offers both options)

## Files

```
SKILL.md              — Agent instructions (the skill itself)
scripts/
  fetch_bookmarks.sh      — bird CLI wrapper
  fetch_bookmarks_api.py  — X API v2 fetcher
  x_api_auth.py           — OAuth 2.0 PKCE auth helper
references/
  auth-setup.md           — Detailed setup guide for both backends
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
