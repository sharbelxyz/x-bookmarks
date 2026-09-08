#!/usr/bin/env python3
"""Deterministic resource typing and pick scoring for the group radar.

Everything in this module is pure and side-effect free: no I/O, no database,
no network. It runs on the same Python 3.9 interpreter that cron uses.

Two questions are answered for every resource:

* ``classify_resource_type`` – is this a piece of *software to try* (``tool``),
  a *practice to learn* (``practice``), *research or news* to be aware of
  (``research``), or none of those (``other``)? The answer comes from weighted
  English, Arabic, and Chinese keyword signals, third-person product-description
  verbs, and URL-host signals. It is a lane assignment for the dashboard, not a
  relevance decision; relevance stays with the rules + semantic review in
  ``group_monitor``.
* ``compute_pick_score`` – how strongly should a relevant resource surface in
  "Top picks"? The score rewards group reshares, project fit, concrete software
  links, recency, and (capped) engagement, in that order of influence. Virality
  alone must not win: a 78k-like meme should lose to a reshared Hermes skill.
"""

from __future__ import annotations

import datetime as dt
import math
import re
import urllib.parse
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


# Lanes describe the ACTION a resource demands, not the format it arrives in.
# The earlier taxonomy (tool / practice / research) mixed those: "practice" held
# techniques you apply, news you read, and courses you follow all at once, so a
# thing to TRY and a thing to READ were reported as the same kind of item. They
# are not interchangeable and must never be presented as each other.
RESOURCE_TYPES = ("try", "learn", "read", "reference", "other")

TYPE_LABELS = {
    "try": "Try it",
    "learn": "Learn it",
    "read": "Read it",
    "reference": "Keep for reference",
    "other": "Uncategorized",
}

TYPE_VERBS = {
    "try": "install, run or apply it",
    "learn": "follow it end to end",
    "read": "read it once",
    "reference": "look it up when needed",
    "other": "unclear",
}

# Tie order: the more a lane demands of you, the earlier it wins, so an item
# that is genuinely runnable is never filed as something to skim.
TYPE_PRIORITY = ("try", "learn", "read", "reference")

# Which verdicts each lane can legitimately carry. You cannot "try" a paper and
# you do not "read" a CLI: a verdict that crosses this is a category error, and
# the endpoint refuses it rather than quietly storing something meaningless.
VERDICT_FOR_TYPE = {
    "try": {"must_try", "already_have", "excluded"},
    "learn": {"must_read", "already_have", "excluded"},
    "read": {"must_read", "already_have", "excluded"},
    "reference": {"must_read", "must_try", "already_have", "excluded"},
    "other": {"must_try", "must_read", "already_have", "excluded"},
}

MINIMUM_TYPE_SCORE = 2

