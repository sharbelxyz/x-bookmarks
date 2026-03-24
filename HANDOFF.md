# X Bookmarks Project — Handoff Summary

## What This Project Does

Harvests ALL bookmarks from multiple X/Twitter accounts, hydrates DM tweet URLs, categorizes everything by topic using local AI (Ollama), and serves a native macOS dashboard with browse/search/filter, a BS (noise) filter, and a Growth Feed that scores and prioritizes tweets by professional relevance.

## Current Status: FULLY OPERATIONAL

### What's Done
- **Harvest**: 24,554 tweets collected (bookmarks from 3 accounts + hydrated DM tweets)
- **AI Categorization**: 100% classified (13 categories, 60+ subcategories)
- **LLM Provider**: Ollama local inference (qwen3:8b default, gemma3:4b fallback) — also supports Gemini and Claude API
- **Native Dashboard**: Flask + pywebview macOS app with real-time sync controls, logs, account management
- **macOS .app Bundle**: `/Applications/X Bookmark Manager.app` — launchable from Finder/Spotlight/Dock
- **Browse Page**: Full-text search, category/source filters, sort, seen/unseen tracking, keyboard shortcuts
- **BS Filter**: Hides noise tweets (Humor & Memes, Uncategorizable, very short) — ON by default
- **Growth Feed**: Scores tweets by 5-factor algorithm, tiers into Must Read / Should Read / Worth Checking
- **Auto-Sync**: Python scheduler daemon via launchd — random intervals, max 2x/day
- **File Locking**: All JSON data reads/writes use fcntl.flock() for thread safety
- **Security**: No hardcoded credentials — all secrets via `.env` and `data/accounts.json` (both gitignored)

### Data Breakdown
| Source | Count |
|--------|-------|
| bookmark:cs_omc | 8,192 |
| bookmark:mshryist | 6,248 |
| bookmark:mshryism | 1,847 |
| DM hydrated tweets | 11,282 |
| **Total unique** | **24,554** |

### Category Distribution
| Category | Count | % |
|----------|-------|---|
| Lifestyle/Personal | 16,234 | 66.1% |
| Other | 3,293 | 13.4% |
| Science/Education | 1,317 | 5.4% |
| Business/Startup | 1,125 | 4.6% |
| AI/ML | 723 | 2.9% |
| Content/Media | 518 | 2.1% |
| Politics/News | 330 | 1.3% |
| Programming/Dev | 291 | 1.2% |
| Tools/Productivity | 256 | 1.0% |
| Marketing/Growth | 191 | 0.8% |
| Finance/Trading | 162 | 0.7% |
| Design/UI | 103 | 0.4% |
| Crypto/Web3 | 11 | 0.0% |

## Project Directory

```
/Users/mshrmnsr/claude1/x-bookmarks/
├── scripts/
│   ├── service.py              # ⭐ MAIN CLI — harvest, sync, categorize, serve, export, dashboard
│   ├── dashboard.py            # Flask + pywebview native dashboard (API + UI server)
│   ├── scheduler.py            # Python scheduler daemon (replaces bash sync_scheduler.sh)
│   ├── llm_provider.py         # Multi-provider LLM abstraction (Ollama/Gemini/Claude)
│   ├── json_filelock.py        # Atomic JSON read/write with fcntl.flock()
│   ├── fetch_bookmarks_api.py  # Twitter API bookmark fetching
│   ├── x_api_auth.py           # Twitter API auth helpers
│   ├── sync_scheduler.sh       # Legacy bash scheduler (kept for reference)
│   └── run_pipeline.sh         # Legacy pipeline runner
├── app/
│   ├── dashboard.html          # ⭐ Dashboard UI — Browse, Growth Feed, Logs, Accounts, DM Config
│   ├── index.html              # Legacy standalone browser app
│   └── tweets.json             # Compact browser data (auto-updated by sync)
├── data/
│   ├── accounts.json           # 🔒 Account credentials (gitignored)
│   ├── dm_config.json          # 🔒 DM conversation config (gitignored)
│   ├── categorized_tweets.json # ⭐ Master data store — 24,554 categorized tweets
│   ├── sync_log.json           # Sync history
│   ├── sync_scheduler_state.json # Scheduler run count + dates
│   ├── bookmarks_all.json      # Raw bookmarks
│   ├── hydrated_tweets.jsonl   # Hydrated DM tweets
│   └── merged_tweets.json      # Pre-categorization merged data
├── build_app.py                # Builds /Applications/X Bookmark Manager.app
├── .env                        # 🔒 API keys (gitignored) — LLM_PROVIDER, GEMINI_API_KEY, etc.
├── .gitignore                  # Excludes secrets, locks, checkpoints, logs
├── SERVICE_GUIDE.md            # Productization & monetization guide
└── HANDOFF.md                  # This file
```

## Quick Commands

