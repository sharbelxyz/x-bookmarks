import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import enrich_tools
import generate_architecture
import ingest_bookmarks
import learn_negatives
import telegram_decisions
import group_monitor as gm
import group_filter_loop as loop
import manage_radar_server
import radar_server
import resource_typing as rt
from dashboard_renderer import build_dashboard_payload, render_dashboard


class GroupMonitorTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_data_dir = gm.DATA_DIR
        gm.DATA_DIR = Path(self.tempdir.name) / "outputs"
        self.conn = gm.connect_db(Path(self.tempdir.name) / "monitor.sqlite3")
        self.profile = {
            "conversation": {"id": "group", "auth_account": "owner"},
            "owners": [{"username": "owner", "sender_id": "42"}],
            "bootstrap": {
                "resume_after_message_id": "100",
                "evidence": "unit test",
            },
            "selection": {
                "minimum_score": 3,
                "ai_weight": 4,
                "ai_terms": ["ai", "claude"],
                "project_areas": {
                    "marketplace": {
                        "label": "Marketplace",
                        "description": "Seller operations",
                        "weight": 3,
                        "keywords": ["noon", "inventory"],
                    }
                },
            },
        }

    def tearDown(self):
        self.conn.close()
        gm.DATA_DIR = self.old_data_dir
        self.tempdir.cleanup()

    def _insert_relevant(self, resource_id, message_id, text, payload=None, areas=("marketplace",)):
        now = gm.utc_now()
        self.conn.execute(
            """
            INSERT INTO messages(
                message_id, conversation_id, sent_at_ms, sender_id, is_owner,
                text, urls_json, captured_at
            ) VALUES(?, 'group', ?, '42', 1, ?, '[]', ?)
            """,
            (message_id, 1_700_000_000_000 + int(message_id), text, now),
        )
        self.conn.execute(
            """
            INSERT INTO resources(
                resource_id, kind, canonical_url, tweet_id, first_message_id, last_message_id,
                sender_id, source_text, status, payload_json, title, author, content_text,
                score, project_areas_json, reasons_json, decision_source,
                first_seen_at, updated_at
            ) VALUES(?, 'tweet', ?, ?, ?, ?, '42', ?, 'relevant', ?, ?, 'author', ?,
                     3, ?, '["fits"]', 'rules', ?, ?)
            """,
            (
                resource_id,
                "https://x.com/i/status/" + resource_id.split(":")[1],
                resource_id.split(":")[1],
                message_id,
                message_id,
                text,
                json.dumps(payload) if payload else None,
                text[:60],
                text,
                json.dumps(list(areas)),
                now,
                now,
            ),
        )
        self.conn.execute(
            "INSERT INTO message_resources(message_id, resource_id) VALUES(?, ?)",
            (message_id, resource_id),
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO senders(sender_id, username, display_name, avatar_url, is_owner, updated_at) "
            "VALUES('42', 'owner', 'Owner', '', 1, ?)",
            (now,),
        )
        self.conn.commit()

    def test_extract_resources_prefers_expanded_url(self):
        message = {
            "id": "101",
            "text": "See https://t.co/abc and https://example.com/path?utm_source=x",
            "urls": ["https://x.com/someone/status/123456789"],
        }
        resources = gm.extract_resources(message)
        self.assertEqual(
            [item["resource_id"] for item in resources],
            ["tweet:123456789", "url:5faa4bf4918ff56562141cc3"],
        )
        self.assertEqual(resources[1]["canonical_url"], "https://example.com/path")

    def test_capture_extracts_resources_from_every_sender(self):
        gm.set_meta(self.conn, "fetch_cursor", "100")
        self.conn.commit()
        result = gm.FetchResult(
            messages=[
                {
                    "id": "101",
                    "time": 1,
                    "sender_id": "42",
                    "text": "https://x.com/i/status/999",
                    "urls": ["https://x.com/i/status/999"],
                },
                {
                    "id": "102",
                    "time": 2,
                    "sender_id": "77",
                    "text": "https://x.com/i/status/888",
                    "urls": ["https://x.com/i/status/888"],
                },
            ],
            pages=1,
            reached_checkpoint=True,
            newest_message_id="102",
            oldest_message_id="101",
        )
        stats = gm.persist_fetch(self.conn, self.profile, result, "100")
        self.assertEqual(stats["owner_messages"], 1)
        self.assertEqual(stats["non_owner_messages"], 1)
        self.assertEqual(stats["new_resources"], 2)
        self.assertEqual(gm.get_meta(self.conn, "fetch_cursor"), "102")
        rows = self.conn.execute(
            "SELECT resource_id, sender_id FROM resources ORDER BY resource_id"
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in rows],
            [("tweet:888", "77"), ("tweet:999", "42")],
        )
        non_owner = self.conn.execute(
            "SELECT text, urls_json FROM messages WHERE message_id = '102'"
        ).fetchone()
        self.assertIn("status/888", non_owner["text"])
        self.assertIn("status/888", non_owner["urls_json"])

    def test_attachment_only_share_is_a_resource(self):
        resources = gm.extract_resources(
            {
                "id": "101",
                "text": "",
                "urls": [],
                "attachment_tweet": {"id": "555", "text": "AI workflow"},
            }
        )
        self.assertEqual(resources[0]["resource_id"], "tweet:555")

    def test_embedded_dm_tweet_skips_secondary_hydration(self):
        gm.set_meta(self.conn, "fetch_cursor", "100")
        self.conn.commit()
        payload = {
            "id": "999",
            "text": "AI agent course",
            "author": {"username": "teacher", "name": "Teacher"},
            "media": [],
            "quotedTweet": None,
        }
        result = gm.FetchResult(
            messages=[
                {
                    "id": "101",
                    "time": 1,
                    "sender_id": "42",
                    "text": "https://x.com/i/status/999",
                    "urls": ["https://x.com/i/status/999"],
                    "attachment_tweet": payload,
                }
            ],
            pages=1,
            reached_checkpoint=True,
            newest_message_id="101",
            oldest_message_id="101",
        )
        stats = gm.persist_fetch(self.conn, self.profile, result, "100")
        self.assertEqual(stats["attachment_hydrated"], 1)
        row = self.conn.execute(
            "SELECT status, author, content_text FROM resources WHERE resource_id = 'tweet:999'"
        ).fetchone()
        self.assertEqual(tuple(row), ("pending_review", "teacher", "AI agent course"))

    def test_attachment_urls_are_kept_and_backfilled_on_replay(self):
        attachment = gm.normalize_dm_attachment(
            {
                "tweet": {
                    "status": {
                        "id_str": "777",
                        "full_text": "Open-source repo https://t.co/abc",
                        "user": {"screen_name": "dev", "name": "Dev"},
                        "entities": {
                            "urls": [
                                {"url": "https://t.co/abc", "expanded_url": "https://github.com/dev/tool"}
                            ]
                        },
                    }
                }
            }
        )
        self.assertEqual(attachment["urls"], ["https://github.com/dev/tool"])

        gm.set_meta(self.conn, "fetch_cursor", "100")
        self.conn.commit()
        old_payload = dict(attachment)
        old_payload.pop("urls")
        first = gm.FetchResult(
            messages=[
                {
                    "id": "101",
                    "time": 1,
                    "sender_id": "42",
                    "text": "https://x.com/i/status/777",
                    "urls": ["https://x.com/i/status/777"],
                    "attachment_tweet": old_payload,
                }
            ],
            pages=1,
            reached_checkpoint=True,
            newest_message_id="101",
            oldest_message_id="101",
        )
        gm.persist_fetch(self.conn, self.profile, first, "100")
        self.conn.execute(
            "UPDATE resources SET status = 'relevant', reasons_json = '[\"x\"]', "
            "decision_source = 'rules' WHERE resource_id = 'tweet:777'"
        )
        self.conn.commit()
        replay = gm.FetchResult(
            messages=[dict(first.messages[0], attachment_tweet=attachment)],
            pages=1,
            reached_checkpoint=True,
            newest_message_id="101",
            oldest_message_id="101",
        )
        stats = gm.persist_fetch(self.conn, self.profile, replay, "100")
        self.assertEqual(stats["urls_backfilled"], 1)
        stored = json.loads(
            self.conn.execute(
                "SELECT payload_json FROM resources WHERE resource_id = 'tweet:777'"
            ).fetchone()[0]
        )
        self.assertEqual(stored["urls"], ["https://github.com/dev/tool"])
        self.assertEqual(
            self.conn.execute("SELECT status FROM resources WHERE resource_id = 'tweet:777'").fetchone()[0],
            "relevant",
        )

    def test_bird_subprocess_has_homebrew_node_on_cron_path(self):
        fake = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"id": "999", "text": "AI"}),
            stderr="",
        )
        with mock.patch.object(gm, "BIRD", Path("/opt/homebrew/bin/bird")), mock.patch.object(
            gm.subprocess, "run", return_value=fake
        ) as run:
            payload, error = gm._bird_read(
                "999", {"auth_token": "secret", "ct0": "csrf"}
            )
        self.assertIsNone(error)
        self.assertEqual(payload["id"], "999")
        path_value = run.call_args.kwargs["env"]["PATH"].split(":")
        self.assertIn("/opt/homebrew/bin", path_value)

    def test_incomplete_fetch_never_advances_cursor(self):
        gm.set_meta(self.conn, "fetch_cursor", "100")
        self.conn.commit()
        result = gm.FetchResult(
            messages=[],
            pages=5,
            reached_checkpoint=False,
            newest_message_id="500",
            oldest_message_id="401",
        )
        gm.persist_fetch(self.conn, self.profile, result, "100")
        self.assertEqual(gm.get_meta(self.conn, "fetch_cursor"), "100")
        self.assertEqual(gm.get_meta(self.conn, "fetch_incomplete"), "true")

    def test_rule_filter_is_high_recall_without_auto_rejecting(self):
        now = gm.utc_now()
        self.conn.execute(
            """
            INSERT INTO resources(
                resource_id, kind, canonical_url, first_message_id, last_message_id,
                sender_id, source_text, status, content_text, first_seen_at, updated_at
            ) VALUES('note:1', 'note', '', '1', '1', '42', '',
                     'pending_review', 'Claude AI agent memory system', ?, ?)
            """,
            (now, now),
        )
        self.conn.execute(
            """
            INSERT INTO resources(
                resource_id, kind, canonical_url, first_message_id, last_message_id,
                sender_id, source_text, status, content_text, first_seen_at, updated_at
            ) VALUES('note:2', 'note', '', '2', '2', '42', '',
                     'pending_review', 'A funny football clip', ?, ?)
            """,
            (now, now),
        )
        self.conn.commit()
        result = gm.apply_rule_classification(self.conn, self.profile)
        self.assertEqual(result["relevant_by_rules"], 1)
        states = dict(
            self.conn.execute("SELECT resource_id, status FROM resources").fetchall()
        )
        self.assertEqual(states["note:1"], "relevant")
        self.assertEqual(states["note:2"], "pending_review")

    def test_semantic_decisions_are_schema_checked_and_applied(self):
        now = gm.utc_now()
        self.conn.execute(
            """
            INSERT INTO resources(
                resource_id, kind, canonical_url, first_message_id, last_message_id,
                sender_id, source_text, status, first_seen_at, updated_at
            ) VALUES('url:1', 'url', 'https://example.com', '1', '1', '42',
                     'example', 'pending_review', ?, ?)
            """,
            (now, now),
        )
        self.conn.commit()
        decisions = Path(self.tempdir.name) / "decisions.json"
        decisions.write_text(
            json.dumps(
                {
                    "decisions": [
                        {
                            "resource_id": "url:1",
                            "relevant": True,
                            "project_areas": ["marketplace"],
                            "reason": "Useful for Noon inventory operations",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        result = gm.apply_decisions(self.conn, self.profile, decisions)
        self.assertEqual(result, {"applied": 1, "relevant": 1, "irrelevant": 0})
        row = self.conn.execute(
            "SELECT status, decision_source FROM resources WHERE resource_id = 'url:1'"
        ).fetchone()
        self.assertEqual(tuple(row), ("relevant", "claude"))
        result = gm.requeue_resources(self.conn, self.profile, ["url:1"])
        self.assertEqual(result, {"requeued": 1})
        status = self.conn.execute(
            "SELECT status FROM resources WHERE resource_id = 'url:1'"
        ).fetchone()[0]
        self.assertEqual(status, "pending_review")

    def test_decision_schema_areas_match_tracked_profile(self):
        profile = json.loads(
            (ROOT / "config" / "group-filter-profile.json").read_text(encoding="utf-8")
        )
        schema = json.loads(
            (ROOT / "config" / "group-filter-decisions.schema.json").read_text(
                encoding="utf-8"
            )
        )
        actual = set(
            schema["properties"]["decisions"]["items"]["properties"][
                "project_areas"
            ]["items"]["enum"]
        )
        expected = set(profile["selection"]["project_areas"]) | {"ai"}
        self.assertEqual(actual, expected)

    def test_notification_baseline_uses_message_cutover(self):
        now = gm.utc_now()
        gm.set_meta(self.conn, "fetch_cursor", "100")
        for resource_id, message_id in (("note:old", "90"), ("note:new", "110")):
            self.conn.execute(
                """
                INSERT INTO messages(
                    message_id, conversation_id, sent_at_ms, sender_id, is_owner,
                    text, urls_json, captured_at
                ) VALUES(?, 'group', 1, '42', 1, 'AI note', '[]', ?)
                """,
                (message_id, now),
            )
            self.conn.execute(
                """
                INSERT INTO resources(
                    resource_id, kind, canonical_url, first_message_id, last_message_id,
                    sender_id, source_text, status, reasons_json, decision_source,
                    first_seen_at, updated_at
                ) VALUES(?, 'note', '', ?, ?, '42', 'AI note', 'relevant',
                         '["AI signal"]', 'rules', ?, ?)
                """,
                (resource_id, message_id, message_id, now, now),
            )
            self.conn.execute(
                "INSERT INTO message_resources(message_id, resource_id) VALUES(?, ?)",
                (message_id, resource_id),
            )
        self.conn.commit()
        result = gm.baseline_notifications(self.conn)
        self.assertEqual(result["cutover_message_id"], "100")
        preview = gm.notify_relevant(self.conn, live=False)
        self.assertEqual(preview["pending"], 1)
        self.assertIn("AI note", preview["preview"])

    def test_resource_to_dict_adds_typing_links_and_pick_score(self):
        self._insert_relevant(
            "tweet:1001",
            "201",
            "An open-source CLI tool on GitHub for Noon sellers https://t.co/xyz",
            payload={
                "id": "1001",
                "text": "An open-source CLI tool on GitHub for Noon sellers https://t.co/xyz",
                "likeCount": 285,
                "retweetCount": 30,
                "replyCount": 4,
                "createdAt": "Fri Aug 28 22:30:02 +0000 2026",
                "urls": ["https://github.com/dev/noon-cli"],
                "media": [],
            },
        )
        row = gm.select_resource_rows(self.conn, "r.resource_id = ?", ("tweet:1001",))[0]
        record = gm.resource_to_dict(row)
        self.assertEqual(record["resource_type"], "try")
        self.assertEqual(record["external_urls"], ["https://github.com/dev/noon-cli"])
        self.assertEqual(record["external_label"], "github.com/dev/noon-cli")
        self.assertEqual(record["likes"], 285)
        self.assertEqual(record["tweet_created_at"], "2026-08-28T22:30:02+00:00")
        self.assertGreater(record["pick_score"], 0)
        self.assertEqual(record["pick_parts"]["repo"], 3.0)

    def test_export_writes_dashboard_data_json_and_briefing(self):
        self._insert_relevant(
            "tweet:2001", "301", "Open-source GitHub repo for AI agents", payload={"id": "2001", "likeCount": 10}
        )
        self._insert_relevant(
            "tweet:2002", "302", "How to run inventory checks step by step", payload={"id": "2002", "likeCount": 5}
        )
        result = gm.export_relevant(self.conn, self.profile)
        data_path = gm.DATA_DIR / "dashboard-data.json"
        self.assertTrue(data_path.exists())
        self.assertEqual(result["dashboard_data"], str(data_path))
        payload = json.loads(data_path.read_text(encoding="utf-8"))
        self.assertEqual(len(payload["resources"]), 2)
        self.assertEqual(payload["conversationId"], "group")
        self.assertEqual(payload["schedule"]["cronMinutes"], [17, 47])
        self.assertEqual(len(payload["activity"]), 14)
        self.assertEqual(sorted(payload["briefing"]["topPicks"]), ["tweet:2001", "tweet:2002"])
        self.assertEqual(payload["briefing"]["lanes"]["try"], ["tweet:2001"])
        self.assertIn("tweet:2002", payload["briefing"]["lanes"]["learn"] + payload["briefing"]["lanes"]["read"])
        html = (gm.DATA_DIR / "dashboard.html").read_text(encoding="utf-8")
        self.assertEqual(html.count('<script id="dashboard-data"'), 1)
        self.assertIn("Focus now", html)
        self.assertIn("Caught up", html)
        self.assertIn("focus-window-select", html)
        self.assertIn("ranked from all data", html)
        csv_header = (gm.DATA_DIR / "relevant.csv").read_text(encoding="utf-8").splitlines()[0]
        self.assertTrue(
            csv_header.endswith(
                "resource_type,pick_score,external_urls,verdict,verdict_why,outcome,outcome_note"
            ),
            csv_header,
        )
        gm.set_meta(self.conn, "fetch_cursor", "302")
        gm.set_meta(self.conn, "capture_scope_version", gm.CAPTURE_SCOPE_VERSION)
        self.conn.commit()
        verification = gm.verify(self.conn, strict=True)
        self.assertTrue(verification["pass"], verification["problems"])

    def test_dashboard_is_self_contained_and_escapes_script_boundaries(self):
        html = render_dashboard(
            resources=[
                {
                    "resource_id": "note:1",
                    "kind": "note",
                    "status": "relevant",
                    "title": "AI </script> resource",
                    "text": "Useful for the project",
                    "project_areas": ["marketplace"],
                    "reasons": ["Concrete fit"],
                    "sharer_ids": ["77"],
                }
            ],
            senders=[],
            status={"gate_ready": True},
            project_areas={"marketplace": "Marketplace"},
            group_name="Main Group",
            generated_at="2026-08-29T00:00:00+00:00",
        )
        self.assertIn("Group Resource Radar", html)
        self.assertIn("AI <\\/script> resource", html)
        self.assertEqual(html.count('<script id="dashboard-data"'), 1)
        self.assertIn("status.json", html)
        self.assertIn("dashboard-data.json", html)

    def test_dashboard_payload_windows_and_activity(self):
        now = rt.parse_iso("2026-08-29T20:00:00+00:00")
        records = []
        for index, (kind, days_ago, score) in enumerate(
            (("try", 1, 20.0), ("learn", 3, 12.0), ("read", 10, 9.0), ("other", 20, 30.0))
        ):
            moment = now - __import__("datetime").timedelta(days=days_ago)
            records.append(
                {
                    "resource_id": "tweet:{}".format(index),
                    "status": "relevant",
                    "resource_type": kind,
                    "pick_score": score,
                    "shared_at": moment.isoformat(),
                    "first_seen_at": moment.isoformat(),
                }
            )
        payload = build_dashboard_payload(
            records, [], {}, {}, "Group", now.isoformat(), conversation_id="g", now=now
        )
        # Fewer than five picks in 7/14 days, so the window widens until all four qualify.
        self.assertEqual(payload["briefing"]["topPicksWindowDays"], 0)
        self.assertEqual(payload["briefing"]["topPicks"][0], "tweet:3")
        self.assertEqual(payload["briefing"]["lanes"]["read"], ["tweet:2"])
        self.assertEqual(payload["briefing"]["laneTotals"]["other"], 1)
        self.assertEqual(len(payload["activity"]), 14)
        self.assertEqual(sum(day["relevant"] for day in payload["activity"]), 3)

    def test_sparse_media_rows_request_visual_review(self):
        self.assertTrue(
            loop._needs_visual(
                {
                    "text": "6k stars in 4 days",
                    "quoted_text": "",
                    "media_urls": ["https://pbs.twimg.com/media/example.jpg"],
                }
            )
        )
        self.assertFalse(
            loop._needs_visual(
                {
                    "text": "A" * 200,
                    "quoted_text": "",
                    "media_urls": ["https://pbs.twimg.com/media/example.jpg"],
                }
            )
        )

    def test_supervised_batch_override_is_capped(self):
        with self.assertRaises(ValueError):
            loop.run_workflow(no_record=True, no_notify=True, max_batches=21)

    def test_tool_index_surfaces_every_link_not_just_the_first(self):
        """A tool buried at link #3 of a thread must still get its own row."""
        records = [
            {
                "resource_id": "tweet:1",
                "status": "relevant",
                "pick_score": 22.5,
                "shared_at": "2026-08-28T10:00:00+00:00",
                "external_urls": [
                    "https://github.com/someone/unrelated",
                    "https://github.com/other/thing",
                    "https://github.com/microsoft/markitdown",
                ],
                "tool_keys": [
                    "github.com/someone/unrelated",
                    "github.com/other/thing",
                    "github.com/microsoft/markitdown",
                ],
                "verdict": None,
            }
        ]
        verdicts = {
            "github.com/microsoft/markitdown": {
                "key": "github.com/microsoft/markitdown",
                "name": "microsoft/markitdown",
                "verdict": "must_try",
                "rank": 1,
                "why": "document conversion",
                "first_step": "pip install",
            }
        }
        tools = gm.build_tool_index(records, verdicts)
        by_key = {t["key"]: t for t in tools}
        self.assertIn("github.com/microsoft/markitdown", by_key)
        self.assertEqual(by_key["github.com/microsoft/markitdown"]["verdict"], "must_try")
        # every link got a row, not only the first
        self.assertEqual(len(tools), 3)
        # must_try sorts ahead of unreviewed
        self.assertEqual(tools[0]["key"], "github.com/microsoft/markitdown")
        # and the verdict is stamped back onto the post
        self.assertEqual(records[0]["verdict"]["verdict"], "must_try")

    def test_tool_index_names_which_links_are_repos(self):
        """The dashboard must be able to say WHICH repo earned the repo bonus.

        The score's repo term and the dashboard's repo labelling must come from
        the same REPO_HOSTS set, so is_repo is stamped server-side per tool.
        """
        records = [
            {
                "resource_id": "tweet:9",
                "status": "relevant",
                "pick_score": 10.0,
                "shared_at": "2026-08-28T10:00:00+00:00",
                "external_urls": [
                    "https://github.com/microsoft/markitdown",
                    "https://example.com/blog/post",
                ],
                "tool_keys": [
                    "github.com/microsoft/markitdown",
                    "example.com/blog/post",
                ],
                "verdict": None,
            }
        ]
        tools = gm.build_tool_index(records, {})
        by_key = {t["key"]: t for t in tools}
        self.assertTrue(by_key["github.com/microsoft/markitdown"]["is_repo"])
        self.assertFalse(by_key["example.com/blog/post"]["is_repo"])
        # A verdict with no surviving mention still gets the flag, so the
        # Tools card can label it consistently.
        verdict_only = gm.build_tool_index(
            [],
            {
                "github.com/kacperkapusciak/goldie": {
                    "key": "github.com/kacperkapusciak/goldie",
                    "name": "kacperkapusciak/goldie",
                    "verdict": "must_try",
                }
            },
        )
        self.assertTrue(verdict_only[0]["is_repo"])

    def test_tool_index_matches_case_insensitively_and_ignores_markdown_junk(self):
        records = [
            {
                "resource_id": "tweet:2",
                "status": "relevant",
                "pick_score": 1.0,
                "external_urls": ["https://github.com/OBRA/Superpowers](https:"],
                "tool_keys": [rt.tool_key("https://github.com/OBRA/Superpowers](https:")],
                "verdict": None,
            }
        ]
        verdicts = {"github.com/obra/superpowers": {"key": "github.com/obra/superpowers", "verdict": "must_try", "name": "obra/superpowers"}}
        tools = gm.build_tool_index(records, verdicts)
        self.assertEqual(tools[0]["verdict"], "must_try")
        self.assertEqual(records[0]["verdict"]["verdict"], "must_try")

    def test_reviewed_tool_appears_even_when_nothing_links_it(self):
        """Verdicts for tools named only in text must still be visible."""
        verdicts = {
            "github.com/kacperkapusciak/goldie": {
                "key": "github.com/kacperkapusciak/goldie",
                "name": "kacperkapusciak/goldie",
                "verdict": "must_try",
                "rank": 4,
            }
        }
        tools = gm.build_tool_index([], verdicts)
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["mentions"], 0)
        self.assertEqual(tools[0]["verdict"], "must_try")

    def test_shipped_verdicts_all_reach_the_dashboard(self):
        """Every entry in config/verdicts.json must be representable."""
        verdicts = gm.load_verdicts()
        self.assertGreaterEqual(len(verdicts), 20)
        tools = gm.build_tool_index([], verdicts)
        self.assertEqual(len(tools), len(verdicts))
        kinds = {t["verdict"] for t in tools}
        self.assertEqual(kinds, {"must_try", "excluded", "already_have"})

    def test_hard_deadline_interrupts_a_blocked_stage(self):
        """A stage that never returns must still end the run, not hang forever."""
        disarm = loop.arm_hard_deadline(1)
        started = time.monotonic()
        try:
            with self.assertRaises(loop.StuckRun) as caught:
                time.sleep(30)  # stands in for a call that never returns
        finally:
            disarm()
        self.assertLess(time.monotonic() - started, 10)
        self.assertIn("hard deadline", str(caught.exception))

    def test_hard_deadline_releases_the_worker_lock(self):
        """After the guard fires, the next run can acquire the lock immediately."""
        gm.DATA_DIR.mkdir(parents=True, exist_ok=True)
        disarm = loop.arm_hard_deadline(1)
        try:
            with self.assertRaises(loop.StuckRun):
                with gm.exclusive_run_lock():
                    time.sleep(30)
        finally:
            disarm()
        # The lock is free again, so a following run is not refused.
        with gm.exclusive_run_lock():
            pass

    def test_hard_deadline_is_longer_than_the_soft_deadline(self):
        self.assertGreater(loop.HARD_DEADLINE_SECONDS, loop.MAX_DURATION_SECONDS)
        # and still inside the 30-minute cap the LoopSpec declares
        spec = json.loads((ROOT / "group-share-filter.loop.json").read_text(encoding="utf-8"))
        self.assertLessEqual(
            loop.HARD_DEADLINE_SECONDS, spec["limits"]["max_duration_minutes"] * 60
        )


class ResourceTypingTests(unittest.TestCase):
    def test_lanes_from_keywords_hosts_and_verbs(self):
        cases = [
            ("Open-source CLI tool on GitHub for agents", (), "try"),
            ("How to build AI agents step by step: a full course", (), "learn"),
            ("New paper on arXiv sets a benchmark", (), "read"),
            ("Funny football clip from last night", (), "other"),
            ("أداة جديدة للذكاء الاصطناعي", (), "try"),
            ("开源知识库项目", (), "try"),
            ("Watchtower monitors every Claude Code pane across tmux sessions", (), "try"),
            ("check this", ("https://github.com/a/b",), "try"),
            ("شرح كامل من الصفر دورة", (), "learn"),
        ]
        for text, urls, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(rt.classify_resource_type(text, urls)["type"], expected)

    def test_pick_score_prefers_reshared_project_fit_over_virality(self):
        now = rt.parse_iso("2026-08-29T20:00:00+00:00")
        meme = {
            "likes": 78576,
            "retweets": 5000,
            "share_count": 1,
            "sharer_count": 1,
            "project_areas": ["ai", "apps-frontend-release"],
            "resource_type": "other",
            "external_urls": [],
            "shared_at": now.isoformat(),
            "decision_source": "claude",
        }
        reshared_tool = {
            "likes": 285,
            "retweets": 30,
            "share_count": 2,
            "sharer_count": 2,
            "project_areas": ["ai", "ai-agent-systems", "hermes-communications"],
            "resource_type": "try",
            "external_urls": ["https://github.com/x/y"],
            "shared_at": now.isoformat(),
            "decision_source": "rules",
        }
        meme_score = rt.compute_pick_score(meme, now)
        tool_score = rt.compute_pick_score(reshared_tool, now)
        self.assertGreater(tool_score["score"], meme_score["score"])
        self.assertEqual(tool_score["parts"]["reshare"], 7.0)
        self.assertEqual(tool_score["parts"]["repo"], 3.0)
        self.assertLessEqual(meme_score["parts"]["engagement"], 10.5)

    def test_external_urls_and_labels_skip_x_links(self):
        text = "see https://x.com/a/status/1 and https://github.com/owner/repo/tree/main?x=1."
        self.assertEqual(rt.external_urls_from_text(text), ["https://github.com/owner/repo/tree/main?x=1"])
        self.assertEqual(rt.short_link_label("https://github.com/owner/repo/tree/main"), "github.com/owner/repo")
        self.assertEqual(rt.short_link_label("https://x.com/a/status/1"), "")


class AutoGateTests(unittest.TestCase):
    """Facts alone may disqualify a tool; opinions may not."""

    def test_archived_dead_tiny_and_gone_are_gated(self):
        cases = [
            ({"ok": True, "archived": True, "stars": 9000, "pushed_at": "2026-08-01"}, "archived"),
            ({"ok": True, "is_empty": True, "stars": 9000, "pushed_at": "2026-08-01"}, "empty"),
            ({"ok": True, "stars": 9000, "pushed_at": "2024-01-01"}, "stale"),
            ({"ok": True, "stars": 4, "pushed_at": "2026-08-01"}, "tiny"),
            ({"ok": False, "missing": True}, "gone"),
        ]
        for meta, expected in cases:
            with self.subTest(expected=expected):
                code, human = gm.auto_gate(meta)
                self.assertEqual(code, expected)
                self.assertTrue(human)

    def test_healthy_repo_is_left_for_a_human(self):
        code, human = gm.auto_gate(
            {"ok": True, "archived": False, "stars": 4698, "pushed_at": "2026-08-29"}
        )
        self.assertEqual(code, "")
        self.assertEqual(human, "")

    def test_unfetched_metadata_never_gates(self):
        self.assertEqual(gm.auto_gate({}), ("", ""))
        self.assertEqual(gm.auto_gate({"ok": False, "error": "timeout"}), ("", ""))

    def test_hand_written_verdict_beats_the_auto_gate(self):
        """A curated call must survive even if the repo looks unimpressive."""
        records = [{
            "resource_id": "tweet:1", "status": "relevant", "pick_score": 5.0,
            "external_urls": ["https://github.com/kacperkapusciak/goldie"],
            "tool_keys": ["github.com/kacperkapusciak/goldie"], "verdict": None,
        }]
        verdicts = {"github.com/kacperkapusciak/goldie": {
            "key": "github.com/kacperkapusciak/goldie", "name": "goldie", "verdict": "must_try", "rank": 4,
        }}
        with mock.patch.object(gm, "load_tool_meta", return_value={
            "github.com/kacperkapusciak/goldie": {"ok": True, "stars": 3, "pushed_at": "2026-08-28"}
        }):
            tools = gm.build_tool_index(records, verdicts)
        self.assertEqual(tools[0]["verdict"], "must_try")
        self.assertFalse(tools[0].get("auto"))


class EnrichmentTests(unittest.TestCase):
    def test_transient_failures_retry_but_missing_repos_keep_their_ttl(self):
        """A 401 spell parked 13 fresh repos for the 30-day TTL; that must not recur.

        An auth outage or timeout is not a fact about the repo, so it expires
        immediately. A confirmed-gone repo IS a fact and keeps its TTL.
        """
        now = enrich_tools.utc_now()
        recent = enrich_tools.iso(now)
        transient = {"ok": False, "missing": False, "error": "HTTP 401", "fetched_at": recent}
        gone = {"ok": False, "missing": True, "error": "missing", "fetched_at": recent}
        healthy = {"ok": True, "fetched_at": recent}
        self.assertTrue(enrich_tools.is_expired(transient, "candidate", now))
        self.assertFalse(enrich_tools.is_expired(gone, "candidate", now))
        self.assertFalse(enrich_tools.is_expired(healthy, "candidate", now))

    def test_gh_env_reads_token_sidecar_only_when_env_has_none(self):
        """cron cannot read the macOS keychain, so gh needs the sidecar there."""
        with tempfile.TemporaryDirectory() as tmp:
            sidecar = Path(tmp) / "gh-token"
            sidecar.write_text("sidecar-token\n", encoding="utf-8")
            original_path = enrich_tools.TOKEN_PATH
            enrich_tools.TOKEN_PATH = sidecar
            try:
                for variable in ("GH_TOKEN", "GITHUB_TOKEN"):
                    os.environ.pop(variable, None)
                self.assertEqual(enrich_tools._gh_env().get("GH_TOKEN"), "sidecar-token")
                os.environ["GH_TOKEN"] = "env-token"
                try:
                    self.assertEqual(enrich_tools._gh_env().get("GH_TOKEN"), "env-token")
                finally:
                    os.environ.pop("GH_TOKEN", None)
                enrich_tools.TOKEN_PATH = Path(tmp) / "does-not-exist"
                self.assertNotIn("GH_TOKEN", enrich_tools._gh_env())
            finally:
                enrich_tools.TOKEN_PATH = original_path

    def test_repo_slug_only_accepts_real_github_repos(self):
        self.assertEqual(enrich_tools.repo_slug("github.com/microsoft/markitdown"), "microsoft/markitdown")
        self.assertEqual(enrich_tools.repo_slug("github.com/microsoft"), "")
        self.assertEqual(enrich_tools.repo_slug("sonarsource.com/products/x"), "")
        self.assertEqual(enrich_tools.repo_slug("github.com/bad;name/repo"), "")

    def test_queue_refreshes_reviewed_tools_before_the_long_tail(self):
        """A stale 'must try' is actively misleading, so it is re-checked first."""
        tools = [
            {"key": "github.com/a/candidate", "verdict": "unreviewed", "best_score": 99.0},
            {"key": "github.com/b/reviewed", "verdict": "must_try", "best_score": 1.0},
            {"key": "github.com/c/rejected", "verdict": "excluded", "best_score": 50.0},
        ]
        order = enrich_tools.select_queue(tools, {}, limit=10)
        self.assertEqual(order[0], "b/reviewed")
        self.assertEqual(order[1], "a/candidate")
        self.assertEqual(order[2], "c/rejected")

    def test_fresh_cache_entries_are_not_refetched(self):
        now = enrich_tools.utc_now()
        cache = {"a/tool": {"fetched_at": enrich_tools.iso(now), "ok": True}}
        tools = [{"key": "github.com/a/tool", "verdict": "unreviewed", "best_score": 1.0}]
        self.assertEqual(enrich_tools.select_queue(tools, cache, limit=10, now=now), [])
        later = now + __import__("datetime").timedelta(days=40)
        self.assertEqual(enrich_tools.select_queue(tools, cache, limit=10, now=later), ["a/tool"])

    def test_a_failed_fetch_keeps_the_last_good_facts(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "meta.json"
            enrich_tools.save_cache({"a/tool": {"ok": True, "stars": 500, "fetched_at": "2020-01-01T00:00:00+00:00"}}, cache_path)
            tools = [{"key": "github.com/a/tool", "verdict": "unreviewed", "best_score": 1.0}]
            with mock.patch.object(enrich_tools, "fetch_repo", return_value={"slug": "a/tool", "ok": False, "error": "timeout", "fetched_at": "2026-08-30T00:00:00+00:00"}):
                enrich_tools.enrich(tools, limit=5, budget_seconds=10, cache_path=cache_path)
            kept = enrich_tools.load_cache(cache_path)["a/tool"]
            self.assertTrue(kept["ok"])
            self.assertEqual(kept["stars"], 500)
            self.assertEqual(kept["last_error"], "timeout")


class OutcomeTests(unittest.TestCase):
    """Outcomes are separate from verdicts so history survives a change of mind."""

    def test_endpoint_records_replaces_and_clears(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "outcomes.json"
            path.write_text(json.dumps({"version": 1, "outcomes": []}), encoding="utf-8")
            with mock.patch.object(radar_server, "OUTCOMES_PATH", path):
                code, body = radar_server.record_outcome(
                    {"key": "github.com/a/tool", "state": "kept", "note": "works"}
                )
                self.assertEqual((code, body["action"]), (200, "added"))
                code, body = radar_server.record_outcome({"key": "github.com/a/tool", "state": "dropped"})
                self.assertEqual(body["action"], "replaced")
                entries = json.loads(path.read_text(encoding="utf-8"))["outcomes"]
                self.assertEqual(len(entries), 1)
                self.assertEqual(entries[0]["state"], "dropped")
                code, body = radar_server.record_outcome({"key": "github.com/a/tool", "state": "clear"})
                self.assertEqual(body["action"], "cleared")
                self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["outcomes"], [])

    def test_endpoint_rejects_bad_state_without_touching_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "outcomes.json"
            original = json.dumps({"version": 1, "outcomes": [{"key": "github.com/keep/me", "state": "kept"}]})
            path.write_text(original, encoding="utf-8")
            with mock.patch.object(radar_server, "OUTCOMES_PATH", path):
                for bad in ({"key": "nope", "state": "kept"}, {"key": "github.com/a/b", "state": "invented"}, {}):
                    code, _ = radar_server.record_outcome(bad)
                    self.assertEqual(code, 400, bad)
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_outcome_survives_a_verdict_change(self):
        """The whole reason outcomes live in their own file."""
        with tempfile.TemporaryDirectory() as tmp:
            verdicts = Path(tmp) / "verdicts.json"
            outcomes = Path(tmp) / "outcomes.json"
            verdicts.write_text(json.dumps({"version": 1, "verdicts": []}), encoding="utf-8")
            outcomes.write_text(json.dumps({"version": 1, "outcomes": []}), encoding="utf-8")
            with mock.patch.object(radar_server, "VERDICTS_PATH", verdicts), mock.patch.object(
                radar_server, "OUTCOMES_PATH", outcomes
            ):
                radar_server.record_verdict({"key": "github.com/a/tool", "verdict": "must_try"})
                radar_server.record_outcome({"key": "github.com/a/tool", "state": "kept"})
                radar_server.record_verdict({"key": "github.com/a/tool", "verdict": "clear"})
            self.assertEqual(json.loads(verdicts.read_text(encoding="utf-8"))["verdicts"], [])
            kept = json.loads(outcomes.read_text(encoding="utf-8"))["outcomes"]
            self.assertEqual(len(kept), 1)
            self.assertEqual(kept[0]["state"], "kept")


class NegativeRuleTests(unittest.TestCase):
    def test_log_odds_beats_raw_frequency(self):
        """A word common in both piles carries no signal and must not be proposed."""
        excluded = [{"fitness", "ai"} for _ in range(9)]
        relevant = [{"ai", "agent"} for _ in range(9)]
        proposals = learn_negatives.propose(excluded, relevant, protected=set())
        terms = [p["term"] for p in proposals]
        self.assertIn("fitness", terms)
        self.assertNotIn("ai", terms)

    def test_support_and_contamination_thresholds(self):
        thin = [{"rare"} for _ in range(learn_negatives.MIN_EXCLUDED_HITS - 1)]
        self.assertEqual(learn_negatives.propose(thin, [], set()), [])
        contaminated = [{"shared"} for _ in range(20)]
        keeps = [{"shared"} for _ in range(5)]
        self.assertEqual(
            [p["term"] for p in learn_negatives.propose(contaminated, keeps, set())], []
        )

    def test_profile_terms_are_never_proposed(self):
        excluded = [{"whatsapp"} for _ in range(9)]
        proposals = learn_negatives.propose(excluded, [], protected={"whatsapp"})
        self.assertEqual(proposals, [])

    def test_arabic_normalization_merges_spellings(self):
        self.assertEqual(learn_negatives.normalize_arabic("جدًا"), learn_negatives.normalize_arabic("جدا"))
        self.assertNotIn("جدا", learn_negatives.tokenize("هذا جدًا مفيد"))

    def test_gate_stands_down_when_a_positive_rule_matched(self):
        """A learned negative may only act where nothing vouched for the item."""
        profile = {"selection": {"negative_terms": ["fitness"]}}

        class Row(dict):
            def __getitem__(self, key):
                return dict.get(self, key, "")

        matched = Row(title="fitness tracker", content_text="", source_text="", canonical_url="")
        code, _ = gm.negative_gate(matched, profile)
        self.assertEqual(code, "negative-rule")
        # and with no approved terms at all it can never fire
        self.assertEqual(gm.negative_gate(matched, {"selection": {}}), ("", ""))

    def test_endpoint_refuses_to_shadow_a_positive_term(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "profile.json"
            path.write_text(json.dumps({
                "selection": {"ai_terms": ["ai"], "project_areas": {"x": {"keywords": ["whatsapp"]}}}
            }), encoding="utf-8")
            with mock.patch.object(radar_server, "PROFILE_PATH", path):
                code, body = radar_server.record_negative_term({"term": "whatsapp"})
                self.assertEqual(code, 409)
                self.assertIn("positive term", body["error"])
                for bad in ({"term": "ab"}, {"term": "two words"}, {}):
                    self.assertEqual(radar_server.record_negative_term(bad)[0], 400, bad)
                code, body = radar_server.record_negative_term({"term": "fitness"})
                self.assertEqual((code, body["action"]), (200, "added"))
                stored = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(stored["selection"]["negative_terms"], ["fitness"])


class BookmarkIngestTests(unittest.TestCase):
    def test_normalize_tweet_matches_the_dm_attachment_shape(self):
        payload = ingest_bookmarks.normalize_tweet({
            "id": "123", "text": "An open-source CLI",
            "author": {"username": "dev", "name": "Dev"},
            "likeCount": 10, "media": [{"type": "photo", "url": "https://pbs.twimg.com/x.jpg"}],
        })
        self.assertEqual(payload["id"], "123")
        self.assertEqual(payload["author"]["username"], "dev")
        self.assertEqual(payload["media"][0]["url"], "https://pbs.twimg.com/x.jpg")
        self.assertIsNone(ingest_bookmarks.normalize_tweet({"id": "not-a-number"}))

    def test_archive_rows_terminate_instead_of_flooding_the_reviewer(self):
        """20,000 unmatched archive rows must not become 20,000 model calls."""
        with tempfile.TemporaryDirectory() as tmp:
            old_dir = gm.DATA_DIR
            gm.DATA_DIR = Path(tmp) / "out"
            conn = gm.connect_db(Path(tmp) / "db.sqlite3")
            try:
                profile = {
                    "conversation": {"id": "g", "auth_account": "o"},
                    "owners": [{"username": "o", "sender_id": "42"}],
                    "bootstrap": {"resume_after_message_id": "1", "evidence": "t"},
                    "selection": {"minimum_score": 3, "ai_weight": 4, "ai_terms": ["ai"], "project_areas": {}},
                }
                tweets = [
                    {"id": "900", "text": "a lovely sunset", "author": {"username": "a"}},
                    {"id": "901", "text": "an ai agent toolkit", "author": {"username": "b"}},
                ]
                result = ingest_bookmarks.ingest(
                    conn, profile, tweets, "bookmark-archive",
                    terminal_on_rule_miss=True, progress_every=0,
                )
                self.assertEqual(result["inserted"], 2)
                self.assertEqual(result["relevant_by_rules"], 1)
                self.assertEqual(result["terminated_by_archive_policy"], 1)
                left = conn.execute(
                    "SELECT COUNT(*) FROM resources WHERE status='pending_review'"
                ).fetchone()[0]
                self.assertEqual(left, 0, "archive rows must never wait for the reviewer")
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM resources WHERE source='bookmark-archive'").fetchone()[0], 2
                )
            finally:
                conn.close()
                gm.DATA_DIR = old_dir

    def test_ingest_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_dir = gm.DATA_DIR
            gm.DATA_DIR = Path(tmp) / "out"
            conn = gm.connect_db(Path(tmp) / "db.sqlite3")
            try:
                profile = {
                    "conversation": {"id": "g", "auth_account": "o"},
                    "owners": [{"username": "o", "sender_id": "42"}],
                    "bootstrap": {"resume_after_message_id": "1", "evidence": "t"},
                    "selection": {"minimum_score": 3, "ai_weight": 4, "ai_terms": ["ai"], "project_areas": {}},
                }
                tweets = [{"id": "900", "text": "an ai toolkit", "author": {"username": "a"}}]
                first = ingest_bookmarks.ingest(conn, profile, tweets, "bookmark", False, progress_every=0)
                second = ingest_bookmarks.ingest(conn, profile, tweets, "bookmark", False, progress_every=0)
                self.assertEqual(first["inserted"], 1)
                self.assertEqual(second["inserted"], 0)
                self.assertEqual(second["already_known"], 1)
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM resources").fetchone()[0], 1
                )
            finally:
                conn.close()
                gm.DATA_DIR = old_dir

    def test_payload_caps_imported_sources_but_never_the_group(self):
        records = [{"resource_id": "g%d" % i, "source": "group", "status": "irrelevant"} for i in range(50)]
        records += [
            {"resource_id": "a%d" % i, "source": "bookmark-archive", "status": "relevant", "pick_score": i}
            for i in range(gm.DASHBOARD_BOOKMARK_CAP + 500)
        ]
        records += [{"resource_id": "x", "source": "bookmark-archive", "status": "irrelevant"}]
        payload = gm.select_payload_records(records)
        sources = [r["source"] for r in payload]
        self.assertEqual(sources.count("group"), 50, "every group row must travel")
        self.assertEqual(sources.count("bookmark-archive"), gm.DASHBOARD_BOOKMARK_CAP)
        self.assertNotIn("x", [r["resource_id"] for r in payload])


class TelegramDecisionTests(unittest.TestCase):
    def test_callback_payload_fits_telegram_limit_for_the_longest_real_key(self):
        longest = max(
            (v["key"] for v in json.loads(
                (ROOT / "config" / "verdicts.json").read_text(encoding="utf-8"))["verdicts"]),
            key=len,
        )
        payload = telegram_decisions.callback_data(longest, "y")
        self.assertLessEqual(len(payload.encode("utf-8")), telegram_decisions.CALLBACK_LIMIT)
        self.assertTrue(payload.startswith("rdr:"))

    def test_short_id_is_stable_and_distinct(self):
        a = telegram_decisions.short_id("github.com/a/one")
        self.assertEqual(a, telegram_decisions.short_id("github.com/a/one"))
        self.assertNotEqual(a, telegram_decisions.short_id("github.com/a/two"))
        self.assertEqual(len(a), telegram_decisions.ID_LENGTH)

    def test_unknown_id_is_ignored_rather_than_applied(self):
        with mock.patch.object(telegram_decisions, "PENDING_PATH", Path("/nonexistent/pending.json")):
            result = telegram_decisions.apply_decisions([{"id": "deadbeef", "action": "y"}])
        self.assertEqual(result, {"applied": 0, "unknown": 1, "rejected": 0})

    def test_keyboard_offers_both_verdicts(self):
        keyboard = telegram_decisions.build_keyboard("github.com/a/tool", "https://github.com/a/tool")
        buttons = keyboard["inline_keyboard"][0]
        self.assertEqual(len(buttons), 3)
        self.assertTrue(buttons[0]["callback_data"].endswith(":y"))
        self.assertTrue(buttons[1]["callback_data"].endswith(":n"))
        self.assertEqual(buttons[2]["url"], "https://github.com/a/tool")


class ArchitectureDocTests(unittest.TestCase):
    """The document is generated, so it must never drift from the code."""

    def test_document_is_current(self):
        self.assertEqual(
            generate_architecture.main(["--check"]),
            0,
            "ARCHITECTURE.md is stale — run python3 scripts/generate_architecture.py",
        )

    def test_document_reports_the_real_entry_points_and_modules(self):
        document = generate_architecture.build_document()
        for expected in (
            "scripts/group_filter_loop.py",
            "scripts/radar_server.py",
            "scripts/resource_typing.py",
            "group-monitor.sqlite3",
            "/api/verdict",
        ):
            self.assertIn(expected, document, expected)

    def test_document_never_leaks_credentials(self):
        document = generate_architecture.build_document()
        for secret in ("auth_token", "ct0", "TELEGRAM_BOT_TOKEN", "accounts.json\":"):
            self.assertNotIn(secret, document.replace("`data/accounts.json`", ""))

    def test_live_and_legacy_modules_are_separated(self):
        graph = generate_architecture.dependency_graph()
        live = generate_architecture.reachable_from(
            [name for name, _k, _w in generate_architecture.ENTRY_POINTS], graph
        )
        # the pipeline and the viewer are live; the old digest CLI is not
        self.assertIn("group_monitor.py", live)
        self.assertIn("radar_server.py", live)
        self.assertIn("enrich_tools.py", live)
        self.assertNotIn("dashboard.py", live)

    def test_check_detects_a_stale_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ARCHITECTURE.md"
            path.write_text("# stale\n", encoding="utf-8")
            self.assertEqual(generate_architecture.main(["--check", "--out", str(path)]), 1)


class RadarServerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.data = Path(self.tempdir.name)
        (self.data / "dashboard.html").write_text("<!doctype html><title>t</title>", encoding="utf-8")
        (self.data / "status.json").write_text(
            json.dumps({"updated_at": gm.utc_now(), "gate_ready": True, "resources": 1, "status_counts": {}}),
            encoding="utf-8",
        )
        (self.data / "group-monitor.sqlite3").write_bytes(b"secret")
        self.old_dir = radar_server.RadarHandler.data_dir
        radar_server.RadarHandler.data_dir = self.data
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), radar_server.RadarHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:{}".format(self.server.server_address[1])

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        radar_server.RadarHandler.data_dir = self.old_dir
        self.tempdir.cleanup()

    def request(self, path, method="GET", headers=None):
        request = urllib.request.Request(self.base + path, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read()

    def test_serves_dashboard_with_hardening_headers(self):
        code, headers, body = self.request("/")
        self.assertEqual(code, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn("no-store", headers["Cache-Control"])
        self.assertIn("connect-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn(b"<title>t</title>", body)
        etag = headers["ETag"]
        code, _headers, body = self.request("/", headers={"If-None-Match": etag})
        self.assertEqual(code, 304)
        code, _headers, body = self.request("/", method="HEAD")
        self.assertEqual(code, 200)
        self.assertEqual(body, b"")

    def test_whitelist_blocks_everything_else(self):
        self.assertEqual(self.request("/group-monitor.sqlite3")[0], 404)
        self.assertEqual(self.request("/../etc/passwd")[0], 404)
        self.assertEqual(self.request("/data/group-monitor/status.json")[0], 404)
        self.assertEqual(self.request("/status.json", method="POST")[0], 405)
        self.assertEqual(self.request("/dashboard-data.json")[0], 404)

    def test_health_document(self):
        code, _headers, body = self.request("/api/health")
        self.assertEqual(code, 200)
        health = json.loads(body.decode("utf-8"))
        self.assertEqual(health["service"], "group-radar")
        self.assertTrue(health["ok"])
        self.assertFalse(health["stale"])
        self.assertIn("next_run_at", health)
        self.assertEqual(health["cron_minutes"], [17, 47])

    def test_symlink_escape_is_rejected(self):
        outside = Path(self.tempdir.name).parent / "radar-outside-{}".format(self.server.server_address[1])
        outside.write_text("nope", encoding="utf-8")
        try:
            (self.data / "latest.md").symlink_to(outside)
            self.assertEqual(self.request("/latest.md")[0], 404)
        finally:
            outside.unlink()

    def test_refuses_non_loopback_host(self):
        self.assertEqual(radar_server.serve("0.0.0.0", 0, self.data), 2)

    def test_verdict_endpoint_records_replaces_and_clears(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "verdicts.json"
            path.write_text(json.dumps({"version": 1, "verdicts": []}), encoding="utf-8")
            with mock.patch.object(radar_server, "VERDICTS_PATH", path):
                code, body = self.request_verdict({"key": "github.com/a/tool", "name": "a/tool", "verdict": "must_try"})
                self.assertEqual(code, 200)
                self.assertEqual(body["action"], "added")
                entries = json.loads(path.read_text(encoding="utf-8"))["verdicts"]
                self.assertEqual(entries[0]["rank"], 1)
                self.assertEqual(entries[0]["decided_by"], "dashboard")

                # a second must_try gets the next rank, not a duplicate
                self.request_verdict({"key": "github.com/b/tool", "verdict": "must_try"})
                entries = json.loads(path.read_text(encoding="utf-8"))["verdicts"]
                self.assertEqual([e["rank"] for e in entries], [1, 2])

                # changing your mind replaces rather than duplicates
                code, body = self.request_verdict({"key": "github.com/a/tool", "verdict": "excluded"})
                self.assertEqual(body["action"], "replaced")
                entries = json.loads(path.read_text(encoding="utf-8"))["verdicts"]
                self.assertEqual(len(entries), 2)
                self.assertEqual([e["verdict"] for e in entries if e["key"] == "github.com/a/tool"], ["excluded"])

                # and it can be undone entirely
                code, body = self.request_verdict({"key": "github.com/a/tool", "verdict": "clear"})
                self.assertEqual(body["action"], "cleared")
                self.assertEqual(len(json.loads(path.read_text(encoding="utf-8"))["verdicts"]), 1)

    def test_verdict_endpoint_rejects_bad_input_without_touching_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "verdicts.json"
            original = json.dumps({"version": 1, "verdicts": [{"key": "github.com/keep/me", "verdict": "must_try"}]})
            path.write_text(original, encoding="utf-8")
            with mock.patch.object(radar_server, "VERDICTS_PATH", path):
                for bad in ({"key": "nope", "verdict": "must_try"}, {"key": "github.com/a/b", "verdict": "delete_everything"}, {}):
                    code, _body = self.request_verdict(bad)
                    self.assertEqual(code, 400, bad)
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_verdict_endpoint_requires_its_header(self):
        code, _headers, _body = self.request("/api/verdict", method="POST", headers={"Content-Type": "application/json"})
        self.assertEqual(code, 400)
        self.assertEqual(self.request("/api/verdict")[0], 405)

    def request_verdict(self, payload):
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.base + "/api/verdict", data=body, method="POST",
            headers={"X-Radar-Action": "verdict", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def test_scan_now_trigger_is_guarded(self):
        fake_loop = self.data / "fake_loop.py"
        fake_loop.write_text("print('fake scan')\n", encoding="utf-8")
        with mock.patch.object(radar_server, "LOOP_SCRIPT", fake_loop), mock.patch.object(
            radar_server, "_last_run_started_at", 0.0
        ):
            self.assertEqual(self.request("/api/run")[0], 405)
            self.assertEqual(self.request("/api/run", method="POST")[0], 400)
            code, _headers, body = self.request("/api/run", method="POST", headers={"X-Radar-Action": "run"})
            self.assertEqual(code, 202)
            self.assertTrue(json.loads(body.decode("utf-8"))["started"])
            code, _headers, body = self.request("/api/run", method="POST", headers={"X-Radar-Action": "run"})
            self.assertEqual(code, 429)
            self.assertIn("wait", json.loads(body.decode("utf-8"))["reason"])
        radar_server._last_run_started_at = 0.0
        self.assertEqual(self.request("/api/other", method="POST", headers={"X-Radar-Action": "run"})[0], 405)


class ManageRadarServerTests(unittest.TestCase):
    def test_plist_document_is_keepalive_loopback_server(self):
        document = manage_radar_server.plist_document()
        self.assertEqual(document["Label"], "com.mshrmnsr.group-radar-server")
        self.assertEqual(document["ProgramArguments"][1], str(manage_radar_server.SERVER))
        self.assertEqual(document["ProgramArguments"][-2:], ["--port", "8765"])
        self.assertEqual(document["KeepAlive"], {"SuccessfulExit": False})
        self.assertTrue(document["RunAtLoad"])
        self.assertEqual(document["ThrottleInterval"], 30)


if __name__ == "__main__":
    unittest.main()


class ScoringV2Tests(unittest.TestCase):
    """The v1 weights said one thing and the ledger did another. These pin the fix."""

    def _base(self, **kw):
        now = rt.parse_iso("2026-08-31T12:00:00+00:00")
        base = {
            "likes": 0, "retweets": 0, "share_count": 1, "sharer_count": 1,
            "project_areas": [], "resource_type": "other", "external_urls": [],
            "shared_at": now.isoformat(),
        }
        base.update(kw)
        return base, now

    def test_engagement_can_break_a_tie_but_never_create_one(self):
        viral, now = self._base(likes=78576, retweets=5000, project_areas=["ai"])
        fitted, _ = self._base(
            project_areas=["ai", "ai-agent-systems", "hermes-communications"],
            resource_type="try", external_urls=["https://github.com/x/y"],
        )
        self.assertGreater(
            rt.compute_pick_score(fitted, now)["score"],
            rt.compute_pick_score(viral, now)["score"],
            "project fit must beat raw virality",
        )
        self.assertLessEqual(
            rt.compute_pick_score(viral, now)["parts"]["engagement"], rt.ENGAGEMENT_CAP
        )

    def test_an_excluded_verdict_sinks_the_score(self):
        record, now = self._base(
            likes=4996, project_areas=["ai", "apps-frontend-release"], resource_type="try"
        )
        plain = rt.compute_pick_score(record, now)["score"]
        excluded = rt.compute_pick_score(record, now, verdict="excluded")["score"]
        self.assertLess(excluded, plain - 10, "a hand exclusion must dominate the arithmetic")

    def test_repo_health_rewards_alive_and_punishes_archived(self):
        record, now = self._base(project_areas=["ai"], resource_type="try")
        alive = rt.compute_pick_score(record, now, facts={"ok": True, "stars": 24550, "pushed_at": "2026-08-18"})
        archived = rt.compute_pick_score(record, now, facts={"ok": True, "stars": 24550, "archived": True, "pushed_at": "2026-08-18"})
        stale = rt.compute_pick_score(record, now, facts={"ok": True, "stars": 24550, "pushed_at": "2024-01-01"})
        self.assertGreater(alive["parts"]["health"], 0)
        self.assertLess(archived["parts"]["health"], alive["parts"]["health"])
        self.assertLess(stale["parts"]["health"], alive["parts"]["health"])

    def test_reading_material_is_no_longer_buried(self):
        reading, now = self._base(project_areas=["ai", "ai-agent-systems"], resource_type="read")
        noise, _ = self._base(likes=50000, project_areas=[], resource_type="other")
        self.assertGreater(
            rt.compute_pick_score(reading, now)["score"],
            rt.compute_pick_score(noise, now)["score"],
            "research is what the user asked to keep up with; it must beat unfitted noise",
        )
        self.assertGreaterEqual(rt.TYPE_BONUS["read"], 2.0)

    def test_a_roundup_does_not_inherit_a_verdict_from_one_of_its_links(self):
        """A 43-link listicle is not 'must try' because one link is."""
        verdicts = {"github.com/obra/superpowers": {
            "key": "github.com/obra/superpowers", "name": "obra/superpowers", "verdict": "must_try", "rank": 1}}
        roundup = {
            "resource_id": "tweet:1", "status": "relevant", "pick_score": 1.0,
            "external_urls": ["https://github.com/o/r%d" % i for i in range(10)],
            "tool_keys": ["github.com/obra/superpowers"] + ["github.com/o/r%d" % i for i in range(9)],
            "verdict": None,
        }
        focused = {
            "resource_id": "tweet:2", "status": "relevant", "pick_score": 1.0,
            "external_urls": ["https://github.com/obra/superpowers"],
            "tool_keys": ["github.com/obra/superpowers"], "verdict": None,
        }
        with mock.patch.object(gm, "load_tool_meta", return_value={}):
            gm.build_tool_index([roundup, focused], verdicts)
        self.assertIsNone(roundup["verdict"], "roundup must not absorb the verdict")
        self.assertEqual(focused["verdict"]["verdict"], "must_try")

    def test_must_read_is_a_first_class_verdict(self):
        self.assertIn("must_read", radar_server.ALLOWED_VERDICTS)
        self.assertIn("must_read", gm.VERDICT_STRENGTH)
        self.assertGreater(rt.VERDICT_BONUS["must_read"], 0)


class TaxonomySeparationTests(unittest.TestCase):
    """A thing to try and a thing to read are different commitments.

    The old lanes mixed them: `practice` held techniques you apply, news you
    read, and courses you follow, so the same lane was reported as "reading"
    when most of it was not. These pin the separation.
    """

    def test_each_lane_is_assigned_by_the_action_it_demands(self):
        cases = [
            ("Stop asking AI to summarise. Use this prompt instead", (), "try"),
            ("An open-source CLI that converts PDFs", ("https://github.com/microsoft/markitdown",), "try"),
            ("Today in Nature Medicine we report AI predicts 130 diseases", (), "read"),
            ("Stanford just explained why the next AI startup won't train models", (), "read"),
            ("بشكل مجاني تعلم كيف تسوي AI Agent من الصفر", (), "learn"),
            ("Full course playlist", ("https://youtube.com/playlist?list=PLX",), "learn"),
            ("awesome-claude-code: a curated list of resources", ("https://github.com/a/awesome",), "reference"),
        ]
        for text, urls, expected in cases:
            with self.subTest(text=text[:40]):
                self.assertEqual(rt.classify_resource_type(text, urls)["type"], expected)

    def test_a_verdict_may_not_cross_the_boundary(self):
        self.assertFalse(rt.verdict_fits_type("must_try", "read"), "cannot 'try' an article")
        self.assertFalse(rt.verdict_fits_type("must_try", "learn"), "cannot 'try' a course")
        self.assertFalse(rt.verdict_fits_type("must_read", "try"), "do not 'read' a CLI")
        self.assertTrue(rt.verdict_fits_type("must_read", "read"))
        self.assertTrue(rt.verdict_fits_type("must_try", "try"))
        # excluding anything is always legitimate
        for lane in rt.RESOURCE_TYPES:
            self.assertIn("excluded", rt.VERDICT_FOR_TYPE[lane])

    def test_endpoint_refuses_a_mismatched_verdict_and_leaves_the_file_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "verdicts.json"
            original = json.dumps({"version": 1, "verdicts": []})
            path.write_text(original, encoding="utf-8")
            with mock.patch.object(radar_server, "VERDICTS_PATH", path):
                code, body = radar_server.record_verdict(
                    {"key": "github.com/a/paper", "verdict": "must_try", "resource_type": "read"}
                )
                self.assertEqual(code, 409)
                self.assertIn("does not fit", body["error"])
                self.assertEqual(path.read_text(encoding="utf-8"), original)
                code, _ = radar_server.record_verdict(
                    {"key": "github.com/a/paper", "verdict": "must_read", "resource_type": "read"}
                )
                self.assertEqual(code, 200)

    def test_every_lane_has_a_distinct_label_and_expected_action(self):
        for lane in rt.RESOURCE_TYPES:
            self.assertIn(lane, rt.TYPE_LABELS)
            self.assertIn(lane, rt.TYPE_VERBS)
        self.assertEqual(len(set(rt.TYPE_LABELS.values())), len(rt.RESOURCE_TYPES))

    def test_trying_outranks_reading_which_outranks_reference(self):
        self.assertGreater(rt.TYPE_BONUS["try"], rt.TYPE_BONUS["learn"])
        self.assertGreater(rt.TYPE_BONUS["learn"], rt.TYPE_BONUS["read"])
        self.assertGreater(rt.TYPE_BONUS["read"], rt.TYPE_BONUS["reference"])