# (term, weight). ASCII terms match on word boundaries; non-ASCII terms match
# as substrings (Arabic and Chinese morphology make boundaries unreliable).
TOOL_TERMS: Sequence[Tuple[str, int]] = (
    ("github", 3),
    ("open source", 3),
    ("open-source", 3),
    ("opensource", 3),
    ("oss", 2),
    ("pip install", 3),
    ("npm install", 3),
    ("brew install", 3),
    ("npx", 2),
    ("mcp server", 3),
    ("browser extension", 3),
    ("chrome extension", 3),
    ("vscode extension", 3),
    ("self-hosted", 3),
    ("self hosted", 3),
    ("free tool", 3),
    ("alternative to", 2),
    ("alternative", 1),
    ("repo", 2),
    ("repository", 2),
    ("library", 2),
    ("sdk", 2),
    ("cli", 2),
    ("plugin", 2),
    ("extension", 2),
    ("mcp", 2),
    ("npm", 2),
    ("docker", 2),
    ("tool", 2),
    ("toolkit", 2),
    ("launch", 2),
    ("launched", 2),
    ("launches", 2),
    ("introducing", 2),
    ("shipped", 2),
    ("release", 2),
    ("released", 2),
    ("now available", 2),
    ("stars", 2),
    ("desktop app", 2),
    ("mac app", 2),
    ("macos app", 2),
    ("ios app", 2),
    ("web app", 2),
    ("components", 2),
    ("ui kit", 2),
    ("starter", 2),
    ("boilerplate", 2),
    ("generator", 2),
    ("template", 2),
    ("engine", 1),
    ("app", 1),
    ("api", 1),
    ("framework", 1),
    ("platform", 1),
    ("beta", 1),
    ("bot", 1),
    ("أداة", 3),
    ("اداة", 3),
    ("مفتوح المصدر", 3),
    ("مفتوحة المصدر", 3),
    ("تطبيق", 2),
    ("برنامج", 2),
    ("موقع", 2),
    ("مكتبة", 2),
    ("إضافة", 2),
    ("اضافة", 2),
    ("نموذج", 2),
    ("مشروع", 1),
    ("أطلقت", 2),
    ("اطلقت", 2),
    ("إطلاق", 2),
    ("اطلاق", 2),
    ("مجاني", 1),
    ("مجانا", 1),
    ("مجاناً", 1),
    ("开源", 3),
    ("工具", 3),
    ("项目", 2),
    ("插件", 2),
    ("模型", 1),
    ("prompt", 2), ("prompts", 2), ("system prompt", 3), ("prompt template", 3),
    ("workflow", 2), ("skill", 2), ("agent skill", 3), ("recipe", 2),
    ("playbook", 2), ("checklist", 2), ("template", 2), ("boilerplate", 2),
    ("try it", 3), ("stop asking", 2), ("stop doing", 2), ("instead of", 1),
    ("برومبت", 3), ("برومت", 3), ("قالب", 2),
    ("herramienta", 3),
    ("código abierto", 3),
    ("extensión", 3),
    ("aplicación", 2),
    ("biblioteca", 2),
    ("gratis", 1),
)

# Third-person product descriptions ("X monitors every…", "Converts Python to…")
# are how curators describe repos. One verb hit is worth a strong term.
TOOL_VERB_RE = re.compile(
    r"(?<![a-z0-9_])("
    r"monitors|generates|converts|automates|builds|creates|manages|tracks|syncs|"
    r"scans|detects|extracts|transcribes|renders|deploys|orchestrates|indexes|"
    r"summarizes|summarises|translates|schedules|records|captures|streams|"
    r"clones|designs|trains|organizes|organises|analyzes|analyses|transforms|"
    r"compiles|searches|downloads|encrypts|visualizes|visualises|controls|"
    r"connects|fetches|traces|identifies|removes|replaces|can now|"
    r"lets you|allows you|helps you|turns [^.]{1,40} into|runs locally|runs on|"
    r"works with|supports|integrates with|plugs into|drop-in|"
    r"convierte|permite|extensi[oó]n que"
    r")(?![a-z0-9_])"
)
TOOL_VERB_WEIGHT = 5

# LEARN: time-boxed material you work through end to end. A course is not an
# article — it costs hours, not minutes — so it gets its own lane.
LEARN_TERMS: Sequence[Tuple[str, int]] = (
    ("course", 4), ("crash course", 4), ("masterclass", 4), ("bootcamp", 4),
    ("curriculum", 4), ("roadmap", 3), ("learning path", 4),
    ("full guide", 3), ("complete guide", 3), ("from scratch", 3),
    ("step by step", 3), ("step-by-step", 3), ("walkthrough", 3),
    ("tutorial", 3), ("series", 2), ("lesson", 2), ("lessons", 2),
    ("beginner", 2), ("beginners", 2), ("learn how", 3), ("teach you", 3),
    ("دورة", 4), ("كورس", 4), ("مسار", 3), ("منهج", 3), ("تعلم", 3),
    ("شرح", 3), ("دليل", 3), ("خطوات", 2), ("من الصفر", 4), ("سلسلة", 2),
    ("教程", 4), ("课程", 4), ("指南", 3),
    ("curso", 4), ("guía", 3),
)

