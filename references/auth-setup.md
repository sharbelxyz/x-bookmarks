# X Bookmarks — Auth Setup

Two ways to authenticate. Pick whichever works for you.

---

## Option A: bird CLI (browser cookies)

Easiest if you have bird installed (`npm i -g bird-cli`).

### A1. Chrome Cookie Extraction (Recommended)

```bash
bird --chrome-profile "Default" bookmarks --json
```

Find your Chrome profile name:
1. Open `chrome://version` in Chrome
2. Look for "Profile Path" — the last folder name is your profile (e.g., "Default", "Profile 1")

**Troubleshooting:**
- macOS may prompt for Keychain access → click "Allow"
- Must be logged into x.com in that Chrome profile
- If cookie extraction fails, close Chrome first (locked DB)

**Make it permanent** — create `~/.config/bird/config.json5`:
```json5
{
  chromeProfile: "Default"
}
```

### A2. Firefox Cookie Extraction

```bash
bird --firefox-profile "default-release" bookmarks --json
```

### A3. Brave Browser

```bash
bird --chrome-profile-dir "$HOME/Library/Application Support/BraveSoftware/Brave-Browser/Default" bookmarks --json
```

### A4. Manual Tokens

Extract from browser DevTools:
1. Open x.com → DevTools (F12) → Application → Cookies → `https://x.com`
2. Copy `auth_token` and `ct0`

```bash
bird --auth-token "YOUR_AUTH_TOKEN" --ct0 "YOUR_CT0" bookmarks --json
```

Or save to `.env.bird`:
```bash
export AUTH_TOKEN="abc123..."
export CT0="xyz789..."
```

### Verify

```bash
bird whoami
```

---

## Option B: X API v2 (no bird needed)

Use this if bird CLI isn't available or stops working.

### Step 1: Create an X Developer App

1. Go to [X Developer Console](https://developer.x.com/en/portal/petition/essential/basic-info)
2. Create a project + app
3. Under **User authentication settings**, configure:
   - **App permissions:** Read (minimum)
   - **Type of App:** Native App (public client) or Web App (confidential)
   - **Callback URL:** `http://localhost:8739/callback`
   - **Website URL:** anything (e.g., `https://example.com`)
4. Note your **Client ID** (and Client Secret if confidential app)

### Step 2: Authorize

Run the auth helper (one-time):

```bash
# Public client (no secret)
python3 scripts/x_api_auth.py --client-id "YOUR_CLIENT_ID"

# Confidential client (with secret)
python3 scripts/x_api_auth.py --client-id "YOUR_CLIENT_ID" --client-secret "YOUR_SECRET"
```

This opens your browser → you log in to X → authorize the app → tokens are saved automatically to `~/.config/x-bookmarks/tokens.json`.

### Step 3: Fetch Bookmarks

```bash
python3 scripts/fetch_bookmarks_api.py -n 20
```

Tokens auto-refresh. If they expire, re-run step 2.

### Alternative: Bearer Token Override

If you already have a valid Bearer token from another source:

```bash
X_API_BEARER_TOKEN="your_token" python3 scripts/fetch_bookmarks_api.py -n 20
```

### X API Pricing Note

As of 2025, the X API uses **pay-per-usage pricing**. Bookmark reads are low volume (a few calls/day for digests), so costs should be minimal. Check [X Developer Console](https://console.x.com) for current rates.

---

## Which Should I Use?

| | bird CLI | X API v2 |
|---|---|---|
| **Setup** | `npm i -g bird-cli` + browser login | Developer account + OAuth |
| **Auth** | Browser cookies (auto) | OAuth 2.0 tokens (auto-refresh) |
| **Cost** | Free | Pay-per-use (very cheap) |
| **Reliability** | Depends on cookie access | Official API, stable |
| **Extra data** | Thread context, folders | View count, bookmark count |
| **Unbookmark** | ✅ `bird unbookmark <id>` | ✅ via API (DELETE endpoint) |

**TL;DR:** Try bird first. If it doesn't work, use the API.
