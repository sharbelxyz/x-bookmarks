"""Lane 05 adversarial tests for the C7 HTTP boundary (A08).

Every hostile request must produce a controlled response AND leave stored
state byte-identical. Raw sockets are used where urllib would "fix" the
request for us. Ephemeral loopback port, temp data dir, patched store
paths; trigger_run is stubbed so nothing ever spawns.
"""
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

LANE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LANE / "scripts"))

import http_guards  # noqa: E402
import radar_server as server  # noqa: E402

GOOD_BODY = json.dumps({"key": "github.com/example/synthetic-cli",
                        "verdict": "must_try", "resource_type": "try"}).encode()


class AdversarialBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.data_dir = root / "data"
        cls.data_dir.mkdir()
        (cls.data_dir / "status.json").write_text("{}")
        cls.verdicts = root / "verdicts.json"
        cls.outcomes = root / "outcomes.json"
        cls.profile = root / "profile.json"
        cls.verdicts.write_text(json.dumps({"version": 1, "verdicts": []}))
        cls.outcomes.write_text(json.dumps({"version": 1, "outcomes": []}))
        cls.profile.write_text(json.dumps({"selection": {"negative_terms": []}}))
        cls.patchers = [
            mock.patch.object(server, "VERDICTS_PATH", cls.verdicts),
            mock.patch.object(server, "OUTCOMES_PATH", cls.outcomes),
            mock.patch.object(server, "PROFILE_PATH", cls.profile),
        ]
        for patcher in cls.patchers:
            patcher.start()
        server.RadarHandler.data_dir = cls.data_dir
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.RadarHandler)
        cls.port = cls.httpd.server_address[1]
        assert cls.port != 8765
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        for patcher in cls.patchers:
            patcher.stop()
        cls.tmp.cleanup()

    # -- helpers ---------------------------------------------------------
    def snapshot_state(self):
        return (self.verdicts.read_bytes(), self.outcomes.read_bytes(),
                self.profile.read_bytes())

    def raw(self, payload, read_timeout=10):
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as sock:
            sock.sendall(payload)
            sock.settimeout(read_timeout)
            data = b""
            try:
                while len(data) < 65536:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    data += chunk
            except socket.timeout:
                pass
        return data

    def raw_status(self, payload, **kwargs):
        response = self.raw(payload, **kwargs)
        if not response:
            return 0
        return int(response.split(b" ", 2)[1])

    def post(self, path, body, headers, host=None):
        head = "POST {} HTTP/1.1\r\nHost: {}\r\n".format(
            path, host or "127.0.0.1:{}".format(self.port))
        for key, value in headers.items():
            head += "{}: {}\r\n".format(key, value)
        head += "Content-Length: {}\r\nConnection: close\r\n\r\n".format(len(body))
        return self.raw(head.encode() + body)

    def mutation_headers(self, action="verdict", origin=None, extra=None):
        headers = {"X-Radar-Action": action, "Content-Type": "application/json"}
        if origin is not None:
            headers["Origin"] = origin
        headers.update(extra or {})
        return headers

    def assert_rejected_without_state_change(self, response, expected_status):
        before = getattr(self, "_state_before", None)
        self.assertIsNotNone(before, "call mark_state() before the attack")
        status = int(response.split(b" ", 2)[1]) if response else 0
        self.assertEqual(status, expected_status, response[:200])
        self.assertEqual(self.snapshot_state(), before, "hostile request changed stored state")
        self._state_before = None

    def mark_state(self):
        self._state_before = self.snapshot_state()


