"""Lane 05 tests for the C6 safe-fetch provider.

All network activity is loopback-only fixture servers (harness-compatible).
Because production policy denies loopback, tests widen `_address_permitted`
through a monkeypatch seam to admit loopback ONLY — every denial asserted
here therefore comes from safe_fetch's own logic, not the harness guard.
"""
import gzip
import http.server
import ipaddress
import json
import socket
import ssl
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

LANE = Path(__file__).resolve().parents[1]
def _find_run_root(start):
    # Integration-tree repair by Chat 07 (2026-09-07): lanes sit at
    # <run>/workers/NN but integration at <run>/integration, so a fixed
    # parents[] hop cannot serve both. Walk up to the contracts dir instead
    # (same pattern as test_lane04_provider_contract).
    for parent in [start] + list(start.parents):
        if (parent / "contracts" / "fixtures").is_dir():
            return parent
    return start.parents[3]

RUN = _find_run_root(Path(__file__).resolve().parent)
FIX = RUN / "contracts" / "fixtures"
TLS_DIR = Path(__file__).resolve().parent / "fixtures" / "tls"
sys.path.insert(0, str(LANE / "scripts"))

import safe_fetch as sf  # noqa: E402

SPEC = json.loads((FIX / "c6-safe-fetch.json").read_text())
RESULT_KEYS = set(SPEC["result_keys"])
DENIED = set(SPEC["denied_reasons"])

BOMB_PLAIN_BYTES = 8_000_000
GZIP_BOMB = gzip.compress(b"\0" * BOMB_PLAIN_BYTES)
SMALL_GZIP_TEXT = "مرحبا safe-fetch"  # Arabic exercises UTF-8 decode
SMALL_GZIP = gzip.compress(SMALL_GZIP_TEXT.encode("utf-8"))
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"x" * 64


class FixtureHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, status, body, content_type="text/html; charset=utf-8", extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        route = self.path.split("?", 1)[0]
        if route == "/ok":
            self._send(200, "<html>hello صديقي</html>".encode("utf-8"))
        elif route == "/json":
            self._send(200, b'{"ok": true}', "application/json")
        elif route == "/binary":
            self._send(200, PNG_BYTES, "image/png")
        elif route == "/big":
            self._send(200, b"A" * 5000, "text/plain")
        elif route == "/gzip-ok":
            self._send(200, SMALL_GZIP, "text/plain; charset=utf-8",
                       {"Content-Encoding": "gzip"})
        elif route == "/gzip-bomb":
            self._send(200, GZIP_BOMB, "text/plain", {"Content-Encoding": "gzip"})
        elif route == "/enc-br":
            self._send(200, b"xxxx", "text/plain", {"Content-Encoding": "br"})
        elif route == "/redirect-ok":
            self._send(302, b"", extra={"Location": "/ok"})
        elif route == "/redirect-private":
            self._send(302, b"", extra={"Location": "http://10.99.99.99/x"})
        elif route == "/redirect-scheme":
            self._send(302, b"", extra={"Location": "ftp://example.com/x"})
        elif route == "/redirect-loop":
            self._send(302, b"", extra={"Location": "/redirect-loop"})
        elif route == "/no-length":
            # Bounded read must work without Content-Length too.
            body = b"stream" * 10
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True
        elif route == "/slow":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "10000")
            self.end_headers()
            self.wfile.write(b"x" * 100)
            self.wfile.flush()
            time.sleep(5)
        else:
            self._send(404, b"nope")

    def log_message(self, *args):  # silence
        pass


def _allow_loopback(ip):
    return ip.is_loopback or sf_original_policy(ip)


sf_original_policy = sf._address_permitted


class SafeFetchBase(unittest.TestCase):
    """Shared plumbing: fixture server + loopback-permitting policy seam."""

    @classmethod
    def setUpClass(cls):
        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.policy = mock.patch.object(sf, "_address_permitted", _allow_loopback)
        cls.policy.start()

    @classmethod
    def tearDownClass(cls):
        cls.policy.stop()
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def url(self, route):
        return "http://127.0.0.1:{}{}".format(self.port, route)

    def fetch(self, *args, **kwargs):
        result = sf.safe_fetch(*args, **kwargs)
        self.assertEqual(set(result), RESULT_KEYS, "result shape must match C6 fixture")
        if result["denied_reason"] is not None:
            self.assertIn(result["denied_reason"], DENIED)
        return result


