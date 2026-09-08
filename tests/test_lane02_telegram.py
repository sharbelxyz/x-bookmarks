"""Lane 02 acceptance tests — A05 Telegram/log recovery + outcome trial fields.

All remote interaction is mocked at subprocess.run with byte-exact framed
responses; all files live in temporary directories.
"""
import base64
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import radar_server as server  # noqa: E402
import telegram_decisions as telegram  # noqa: E402


def frame(inode, payload: bytes, size=None, prefix_of: bytes = None,
          prefix_len: int = None) -> bytes:
    """Build the labeled remote response the consumer protocol expects.

    ``prefix_len`` emulates the remote's ``head -c N``: the consumer asks for
    the length it stored at checkpoint time, so append-only growth keeps the
    hash stable. Defaults to the protocol default for fresh identities.
    """
    content = prefix_of if prefix_of is not None else payload
    length = telegram.DEFAULT_PREFIX_LEN if prefix_len is None else prefix_len
    prefix_hash = hashlib.sha256(content[:length]).hexdigest()
    size = len(content) if size is None else size
    return ("ident {}\nprefix {}\nsize {}\npayload\n".format(inode, prefix_hash, size)
            .encode("utf-8") + payload)


def line(record: dict) -> bytes:
    return json.dumps(record, ensure_ascii=False).encode("utf-8") + b"\n"


def remote(*responses):
    """subprocess.run mock returning framed responses in order."""
    return mock.patch.object(
        telegram.subprocess, "run",
        side_effect=[SimpleNamespace(returncode=0, stdout=r, stderr=b"") for r in responses])


class TelegramCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.data = base / "data"
        self.data.mkdir()
        self.pending = self.data / "pending-decisions.json"
        self.offset = self.data / "telegram-offset.json"
        self.verdicts = base / "verdicts.json"
        self.key_file = base / "dummy-key"
        self.key_file.touch()
        self.patches = [
            mock.patch.object(telegram, "DATA_DIR", self.data),
            mock.patch.object(telegram, "PENDING_PATH", self.pending),
            mock.patch.object(telegram, "OFFSET_PATH", self.offset),
            mock.patch.object(telegram, "VPS_KEY", self.key_file),
            mock.patch.object(server, "VERDICTS_PATH", self.verdicts),
            mock.patch.object(server, "OUTCOMES_PATH", base / "outcomes.json"),
            mock.patch.object(server, "TOOL_INDEX_PATH", base / "dashboard-data.json"),
        ]
        for p in self.patches:
            p.start()
        self.pending.write_text(json.dumps({
            "aaaa1111": {"key": "github.com/example/tool", "name": "tool",
                         "offered_at": "2026-09-05T10:00:00+00:00"}}))

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def rejected_lines(self):
        path = self.data / "telegram-rejected.jsonl"
        if not path.exists():
            return []
        return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]

    def received_lines(self):
        path = self.data / "telegram-received.jsonl"
        if not path.exists():
            return []
        return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


