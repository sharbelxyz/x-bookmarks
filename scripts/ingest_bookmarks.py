#!/usr/bin/env python3
"""Bring saved bookmarks into the radar: live saves and the historical archive.

Two modes, deliberately different because the inputs are different:

``--live``     New bookmarks, read through the same ``bird`` path the group
               reader uses. Small, current, high-signal, so they take the normal
               high-recall route: anything the rules cannot confirm goes to the
               semantic reviewer, exactly like a group share.

``--archive``  The historical corpus in ``data/categorized_tweets.json``.
               25,505 items whose newest entry is 2026-04-15 and which is ~65%
               personal content. Two things follow from that:

               1. It is already hydrated (text, author, media, counts are all
                  present), so importing costs zero X reads.
               2. Sending ~20,000 unmatched rows to the semantic reviewer would
                  cost days of calls and real money for a dead archive. So in
                  archive mode a rule miss is **terminal** — the row lands as
                  ``irrelevant`` with ``decision_source='archive-rules'`` rather
                  than entering ``pending_review``. This is a deliberate
                  departure from the group's high-recall policy, justified by
                  the corpus being historical rather than live, and reversible:
                  ``--semantic`` requeues any slice for full review later.

Both modes tag ``resources.source`` so the dashboard can keep the briefing to
the group by default while the archive stays searchable underneath.

Notifications: the archive arms its own cutover before inserting, so importing
25,505 rows cannot produce a single Telegram message.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple


os.umask(0o077)

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
ARCHIVE_PATH = ROOT / "data" / "categorized_tweets.json"

SOURCE_LIVE = "bookmark"
SOURCE_ARCHIVE = "bookmark-archive"
COMMIT_EVERY = 1000
LIVE_FETCH_LIMIT = 100

sys.path.insert(0, str(SCRIPTS))
import group_monitor as monitor  # noqa: E402


def utc_now() -> str:
    return monitor.utc_now()


def _created_at_ms(tweet: Dict[str, Any]) -> Optional[int]:
    raw = str(tweet.get("createdAt") or "")
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return int(dt.datetime.strptime(raw, fmt).timestamp() * 1000)
        except ValueError:
            continue
    return None


def normalize_tweet(tweet: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Shape a stored tweet like the DM attachments the pipeline already knows.

    Reusing that shape is the point: typing, scoring, enrichment and the
    dashboard then need no special cases for bookmarks.
    """
    tweet_id = str(tweet.get("id") or tweet.get("id_str") or "")
    if not tweet_id.isdigit():
        return None
    author = tweet.get("author")
    if isinstance(author, dict):
        username = str(author.get("username") or author.get("screen_name") or "")
        name = str(author.get("name") or "")
    else:
        username, name = str(author or ""), ""
    media = []
    for item in tweet.get("media") or []:
        if isinstance(item, dict):
            url = item.get("url") or item.get("media_url_https") or ""
            if url:
                media.append({"type": item.get("type") or "photo", "url": url, "previewUrl": url})
    quoted = tweet.get("quotedTweet") if isinstance(tweet.get("quotedTweet"), dict) else None
    return {
        "id": tweet_id,
        "text": str(tweet.get("text") or tweet.get("full_text") or ""),
        "createdAt": tweet.get("createdAt") or "",
        "likeCount": tweet.get("likeCount") or 0,
        "retweetCount": tweet.get("retweetCount") or 0,
        "replyCount": tweet.get("replyCount") or 0,
        "author": {"username": username, "name": name},
        "media": media,
        "quotedTweet": quoted,
        "_source": "bookmark",
    }


