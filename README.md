# x-bookmarks

Your bookmarks are smarter than you think. This skill reads them so you don't have to.

Most people bookmark tweets and never look at them again. Bookmarks become a graveyard of good intentions — "I'll read this later" turns into "I forgot this existed."

This is a skill for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) and [OpenClaw](https://github.com/openclaw/openclaw) that turns your X bookmarks into agent actions. It doesn't just summarize — it proposes work your agent can actually do based on what you saved.

## How it works

You say something like:

> "check my bookmarks"

The skill:

1. Fetches your recent X bookmarks via the `bird` CLI
2. Categorizes them by topic (trading, AI, content, tools, etc.)
3. For each category, proposes specific actions the agent can take
4. Flags stale bookmarks you saved weeks ago and never touched
5. Extracts actionable tips into checklists you can actually follow

## What it looks like

Instead of:
> "You have 20 bookmarks. Here's a summary."

You get:
> 📂 **Trading & Bots (6 bookmarks)**
> - Copy-trading bot repo, arbitrage formula, weather trading setup
> - 🤖 **I CAN:** Analyze that repo, compare to your bot, find edges you're missing
>
> 📂 **Content Tools (3 bookmarks)**
> - Claude prompts, clipper agent, vibe coding prompt
> - 🤖 **I CAN:** Test these prompts against your current system
>
> 🪦 **Ancient (4 bookmarks from 2022)**
> - 🤖 **I CAN:** Clear these. They're digital cobwebs.

## Features

- **Action-first digests** — not "here's what you saved" but "here's what I can do about it"
- **Pattern detection** — "You've bookmarked 12 posts about email marketing. Want me to go deeper?"
- **Content recycling** — "These bookmarks would make great tweets in your voice"
- **Scheduled digests** — set up a daily/weekly cron to process new bookmarks
- **Bookmark cleanup** — flag stale saves with TL;DRs and "use it or lose it" nudges

## Install

```bash
npx clawhub install x-bookmarks
```

Or clone directly:

```bash
git clone https://github.com/sharbelxyz/x-bookmarks.git skills/x-bookmarks
```

## Prerequisites

- **bird CLI**: `npm install -g bird-cli`
- **Auth**: Log into x.com in Chrome, then bird extracts cookies automatically

```bash
# Verify auth works
bird whoami

# Fetch bookmarks
bird bookmarks --json
```

See [references/auth-setup.md](references/auth-setup.md) for all auth options (Chrome, Firefox, Brave, manual tokens).

## Stop hoarding. Start applying.

Built by [@sharbel](https://x.com/sharbel) and his AI agent Max.
