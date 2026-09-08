#!/usr/bin/env python3
"""Durable monitor for resources shared in a private X group DM.

The legacy bookmark sync advanced its DM cursor before linked tweets were
successfully read. This worker separates durable message capture from resource
hydration and classification so a transient X failure cannot lose a share.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import recommend_eligibility
from resource_typing import (
    REPO_HOSTS,
    classify_resource_type,
    compute_pick_score,
    external_urls_from_text,
    parse_iso,
    short_link_label,
    tool_key,
)


os.umask(0o077)

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
DATA_DIR = ROOT / "data" / "group-monitor"
DB_PATH = DATA_DIR / "group-monitor.sqlite3"
PROFILE_PATH = ROOT / "config" / "group-filter-profile.json"
VERDICTS_PATH = ROOT / "config" / "verdicts.json"
OUTCOMES_PATH = ROOT / "config" / "outcomes.json"
ACCOUNTS_PATH = ROOT / "data" / "accounts.json"
DM_CONFIG_PATH = ROOT / "data" / "dm_config.json"
BIRD = Path("/opt/homebrew/bin/bird")
NOTIFY = Path.home() / "assistant" / "scripts" / "telegram-notify.sh"
CAPTURE_SCOPE_VERSION = "all-senders-v1"
# Mirrors the crontab block managed by manage_group_filter_schedule.py; the
# dashboard uses it to show "next scan" and to decide when data is stale.
DASHBOARD_SCHEDULE = {"cronMinutes": [17, 47], "cadenceMinutes": 30, "staleAfterMinutes": 90}
# The briefing must stay a briefing. Group shares are always carried in full;
# the imported bookmark archive is 25k rows, so only its relevant items travel,
# capped. Everything else stays queryable in SQLite and in all-resources.csv.
DASHBOARD_GROUP_SOURCES = {"group"}
DASHBOARD_BOOKMARK_CAP = 2000

TWEET_RE = re.compile(
    r"https?://(?:www\.)?(?:x|twitter)\.com/(?:i/)?(?:[^/\s]+/)?status/(\d+)",
    re.IGNORECASE,
)
URL_RE = re.compile(r"https?://[^\s\"<>]+", re.IGNORECASE)
TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "igshid",
    "mc_cid",
    "mc_eid",
    "ref_src",
    "s",
}
VALID_STATUSES = {
    "pending_hydration",
    "pending_review",
    "relevant",
    "irrelevant",
    "unavailable",
}
# Content-processing evidence states, deliberately separate from the relevance
# lifecycle above: "we could not read it" must never masquerade as "it is
# irrelevant". Vocabulary shared with the C5 evidence contract.
#   NULL        extraction not tracked (legacy rows, kinds with nothing to fetch)
#   pending     content known but not yet read (media bytes, URL destination)
#   ok          content was actually read and evidence captured
#   unsupported cannot be interpreted with approved local capabilities; detail says why
#   failed      an attempted extraction errored; detail carries the reason
EXTRACTION_STATES = {"pending", "ok", "unsupported", "failed"}

# ---------------------------------------------------------------------------
# Occurrence identity and ordering (contract C1, revision c1)
#
# A resource occurrence is keyed by the message that carried it. Two message-id
# types exist and they live in different sequence spaces:
#   * Group DM ids — all-digit strings from X's DM sequence (e.g. "101").
#     Only these carry chronology: they order numerically among themselves and
#     they alone drive the durable fetch cursor and notification cutovers.
#   * Synthetic occurrence ids — "<origin>:<suffix>" (e.g. "bookmark:999"),
#     minted by ingest_bookmarks for saved-bookmark occurrences. They record
#     provenance, not a DM-sequence position, so they must NEVER be compared
#     numerically against group ids (int("bookmark:999") was audit A01: one
#     overlapping bookmark aborted the whole unrelated group batch).
#
# Ordering rule for the last_message_id convenience column: any group id
# outranks every synthetic id; group ids compare numerically; synthetic ids
# compare lexically among themselves (stable, chronology-free). first_message_id
# keeps the first-arrival occurrence untouched, and the full provenance record
# is message_resources — every occurrence of both kinds is preserved there
# regardless of which one last_message_id points at.
# ---------------------------------------------------------------------------


def is_group_message_id(message_id: Any) -> bool:
    """True only for real group-DM message ids (all digits)."""
    return str(message_id).isdigit()


def message_order_key(message_id: Any) -> Tuple[int, int, str]:
    """Typed sort key implementing the C1 occurrence-ordering rule."""
    text = str(message_id)
    if text.isdigit():
        return (1, int(text), "")
    return (0, 0, text)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def load_profile(path: Path = PROFILE_PATH) -> Dict[str, Any]:
    profile = load_json(path)
    required = {"conversation", "owners", "bootstrap", "selection"}
    missing = sorted(required - set(profile))
    if missing:
        raise RuntimeError("profile missing fields: " + ", ".join(missing))
    if profile["conversation"].get("capture_scope") != "all_senders":
        raise RuntimeError("conversation capture_scope must be all_senders")
    return profile


def load_verdicts(path: Path = VERDICTS_PATH) -> Dict[str, Dict[str, Any]]:
    """Hand-checked verdicts keyed by tool, so one entry covers every post that links it."""
    try:
        payload = load_json(path)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    entries = payload.get("verdicts") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return {}
    table: Dict[str, Dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        key = tool_key("https://" + str(entry.get("key") or "").lstrip("/"))
        if not key:
            continue
        table[key.lower()] = entry
    return table


def load_negative_proposals(path: Path = DATA_DIR / "negative-proposals.json") -> List[Dict[str, Any]]:
    """Exclusion rules the learner suggests. Proposals only; nothing acts on them
    until a human approves one through /api/negative-term."""
    try:
        payload = load_json(path)
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    proposals = payload.get("proposals") if isinstance(payload, dict) else None
    return proposals if isinstance(proposals, list) else []


def load_outcomes(path: Path = OUTCOMES_PATH) -> Dict[str, Dict[str, Any]]:
    """What actually happened after a tool was recommended, keyed by tool.

    Deliberately a separate file from verdicts: clearing a verdict must not
    erase the record that you tried the thing.
    """
    try:
        payload = load_json(path)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    entries = payload.get("outcomes") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return {}
    table: Dict[str, Dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        key = tool_key("https://" + str(entry.get("key") or "").lstrip("/"))
        if key:
            table[key.lower()] = entry
    return table


def connect_db(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    conn = sqlite3.connect(str(path), timeout=30)
    path.chmod(0o600)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS messages (
            message_id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            sent_at_ms INTEGER,
            sender_id TEXT NOT NULL,
            is_owner INTEGER NOT NULL,
            text TEXT NOT NULL,
            urls_json TEXT NOT NULL,
            captured_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS senders (
            sender_id TEXT PRIMARY KEY,
            username TEXT,
            display_name TEXT,
            avatar_url TEXT,
            is_owner INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS resources (
            resource_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            canonical_url TEXT,
            tweet_id TEXT,
            first_message_id TEXT NOT NULL,
            last_message_id TEXT NOT NULL,
            sender_id TEXT NOT NULL,
            source_text TEXT NOT NULL,
            status TEXT NOT NULL,
            hydration_attempts INTEGER NOT NULL DEFAULT 0,
            next_retry_at TEXT,
            last_error TEXT,
            payload_json TEXT,
            title TEXT,
            author TEXT,
            content_text TEXT,
            score INTEGER,
            project_areas_json TEXT,
            reasons_json TEXT,
            decision_source TEXT,
            first_seen_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            notified_at TEXT
        );

        CREATE TABLE IF NOT EXISTS message_resources (
            message_id TEXT NOT NULL REFERENCES messages(message_id) ON DELETE CASCADE,
            resource_id TEXT NOT NULL REFERENCES resources(resource_id) ON DELETE CASCADE,
            notified_at TEXT,
            PRIMARY KEY (message_id, resource_id)
        );

        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            outcome TEXT NOT NULL,
            details_json TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_resources_status ON resources(status);
        CREATE INDEX IF NOT EXISTS idx_resources_last_message ON resources(last_message_id);
        CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(sender_id);
        -- message_resources' primary key is (message_id, resource_id), so a lookup
        -- by resource_id alone cannot use it. select_resource_rows does exactly that
        -- four times per row; without this index that is a full scan per resource,
        -- which is unnoticeable at a thousand rows and takes minutes at thirty.
        CREATE INDEX IF NOT EXISTS idx_message_resources_resource
            ON message_resources(resource_id);
        """
    )
    resource_columns = {row["name"] for row in conn.execute("PRAGMA table_info(resources)")}
    if "source" not in resource_columns:
        # Where a resource came from: the group chat, a live bookmark, or the
        # historical bookmark archive. Existing rows are all group shares.
        conn.execute("ALTER TABLE resources ADD COLUMN source TEXT NOT NULL DEFAULT 'group'")
        conn.commit()
    conn.execute("CREATE INDEX IF NOT EXISTS idx_resources_source ON resources(source)")
    if "extraction_state" not in resource_columns:
        # Content-processing evidence (see EXTRACTION_STATES): whether linked
        # content was actually read, kept separate from the relevance status so
        # unreadable never silently becomes irrelevant. NULL on legacy rows.
        conn.execute("ALTER TABLE resources ADD COLUMN extraction_state TEXT")
        conn.execute("ALTER TABLE resources ADD COLUMN extraction_detail TEXT")
        conn.execute("ALTER TABLE resources ADD COLUMN extraction_checked_at TEXT")
        conn.commit()
    message_resource_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(message_resources)")
    }
    if "notified_at" not in message_resource_columns:
        conn.execute("ALTER TABLE message_resources ADD COLUMN notified_at TEXT")
        conn.commit()
    return conn


