#!/usr/bin/env python3
"""
Fetch X/Twitter bookmarks through the Xquik API.

Usage:
    python3 fetch_bookmarks_xquik.py [--count 20] [--all]
        [--cursor CURSOR] [--folder-id ID]

Output: JSON array of bookmarks matching the other skill backends.

Environment:
    XQUIK_API_KEY - Xquik API key for an account with X connected
"""

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from urllib.error import HTTPError, URLError

XQUIK_BOOKMARKS_URL = "https://xquik.com/api/v1/x/bookmarks"
REQUEST_TIMEOUT_SECONDS = 30


class XquikApiError(RuntimeError):
    """Raised when Xquik bookmarks cannot be fetched safely."""


def _close_response(response):
    close = getattr(response, "close", None)
    if callable(close):
        close()


def _resolve_api_key(api_key):
    resolved = api_key if api_key is not None else os.environ.get("XQUIK_API_KEY")
    if resolved is None or not resolved.strip():
        raise XquikApiError("XQUIK_API_KEY is required for Xquik bookmarks")
    return resolved.strip()


def _decode_page(response):
    payload = response.read()
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    page = json.loads(payload)
    if not isinstance(page, dict):
        raise XquikApiError("Xquik returned an invalid bookmark response")

    tweets = page.get("tweets")
    has_next_page = page.get("has_next_page")
    next_cursor = page.get("next_cursor")
    if (
        not isinstance(tweets, list)
        or any(not isinstance(tweet, dict) for tweet in tweets)
        or not isinstance(has_next_page, bool)
        or not isinstance(next_cursor, str)
    ):
        raise XquikApiError("Xquik returned an invalid bookmark response")
    return page


def fetch_bookmarks_page(
    api_key,
    cursor=None,
    folder_id=None,
    opener=None,
):
    """Fetch and validate one Xquik bookmark page."""
    params = {}
    if cursor:
        params["cursor"] = cursor
    if folder_id:
        params["folderId"] = folder_id
    query = urllib.parse.urlencode(params)
    url = XQUIK_BOOKMARKS_URL if not query else f"{XQUIK_BOOKMARKS_URL}?{query}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "x-api-key": _resolve_api_key(api_key),
        },
    )
    transport = opener or urllib.request.urlopen
    response = None
    try:
        response = transport(request, timeout=REQUEST_TIMEOUT_SECONDS)
        return _decode_page(response)
    except HTTPError as error:
        _close_response(error)
        raise XquikApiError(
            f"Xquik bookmark request failed with HTTP {error.code}"
        ) from error
    except (URLError, TimeoutError) as error:
        raise XquikApiError(
            "Xquik bookmark request failed. Check connectivity and try again."
        ) from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise XquikApiError("Xquik returned an invalid bookmark response") from error
    finally:
        if response is not None:
            _close_response(response)


def fetch_bookmarks(
    api_key=None,
    count=20,
    all_pages=False,
    cursor=None,
    folder_id=None,
    opener=None,
):
    """Fetch Xquik bookmarks, following cursors until the requested limit."""
    if count < 1:
        raise ValueError("count must be at least 1")

    bookmarks = []
    next_cursor = cursor
    seen_cursors = {cursor} if cursor else set()

    while True:
        page = fetch_bookmarks_page(
            api_key,
            cursor=next_cursor,
            folder_id=folder_id,
            opener=opener,
        )
        tweets = page["tweets"]
        if all_pages:
            bookmarks.extend(tweets)
        else:
            remaining = count - len(bookmarks)
            bookmarks.extend(tweets[:remaining])
            if len(bookmarks) >= count:
                break

        next_cursor = page["next_cursor"]
        if not page["has_next_page"] or not next_cursor:
            break
        if next_cursor in seen_cursors:
            raise XquikApiError("Xquik returned a repeated bookmark cursor")
        seen_cursors.add(next_cursor)

    return bookmarks


def main():
    parser = argparse.ArgumentParser(description="Fetch X bookmarks through Xquik")
    parser.add_argument("-n", "--count", type=int, default=20)
    parser.add_argument("--all", action="store_true", help="Fetch all bookmarks")
    parser.add_argument("--cursor", help="Resume from this Xquik cursor")
    parser.add_argument("--folder-id", help="Fetch one bookmark folder")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()

    try:
        bookmarks = fetch_bookmarks(
            count=args.count,
            all_pages=args.all,
            cursor=args.cursor,
            folder_id=args.folder_id,
        )
    except (ValueError, XquikApiError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)

    indent = 2 if args.pretty else None
    print(json.dumps(bookmarks, indent=indent, ensure_ascii=False))


if __name__ == "__main__":
    main()
