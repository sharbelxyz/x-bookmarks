#!/usr/bin/env python3
"""Type-aware review eligibility, evidence and project-action briefs (C5, A09).

The review queue used to admit only tools whose GitHub facts had been fetched
successfully — `(t.facts || {}).ok` in the dashboard — so hosted services,
articles, courses, creative methods and Saudi-relevant resources could never
reach a decision, however relevant. This module is the provider side of the
fix: every unreviewed tool/resource gets a `review_eligibility` block telling
the UI which lane it belongs to and why.

Contract (frozen at revision c1, `contracts/fixtures/c5-eligibility-entry.json`)::

    review_eligibility: {
        lane:        review | evidence_pending | blocked,
        reasons:     [string],                    # why not plain "review"
        evidence:    {source_url, checked_at,
                      extraction_state: ok|failed|pending|unsupported,
                      confidence: high|medium|low} | null,
        project_fit: {project, benefit, first_step, success_measure} | null,
    }

Additive fields carried alongside the frozen ones (optional for consumers):
`queue_band`, `constraints`, `retryable`, `action_brief`, and inside
`evidence`: `origin`, `age_days`, `content_flags`, `error`.

Hard rules, all enforced here and by the lane tests:

* **Evidence routes are typed.** A GitHub repo is judged on repository facts;
  a hosted service or article on its fetched destination page; a video
  tutorial, method or note on the captured text itself. Missing stars is a
  repo heuristic, never a universal exclusion.
* **A failed fetch never invents facts.** No description, licence, price,
  compatibility or adoption claim appears unless it came from an actual
  source. Unknown stays unknown — Saudi eligibility in particular is a
  `constraints` entry with state "unknown" until a human checks it.
* **Generated is not authored.** Everything this module produces is a
  proposal. It never writes `config/verdicts.json` / `config/outcomes.json`
  (the only authored stores, owned by C2) and it cannot mark anything
  installed, adopted or tried. Changed evidence yields a labelled `re_review`
  proposal, never a silently reopened decision.
* **Fetched content is data.** Retrieved text is stored inert (bounded
  excerpt), scanned for instruction-like patterns and flagged — it is never
  executed and never mutates a decision.
* **Bounded work.** Web evidence is cached with per-state TTLs, refreshed
  under an explicit per-run quota, and never triggers a mass reanalysis of
  the archive.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from resource_typing import parse_iso

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data" / "group-monitor"
# Generated evidence lives in data/ next to tool-meta.json, deliberately far
# from config/ where the authored decision files live: regenerable cache on
# one side, human judgement on the other.
WEB_EVIDENCE_PATH = DATA_DIR / "web-evidence.json"

ELIGIBILITY_LANES = ("review", "evidence_pending", "blocked")
EXTRACTION_STATES = ("ok", "failed", "pending", "unsupported")
CONFIDENCE_LEVELS = ("high", "medium", "low")

# Hosts whose destination is a video/media player: fetching the page cannot
# yield the actual content, so text extraction is permanently "unsupported"
# and the honest evidence is the captured description, not a pending fetch
# that would never help.
VIDEO_HOSTS = ("youtube.com", "youtu.be", "vimeo.com", "twitch.tv")

# Repo-facts route (C5 tool-meta) applies to these; everything else uses web
# or captured-text evidence. Mirrors resource_typing.REPO_HOSTS.
REPO_FACT_HOSTS = ("github.com",)

# Captured text below this length is a bare link, not reviewable evidence.
# URL-less items (notes, methods, threads) get a lower bar: their text is the
# complete artifact by construction — there is no destination to wait for.
MIN_CAPTURED_TEXT = 120
MIN_CAPTURED_TEXT_STANDALONE = 60

# Evidence freshness. Stale evidence keeps its lane but gains a visible
# reason; the bounded refresh loop re-fetches it before anything else new.
EVIDENCE_STALE_DAYS = 45

# C6 denied_reason → what it means for the queue. Policy denials are final
# (blocked); transient failures and provider absence stay retryable.
_BLOCKED_DENIALS = {"scheme", "private_target", "redirect_target"}
_RETRYABLE_DENIALS = {"too_large", "timeout", "error", "provider_unavailable"}

# Instruction-like patterns in retrieved content. Matching text is flagged so
# a reviewer sees "this page tries to talk to agents"; nothing here executes
# content under any circumstances, flag or no flag.
_INSTRUCTION_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"ignore\s+(all\s+|any\s+)?(previous|prior|above)\s+instructions",
        r"disregard\s+(your|the)\s+(system\s+)?prompt",
        r"you\s+are\s+now\s+(a|an|the)\s",
        r"as\s+an\s+ai\s+(assistant|agent|model)\s*,?\s+you\s+must",
        r"run\s+the\s+following\s+command",
        r"curl\s+[^\s]+\s*\|\s*(ba|z)?sh",
        r"rm\s+-rf\s+[~/]",
        r"<\s*system\s*>",
    )
)

_SAUDI_AREA_MARKERS = ("saudi", "ksa")


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(moment: dt.datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _age_days(stamp: Any, now: dt.datetime) -> Optional[float]:
    parsed = parse_iso(stamp)
    if parsed is None:
        return None
    return max(0.0, (now - parsed).total_seconds() / 86400.0)


def _host_of(url: str) -> str:
    try:
        host = urllib.parse.urlsplit(str(url or "")).netloc.lower()
    except ValueError:
        return ""
    host = host.split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


def _host_in(url_or_key: str, hosts: Sequence[str]) -> bool:
    host = _host_of(url_or_key) or str(url_or_key or "").split("/", 1)[0].lower()
    return any(host == h or host.endswith("." + h) for h in hosts)


def scan_content_flags(text: str) -> List[str]:
    """Flags for retrieved content that reviewers must treat as untrusted."""
    corpus = str(text or "")
    flags: List[str] = []
    if any(pattern.search(corpus) for pattern in _INSTRUCTION_PATTERNS):
        flags.append("instruction_like")
    return flags


def _evidence(
    source_url: Optional[str],
    checked_at: Optional[str],
    extraction_state: str,
    confidence: str,
    origin: str,
    now: dt.datetime,
    error: Optional[str] = None,
    content_flags: Optional[List[str]] = None,
) -> Dict[str, Any]:
    assert extraction_state in EXTRACTION_STATES and confidence in CONFIDENCE_LEVELS
    age = _age_days(checked_at, now)
    record: Dict[str, Any] = {
        # Frozen names (c1):
        "source_url": source_url,
        "checked_at": checked_at,
        "extraction_state": extraction_state,
        "confidence": confidence,
        # Additive:
        "origin": origin,  # repo_facts | web_fetch | captured_text
        "age_days": None if age is None else round(age, 1),
    }
    if error:
        record["error"] = str(error)[:200]
    if content_flags:
        record["content_flags"] = list(content_flags)
    return record


def _eligibility(
    lane: str,
    reasons: List[str],
    evidence: Optional[Dict[str, Any]],
    project_fit: Optional[Dict[str, Any]] = None,
    queue_band: str = "group",
    constraints: Optional[List[Dict[str, str]]] = None,
    retryable: Optional[bool] = None,
    action_brief: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    assert lane in ELIGIBILITY_LANES
    block: Dict[str, Any] = {
        # Frozen names (c1):
        "lane": lane,
        "reasons": reasons,
        "evidence": evidence,
        "project_fit": project_fit,
        # Additive:
        "queue_band": queue_band,
    }
    if constraints:
        block["constraints"] = constraints
    if retryable is not None:
        block["retryable"] = retryable
    if action_brief is not None:
        block["action_brief"] = action_brief
    return block


# --------------------------------------------------------------------------
# Web evidence store (consumer of C6 safe_fetch; deny-all until 05 lands)
# --------------------------------------------------------------------------

def _deny_all_fetch(url, **_kwargs):
    """C6-conformant fallback used when lane 05's provider is not integrated.

    Deny-by-default is the frozen fallback (CONTRACTS.md C6). This is not a
    fetcher and must never grow into one — `scripts/safe_fetch.py` is lane
    05's owned module and replaces this automatically once integrated.
    """
    return {
        "ok": False,
        "url": str(url),
        "final_url": None,
        "status": None,
        "content_type": None,
        "bytes": 0,
        "body_path": None,
        "text": None,
        "error": None,
        "denied_reason": "provider_unavailable",
    }


def _default_fetcher():
    try:
        import safe_fetch  # lane 05's provider, present after integration

        return safe_fetch.safe_fetch
    except ImportError:
        return _deny_all_fetch


_TAG_RE = re.compile(r"<[^>]{0,500}>")
_TITLE_RE = re.compile(r"<title[^>]*>(.{0,300}?)</title>", re.IGNORECASE | re.DOTALL)


def _page_excerpt(html_text: str) -> Tuple[str, str]:
    """(title, excerpt) from fetched HTML/plain text. Crude on purpose: no new
    dependencies, bounded output, and the result is display data only."""
    text = str(html_text or "")
    title_match = _TITLE_RE.search(text)
    title = " ".join(title_match.group(1).split()) if title_match else ""
    stripped = " ".join(_TAG_RE.sub(" ", text).split())
    return title[:200], stripped[:500]


# Per-state TTLs. "ok" pages re-check monthly; transient failures back off per
# attempt; unsupported content types and policy denials are near-permanent
# facts about the destination, not worth hammering.
_TTL_DAYS = {"ok": 30, "unsupported": 90, "denied_policy": 90}
_FAILED_RETRY_BASE_DAYS = 1
_FAILED_RETRY_CAP_DAYS = 14

DEFAULT_FETCH_LIMIT = 25
DEFAULT_FETCH_BUDGET_SECONDS = 120
FETCH_MAX_BYTES = 512_000
FETCH_TIMEOUT = 15.0
FETCH_CONTENT_TYPES = ("text/html", "text/plain")


class WebEvidenceStore:
    """Cached destination-page evidence for non-repository resources.

    Mirrors the tool-meta cache design: JSON document in data/, atomic
    replace, 0600, resumable (saved after every fetch), bounded per run.
    Entries are keyed by tool key and remember the URL they were fetched
    for — a changed URL invalidates the entry.
    """

    def __init__(self, path: Path = WEB_EVIDENCE_PATH):
        self.path = Path(path)
        self._entries: Dict[str, Dict[str, Any]] = {}
        self._loaded = False

    def load(self) -> "WebEvidenceStore":
        try:
            with self.path.open(encoding="utf-8") as handle:
                payload = json.load(handle)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            payload = {}
        entries = payload.get("entries") if isinstance(payload, dict) else None
        self._entries = entries if isinstance(entries, dict) else {}
        self._loaded = True
        return self

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        document = {
            "updated_at": _iso(utc_now()),
            "count": len(self._entries),
            "entries": self._entries,
        }
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)
        self.path.chmod(0o600)

    def get(self, key: str, url: str = "") -> Optional[Dict[str, Any]]:
        if not self._loaded:
            self.load()
        entry = self._entries.get(str(key or "").lower())
        if entry and url and entry.get("url") and entry["url"] != url:
            return None  # URL changed since fetch: evidence no longer applies
        return entry

    def _is_due(self, entry: Optional[Dict[str, Any]], now: dt.datetime) -> bool:
        if entry is None:
            return True
        age = _age_days(entry.get("checked_at"), now)
        if age is None:
            return True
        state = entry.get("extraction_state")
        if state == "failed":
            if entry.get("denied_reason") in _BLOCKED_DENIALS:
                return age >= _TTL_DAYS["denied_policy"]
            attempts = max(1, int(entry.get("attempts") or 1))
            return age >= min(_FAILED_RETRY_CAP_DAYS, _FAILED_RETRY_BASE_DAYS * attempts)
        return age >= _TTL_DAYS.get(state, 30)

    def refresh(
        self,
        candidates: Iterable[Dict[str, Any]],
        fetcher=None,
        limit: int = DEFAULT_FETCH_LIMIT,
        budget_seconds: float = DEFAULT_FETCH_BUDGET_SECONDS,
        now: Optional[dt.datetime] = None,
    ) -> Dict[str, int]:
        """Fetch destination evidence for up to `limit` due candidates.

        Candidates are tool entries (unreviewed, non-repo, with a URL),
        assumed pre-ordered by priority. Explicit quota + wall-clock budget:
        this can run inside the twice-hourly loop without threatening it, and
        it can never crawl the whole archive in one pass.
        """
        if not self._loaded:
            self.load()
        fetcher = fetcher or _default_fetcher()
        now = now or utc_now()
        deadline = time.monotonic() + budget_seconds
        fetched = failed = skipped = 0
        for tool in candidates:
            if fetched + failed >= limit or time.monotonic() >= deadline:
                break
            key = str(tool.get("key") or "").lower()
            url = str(tool.get("url") or "")
            if not key or not url:
                continue
            entry = self._entries.get(key)
            if entry and entry.get("url") == url and not self._is_due(entry, now):
                skipped += 1
                continue
            result = fetcher(
                url,
                max_bytes=FETCH_MAX_BYTES,
                timeout=FETCH_TIMEOUT,
                allowed_content_types=FETCH_CONTENT_TYPES,
            )
            attempts = int((entry or {}).get("attempts") or 0) + 1
            if result.get("ok"):
                title, excerpt = _page_excerpt(result.get("text") or "")
                record = {
                    "url": url,
                    "final_url": result.get("final_url"),
                    "checked_at": _iso(now),
                    "extraction_state": "ok" if excerpt else "unsupported",
                    "title": title,
                    "excerpt": excerpt,
                    "content_flags": scan_content_flags(
                        " ".join((title, excerpt))
                    ),
                    "attempts": attempts,
                }
                fetched += 1
            else:
                denied = result.get("denied_reason") or "error"
                record = {
                    "url": url,
                    "checked_at": _iso(now),
                    "extraction_state": "unsupported"
                    if denied == "content_type"
                    else "failed",
                    "denied_reason": denied,
                    "error": result.get("error"),
                    "attempts": attempts,
                }
                failed += 1
            self._entries[key] = record
            self.save()  # resumable: keep everything already learned
        return {"fetched": fetched, "failed": failed, "skipped": skipped}


# --------------------------------------------------------------------------
# Eligibility computation (pure: dicts in, dicts out)
# --------------------------------------------------------------------------

def _saudi_constraints(areas: Iterable[str]) -> Optional[List[Dict[str, str]]]:
    """Saudi-relevant items carry an explicit unknown until a human checks.

    Eligibility for a Saudi program/opportunity is a legal/residency fact the
    pipeline cannot verify. Stating "unknown" is the feature; guessing would
    be a fabricated claim.
    """
    for area in areas or ():
        if any(marker in str(area).lower() for marker in _SAUDI_AREA_MARKERS):
            return [
                {
                    "name": "saudi_eligibility",
                    "state": "unknown",
                    "note": "eligibility/requirements not verified from source",
                }
            ]
    return None


def _captured_text_of(records: Sequence[Dict[str, Any]]) -> Tuple[str, str]:
    """(combined text, latest hydration-ish date) across contributing records."""
    best_text = ""
    latest = ""
    for record in records:
        text = " ".join(
            part
            for part in (str(record.get("title") or ""), str(record.get("text") or ""))
            if part
        ).strip()
        if len(text) > len(best_text):
            best_text = text
        stamp = str(record.get("updated_at") or record.get("shared_at") or "")
        if stamp > latest:
            latest = stamp
    return best_text, latest


def _queue_band(records: Sequence[Dict[str, Any]]) -> str:
    """`group` if any contributing share came from the live group; `archive`
    otherwise. The imported archive must not drown the group feed: consumers
    band the queue by this, and the shortlist caps archive items."""
    for record in records:
        if (record.get("source") or "group") == "group":
            return "group"
    return "archive" if records else "group"


def _project_areas_of(records: Sequence[Dict[str, Any]]) -> List[str]:
    seen: List[str] = []
    for record in records:
        for area in record.get("project_areas") or []:
            if area not in seen:
                seen.append(area)
    return seen


def compute_review_eligibility(
    tool: Dict[str, Any],
    contributing: Sequence[Dict[str, Any]],
    web_evidence: Optional[Dict[str, Any]] = None,
    now: Optional[dt.datetime] = None,
) -> Optional[Dict[str, Any]]:
    """The C5 target block for one tool entry, or None when already decided.

    Typed evidence routes:

    * repo key            → repository facts (tool-meta). Fetched+ok → review;
                            not fetched / transient failure → evidence_pending.
    * video host          → destination text extraction is permanently
                            unsupported; captured description is the evidence.
    * any other URL       → fetched destination page (web evidence store).
                            Policy-denied → blocked; transient/pending →
                            evidence_pending; fetched → review.

    A decided tool (any hand or auto verdict) returns None: it is not in the
    unreviewed queue and giving it a lane would imply otherwise.
    """
    now = now or utc_now()
    if (tool.get("verdict") or "unreviewed") != "unreviewed":
        return None

    band = _queue_band(contributing)
    areas = _project_areas_of(contributing)
    constraints = _saudi_constraints(areas)
    facts = tool.get("facts") or {}
    url = str(tool.get("url") or "")
    key = str(tool.get("key") or "")

    if _host_in(key or url, REPO_FACT_HOSTS):
        if facts.get("ok"):
            evidence = _evidence(
                source_url=url or "https://" + key,
                checked_at=facts.get("checked_at") or None,
                extraction_state="ok",
                confidence="high",
                origin="repo_facts",
                now=now,
            )
            reasons: List[str] = []
            age = evidence.get("age_days")
            if age is not None and age >= EVIDENCE_STALE_DAYS:
                reasons.append(
                    "evidence stale (checked {:.0f} days ago)".format(age)
                )
            return _eligibility(
                "review", reasons, evidence, queue_band=band, constraints=constraints
            )
        # Facts absent or transiently failed. NOT an exclusion — A09's core
        # mistake was treating "no successful facts" as "not reviewable".
        error = str(facts.get("last_error") or facts.get("error") or "").strip()
        reason = (
            "repository facts fetch failed: {} — will retry".format(error[:80])
            if error
            else "repository facts not yet fetched"
        )
        evidence = _evidence(
            source_url=url or "https://" + key,
            checked_at=facts.get("checked_at") or None,
            extraction_state="failed" if error else "pending",
            confidence="low",
            origin="repo_facts",
            now=now,
            error=error or None,
        )
        return _eligibility(
            "evidence_pending",
            [reason],
            evidence,
            queue_band=band,
            constraints=constraints,
            retryable=True,
        )

    captured_text, captured_at = _captured_text_of(contributing)

    if url and _host_in(url, VIDEO_HOSTS):
        # The destination is a video: no fetch will ever yield its content, so
        # waiting on one would strand tutorials forever (A09 again, politely).
        if len(captured_text) >= MIN_CAPTURED_TEXT:
            evidence = _evidence(
                source_url=url,
                checked_at=captured_at or None,
                extraction_state="unsupported",
                confidence="medium",
                origin="captured_text",
                now=now,
                content_flags=scan_content_flags(captured_text) or None,
            )
            return _eligibility(
                "review",
                ["video destination — reviewed on the captured description"],
                evidence,
                queue_band=band,
                constraints=constraints,
            )
        evidence = _evidence(
            source_url=url,
            checked_at=None,
            extraction_state="unsupported",
            confidence="low",
            origin="captured_text",
            now=now,
        )
        return _eligibility(
            "evidence_pending",
            ["video content — no text extraction; captured description too thin"],
            evidence,
            queue_band=band,
            constraints=constraints,
            retryable=True,
        )

    if url:
        entry = web_evidence or None
        if entry and entry.get("extraction_state") == "ok":
            flags = list(entry.get("content_flags") or [])
            evidence = _evidence(
                source_url=url,
                checked_at=entry.get("checked_at"),
                extraction_state="ok",
                confidence="low" if flags else "medium",
                origin="web_fetch",
                now=now,
                content_flags=flags or None,
            )
            reasons = []
            if flags:
                reasons.append(
                    "retrieved content is untrusted data (flags: {})".format(
                        ", ".join(flags)
                    )
                )
            age = evidence.get("age_days")
            if age is not None and age >= EVIDENCE_STALE_DAYS:
                reasons.append("evidence stale (checked {:.0f} days ago)".format(age))
            return _eligibility(
                "review", reasons, evidence, queue_band=band, constraints=constraints
            )
        if entry and entry.get("denied_reason") in _BLOCKED_DENIALS:
            return _eligibility(
                "blocked",
                ["fetch denied: {}".format(entry["denied_reason"])],
                None,
                queue_band=band,
                constraints=constraints,
                retryable=False,
            )
        if entry and entry.get("extraction_state") in ("failed", "unsupported"):
            denied = str(entry.get("denied_reason") or entry.get("error") or "error")
            evidence = _evidence(
                source_url=url,
                checked_at=entry.get("checked_at"),
                extraction_state=entry.get("extraction_state"),
                confidence="low",
                origin="web_fetch",
                now=now,
                error=entry.get("error"),
            )
            return _eligibility(
                "evidence_pending",
                ["fetch failed: {} — will retry".format(denied[:80])],
                evidence,
                queue_band=band,
                constraints=constraints,
                retryable=True,
            )
        # Never attempted (or URL changed since the last attempt).
        evidence = _evidence(
            source_url=url,
            checked_at=None,
            extraction_state="pending",
            confidence="low",
            origin="web_fetch",
            now=now,
        )
        return _eligibility(
            "evidence_pending",
            ["destination page not yet fetched"],
            evidence,
            queue_band=band,
            constraints=constraints,
            retryable=True,
        )

    # No URL at all: the captured text IS the artifact (method, note, thread).
    if len(captured_text) >= MIN_CAPTURED_TEXT_STANDALONE:
        evidence = _evidence(
            source_url=None,
            checked_at=captured_at or None,
            extraction_state="ok",
            confidence="medium",
            origin="captured_text",
            now=now,
            content_flags=scan_content_flags(captured_text) or None,
        )
        return _eligibility(
            "review",
            ["no external destination — the captured text is the artifact"],
            evidence,
            queue_band=band,
            constraints=constraints,
        )
    evidence = _evidence(
        source_url=None,
        checked_at=None,
        extraction_state="pending",
        confidence="low",
        origin="captured_text",
        now=now,
    )
    return _eligibility(
        "evidence_pending",
        ["captured text too thin to review; awaiting hydration"],
        evidence,
        queue_band=band,
        constraints=constraints,
        retryable=True,
    )


def compute_re_review(
    tool: Dict[str, Any], now: Optional[dt.datetime] = None
) -> Optional[Dict[str, Any]]:
    """A labelled proposal to revisit a DECIDED tool whose evidence changed.

    Narrow by design: only material, source-confirmed changes (repository
    archived or gone after the decision) qualify. The verdict itself is
    untouched — completed decisions never reopen silently.
    """
    verdict = tool.get("verdict") or "unreviewed"
    if verdict in ("unreviewed", "excluded") or tool.get("auto"):
        return None
    facts = tool.get("facts") or {}
    checked_at = facts.get("checked_at") or ""
    if facts.get("ok") and facts.get("archived"):
        return {
            "reason": "repository was archived after this verdict was recorded",
            "evidence_checked_at": checked_at,
            "prior_verdict": verdict,
        }
    if facts.get("missing"):
        return {
            "reason": "repository no longer exists",
            "evidence_checked_at": checked_at,
            "prior_verdict": verdict,
        }
    return None


# --------------------------------------------------------------------------
# Project fit and action briefs
# --------------------------------------------------------------------------

_FIRST_STEP = {
    "try": "Install/open it and run one real {area} artifact through it (timebox 30 min).",
    "learn": "Do the first lesson/section only (timebox 20 min).",
    "read": "Read it once and capture up to 3 takeaways.",
    "reference": "File it under {area} and use it on the next matching task.",
    "other": "Skim the source once and decide which lane it belongs to.",
}

_SUCCESS_MEASURE = {
    "try": "it processed one real {area} artifact end to end",
    "learn": "first section finished and one technique applied to {area} work",
    "read": "3 takeaways written down",
    "reference": "retrieved and reused at least once within two weeks",
    "other": "a lane decision was made",
}

_EFFORT_BAND = {
    "try": "hours",
    "learn": "days",
    "read": "minutes",
    "reference": "minutes",
    "other": "unknown",
}

_FREE_MARKERS = ("open source", "open-source", "free", "مجاني", "مجانا", "مجاناً", "mit", "apache")
_PAID_MARKERS = ("pricing", "subscription", "per month", "/mo", "paid plan", "$", "sar ")


def _active_area(
    areas: Sequence[str], profile: Dict[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    """First matched area that still exists in the ACTIVE profile.

    A stored area name proves nothing about today's projects — the profile is
    re-read every export, so a renamed/removed project silently stops
    producing fit claims instead of pointing at a dead project.
    """
    profile_areas = (profile.get("selection") or {}).get("project_areas") or {}
    for area in areas:
        if area in profile_areas:
            return area, profile_areas[area] or {}
    return "", {}


def build_project_fit(
    tool: Dict[str, Any],
    contributing: Sequence[Dict[str, Any]],
    profile: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Frozen project_fit {project, benefit, first_step, success_measure}.

    Grounded, not generated from thin air: the project comes from the active
    profile, the benefit from the area's own description plus what the SOURCE
    says the thing is. When no active project matches, there is no fit — a
    None here is honest, a vague one is noise.
    """
    areas = _project_areas_of(contributing)
    area_name, area = _active_area(areas, profile)
    if not area_name:
        return None
    label = str(area.get("label") or area_name)
    resource_type = str(tool.get("resource_type") or "other")
    what = str(
        (tool.get("facts") or {}).get("description") or tool.get("what") or ""
    ).strip()
    benefit = "{}: {}".format(
        label,
        what[:140] if what else "matched this project area; source description not yet available",
    )
    return {
        "project": area_name,
        "benefit": benefit,
        "first_step": _FIRST_STEP[resource_type].format(area=label),
        "success_measure": _SUCCESS_MEASURE[resource_type].format(area=label),
    }