def get_meta(conn: sqlite3.Connection, key: str, default: Optional[str] = None) -> Optional[str]:
    row = conn.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO metadata(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


@contextlib.contextmanager
def exclusive_run_lock() -> Iterable[None]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    handle = (DATA_DIR / "worker.lock").open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise RuntimeError("group monitor is already running") from exc
    try:
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


@dataclass
class FetchResult:
    messages: List[Dict[str, Any]]
    pages: int
    reached_checkpoint: bool
    newest_message_id: Optional[str]
    oldest_message_id: Optional[str]
    senders: Dict[str, Dict[str, Any]] = field(default_factory=dict)


def normalize_dm_user(sender_id: Any, user: Any) -> Optional[Dict[str, str]]:
    if not isinstance(user, dict):
        return None
    normalized_id = str(user.get("id_str") or user.get("id") or sender_id or "")
    if not normalized_id.isdigit():
        return None
    return {
        "sender_id": normalized_id,
        "username": str(user.get("screen_name") or ""),
        "display_name": str(user.get("name") or ""),
        "avatar_url": str(
            user.get("profile_image_url_https")
            or user.get("profile_image_url")
            or ""
        ),
    }


def normalize_dm_attachment(attachment: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(attachment, dict):
        return None
    tweet = attachment.get("tweet")
    if not isinstance(tweet, dict):
        return None
    status = tweet.get("status")
    if not isinstance(status, dict):
        return None
    tweet_id = str(status.get("id_str") or status.get("id") or tweet.get("id") or "")
    if not tweet_id.isdigit():
        return None
    extended = status.get("extended_tweet") if isinstance(status.get("extended_tweet"), dict) else {}
    text = str(
        extended.get("full_text")
        or status.get("full_text")
        or status.get("text")
        or ""
    )
    user = status.get("user") if isinstance(status.get("user"), dict) else {}
    media_source = status.get("extended_entities")
    if not isinstance(media_source, dict):
        media_source = status.get("entities") if isinstance(status.get("entities"), dict) else {}
    media = []
    for item in media_source.get("media", []) or []:
        if not isinstance(item, dict):
            continue
        media.append(
            {
                "type": item.get("type") or "photo",
                "url": item.get("media_url_https") or item.get("media_url") or "",
                "previewUrl": item.get("media_url_https") or item.get("media_url") or "",
            }
        )
    quoted = None
    quoted_status = status.get("quoted_status")
    if isinstance(quoted_status, dict):
        quoted_user = (
            quoted_status.get("user")
            if isinstance(quoted_status.get("user"), dict)
            else {}
        )
        quoted = {
            "id": str(quoted_status.get("id_str") or quoted_status.get("id") or ""),
            "text": str(
                quoted_status.get("full_text")
                or quoted_status.get("text")
                or ""
            ),
            "author": {
                "username": quoted_user.get("screen_name") or "",
                "name": quoted_user.get("name") or "",
            },
        }
    urls: List[str] = []
    for entity_source in (extended.get("entities"), status.get("entities")):
        if not isinstance(entity_source, dict):
            continue
        for item in entity_source.get("urls", []) or []:
            if not isinstance(item, dict):
                continue
            expanded = str(item.get("expanded_url") or "")
            if expanded and "//t.co/" not in expanded and expanded not in urls:
                urls.append(expanded)
    return {
        "id": tweet_id,
        "text": text,
        "createdAt": status.get("created_at") or "",
        "urls": urls,
        "replyCount": status.get("reply_count") or 0,
        "retweetCount": status.get("retweet_count") or 0,
        "likeCount": status.get("favorite_count") or 0,
        "author": {
            "username": user.get("screen_name") or "",
            "name": user.get("name") or "",
        },
        "media": media,
        "quotedTweet": quoted,
        "_source": "dm_attachment",
    }


def _load_credentials(profile: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    accounts = load_json(ACCOUNTS_PATH)
    auth_name = profile["conversation"]["auth_account"]
    if auth_name not in accounts:
        raise RuntimeError("configured group auth account is missing")
    account = accounts[auth_name]
    if not account.get("auth_token") or not account.get("ct0"):
        raise RuntimeError("configured group auth account has incomplete credentials")
    return accounts, account


def fetch_group_messages(
    profile: Dict[str, Any], since_id: str, max_pages: int
) -> FetchResult:
    """Read newest-to-oldest until the durable checkpoint is encountered."""
    if max_pages < 1:
        raise ValueError("max_pages must be positive")

    sys.path.insert(0, str(SCRIPTS_DIR))
    import service as legacy_service  # Reuse the proven private-DM endpoint and headers.

    _accounts, account = _load_credentials(profile)
    conversation_id = str(profile["conversation"]["id"])
    cursor = None
    pages = 0
    reached = False
    dedup: Dict[str, Dict[str, Any]] = {}
    senders: Dict[str, Dict[str, Any]] = {}
    since_num = int(since_id)

    for _ in range(max_pages):
        url = legacy_service.DM_CONVERSATION_URL.format(conversation_id=conversation_id)
        params = {"count": "100"}
        if cursor:
            params["max_id"] = str(cursor)
        url += "?" + urllib.parse.urlencode(params)

        headers = dict(legacy_service.X_HEADERS_BASE)
        headers["cookie"] = "auth_token={}; ct0={}".format(
            account["auth_token"], account["ct0"]
        )
        headers["x-csrf-token"] = account["ct0"]
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError("group fetch HTTP {}".format(exc.code)) from exc
        except Exception as exc:
            raise RuntimeError("group fetch failed: {}".format(exc)) from exc

        timeline = payload.get("conversation_timeline")
        if not isinstance(timeline, dict):
            raise RuntimeError("group fetch returned no conversation timeline")
        page_users = timeline.get("users") or {}
        if isinstance(page_users, dict):
            user_items = page_users.items()
        elif isinstance(page_users, list):
            user_items = ((str(user.get("id_str") or user.get("id") or ""), user) for user in page_users if isinstance(user, dict))
        else:
            user_items = ()
        for sender_id, user in user_items:
            normalized = normalize_dm_user(sender_id, user)
            if normalized:
                senders[normalized["sender_id"]] = normalized
        entries = timeline.get("entries") or []
        pages += 1
        if not entries:
            if timeline.get("status") == "AT_END":
                reached = True
                break
            raise RuntimeError("group fetch returned an empty page before the checkpoint")

        for entry in entries:
            message = entry.get("message", {}).get("message_data", {})
            message_id = str(message.get("id") or "")
            if not message_id.isdigit():
                continue
            if int(message_id) <= since_num:
                reached = True
                break
            urls = []
            for item in message.get("entities", {}).get("urls", []):
                expanded = item.get("expanded_url") or item.get("url")
                if expanded:
                    urls.append(str(expanded))
            attachment = message.get("attachment")
            dedup[message_id] = {
                "id": message_id,
                "time": message.get("time"),
                "sender_id": str(message.get("sender_id") or ""),
                "text": str(message.get("text") or ""),
                "urls": urls,
                "attachment_tweet": normalize_dm_attachment(attachment),
                # Native media and any other non-tweet attachment payload used
                # to be dropped here, which made media-only shares vanish
                # (audit A04). Keep the raw metadata so capture can persist an
                # honest processing state and a later retry stays possible.
                "attachment_raw": (
                    {key: value for key, value in attachment.items() if key != "tweet"}
                    if isinstance(attachment, dict)
                    else None
                ),
            }

        if reached:
            break

        next_cursor = timeline.get("min_entry_id")
        if timeline.get("status") == "AT_END":
            reached = True
            break
        if not next_cursor or str(next_cursor) == str(cursor):
            raise RuntimeError("group pagination stopped before the checkpoint")
        cursor = str(next_cursor)

    messages = sorted(dedup.values(), key=lambda item: int(item["id"]))
    ids = [item["id"] for item in messages]
    return FetchResult(
        messages=messages,
        pages=pages,
        reached_checkpoint=reached,
        newest_message_id=max(ids, key=int) if ids else None,
        oldest_message_id=min(ids, key=int) if ids else None,
        senders=senders,
    )


def _clean_url(url: str) -> Optional[str]:
    value = url.strip().rstrip(".,;:!?)]}'\"")
    try:
        parts = urllib.parse.urlsplit(value)
    except ValueError:
        return None
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        return None
    query = []
    for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True):
        lower = key.lower()
        if lower.startswith("utm_") or lower in TRACKING_QUERY_KEYS:
            continue
        query.append((key, value))
    return urllib.parse.urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path or "/",
            urllib.parse.urlencode(query, doseq=True),
            "",
        )
    )


NATIVE_MEDIA_TYPES = ("photo", "video", "animated_gif")


def _native_media_resource(
    message_id: str, attachment: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """A durable resource for message-level native media (audit A04).

    Covers photos/videos/GIFs sent directly into the DM — distinct from
    attachment_tweet media, which belongs to the embedded tweet and travels in
    that tweet's payload. Identity is message-scoped ("media:<message_id>",
    mirroring notes) because DM media URLs are private and content identity
    cannot be proven without reading the bytes. An attachment whose shape we do
    not recognize still persists, marked unsupported with the reason — captured
    metadata is evidence of a share, never proof the content was read.
    """
    if not message_id:
        return None
    payload_keys = sorted(key for key in attachment if key != "tweet")
    if not payload_keys:
        return None
    media = []
    for media_type in NATIVE_MEDIA_TYPES:
        item = attachment.get(media_type)
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or item.get("media_url_https") or "")
        media.append(
            {
                "type": media_type,
                "url": url,
                "previewUrl": str(item.get("preview_url") or item.get("previewUrl") or url),
            }
        )
    if media:
        state = "pending"
        detail = "native media captured; content not extracted yet"
    else:
        state = "unsupported"
        detail = "unrecognized attachment shape: " + ", ".join(payload_keys)[:200]
    return {
        "resource_id": "media:" + message_id,
        "kind": "media",
        "canonical_url": "",
        "tweet_id": "",
        "media": media,
        "attachment_keys": payload_keys,
        "extraction_state": state,
        "extraction_detail": detail,
    }


def extract_resources(message: Dict[str, Any]) -> List[Dict[str, str]]:
    entity_urls = [str(url) for url in message.get("urls", []) if url]
    text_urls = URL_RE.findall(str(message.get("text") or ""))
    raw_urls = entity_urls + text_urls
    has_expanded = bool(entity_urls)
    seen = set()
    resources = []

    attachment = message.get("attachment_tweet")
    if isinstance(attachment, dict):
        tweet_id = str(attachment.get("id") or "")
        if tweet_id.isdigit():
            resources.append(
                {
                    "resource_id": "tweet:" + tweet_id,
                    "kind": "tweet",
                    "canonical_url": "https://x.com/i/status/{}".format(tweet_id),
                    "tweet_id": tweet_id,
                }
            )
            seen.add("tweet:" + tweet_id)

    for raw_url in raw_urls:
        if has_expanded and urllib.parse.urlsplit(raw_url).netloc.lower() == "t.co":
            continue
        tweet_match = TWEET_RE.search(raw_url)
        if tweet_match:
            tweet_id = tweet_match.group(1)
            resource_id = "tweet:" + tweet_id
            candidate = {
                "resource_id": resource_id,
                "kind": "tweet",
                "canonical_url": "https://x.com/i/status/{}".format(tweet_id),
                "tweet_id": tweet_id,
            }
        else:
            canonical = _clean_url(raw_url)
            if not canonical:
                continue
            digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
            candidate = {
                "resource_id": "url:" + digest,
                "kind": "url",
                "canonical_url": canonical,
                "tweet_id": "",
            }
        if candidate["resource_id"] not in seen:
            resources.append(candidate)
            seen.add(candidate["resource_id"])

    text = str(message.get("text") or "").strip()
    if not resources and text:
        message_id = str(message["id"])
        resources.append(
            {
                "resource_id": "note:" + message_id,
                "kind": "note",
                "canonical_url": "",
                "tweet_id": "",
            }
        )

    # Message-level native media is additional to the text/URL resources above,
    # so the note fallback keeps its existing meaning (text with no links).
    raw_attachment = message.get("attachment_raw")
    if raw_attachment is None:
        raw_attachment = message.get("attachment")
    if isinstance(raw_attachment, dict):
        media_candidate = _native_media_resource(str(message.get("id") or ""), raw_attachment)
        if media_candidate and media_candidate["resource_id"] not in seen:
            resources.append(media_candidate)
            seen.add(media_candidate["resource_id"])
    return resources