class HostHeaderAttacks(AdversarialBase):
    def test_dns_rebinding_host_on_mutation(self):
        self.mark_state()
        response = self.post("/api/verdict", GOOD_BODY, self.mutation_headers(),
                             host="radar.attacker.example")
        self.assert_rejected_without_state_change(response, 400)

    def test_host_with_wrong_port(self):
        self.mark_state()
        response = self.post("/api/verdict", GOOD_BODY, self.mutation_headers(),
                             host="127.0.0.1:1")
        self.assert_rejected_without_state_change(response, 400)

    def test_missing_host_header(self):
        request = ("POST /api/verdict HTTP/1.1\r\nX-Radar-Action: verdict\r\n"
                   "Content-Type: application/json\r\nContent-Length: {}\r\n"
                   "Connection: close\r\n\r\n").format(len(GOOD_BODY)).encode() + GOOD_BODY
        self.mark_state()
        self.assert_rejected_without_state_change(self.raw(request), 400)

    def test_forwarded_headers_do_not_rescue_hostile_host(self):
        self.mark_state()
        response = self.post(
            "/api/verdict", GOOD_BODY,
            self.mutation_headers(extra={"X-Forwarded-Host": "127.0.0.1:{}".format(self.port),
                                         "X-Forwarded-For": "127.0.0.1"}),
            host="attacker.example")
        self.assert_rejected_without_state_change(response, 400)

    def test_hostile_host_on_get_and_head(self):
        for verb in ("GET", "HEAD"):
            status = self.raw_status(
                "{} /api/health HTTP/1.1\r\nHost: attacker.example\r\n"
                "Connection: close\r\n\r\n".format(verb).encode())
            self.assertEqual(status, 400, verb)

    def test_ipv6_bracket_host_accepted_shape(self):
        # Server listens on IPv4 only here; validate the parser directly.
        self.assertIsNone(http_guards.check_host("[::1]:{}".format(self.port), self.port))
        self.assertIsNotNone(http_guards.check_host("[::1]:1", self.port))

    def test_localhost_variants_work(self):
        status = self.raw_status(
            "GET /api/health HTTP/1.1\r\nHost: localhost:{}\r\n"
            "Connection: close\r\n\r\n".format(self.port).encode())
        self.assertEqual(status, 200)


class OriginPolicyAttacks(AdversarialBase):
    def test_null_origin_rejected(self):
        self.mark_state()
        response = self.post("/api/verdict", GOOD_BODY,
                             self.mutation_headers(origin="null"))
        self.assert_rejected_without_state_change(response, 403)

    def test_https_loopback_origin_rejected(self):
        self.mark_state()
        response = self.post("/api/verdict", GOOD_BODY,
                             self.mutation_headers(origin="https://127.0.0.1:{}".format(self.port)))
        self.assert_rejected_without_state_change(response, 403)

    def test_wrong_port_origin_rejected(self):
        self.mark_state()
        response = self.post("/api/verdict", GOOD_BODY,
                             self.mutation_headers(origin="http://127.0.0.1:1"))
        self.assert_rejected_without_state_change(response, 403)

    def test_cross_origin_referer_without_origin_rejected(self):
        self.mark_state()
        response = self.post("/api/verdict", GOOD_BODY,
                             self.mutation_headers(extra={"Referer": "https://evil.example/p"}))
        self.assert_rejected_without_state_change(response, 403)

    def test_sec_fetch_site_cross_site_rejected_even_with_good_origin(self):
        self.mark_state()
        response = self.post(
            "/api/verdict", GOOD_BODY,
            self.mutation_headers(origin="http://127.0.0.1:{}".format(self.port),
                                  extra={"Sec-Fetch-Site": "cross-site"}))
        self.assert_rejected_without_state_change(response, 403)

    def test_run_route_cross_origin_never_reaches_trigger(self):
        calls = []

        def fake_trigger(data_dir, now=None):
            calls.append(data_dir)
            return 202, {"started": True}

        with mock.patch.object(server, "trigger_run", fake_trigger):
            self.mark_state()
            response = self.post("/api/run", b"", {"X-Radar-Action": "run",
                                                   "Origin": "https://evil.example"})
            self.assert_rejected_without_state_change(response, 403)
            self.assertEqual(calls, [])
            # Authorized local non-browser client still works.
            status = self.raw_status(
                "POST /api/run HTTP/1.1\r\nHost: 127.0.0.1:{}\r\n"
                "X-Radar-Action: run\r\nContent-Length: 0\r\n"
                "Connection: close\r\n\r\n".format(self.port).encode())
            self.assertEqual(status, 202)
            self.assertEqual(len(calls), 1)


