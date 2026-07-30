# X Bookmarks — Auth Setup

Three ways to authenticate. Pick whichever works for you.

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

## Option B: Xquik (no browser cookies)

Use Xquik when you want direct bookmark reads without local browser cookies.

### Step 1: Connect X and Create a Key

1. Connect your X account in the [Xquik dashboard](https://xquik.com/dashboard/account?tab=x-accounts).
2. Create an API key.
3. Export it in your shell:

```bash
read -rsp "Xquik API key: " XQUIK_API_KEY
export XQUIK_API_KEY
printf '\n'
```

Keep the key outside source code, shell history, and committed files.

### Step 2: Fetch Bookmarks

```bash
python3 scripts/fetch_bookmarks_xquik.py -n 20
```

Fetch every page, resume from a cursor, or select one folder:

```bash
python3 scripts/fetch_bookmarks_xquik.py --all
python3 scripts/fetch_bookmarks_xquik.py --cursor "CURSOR"
python3 scripts/fetch_bookmarks_xquik.py --folder-id "FOLDER_ID"
```

See the [Xquik Bookmarks API](https://docs.xquik.com/api-reference/x/bookmarks) for the public response contract.

---

## Option C: X API v2 (no bird needed)

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

Check the [X Developer Console](https://console.x.com) for current access and pricing.

---

## Which Should I Use?

| | bird CLI | Xquik | X API v2 |
|---|---|---|---|
| **Setup** | npm package + browser login | API key + connected X account | Developer account + OAuth |
| **Auth** | Browser cookies | Xquik API key | OAuth 2.0 tokens |
| **Pagination** | CLI-managed | Cursor-managed | Cursor-managed |
| **Folders** | Supported | Supported | Depends on X API access |
| **Extra data** | Thread context | Engagement and media | Engagement and media |

**TL;DR:** Try bird first. Use Xquik when browser-cookie access is unavailable. Keep X API v2 as another direct option.

Xquik is an independent third-party service. Not affiliated with X Corp. "Twitter" and "X" are trademarks of X Corp.
