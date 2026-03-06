#!/usr/bin/env python3
"""
X Bookmark Manager — Multi-Account Service CLI

Onboard any Twitter/X account, harvest all bookmarks, categorize them,
serve a browseable app, and auto-sync new bookmarks.

Usage:
  python3 service.py add <username> <auth_token> <ct0>    # Add account
  python3 service.py remove <username>                     # Remove account
  python3 service.py list                                  # List accounts
  python3 service.py harvest <username> [--all]            # Harvest bookmarks
  python3 service.py harvest-all [--all]                   # Harvest all accounts
  python3 service.py sync [--all]                          # Sync + categorize
  python3 service.py serve [--port PORT]                   # Start browser app
  python3 service.py status                                # Show stats
  python3 service.py export <username> [--format csv|json] # Export user's bookmarks
  python3 service.py setup-auto                            # Install auto-sync daemon
  python3 service.py dm-sync                                # Sync DM conversations only
  python3 service.py reclassify-other                        # Re-classify "Other" tweets
  python3 service.py dashboard                              # Open native dashboard window
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import urllib.error
import shutil
from collections import Counter
from pathlib import Path

from json_filelock import locked_json_read, locked_json_write, atomic_json_write, locked_read_modify_write
from llm_provider import call_llm, get_batch_size, get_ollama_models

# ─── Config ────────────────────────────────────────────────────────────────────

SERVICE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = SERVICE_DIR / "data"
APP_DIR = SERVICE_DIR / "app"
ACCOUNTS_FILE = DATA_DIR / "accounts.json"
DM_CONFIG_FILE = DATA_DIR / "dm_config.json"
BIRD = "/opt/homebrew/bin/bird"

# X API constants for DM fetching
DM_CONVERSATION_URL = "https://x.com/i/api/1.1/dm/conversation/{conversation_id}.json"
TWEET_DETAIL_URL = "https://x.com/i/api/graphql/_NvJCnIjOW__EP5-RF197A/TweetDetail"
X_BEARER = "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
X_HEADERS_BASE = {
    "authorization": f"Bearer {X_BEARER}",
    "x-twitter-active-user": "yes",
    "x-twitter-auth-type": "OAuth2Session",
    "x-twitter-client-language": "en",
    "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
}
TWEET_URL_PATTERN = re.compile(r'https?://(?:twitter\.com|x\.com)/\w+/status/(\d+)')
URL_PATTERN = re.compile(r'https?://[^\s"<>]+')

# Gemini config (free tier: 15 RPM, 1500 RPD)
# Load from .env file if present
_env_file = Path(__file__).resolve().parent.parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

CATEGORIES = {
    "AI/ML": ["LLMs & Chatbots", "Image & Video Generation", "AI Tools & Products",
              "AI Research & Papers", "Prompt Engineering", "AI Agents & Automation", "AI News & Industry"],
    "Programming/Dev": ["Web Development", "Mobile Development", "Backend & APIs",
                        "DevOps & Infrastructure", "Open Source", "Programming Languages",
                        "Databases & Data", "Developer Tools"],
    "Marketing/Growth": ["SEO & Content Marketing", "Social Media Marketing", "Growth Hacking",
                         "Copywriting & Persuasion", "Email Marketing", "Analytics & Metrics", "Branding"],
    "Business/Startup": ["Startup Advice", "Fundraising & VC", "SaaS & Products",
                         "Entrepreneurship", "Revenue & Monetization", "Leadership & Management"],
    "Design/UI": ["UI/UX Design", "Graphic Design", "Design Tools", "Web Design & CSS", "Typography & Visual"],
    "Tools/Productivity": ["Productivity Apps", "Automation & Workflows", "Note-taking & PKM",
                           "Browser & Extensions", "Mac/PC Tools"],
    "Finance/Trading": ["Stock Market", "Personal Finance", "Trading Strategies", "Economic Analysis"],
    "Crypto/Web3": ["Bitcoin & Ethereum", "DeFi & NFTs", "Web3 & DAOs", "Crypto Trading"],
    "Content/Media": ["YouTube & Video", "Podcasts & Audio", "Newsletters & Writing",
                      "Creator Economy", "Social Media Tips"],
    "Lifestyle/Personal": ["Health & Fitness", "Books & Reading", "Philosophy & Mindset",
                           "Humor & Memes", "Travel & Food"],
    "Politics/News": ["Tech Policy & Regulation", "Current Events", "Geopolitics"],
    "Science/Education": ["Science & Research", "Education & Learning", "Math & Statistics"],
    "Other": ["Uncategorizable"],
}

CATEGORIES_PROMPT = json.dumps(CATEGORIES, indent=2, ensure_ascii=False)
VALID_CATS = set(CATEGORIES.keys())
VALID_SUBS = {cat: set(subs) for cat, subs in CATEGORIES.items()}


# ─── Helpers ───────────────────────────────────────────────────────────────────

def log(msg):
    print(f"[service] {msg}", flush=True)


def load_accounts():
    return locked_json_read(ACCOUNTS_FILE, default={})


def save_accounts(accounts):
    locked_json_write(ACCOUNTS_FILE, accounts, indent=2)


def user_data_file(username):
    return DATA_DIR / f"tweets_{username}.json"


def master_data_file():
    return DATA_DIR / "categorized_tweets.json"


def browser_file():
    return APP_DIR / "tweets.json"


# ─── Account Management ───────────────────────────────────────────────────────

def cmd_add(username, auth_token, ct0):
    """Add a new account."""
    accounts = load_accounts()

    # Test credentials with bird
    log(f"Testing credentials for @{username}...")
    try:
        result = subprocess.run(
            [BIRD, "bookmarks", "-n", "1", "--json",
             "--auth-token", auth_token, "--ct0", ct0],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            log(f"ERROR: bird failed: {result.stderr[:200]}")
            log("Check your auth_token and ct0 — they might be expired.")
            return False
        if not result.stdout.strip():
            log("WARNING: bird returned empty. Credentials may be invalid or account has no bookmarks.")
    except Exception as e:
        log(f"ERROR: {e}")
        return False

    accounts[username] = {
        "auth_token": auth_token,
        "ct0": ct0,
        "added": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    save_accounts(accounts)
    log(f"Account @{username} added successfully.")
    return True


def cmd_remove(username):
    """Remove an account."""
    accounts = load_accounts()
    if username not in accounts:
        log(f"Account @{username} not found.")
        return
    del accounts[username]
    save_accounts(accounts)
    log(f"Account @{username} removed. Data files preserved.")


def cmd_list():
    """List all accounts."""
    accounts = load_accounts()
    if not accounts:
        log("No accounts configured. Use: service.py add <username> <auth_token> <ct0>")
        return

    log(f"{len(accounts)} account(s):")
    for uname, info in accounts.items():
        data_file = user_data_file(uname)
        count = 0
        if data_file.exists():
            with open(data_file) as f:
                count = len(json.load(f))
        log(f"  @{uname} — {count} bookmarks — added {info.get('added', '?')}")


# ─── DM Config & Fetching ────────────────────────────────────────────────────

def load_dm_config():
    return locked_json_read(
        DM_CONFIG_FILE,
        default={"conversations": [], "last_synced": None, "last_message_ids": {}}
    )


def save_dm_config(config):
    locked_json_write(DM_CONFIG_FILE, config, indent=2)


def fetch_dm_messages(conversation_id, auth_token, ct0, since_id=None, max_pages=5):
    """Fetch recent messages from a DM conversation, stopping at since_id."""
    all_messages = []
    cursor = None

    for page in range(max_pages):
        url = DM_CONVERSATION_URL.format(conversation_id=conversation_id)
        params = ["count=100"]
        if cursor:
            params.append(f"max_id={cursor}")
        url += "?" + "&".join(params)

        headers = dict(X_HEADERS_BASE)
        headers["cookie"] = f"auth_token={auth_token}; ct0={ct0}"
        headers["x-csrf-token"] = ct0

        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            log(f"  DM fetch error: HTTP {e.code}")
            break
        except Exception as e:
            log(f"  DM fetch error: {e}")
            break

        entries = data.get("conversation_timeline", {}).get("entries", [])
        if not entries:
            break

        messages = []
        hit_since = False
        for entry in entries:
            msg = entry.get("message", {}).get("message_data", {})
            if msg:
                msg_id = msg.get("id")
                if since_id and msg_id and str(msg_id) <= str(since_id):
                    hit_since = True
                    break
                messages.append({
                    "id": msg_id,
                    "time": msg.get("time"),
                    "sender_id": msg.get("sender_id"),
                    "text": msg.get("text", ""),
                    "urls": [u.get("expanded_url", u.get("url", ""))
                             for u in msg.get("entities", {}).get("urls", [])],
                })

        all_messages.extend(messages)

        if hit_since:
            break

        min_entry_id = data.get("conversation_timeline", {}).get("min_entry_id")
        status = data.get("conversation_timeline", {}).get("status")
        if not min_entry_id or status == "AT_END" or cursor == min_entry_id:
            break
        cursor = min_entry_id

    return all_messages


def extract_tweet_ids_from_messages(messages):
    """Extract unique tweet IDs from DM messages."""
    tweet_ids = set()
    for msg in messages:
        # From entities
        for url in msg.get("urls", []):
            if url:
                m = TWEET_URL_PATTERN.search(url)
                if m:
                    tweet_ids.add(m.group(1))
        # From text
        for url in URL_PATTERN.findall(msg.get("text", "")):
            m = TWEET_URL_PATTERN.search(url)
            if m:
                tweet_ids.add(m.group(1))
    return tweet_ids


def hydrate_tweet(tweet_id, auth_token, ct0):
    """Fetch full tweet data for a single tweet ID via GraphQL."""
    import urllib.parse as urlparse

    features = {
        "rweb_video_screen_enabled": True,
        "responsive_web_graphql_timeline_navigation_enabled": True,
        "responsive_web_graphql_exclude_directive_enabled": True,
        "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
        "creator_subscriptions_tweet_preview_api_enabled": True,
        "communities_web_enable_tweet_community_results_fetch": True,
        "c9s_tweet_anatomy_moderator_badge_enabled": True,
        "responsive_web_jetfuel_frame": True,
    }
    field_toggles = {
        "withArticlePlainText": False,
        "withArticleRichContentState": False,
        "withGrokAnalyze": False,
        "withDisallowedReplyControls": False,
    }
    variables = {
        "focalTweetId": str(tweet_id),
        "with_rux_injections": False,
        "rankingMode": "Relevance",
        "includePromotedContent": False,
        "withCommunity": True,
        "withQuickPromoteEligibilityTweetFields": False,
        "withBirdwatchNotes": True,
        "withVoice": True,
    }

    params = urlparse.urlencode({
        "variables": json.dumps(variables),
        "features": json.dumps(features),
        "fieldToggles": json.dumps(field_toggles),
    })

    url = f"{TWEET_DETAIL_URL}?{params}"
    headers = dict(X_HEADERS_BASE)
    headers["cookie"] = f"auth_token={auth_token}; ct0={ct0}"
    headers["x-csrf-token"] = ct0

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            return None  # Rate limited
        return {}
    except Exception:
        return {}

    # Parse tweet from GraphQL response
    try:
        instructions = data.get("data", {}).get("tweetResult", {})
        if not instructions:
            instructions = data.get("data", {}).get("threaded_conversation_with_injections_v2", {})
            entries = instructions.get("instructions", [{}])[0].get("entries", [])
            for entry in entries:
                result = entry.get("content", {}).get("itemContent", {}).get("tweet_results", {}).get("result", {})
                if result:
                    core = result.get("core", {}).get("user_results", {}).get("result", {})
                    legacy = result.get("legacy", {})
                    user_legacy = core.get("legacy", {})

                    if legacy.get("id_str") == str(tweet_id) or result.get("rest_id") == str(tweet_id):
                        media_list = []
                        for m in legacy.get("entities", {}).get("media", []):
                            media_list.append({
                                "type": m.get("type", "photo"),
                                "url": m.get("media_url_https", ""),
                                "previewUrl": m.get("media_url_https", ""),
                            })

                        return {
                            "id": str(tweet_id),
                            "text": legacy.get("full_text", ""),
                            "createdAt": legacy.get("created_at", ""),
                            "likeCount": legacy.get("favorite_count", 0),
                            "retweetCount": legacy.get("retweet_count", 0),
                            "viewCount": result.get("views", {}).get("count", 0),
                            "bookmarkCount": legacy.get("bookmark_count", 0),
                            "author": {
                                "username": user_legacy.get("screen_name", ""),
                                "name": user_legacy.get("name", ""),
                            },
                            "media": media_list if media_list else [],
                            "sources": ["dm"],
                        }
    except Exception:
        pass

    return {}


def cmd_sync_dms():
    """Sync DM conversations: fetch new messages, extract tweets, hydrate, categorize."""
    dm_config = load_dm_config()
    accounts = load_accounts()

    if not dm_config.get("conversations"):
        log("No DM conversations configured.")
        return []

    master = master_data_file()
    existing_ids = set()
    if master.exists():
        with open(master) as f:
            existing = json.load(f)
        existing_ids = {t.get("id") for t in existing}

    all_new_tweets = []

    for conv in dm_config["conversations"]:
        conv_id = conv["id"]
        conv_name = conv.get("name", conv_id)
        auth_account = conv.get("auth_account", "")

        if auth_account not in accounts:
            log(f"  DM auth account @{auth_account} not found, skipping {conv_name}")
            continue

        acct = accounts[auth_account]
        since_id = dm_config.get("last_message_ids", {}).get(conv_id)

        log(f"Fetching DM conversation: {conv_name}...")
        messages = fetch_dm_messages(
            conv_id, acct["auth_token"], acct["ct0"],
            since_id=since_id, max_pages=5
        )
        log(f"  Got {len(messages)} new messages")

        if not messages:
            continue

        # Update last_message_id
        newest_id = max((m["id"] for m in messages if m.get("id")), default=since_id)
        dm_config.setdefault("last_message_ids", {})[conv_id] = str(newest_id)

        # Extract tweet IDs
        tweet_ids = extract_tweet_ids_from_messages(messages)
        new_tweet_ids = tweet_ids - existing_ids
        log(f"  Found {len(tweet_ids)} tweet links, {len(new_tweet_ids)} new")

        if not new_tweet_ids:
            continue

        # Hydrate new tweets (use accounts round-robin)
        acct_list = list(accounts.items())
        hydrated = []
        for i, tid in enumerate(new_tweet_ids):
            acct_name, acct_info = acct_list[i % len(acct_list)]
            tweet = hydrate_tweet(tid, acct_info["auth_token"], acct_info["ct0"])
            if tweet is None:
                # Rate limited, wait and retry with same account
                log(f"  Rate limited, waiting 60s...")
                time.sleep(60)
                tweet = hydrate_tweet(tid, acct_info["auth_token"], acct_info["ct0"])
            if tweet and tweet.get("id"):
                tweet["sources"] = [f"dm:{conv_name}"]
                hydrated.append(tweet)
            elif tweet == {}:
                # Tweet may be deleted or unavailable
                pass
            time.sleep(2)  # Be gentle with rate limits

        log(f"  Hydrated {len(hydrated)}/{len(new_tweet_ids)} tweets")
        all_new_tweets.extend(hydrated)

    # Save updated config
    dm_config["last_synced"] = time.strftime("%Y-%m-%d %H:%M:%S")
    save_dm_config(dm_config)

    return all_new_tweets


# ─── Fetch Bookmarks ──────────────────────────────────────────────────────────

def fetch_bookmarks(username, auth_token, ct0, fetch_all=False):
    """Fetch bookmarks for one account using bird CLI."""
    cmd = [
        BIRD, "bookmarks", "--json",
        "--auth-token", auth_token,
        "--ct0", ct0,
    ]
    if fetch_all:
        cmd.append("--all")
    else:
        cmd.extend(["-n", "100"])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            log(f"  bird error for @{username}: {result.stderr[:200]}")
            return []
        if not result.stdout.strip():
            log(f"  bird returned empty for @{username}")
            return []
        tweets = json.loads(result.stdout)
        if isinstance(tweets, dict) and "tweets" in tweets:
            tweets = tweets["tweets"]
        if not isinstance(tweets, list):
            return []
        for t in tweets:
            t["sources"] = [f"bookmark:{username}"]
        return tweets
    except subprocess.TimeoutExpired:
        log(f"  bird timeout for @{username}")
        return []
    except json.JSONDecodeError as e:
        log(f"  JSON parse error for @{username}: {e}")
        return []
    except Exception as e:
        log(f"  Error fetching @{username}: {e}")
        return []


def cmd_harvest(username, fetch_all=False):
    """Harvest bookmarks for a single account."""
    accounts = load_accounts()
    if username not in accounts:
        log(f"Account @{username} not found. Add it first.")
        return

    acct = accounts[username]
    log(f"Harvesting @{username} ({'all' if fetch_all else 'recent 100'})...")

    tweets = fetch_bookmarks(username, acct["auth_token"], acct["ct0"], fetch_all=fetch_all)
    log(f"  Fetched {len(tweets)} tweets")

    if not tweets:
        return

    # Load existing per-user data
    data_file = user_data_file(username)
    existing = []
    existing_ids = set()
    if data_file.exists():
        with open(data_file) as f:
            existing = json.load(f)
        existing_ids = {t.get("id") for t in existing}

    new_tweets = [t for t in tweets if t.get("id") and t["id"] not in existing_ids]
    log(f"  New: {len(new_tweets)} (existing: {len(existing)})")

    if not new_tweets:
        log("  No new bookmarks.")
        return

    # Categorize new tweets
    log(f"  Categorizing {len(new_tweets)} new tweets...")
    categorize_tweets_batch(new_tweets)

    # Merge
    existing.extend(new_tweets)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(data_file, "w") as f:
        json.dump(existing, f, ensure_ascii=False)
    log(f"  Saved {len(existing)} total tweets for @{username}")

    # Also merge into master file
    merge_into_master(new_tweets)


def cmd_harvest_all(fetch_all=False):
    """Harvest bookmarks for all accounts."""
    accounts = load_accounts()
    if not accounts:
        log("No accounts configured.")
        return

    for username in accounts:
        cmd_harvest(username, fetch_all=fetch_all)

    # Rebuild browser app
    rebuild_browser_app()


# ─── Categorization ───────────────────────────────────────────────────────────

# LLM calls handled by llm_provider.py (call_llm, call_ollama, call_gemini, call_claude)


def parse_llm_response(text):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r'^```\w*\n?', '', text)
        text = re.sub(r'\n?```$', '', text)
    text = text.strip()

    data = None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Try to find JSON object or array in the text
        match = re.search(r'[\[{][\s\S]*[\]}]', text)
        if match:
            try:
                data = json.loads(match.group())
            except json.JSONDecodeError:
                pass

    if data is None:
        return {}

    # Normalize: if it's already {tweet_id: {category, ...}} format, return as-is
    if isinstance(data, dict):
        # Check if it looks like {id: {category: ..., ...}} (expected format)
        first_val = next(iter(data.values()), None) if data else None
        if isinstance(first_val, dict) and "category" in first_val:
            return data
        # Handle flat object: {"id": "...", "category": "...", ...} or {"tweet_id": "...", ...}
        if "category" in data and ("id" in data or "tweet_id" in data):
            tid = str(data.get("id", data.get("tweet_id", "")))
            return {tid: {"category": data.get("category", "Other"),
                          "subcategory": data.get("subcategory", "Uncategorizable"),
                          "summary": data.get("summary", "")}}
        # Handle {"results": [...]} wrapper
        if "results" in data and isinstance(data["results"], list):
            data = data["results"]  # Fall through to list handling
        else:
            return data

    # Handle array of objects: [{"id": "...", "category": "...", ...}, ...]
    if isinstance(data, list):
        result = {}
        for item in data:
            if isinstance(item, dict) and ("id" in item or "tweet_id" in item):
                tid = str(item.get("id", item.get("tweet_id", "")))
                result[tid] = {"category": item.get("category", "Other"),
                               "subcategory": item.get("subcategory", "Uncategorizable"),
                               "summary": item.get("summary", "")}
        return result

    return {}


def validate_classification(cat, sub):
    if cat not in VALID_CATS:
        cat = "Other"
        sub = "Uncategorizable"
    elif sub not in VALID_SUBS.get(cat, set()):
        sub = list(VALID_SUBS[cat])[0] if VALID_SUBS.get(cat) else "Uncategorizable"
    if cat == "Arabic/Regional":
        cat = "Lifestyle/Personal"
        sub = "Philosophy & Mindset"
    return cat, sub


def classify_batch(tweets, model=None):
    """Classify a batch of tweets. Optional model overrides default LLM."""
    tweet_items = []
    for t in tweets:
        tid = t.get("id", "")
        text = (t.get("text", "") or "")[:400]
        text = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").replace("\r", "")
        author = ""
        if isinstance(t.get("author"), dict):
            author = t["author"].get("username", t["author"].get("userName", t["author"].get("name", "")))
        tweet_items.append(f'{{"id":"{tid}","text":"{text}","author":"@{author}"}}')

    tweets_json = "[" + ",".join(tweet_items) + "]"

    # Build example using first tweet's actual ID for clarity
    example_id = tweets[0].get("id", "123") if tweets else "123"
    prompt = f"""Classify each tweet by its TOPIC. Return ONLY valid JSON.

