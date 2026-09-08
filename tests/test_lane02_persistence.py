"""Lane 02 acceptance tests — A02/A06/A10 durable decision persistence.

Everything runs against temporary directories; two-process tests use real
separate OS processes via subprocess (not threads, not fork-with-mocks), so
the inter-process guarantees are exercised for real.
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import decision_store  # noqa: E402
import radar_server as server  # noqa: E402


CONCURRENT_WRITER = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
import radar_server as server
from pathlib import Path
server.VERDICTS_PATH = Path(sys.argv[2])
prefix, count = sys.argv[3], int(sys.argv[4])
codes = []
for index in range(count):
    code, payload = server.record_verdict(
        {"key": "github.com/{}/tool{}".format(prefix, index), "verdict": "must_try"})
    codes.append(int(code))
print(json.dumps(codes))
"""

SAME_KEY_WRITER = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
import radar_server as server
from pathlib import Path
server.VERDICTS_PATH = Path(sys.argv[2])
code, payload = server.record_verdict(
    {"key": "github.com/shared/tool", "verdict": sys.argv[3], "why": sys.argv[4]})
print(json.dumps({"code": int(code), "revision": payload.get("revision")}))
"""

KILL_TARGET_WRITER = r"""
import sys
sys.path.insert(0, sys.argv[1])
import radar_server as server
from pathlib import Path
server.VERDICTS_PATH = Path(sys.argv[2])
index = 0
print("ready", flush=True)
while True:
    server.record_verdict({"key": "github.com/spin/tool{}".format(index % 7), "verdict": "must_try"})
    index += 1
