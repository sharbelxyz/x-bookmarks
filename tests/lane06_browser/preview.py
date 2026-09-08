#!/usr/bin/env python3
"""Serve the lane 06 dashboard preview (FIXTURE DATA) on a loopback port.

Isolated preview only: synthetic payload, in-memory decisions, no live files,
never port 8765. Ownership is recorded in <lane>/tmp/preview-port.json.

Usage: /usr/bin/python3 tests/lane06_browser/preview.py [--port 8791]
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import sys
import time
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parents[1]
LANE = TESTS_DIR.parent
sys.path.insert(0, str(TESTS_DIR))
sys.path.insert(0, str(LANE / "scripts"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8791)
    args = parser.parse_args()
    if args.port == 8765:
        print("refusing production port 8765", file=sys.stderr)
        return 2
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", args.port))
    except OSError:
        print("port {} is taken; pick another with --port".format(args.port), file=sys.stderr)
        return 1
    finally:
        probe.close()

    from http.server import ThreadingHTTPServer
    from lane06_browser.fixture_server import FixtureState, make_handler
    from dashboard_renderer import render_dashboard_from_payload

    base = "http://127.0.0.1:{}".format(args.port)
    state = FixtureState(media_base=base)
    page = render_dashboard_from_payload(state.payload)
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(state, page))
    httpd.daemon_threads = True
    ownership = {"pid": os.getpid(), "port": args.port, "url": base + "/",
                 "lane": "06", "run": "run-20260906-2000", "mode": "fixture",
                 "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    (LANE / "tmp" / "preview-port.json").write_text(json.dumps(ownership, indent=2) + "\n", encoding="utf-8")
    print("lane06 preview (FIXTURE DATA) at {}/ pid {}".format(base, os.getpid()), flush=True)

    def stop(signum, frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        httpd.serve_forever(poll_interval=0.5)
    except SystemExit:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