VALID CATEGORIES AND SUBCATEGORIES (use EXACTLY these strings):
{CATEGORIES_PROMPT}

CLASSIFICATION RULES:
- Classify by TOPIC, not by language
- Psychology/mental health/ADHD/therapy -> "Science/Education" > "Science & Research"
- Self-improvement/motivation/wisdom/mindset -> "Lifestyle/Personal" > "Philosophy & Mindset"
- Funny/relatable/meme/humor tweets -> "Lifestyle/Personal" > "Humor & Memes"
- Relationship/dating/love content -> "Lifestyle/Personal" > "Philosophy & Mindset"
- Job/career/resume tweets -> "Business/Startup" > "Entrepreneurship"
- Book/reading recommendations -> "Lifestyle/Personal" > "Books & Reading"
- Health/fitness/diet -> "Lifestyle/Personal" > "Health & Fitness"
- Food/cooking/recipes -> "Lifestyle/Personal" > "Travel & Food"
- News/current events/politics -> "Politics/News" > "Current Events"
- Religious/Islamic content -> "Lifestyle/Personal" > "Philosophy & Mindset"
- NSFW/adult content -> "Lifestyle/Personal" > "Humor & Memes"
- If tweet is ONLY a URL with no text -> "Other" > "Uncategorizable"
- Subcategory MUST be from the list above for the chosen category

