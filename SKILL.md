---
name: x-bookmarks
description: >
  Fetch, summarize, and manage X/Twitter bookmarks via the `bird` CLI.
  Use when: (1) user says "check my bookmarks", "what did I bookmark", "bookmark digest",
  "summarize my bookmarks", "x bookmarks", "twitter bookmarks", (2) user wants a periodic
  digest of saved tweets, (3) user wants to categorize, search, or analyze their bookmarks,
  (4) scheduled bookmark digests via cron. Requires `bird` CLI (npm i -g bird-cli) and
  X/Twitter authentication via browser cookies or manual tokens.
---

# X Bookmarks

Turn X/Twitter bookmarks from a graveyard of good intentions into actionable work.

**Core philosophy:** Don't just summarize — propose actions the agent can execute.

## Prerequisites

- **bird CLI**: `npm install -g bird-cli` (v0.8+)
- **Auth** (one of):
  - `--chrome-profile <name>` — auto-extracts cookies from Chrome (recommended)
  - `--firefox-profile <name>` — Firefox equivalent
  - Manual: `--auth-token <token> --ct0 <token>` from browser dev tools
  - Config: `~/.config/bird/config.json5` with `{ chromeProfile: "Default" }`
- See [references/auth-setup.md](references/auth-setup.md) for detailed setup guide

## Fetching Bookmarks

```bash
# Latest 20 bookmarks (default)
bird bookmarks --json

# Specific count
bird bookmarks -n 50 --json

# All bookmarks (paginated)
bird bookmarks --all --json

# With thread context
bird bookmarks --include-parent --thread-meta --json

# With Chrome cookie auth
bird --chrome-profile "Default" bookmarks --json

# With manual tokens
bird --auth-token "$AUTH_TOKEN" --ct0 "$CT0" bookmarks --json
```

If user has a `.env.bird` file or env vars `AUTH_TOKEN`/`CT0`, source them first: `source .env.bird`

## JSON Output Format

Each bookmark returns:
```json
{
  "id": "tweet_id",
  "text": "tweet content",
  "createdAt": "Wed Feb 11 01:00:06 +0000 2026",
  "replyCount": 46,
  "retweetCount": 60,
  "likeCount": 801,
  "author": { "username": "handle", "name": "Display Name" },
  "media": [{ "type": "photo|video", "url": "..." }],
  "quotedTweet": { ... }
}
```

## Core Workflows

### 1. Action-First Digest (Primary Use Case)

The key differentiator: don't just summarize, **propose actions the agent can execute**.

1. Fetch bookmarks: `bird bookmarks -n <count> --json`
2. Parse and categorize by topic (auto-detect: crypto, AI, marketing, tools, personal, etc.)
3. For EACH category, propose specific actions:
   - **Tool/repo bookmarks** → "I can test this, set it up, or analyze the code"
   - **Strategy/advice bookmarks** → "Here are the actionable steps extracted — want me to implement any?"
   - **News/trends** → "This connects to [user's work]. Here's the angle for content"
   - **Content ideas** → "This would make a great tweet/video in your voice. Here's a draft"
   - **Questions/discussions** → "I can research this deeper and give you a summary"
4. Flag stale bookmarks (>2 weeks old) — "Use it or lose it"
5. Deliver categorized digest with actions

Format output as:
```
📂 CATEGORY (count)
• Bookmark summary (@author)
→ 🤖 I CAN: [specific action the agent can take]
```

### 2. Scheduled Digest (Cron)

Set up a recurring bookmark check. Suggest this cron config to the user:

```
Schedule: daily or weekly
Payload: "Check my X bookmarks for new saves since last check.
  Fetch bookmarks, compare against last digest, summarize only NEW ones.
  Categorize and propose actions. Deliver to me."
```

Track state by saving the most recent bookmark ID processed. Store in workspace:
`memory/bookmark-state.json` → `{ "lastSeenId": "...", "lastDigestAt": "..." }`

### 3. Content Recycling

When user asks for content ideas from bookmarks:
1. Fetch recent bookmarks
2. Identify high-engagement tweets (>500 likes) with frameworks, tips, or insights
3. Rewrite key ideas in the user's voice (if voice data available)
4. Suggest posting times based on the bookmark's original engagement

### 4. Pattern Detection

When user has enough bookmark history:
1. Fetch all bookmarks (`--all`)
2. Cluster by topic/keywords
3. Report: "You've bookmarked N tweets about [topic]. Want me to go deeper?"
4. Suggest: research reports, content series, or tools based on patterns

### 5. Bookmark Cleanup

For stale bookmarks:
1. Identify bookmarks older than a threshold (default: 30 days)
2. For each: extract the TL;DR and one actionable takeaway
3. Present: "Apply it today or clear it"
4. User can unbookmark via: `bird unbookmark <tweet-id>`

## Error Handling

| Error | Cause | Fix |
|-------|-------|-----|
| "No Twitter cookies found" | Not logged into X in browser | Log into x.com in Chrome/Firefox |
| EPERM on Safari cookies | macOS permissions | Use Chrome or Firefox instead |
| Empty results | Cookies expired | Re-login to x.com, retry |
| Rate limit | Too many requests | Wait and retry, use `--max-pages` to limit |

## Tips

- Start with `-n 20` for quick digests, `--all` for deep analysis
- Use `--include-parent` to get thread context for replies
- Bookmark folders are supported via `--folder-id <id>`
- Add `--sort-chronological` for time-ordered output
