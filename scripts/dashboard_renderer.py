#!/usr/bin/env python3
"""Render the private-group resource ledger as a self-contained HTML dashboard.

Two layers:

* ``build_dashboard_payload`` turns exported records into one JSON document.
  The same document is embedded inline in the HTML (so ``file://`` keeps
  working) and written as ``dashboard-data.json`` (so a served page can
  re-fetch it and re-render in place without reloading).
* ``render_dashboard_from_payload`` wraps that document in the page.

The page is triage-first: search and quick filters sit directly under the
header, then a three-at-a-time "Decide next" queue (with evidence-pending and
blocked lanes made visible instead of stranded), the "new since you caught up"
line, three ranked focus picks, the collapsed tools/verdicts backlog, a pulse
line, and only then the full filterable stream. Live data is staged behind an
"Apply" control while the page is visible so nothing reorders under the
reader's eyes.

Decision state model (A10): authored verdicts/outcomes are read through one
overlay store — payload values, then a local pending overlay, then the
server's authoritative read-back (``GET /api/decisions``) when that route
exists. Saves are transactional in the UI: in-progress, success, failure with
retry, and revision-conflict states are explicit, and a failed save never
mutates what is shown. Done / Not-for-me / Skip / Caught-up remain
browser-local and are labeled as such. Times use the browser's local zone
with a 12-hour clock.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, Dict, List, Optional, Sequence  # noqa: F401

from resource_typing import TYPE_LABELS, parse_iso


ACTIVITY_DAYS = 14
TOP_PICK_LIMIT = 12
LANE_LIMIT = 8
DEFAULT_SCHEDULE = {"cronMinutes": [17, 47], "cadenceMinutes": 30, "staleAfterMinutes": 90}


def _json_for_script(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace(
        "</", "<\\/"
    )


def _when(record: Dict[str, Any]) -> Optional[dt.datetime]:
    return parse_iso(record.get("shared_at")) or parse_iso(record.get("first_seen_at"))


def _timestamp(record: Dict[str, Any]) -> float:
    moment = _when(record)
    return moment.timestamp() if moment else 0.0


def build_dashboard_payload(
    resources: Sequence[Dict[str, Any]],
    senders: Sequence[Dict[str, Any]],
    status: Dict[str, Any],
    project_areas: Dict[str, str],
    group_name: str,
    generated_at: str,
    conversation_id: str = "",
    schedule: Optional[Dict[str, Any]] = None,
    tools: Optional[Sequence[Dict[str, Any]]] = None,
    negative_proposals: Optional[Sequence[Dict[str, Any]]] = None,
    now: Optional[dt.datetime] = None,
    local_tz: Optional[dt.tzinfo] = None,
) -> Dict[str, Any]:
    now = now or dt.datetime.now(dt.timezone.utc)
    local_tz = local_tz or dt.datetime.now().astimezone().tzinfo or dt.timezone.utc
    resources = list(resources)
    relevant = [item for item in resources if item.get("status") == "relevant"]

    # Top picks: newest window that yields enough candidates. The page keeps a
    # longer list than it shows so local Done / Not-for-me states can be
    # applied without running out of candidates.
    pool: List[Dict[str, Any]] = []
    window_used = 0
    for window in (7, 14, 30, 0):
        if window:
            cutoff = now - dt.timedelta(days=window)
            pool = [item for item in relevant if (_when(item) or now) >= cutoff]
        else:
            pool = list(relevant)
        window_used = window
        if len(pool) >= 5:
            break
    top_picks = sorted(
        pool,
        key=lambda item: (-(float(item.get("pick_score") or 0.0)), -_timestamp(item)),
    )[:TOP_PICK_LIMIT]

    lanes: Dict[str, List[str]] = {}
    lane_totals: Dict[str, int] = {}
    for lane in ("try", "learn", "read", "reference"):
        members = sorted(
            (item for item in relevant if item.get("resource_type") == lane),
            key=lambda item: -_timestamp(item),
        )
        lanes[lane] = [item["resource_id"] for item in members[:LANE_LIMIT]]
        lane_totals[lane] = len(members)
    lane_totals["other"] = sum(1 for item in relevant if item.get("resource_type") == "other")

    today = now.astimezone(local_tz).date()
    day_index = {
        (today - dt.timedelta(days=offset)).isoformat(): {
            "day": (today - dt.timedelta(days=offset)).isoformat(),
            "relevant": 0,
            "total": 0,
        }
        for offset in range(ACTIVITY_DAYS - 1, -1, -1)
    }
    for item in resources:
        moment = _when(item)
        if moment is None:
            continue
        key = moment.astimezone(local_tz).date().isoformat()
        bucket = day_index.get(key)
        if bucket is None:
            continue
        bucket["total"] += 1
        if item.get("status") == "relevant":
            bucket["relevant"] += 1
    activity = list(day_index.values())

    # What the ranking actually covers, so "all data" is verifiable on the page
    # rather than something the user has to take on trust.
    # Coverage describes the briefing, so it is measured over group shares only.
    # Including the imported archive would report a span back to 2016 and make the
    # number meaningless for the thing the page is actually about.
    briefing = [item for item in resources if (item.get("source") or "group") == "group"]
    moments = [m for m in (_when(item) for item in briefing) if m is not None]
    coverage = {
        "resources": len(briefing),
        "relevant": sum(1 for item in briefing if item.get("status") == "relevant"),
        "imported": len(resources) - len(briefing),
        "oldest": min(moments).isoformat() if moments else None,
        "newest": max(moments).isoformat() if moments else None,
        "days": (max(moments) - min(moments)).days + 1 if moments else 0,
    }

    return {
        "resources": resources,
        "tools": list(tools or []),
        "negativeProposals": list(negative_proposals or []),
        "coverage": coverage,
        "senders": list(senders),
        "status": status,
        "projectAreas": project_areas,
        "resourceTypes": dict(TYPE_LABELS),
        "groupName": group_name,
        "conversationId": str(conversation_id or ""),
        "generatedAt": generated_at,
        # A served page applies new data in place; when the page template itself
        # changed it must reload instead, otherwise old JS renders new data.
        "templateVersion": template_version(),
        "schedule": dict(DEFAULT_SCHEDULE, **(schedule or {})),
        "briefing": {
            "topPicks": [item["resource_id"] for item in top_picks],
            "topPicksWindowDays": window_used,
            "lanes": lanes,
            "laneTotals": lane_totals,
        },
        "activity": activity,
    }


def render_dashboard(
    resources: Sequence[Dict[str, Any]],
    senders: Sequence[Dict[str, Any]],
    status: Dict[str, Any],
    project_areas: Dict[str, str],
    group_name: str,
    generated_at: str,
    **extras: Any,
) -> str:
    """Backwards-compatible entry point: build the payload and render it."""
    payload = build_dashboard_payload(
        resources, senders, status, project_areas, group_name, generated_at, **extras
    )
    return render_dashboard_from_payload(payload)


def template_version() -> str:
    import hashlib

    return hashlib.sha1(PAGE_TEMPLATE.encode("utf-8")).hexdigest()[:12]


def render_dashboard_from_payload(payload: Dict[str, Any]) -> str:
    return PAGE_TEMPLATE.replace("__DASHBOARD_DATA__", _json_for_script(payload))


PAGE_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <link rel="icon" href="data:,">
  <title>Group Resource Radar</title>
  <style>
    :root {
      --canvas: #f2f5f3;
      --surface: #ffffff;
      --surface-2: #e9efec;
      --ink: #17201d;
      --muted: #67726e;
      --line: #d6ded9;
      --line-strong: #adb9b3;
      --relevant: #147a59;
      --relevant-soft: #dff1e9;
      --irrelevant: #bd503e;
      --irrelevant-soft: #f8e8e4;
      --pending: #a8730d;
      --pending-soft: #f8edd1;
      --unavailable: #39708b;
      --unavailable-soft: #e1edf3;
      --focus: #12628a;
      --tool: #4f46a3;
      --tool-soft: #e9e7f7;
      --practice: #8a3d6b;
      --practice-soft: #f5e5ee;
      --research: #5c5326;
      --research-soft: #efeadb;
      --other: #5d6763;
      --other-soft: #e6eae8;
      --shadow: 0 8px 24px rgba(25, 43, 35, 0.07);
      --mono: "SFMono-Regular", Menlo, Consolas, monospace;
      font-family: "Avenir Next", Avenir, "Helvetica Neue", Arial, sans-serif;
      letter-spacing: 0;
    }

    * { box-sizing: border-box; letter-spacing: 0; }
    /* An explicit `display` on a class beats the hidden attribute's UA default,
       so hidden elements would stay visible. Make the attribute authoritative. */
    [hidden] { display: none !important; }
    html { background: var(--canvas); color: var(--ink); }
    body { margin: 0; min-width: 320px; background: var(--canvas); }
    img, svg, video { max-width: 100%; }
    button, input, select, textarea { font: inherit; color: inherit; }
    button, select, summary { cursor: pointer; }
    button:focus-visible, input:focus-visible, select:focus-visible, a:focus-visible,
    summary:focus-visible, textarea:focus-visible, [tabindex]:focus-visible {
      outline: 3px solid var(--focus);
      outline-offset: 2px;
    }
    a { color: inherit; }
    details > summary { list-style: none; }
    details > summary::-webkit-details-marker { display: none; }
    .sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
    /* Technical identifiers (keys, URLs, commands) keep left-to-right order
       even inside Arabic text; never visually reverse a path or flag. */
    .idtext { direction: ltr; unicode-bidi: isolate; }
    .skip-link {
      position: absolute; left: -9999px; top: 0; z-index: 100; padding: 8px 14px;
      background: var(--ink); color: #fff; border-radius: 0 0 6px 0; font-weight: 700; font-size: 13px;
    }
    .skip-link:focus { left: 0; }

    .shell { min-height: 100vh; }
    .topbar {
      min-height: 54px; display: flex; align-items: center; gap: 10px 14px; padding: 8px 20px;
      background: var(--surface); border-bottom: 1px solid var(--line); position: sticky; top: 0; z-index: 20;
      flex-wrap: wrap;
    }
    .signal-mark {
      width: 30px; height: 30px; display: grid; grid-template-columns: repeat(3, 1fr); align-items: end; gap: 3px;
      padding: 6px; background: var(--ink); border-radius: 6px; flex: 0 0 auto;
    }
    .signal-mark span { display: block; background: #fff; border-radius: 1px; }
    .signal-mark span:nth-child(1) { height: 35%; }
    .signal-mark span:nth-child(2) { height: 70%; background: #62d0a8; }
    .signal-mark span:nth-child(3) { height: 100%; background: #f0be58; }
    .identity { min-width: 0; flex: 0 1 auto; }
    .identity h1 { margin: 0; font-size: 17px; line-height: 1.15; font-weight: 700; }
    .identity p { margin: 1px 0 0; color: var(--muted); font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .fixture-flag {
      padding: 2px 7px; border-radius: 4px; background: var(--pending-soft); color: var(--pending);
      font-family: var(--mono); font-size: 10px; font-weight: 700; flex: 0 0 auto;
    }
    .topnav { display: flex; gap: 4px; flex-wrap: wrap; align-items: center; }
    .topnav a {
      display: inline-flex; align-items: center; gap: 5px; min-height: 30px; padding: 4px 9px;
      border-radius: 5px; border: 1px solid transparent; color: var(--muted);
      font-size: 12px; font-weight: 600; text-decoration: none; white-space: nowrap;
    }
    .topnav a:hover { background: var(--surface-2); color: var(--ink); }
    .topnav .nav-count { font-family: var(--mono); font-size: 10px; font-weight: 700; padding: 1px 6px; border-radius: 999px; background: var(--surface-2); color: var(--ink); }
    .topnav .nav-count.hot { background: var(--relevant-soft); color: var(--relevant); }
    .live-pill {
      margin-left: auto; display: flex; align-items: center; gap: 8px; padding: 5px 10px; min-width: 0;
      border: 1px solid var(--line); border-radius: 999px; background: var(--canvas); color: var(--muted);
      font-size: 12px;
    }
    .live-dot { width: 9px; height: 9px; border-radius: 50%; background: var(--line-strong); flex: 0 0 auto; }
    .live-pill.live .live-dot { background: var(--relevant); box-shadow: 0 0 0 3px rgba(20, 122, 89, 0.18); }
    .live-pill.stale .live-dot { background: var(--pending); }
    .live-pill.offline .live-dot { background: var(--irrelevant); }
    .live-pill strong { color: var(--ink); font-weight: 700; white-space: nowrap; }
    .live-pill .pill-detail { font-family: var(--mono); font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .pill-button {
      border: 1px solid var(--ink); background: var(--ink); color: #fff; border-radius: 999px;
      padding: 3px 10px; font-size: 11px; font-weight: 700; flex: 0 0 auto;
    }

    /* Stage health strip (C4 extended block). Liveness stays in the pill;
       this row exists only when a stage needs attention. */
    .health-strip {
      display: flex; gap: 6px; align-items: center; flex-wrap: wrap;
      max-width: 1200px; margin: 10px auto 0; padding: 8px 12px;
      border: 1px solid var(--line); border-radius: 6px; background: var(--surface); font-size: 12px;
    }
    .health-strip .health-lead { font-weight: 700; color: var(--ink); }
    .stage-chip {
      display: inline-flex; align-items: center; gap: 5px; max-width: 100%; padding: 3px 8px;
      border-radius: 4px; border: 1px solid var(--line); background: var(--canvas);
      font-size: 11px; color: var(--ink);
    }
    .stage-chip .stage-when { color: var(--muted); font-family: var(--mono); font-size: 10px; white-space: nowrap; }
    .stage-chip.degraded { background: var(--pending-soft); border-color: #e3c88a; }
    .stage-chip.failed { background: var(--irrelevant-soft); border-color: #dfa79b; }
    .stage-chip.auth_required { background: var(--irrelevant-soft); border-color: #dfa79b; font-weight: 700; }
    .stage-chip.recovering { background: var(--unavailable-soft); }
    .stage-chip.unknown { color: var(--muted); }
    .stage-chip.ok { background: var(--relevant-soft); }

    .banner {
      max-width: 1200px; margin: 10px auto 0; padding: 10px 14px; display: flex; gap: 12px; align-items: center; flex-wrap: wrap;
      border: 1px solid #e3c88a; border-radius: 6px; background: #fff7e2; color: #5a3d00; font-size: 13px; line-height: 1.45;
    }
    .banner code { font-family: var(--mono); font-size: 12px; background: rgba(0,0,0,0.05); padding: 1px 5px; border-radius: 3px; overflow-wrap: anywhere; }
    .banner .spacer { flex: 1 1 auto; }
    .banner.quiet { border-color: var(--line); background: var(--surface); color: var(--muted); }
    .container { max-width: 1200px; margin: 0 auto; padding: 0 20px; }

    /* Command bar: search + primary filters, first thing after the header. */
    .command-bar {
      display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
      margin-top: 12px; padding: 10px 12px; background: var(--surface);
      border: 1px solid var(--line); border-radius: 8px; box-shadow: var(--shadow);
    }
    .command-bar .search-input { flex: 1 1 220px; min-width: 0; }
    .command-bar select { flex: 0 1 auto; width: auto; min-width: 0; max-width: 46%; }
    .chip-toggle {
      min-height: 34px; padding: 5px 11px; border: 1px solid var(--line-strong); border-radius: 5px;
      background: var(--surface); font-size: 12px; font-weight: 600; color: var(--muted); white-space: nowrap;
    }
    .chip-toggle[aria-pressed="true"] { border-color: var(--ink); background: var(--surface-2); color: var(--ink); }
    .match-chip {
      display: inline-flex; align-items: center; gap: 6px; min-height: 34px; padding: 5px 11px;
      border: 1px solid var(--line-strong); border-radius: 5px; background: var(--surface);
      font-size: 12px; font-weight: 700; color: var(--ink); white-space: nowrap;
    }
    .match-chip .n { font-family: var(--mono); }

    .briefing { display: grid; gap: 10px; padding-top: 12px; }
    .card {
      background: var(--surface); border: 1px solid var(--line); border-radius: 8px;
      box-shadow: var(--shadow); min-width: 0;
    }
    .card-head { display: flex; align-items: center; gap: 8px; padding: 10px 14px; border-bottom: 1px solid var(--line); flex-wrap: wrap; }
    .card-head h2 { margin: 0; font-size: 12px; text-transform: uppercase; font-weight: 700; color: var(--muted); }
    .card-head .spacer { margin-left: auto; }
    .count-badge { font-family: var(--mono); font-size: 12px; font-weight: 700; padding: 2px 8px; border-radius: 999px; background: var(--surface-2); color: var(--ink); }
    .soft-note { color: var(--muted); font-size: 12px; }
    .local-note { color: var(--muted); font-size: 10px; border: 1px dashed var(--line-strong); border-radius: 3px; padding: 1px 5px; white-space: nowrap; }
    .focus-window {
      width: auto; min-height: 28px; padding: 3px 6px; border: 1px solid var(--line);
      border-radius: 5px; background: var(--surface); color: var(--muted); font-size: 12px;
    }
    .ghost-button {
      min-height: 30px; padding: 4px 10px; border: 1px solid var(--line-strong); border-radius: 5px;
      background: var(--surface); font-size: 12px; font-weight: 600; color: var(--ink);
    }
    .ghost-button:hover { background: var(--surface-2); }
    .ghost-button.subtle { border-color: transparent; color: var(--focus); }
    .ghost-button.small { min-height: 26px; padding: 2px 8px; font-size: 11px; }
    .ghost-button.danger { color: var(--irrelevant); }
    .ghost-button[aria-pressed="true"], .ghost-button.is-current { border-color: var(--ink); background: var(--surface-2); }
    .ghost-button[disabled], .primary-button[disabled] { opacity: 0.55; cursor: not-allowed; }
    .primary-button {
      min-height: 32px; padding: 5px 13px; border: 1px solid var(--ink); border-radius: 6px;
      background: var(--ink); color: #fff; font-size: 13px; font-weight: 700; text-decoration: none; display: inline-flex; align-items: center; gap: 6px;
    }
    .primary-button:hover { background: #2a3530; }
    .save-error {
      flex: 1 1 100%; display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
      padding: 6px 9px; border: 1px solid #dfa79b; border-radius: 5px; background: var(--irrelevant-soft);
      color: #7c2d1e; font-size: 12px;
    }

    /* Decide next (review queue) */
    .queue-list { list-style: none; margin: 0; padding: 0; }
    .queue-item { display: grid; grid-template-columns: minmax(0, 1fr); gap: 3px; padding: 11px 14px; border-bottom: 1px solid var(--line); }
    .queue-item:last-child { border-bottom: 0; }
    .queue-name { margin: 0; font-size: 15px; font-weight: 700; overflow-wrap: anywhere; }
    .queue-name a { text-decoration: none; }
    .queue-name a:hover { text-decoration: underline; }
    .queue-what { margin: 2px 0 0; color: #4e5a55; font-size: 13px; line-height: 1.45; overflow-wrap: anywhere; }
    .evidence-line { display: flex; flex-wrap: wrap; gap: 5px 10px; align-items: center; margin-top: 5px; color: var(--muted); font-size: 11px; }
    .evidence-line .ev-state { font-family: var(--mono); font-weight: 700; padding: 1px 6px; border-radius: 3px; background: var(--surface-2); }
    .evidence-line .ev-state.ok { background: var(--relevant-soft); color: var(--relevant); }
    .evidence-line .ev-state.failed { background: var(--irrelevant-soft); color: var(--irrelevant); }
    .evidence-line .ev-state.pending { background: var(--pending-soft); color: var(--pending); }
    .fit-line { margin: 5px 0 0; padding: 6px 9px; border-radius: 5px; background: var(--surface-2); font-size: 12px; line-height: 1.5; overflow-wrap: anywhere; }
    .fit-line .fit-tag { font-family: var(--mono); font-size: 9px; font-weight: 700; text-transform: uppercase; color: var(--muted); margin-inline-end: 6px; }
    .queue-actions { display: flex; gap: 7px; align-items: center; flex-wrap: wrap; margin-top: 8px; }
    .queue-reason { color: var(--muted); font-size: 12px; overflow-wrap: anywhere; }
    .queue-sub summary { display: flex; align-items: center; gap: 8px; padding: 10px 14px; font-size: 12px; font-weight: 700; color: var(--muted); border-top: 1px solid var(--line); }
    .queue-sub[open] summary { border-bottom: 1px solid var(--line); color: var(--ink); }
    .queue-sub .brief-empty { padding: 12px 14px; }

    .disclosure summary {
      display: flex; align-items: center; gap: 9px; padding: 10px 14px; font-size: 13px; font-weight: 700; color: var(--ink); flex-wrap: wrap;
    }
    .disclosure summary .chev { display: inline-block; width: 8px; height: 8px; border-right: 2px solid var(--muted); border-bottom: 2px solid var(--muted); transform: rotate(-45deg); transition: transform 130ms ease; flex: 0 0 auto; }
    .disclosure[open] summary .chev { transform: rotate(45deg); }
    .disclosure[open] summary { border-bottom: 1px solid var(--line); }
    .disclosure summary .soft-note { font-weight: 400; }
    .brief-list { list-style: none; margin: 0; padding: 2px 0; }
    .brief-item { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 3px 10px; padding: 8px 14px; border-bottom: 1px solid var(--line); align-items: start; }
    .brief-item:last-child { border-bottom: 0; }
    .brief-item:hover { background: #fbfcfb; }
    .brief-title { margin: 0; font-size: 13px; line-height: 1.35; font-weight: 600; overflow-wrap: anywhere; }
    .brief-title a { text-decoration: none; }
    .brief-title a:hover { text-decoration: underline; }
    .brief-meta { grid-column: 1 / -1; color: var(--muted); font-size: 11px; display: flex; flex-wrap: wrap; gap: 5px 9px; align-items: center; }
    .brief-actions { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
    .brief-empty { padding: 14px; color: var(--muted); font-size: 13px; line-height: 1.5; }
    .brief-empty strong { display: block; color: var(--ink); margin-bottom: 3px; }
    .brief-foot { padding: 7px 12px; border-top: 1px solid var(--line); display: flex; gap: 8px; justify-content: flex-end; flex-wrap: wrap; }

    /* Focus now: three compact ranked picks. */
    .focus-list { list-style: none; margin: 0; padding: 0; }
    .focus-item { display: grid; grid-template-columns: 28px minmax(0, 1fr); gap: 2px 10px; padding: 10px 14px; border-bottom: 1px solid var(--line); }
    .focus-item:last-child { border-bottom: 0; }
    .focus-item.lead { padding: 13px 14px 12px; background: #f9fbfa; }
    .rank { font-family: var(--mono); font-size: 13px; font-weight: 700; color: var(--muted); align-self: start; padding-top: 3px; }
    .focus-item.lead .rank { font-size: 17px; color: var(--ink); }
    .focus-title { margin: 0; font-size: 14px; line-height: 1.35; font-weight: 700; overflow-wrap: anywhere; }
    .focus-item.lead .focus-title { font-size: 17px; }
    .focus-title a { text-decoration: none; }
    .focus-title a:hover { text-decoration: underline; }
    .focus-reason { margin: 3px 0 0; color: #4e5a55; font-size: 12px; line-height: 1.45; grid-column: 2; overflow-wrap: anywhere; }
    .focus-why { grid-column: 2; margin: 4px 0 0; color: var(--muted); font-size: 11px; font-family: var(--mono); overflow-wrap: anywhere; }
    .repo-facts { list-style: none; margin: 5px 0 0; padding: 0; display: grid; gap: 4px; grid-column: 2; min-width: 0; }
    .repo-fact { display: flex; flex-wrap: wrap; align-items: baseline; gap: 5px; font-size: 12px; color: var(--muted); line-height: 1.45; min-width: 0; }
    /* The audited 390px overflow came from nowrap on these links. Long keys
       must wrap; direction stays LTR so the path reads correctly in RTL text. */
    .repo-fact a, .repo-fact .repo-name {
      font-family: var(--mono); font-size: 11px; color: var(--ink); text-decoration: none;
      overflow-wrap: anywhere; word-break: break-word; min-width: 0; direction: ltr; unicode-bidi: isolate;
    }
    .repo-fact a:hover { text-decoration: underline; }
    .repo-why { flex: 1 1 100%; font-style: italic; overflow-wrap: anywhere; }
    .chip-stars { color: var(--muted); font-size: 10px; white-space: nowrap; }
    .focus-actions { grid-column: 2; display: flex; gap: 7px; align-items: center; flex-wrap: wrap; margin-top: 7px; }
    .meta-line { grid-column: 2; color: var(--muted); font-size: 11px; display: flex; flex-wrap: wrap; gap: 4px 9px; align-items: center; }
    .focus-empty { padding: 20px 14px; color: var(--muted); font-size: 13px; line-height: 1.5; }
    .focus-empty strong { display: block; color: var(--ink); margin-bottom: 3px; }

    .type-chip {
      display: inline-flex; align-items: center; gap: 4px; padding: 2px 6px; border-radius: 3px;
      font-size: 10px; font-weight: 700; text-transform: uppercase; background: var(--other-soft); color: var(--other);
      white-space: nowrap; font-family: var(--mono);
    }
    .type-chip.try { background: var(--tool-soft); color: var(--tool); }
    .type-chip.learn { background: var(--practice-soft); color: var(--practice); }
    .type-chip.read { background: var(--research-soft); color: var(--research); }
    .type-chip.reference { background: var(--other-soft); color: var(--other); }
    .state-chip { display: inline-block; padding: 2px 6px; border-radius: 3px; background: var(--surface-2); color: var(--muted); font-family: var(--mono); font-size: 9px; font-weight: 700; text-transform: uppercase; white-space: nowrap; }
    .state-chip.new { background: var(--relevant-soft); color: var(--relevant); }
    .metric-chip { display: inline-flex; gap: 4px; align-items: center; font-family: var(--mono); font-size: 11px; color: var(--muted); white-space: nowrap; }
    .link-chip {
      display: inline-flex; align-items: center; gap: 4px; max-width: 100%; min-width: 0; padding: 2px 7px; border: 1px solid var(--line); border-radius: 3px;
      background: var(--canvas); color: var(--focus); font-size: 11px; text-decoration: none; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
      direction: ltr; unicode-bidi: isolate;
    }
    .link-chip:hover { border-color: var(--line-strong); text-decoration: underline; }

    .verdict-chip {
      display: inline-flex; align-items: center; gap: 4px; padding: 2px 7px; border-radius: 3px;
      font-family: var(--mono); font-size: 10px; font-weight: 700; text-transform: uppercase; white-space: nowrap;
      background: var(--other-soft); color: var(--other);
    }
    .verdict-chip.must_try { background: var(--relevant-soft); color: var(--relevant); }
    .verdict-chip.excluded { background: var(--irrelevant-soft); color: var(--irrelevant); }
    .verdict-chip.already_have { background: var(--unavailable-soft); color: var(--unavailable); }
    .verdict-chip.must_read { background: var(--practice-soft); color: var(--practice); }
    .outcome-chip {
      display: inline-flex; align-items: center; gap: 4px; padding: 2px 7px; border-radius: 3px;
      font-family: var(--mono); font-size: 10px; font-weight: 700; text-transform: uppercase; white-space: nowrap;
      background: var(--pending-soft); color: var(--pending);
    }
    .outcome-chip.kept { background: var(--relevant-soft); color: var(--relevant); }
    .outcome-chip.dropped { background: var(--irrelevant-soft); color: var(--irrelevant); }
    .outcome-row { margin-top: 8px; display: flex; gap: 7px; flex-wrap: wrap; align-items: center; }
    .outcome-row .label { color: var(--muted); font-size: 11px; }
    .outcome-note { margin: 5px 0 0; color: var(--muted); font-size: 12px; font-style: italic; overflow-wrap: anywhere; }
    .outcome-form { margin-top: 8px; display: grid; gap: 6px; padding: 9px 10px; border: 1px solid var(--line); border-radius: 6px; background: var(--canvas); }
    .outcome-form .of-row { display: grid; gap: 6px; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); }
    .outcome-form label { display: grid; gap: 3px; font-size: 11px; color: var(--muted); min-width: 0; }
    .outcome-form input, .outcome-form textarea { min-height: 32px; border: 1px solid var(--line-strong); border-radius: 5px; padding: 5px 8px; font-size: 12px; background: var(--surface); width: 100%; }
    .outcome-form .of-actions { display: flex; gap: 7px; flex-wrap: wrap; }
    .adoption { color: var(--muted); font-size: 12px; }
    .adoption strong { color: var(--ink); font-family: var(--mono); }

    /* Tools & verdicts backlog: pressed filter chips (not tabs) + compact rows. */
    .tool-filters { display: flex; gap: 4px; flex-wrap: wrap; padding: 10px 14px 0; }
    .tool-tab {
      min-height: 30px; padding: 4px 9px; border: 1px solid transparent; border-radius: 5px;
      background: transparent; color: var(--muted); font-size: 12px; font-weight: 600; white-space: nowrap;
    }
    .tool-tab:hover { background: var(--surface-2); color: var(--ink); }
    .tool-tab[aria-pressed="true"] { color: var(--ink); border-color: var(--line-strong); background: var(--surface-2); }
    .tool-search { margin: 8px 14px 0; }
    .tool-list { list-style: none; margin: 0; padding: 2px 0; }
    .tool-row { display: grid; grid-template-columns: minmax(0, 1fr); gap: 2px; padding: 9px 14px; border-bottom: 1px solid var(--line); }
    .tool-row:last-child { border-bottom: 0; }
    .tool-row:hover { background: #fbfcfb; }
    .tool-row.is-must { background: #f9fbfa; }
    .tool-head { display: flex; align-items: baseline; gap: 6px 8px; flex-wrap: wrap; min-width: 0; }
    .tool-rank { font-family: var(--mono); font-size: 12px; font-weight: 700; color: var(--muted); flex: 0 0 auto; }
    .tool-name { margin: 0; font-size: 14px; font-weight: 700; overflow-wrap: anywhere; min-width: 0; }
    .tool-name a { text-decoration: none; }
    .tool-name a:hover { text-decoration: underline; }
    .tool-what { margin: 2px 0 0; color: #4e5a55; font-size: 12px; line-height: 1.45; overflow-wrap: anywhere; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
    .tool-why { margin: 5px 0 0; color: var(--ink); font-size: 12px; line-height: 1.5; overflow-wrap: anywhere; }
    .tool-step {
      margin: 6px 0 0; padding: 6px 9px; border-radius: 5px; background: var(--surface-2);
      font-family: var(--mono); font-size: 11px; line-height: 1.45; overflow-wrap: anywhere; direction: ltr; unicode-bidi: isolate;
    }
    .tool-meta { margin-top: 5px; display: flex; flex-wrap: wrap; gap: 4px 10px; align-items: center; color: var(--muted); font-size: 11px; }
    .tool-actions { margin-top: 7px; display: flex; gap: 7px; flex-wrap: wrap; align-items: center; }
    .tool-more summary { display: inline-flex; gap: 6px; align-items: center; color: var(--focus); font-size: 11px; font-weight: 600; padding: 4px 0; }
    .coverage-note { color: var(--muted); font-size: 11px; font-family: var(--mono); overflow-wrap: anywhere; }

    .pulse-line { display: flex; align-items: center; gap: 6px 14px; flex-wrap: wrap; padding: 9px 14px; font-size: 13px; color: var(--muted); }
    .pulse-line strong { color: var(--ink); font-family: var(--mono); }
    .pulse-line .spacer { margin-left: auto; }
    .lane-chips { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; padding: 0 14px 10px; }
    .lane-chip {
      display: inline-flex; align-items: center; gap: 6px; min-height: 30px; padding: 4px 9px;
      border: 1px solid var(--line); border-radius: 5px; background: var(--surface); font-size: 11px; font-weight: 700;
    }
    .lane-chip:hover { background: var(--surface-2); }
    .lane-chip .n { font-family: var(--mono); color: var(--muted); font-weight: 700; }
    .pulse-chart { display: grid; grid-template-columns: repeat(14, minmax(0, 1fr)); gap: 3px; align-items: end; height: 56px; padding: 12px 14px 0; }
    .pulse-bar { position: relative; background: var(--relevant); opacity: 0.55; border-radius: 3px 3px 0 0; min-height: 2px; cursor: default; }
    .pulse-bar.today { opacity: 1; }
    .pulse-bar:hover, .pulse-bar:focus { opacity: 1; }
    .pulse-labels { display: flex; justify-content: space-between; padding: 6px 14px 10px; color: var(--muted); font-size: 10px; font-family: var(--mono); }
    .ledger { display: flex; flex-wrap: wrap; gap: 5px; }
    .ledger button {
      border: 1px solid var(--line); border-radius: 5px; background: var(--surface); padding: 3px 8px;
      font-size: 11px; color: var(--muted); display: inline-flex; gap: 6px; align-items: center;
    }
    .ledger button i { width: 7px; height: 7px; border-radius: 2px; display: inline-block; }
    .ledger button strong { font-family: var(--mono); color: var(--ink); }
    .ledger button[aria-pressed="true"] { border-color: var(--ink); }
    .ledger .relevant i { background: var(--relevant); }
    .ledger .irrelevant i { background: var(--irrelevant); }
    .ledger .pending i { background: var(--pending); }
    .ledger .unavailable i { background: var(--unavailable); }
    .tooltip {
      position: fixed; z-index: 40; pointer-events: none; padding: 6px 9px; border-radius: 5px; background: var(--ink); color: #fff;
      font-size: 11px; font-family: var(--mono); white-space: nowrap; transform: translate(-50%, calc(-100% - 10px)); box-shadow: var(--shadow);
    }

    .workspace { padding: 14px 0 32px; display: grid; grid-template-columns: minmax(0, 1fr) 230px; gap: 14px; align-items: start; }
    .side-rail { position: sticky; top: 66px; background: var(--surface); border: 1px solid var(--line); border-radius: 6px; overflow: hidden; }
    .rail-section { padding: 12px 14px; border-bottom: 1px solid var(--line); }
    .rail-section:last-child { border-bottom: 0; }
    .rail-heading { margin: 0 0 9px; font-size: 11px; text-transform: uppercase; color: var(--muted); font-weight: 700; }
    .breakdown { display: grid; gap: 9px; }
    .breakdown-row { display: grid; gap: 4px; }
    .breakdown-meta { display: flex; justify-content: space-between; gap: 8px; font-size: 11px; }
    .breakdown-name { color: var(--ink); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .breakdown-count { color: var(--muted); font-family: var(--mono); }
    .breakdown-bar { height: 4px; background: var(--surface-2); border-radius: 2px; overflow: hidden; }
    .breakdown-fill { height: 100%; background: var(--relevant); }
    .sender-list { display: grid; gap: 9px; }
    .sender-row { display: grid; grid-template-columns: 26px minmax(0, 1fr) auto; gap: 8px; align-items: center; }
    .sender-avatar, .sender-fallback { width: 26px; height: 26px; border-radius: 50%; object-fit: cover; background: var(--surface-2); border: 1px solid var(--line); }
    .sender-fallback { display: grid; place-items: center; font-size: 11px; font-weight: 700; }
    .sender-copy { min-width: 0; }
    .sender-name { font-size: 11px; font-weight: 700; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .sender-volume { color: var(--muted); font-size: 10px; }
    .sender-count { font-family: var(--mono); font-size: 11px; color: var(--muted); }

    .results-panel { min-width: 0; background: var(--surface); border: 1px solid var(--line); border-radius: 6px; overflow: hidden; box-shadow: var(--shadow); scroll-margin-top: 72px; }
    .results-tools { padding: 10px 12px; border-bottom: 1px solid var(--line); display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
    .results-tools select { width: auto; max-width: 46%; }
    .status-tabs { display: flex; gap: 4px; flex-wrap: wrap; padding: 8px 12px 10px; border-bottom: 1px solid var(--line); }
    .status-tab { min-height: 30px; padding: 4px 9px; border: 1px solid transparent; border-radius: 5px; background: transparent; color: var(--muted); font-size: 12px; font-weight: 600; white-space: nowrap; }
    .status-tab:hover { background: var(--surface-2); color: var(--ink); }
    .status-tab[aria-pressed="true"] { color: var(--ink); border-color: var(--line-strong); background: var(--surface-2); }
    .results-head { min-height: 36px; display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 7px 12px; background: #f8faf9; border-bottom: 1px solid var(--line); color: var(--muted); font-size: 11px; flex-wrap: wrap; }
    .results-count { font-family: var(--mono); }
    .active-filters { display: flex; gap: 5px; flex-wrap: wrap; justify-content: flex-end; }
    .active-filters span { padding: 2px 7px; border: 1px solid var(--line); border-radius: 3px; background: var(--surface); overflow-wrap: anywhere; }
    .reset-button { min-height: 30px; padding: 4px 10px; border: 1px solid var(--line-strong); border-radius: 5px; background: var(--surface); font-weight: 600; font-size: 12px; }
    .reset-button:hover { background: var(--surface-2); }
    select, .search-input { min-height: 36px; border: 1px solid var(--line-strong); border-radius: 5px; background: var(--surface); padding: 6px 9px; font-size: 13px; }
    .search-input { width: 100%; }

    .resource-list { min-height: 240px; }
    .resource-row { position: relative; display: grid; grid-template-columns: 5px minmax(0, 1fr); border-bottom: 1px solid var(--line); background: var(--surface); }
    .resource-row:last-child { border-bottom: 0; }
    .resource-row:hover { background: #fbfcfb; }
    .resource-row.handled { opacity: 0.62; }
    .status-stripe { background: var(--pending); }
    .resource-row[data-status="relevant"] .status-stripe { background: var(--relevant); }
    .resource-row[data-status="irrelevant"] .status-stripe { background: var(--irrelevant); }
    .resource-row[data-status="unavailable"] .status-stripe { background: var(--unavailable); }
    .resource-body { display: grid; grid-template-columns: minmax(0, 1fr) 148px; gap: 14px; padding: 12px 14px; min-width: 0; }
    .resource-body.no-media { grid-template-columns: minmax(0, 1fr); }
    .resource-main { min-width: 0; }
    .resource-kicker { display: flex; gap: 6px 8px; flex-wrap: wrap; align-items: center; margin-bottom: 5px; }
    .status-label { padding: 2px 6px; border-radius: 3px; background: var(--pending-soft); color: var(--pending); font-family: var(--mono); font-size: 9px; font-weight: 700; text-transform: uppercase; white-space: nowrap; }
    .resource-row[data-status="relevant"] .status-label { background: var(--relevant-soft); color: var(--relevant); }
    .resource-row[data-status="irrelevant"] .status-label { background: var(--irrelevant-soft); color: var(--irrelevant); }
    .resource-row[data-status="unavailable"] .status-label { background: var(--unavailable-soft); color: var(--unavailable); }
    .source-meta { color: var(--muted); font-size: 11px; overflow-wrap: anywhere; }
    .resource-title { margin: 0; font-size: 14px; line-height: 1.35; font-weight: 700; overflow-wrap: anywhere; }
    .resource-text { margin: 5px 0 0; color: #4e5a55; font-size: 13px; line-height: 1.5; display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; overflow-wrap: anywhere; }
    .tag-row { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 8px; align-items: center; min-width: 0; }
    .tag { max-width: 100%; padding: 3px 7px; border: 1px solid var(--line); border-radius: 3px; background: var(--canvas); color: #44514c; font-size: 10px; overflow-wrap: anywhere; }
    .reason { margin: 7px 0 0; color: var(--muted); font-size: 11px; line-height: 1.4; overflow-wrap: anywhere; }
    .resource-actions { display: flex; align-items: center; gap: 8px; margin-top: 8px; flex-wrap: wrap; }
    .source-link { color: var(--focus); font-size: 12px; font-weight: 700; text-decoration: none; }
    .source-link:hover { text-decoration: underline; }
    .share-note { color: var(--muted); font-size: 10px; overflow-wrap: anywhere; }
    .media-box { position: relative; width: 148px; aspect-ratio: 13 / 9; align-self: start; border-radius: 5px; border: 1px solid var(--line); background: var(--surface-2); overflow: hidden; }
    .media-box img { width: 100%; height: 100%; object-fit: cover; display: block; }
    .media-box.media-failed img { display: none; }
    .media-note { position: absolute; inset: 0; display: none; place-items: center; padding: 6px; color: var(--muted); font-size: 10px; text-align: center; }
    .media-box.media-failed .media-note { display: grid; }
    .empty-state { padding: 56px 20px; text-align: center; color: var(--muted); }
    .empty-state strong { display: block; color: var(--ink); margin-bottom: 5px; }
    .load-more-wrap { padding: 12px; border-top: 1px solid var(--line); text-align: center; }
    .load-more { min-height: 36px; padding: 7px 16px; border-radius: 5px; border: 1px solid var(--line-strong); background: var(--surface); font-size: 12px; font-weight: 700; }
    .load-more:hover { background: var(--surface-2); }
    .footer { padding: 8px 0 28px; color: var(--muted); font-size: 10px; line-height: 1.6; }
    .footer .export-links { display: flex; gap: 5px 12px; flex-wrap: wrap; margin-top: 5px; }
    .footer .export-links a { color: var(--focus); text-decoration: none; font-family: var(--mono); }
    .footer .export-links a:hover { text-decoration: underline; }

    .toast {
      position: fixed; left: 50%; bottom: 20px; transform: translateX(-50%); z-index: 50; display: flex; align-items: center; gap: 10px;
      padding: 10px 14px; background: var(--ink); color: #fff; border-radius: 8px; box-shadow: 0 10px 30px rgba(0,0,0,0.25); font-size: 13px; max-width: calc(100vw - 24px);
    }
    .toast button { border: 1px solid rgba(255,255,255,0.4); background: transparent; color: #fff; border-radius: 5px; padding: 4px 10px; font-size: 12px; font-weight: 700; flex: 0 0 auto; }
    .toast #toast-text { overflow-wrap: anywhere; }

    @media (max-width: 980px) {
      .workspace { grid-template-columns: 1fr; }
      .side-rail { position: static; order: 2; }
      .results-panel { order: 1; }
    }
    @media (max-width: 680px) {
      .topbar { padding: 8px 12px; gap: 8px 10px; }
      .live-pill .pill-detail { display: none; }
      .identity h1 { font-size: 15px; }
      .identity p { display: none; }
      .container { padding: 0 12px; }
      .banner, .health-strip { margin-inline: 12px; }
      .focus-item.lead .focus-title { font-size: 15px; }
      .command-bar select { max-width: 100%; flex: 1 1 44%; }
      .resource-body { grid-template-columns: 1fr; }
      .media-box { grid-row: 1; width: 100%; max-height: 200px; aspect-ratio: 16 / 9; }
      .active-filters { display: none; }
    }
    @media (prefers-reduced-motion: reduce) { *, *::before, *::after { scroll-behavior: auto !important; transition: none !important; } }
  </style>
</head>
<body>
  <a class="skip-link" href="#stream">Skip to resource stream</a>
  <div class="shell">
    <header class="topbar">
      <div class="signal-mark" aria-hidden="true"><span></span><span></span><span></span></div>
      <div class="identity">
        <h1 id="page-title">Group Resource Radar</h1>
        <p id="group-subtitle"></p>
      </div>
      <span class="fixture-flag" id="fixture-flag" hidden>FIXTURE DATA</span>
      <nav class="topnav" aria-label="Sections">
        <a href="#decide-card" id="nav-decide">Decide <span class="nav-count" id="nav-decide-count">0</span></a>
        <a href="#since-card" id="nav-new">New <span class="nav-count" id="nav-new-count">0</span></a>
        <a href="#stream" id="nav-browse">Browse ⌕</a>
      </nav>
      <div class="live-pill" id="live-pill" role="status" aria-live="polite">
        <span class="live-dot" aria-hidden="true"></span>
        <strong id="pill-label">Loading</strong>
        <span class="pill-detail" id="pill-detail"></span>
        <button class="pill-button" id="apply-update" type="button" hidden>Apply</button>
      </div>
    </header>

    <div class="health-strip" id="health-strip" role="status" aria-live="polite" hidden></div>
    <div class="banner" id="stale-banner" hidden role="alert"></div>
    <div class="banner quiet" id="mode-banner" hidden></div>

    <div class="container">
      <div class="command-bar" role="search" aria-label="Search and filter resources">
        <input class="search-input" id="search-input" type="search"
               placeholder="Search resources, tools, reasons, senders — عربي أو English"
               aria-label="Search resources and tools" autocomplete="off">
        <select id="source-filter" aria-label="Source">
          <option value="group">Group chat</option>
          <option value="bookmark">My bookmarks</option>
          <option value="bookmark-archive">Bookmark archive</option>
          <option value="all">All sources</option>
        </select>
        <select id="type-filter" aria-label="Resource type"><option value="all">All types</option></select>
        <select id="project-filter" aria-label="Project fit"><option value="all">All project areas</option></select>
        <button class="chip-toggle" id="unseen-filter" type="button" aria-pressed="false">New only</button>
        <button class="match-chip" id="match-chip" type="button" title="Open the matching resources in the stream">
          <span class="n" id="match-count">0</span> match ↓
        </button>
      </div>

      <section class="briefing" aria-label="Briefing">
        <section class="card" id="decide-card" data-lane06-queue aria-labelledby="decide-heading" style="scroll-margin-top:72px">
          <div class="card-head">
            <h2 id="decide-heading">Decide next</h2>
            <span class="count-badge" id="queue-count">0</span>
            <span class="soft-note">3 at a time · your call is saved to the radar</span>
            <span class="spacer"></span>
            <span class="coverage-note" id="queue-note"></span>
          </div>
          <ul class="queue-list" id="queue-list"></ul>
          <details class="queue-sub" id="queue-pending-wrap">
            <summary><span class="chev" aria-hidden="true"></span>Waiting for evidence
              <span class="count-badge" id="queue-pending-count">0</span>
              <span class="soft-note">not dismissed — facts not fetched yet; you can still decide</span>
            </summary>
            <ul class="brief-list" id="queue-pending-list"></ul>
          </details>
          <details class="queue-sub" id="queue-blocked-wrap" hidden>
            <summary><span class="chev" aria-hidden="true"></span>Blocked
              <span class="count-badge" id="queue-blocked-count">0</span>
              <span class="soft-note">fetch denied or unsafe target — reason shown</span>
            </summary>
            <ul class="brief-list" id="queue-blocked-list"></ul>
          </details>
        </section>

        <details class="card disclosure" id="since-card" data-lane06-new>
          <summary>
            <span class="chev" aria-hidden="true"></span>
            <span id="since-summary">New since you caught up</span>
            <span class="count-badge" id="since-count">0</span>
            <span class="spacer"></span>
            <button class="ghost-button small" id="caught-up" type="button" title="Mark everything as seen; only shares after this moment count as new. Stored on this Mac only.">Caught up</button>
          </summary>
          <ul class="brief-list" id="since-list"></ul>
          <div class="brief-foot" id="since-foot" hidden>
            <button class="ghost-button subtle" id="since-more" type="button"></button>
          </div>
        </details>

        <section class="card" data-lane06-focus aria-labelledby="focus-heading">
          <div class="card-head">
            <h2 id="focus-heading">Focus now</h2>
            <select id="focus-window-select" class="focus-window" aria-label="Ranking window">
              <option value="0">ranked from all data</option>
              <option value="7">ranked from the last 7 days</option>
              <option value="30">ranked from the last 30 days</option>
            </select>
            <span class="spacer"></span>
            <span class="soft-note" id="focus-note">3 picks · Done and Not-for-me stay on this Mac</span>
            <span class="coverage-note" id="focus-coverage"></span>
          </div>
          <ol class="focus-list" id="focus-list"></ol>
          <div class="brief-foot">
            <button class="ghost-button subtle" id="focus-stream" type="button">See everything ranked →</button>
          </div>
        </section>

        <details class="card disclosure" id="tools-card">
          <summary>
            <span class="chev" aria-hidden="true"></span>
            <span id="tools-heading">Tools &amp; verdicts</span>
            <span class="count-badge" id="tools-count">0</span>
            <span class="adoption" id="tools-adoption"></span>
            <span class="spacer"></span>
            <span class="coverage-note" id="tools-coverage"></span>
          </summary>
          <div class="tool-filters" id="tool-tabs" role="group" aria-label="Filter tools by verdict">
            <button class="tool-tab" type="button" data-verdict="must_try" aria-pressed="true">Must try</button>
            <button class="tool-tab" type="button" data-verdict="must_read" aria-pressed="false">Must read</button>
            <button class="tool-tab" type="button" data-verdict="excluded" aria-pressed="false">Excluded</button>
            <button class="tool-tab" type="button" data-verdict="already_have" aria-pressed="false">Already have</button>
            <button class="tool-tab" type="button" data-verdict="unreviewed" aria-pressed="false">Not reviewed</button>
            <button class="tool-tab" type="button" data-verdict="all" aria-pressed="false">All tools</button>
          </div>
          <div class="tool-search">
            <input class="search-input" id="tool-search" type="search" placeholder="Search every linked tool by name, e.g. markitdown" aria-label="Search tools" autocomplete="off">
          </div>
          <ul class="tool-list" id="tool-list"></ul>
          <div class="brief-foot" id="tools-foot" hidden>
            <button class="ghost-button subtle" id="tools-more" type="button"></button>
          </div>
        </details>

        <details class="card disclosure" id="rules-card" hidden>
          <summary>
            <span class="chev" aria-hidden="true"></span>
            Suggested exclusion rules
            <span class="count-badge" id="rules-count">0</span>
            <span class="soft-note">learned from what you already rejected</span>
          </summary>
          <ul class="brief-list" id="rules-list"></ul>
        </details>

        <section class="card" aria-label="Activity pulse and lanes">
          <div class="pulse-line" id="pulse-line"></div>
          <div class="lane-chips" id="lane-chips" role="group" aria-label="Open a lane in the stream"></div>
          <details class="disclosure" id="pulse-details">
            <summary><span class="chev" aria-hidden="true"></span>14-day pulse <span class="soft-note">relevant shares per day</span></summary>
            <div class="pulse-chart" id="pulse-chart" role="img" aria-label="Relevant shares per day, last 14 days"></div>
            <div class="pulse-labels"><span id="pulse-start"></span><span>today</span></div>
          </details>
        </section>
      </section>

      <main class="workspace">
        <section class="results-panel" id="stream" aria-label="Group resources" tabindex="-1">
          <div class="results-tools">
            <select id="sender-filter" aria-label="Shared by"><option value="all">All senders</option></select>
            <select id="handled-filter" aria-label="Done and Not-for-me visibility">
              <option value="hide">Hide handled</option>
              <option value="show">Show handled</option>
              <option value="only">Only handled</option>
            </select>
            <select id="sort-order" aria-label="Sort resources">
              <option value="latest">Latest shared</option>
              <option value="pick">Top pick score</option>
              <option value="shares">Most shared</option>
              <option value="score">Highest fit score</option>
              <option value="oldest">Oldest shared</option>
            </select>
            <button class="reset-button" id="reset-filters" type="button">Reset filters</button>
          </div>
          <div class="status-tabs" id="status-tabs" role="group" aria-label="Filter by classification status">
            <button class="status-tab" type="button" data-status="all" aria-pressed="true">All</button>
            <button class="status-tab" type="button" data-status="relevant" aria-pressed="false">Relevant</button>
            <button class="status-tab" type="button" data-status="irrelevant" aria-pressed="false">Irrelevant</button>
            <button class="status-tab" type="button" data-status="pending" aria-pressed="false">Pending</button>
            <button class="status-tab" type="button" data-status="unavailable" aria-pressed="false">Unavailable</button>
          </div>
          <div class="results-head">
            <span class="results-count" id="results-count" aria-live="polite">0 resources</span>
            <div class="active-filters" id="active-filters" aria-label="Active filters"></div>
          </div>
          <div class="resource-list" id="resource-list"></div>
          <div class="load-more-wrap" id="load-more-wrap" hidden>
            <button class="load-more" id="load-more" type="button">Show more</button>
          </div>
        </section>

        <aside class="side-rail" id="side-rail" aria-label="Breakdowns">
          <section class="rail-section">
            <h2 class="rail-heading">Relevant by project</h2>
            <div class="breakdown" id="project-breakdown"></div>
          </section>
          <section class="rail-section">
            <h2 class="rail-heading">Group contributors</h2>
            <div class="sender-list" id="sender-breakdown"></div>
          </section>
          <section class="rail-section">
            <h2 class="rail-heading">Coverage</h2>
            <div class="breakdown" id="coverage-breakdown"></div>
          </section>
        </aside>
      </main>
      <footer class="footer">
        <div id="footer-copy"></div>
        <div class="export-links" id="export-links" hidden></div>
      </footer>
    </div>
  </div>

  <div class="tooltip" id="tooltip" hidden></div>
  <div class="toast" id="toast" hidden role="status" aria-live="polite">
    <span id="toast-text"></span>
    <button type="button" id="toast-action" hidden>Show</button>
    <button type="button" id="toast-close" aria-label="Dismiss">×</button>
  </div>
  <div class="sr" id="sr-status" role="status" aria-live="polite"></div>
  <div class="sr" id="sr-alert" role="alert"></div>

  <script id="dashboard-data" type="application/json">__DASHBOARD_DATA__</script>
  <script>
    (() => {
      'use strict';
      const PAGE_SIZE = 60;
      const FOCUS_LIMIT = 3;
      const SINCE_LIMIT = 3;
      const LIVE = /^https?:$/.test(location.protocol);
      const params = new URLSearchParams(location.search);
      const POLL_VISIBLE_MS = Math.max(5, Number(params.get('poll')) || 60) * 1000;
      const POLL_HIDDEN_MS = Math.max(POLL_VISIBLE_MS, 5 * 60 * 1000);
      const STATIC_RELOAD_MS = 15 * 60 * 1000;
      const SAVE_TIMEOUT_MS = 12 * 1000;

      const el = (id) => document.getElementById(id);
      const count = (value) => new Intl.NumberFormat('en-US').format(Number(value || 0));
      const compact = (value) => new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 }).format(Number(value || 0));
      const escapeHTML = (value) => String(value == null ? '' : value)
        .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;').replaceAll("'", '&#039;');
      const cleanText = (value) => String(value || '').replace(/\s+/g, ' ').trim();
      const clip = (value, max) => { const text = cleanText(value); return text.length > max ? text.slice(0, max - 1) + '…' : text; };
      const safeURL = (value) => /^https?:\/\//i.test(String(value || '')) ? String(value) : '';
      const pendingStatus = (value) => value === 'pending_review' || value === 'pending_hydration';
      const displayStatus = (value) => pendingStatus(value) ? 'pending' : value;
      const dateValue = (item) => Date.parse(item.shared_at || item.first_seen_at || '') || 0;
      const clock = new Intl.DateTimeFormat('en-US', { hour: 'numeric', minute: '2-digit', hour12: true });
      const dayClock = new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', hour12: true });
      const dayOnly = new Intl.DateTimeFormat('en-US', { weekday: 'short', month: 'short', day: 'numeric' });
      const asTime = (value) => typeof value === 'number' ? value : (Date.parse(value || '') || 0);
      const formatClock = (value) => { const t = asTime(value); return t ? clock.format(t) : '—'; };
      const formatDay = (value) => { const t = asTime(value); return t ? dayOnly.format(t) : '—'; };
      const formatDateTime = (value) => { const t = asTime(value); return t ? dayClock.format(t) : 'Date unavailable'; };
      const timeAgo = (value) => {
        const t = asTime(value);
        if (!t) return '';
        const diff = Math.max(0, Date.now() - t);
        const min = Math.round(diff / 60000);
        if (min < 1) return 'just now';
        if (min < 60) return `${min} min ago`;
        const hours = Math.round(min / 60);
        if (hours < 24) return `${hours} h ago`;
        return formatDateTime(t);
      };
      const senderLabel = (item) => item.sender_username ? '@' + String(item.sender_username).replace(/^@/, '') : item.sender_display_name || item.sender_id || 'Unknown sender';
      const titleFor = (item) => cleanText(item.title || item.text || item.url || (item.kind === 'note' ? 'Group note' : 'Untitled resource'));
      const excerptFor = (item) => { const title = cleanText(item.title); const text = cleanText(item.text); return text && text !== title ? text : ''; };
      const typeOf = (item) => item.resource_type || 'other';
      const lk = (key) => String(key || '').toLowerCase();
      const normalizeArabic = (value) => String(value || '').toLowerCase()
        .replace(/[أإآٱ]/g, 'ا').replace(/ة/g, 'ه').replace(/ى/g, 'ي')
        .replace(/[ً-ْـ]/g, '');
      // Identifiers (keys, URLs, commands) render LTR inside any surrounding text.
      const idtext = (value) => `<span class="idtext">${escapeHTML(value)}</span>`;

      function announce(text, assertive) {
        const region = el(assertive ? 'sr-alert' : 'sr-status');
        region.textContent = '';
        setTimeout(() => { region.textContent = text; }, 30);
      }

      // ---- data + derived state -------------------------------------------------
      let data = JSON.parse(document.getElementById('dashboard-data').textContent);
      let resources = [], senders = [], projectAreas = {}, typeLabels = {}, status = {}, briefing = {}, activity = [], schedule = {};
      let tools = [], coverage = {}, toolsByKey = new Map();
      let toolVerdict = 'must_try', toolQuery = '', toolsExpanded = false;
      const TOOLS_COLLAPSED = 10;
      const VERDICT_LABEL = { must_try: 'Must try', must_read: 'Must read', excluded: 'Excluded', already_have: 'Already have', unreviewed: 'Not reviewed' };
      const OUTCOME_LABEL = { trying: 'Trying', kept: 'Kept', dropped: 'Dropped' };
      const STAGE_LABEL = { capture: 'capture', hydration: 'hydration', semantic_review: 'semantic review', decision_sync: 'decision sync', notification: 'notification', backup: 'backup', export: 'export' };
      const STAGE_STATE_LABEL = { ok: 'ok', degraded: 'degraded', failed: 'failed', auth_required: 'needs your sign-in', recovering: 'recovering', unknown: 'no signal yet' };
      const QUEUE_BATCH = 3;   // never show more than three decisions at once
      let byId = new Map();
      let lastStatusSeen = null;
      let pollFailures = 0;
      let serverOnline = LIVE;
      let stagedData = null;
      let recheckData = false;
      let health = null;

      const storeKey = (suffix) => `radar:${data.conversationId || 'group'}:${suffix}`;
      const readStore = (key, fallback) => { try { const raw = localStorage.getItem(key); return raw == null ? fallback : JSON.parse(raw); } catch (_) { return fallback; } };
      const writeStore = (key, value) => { try { localStorage.setItem(key, JSON.stringify(value)); } catch (_) { /* ignore */ } };

      // ---- authored decisions: one overlay store (A10) -------------------------
      // Reads go through effVerdict/effOutcome. Precedence: server read-back
      // (`api/decisions`, additive route registered by the coordinator) >
      // locally-confirmed saves (localStorage overlay, survives reload and
      // syncs across this browser's tabs) > the exported payload. A failed
      // save changes nothing. `readback` records whether the authoritative
      // route exists so the UI can be honest about second-device freshness.
      const decisions = {
        verdicts: new Map(), outcomes: new Map(),
        revision: null, source: 'payload', readback: LIVE ? 'unknown' : 'static',
      };
      const overlayKey = () => storeKey('decisionsOverlay');
      const readOverlay = () => { const o = readStore(overlayKey(), null); return o && typeof o === 'object' ? o : { verdicts: {}, outcomes: {}, savedAt: 0 }; };
      function seedDecisions() {
        decisions.verdicts.clear();
        decisions.outcomes.clear();
        decisions.source = 'payload';
        for (const t of tools) {
          if (t.verdict && t.verdict !== 'unreviewed' && !t.auto) {
            decisions.verdicts.set(lk(t.key), { key: t.key, name: t.name, verdict: t.verdict, why: t.why || '', what: t.what || '', first_step: t.first_step || '', lane: t.lane || '', reason_code: t.reason_code || '', resource_type: t.resource_type || '', rank: t.rank, decided_by: 'export' });
          }
          if (t.outcome) {
            decisions.outcomes.set(lk(t.key), { key: t.key, name: t.name, state: t.outcome, note: t.outcome_note || '', decided_at: t.outcome_at || '', decided_by: 'export' });
          }
        }
        const overlay = readOverlay();
        const generated = Date.parse(data.generatedAt || '') || 0;
        let applied = 0;
        for (const [key, entry] of Object.entries(overlay.verdicts || {})) {
          if (!entry || (entry.at || 0) <= generated) continue;
          if (entry.cleared) decisions.verdicts.delete(key); else decisions.verdicts.set(key, entry.record);
          applied += 1;
        }
        for (const [key, entry] of Object.entries(overlay.outcomes || {})) {
          if (!entry || (entry.at || 0) <= generated) continue;
          if (entry.cleared) decisions.outcomes.delete(key); else decisions.outcomes.set(key, entry.record);
          applied += 1;
        }
        if (applied) decisions.source = 'local';
      }
      function adoptDecisionDocuments(payload) {
        if (!payload || typeof payload !== 'object') return false;
        const verdictList = ((payload.verdicts_document || {}).verdicts) || null;
        const outcomeList = ((payload.outcomes_document || {}).outcomes) || null;
        if (!Array.isArray(verdictList) || !Array.isArray(outcomeList)) return false;
        decisions.verdicts = new Map(verdictList.filter((e) => e && e.key).map((e) => [lk(e.key), e]));
        decisions.outcomes = new Map(outcomeList.filter((e) => e && e.key).map((e) => [lk(e.key), e]));
        decisions.revision = payload.revision != null ? payload.revision : decisions.revision;
        decisions.source = 'server';
        try { localStorage.removeItem(overlayKey()); } catch (_) { /* ignore */ }
        return true;
      }
      let decisionsFetchAt = 0;
      async function fetchDecisions(force) {
        if (!LIVE || decisions.readback === 'unavailable') return false;
        if (!force && Date.now() - decisionsFetchAt < 5000) return false;
        decisionsFetchAt = Date.now();
        try {
          const response = await fetch('api/decisions', { cache: 'no-store' });
          if (response.status === 404 || response.status === 405) {
            decisions.readback = 'unavailable';
            return false;
          }
          if (!response.ok) return false;
          const body = await response.json();
          if (adoptDecisionDocuments(body)) { decisions.readback = 'ok'; return true; }
        } catch (_) { /* offline: overlay stays authoritative locally */ }
        return false;
      }
      function rememberSave(kind, key, record, cleared) {
        // Only needed while the read-back route is absent: it is what makes a
        // reload (and this browser's other tabs) agree with a confirmed save.
        if (decisions.readback === 'ok') return;
        const overlay = readOverlay();
        const bucket = kind === 'verdict' ? overlay.verdicts : overlay.outcomes;
        bucket[lk(key)] = cleared ? { cleared: true, at: Date.now() } : { record, at: Date.now() };
        overlay.savedAt = Date.now();
        writeStore(overlayKey(), overlay);
      }
      const effVerdictEntry = (tool) => decisions.verdicts.get(lk(tool.key)) || null;
      function effVerdict(tool) {
        const entry = effVerdictEntry(tool);
        if (entry) return entry.verdict;
        if (tool.auto && tool.verdict && tool.verdict !== 'unreviewed') return tool.verdict;
        if (decisions.source === 'payload') return tool.verdict || 'unreviewed';
        // Overlay/server say nothing about this key: an export-time authored
        // verdict may have been cleared. Server mode is authoritative; local
        // mode only knows about keys it touched, so fall back to the export.
        if (decisions.source === 'server') return 'unreviewed';
        return tool.verdict || 'unreviewed';
      }
      const effRank = (tool) => { const e = effVerdictEntry(tool); return e && e.rank != null ? e.rank : tool.rank; };
      const effWhy = (tool) => { const e = effVerdictEntry(tool); return e && e.why ? e.why : (tool.why || ''); };
      function effOutcome(tool) {
        const entry = decisions.outcomes.get(lk(tool.key));
        if (entry) return { state: entry.state || '', note: entry.note || '', at: entry.decided_at || '' };
        if (decisions.source === 'server') return { state: '', note: '', at: '' };
        return { state: tool.outcome || '', note: tool.outcome_note || '', at: tool.outcome_at || '' };
      }

      // ---- save pipeline: explicit pending/success/failure/conflict ------------
      const savingKeys = new Set();
      let lastFailedSave = null;
      async function postAction(route, action, body) {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), SAVE_TIMEOUT_MS);
        try {
          const response = await fetch(route, {
            method: 'POST',
            headers: { 'X-Radar-Action': action, 'Content-Type': 'application/json' },
            cache: 'no-store',
            signal: controller.signal,
            body: JSON.stringify(body),
          });
          const payload = await response.json().catch(() => ({}));
          return { ok: response.ok, status: response.status, body: payload };
        } catch (error) {
          return { ok: false, status: 0, body: { error: error && error.name === 'AbortError' ? 'save timed out' : 'could not reach the local radar server' } };
        } finally {
          clearTimeout(timer);
        }
      }
      function staticModeToast() {
        showToast('Read-only file view — open the served dashboard (127.0.0.1:8765) to record decisions.');
        announce('Read-only file view. Decisions need the served dashboard.', true);
      }
      function saveFreshnessNote() {
        return decisions.readback === 'ok'
          ? 'Saved — every tab and browser sees it now.'
          : 'Saved on the radar. Other browsers catch up at the next export; this browser shows it now.';
      }
      async function submitVerdict(tool, verdict, why) {
        if (!LIVE) { staticModeToast(); return false; }
        const key = lk(tool.key);
        if (savingKeys.has(key)) return false;
        savingKeys.add(key);
        renderDecisionsUI();
        announce(`Saving ${VERDICT_LABEL[verdict] || verdict} for ${tool.name}…`);
        const body = {
          key: tool.key, name: tool.name, verdict,
          resource_type: tool.resource_type || '',
          what: ((tool.facts || {}).description || tool.what || ''),
          why: why || '',
          lane: tool.lane || '', stars: (tool.facts || {}).stars,
          license: (tool.facts || {}).license || '', last_push: (tool.facts || {}).pushed_at || '',
        };
        // Integration repair (Chat 07, 2026-09-07): /api/decisions returns
        // revision as an OBJECT {verdicts, outcomes}, while the mutation
        // endpoints take a per-document non-negative INTEGER. Forwarding the
        // object made every post-read-back save fail with 400. Select the
        // matching document's revision; tolerate a bare integer too.
        const _vrev = revisionFor('verdicts');
        if (_vrev != null) body.expected_revision = _vrev;
        const result = await postAction('api/verdict', 'verdict', body);
        savingKeys.delete(key);
        if (result.ok) {
          lastFailedSave = null;
          const record = result.body.record || {
            key: tool.key, name: tool.name, verdict, why: body.why, what: body.what,
            resource_type: body.resource_type, lane: body.lane, decided_by: 'dashboard',
            decided_at: new Date().toISOString(),
          };
          if (verdict === 'clear') decisions.verdicts.delete(key);
          else decisions.verdicts.set(key, record);
          if (result.body.revision != null) decisions.revision = result.body.revision;
          if (decisions.source === 'payload') decisions.source = 'local';
          rememberSave('verdict', tool.key, record, verdict === 'clear');
          renderDecisionsUI();
          const message = verdict === 'clear'
            ? `${tool.name}: verdict cleared. ${saveFreshnessNote()}`
            : `${tool.name} → ${VERDICT_LABEL[verdict]}. ${saveFreshnessNote()}`;
          showToast(message);
          announce(message);
          return true;
        }
        return handleSaveFailure(result, () => submitVerdict(tool, verdict, why), tool, renderDecisionsUI);
      }
      async function submitOutcome(tool, state, note) {
        if (!LIVE) { staticModeToast(); return false; }
        const key = lk(tool.key);
        if (savingKeys.has(key)) return false;
        savingKeys.add(key);
        renderDecisionsUI();
        announce(`Saving outcome for ${tool.name}…`);
        const body = { key: tool.key, name: tool.name, state, note: note || '' };
        // Integration repair (Chat 07, 2026-09-07): /api/decisions returns
        // revision as an OBJECT {verdicts, outcomes}, while the mutation
        // endpoints take a per-document non-negative INTEGER. Forwarding the
        // object made every post-read-back save fail with 400. Select the
        // matching document's revision; tolerate a bare integer too.
        const _orev = revisionFor('outcomes');
        if (_orev != null) body.expected_revision = _orev;
        const result = await postAction('api/outcome', 'outcome', body);
        savingKeys.delete(key);
        if (result.ok) {
          lastFailedSave = null;
          if (state === 'clear') decisions.outcomes.delete(key);
          else decisions.outcomes.set(key, result.body.record || { key: tool.key, name: tool.name, state, note: note || '', decided_at: new Date().toISOString(), decided_by: 'dashboard' });
          if (result.body.revision != null) decisions.revision = result.body.revision;
          if (decisions.source === 'payload') decisions.source = 'local';
          rememberSave('outcome', tool.key, decisions.outcomes.get(key) || null, state === 'clear');
          openOutcomeForms.delete(key);
          renderDecisionsUI();
          const message = state === 'clear' ? `${tool.name}: outcome cleared. ${saveFreshnessNote()}` : `${tool.name} → ${OUTCOME_LABEL[state]}. ${saveFreshnessNote()}`;
          showToast(message);
          announce(message);
          return true;
        }
        return handleSaveFailure(result, () => submitOutcome(tool, state, note), tool, renderDecisionsUI);
      }
      function handleSaveFailure(result, retry, tool, rerender) {
        // The previous state is untouched: nothing was applied optimistically.
        if (result.status === 409 && result.body && result.body.current_revision != null) {
          announce('Not saved: decisions changed elsewhere. Reloading the latest decisions.', true);
          showToast('Not saved — decisions changed elsewhere (another tab or Telegram). Refreshed; check and retry.');
          fetchDecisions(true).then(() => renderDecisionsUI());
          rerender();
          return false;
        }
        if (result.status === 409) {
          const hint = result.body.hint ? ` ${result.body.hint}` : '';
          const message = `Not saved: ${result.body.error || 'the server rejected that decision'}.${hint}`;
          showToast(message);
          announce(message, true);
          rerender();
          return false;
        }
        lastFailedSave = { retry, key: tool ? tool.key : '' };
        const message = `Not saved — ${result.body && result.body.error ? result.body.error : 'the radar server did not answer'}. Your previous state is unchanged.`;
        showToast(message, { label: 'Retry', onAction: () => { if (lastFailedSave) lastFailedSave.retry(); } });
        announce(message, true);
        rerender();
        return false;
      }

      // ---- browser-local states: Done / Not-for-me / Caught up / Skip ----------
      // These live in this browser only (localStorage) and are labeled as such
      // in the UI. Nothing here is written back to the ledger or the authored
      // decision files; durable actions go through submitVerdict/submitOutcome.
      let handled = readStore(storeKey('handled'), {});
      if (!handled || typeof handled !== 'object') handled = {};
      const handledState = (item) => (handled[item.resource_id] || {}).state || '';
      const isHandled = (item) => Boolean(handledState(item));
      function setHandled(resourceId, value) {
        if (value) handled[resourceId] = { state: value, at: Date.now() };
        else delete handled[resourceId];
        writeStore(storeKey('handled'), handled);
        renderBriefing();
        renderStream();
      }
      let caughtUpAt = Number(readStore(storeKey('caughtUpAt'), 0)) || 0;
      const firstVisit = !caughtUpAt;
      const baseline = () => caughtUpAt || ((Date.parse(data.generatedAt) || Date.now()) - 24 * 3600 * 1000);
      const isNew = (item) => item.status === 'relevant' && !isHandled(item) && dateValue(item) > baseline();
      function markCaughtUp() {
        caughtUpAt = Date.now();
        writeStore(storeKey('caughtUpAt'), caughtUpAt);
        renderBriefing();
        renderStream();
        showToast(`Caught up at ${formatClock(caughtUpAt)} — new shares from now on will show here.`);
      }
      let queueSkipped = readStore(storeKey('queueSkipped'), {}) || {};
      function skipForNow(tool) {
        queueSkipped[tool.key] = Date.now();
        writeStore(storeKey('queueSkipped'), queueSkipped);
        announce(`${tool.name} skipped for now on this Mac. It stays under Not reviewed.`);
        renderDecisionsUI();
      }
      function unskipAll() {
        queueSkipped = {};
        writeStore(storeKey('queueSkipped'), queueSkipped);
        renderDecisionsUI();
      }
      const openOutcomeForms = new Map();

      // Disclosure open/closed state is remembered so a lane opened once stays open.
      function rememberDisclosure(node, key) {
        const saved = readStore(storeKey('open:' + key), null);
        if (saved != null) node.open = Boolean(saved);
        node.addEventListener('toggle', () => writeStore(storeKey('open:' + key), node.open));
      }

      // ---- filter state ---------------------------------------------------------
      // Source defaults to the group: the briefing must stay a briefing even
      // though the imported archive is many times larger.
      const state = { status: 'all', source: 'group', sender: 'all', project: 'all', type: 'all', handled: 'hide', unseen: false, query: '', sort: 'latest', visible: PAGE_SIZE };
      try {
        const saved = JSON.parse(sessionStorage.getItem(storeKey('filters')) || 'null');
        if (saved && typeof saved === 'object') Object.assign(state, saved, { visible: PAGE_SIZE });
      } catch (_) { /* ignore */ }
      const persistState = () => { try { sessionStorage.setItem(storeKey('filters'), JSON.stringify(state)); } catch (_) { /* ignore */ } };

      // ---- apply payload --------------------------------------------------------
      function applyData(next) {
        const previousIds = new Set(resources.map((item) => item.resource_id));
        data = next;
        resources = Array.isArray(data.resources) ? data.resources : [];
        senders = Array.isArray(data.senders) ? data.senders : [];
        projectAreas = data.projectAreas || {};
        typeLabels = data.resourceTypes || { try: 'Try it', learn: 'Learn it', read: 'Read it', reference: 'Keep for reference', other: 'Uncategorized' };
        status = data.status || {};
        briefing = data.briefing || {};
        activity = Array.isArray(data.activity) ? data.activity : [];
        tools = Array.isArray(data.tools) ? data.tools : [];
        toolsByKey = new Map(tools.map((t) => [lk(t.key), t]));
        coverage = data.coverage || {};
        schedule = data.schedule || { cronMinutes: [17, 47], cadenceMinutes: 30, staleAfterMinutes: 90 };
        byId = new Map(resources.map((item) => [item.resource_id, item]));
        if (status.updated_at) lastStatusSeen = status.updated_at;
        stagedData = null;
        seedDecisions();
        renderAll();
        // A fresh export may still trail decisions saved seconds ago; the
        // authoritative read-back reconciles when the route exists.
        if (decisions.readback === 'ok') fetchDecisions(true).then((changed) => { if (changed) renderDecisionsUI(); });
        return resources.filter((item) => item.status === 'relevant' && !previousIds.has(item.resource_id)).length;
      }
      const needsReload = (next) => Boolean(next && next.templateVersion && data.templateVersion && next.templateVersion !== data.templateVersion);
      function reloadForNewTemplate() { persistState(); location.reload(); }
      function stageData(next) {
        const currentIds = new Set(resources.map((item) => item.resource_id));
        const fresh = (next.resources || []).filter((item) => item.status === 'relevant' && !currentIds.has(item.resource_id)).length;
        if (document.hidden) { if (needsReload(next)) reloadForNewTemplate(); else applyData(next); return; }
        stagedData = next;
        const button = el('apply-update');
        button.textContent = needsReload(next) ? 'Reload · new layout' : (fresh ? `Apply · ${count(fresh)} new` : 'Apply update');
        button.hidden = false;
        renderPill();
      }
      el('apply-update').addEventListener('click', () => {
        if (stagedData && needsReload(stagedData)) { reloadForNewTemplate(); return; }
        if (stagedData) { const fresh = applyData(stagedData); showToast(fresh ? `${count(fresh)} new relevant share${fresh === 1 ? '' : 's'} added` : 'Dashboard is current'); }
        el('apply-update').hidden = true;
      });
      document.addEventListener('visibilitychange', () => {
        if (!document.hidden) {
          if (stagedData) { if (needsReload(stagedData)) { reloadForNewTemplate(); return; } applyData(stagedData); el('apply-update').hidden = true; }
          if (LIVE) schedulePoll(0);
        }
      });
      window.addEventListener('pagehide', persistState);

      // ---- live pill / stale banner --------------------------------------------
      function nextScan(now) {
        const minutes = (schedule.cronMinutes || [17, 47]).map(Number).sort((a, b) => a - b);
        const base = new Date(now); base.setSeconds(0, 0);
        for (let hour = 0; hour <= 1; hour += 1) {
          for (const minute of minutes) {
            const candidate = new Date(base); candidate.setHours(base.getHours() + hour, minute, 0, 0);
            if (candidate.getTime() > now) return candidate.getTime();
          }
        }
        return now + 30 * 60000;
      }
      let scanRequestedAt = 0;
      function healthIssues() {
        if (!health || typeof health !== 'object') return [];
        const issues = [];
        const stages = health.stages && typeof health.stages === 'object' ? health.stages : null;
        if (stages) {
          for (const [name, record] of Object.entries(stages)) {
            const stageState = (record || {}).state || 'unknown';
            if (stageState !== 'ok') issues.push({ name, state: stageState, at: (record || {}).at || null, detail: (record || {}).detail || '' });
          }
        }
        if (health.auth_required && !issues.some((issue) => issue.state === 'auth_required')) {
          issues.push({ name: 'account', state: 'auth_required', at: health.last_run_at || null, detail: '' });
        }
        return issues;
      }
      function renderHealthStrip() {
        const strip = el('health-strip');
        const issues = healthIssues();
        const backoff = health && health.backoff && health.backoff.active ? health.backoff : null;
        const attention = issues.filter((issue) => issue.state !== 'unknown');
        if (!attention.length && !backoff) { strip.hidden = true; strip.innerHTML = ''; return; }
        const chips = [];
        if (health && health.auth_required) chips.push('<span class="stage-chip auth_required">⏸ needs your sign-in before the next semantic pass</span>');
        for (const issue of issues) {
          const when = issue.at ? `<span class="stage-when">${escapeHTML(timeAgo(issue.at))}</span>` : '<span class="stage-when">no timestamp</span>';
          const detail = issue.detail ? ` — ${escapeHTML(clip(issue.detail, 90))}` : '';
          chips.push(`<span class="stage-chip ${escapeHTML(issue.state)}" title="${escapeHTML(clip(issue.detail || issue.state, 180))}">${escapeHTML(STAGE_LABEL[issue.name] || issue.name)}: ${escapeHTML(STAGE_STATE_LABEL[issue.state] || issue.state)}${detail} ${when}</span>`);
        }
        if (backoff) chips.push(`<span class="stage-chip degraded">retrying until ${escapeHTML(formatClock(backoff.until))}${backoff.reason ? ` — ${escapeHTML(clip(backoff.reason, 60))}` : ''}</span>`);
        if (health && Number(health.backlog_age_seconds) > 3600) chips.push(`<span class="stage-chip unknown">review backlog ${escapeHTML(String(Math.round(health.backlog_age_seconds / 3600)))} h old</span>`);
        strip.innerHTML = `<span class="health-lead">Pipeline:</span>${chips.join('')}`;
        strip.hidden = false;
      }
      function renderPill() {
        const pill = el('live-pill');
        const updated = asTime(status.updated_at || data.generatedAt);
        const ageMin = updated ? (Date.now() - updated) / 60000 : Infinity;
        const stale = ageMin > Number(schedule.staleAfterMinutes || 90);
        pill.classList.remove('live', 'stale', 'offline');
        let label;
        if (LIVE && !serverOnline) { pill.classList.add('offline'); label = 'Server offline'; }
        else if (stale) { pill.classList.add('stale'); label = 'Stale'; }
        else if (LIVE) { pill.classList.add('live'); label = stagedData ? 'Update ready' : 'Live'; }
        else { label = 'Static file'; }
        el('pill-label').textContent = label;
        const parts = [updated ? `updated ${timeAgo(updated)}` : 'no run recorded'];
        const attention = healthIssues().filter((issue) => issue.state !== 'unknown').length;
        if (attention) parts.push(`${attention} stage issue${attention === 1 ? '' : 's'}`);
        if (!stale) parts.push(`next scan ${formatClock(nextScan(Date.now()))}`);
        if (!LIVE) parts.push('reloads every 15 min');
        el('pill-detail').textContent = parts.join(' · ');
        pill.title = updated ? `Monitor last ran ${formatDateTime(updated)}` : '';

        const banner = el('stale-banner');
        if (stale) {
          const since = updated ? formatDateTime(updated) : 'an unknown time';
          const minutes = (schedule.cronMinutes || [17, 47]).map((m) => ':' + String(m).padStart(2, '0')).join(' / ');
          const scanning = scanRequestedAt && Date.now() - scanRequestedAt < 10 * 60000;
          banner.innerHTML = `<span><strong>No scan since ${escapeHTML(since)}.</strong> After sleep the monitor catches up on its own at ${escapeHTML(minutes)}. ${LIVE ? (scanning ? 'A scan is running now; this page updates when it finishes.' : 'Or start one now.') : 'Or run <code>python3 scripts/group_filter_loop.py</code> in x-bookmarks.'}</span><span class="spacer"></span>${LIVE && !scanning ? '<button class="ghost-button" id="scan-now" type="button">Scan now</button>' : ''}`;
          banner.hidden = false;
          const scan = el('scan-now');
          if (scan) scan.addEventListener('click', requestScan);
        } else {
          banner.hidden = true;
        }
      }
      async function requestScan() {
        try {
          const response = await fetch('api/run', { method: 'POST', headers: { 'X-Radar-Action': 'run' }, cache: 'no-store' });
          const body = await response.json().catch(() => ({}));
          if (response.ok) { scanRequestedAt = Date.now(); showToast('Scan started — usually done in about a minute.'); schedulePoll(20000); }
          else showToast(body.reason ? `Could not start a scan: ${body.reason}` : 'Could not start a scan.');
        } catch (_) { showToast('Could not reach the local radar server.'); }
        renderPill();
      }

      // ---- shared row pieces ----------------------------------------------------
      function chip(item) { const type = typeOf(item); return `<span class="type-chip ${escapeHTML(type)}">${escapeHTML((typeLabels[type] || type).split(' ')[0])}</span>`; }
      function repoRefs(item) {
        return (item.tool_keys || [])
          .map((key) => toolsByKey.get(lk(key)))
          .filter((t) => t && t.is_repo);
      }
      function toolForUrl(item, url) {
        const path = String(url).replace(/^https?:\/\/(www\.)?/, '').toLowerCase();
        for (const key of item.tool_keys || []) {
          if (path.startsWith(lk(key))) return toolsByKey.get(lk(key));
        }
        return null;
      }
      // Prefer key over label: the key is canonical and never truncated.
      const repoShortName = (t) => String(t.key || t.label || '').replace(/^github\.com\//, '');
      function linkChip(item) {
        const url = safeURL((item.external_urls || [])[0]);
        const label = cleanText(item.external_label || '');
        if (!url || !label) return '';
        const tool = toolForUrl(item, url);
        const desc = tool && tool.what ? ` — ${cleanText(tool.what)}` : '';
        return `<a class="link-chip" href="${escapeHTML(url)}" target="_blank" rel="noopener noreferrer" title="${escapeHTML(url + desc)}">↗ ${escapeHTML(label)}</a>`;
      }
      // A post can link dozens of tools; showing only the first hides the rest.
      function linkChips(item, limit) {
        const urls = (item.external_urls || []).filter(safeURL);
        if (!urls.length) return '';
        const chips = urls.slice(0, limit).map((url) => {
          const label = url.replace(/^https?:\/\/(www\.)?/, '').replace(/\/$/, '').slice(0, 44);
          const tool = toolForUrl(item, url);
          const desc = tool && tool.what ? ` — ${cleanText(tool.what)}` : '';
          const stars = tool && tool.is_repo ? Number((tool.facts || {}).stars || tool.stars || 0) : 0;
          return `<a class="link-chip" href="${escapeHTML(url)}" target="_blank" rel="noopener noreferrer" title="${escapeHTML(url + desc)}">↗ ${escapeHTML(label)}${stars > 0 ? `<span class="chip-stars">★ ${escapeHTML(compact(stars))}</span>` : ''}</a>`;
        });
        if (urls.length > limit) chips.push(`<span class="link-chip" title="${escapeHTML(urls.slice(limit).join('\n'))}">+${urls.length - limit} more links</span>`);
        return chips.join('');
      }
      function verdictChip(item) {
        const v = item.verdict;
        return v && v.verdict ? `<span class="verdict-chip ${escapeHTML(v.verdict)}" title="${escapeHTML(v.name || '')}: ${escapeHTML(v.why || '')}">${escapeHTML(VERDICT_LABEL[v.verdict] || v.verdict)}</span>` : '';
      }
      function metaBits(item, options) {
        const bits = [`<bdi>${escapeHTML(senderLabel(item))}</bdi>`, escapeHTML(timeAgo(dateValue(item)))];
        if (!(options && options.noReshare) && Number(item.share_count || 1) > 1) bits.push(`<span class="metric-chip" title="Shared in the group more than once">↻ ${escapeHTML(String(item.share_count))}×</span>`);
        if (Number(item.likes || 0) > 0) bits.push(`<span class="metric-chip" title="Likes on X">♥ ${escapeHTML(compact(item.likes))}</span>`);
        return bits.join('<span aria-hidden="true">·</span>');
      }
      // Codex decisions carry a one-sentence reason; rule decisions only carry
      // keyword dumps — show the matched project areas instead.
      function glanceReason(item) {
        if (item.decision_source === 'claude') return cleanText((item.reasons || []).join(' / '));
        const areas = (item.project_areas || []).filter((key) => key !== 'ai').map((key) => projectAreas[key] || key);
        if (!areas.length) return (item.project_areas || []).includes('ai') ? 'Matches: AI' : cleanText((item.reasons || []).join(' / '));
        return `Matches: ${areas.slice(0, 4).join(', ')}${areas.length > 4 ? ` +${areas.length - 4}` : ''}`;
      }
      function actionButtons(item, small) {
        const size = small ? ' small' : '';
        const current = handledState(item);
        if (current) return `<button class="ghost-button${size}" type="button" data-undo="${escapeHTML(item.resource_id)}">Undo ${current === 'done' ? 'done' : 'not-for-me'}</button>`;
        return `<button class="ghost-button${size}" type="button" data-done="${escapeHTML(item.resource_id)}" title="I looked at this / tried it. Stored on this Mac only.">Done</button><button class="ghost-button${size}" type="button" data-dismiss="${escapeHTML(item.resource_id)}" title="Stop suggesting this. Stored on this Mac only.">Not for me</button>`;
      }
      function whyRanked(item) {
        const parts = item.pick_parts || {};
        const bits = [];
        if (Number(item.share_count || 1) > 1) bits.push(Number(item.sharer_count || 1) > 1 ? `shared ${item.share_count}× by ${item.sharer_count} members` : `shared ${item.share_count}× in the group`);
        const areas = (item.project_areas || []).filter((key) => key !== 'ai');
        if (areas.length) bits.push(`${areas.length} project area${areas.length === 1 ? '' : 's'}`);
        if (Number(parts.repo || 0) > 0) {
          const repos = repoRefs(item);
          const named = repos.slice(0, 2).map(repoShortName);
          bits.push(named.length ? `repo: ${named.join(', ')}${repos.length > 2 ? ` +${repos.length - 2}` : ''}` : 'repo link');
        }
        const age = dateValue(item) ? Math.round((Date.now() - dateValue(item)) / 86400000) : null;
        if (age != null) bits.push(age === 0 ? 'today' : `${age} d old`);
        return bits.join(' · ');
      }
      // Naming the repo answers "which"; this block answers "why would it help":
      // the repo's real description and stars from the enrichment cache, plus
      // your own verdict when one exists. A repo that has not been enriched
      // yet says so instead of guessing.
      function repoHelp(item, limit) {
        const repos = repoRefs(item).slice(0, limit || 2);
        if (!repos.length) return '';
        return `<ul class="repo-facts">${repos.map((t) => {
          const facts = t.facts || {};
          const url = safeURL(t.url) || safeURL(`https://${t.key}`);
          const stars = Number(facts.stars || t.stars || 0);
          const desc = cleanText(t.what || '');
          const verdict = effVerdict(t);
          const verdictHtml = verdict && verdict !== 'unreviewed' ? `<span class="verdict-chip ${escapeHTML(verdict)}">${escapeHTML(VERDICT_LABEL[verdict] || verdict)}</span>` : '';
          const why = cleanText(effWhy(t));
          return `<li class="repo-fact">
            ${url ? `<a href="${escapeHTML(url)}" target="_blank" rel="noopener noreferrer">↗ ${escapeHTML(t.key || t.label)}</a>` : `<span class="repo-name">${escapeHTML(t.key || t.label)}</span>`}
            ${stars > 0 ? `<span class="metric-chip" title="GitHub stars">★ ${escapeHTML(compact(stars))}</span>` : ''}
            ${verdictHtml}
            <span class="repo-desc" dir="auto">${desc ? escapeHTML(desc) : 'not checked against GitHub yet'}</span>
            ${why ? `<span class="repo-why" dir="auto">${escapeHTML(why)}</span>` : ''}
          </li>`;
        }).join('')}</ul>`;
      }
      function bindActions(root) {
        root.querySelectorAll('[data-done]').forEach((button) => button.addEventListener('click', () => { setHandled(button.dataset.done, 'done'); showToast('Done ✓ — it will not resurface in Focus now. Stored on this Mac only.'); }));
        root.querySelectorAll('[data-dismiss]').forEach((button) => button.addEventListener('click', () => setHandled(button.dataset.dismiss, 'dismissed')));
        root.querySelectorAll('[data-undo]').forEach((button) => button.addEventListener('click', () => setHandled(button.dataset.undo, '')));
      }
      function briefItem(item) {
        const url = safeURL(item.url);
        const title = escapeHTML(titleFor(item));
        const link = url ? `<a href="${escapeHTML(url)}" target="_blank" rel="noopener noreferrer">${title}</a>` : title;
        return `<li class="brief-item"><h3 class="brief-title" dir="auto">${chip(item)} ${link}</h3><div class="brief-actions">${actionButtons(item, true)}</div><div class="brief-meta">${metaBits(item)}${linkChip(item)}</div></li>`;
      }

      // ---- focus / since / lanes / pulse ---------------------------------------
      // The ranking window is the user's choice; default is the WHOLE ledger.
      // In the all-data view the recency bonus is subtracted from pick_score so
      // an old-but-great tool is not buried under fresh mediocre ones.
      let focusWindow = Number(readStore(storeKey('focusWindow'), 0)) || 0;
      const allTimeScore = (item) => Number(item.pick_score || 0) - Number((item.pick_parts || {}).recency || 0);
      function focusCandidates() {
        const pool = resources.filter((item) => item.status === 'relevant' && !isHandled(item));
        const cutoff = focusWindow > 0 ? Date.now() - focusWindow * 86400000 : 0;
        const windowed = cutoff ? pool.filter((item) => dateValue(item) >= cutoff) : pool;
        const scorer = focusWindow > 0 ? (item) => Number(item.pick_score || 0) : allTimeScore;
        return [...windowed].sort((a, b) => scorer(b) - scorer(a) || dateValue(b) - dateValue(a)).slice(0, FOCUS_LIMIT);
      }
      function renderFocus() {
        const picks = focusCandidates();
        const select = el('focus-window-select');
        if (select.value !== String(focusWindow)) select.value = String(focusWindow);
        // Spell out what the chosen window actually covers, so "all data" is checkable.
        const pool = resources.filter((item) => item.status === 'relevant');
        const cutoff = focusWindow > 0 ? Date.now() - focusWindow * 86400000 : 0;
        const inWindow = cutoff ? pool.filter((item) => dateValue(item) >= cutoff).length : pool.length;
        el('focus-coverage').textContent = focusWindow > 0
          ? `${count(inWindow)} of ${count(pool.length)} relevant`
          : `all ${count(pool.length)} relevant · ${count(coverage.days || 0)} days · ${formatDay(coverage.oldest)} → ${formatDay(coverage.newest)}`;
        const list = el('focus-list');
        if (!picks.length) {
          list.innerHTML = '<li class="focus-empty"><strong>Nothing to focus on right now.</strong>Everything relevant is handled, or the monitor has not classified anything yet.</li>';
          return;
        }
        list.innerHTML = picks.map((item, index) => {
          const lead = index === 0;
          const url = safeURL(item.url);
          const repo = safeURL((item.external_urls || [])[0]);
          const title = escapeHTML(titleFor(item));
          const reason = glanceReason(item);
          const openLabel = item.external_label && repo ? `Open ${escapeHTML(item.external_label.split('/')[0])}` : 'Open on X';
          const primaryHref = repo || url;
          const secondary = repo && url ? `<a class="ghost-button" href="${escapeHTML(url)}" target="_blank" rel="noopener noreferrer">View post</a>` : '';
          return `<li class="focus-item ${lead ? 'lead' : ''}">
            <span class="rank" aria-hidden="true">${index + 1}</span>
            <h3 class="focus-title" dir="auto">${chip(item)} ${isNew(item) ? '<span class="state-chip new">new</span> ' : ''}${url ? `<a href="${escapeHTML(url)}" target="_blank" rel="noopener noreferrer">${title}</a>` : title}</h3>
            ${reason ? `<p class="focus-reason" dir="auto">${escapeHTML(reason)}</p>` : ''}
            <div class="meta-line">${metaBits(item, { noReshare: true })}</div>
            <p class="focus-why">${escapeHTML(whyRanked(item))}</p>
            ${lead ? repoHelp(item, 2) : ''}
            <div class="focus-actions">
              ${primaryHref ? `<a class="${lead ? 'primary-button' : 'ghost-button'}" href="${escapeHTML(primaryHref)}" target="_blank" rel="noopener noreferrer">${lead ? '↗ ' : ''}${openLabel}</a>` : ''}
              ${lead ? secondary : ''}
              ${actionButtons(item, !lead)}
            </div>
          </li>`;
        }).join('');
        bindActions(list);
      }
      function freshItems() {
        return resources.filter(isNew).sort((a, b) => dateValue(b) - dateValue(a));
      }
      function renderSince() {
        const fresh = freshItems();
        el('since-count').textContent = count(fresh.length);
        const since = caughtUpAt ? `since you caught up (${formatDateTime(caughtUpAt)})` : 'in the last 24 hours (press Caught up to start counting from now)';
        el('since-summary').textContent = fresh.length ? `${count(fresh.length)} new ${since}` : `Nothing new ${since}`;
        const list = el('since-list');
        if (!fresh.length) {
          const latest = resources.reduce((max, item) => item.status === 'relevant' ? Math.max(max, dateValue(item)) : max, 0);
          list.innerHTML = `<li class="brief-empty">${latest ? `Latest relevant share was ${escapeHTML(timeAgo(latest))}. ` : ''}Next scan at ${escapeHTML(formatClock(nextScan(Date.now())))}.</li>`;
          el('since-foot').hidden = true;
          return;
        }
        list.innerHTML = fresh.slice(0, SINCE_LIMIT).map(briefItem).join('');
        bindActions(list);
        el('since-foot').hidden = fresh.length <= SINCE_LIMIT;
        el('since-more').textContent = `Show all ${count(fresh.length)} in the stream →`;
      }
      // Lanes are one compact chip row; the records themselves live one click
      // away in the filtered stream, so nothing is hidden — just not expanded.
      function renderLaneChips() {
        const totals = briefing.laneTotals || {};
        const container = el('lane-chips');
        const lanes = ['try', 'learn', 'read', 'reference', 'other'];
        container.innerHTML = '<span class="soft-note">Lanes:</span>' + lanes.map((lane) => {
          const total = Number(totals[lane] || 0);
          return `<button class="lane-chip" type="button" data-lane="${escapeHTML(lane)}" title="Open all ${escapeHTML(typeLabels[lane] || lane)} resources in the stream"><span class="type-chip ${escapeHTML(lane)}">${escapeHTML((typeLabels[lane] || lane).split(' ')[0])}</span><span class="n">${escapeHTML(count(total))}</span></button>`;
        }).join('');
        container.querySelectorAll('[data-lane]').forEach((button) => button.addEventListener('click', () => {
          Object.assign(state, { type: button.dataset.lane, status: 'relevant', unseen: false, visible: PAGE_SIZE });
          renderStream(); scrollToStream();
        }));
      }
      const tooltip = el('tooltip');
      function showTooltip(target, text) { const rect = target.getBoundingClientRect(); tooltip.textContent = text; tooltip.hidden = false; tooltip.style.left = `${rect.left + rect.width / 2}px`; tooltip.style.top = `${rect.top}px`; }
      function hideTooltip() { tooltip.hidden = true; }
      function renderPulse() {
        const today = activity.length ? Number(activity[activity.length - 1].relevant || 0) : 0;
        const week = activity.slice(-7).reduce((sum, day) => sum + Number(day.relevant || 0), 0);
        const counts = status.status_counts || {};
        const pending = Number(counts.pending_review || 0) + Number(counts.pending_hydration || 0);
        el('pulse-line').innerHTML = `<span><strong>${escapeHTML(count(today))}</strong> relevant today</span><span><strong>${escapeHTML(count(week))}</strong> in 7 days</span><span><strong>${escapeHTML(count(counts.relevant))}</strong> relevant of ${escapeHTML(count(resources.length))}</span><span class="spacer"></span>
          <div class="ledger" role="group" aria-label="Show a classification status in the stream">
            <button type="button" class="relevant" data-ledger="relevant" aria-pressed="false"><i aria-hidden="true"></i><strong>${escapeHTML(count(counts.relevant))}</strong><span class="sr">relevant</span></button>
            <button type="button" class="irrelevant" data-ledger="irrelevant" aria-pressed="false"><i aria-hidden="true"></i><strong>${escapeHTML(count(counts.irrelevant))}</strong><span class="sr">irrelevant</span></button>
            <button type="button" class="pending" data-ledger="pending" aria-pressed="false"><i aria-hidden="true"></i><strong>${escapeHTML(count(pending))}</strong><span class="sr">pending</span></button>
            <button type="button" class="unavailable" data-ledger="unavailable" aria-pressed="false"><i aria-hidden="true"></i><strong>${escapeHTML(count(counts.unavailable))}</strong><span class="sr">unavailable</span></button>
          </div>`;
        el('pulse-line').querySelectorAll('[data-ledger]').forEach((button) => { button.title = `Show ${button.dataset.ledger} in the stream`; button.addEventListener('click', () => { setStatus(button.dataset.ledger); scrollToStream(); }); });
        const chart = el('pulse-chart');
        const max = Math.max(1, ...activity.map((day) => Number(day.relevant || 0)));
        const todayKey = activity.length ? activity[activity.length - 1].day : '';
        chart.innerHTML = activity.map((day) => {
          const value = Number(day.relevant || 0);
          const height = Math.max(4, Math.round(value * 100 / max));
          const label = `${dayOnly.format(Date.parse(day.day + 'T12:00:00'))} · ${value} relevant · ${Number(day.total || 0)} shared`;
          return `<div class="pulse-bar ${day.day === todayKey ? 'today' : ''}" style="height:${height}%" data-tip="${escapeHTML(label)}" tabindex="0" aria-label="${escapeHTML(label)}"></div>`;
        }).join('');
        chart.querySelectorAll('.pulse-bar').forEach((bar) => {
          bar.addEventListener('mouseenter', () => showTooltip(bar, bar.dataset.tip));
          bar.addEventListener('focus', () => showTooltip(bar, bar.dataset.tip));
          bar.addEventListener('mouseleave', hideTooltip);
          bar.addEventListener('blur', hideTooltip);
        });
        el('pulse-start').textContent = activity.length ? dayOnly.format(Date.parse(activity[0].day + 'T12:00:00')) : '';
      }

      // Integration repair (Chat 07, 2026-09-07): pick the per-document
      // revision integer out of the read-back payload's revision object.
      function revisionFor(name) {
        if (decisions.readback !== 'ok') return null;
        const rev = decisions.revision;
        if (rev == null) return null;
        if (typeof rev === 'number') return Number.isInteger(rev) && rev >= 0 ? rev : null;
        const value = rev[name];
        return typeof value === 'number' && Number.isInteger(value) && value >= 0 ? value : null;
      }

      // ---- review eligibility (A09) --------------------------------------------
      // Provider 04's additive review_eligibility block wins when present.
      // Without it, eligibility is DERIVED and labeled as such: GitHub facts →
      // review; failed/missing facts or non-repository targets → a visible
      // evidence-pending lane with the reason, never silent exclusion and
      // never invented facts. The user may always decide anyway.
      function eligibilityOf(tool) {
        const provided = tool.review_eligibility;
        if (provided && provided.lane) return provided;
        const facts = tool.facts;
        if (facts && facts.ok) {
          return { lane: 'review', reasons: [], derived: true,
                   evidence: { source_url: tool.url || '', checked_at: facts.checked_at || '', extraction_state: 'ok', confidence: 'medium' },
                   project_fit: null };
        }
        if (facts && !facts.ok) {
          return { lane: 'evidence_pending', reasons: ['GitHub check failed — no facts available; will retry'], derived: true,
                   evidence: { source_url: tool.url || '', checked_at: facts.checked_at || null, extraction_state: 'failed', confidence: 'low' },
                   project_fit: null };
        }
        if (tool.is_repo) {
          return { lane: 'evidence_pending', reasons: ['GitHub facts not fetched yet'], derived: true, evidence: null, project_fit: null };
        }
        return { lane: 'evidence_pending', reasons: ['destination page not fetched yet (needs the safe-fetch provider)'], derived: true, evidence: null, project_fit: null };
      }
      function queuePools() {
        const undecided = tools.filter((t) => effVerdict(t) === 'unreviewed' && Number(t.mentions || 0) > 0 && !queueSkipped[t.key]);
        const pools = { review: [], evidence_pending: [], blocked: [] };
        for (const t of undecided) {
          const lane = eligibilityOf(t).lane;
          (pools[lane] || pools.evidence_pending).push(t);
        }
        for (const lane of Object.keys(pools)) pools[lane].sort((a, b) => Number(b.best_score || 0) - Number(a.best_score || 0));
        return pools;
      }
      function evidenceLine(tool) {
        const eligibility = eligibilityOf(tool);
        const facts = tool.facts || {};
        const bits = [];
        if (facts.ok) {
          if (facts.stars != null) bits.push(`<span class="metric-chip">★ ${escapeHTML(compact(facts.stars))}</span>`);
          if (facts.license) bits.push(escapeHTML(facts.license));
          if (facts.pushed_at) bits.push(`pushed ${idtext(escapeHTML(facts.pushed_at))}`);
          if (facts.language) bits.push(escapeHTML(facts.language));
        }
        const ev = eligibility.evidence;
        if (ev && ev.extraction_state) {
          bits.push(`<span class="ev-state ${escapeHTML(ev.extraction_state)}">evidence ${escapeHTML(ev.extraction_state)}</span>`);
          if (ev.confidence) bits.push(`confidence ${escapeHTML(ev.confidence)}`);
          if (ev.checked_at) bits.push(`checked ${idtext(escapeHTML(String(ev.checked_at)))}`);
        } else if (!facts.ok) {
          bits.push('<span class="ev-state pending">no evidence yet</span>');
        }
        if (eligibility.derived) bits.push('<span title="Estimated by this page from available facts; the evidence pipeline has not labeled this item yet">estimated</span>');
        bits.push(`${count(tool.mentions)} mention${Number(tool.mentions) === 1 ? '' : 's'}`);
        if (tool.lane) bits.push(escapeHTML(tool.lane));
        if (tool.best_score) bits.push(`score ${escapeHTML(Number(tool.best_score).toFixed(1))}`);
        return `<div class="evidence-line">${bits.join('<span aria-hidden="true">·</span>')}</div>`;
      }
      function fitLine(tool) {
        const fit = (tool.review_eligibility || {}).project_fit;
        if (!fit) return '';
        const parts = [];
        if (fit.project) parts.push(`<strong>${escapeHTML(fit.project)}</strong>`);
        if (fit.benefit) parts.push(escapeHTML(fit.benefit));
        if (fit.first_step) parts.push(`first step: ${escapeHTML(fit.first_step)}`);
        if (fit.success_measure) parts.push(`success = ${escapeHTML(fit.success_measure)}`);
        if (!parts.length) return '';
        // Generated proposal, distinct from an authored verdict.
        return `<p class="fit-line" dir="auto"><span class="fit-tag" title="Generated suggestion from the recommendation pipeline — becomes real only when you decide">suggested fit</span>${parts.join(' · ')}</p>`;
      }
      function decideButtons(tool, small) {
        const key = lk(tool.key);
        const busy = savingKeys.has(key);
        const size = small ? ' small' : '';
        const saving = busy ? ' disabled aria-busy="true"' : '';
        const primary = (tool.resource_type === 'read' || tool.resource_type === 'learn')
          ? `<button class="${small ? 'ghost-button small' : 'primary-button'}" type="button" data-decide="must_read" data-key="${escapeHTML(tool.key)}"${saving}>${busy ? 'Saving…' : 'Must read'}</button>`
          : `<button class="${small ? 'ghost-button small' : 'primary-button'}" type="button" data-decide="must_try" data-key="${escapeHTML(tool.key)}"${saving}>${busy ? 'Saving…' : 'Must try'}</button>`;
        return `${primary}
          <button class="ghost-button${size}" type="button" data-decide="excluded" data-key="${escapeHTML(tool.key)}"${saving}>Not for me</button>
          <button class="ghost-button${size}" type="button" data-decide="already_have" data-key="${escapeHTML(tool.key)}"${saving}>Already have</button>`;
      }
      function bindDecides(root) {
        root.querySelectorAll('[data-decide]').forEach((button) => button.addEventListener('click', () => {
          const tool = toolsByKey.get(lk(button.dataset.key));
          if (tool) submitVerdict(tool, button.dataset.decide);
        }));
        root.querySelectorAll('[data-skip]').forEach((button) => button.addEventListener('click', () => {
          const tool = toolsByKey.get(lk(button.dataset.skip));
          if (tool) skipForNow(tool);
        }));
      }
      function pendingRow(tool, blocked) {
        const eligibility = eligibilityOf(tool);
        const url = safeURL(tool.url);
        const reasons = (eligibility.reasons || []).map((reason) => escapeHTML(reason)).join(' · ');
        return `<li class="brief-item">
          <h3 class="brief-title" dir="auto">${url ? `<a href="${escapeHTML(url)}" target="_blank" rel="noopener noreferrer">${escapeHTML(tool.name)}</a>` : escapeHTML(tool.name)}</h3>
          <div class="brief-actions">${blocked ? '' : decideButtons(tool, true)}</div>
          <div class="brief-meta"><span class="queue-reason">${reasons || 'no reason recorded'}</span>${eligibility.derived ? '<span title="Estimated by this page, not yet labeled by the evidence pipeline">estimated</span>' : ''}${blocked ? decideButtons(tool, true) : ''}</div>
        </li>`;
      }
      function renderQueue() {
        const pools = queuePools();
        const review = pools.review;
        const visible = review.slice(0, QUEUE_BATCH);
        el('queue-count').textContent = count(review.length);
        const skippedCount = Object.keys(queueSkipped).length;
        el('queue-note').innerHTML = skippedCount
          ? `${escapeHTML(count(skippedCount))} skipped on this Mac · <button class="ghost-button small" type="button" id="unskip-all">Unskip</button>`
          : '';
        const unskip = el('unskip-all');
        if (unskip) unskip.addEventListener('click', unskipAll);
        const list = el('queue-list');
        if (!visible.length) {
          list.innerHTML = `<li class="brief-empty"><strong>Queue is clear.</strong>${pools.evidence_pending.length ? `${count(pools.evidence_pending.length)} item${pools.evidence_pending.length === 1 ? ' is' : 's are'} waiting for evidence below — you can decide them anyway.` : 'Nothing is waiting on your decision. New tools appear here after the next scan enriches them.'}</li>`;
        } else {
          const remaining = review.length - visible.length;
          list.innerHTML = visible.map((t) => {
            const url = safeURL(t.url);
            const what = cleanText((t.facts || {}).description || t.what || '');
            return `<li class="queue-item">
              <h3 class="queue-name" dir="auto"><span class="type-chip ${escapeHTML(t.resource_type || 'other')}">${escapeHTML(((typeLabels[t.resource_type] || t.resource_type || 'other')).split(' ')[0])}</span> ${url ? `<a href="${escapeHTML(url)}" target="_blank" rel="noopener noreferrer">${escapeHTML(t.name)}</a>` : escapeHTML(t.name)}</h3>
              ${what ? `<p class="queue-what" dir="auto">${escapeHTML(what)}</p>` : ''}
              ${evidenceLine(t)}
              ${fitLine(t)}
              <div class="queue-actions">
                ${url ? `<a class="ghost-button" href="${escapeHTML(url)}" target="_blank" rel="noopener noreferrer">↗ Look</a>` : ''}
                ${decideButtons(t, false)}
                <button class="ghost-button small" type="button" data-skip="${escapeHTML(t.key)}" title="Hide from this queue on this Mac only; it stays under Not reviewed">Skip for now</button>
              </div>
            </li>`;
          }).join('') + (remaining > 0 ? `<li class="brief-empty">${count(remaining)} more waiting — decide these three first.</li>` : '');
        }
        bindDecides(list);
        const pendingWrap = el('queue-pending-wrap');
        el('queue-pending-count').textContent = count(pools.evidence_pending.length);
        pendingWrap.hidden = pools.evidence_pending.length === 0;
        el('queue-pending-list').innerHTML = pools.evidence_pending.slice(0, 20).map((t) => pendingRow(t, false)).join('')
          + (pools.evidence_pending.length > 20 ? `<li class="brief-empty">${count(pools.evidence_pending.length - 20)} more under Not reviewed in Tools &amp; verdicts.</li>` : '');
        bindDecides(el('queue-pending-list'));
        const blockedWrap = el('queue-blocked-wrap');
        el('queue-blocked-count').textContent = count(pools.blocked.length);
        blockedWrap.hidden = pools.blocked.length === 0;
        el('queue-blocked-list').innerHTML = pools.blocked.slice(0, 20).map((t) => pendingRow(t, true)).join('');
        bindDecides(el('queue-blocked-list'));
        updateNavCounts();
      }
      function updateNavCounts() {
        const pools = queuePools();
        const navDecide = el('nav-decide-count');
        navDecide.textContent = count(pools.review.length);
        navDecide.classList.toggle('hot', pools.review.length > 0);
        const fresh = freshItems().length;
        const navNew = el('nav-new-count');
        navNew.textContent = count(fresh);
        navNew.classList.toggle('hot', fresh > 0);
      }

      // ---- tools & verdicts backlog --------------------------------------------
      function outcomeControls(tool) {
        const key = lk(tool.key);
        const outcome = effOutcome(tool);
        const busy = savingKeys.has(key);
        const draft = openOutcomeForms.get(key);
        const button = (value, label) => `<button class="ghost-button small${outcome.state === value ? ' is-current' : ''}" type="button" data-outcome="${escapeHTML(value)}" data-okey="${escapeHTML(tool.key)}"${outcome.state === value ? ' aria-pressed="true"' : ' aria-pressed="false"'}${busy ? ' disabled' : ''}>${label}</button>`;
        let form = '';
        if (draft) {
          form = `<div class="outcome-form" data-oform="${escapeHTML(tool.key)}">
            <div class="of-row">
              <label>baseline (before)<input type="text" data-of="baseline" value="${escapeHTML(draft.baseline || '')}" placeholder="e.g. listing takes 3 h"></label>
              <label>result (after)<input type="text" data-of="result" value="${escapeHTML(draft.result || '')}" placeholder="e.g. 40 min with the tool"></label>
            </div>
            <label>note<input type="text" data-of="note" value="${escapeHTML(draft.note || '')}" placeholder="what happened, worth keeping?"></label>
            <div class="of-actions">
              <button class="primary-button" type="button" data-osave="${escapeHTML(tool.key)}"${busy ? ' disabled aria-busy="true"' : ''}>${busy ? 'Saving…' : `Save ${escapeHTML(OUTCOME_LABEL[draft.state] || draft.state)}`}</button>
              <button class="ghost-button" type="button" data-ocancel="${escapeHTML(tool.key)}">Cancel</button>
              <span class="soft-note">baseline/result are optional — they go into the outcome note</span>
            </div>
          </div>`;
        }
        return `<div class="outcome-row">
          <span class="label">${outcome.state ? 'Outcome:' : 'Did you try it? Record what actually happened:'}</span>
          ${button('trying', 'Trying')}${button('kept', 'Kept')}${button('dropped', 'Dropped')}
          ${outcome.state ? `<button class="ghost-button small" type="button" data-outcome="clear" data-okey="${escapeHTML(tool.key)}"${busy ? ' disabled' : ''}>Clear</button>` : ''}
        </div>${outcome.note && !draft ? `<p class="outcome-note" dir="auto">${escapeHTML(outcome.note)}</p>` : ''}${form}`;
      }
      function composeOutcomeNote(draft) {
        const marks = [];
        if (cleanText(draft.baseline)) marks.push(`baseline: ${cleanText(draft.baseline)}`);
        if (cleanText(draft.result)) marks.push(`result: ${cleanText(draft.result)}`);
        const head = marks.length ? `[${marks.join(' → ')}] ` : '';
        return (head + cleanText(draft.note || '')).trim();
      }
      function bindOutcomes(root) {
        root.querySelectorAll('[data-outcome]').forEach((button) => button.addEventListener('click', () => {
          const tool = toolsByKey.get(lk(button.dataset.okey));
          if (!tool) return;
          const value = button.dataset.outcome;
          const key = lk(tool.key);
          if (value === 'clear') { submitOutcome(tool, 'clear', ''); return; }
          if (value === 'trying') { openOutcomeForms.delete(key); submitOutcome(tool, 'trying', effOutcome(tool).note || ''); return; }
          openOutcomeForms.set(key, { state: value, baseline: '', result: '', note: '' });
          renderTools();
          const form = document.querySelector(`[data-oform="${CSS.escape(tool.key)}"] input`);
          if (form) form.focus();
        }));
        root.querySelectorAll('[data-oform]').forEach((form) => form.querySelectorAll('[data-of]').forEach((input) => input.addEventListener('input', () => {
          const draft = openOutcomeForms.get(lk(form.dataset.oform));
          if (draft) draft[input.dataset.of] = input.value;
        })));
        root.querySelectorAll('[data-osave]').forEach((button) => button.addEventListener('click', () => {
          const tool = toolsByKey.get(lk(button.dataset.osave));
          const draft = openOutcomeForms.get(lk(button.dataset.osave));
          if (tool && draft) submitOutcome(tool, draft.state, composeOutcomeNote(draft));
        }));
        root.querySelectorAll('[data-ocancel]').forEach((button) => button.addEventListener('click', () => {
          openOutcomeForms.delete(lk(button.dataset.ocancel));
          renderTools();
        }));
      }

      function renderTools() {
        const counts = { must_try: 0, must_read: 0, excluded: 0, already_have: 0, unreviewed: 0, all: tools.length };
        for (const t of tools) { const v = effVerdict(t); if (counts[v] != null) counts[v] += 1; }
        document.querySelectorAll('#tool-tabs .tool-tab').forEach((tab) => {
          const key = tab.dataset.verdict;
          tab.setAttribute('aria-pressed', key === toolVerdict ? 'true' : 'false');
          const base = key === 'all' ? 'All tools' : VERDICT_LABEL[key];
          tab.textContent = `${base} (${count(counts[key] || 0)})`;
        });
        let list = toolVerdict === 'all' ? tools.slice() : tools.filter((t) => effVerdict(t) === toolVerdict);
        if (toolQuery) {
          const q = toolQuery;
          list = list.filter((t) => normalizeArabic([t.name, t.key, t.label, t.what, effWhy(t), t.lane].join(' ')).includes(q));
        }
        if (toolVerdict === 'must_try') list.sort((a, b) => (effRank(a) ?? 1e9) - (effRank(b) ?? 1e9));
        el('tools-count').textContent = count(list.length);
        // The accountability numbers: a recommendation only matters if it gets
        // tried, so adoption sits in the summary, visible while collapsed.
        const mustTry = tools.filter((t) => effVerdict(t) === 'must_try');
        const tried = mustTry.filter((t) => effOutcome(t).state).length;
        const kept = mustTry.filter((t) => effOutcome(t).state === 'kept').length;
        el('tools-adoption').innerHTML = mustTry.length
          ? `<strong>${escapeHTML(count(mustTry.length))}</strong> must try · <strong>${escapeHTML(count(tried))}</strong> tried · <strong>${escapeHTML(count(kept))}</strong> kept`
          : '';
        const syncBits = [];
        if (coverage.days) syncBits.push(`${count(coverage.relevant)} relevant over ${count(coverage.days)} days`);
        if (LIVE && decisions.readback === 'unavailable' && decisions.source === 'local') syncBits.push('saved decisions apply everywhere after the next export');
        el('tools-coverage').textContent = syncBits.join(' · ');
        const visible = (toolsExpanded || toolQuery) ? list : list.slice(0, TOOLS_COLLAPSED);
        const node = el('tool-list');
        if (!visible.length) {
          node.innerHTML = `<li class="brief-empty">${toolQuery ? 'No tool matches that search.' : 'Nothing in this group yet.'}</li>`;
        } else {
          node.innerHTML = visible.map((t) => {
            const url = safeURL(t.url);
            const name = escapeHTML(t.name || t.key);
            const verdict = effVerdict(t);
            const outcome = effOutcome(t);
            const isMust = verdict === 'must_try';
            const rank = effRank(t);
            const stats = [];
            if (t.stars) stats.push(`${compact(t.stars)}★`);
            if (t.license) stats.push(escapeHTML(t.license));
            if (t.last_push) stats.push(`pushed ${idtext(escapeHTML(t.last_push))}`);
            if (t.lane) stats.push(escapeHTML(t.lane));
            stats.push(`${count(t.mentions)} mention${Number(t.mentions) === 1 ? '' : 's'} in the group`);
            if (t.auto && t.reason_code) stats.push(`auto-excluded: ${escapeHTML(t.reason_code)}`);
            const why = cleanText(effWhy(t));
            const detail = [
              why ? `<p class="tool-why" dir="auto">${escapeHTML(why)}</p>` : '',
              t.first_step ? `<p class="tool-step">▸ ${escapeHTML(t.first_step)}</p>` : '',
            ].join('');
            return `<li class="tool-row ${isMust ? 'is-must' : ''}">
              <div class="tool-head">
                <span class="tool-rank">${rank != null ? escapeHTML(String(rank)) : ''}</span>
                <span class="verdict-chip ${escapeHTML(verdict)}">${escapeHTML(VERDICT_LABEL[verdict] || verdict)}</span>
                ${t.auto ? '<span class="state-chip" title="Excluded automatically by policy (stale/tiny/archived/gone/empty); any hand verdict overrides it">auto</span>' : ''}
                ${outcome.state ? `<span class="outcome-chip ${escapeHTML(outcome.state)}">${escapeHTML(OUTCOME_LABEL[outcome.state] || outcome.state)}</span>` : ''}
                <h3 class="tool-name" dir="auto">${url ? `<a href="${escapeHTML(url)}" target="_blank" rel="noopener noreferrer">${name}</a>` : name}</h3>
              </div>
              ${t.what ? `<p class="tool-what" dir="auto">${escapeHTML(t.what)}</p>` : ''}
              ${detail ? `<details class="tool-more"><summary>why + first step</summary>${detail}</details>` : ''}
              <div class="tool-meta">${stats.join('<span aria-hidden="true">·</span>')}</div>
              <div class="tool-actions">
                ${url ? `<a class="${isMust ? 'primary-button' : 'ghost-button'}" href="${escapeHTML(url)}" target="_blank" rel="noopener noreferrer">${isMust ? '↗ ' : ''}Open ${escapeHTML((t.label || '').split('/')[0] || 'link')}</a>` : ''}
                ${t.mentions ? `<button class="ghost-button small" type="button" data-tool-posts="${escapeHTML(t.key)}">Show ${count(t.mentions)} post${Number(t.mentions) === 1 ? '' : 's'}</button>` : '<span class="soft-note">named in a post, not linked — nothing to open in the stream</span>'}
                ${verdict !== 'unreviewed' && !t.auto ? `<button class="ghost-button small danger" type="button" data-decide="clear" data-key="${escapeHTML(t.key)}" title="Remove the verdict; the trial record (outcome) is kept separately">Clear verdict</button>` : ''}
              </div>
              ${isMust ? outcomeControls(t) : (outcome.state ? `<div class="outcome-row"><span class="label">Outcome:</span><span class="outcome-chip ${escapeHTML(outcome.state)}">${escapeHTML(OUTCOME_LABEL[outcome.state] || outcome.state)}</span>${outcome.note ? `<span class="outcome-note" dir="auto">${escapeHTML(outcome.note)}</span>` : ''}</div>` : '')}
            </li>`;
          }).join('');
          bindOutcomes(node);
          bindDecides(node);
          node.querySelectorAll('[data-tool-posts]').forEach((button) => button.addEventListener('click', () => {
            Object.assign(state, { status: 'all', source: 'all', type: 'all', handled: 'show', unseen: false, sender: 'all', project: 'all', query: button.dataset.toolPosts.toLowerCase(), visible: PAGE_SIZE });
            el('search-input').value = button.dataset.toolPosts;
            renderStream(); scrollToStream();
          }));
        }
        const foot = el('tools-foot');
        foot.hidden = Boolean(toolQuery) || list.length <= TOOLS_COLLAPSED;
        el('tools-more').textContent = toolsExpanded ? 'Show fewer' : `Show all ${count(list.length)} →`;
      }
      document.querySelectorAll('#tool-tabs .tool-tab').forEach((tab) => tab.addEventListener('click', () => {
        toolVerdict = tab.dataset.verdict; toolsExpanded = false; renderTools();
      }));
      el('tool-search').addEventListener('input', (event) => {
        toolQuery = normalizeArabic(event.target.value.trim());
        if (toolQuery) toolVerdict = 'all';
        renderTools();
      });
      el('tools-more').addEventListener('click', () => { toolsExpanded = !toolsExpanded; renderTools(); });

      // Learned exclusion rules. Proposals only — approving one is a deliberate
      // act, and it then applies solely to items no positive rule matched.
      function renderNegativeProposals() {
        const card = el('rules-card');
        const dismissed = readStore(storeKey('rulesDismissed'), {}) || {};
        const proposals = (data.negativeProposals || []).filter((p) => !dismissed[p.term]);
        card.hidden = proposals.length === 0;
        if (!proposals.length) return;
        el('rules-count').textContent = count(proposals.length);
        const node = el('rules-list');
        node.innerHTML = proposals.slice(0, 8).map((p) => `<li class="brief-item">
          <h3 class="brief-title" dir="auto">“${escapeHTML(p.term)}”</h3>
          <div class="brief-actions">
            <button class="ghost-button small" type="button" data-rule-add="${escapeHTML(p.term)}">Always exclude</button>
            <button class="ghost-button small" type="button" data-rule-skip="${escapeHTML(p.term)}">Not a rule</button>
          </div>
          <div class="brief-meta">${escapeHTML(p.evidence)}<span aria-hidden="true">·</span>strength ${escapeHTML(String(p.log_odds))}</div>
        </li>`).join('');
        node.querySelectorAll('[data-rule-add]').forEach((button) => button.addEventListener('click', async () => {
          const term = button.dataset.ruleAdd;
          if (!LIVE) { staticModeToast(); return; }
          const result = await postAction('api/negative-term', 'negative-term', { term, action: 'add' });
          if (!result.ok) { showToast(result.body.error || 'Could not approve that rule.'); announce(result.body.error || 'Could not approve that rule.', true); return; }
          dismissed[term] = Date.now();
          writeStore(storeKey('rulesDismissed'), dismissed);
          renderNegativeProposals();
          showToast(`“${term}” will be excluded from the next scan on.`);
        }));
        node.querySelectorAll('[data-rule-skip]').forEach((button) => button.addEventListener('click', () => {
          dismissed[button.dataset.ruleSkip] = Date.now();
          writeStore(storeKey('rulesDismissed'), dismissed);
          renderNegativeProposals();
        }));
      }

      function renderBriefing() { renderQueue(); renderFocus(); renderTools(); renderNegativeProposals(); renderSince(); renderLaneChips(); renderPulse(); }
      function renderDecisionsUI() { renderQueue(); renderTools(); renderFocus(); renderStream(); }

      // ---- rail breakdowns ------------------------------------------------------
      function fillSelect(select, options, current) {
        const keep = select.firstElementChild ? select.firstElementChild.outerHTML : '';
        select.innerHTML = keep + options.map(([value, label]) => `<option value="${escapeHTML(value)}">${escapeHTML(label)}</option>`).join('');
        select.value = [...select.options].some((option) => option.value === current) ? current : 'all';
      }
      function renderRail() {
        fillSelect(el('type-filter'), ['try', 'learn', 'read', 'reference', 'other'].map((key) => [key, `${typeLabels[key] || key} (${count(resources.filter((item) => item.status === 'relevant' && typeOf(item) === key).length)})`]), state.type);
        fillSelect(el('project-filter'), Object.entries(projectAreas).sort((a, b) => a[1].localeCompare(b[1])), state.project);
        fillSelect(el('sender-filter'), [...senders].sort((a, b) => Number(b.message_count || 0) - Number(a.message_count || 0)).map((sender) => {
          const label = sender.username ? '@' + String(sender.username).replace(/^@/, '') : sender.display_name || sender.sender_id;
          return [String(sender.sender_id), `${label} (${count(sender.resource_count)})`];
        }), state.sender);
        state.type = el('type-filter').value; state.project = el('project-filter').value; state.sender = el('sender-filter').value;

        const relevantResources = resources.filter((item) => item.status === 'relevant');
        const projectCounts = {};
        relevantResources.forEach((item) => (item.project_areas || []).forEach((area) => { projectCounts[area] = (projectCounts[area] || 0) + 1; }));
        const topProjects = Object.entries(projectCounts).sort((a, b) => b[1] - a[1]).slice(0, 8);
        const maxProject = topProjects.length ? topProjects[0][1] : 1;
        el('project-breakdown').innerHTML = topProjects.length ? topProjects.map(([key, value]) => `
          <div class="breakdown-row">
            <div class="breakdown-meta"><span class="breakdown-name" title="${escapeHTML(projectAreas[key] || key)}">${escapeHTML(projectAreas[key] || key)}</span><span class="breakdown-count">${count(value)}</span></div>
            <div class="breakdown-bar"><div class="breakdown-fill" style="width:${value * 100 / maxProject}%"></div></div>
          </div>`).join('') : '<span class="source-meta">No relevant project matches yet.</span>';

        const topSenders = [...senders].sort((a, b) => Number(b.resource_count || 0) - Number(a.resource_count || 0)).slice(0, 6);
        el('sender-breakdown').innerHTML = topSenders.map((sender) => {
          const username = sender.username ? '@' + String(sender.username).replace(/^@/, '') : sender.display_name || sender.sender_id;
          const avatar = safeURL(sender.avatar_url);
          const fallback = escapeHTML(String(username).replace('@', '').slice(0, 1).toUpperCase() || '?');
          return `<div class="sender-row">
            ${avatar ? `<img class="sender-avatar" src="${escapeHTML(avatar)}" alt="" loading="lazy" decoding="async" onerror="this.replaceWith(Object.assign(document.createElement('span'),{className:'sender-fallback',textContent:'${fallback}'}))">` : `<span class="sender-fallback" aria-hidden="true">${fallback}</span>`}
            <div class="sender-copy"><div class="sender-name"><bdi>${escapeHTML(username)}</bdi></div><div class="sender-volume">${count(sender.message_count)} messages</div></div>
            <span class="sender-count">${count(sender.resource_count)}</span>
          </div>`;
        }).join('');

        const coverageRows = [
          ['All messages', status.messages_captured || 0],
          ['Owner messages', status.owner_messages_captured || 0],
          ['Other members', status.non_owner_messages_captured || 0],
          ['Unique resources', resources.length]
        ];
        const coverageMax = Math.max(1, ...coverageRows.map((row) => Number(row[1] || 0)));
        el('coverage-breakdown').innerHTML = coverageRows.map(([label, value]) => `
          <div class="breakdown-row">
            <div class="breakdown-meta"><span class="breakdown-name">${escapeHTML(label)}</span><span class="breakdown-count">${count(value)}</span></div>
            <div class="breakdown-bar"><div class="breakdown-fill" style="width:${Number(value || 0) * 100 / coverageMax}%"></div></div>
          </div>`).join('');
      }

      // ---- stream ---------------------------------------------------------------
      function matches(item) {
        const itemStatus = displayStatus(item.status);
        if (state.status !== 'all' && itemStatus !== state.status) return false;
        if (state.source !== 'all' && (item.source || 'group') !== state.source) return false;
        if (state.type !== 'all' && typeOf(item) !== state.type) return false;
        const handledNow = isHandled(item);
        if (state.handled === 'hide' && handledNow) return false;
        if (state.handled === 'only' && !handledNow) return false;
        if (state.unseen && !isNew(item)) return false;
        if (state.sender !== 'all' && !(item.sharer_ids || [String(item.sender_id)]).map(String).includes(state.sender)) return false;
        if (state.project !== 'all' && !(item.project_areas || []).includes(state.project)) return false;
        if (state.query) {
          // Every linked tool is searchable, not just the first one shown as a
          // chip — otherwise a tool buried at link #12 of a thread is unfindable.
          const haystack = normalizeArabic([item.title, item.text, item.url, item.author, item.sender_username, item.sender_display_name, item.external_label, (item.verdict || {}).name, ...(item.tool_keys || []), ...(item.external_urls || []), ...(item.reasons || []), ...(item.project_areas || []), ...(item.sharers || [])].join(' '));
          if (!haystack.includes(state.query)) return false;
        }
        return true;
      }
      function sorted(items) {
        return [...items].sort((a, b) => {
          if (state.sort === 'oldest') return dateValue(a) - dateValue(b);
          if (state.sort === 'pick') return Number(b.pick_score || 0) - Number(a.pick_score || 0) || dateValue(b) - dateValue(a);
          if (state.sort === 'score') return Number(b.score || 0) - Number(a.score || 0) || dateValue(b) - dateValue(a);
          if (state.sort === 'shares') return Number(b.share_count || 0) - Number(a.share_count || 0) || dateValue(b) - dateValue(a);
          return dateValue(b) - dateValue(a);
        });
      }
      function resourceHTML(item) {
        const itemStatus = displayStatus(item.status);
        const title = titleFor(item);
        const excerpt = excerptFor(item);
        const media = safeURL((item.media_urls || [])[0]);
        const url = safeURL(item.url);
        const areas = (item.project_areas || []).map((key) => `<span class="tag">${escapeHTML(projectAreas[key] || key)}</span>`).join('');
        const reasons = cleanText((item.reasons || []).join(' / '));
        const sender = senderLabel(item);
        const shareCount = Number(item.share_count || 1);
        const sourceAuthor = item.author ? ` / post by @${String(item.author).replace(/^@/, '')}` : '';
        const handledNow = handledState(item);
        const pick = item.pick_score != null && item.status === 'relevant' ? `<span class="share-note" title="${escapeHTML(whyRanked(item))}">pick ${escapeHTML(Number(item.pick_score).toFixed(1))}</span>` : '';
        return `<article class="resource-row ${handledNow ? 'handled' : ''}" data-status="${escapeHTML(itemStatus)}">
          <div class="status-stripe" aria-hidden="true"></div>
          <div class="resource-body ${media ? '' : 'no-media'}">
            <div class="resource-main">
              <div class="resource-kicker">
                <span class="status-label">${escapeHTML(itemStatus)}</span>
                ${chip(item)}
                ${verdictChip(item)}
                ${isNew(item) ? '<span class="state-chip new">new</span>' : ''}
                ${handledNow ? `<span class="state-chip">${handledNow === 'done' ? 'done' : 'not for me'}</span>` : ''}
                <span class="source-meta"><bdi>${escapeHTML(sender)}</bdi> / ${escapeHTML(formatDateTime(dateValue(item)))}${escapeHTML(sourceAuthor)}</span>
              </div>
              <h2 class="resource-title" dir="auto">${escapeHTML(title)}</h2>
              ${excerpt ? `<p class="resource-text" dir="auto">${escapeHTML(excerpt)}</p>` : ''}
              ${areas || linkChips(item, 6) ? `<div class="tag-row">${areas}${linkChips(item, 6)}</div>` : ''}
              ${reasons ? `<p class="reason" dir="auto">${escapeHTML(reasons)}</p>` : ''}
              <div class="resource-actions">
                ${url ? `<a class="source-link" href="${escapeHTML(url)}" target="_blank" rel="noopener noreferrer">Open source</a>` : '<span class="source-meta">Group note</span>'}
                ${Number(item.likes || 0) > 0 ? `<span class="share-note" title="Likes on X">♥ ${escapeHTML(compact(item.likes))}</span>` : ''}
                <span class="share-note">${shareCount > 1 ? `${count(shareCount)} shares by ${count(item.sharer_count || 1)} senders` : escapeHTML(item.kind || 'resource')}</span>
                ${pick}
                ${item.status === 'relevant' ? `<span class="brief-actions">${actionButtons(item, true)}</span>` : ''}
              </div>
            </div>
            ${media ? `<div class="media-box"><img src="${escapeHTML(media)}" alt="" loading="lazy" decoding="async" onerror="this.parentElement.classList.add('media-failed')"><span class="media-note">image unavailable</span></div>` : ''}
          </div>
        </article>`;
      }
      function syncControls() {
        document.querySelectorAll('#status-tabs [data-status]').forEach((button) => button.setAttribute('aria-pressed', button.dataset.status === state.status ? 'true' : 'false'));
        document.querySelectorAll('[data-ledger]').forEach((button) => button.setAttribute('aria-pressed', button.dataset.ledger === state.status ? 'true' : 'false'));
        el('source-filter').value = state.source;
        el('sender-filter').value = state.sender;
        el('project-filter').value = state.project;
        el('type-filter').value = state.type;
        el('handled-filter').value = state.handled;
        el('unseen-filter').setAttribute('aria-pressed', state.unseen ? 'true' : 'false');
        el('sort-order').value = state.sort;
        if (normalizeArabic(el('search-input').value.trim()) !== state.query) el('search-input').value = state.query;
        const active = [];
        if (state.source !== 'group') active.push(state.source === 'all' ? 'all sources' : state.source);
        if (state.type !== 'all') active.push(typeLabels[state.type] || state.type);
        if (state.project !== 'all') active.push(projectAreas[state.project] || state.project);
        if (state.sender !== 'all') { const sender = senders.find((entry) => String(entry.sender_id) === state.sender); active.push(sender && sender.username ? '@' + sender.username : 'sender'); }
        if (state.handled !== 'hide') active.push(state.handled === 'only' ? 'handled only' : 'incl. handled');
        if (state.unseen) active.push('new only');
        if (state.query) active.push(`“${state.query}”`);
        el('active-filters').innerHTML = active.map((label) => `<span>${escapeHTML(label)}</span>`).join('');
      }
      function renderStream() {
        const filtered = sorted(resources.filter(matches));
        const visible = filtered.slice(0, state.visible);
        el('results-count').textContent = `${count(filtered.length)} ${filtered.length === 1 ? 'resource' : 'resources'}`;
        el('match-count').textContent = count(filtered.length);
        const list = el('resource-list');
        list.innerHTML = visible.length ? visible.map(resourceHTML).join('') : '<div class="empty-state"><strong>No matching resources</strong><span>Change the active filters or search terms.</span></div>';
        list.dataset.renders = String((Number(list.dataset.renders) || 0) + 1);
        bindActions(list);
        el('load-more-wrap').hidden = visible.length >= filtered.length;
        el('load-more').textContent = `Show more (${count(filtered.length - visible.length)} remaining)`;
        syncControls();
        persistState();
      }
      function scrollToStream() { el('stream').scrollIntoView({ behavior: 'smooth', block: 'start' }); }
      function setStatus(value) { state.status = value; state.visible = PAGE_SIZE; renderStream(); }

      function renderAll() {
        document.title = `${data.groupName || 'X Group'} Resource Radar`;
        el('group-subtitle').textContent = `${data.groupName || 'X Group'} / all senders / AI and project filter`;
        el('fixture-flag').hidden = !/^fixture-/.test(String(data.conversationId || ''));
        const mode = el('mode-banner');
        if (!LIVE) {
          mode.innerHTML = '<span><strong>Read-only file view.</strong> Data is frozen at generation time and reloads every 15 minutes. Recording verdicts, outcomes and rules needs the served dashboard — run <code>python3 scripts/manage_radar_server.py install</code> once, then open <code>http://127.0.0.1:8765/</code>.</span>';
          mode.hidden = false;
        } else {
          mode.hidden = true;
        }
        const exports = el('export-links');
        if (LIVE) {
          const exportLabels = { 'relevant-sheet.csv': 'relevant-sheet.csv (spreadsheet-safe)', 'relevant.csv': 'relevant.csv (raw)' };
          exports.innerHTML = ['relevant-sheet.csv', 'relevant.csv', 'all-resources.csv', 'relevant.jsonl', 'latest.md', 'verification.json', 'negative-proposals.json']
            .map((name) => `<a href="${name}" download>${exportLabels[name] || name}</a>`).join('');
          exports.hidden = false;
        } else {
          exports.hidden = true;
        }
        const tz = (() => { try { return new Intl.DateTimeFormat('en-US', { timeZoneName: 'short' }).formatToParts(new Date()).find((part) => part.type === 'timeZoneName').value; } catch (_) { return ''; } })();
        el('footer-copy').textContent = `Generated ${formatDateTime(data.generatedAt)}${tz ? ' ' + tz : ''} from the durable local group ledger. Done, Not-for-me, Skip and Caught-up states are stored on this Mac only; verdicts and outcomes are saved to the radar itself.${LIVE ? ' Live: new data is applied when you press Apply or when the tab was hidden.' : ' Opened from disk: reloads every 15 minutes; run manage_radar_server.py install for in-place updates.'}`;
        renderPill();
        renderHealthStrip();
        renderBriefing();
        renderRail();
        renderStream();
      }

      // ---- toast ----------------------------------------------------------------
      let toastTimer = 0;
      let toastAction = null;
      function showToast(text, opts) {
        const toast = el('toast');
        el('toast-text').textContent = text;
        const actionButton = el('toast-action');
        if (opts && opts.label && typeof opts.onAction === 'function') {
          actionButton.textContent = opts.label;
          actionButton.hidden = false;
          toastAction = opts.onAction;
        } else {
          actionButton.hidden = true;
          toastAction = null;
        }
        toast.hidden = false;
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => { toast.hidden = true; }, opts && opts.label ? 15000 : 9000);
      }
      el('toast-action').addEventListener('click', () => { const run = toastAction; el('toast').hidden = true; toastAction = null; if (run) run(); });
      el('toast-close').addEventListener('click', () => { el('toast').hidden = true; toastAction = null; });

      // ---- controls -------------------------------------------------------------
      document.querySelectorAll('#status-tabs [data-status]').forEach((button) => button.addEventListener('click', () => setStatus(button.dataset.status)));
      // Debounced: typing must not re-render the whole list on every keystroke.
      let searchTimer = 0;
      el('search-input').addEventListener('input', (event) => {
        clearTimeout(searchTimer);
        const raw = event.target.value;
        searchTimer = setTimeout(() => {
          const query = normalizeArabic(raw.trim());
          if (query === state.query) return;
          state.query = query;
          state.visible = PAGE_SIZE;
          renderStream();
        }, 150);
      });
      el('search-input').addEventListener('keydown', (event) => { if (event.key === 'Enter') { event.preventDefault(); scrollToStream(); } });
      el('match-chip').addEventListener('click', scrollToStream);
      el('source-filter').addEventListener('change', (event) => { state.source = event.target.value; state.visible = PAGE_SIZE; renderStream(); });
      el('sender-filter').addEventListener('change', (event) => { state.sender = event.target.value; state.visible = PAGE_SIZE; renderStream(); });
      el('project-filter').addEventListener('change', (event) => { state.project = event.target.value; state.visible = PAGE_SIZE; renderStream(); });
      el('type-filter').addEventListener('change', (event) => { state.type = event.target.value; state.visible = PAGE_SIZE; renderStream(); });
      el('handled-filter').addEventListener('change', (event) => { state.handled = event.target.value; state.visible = PAGE_SIZE; renderStream(); });
      el('unseen-filter').addEventListener('click', () => {
        state.unseen = !state.unseen;
        if (state.unseen) state.status = 'relevant';
        state.visible = PAGE_SIZE;
        renderStream();
      });
      el('sort-order').addEventListener('change', (event) => { state.sort = event.target.value; renderStream(); });
      el('load-more').addEventListener('click', () => { state.visible += PAGE_SIZE; renderStream(); });
      el('reset-filters').addEventListener('click', () => {
        Object.assign(state, { status: 'all', source: 'group', sender: 'all', project: 'all', type: 'all', handled: 'hide', unseen: false, query: '', sort: 'latest', visible: PAGE_SIZE });
        el('search-input').value = '';
        renderStream();
      });
      el('since-more').addEventListener('click', () => { Object.assign(state, { status: 'relevant', unseen: true, type: 'all', handled: 'hide', visible: PAGE_SIZE }); renderStream(); scrollToStream(); });
      el('focus-stream').addEventListener('click', () => { Object.assign(state, { status: 'relevant', sort: 'pick', unseen: false, type: 'all', visible: PAGE_SIZE }); renderStream(); scrollToStream(); });
      el('focus-window-select').addEventListener('change', (event) => { focusWindow = Number(event.target.value) || 0; writeStore(storeKey('focusWindow'), focusWindow); renderFocus(); });
      el('caught-up').addEventListener('click', (event) => { event.preventDefault(); event.stopPropagation(); markCaughtUp(); });
      document.addEventListener('keydown', (event) => {
        if (event.key !== '/' || event.metaKey || event.ctrlKey || event.altKey) return;
        const target = event.target;
        if (target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.tagName === 'SELECT' || target.isContentEditable)) return;
        event.preventDefault();
        el('search-input').focus();
      });
      rememberDisclosure(el('since-card'), 'since');
      rememberDisclosure(el('pulse-details'), 'pulse');
      rememberDisclosure(el('tools-card'), 'tools');
      rememberDisclosure(el('queue-pending-wrap'), 'queuePending');
      rememberDisclosure(el('queue-blocked-wrap'), 'queueBlocked');

      // ---- cross-tab sync -------------------------------------------------------
      // localStorage writes in one tab fire `storage` in the others: handled
      // marks, caught-up, queue skips and confirmed saves stay consistent
      // across this browser's windows without waiting for a poll.
      let storageRerenderTimer = 0;
      window.addEventListener('storage', (event) => {
        if (!event.key || !event.key.startsWith(`radar:${data.conversationId || 'group'}:`)) return;
        handled = readStore(storeKey('handled'), {}) || {};
        caughtUpAt = Number(readStore(storeKey('caughtUpAt'), 0)) || 0;
        queueSkipped = readStore(storeKey('queueSkipped'), {}) || {};
        if (event.key === overlayKey() && decisions.readback !== 'ok') seedDecisions();
        clearTimeout(storageRerenderTimer);
        storageRerenderTimer = setTimeout(() => { renderBriefing(); renderStream(); }, 150);
      });

      // ---- live updates ---------------------------------------------------------
      let pollTimer = 0;
      function schedulePoll(delay) {
        clearTimeout(pollTimer);
        pollTimer = setTimeout(poll, delay == null ? (document.hidden ? POLL_HIDDEN_MS : POLL_VISIBLE_MS) : delay);
      }
      async function pollHealth() {
        if (!LIVE) return;
        try {
          const response = await fetch(`api/health?_=${Date.now()}`, { cache: 'no-store' });
          if (!response.ok) return;
          const latest = await response.json();
          if (latest && typeof latest === 'object') { health = latest; renderHealthStrip(); }
        } catch (_) { /* the status poll already tracks offline */ }
      }
      async function poll() {
        try {
          const response = await fetch(`status.json?_=${Date.now()}`, { cache: 'no-store' });
          if (!response.ok) throw new Error(`status ${response.status}`);
          const latest = await response.json();
          pollFailures = 0;
          serverOnline = true;
          const statusMoved = latest && latest.updated_at && latest.updated_at !== lastStatusSeen;
          if (statusMoved || recheckData) {
            if (statusMoved) { lastStatusSeen = latest.updated_at; status = latest; }
            const dataResponse = await fetch(`dashboard-data.json?_=${Date.now()}`, { cache: 'no-store' });
            if (dataResponse.ok) {
              const next = await dataResponse.json();
              const currentGenerated = stagedData ? stagedData.generatedAt : data.generatedAt;
              if (next && next.generatedAt && next.generatedAt !== currentGenerated) { stageData(next); recheckData = false; }
              else { recheckData = Boolean(statusMoved); if (next && next.status && !stagedData) status = next.status; }
            }
          }
        } catch (_) {
          pollFailures += 1;
          if (pollFailures >= 3) serverOnline = false;
        }
        pollHealth();
        renderPill();
        schedulePoll();
      }

      // ---- boot -----------------------------------------------------------------
      applyData(data);
      if (firstVisit) el('since-card').open = true;
      setInterval(renderPill, 30000);
      if (LIVE) {
        fetchDecisions(true).then((changed) => { renderDecisionsUI(); if (!changed) renderTools(); });
        pollHealth();
        schedulePoll();
      } else {
        setTimeout(() => { persistState(); location.reload(); }, STATIC_RELOAD_MS);
      }
    })();
  </script>
</body>
</html>
"""
