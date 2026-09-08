"""Lane 01 acceptance tests — lossless cross-source capture (audit A01) and
native media / content coverage (audit A04), contract revision c1.

Every test writes only to a temporary database; nothing touches live data,
network, or notifications.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import group_monitor as gm  # noqa: E402
import ingest_bookmarks as ingest  # noqa: E402

PROFILE = {
    "conversation": {"id": "123", "capture_scope": "all_senders"},
    "owners": [{"sender_id": "42", "username": "owner"}],
    "bootstrap": {"resume_after_message_id": "100"},
    "selection": {
        "minimum_score": 3,
        "ai_weight": 4,
        "ai_terms": ["ai"],
        "project_areas": {},
    },
}


def tweet_message(tweet_id, message_id, sender_id="77", extra_text=""):
    url = "https://x.com/i/status/{}".format(tweet_id)
    text = (url + " " + extra_text).strip()
    return {
        "id": message_id,
        "time": int(message_id) if str(message_id).isdigit() else 1,
        "sender_id": sender_id,
        "text": text,
        "urls": [url],
    }


def batch(messages, reached=True):
    numeric = [m["id"] for m in messages if str(m["id"]).isdigit()]
    return gm.FetchResult(
        messages=messages,
        pages=1,
        reached_checkpoint=reached,
        newest_message_id=max(numeric, key=int) if numeric else None,
        oldest_message_id=min(numeric, key=int) if numeric else None,
    )


class TempDbCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.object(gm, "DATA_DIR", Path(self._tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.conn = gm.connect_db(Path(self._tmp.name) / "lane01.sqlite3")
        self.addCleanup(self.conn.close)

    def occurrences(self, resource_id):
        return {
            row[0]
            for row in self.conn.execute(
                "SELECT message_id FROM message_resources WHERE resource_id = ?",
                (resource_id,),
            )
        }

    def resource(self, resource_id):
        return self.conn.execute(
            "SELECT * FROM resources WHERE resource_id = ?", (resource_id,)
        ).fetchone()

    def record(self, resource_id):
        for row in gm.select_resource_rows(self.conn):
            if row["resource_id"] == resource_id:
                return gm.resource_to_dict(row)
        raise AssertionError("resource not selected: " + resource_id)


class OrderingRuleTests(unittest.TestCase):
    """The explicit typed occurrence-ordering rule (C1)."""

    def test_group_ids_are_numeric_and_outrank_synthetic_ids(self):
        self.assertTrue(gm.is_group_message_id("101"))
        self.assertFalse(gm.is_group_message_id("bookmark:999"))
        self.assertEqual(
            max("bookmark:999", "101", key=gm.message_order_key), "101"
        )
        self.assertEqual(
            max("9", "101", key=gm.message_order_key), "101",
            "group ids compare numerically, not lexically",
        )
        self.assertEqual(
            max("bookmark:111", "bookmark:99", key=gm.message_order_key),
            "bookmark:99",
            "synthetic ids order lexically among themselves (stable only)",
        )


class CrossSourceProvenanceTests(TempDbCase):
    """A01: both arrival orders keep one resource, all occurrences, and group
    visibility; an overlapping bookmark cannot abort unrelated capture."""

    def test_bookmark_first_group_later_is_lossless(self):
        ingest.ingest(
            self.conn,
            PROFILE,
            [{"id": "999", "text": "AI tool"}],
            ingest.SOURCE_ARCHIVE,
            True,
            progress_every=0,
        )
        overlapping = tweet_message("999", "101")
        unrelated = tweet_message("888", "102", sender_id="88")
        stats = gm.persist_fetch(
            self.conn, PROFILE, batch([overlapping, unrelated]), "100"
        )

        self.assertEqual(stats["messages"], 2)
        self.assertEqual(self.occurrences("tweet:999"), {"bookmark:999", "101"})
        self.assertEqual(
            self.occurrences("tweet:888"),
            {"102"},
            "an overlapping bookmark must not abort unrelated group capture",
        )
        row = self.resource("tweet:999")
        self.assertEqual(row["source"], ingest.SOURCE_ARCHIVE,
                         "first origin is preserved, not overwritten")
        self.assertEqual(row["first_message_id"], "bookmark:999")
        self.assertEqual(row["last_message_id"], "101",
                         "a real group occurrence supplies chronology")
        self.assertEqual(gm.get_meta(self.conn, "fetch_cursor"), "102")

        record = self.record("tweet:999")
        self.assertTrue(record["in_group"])
        self.assertEqual(record["group_share_count"], 1)
        self.assertEqual(record["origins"], sorted({ingest.SOURCE_ARCHIVE, "group"}))
        self.assertEqual(record["share_count"], 2)
        carried = gm.select_payload_records([record])
        self.assertEqual(
            [r["resource_id"] for r in carried],
            ["tweet:999"],
            "a group-shared resource travels with the briefing even when its "
            "first origin was a bookmark",
        )

    def test_group_first_bookmark_later_is_lossless(self):
        gm.persist_fetch(self.conn, PROFILE, batch([tweet_message("998", "102")]), "100")
        ingest.ingest(
            self.conn,
            PROFILE,
            [{"id": "998", "text": "AI tool"}],
            ingest.SOURCE_ARCHIVE,
            True,
            progress_every=0,
        )
        self.assertEqual(self.occurrences("tweet:998"), {"102", "bookmark:998"})
        row = self.resource("tweet:998")
        self.assertEqual(row["source"], "group")
        self.assertEqual(row["last_message_id"], "102",
                         "a synthetic occurrence never displaces group chronology")
        record = self.record("tweet:998")
        self.assertTrue(record["in_group"])
        self.assertEqual(record["origins"], ["group"])

    def test_replay_out_of_order_multi_sender_multi_link(self):
        first = {
            "id": "103",
            "time": 103,
            "sender_id": "77",
            "text": "https://x.com/i/status/555 and https://example.com/tool",
            "urls": ["https://x.com/i/status/555", "https://example.com/tool"],
        }
        second = tweet_message("555", "104", sender_id="88")
        # Out-of-order arrival: newer message delivered first.
        gm.persist_fetch(self.conn, PROFILE, batch([second, first]), "100")
        url_id = gm.extract_resources(first)[1]["resource_id"]

        self.assertEqual(self.occurrences("tweet:555"), {"103", "104"})
        self.assertEqual(self.occurrences(url_id), {"103"})
        self.assertEqual(self.resource("tweet:555")["last_message_id"], "104")

        # Mark the older occurrence notified, then replay the whole batch.
        self.conn.execute(
            "UPDATE message_resources SET notified_at = '2026-09-06T00:00:00+00:00' "
            "WHERE message_id = '103' AND resource_id = 'tweet:555'"
        )
        self.conn.commit()
        gm.persist_fetch(self.conn, PROFILE, batch([first, second]), "100")

        self.assertEqual(self.occurrences("tweet:555"), {"103", "104"},
                         "replay must not lose or duplicate occurrences")
        record = self.record("tweet:555")
        self.assertEqual(record["share_count"], 2)
        self.assertEqual(record["sharer_count"], 2)
        notified = self.conn.execute(
            "SELECT notified_at FROM message_resources "
            "WHERE message_id = '103' AND resource_id = 'tweet:555'"
        ).fetchone()[0]
        self.assertIsNotNone(
            notified, "replay must not rearm an already-notified occurrence"
        )

    def test_failure_before_commit_moves_nothing_durable(self):
        gm.initialize_cursor(self.conn, PROFILE)
        good = tweet_message("777", "105")
        poison = tweet_message("778", "106")
        real_extract = gm.extract_resources

        def poisoned(message):
            if message["id"] == "106":
                raise RuntimeError("injected mid-batch failure")
            return real_extract(message)

        with mock.patch.object(gm, "extract_resources", side_effect=poisoned):
            with self.assertRaises(RuntimeError):
                gm.persist_fetch(self.conn, PROFILE, batch([good, poison]), "100")

        self.assertEqual(gm.get_meta(self.conn, "fetch_cursor"), "100",
                         "a failed persist must not move the durable cursor")
        self.assertIsNone(
            self.conn.execute(
                "SELECT 1 FROM messages WHERE message_id = '105'"
            ).fetchone(),
            "a failed batch rolls back entirely — no partial capture",
        )
        # The same batch succeeds untouched afterwards: nothing was consumed.
        gm.persist_fetch(self.conn, PROFILE, batch([good, poison]), "100")
        self.assertEqual(self.occurrences("tweet:777"), {"105"})
        self.assertEqual(gm.get_meta(self.conn, "fetch_cursor"), "106")


class ContentCoverageFixtureTests(TempDbCase):
    """A04: every message shape persists with an evidence-backed classification
    or a visible, explicit processing limitation — never silently vanishes."""

    def persist_one(self, message):
        gm.persist_fetch(self.conn, PROFILE, batch([message]), "100")

    def test_text_only_message_is_a_note(self):
        self.persist_one(
            {"id": "110", "time": 110, "sender_id": "77", "text": "just a thought", "urls": []}
        )
        row = self.resource("note:110")
        self.assertEqual(row["kind"], "note")
        self.assertIsNone(row["extraction_state"],
                          "a note is its own content; nothing awaits extraction")

    def test_generic_link_only_message_is_pending_extraction(self):
        self.persist_one(
            {
                "id": "111",
                "time": 111,
                "sender_id": "77",
                "text": "https://example.com/paper.pdf",
                "urls": ["https://example.com/paper.pdf"],
            }
        )
        row = self.conn.execute(
            "SELECT * FROM resources WHERE kind = 'url'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["extraction_state"], "pending",
                         "URL text is not proof the destination was read")
        self.assertTrue(row["extraction_detail"])
        self.assertIsNotNone(row["extraction_checked_at"])

    def test_embedded_tweet_stays_a_tweet_resource_without_media_double(self):
        self.persist_one(
            {
                "id": "112",
                "time": 112,
                "sender_id": "77",
                "text": "",
                "urls": [],
                "attachment_tweet": {"id": "556", "text": "AI workflow"},
                "attachment_raw": {},
            }
        )
        self.assertEqual(self.occurrences("tweet:556"), {"112"})
        self.assertIsNone(
            self.conn.execute(
                "SELECT 1 FROM resources WHERE resource_id = 'media:112'"
            ).fetchone(),
            "embedded-tweet media belongs to the tweet payload, not a media row",
        )

    def test_native_image_only_message_persists_with_pending_state(self):
        self.persist_one(
            {
                "id": "113",
                "time": 113,
                "sender_id": "77",
                "text": "",
                "urls": [],
                "attachment": {"photo": {"url": "https://pbs.twimg.com/media/synthetic.jpg"}},
            }
        )
        row = self.resource("media:113")
        self.assertEqual(row["kind"], "media")
        self.assertEqual(row["status"], "pending_review",
                         "relevance stays undetermined — separate from processing")
        self.assertEqual(row["extraction_state"], "pending")
        payload = json.loads(row["payload_json"])
        self.assertEqual(
            [m["url"] for m in payload["media"]],
            ["https://pbs.twimg.com/media/synthetic.jpg"],
            "raw metadata needed for a later retry is retained",
        )
        record = self.record("media:113")
        self.assertEqual(record["media_urls"],
                         ["https://pbs.twimg.com/media/synthetic.jpg"])
        self.assertEqual(record["extraction_state"], "pending")

    def test_native_video_only_message_persists_with_pending_state(self):
        self.persist_one(
            {
                "id": "114",
                "time": 114,
                "sender_id": "77",
                "text": "",
                "urls": [],
                "attachment_raw": {"video": {"url": "https://video.twimg.com/v/synthetic.mp4"}},
            }
        )
        row = self.resource("media:114")
        self.assertEqual(row["extraction_state"], "pending")
        self.assertIn("video", json.loads(row["payload_json"])["attachment_keys"])

    def test_unknown_attachment_is_retained_as_explicit_unsupported(self):
        self.persist_one(
            {
                "id": "115",
                "time": 115,
                "sender_id": "77",
                "text": "",
                "urls": [],
                "attachment": {"card": {"name": "mystery"}},
            }
        )
        row = self.resource("media:115")
        self.assertEqual(row["extraction_state"], "unsupported",
                         "malformed/unknown content is retained with a reason, "
                         "not silently skipped")
        self.assertIn("card", row["extraction_detail"])
        self.assertEqual(row["status"], "pending_review",
                         "unreadable is not irrelevant and not proven deleted")

    def test_text_with_media_keeps_both_note_and_media(self):
        self.persist_one(
            {
                "id": "116",
                "time": 116,
                "sender_id": "77",
                "text": "look at this",
                "urls": [],
                "attachment": {"photo": {"url": "https://pbs.twimg.com/media/two.jpg"}},
            }
        )
        self.assertEqual(self.occurrences("note:116"), {"116"})
        self.assertEqual(self.occurrences("media:116"), {"116"})

    def test_media_replay_is_idempotent(self):
        message = {
            "id": "117",
            "time": 117,
            "sender_id": "77",
            "text": "",
            "urls": [],
            "attachment": {"photo": {"url": "https://pbs.twimg.com/media/replay.jpg"}},
        }
        self.persist_one(message)
        self.persist_one(dict(message))
        self.assertEqual(self.occurrences("media:117"), {"117"})
        count = self.conn.execute(
            "SELECT COUNT(*) FROM resources WHERE resource_id = 'media:117'"
        ).fetchone()[0]
        self.assertEqual(count, 1)


class PayloadVisibilityTests(unittest.TestCase):
    """select_payload_records honors occurrence-derived group membership while
    keeping the imported cap for purely-bookmark rows."""

    def test_group_occurrence_beats_first_origin_label(self):
        records = [
            {"resource_id": "a", "source": "bookmark-archive", "status": "irrelevant",
             "in_group": True},
            {"resource_id": "b", "source": "bookmark-archive", "status": "irrelevant",
             "in_group": False},
            {"resource_id": "c", "source": "group", "status": "irrelevant"},
        ]
        carried = [r["resource_id"] for r in gm.select_payload_records(records)]
        self.assertIn("a", carried)
        self.assertIn("c", carried)
        self.assertNotIn("b", carried,
                         "a purely-bookmark irrelevant row still stays out")

    def test_records_without_the_new_key_fall_back_to_source(self):
        records = [
            {"resource_id": "g", "source": "group", "status": "irrelevant"},
            {"resource_id": "i", "source": "bookmark-archive", "status": "relevant",
             "pick_score": 1.0},
        ]
        carried = [r["resource_id"] for r in gm.select_payload_records(records)]
        self.assertEqual(carried, ["g", "i"])


if __name__ == "__main__":
    unittest.main()
