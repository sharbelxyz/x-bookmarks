"""Lane 06 fixture server — frozen-contract stand-in for browser tests.

Implements the C7 surface the dashboard client uses (GET exports, POST
verdict/outcome/negative-term with the X-Radar-Action header) plus the
ADDITIVE targets the UI must consume when providers land:

* C2-T-A10: mutation responses carry `revision` + `record`; a read-back
  route `GET /api/decisions` returns both authored documents + revision.
* C4/A03: `/api/health` can serve the extended stages block.

Scenario switches (test-only routes, NOT part of any contract, never to be
integrated) let tests exercise failure/conflict/slow/offline paths:

    POST /__test/scenario   {"save": "ok|fail|conflict|slow",
                             "decisions_route": true|false,
                             "health": "mixed|all-ok|frozen-only"}
    POST /__test/bump-export  regenerate payload with a newer generatedAt
    GET  /__media/ok.png      1x1 PNG; /__media/missing.png -> 404

Binds 127.0.0.1 port 0 only. All state in memory; nothing touches config
or live files. FIXTURE MODE ONLY — passing here is a development check,
not provider acceptance (PARALLEL-CONTRACTS.md).
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

TESTS_DIR = Path(__file__).resolve().parents[1]
LANE_ROOT = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(LANE_ROOT / "scripts"))

import lane06_fixtures as fx  # noqa: E402

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)

ALLOWED_VERDICTS = {"must_try", "must_read", "excluded", "already_have"}
ALLOWED_OUTCOMES = {"trying", "kept", "dropped"}
# Mirrors resource_typing.VERDICT_FOR_TYPE closely enough for fixture 409s.
VERDICT_FOR_TYPE = {
    "try": {"must_try", "excluded", "already_have"},
    "learn": {"must_read", "excluded", "already_have"},
    "read": {"must_read", "excluded"},
    "reference": {"already_have", "excluded", "must_read"},
    "other": {"must_try", "must_read", "excluded", "already_have"},
}


class FixtureState:
    def __init__(self, media_base: str, empty: bool = False) -> None:
        self.lock = threading.Lock()
        self.save_mode = "ok"
        self.decisions_route = True
        self.health_mode = "mixed"
        self.revision = 1
        self.media_base = media_base
        self.empty = empty
        self.export_bumps = 0
        self.verdicts: Dict[str, Dict[str, Any]] = {}
        self.outcomes: Dict[str, Dict[str, Any]] = {}
        self.negative_terms: list = []
        self.requests: list = []
        self.rebuild()

    def rebuild(self) -> None:
        if self.empty:
            payload = fx.build_payload(media_base=self.media_base)
            payload["resources"] = []
            payload["tools"] = []
            payload["negativeProposals"] = []
            payload["briefing"] = {"topPicks": [], "topPicksWindowDays": 0,
                                   "lanes": {}, "laneTotals": {}}
            self.payload = payload
        else:
            now = fx.NOW + dt.timedelta(minutes=self.export_bumps)
            self.payload = fx.build_payload(media_base=self.media_base, now=now)
        # Fixture tools already carry verdict/outcome fields; the authored maps
        # start from what the payload claims so read-back agrees with the page.
        for tool in self.payload.get("tools", []):
            if tool.get("verdict") not in (None, "", "unreviewed") and not tool.get("auto"):
                self.verdicts.setdefault(tool["key"].lower(), {
                    "key": tool["key"], "name": tool.get("name") or tool["key"],
                    "verdict": tool["verdict"], "why": tool.get("why") or "",
                    "what": tool.get("what") or "", "first_step": tool.get("first_step") or "",
                    "lane": tool.get("lane") or "", "reason_code": tool.get("reason_code") or "",
                    "resource_type": tool.get("resource_type") or "",
                    "decided_at": "2026-09-01T00:00:00+00:00", "decided_by": "dashboard",
                    "rank": tool.get("rank"),
                })
            if tool.get("outcome"):
                self.outcomes.setdefault(tool["key"].lower(), {
                    "key": tool["key"], "name": tool.get("name") or tool["key"],
                    "state": tool["outcome"], "note": tool.get("outcome_note") or "",
                    "decided_at": tool.get("outcome_at") or "2026-09-01T00:00:00+00:00",
                    "decided_by": "dashboard",
                })

    def decisions_documents(self) -> Dict[str, Any]:
        return {
            "verdicts_document": {
                "version": 1,
                "updated_at": dt.date.today().isoformat(),
                "verdicts": sorted(self.verdicts.values(), key=lambda e: e["key"]),
            },
            "outcomes_document": {
                "version": 1,
                "updated_at": dt.date.today().isoformat(),
                "outcomes": sorted(self.outcomes.values(), key=lambda e: e["key"]),
            },
            "revision": self.revision,
        }


def make_handler(state: FixtureState, page_html: str):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, code: int, payload: Dict[str, Any]) -> None:
            self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def log_message(self, fmt: str, *args: Any) -> None:  # quiet
            pass

        def do_GET(self) -> None:  # noqa: N802
            route = self.path.split("?", 1)[0]
            state.requests.append(("GET", route))
            if route in ("/", "/dashboard.html"):
                self._send(HTTPStatus.OK, page_html.encode("utf-8"), "text/html; charset=utf-8")
            elif route == "/dashboard-data.json":
                with state.lock:
                    body = json.dumps(state.payload, ensure_ascii=False).encode("utf-8")
                self._send(HTTPStatus.OK, body, "application/json; charset=utf-8")
            elif route == "/status.json":
                with state.lock:
                    status = dict(fx.build_status())
                    if state.export_bumps:
                        bumped = fx.NOW + dt.timedelta(minutes=state.export_bumps)
                        status["updated_at"] = bumped.isoformat(timespec="seconds")
                self._json(HTTPStatus.OK, status)
            elif route == "/negative-proposals.json":
                self._json(HTTPStatus.OK, {"proposals": fx.build_negative_proposals()})
            elif route == "/api/health":
                with state.lock:
                    mode = state.health_mode
                if mode == "frozen-only":
                    self._json(HTTPStatus.OK, fx.build_health(extended=False))
                else:
                    self._json(HTTPStatus.OK, fx.build_health(extended=True, scenario=mode))
            elif route == "/api/decisions":
                with state.lock:
                    enabled = state.decisions_route
                    documents = state.decisions_documents()
                if enabled:
                    self._json(HTTPStatus.OK, documents)
                else:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "not found", "path": route})
            elif route == "/__media/ok.png":
                self._send(HTTPStatus.OK, PNG_1PX, "image/png")
            elif route == "/favicon.ico":
                self._send(HTTPStatus.NO_CONTENT, b"", "image/x-icon")
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found", "path": route})

        def do_HEAD(self) -> None:  # noqa: N802
            self.do_GET()

        def _read_body(self) -> Optional[Dict[str, Any]]:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return None
            if length <= 0 or length > 16384:
                return None
            try:
                parsed = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return None
            return parsed if isinstance(parsed, dict) else None

        def do_POST(self) -> None:  # noqa: N802
            route = self.path.split("?", 1)[0]
            state.requests.append(("POST", route))
            if route == "/__test/scenario":
                body = self._read_body() or {}
                with state.lock:
                    state.save_mode = str(body.get("save", state.save_mode))
                    if "decisions_route" in body:
                        state.decisions_route = bool(body["decisions_route"])
                    if "health" in body:
                        state.health_mode = str(body["health"])
                self._json(HTTPStatus.OK, {"ok": True})
                return
            if route == "/__test/bump-export":
                with state.lock:
                    state.export_bumps += 5
                    state.rebuild()
                self._json(HTTPStatus.OK, {"ok": True, "generatedAt": state.payload["generatedAt"]})
                return
            wanted = {"/api/verdict": "verdict", "/api/outcome": "outcome",
                      "/api/negative-term": "negative-term", "/api/run": "run"}.get(route)
            if wanted is None:
                self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "method not allowed"})
                return
            if self.headers.get("X-Radar-Action", "").strip().lower() != wanted:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "missing X-Radar-Action header"})
                return
            if route == "/api/run":
                self._json(HTTPStatus.ACCEPTED, {"started": True, "pid": 999, "cooldown_seconds": 600})
                return
            body = self._read_body()
            if body is None:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "body must be a JSON object of 1..16384 bytes"})
                return
            with state.lock:
                mode = state.save_mode
            if mode == "slow":
                time.sleep(1.5)
            if mode == "fail":
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "injected save failure (fixture)"})
                return
            if mode == "conflict":
                with state.lock:
                    current = state.revision
                self._json(HTTPStatus.CONFLICT, {"error": "revision mismatch", "current_revision": current})
                return
            if route == "/api/verdict":
                self._handle_verdict(body)
            elif route == "/api/outcome":
                self._handle_outcome(body)
            else:
                self._handle_negative(body)

        def _handle_verdict(self, body: Dict[str, Any]) -> None:
            key = str(body.get("key") or "").strip().lstrip("/")
            verdict = str(body.get("verdict") or "").strip()
            if not key or "/" not in key:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "key must look like host/owner/name"})
                return
            if verdict not in ALLOWED_VERDICTS and verdict != "clear":
                self._json(HTTPStatus.BAD_REQUEST, {"error": "verdict must be one of {} or 'clear'".format(sorted(ALLOWED_VERDICTS))})
                return
            resource_type = str(body.get("resource_type") or "").strip()
            if verdict != "clear" and resource_type and verdict not in VERDICT_FOR_TYPE.get(resource_type, ALLOWED_VERDICTS):
                self._json(HTTPStatus.CONFLICT, {
                    "error": "'{}' does not fit a '{}' resource; allowed here: {}".format(
                        verdict, resource_type, sorted(VERDICT_FOR_TYPE.get(resource_type, set()))),
                    "hint": "pick a verdict that matches what the thing is",
                })
                return
            with state.lock:
                existed = key.lower() in state.verdicts
                if verdict == "clear":
                    state.verdicts.pop(key.lower(), None)
                    action = "cleared" if existed else "not present"
                    record = None
                else:
                    record = {
                        "key": key, "name": str(body.get("name") or key)[:200],
                        "verdict": verdict, "why": str(body.get("why") or "")[:600],
                        "what": str(body.get("what") or "")[:400],
                        "first_step": str(body.get("first_step") or "")[:400],
                        "lane": str(body.get("lane") or "")[:120],
                        "reason_code": str(body.get("reason_code") or "")[:40],
                        "resource_type": resource_type,
                        "decided_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                        "decided_by": "dashboard",
                    }
                    if verdict == "must_try":
                        ranks = [e.get("rank") or 0 for e in state.verdicts.values() if e.get("verdict") == "must_try"]
                        record["rank"] = (max(ranks) + 1) if ranks else 1
                    state.verdicts[key.lower()] = record
                    action = "replaced" if existed else "added"
                state.revision += 1
                response = {
                    "ok": True, "action": action, "key": key, "verdict": verdict,
                    "total": len(state.verdicts),
                    "note": "Takes effect in the dashboard after the next export (within 30 minutes) or Scan now.",
                    "revision": state.revision,
                }
                if record is not None:
                    response["record"] = record
            self._json(HTTPStatus.OK, response)

        def _handle_outcome(self, body: Dict[str, Any]) -> None:
            key = str(body.get("key") or "").strip().lstrip("/")
            outcome_state = str(body.get("state") or "").strip()
            if not key or "/" not in key:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "key must look like host/owner/name"})
                return
            if outcome_state not in ALLOWED_OUTCOMES and outcome_state != "clear":
                self._json(HTTPStatus.BAD_REQUEST, {"error": "state must be one of {} or 'clear'".format(sorted(ALLOWED_OUTCOMES))})
                return
            with state.lock:
                existed = key.lower() in state.outcomes
                if outcome_state == "clear":
                    state.outcomes.pop(key.lower(), None)
                    action = "cleared" if existed else "not present"
                    record = None
                else:
                    record = {
                        "key": key, "name": str(body.get("name") or key)[:200],
                        "state": outcome_state, "note": str(body.get("note") or "")[:600],
                        "decided_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                        "decided_by": "dashboard",
                    }
                    state.outcomes[key.lower()] = record
                    action = "replaced" if existed else "added"
                state.revision += 1
                response = {"ok": True, "action": action, "key": key, "state": outcome_state,
                            "total": len(state.outcomes), "revision": state.revision}
                if record is not None:
                    response["record"] = record
            self._json(HTTPStatus.OK, response)

        def _handle_negative(self, body: Dict[str, Any]) -> None:
            term = str(body.get("term") or "").strip().lower()
            action = str(body.get("action") or "add").strip()
            if not term or len(term) < 3 or len(term) > 40 or any(c.isspace() for c in term):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "term must be a single token of 3-40 characters"})
                return
            with state.lock:
                if action == "add" and term not in state.negative_terms:
                    state.negative_terms.append(term)
                elif action == "remove":
                    state.negative_terms = [t for t in state.negative_terms if t != term]
            self._json(HTTPStatus.OK, {"ok": True, "action": "added" if action == "add" else "removed",
                                       "term": term, "total": len(state.negative_terms)})

    return Handler


class FixtureServer:
    """Context manager: renders the CURRENT lane renderer against fixtures."""

    def __init__(self, empty: bool = False, renderer: str = "lane") -> None:
        self.empty = empty
        self.renderer = renderer
        self.httpd: Optional[ThreadingHTTPServer] = None
        self.state: Optional[FixtureState] = None
        self.base = ""

    def __enter__(self) -> "FixtureServer":
        probe = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        port = probe.server_address[1]
        probe.server_close()
        self.base = "http://127.0.0.1:{}".format(port)
        self.state = FixtureState(media_base=self.base, empty=self.empty)
        from dashboard_renderer import render_dashboard_from_payload
        page = render_dashboard_from_payload(self.state.payload)
        handler = make_handler(self.state, page)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self.httpd.daemon_threads = True
        thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        thread.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()

    def set_scenario(self, **kwargs: Any) -> None:
        import urllib.request

        request = urllib.request.Request(
            self.base + "/__test/scenario",
            data=json.dumps(kwargs).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(request, timeout=5).read()