def iter_archive(path: Path = ARCHIVE_PATH) -> Iterator[Dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                yield item


def arm_source_cutover(conn, source: str) -> None:
    """Mark everything already present for this source as notified.

    Without this, a bulk import would look like thousands of brand-new relevant
    shares and produce a notification flood.
    """
    now = utc_now()
    with conn:
        conn.execute(
            "UPDATE message_resources SET notified_at = ? WHERE notified_at IS NULL "
            "AND resource_id IN (SELECT resource_id FROM resources WHERE source = ?)",
            (now, source),
        )
        conn.execute(
            "UPDATE resources SET notified_at = ? WHERE notified_at IS NULL AND source = ?",
            (now, source),
        )
        monitor.set_meta(conn, "cutover_{}".format(source.replace("-", "_")), now)


def ingest(
    conn,
    profile: Dict[str, Any],
    tweets: Iterable[Dict[str, Any]],
    source: str,
    terminal_on_rule_miss: bool,
    limit: Optional[int] = None,
    progress_every: int = 5000,
) -> Dict[str, int]:
    """Insert tweets as resources, idempotently.

    A bookmark that is also a group share merges into the existing resource
    rather than duplicating it: the resource keeps its original source, and the
    bookmark simply becomes another occurrence.
    """
    seen = skipped = inserted = merged = 0
    captured = utc_now()
    pending: List[Tuple[Any, ...]] = []
    started = time.monotonic()

    def flush() -> None:
        if pending:
            conn.commit()
            pending.clear()

    for tweet in tweets:
        if limit is not None and seen >= limit:
            break
        seen += 1
        payload = normalize_tweet(tweet)
        if payload is None:
            skipped += 1
            continue
        tweet_id = payload["id"]
        resource_id = "tweet:" + tweet_id
        message_id = "bookmark:" + tweet_id
        sender_id = "bookmark"
        title, author, content = monitor._tweet_content(payload)

        # Synthetic message keeps the message_resources foreign key intact, so
        # every existing join and count keeps working untouched.
        conn.execute(
            "INSERT OR IGNORE INTO messages(message_id, conversation_id, sent_at_ms, sender_id, "
            "is_owner, text, urls_json, captured_at) VALUES(?, ?, ?, ?, 1, ?, '[]', ?)",
            (message_id, source, _created_at_ms(tweet), sender_id, payload["text"], captured),
        )
        conn.execute(
            "INSERT OR IGNORE INTO senders(sender_id, username, display_name, avatar_url, is_owner, updated_at) "
            "VALUES(?, 'bookmarks', 'Your bookmarks', '', 1, ?)",
            (sender_id, captured),
        )

        existing = conn.execute(
            "SELECT status, source FROM resources WHERE resource_id = ?", (resource_id,)
        ).fetchone()
        if existing:
            merged += 1
        else:
            status = "pending_review"
            conn.execute(
                "INSERT INTO resources(resource_id, kind, canonical_url, tweet_id, "
                "first_message_id, last_message_id, sender_id, source_text, status, "
                "payload_json, title, author, content_text, source, first_seen_at, updated_at) "
                "VALUES(?, 'tweet', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    resource_id,
                    "https://x.com/i/status/{}".format(tweet_id),
                    tweet_id,
                    message_id,
                    message_id,
                    sender_id,
                    payload["text"],
                    status,
                    json.dumps(payload, ensure_ascii=False),
                    title or None,
                    author or None,
                    content or None,
                    source,
                    captured,
                    captured,
                ),
            )
            inserted += 1
        conn.execute(
            "INSERT OR IGNORE INTO message_resources(message_id, resource_id) VALUES(?, ?)",
            (message_id, resource_id),
        )
        pending.append((resource_id,))
        if len(pending) >= COMMIT_EVERY:
            flush()
        if progress_every and seen % progress_every == 0:
            print("  {:>6} read · {:>6} new · {:>5} already known · {:.0f}s".format(
                seen, inserted, merged, time.monotonic() - started), flush=True)
    flush()
    conn.commit()

    rules = monitor.apply_rule_classification(conn, profile)

    terminated = 0
    if terminal_on_rule_miss:
        # A dead archive does not justify thousands of model calls. Anything the
        # rules did not accept ends here, labelled so it can be requeued later.
        with conn:
            cursor = conn.execute(
                "UPDATE resources SET status = 'irrelevant', score = 0, "
                "project_areas_json = '[]', "
                "reasons_json = '[\"Archive import: no project or AI rule matched.\"]', "
                "decision_source = 'archive-rules', updated_at = ? "
                "WHERE status = 'pending_review' AND source = ?",
                (utc_now(), source),
            )
            terminated = cursor.rowcount
    return {
        "read": seen,
        "inserted": inserted,
        "already_known": merged,
        "unusable": skipped,
        "relevant_by_rules": rules.get("relevant_by_rules", 0),
        "terminated_by_archive_policy": terminated,
        "seconds": round(time.monotonic() - started, 1),
    }


def ingest_live(conn, profile: Dict[str, Any], limit: int = LIVE_FETCH_LIMIT) -> Dict[str, Any]:
    import service as legacy

    accounts = monitor.load_json(monitor.ACCOUNTS_PATH)
    names = profile.get("bird_accounts") or [o["username"] for o in profile["owners"]]
    collected: List[Dict[str, Any]] = []
    errors: List[str] = []
    for name in names:
        account = accounts.get(name)
        if not account:
            continue
        try:
            collected.extend(legacy.fetch_bookmarks(name, account["auth_token"], account["ct0"]))
        except Exception as exc:  # noqa: BLE001 - one bad account must not stop the rest
            errors.append("{}: {}".format(name, str(exc)[:120]))
    result = ingest(conn, profile, collected[:limit], SOURCE_LIVE, terminal_on_rule_miss=False, progress_every=0)
    result["accounts"] = len(names)
    if errors:
        result["errors"] = errors
    return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="fetch and ingest new bookmarks")
    parser.add_argument("--archive", action="store_true", help="ingest data/categorized_tweets.json")
    parser.add_argument("--limit", type=int, default=None, help="cap items processed (testing)")
    parser.add_argument("--no-export", action="store_true", help="skip rebuilding dashboard artifacts")
    args = parser.parse_args(argv)
    if not args.live and not args.archive:
        parser.error("choose --live and/or --archive")

    profile = monitor.load_profile()
    conn = monitor.connect_db()
    summary: Dict[str, Any] = {}
    try:
        with monitor.exclusive_run_lock():
            if args.archive:
                if not ARCHIVE_PATH.exists():
                    parser.error("archive not found: {}".format(ARCHIVE_PATH))
                print("Importing archive from {} …".format(ARCHIVE_PATH.name), flush=True)
                summary["archive"] = ingest(
                    conn, profile, iter_archive(), SOURCE_ARCHIVE,
                    terminal_on_rule_miss=True, limit=args.limit,
                )
                arm_source_cutover(conn, SOURCE_ARCHIVE)
            if args.live:
                summary["live"] = ingest_live(conn, profile)
                arm_source_cutover(conn, SOURCE_LIVE)
            if not args.no_export:
                summary["export"] = monitor.export_relevant(conn, profile)
            summary["status"] = monitor.status_snapshot(conn)
    finally:
        conn.close()
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