def _safe_ms(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def initialize_cursor(conn: sqlite3.Connection, profile: Dict[str, Any]) -> str:
    cursor = get_meta(conn, "fetch_cursor")
    if cursor:
        return cursor
    cursor = str(profile["bootstrap"]["resume_after_message_id"])
    if not cursor.isdigit():
        raise RuntimeError("bootstrap resume_after_message_id is invalid")
    with conn:
        set_meta(conn, "fetch_cursor", cursor)
        set_meta(conn, "bootstrap_evidence", profile["bootstrap"].get("evidence", ""))
        set_meta(conn, "fetch_incomplete", "false")
    return cursor


def persist_fetch(
    conn: sqlite3.Connection, profile: Dict[str, Any], result: FetchResult, since_id: str
) -> Dict[str, int]:
    owners = {str(item["sender_id"]) for item in profile["owners"]}
    captured_at = utc_now()
    owner_messages = 0
    linked_resources = 0
    inserted_resources = 0
    attachment_hydrated = 0
    urls_backfilled = 0

    with conn:
        sender_profiles = dict(result.senders)
        for owner in profile["owners"]:
            sender_id = str(owner["sender_id"])
            sender_profiles.setdefault(
                sender_id,
                {
                    "sender_id": sender_id,
                    "username": str(owner.get("username") or ""),
                    "display_name": "",
                    "avatar_url": "",
                },
            )
        for message in result.messages:
            sender_id = str(message.get("sender_id") or "")
            if sender_id:
                sender_profiles.setdefault(
                    sender_id,
                    {
                        "sender_id": sender_id,
                        "username": "",
                        "display_name": "",
                        "avatar_url": "",
                    },
                )
        for sender in sender_profiles.values():
            sender_id = str(sender["sender_id"])
            conn.execute(
                """
                INSERT INTO senders(
                    sender_id, username, display_name, avatar_url, is_owner, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(sender_id) DO UPDATE SET
                    username = COALESCE(NULLIF(excluded.username, ''), senders.username),
                    display_name = COALESCE(NULLIF(excluded.display_name, ''), senders.display_name),
                    avatar_url = COALESCE(NULLIF(excluded.avatar_url, ''), senders.avatar_url),
                    is_owner = excluded.is_owner,
                    updated_at = excluded.updated_at
                """,
                (
                    sender_id,
                    str(sender.get("username") or ""),
                    str(sender.get("display_name") or ""),
                    str(sender.get("avatar_url") or ""),
                    1 if sender_id in owners else 0,
                    captured_at,
                ),
            )
        for message in result.messages:
            is_owner = str(message.get("sender_id")) in owners
            if is_owner:
                owner_messages += 1
            conn.execute(
                """
                INSERT INTO messages(
                    message_id, conversation_id, sent_at_ms, sender_id, is_owner,
                    text, urls_json, captured_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                    sent_at_ms = excluded.sent_at_ms,
                    sender_id = excluded.sender_id,
                    is_owner = excluded.is_owner,
                    text = excluded.text,
                    urls_json = excluded.urls_json,
                    captured_at = excluded.captured_at
                """,
                (
                    message["id"],
                    str(profile["conversation"]["id"]),
                    _safe_ms(message.get("time")),
                    str(message.get("sender_id") or ""),
                    1 if is_owner else 0,
                    str(message.get("text") or ""),
                    json.dumps(message.get("urls", [])),
                    captured_at,
                ),
            )

            for resource in extract_resources(message):
                linked_resources += 1
                attachment = message.get("attachment_tweet")
                if not isinstance(attachment, dict) or str(attachment.get("id")) != resource.get("tweet_id"):
                    attachment = None
                attachment_title = ""
                attachment_author = ""
                attachment_content = ""
                if attachment:
                    attachment_title, attachment_author, attachment_content = _tweet_content(attachment)
                existing = conn.execute(
                    "SELECT resource_id, last_message_id, source_text, status, payload_json "
                    "FROM resources "
                    "WHERE resource_id = ?",
                    (resource["resource_id"],),
                ).fetchone()
                if existing:
                    source_text = existing["source_text"]
                    incoming_text = str(message.get("text") or "")
                    if len(incoming_text) > len(source_text or ""):
                        source_text = incoming_text
                    last_id = max(
                        str(existing["last_message_id"]),
                        str(message["id"]),
                        key=message_order_key,
                    )
                    conn.execute(
                        "UPDATE resources SET last_message_id = ?, source_text = ?, "
                        "updated_at = ? WHERE resource_id = ?",
                        (last_id, source_text, captured_at, resource["resource_id"]),
                    )
                    if (
                        attachment
                        and attachment.get("urls")
                        and existing["status"] not in {"pending_hydration", "unavailable"}
                        and _merge_payload_urls(
                            conn,
                            resource["resource_id"],
                            existing["payload_json"],
                            attachment["urls"],
                            captured_at,
                        )
                    ):
                        urls_backfilled += 1
                    if attachment and existing["status"] in {
                        "pending_hydration",
                        "unavailable",
                    }:
                        conn.execute(
                            """
                            UPDATE resources
                            SET status = 'pending_review', next_retry_at = NULL,
                                last_error = NULL, payload_json = ?, title = ?,
                                author = ?, content_text = ?, updated_at = ?
                            WHERE resource_id = ?
                            """,
                            (
                                json.dumps(attachment, ensure_ascii=False),
                                attachment_title,
                                attachment_author,
                                attachment_content,
                                captured_at,
                                resource["resource_id"],
                            ),
                        )
                        attachment_hydrated += 1
                else:
                    inserted_resources += 1
                    status = (
                        "pending_hydration"
                        if resource["kind"] == "tweet" and not attachment
                        else "pending_review"
                    )
                    payload_json = (
                        json.dumps(attachment, ensure_ascii=False) if attachment else None
                    )
                    if resource["kind"] == "media":
                        payload_json = json.dumps(
                            {
                                "media": resource.get("media") or [],
                                "attachment_keys": resource.get("attachment_keys") or [],
                                "_source": "dm_media",
                            },
                            ensure_ascii=False,
                        )
                    extraction_state = resource.get("extraction_state")
                    extraction_detail = resource.get("extraction_detail")
                    if extraction_state is None and resource["kind"] == "url":
                        # A shared link proves a URL was posted, not that the
                        # destination was ever read.
                        extraction_state = "pending"
                        extraction_detail = "destination content not fetched yet"
                    conn.execute(
                        """
                        INSERT INTO resources(
                            resource_id, kind, canonical_url, tweet_id,
                            first_message_id, last_message_id, sender_id, source_text,
                            status, payload_json, title, author, content_text,
                            first_seen_at, updated_at,
                            extraction_state, extraction_detail, extraction_checked_at
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            resource["resource_id"],
                            resource["kind"],
                            resource["canonical_url"],
                            resource["tweet_id"] or None,
                            message["id"],
                            message["id"],
                            str(message.get("sender_id") or ""),
                            str(message.get("text") or ""),
                            status,
                            payload_json,
                            attachment_title or None,
                            attachment_author or None,
                            attachment_content or None,
                            captured_at,
                            captured_at,
                            extraction_state,
                            extraction_detail,
                            captured_at if extraction_state else None,
                        ),
                    )
                    if attachment:
                        attachment_hydrated += 1
                conn.execute(
                    "INSERT OR IGNORE INTO message_resources(message_id, resource_id) "
                    "VALUES(?, ?)",
                    (message["id"], resource["resource_id"]),
                )

        set_meta(conn, "last_fetch_at", captured_at)
        set_meta(conn, "last_fetch_pages", result.pages)
        if result.reached_checkpoint:
            if result.newest_message_id:
                current_cursor = get_meta(conn, "fetch_cursor", since_id) or since_id
                set_meta(
                    conn,
                    "fetch_cursor",
                    max(current_cursor, result.newest_message_id, key=int),
                )
            else:
                set_meta(conn, "fetch_cursor", since_id)
            set_meta(conn, "fetch_incomplete", "false")
            set_meta(conn, "last_fetch_error", "")
        else:
            set_meta(conn, "fetch_incomplete", "true")
            set_meta(
                conn,
                "last_fetch_error",
                "page cap reached before durable checkpoint {}".format(since_id),
            )

    return {
        "messages": len(result.messages),
        "owner_messages": owner_messages,
        "non_owner_messages": len(result.messages) - owner_messages,
        "senders_seen": len({str(message.get("sender_id") or "") for message in result.messages}),
        "resource_links": linked_resources,
        "new_resources": inserted_resources,
        "attachment_hydrated": attachment_hydrated,
        "urls_backfilled": urls_backfilled,
    }


def _merge_payload_urls(
    conn: sqlite3.Connection,
    resource_id: str,
    payload_json: Optional[str],
    urls: Sequence[str],
    now: str,
) -> bool:
    """Add expanded external URLs to an already-hydrated payload (replay backfill)."""
    try:
        payload = json.loads(payload_json) if payload_json else {}
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        return False
    current = payload.get("urls") if isinstance(payload.get("urls"), list) else []
    merged = list(dict.fromkeys([str(url) for url in current] + [str(url) for url in urls if url]))
    if merged == [str(url) for url in current]:
        return False
    payload["urls"] = merged
    conn.execute(
        "UPDATE resources SET payload_json = ?, updated_at = ? WHERE resource_id = ?",
        (json.dumps(payload, ensure_ascii=False), now, resource_id),
    )
    return True


def _tweet_content(payload: Dict[str, Any]) -> Tuple[str, str, str]:
    author_obj = payload.get("author") or {}
    author = str(
        author_obj.get("username")
        or author_obj.get("userName")
        or author_obj.get("name")
        or ""
    )
    parts = [str(payload.get("text") or "")]
    quoted = payload.get("quotedTweet")
    if isinstance(quoted, dict):
        parts.append(str(quoted.get("text") or ""))
    article = payload.get("article")
    if isinstance(article, dict):
        parts.append(str(article.get("title") or ""))
        parts.append(str(article.get("text") or ""))
    content = "\n".join(part for part in parts if part).strip()
    title = content.splitlines()[0][:240] if content else ""
    return title, author, content


def _bird_read(
    tweet_id: str, account: Dict[str, Any], timeout_seconds: int = 30
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not BIRD.exists():
        return None, "bird CLI is missing"
    command = [
        str(BIRD),
        "read",
        tweet_id,
        "--json",
        "--plain",
        "--auth-token",
        account["auth_token"],
        "--ct0",
        account["ct0"],
        "--timeout",
        "20000",
    ]
    try:
        runtime_path = os.pathsep.join(
            path
            for path in (
                "/opt/homebrew/bin",
                "/usr/local/bin",
                "/usr/bin",
                "/bin",
                "/usr/sbin",
                "/sbin",
                os.environ.get("PATH", ""),
            )
            if path
        )
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            env={**os.environ, "PATH": runtime_path},
        )
    except subprocess.TimeoutExpired:
        return None, "bird read timed out"
    except OSError as exc:
        return None, "bird read failed: {}".format(exc)

    if result.returncode != 0:
        error = (result.stderr or result.stdout or "bird read failed").strip()
        return None, error[-500:]
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None, "bird returned invalid JSON"
    if not isinstance(payload, dict) or not str(payload.get("id") or ""):
        return None, "bird returned no tweet"
    return payload, None


def _next_retry(attempts: int) -> str:
    minutes = min(24 * 60, 15 * (2 ** max(0, attempts - 1)))
    value = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=minutes)
    return value.isoformat(timespec="seconds")


def hydrate_pending(
    conn: sqlite3.Connection,
    profile: Dict[str, Any],
    limit: int,
    concurrency: int,
    force_retry: bool = False,
) -> Dict[str, int]:
    if limit <= 0:
        return {"attempted": 0, "hydrated": 0, "failed": 0, "unavailable": 0}
    now = utc_now()
    retry_clause = "" if force_retry else "AND (next_retry_at IS NULL OR next_retry_at <= ?)"
    params: Tuple[Any, ...] = (limit,) if force_retry else (now, limit)
    rows = conn.execute(
        """
        SELECT resource_id, tweet_id, hydration_attempts
        FROM resources
        WHERE status = 'pending_hydration'
          {retry_clause}
        ORDER BY CAST(last_message_id AS INTEGER) DESC
        LIMIT ?
        """.format(retry_clause=retry_clause),
        params,
    ).fetchall()
    if not rows:
        return {"attempted": 0, "hydrated": 0, "failed": 0, "unavailable": 0}

    accounts = load_json(ACCOUNTS_PATH)
    account_names = profile.get("bird_accounts") or [
        item["username"] for item in profile["owners"]
    ]
    usable = [accounts[name] for name in account_names if name in accounts]
    if not usable:
        raise RuntimeError("no configured bird accounts are available")

    jobs = []
    for index, row in enumerate(rows):
        jobs.append((row, usable[index % len(usable)]))

    completed = []
    with ThreadPoolExecutor(max_workers=max(1, min(concurrency, 3))) as pool:
        future_map = {
            pool.submit(_bird_read, str(row["tweet_id"]), account): row
            for row, account in jobs
        }
        for future in as_completed(future_map):
            row = future_map[future]
            try:
                payload, error = future.result()
            except Exception as exc:
                payload, error = None, "hydration worker failed: {}".format(exc)
            completed.append((row, payload, error))

    hydrated = 0
    failed = 0
    unavailable = 0
    max_attempts = int(profile.get("hydration", {}).get("max_attempts", 3))
    with conn:
        for row, payload, error in completed:
            attempts = int(row["hydration_attempts"] or 0) + 1
            if payload:
                title, author, content = _tweet_content(payload)
                conn.execute(
                    """
                    UPDATE resources
                    SET status = 'pending_review', hydration_attempts = ?,
                        next_retry_at = NULL, last_error = NULL, payload_json = ?,
                        title = ?, author = ?, content_text = ?, updated_at = ?
                    WHERE resource_id = ?
                    """,
                    (
                        attempts,
                        json.dumps(payload, ensure_ascii=False),
                        title,
                        author,
                        content,
                        utc_now(),
                        row["resource_id"],
                    ),
                )
                hydrated += 1
                continue

            failed += 1
            lower = str(error or "").lower()
            permanent_hint = any(
                marker in lower
                for marker in ("not found", "does not exist", "tombstone", "deleted", "404")
            )
            status = "unavailable" if permanent_hint and attempts >= max_attempts else "pending_hydration"
            if status == "unavailable":
                unavailable += 1
            conn.execute(
                """
                UPDATE resources
                SET status = ?, hydration_attempts = ?, next_retry_at = ?,
                    last_error = ?, updated_at = ?
                WHERE resource_id = ?
                """,
                (
                    status,
                    attempts,
                    None if status == "unavailable" else _next_retry(attempts),
                    str(error or "unknown hydration failure")[-500:],
                    utc_now(),
                    row["resource_id"],
                ),
            )

    return {
        "attempted": len(rows),
        "hydrated": hydrated,
        "failed": failed,
        "unavailable": unavailable,
    }


def _term_present(corpus: str, term: str) -> bool:
    term = term.casefold().strip()
    if not term:
        return False
    if all(ord(char) < 128 for char in term):
        pattern = r"(?<![a-z0-9_]){}(?![a-z0-9_])".format(re.escape(term))
        return re.search(pattern, corpus) is not None
    return term in corpus


def score_resource(row: sqlite3.Row, profile: Dict[str, Any]) -> Dict[str, Any]:
    payload = {}
    if row["payload_json"]:
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            payload = {}
    corpus_parts = [
        str(row["canonical_url"] or ""),
        str(row["source_text"] or ""),
        str(row["title"] or ""),
        str(row["author"] or ""),
        str(row["content_text"] or ""),
    ]
    if isinstance(payload.get("quotedTweet"), dict):
        corpus_parts.append(str(payload["quotedTweet"].get("text") or ""))
    corpus = "\n".join(corpus_parts).casefold()
    selection = profile["selection"]
    score = 0
    areas = []
    reasons = []

    ai_matches = [
        term for term in selection.get("ai_terms", []) if _term_present(corpus, term)
    ]
    if ai_matches:
        weight = int(selection.get("ai_weight", 4))
        score += weight
        areas.append("ai")
        reasons.append("AI signal: " + ", ".join(ai_matches[:4]))

    for area_name, area in selection.get("project_areas", {}).items():
        matches = [
            term for term in area.get("keywords", []) if _term_present(corpus, term)
        ]
        if matches:
            score += int(area.get("weight", 3))
            areas.append(area_name)
            reasons.append(
                "{}: {}".format(area.get("label", area_name), ", ".join(matches[:4]))
            )

    return {
        "score": score,
        "project_areas": areas,
        "reasons": reasons,
        "relevant": score >= int(selection.get("minimum_score", 3)),
    }


def negative_gate(row: sqlite3.Row, profile: Dict[str, Any]) -> Tuple[str, str]:
    """Approved exclusion terms, applied only where nothing vouched for the item.

    The rules layer never rejects, on purpose: a keyword miss must not be able to
    hide something useful. So a learned negative is allowed to act only when the
    positive rules found *nothing at all* — no AI signal, no project area. If any
    rule matched, the negative stands down. That keeps the guarantee intact while
    still clearing the chatter that has no redeeming signal.
    """
    terms = [str(t).lower() for t in profile.get("selection", {}).get("negative_terms", [])]
    if not terms:
        return "", ""
    corpus = "\n".join(
        str(part or "")
        for part in (row["title"], row["content_text"], row["source_text"], row["canonical_url"])
    ).casefold()
    hits = [term for term in terms if _term_present(corpus, term)]
    if not hits:
        return "", ""
    return "negative-rule", "Matched approved exclusion term{}: {}.".format(
        "" if len(hits) == 1 else "s", ", ".join(hits[:4])
    )


def apply_rule_classification(
    conn: sqlite3.Connection, profile: Dict[str, Any]
) -> Dict[str, int]:
    rows = conn.execute(
        "SELECT * FROM resources WHERE status = 'pending_review'"
    ).fetchall()
    relevant = 0
    auto_excluded = 0
    with conn:
        for row in rows:
            decision = score_resource(row, profile)
            if not decision["relevant"]:
                # Nothing vouched for it; only now may an approved negative act.
                code, human = negative_gate(row, profile)
                if code:
                    conn.execute(
                        "UPDATE resources SET status = 'irrelevant', score = 0, "
                        "project_areas_json = '[]', reasons_json = ?, "
                        "decision_source = ?, updated_at = ? WHERE resource_id = ?",
                        (
                            json.dumps([human], ensure_ascii=False),
                            code,
                            utc_now(),
                            row["resource_id"],
                        ),
                    )
                    auto_excluded += 1
                continue
            conn.execute(
                """
                UPDATE resources
                SET status = 'relevant', score = ?, project_areas_json = ?,
                    reasons_json = ?, decision_source = 'rules', updated_at = ?
                WHERE resource_id = ?
                """,
                (
                    decision["score"],
                    json.dumps(decision["project_areas"]),
                    json.dumps(decision["reasons"], ensure_ascii=False),
                    utc_now(),
                    row["resource_id"],
                ),
            )
            relevant += 1
    return {
        "reviewed_by_rules": len(rows),
        "relevant_by_rules": relevant,
        "excluded_by_negatives": auto_excluded,
    }


def _json_list(value: Any) -> List[Any]:
    if not value:
        return []
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _optional_column(row: sqlite3.Row, name: str, default: Any = None) -> Any:
    return row[name] if name in row.keys() else default


def _iso_from_ms(value: Any) -> Optional[str]:
    milliseconds = _safe_ms(value)
    if milliseconds is None:
        return None
    return dt.datetime.fromtimestamp(
        milliseconds / 1000, tz=dt.timezone.utc
    ).isoformat(timespec="seconds")


def select_resource_rows(
    conn: sqlite3.Connection,
    where_sql: str = "",
    params: Sequence[Any] = (),
    order_sql: str = "CAST(r.last_message_id AS INTEGER) DESC",
) -> List[sqlite3.Row]:
    query = """
        SELECT
            r.*,
            m.sent_at_ms AS shared_at_ms,
            m.sender_id AS last_sender_id,
            m.is_owner AS last_sender_is_owner,
            s.username AS sender_username,
            s.display_name AS sender_display_name,
            s.avatar_url AS sender_avatar_url,
            (SELECT COUNT(*) FROM message_resources mr_count
             WHERE mr_count.resource_id = r.resource_id) AS share_count,
            (SELECT COUNT(DISTINCT m_sender.sender_id)
             FROM message_resources mr_sender
             JOIN messages m_sender ON m_sender.message_id = mr_sender.message_id
             WHERE mr_sender.resource_id = r.resource_id) AS sharer_count,
            (SELECT GROUP_CONCAT(sharer_label, ' | ')
             FROM (
                 SELECT DISTINCT COALESCE(NULLIF(s_all.username, ''), m_all.sender_id)
                     AS sharer_label
                 FROM message_resources mr_all
                 JOIN messages m_all ON m_all.message_id = mr_all.message_id
                 LEFT JOIN senders s_all ON s_all.sender_id = m_all.sender_id
                 WHERE mr_all.resource_id = r.resource_id
                 ORDER BY sharer_label
             )) AS sharers
            ,(SELECT GROUP_CONCAT(sharer_id, ' | ')
              FROM (
                  SELECT DISTINCT m_ids.sender_id AS sharer_id
                  FROM message_resources mr_ids
                  JOIN messages m_ids ON m_ids.message_id = mr_ids.message_id
                  WHERE mr_ids.resource_id = r.resource_id
                  ORDER BY sharer_id
              )) AS sharer_ids
            -- Occurrences carried by real group DM messages (all-digit ids;
            -- synthetic ids contain ':'). Group visibility derives from these,
            -- so a resource first seen as a bookmark still surfaces in the
            -- group view once somebody shares it there (audit A01).
            ,(SELECT COUNT(*)
              FROM message_resources mr_group
              WHERE mr_group.resource_id = r.resource_id
                AND instr(mr_group.message_id, ':') = 0) AS group_share_count
        FROM resources r
        LEFT JOIN messages m ON m.message_id = r.last_message_id
        LEFT JOIN senders s ON s.sender_id = m.sender_id
    """
    if where_sql:
        query += " WHERE " + where_sql
    query += " ORDER BY " + order_sql
    return conn.execute(query, tuple(params)).fetchall()


_X_SELF_LINK_RE = re.compile(
    r"^https?://(?:www\.|mobile\.)?(?:x\.com|twitter\.com|t\.co)/", re.IGNORECASE
)


def _safe_count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _tweet_created_iso(value: Any) -> Optional[str]:
    if not value:
        return None
    text = str(value).strip()
    try:
        parsed = dt.datetime.strptime(text, "%a %b %d %H:%M:%S %z %Y")
    except ValueError:
        parsed = parse_iso(text)
    if parsed is None:
        return None
    return parsed.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def _payload_external_urls(payload: Dict[str, Any], text: str) -> List[str]:
    """External (non-X) links: DM entities, bird raw entities, then free text."""
    urls: List[str] = []

    def add(value: Any) -> None:
        candidate = str(value or "").strip()
        if candidate and candidate not in urls:
            urls.append(candidate)

    if isinstance(payload.get("urls"), list):
        for item in payload["urls"]:
            add(item)
    raw = payload.get("_raw") if isinstance(payload.get("_raw"), dict) else {}
    legacy = raw.get("legacy") if isinstance(raw.get("legacy"), dict) else {}
    entities = legacy.get("entities") if isinstance(legacy.get("entities"), dict) else {}
    for item in entities.get("urls") or []:
        if isinstance(item, dict):
            add(item.get("expanded_url"))
    note = raw.get("note_tweet") if isinstance(raw.get("note_tweet"), dict) else {}
    note_result = note.get("note_tweet_results") if isinstance(note.get("note_tweet_results"), dict) else {}
    result = note_result.get("result") if isinstance(note_result.get("result"), dict) else {}
    entity_set = result.get("entity_set") if isinstance(result.get("entity_set"), dict) else {}
    for item in entity_set.get("urls") or []:
        if isinstance(item, dict):
            add(item.get("expanded_url"))
    for item in external_urls_from_text(text):
        add(item)
    return [url for url in urls if not _X_SELF_LINK_RE.match(url)]


def _needs_full_analysis(row: sqlite3.Row) -> bool:
    """Whether a row is worth classifying and scoring.

    Typing and pick-scoring cost ~200 regex tests per row. They are only ever
    read for something the dashboard shows or ranks, so imported rows that the
    rules already rejected are skipped. On a 27k-row ledger this is the whole
    difference between a five-minute export and a fast one.
    """
    source = (_optional_column(row, "source", "group") or "group")
    if source in DASHBOARD_GROUP_SOURCES or row["status"] == "relevant":
        return True
    # A bookmark-first resource that was later shared in the group is part of
    # the briefing, so it earns the same full analysis as any group share.
    return bool(_optional_column(row, "group_share_count", 0))


def resource_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    payload = {}
    if row["payload_json"]:
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            payload = {}
    media = payload.get("media") if isinstance(payload.get("media"), list) else []
    media_urls = [
        str(item.get("url") or item.get("previewUrl") or "")
        for item in media
        if isinstance(item, dict) and (item.get("url") or item.get("previewUrl"))
    ]
    sender_id = str(_optional_column(row, "last_sender_id", row["sender_id"]) or row["sender_id"])
    sender_username = str(_optional_column(row, "sender_username", "") or "")
    source_value = str(_optional_column(row, "source", "group") or "group")
    group_share_count = int(_optional_column(row, "group_share_count", 0) or 0)
    in_group = source_value in DASHBOARD_GROUP_SOURCES or group_share_count > 0
    origins = sorted({source_value} | ({"group"} if group_share_count else set()))
    quoted = payload.get("quotedTweet") if isinstance(payload.get("quotedTweet"), dict) else {}
    quoted_text = str(quoted.get("text") or "") if quoted else ""
    corpus_text = "\n".join(
        part
        for part in (
            str(row["title"] or ""),
            str(row["content_text"] or row["source_text"] or ""),
            quoted_text,
        )
        if part
    )
    external_urls = _payload_external_urls(payload, corpus_text)
    analyse = _needs_full_analysis(row)
    typing = (
        classify_resource_type(corpus_text, external_urls, str(row["author"] or ""))
        if analyse
        else {"type": "other", "signals": []}
    )
    record: Dict[str, Any] = {
        "resource_id": row["resource_id"],
        "source": source_value,
        # Additive provenance keys (C1): source stays the first origin; these
        # expose every origin so a later group share is never hidden by it.
        "origins": origins,
        "in_group": in_group,
        "group_share_count": group_share_count,
        # Additive processing-state keys (C1/A04), independent of relevance.
        "extraction_state": _optional_column(row, "extraction_state"),
        "extraction_detail": _optional_column(row, "extraction_detail"),
        "kind": row["kind"],
        "url": row["canonical_url"],
        "tweet_id": row["tweet_id"],
        "message_id": row["last_message_id"],
        "sender_id": sender_id,
        "sender_username": sender_username,
        "sender_display_name": str(_optional_column(row, "sender_display_name", "") or ""),
        "sender_avatar_url": str(_optional_column(row, "sender_avatar_url", "") or ""),
        "sender_is_owner": bool(_optional_column(row, "last_sender_is_owner", 0)),
        "shared_at": _iso_from_ms(_optional_column(row, "shared_at_ms")),
        "share_count": int(_optional_column(row, "share_count", 1) or 1),
        "sharer_count": int(_optional_column(row, "sharer_count", 1) or 1),
        "sharers": [
            value for value in str(_optional_column(row, "sharers", "") or "").split(" | ") if value
        ],
        "sharer_ids": [
            value for value in str(_optional_column(row, "sharer_ids", "") or "").split(" | ") if value
        ],
        "author": row["author"],
        "title": row["title"],
        "text": row["content_text"] or row["source_text"],
        "status": row["status"],
        "score": row["score"],
        "project_areas": _json_list(row["project_areas_json"]),
        "reasons": _json_list(row["reasons_json"]),
        "decision_source": row["decision_source"],
        "hydration_attempts": row["hydration_attempts"],
        "last_error": row["last_error"],
        "notified_at": row["notified_at"],
        "media_urls": media_urls,
        "first_seen_at": row["first_seen_at"],
        "updated_at": row["updated_at"],
        "likes": _safe_count(payload.get("likeCount")),
        "retweets": _safe_count(payload.get("retweetCount")),
        "replies": _safe_count(payload.get("replyCount")),
        "tweet_created_at": _tweet_created_iso(payload.get("createdAt")),
        "quoted_text": quoted_text,
        "external_urls": external_urls,
        "external_label": short_link_label(external_urls[0]) if external_urls else "",
        # Every distinct tool this post links, so a verdict attaches even when the
        # tool is link #12 inside somebody else's thread.
        "tool_keys": list(dict.fromkeys(key for key in (tool_key(u) for u in external_urls) if key)),
        "verdict": None,
        "resource_type": typing["type"],
        "type_signals": typing["signals"],
        "pick_score": None,
        "pick_parts": None,
    }
    if analyse and record["status"] == "relevant":
        pick = compute_pick_score(record)
        record["pick_score"] = pick["score"]
        record["pick_parts"] = pick["parts"]
    return record


VERDICT_STRENGTH = {"must_try": 4, "must_read": 3, "already_have": 2, "excluded": 1}
# A verdict describes a tool, not every post that happens to mention it. A
# roundup linking forty repos is not "must try" merely because one of them is —
# without this, a link-dump inherits the best verdict among its links and rides
# it to the top. Posts about a specific thing link one or two things.
MAX_TOOLS_FOR_VERDICT_INHERITANCE = 3
TOOL_META_PATH = DATA_DIR / "tool-meta.json"
# Objective disqualifiers. These are facts, not opinions, so the pipeline may
# apply them without asking: an archived or long-dead repo is not a candidate
# no matter how well it scored. A hand-written verdict always overrides them.
AUTO_STALE_DAYS = 365
AUTO_MIN_STARS = 25


def load_tool_meta(path: Path = TOOL_META_PATH) -> Dict[str, Dict[str, Any]]:
    """Cached GitHub facts written by scripts/enrich_tools.py.

    The cache is keyed by repo slug (``owner/repo``) while the tool index is
    keyed by host path (``github.com/owner/repo``), so index both spellings.
    """
    try:
        payload = load_json(path)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    tools = payload.get("tools") if isinstance(payload, dict) else None
    if not isinstance(tools, dict):
        return {}
    table: Dict[str, Dict[str, Any]] = {}
    for slug, value in tools.items():
        slug = str(slug)
        table[slug.lower()] = value
        table["github.com/{}".format(slug).lower()] = value
    return table


def auto_gate(meta: Dict[str, Any], now: Optional[dt.datetime] = None) -> Tuple[str, str]:
    """Return (reason_code, human_reason) when the facts alone disqualify a tool."""
    if not meta or not meta.get("ok"):
        if meta and meta.get("missing"):
            return "gone", "The repository no longer exists on GitHub."
        return "", ""
    if meta.get("archived"):
        return "archived", "Archived on GitHub — the author has stopped maintaining it."
    if meta.get("is_empty"):
        return "empty", "The repository is empty."
    pushed = parse_iso(meta.get("pushed_at"))
    now = now or dt.datetime.now(dt.timezone.utc)
    if pushed is not None and (now - pushed).days >= AUTO_STALE_DAYS:
        return "stale", "No commit in over a year (last push {}).".format(meta.get("pushed_at"))
    stars = meta.get("stars")
    if isinstance(stars, int) and stars < AUTO_MIN_STARS:
        return "tiny", "Only {} stars — too little external validation to spend time on.".format(stars)
    return "", ""


def build_tool_index(
    records: Sequence[Dict[str, Any]],
    verdicts: Dict[str, Dict[str, Any]],
    outcomes: Optional[Dict[str, Dict[str, Any]]] = None,
    *,
    profile: Optional[Dict[str, Any]] = None,
    evidence_store: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """Collapse every linked tool across all posts into one row per tool.

    The ledger's unit is the post, but a post can link 50 tools and the user's
    unit is the tool. Without this, a tool that appears as link #12 inside
    somebody else's thread is invisible. Each record also gets its strongest
    matching verdict stamped on it, in place.

    Every unreviewed tool (and every relevant record with no tool key) also
    gets an additive C5 `review_eligibility` block — the review queue must be
    able to say "review / evidence pending / blocked + why" for EVERY item,
    not only for repositories whose GitHub facts happened to fetch (A09).
    `profile`/`evidence_store` are additive keyword-only inputs; omitted, the
    active profile is read and the cached web evidence is loaded from disk.
    """
    tools: Dict[str, Dict[str, Any]] = {}
    tool_meta = load_tool_meta()
    outcomes = load_outcomes() if outcomes is None else outcomes
    for record in records:
        best: Optional[Dict[str, Any]] = None
        # Roundups still contribute their links to the tool index below; they
        # just do not inherit any single link's verdict as their own.
        inherits = len(record.get("tool_keys") or []) <= MAX_TOOLS_FOR_VERDICT_INHERITANCE
        for index, key in enumerate(record.get("tool_keys") or []):
            entry = verdicts.get(key.lower()) or {}
            url = ""
            for candidate in record.get("external_urls") or []:
                if tool_key(candidate) == key:
                    url = candidate
                    break
            tool = tools.setdefault(
                key,
                {
                    "key": key,
                    "name": entry.get("name") or key,
                    "url": url,
                    "label": short_link_label(url) if url else key,
                    # Lets the dashboard answer "which repo earned the repo-link
                    # bonus" without re-deriving REPO_HOSTS in JavaScript.
                    "is_repo": key.split("/", 1)[0] in REPO_HOSTS,
                    "verdict": entry.get("verdict") or "unreviewed",
                    "rank": entry.get("rank"),
                    "lane": entry.get("lane") or "",
                    "what": entry.get("what") or "",
                    "why": entry.get("why") or "",
                    "first_step": entry.get("first_step") or "",
                    "reason_code": entry.get("reason_code") or "",
                    "stars": entry.get("stars"),
                    "license": entry.get("license") or "",
                    "last_push": entry.get("last_push") or "",
                    "mentions": 0,
                    "resource_ids": [],
                    "best_score": 0.0,
                    "latest_share": None,
                    "auto": False,
                    "facts": None,
                    "meta_loaded": False,
                    "outcome": (outcomes.get(key.lower()) or {}).get("state") or "",
                    "outcome_note": (outcomes.get(key.lower()) or {}).get("note") or "",
                    "outcome_at": (outcomes.get(key.lower()) or {}).get("decided_at") or "",
                    # The lane of the post that introduced it, so the dashboard
                    # offers "must read" for an article and "must try" for a CLI.
                    "resource_type": record.get("resource_type") or "other",
                },
            )
            if not tool.get("meta_loaded"):
                fact = tool_meta.get(key.lower(), {})
                tool["meta_loaded"] = True
                if fact:
                    tool["facts"] = {
                        "stars": fact.get("stars"),
                        "pushed_at": fact.get("pushed_at"),
                        "archived": fact.get("archived"),
                        "license": fact.get("license") or "",
                        "language": fact.get("language") or "",
                        "description": fact.get("description") or "",
                        "checked_at": (fact.get("fetched_at") or "")[:10],
                        "ok": bool(fact.get("ok")),
                    }
                    if not tool["stars"]:
                        tool["stars"] = fact.get("stars")
                    if not tool["license"]:
                        tool["license"] = fact.get("license") or ""
                    if not tool["last_push"]:
                        tool["last_push"] = fact.get("pushed_at") or ""
                    if not tool["what"]:
                        tool["what"] = fact.get("description") or ""
                    if tool["verdict"] == "unreviewed":
                        code, human = auto_gate(fact)
                        if code:
                            tool["verdict"] = "excluded"
                            tool["reason_code"] = code
                            tool["why"] = human
                            tool["auto"] = True
            tool["mentions"] += 1
            if record["resource_id"] not in tool["resource_ids"]:
                tool["resource_ids"].append(record["resource_id"])
            tool["best_score"] = max(tool["best_score"], float(record.get("pick_score") or 0.0))
            shared = record.get("shared_at") or record.get("first_seen_at")
            if shared and (tool["latest_share"] is None or shared > tool["latest_share"]):
                tool["latest_share"] = shared
            if (
                entry
                and inherits
                and (
                    best is None
                    or VERDICT_STRENGTH.get(entry.get("verdict"), 0)
                    > VERDICT_STRENGTH.get(best.get("verdict"), 0)
                )
            ):
                best = entry
        # Rescore with everything now known: the verdict you made and the health
        # facts of the linked repo. Without this the arithmetic ignores both, and
        # an explicitly excluded tool can still rank near the top.
        if record.get("status") == "relevant":
            best_facts: Dict[str, Any] = {}
            for key in record.get("tool_keys") or []:
                candidate = tool_meta.get(key.lower()) or {}
                if candidate.get("ok"):
                    best_facts = candidate
                    break
            rescored = compute_pick_score(
                record,
                verdict=str((best or {}).get("verdict") or ""),
                facts=best_facts,
            )
            record["pick_score"] = rescored["score"]
            record["pick_parts"] = rescored["parts"]

        if best:
            best_outcome = outcomes.get(
                tool_key("https://" + str(best.get("key") or "").lstrip("/")).lower()
            ) or {}
            record["verdict"] = {
                "outcome": best_outcome.get("state") or "",
                "outcome_note": best_outcome.get("note") or "",
                "name": best.get("name") or "",
                "verdict": best.get("verdict"),
                "rank": best.get("rank"),
                "why": best.get("why") or "",
                "first_step": best.get("first_step") or "",
                "reason_code": best.get("reason_code") or "",
            }
    for entry in verdicts.values():
        key = tool_key("https://" + str(entry.get("key") or "").lstrip("/"))
        if not key or key.lower() in {k.lower() for k in tools}:
            continue
        url = "https://" + str(entry.get("key") or "").lstrip("/")
        tools[key] = {
            "key": key,
            "name": entry.get("name") or key,
            "url": url,
            "label": short_link_label(url),
            "is_repo": key.split("/", 1)[0] in REPO_HOSTS,
            "verdict": entry.get("verdict") or "unreviewed",
            "rank": entry.get("rank"),
            "lane": entry.get("lane") or "",
            "what": entry.get("what") or "",
            "why": entry.get("why") or "",
            "first_step": entry.get("first_step") or "",
            "reason_code": entry.get("reason_code") or "",
            "stars": entry.get("stars"),
            "license": entry.get("license") or "",
            "last_push": entry.get("last_push") or "",
            "mentions": 0,
            "resource_ids": [],
            "best_score": 0.0,
            "latest_share": None,
            "outcome": (outcomes.get(key.lower()) or {}).get("state") or "",
            "outcome_note": (outcomes.get(key.lower()) or {}).get("note") or "",
            "outcome_at": (outcomes.get(key.lower()) or {}).get("decided_at") or "",
        }

    ordered = sorted(
        tools.values(),
        key=lambda t: (
            -VERDICT_STRENGTH.get(t["verdict"], 0),
            t["rank"] if t["rank"] is not None else 999,
            -t["best_score"],
        ),
    )
    if profile is None:
        try:
            profile = load_profile()
        except (OSError, RuntimeError, json.JSONDecodeError):
            # No active profile → no project-fit claims. Eligibility itself
            # never depends on the profile, so the queue still fills.
            profile = {}
    recommend_eligibility.annotate_tools(
        ordered, records, profile=profile, evidence_store=evidence_store
    )
    return ordered


def select_payload_records(records: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Which resources the dashboard actually carries.

    All group shares, because that is the briefing — including resources first
    seen as bookmarks that somebody later shared in the group (in_group derives
    from actual group occurrences, so the first-origin source label cannot hide
    them). From purely imported sources, only relevant rows, highest-scoring
    first, capped — a 25k archive would produce a ~60 MB payload and an
    unusable page. Nothing is lost: the full corpus stays in the database and
    in all-resources.csv.
    """

    def carried_as_group(record: Dict[str, Any]) -> bool:
        in_group = record.get("in_group")
        if in_group is not None:
            return bool(in_group)
        return (record.get("source") or "group") in DASHBOARD_GROUP_SOURCES

    group = [r for r in records if carried_as_group(r)]
    imported = [
        r
        for r in records
        if not carried_as_group(r) and r.get("status") == "relevant"
    ]
    imported.sort(
        key=lambda r: (-(float(r.get("pick_score") or 0.0)), str(r.get("shared_at") or "")),
    )
    return group + imported[:DASHBOARD_BOOKMARK_CAP]


def export_relevant(conn: sqlite3.Connection, profile: Dict[str, Any]) -> Dict[str, Any]:
    all_rows = select_resource_rows(conn)
    all_records = [resource_to_dict(row) for row in all_rows]
    tools = build_tool_index(all_records, load_verdicts())
    rows = [row for row in all_rows if row["status"] == "relevant"]
    records = [record for record in all_records if record["status"] == "relevant"]

    jsonl = "".join(
        json.dumps(record, ensure_ascii=False) + "\n" for record in records
    )
    atomic_write(DATA_DIR / "relevant.jsonl", jsonl)

    csv_path = DATA_DIR / "relevant.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix="relevant.csv.", dir=str(csv_path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            fields = [
                "resource_id",
                "kind",
                "url",
                "message_id",
                "sender_id",
                "sender_username",
                "shared_at",
                "share_count",
                "author",
                "title",
                "text",
                "score",
                "project_areas",
                "reasons",
                "decision_source",
                "first_seen_at",
                "resource_type",
                "pick_score",
                "external_urls",
                "verdict",
                "verdict_why",
                "outcome",
                "outcome_note",
            ]
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            sheet_rows = []
            for record in records:
                out = {field: record.get(field, "") for field in fields}
                out["project_areas"] = "; ".join(record["project_areas"])
                out["reasons"] = "; ".join(record["reasons"])
                out["external_urls"] = "; ".join(record.get("external_urls") or [])
                out["verdict"] = (record.get("verdict") or {}).get("verdict", "")
                out["verdict_why"] = (record.get("verdict") or {}).get("why", "")
                out["outcome"] = (record.get("verdict") or {}).get("outcome", "")
                out["outcome_note"] = (record.get("verdict") or {}).get("outcome_note", "")
                writer.writerow(out)
                sheet_rows.append(out)
        os.replace(tmp_name, csv_path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)

    # A07: relevant.csv above stays a RAW machine format (original text).
    # The spreadsheet the human opens is a separate neutralized artifact,
    # built from the very same flattened rows so content cannot diverge.
    import export_safety

    export_safety.write_human_sheet_csv(
        DATA_DIR / export_safety.HUMAN_SHEET_NAME, fields, sheet_rows
    )

    labels = {"ai": "Artificial intelligence", **{
        name: area.get("label", name)
        for name, area in profile["selection"].get("project_areas", {}).items()
    }}
    lines = [
        "# Relevant Group Shares",
        "",
        "Updated: {}".format(utc_now()),
        "",
        "Total relevant resources: **{}**".format(len(records)),
        "",
    ]
    for record in records[:100]:
        title = (record["title"] or record["text"] or record["url"] or "Untitled").strip()
        title = " ".join(title.split())[:220]
        area_text = ", ".join(labels.get(name, name) for name in record["project_areas"])
        reason_text = "; ".join(record["reasons"])
        lines.append("## {}".format(title))
        lines.append("")
        if record["url"]:
            lines.append("- Source: {}".format(record["url"]))
        if record["author"]:
            lines.append("- Author: @{}".format(record["author"].lstrip("@")))
        if record["sender_username"]:
            lines.append("- Shared by: @{}".format(record["sender_username"].lstrip("@")))
        if area_text:
            lines.append("- Fit: {}".format(area_text))
        if reason_text:
            lines.append("- Why: {}".format(reason_text))
        lines.append("")
    atomic_write(DATA_DIR / "latest.md", "\n".join(lines).rstrip() + "\n")

    audit_path = DATA_DIR / "all-resources.csv"
    fd, tmp_name = tempfile.mkstemp(prefix="all-resources.csv.", dir=str(audit_path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            fields = [
                "resource_id",
                "status",
                "kind",
                "url",
                "message_id",
                "sender_id",
                "sender_username",
                "sender_display_name",
                "shared_at",
                "share_count",
                "sharer_count",
                "sharers",
                "author",
                "title",
                "text",
                "score",
                "project_areas",
                "reasons",
                "decision_source",
                "hydration_attempts",
                "last_error",
                "first_seen_at",
                "updated_at",
                "resource_type",
                "pick_score",
                "external_urls",
            ]
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for record in all_records:
                out = {field: record.get(field, "") for field in fields}
                out["project_areas"] = "; ".join(record["project_areas"])
                out["reasons"] = "; ".join(record["reasons"])
                out["sharers"] = "; ".join(record["sharers"])
                out["external_urls"] = "; ".join(record.get("external_urls") or [])
                writer.writerow(out)
        os.replace(tmp_name, audit_path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)

    unavailable_records = [
        record for record in all_records if record["status"] == "unavailable"
    ]
    atomic_write(
        DATA_DIR / "unavailable.jsonl",
        "".join(
            json.dumps(record, ensure_ascii=False) + "\n"
            for record in unavailable_records
        ),
    )

    sender_rows = conn.execute(
        """
        SELECT
            s.sender_id, s.username, s.display_name, s.avatar_url, s.is_owner,
            COUNT(DISTINCT m.message_id) AS message_count,
            COUNT(DISTINCT mr.resource_id) AS resource_count
        FROM senders s
        LEFT JOIN messages m ON m.sender_id = s.sender_id
        LEFT JOIN message_resources mr ON mr.message_id = m.message_id
        GROUP BY s.sender_id
        HAVING COUNT(DISTINCT m.message_id) > 0
        ORDER BY message_count DESC, s.sender_id
        """
    ).fetchall()
    sender_records = [dict(row) for row in sender_rows]
    from dashboard_renderer import build_dashboard_payload, render_dashboard_from_payload

    payload_records = select_payload_records(all_records)
    payload = build_dashboard_payload(
        resources=payload_records,
        senders=sender_records,
        status=status_snapshot(conn),
        project_areas=labels,
        group_name=str(profile["conversation"].get("name") or "X Group"),
        generated_at=utc_now(),
        conversation_id=str(profile["conversation"].get("id") or ""),
        schedule=DASHBOARD_SCHEDULE,
        tools=tools,
        negative_proposals=load_negative_proposals(),
    )
    # The JSON twin lets a served page re-fetch and re-render in place; the
    # HTML keeps the same document inline so file:// still works offline.
    atomic_write(
        DATA_DIR / "dashboard-data.json",
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
    )
    atomic_write(DATA_DIR / "dashboard.html", render_dashboard_from_payload(payload))
    return {
        "relevant": len(records),
        "latest_rows": min(100, len(records)),
        "all_resources": len(all_rows),
        "unavailable": len(unavailable_records),
        "senders": len(sender_records),
        "dashboard": str(DATA_DIR / "dashboard.html"),
        "dashboard_data": str(DATA_DIR / "dashboard-data.json"),
        "payload_resources": len(payload_records),
        "tools": len(tools),
        "must_try": sum(1 for t in tools if t["verdict"] == "must_try"),
        "excluded": sum(1 for t in tools if t["verdict"] == "excluded"),
    }


def status_snapshot(conn: sqlite3.Connection) -> Dict[str, Any]:
    counts = {status: 0 for status in VALID_STATUSES}
    for row in conn.execute("SELECT status, COUNT(*) AS n FROM resources GROUP BY status"):
        counts[row["status"]] = int(row["n"])
    totals = conn.execute(
        "SELECT COUNT(*) AS messages, SUM(is_owner) AS owner_messages, "
        "COUNT(DISTINCT sender_id) AS senders FROM messages"
    ).fetchone()
    occurrences = conn.execute("SELECT COUNT(*) FROM message_resources").fetchone()[0]
    unattempted = conn.execute(
        "SELECT COUNT(*) AS n FROM resources "
        "WHERE status = 'pending_hydration' AND hydration_attempts = 0"
    ).fetchone()["n"]
    snapshot = {
        "updated_at": utc_now(),
        "fetch_cursor": get_meta(conn, "fetch_cursor"),
        "fetch_incomplete": get_meta(conn, "fetch_incomplete", "false") == "true",
        "last_fetch_at": get_meta(conn, "last_fetch_at"),
        "last_fetch_error": get_meta(conn, "last_fetch_error", ""),
        "messages_captured": int(totals["messages"] or 0),
        "owner_messages_captured": int(totals["owner_messages"] or 0),
        "non_owner_messages_captured": int(totals["messages"] or 0)
        - int(totals["owner_messages"] or 0),
        "senders_captured": int(totals["senders"] or 0),
        "resource_occurrences": int(occurrences or 0),
        "capture_scope_version": get_meta(conn, "capture_scope_version", "legacy-owner-only"),
        "resources": sum(counts.values()),
        "status_counts": counts,
        "unattempted_hydration": int(unattempted or 0),
    }
    snapshot["gate_ready"] = (
        not snapshot["fetch_incomplete"]
        and snapshot["capture_scope_version"] == CAPTURE_SCOPE_VERSION
        and counts["pending_review"] == 0
        and snapshot["unattempted_hydration"] == 0
    )
    atomic_write(DATA_DIR / "status.json", json.dumps(snapshot, indent=2) + "\n")
    return snapshot


def prepare_review_batch(
    conn: sqlite3.Connection,
    profile: Dict[str, Any],
    limit: int,
    out_path: Path,
) -> Dict[str, Any]:
    rows = conn.execute(
        "SELECT * FROM resources WHERE status = 'pending_review' "
        "ORDER BY CAST(last_message_id AS INTEGER) ASC LIMIT ?",
        (limit,),
    ).fetchall()
    items = []
    for row in rows:
        record = resource_to_dict(row)
        payload = {}
        if row["payload_json"]:
            try:
                payload = json.loads(row["payload_json"])
            except json.JSONDecodeError:
                payload = {}
        media = payload.get("media") if isinstance(payload.get("media"), list) else []
        quoted = payload.get("quotedTweet") if isinstance(payload.get("quotedTweet"), dict) else {}
        record["media_urls"] = [item.get("url") for item in media if item.get("url")]
        record["quoted_text"] = str(quoted.get("text") or "")
        items.append(record)

    areas = {
        name: {
            "label": area.get("label", name),
            "description": area.get("description", ""),
        }
        for name, area in profile["selection"].get("project_areas", {}).items()
    }
    batch = {
        "created_at": utc_now(),
        "criteria": (
            "Mark relevant when the resource concerns AI or could materially help one "
            "of the listed existing project areas. Prefer high recall. General lifestyle, "
            "entertainment, unrelated trading, and generic news are irrelevant."
        ),
        "project_areas": areas,
        "items": items,
        "decision_schema": {
            "decisions": [
                {
                    "resource_id": "tweet:123",
                    "relevant": True,
                    "project_areas": ["ai-agent-systems"],
                    "reason": "Concrete benefit",
                }
            ]
        },
    }
    atomic_write(out_path, json.dumps(batch, ensure_ascii=False, indent=2) + "\n")
    return {"count": len(items), "path": str(out_path)}


def apply_decisions(
    conn: sqlite3.Connection, profile: Dict[str, Any], decisions_path: Path
) -> Dict[str, int]:
    payload = load_json(decisions_path)
    decisions = payload.get("decisions") if isinstance(payload, dict) else None
    if not isinstance(decisions, list):
        raise RuntimeError("decision file must contain a decisions list")
    valid_areas = set(profile["selection"].get("project_areas", {})) | {"ai"}
    seen = set()
    applied = 0
    relevant_count = 0
    with conn:
        for decision in decisions:
            if not isinstance(decision, dict):
                raise RuntimeError("every decision must be an object")
            resource_id = str(decision.get("resource_id") or "")
            if not resource_id or resource_id in seen:
                raise RuntimeError("decision resource IDs must be present and unique")
            seen.add(resource_id)
            if not isinstance(decision.get("relevant"), bool):
                raise RuntimeError("decision relevant must be true or false")
            reason = str(decision.get("reason") or "").strip()
            if len(reason) < 5:
                raise RuntimeError("every decision needs a concrete reason")
            areas = decision.get("project_areas") or []
            if not isinstance(areas, list) or any(area not in valid_areas for area in areas):
                raise RuntimeError("decision contains an unknown project area")
            row = conn.execute(
                "SELECT status FROM resources WHERE resource_id = ?", (resource_id,)
            ).fetchone()
            if not row:
                raise RuntimeError("unknown resource in decisions: {}".format(resource_id))
            if row["status"] != "pending_review":
                raise RuntimeError(
                    "resource {} is not pending review".format(resource_id)
                )
            relevant = bool(decision["relevant"])
            status = "relevant" if relevant else "irrelevant"
            if relevant:
                relevant_count += 1
            conn.execute(
                """
                UPDATE resources
                SET status = ?, score = ?, project_areas_json = ?, reasons_json = ?,
                    decision_source = 'claude', updated_at = ?
                WHERE resource_id = ?
                """,
                (
                    status,
                    3 if relevant else 0,
                    json.dumps(areas),
                    json.dumps([reason], ensure_ascii=False),
                    utc_now(),
                    resource_id,
                ),
            )
            applied += 1
    export_relevant(conn, profile)
    status_snapshot(conn)
    return {"applied": applied, "relevant": relevant_count, "irrelevant": applied - relevant_count}


def requeue_resources(
    conn: sqlite3.Connection, profile: Dict[str, Any], resource_ids: Sequence[str]
) -> Dict[str, int]:
    if not resource_ids or len(set(resource_ids)) != len(resource_ids):
        raise RuntimeError("requeue resource IDs must be present and unique")
    requeued = 0
    with conn:
        for resource_id in resource_ids:
            row = conn.execute(
                "SELECT kind, payload_json, content_text FROM resources WHERE resource_id = ?",
                (resource_id,),
            ).fetchone()
            if not row:
                raise RuntimeError("unknown resource for requeue: {}".format(resource_id))
            status = (
                "pending_review"
                if row["kind"] != "tweet" or row["payload_json"] or row["content_text"]
                else "pending_hydration"
            )
            conn.execute(
                """
                UPDATE resources
                SET status = ?, score = NULL, project_areas_json = NULL,
                    reasons_json = NULL, decision_source = NULL, updated_at = ?
                WHERE resource_id = ?
                """,
                (status, utc_now(), resource_id),
            )
            requeued += 1
    export_relevant(conn, profile)
    status_snapshot(conn)
    return {"requeued": requeued}


def baseline_notifications(conn: sqlite3.Connection) -> Dict[str, Any]:
    now = utc_now()
    cutover = get_meta(conn, "fetch_cursor")
    if not cutover or not cutover.isdigit():
        raise RuntimeError("cannot arm notifications without a durable fetch cursor")
    with conn:
        relation_cursor = conn.execute(
            """
            UPDATE message_resources
            SET notified_at = ?
            WHERE notified_at IS NULL
              AND CAST(message_id AS INTEGER) <= CAST(? AS INTEGER)
              AND resource_id IN (
                  SELECT resource_id FROM resources WHERE status = 'relevant'
              )
            """,
            (now, cutover),
        )
        conn.execute(
            "UPDATE resources SET notified_at = ? "
            "WHERE status = 'relevant' AND notified_at IS NULL "
            "AND CAST(last_message_id AS INTEGER) <= CAST(? AS INTEGER)",
            (now, cutover),
        )
        set_meta(conn, "notification_baseline_at", now)
        set_meta(conn, "notification_cutover_cursor", cutover)
        set_meta(conn, "notifications_enabled", "true")
    return {
        "acknowledged": relation_cursor.rowcount,
        "cutover_message_id": cutover,
        "notifications_enabled": True,
    }


def _decision_offer(records: Sequence[Dict[str, Any]]) -> Optional[Dict[str, str]]:
    """The one tool in this batch worth asking about, if any.

    Only tools with no verdict yet, and only one per message: a keyboard with
    several tools on it cannot say which button belongs to which.
    """
    verdicts = load_verdicts()
    for record in records:
        for key in record.get("tool_keys") or []:
            if key.lower() in verdicts:
                continue
            url = ""
            for candidate in record.get("external_urls") or []:
                if tool_key(candidate) == key:
                    url = candidate
                    break
            return {"key": key, "name": key, "url": url}
    return None


def notify_relevant(conn: sqlite3.Connection, live: bool) -> Dict[str, Any]:
    if get_meta(conn, "notifications_enabled", "false") != "true":
        pending = conn.execute(
            """
            SELECT COUNT(*)
            FROM message_resources mr
            JOIN resources r ON r.resource_id = mr.resource_id
            WHERE r.status = 'relevant' AND mr.notified_at IS NULL
            """
        ).fetchone()[0]
        return {
            "pending": int(pending),
            "notified": 0,
            "live": live,
            "suppressed": "notification baseline has not been armed",
        }
    cutover = get_meta(conn, "notification_cutover_cursor")
    if not cutover or not cutover.isdigit():
        raise RuntimeError("notification cutover cursor is missing")
    now = utc_now()
    with conn:
        conn.execute(
            """
            UPDATE message_resources
            SET notified_at = ?
            WHERE notified_at IS NULL
              AND CAST(message_id AS INTEGER) <= CAST(? AS INTEGER)
              AND resource_id IN (
                  SELECT resource_id FROM resources WHERE status = 'relevant'
              )
            """,
            (now, cutover),
        )
        conn.execute(
            "UPDATE resources SET notified_at = ? "
            "WHERE status = 'relevant' AND notified_at IS NULL "
            "AND CAST(last_message_id AS INTEGER) <= CAST(? AS INTEGER)",
            (now, cutover),
        )
    rows = conn.execute(
        """
        SELECT
            r.*,
            mr.message_id AS notification_message_id,
            m.sent_at_ms AS shared_at_ms,
            m.sender_id AS last_sender_id,
            m.is_owner AS last_sender_is_owner,
            s.username AS sender_username,
            s.display_name AS sender_display_name,
            s.avatar_url AS sender_avatar_url
        FROM message_resources mr
        JOIN resources r ON r.resource_id = mr.resource_id
        JOIN messages m ON m.message_id = mr.message_id
        LEFT JOIN senders s ON s.sender_id = m.sender_id
        WHERE r.status = 'relevant'
          AND mr.notified_at IS NULL
          AND CAST(mr.message_id AS INTEGER) > CAST(? AS INTEGER)
        ORDER BY CAST(mr.message_id AS INTEGER) ASC
        """,
        (cutover,),
    ).fetchall()
    if not rows:
        return {"pending": 0, "notified": 0, "live": live}
    records = []
    for row in rows:
        record = resource_to_dict(row)
        record["message_id"] = row["notification_message_id"]
        records.append(record)
    lines = ["Group filter: {} new relevant share(s)".format(len(records))]
    for record in records[:8]:
        summary = " ".join(
            (record["title"] or record["text"] or record["url"] or "Untitled").split()
        )[:180]
        areas = ", ".join(record["project_areas"][:3])
        shared_by = (
            " via @{}".format(record["sender_username"].lstrip("@"))
            if record["sender_username"]
            else ""
        )
        lines.append("- {}{}{}".format(summary, shared_by, " [{}]".format(areas) if areas else ""))
        if record["url"]:
            lines.append("  " + record["url"])
    if len(records) > 8:
        lines.append(
            "+{} more in {}".format(len(records) - 8, DATA_DIR / "latest.md")
        )
    message = "\n".join(lines)
    if not live:
        return {"pending": len(records), "notified": 0, "live": False, "preview": message}

    # If the batch introduces a tool with no verdict yet, offer the decision in
    # the message itself. Best effort: a keyboard problem must never stop the
    # notification, so this falls back to the plain shared notifier.
    keyboard = None
    delivered = False
    try:
        offer = _decision_offer(records)
        if offer:
            import notify_buttons
            import telegram_decisions

            telegram_decisions.register_pending([(offer["key"], offer["name"])])
            keyboard = telegram_decisions.build_keyboard(offer["key"], offer.get("url", ""))
            message += "\n\nDecide {}:".format(offer["name"])
            outcome = notify_buttons.send(message, keyboard)
            delivered = bool(outcome.get("sent"))
    except Exception:  # noqa: BLE001 - never let the keyboard path break delivery
        delivered = False

    if not delivered:
        if not NOTIFY.exists():
            raise RuntimeError("Telegram notifier is missing")
        result = subprocess.run(
            [str(NOTIFY), message], capture_output=True, text=True, timeout=60, check=False
        )
        if result.returncode != 0:
            raise RuntimeError("Telegram notification failed")
    now = utc_now()
    with conn:
        conn.executemany(
            "UPDATE message_resources SET notified_at = ? "
            "WHERE message_id = ? AND resource_id = ?",
            [
                (now, row["notification_message_id"], row["resource_id"])
                for row in rows
            ],
        )
        conn.executemany(
            "UPDATE resources SET notified_at = ? WHERE resource_id = ?",
            [(now, resource_id) for resource_id in {row["resource_id"] for row in rows}],
        )
    return {"pending": len(records), "notified": len(records), "live": True}


def verify(conn: sqlite3.Connection, strict: bool) -> Dict[str, Any]:
    problems = []
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        problems.append("SQLite integrity_check: {}".format(integrity))
    invalid = conn.execute(
        "SELECT COUNT(*) FROM resources WHERE status NOT IN ({})".format(
            ",".join("?" for _ in VALID_STATUSES)
        ),
        tuple(sorted(VALID_STATUSES)),
    ).fetchone()[0]
    if invalid:
        problems.append("{} resources have invalid statuses".format(invalid))
    orphans = conn.execute(
        """
        SELECT COUNT(*) FROM message_resources mr
        LEFT JOIN messages m ON m.message_id = mr.message_id
        LEFT JOIN resources r ON r.resource_id = mr.resource_id
        WHERE m.message_id IS NULL OR r.resource_id IS NULL
        """
    ).fetchone()[0]
    if orphans:
        problems.append("{} message-resource relations are orphaned".format(orphans))
    sourceless = conn.execute(
        "SELECT COUNT(*) FROM resources WHERE source IS NULL OR TRIM(source) = ''"
    ).fetchone()[0]
    if sourceless:
        problems.append("{} resources have no source".format(sourceless))
    missing_reason = conn.execute(
        "SELECT COUNT(*) FROM resources WHERE status = 'relevant' "
        "AND (reasons_json IS NULL OR decision_source IS NULL)"
    ).fetchone()[0]
    if missing_reason:
        problems.append("{} relevant resources lack decision evidence".format(missing_reason))
    if get_meta(conn, "fetch_incomplete", "false") == "true":
        problems.append("latest fetch did not reach the durable checkpoint")
    if get_meta(conn, "capture_scope_version") != CAPTURE_SCOPE_VERSION:
        problems.append("all-sender historical replay has not completed")
    cursor = get_meta(conn, "fetch_cursor")
    if not cursor or not cursor.isdigit():
        problems.append("durable fetch cursor is missing or invalid")
    missing_senders = conn.execute(
        """
        SELECT COUNT(*) FROM messages m
        LEFT JOIN senders s ON s.sender_id = m.sender_id
        WHERE s.sender_id IS NULL
        """
    ).fetchone()[0]
    if missing_senders:
        problems.append("{} messages lack sender metadata".format(missing_senders))
    unlinked_content = conn.execute(
        """
        SELECT COUNT(*) FROM messages m
        WHERE (TRIM(m.text) <> '' OR m.urls_json <> '[]')
          AND NOT EXISTS (
              SELECT 1 FROM message_resources mr WHERE mr.message_id = m.message_id
          )
        """
    ).fetchone()[0]
    if unlinked_content:
        problems.append("{} content messages lack a classified resource".format(unlinked_content))
    if strict:
        pending_review = conn.execute(
            "SELECT COUNT(*) FROM resources WHERE status = 'pending_review'"
        ).fetchone()[0]
        if pending_review:
            problems.append("{} resources still need semantic review".format(pending_review))
        unattempted = conn.execute(
            "SELECT COUNT(*) FROM resources WHERE status = 'pending_hydration' "
            "AND hydration_attempts = 0"
        ).fetchone()[0]
        if unattempted:
            problems.append("{} resources have not had a hydration attempt".format(unattempted))
    for name in (
        "relevant.jsonl",
        "relevant.csv",
        "all-resources.csv",
        "unavailable.jsonl",
        "latest.md",
        "dashboard.html",
        "dashboard-data.json",
        "status.json",
    ):
        if not (DATA_DIR / name).exists():
            problems.append("missing output artifact: {}".format(name))
    result = {
        "verified_at": utc_now(),
        "strict": strict,
        "pass": not problems,
        "problems": problems,
        "status": status_snapshot(conn),
    }
    atomic_write(DATA_DIR / "verification.json", json.dumps(result, indent=2) + "\n")
    return result


def sync_once(
    conn: sqlite3.Connection,
    profile: Dict[str, Any],
    max_pages: int,
    max_hydrate: int,
    concurrency: int,
    since_override: Optional[str] = None,
    force_retry: bool = False,
) -> Tuple[Dict[str, Any], bool]:
    started = utc_now()
    run_id = conn.execute(
        "INSERT INTO runs(started_at, outcome, details_json) VALUES(?, 'running', '{}')",
        (started,),
    ).lastrowid
    conn.commit()
    details: Dict[str, Any] = {}
    outcome = "ok"
    try:
        cursor = since_override or initialize_cursor(conn, profile)
        if since_override:
            details["replay_from"] = since_override
        fetch = fetch_group_messages(profile, cursor, max_pages)
        details["fetch"] = persist_fetch(conn, profile, fetch, cursor)
        details["fetch"].update(
            {
                "pages": fetch.pages,
                "reached_checkpoint": fetch.reached_checkpoint,
                "oldest_message_id": fetch.oldest_message_id,
                "newest_message_id": fetch.newest_message_id,
            }
        )
        if (
            fetch.reached_checkpoint
            and since_override
            and since_override == str(profile["bootstrap"]["resume_after_message_id"])
        ):
            with conn:
                set_meta(conn, "capture_scope_version", CAPTURE_SCOPE_VERSION)
                set_meta(conn, "capture_scope_replayed_at", utc_now())
        if not fetch.reached_checkpoint:
            outcome = "error"
        details["hydration"] = hydrate_pending(
            conn, profile, max_hydrate, concurrency, force_retry=force_retry
        )
        details["rules"] = apply_rule_classification(conn, profile)
        details["export"] = export_relevant(conn, profile)
        details["status"] = status_snapshot(conn)
    except Exception as exc:
        outcome = "error"
        details["error"] = str(exc)
        with conn:
            set_meta(conn, "last_fetch_error", str(exc))
        raise
    finally:
        with conn:
            conn.execute(
                "UPDATE runs SET finished_at = ?, outcome = ?, details_json = ? WHERE id = ?",
                (utc_now(), outcome, json.dumps(details, ensure_ascii=False), run_id),
            )
    return details, outcome == "ok"


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--profile", type=Path, default=PROFILE_PATH)
    sub = parser.add_subparsers(dest="command", required=True)

    sync_parser = sub.add_parser("sync", help="capture, hydrate, and rule-filter new shares")
    sync_parser.add_argument("--max-pages", type=int, default=100)
    sync_parser.add_argument("--max-hydrate", type=int, default=120)
    sync_parser.add_argument("--concurrency", type=int, default=2)
    sync_parser.add_argument(
        "--force-retry",
        action="store_true",
        help="ignore retry timestamps for a supervised retry run",
    )
    sync_parser.add_argument(
        "--replay-bootstrap",
        action="store_true",
        help="idempotently replay from the audited bootstrap cursor to recover embedded payloads",
    )

    review_parser = sub.add_parser("prepare-review", help="write the next semantic-review batch")
    review_parser.add_argument("--limit", type=int, default=80)
    review_parser.add_argument("--out", type=Path, default=DATA_DIR / "review-batch.json")

    apply_parser = sub.add_parser("apply-decisions", help="apply validated semantic decisions")
    apply_parser.add_argument("decisions", type=Path)

    requeue_parser = sub.add_parser(
        "requeue", help="return selected resources to the appropriate supervised queue"
    )
    requeue_parser.add_argument("resource_ids", nargs="+")

    extract_parser = sub.add_parser(
        "extract-content",
        help="read pending url/media content through the C6 safe-fetch "
        "provider (deny-all stub until integrated) and rescore by rules; "
        "run export afterwards to refresh artifacts",
    )
    extract_parser.add_argument("--limit", type=int, default=25)

    sub.add_parser("export", help="rebuild current feed artifacts")
    verify_parser = sub.add_parser("verify", help="verify database and pipeline invariants")
    verify_parser.add_argument("--strict", action="store_true")
    sub.add_parser("status", help="print current monitor status")
    notify_parser = sub.add_parser("notify", help="preview or deliver unseen relevant shares")
    notify_parser.add_argument("--live", action="store_true")
    sub.add_parser("baseline", help="acknowledge current relevant rows without notification")

    args = parser.parse_args(argv)
    profile = load_profile(args.profile)
    conn = connect_db(args.db)
    try:
        if args.command == "sync":
            with exclusive_run_lock():
                details, ok = sync_once(
                    conn,
                    profile,
                    max_pages=args.max_pages,
                    max_hydrate=args.max_hydrate,
                    concurrency=args.concurrency,
                    since_override=(
                        str(profile["bootstrap"]["resume_after_message_id"])
                        if args.replay_bootstrap
                        else None
                    ),
                    force_retry=args.force_retry,
                )
            print_json(details)
            return 0 if ok else 1
        if args.command == "prepare-review":
            print_json(prepare_review_batch(conn, profile, args.limit, args.out))
            return 0
        if args.command == "apply-decisions":
            print_json(apply_decisions(conn, profile, args.decisions))
            return 0
        if args.command == "requeue":
            print_json(requeue_resources(conn, profile, args.resource_ids))
            return 0
        if args.command == "extract-content":
            import content_extraction

            with exclusive_run_lock():
                details = content_extraction.extract_pending_content(
                    conn, limit=args.limit
                )
                details["rules"] = apply_rule_classification(conn, profile)
                details["status"] = status_snapshot(conn)
            print_json(details)
            return 0
        if args.command == "export":
            print_json(export_relevant(conn, profile))
            print_json(status_snapshot(conn))
            return 0
        if args.command == "verify":
            result = verify(conn, args.strict)
            print_json(result)
            return 0 if result["pass"] else 1
        if args.command == "status":
            print_json(status_snapshot(conn))
            return 0
        if args.command == "notify":
            print_json(notify_relevant(conn, args.live))
            return 0
        if args.command == "baseline":
            print_json(baseline_notifications(conn))
            return 0
    finally:
        conn.close()
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print("group-monitor: {}".format(exc), file=sys.stderr)
        sys.exit(1)
