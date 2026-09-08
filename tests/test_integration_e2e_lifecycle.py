"""Chat 07 combined end-to-end lifecycle drill (Phase C.3/C.4).

One isolated temp project tree, real integrated code end to end:
mixed all-sender capture (group tweet + overlapping bookmark + media-only +
generic link + note) -> extraction evidence -> export (incl. the A07 safe
sheet) -> HTTP verdict/outcome with read-back -> server restart durability ->
replay idempotence -> consistent backup and restore into a fresh directory.

Relevance classification is SIMULATED by direct status updates (the semantic
stage is model/human-gated and out of scope here); everything else runs the
real providers. Nothing touches live data; server binds port 0.
"""
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

LANE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(LANE / "scripts"))

import content_extraction as cx  # noqa: E402
import group_monitor as gm  # noqa: E402
import ingest_bookmarks as ingest  # noqa: E402
import radar_backup  # noqa: E402
import radar_server as server  # noqa: E402

PROFILE = {
    "conversation": {"id": "g1", "name": "E2E Group", "capture_scope": "all_senders"},
    "owners": [{"sender_id": "42", "username": "owner"}],
    "bootstrap": {"resume_after_message_id": "100"},
    "selection": {"minimum_score": 3, "ai_weight": 4, "ai_terms": ["ai"],
                  "project_areas": {}},
}


def message(mid, sender, text="", urls=(), attachment=None):
    doc = {"id": mid, "time": int(mid), "sender_id": sender, "text": text,
           "urls": list(urls)}
    if attachment:
        doc["attachment"] = attachment
    return doc


def batch(messages):
    return gm.FetchResult(messages=list(messages), pages=1,
                          reached_checkpoint=True,
                          newest_message_id=messages[-1]["id"],
                          oldest_message_id=messages[0]["id"])


def fake_fetch(url, **kwargs):
    if url.endswith(".jpg"):
        return {"ok": True, "url": url, "final_url": url, "status": 200,
                "content_type": "image/jpeg", "bytes": 2048, "body_path": None,
                "text": None, "error": None, "denied_reason": None}
    return {"ok": True, "url": url, "final_url": url, "status": 200,
            "content_type": "text/html; charset=utf-8", "bytes": 512,
            "body_path": None,
            "text": "<html><title>Synthetic AI tool</title><body>agent workflow tool</body></html>",
            "error": None, "denied_reason": None}


