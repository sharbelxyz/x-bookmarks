#!/usr/bin/env python3
"""Lane 06 UI against the REAL baseline radar_server handler (in-lane).

Unlike browser_checks.py (fixture server), this drives the dashboard through
the unmodified `radar_server.RadarHandler` + `record_verdict`/`record_outcome`
from this lane's baseline copy, with VERDICTS_PATH/OUTCOMES_PATH/data_dir
patched into a temp sandbox (port 0, loopback). It proves, against real
provider code:

* verdict + outcome saves hit the real validation and land in real files
  (atomic tmp+replace, 0600) — and the response contract matches C2;
* the baseline server has NO `api/decisions` read-back → the client detects
  that, keeps the save visible across reload via its local overlay, and shows
  the honest "next export" freshness note (A10 fallback);
* a real type-fit 409 from resource_typing surfaces as a controlled error;
* the flow also works at a 390px mobile viewport.

Still NOT final acceptance: revision/conflict semantics, hardened guards and
the read-back route are provider work (02/05/07) verified at integration.

Usage: /usr/bin/python3 tests/lane06_browser/real_server_check.py --out DIR
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parents[1]
LANE = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(LANE / "scripts"))

import lane06_fixtures as fx  # noqa: E402
import radar_server as server  # noqa: E402
from dashboard_renderer import render_dashboard_from_payload  # noqa: E402

RESULTS: list = []
PAGE_ERRORS: list = []


def check(name: str, passed: bool, detail: str = "") -> None:
    RESULTS.append({"name": name, "pass": bool(passed), "detail": detail})
    print("{} {}{}".format("PASS" if passed else "FAIL", name, " — " + detail if detail and not passed else ""))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)

    from playwright.sync_api import sync_playwright

    with tempfile.TemporaryDirectory(dir=str(LANE / "tmp")) as tmp:
        sandbox = Path(tmp)
        data_dir = sandbox / "data"
        data_dir.mkdir()
        verdicts_path = sandbox / "verdicts.json"
        outcomes_path = sandbox / "outcomes.json"

        payload = fx.build_payload()
        (data_dir / "dashboard.html").write_text(render_dashboard_from_payload(payload), encoding="utf-8")
        (data_dir / "dashboard-data.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        (data_dir / "status.json").write_text(json.dumps(fx.build_status(), ensure_ascii=False), encoding="utf-8")

        patches = [
            mock.patch.object(server, "VERDICTS_PATH", verdicts_path),
            mock.patch.object(server, "OUTCOMES_PATH", outcomes_path),
        ]
        for patch in patches:
            patch.start()
        server.RadarHandler.data_dir = data_dir
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.RadarHandler)
        port = httpd.server_address[1]
        base = "http://127.0.0.1:{}".format(port)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(channel="chrome", headless=True)
                context = browser.new_context(viewport={"width": 390, "height": 850})

                def guard(route):
                    url = route.request.url
                    if url.startswith(base) or url.startswith("data:"):
                        return route.continue_()
                    return route.abort()

                context.route("**/*", guard)
                page = context.new_page()
                page.on("pageerror", lambda e: PAGE_ERRORS.append(str(e)))
                page.goto(base + "/", wait_until="load")
                page.wait_for_timeout(1000)

                # Lane 06 wrote this against the BASELINE server, where the
                # read-back route did not exist yet (404/405 → client degrades).
                # Chat 07 registered lane 02's GET /api/decisions at integration,
                # so 200 is now the correct integrated answer. Accept either and
                # report which contract this run exercised.
                readback_status = page.evaluate("(b) => fetch(b + '/api/decisions').then(r => r.status)", base)
                check("real.readback-route-honest",
                      readback_status in (200, 404, 405),
                      "status={} ({})".format(
                          readback_status,
                          "integrated read-back present" if readback_status == 200
                          else "absent, client degrades"))
                if readback_status == 200:
                    shape = page.evaluate(
                        "(b) => fetch(b + '/api/decisions').then(r => r.json()).then(d => ({ok: d.ok, revType: typeof d.revision, keys: d.revision ? Object.keys(d.revision) : []}))",
                        base)
                    check("real.readback-revision-shape",
                          shape.get("revType") == "object"
                          and set(shape.get("keys") or []) == {"verdicts", "outcomes"},
                          str(shape))

                readback_live = readback_status == 200

                # Save a verdict at 390px through the real handler.
                first_key = page.evaluate("() => { const b = document.querySelector('#queue-list [data-decide]'); return b ? b.dataset.key : null; }")
                page.click("#queue-list [data-decide='must_try']")
                page.wait_for_timeout(1200)
                toast = page.evaluate("() => document.getElementById('toast-text').textContent")
                check("real.save-succeeds-mobile", "Must try" in toast and "Saved" in toast, toast)
                # Integration repair (Chat 07, 2026-09-07): lane 06 wrote this
                # against the baseline server, where a save was NOT visible until
                # the next export, so an honest "next export" lag note was
                # required. With lane 02's read-back integrated the save IS
                # authoritative immediately — the A10 acceptance criterion — so
                # the lag note would now be a LIE. Assert whichever statement is
                # true of the contract actually in force.
                if readback_live:
                    check("real.save-visibility-note-honest",
                          "next export" not in toast
                          and ("sees it now" in toast or "immediately" in toast),
                          toast)
                else:
                    check("real.save-visibility-note-honest", "next export" in toast, toast)

                document = json.loads(verdicts_path.read_text(encoding="utf-8"))
                entry = next((e for e in document.get("verdicts", []) if e.get("key") == first_key), None)
                file_mode = oct(verdicts_path.stat().st_mode & 0o777)
                check("real.verdict-file-written", bool(entry) and entry["verdict"] == "must_try" and entry["decided_by"] == "dashboard", json.dumps(entry, ensure_ascii=False) if entry else "missing")
                check("real.verdict-file-0600", file_mode == "0o600", file_mode)

                # Reload: overlay keeps the save visible without read-back.
                page.reload(wait_until="load")
                page.wait_for_timeout(1000)
                still = page.evaluate("""(key) => ({
                  inQueue: Boolean(document.querySelector(`#queue-list [data-key='${key}']`)),
                  note: (() => { document.getElementById('tools-card').open = true; return document.getElementById('tools-coverage').textContent; })(),
                })""", first_key)
                check("real.reload-keeps-save", not still["inQueue"], json.dumps(still))
                if readback_live:
                    check("real.sync-note-shown", "next export" not in still["note"],
                          still["note"])
                else:
                    check("real.sync-note-shown", "next export" in still["note"],
                          still["note"])

                # A second browser profile must NOT silently pretend freshness:
                # baseline has no read-back, so it shows export-time state.
                context2 = browser.new_context(viewport={"width": 1280, "height": 900})
                context2.route("**/*", guard)
                page2 = context2.new_page()
                page2.goto(base + "/", wait_until="load")
                page2.wait_for_timeout(900)
                second_sees = page2.evaluate("""(key) => Boolean(document.querySelector(`#queue-list [data-key='${key}']`))""", first_key)
                if readback_live:
                    # A10 acceptance: save -> reload -> SECOND browser agree
                    # immediately. The decided tool must be gone from the second
                    # browser's queue without waiting for an export.
                    check("real.second-browser-agrees-immediately", not second_sees,
                          "second browser still queues the decided tool" if second_sees
                          else "second browser reflects the durable decision")
                else:
                    check("real.second-browser-agrees-immediately", bool(second_sees),
                          "no read-back: stale until next export (honest, labeled)")
                context2.close()

                # Real outcome write through record_outcome.
                outcome_key = page.evaluate("""() => {
                  document.getElementById('tools-card').open = true;
                  const b = document.querySelector("#tool-list [data-outcome='trying']");
                  if (b) b.click();
                  return b ? b.dataset.okey : null;
                }""")
                page.wait_for_timeout(1000)
                outcome_document = json.loads(outcomes_path.read_text(encoding="utf-8")) if outcomes_path.exists() else {}
                outcome_entry = next((e for e in outcome_document.get("outcomes", []) if e.get("key") == outcome_key), None)
                check("real.outcome-file-written", bool(outcome_entry) and outcome_entry["state"] == "trying", json.dumps(outcome_entry, ensure_ascii=False) if outcome_entry else "missing")

                # Real type-fit conflict from resource_typing → controlled 409.
                conflict = page.evaluate("""(b) => fetch(b + '/api/verdict', {
                    method: 'POST',
                    headers: {'X-Radar-Action': 'verdict', 'Content-Type': 'application/json'},
                    body: JSON.stringify({key: 'example.com/articles/x', verdict: 'must_try', resource_type: 'read'}),
                  }).then(r => r.json().then(j => ({status: r.status, error: j.error || '', hint: j.hint || ''})))""", base)
                check("real.type-conflict-409", conflict["status"] == 409 and "does not fit" in conflict["error"], json.dumps(conflict))

                page.screenshot(path=str(out / "real-server-390.png"), full_page=False)
                context.close()
                browser.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
            for patch in patches:
                patch.stop()
            server.RadarHandler.data_dir = server.DATA_DIR

    check("real.no-page-errors", not PAGE_ERRORS, "; ".join(PAGE_ERRORS[:3]))
    passed = sum(1 for r in RESULTS if r["pass"])
    (out / "real-server-results.json").write_text(json.dumps({"total": len(RESULTS), "passed": passed, "results": RESULTS}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("== {} / {} passed ==".format(passed, len(RESULTS)))
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