"""


def run_worker(script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", script, str(SCRIPTS), *args],
        capture_output=True, text=True, timeout=60, check=False,
    )


class TempStoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.verdicts = base / "verdicts.json"
        self.outcomes = base / "outcomes.json"
        self.tool_index = base / "dashboard-data.json"
        self.patches = [
            mock.patch.object(server, "VERDICTS_PATH", self.verdicts),
            mock.patch.object(server, "OUTCOMES_PATH", self.outcomes),
            mock.patch.object(server, "TOOL_INDEX_PATH", self.tool_index),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()


class A02TwoRealProcesses(TempStoreCase):
    def test_interleaved_processes_lose_nothing(self):
        """Two separate OS processes, 12 writes each, all 24 must survive."""
        first = threading.Thread(target=lambda: results.append(
            run_worker(CONCURRENT_WRITER, str(self.verdicts), "alpha", "12")))
        second = threading.Thread(target=lambda: results.append(
            run_worker(CONCURRENT_WRITER, str(self.verdicts), "beta", "12")))
        results = []
        first.start(); second.start(); first.join(); second.join()
        for proc in results:
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout), [200] * 12, proc.stderr)
        document = json.loads(self.verdicts.read_text())
        keys = {entry["key"] for entry in document["verdicts"]}
        expected = {"github.com/{}/tool{}".format(p, i)
                    for p in ("alpha", "beta") for i in range(12)}
        self.assertEqual(keys, expected)
        self.assertEqual(document["revision"], 24, "every commit bumps the revision once")

    def test_same_key_concurrency_is_deterministic(self):
        """Same key from two processes: exactly one entry survives, both
        versions stay recoverable in history, revision counts both commits."""
        threads, results = [], []
        for verdict, why in (("must_try", "process-a"), ("excluded", "process-b")):
            threads.append(threading.Thread(target=lambda v=verdict, w=why: results.append(
                run_worker(SAME_KEY_WRITER, str(self.verdicts), v, w))))
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        for proc in results:
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout)["code"], 200)
        document = json.loads(self.verdicts.read_text())
        entries = [e for e in document["verdicts"] if e["key"] == "github.com/shared/tool"]
        self.assertEqual(len(entries), 1, "last writer wins, exactly one entry")
        self.assertEqual(document["revision"], 2)
        history = self.verdicts.parent / "_history" / "verdicts"
        self.assertTrue(history.is_dir())
        # First commit archives the pre-existing state (nothing on first save
        # of a missing file), second commit archives the first version: the
        # losing same-key version is recoverable, with provenance explaining it.
        archived = list(history.glob("*.json"))
        self.assertEqual(len(archived), 1)
        first_version = json.loads(archived[0].read_text())
        self.assertEqual(len(first_version["verdicts"]), 1)

    def test_killed_writer_leaves_valid_document_and_free_lock(self):
        process = subprocess.Popen(
            [sys.executable, "-c", KILL_TARGET_WRITER, str(SCRIPTS), str(self.verdicts)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            self.assertEqual(process.stdout.readline().strip(), "ready")
            deadline = time.monotonic() + 10
            while not self.verdicts.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            time.sleep(0.15)  # let it get into the middle of the write loop
        finally:
            process.send_signal(signal.SIGKILL)
            process.wait(timeout=10)
            process.stdout.close()
            process.stderr.close()
        document = json.loads(self.verdicts.read_text())
        self.assertIsInstance(document["verdicts"], list, "old or new, never torn")
        code, payload = server.record_verdict(
            {"key": "github.com/after/kill", "verdict": "must_try"})
        self.assertEqual(code, 200, "flock must be released by process death: {}".format(payload))

    def test_lock_timeout_is_visible_not_a_hang(self):
        import fcntl
        lock_file = self.verdicts.with_name(self.verdicts.name + ".lock")
        handle = lock_file.open("a+b")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            with mock.patch.object(decision_store, "LOCK_TIMEOUT_SECONDS", 0.3):
                started = time.monotonic()
                code, payload = server.record_verdict(
                    {"key": "github.com/a/b", "verdict": "must_try"})
                elapsed = time.monotonic() - started
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
        self.assertEqual(code, 503)
        self.assertIn("lock", payload["error"])
        self.assertLess(elapsed, 5, "bounded wait, not a stuck writer")
        self.assertFalse(self.verdicts.exists(), "no write happened under contention")


class A06CorruptionAndAbsence(TempStoreCase):
    CORRUPTIONS = (
        b"{corrupt existing history",
        b"[1, 2, 3]",
        b'{"version": 1}',
        b'{"version": 1, "verdicts": {"not": "a list"}}',
        b"\xff\xfe\x00garbage",
    )

    def test_corrupt_variants_fail_closed_byte_identical(self):
        for corrupt in self.CORRUPTIONS:
            with self.subTest(corrupt=corrupt[:20]):
                self.verdicts.write_bytes(corrupt)
                code, payload = server.record_verdict(
                    {"key": "github.com/a/tool", "verdict": "must_try"})
                self.assertGreaterEqual(int(code), 400)
                self.assertIn("error", payload)
                self.assertEqual(self.verdicts.read_bytes(), corrupt)
                history = self.verdicts.parent / "_history" / "verdicts"
                self.assertFalse(list(history.glob("*.json")) if history.exists() else [],
                                 "a rejected write must not archive anything")
                self.verdicts.unlink()

    def test_absence_is_not_corruption(self):
        code, payload = server.record_verdict(
            {"key": "github.com/a/tool", "verdict": "must_try"})
        self.assertEqual(code, 200)
        self.assertEqual(payload["revision"], 1)
        document = json.loads(self.verdicts.read_text())
        self.assertEqual(len(document["verdicts"]), 1)

    def test_legacy_document_without_revision_stays_readable(self):
        legacy = {"version": 1, "updated_at": "2026-08-01", "verdicts": [
            {"key": "github.com/old/entry", "verdict": "must_read", "name": "old"}]}
        self.verdicts.write_text(json.dumps(legacy))
        code, payload = server.record_verdict(
            {"key": "github.com/new/entry", "verdict": "excluded"})
        self.assertEqual(code, 200)
        self.assertEqual(payload["revision"], 1, "legacy revision counts from 0")
        document = json.loads(self.verdicts.read_text())
        self.assertEqual({e["key"] for e in document["verdicts"]},
                         {"github.com/old/entry", "github.com/new/entry"})

    def test_history_is_pruned_but_recent_versions_remain(self):
        for index in range(decision_store.HISTORY_KEEP + 5):
            code, _ = server.record_verdict(
                {"key": "github.com/spin/tool{}".format(index), "verdict": "must_try"})
            self.assertEqual(code, 200)
        history = self.verdicts.parent / "_history" / "verdicts"
        versions = list(history.glob("*.json"))
        self.assertEqual(len(versions), decision_store.HISTORY_KEEP)


class A10RevisionAndReadBack(TempStoreCase):
    def test_mutation_returns_authoritative_record_and_revision(self):
        code, payload = server.record_verdict(
            {"key": "github.com/a/tool", "verdict": "must_try", "why": "fits"})
        self.assertEqual(code, 200)
        self.assertEqual(payload["revision"], 1)
        self.assertEqual(payload["record"]["key"], "github.com/a/tool")
        self.assertEqual(payload["record"]["decided_by"], "dashboard")
        on_disk = json.loads(self.verdicts.read_text())["verdicts"][0]
        self.assertEqual(on_disk, payload["record"], "response record IS the stored record")
        code, payload = server.record_verdict({"key": "github.com/a/tool", "verdict": "clear"})
        self.assertEqual(code, 200)
        self.assertIsNone(payload["record"])
        self.assertEqual(payload["revision"], 2)

    def test_expected_revision_conflict_answer(self):
        server.record_verdict({"key": "github.com/a/tool", "verdict": "must_try"})
        code, payload = server.record_verdict(
            {"key": "github.com/a/tool", "verdict": "excluded", "expected_revision": 0})
        self.assertEqual(code, 409)
        self.assertEqual(payload["current_revision"], 1)
        self.assertEqual(json.loads(self.verdicts.read_text())["verdicts"][0]["verdict"],
                         "must_try", "conflicting write must not land")
        code, payload = server.record_verdict(
            {"key": "github.com/a/tool", "verdict": "excluded", "expected_revision": 1})
        self.assertEqual(code, 200)
        self.assertEqual(payload["revision"], 2)
        self.assertEqual(server.record_verdict(
            {"key": "github.com/a/b", "verdict": "must_try",
             "expected_revision": "yes"})[0], 400)

    def test_read_decisions_shows_save_immediately(self):
        server.record_verdict({"key": "github.com/a/tool", "verdict": "must_try"})
        server.record_outcome({"key": "github.com/a/tool", "state": "trying"})
        code, payload = server.read_decisions()
        self.assertEqual(code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["verdicts_document"]["verdicts"][0]["key"], "github.com/a/tool")
        self.assertEqual(payload["outcomes_document"]["outcomes"][0]["state"], "trying")
        self.assertEqual(payload["revision"], {"verdicts": 1, "outcomes": 1})

    def test_read_decisions_absent_files_are_empty_not_errors(self):
        code, payload = server.read_decisions()
        self.assertEqual(code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["verdicts_document"]["verdicts"], [])
        self.assertEqual(payload["revision"], {"verdicts": 0, "outcomes": 0})

    def test_read_decisions_is_honest_about_corruption(self):
        self.verdicts.write_bytes(b"{broken")
        server.record_outcome({"key": "github.com/a/tool", "state": "kept"})
        code, payload = server.read_decisions()
        self.assertEqual(code, 200)
        self.assertFalse(payload["ok"])
        self.assertIn("verdicts", payload["errors"])
        self.assertIsNone(payload["verdicts_document"])
        self.assertEqual(payload["outcomes_document"]["outcomes"][0]["key"], "github.com/a/tool")

    def test_failed_save_never_reports_success(self):
        self.verdicts.write_bytes(b"{broken")
        code, payload = server.record_verdict(
            {"key": "github.com/a/tool", "verdict": "must_try"})
        self.assertGreaterEqual(code, 400)
        self.assertNotIn("ok", payload)
        self.assertNotIn("revision", payload)


class ServerSideTyping(TempStoreCase):
    def _write_index(self, resource_type: str) -> None:
        self.tool_index.write_text(json.dumps({
            "tools": [{"key": "github.com/typed/thing", "resource_type": resource_type}]}))

    def test_type_resolved_from_server_index_when_client_omits_it(self):
        self._write_index("read")
        code, payload = server.record_verdict(
            {"key": "github.com/typed/thing", "verdict": "must_try"})
        self.assertEqual(code, 409, "server-resolved type must enforce the same rule")
        self.assertIn("hint", payload)
        code, payload = server.record_verdict(
            {"key": "github.com/typed/thing", "verdict": "must_read"})
        self.assertEqual(code, 200)
        self.assertEqual(payload["record"]["resource_type"], "read")
        self.assertEqual(payload["record"]["resource_type_source"], "server_index")

    def test_unknown_resource_type_token_is_rejected(self):
        self.assertEqual(server.record_verdict(
            {"key": "github.com/a/b", "verdict": "must_try",
             "resource_type": "banana"})[0], 400)

    def test_missing_or_broken_index_never_blocks_a_save(self):
        self.tool_index.write_bytes(b"{broken index")
        code, _ = server.record_verdict(
            {"key": "github.com/untyped/thing", "verdict": "must_try"})
        self.assertEqual(code, 200)

    def test_body_cannot_forge_provenance(self):
        code, payload = server.record_verdict(
            {"key": "github.com/a/tool", "verdict": "must_try",
             "decided_by": "telegram", "actor": "impostor", "source_event": "faked"})
        self.assertEqual(code, 200)
        self.assertEqual(payload["record"]["decided_by"], "dashboard")
        self.assertNotIn("actor", payload["record"])
        self.assertNotIn("source_event", payload["record"])


class SeparationOfFiles(TempStoreCase):
    def test_clearing_verdict_keeps_trial_and_both_revisions_independent(self):
        server.record_verdict({"key": "github.com/a/tool", "verdict": "must_try"})
        server.record_outcome({"key": "github.com/a/tool", "state": "kept",
                               "note": "measured win"})
        code, _ = server.record_verdict({"key": "github.com/a/tool", "verdict": "clear"})
        self.assertEqual(code, 200)
        outcomes = json.loads(self.outcomes.read_text())
        self.assertEqual(outcomes["outcomes"][0]["state"], "kept")
        self.assertEqual(outcomes["revision"], 1, "outcome file untouched by verdict clear")


class ReadBackOverHTTP(unittest.TestCase):
    """Save -> fresh HTTP read-back -> server restart -> still there.

    Uses a lane-local test handler that adds the proposed GET /api/decisions
    route on top of the frozen RadarHandler; the production registration of
    that route belongs to the coordinator (07), not this lane.
    """

    class DecisionsHandler(server.RadarHandler):
        def do_GET(self):  # noqa: N802
            if self.path.split("?", 1)[0] == "/api/decisions":
                code, payload = server.read_decisions()
                self._send_json(code, payload)
                return
            super().do_GET()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.verdicts = base / "verdicts.json"
        self.outcomes = base / "outcomes.json"
        self.patches = [
            mock.patch.object(server, "VERDICTS_PATH", self.verdicts),
            mock.patch.object(server, "OUTCOMES_PATH", self.outcomes),
            mock.patch.object(server, "TOOL_INDEX_PATH", base / "dashboard-data.json"),
        ]
        for p in self.patches:
            p.start()
        self.DecisionsHandler.data_dir = base
        self.httpd = None
        self.thread = None

    def _start(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self.DecisionsHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self.httpd.server_address[1]

    def _stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.thread.join(timeout=5)
            self.httpd = None

    def tearDown(self):
        self._stop()
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def _post_verdict(self, port, body):
        request = urllib.request.Request(
            "http://127.0.0.1:{}/api/verdict".format(port),
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Radar-Action": "verdict"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def _get_decisions(self, port):
        with urllib.request.urlopen(
                "http://127.0.0.1:{}/api/decisions".format(port), timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def test_save_readback_and_server_restart(self):
        port = self._start()
        status, saved = self._post_verdict(
            port, {"key": "github.com/http/tool", "verdict": "must_try"})
        self.assertEqual(status, 200)
        self.assertEqual(saved["revision"], 1)
        fresh = self._get_decisions(port)  # a second, fresh client
        self.assertEqual(fresh["verdicts_document"]["verdicts"][0]["key"], "github.com/http/tool")
        self.assertEqual(fresh["revision"]["verdicts"], 1)
        self._stop()
        port = self._start()  # server restart: durable, not in-memory
        again = self._get_decisions(port)
        self.assertEqual(again["verdicts_document"]["verdicts"][0]["key"], "github.com/http/tool")


if __name__ == "__main__":
    unittest.main()