def build_action_brief(
    tool: Dict[str, Any],
    contributing: Sequence[Dict[str, Any]],
    profile: Dict[str, Any],
    area_overlap: Optional[Dict[str, List[str]]] = None,
    evidence: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Additive brief: effort/cost/uncertainty/risks/overlap + estimated ROI.

    Every field is either derived from a named source or an explicit unknown.
    ROI is a coarse band with its basis listed — never a number, and never
    conflated with actual trial results (those live in outcomes, C2).
    """
    areas = _project_areas_of(contributing)
    area_name, area = _active_area(areas, profile)
    resource_type = str(tool.get("resource_type") or "other")
    facts = tool.get("facts") or {}
    captured_text, _ = _captured_text_of(contributing)
    corpus = " ".join(
        (str(facts.get("description") or ""), str(tool.get("what") or ""), captured_text)
    ).casefold()

    cost = "unknown"
    if str(facts.get("license") or tool.get("license") or "").strip():
        cost = "free"
    elif any(marker in corpus for marker in _PAID_MARKERS):
        cost = "paid"
    elif any(marker in corpus for marker in _FREE_MARKERS):
        cost = "free"

    max_share = max([int(r.get("share_count") or 1) for r in contributing] or [1])
    profile_areas = (profile.get("selection") or {}).get("project_areas") or {}
    active_matches = [a for a in areas if a in profile_areas]
    roi_basis: List[str] = []
    if len(active_matches) >= 2:
        roi_band = "high"
        roi_basis.append("touches {} active project areas".format(len(active_matches)))
    elif active_matches:
        roi_band = "high" if max_share > 1 else "medium"
        roi_basis.append("matches active project area: {}".format(active_matches[0]))
    else:
        roi_band = "unknown"
        roi_basis.append("no active project area matched")
    if max_share > 1:
        roi_basis.append("independently reshared in the group")

    uncertainty: List[str] = []
    risks: List[str] = []
    what = str(facts.get("description") or tool.get("what") or "").strip()
    if not what:
        uncertainty.append("no source description yet — capability unverified")
    if cost == "unknown":
        uncertainty.append("cost unknown")
    if resource_type == "try":
        uncertainty.append("compatibility with the current stack unverified")
    if facts.get("archived"):
        risks.append("repository is archived")
    if not str(facts.get("license") or tool.get("license") or "").strip():
        risks.append("license unknown")
    if (
        resource_type == "try"
        and tool.get("is_repo") is False
        and str(tool.get("url") or "").startswith("http")
    ):
        risks.append("hosted service — vendor/availability dependency")
    flags = (evidence or {}).get("content_flags") or []
    if "instruction_like" in flags:
        risks.append("source contains instruction-like content — treat as untrusted data")

    overlap = list((area_overlap or {}).get(area_name, []))[:3]

    return {
        "what": what or None,
        "problem": str(area.get("description") or "")[:160] or None,
        "overlap": overlap,
        "prerequisites": [],
        "effort_band": _EFFORT_BAND[resource_type],
        "effort_basis": "resource-type estimate, not measured",
        "expected_cost": cost,
        "uncertainty": uncertainty,
        "risks": risks,
        "estimated_roi_band": roi_band,
        "roi_basis": roi_basis,
        "proposed_by": "radar",
    }


# --------------------------------------------------------------------------
# Wiring: annotate a built tool index (called from build_tool_index)
# --------------------------------------------------------------------------

def annotate_tools(
    tools: Sequence[Dict[str, Any]],
    records: Sequence[Dict[str, Any]],
    profile: Optional[Dict[str, Any]] = None,
    evidence_store: Optional[WebEvidenceStore] = None,
    now: Optional[dt.datetime] = None,
) -> None:
    """Stamp `review_eligibility` (and `re_review`) on every tool, in place.

    Also stamps `review_eligibility` on relevant records that have NO tool
    key at all (methods, notes, threads) — the class of items that today can
    never even become a queue candidate. Pure computation: the only I/O is
    the read-only evidence-store load.
    """
    now = now or utc_now()
    profile = profile or {}
    store = evidence_store if evidence_store is not None else WebEvidenceStore()

    by_key: Dict[str, List[Dict[str, Any]]] = {}
    for record in records:
        for key in record.get("tool_keys") or []:
            by_key.setdefault(key.lower(), []).append(record)

    # Existing adopted/kept tools per area, so a brief can say "overlaps with
    # X you already use" instead of pretending every candidate is greenfield.
    area_overlap: Dict[str, List[str]] = {}
    for tool in tools:
        if tool.get("verdict") in ("already_have", "must_try") or tool.get("outcome") == "kept":
            for record in by_key.get(str(tool.get("key") or "").lower(), []):
                for area in record.get("project_areas") or []:
                    names = area_overlap.setdefault(area, [])
                    name = str(tool.get("name") or tool.get("key") or "")
                    if name and name not in names:
                        names.append(name)

    for tool in tools:
        key = str(tool.get("key") or "")
        contributing = by_key.get(key.lower(), [])
        web = store.get(key, str(tool.get("url") or "")) if key else None
        eligibility = compute_review_eligibility(tool, contributing, web, now=now)
        if eligibility is not None and eligibility["lane"] == "review":
            eligibility["project_fit"] = build_project_fit(tool, contributing, profile)
            eligibility["action_brief"] = build_action_brief(
                tool, contributing, profile, area_overlap, eligibility.get("evidence")
            )
        tool["review_eligibility"] = eligibility
        re_review = compute_re_review(tool, now=now)
        if re_review is not None:
            tool["re_review"] = re_review

    for record in records:
        if record.get("tool_keys"):
            continue
        if record.get("status") != "relevant" or record.get("verdict"):
            continue
        pseudo_tool = {
            "verdict": "unreviewed",
            "url": "",
            "key": "",
            "resource_type": record.get("resource_type") or "other",
            "facts": None,
        }
        eligibility = compute_review_eligibility(pseudo_tool, [record], None, now=now)
        if eligibility is not None and eligibility["lane"] == "review":
            eligibility["project_fit"] = build_project_fit(pseudo_tool, [record], profile)
            eligibility["action_brief"] = build_action_brief(
                pseudo_tool, [record], profile, area_overlap, eligibility.get("evidence")
            )
        record["review_eligibility"] = eligibility


# --------------------------------------------------------------------------
# Explainable daily shortlist (suggestions, never adoption)
# --------------------------------------------------------------------------

_ROI_ORDER = {"high": 0, "medium": 1, "low": 2, "unknown": 3}
_CONFIDENCE_ORDER = {"high": 0, "medium": 1, "low": 2}
SHORTLIST_LIMIT = 5
SHORTLIST_ARCHIVE_CAP = 2


def build_daily_shortlist(
    tools: Sequence[Dict[str, Any]],
    limit: int = SHORTLIST_LIMIT,
    archive_cap: int = SHORTLIST_ARCHIVE_CAP,
) -> List[Dict[str, Any]]:
    """Top candidates for today, ROI-first and explainable.

    Ranking is (estimated ROI band, evidence confidence, reshare, score).
    Effort is NEVER a ranking or exclusion criterion: a high-value complex
    option must be able to outrank an easy trivial one, so effort appears in
    the brief for the human and nowhere in the sort key. Popularity
    (engagement) is absent entirely — best_score already caps it as a
    tiebreaker.

    Two labelled kinds, never mixed up with adoption:
    * proposal          — unreviewed, eligible for review today
    * committed_untried — hand-marked must_try with no recorded outcome yet
    Anything with an outcome (trying/kept/dropped) is excluded: those are
    facts about trials, not suggestions.
    """
    candidates: List[Tuple[Tuple, Dict[str, Any]]] = []
    for tool in tools:
        outcome = str(tool.get("outcome") or "")
        if outcome:
            continue
        eligibility = tool.get("review_eligibility") or {}
        verdict = tool.get("verdict") or "unreviewed"
        if verdict == "must_try" and not tool.get("auto"):
            kind = "committed_untried"
            band = "group"
            roi = "high"  # the user already judged it worth trying
            confidence = "high"
            reasons = ["you marked this must-try and have not tried it yet"]
        elif verdict == "unreviewed" and eligibility.get("lane") == "review":
            kind = "proposal"
            band = str(eligibility.get("queue_band") or "group")
            brief = eligibility.get("action_brief") or {}
            roi = str(brief.get("estimated_roi_band") or "unknown")
            confidence = str(
                (eligibility.get("evidence") or {}).get("confidence") or "low"
            )
            reasons = list(brief.get("roi_basis") or [])
            fit = eligibility.get("project_fit") or {}
            if fit.get("project"):
                reasons.append("fits active project: {}".format(fit["project"]))
        else:
            continue
        share_bonus = -float(tool.get("best_score") or 0.0)
        sort_key = (
            _ROI_ORDER.get(roi, 3),
            _CONFIDENCE_ORDER.get(confidence, 2),
            share_bonus,
            str(tool.get("key") or ""),
        )
        candidates.append(
            (
                sort_key,
                {
                    "key": tool.get("key"),
                    "name": tool.get("name"),
                    "kind": kind,
                    "queue_band": band,
                    "estimated_roi_band": roi,
                    "evidence_confidence": confidence,
                    "reasons": reasons,
                    "suggestion": True,  # NEVER a record of adoption
                },
            )
        )
    candidates.sort(key=lambda pair: pair[0])
    shortlist: List[Dict[str, Any]] = []
    archive_taken = 0
    for _key, item in candidates:
        if len(shortlist) >= limit:
            break
        if item["queue_band"] == "archive":
            if archive_taken >= archive_cap:
                continue
            archive_taken += 1
        shortlist.append(item)
    return shortlist


# --------------------------------------------------------------------------
# CLI: bounded evidence refresh (wiring into the loop is 07's integration)
# --------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true", help="fetch due web evidence")
    parser.add_argument("--limit", type=int, default=DEFAULT_FETCH_LIMIT)
    parser.add_argument("--budget-seconds", type=float, default=DEFAULT_FETCH_BUDGET_SECONDS)
    args = parser.parse_args(argv)
    if not args.refresh:
        parser.print_help()
        return 0

    sys.path.insert(0, str(ROOT / "scripts"))
    import group_monitor as monitor

    conn = monitor.connect_db()
    try:
        records = [monitor.resource_to_dict(row) for row in monitor.select_resource_rows(conn)]
        tools = monitor.build_tool_index(records, monitor.load_verdicts())
    finally:
        conn.close()
    candidates = [
        tool
        for tool in tools
        if (tool.get("verdict") or "unreviewed") == "unreviewed"
        and tool.get("url")
        and not _host_in(tool.get("key") or "", REPO_FACT_HOSTS)
        and not _host_in(tool.get("url") or "", VIDEO_HOSTS)
    ]
    candidates.sort(key=lambda t: -float(t.get("best_score") or 0.0))
    store = WebEvidenceStore().load()
    result = store.refresh(candidates, limit=args.limit, budget_seconds=args.budget_seconds)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