class BodyAttacks(AdversarialBase):
    def test_malformed_utf8_controlled_400(self):
        self.mark_state()
        response = self.post("/api/verdict", b'\xff\xfe{"key"}',
                             self.mutation_headers())
        self.assert_rejected_without_state_change(response, 400)

    def test_truncated_body_controlled_400(self):
        head = ("POST /api/verdict HTTP/1.1\r\nHost: 127.0.0.1:{}\r\n"
                "X-Radar-Action: verdict\r\nContent-Type: application/json\r\n"
                "Content-Length: 500\r\nConnection: close\r\n\r\n").format(self.port)
        self.mark_state()
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as sock:
            sock.sendall(head.encode() + b'{"key": "a/b"')
            sock.shutdown(socket.SHUT_WR)
            sock.settimeout(10)
            data = b""
            try:
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    data += chunk
            except socket.timeout:
                pass
        self.assert_rejected_without_state_change(data, 400)

    def test_slow_body_gets_408_within_budget(self):
        head = ("POST /api/verdict HTTP/1.1\r\nHost: 127.0.0.1:{}\r\n"
                "X-Radar-Action: verdict\r\nContent-Type: application/json\r\n"
                "Content-Length: 4000\r\nConnection: close\r\n\r\n").format(self.port)
        self.mark_state()
        with mock.patch.object(http_guards, "BODY_READ_TIMEOUT", 0.5):
            started = time.monotonic()
            with socket.create_connection(("127.0.0.1", self.port), timeout=10) as sock:
                sock.sendall(head.encode() + b'{"key":')
                sock.settimeout(10)
                data = b""
                try:
                    while True:
                        chunk = sock.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                except socket.timeout:
                    pass
            elapsed = time.monotonic() - started
        self.assert_rejected_without_state_change(data, 408)
        self.assertLess(elapsed, 5.0, "worker must be released promptly")

    def test_chunked_transfer_encoding_rejected(self):
        request = ("POST /api/verdict HTTP/1.1\r\nHost: 127.0.0.1:{}\r\n"
                   "X-Radar-Action: verdict\r\nContent-Type: application/json\r\n"
                   "Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
                   "5\r\nhello\r\n0\r\n\r\n").format(self.port).encode()
        self.mark_state()
        self.assert_rejected_without_state_change(self.raw(request), 501)

    def test_content_length_variants(self):
        for content_length in ("", "-5", "NaN", "999999999"):
            request = ("POST /api/verdict HTTP/1.1\r\nHost: 127.0.0.1:{}\r\n"
                       "X-Radar-Action: verdict\r\nContent-Type: application/json\r\n"
                       "Content-Length: {}\r\nConnection: close\r\n\r\n").format(
                           self.port, content_length).encode()
            self.mark_state()
            self.assert_rejected_without_state_change(self.raw(request), 400)

    def test_nested_container_in_raw_persisted_field(self):
        body = json.dumps({"key": "github.com/example/x", "verdict": "must_try",
                           "stars": {"$gt": 0}}).encode()
        self.mark_state()
        response = self.post("/api/verdict", body, self.mutation_headers())
        self.assert_rejected_without_state_change(response, 400)

    def test_huge_string_field_rejected(self):
        body = json.dumps({"key": "github.com/example/x", "verdict": "must_try",
                           "why": "A" * 5000}).encode()
        self.mark_state()
        response = self.post("/api/verdict", body, self.mutation_headers())
        self.assert_rejected_without_state_change(response, 400)

    def test_missing_content_type_rejected(self):
        request = ("POST /api/verdict HTTP/1.1\r\nHost: 127.0.0.1:{}\r\n"
                   "X-Radar-Action: verdict\r\n"
                   "Content-Length: {}\r\nConnection: close\r\n\r\n").format(
                       self.port, len(GOOD_BODY)).encode() + GOOD_BODY
        self.mark_state()
        self.assert_rejected_without_state_change(self.raw(request), 400)

    def test_wrong_charset_rejected(self):
        self.mark_state()
        response = self.post(
            "/api/verdict", GOOD_BODY,
            {"X-Radar-Action": "verdict",
             "Content-Type": "application/json; charset=latin-1"})
        self.assert_rejected_without_state_change(response, 400)

    def test_rejection_closes_connection(self):
        response = self.post("/api/verdict", GOOD_BODY, self.mutation_headers(),
                             host="attacker.example")
        self.assertIn(b"Connection: close", response)