TWEETS:
{tweets_json}

Return a JSON object where each key is the tweet's id. Example:
{{"{example_id}": {{"category": "AI/ML", "subcategory": "LLMs & NLP", "summary": "New AI model release"}}}}
Include ALL {len(tweets)} tweets. Return ONLY the JSON object."""

    response = call_llm(prompt, model=model)
    if not response:
        return {}
    return parse_llm_response(response)


def categorize_tweets_batch(tweets):
    """Categorize a list of tweets in batches."""
    batch_size = get_batch_size()
    batches = [tweets[i:i+batch_size] for i in range(0, len(tweets), batch_size)]

    for bi, batch in enumerate(batches):
        log(f"    Batch {bi+1}/{len(batches)} ({len(batch)} tweets)...")
        results = classify_batch(batch)

        for t in batch:
            tid = t.get("id", "")
            if tid in results:
                r = results[tid]
                cat, sub = validate_classification(
                    r.get("category", "Other"),
                    r.get("subcategory", "Uncategorizable")
                )
                t["category"] = cat
                t["subcategory"] = sub
                t["summary"] = r.get("summary", "")
            else:
                t["category"] = "Other"
                t["subcategory"] = "Uncategorizable"
                t["summary"] = ""

        classified = sum(1 for t in batch if t.get("category") != "Other")
        log(f"    Classified: {classified}/{len(batch)}")


# ─── Merge & Browser Export ───────────────────────────────────────────────────

def merge_into_master(new_tweets):
    """Merge new tweets into the master categorized_tweets.json (locked + atomic)."""
    master = master_data_file()

    def _merge(existing):
        existing_ids = {t.get("id") for t in existing}
        added = 0
        for t in new_tweets:
            tid = t.get("id")
            if tid and tid not in existing_ids:
                existing.append(t)
                existing_ids.add(tid)
                added += 1
            elif tid in existing_ids:
                # Update sources
                for et in existing:
                    if et.get("id") == tid:
                        for src in t.get("sources", []):
                            if src not in et.get("sources", []):
                                et.setdefault("sources", []).append(src)
                        break
        log(f"  Master file: +{added} new, {len(existing)} total")
        return existing

    locked_read_modify_write(master, _merge, ensure_ascii=False, default=[])


def rebuild_browser_app():
    """Rebuild the compact browser JSON from master data."""
    master = master_data_file()
    tweets = locked_json_read(master, default=[])
    if not tweets:
        log("No master data file. Run harvest first.")
        return

    compact = []
    for t in tweets:
        author = t.get("author", {})
        username = ""
        name = ""
        if isinstance(author, dict):
            username = author.get("username", author.get("userName", ""))
            name = author.get("name", "")

        thumb = ""
        media_type = ""
        if t.get("media"):
            m = t["media"][0]
            media_type = m.get("type", "")
            thumb = m.get("previewUrl", m.get("url", ""))
            if thumb and "twimg.com" in thumb and media_type == "photo" and ":small" not in thumb:
                thumb += ":small"

        rec = {
            "i": t.get("id", ""),
            "t": (t.get("text", "") or "")[:280],
            "u": username,
            "n": name,
            "d": t.get("createdAt", ""),
            "l": t.get("likeCount", 0),
            "r": t.get("retweetCount", 0),
            "v": t.get("viewCount", 0),
            "c": t.get("category", "Other"),
            "s": t.get("subcategory", ""),
            "m": t.get("summary", ""),
            "src": t.get("sources", []),
        }
        if thumb:
            rec["th"] = thumb
        if media_type:
            rec["mt"] = media_type
        compact.append(rec)

    compact.sort(key=lambda x: x.get("l", 0), reverse=True)

    atomic_json_write(browser_file(), compact, ensure_ascii=False, separators=(",", ":"))

    log(f"Browser app updated: {len(compact)} tweets")


# ─── Sync (incremental) ───────────────────────────────────────────────────────

def cmd_sync(fetch_all=False):
    """Sync all accounts: fetch new bookmarks, categorize, update browser."""
    accounts = load_accounts()
    if not accounts:
        log("No accounts configured.")
        return

    start = time.time()
    total_new = 0

    # Load master data
    master = master_data_file()
    existing_ids = set()
    existing = locked_json_read(master, default=[])
    if existing:
        existing_ids = {t.get("id") for t in existing}
        log(f"Existing tweets: {len(existing)}")

    all_new = []
    for username, acct in accounts.items():
        log(f"Fetching @{username}...")
        tweets = fetch_bookmarks(username, acct["auth_token"], acct["ct0"], fetch_all=fetch_all)
        log(f"  Got {len(tweets)} tweets")

        new_tweets = []
        for t in tweets:
            tid = t.get("id", "")
            if tid and tid not in existing_ids:
                new_tweets.append(t)
                existing_ids.add(tid)
            elif tid in existing_ids:
                src = f"bookmark:{username}"
                for et in existing:
                    if et.get("id") == tid:
                        if src not in et.get("sources", []):
                            et.setdefault("sources", []).append(src)
                        break

        log(f"  New: {len(new_tweets)}")
        all_new.extend(new_tweets)

    # Also sync DM conversations
    log("Syncing DM conversations...")
    dm_tweets = cmd_sync_dms()
    if dm_tweets:
        # Filter out any that were already added by bookmark sync
        dm_new = [t for t in dm_tweets if t.get("id") and t["id"] not in existing_ids]
        if dm_new:
            all_new.extend(dm_new)
            for t in dm_new:
                existing_ids.add(t["id"])
            log(f"  DM sync added {len(dm_new)} new tweets")
        else:
            # Update sources on existing tweets
            for t in dm_tweets:
                tid = t.get("id")
                if tid in existing_ids:
                    for et in existing:
                        if et.get("id") == tid:
                            for src in t.get("sources", []):
                                if src not in et.get("sources", []):
                                    et.setdefault("sources", []).append(src)
                            break

    if not all_new:
        log("No new bookmarks found.")
        rebuild_browser_app()
        log(f"Done in {time.time()-start:.1f}s")
        return

    log(f"Categorizing {len(all_new)} new tweets...")
    categorize_tweets_batch(all_new)

    existing.extend(all_new)
    locked_json_write(master, existing, ensure_ascii=False)
    log(f"Saved {len(existing)} tweets")

    rebuild_browser_app()

    elapsed = time.time() - start
    log(f"Done in {elapsed:.1f}s — {len(all_new)} new, {len(existing)} total")

    cats = Counter(t.get("category", "Other") for t in all_new)
    log("New tweets by category:")
    for cat, count in cats.most_common():
        log(f"  {cat}: {count}")


# ─── Serve ─────────────────────────────────────────────────────────────────────

def cmd_serve(port=8742):
    """Start the browser app server."""
    import http.server
    import functools

    if not browser_file().exists():
        log("No browser data. Run sync or harvest first.")
        rebuild_browser_app()

    os.chdir(APP_DIR)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(APP_DIR))
    server = http.server.HTTPServer(("0.0.0.0", port), handler)
    log(f"Serving bookmark browser at http://localhost:{port}")
    log("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("Server stopped.")


# ─── Status ────────────────────────────────────────────────────────────────────

def cmd_status():
    """Show status of all accounts and data."""
    accounts = load_accounts()
    master = master_data_file()

    log("=== X Bookmark Manager Status ===\n")

    # Accounts
    log(f"Accounts: {len(accounts)}")
    for uname, info in accounts.items():
        data_file = user_data_file(uname)
        count = 0
        if data_file.exists():
            with open(data_file) as f:
                count = len(json.load(f))
        log(f"  @{uname}: {count} bookmarks (added {info.get('added', '?')})")

    # Master data
    if master.exists():
        with open(master) as f:
            tweets = json.load(f)
        log(f"\nTotal tweets: {len(tweets)}")

        cats = Counter(t.get("category", "Other") for t in tweets)
        log("\nBy category:")
        for cat, count in cats.most_common():
            pct = count / len(tweets) * 100
            log(f"  {cat}: {count} ({pct:.1f}%)")

        sources = Counter()
        for t in tweets:
            for src in t.get("sources", []):
                sources[src] += 1
        log("\nBy source:")
        for src, count in sources.most_common():
            log(f"  {src}: {count}")
    else:
        log("\nNo data yet. Run: service.py harvest-all --all")

    # DM config
    dm_config = load_dm_config()
    if dm_config.get("conversations"):
        log(f"\nDM Conversations: {len(dm_config['conversations'])}")
        for conv in dm_config["conversations"]:
            last_mid = dm_config.get("last_message_ids", {}).get(conv["id"], "never")
            log(f"  {conv.get('name', conv['id'])}: auth=@{conv.get('auth_account', '?')}, last_msg={last_mid}")
        if dm_config.get("last_synced"):
            log(f"  Last DM sync: {dm_config['last_synced']}")

    # Sync log
    sync_log = DATA_DIR / "sync_log.json"
    if sync_log.exists():
        with open(sync_log) as f:
            history = json.load(f)
        if history:
            last = history[-1]
            log(f"\nLast sync: {last.get('timestamp', '?')} ({last.get('mode', '?')}, +{last.get('new_tweets', 0)} new)")

    # Scheduler
    try:
        result = subprocess.run(
            ["launchctl", "list", "com.mshrmnsr.bookmark-sync"],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            log("\nAuto-sync: ACTIVE")
        else:
            log("\nAuto-sync: INACTIVE (run: service.py setup-auto)")
    except Exception:
        pass


# ─── Export ────────────────────────────────────────────────────────────────────

def cmd_export(username, fmt="json"):
    """Export a user's bookmarks."""
    data_file = user_data_file(username)
    if not data_file.exists():
        # Try from master
        master = master_data_file()
        if not master.exists():
            log(f"No data for @{username}")
            return
        with open(master) as f:
            all_tweets = json.load(f)
        tweets = [t for t in all_tweets if f"bookmark:{username}" in t.get("sources", [])]
    else:
        with open(data_file) as f:
            tweets = json.load(f)

    output_dir = SERVICE_DIR / "output"
    output_dir.mkdir(exist_ok=True)

    if fmt == "csv":
        import csv
        out_file = output_dir / f"bookmarks_{username}.csv"
        with open(out_file, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["id", "text", "author", "date", "likes", "retweets",
                           "views", "category", "subcategory", "summary", "url"])
            for t in tweets:
                author = ""
                if isinstance(t.get("author"), dict):
                    author = t["author"].get("username", "")
                tid = t.get("id", "")
                writer.writerow([
                    tid,
                    (t.get("text", "") or "")[:500],
                    author,
                    t.get("createdAt", ""),
                    t.get("likeCount", 0),
                    t.get("retweetCount", 0),
                    t.get("viewCount", 0),
                    t.get("category", ""),
                    t.get("subcategory", ""),
                    t.get("summary", ""),
                    f"https://x.com/i/status/{tid}" if tid else "",
                ])
        log(f"Exported {len(tweets)} tweets to {out_file}")
    else:
        out_file = output_dir / f"bookmarks_{username}.json"
        with open(out_file, "w") as f:
            json.dump(tweets, f, indent=2, ensure_ascii=False)
        log(f"Exported {len(tweets)} tweets to {out_file}")


