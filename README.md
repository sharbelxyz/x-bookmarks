# x-bookmarks

Turn X/Twitter bookmarks from a graveyard of good intentions into actionable work.

## How This Works

Once installed, just tell your AI agent:

> "check my bookmarks"

That's it. Your agent will:

1. **Fetch** your latest X bookmarks (auto-detects bird CLI, Xquik, or X API v2)
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

## What it does

- Fetches your X bookmarks via **bird CLI**, **Xquik**, or **X API v2**
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

### Option 2: Xquik API (no browser cookies)

Connect an X account in Xquik and create an API key. Keep the key outside source code:

```bash
read -rsp "Xquik API key: " XQUIK_API_KEY
export XQUIK_API_KEY
printf '\n'
python3 scripts/fetch_bookmarks_xquik.py -n 20
```

Fetch every page or one bookmark folder:

```bash
python3 scripts/fetch_bookmarks_xquik.py --all
python3 scripts/fetch_bookmarks_xquik.py --folder-id "YOUR_FOLDER_ID"
```

The client follows Xquik cursors, including empty filtered pages. It rejects malformed responses and repeated cursors. See the [Xquik Bookmarks API](https://docs.xquik.com/api-reference/x/bookmarks).

Xquik is an independent third-party service. Not affiliated with X Corp. "Twitter" and "X" are trademarks of X Corp.

### Option 3: X API v2 (no bird needed)

```bash
# One-time: create app at https://developer.x.com, then:
python3 scripts/x_api_auth.py --client-id "YOUR_CLIENT_ID"

# Fetch bookmarks
python3 scripts/fetch_bookmarks_api.py -n 20
```

All 3 backends output the same JSON format. Every workflow remains backend-agnostic.

### Optional TweetClaw companion

Keep this Skill focused on saved posts and bookmark digests. For live X workflows beyond bookmarks, install [TweetClaw](https://github.com/Xquik-dev/tweetclaw):

```bash
openclaw plugins install clawhub:@xquik/tweetclaw
```

## Auto-Detection

You don't need to pick a backend. The skill automatically:

1. Tries `bird whoami` — if it works, uses bird CLI
2. If not, checks for `XQUIK_API_KEY`
3. If not, checks for X API tokens in `~/.config/x-bookmarks/`
4. If none work, walks you through all setup options

## Files

```
SKILL.md              — Agent instructions (the skill itself)
scripts/
  fetch_bookmarks.sh      — bird CLI wrapper
  fetch_bookmarks_xquik.py — Xquik bookmark fetcher
  fetch_bookmarks_api.py  — X API v2 fetcher
  x_api_auth.py           — OAuth 2.0 PKCE auth helper
references/
  auth-setup.md           — Detailed setup guide for all backends
```

## Requirements

- **bird CLI path:** Node.js, npm, bird-cli, browser with X login
- **Xquik path:** Python 3.10+, Xquik API key, connected X account
- **X API path:** Python 3.10+, X Developer account, OAuth 2.0 app

## Install as OpenClaw Skill

Copy this folder to your OpenClaw skills directory, or:

```bash
# If published to ClawhHub
openclaw skill install x-bookmarks
```

## License

MIT
