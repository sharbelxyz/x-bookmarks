#!/usr/bin/env python3
"""Bounded content extraction for captured resources (lane 01, audit A04).

Turns honest "pending" capture states into evidence-backed ones by actually
reading linked content — through the C6 safe-fetch contract only. Policy
(schemes, private-network targets, redirect re-validation, byte/time/redirect
budgets) is enforced by the provider, which lane 05 owns; this module never
opens sockets itself. Until that provider is integrated, the frozen deny-all
stub answers every fetch with ``provider_unavailable`` and rows stay
``pending`` — deny by default, exactly as revision c1 freezes it.

Honesty rules encoded here:
- URL text or a thumbnail is never proof the destination was read; only a
  provider ``ok`` moves a row to ``ok``.
- Formats we cannot interpret with approved local capabilities (e.g. PDF,
  arbitrary binaries) become ``unsupported`` with an explicit reason and the
  byte-level evidence we do have.
- Native media bytes are verified for existence/type/size only; semantic
  review flows through the existing group-filter review path. Nothing is sent
  to any new external model, and no media bytes are written to disk here.
- Detail strings carry hosts, sizes and reasons — never full URLs, which for
  private media can embed tokens.
"""

from __future__ import annotations

import html
import json
import sqlite3
import urllib.parse
from html.parser import HTMLParser
from typing import Any, Callable, Dict, List, Optional, Tuple

try:  # Lane 05's real provider (C6), once Chat 07 integrates it.
    from safe_fetch import safe_fetch  # type: ignore
    SAFE_FETCH_PROVIDER = "safe_fetch"
except ImportError:  # Frozen fallback: contract fixture copy, denies everything.
    from safe_fetch_stub import safe_fetch  # type: ignore
    SAFE_FETCH_PROVIDER = "safe_fetch_stub"

# C6 default budgets (contract revision c1). Passed explicitly on every call
# so the call site, not the provider default, is the documented bound.
MAX_FETCH_BYTES = 4_000_000
FETCH_TIMEOUT = 20.0
MAX_REDIRECTS = 4

# Content we can actually interpret with stdlib capabilities available in
# this project. Anything else that fetches fine is honest "unsupported".
TEXTUAL_CONTENT_TYPES = (
    "text/html",
    "application/xhtml+xml",
    "text/plain",
    "text/markdown",
    "application/json",
)
# Documents we accept bytes for but cannot parse locally today.
DOCUMENT_CONTENT_TYPES = ("application/pdf",)
# Native media: existence/type/size evidence only.
MEDIA_CONTENT_TYPES = ("image/", "video/")

EXCERPT_CHARS = 20_000
TITLE_CHARS = 240
DEFAULT_LIMIT = 25
FetchFn = Callable[..., Dict[str, Any]]


