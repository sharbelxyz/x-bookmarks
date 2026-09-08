"""Lane 03 acceptance against REAL integrated storage, not fixtures.

The backup/restore tool is exercised against stores written by the actual
provider packages published in this run:

* lane 01 `01-pkg-002` — group_monitor with the additive extraction columns
  (the ledger's real current schema and capture/provenance behavior);
* lane 02 `02-pkg-002` — decision_store + radar_server + telegram_decisions
  (durable authored decisions with revisions, bounded `_history/`, and the
  rotation-safe Telegram checkpoint/journals).

Provider code is loaded from the published packages into a THROWAWAY tree
under the lane's tmp; nothing here modifies another lane, the integration
tree, or the live runtime. If a package is missing the test skips loudly
rather than passing on a fixture.
"""

import importlib
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

LANE = Path(__file__).resolve().parent.parent
SCRIPTS = LANE / "scripts"


def _find_run_root(start):
    # Integration-tree repair by Chat 07 (2026-09-07): lanes sit at
    # <run>/workers/NN but integration at <run>/integration; walk up to the
    # run root instead of a fixed parents[] hop.
    for parent in [start] + list(start.parents):
        if (parent / "packages").is_dir() and (parent / "BASELINE.json").is_file():
            return parent
    return start.parents[1]


RUN = _find_run_root(LANE)
PACKAGES = RUN / "packages"
# When Chat 07 has integrated the providers, THIS tree's scripts are the real
# provider code (strictly fresher than the package after-files); the drill
# then runs against the merged integration state, which is what lane 03's
# open gate asked for.
PROVIDERS_INTEGRATED = (SCRIPTS / "decision_store.py").is_file()
sys.path.insert(0, str(SCRIPTS))

import radar_backup  # noqa: E402
import run_health  # noqa: E402

PROVIDER_FILES = [
    ("01/01-pkg-002/after/scripts/group_monitor.py", "group_monitor.py"),
    ("02/02-pkg-001/after/scripts/decision_store.py", "decision_store.py"),
    ("02/02-pkg-002/after/scripts/radar_server.py", "radar_server.py"),
    ("02/02-pkg-002/after/scripts/telegram_decisions.py", "telegram_decisions.py"),
]
MISSING = ([] if PROVIDERS_INTEGRATED else
           [rel for rel, _ in PROVIDER_FILES if not (PACKAGES / rel).is_file()])


def build_provider_tree(tmp):
    """A project root running the real 01/02 package code, isolated in tmp."""
    root = Path(tmp) / "integrated"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    (root / "config").mkdir()
    (root / "data" / "group-monitor").mkdir(parents=True)
    # Lane-03 baseline modules the providers import, then provider overrides.
    if PROVIDERS_INTEGRATED:
        for source in SCRIPTS.glob("*.py"):
            shutil.copyfile(source, scripts / source.name)
    else:
        for name in ("resource_typing.py", "json_filelock.py", "llm_provider.py",
                     "enrich_tools.py", "ingest_bookmarks.py", "group_monitor.py",
                     "radar_server.py", "telegram_decisions.py", "notify_buttons.py",
                     "dashboard_renderer.py", "service.py", "x_api_auth.py",
                     "learn_negatives.py", "scheduler.py"):
            source = SCRIPTS / name
            if source.is_file():
                shutil.copyfile(source, scripts / name)
        for rel, name in PROVIDER_FILES:
            shutil.copyfile(PACKAGES / rel, scripts / name)
    (root / "config" / "group-filter-profile.json").write_text(json.dumps({
        "conversation": {"capture_scope": "all_senders", "id": "g1"},
        "owners": [], "bootstrap": {"resume_after_message_id": 1},
        "selection": {"project_areas": {}}}))
    return root


