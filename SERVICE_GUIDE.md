# X Bookmark Manager — Service & Monetization Guide

## What It Does

Turns any Twitter/X account's bookmarks into a searchable, categorized, auto-syncing knowledge base.

**Pipeline:** Fetch bookmarks → AI categorize (13 categories, 60+ subcategories) → Browseable web app with search/filter → Auto-sync new bookmarks 1-2x daily

## Quick Start (Your 3 Accounts)

Already set up and running:
```bash
python3 scripts/service.py status    # Check everything
python3 scripts/service.py serve     # Open browser at localhost:8742
```

Auto-sync is active (1-2x daily, random 1-10h intervals).

## Onboarding a New Account

### Step 1: Get credentials
The user needs to extract their `auth_token` and `ct0` from Twitter. Two ways:

**Browser DevTools method:**
1. Log into x.com in Chrome
2. Open DevTools → Application → Cookies → x.com
3. Copy the value of `auth_token` cookie
4. Copy the value of `ct0` cookie

**Export method (easier for non-technical users):**
You could build a Chrome extension or bookmarklet that extracts these automatically.

### Step 2: Add account + harvest
```bash
# Add the account
python3 scripts/service.py add <username> <auth_token> <ct0>

# Harvest ALL bookmarks (first time — gets everything)
python3 scripts/service.py harvest <username> --all

# Check results
python3 scripts/service.py status
```

### Step 3: Auto-sync is already running
The scheduler picks up all accounts from `data/accounts.json`. New accounts are auto-synced on the next cycle.

### Step 4: Export their data
```bash
python3 scripts/service.py export <username> --format csv
python3 scripts/service.py export <username> --format json
```

## CLI Reference

| Command | Description |
|---------|-------------|
| `service.py add <user> <token> <ct0>` | Add account (validates credentials) |
| `service.py remove <user>` | Remove account (keeps data) |
| `service.py list` | List all accounts with bookmark counts |
| `service.py harvest <user> [--all]` | Fetch + categorize one account |
| `service.py harvest-all [--all]` | Fetch + categorize all accounts |
| `service.py sync` | Incremental sync all accounts |
| `service.py serve [--port N]` | Start browser app (default 8742) |
| `service.py status` | Full status report |
| `service.py export <user> [--format csv\|json]` | Export user's bookmarks |
| `service.py setup-auto` | Install/restart auto-sync daemon |

## Monetization Models

### 1. SaaS (Best fit — recurring revenue)

**Pricing tiers:**
- **Free:** 1 account, 500 bookmarks, manual sync only
- **Pro ($9/mo):** 3 accounts, unlimited bookmarks, auto-sync, CSV export
- **Team ($29/mo):** 10 accounts, shared categories, API access, priority sync

**What you need to build:**
- Web dashboard (user auth, account management, browser app per user)
- Hosted infrastructure (each user gets their own data partition)
- Payment integration (Stripe)
- Credential management (encrypted storage for auth_token/ct0)

**Stack suggestion:**
- Backend: FastAPI or Next.js API routes
- DB: SQLite per user (simple) or Postgres (scalable)
- Queue: Redis + Celery for sync jobs
- Frontend: The existing browser app, wrapped in a dashboard
- Deploy: Railway / Fly.io / VPS

### 2. Managed Service (High-touch, high-margin)

Charge $50-200/setup + $19/mo per account. You manually onboard users, manage their sync, deliver categorized exports.

**Workflow:**
1. User sends you their auth_token/ct0 (via DM or secure form)
2. You run `service.py add` + `service.py harvest --all`
3. Send them their categorized bookmarks as CSV + hosted browser link
4. Auto-sync keeps running

**Pros:** No engineering needed beyond what you have. **Cons:** Doesn't scale past ~50 users without automation.

### 3. One-Time Tool Sale ($29-49)

Package the whole thing as a downloadable CLI tool. User runs it locally.

**What you'd ship:**
- `service.py` + `sync_scheduler.sh` + `index.html` (the browser app)
- Setup script that installs bird, creates launchd agent
- README with credentials extraction guide

**Distribution:** Gumroad, Lemon Squeezy, or your own site.

### 4. API / Whitelabel

Offer the categorization engine as an API. Other builders integrate it into their tools.

```
POST /api/categorize
Body: [{"id": "...", "text": "...", "author": "..."}]
Response: [{"id": "...", "category": "AI/ML", "subcategory": "LLMs & Chatbots", "summary": "..."}]
```

Charge per 1,000 categorizations ($1-5/1K).

## Scaling Considerations

### Credential Security
- Never store raw tokens in plaintext for production
- Use encrypted at-rest storage (e.g., `keyring` library or encrypted SQLite)
- Tokens expire — need refresh mechanism or user re-auth flow

### Rate Limits
- Twitter's internal API: ~500 requests/15 min per account
- bird CLI handles pagination automatically (20 bookmarks/page)
- z.ai API: generous limits, but batch wisely (40 tweets/call)
- Current scheduler already randomizes timing + caps 2x/day

### Multi-User Architecture
```
data/
  accounts.json          # All accounts
  tweets_user1.json      # Per-user data
  tweets_user2.json
  categorized_tweets.json  # Master (all users merged)
app/
  tweets.json            # Browser app data
```

For true multi-tenant: each user gets their own `data/` + `app/` directory, served on separate ports or subdomains.

## Cost Structure

| Component | Cost | Notes |
|-----------|------|-------|
| z.ai API | Free tier / ~$0.001/call | GLM-4-32B is very cheap |
| bird CLI | Free (open source) | npm package |
| VPS hosting | $5-20/mo | For SaaS model |
| Domain | $12/yr | |
| Stripe fees | 2.9% + $0.30 | Per transaction |

**Margin:** Extremely high. The AI categorization costs essentially nothing. The value is in the curation + automation.