class _PageText(HTMLParser):
    """Minimal stdlib HTML → (title, visible text) extractor."""

    _SKIP = {"script", "style", "noscript", "template"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: List[str] = []
        self.text_parts: List[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
        elif data.strip():
            self.text_parts.append(data.strip())


def parse_html(document: str) -> Tuple[str, str]:
    parser = _PageText()
    try:
        parser.feed(document)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed markup still yields best effort
        pass
    title = html.unescape(" ".join(" ".join(parser.title_parts).split()))[:TITLE_CHARS]
    text = "\n".join(parser.text_parts)[:EXCERPT_CHARS]
    return title, text


def _host(url: str) -> str:
    """Log-safe target label: the host only, never path/query/tokens."""
    try:
        return urllib.parse.urlsplit(str(url)).netloc.lower() or "unknown-host"
    except ValueError:
        return "unknown-host"


def _base_content_type(value: Any) -> str:
    return str(value or "").split(";")[0].strip().lower()


def _media_urls(payload_json: Optional[str]) -> List[str]:
    try:
        payload = json.loads(payload_json) if payload_json else {}
    except json.JSONDecodeError:
        return []
    media = payload.get("media") if isinstance(payload, dict) else None
    urls: List[str] = []
    for item in media or []:
        if isinstance(item, dict) and item.get("url"):
            urls.append(str(item["url"]))
    return urls


def _classify_failure(result: Dict[str, Any], target_host: str) -> Tuple[str, str]:
    """Map a C6 non-ok result onto (extraction_state, detail)."""
    reason = str(result.get("denied_reason") or "")
    if reason == "provider_unavailable":
        return (
            "pending",
            "safe-fetch provider unavailable; deny-by-default until the C6 "
            "provider is integrated",
        )
    if reason in {"scheme", "private_target", "redirect_target"}:
        return "failed", "denied: {} ({})".format(reason, target_host)
    if reason == "content_type":
        return (
            "unsupported",
            "content-type {} not accepted for extraction ({})".format(
                _base_content_type(result.get("content_type")) or "unknown", target_host
            ),
        )
    if reason == "too_large":
        return (
            "failed",
            "exceeded {} byte budget ({})".format(MAX_FETCH_BYTES, target_host),
        )
    if reason == "timeout":
        return (
            "failed",
            "exceeded {}s time budget ({})".format(int(FETCH_TIMEOUT), target_host),
        )
    error = str(result.get("error") or reason or "fetch failed")[:200]
    return "failed", "{} ({})".format(error, target_host)


def _extract_url(
    row: sqlite3.Row, fetcher: FetchFn
) -> Tuple[str, str, Optional[str], Optional[str]]:
    """Returns (state, detail, title_or_None, content_text_or_None)."""
    url = str(row["canonical_url"] or "")
    scheme = urllib.parse.urlsplit(url).scheme.lower() if url else ""
    if scheme not in {"http", "https"}:
        # Defense in depth: capture already restricts to http/https, and the
        # provider re-checks; a row edited any other way still never fetches.
        return "failed", "denied: scheme {} (never fetched)".format(scheme or "empty"), None, None
    result = fetcher(
        url,
        max_bytes=MAX_FETCH_BYTES,
        timeout=FETCH_TIMEOUT,
        max_redirects=MAX_REDIRECTS,
        allowed_content_types=TEXTUAL_CONTENT_TYPES + DOCUMENT_CONTENT_TYPES,
    )
    host = _host(result.get("final_url") or url)
    if not result.get("ok"):
        state, detail = _classify_failure(result, host)
        return state, detail, None, None
    content_type = _base_content_type(result.get("content_type"))
    size = int(result.get("bytes") or 0)
    if content_type in TEXTUAL_CONTENT_TYPES:
        body = str(result.get("text") or "")
        if content_type in ("text/html", "application/xhtml+xml"):
            title, text = parse_html(body)
        else:
            title, text = "", body[:EXCERPT_CHARS]
        detail = "read {} bytes ({}) from {}".format(size, content_type, host)
        return "ok", detail, (title or None), (text or None)
    if content_type in DOCUMENT_CONTENT_TYPES:
        return (
            "unsupported",
            "no approved local extractor for {}; {} bytes verified at {}".format(
                content_type, size, host
            ),
            None,
            None,
        )
    return (
        "unsupported",
        "content-type {} not extractable locally ({} bytes at {})".format(
            content_type or "unknown", size, host
        ),
        None,
        None,
    )


def _extract_media(row: sqlite3.Row, fetcher: FetchFn) -> Tuple[str, str]:
    urls = _media_urls(row["payload_json"])
    if not urls:
        return (
            "unsupported",
            "no fetchable media URL in retained attachment metadata",
        )
    url = urls[0]
    if urllib.parse.urlsplit(url).scheme.lower() != "https":
        return "failed", "denied: non-https media target (never fetched)"
    result = fetcher(
        url,
        max_bytes=MAX_FETCH_BYTES,
        timeout=FETCH_TIMEOUT,
        max_redirects=MAX_REDIRECTS,
        allowed_content_types=MEDIA_CONTENT_TYPES,
    )
    host = _host(result.get("final_url") or url)
    if not result.get("ok"):
        return _classify_failure(result, host)
    extra = "" if len(urls) == 1 else "; {} further item(s) recorded".format(len(urls) - 1)
    return (
        "ok",
        "media bytes verified: {} bytes ({}) at {}{}; semantic review flows "
        "through the existing group-filter review path; nothing sent to any "
        "external model".format(
            int(result.get("bytes") or 0),
            _base_content_type(result.get("content_type")) or "unknown",
            host,
            extra,
        ),
    )


def extract_pending_content(
    conn: sqlite3.Connection,
    limit: int = DEFAULT_LIMIT,
    fetcher: Optional[FetchFn] = None,
    now: Optional[str] = None,
) -> Dict[str, Any]:
    """Process up to ``limit`` pending url/media rows through the C6 contract.

    Each row commits independently (extraction is idempotent and retryable),
    so one bad target cannot roll back the evidence of the others. Rows only
    leave ``pending`` on a definitive provider answer.
    """
    import datetime as dt

    fetcher = fetcher or safe_fetch
    stamp = now or dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    rows = conn.execute(
        """
        SELECT resource_id, kind, canonical_url, payload_json, title
        FROM resources
        WHERE extraction_state = 'pending' AND kind IN ('url', 'media')
        ORDER BY CAST(last_message_id AS INTEGER) DESC
        LIMIT ?
        """,
        (max(0, int(limit)),),
    ).fetchall()
    counts = {"examined": len(rows), "ok": 0, "pending": 0, "unsupported": 0, "failed": 0}
    for row in rows:
        title: Optional[str] = None
        text: Optional[str] = None
        if row["kind"] == "url":
            state, detail, title, text = _extract_url(row, fetcher)
        else:
            state, detail = _extract_media(row, fetcher)
        counts[state] += 1
        with conn:
            if state == "ok" and (title or text):
                conn.execute(
                    """
                    UPDATE resources
                    SET extraction_state = ?, extraction_detail = ?,
                        extraction_checked_at = ?, updated_at = ?,
                        title = COALESCE(NULLIF(title, ''), ?),
                        content_text = COALESCE(?, content_text)
                    WHERE resource_id = ?
                    """,
                    (state, detail, stamp, stamp, title, text, row["resource_id"]),
                )
            else:
                conn.execute(
                    """
                    UPDATE resources
                    SET extraction_state = ?, extraction_detail = ?,
                        extraction_checked_at = ?, updated_at = ?
                    WHERE resource_id = ?
                    """,
                    (state, detail, stamp, stamp, row["resource_id"]),
                )
    counts["provider"] = getattr(fetcher, "__module__", SAFE_FETCH_PROVIDER)
    return counts