class PolicyUnit(unittest.TestCase):
    """No network: the address policy itself."""

    def test_denied_addresses(self):
        for raw in ("127.0.0.1", "10.0.0.1", "172.16.5.5", "192.168.1.1",
                    "169.254.169.254", "100.64.0.1", "0.0.0.0", "::1",
                    "fd00:ec2::254", "fe80::1", "::ffff:127.0.0.1",
                    "::ffff:10.0.0.1", "64:ff9b::7f00:1", "64:ff9b::a00:1",
                    "255.255.255.255", "224.0.0.1"):
            self.assertFalse(sf._address_permitted(ipaddress.ip_address(raw)), raw)

    def test_permitted_addresses(self):
        for raw in ("8.8.8.8", "1.1.1.1", "2606:4700::1111", "64:ff9b::808:808"):
            self.assertTrue(sf._address_permitted(ipaddress.ip_address(raw)), raw)


class NoNetworkDenials(SafeFetchBase):
    def test_scheme_denials(self):
        for url in ("ftp://example.com/a", "file:///etc/passwd", "gopher://x/1",
                    "javascript:alert(1)", "data:text/html,hi"):
            result = self.fetch(url)
            self.assertFalse(result["ok"])
            self.assertEqual(result["denied_reason"], "scheme", url)
            self.assertIsNone(result["status"])

    def test_userinfo_denied(self):
        result = self.fetch("http://user:pw@127.0.0.1:{}/ok".format(self.port))
        self.assertEqual(result["denied_reason"], "scheme")

    def test_no_host_is_error(self):
        self.assertEqual(self.fetch("http:///nohost")["denied_reason"], "error")

    def test_metadata_ip_matches_fixture_sample(self):
        with mock.patch.object(sf, "_address_permitted", sf_original_policy):
            result = self.fetch("http://169.254.169.254/meta")
        sample = SPEC["samples"]["denied_private"]
        for key, value in sample.items():
            self.assertEqual(result[key], value, key)

    def test_private_literals_denied_without_connecting(self):
        with mock.patch.object(sf, "_address_permitted", sf_original_policy):
            for target in ("http://127.0.0.1:{}/ok".format(self.port),
                           "http://10.0.0.8/", "http://192.168.1.1/",
                           "http://[::1]/", "http://[fd00:ec2::254]/"):
                result = self.fetch(target)
                self.assertEqual(result["denied_reason"], "private_target", target)

    def test_mixed_dns_answer_is_denied(self):
        def rebind(host, port, *args, **kwargs):
            self.assertEqual(host, "rebind.example")
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),
            ]

        with mock.patch.object(sf, "_address_permitted", sf_original_policy), \
             mock.patch.object(socket, "getaddrinfo", side_effect=rebind):
            result = self.fetch("http://rebind.example/")
        self.assertEqual(result["denied_reason"], "private_target")

    def test_dns_failure_is_error(self):
        def boom(host, *args, **kwargs):
            raise socket.gaierror(8, "nodename nor servname provided")

        with mock.patch.object(socket, "getaddrinfo", side_effect=boom):
            result = self.fetch("http://unresolvable.invalid/")
        self.assertEqual(result["denied_reason"], "error")
        self.assertIn("resolve", result["error"])