class A05RotationTruncation(TelegramCase):
    def test_first_pull_establishes_identity_and_applies(self):
        payload = line({"id": "aaaa1111", "action": "y", "at": "2026-09-06T12:00:00+00:00"})
        with remote(frame(777, payload)):
            result = telegram.pull()
        self.assertEqual(result["applied"], 1)
        self.assertEqual(result["offset"], len(payload))
        checkpoint = json.loads(self.offset.read_text())
        self.assertEqual(checkpoint["version"], telegram.CHECKPOINT_VERSION)
        self.assertEqual(checkpoint["log_identity"]["inode"], "777")
        self.assertEqual(len(checkpoint["consumed_ids"]), 1)
        saved = json.loads(self.verdicts.read_text())["verdicts"][0]
        self.assertEqual(saved["decided_by"], "telegram")
        self.assertEqual(saved["verdict"], "must_try")
        self.assertTrue(saved["source_event"])
        self.assertEqual(len(self.received_lines()), 1, "event journaled before advancing")

    def test_truncated_log_is_reread_never_skipped(self):
        first = line({"id": "aaaa1111", "action": "y"})
        with remote(frame(777, first)):
            telegram.pull()
        # Log shrank in place (same inode): a fresh decision now sits at the
        # start, BELOW the old offset. A byte-offset consumer would skip it.
        self.pending.write_text(json.dumps({
            "bbbb2222": {"key": "github.com/example/other", "name": "other",
                         "offered_at": "2026-09-05T10:00:00+00:00"}}))
        replacement = line({"id": "bbbb2222", "action": "n"})
        shrunk = frame(777, b"", size=len(replacement), prefix_of=replacement)
        full = frame(777, replacement)
        with remote(shrunk, full) as runner:
            result = telegram.pull()
        self.assertEqual(runner.call_count, 2, "shrink must trigger a re-read from the start")
        self.assertTrue(result["reread"])
        self.assertEqual(result["applied"], 1)
        keys = {e["key"]: e["verdict"] for e in json.loads(self.verdicts.read_text())["verdicts"]}
        self.assertEqual(keys["github.com/example/other"], "excluded")

    def test_rotated_inode_is_reread(self):
        first = line({"id": "aaaa1111", "action": "y"})
        with remote(frame(777, first)):
            telegram.pull()
        rotated = line({"id": "aaaa1111", "action": "n"})  # same id re-offered post-rotation
        with remote(frame(888, rotated, size=len(rotated) + 40, prefix_of=rotated),
                    frame(888, rotated)) as runner:
            result = telegram.pull()
        self.assertEqual(runner.call_count, 2)
        self.assertEqual(result["applied"], 1, "new content after rotation applies")
        checkpoint = json.loads(self.offset.read_text())
        self.assertEqual(checkpoint["log_identity"]["inode"], "888")

    def test_same_size_rewrite_is_detected_by_prefix_hash(self):
        first = line({"id": "aaaa1111", "action": "y", "pad": "xxxx"})
        with remote(frame(777, first)):
            telegram.pull()
        # Same inode, same size, different first bytes: copytruncate-style
        # rewrite. Offset arithmetic alone can never catch this.
        rewrite = line({"id": "aaaa1111", "action": "n", "pad": "yyyy"})
        self.assertEqual(len(rewrite), len(first))
        with remote(frame(777, b"", size=len(rewrite), prefix_of=rewrite),
                    frame(777, rewrite)) as runner:
            result = telegram.pull()
        self.assertEqual(runner.call_count, 2, "prefix-hash mismatch must trigger re-read")
        self.assertTrue(result["reread"])

    def test_replayed_records_after_rotation_are_not_double_applied(self):
        payload = line({"id": "aaaa1111", "action": "y"})
        with remote(frame(777, payload)):
            telegram.pull()
        first_saved = json.loads(self.verdicts.read_text())
        # Rotation replays the identical line from offset 0.
        with remote(frame(888, payload, size=len(payload) + 10, prefix_of=payload),
                    frame(888, payload)):
            result = telegram.pull()
        self.assertEqual(result["applied"], 0)
        self.assertEqual(result["duplicates"], 1, "same event id must be skipped")
        second_saved = json.loads(self.verdicts.read_text())
        self.assertEqual(first_saved["verdicts"], second_saved["verdicts"],
                         "replay must not rewrite the entry (rank/decided_at stable)")

    def test_partial_append_is_left_unconsumed(self):
        complete = line({"id": "aaaa1111", "action": "y"})
        partial = b'{"id": "bbbb2222", "action": "\xd9\x85'  # cut mid-UTF-8, no newline
        with remote(frame(777, complete + partial)):
            result = telegram.pull()
        self.assertEqual(result["applied"], 1)
        self.assertEqual(result["offset"], len(complete),
                         "checkpoint stops at the last complete line")
        self.pending.write_text(json.dumps({
            "bbbb2222": {"key": "github.com/example/other", "name": "other",
                         "offered_at": "2026-09-05T10:00:00+00:00"}}))
        finished = partial + '\xd8\xa9", "at": "x"}'.encode("latin-1") + b"\n"
        whole = complete + finished
        stored_prefix_len = len(complete + partial)  # what the checkpoint recorded
        with remote(frame(777, finished, size=len(whole), prefix_of=whole,
                          prefix_len=stored_prefix_len)):
            result = telegram.pull()
        self.assertEqual(result["offset"], len(whole))
        self.assertEqual(result["applied"] + result["unknown"] + result["invalid"], 1,
                         "the finished record is consumed exactly once")

    def test_ssh_failure_and_missing_log_never_advance(self):
        with remote(frame(777, line({"id": "aaaa1111", "action": "y"}))):
            telegram.pull()
        before = self.offset.read_bytes()
        with mock.patch.object(telegram.subprocess, "run",
                               side_effect=telegram.subprocess.TimeoutExpired("ssh", 1)):
            result = telegram.pull()
        self.assertIn("timed out", result["error"])
        with remote(b"absent\n"):
            result = telegram.pull()
        self.assertTrue(result["absent"])
        self.assertIn("missing", result["error"])
        self.assertEqual(self.offset.read_bytes(), before, "checkpoint untouched on failure")

    def test_legacy_offset_file_is_still_readable(self):
        self.offset.write_text(json.dumps({"offset": 3, "checked_at": "2026-09-01T00:00:00+00:00"}))
        payload = b"xy\n" + line({"id": "aaaa1111", "action": "y"})
        tail = payload[3:]
        with remote(frame(777, tail, size=len(payload), prefix_of=payload)) as runner:
            result = telegram.pull()
        self.assertEqual(runner.call_count, 1, "legacy offset with intact log reads the tail only")
        self.assertEqual(result["applied"], 1)
        self.assertEqual(json.loads(self.offset.read_text())["version"], telegram.CHECKPOINT_VERSION)