# ─── Auto-sync Setup ──────────────────────────────────────────────────────────

def cmd_setup_auto():
    """Install/update the auto-sync launchd daemon."""
    plist_path = Path.home() / "Library/LaunchAgents/com.mshrmnsr.bookmark-sync.plist"
    scheduler_path = SERVICE_DIR / "scripts/scheduler.py"

    # Unload if exists
    if plist_path.exists():
        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)

    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.mshrmnsr.bookmark-sync</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>{scheduler_path}</string>
    </array>
    <key>StandardOutPath</key>
    <string>{DATA_DIR}/sync_stdout.log</string>
    <key>StandardErrorPath</key>
    <string>{DATA_DIR}/sync_stderr.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
</dict>
</plist>"""

    with open(plist_path, "w") as f:
        f.write(plist)

    subprocess.run(["launchctl", "load", str(plist_path)])
    log(f"Auto-sync daemon installed and started.")
    log(f"  Plist: {plist_path}")
    log(f"  Scheduler: {scheduler_path} (Python)")
    log(f"  Syncs 1-2x daily at random intervals (1-10h)")
    log(f"  Logs: {DATA_DIR}/sync_stdout.log")


# ─── Reclassify "Other" Tweets ────────────────────────────────────────────────

def cmd_reclassify_other():
    """Re-classify 'Other' tweets using parallel Ollama models."""
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed

    master = master_data_file()
    tweets = locked_json_read(master, default=[])
    if not tweets:
        log("No master data file.")
        return

    url_re = re.compile(r'https?://\S+')

    # Find "Other" tweets that have meaningful text (>= 10 chars excluding URLs)
    other_tweets = []
    for t in tweets:
        if t.get("category") != "Other":
            continue
        text = (t.get("text", "") or "").strip()
        text_no_urls = url_re.sub("", text).strip()
        if len(text_no_urls) >= 10:
            other_tweets.append(t)

    total_other = sum(1 for t in tweets if t.get("category") == "Other")
    log(f"Found {len(other_tweets)} 'Other' tweets with meaningful text (of {total_other} total 'Other')")
    if not other_tweets:
        log("Nothing to reclassify.")
        return

    # Load checkpoint for resumability
    checkpoint_file = DATA_DIR / "reclassify_other_checkpoint.json"
    checkpoint = locked_json_read(checkpoint_file, default={})
    done_ids = set(checkpoint.keys())
    to_classify = [t for t in other_tweets if t.get("id", "") not in done_ids]
    log(f"  Checkpoint: {len(done_ids)} already processed, {len(to_classify)} remaining")

    if not to_classify:
        log("All already processed. Applying results...")
    else:
        batch_size = get_batch_size()
        batches = [to_classify[i:i+batch_size] for i in range(0, len(to_classify), batch_size)]
        total_batches = len(batches)

        # Parallel processing with multiple Ollama models
        models = get_ollama_models()
        num_workers = len(models)
        log(f"  Processing {total_batches} batches with {num_workers} parallel models: {', '.join(models)}")

        import time as _time
        start_time = _time.time()
        lock = threading.Lock()
        done_count = [0]
        reclassified_count = [0]

        def process_batch(bi, batch, model):
            results = classify_batch(batch, model=model)
            with lock:
                for t in batch:
                    tid = t.get("id", "")
                    if tid in results:
                        r = results[tid]
                        cat, sub = validate_classification(
                            r.get("category", "Other"),
                            r.get("subcategory", "Uncategorizable")
                        )
                        checkpoint[tid] = {
                            "category": cat,
                            "subcategory": sub,
                            "summary": r.get("summary", ""),
                        }
                        if cat != "Other":
                            reclassified_count[0] += 1
                    else:
                        checkpoint[tid] = {
                            "category": "Other",
                            "subcategory": "Uncategorizable",
                            "summary": "",
                        }

                done_count[0] += 1
                done = done_count[0]

                # Progress log every 5 batches or at end
                if done % 5 == 0 or done == total_batches:
                    elapsed = _time.time() - start_time
                    rate = done * batch_size / max(elapsed, 1) * 3600
                    log(f"  {done}/{total_batches} batches | reclassified: {reclassified_count[0]} | {rate:.0f} tweets/hr [{model}]")

                # Save checkpoint every 10 completed batches
                if done % 10 == 0:
                    locked_json_write(checkpoint_file, checkpoint, ensure_ascii=False)

        with ThreadPoolExecutor(max_workers=num_workers) as executor:
            futures = {}
            for bi, batch in enumerate(batches):
                model = models[bi % num_workers]
                f = executor.submit(process_batch, bi, batch, model)
                futures[f] = bi
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    log(f"  Worker error on batch {futures[future]}: {e}")

        # Final checkpoint save
        locked_json_write(checkpoint_file, checkpoint, ensure_ascii=False)
        elapsed = _time.time() - start_time
        log(f"  Done in {elapsed/60:.1f} min. Reclassified: {reclassified_count[0]}/{len(to_classify)}")

    # Apply reclassifications to master data
    updated = 0
    for t in tweets:
        tid = t.get("id", "")
        if tid in checkpoint and checkpoint[tid]["category"] != "Other":
            t["category"] = checkpoint[tid]["category"]
            t["subcategory"] = checkpoint[tid]["subcategory"]
            if checkpoint[tid].get("summary"):
                t["summary"] = checkpoint[tid]["summary"]
            updated += 1

    locked_json_write(master, tweets, ensure_ascii=False)
    rebuild_browser_app()

    remaining_other = sum(1 for t in tweets if t.get("category") == "Other")
    log(f"Reclassification complete: {updated} tweets moved from 'Other', {remaining_other} still 'Other'")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    cmd = sys.argv[1]

    if cmd == "add":
        if len(sys.argv) < 5:
            print("Usage: service.py add <username> <auth_token> <ct0>")
            return
        cmd_add(sys.argv[2], sys.argv[3], sys.argv[4])

    elif cmd == "remove":
        if len(sys.argv) < 3:
            print("Usage: service.py remove <username>")
            return
        cmd_remove(sys.argv[2])

    elif cmd == "list":
        cmd_list()

    elif cmd == "harvest":
        if len(sys.argv) < 3:
            print("Usage: service.py harvest <username> [--all]")
            return
        cmd_harvest(sys.argv[2], fetch_all="--all" in sys.argv)

    elif cmd == "harvest-all":
        cmd_harvest_all(fetch_all="--all" in sys.argv)

    elif cmd == "sync":
        cmd_sync(fetch_all="--all" in sys.argv)

    elif cmd == "serve":
        port = 8742
        if "--port" in sys.argv:
            idx = sys.argv.index("--port")
            if idx + 1 < len(sys.argv):
                port = int(sys.argv[idx + 1])
        cmd_serve(port)

    elif cmd == "status":
        cmd_status()

    elif cmd == "export":
        if len(sys.argv) < 3:
            print("Usage: service.py export <username> [--format csv|json]")
            return
        fmt = "json"
        if "--format" in sys.argv:
            idx = sys.argv.index("--format")
            if idx + 1 < len(sys.argv):
                fmt = sys.argv[idx + 1]
        cmd_export(sys.argv[2], fmt)

    elif cmd == "setup-auto":
        cmd_setup_auto()

    elif cmd == "dm-sync":
        dm_tweets = cmd_sync_dms()
        if dm_tweets:
            log(f"Categorizing {len(dm_tweets)} DM tweets...")
            categorize_tweets_batch(dm_tweets)
            merge_into_master(dm_tweets)
            rebuild_browser_app()
            log(f"DM sync complete: {len(dm_tweets)} new tweets")
        else:
            log("No new DM tweets found.")

    elif cmd == "reclassify-other":
        cmd_reclassify_other()

    elif cmd == "dashboard":
        dashboard_script = SERVICE_DIR / "scripts" / "dashboard.py"
        subprocess.Popen([sys.executable, str(dashboard_script)])
        log("Dashboard launched.")

    elif cmd == "build-app":
        build_script = SERVICE_DIR / "build_app.py"
        subprocess.run([sys.executable, str(build_script)])

    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)


if __name__ == "__main__":
    main()