# READ: consume once to understand. News, analysis, papers, opinion, threads.
READ_TERMS: Sequence[Tuple[str, int]] = (
    ("paper", 4), ("arxiv", 4), ("study", 3), ("research shows", 4),
    ("report", 3), ("analysis", 3), ("explains", 3), ("explained", 3),
    ("why ", 2), ("what happens", 2), ("breakdown", 3), ("deep dive", 3),
    ("thread", 2), ("🧵", 2), ("essay", 3), ("article", 3), ("blog post", 3),
    ("lessons learned", 3), ("mistakes", 2), ("i learned", 2),
    ("announces", 3), ("announced", 3), ("just dropped", 2), ("breaking", 3),
    ("raises", 3), ("funding", 3), ("valuation", 3), ("acquires", 3),
    ("benchmark", 3), ("state of", 3), ("survey", 3), ("interview", 3),
    ("مقال", 4), ("دراسة", 3), ("تقرير", 3), ("بحث", 3), ("تحليل", 3),
    ("أعلنت", 3), ("اعلنت", 3), ("عاجل", 3), ("خبر", 3), ("أخبار", 3),
    ("论文", 4), ("研究", 3), ("发布", 2), ("宣布", 3),
)

# REFERENCE: you do not read it through, you look things up in it.
REFERENCE_TERMS: Sequence[Tuple[str, int]] = (
    ("awesome", 4), ("awesome-", 4), ("curated list", 4), ("curated", 3),
    ("collection of", 3), ("cheat sheet", 4), ("cheatsheet", 4),
    ("directory of", 3), ("list of", 3), ("resources", 2), ("catalog", 3),
    ("compendium", 3), ("index of", 3), ("every ", 1),
    ("قائمة", 3), ("مجموعة", 3), ("مرجع", 4),
    ("合集", 3), ("清单", 3),
)

RESEARCH_TERMS: Sequence[Tuple[str, int]] = (
    ("paper", 3),
    ("arxiv", 3),
    ("benchmark", 3),
    ("announces", 3),
    ("announced", 3),
    ("announcement", 3),
    ("just announced", 3),
    ("breaking", 3),
    ("acquires", 3),
    ("acquired", 3),
    ("acquisition", 3),
    ("study", 2),
    ("research", 2),
    ("report", 2),
    ("news", 2),
    ("raises", 2),
    ("raised", 2),
    ("funding", 2),
    ("valuation", 2),
    ("valued at", 2),
    ("billion", 1),
    ("leaked", 2),
    ("rumor", 2),
    ("officially", 2),
    ("survey", 2),
    ("state of", 2),
    ("ceo", 1),
    ("confirmed", 1),
    ("أعلنت", 3),
    ("اعلنت", 3),
    ("عاجل", 3),
    ("استحوذت", 3),
    ("بحث", 2),
    ("دراسة", 2),
    ("تقرير", 2),
    ("إعلان", 2),
    ("اعلان", 2),
    ("خبر", 2),
    ("أخبار", 2),
    ("اخبار", 2),
    ("تمويل", 2),
    ("论文", 3),
    ("研究", 2),
    ("发布", 2),
    ("宣布", 3),
)

TOOL_HOSTS = {
    "github.com": 3,
    "gitlab.com": 3,
    "huggingface.co": 3,
    "pypi.org": 3,
    "npmjs.com": 3,
    "producthunt.com": 3,
    "apps.apple.com": 3,
    "play.google.com": 3,
    "chromewebstore.google.com": 3,
    "chrome.google.com": 3,
    "marketplace.visualstudio.com": 3,
    "ollama.com": 3,
    "replicate.com": 3,
    "hub.docker.com": 3,
    "vercel.app": 1,
    "netlify.app": 1,
}

RESEARCH_HOSTS = {
    "arxiv.org": 3,
    "openreview.net": 3,
    "paperswithcode.com": 3,
    "semanticscholar.org": 3,
    "techcrunch.com": 3,
    "theverge.com": 3,
    "reuters.com": 3,
    "bloomberg.com": 3,
    "theinformation.com": 3,
    "cnbc.com": 3,
    "bbc.com": 3,
    "bbc.co.uk": 3,
}

PRACTICE_HOSTS = {
    "youtube.com": 2,
    "youtu.be": 2,
    "medium.com": 1,
    "substack.com": 1,
    "dev.to": 1,
    "notion.site": 1,
}