```bash
cd /Users/mshrmnsr/claude1/x-bookmarks

# ─── Dashboard (primary interface) ─────────────────────────────
python3 scripts/service.py dashboard                      # Open native macOS window
open "/Applications/X Bookmark Manager.app"               # Launch from Finder/Dock

# ─── Service CLI ────────────────────────────────────────────────
python3 scripts/service.py status                         # Full status report
python3 scripts/service.py list                           # List accounts
python3 scripts/service.py sync                           # Manual incremental sync
python3 scripts/service.py sync --all                     # Full sync (all bookmarks)
python3 scripts/service.py serve                          # Legacy browser app at :8742
python3 scripts/service.py export mshryism --format csv   # Export one account

# ─── Onboard a new account ──────────────────────────────────────
python3 scripts/service.py add <username> <auth_token> <ct0>
python3 scripts/service.py harvest <username> --all
python3 scripts/service.py export <username> --format csv

# ─── DM sync ────────────────────────────────────────────────────
python3 scripts/service.py dm-sync                        # Sync DM conversations

# ─── Reclassify ─────────────────────────────────────────────────
python3 scripts/service.py reclassify-other               # Parallel reclassify "Other" tweets

# ─── Auto-sync management ───────────────────────────────────────
python3 scripts/service.py setup-auto                     # Install/restart launchd daemon
launchctl list | grep bookmark-sync                       # Check if running
tail -20 data/sync_stdout.log                             # Recent sync log

# ─── Build .app bundle ──────────────────────────────────────────
python3 build_app.py                                      # Build/rebuild macOS app
```

## Key Technical Details

### AI Categorization
- **Default provider**: Ollama (local, free, fast)
- **Default model**: qwen3:8b (also tested: gemma3:4b)
- **Fallback providers**: Gemini (API key in .env), Claude (API key in .env)
- **Configuration**: Set in `.env` file — `LLM_PROVIDER=ollama`, `LLM_MODEL=qwen3:8b`, `LLM_BATCH_SIZE=20`
- **Provider abstraction**: `scripts/llm_provider.py` — `call_llm(system_prompt, user_prompt)` routes to any provider
- **Batch size**: 20 tweets per call (Ollama) / 40 (Gemini) / 50 (Claude)
- **Categories**: 13 main, 60+ subcategories (see `CATEGORIES` dict in service.py)
- **Reclassification**: Parallel multi-model passes brought accuracy to 94-98%

### Bird CLI
- **Path**: `/opt/homebrew/bin/bird` (v0.8.0, `@steipete/bird`)
- **Usage**: `bird bookmarks -n 100 --json --auth-token TOKEN --ct0 CT0`
- **Note**: `--all` flag returns empty for some accounts; `-n 100` works reliably for incremental sync

### 3 Configured Accounts
Credentials stored in `data/accounts.json` (gitignored). Manage via:
- `service.py add <username> <auth_token> <ct0>`
- `service.py remove <username>`
- Dashboard UI → Accounts tab

### Auto-Sync Daemon
- **Plist**: `~/Library/LaunchAgents/com.mshrmnsr.bookmark-sync.plist`
- **Scheduler**: `scripts/scheduler.py` (Python, replaces legacy bash script)
- **Behavior**: Random sleep 1-10 hours between syncs, max 2 runs per day
- **State**: `data/sync_scheduler_state.json`
- **KeepAlive**: true (restarts if killed, survives reboots)
- **Logs**: `data/sync_stdout.log`, `data/sync_stderr.log`

### Native Dashboard
- **Port**: 8743 (Flask API + static files)
- **Window**: pywebview 6.1 native macOS window (1200×800, min 900×600)
- **Fallback**: Opens in browser if pywebview not installed
- **Pages**: Overview, Browse, Growth Feed, Logs, Accounts, DM Config

### BS Filter (Browse page)
- Hides noise tweets — ON by default, persisted in localStorage
- Noise = Humor & Memes subcategory OR Uncategorizable OR text < 10 chars (excluding URLs)
- Toggle button in browse header bar

### Growth Feed
- Shows ONLY growth-relevant categories: AI/ML, Programming/Dev, Business/Startup, Tools/Productivity, Marketing/Growth, Design/UI, Science/Education
- **5-factor scoring algorithm** (max ~107 points):
  - Category relevance (0-30) — weighted by category importance
  - Content substance (0-23) — text length tiers + summary quality
  - Engagement quality (0-25) — log10(likes) × 4 + log10(views) × 0.75
  - Recency (0-15) — ≤7d=15, ≤30d=12, ≤90d=8, ≤180d=4
  - Unseen bonus (0-10) — not yet seen = +10
- **Priority tiers** (by rank position):
  - 🔴 Must Read — top 10% by score
  - 🟡 Should Read — next 20%
  - 🟢 Worth Checking — remaining
- Tier/category filters, search, sort, seen/unseen tracking, keyboard shortcuts (j/k/s/o)

### Browser App Data Format
- **File**: `app/tweets.json` — compact format with shortened field names
- **Fields**: i(id), t(text), u(username), n(name), d(date), l(likes), r(retweets), v(views), c(category), s(subcategory), m(summary), src(sources), th(hasThread), mt(mediaType)

### File Locking
- All JSON data operations use `json_filelock.py` — `locked_json_read()`, `locked_json_write()`, `atomic_json_write()`
- Uses `fcntl.flock()` (POSIX advisory locking) for safe concurrent access
- Prevents data corruption during parallel sync/dashboard/scheduler operations

### Dependencies
```
pywebview>=6.1        # Native macOS window
flask                 # Dashboard API server
Pillow                # Icon generation (optional, for build_app.py)
ollama                # Local LLM inference (installed separately: brew install ollama)
```

### Productization
See `SERVICE_GUIDE.md` for monetization models, pricing, architecture notes.
