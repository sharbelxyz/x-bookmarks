# X Bookmarks — Auth Setup

The `bird` CLI needs X/Twitter authentication to fetch your bookmarks. Choose one method:

## Option 1: Chrome Cookie Extraction (Recommended)

Easiest method — bird extracts cookies directly from your Chrome profile.

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
Now just run `bird bookmarks --json` with no flags.

## Option 2: Firefox Cookie Extraction

```bash
bird --firefox-profile "default-release" bookmarks --json
```

## Option 3: Manual Tokens

Extract from browser DevTools:

1. Open x.com, log in
2. DevTools (F12) → Application → Cookies → `https://x.com`
3. Copy:
   - `auth_token` → your AUTH_TOKEN
   - `ct0` → your CT0

```bash
bird --auth-token "YOUR_AUTH_TOKEN" --ct0 "YOUR_CT0" bookmarks --json
```

Or save to `.env.bird`:
```bash
export AUTH_TOKEN="abc123..."
export CT0="xyz789..."
```
Then: `source .env.bird && bird bookmarks --json`

## Option 4: Brave Browser

Brave uses the same cookie format as Chrome. Use:
```bash
bird --chrome-profile-dir "/Users/YOU/Library/Application Support/BraveSoftware/Brave-Browser/Default" bookmarks --json
```

macOS path: `~/Library/Application Support/BraveSoftware/Brave-Browser/Default`
Linux path: `~/.config/BraveSoftware/Brave-Browser/Default`

## Verifying Auth Works

```bash
bird whoami
```
Should print your X username. If this works, bookmarks will too.