def load_provider_modules(root):
    """Import the provider modules from the throwaway tree, first on sys.path."""
    scripts = str(root / "scripts")
    sys.path.insert(0, scripts)
    loaded = {}
    try:
        for name in ("group_monitor", "decision_store", "radar_server",
                     "telegram_decisions"):
            sys.modules.pop(name, None)
            loaded[name] = importlib.import_module(name)
            assert Path(loaded[name].__file__).parent == Path(scripts), (
                "loaded the wrong copy of " + name)
    finally:
        sys.path.remove(scripts)
    return loaded


def restore_lane_modules():
    """Put the lane's own modules back so later tests are unaffected."""
    for name in ("group_monitor", "decision_store", "radar_server",
                 "telegram_decisions"):
        sys.modules.pop(name, None)
    importlib.import_module("group_monitor")
    importlib.import_module("radar_server")


@unittest.skipIf(MISSING, "provider packages not published yet: {}".format(MISSING))
class RealIntegratedStorageBackupTests(unittest.TestCase):
    def setUp(self):
        # Keep big ledger copies inside the project (isolation intent),
        # but do not assume the scratch dir already exists: the lane
        # harness pre-creates it, a plain checkout does not.
        scratch = LANE / "tmp"
        scratch.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=str(scratch))
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(restore_lane_modules)
        self.root = build_provider_tree(self.tmp.name)
        self.modules = load_provider_modules(self.root)

    def _seed_real_stores(self):
        gm = self.modules["group_monitor"]
        server = self.modules["radar_server"]
        data = self.root / "data" / "group-monitor"
        config = self.root / "config"

        conn = gm.connect_db(data / "group-monitor.sqlite3")
        now = gm.utc_now()
        # Two occurrences of ONE logical resource: a group share and a
        # bookmark — the A01 provenance case lane 01 fixed.
        conn.execute(
            "INSERT INTO messages(message_id, conversation_id, sent_at_ms,"
            " sender_id, is_owner, text, urls_json, captured_at)"
            " VALUES('800','g1',1,'42',0,'shared','[]',?)", (now,))
        conn.execute(
            "INSERT INTO messages(message_id, conversation_id, sent_at_ms,"
            " sender_id, is_owner, text, urls_json, captured_at)"
            " VALUES('bookmark:800','g1',2,'bookmark',1,'','[]',?)", (now,))
        conn.execute(
            "INSERT INTO resources(resource_id, kind, canonical_url,"
            " first_message_id, last_message_id, sender_id, source_text, status,"
            " first_seen_at, updated_at) VALUES('tweet:800','tweet',"
            "'https://github.com/example/integrated','800','800','42','shared',"
            "'relevant',?,?)", (now, now))
        for message_id in ("800", "bookmark:800"):
            conn.execute(
                "INSERT INTO message_resources(message_id, resource_id)"
                " VALUES(?, 'tweet:800')", (message_id,))
        gm.set_meta(conn, "fetch_cursor", "800")
        gm.set_meta(conn, "capture_scope_version", gm.CAPTURE_SCOPE_VERSION)
        conn.commit()
        columns = {row[1] for row in conn.execute("PRAGMA table_info(resources)")}
        conn.close()

        verdicts_path = config / "verdicts.json"
        outcomes_path = config / "outcomes.json"
        with mock.patch.object(server, "VERDICTS_PATH", verdicts_path), \
                mock.patch.object(server, "OUTCOMES_PATH", outcomes_path):
            for rank in range(3):
                code, _ = server.record_verdict({
                    "key": "github.com/example/integrated{}".format(rank),
                    "verdict": "must_try", "why": "real provider write"})
                self.assertEqual(int(code), 200)
            code, _ = server.record_outcome({
                "key": "github.com/example/integrated0", "state": "trying",
                "note": "real outcome"})
            self.assertEqual(int(code), 200)
        (self.root / "data" / "group-monitor" / "telegram-offset.json").write_text(
            json.dumps({"version": 2, "offset": 17, "consumed_ids": ["e1", "e2"]}))
        (self.root / "data" / "group-monitor" / "telegram-received.jsonl").write_text(
            json.dumps({"event_id": "e1", "verdict": "must_try"}) + "\n")
        (self.root / "data" / "group-monitor" / "telegram-rejected.jsonl").write_text(
            json.dumps({"event_id": "e9", "reason": "unknown key"}) + "\n")
        return columns, verdicts_path, outcomes_path

    def test_backup_restore_round_trip_on_real_provider_stores(self):
        gm = self.modules["group_monitor"]
        server = self.modules["radar_server"]
        columns, verdicts_path, outcomes_path = self._seed_real_stores()
        self.assertTrue({"extraction_state", "extraction_detail",
                         "extraction_checked_at"} <= columns,
                        "expected lane 01's additive extraction columns")

        # Lane 02 writes bounded history; it must be part of the recovery set.
        history = self.root / "config" / "_history" / "verdicts"
        self.assertTrue(history.is_dir(), "lane 02 history dir expected")

        with mock.patch.object(server, "VERDICTS_PATH", verdicts_path), \
                mock.patch.object(server, "OUTCOMES_PATH", outcomes_path):
            _, before = server.read_decisions()

        result = radar_backup.create_backup(self.root)
        backup = Path(result["path"])
        manifest = json.loads((backup / "backup-manifest.json").read_text())

        captured = {entry["path"] for entry in manifest["files"] if entry.get("present")}
        self.assertIn("data/group-monitor/telegram-received.jsonl", captured)
        self.assertIn("data/group-monitor/telegram-rejected.jsonl", captured)
        self.assertTrue(any(path.startswith("config/_history/verdicts/")
                            for path in captured), "authored history must be captured")
        self.assertFalse(any(path.endswith((".lock", ".tmp")) for path in captured),
                         "lock/tmp sidecars must never enter a backup")
        self.assertEqual(manifest["document_revisions"]["config/verdicts.json"], 3,
                         "lane 02 monotonic revision must be recorded")

        self.assertTrue(radar_backup.verify_backup(backup)["pass"])
        target = Path(self.tmp.name) / "restored"
        restored = radar_backup.restore_backup(backup, target)
        self.assertTrue(restored["restored"])

        # Ledger: schema, provenance occurrences and cursor all survive.
        conn = sqlite3.connect(radar_backup._readonly_uri(
            target / "data/group-monitor/group-monitor.sqlite3"), uri=True)
        try:
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM message_resources"
                             " WHERE resource_id='tweet:800'").fetchone()[0], 2,
                "both occurrences of the logical resource must survive")
            self.assertEqual(
                conn.execute("SELECT value FROM metadata WHERE key='fetch_cursor'"
                             ).fetchone()[0], "800")
            restored_columns = {row[1] for row in
                                conn.execute("PRAGMA table_info(resources)")}
        finally:
            conn.close()
        self.assertEqual(restored_columns, columns,
                         "additive provider columns must survive VACUUM INTO")

        # Decisions/outcomes read back through the REAL provider API.
        with mock.patch.object(server, "VERDICTS_PATH", target / "config/verdicts.json"), \
                mock.patch.object(server, "OUTCOMES_PATH", target / "config/outcomes.json"):
            code, after = server.read_decisions()
        self.assertEqual(int(code), 200)
        self.assertTrue(after["ok"])
        self.assertEqual(after["verdicts_document"], before["verdicts_document"])
        self.assertEqual(after["outcomes_document"], before["outcomes_document"])
        self.assertEqual(
            gm.load_verdicts(target / "config/verdicts.json"),
            gm.load_verdicts(verdicts_path))
        self.assertEqual(
            gm.load_outcomes(target / "config/outcomes.json"),
            gm.load_outcomes(outcomes_path))

        # Decision-sync checkpoint and its journals survive together.
        offset = json.loads(
            (target / "data/group-monitor/telegram-offset.json").read_text())
        self.assertEqual(offset["consumed_ids"], ["e1", "e2"])
        self.assertIn("e9", (target / "data/group-monitor/telegram-rejected.jsonl"
                             ).read_text())

        # Restore stays inert: scanning disabled, no alerts, no schedule.
        self.assertIsNotNone(run_health.restore_block(target / "data/group-monitor"))
        self.assertFalse((target / "data/group-monitor/health-alerts.jsonl").exists())

    def test_restore_recovers_a_corrupt_authored_file_from_captured_history(self):
        """02's documented recovery path works from a backup, end to end."""
        server = self.modules["radar_server"]
        _, verdicts_path, _ = self._seed_real_stores()
        backup = Path(radar_backup.create_backup(self.root)["path"])
        target = Path(self.tmp.name) / "restored-history"
        radar_backup.restore_backup(backup, target)

        restored_verdicts = target / "config" / "verdicts.json"
        good = json.loads(restored_verdicts.read_text())
        restored_verdicts.write_text("{ this is not json")

        with mock.patch.object(server, "VERDICTS_PATH", restored_verdicts):
            code, payload = server.record_verdict({
                "key": "github.com/example/new", "verdict": "must_try"})
        self.assertGreaterEqual(int(code), 400, payload)
        self.assertEqual(restored_verdicts.read_text(), "{ this is not json",
                         "a corrupt file must never be silently overwritten")

        versions = sorted((target / "config" / "_history" / "verdicts").glob("*.json"))
        self.assertTrue(versions, "history must have been restored with the backup")
        recovered = json.loads(versions[-1].read_text())
        # 02 archives the version BEFORE each replace, so the newest history
        # file is the previous revision: recovering from it is lossless except
        # for the final write. Assert exactly that, rather than equality.
        self.assertEqual(recovered.get("revision", 0), good.get("revision", 0) - 1)
        recovered_keys = [entry["key"] for entry in recovered["verdicts"]]
        good_keys = [entry["key"] for entry in good["verdicts"]]
        self.assertEqual(recovered_keys, good_keys[:len(recovered_keys)])
        self.assertTrue(recovered_keys, "recovered document must be usable")
        # And it is a valid document the real loader accepts.
        import group_monitor as provider_gm
        self.assertTrue(provider_gm.load_verdicts(versions[-1]))

    def test_concurrent_real_decision_writes_do_not_tear_the_backup(self):
        server = self.modules["radar_server"]
        _, verdicts_path, outcomes_path = self._seed_real_stores()
        import threading

        stop = threading.Event()
        errors = []

        def writer():
            index = 0
            with mock.patch.object(server, "VERDICTS_PATH", verdicts_path), \
                    mock.patch.object(server, "OUTCOMES_PATH", outcomes_path):
                while not stop.is_set():
                    index += 1
                    try:
                        server.record_verdict({
                            "key": "github.com/example/live{}".format(index),
                            "verdict": "must_try"})
                    except Exception as exc:  # noqa: BLE001
                        errors.append(exc)
                        return

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            result = radar_backup.create_backup(self.root)
        finally:
            stop.set()
            thread.join(timeout=10)
        self.assertEqual(errors, [])
        backup = Path(result["path"])
        report = radar_backup.verify_backup(backup)
        self.assertTrue(report["pass"], report["problems"])
        document = json.loads((backup / "config/verdicts.json").read_text())
        self.assertIsInstance(document.get("verdicts"), list)
        self.assertTrue(document["verdicts"])
        # The captured document is a real committed revision of the provider.
        self.assertEqual(
            json.loads((backup / "backup-manifest.json").read_text()
                       )["document_revisions"]["config/verdicts.json"],
            document.get("revision", 0))


class ProviderAvailabilityTests(unittest.TestCase):
    def test_report_provider_availability_explicitly(self):
        """Never let a skipped real-provider check look like a pass."""
        if PROVIDERS_INTEGRATED:
            for _, name in PROVIDER_FILES:
                self.assertTrue((SCRIPTS / name).is_file(),
                                "integrated provider missing: " + name)
            return
        if MISSING:
            self.skipTest("providers missing: {}".format(MISSING))
        for rel, _ in PROVIDER_FILES:
            self.assertTrue((PACKAGES / rel).is_file(), rel)


if __name__ == "__main__":
    unittest.main()
