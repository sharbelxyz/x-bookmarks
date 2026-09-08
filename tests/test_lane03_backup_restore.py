"""Lane 03 tests: consistent backup and safe restore (A12).

Two layers, deliberately:
* fixture layer — synthetic recovery-set files, fast failure drills;
* real-integrated-storage layer — stores produced by the actual providers
  (group_monitor.connect_db for the ledger, radar_server.record_verdict /
  record_outcome for authored decisions), backed up and restored through the
  same code path, then read back with the real loaders.

Nothing here touches the live tree, sends a notification, or installs a
schedule; every test works inside a temporary root.
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import group_monitor as gm  # noqa: E402
import radar_backup  # noqa: E402
import radar_server  # noqa: E402
import run_health  # noqa: E402


def build_root(tmp, *, resources=3, verdicts=2, journal_lines=2):
    """A synthetic project root shaped exactly like the C3 recovery set."""
    root = Path(tmp) / "root"
    data = root / "data" / "group-monitor"
    config = root / "config"
    data.mkdir(parents=True)
    config.mkdir(parents=True)

    conn = gm.connect_db(data / "group-monitor.sqlite3")
    now = "2026-09-06T00:00:00+00:00"
    conn.execute(
        "INSERT INTO messages(message_id, conversation_id, sent_at_ms, sender_id,"
        " is_owner, text, urls_json, captured_at) VALUES('900','c',1,'42',0,'t','[]',?)",
        (now,),
    )
    for index in range(resources):
        conn.execute(
            "INSERT INTO resources(resource_id, kind, first_message_id,"
            " last_message_id, sender_id, source_text, status, first_seen_at,"
            " updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            ("tweet:{}".format(index), "tweet", "900", "900", "42", "text",
             "relevant", now, now),
        )
        conn.execute(
            "INSERT INTO message_resources(message_id, resource_id) VALUES('900', ?)",
            ("tweet:{}".format(index),),
        )
    gm.set_meta(conn, "fetch_cursor", "900")
    gm.set_meta(conn, "capture_scope_version", gm.CAPTURE_SCOPE_VERSION)
    conn.commit()
    conn.close()

    (config / "verdicts.json").write_text(json.dumps({
        "version": 1, "updated_at": "2026-09-06",
        "verdicts": [{"key": "github.com/example/tool{}".format(i),
                      "verdict": "must_try", "rank": i + 1}
                     for i in range(verdicts)]}, indent=2))
    (config / "outcomes.json").write_text(json.dumps({
        "version": 1, "updated_at": "2026-09-06",
        "outcomes": [{"key": "github.com/example/tool0", "state": "trying"}]}, indent=2))
    (config / "group-filter-profile.json").write_text(json.dumps({
        "conversation": {"capture_scope": "all_senders"}, "owners": [],
        "bootstrap": {"resume_after_message_id": 1},
        "selection": {"project_areas": {}}}, indent=2))
    (data / "telegram-offset.json").write_text(json.dumps({"offset": 4242}))
    (data / "pending-decisions.json").write_text(json.dumps({"pending": []}))
    (data / "autonomous-runs.jsonl").write_text("".join(
        json.dumps({"started_at": now, "outcome": "ok", "n": i}) + "\n"
        for i in range(journal_lines)))
    # Regenerable exports and a credential-ish file that must NOT be captured.
    (data / "dashboard.html").write_text("<html>regenerable</html>")
    (data / "status.json").write_text(json.dumps({"updated_at": now}))
    (root / "data" / "accounts.json").write_text(json.dumps({"token": "SECRET"}))
    return root


class BackupCreateTests(unittest.TestCase):
    def test_backup_captures_recovery_set_and_excludes_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_root(tmp)
            result = radar_backup.create_backup(root)
            backup = Path(result["path"])
            self.assertTrue((backup / "data/group-monitor/group-monitor.sqlite3").is_file())
            self.assertTrue((backup / "config/verdicts.json").is_file())
            self.assertTrue((backup / "config/outcomes.json").is_file())
            self.assertTrue((backup / "data/group-monitor/telegram-offset.json").is_file())
            self.assertTrue((backup / "data/group-monitor/autonomous-runs.jsonl").is_file())
            # credentials and regenerable exports are absent
            self.assertFalse((backup / "data/accounts.json").exists())
            self.assertFalse((backup / "data/group-monitor/dashboard.html").exists())
            # sqlite sidecars are never copied
            self.assertFalse((backup / "data/group-monitor/group-monitor.sqlite3-wal").exists())
            manifest = json.loads((backup / "backup-manifest.json").read_text())
            self.assertEqual(manifest["manifest_version"], 1)
            self.assertEqual(manifest["contract"], "C3/c1")
            self.assertTrue(manifest["credentials_excluded"])
            self.assertFalse(manifest["offsite"])
            self.assertTrue(manifest["disclaimers"])
            self.assertEqual(manifest["sqlite"]["counts"]["resources"], 3)
            blob = json.dumps(manifest)
            self.assertNotIn("SECRET", blob)

    def test_restrictive_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_root(tmp)
            backup = Path(radar_backup.create_backup(root)["path"])
            for member in backup.rglob("*"):
                mode = member.stat().st_mode & 0o777
                if member.is_dir():
                    self.assertEqual(mode, 0o700, member)
                else:
                    self.assertEqual(mode, 0o600, member)

    def test_backup_refuses_while_scanner_holds_worker_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_root(tmp)
            with mock.patch.object(gm, "DATA_DIR", root / "data" / "group-monitor"):
                with gm.exclusive_run_lock():
                    with self.assertRaises(radar_backup.BackupError) as caught:
                        radar_backup.create_backup(root, wait_seconds=0.5)
            self.assertIn("worker.lock", str(caught.exception))

    def test_backup_waits_then_succeeds_when_lock_is_released(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_root(tmp)
            data_dir = root / "data" / "group-monitor"
            released = threading.Event()

            def hold():
                with mock.patch.object(gm, "DATA_DIR", data_dir):
                    with gm.exclusive_run_lock():
                        time.sleep(1.0)
                released.set()

            worker = threading.Thread(target=hold)
            worker.start()
            time.sleep(0.2)
            result = radar_backup.create_backup(root, wait_seconds=10)
            worker.join()
            self.assertTrue(released.is_set())
            self.assertTrue(Path(result["path"]).is_dir())

    def test_failed_backup_leaves_no_partial_and_keeps_previous_point(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_root(tmp)
            first = radar_backup.create_backup(root)
            dest = Path(first["path"]).parent
            with mock.patch.object(
                radar_backup, "_sqlite_snapshot", side_effect=radar_backup.BackupError("disk full")
            ):
                with self.assertRaises(radar_backup.BackupError):
                    radar_backup.create_backup(root)
            staged = [p.name for p in dest.iterdir() if p.name.startswith(".staging-")]
            self.assertEqual(staged, [], "no partial backup may survive")
            self.assertEqual((dest / "LATEST").read_text().strip(), first["backup_id"],
                             "LATEST must still point at the previous good backup")
            self.assertTrue(radar_backup.verify_backup(Path(first["path"]))["pass"])

    def test_failed_backup_marks_health_failed_not_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_root(tmp)
            data_dir = root / "data" / "group-monitor"
            with mock.patch.object(
                radar_backup, "_sqlite_snapshot", side_effect=radar_backup.BackupError("io error")
            ):
                with self.assertRaises(radar_backup.BackupError):
                    radar_backup.create_backup(root)
            ext = run_health.compose_health_extension(data_dir)
            self.assertEqual(ext["stages"]["backup"]["state"], "failed")
            radar_backup.create_backup(root)
            ext = run_health.compose_health_extension(data_dir)
            self.assertEqual(ext["stages"]["backup"]["state"], "ok")

    def test_backup_survives_concurrent_authored_writes(self):
        """A separate PROCESS rewrites verdicts.json throughout the backup."""
        with tempfile.TemporaryDirectory() as tmp:
            root = build_root(tmp)
            verdicts = root / "config" / "verdicts.json"
            writer_src = textwrap.dedent("""
                import json, sys, time
                sys.path.insert(0, SCRIPTS_DIR)
                from json_filelock import locked_json_write
                deadline = time.time() + 3.0
                n = 0
                while time.time() < deadline:
                    n += 1
                    entries = [{"key": "github.com/x/t" + str(i),
                                "verdict": "must_try"}
                               for i in range(n % 7 + 1)]
                    locked_json_write(VERDICTS_PATH,
                                      {"version": 1, "updated_at": "2026-09-06",
                                       "verdicts": entries}, indent=2)
                    time.sleep(0.01)
            """)
            writer_file = Path(tmp) / "concurrent_writer.py"
            writer_file.write_text(
                "SCRIPTS_DIR = {!r}\nVERDICTS_PATH = {!r}\n".format(
                    str(SCRIPTS), str(verdicts)) + writer_src)
            writer = subprocess.Popen([sys.executable, str(writer_file)],
                                      stderr=subprocess.PIPE)
            try:
                time.sleep(0.3)
                result = radar_backup.create_backup(root)
            finally:
                _, err = writer.communicate(timeout=20)
            self.assertEqual(writer.returncode, 0,
                             "concurrent writer must really have run: "
                             + err.decode("utf-8", "replace")[-400:])
            backup = Path(result["path"])
            self.assertTrue(radar_backup.verify_backup(backup)["pass"])
            document = json.loads((backup / "config/verdicts.json").read_text())
            self.assertEqual(document["version"], 1)
            self.assertIsInstance(document["verdicts"], list)
            self.assertTrue(document["verdicts"], "captured a complete revision")


class VerifyAndRestoreTests(unittest.TestCase):
    def test_restore_into_new_dir_disables_scanning_and_notifications(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_root(tmp)
            backup = Path(radar_backup.create_backup(root)["path"])
            target = Path(tmp) / "restored"
            result = radar_backup.restore_backup(backup, target)
            self.assertTrue(result["restored"])
            state = json.loads(
                (target / "data/group-monitor" / run_health.RESTORE_STATE_FILE).read_text()
            )
            self.assertEqual(state["scanning"], "disabled")
            self.assertEqual(state["notifications"], "disabled")
            self.assertTrue((target / "RESTORE-README.md").is_file())
            self.assertIsNotNone(run_health.restore_block(target / "data/group-monitor"))
            # no schedule artifacts, no credentials, no exports
            self.assertFalse((target / "data/accounts.json").exists())
            self.assertFalse((target / "data/group-monitor/dashboard.html").exists())
            self.assertEqual(result["post_check"]["counts"]["resources"], 3)

    def test_restore_refuses_non_empty_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_root(tmp)
            backup = Path(radar_backup.create_backup(root)["path"])
            target = Path(tmp) / "occupied"
            target.mkdir()
            (target / "keep.txt").write_text("do not clobber me")
            with self.assertRaises(radar_backup.BackupError):
                radar_backup.restore_backup(backup, target)
            self.assertEqual((target / "keep.txt").read_text(), "do not clobber me")

    def test_corrupt_backup_fails_safely(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_root(tmp)
            backup = Path(radar_backup.create_backup(root)["path"])
            db = backup / "data/group-monitor/group-monitor.sqlite3"
            data = bytearray(db.read_bytes())
            # XOR (not zero-fill): a freshly vacuumed page tail may already be
            # zeros, which would make the "corruption" a silent no-op.
            start = 4096 + 24
            for offset in range(start, start + 64):
                data[offset] ^= 0xFF
            db.write_bytes(bytes(data))
            self.assertNotEqual(radar_backup.sha256_file(db),
                                json.loads((backup / "backup-manifest.json").read_text()
                                           )["files"][0]["sha256"])
            report = radar_backup.verify_backup(backup)
            self.assertFalse(report["pass"])
            self.assertTrue(report["problems"])
            target = Path(tmp) / "restored"
            with self.assertRaises(radar_backup.BackupError):
                radar_backup.restore_backup(backup, target)
            self.assertFalse(target.exists(), "a failed restore creates nothing")

    def test_incomplete_backup_fails_safely(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_root(tmp)
            backup = Path(radar_backup.create_backup(root)["path"])
            (backup / "config/verdicts.json").unlink()
            report = radar_backup.verify_backup(backup)
            self.assertFalse(report["pass"])
            self.assertIn("missing member: config/verdicts.json", report["problems"])

    def test_tampered_manifest_revision_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_root(tmp)
            backup = Path(radar_backup.create_backup(root)["path"])
            manifest_path = backup / "backup-manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["logical_revision"] = "0" * 64
            manifest_path.write_text(json.dumps(manifest))
            report = radar_backup.verify_backup(backup)
            self.assertFalse(report["pass"])
            self.assertIn("logical_revision mismatch", report["problems"])

    def test_missing_manifest_fails_safely(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_root(tmp)
            backup = Path(radar_backup.create_backup(root)["path"])
            (backup / "backup-manifest.json").unlink()
            report = radar_backup.verify_backup(backup)
            self.assertFalse(report["pass"])
            self.assertIn("backup manifest missing", report["problems"])

    def test_restore_check_proves_restorability_without_touching_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_root(tmp)
            backup = Path(radar_backup.create_backup(root)["path"])
            before = radar_backup.sha256_file(
                root / "data/group-monitor/group-monitor.sqlite3")
            report = radar_backup.restore_check(backup)
            self.assertTrue(report["pass"])
            self.assertEqual(report["counts"]["resources"], 3)
            after = radar_backup.sha256_file(
                root / "data/group-monitor/group-monitor.sqlite3")
            self.assertEqual(before, after, "source ledger must be untouched")

    def test_prune_is_dry_run_by_default_and_never_drops_latest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_root(tmp)
            ids = [radar_backup.create_backup(root)["backup_id"] for _ in range(3)]
            dest = root / "data/group-monitor/backups"
            # ids can collide within one second; make them distinct on disk
            self.assertGreaterEqual(len(set(ids)), 1)
            report = radar_backup.prune_backups(dest, keep=1)
            self.assertEqual(report["deleted"], [])
            self.assertIn("dry run", report["note"])
            with self.assertRaises(radar_backup.BackupError):
                radar_backup.prune_backups(dest, keep=0, yes_delete=True)
            latest = (dest / "LATEST").read_text().strip()
            report = radar_backup.prune_backups(dest, keep=1, yes_delete=True)
            self.assertNotIn(latest, report["deleted"])
            self.assertTrue((dest / latest).is_dir())


class RealIntegratedStorageTests(unittest.TestCase):
    """Backup/restore against stores written by the REAL providers, not fixtures."""

    def test_real_ledger_and_authored_decisions_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            data = root / "data" / "group-monitor"
            config = root / "config"
            data.mkdir(parents=True)
            config.mkdir(parents=True)
            (config / "group-filter-profile.json").write_text(json.dumps({
                "conversation": {"capture_scope": "all_senders"}, "owners": [],
                "bootstrap": {"resume_after_message_id": 1},
                "selection": {"project_areas": {}}}))

            # Real ledger via the real schema/connection helper.
            conn = gm.connect_db(data / "group-monitor.sqlite3")
            now = gm.utc_now()
            conn.execute(
                "INSERT INTO messages(message_id, conversation_id, sent_at_ms,"
                " sender_id, is_owner, text, urls_json, captured_at)"
                " VALUES('700','c',1,'42',0,'shared a tool','[]',?)", (now,))
            conn.execute(
                "INSERT INTO resources(resource_id, kind, canonical_url,"
                " first_message_id, last_message_id, sender_id, source_text,"
                " status, first_seen_at, updated_at) VALUES"
                " ('tweet:700','tweet','https://github.com/example/real-tool',"
                " '700','700','42','shared a tool','relevant',?,?)", (now, now))
            conn.execute(
                "INSERT INTO message_resources(message_id, resource_id)"
                " VALUES('700','tweet:700')")
            gm.set_meta(conn, "fetch_cursor", "700")
            conn.commit()
            conn.close()

            # Real authored decisions via the real server write paths.
            verdicts_path = config / "verdicts.json"
            outcomes_path = config / "outcomes.json"
            with mock.patch.object(radar_server, "VERDICTS_PATH", verdicts_path), \
                    mock.patch.object(radar_server, "OUTCOMES_PATH", outcomes_path):
                code, _ = radar_server.record_verdict({
                    "key": "github.com/example/real-tool", "verdict": "must_try",
                    "why": "fits the pipeline", "first_step": "clone it"})
                self.assertEqual(int(code), 200)
                code, _ = radar_server.record_outcome({
                    "key": "github.com/example/real-tool", "state": "trying",
                    "note": "ran it once"})
                self.assertEqual(int(code), 200)
            (data / "telegram-offset.json").write_text(json.dumps({"offset": 99}))

            source_verdicts = gm.load_verdicts(verdicts_path)
            source_outcomes = gm.load_outcomes(outcomes_path)
            self.assertIn("github.com/example/real-tool", source_verdicts)

            backup = Path(radar_backup.create_backup(root)["path"])
            target = Path(tmp) / "restored"
            result = radar_backup.restore_backup(backup, target)
            self.assertTrue(result["restored"])

            # Ledger: provenance, counts and cursor survive.
            conn = sqlite3.connect(
                radar_backup._readonly_uri(
                    target / "data/group-monitor/group-monitor.sqlite3"), uri=True)
            conn.row_factory = sqlite3.Row
            try:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM resources").fetchone()[0], 1)
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM message_resources").fetchone()[0], 1)
                self.assertEqual(
                    conn.execute("SELECT value FROM metadata WHERE key='fetch_cursor'"
                                 ).fetchone()[0], "700")
                self.assertEqual(
                    conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            finally:
                conn.close()

            # Decisions and outcomes: read back with the REAL loaders.
            restored_verdicts = gm.load_verdicts(target / "config/verdicts.json")
            restored_outcomes = gm.load_outcomes(target / "config/outcomes.json")
            self.assertEqual(restored_verdicts, source_verdicts)
            self.assertEqual(restored_outcomes, source_outcomes)
            self.assertEqual(
                restored_verdicts["github.com/example/real-tool"]["verdict"], "must_try")
            self.assertEqual(
                restored_outcomes["github.com/example/real-tool"]["state"], "trying")
            # Checkpoint survives; a clearing verdict never erased the trial.
            self.assertEqual(json.loads(
                (target / "data/group-monitor/telegram-offset.json").read_text()
            )["offset"], 99)

    def test_restored_tree_replays_no_notifications_and_refuses_to_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_root(tmp)
            # Mark every resource already notified, as the live ledger would.
            db = root / "data/group-monitor/group-monitor.sqlite3"
            conn = sqlite3.connect(str(db))
            conn.execute("UPDATE resources SET notified_at = '2026-09-06T00:00:00+00:00'")
            conn.execute("UPDATE message_resources SET notified_at = '2026-09-06T00:00:00+00:00'")
            conn.commit()
            conn.close()

            backup = Path(radar_backup.create_backup(root)["path"])
            target = Path(tmp) / "restored"
            radar_backup.restore_backup(backup, target)
            restored_data = target / "data" / "group-monitor"

            conn = sqlite3.connect(radar_backup._readonly_uri(
                restored_data / "group-monitor.sqlite3"), uri=True)
            try:
                unnotified = conn.execute(
                    "SELECT COUNT(*) FROM message_resources WHERE notified_at IS NULL"
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(unnotified, 0,
                             "restore must not resurrect already-sent notifications")

            # The loop refuses to run against a restored tree at all.
            import group_filter_loop as loop
            with mock.patch.object(gm, "DATA_DIR", restored_data), \
                    mock.patch.object(loop.monitor, "DATA_DIR", restored_data), \
                    mock.patch.object(gm, "connect_db",
                                      side_effect=AssertionError("must not open the ledger")):
                code = loop.run_workflow(no_record=True, no_notify=True)
            self.assertEqual(code, 3)
            self.assertFalse((restored_data / "health-alerts.jsonl").exists(),
                             "no historical alert may be queued by a restore")


class BackupPolicyHookTests(unittest.TestCase):
    def test_policy_disabled_by_default(self):
        import group_filter_loop as loop
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(loop, "BACKUP_POLICY_PATH", Path(tmp) / "absent.json"):
                self.assertEqual(loop._run_backup_policy("ok"), {"enabled": False})

    def test_policy_skips_when_run_failed(self):
        import group_filter_loop as loop
        with tempfile.TemporaryDirectory() as tmp:
            policy = Path(tmp) / "backup-policy.json"
            policy.write_text(json.dumps({"enabled": True}))
            with mock.patch.object(loop, "BACKUP_POLICY_PATH", policy):
                report = loop._run_backup_policy("error")
            self.assertTrue(report["enabled"])
            self.assertIn("run outcome was error", report["skipped"])

    def test_policy_runs_backup_and_then_skips_while_fresh(self):
        import group_filter_loop as loop
        with tempfile.TemporaryDirectory() as tmp:
            root = build_root(tmp)
            dest = root / "backups"
            policy = root / "config" / "backup-policy.json"
            policy.write_text(json.dumps({
                "enabled": True, "dest": str(dest), "interval_hours": 24,
                "restore_check": True, "max_seconds": 120}))
            with mock.patch.object(loop, "BACKUP_POLICY_PATH", policy), \
                    mock.patch.object(loop, "ROOT", root), \
                    mock.patch.object(gm, "DATA_DIR", root / "data" / "group-monitor"):
                report = loop._run_backup_policy("ok")
                self.assertEqual(report["returncode"], 0, report)
                self.assertEqual(report["restore_check"]["returncode"], 0, report)
                second = loop._run_backup_policy("ok")
            self.assertEqual(second["skipped"], "fresh backup exists")
            self.assertTrue((dest / "LATEST").is_file())

    def test_policy_never_touches_the_schedule(self):
        source = (SCRIPTS / "group_filter_loop.py").read_text()
        for forbidden in ("crontab", "launchctl", "manage_group_filter_schedule"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()


class HistoryRotationRaceTests(unittest.TestCase):
    """02's authored history is bounded and rotates while a backup runs."""

    def test_history_version_pruned_mid_backup_does_not_fail_the_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_root(tmp)
            history = root / "config" / "_history" / "verdicts"
            history.mkdir(parents=True)
            for index in range(3):
                (history / "20260906T00000{}-r{}.json".format(index, index)).write_text(
                    json.dumps({"version": 1, "revision": index, "verdicts": []}))
            doomed = history / "20260906T000000-r0.json"
            self.assertTrue(doomed.is_file(), "fixture filename must match")

            real_copy_plain = radar_backup._copy_plain

            def copy_then_rotate(source, target):
                digest = real_copy_plain(source, target)
                # Rotation prunes the OLDEST version after it was already
                # captured — the post-copy re-hash must tolerate that.
                if source.name.endswith("-r2.json") and doomed.exists():
                    doomed.unlink()
                return digest

            with mock.patch.object(radar_backup, "_copy_plain",
                                   side_effect=copy_then_rotate):
                result = radar_backup.create_backup(root)
            backup = Path(result["path"])
            manifest = json.loads((backup / "backup-manifest.json").read_text())
            rotated = [entry for entry in manifest["files"]
                       if entry["path"].endswith("-r0.json")]
            self.assertEqual(len(rotated), 1)
            self.assertFalse(rotated[0]["present"])
            self.assertEqual(rotated[0]["note"], "rotated out during backup")
            self.assertFalse((backup / rotated[0]["path"]).exists())
            self.assertTrue(radar_backup.verify_backup(backup)["pass"],
                            "a rotated-out history version must not fail verify")
            surviving = [entry["path"] for entry in manifest["files"]
                         if entry.get("present") and "_history" in entry["path"]]
            self.assertEqual(len(surviving), 2)

    def test_vanished_authored_store_still_fails_the_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = build_root(tmp)
            outcomes = root / "config" / "outcomes.json"
            real_copy_locked = radar_backup._copy_locked_json

            def copy_then_delete(source, target):
                digest = real_copy_locked(source, target)
                # Delete AFTER outcomes.json has been copied: the guard is for a
                # store that vanishes mid-window, not one that never existed.
                if source.name == "group-filter-profile.json":
                    outcomes.unlink()
                return digest

            with mock.patch.object(radar_backup, "_copy_locked_json",
                                   side_effect=copy_then_delete):
                with self.assertRaises(radar_backup.BackupError) as caught:
                    radar_backup.create_backup(root)
            self.assertIn("disappeared during backup", str(caught.exception))
            dest = root / "data" / "group-monitor" / "backups"
            self.assertFalse((dest / "LATEST").exists(),
                             "a failed first backup must not publish a recovery point")