REPO_HOSTS = {"github.com", "gitlab.com", "huggingface.co"}

_URL_RE = re.compile(r"https?://[^\s\"<>]+", re.IGNORECASE)
_SELF_HOSTS = {"x.com", "twitter.com", "t.co", "mobile.twitter.com"}


def _normalize_host(url: str) -> str:
    try:
        host = urllib.parse.urlsplit(str(url or "")).netloc.lower()
    except ValueError:
        return ""
    host = host.split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host


def _host_matches(host: str, table: Dict[str, int]) -> int:
    if not host:
        return 0
    best = 0
    for candidate, weight in table.items():
        if host == candidate or host.endswith("." + candidate):
            best = max(best, weight)
    return best


# Classification runs a couple of hundred term tests per resource, so rebuilding
# the pattern string each time dominated export time on a large ledger. Compile
# once per term and reuse.
_TERM_PATTERNS: Dict[str, Any] = {}


def _term_pattern(term: str):
    pattern = _TERM_PATTERNS.get(term)
    if pattern is None:
        pattern = re.compile(r"(?<![a-z0-9_]){}(?![a-z0-9_])".format(re.escape(term)))
        _TERM_PATTERNS[term] = pattern
    return pattern


def _term_present(corpus: str, term: str) -> bool:
    term = term.casefold().strip()
    if not term:
        return False
    if term.isascii():
        return _term_pattern(term).search(corpus) is not None
    return term in corpus


def external_urls_from_text(text: str) -> List[str]:
    """Return non-X http(s) URLs found in free text, in order, without dupes."""
    found: List[str] = []
    seen = set()
    for raw in _URL_RE.findall(str(text or "")):
        cleaned = raw.rstrip(".,;:!?)]}'\"")
        host = _normalize_host(cleaned)
        # Titles are truncated at 240 chars, so "https://t" or "https://t.c" can
        # appear; a real host has a dot and is not a prefix of an X short link.
        if not host or "." not in host or host in _SELF_HOSTS:
            continue
        if any(self_host.startswith(host) for self_host in _SELF_HOSTS):
            continue
        if cleaned not in seen:
            found.append(cleaned)
            seen.add(cleaned)
    return found


def is_repo_url(url: str) -> bool:
    return _normalize_host(url) in REPO_HOSTS


# Links are often pasted out of Markdown, so a path segment can arrive as
# "superpowers](https:" or "repo.git". Keep only the leading run of characters
# that are actually legal in a repo/package name.
_SEGMENT_HEAD = re.compile(r"^[A-Za-z0-9._-]+")


def _clean_segment(segment: str) -> str:
    match = _SEGMENT_HEAD.match(str(segment or ""))
    if not match:
        return ""
    return re.sub(r"\.git$", "", match.group(0)).rstrip(".")


def tool_key(url: str) -> str:
    """Stable identity for a linked tool, shared by every post that links it.

    ``https://github.com/microsoft/markitdown/tree/main`` and
    ``https://github.com/microsoft/markitdown.git]`` both collapse to
    ``github.com/microsoft/markitdown``, so one verdict covers every mention.
    Returns "" for X links and anything without a usable host.
    """
    host = _normalize_host(url)
    if not host or "." not in host or host in _SELF_HOSTS:
        return ""
    try:
        path = urllib.parse.urlsplit(str(url)).path
    except ValueError:
        return ""
    segments = [_clean_segment(segment) for segment in path.split("/") if segment][:2]
    segments = [segment for segment in segments if segment]
    return "/".join([host] + segments) if segments else host


def short_link_label(url: str, limit: int = 42) -> str:
    """'github.com/owner/repo' style label for a chip; empty for X links."""
    host = _normalize_host(url)
    if not host or host in _SELF_HOSTS:
        return ""
    try:
        path = urllib.parse.urlsplit(str(url)).path.rstrip("/")
    except ValueError:
        path = ""
    segments = [_clean_segment(segment) for segment in path.split("/") if segment]
    segments = [segment for segment in segments if segment]
    label = host
    if segments:
        label += "/" + "/".join(segments[:2])
    if len(label) > limit:
        label = label[: limit - 1] + "…"
    return label


