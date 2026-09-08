#!/usr/bin/env python3
"""Viewport/accessibility measurement battery for the lane 06 dashboard.

Runs the CURRENT lane renderer against the frozen-contract fixture server and
records, per viewport (320/390/768/1440):

* horizontal overflow (documentElement.scrollWidth vs innerWidth), worst
  offending elements, and the same check again at 125% root font size;
* scroll cost: offsetTop of the search control, review/new areas and the
  stream, plus total scrollHeight;
* semantics audit: role=tablist contents, buttons lacking accessible names,
  aria-pressed coverage on filter controls;
* review-queue coverage vs stranded unreviewed tools (A09);
* console errors / uncaught page errors;
* screenshots (viewport at every width, full-page at 390 and 1440).

Usage:
    /usr/bin/python3 tests/lane06_browser/measure.py --label baseline --out DIR

Only loopback URLs are reachable: every other request is aborted at the
context route layer. Evidence is JSON + PNG; no invented scores.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(TESTS_DIR.parent / "scripts"))

from lane06_browser.fixture_server import FixtureServer  # noqa: E402

VIEWPORTS = (320, 390, 768, 1440)
VIEW_HEIGHT = 850

MEASURE_JS = """
() => {
  const doc = document.documentElement;
  const vw = window.innerWidth;
  const offenders = [];
  for (const node of document.querySelectorAll('body *')) {
    const rect = node.getBoundingClientRect();
    if (rect.right - vw > 1 || rect.left < -1) {
      const label = node.tagName.toLowerCase()
        + (node.id ? '#' + node.id : '')
        + (node.className && typeof node.className === 'string'
            ? '.' + node.className.trim().split(/\\s+/).slice(0, 2).join('.') : '');
      offenders.push({el: label, left: Math.round(rect.left), right: Math.round(rect.right)});
      if (offenders.length >= 8) break;
    }
  }
  const top = (sel) => { const n = document.querySelector(sel); return n ? Math.round(n.getBoundingClientRect().top + window.scrollY) : null; };
  const tablists = [...document.querySelectorAll('[role=tablist]')].map((t) => ({
    label: t.getAttribute('aria-label') || t.id || '',
    buttons: t.querySelectorAll('button').length,
    role_tabs: t.querySelectorAll('[role=tab]').length,
    with_aria_selected: t.querySelectorAll('[aria-selected]').length,
  }));
  const unnamed = [...document.querySelectorAll('button')].filter((b) =>
    !(b.textContent || '').trim() && !b.getAttribute('aria-label') && !b.getAttribute('title')).length;
  const pressable = [...document.querySelectorAll('.status-tab, .tool-tab, [data-filter-chip]')];
  return {
    innerWidth: vw,
    scrollWidth: doc.scrollWidth,
    overflowPx: Math.max(0, doc.scrollWidth - vw),
    scrollHeight: doc.scrollHeight,
    offenders,
    positions: {
      stream_search: top('#search-input'),
      any_search: top('input[type=search]'),
      queue_area: top('[data-lane06-queue]') ?? top('#tools-card'),
      since_area: top('[data-lane06-new]') ?? top('#since-card'),
      focus_area: top('[data-lane06-focus]') ?? top('#focus-list'),
      stream_panel: top('#stream'),
      first_resource: top('.resource-row'),
    },
    semantics: {
      tablists,
      unnamed_buttons: unnamed,
      filter_buttons: pressable.length,
      filter_buttons_with_aria_pressed: pressable.filter((b) => b.hasAttribute('aria-pressed')).length,
    },
  };
}
"""


def queue_coverage() -> dict:
    """A09 numbers straight from the fixture tools (no browser involved)."""
    import lane06_fixtures as fx

    tools = fx.build_tools()
    unreviewed = [t for t in tools if t["verdict"] == "unreviewed"]
    legacy_eligible = [t for t in unreviewed if t.get("mentions", 0) > 0 and (t.get("facts") or {}).get("ok")]
    return {
        "tools_total": len(tools),
        "unreviewed": len(unreviewed),
        "legacy_queue_predicate_facts_ok": len(legacy_eligible),
        "stranded_by_legacy_predicate": len(unreviewed) - len(legacy_eligible),
        "with_review_eligibility_block": sum(1 for t in unreviewed if t.get("review_eligibility")),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)

    from playwright.sync_api import sync_playwright

    console_errors: list = []
    page_errors: list = []
    results: dict = {"label": args.label, "viewports": {}, "queue_coverage": queue_coverage()}

    with FixtureServer() as server, sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": VIEW_HEIGHT})

        def guard(route):
            url = route.request.url
            if url.startswith(server.base) or url.startswith("data:") or "://127.0.0.1" in url or "://localhost" in url:
                return route.continue_()
            return route.abort()

        context.route("**/*", guard)
        page = context.new_page()
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: page_errors.append(str(e)))

        for width in VIEWPORTS:
            page.set_viewport_size({"width": width, "height": VIEW_HEIGHT})
            page.goto(server.base + "/", wait_until="load")
            page.wait_for_timeout(700)
            measured = page.evaluate(MEASURE_JS)
            page.evaluate("document.documentElement.style.fontSize = '125%'")
            page.wait_for_timeout(250)
            zoomed = page.evaluate(
                "() => ({scrollWidth: document.documentElement.scrollWidth, innerWidth: window.innerWidth})"
            )
            page.evaluate("document.documentElement.style.fontSize = ''")
            measured["zoom125"] = {
                "scrollWidth": zoomed["scrollWidth"],
                "overflowPx": max(0, zoomed["scrollWidth"] - zoomed["innerWidth"]),
            }
            results["viewports"][str(width)] = measured
            page.screenshot(path=str(out / "{}-{}.png".format(args.label, width)))
            if width in (390, 1440):
                page.screenshot(path=str(out / "{}-{}-full.png".format(args.label, width)), full_page=True)

        context.close()
        browser.close()

    results["console_errors"] = [e for e in console_errors
                                 if "__media/missing.png" not in e and "ERR_ABORTED" not in e]
    results["page_errors"] = page_errors
    (out / "{}-measurements.json".format(args.label)).write_text(
        json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    summary_rows = []
    for width, m in results["viewports"].items():
        summary_rows.append(
            "{}px: overflow {}px (zoom125 {}px), search@{} queue@{} stream@{} height {}".format(
                width, m["overflowPx"], m["zoom125"]["overflowPx"],
                m["positions"]["any_search"], m["positions"]["queue_area"],
                m["positions"]["stream_panel"], m["scrollHeight"]))
    print("\n".join(summary_rows))
    print("tablists:", json.dumps(results["viewports"]["390"]["semantics"]["tablists"]))
    print("queue coverage:", json.dumps(results["queue_coverage"]))
    print("page errors:", len(page_errors), "console errors:", len(results["console_errors"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