class E2ELifecycle(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name) / "project"
        cls.data = cls.root / "data" / "group-monitor"
        cls.config = cls.root / "config"
        cls.data.mkdir(parents=True)
        cls.config.mkdir(parents=True)
        (cls.config / "group-filter-profile.json").write_text(
            json.dumps(PROFILE), encoding="utf-8")
        cls.patches = [
            mock.patch.object(gm, "DATA_DIR", cls.data),
            mock.patch.object(gm, "DB_PATH", cls.data / "group-monitor.sqlite3"),
            mock.patch.object(gm, "VERDICTS_PATH", cls.config / "verdicts.json"),
            mock.patch.object(gm, "OUTCOMES_PATH", cls.config / "outcomes.json"),
            mock.patch.object(server, "VERDICTS_PATH", cls.config / "verdicts.json"),
            mock.patch.object(server, "OUTCOMES_PATH", cls.config / "outcomes.json"),
            mock.patch.object(server, "PROFILE_PATH",
                              cls.config / "group-filter-profile.json"),
        ]
        for patch in cls.patches:
            patch.start()
        cls.conn = gm.connect_db(cls.data / "group-monitor.sqlite3")

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        for patch in cls.patches:
            patch.stop()
        cls._tmp.cleanup()

    # helpers -----------------------------------------------------------
    def occurrences(self, resource_id):
        return {row[0] for row in self.conn.execute(
            "SELECT message_id FROM message_resources WHERE resource_id = ?",
            (resource_id,))}

    def start_server(self):
        server.RadarHandler.data_dir = self.data
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.RadarHandler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        return httpd, httpd.server_address[1]

    def http(self, port, path, method="GET", body=None, headers=None):
        base = {"Host": "127.0.0.1:{}".format(port)}
        base.update(headers or {})
        request = urllib.request.Request(
            "http://127.0.0.1:{}{}".format(port, path), data=body,
            method=method, headers=base)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    # the drill ---------------------------------------------------------
    def test_full_lifecycle(self):
        conn = self.conn
        # 1. CAPTURE: bookmark first, then mixed all-sender group batch.
        ingest.ingest(conn, PROFILE, [{"id": "555", "text": "AI tool bookmark"}],
                      ingest.SOURCE_ARCHIVE, True, progress_every=0)
        group = batch([
            message("201", "77",
                    text="https://x.com/i/status/555",
                    urls=["https://x.com/i/status/555"]),
            message("202", "88",
                    attachment={"photo": {"url": "https://pbs.twimg.com/media/e2e.jpg"}}),
            message("203", "42", text="https://example.com/ai-agent-guide",
                    urls=["https://example.com/ai-agent-guide"]),
            message("204", "99", text="thought: try smaller batches for ai runs"),
        ])
        gm.persist_fetch(conn, PROFILE, group, "100")

        self.assertEqual(self.occurrences("tweet:555"), {"bookmark:555", "201"},
                         "overlapping bookmark+group must keep BOTH occurrences")
        senders = {row[0] for row in conn.execute("SELECT sender_id FROM messages")}
        self.assertEqual(senders, {"bookmark", "77", "88", "42", "99"})
        media_rows = conn.execute(
            "SELECT resource_id, extraction_state FROM resources "
            "WHERE resource_id LIKE 'media:%'").fetchall()
        self.assertEqual(len(media_rows), 1,
                         "media-only share must persist as a discoverable resource")
        cursor = gm.get_meta(conn, "fetch_cursor")
        self.assertEqual(cursor, "204", "cursor advances to the newest message")

        # 2. EXTRACTION through the C6 contract shape (deterministic fetcher).
        summary = cx.extract_pending_content(conn, fetcher=fake_fetch)
        self.assertGreaterEqual(summary.get("ok", 0), 1)
        state = conn.execute(
            "SELECT extraction_state FROM resources WHERE canonical_url = ?",
            ("https://example.com/ai-agent-guide",)).fetchone()[0]
        self.assertEqual(state, "ok")

        # 3. RELEVANCE (simulated; semantic stage is gated elsewhere).
        with conn:
            conn.execute(
                "UPDATE resources SET status='relevant', score=6, "
                "project_areas_json='[\"ai\"]', reasons_json='[\"e2e drill\"]', "
                "decision_source='rules' WHERE status IN "
                "('pending_review','pending_hydration')")

        # 4. EXPORT: all artifacts incl. the A07 human-safe sheet.
        export = gm.export_relevant(conn, PROFILE)
        for name in ("dashboard.html", "dashboard-data.json", "relevant.csv",
                     "all-resources.csv", "relevant.jsonl", "latest.md",
                     "relevant-sheet.csv"):
            self.assertTrue((self.data / name).is_file(), name)
        payload = json.loads((self.data / "dashboard-data.json").read_text())
        ids = {r["resource_id"] for r in payload["resources"]}
        self.assertIn("tweet:555", ids)
        self.assertTrue(any(i.startswith("media:") for i in ids),
                        "media share must reach the dashboard payload")
        row555 = next(r for r in payload["resources"]
                      if r["resource_id"] == "tweet:555")
        self.assertEqual(row555["share_count"], 2)

        # 5. DECISIONS over HTTP with read-back.
        httpd, port = self.start_server()
        try:
            body = json.dumps({"key": "github.com/e2e/synthetic-tool",
                               "verdict": "must_try", "resource_type": "try",
                               "name": "e2e tool"}).encode()
            status, response = self.http(
                port, "/api/verdict", "POST", body,
                {"X-Radar-Action": "verdict", "Content-Type": "application/json"})
            self.assertEqual(status, 200, response)
            body = json.dumps({"key": "github.com/e2e/synthetic-tool",
                               "state": "trying", "note": "e2e drill"}).encode()
            status, response = self.http(
                port, "/api/outcome", "POST", body,
                {"X-Radar-Action": "outcome", "Content-Type": "application/json"})
            self.assertEqual(status, 200, response)
            status, response = self.http(port, "/api/decisions")
            self.assertEqual(status, 200)
            doc = json.loads(response)
            self.assertTrue(doc["ok"])
            self.assertEqual(
                doc["verdicts_document"]["verdicts"][0]["key"],
                "github.com/e2e/synthetic-tool")
            self.assertEqual(
                doc["outcomes_document"]["outcomes"][0]["state"], "trying")
            first_revision = doc["revision"]
            status, response = self.http(port, "/api/decisions")
            self.assertEqual(json.loads(response)["revision"], first_revision,
                             "second tab sees the same durable state")
            status, _ = self.http(port, "/relevant-sheet.csv")
            self.assertEqual(status, 200, "safe sheet is served")
        finally:
            httpd.shutdown()
            httpd.server_close()

        # 6. RESTART durability: a fresh server instance sees the decisions.
        httpd, port = self.start_server()
        try:
            status, response = self.http(port, "/api/decisions")
            self.assertEqual(status, 200)
            doc = json.loads(response)
            self.assertEqual(len(doc["verdicts_document"]["verdicts"]), 1)
            self.assertEqual(len(doc["outcomes_document"]["outcomes"]), 1)
        finally:
            httpd.shutdown()
            httpd.server_close()

        # 7. REPLAY idempotence: same batch again, nothing duplicates.
        before = conn.execute("SELECT COUNT(*) FROM message_resources").fetchone()[0]
        gm.persist_fetch(conn, PROFILE, group, "100")
        after = conn.execute("SELECT COUNT(*) FROM message_resources").fetchone()[0]
        self.assertEqual(before, after, "replay must not duplicate occurrences")

        # 8. BACKUP + RESTORE into a fresh directory; state survives intact.
        result = radar_backup.create_backup(self.root, update_health=False)
        backup_dir = Path(result["path"])
        with tempfile.TemporaryDirectory() as fresh:
            restored = radar_backup.restore_backup(backup_dir, Path(fresh) / "restored")
            restored_root = Path(fresh) / "restored"
            restored_db = restored_root / "data" / "group-monitor" / "group-monitor.sqlite3"
            self.assertTrue(restored_db.is_file())
            check = sqlite3.connect(str(restored_db))
            try:
                self.assertEqual(
                    check.execute("SELECT COUNT(*) FROM message_resources").fetchone()[0],
                    after, "restored occurrence count matches")
                self.assertEqual(
                    check.execute(
                        "SELECT value FROM metadata WHERE key='fetch_cursor'"
                    ).fetchone()[0], "204", "restored cursor matches")
            finally:
                check.close()
            restored_verdicts = json.loads(
                (restored_root / "config" / "verdicts.json").read_text())
            self.assertEqual(restored_verdicts["verdicts"][0]["key"],
                             "github.com/e2e/synthetic-tool")
            self.assertNotIn("notifications", restored)


if __name__ == "__main__":
    unittest.main()
