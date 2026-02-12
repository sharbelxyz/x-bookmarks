#!/usr/bin/env python3
"""
Fetch X/Twitter bookmarks via X API v2.

Fallback for when bird CLI is unavailable. Uses OAuth 2.0 tokens
managed by x_api_auth.py.

Usage:
    python3 fetch_bookmarks_api.py [--count 20] [--all] [--since-id ID]

Output: JSON array of bookmarks matching bird CLI format for compatibility.

Environment:
    X_API_BEARER_TOKEN — override: use this Bearer token directly
    X_API_CLIENT_ID — used for token refresh if no bearer override
"""

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

# Import auth helper (same directory)
sys.path.insert(0, str(Path(__file__).parent))
from x_api_auth import get_valid_token, load_tokens, load_config

BASE_URL = "https://api.x.com/2"
MAX_RESULTS_PER_PAGE = 100  # X API max


def get_me(token: str) -> str:
    """Get authenticated user's ID."""
    req = urllib.request.Request(f"{BASE_URL}/users/me")
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
        return data["data"]["id"]


def fetch_bookmarks_page(
    token: str, user_id: str, max_results: int = 20,
    pagination_token: str = None, since_id: str = None
) -> dict:
    """Fetch a single page of bookmarks."""
    params = {
        "max_results": min(max_results, MAX_RESULTS_PER_PAGE),
        "tweet.fields": "created_at,public_metrics,entities,conversation_id,referenced_tweets",
        "user.fields": "username,name,profile_image_url,verified",
        "media.fields": "type,url,preview_image_url",
        "expansions": "author_id,attachments.media_keys,referenced_tweets.id",
    }
    if pagination_token:
        params["pagination_token"] = pagination_token
    if since_id:
        params["since_id"] = since_id

    qs = urllib.parse.urlencode(params)
    url = f"{BASE_URL}/users/{user_id}/bookmarks?{qs}"

    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")

    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def normalize_tweet(tweet: dict, users: dict, media: dict) -> dict:
    """Convert X API v2 tweet to bird-CLI-compatible format."""
    author_id = tweet.get("author_id", "")
    author = users.get(author_id, {})
    metrics = tweet.get("public_metrics", {})

    # Resolve media
    media_list = []
    media_keys = tweet.get("attachments", {}).get("media_keys", [])
    for mk in media_keys:
        m = media.get(mk, {})
        if m:
            media_list.append({
                "type": m.get("type", "photo"),
                "url": m.get("url") or m.get("preview_image_url", ""),
            })

    # Resolve quoted tweet
    quoted = None
    for ref in tweet.get("referenced_tweets", []):
        if ref.get("type") == "quoted":
            quoted = {"id": ref["id"]}

    result = {
        "id": tweet["id"],
        "text": tweet.get("text", ""),
        "createdAt": tweet.get("created_at", ""),
        "replyCount": metrics.get("reply_count", 0),
        "retweetCount": metrics.get("retweet_count", 0),
        "likeCount": metrics.get("like_count", 0),
        "bookmarkCount": metrics.get("bookmark_count", 0),
        "viewCount": metrics.get("impression_count", 0),
        "author": {
            "username": author.get("username", ""),
            "name": author.get("name", ""),
        },
        "media": media_list,
    }
    if quoted:
        result["quotedTweet"] = quoted

    return result


def fetch_all_bookmarks(token: str, count: int = 20, all_pages: bool = False, since_id: str = None) -> list:
    """Fetch bookmarks with pagination support."""
    user_id = get_me(token)
    bookmarks = []
    pagination_token = None
    remaining = count if not all_pages else float("inf")

    while remaining > 0:
        page_size = min(int(remaining), MAX_RESULTS_PER_PAGE)
        try:
            response = fetch_bookmarks_page(
                token, user_id, page_size, pagination_token, since_id
            )
        except urllib.error.HTTPError as e:
            if e.code == 429:
                print("Rate limited. Try again later.", file=sys.stderr)
                break
            raise

        data = response.get("data", [])
        if not data:
            break

        # Build lookup maps from includes
        includes = response.get("includes", {})
        users = {u["id"]: u for u in includes.get("users", [])}
        media = {m["media_key"]: m for m in includes.get("media", [])}

        for tweet in data:
            bookmarks.append(normalize_tweet(tweet, users, media))

        remaining -= len(data)

        # Check for next page
        meta = response.get("meta", {})
        pagination_token = meta.get("next_token")
        if not pagination_token:
            break

    return bookmarks


def main():
    parser = argparse.ArgumentParser(description="Fetch X bookmarks via API v2")
    parser.add_argument("-n", "--count", type=int, default=20, help="Number of bookmarks")
    parser.add_argument("--all", action="store_true", help="Fetch all bookmarks")
    parser.add_argument("--since-id", help="Only return bookmarks after this tweet ID")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")

    args = parser.parse_args()

    # Get token: env override > stored token
    token = os.environ.get("X_API_BEARER_TOKEN")
    if not token:
        token = get_valid_token()
    if not token:
        print(
            "No X API token found. Run x_api_auth.py first to authorize, "
            "or set X_API_BEARER_TOKEN env var.",
            file=sys.stderr,
        )
        sys.exit(1)

    bookmarks = fetch_all_bookmarks(
        token, count=args.count, all_pages=args.all, since_id=args.since_id
    )

    indent = 2 if args.pretty else None
    print(json.dumps(bookmarks, indent=indent, ensure_ascii=False))


if __name__ == "__main__":
    main()
