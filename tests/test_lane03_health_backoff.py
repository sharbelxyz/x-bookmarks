"""Lane 03 tests: truthful stage health, bounded persisted backoff (A03).

Covers the C4 provider surface (compose_health_extension + build_health
integration) and the group_filter_loop failure drills: injected model 401,
rate limit, DNS failure, subprocess timeout, circuit-open skip, restart
survival, cleanup-concealment, and a recovery drill draining a fixture
backlog exactly once.
"""

import contextlib
import datetime as dt
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
def _find_run_root(start):
    # Integration-tree repair by Chat 07 (2026-09-07): lanes sit at
    # <run>/workers/NN but integration at <run>/integration, so a fixed
    # parents[] hop cannot serve both. Walk up to the contracts dir instead
    # (same pattern as test_lane04_provider_contract).
    for parent in [start] + list(start.parents):
        if (parent / "contracts" / "fixtures").is_dir():
            return parent
    return start.parents[3]

RUN_DIR = _find_run_root(Path(__file__).resolve().parent)
FIXTURES = RUN_DIR / "contracts" / "fixtures"
sys.path.insert(0, str(SCRIPTS))

import group_filter_loop as loop  # noqa: E402
import group_monitor as gm  # noqa: E402
import radar_server  # noqa: E402
import run_health  # noqa: E402


def iso(delta_seconds=0):
    moment = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=delta_seconds)
    return moment.isoformat(timespec="seconds")


class ScrubAndClassifyTests(unittest.TestCase):
    def test_scrub_detail_redacts_secrets_and_caps_length(self):
        raw = (
            "call failed: Bearer abc.def.ghi token=sk-live-1234567890abcdef "
            "cookie: session=" + "a" * 60 + " plus " + "b" * 500
        )
        cleaned = run_health.scrub_detail(raw)
        self.assertNotIn("sk-live", cleaned)
        self.assertNotIn("abc.def.ghi", cleaned)
        self.assertNotIn("a" * 40, cleaned)
        self.assertIn("[redacted]", cleaned)
        self.assertLessEqual(len(cleaned), run_health.MAX_DETAIL_CHARS)

    def test_classify_failure_covers_observed_classes(self):
        self.assertEqual(run_health.classify_failure("HTTP 401 Unauthorized"), "auth")
        self.assertEqual(run_health.classify_failure("Please sign in to continue"), "auth")
        self.assertEqual(run_health.classify_failure("429 Too Many Requests"), "rate_limit")
        self.assertEqual(
            run_health.classify_failure("nodename nor servname provided"), "dns"
        )
        self.assertEqual(run_health.classify_failure("read timed out"), "timeout")
        self.assertEqual(
            run_health.classify_failure("Connection reset by peer"), "network"
        )
        self.assertEqual(run_health.classify_failure("something odd"), "transient")


class StageStoreTests(unittest.TestCase):
    def test_old_pass_cannot_overwrite_newer_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = run_health.StageHealthStore(Path(tmp))
            store.update_stage("capture", "failed", at=iso(0), detail="broke")
            store.update_stage("capture", "ok", at=iso(-3600))
            record = store.read()["stages"]["capture"]
            self.assertEqual(record["state"], "failed")
            # and a genuinely newer observation does win
            store.update_stage("capture", "ok", at=iso(60))
            self.assertEqual(store.read()["stages"]["capture"]["state"], "ok")

    def test_run_result_is_monotonic_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = run_health.StageHealthStore(Path(tmp))
            store.record_run_result("error", iso(-60), iso(0))
            store.record_run_result("ok", iso(-7200), iso(-3600))
            self.assertEqual(store.read()["last_run_outcome"], "error")