class A05DurableRetention(TelegramCase):
    def test_bad_action_and_unknown_id_are_retained_with_reasons(self):
        payload = (line({"id": "aaaa1111", "action": "zz"})
                   + line({"id": "nope0000", "action": "y"}))
        with remote(frame(777, payload)):
            result = telegram.pull()
        self.assertEqual(result["unknown"], 2)
        reasons = " | ".join(r["reason"] for r in self.rejected_lines())
        self.assertIn("unsupported action 'zz'", reasons)
        self.assertIn("unknown id 'nope0000'", reasons)
        self.assertEqual(result["offset"], len(payload),
                         "retained rejects are safe to advance past")

    def test_undecodable_line_is_retained_with_raw_bytes(self):
        garbage = b"\xff\xfe{not json\n"
        payload = line({"id": "aaaa1111", "action": "y"}) + garbage
        with remote(frame(777, payload)):
            result = telegram.pull()
        self.assertEqual(result["applied"], 1)
        self.assertEqual(result["invalid"], 1)
        retained = self.rejected_lines()
        self.assertEqual(len(retained), 1)
        self.assertEqual(base64.b64decode(retained[0]["raw_base64"]), garbage.rstrip(b"\n"))

    def test_server_refusal_is_retained_and_type_rules_reach_telegram(self):
        (Path(self.tmp.name) / "dashboard-data.json").write_text(json.dumps({
            "tools": [{"key": "github.com/example/tool", "resource_type": "read"}]}))
        with remote(frame(777, line({"id": "aaaa1111", "action": "y"}))):
            result = telegram.pull()
        self.assertEqual(result["applied"], 0)
        self.assertEqual(result["rejected"], 1,
                         "must_try on a read-typed resource is refused via Telegram too")
        retained = self.rejected_lines()
        self.assertIn("does not fit", retained[0]["reason"])
        self.assertFalse(self.verdicts.exists(), "refused verdict stores nothing")

    def test_duplicate_event_in_same_batch_is_counted_once(self):
        one = line({"id": "aaaa1111", "action": "y"})
        with remote(frame(777, one + one)):
            result = telegram.pull()
        self.assertEqual(result["applied"], 1)
        self.assertEqual(result["duplicates"], 1)

    def test_crash_window_replay_is_idempotent_via_source_event(self):
        payload = line({"id": "aaaa1111", "action": "y"})
        with remote(frame(777, payload)):
            telegram.pull()
        # Simulate the crash window: checkpoint lost AFTER a successful apply.
        self.offset.unlink()
        with remote(frame(777, payload)):
            result = telegram.pull()
        self.assertEqual(result["applied"], 0)
        self.assertEqual(result["duplicates"], 1,
                         "source_event on the stored verdict blocks re-apply")
        self.assertEqual(len(json.loads(self.verdicts.read_text())["verdicts"]), 1)

    def test_failed_journal_persistence_blocks_checkpoint_advance(self):
        payload = line({"id": "aaaa1111", "action": "y"})
        with remote(frame(777, payload)), \
             mock.patch.object(telegram, "_append_jsonl", side_effect=OSError("disk full")):
            result = telegram.pull()
        self.assertIn("journal", result["error"])
        self.assertFalse(self.offset.exists(), "no checkpoint until events are journaled")

    def test_failed_reject_retention_blocks_checkpoint_advance(self):
        payload = line({"id": "nope0000", "action": "y"})
        real_append = telegram._append_jsonl

        def fail_rejected_only(path, record):
            if path.name == "telegram-rejected.jsonl":
                raise OSError("disk full")
            return real_append(path, record)

        with remote(frame(777, payload)), \
             mock.patch.object(telegram, "_append_jsonl", side_effect=fail_rejected_only):
            result = telegram.pull()
        self.assertIn("retain", result["error"])
        self.assertFalse(self.offset.exists())

    def test_corrupt_checkpoint_is_preserved_and_recovered_from_zero(self):
        self.offset.write_bytes(b"{broken checkpoint")
        payload = line({"id": "aaaa1111", "action": "y"})
        with remote(frame(777, payload)):
            result = telegram.pull()
        self.assertEqual(result["applied"], 1)
        preserved = list(self.data.glob("telegram-offset.json.corrupt-*"))
        self.assertEqual(len(preserved), 1, "corrupt checkpoint bytes preserved aside")
        self.assertEqual(preserved[0].read_bytes(), b"{broken checkpoint")

    def test_pull_summary_reports_counts_and_frozen_wrapper_shape(self):
        result = telegram.apply_decisions([{"id": "nope0000", "action": "y"}])
        self.assertEqual(set(result), {"applied", "unknown", "rejected"},
                         "public wrapper keeps the frozen 3-key shape")
        self.assertEqual(result["unknown"], 1)
        self.assertTrue(self.rejected_lines(), "wrapper still retains durably")


