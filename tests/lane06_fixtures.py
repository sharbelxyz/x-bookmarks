"""Synthetic dashboard payload fixtures for lane 06 (run-20260906-2000).

Everything here is synthetic. No live usernames, tweet IDs, group content or
credentials. Shapes follow the frozen contracts:

* resources — C1 `resource_to_dict` projection (all 43 required keys);
* tools — C5 `build_tool_index` frozen keys, plus the ADDITIVE
  `review_eligibility` target block (A09) on a subset, so the UI is exercised
  both with and without the provider-04 extension;
* status — C4 status-snapshot frozen fields;
* health — C4 frozen envelope plus the additive stages block (A03 target).

The payload deliberately reproduces the audited defects' inputs: very long
repository labels, Arabic and mixed-direction text, a 14-row must-try
shortlist, media rows, and non-repository unreviewed tools that the current
queue predicate strands (A09).
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, Dict, List, Optional

UTC = dt.timezone.utc

# A fixed "now" so measurements are reproducible run to run.
NOW = dt.datetime(2026, 9, 6, 18, 0, 0, tzinfo=UTC)

LONG_REPO_KEY = (
    "github.com/very-long-organization-name-for-testing/"
    "extremely-long-repository-name-that-previously-forced-horizontal-overflow"
)
ARABIC_TITLE = "أداة مفتوحة المصدر لأتمتة متاجر نون وسلة مع دعم كامل للتقارير العربية والتسعير الديناميكي"
ARABIC_TEXT = (
    "جرّب هذه الأداة github.com/mithal/noon-automation فهي تدعم RTL بالكامل — "
    "التثبيت: pip install noon-automation ثم شغّل الأمر من الطرفية"
)
MIXED_URL = "https://example.com/مقالات/agentic-workflows-دليل-عملي?utm=0"

PROJECT_AREAS = {
    "ai": "AI & agents",
    "noon": "Noon seller ops",
    "salla": "Salla apps",
    "lms": "Documents & LMS",
    "creative": "Creative & motion",
    "arabic": "Saudi/Arabic market",
}

TYPE_LABELS = {
    "try": "Try it",
    "learn": "Learn it",
    "read": "Read it",
    "reference": "Keep for reference",
    "other": "Uncategorized",
}


def _iso(moment: dt.datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _ago(**kwargs: float) -> str:
    return _iso(NOW - dt.timedelta(**kwargs))


def make_resource(index: int, **overrides: Any) -> Dict[str, Any]:
    """One C1-shaped record with every required key present."""
    rid = overrides.get("resource_id", "tweet:1000000000000000{:03d}".format(index))
    base: Dict[str, Any] = {
        "resource_id": rid,
        "source": "group",
        "kind": "tweet",
        "url": "https://x.com/i/status/1000000000000000{:03d}".format(index),
        "tweet_id": "1000000000000000{:03d}".format(index),
        "message_id": "3000000000000000{:03d}".format(index),
        "sender_id": "920000000000000001",
        "sender_username": "member1",
        "sender_display_name": "Member One",
        "sender_avatar_url": "",
        "sender_is_owner": False,
        "shared_at": _ago(hours=index),
        "share_count": 1,
        "sharer_count": 1,
        "sharers": ["member1"],
        "sharer_ids": ["920000000000000001"],
        "author": "toolmaker",
        "title": "Synthetic resource {:03d}".format(index),
        "text": "Synthetic body text for resource {:03d}".format(index),
        "status": "relevant",
        "score": 5,
        "project_areas": ["ai"],
        "reasons": ["AI tool for agents"],
        "decision_source": "rules",
        "hydration_attempts": 1,
        "last_error": None,
        "notified_at": None,
        "media_urls": [],
        "first_seen_at": _ago(hours=index, minutes=1),
        "updated_at": _ago(hours=max(0, index - 1)),
        "likes": 10 + index,
        "retweets": 2,
        "replies": 1,
        "tweet_created_at": _ago(hours=index, minutes=5),
        "quoted_text": "",
        "external_urls": [],
        "external_label": "",
        "tool_keys": [],
        "verdict": None,
        "resource_type": "try",
        "type_signals": ["github"],
        "pick_score": 5.0,
        "pick_parts": {"fit": 3, "engagement": 0.5, "recency": 1.0},
    }
    base.update(overrides)
    return base


def make_tool(key: str, **overrides: Any) -> Dict[str, Any]:
    """One C5-shaped tool entry with every frozen key present."""
    base: Dict[str, Any] = {
        "key": key,
        "name": key.rsplit("/", 1)[-1],
        "url": "https://" + key,
        "label": key,
        "is_repo": key.split("/", 1)[0] in ("github.com", "gitlab.com", "huggingface.co"),
        "verdict": "unreviewed",
        "rank": None,
        "lane": "",
        "what": "",
        "why": "",
        "first_step": "",
        "reason_code": "",
        "stars": None,
        "license": "",
        "last_push": "",
        "mentions": 1,
        "resource_ids": [],
        "best_score": 3.0,
        "latest_share": _ago(hours=6),
        "auto": False,
        "facts": None,
        "meta_loaded": True,
        "outcome": "",
        "outcome_note": "",
        "outcome_at": "",
        "resource_type": "try",
    }
    base.update(overrides)
    return base


def repo_facts(stars: int = 1200, description: str = "Synthetic description") -> Dict[str, Any]:
    return {
        "stars": stars,
        "pushed_at": "2026-08-30",
        "archived": False,
        "license": "MIT",
        "language": "Python",
        "description": description,
        "checked_at": "2026-09-05",
        "ok": True,
    }


def build_resources(media_base: str = "") -> List[Dict[str, Any]]:
    resources: List[Dict[str, Any]] = []

    # 1) Top pick with the pathologically long repo label (A11 reproduction).
    resources.append(make_resource(
        1,
        title="Agent toolchain announcement with a very long repository link inside",
        text="Ship agents faster with " + LONG_REPO_KEY,
        external_urls=["https://" + LONG_REPO_KEY],
        external_label=LONG_REPO_KEY,
        tool_keys=[LONG_REPO_KEY],
        pick_score=9.4,
        share_count=3,
        sharer_count=2,
        sharers=["member1", "owner1"],
        sharer_ids=["920000000000000001", "910000000000000001"],
    ))

    # 2) Arabic-heavy resource with mixed-direction body (RTL checks).
    resources.append(make_resource(
        2,
        title=ARABIC_TITLE,
        text=ARABIC_TEXT,
        external_urls=["https://github.com/mithal/noon-automation"],
        external_label="github.com/mithal/noon-automation",
        tool_keys=["github.com/mithal/noon-automation"],
        project_areas=["noon", "arabic"],
        reasons=["أتمتة عمليات نون"],
        resource_type="try",
        pick_score=8.8,
        sender_username="",
        sender_display_name="عضو المجموعة الثاني",
        sender_id="920000000000000002",
        sharers=["عضو المجموعة الثاني"],
        sharer_ids=["920000000000000002"],
    ))

    # 3) Mixed-direction URL resource.
    resources.append(make_resource(
        3,
        resource_id="url:aaaaaaaaaaaaaaaaaaaaaaa1",
        kind="url",
        url=MIXED_URL,
        tweet_id="",
        title="دليل عملي — agentic workflows بالعربية",
        text="Long-form Arabic guide about agent workflows دليل شامل خطوة بخطوة",
        resource_type="read",
        project_areas=["ai", "arabic"],
        pick_score=7.1,
    ))

    # 4) Media resource (thumbnail served by the fixture server when media_base set).
    resources.append(make_resource(
        4,
        title="Motion design breakdown video",
        media_urls=[media_base + "/__media/ok.png"] if media_base else [],
        resource_type="learn",
        project_areas=["creative"],
        pick_score=6.9,
    ))
    # 5) Media resource whose thumbnail 404s (loading/error fallback check).
    resources.append(make_resource(
        5,
        title="Broken thumbnail case",
        media_urls=[media_base + "/__media/missing.png"] if media_base else [],
        resource_type="reference",
        pick_score=1.2,
    ))

    # 6) Note-only resource.
    resources.append(make_resource(
        6,
        resource_id="note:3000000000000000006",
        kind="note",
        url="",
        tweet_id="",
        title="",
        text="تذكير: راجعوا تسعير المنافسين قبل إطلاق المنتج الجديد",
        resource_type="other",
        project_areas=["noon"],
        pick_score=0.5,
    ))

    # 7) Statuses beyond relevant.
    resources.append(make_resource(7, status="pending_review", pick_score=0.0))
    resources.append(make_resource(8, status="pending_hydration", hydration_attempts=0, pick_score=0.0))
    resources.append(make_resource(
        9, status="unavailable", hydration_attempts=3, last_error="not found",
        title="Unreadable post — not proven deleted", pick_score=0.0,
    ))
    resources.append(make_resource(
        10, status="irrelevant", reasons=["No project match"], pick_score=0.0,
    ))

    # 8) Bookmark-sourced record (source filter checks).
    resources.append(make_resource(
        11,
        source="bookmark",
        sender_id="bookmark",
        sender_username="bookmark",
        sender_display_name="Bookmark import",
        title="Imported bookmark about LMS authoring",
        resource_type="read",
        project_areas=["lms"],
        pick_score=6.0,
    ))

    # 9) Bulk rows so the stream paginates and scroll cost is realistic.
    for i in range(12, 92):
        overrides: Dict[str, Any] = {"pick_score": round(4.0 - (i % 17) * 0.2, 2)}
        if i % 9 == 0:
            overrides["title"] = "مورد عربي رقم {} عن تحسين المتاجر".format(i)
            overrides["project_areas"] = ["salla", "arabic"]
        if i % 13 == 0:
            overrides["status"] = "irrelevant"
        if i % 4 == 0:
            overrides["resource_type"] = ("learn", "read", "reference", "other")[(i // 4) % 4]
        resources.append(make_resource(i, **overrides))
    return resources


def build_tools() -> List[Dict[str, Any]]:
    tools: List[Dict[str, Any]] = []

    # 14 authored must_try entries: the expanded shortlist the audit measured.
    for rank in range(1, 15):
        key = "github.com/shortlist/tool-number-{:02d}".format(rank)
        tools.append(make_tool(
            key,
            name="tool-number-{:02d}".format(rank),
            verdict="must_try",
            rank=rank,
            lane="agents" if rank % 2 else "seller-ops",
            what="Synthetic capability summary for shortlist tool {:02d}.".format(rank),
            why="Cuts a real workflow from hours to minutes in project fixtures.",
            first_step="uvx tool-number-{:02d} --help  # try on one exported CSV".format(rank),
            stars=900 + rank * 37,
            license="MIT",
            last_push="2026-08-2{}".format(rank % 9),
            mentions=rank % 3 + 1,
            facts=repo_facts(stars=900 + rank * 37),
            outcome="kept" if rank == 1 else ("trying" if rank == 2 else ("dropped" if rank == 3 else "")),
            outcome_note="ساعتان توفير أسبوعيًا — baseline: 3h, result: 1h" if rank == 1 else ("" if rank != 3 else "Too fiddly on macOS"),
            outcome_at=_ago(days=rank) if rank <= 3 else "",
        ))

    # The long-label must_try (A11) + Arabic must_try.
    tools.append(make_tool(
        LONG_REPO_KEY,
        verdict="must_try",
        rank=15,
        what="Reproduces the overflow: a repository key long enough to exceed a 390px viewport when unwrapped.",
        why="Overflow reproduction fixture.",
        first_step="clone and run the quickstart against one sandbox store",
        stars=15234,
        facts=repo_facts(stars=15234, description="Very long name repository used to verify wrapping."),
        mentions=2,
    ))
    tools.append(make_tool(
        "github.com/mithal/noon-automation",
        name="noon-automation",
        verdict="must_try",
        rank=16,
        lane="seller-ops",
        what="أتمتة رفع المنتجات وتحديث الأسعار على نون",
        why="يختصر إدخال المنتجات اليدوي",
        first_step="pip install noon-automation ثم جرّب على منتج واحد",
        stars=421,
        facts=repo_facts(stars=421, description="Noon store automation with Arabic-first reports"),
        resource_type="try",
        mentions=3,
    ))

    # must_read / excluded / already_have coverage.
    tools.append(make_tool(
        "example.com/articles/agentic-patterns",
        name="Agentic patterns field guide",
        is_repo=False,
        verdict="must_read",
        what="Long-form article on agent design patterns.",
        why="Directly relevant to Hermes orchestration work.",
        resource_type="read",
        mentions=2,
    ))
    tools.append(make_tool(
        "github.com/old/abandoned-thing",
        verdict="excluded",
        reason_code="stale",
        why="No commit in over a year (last push 2024-06-01).",
        auto=True,
        facts={"stars": 3200, "pushed_at": "2024-06-01", "archived": False, "license": "MIT",
               "language": "Go", "description": "Abandoned tool", "checked_at": "2026-09-05", "ok": True},
    ))
    tools.append(make_tool(
        "github.com/have/already-installed",
        verdict="already_have",
        why="Installed since May; superseded by nothing.",
        facts=repo_facts(stars=50000),
    ))

    # Review queue candidates WITH GitHub facts (legacy-eligible).
    for i in range(1, 4):
        tools.append(make_tool(
            "github.com/queue/candidate-{:02d}".format(i),
            best_score=8.0 - i,
            what="Queue candidate {} description from GitHub.".format(i),
            facts=repo_facts(stars=2000 - i * 100, description="Queue candidate {} description from GitHub.".format(i)),
            mentions=2,
        ))

    # A09: non-repo unreviewed tools the old predicate strands.
    tools.append(make_tool(
        "example.com/courses/arabic-lms-authoring",
        name="دورة تأليف المحتوى التعليمي",
        is_repo=False,
        resource_type="learn",
        best_score=7.5,
        mentions=2,
        facts=None,
        meta_loaded=False,
    ))
    tools.append(make_tool(
        "serviceprovider.example/pricing-optimizer",
        name="Pricing optimizer service",
        is_repo=False,
        resource_type="try",
        best_score=6.5,
        facts=None,
        meta_loaded=False,
    ))
    # A09: repo whose facts fetch failed — visible reason, never fabricated.
    tools.append(make_tool(
        "github.com/queue/facts-fetch-failed",
        best_score=5.5,
        facts={"stars": None, "pushed_at": "", "archived": None, "license": "",
               "language": "", "description": "", "checked_at": "2026-09-05", "ok": False},
    ))

    # C5 target: entries carrying the additive review_eligibility block.
    tools.append(make_tool(
        "github.com/eligible/ready-with-project-fit",
        best_score=8.9,
        what="Ready-to-review candidate with provider-04 eligibility attached.",
        facts=repo_facts(stars=3100),
        review_eligibility={
            "lane": "review",
            "reasons": [],
            "evidence": {
                "source_url": "https://github.com/eligible/ready-with-project-fit",
                "checked_at": "2026-09-06",
                "extraction_state": "ok",
                "confidence": "high",
            },
            "project_fit": {
                "project": "agents",
                "benefit": "faster briefing triage",
                "first_step": "run --help on one export",
                "success_measure": "one briefing produced in < 5 min",
            },
        },
    ))
    tools.append(make_tool(
        "example.com/ar/article-pending-evidence",
        name="مقال ينتظر جلب الأدلة",
        is_repo=False,
        resource_type="read",
        best_score=6.8,
        facts=None,
        meta_loaded=False,
        review_eligibility={
            "lane": "evidence_pending",
            "reasons": ["destination page not yet fetched"],
            "evidence": {
                "source_url": "https://example.com/ar/article",
                "checked_at": None,
                "extraction_state": "pending",
                "confidence": "low",
            },
            "project_fit": None,
        },
    ))
    tools.append(make_tool(
        "internal.example/blocked-target",
        name="Blocked fetch target",
        is_repo=False,
        best_score=2.0,
        facts=None,
        meta_loaded=False,
        review_eligibility={
            "lane": "blocked",
            "reasons": ["fetch denied: private_target"],
            "evidence": None,
            "project_fit": None,
        },
    ))
    return tools


def build_status() -> Dict[str, Any]:
    return {
        "updated_at": _ago(minutes=12),
        "fetch_cursor": "synthetic-cursor-0001",
        "fetch_incomplete": False,
        "last_fetch_at": _ago(minutes=12),
        "last_fetch_error": "",
        "messages_captured": 320,
        "owner_messages_captured": 140,
        "non_owner_messages_captured": 180,
        "senders_captured": 4,
        "resource_occurrences": 150,
        "capture_scope_version": "all-senders-v2",
        "resources": 101,
        "status_counts": {
            "pending_hydration": 1,
            "pending_review": 1,
            "relevant": 80,
            "irrelevant": 17,
            "unavailable": 2,
        },
        "unattempted_hydration": 1,
        "gate_ready": False,
    }


def build_senders() -> List[Dict[str, Any]]:
    return [
        {"sender_id": "920000000000000001", "username": "member1", "display_name": "Member One",
         "avatar_url": "", "is_owner": 0, "message_count": 180, "resource_count": 88},
        {"sender_id": "910000000000000001", "username": "owner1", "display_name": "Owner",
         "avatar_url": "", "is_owner": 1, "message_count": 120, "resource_count": 40},
        {"sender_id": "920000000000000002", "username": "", "display_name": "عضو المجموعة الثاني",
         "avatar_url": "", "is_owner": 0, "message_count": 20, "resource_count": 12},
    ]


def build_negative_proposals() -> List[Dict[str, Any]]:
    return [
        {"term": "airdrop", "evidence": "rejected 6 of 6 posts containing it", "log_odds": 2.8},
        {"term": "giveaway", "evidence": "rejected 4 of 5 posts containing it", "log_odds": 1.9},
    ]


def build_health(extended: bool = True, scenario: str = "mixed") -> Dict[str, Any]:
    """C4 frozen envelope; optionally with the additive stages block."""
    health: Dict[str, Any] = {
        "service": "group-radar",
        "ok": True,
        "now": _iso(NOW),
        "server_started_at": _ago(hours=8),
        "pid": 4242,
        "status_updated_at": _ago(minutes=12),
        "status_error": "",
        "age_seconds": 720,
        "stale": False,
        "stale_after_seconds": 5400,
        "gate_ready": False,
        "resources": 101,
        "status_counts": build_status()["status_counts"],
        "dashboard_modified_at": _ago(minutes=12),
        "dashboard_data_modified_at": _ago(minutes=12),
        "next_run_at": _iso(NOW + dt.timedelta(minutes=17)),
        "cron_minutes": [17, 47],
    }
    if not extended:
        return health
    if scenario == "all-ok":
        stages = {name: {"state": "ok", "at": _ago(minutes=13)} for name in (
            "capture", "hydration", "semantic_review", "decision_sync", "notification", "backup", "export")}
        health.update({
            "stages": stages, "last_run_outcome": "ok", "last_run_at": _ago(minutes=13),
            "last_semantic_success_at": _ago(minutes=13), "auth_required": False,
            "backlog_age_seconds": 0, "backoff": {"active": False, "until": None, "reason": ""},
        })
        return health
    health.update({
        "stages": {
            "capture": {"state": "ok", "at": _ago(minutes=13)},
            "hydration": {"state": "degraded", "at": _ago(minutes=14), "detail": "3 of 40 fetches timed out"},
            "semantic_review": {"state": "auth_required", "at": _ago(minutes=15), "detail": "model HTTP 401"},
            "decision_sync": {"state": "recovering", "at": _ago(minutes=16), "detail": "replaying 2 checkpointed decisions"},
            "notification": {"state": "ok", "at": _ago(minutes=13)},
            "backup": {"state": "unknown", "at": None},
            "export": {"state": "ok", "at": _ago(minutes=12)},
        },
        "last_run_outcome": "error",
        "last_run_at": _ago(minutes=13),
        "last_semantic_success_at": _ago(hours=19),
        "auth_required": True,
        "backlog_age_seconds": 66000,
        "backoff": {"active": True, "until": _iso(NOW + dt.timedelta(hours=2)), "reason": "model HTTP 401 x3"},
    })
    return health


def build_payload(media_base: str = "", now: Optional[dt.datetime] = None) -> Dict[str, Any]:
    """Full dashboard payload via the real builder, so shape drift is caught."""
    import sys
    from pathlib import Path

    lane_scripts = Path(__file__).resolve().parents[1] / "scripts"
    if str(lane_scripts) not in sys.path:
        sys.path.insert(0, str(lane_scripts))
    from dashboard_renderer import build_dashboard_payload

    return build_dashboard_payload(
        resources=build_resources(media_base=media_base),
        senders=build_senders(),
        status=build_status(),
        project_areas=PROJECT_AREAS,
        group_name="Synthetic Fixture Group",
        generated_at=_iso(now or NOW),
        conversation_id="fixture-lane06",
        schedule={"cronMinutes": [17, 47], "cadenceMinutes": 30, "staleAfterMinutes": 90},
        tools=build_tools(),
        negative_proposals=build_negative_proposals(),
        now=now or NOW,
    )


if __name__ == "__main__":
    print(json.dumps(build_payload(), ensure_ascii=False)[:2000])