class LegitimateClients(AdversarialBase):
    def test_browser_style_mutation_succeeds(self):
        response = self.post(
            "/api/verdict", GOOD_BODY,
            self.mutation_headers(origin="http://127.0.0.1:{}".format(self.port),
                                  extra={"Sec-Fetch-Site": "same-origin"}))
        self.assertIn(b" 200 ", response.split(b"\r\n", 1)[0])
        document = json.loads(self.verdicts.read_text())
        self.assertEqual(len(document["verdicts"]), 1)

    def test_curl_style_mutation_succeeds(self):
        body = json.dumps({"key": "github.com/example/second",
                           "verdict": "must_read", "resource_type": "learn"}).encode()
        response = self.post("/api/verdict", body, self.mutation_headers())
        self.assertIn(b" 200 ", response.split(b"\r\n", 1)[0])

    def test_health_probe_usable(self):
        with urllib.request.urlopen(
                "http://127.0.0.1:{}/api/health".format(self.port), timeout=10) as response:
            self.assertEqual(response.status, 200)
            payload = json.loads(response.read())
        self.assertEqual(payload["service"], "group-radar")

    def test_keep_alive_still_works_for_valid_clients(self):
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as sock:
            request = ("GET /api/health HTTP/1.1\r\nHost: 127.0.0.1:{}\r\n\r\n"
                       .format(self.port)).encode()
            for _ in range(2):
                sock.sendall(request)
                sock.settimeout(5)
                data = b""
                while b"\r\n\r\n" not in data:
                    data += sock.recv(4096)
                head, _, rest = data.partition(b"\r\n\r\n")
                length = int([l for l in head.split(b"\r\n")
                              if l.lower().startswith(b"content-length")][0].split(b":")[1])
                while len(rest) < length:
                    rest += sock.recv(4096)
                self.assertIn(b" 200 ", head.split(b"\r\n", 1)[0])


class StaticSurface(AdversarialBase):
    def test_traversal_and_encoded_paths_404(self):
        for path in ("/../etc/passwd", "/..%2f..%2fetc/passwd", "/%2e%2e/config",
                     "//etc/passwd", "/%64ashboard.html", "/config/verdicts.json",
                     "/data/group-monitor/group-monitor.sqlite3", "/worker.lock",
                     "/cron.log", "/.env", "/..\\config"):
            status = self.raw_status(
                "GET {} HTTP/1.1\r\nHost: 127.0.0.1:{}\r\nConnection: close\r\n\r\n"
                .format(path, self.port).encode())
            self.assertEqual(status, 404, path)

    def test_symlinked_route_target_is_not_served(self):
        target = self.data_dir / "verification.json"
        try:
            target.symlink_to("/etc/hosts")
            status = self.raw_status(
                "GET /verification.json HTTP/1.1\r\nHost: 127.0.0.1:{}\r\n"
                "Connection: close\r\n\r\n".format(self.port).encode())
            self.assertEqual(status, 404)
        finally:
            target.unlink()

    def test_html_gets_csp_and_nosniff(self):
        (self.data_dir / "dashboard.html").write_text("<html>x</html>")
        try:
            response = self.raw(
                "GET /dashboard.html HTTP/1.1\r\nHost: 127.0.0.1:{}\r\n"
                "Connection: close\r\n\r\n".format(self.port).encode())
            self.assertIn(b"Content-Security-Policy", response)
            self.assertIn(b"X-Content-Type-Options: nosniff", response)
        finally:
            (self.data_dir / "dashboard.html").unlink()

    def test_method_abuse_rejected(self):
        for verb in ("PUT", "DELETE", "PATCH", "OPTIONS"):
            status = self.raw_status(
                "{} /status.json HTTP/1.1\r\nHost: 127.0.0.1:{}\r\n"
                "Connection: close\r\n\r\n".format(verb, self.port).encode())
            self.assertEqual(status, 405, verb)

    def test_options_preflight_gets_no_cors_grant(self):
        response = self.raw(
            "OPTIONS /api/verdict HTTP/1.1\r\nHost: 127.0.0.1:{}\r\n"
            "Origin: https://evil.example\r\n"
            "Access-Control-Request-Method: POST\r\n"
            "Access-Control-Request-Headers: x-radar-action\r\n"
            "Connection: close\r\n\r\n".format(self.port).encode())
        self.assertNotIn(b"Access-Control-Allow", response)


if __name__ == "__main__":
    unittest.main()