def classify_resource_type(
    text: str,
    urls: Iterable[str] = (),
    author: str = "",
) -> Dict[str, Any]:
    """Assign the lane by the ACTION the resource demands.

    ``try`` install/run/apply · ``learn`` follow end to end · ``read`` consume
    once · ``reference`` look up later. The distinction is the point: a
    technique you apply and an article about it are different commitments, and
    reporting one as the other wastes the reader's time in both directions.

    Returns ``{"type", "signals", "scores"}``. Signals are short, human-readable
    justifications so a lane assignment can always be argued with.
    """
    corpus = " ".join(part for part in (str(text or ""), str(author or "")) if part).casefold()
    scores = {"try": 0, "learn": 0, "read": 0, "reference": 0}
    hits: Dict[str, List[str]] = {"try": [], "learn": [], "read": [], "reference": []}

    for lane, terms in (
        ("try", TOOL_TERMS),
        ("learn", LEARN_TERMS),
        ("read", READ_TERMS),
        ("reference", REFERENCE_TERMS),
    ):
        for term, weight in terms:
            if _term_present(corpus, term):
                scores[lane] += weight
                hits[lane].append(term)

    verb = TOOL_VERB_RE.search(corpus)
    if verb:
        scores["try"] += TOOL_VERB_WEIGHT
        hits["try"].append(verb.group(1))

    # A package or repository link is near-decisive: you can install the thing.
    # Without this, a tool whose blurb happens to contain "explains" or
    # "playlist" gets filed as something to read or follow.
    hosts_seen = set()
    for url in urls or ():
        host = _normalize_host(str(url or ""))
        if not host or host in hosts_seen:
            continue
        hosts_seen.add(host)
        for lane, table in (
            ("try", TOOL_HOSTS),
            ("read", RESEARCH_HOSTS),
            ("learn", PRACTICE_HOSTS),
        ):
            weight = _host_matches(host, table)
            if weight:
                scores[lane] += weight
                hits[lane].append(host)
        # A YouTube *playlist* is a course; a single video is usually not.
        if "youtube.com" in host and "list=" in str(url):
            scores["learn"] += 3
            hits["learn"].append("playlist")

    best_lane = "other"
    best_score = 0
    for lane in TYPE_PRIORITY:
        if scores[lane] > best_score:
            best_lane = lane
            best_score = scores[lane]
    if best_score < MINIMUM_TYPE_SCORE:
        best_lane = "other"

    signals: List[str] = []
    if best_lane != "other":
        signals = list(dict.fromkeys(hits[best_lane]))[:4]
    return {"type": best_lane, "signals": signals, "scores": scores}


def verdict_fits_type(verdict: str, resource_type: str) -> bool:
    """Whether this verdict can honestly apply to this kind of resource."""
    allowed = VERDICT_FOR_TYPE.get(str(resource_type or "other"), set())
    return str(verdict or "") in allowed


def parse_iso(value: Any) -> Optional[dt.datetime]:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


LIKE_CAP = 20000
RETWEET_CAP = 5000
RECENCY_HALF_LIFE_DAYS = 7.0

# Scoring weights, v2. Measured against the real ledger, v1 behaved the opposite
# of its intent: engagement contributed 50% of the average score while reshare
# contributed 1.5%, because reshare fires on 3% of items and engagement on 100%
# with a wide range. Intent is now enforced by *capping* the weak signal rather
# than merely giving it a small coefficient, and by feeding in evidence the
# pipeline already collects but never used: repository health and your verdicts.
FIT_PER_AREA = 3.0
FIT_AI_BONUS = 1.0
RESHARE_PER_SHARE = 4.0
RESHARE_PER_MEMBER = 3.0
REPO_LINK_BONUS = 3.0
ENGAGEMENT_CAP = 2.0            # a tiebreaker, never a driver
HEALTH_CAP = 4.0
STALE_HEALTH_PENALTY = 3.0
STALE_AFTER_DAYS = 365
# Type bonuses. "research" is reading material; v1 gave it 0.5, which actively
# buried the thing the user asked to keep up with.
TYPE_BONUS = {"try": 3.0, "learn": 2.5, "read": 2.0, "reference": 1.5, "other": 0.0}
# A judgement already made must outrank anything the arithmetic can say.
VERDICT_BONUS = {"must_try": 6.0, "must_read": 5.0, "already_have": -4.0, "excluded": -12.0}