class OutcomeTrialFields(unittest.TestCase):
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

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_full_trial_roundtrip(self):
        code, payload = server.record_outcome({
            "key": "github.com/example/tool", "state": "kept",
            "project": "noon-briefing", "artifact": "reports/close-2026-08.md",
            "success_measure": "briefing build time under 10 min",
            "baseline": 30, "observed_result": 9.5, "units": "minutes",
            "evidence": "outputs/briefing-timing.txt", "trial_date": "2026-09-04",
        })
        self.assertEqual(code, 200)
        record = payload["record"]
        self.assertEqual(record["baseline"], 30)
        self.assertEqual(record["observed_result"], 9.5)
        self.assertEqual(record["units"], "minutes")
        self.assertEqual(record["trial_date"], "2026-09-04")
        on_disk = json.loads(self.outcomes.read_text())["outcomes"][0]
        self.assertEqual(on_disk, record)

    def test_unmeasured_numbers_stay_absent_never_fabricated(self):
        code, payload = server.record_outcome({
            "key": "github.com/example/tool", "state": "trying",
            "project": "radar", "success_measure": "fewer manual checks"})
        self.assertEqual(code, 200)
        record = payload["record"]
        for absent in ("baseline", "observed_result", "units", "evidence", "trial_date"):
            self.assertNotIn(absent, record)

    def test_garbage_trial_inputs_are_400(self):
        self.assertEqual(server.record_outcome(
            {"key": "github.com/a/b", "state": "kept", "trial_date": "someday"})[0], 400)
        self.assertEqual(server.record_outcome(
            {"key": "github.com/a/b", "state": "kept", "baseline": ["x"]})[0], 400)
        self.assertFalse(self.outcomes.exists(), "invalid trial data stores nothing")

    def test_clearing_recommendation_keeps_measured_trial(self):
        server.record_verdict({"key": "github.com/example/tool", "verdict": "must_try"})
        server.record_outcome({
            "key": "github.com/example/tool", "state": "kept",
            "baseline": "manual flow", "observed_result": "20 min saved",
            "trial_date": "2026-09-01"})
        code, _ = server.record_verdict({"key": "github.com/example/tool", "verdict": "clear"})
        self.assertEqual(code, 200)
        outcomes = json.loads(self.outcomes.read_text())["outcomes"]
        self.assertEqual(outcomes[0]["observed_result"], "20 min saved")
        self.assertEqual(outcomes[0]["trial_date"], "2026-09-01")

    def test_states_keep_recommendation_and_trial_semantics_separate(self):
        """'trying' is an in-progress trial, kept/dropped are completed ones;
        none of them is implied by a verdict, and a demo-seeded verdict alone
        never produces an outcome entry."""
        server.record_verdict({"key": "github.com/example/tool", "verdict": "must_try"})
        self.assertFalse(self.outcomes.exists(),
                         "a recommendation must never fabricate a trial")
        code, payload = server.record_outcome(
            {"key": "github.com/example/tool", "state": "trying"})
        self.assertEqual(code, 200)
        self.assertEqual(payload["record"]["state"], "trying")


if __name__ == "__main__":
    unittest.main()