class FetchBehavior(SafeFetchBase):
    def test_success_text(self):
        result = self.fetch(self.url("/ok"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], 200)
        self.assertIn("hello صديقي", result["text"])
        self.assertIsNone(result["body_path"])
        self.assertEqual(result["final_url"], self.url("/ok"))
        self.assertGreater(result["bytes"], 0)
        self.assertIsNone(result["denied_reason"])

    def test_dest_dir_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.fetch(self.url("/binary"), dest_dir=tmp)
            self.assertTrue(result["ok"])
            path = Path(result["body_path"])
            self.assertTrue(path.is_file())
            self.assertEqual(path.suffix, ".png")
            self.assertEqual(path.read_bytes(), PNG_BYTES)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertIsNone(result["text"])
            self.assertTrue(str(path).startswith(tmp))

    def test_binary_without_dest_dir_returns_no_text(self):
        result = self.fetch(self.url("/binary"))
        self.assertTrue(result["ok"])
        self.assertIsNone(result["text"])
        self.assertIsNone(result["body_path"])
        self.assertEqual(result["bytes"], len(PNG_BYTES))

    def test_content_type_allowlist(self):
        denied = self.fetch(self.url("/json"), allowed_content_types=("text/html",))
        self.assertEqual(denied.get("denied_reason"), "content_type")
        self.assertEqual(denied["status"], 200)
        allowed = self.fetch(self.url("/binary"), allowed_content_types=("image/",))
        self.assertTrue(allowed["ok"])

    def test_oversize_declared_length(self):
        result = self.fetch(self.url("/big"), max_bytes=1000)
        self.assertEqual(result["denied_reason"], "too_large")
        self.assertFalse(result["ok"])

    def test_no_content_length_stream_is_read(self):
        result = self.fetch(self.url("/no-length"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["bytes"], 60)

    def test_gzip_decodes_within_budget(self):
        result = self.fetch(self.url("/gzip-ok"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], SMALL_GZIP_TEXT)
        self.assertEqual(result["bytes"], len(SMALL_GZIP_TEXT.encode("utf-8")))

    def test_gzip_bomb_is_bounded(self):
        result = self.fetch(self.url("/gzip-bomb"), max_bytes=100_000)
        self.assertEqual(result["denied_reason"], "too_large")
        self.assertEqual(result["bytes"], 100_001)
        self.assertLess(len(GZIP_BOMB), 100_000,
                        "bomb must be small on the wire to prove decompression bounding")

    def test_unsupported_content_encoding_denied(self):
        result = self.fetch(self.url("/enc-br"))
        self.assertEqual(result["denied_reason"], "content_type")

    def test_redirect_followed_and_revalidated(self):
        result = self.fetch(self.url("/redirect-ok"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["final_url"], self.url("/ok"))

    def test_redirect_to_private_denied(self):
        result = self.fetch(self.url("/redirect-private"))
        self.assertEqual(result["denied_reason"], "redirect_target")

    def test_redirect_to_bad_scheme_denied(self):
        result = self.fetch(self.url("/redirect-scheme"))
        self.assertEqual(result["denied_reason"], "redirect_target")

    def test_redirect_budget(self):
        result = self.fetch(self.url("/redirect-loop"), max_redirects=3)
        self.assertEqual(result["denied_reason"], "redirect_target")

    def test_slow_body_times_out(self):
        started = time.monotonic()
        result = self.fetch(self.url("/slow"), timeout=1.0)
        elapsed = time.monotonic() - started
        self.assertEqual(result["denied_reason"], "timeout")
        self.assertLess(elapsed, 4.0, "budget must cut the read off")

    def test_unresponsive_server_times_out(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        def hold():
            try:
                conn, _ = listener.accept()
                time.sleep(3)
                conn.close()
            except OSError:
                pass

        holder = threading.Thread(target=hold, daemon=True)
        holder.start()
        try:
            result = self.fetch("http://127.0.0.1:{}/".format(port), timeout=1.0)
            self.assertEqual(result["denied_reason"], "timeout")
        finally:
            listener.close()

    def test_connection_refused_is_error(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
        probe.close()
        result = self.fetch("http://127.0.0.1:{}/".format(free_port), timeout=2.0)
        self.assertEqual(result["denied_reason"], "error")


class TlsBehavior(SafeFetchBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tls_httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        good = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        good.load_cert_chain(str(TLS_DIR / "localhost.pem"), str(TLS_DIR / "localhost.key"))
        cls.tls_httpd.socket = good.wrap_socket(cls.tls_httpd.socket, server_side=True)
        cls.tls_port = cls.tls_httpd.server_address[1]
        threading.Thread(target=cls.tls_httpd.serve_forever, daemon=True).start()

        cls.bad_httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        bad = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        bad.load_cert_chain(str(TLS_DIR / "wronghost.pem"), str(TLS_DIR / "wronghost.key"))
        cls.bad_httpd.socket = bad.wrap_socket(cls.bad_httpd.socket, server_side=True)
        cls.bad_port = cls.bad_httpd.server_address[1]
        threading.Thread(target=cls.bad_httpd.serve_forever, daemon=True).start()

        client = ssl.create_default_context(cafile=str(TLS_DIR / "localhost.pem"))
        cls.ctx_patch = mock.patch.object(sf, "_ssl_context", lambda: client)
        cls.ctx_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls.ctx_patch.stop()
        for httpd in (cls.tls_httpd, cls.bad_httpd):
            httpd.shutdown()
            httpd.server_close()
        super().tearDownClass()

    def test_https_verifies_and_fetches(self):
        result = self.fetch("https://localhost:{}/ok".format(self.tls_port))
        self.assertTrue(result["ok"], result["error"])
        self.assertIn("hello", result["text"])

    def test_https_wrong_certificate_fails_closed(self):
        result = self.fetch("https://localhost:{}/ok".format(self.bad_port))
        self.assertFalse(result["ok"])
        self.assertEqual(result["denied_reason"], "error")
        self.assertIn("tls", (result["error"] or "").lower())


if __name__ == "__main__":
    unittest.main()
