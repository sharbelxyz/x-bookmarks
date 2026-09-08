#!/usr/bin/env python3
"""Functional browser battery for the lane 06 dashboard (fixture mode).

Covers the acceptance workflows from 06-dashboard-experience.md against the
frozen-contract fixture server: search/filters, three-at-a-time review with
eligibility lanes, verdict save → reload → second browser, failed-save /
conflict / API-down / static-file behavior, outcome entry with baseline &
result, health strip states, cross-tab sync, media fallbacks, keyboard and
directionality checks, empty payload.

FIXTURE MODE ONLY. Real-provider verification runs separately
(real_server_check.py) and final acceptance happens in integration.

Usage: /usr/bin/python3 tests/lane06_browser/browser_checks.py --out DIR
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parents[1]
LANE = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(LANE / "scripts"))

from lane06_browser.fixture_server import FixtureServer  # noqa: E402

RESULTS: list = []
CONSOLE_ERRORS: list = []
PAGE_ERRORS: list = []


def check(name: str, passed: bool, detail: str = "") -> None:
    RESULTS.append({"name": name, "pass": bool(passed), "detail": detail})
    print("{} {}{}".format("PASS" if passed else "FAIL", name, " — " + detail if detail and not passed else ""))


def guard_context(context, base: str) -> None:
    def guard(route):
        url = route.request.url
        if url.startswith(base) or url.startswith("data:") or url.startswith("file:") or "://127.0.0.1" in url or "://localhost" in url:
            return route.continue_()
        return route.abort()
    context.route("**/*", guard)


def wire_errors(page) -> None:
    page.on("console", lambda m: CONSOLE_ERRORS.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: PAGE_ERRORS.append(str(e)))


def get_json(url: str):
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_for(page, predicate_js: str, timeout_ms: int = 6000) -> bool:
    try:
        page.wait_for_function(predicate_js, timeout=timeout_ms)
        return True
    except Exception:
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)

    from playwright.sync_api import sync_playwright

    with FixtureServer() as server, sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        guard_context(context, server.base)
        page = context.new_page()
        wire_errors(page)
        page.goto(server.base + "/?poll=5", wait_until="load")
        page.wait_for_timeout(800)

        # 1 — semantics: no tablist, pressed-state filters, named buttons
        audit = page.evaluate("""() => ({
          tablists: document.querySelectorAll('[role=tablist]').length,
          missingPressed: [...document.querySelectorAll('.status-tab,.tool-tab,.chip-toggle')].filter(b => !b.hasAttribute('aria-pressed')).length,
          unnamed: [...document.querySelectorAll('button')].filter(b => !(b.textContent||'').trim() && !b.getAttribute('aria-label') && !b.getAttribute('title')).length,
        })""")
        check("a11y.no-tablist", audit["tablists"] == 0, str(audit))
        check("a11y.aria-pressed-coverage", audit["missingPressed"] == 0, str(audit))
        check("a11y.named-buttons", audit["unnamed"] == 0, str(audit))

        # 2 — debounced search: 9 quick keystrokes must not cause 9 stream renders
        before = page.evaluate("() => Number(document.getElementById('resource-list').dataset.renders || 0)")
        page.focus("#search-input")
        page.keyboard.type("noon-auto", delay=25)
        page.wait_for_timeout(600)
        after = page.evaluate("() => Number(document.getElementById('resource-list').dataset.renders || 0)")
        matched = page.evaluate("() => document.getElementById('results-count').textContent")
        check("search.debounce", after - before <= 2, "renders {} -> {}".format(before, after))
        check("search.filters", "1 resource" in matched or " 1 " in matched or matched.startswith("1"), matched)

        # 3 — Arabic search folding + directionality
        page.fill("#search-input", "")
        page.fill("#search-input", "أداه مفتوحه")
        page.wait_for_timeout(400)
        arabic_hits = page.evaluate("() => document.querySelectorAll('#resource-list .resource-row').length")
        check("search.arabic-folding", arabic_hits >= 1, "hits {}".format(arabic_hits))
        directions = page.evaluate("""() => {
          const title = [...document.querySelectorAll('.resource-title')].find(n => /أداة/.test(n.textContent));
          const key = document.querySelector('.repo-fact a, .repo-fact .repo-name, .link-chip');
          return { title: title ? getComputedStyle(title).direction : null,
                   key: key ? getComputedStyle(key).direction : null };
        }""")
        check("rtl.title-auto", directions["title"] == "rtl", str(directions))
        check("rtl.identifier-ltr", directions["key"] in ("ltr", None), str(directions))
        page.fill("#search-input", "")
        page.wait_for_timeout(300)

        # 4 — status filter pressed state + filtering
        page.click("#status-tabs [data-status='irrelevant']")
        page.wait_for_timeout(200)
        pressed = page.evaluate("""() => ({
          pressed: document.querySelector("#status-tabs [data-status='irrelevant']").getAttribute('aria-pressed'),
          others: [...document.querySelectorAll('#resource-list .resource-row')].filter(r => r.dataset.status !== 'irrelevant').length,
          rows: document.querySelectorAll('#resource-list .resource-row').length,
        })""")
        check("filters.status-pressed", pressed["pressed"] == "true" and pressed["others"] == 0 and pressed["rows"] > 0, str(pressed))
        page.click("#status-tabs [data-status='all']")

        # 5 — queue: exactly three, eligibility evidence visible
        queue = page.evaluate("""() => ({
          items: document.querySelectorAll('#queue-list .queue-item').length,
          count: document.getElementById('queue-count').textContent,
          evidence: document.querySelectorAll('#queue-list .evidence-line').length,
          fit: document.querySelectorAll('#queue-list .fit-line').length,
          estimated: [...document.querySelectorAll('#queue-list .evidence-line')].filter(n => n.textContent.includes('estimated')).length,
        })""")
        check("queue.three-at-a-time", queue["items"] == 3 and queue["count"].strip() == "4", str(queue))
        check("queue.evidence-shown", queue["evidence"] == 3, str(queue))
        check("queue.suggested-fit-labeled", queue["fit"] >= 1, str(queue))
        check("queue.derived-labeled-estimated", queue["estimated"] >= 1, str(queue))

        # 6 — evidence-pending + blocked lanes visible with reasons (A09)
        lanes = page.evaluate("""() => {
          document.getElementById('queue-pending-wrap').open = true;
          document.getElementById('queue-blocked-wrap').open = true;
          return {
            pending: document.getElementById('queue-pending-count').textContent,
            pendingReasons: [...document.querySelectorAll('#queue-pending-list .queue-reason')].map(n => n.textContent),
            blocked: document.getElementById('queue-blocked-count').textContent,
            blockedReason: (document.querySelector('#queue-blocked-list .queue-reason')||{}).textContent || '',
            decideAnyway: document.querySelectorAll('#queue-pending-list [data-decide]').length,
          };
        }""")
        check("a09.pending-visible", lanes["pending"].strip() == "4" and len(lanes["pendingReasons"]) == 4, str(lanes))
        check("a09.blocked-reason", "private_target" in lanes["blockedReason"], lanes["blockedReason"])
        check("a09.decide-anyway", lanes["decideAnyway"] >= 4, str(lanes["decideAnyway"]))

        # 7 — verdict save: pending -> saved, leaves queue, next appears
        first_key = page.evaluate("() => document.querySelector('#queue-list [data-decide]').dataset.key")
        page.click("#queue-list [data-decide='must_try']")
        saved = wait_for(page, "() => !document.querySelector(`#queue-list [data-key='" + first_key + "']`)")
        page.wait_for_timeout(300)
        queue_after = page.evaluate("() => document.querySelectorAll('#queue-list .queue-item').length")
        server_doc = get_json(server.base + "/api/decisions")
        on_server = any(e["key"] == first_key and e["verdict"] == "must_try" for e in server_doc["verdicts_document"]["verdicts"])
        check("save.verdict-leaves-queue", saved and queue_after == 3, "queue after {}".format(queue_after))
        check("save.verdict-on-server", on_server, first_key)
        announced = page.evaluate("() => document.getElementById('sr-status').textContent")
        check("save.announced", "Saved" in announced or "Must try" in announced, announced)

        # 8 — reload agrees immediately (read-back)
        page.reload(wait_until="load")
        page.wait_for_timeout(900)
        reloaded = page.evaluate("""(key) => {
          const tools = document.getElementById('tools-card'); tools.open = true;
          const badge = document.getElementById('nav-decide-count').textContent;
          return { badge, inQueue: Boolean(document.querySelector(`#queue-list [data-key='${key}']`)) };
        }""", first_key)
        check("save.reload-agrees", reloaded["badge"].strip() == "3" and not reloaded["inQueue"], str(reloaded))

        # 9 — second browser context agrees immediately (read-back)
        context2 = browser.new_context(viewport={"width": 1440, "height": 900})
        guard_context(context2, server.base)
        page2 = context2.new_page()
        wire_errors(page2)
        page2.goto(server.base + "/", wait_until="load")
        page2.wait_for_timeout(900)
        second = page2.evaluate("""(key) => ({
          badge: document.getElementById('nav-decide-count').textContent,
          inQueue: Boolean(document.querySelector(`#queue-list [data-key='${key}']`)),
        })""", first_key)
        check("save.second-browser-agrees", second["badge"].strip() == "3" and not second["inQueue"], str(second))
        context2.close()

        # 10 — skip is local-only and reversible
        skip_key = page.evaluate("() => document.querySelector('#queue-list [data-skip]').dataset.skip")
        page.click("#queue-list [data-skip]")
        page.wait_for_timeout(300)
        skipped = page.evaluate("""(key) => ({
          gone: !document.querySelector(`#queue-list [data-skip='${key}']`),
          note: document.getElementById('queue-note').textContent,
          unskip: Boolean(document.getElementById('unskip-all')),
        })""", skip_key)
        check("queue.skip-local", skipped["gone"] and "skipped on this Mac" in skipped["note"] and skipped["unskip"], str(skipped))
        page.click("#unskip-all")
        page.wait_for_timeout(200)
        unskipped = page.evaluate("""(key) => Boolean(document.querySelector(`#queue-list [data-skip='${key}']`))""", skip_key)
        check("queue.unskip", unskipped, skip_key)

        # 11 — failed save keeps state + offers retry, retry succeeds
        server.set_scenario(save="fail")
        target_key = page.evaluate("() => document.querySelector('#queue-list [data-decide]').dataset.key")
        page.click("#queue-list [data-decide]")
        page.wait_for_timeout(700)
        failure = page.evaluate("""(key) => ({
          stillInQueue: Boolean(document.querySelector(`#queue-list [data-key='${key}']`)),
          toast: document.getElementById('toast-text').textContent,
          retryVisible: !document.getElementById('toast-action').hidden,
          alert: document.getElementById('sr-alert').textContent,
        })""", target_key)
        check("save.failure-keeps-state", failure["stillInQueue"], str(failure))
        check("save.failure-actionable", "Not saved" in failure["toast"] and failure["retryVisible"] and "unchanged" in failure["toast"], failure["toast"])
        server.set_scenario(save="ok")
        page.click("#toast-action")
        retried = wait_for(page, "() => !document.querySelector(`#queue-list [data-key='" + target_key + "']`)")
        check("save.retry-succeeds", retried, target_key)

        # 12 — revision conflict: explicit message, state unchanged, refetch
        server.set_scenario(save="conflict")
        conflict_key = page.evaluate("() => document.querySelector('#queue-list [data-decide]').dataset.key")
        page.click("#queue-list [data-decide]")
        page.wait_for_timeout(700)
        conflict = page.evaluate("""(key) => ({
          stillInQueue: Boolean(document.querySelector(`#queue-list [data-key='${key}']`)),
          toast: document.getElementById('toast-text').textContent,
        })""", conflict_key)
        check("save.conflict-explicit", conflict["stillInQueue"] and "changed elsewhere" in conflict["toast"], str(conflict))
        server.set_scenario(save="ok")

        # 13 — type-fit 409 (server rejects must_try on read) — controlled error
        conflict409 = page.evaluate("""(base) => fetch(base + '/api/verdict', {
            method: 'POST',
            headers: {'X-Radar-Action': 'verdict', 'Content-Type': 'application/json'},
            body: JSON.stringify({key: 'example.com/articles/x', verdict: 'must_try', resource_type: 'read'}),
          }).then(r => r.status)""", server.base)
        check("save.type-conflict-409", conflict409 == 409, str(conflict409))

        # 14 — outcome flow with baseline/result, then clear
        page.evaluate("() => { document.getElementById('tools-card').open = true; }")
        page.wait_for_timeout(200)
        outcome_key = page.evaluate("""() => {
          const row = [...document.querySelectorAll('#tool-list .tool-row')].find(r => r.querySelector("[data-outcome='kept']") && !r.querySelector('.outcome-chip'));
          const b = row ? row.querySelector("[data-outcome='kept']") : null;
          if (b) b.click();
          return b ? b.dataset.okey : null;
        }""")
        page.wait_for_timeout(300)
        form_ok = page.evaluate("() => Boolean(document.querySelector('.outcome-form'))")
        check("outcome.form-opens", bool(outcome_key) and form_ok, str(outcome_key))
        if form_ok:
            page.fill(".outcome-form [data-of='baseline']", "listing took 3h")
            page.fill(".outcome-form [data-of='result']", "40 min with the tool")
            page.fill(".outcome-form [data-of='note']", "keeping it")
            page.click(".outcome-form [data-osave]")
            page.wait_for_timeout(700)
            outcome_doc = get_json(server.base + "/api/decisions")["outcomes_document"]["outcomes"]
            entry = next((e for e in outcome_doc if e["key"] == outcome_key), None)
            good = entry and entry["state"] == "kept" and "baseline: listing took 3h" in entry["note"] and "result: 40 min" in entry["note"]
            check("outcome.saved-with-baseline-result", bool(good), json.dumps(entry, ensure_ascii=False) if entry else "missing")
            chip_shown = page.evaluate("""(key) => {
              const b = document.querySelector(`[data-outcome='clear'][data-okey='${key}']`);
              return Boolean(b);
            }""", outcome_key)
            check("outcome.clear-available", chip_shown, str(outcome_key))
            page.evaluate("""(key) => document.querySelector(`[data-outcome='clear'][data-okey='${key}']`).click()""", outcome_key)
            page.wait_for_timeout(600)
            outcome_doc2 = get_json(server.base + "/api/decisions")["outcomes_document"]["outcomes"]
            check("outcome.cleared-durably", not any(e["key"] == outcome_key for e in outcome_doc2), outcome_key)
            verdict_kept = any(e["key"] == outcome_key for e in get_json(server.base + "/api/decisions")["verdicts_document"]["verdicts"])
            check("outcome.clear-keeps-verdict", verdict_kept, outcome_key)

        # 15 — clear verdict keeps the trial (separate files invariant, UI side)
        page.evaluate("() => { document.getElementById('tools-card').open = true; }")
        clear_state = page.evaluate("""() => {
          const row = [...document.querySelectorAll('#tool-list .tool-row')].find(r => r.querySelector("[data-decide='clear']") && r.querySelector('.outcome-chip'));
          if (!row) return null;
          return { key: row.querySelector("[data-decide='clear']").dataset.key, outcome: row.querySelector('.outcome-chip').textContent };
        }""")
        if clear_state:
            page.evaluate("""(key) => document.querySelector(`[data-decide='clear'][data-key='${key}']`).click()""", clear_state["key"])
            page.wait_for_timeout(700)
            docs = get_json(server.base + "/api/decisions")
            verdict_gone = not any(e["key"] == clear_state["key"] for e in docs["verdicts_document"]["verdicts"])
            outcome_still = any(e["key"] == clear_state["key"] for e in docs["outcomes_document"]["outcomes"])
            check("verdict.clear-keeps-outcome", verdict_gone and outcome_still, json.dumps(clear_state, ensure_ascii=False))
        else:
            check("verdict.clear-keeps-outcome", False, "no row with verdict+outcome found")

        # 16 — health strip states
        health_mixed = page.evaluate("""() => ({
          hidden: document.getElementById('health-strip').hidden,
          text: document.getElementById('health-strip').textContent,
        })""")
        check("health.mixed-visible", not health_mixed["hidden"] and "needs your sign-in" in health_mixed["text"] and "degraded" in health_mixed["text"], health_mixed["text"][:160])
        has_timestamps = page.evaluate("() => document.querySelectorAll('#health-strip .stage-when').length > 0")
        check("health.timestamps-shown", has_timestamps)
        server.set_scenario(health="all-ok")
        page.wait_for_timeout(6500)
        allok_hidden = page.evaluate("() => document.getElementById('health-strip').hidden")
        check("health.all-ok-hidden", bool(allok_hidden))
        server.set_scenario(health="frozen-only")
        page.wait_for_timeout(6500)
        frozen_ok = page.evaluate("() => document.getElementById('health-strip').hidden")
        check("health.frozen-envelope-fallback", bool(frozen_ok))
        server.set_scenario(health="mixed")

        # 17 — media: bounded thumbnail ok + explicit failure fallback.
        # Thumbnails are lazy; bring them into view, then wait (bounded) until
        # every box has settled as loaded or explicitly failed.
        page.click("#status-tabs [data-status='all']")
        page.wait_for_timeout(200)
        page.evaluate("() => { const b = document.querySelector('.media-box'); if (b) b.scrollIntoView({block: 'center'}); }")
        wait_for(page, """() => [...document.querySelectorAll('.media-box')].every(b => {
          const img = b.querySelector('img');
          return b.classList.contains('media-failed') || (img && img.complete && img.naturalWidth > 0);
        })""", 8000)
        media = page.evaluate("""() => {
          const boxes = [...document.querySelectorAll('.media-box')];
          const ok = boxes.find(b => !b.classList.contains('media-failed') && b.querySelector('img') && b.querySelector('img').complete && b.querySelector('img').naturalWidth > 0);
          const failed = boxes.find(b => b.classList.contains('media-failed'));
          return {
            boxes: boxes.length,
            okLoaded: Boolean(ok),
            okBounded: ok ? ok.getBoundingClientRect().height <= 220 : null,
            failedNote: failed ? failed.querySelector('.media-note').textContent : null,
            failedVisible: failed ? getComputedStyle(failed.querySelector('.media-note')).display !== 'none' : null,
          };
        }""")
        check("media.loads-bounded", media["boxes"] >= 2 and media["okLoaded"] and media["okBounded"], str(media))
        check("media.failure-explicit", media["failedNote"] == "image unavailable" and media["failedVisible"], str(media))

        # 18 — cross-tab: Done in tab A hides the item from tab B's focus list
        # (handled items leave the default views by design; they stay reachable
        # via the "Only handled" filter, which tab B also verifies).
        tabB = context.new_page()
        wire_errors(tabB)
        tabB.goto(server.base + "/", wait_until="load")
        tabB.wait_for_timeout(700)
        done_id = page.evaluate("""() => {
          const b = document.querySelector('#focus-list [data-done]');
          if (!b) return null;
          const id = b.dataset.done; b.click(); return id;
        }""")
        gone_in_b = wait_for(tabB, "() => !document.querySelector(`#focus-list [data-done='" + str(done_id) + "']`)", 4000)
        tabB.select_option("#handled-filter", "only")
        tabB.wait_for_timeout(400)
        reachable_in_b = tabB.evaluate("""(id) => Boolean(document.querySelector(`#resource-list [data-undo='${id}']`))""", done_id)
        check("crosstab.done-syncs", bool(done_id) and gone_in_b, str(done_id))
        check("crosstab.handled-reachable", bool(reachable_in_b), str(done_id))
        tabB.close()

        # 19 — staged export: bump -> Apply appears -> applying updates in place
        r = urllib.request.Request(server.base + "/__test/bump-export", data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(r, timeout=5).read()
        apply_shown = wait_for(page, "() => !document.getElementById('apply-update').hidden", 15000)
        check("stale.apply-offered", apply_shown)
        if apply_shown:
            page.click("#apply-update")
            page.wait_for_timeout(500)
            applied = page.evaluate("() => document.getElementById('apply-update').hidden")
            check("stale.apply-applies", bool(applied))

        # 20 — keyboard: '/' focuses search; focus-visible outline is real
        page.keyboard.press("Escape")
        page.evaluate("() => document.activeElement && document.activeElement.blur()")
        page.keyboard.press("/")
        focused = page.evaluate("() => document.activeElement && document.activeElement.id")
        check("keyboard.slash-focuses-search", focused == "search-input", str(focused))
        outline = page.evaluate("""() => {
          const n = document.getElementById('search-input');
          n.focus();
          return getComputedStyle(n).outlineWidth;
        }""")
        check("keyboard.focus-outline", outline not in ("0px", "", None), str(outline))
        skip_first = page.evaluate("""() => {
          document.activeElement && document.activeElement.blur();
          const first = document.querySelector('.skip-link');
          first.focus();
          return { text: first.textContent, href: first.getAttribute('href'), visibleLeft: first.getBoundingClientRect().left >= 0 };
        }""")
        check("keyboard.skip-link", "Skip" in skip_first["text"] and skip_first["href"] == "#stream" and skip_first["visibleLeft"], str(skip_first))

        page.screenshot(path=str(out / "functional-final-desktop.png"), full_page=False)
        context.close()

        # 21 — read-back OFF: overlay keeps reload honest; second browser lag is labeled
        context3 = browser.new_context(viewport={"width": 1280, "height": 900})
        guard_context(context3, server.base)
        server.set_scenario(decisions_route=False, save="ok")
        page3 = context3.new_page()
        wire_errors(page3)
        page3.goto(server.base + "/", wait_until="load")
        page3.wait_for_timeout(900)
        overlay_key = page3.evaluate("() => { const b = document.querySelector('#queue-list [data-decide]'); return b ? b.dataset.key : null; }")
        if overlay_key:
            page3.click("#queue-list [data-decide]")
            page3.wait_for_timeout(800)
            toast3 = page3.evaluate("() => document.getElementById('toast-text').textContent")
            check("fallback.honest-freshness-note", "next export" in toast3, toast3)
            page3.reload(wait_until="load")
            page3.wait_for_timeout(900)
            still_saved = page3.evaluate("""(key) => !document.querySelector(`#queue-list [data-key='${key}']`)""", overlay_key)
            check("fallback.reload-keeps-save", bool(still_saved), overlay_key)
            note = page3.evaluate("() => { document.getElementById('tools-card').open = true; return document.getElementById('tools-coverage').textContent; }")
            check("fallback.sync-note-shown", "next export" in note, note)
        else:
            check("fallback.honest-freshness-note", False, "no queue item available")
        context3.close()
        server.set_scenario(decisions_route=True)

        # 22 — empty payload: honest empties, no errors
        with FixtureServer(empty=True) as empty_server:
            context4 = browser.new_context(viewport={"width": 390, "height": 850})
            guard_context(context4, empty_server.base)
            page4 = context4.new_page()
            wire_errors(page4)
            page4.goto(empty_server.base + "/", wait_until="load")
            page4.wait_for_timeout(700)
            empty = page4.evaluate("""() => ({
              queue: document.getElementById('queue-list').textContent,
              focus: document.getElementById('focus-list').textContent,
              stream: document.getElementById('resource-list').textContent,
            })""")
            check("empty.honest-states", "Queue is clear" in empty["queue"] and "Nothing to focus on" in empty["focus"] and "No matching resources" in empty["stream"], json.dumps(empty)[:200])
            page4.screenshot(path=str(out / "functional-empty-390.png"))
            context4.close()

        # 23 — API down: save fails honestly, previous state retained.
        # Simulated at the network layer (route abort), which also defeats the
        # browser's keep-alive connections to the still-running fixture server.
        context5 = browser.new_context(viewport={"width": 1280, "height": 900})
        guard_context(context5, server.base)
        page5 = context5.new_page()
        wire_errors(page5)
        page5.goto(server.base + "/", wait_until="load")
        page5.wait_for_timeout(700)
        down_key = page5.evaluate("() => { const b = document.querySelector('#queue-list [data-decide]'); return b ? b.dataset.key : null; }")
        context5.route("**/api/**", lambda route: route.abort())
        page5.click("#queue-list [data-decide]")
        page5.wait_for_timeout(1200)
        down = page5.evaluate("""(key) => ({
          stillInQueue: Boolean(document.querySelector(`#queue-list [data-key='${key}']`)),
          toast: document.getElementById('toast-text').textContent,
        })""", down_key)
        check("apidown.honest-failure", down["stillInQueue"] and "Not saved" in down["toast"], str(down))
        context5.close()

        # 24 — static file:// view: read-only + honest mutation message
        import lane06_fixtures as fx
        from dashboard_renderer import render_dashboard_from_payload
        static_path = LANE / "tmp" / "lane06-static.html"
        # Integration repair (Chat 07, 2026-09-07): lane06_fixtures.NOW is a
        # FIXED wall-clock anchor, but the page computes staleness against the
        # browser's live clock. Once real time drifts past staleAfterMinutes
        # (90) the pill legitimately reads "Stale" and this check failed for a
        # reason that is not a product defect. Freshen only the two freshness
        # fields so the static-mode assertion stays time-independent; every
        # other fixture value (and all relative ordering) is untouched.
        import datetime as _dt
        _static_payload = fx.build_payload()
        _fresh = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
        _static_payload["generatedAt"] = _fresh
        if isinstance(_static_payload.get("status"), dict):
            _static_payload["status"]["updated_at"] = _fresh
        static_path.write_text(render_dashboard_from_payload(_static_payload), encoding="utf-8")
        context6 = browser.new_context(viewport={"width": 1280, "height": 900})
        page6 = context6.new_page()
        wire_errors(page6)
        page6.goto("file://" + str(static_path), wait_until="load")
        page6.wait_for_timeout(700)
        static_probe = page6.evaluate("""() => ({
          banner: document.getElementById('mode-banner').textContent,
          bannerShown: !document.getElementById('mode-banner').hidden,
          pill: document.getElementById('pill-label').textContent,
          exportsHidden: document.getElementById('export-links').hidden,
        })""")
        page6.click("#queue-list [data-decide]")
        page6.wait_for_timeout(400)
        static_toast = page6.evaluate("() => document.getElementById('toast-text').textContent")
        check("static.read-only-banner", static_probe["bannerShown"] and "Read-only" in static_probe["banner"] and static_probe["pill"] == "Static file", str(static_probe)[:200])
        check("static.mutation-honest", "Read-only file view" in static_toast or "served dashboard" in static_toast, static_toast)
        context6.close()

        browser.close()

    filtered_console = [e for e in CONSOLE_ERRORS if "__media/missing.png" not in e and "ERR_ABORTED" not in e
                        and "ERR_CONNECTION_REFUSED" not in e and "Failed to fetch" not in e
                        and "ERR_INTERNET_DISCONNECTED" not in e and "status.json" not in e
                        and "api/health" not in e and "500 (Internal Server Error)" not in e
                        and "409 (Conflict)" not in e and "404 (Not Found)" not in e
                        and "ERR_FAILED" not in e]
    check("console.no-unexpected-errors", not filtered_console, "; ".join(filtered_console[:3]))
    check("pageerrors.none", not PAGE_ERRORS, "; ".join(PAGE_ERRORS[:3]))

    passed = sum(1 for r in RESULTS if r["pass"])
    summary = {"total": len(RESULTS), "passed": passed, "failed": len(RESULTS) - passed,
               "results": RESULTS, "console_errors_all": CONSOLE_ERRORS, "page_errors": PAGE_ERRORS}
    (out / "functional-results.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("== {} / {} passed ==".format(passed, len(RESULTS)))
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