class BackoffTests(unittest.TestCase):
    def test_opens_after_threshold_and_survives_new_instance(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = run_health.BackoffStore(Path(tmp))
            store.record_failure("capture", "dns", "getaddrinfo failed")
            store.record_failure("capture", "dns", "getaddrinfo failed")
            self.assertTrue(store.check("capture")["allowed"])  # below threshold
            store.record_failure("capture", "dns", "getaddrinfo failed")
            # restart: a brand-new instance must still see the open circuit
            fresh = run_health.BackoffStore(Path(tmp))
            gate = fresh.check("capture")
            self.assertFalse(gate["allowed"])
            self.assertEqual(gate["failure_class"], "dns")

    def test_non_auth_circuit_half_opens_after_until_then_recovers(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = run_health.BackoffStore(Path(tmp))
            now = dt.datetime.now(dt.timezone.utc)
            for _ in range(3):
                store.record_failure("capture", "dns", "getaddrinfo", now=now)
            self.assertFalse(store.check("capture", now=now)["allowed"])
            later = now + dt.timedelta(seconds=run_health.BACKOFF_CAP_SECONDS["dns"] + 1)
            gate = store.check("capture", now=later)
            self.assertTrue(gate["allowed"])
            self.assertTrue(gate["probe"])
            outcome = store.record_success("capture", now=later)
            self.assertTrue(outcome["recovered"])
            self.assertTrue(store.check("capture")["allowed"])

    def test_auth_circuit_needs_operator_probe_or_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = run_health.BackoffStore(Path(tmp))
            now = dt.datetime.now(dt.timezone.utc)
            store.record_failure("semantic_review", "auth", "HTTP 401", now=now)
            store.record_failure("semantic_review", "auth", "HTTP 401", now=now)
            far_future = now + dt.timedelta(days=30)
            gate = store.check("semantic_review", now=far_future)
            self.assertFalse(gate["allowed"], "auth must never self-probe")
            # operator arms exactly one probe
            self.assertTrue(store.arm_probe("semantic_review"))
            gate = store.check("semantic_review", now=far_future)
            self.assertTrue(gate["allowed"])
            self.assertTrue(gate["probe"])
            # a failed probe re-opens; reset clears unconditionally
            store.record_failure("semantic_review", "auth", "HTTP 401 again")
            self.assertFalse(store.check("semantic_review", now=far_future)["allowed"])
            store.reset("semantic_review", reason="reauthenticated")
            self.assertTrue(store.check("semantic_review")["allowed"])

    def test_escalation_is_capped(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = run_health.BackoffStore(Path(tmp))
            now = dt.datetime.now(dt.timezone.utc)
            for _ in range(12):
                entry = store.record_failure("capture", "dns", "x", now=now)
            until = run_health.parse_iso(entry["until"])
            delay = (until - now).total_seconds()
            self.assertLessEqual(delay, run_health.BACKOFF_CAP_SECONDS["dns"] + 1)


class AlertTests(unittest.TestCase):
    def test_alerts_are_deduplicated_rate_limited_and_dry_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            self.assertTrue(run_health.prepare_alert(data_dir, "run_error", "auth", "boom"))
            self.assertFalse(
                run_health.prepare_alert(data_dir, "run_error", "auth", "boom again"),
                "same (kind, domain) within the interval must be suppressed",
            )
            self.assertTrue(run_health.prepare_alert(data_dir, "run_error", "dns", "other"))
            lines = (data_dir / run_health.ALERTS_FILE).read_text().splitlines()
            self.assertEqual(len(lines), 2)
            for line in lines:
                self.assertEqual(json.loads(line)["delivery"], "dry_run")


class ComposeTests(unittest.TestCase):
    def test_matches_c4_extended_fixture_shape(self):
        fixture = json.loads((FIXTURES / "c4-health-extended.json").read_text())
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            stages = run_health.StageHealthStore(data_dir)
            stages.update_stage("capture", "ok")
            stages.update_stage("semantic_review", "auth_required", detail="model HTTP 401")
            stages.record_run_result(
                "error", iso(-120), iso(-60),
                semantic_success_at=iso(-86400),
                backlog_oldest_pending_at=iso(-66000),
            )
            backoff = run_health.BackoffStore(data_dir)
            backoff.record_failure("semantic_review", "auth", "HTTP 401")
            backoff.record_failure("semantic_review", "auth", "HTTP 401")
            ext = run_health.compose_health_extension(data_dir)
        for field in fixture["sample_additive_block"]:
            self.assertIn(field, ext, field)
        for name, record in ext["stages"].items():
            self.assertIn(name, fixture["stage_names"])
            self.assertIn(record["state"], fixture["stages_states"])
        self.assertTrue(ext["auth_required"])
        self.assertTrue(ext["backoff"]["active"])
        self.assertIsNotNone(ext["backoff"]["until"])
        self.assertIn("auth", ext["backoff"]["reason"])
        self.assertAlmostEqual(ext["backlog_age_seconds"], 66000, delta=120)
        self.assertEqual(ext["last_run_outcome"], "error")

    def test_journal_fallback_and_newer_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            journal = data_dir / "autonomous-runs.jsonl"
            journal.write_text(
                json.dumps({"finished_at": iso(-30), "outcome": "stuck"}) + "\n"
            )
            ext = run_health.compose_health_extension(data_dir)
            self.assertEqual(ext["last_run_outcome"], "stuck")
            # an older stage-store record must not beat the newer journal line
            run_health.StageHealthStore(data_dir).record_run_result(
                "ok", iso(-7200), iso(-3600)
            )
            ext = run_health.compose_health_extension(data_dir)
            self.assertEqual(ext["last_run_outcome"], "stuck")

    def test_stale_export_and_capture_degrade_at_read_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            stages = run_health.StageHealthStore(data_dir)
            old = iso(-(run_health.STALE_AFTER_SECONDS + 600))
            stages.update_stage("export", "ok", at=old)
            stages.update_stage("capture", "ok", at=old)
            (data_dir / "verification.json").write_text(
                json.dumps({"verified_at": old, "pass": True})
            )
            ext = run_health.compose_health_extension(data_dir)
            self.assertEqual(ext["stages"]["export"]["state"], "degraded")
            self.assertIn("stale", ext["stages"]["export"]["detail"])
            self.assertEqual(ext["stages"]["capture"]["state"], "degraded")

    def test_restore_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            self.assertIsNone(run_health.restore_block(data_dir))
            (data_dir / run_health.RESTORE_STATE_FILE).write_text(
                json.dumps({"scanning": "disabled", "source_backup": "b1"})
            )
            message = run_health.restore_block(data_dir)
            self.assertIsNotNone(message)
            self.assertIn("b1", message)


class BuildHealthIntegrationTests(unittest.TestCase):
    def test_liveness_independent_of_stage_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            (data_dir / "status.json").write_text(
                json.dumps({"updated_at": iso(), "gate_ready": True, "resources": 1,
                            "status_counts": {}})
            )
            (data_dir / "dashboard.html").write_text("<html></html>")
            run_health.StageHealthStore(data_dir).update_stage(
                "semantic_review", "auth_required", detail="model HTTP 401"
            )
            health = radar_server.build_health(data_dir)
            self.assertTrue(health["ok"], "viewer liveness must stay independent")
            self.assertEqual(
                health["stages"]["semantic_review"]["state"], "auth_required"
            )
            for field in json.loads((FIXTURES / "c4-health.json").read_text())[
                "frozen_fields"
            ]:
                self.assertIn(field, health)

    def test_extension_failure_never_breaks_health_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(
                run_health, "compose_health_extension", side_effect=OSError("disk")
            ):
                health = radar_server.build_health(Path(tmp))
            self.assertIn("health_extension_error", health)
            self.assertIn("stages", health)
            self.assertIn("service", health)


@contextlib.contextmanager
def loop_env(pending_rows=0):
    """A run_workflow sandbox: temp DATA_DIR/DB/journal plus safe mocks."""
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp) / "out"
        data_dir.mkdir(parents=True)
        db_path = Path(tmp) / "t.sqlite3"
        conn_probe = gm.connect_db(db_path)
        now = iso()
        for index in range(pending_rows):
            conn_probe.execute(
                "INSERT INTO resources(resource_id, kind, first_message_id,"
                " last_message_id, sender_id, source_text, status,"
                " first_seen_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    "tweet:{}".format(index), "tweet", str(100 + index),
                    str(100 + index), "42", "text", "pending_review", now, now,
                ),
            )
        conn_probe.commit()
        conn_probe.close()

        profile = {
            "conversation": {"capture_scope": "all_senders"},
            "owners": [],
            "bootstrap": {"resume_after_message_id": 1},
            "selection": {"project_areas": {}},
        }
        sync_details = {
            "hydration": {"attempted": 0, "hydrated": 0, "failed": 0},
        }
        real_connect_db = gm.connect_db
        with contextlib.ExitStack() as stack:
            patch = stack.enter_context
            patch(mock.patch.object(gm, "DATA_DIR", data_dir))
            patch(mock.patch.object(gm, "connect_db",
                                    side_effect=lambda: real_connect_db(db_path)))
            patch(mock.patch.object(gm, "load_profile", return_value=profile))
            sync = patch(mock.patch.object(
                gm, "sync_once", return_value=(sync_details, True)))
            patch(mock.patch.object(loop, "RUN_JOURNAL",
                                    data_dir / "autonomous-runs.jsonl"))
            patch(mock.patch.object(loop, "refresh_architecture",
                                    return_value={"refreshed": True, "detail": ""}))
            import telegram_decisions
            patch(mock.patch.object(telegram_decisions, "pull",
                                    return_value={"applied": 0}))
            import ingest_bookmarks
            patch(mock.patch.object(ingest_bookmarks, "ingest_live",
                                    return_value={"ingested": 0}))
            import enrich_tools
            patch(mock.patch.object(enrich_tools, "enrich",
                                    return_value={"fetched": 0}))
            patch(mock.patch.object(gm, "select_resource_rows", return_value=[]))
            patch(mock.patch.object(gm, "build_tool_index", return_value=[]))
            patch(mock.patch.object(gm, "load_verdicts", return_value={}))
            codex = patch(mock.patch.object(loop, "run_codex_review"))
            patch(mock.patch.object(
                gm, "prepare_review_batch",
                side_effect=lambda conn, prof, limit, path: {
                    "count": conn.execute(
                        "SELECT COUNT(*) FROM resources WHERE status = 'pending_review'"
                    ).fetchone()[0]
                },
            ))

            def apply_all(conn, prof, output_path):
                cursor = conn.execute(
                    "UPDATE resources SET status = 'relevant'"
                    " WHERE status = 'pending_review'"
                )
                conn.commit()
                return {"applied": cursor.rowcount}

            apply_mock = patch(mock.patch.object(
                gm, "apply_decisions", side_effect=apply_all))
            export = patch(mock.patch.object(
                gm, "export_relevant", return_value={"relevant": pending_rows}))
            patch(mock.patch.object(
                gm, "verify", return_value={"pass": True, "problems": [],
                                            "verified_at": iso()}))
            notify = patch(mock.patch.object(
                gm, "notify_relevant", return_value={"sent": pending_rows}))
            yield {
                "data_dir": data_dir,
                "db_path": db_path,
                "codex": codex,
                "sync": sync,
                "notify": notify,
                "export": export,
                "apply": apply_mock,
            }


def read_journal(data_dir):
    path = data_dir / "autonomous-runs.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class LoopStageDrillTests(unittest.TestCase):
    def _quiet_run(self, **kwargs):
        with contextlib.redirect_stdout(None):
            return loop.run_workflow(no_record=True, **kwargs)

    def test_model_401_marks_stage_and_opens_circuit_while_capture_continues(self):
        with loop_env(pending_rows=3) as env:
            env["codex"].side_effect = RuntimeError("stream error: HTTP 401 Unauthorized")
            code = self._quiet_run(no_notify=True)
            self.assertEqual(code, 1)
            entry = read_journal(env["data_dir"])[-1]
            self.assertEqual(entry["outcome"], "error")
            self.assertEqual(entry["review_blocked"]["class"], "auth")
            ext = run_health.compose_health_extension(env["data_dir"])
            self.assertEqual(ext["stages"]["semantic_review"]["state"], "auth_required")
            self.assertEqual(ext["stages"]["capture"]["state"], "ok",
                             "capture succeeded and must say so")
            self.assertFalse(ext["auth_required"], "one failure is not yet a circuit")
            # second consecutive 401 opens the circuit (threshold 2 for auth)
            env["codex"].side_effect = RuntimeError("HTTP 401 Unauthorized")
            self._quiet_run(no_notify=True)
            ext = run_health.compose_health_extension(env["data_dir"])
            self.assertTrue(ext["auth_required"])
            self.assertTrue(ext["backoff"]["active"])
            # third run: circuit open -> semantic review skipped, capture still runs
            env["codex"].side_effect = AssertionError("codex must not be invoked")
            code = self._quiet_run(no_notify=True)
            self.assertEqual(code, 1)
            entry = read_journal(env["data_dir"])[-1]
            self.assertEqual(entry["outcome"], "stuck")
            self.assertTrue(entry["review_blocked"]["skipped"])
            self.assertEqual(env["sync"].call_count, 3, "capture ran every time")

    def test_rate_limit_classifies_and_backs_off(self):
        with loop_env(pending_rows=1) as env:
            env["codex"].side_effect = RuntimeError("HTTP 429 Too Many Requests")
            self._quiet_run(no_notify=True)
            entry = read_journal(env["data_dir"])[-1]
            self.assertEqual(entry["review_blocked"]["class"], "rate_limit")
            domain = run_health.BackoffStore(env["data_dir"]).domain("semantic_review")
            self.assertEqual(domain["failure_class"], "rate_limit")
            self.assertEqual(domain["consecutive_failures"], 1)

    def test_review_timeout_is_stuck_not_error(self):
        with loop_env(pending_rows=1) as env:
            env["codex"].side_effect = subprocess.TimeoutExpired("codex", 600)
            code = self._quiet_run(no_notify=True)
            self.assertEqual(code, 1)
            entry = read_journal(env["data_dir"])[-1]
            self.assertEqual(entry["outcome"], "stuck")
            self.assertEqual(entry["review_blocked"]["class"], "timeout")

    def test_dns_failure_in_capture_marks_capture_failed(self):
        with loop_env() as env:
            env["sync"].side_effect = RuntimeError(
                "nodename nor servname provided, or not known"
            )
            env["sync"].return_value = None
            code = self._quiet_run(no_notify=True)
            self.assertEqual(code, 1)
            ext = run_health.compose_health_extension(env["data_dir"])
            self.assertEqual(ext["stages"]["capture"]["state"], "failed")
            domain = run_health.BackoffStore(env["data_dir"]).domain("capture")
            self.assertEqual(domain["failure_class"], "dns")

    def test_open_capture_circuit_skips_x_but_reviews_backlog(self):
        with loop_env(pending_rows=2) as env:
            store = run_health.BackoffStore(env["data_dir"])
            for _ in range(3):
                store.record_failure("capture", "dns", "getaddrinfo failed")
            env["codex"].return_value = None
            code = self._quiet_run(no_notify=True)
            self.assertEqual(code, 0, "review + gate can pass while X is down")
            entry = read_journal(env["data_dir"])[-1]
            self.assertEqual(entry["outcome"], "ok")
            self.assertIn("capture skipped", entry["note"])
            self.assertEqual(env["sync"].call_count, 0, "X capture must be skipped")
            self.assertEqual(entry["sync"].get("skipped", "")[:15], "capture backoff")

    def test_backoff_state_survives_process_restart_via_cli(self):
        with loop_env(pending_rows=1) as env:
            env["codex"].side_effect = RuntimeError("HTTP 401")
            self._quiet_run(no_notify=True)
            self._quiet_run(no_notify=True)
            result = subprocess.run(
                [sys.executable, str(SCRIPTS / "run_health.py"),
                 "--data-dir", str(env["data_dir"]), "status"],
                capture_output=True, text=True, timeout=30, check=True,
            )
            payload = json.loads(result.stdout)
            self.assertTrue(payload["backoff"]["active"])
            self.assertTrue(payload["auth_required"])

    def test_cleanup_failure_cannot_conceal_the_run_result(self):
        with loop_env() as env:
            with mock.patch.object(
                loop, "record_loopsmith", side_effect=RuntimeError("ledger down")
            ):
                with contextlib.redirect_stdout(None):
                    code = loop.run_workflow(no_record=False, no_notify=True)
            self.assertEqual(code, 0, "cleanup failure must not flip the exit code")
            entry = read_journal(env["data_dir"])[-1]
            self.assertEqual(entry["outcome"], "ok")
            self.assertIn("ledger down", entry["record_error"])

    def test_recovery_drill_drains_backlog_once_without_duplicates(self):
        with loop_env(pending_rows=5) as env:
            store = run_health.BackoffStore(env["data_dir"])
            store.record_failure("semantic_review", "auth", "HTTP 401")
            store.record_failure("semantic_review", "auth", "HTTP 401")
            # operator resolves auth outside the system, then resets
            store.reset("semantic_review", reason="reauthenticated")
            env["codex"].return_value = None
            code = self._quiet_run(no_notify=False)
            self.assertEqual(code, 0)
            entry = read_journal(env["data_dir"])[-1]
            self.assertEqual(entry["outcome"], "ok")
            self.assertEqual(len(entry["review_batches"]), 1)
            self.assertEqual(entry["review_batches"][0]["applied"], 5)
            self.assertEqual(env["notify"].call_count, 1)
            ext = run_health.compose_health_extension(env["data_dir"])
            self.assertIn(ext["stages"]["semantic_review"]["state"], ("ok", "recovering"))
            self.assertIsNotNone(ext["last_semantic_success_at"])
            self.assertFalse(ext["backoff"]["active"])
            # a second run finds nothing pending and must not re-notify items
            code = self._quiet_run(no_notify=False)
            self.assertEqual(code, 0)
            self.assertEqual(env["notify"].call_count, 2,
                             "notify_relevant is invoked per run; its per-item "
                             "dedup is covered by existing notified_at tests")
            decided = read_journal(env["data_dir"])[-1]
            self.assertEqual(decided["review_batches"], [])

    def test_restored_data_dir_refuses_to_scan(self):
        with loop_env() as env:
            (env["data_dir"] / run_health.RESTORE_STATE_FILE).write_text(
                json.dumps({"scanning": "disabled", "source_backup": "b9"})
            )
            code = self._quiet_run(no_notify=True)
            self.assertEqual(code, 3)
            self.assertEqual(read_journal(env["data_dir"]), [])
            self.assertEqual(env["sync"].call_count, 0)


if __name__ == "__main__":
    unittest.main()


class CapacityClassTests(unittest.TestCase):
    """A page cap is backlog, not a fault: it must never stop capture."""

    def test_page_cap_classifies_as_capacity(self):
        self.assertEqual(
            run_health.classify_failure(
                "page cap reached before durable checkpoint 800"),
            "capacity")
        self.assertEqual(
            run_health.classify_failure("capture did not reach the durable checkpoint"),
            "capacity")

    def test_capacity_failures_never_open_a_circuit(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = run_health.BackoffStore(Path(tmp))
            for _ in range(10):
                entry = store.record_failure("capture", "capacity", "page cap reached")
            self.assertEqual(entry["state"], "closed")
            self.assertEqual(entry["consecutive_failures"], 10)
            self.assertIsNone(entry.get("until"))
            gate = store.check("capture")
            self.assertTrue(gate["allowed"], "capture must keep draining the backlog")

    def test_loop_marks_capacity_shortfall_degraded_and_keeps_capturing(self):
        with loop_env() as env:
            env["sync"].return_value = (
                {"hydration": {"attempted": 0, "hydrated": 0, "failed": 0}}, False)
            with mock.patch.object(
                gm, "get_meta",
                side_effect=lambda conn, key, default=None: (
                    "page cap reached before durable checkpoint 800"
                    if key == "last_fetch_error" else (default or "0")),
            ):
                for _ in range(4):
                    with contextlib.redirect_stdout(None):
                        loop.run_workflow(no_record=True, no_notify=True)
            ext = run_health.compose_health_extension(env["data_dir"])
            self.assertEqual(ext["stages"]["capture"]["state"], "degraded")
            self.assertFalse(ext["backoff"]["active"],
                             "a page cap must never open the capture circuit")
            self.assertEqual(env["sync"].call_count, 4,
                             "capture must be attempted every run")