def compute_pick_score(
    record: Dict[str, Any],
    now: Optional[dt.datetime] = None,
    verdict: str = "",
    facts: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Score a relevant resource for the "Top picks" surface.

    Ordered by how much each term can move the result:

    * **verdict** - a judgement you already made beats anything computed. An
      excluded tool must never resurface near the top, which v1 allowed: the
      hand-excluded openGym fitness tracker ranked #2 in the whole ledger.
    * **fit** - how many of your active project areas it touches.
    * **repository health** - stars and last push, from the enrichment cache.
      Real evidence that a thing is alive and used, and a far better quality
      proxy than tweet likes. v1 fetched all of this and then ignored it.
    * **reshare** - another group member independently vouching. Rare (3% of
      items) but decisive when it happens.
    * **actionability** - software, practice, or reading.
    * **recency**, then **engagement**, which is hard-capped so popularity can
      break a tie but can never create one. In v1 engagement was 50% of the
      average score despite being intended as the weakest term; a small
      coefficient was not enough, because it was the only term that always fired.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    facts = facts or {}
    likes = min(LIKE_CAP, _as_int(record.get("likes")))
    retweets = min(RETWEET_CAP, _as_int(record.get("retweets")))
    share_count = max(1, _as_int(record.get("share_count")) or 1)
    sharer_count = max(1, _as_int(record.get("sharer_count")) or 1)
    areas = [str(area) for area in (record.get("project_areas") or [])]
    project_areas = [area for area in areas if area != "ai"]
    resource_type = str(record.get("resource_type") or "other")
    urls = [str(url) for url in (record.get("external_urls") or [])]

    fit = FIT_PER_AREA * min(3, len(project_areas)) + (FIT_AI_BONUS if "ai" in areas else 0.0)
    reshare = RESHARE_PER_SHARE * (share_count - 1) + RESHARE_PER_MEMBER * (sharer_count - 1)
    type_bonus = TYPE_BONUS.get(resource_type, 0.0)
    repo_bonus = REPO_LINK_BONUS if any(is_repo_url(url) for url in urls) else 0.0
    engagement = min(
        ENGAGEMENT_CAP, 0.5 * math.log10(1 + likes) + 0.3 * math.log10(1 + retweets)
    )
    verdict_bonus = VERDICT_BONUS.get(str(verdict or ""), 0.0)

    health = 0.0
    if facts.get("ok"):
        stars = _as_int(facts.get("stars"))
        health = min(HEALTH_CAP, max(0.0, math.log10(1 + stars) - 1.0))
        if facts.get("archived"):
            health -= STALE_HEALTH_PENALTY
        pushed = parse_iso(facts.get("pushed_at"))
        if pushed is not None and (now - pushed).days >= STALE_AFTER_DAYS:
            health -= STALE_HEALTH_PENALTY

    shared = parse_iso(record.get("shared_at")) or parse_iso(record.get("first_seen_at"))
    if shared is None:
        recency = 0.0
        age_days = None
    else:
        age_days = max(0.0, (now - shared).total_seconds() / 86400.0)
        recency = 4.0 * math.exp(-age_days / RECENCY_HALF_LIFE_DAYS)

    parts = {
        "verdict": round(verdict_bonus, 2),
        "fit": round(fit, 2),
        "health": round(health, 2),
        "reshare": round(reshare, 2),
        "type": round(type_bonus, 2),
        "repo": round(repo_bonus, 2),
        "recency": round(recency, 2),
        "engagement": round(engagement, 2),
    }
    return {
        "score": round(sum(parts.values()), 2),
        "parts": parts,
        "age_days": None if age_days is None else round(age_days, 1),
    }
