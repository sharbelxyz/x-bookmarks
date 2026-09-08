"""Lane 01 content-extraction tests — bounded, deny-by-default fetching
through the C6 safe-fetch contract (audit A04 content half).

No test opens a socket: the default provider is the frozen deny-all stub and
every other scenario uses deterministic C6-shaped fakes. Temporary databases
only.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
def _find_run_root(start):
    # Integration-tree repair by Chat 07 (2026-09-07): lanes sit at
    # <run>/workers/NN but integration at <run>/integration, so a fixed
    # parents[] hop cannot serve both. Walk up to the contracts dir instead
    # (same pattern as test_lane04_provider_contract).
    for parent in [start] + list(start.parents):
        if (parent / "contracts" / "fixtures").is_dir():
            return parent
    return start.parents[3]

RUN = _find_run_root(ROOT)
sys.path.insert(0, str(ROOT / "scripts"))

import content_extraction as cx  # noqa: E402
import group_monitor as gm  # noqa: E402

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


def c6_result(**overrides):
    base = {
        "ok": False,
        "url": "",
        "final_url": None,
        "status": None,
        "content_type": None,
        "bytes": 0,
        "body_path": None,
        "text": None,
        "error": None,
        "denied_reason": None,
    }
    base.update(overrides)
    return base


class FakeFetcher:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        answer = dict(self.result)
        answer["url"] = url
        return answer


def link_message(message_id, url, text=""):
    return {
        "id": message_id,
        "time": int(message_id),
        "sender_id": "77",
        "text": (url + " " + text).strip(),
        "urls": [url],
    }


def media_message(message_id, url="https://pbs.twimg.com/media/synthetic.jpg"):
    return {
        "id": message_id,
        "time": int(message_id),
        "sender_id": "77",
        "text": "",
        "urls": [],
        "attachment": {"photo": {"url": url}},
    }


def one_batch(message):
    return gm.FetchResult(
        messages=[message],
        pages=1,
        reached_checkpoint=True,
        newest_message_id=message["id"],
        oldest_message_id=message["id"],
    )


class ExtractionCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.object(gm, "DATA_DIR", Path(self._tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.conn = gm.connect_db(Path(self._tmp.name) / "lane01x.sqlite3")
        self.addCleanup(self.conn.close)

    def url_row(self, message_id="120", url="https://example.com/tool"):
        gm.persist_fetch(self.conn, PROFILE, one_batch(link_message(message_id, url)), "100")
        return self.conn.execute(
            "SELECT resource_id FROM resources WHERE kind = 'url'"
        ).fetchone()[0]

    def media_row(self, message_id="121", **kwargs):
        gm.persist_fetch(self.conn, PROFILE, one_batch(media_message(message_id, **kwargs)), "100")
        return "media:" + message_id

    def state_of(self, resource_id):
        return self.conn.execute(
            "SELECT extraction_state, extraction_detail, extraction_checked_at, "
            "title, content_text, status FROM resources WHERE resource_id = ?",
            (resource_id,),
        ).fetchone()



def _contract_stub_fetch():
    """Load the frozen contract deny-all stub straight from the fixtures dir
    (integration adaptation: the lane-local copy is retired once the real C6
    provider is integrated, but these tests still pin the frozen
    provider-unavailable semantics)."""
    import importlib.util
    path = RUN / "contracts" / "fixtures" / "safe_fetch_stub.py"
    spec = importlib.util.spec_from_file_location("contract_safe_fetch_stub", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.safe_fetch


class StubConformanceTests(ExtractionCase):
    def test_lane_stub_copy_is_byte_identical_to_the_contract_fixture(self):
        # Integration adaptation (Chat 07, 2026-09-07): with lane 05's real
        # provider integrated, the lane-local stub copy is retired by design
        # (lane 01's own integration gate). The identity check only applies
        # while the dev shim exists.
        lane_stub = ROOT / "scripts" / "safe_fetch_stub.py"
        if not lane_stub.is_file() and (ROOT / "scripts" / "safe_fetch.py").is_file():
            self.skipTest("dev stub retired: real C6 provider integrated")
        lane_copy = lane_stub.read_bytes()
        fixture = (RUN / "contracts" / "fixtures" / "safe_fetch_stub.py").read_bytes()
        self.assertEqual(lane_copy, fixture,
                         "the deny-all stub must never drift from the frozen contract copy")

    def test_default_provider_denies_and_rows_stay_honestly_pending(self):
        # Integration adaptation: the ambient default is now the real
        # provider; the frozen provider-unavailable semantics are pinned by
        # passing the contract stub explicitly.
        resource_id = self.url_row()
        summary = cx.extract_pending_content(self.conn, fetcher=_contract_stub_fetch())
        self.assertEqual(summary["pending"], 1)
        row = self.state_of(resource_id)
        self.assertEqual(row["extraction_state"], "pending",
                         "provider_unavailable is not a content answer")
        self.assertIn("provider unavailable", row["extraction_detail"])
        self.assertIsNotNone(row["extraction_checked_at"])


class UrlExtractionTests(ExtractionCase):
    def test_html_ok_extracts_and_flows_through_the_relevance_boundary(self):
        resource_id = self.url_row()
        fetcher = FakeFetcher(c6_result(
            ok=True,
            final_url="https://example.com/tool",
            status=200,
            content_type="text/html; charset=utf-8",
            bytes=2048,
            text="<html><head><title>Agent Toolkit</title>"
                 "<script>ignore()</script></head>"
                 "<body><p>An AI agent toolkit for automation.</p></body></html>",
        ))
        summary = cx.extract_pending_content(self.conn, fetcher=fetcher)
        self.assertEqual(summary["ok"], 1)
        row = self.state_of(resource_id)
        self.assertEqual(row["extraction_state"], "ok")
        self.assertEqual(row["title"], "Agent Toolkit")
        self.assertIn("AI agent toolkit", row["content_text"])
        self.assertNotIn("ignore()", row["content_text"], "script text is not content")
        rules = gm.apply_rule_classification(self.conn, PROFILE)
        self.assertEqual(rules["relevant_by_rules"], 1,
                         "extracted content feeds the existing rule boundary")
        self.assertEqual(self.state_of(resource_id)["status"], "relevant")

    def test_budgets_and_allowlist_are_passed_to_the_provider(self):
        self.url_row()
        fetcher = FakeFetcher(c6_result(denied_reason="provider_unavailable"))
        cx.extract_pending_content(self.conn, fetcher=fetcher)
        _, kwargs = fetcher.calls[0]
        self.assertEqual(kwargs["max_bytes"], cx.MAX_FETCH_BYTES)
        self.assertEqual(kwargs["timeout"], cx.FETCH_TIMEOUT)
        self.assertEqual(kwargs["max_redirects"], cx.MAX_REDIRECTS)
        self.assertIn("text/html", kwargs["allowed_content_types"])

    def test_pdf_document_is_unsupported_with_byte_evidence(self):
        resource_id = self.url_row(url="https://example.com/paper.pdf")
        fetcher = FakeFetcher(c6_result(
            ok=True, final_url="https://example.com/paper.pdf", status=200,
            content_type="application/pdf", bytes=90000,
        ))
        cx.extract_pending_content(self.conn, fetcher=fetcher)
        row = self.state_of(resource_id)
        self.assertEqual(row["extraction_state"], "unsupported")
        self.assertIn("90000 bytes verified", row["extraction_detail"])
        self.assertIn("no approved local extractor", row["extraction_detail"])

    def test_budget_and_policy_denials_map_to_explicit_states(self):
        cases = {
            "too_large": ("failed", "byte budget"),
            "timeout": ("failed", "time budget"),
            "private_target": ("failed", "denied: private_target"),
            "redirect_target": ("failed", "denied: redirect_target"),
            "content_type": ("unsupported", "not accepted"),
        }
        for index, (reason, (expected_state, needle)) in enumerate(sorted(cases.items())):
            with self.subTest(denied_reason=reason):
                message_id = str(130 + index)
                gm.persist_fetch(
                    self.conn, PROFILE,
                    one_batch(link_message(message_id,
                                           "https://example.com/{}".format(reason))),
                    "100",
                )
                resource_id = self.conn.execute(
                    "SELECT resource_id FROM message_resources WHERE message_id = ?",
                    (message_id,),
                ).fetchone()[0]
                fetcher = FakeFetcher(c6_result(denied_reason=reason))
                cx.extract_pending_content(self.conn, fetcher=fetcher)
                row = self.state_of(resource_id)
                self.assertEqual(row["extraction_state"], expected_state)
                self.assertIn(needle, row["extraction_detail"])

    def test_unsafe_scheme_row_never_reaches_the_provider(self):
        now = gm.utc_now()
        self.conn.execute(
            "INSERT INTO resources(resource_id, kind, canonical_url, "
            "first_message_id, last_message_id, sender_id, source_text, status, "
            "first_seen_at, updated_at, extraction_state) "
            "VALUES('url:deadbeef', 'url', 'file:///etc/passwd', '150', '150', "
            "'77', 'x', 'pending_review', ?, ?, 'pending')",
            (now, now),
        )
        self.conn.commit()
        fetcher = FakeFetcher(c6_result(ok=True))
        cx.extract_pending_content(self.conn, fetcher=fetcher)
        self.assertEqual(fetcher.calls, [], "unsafe schemes are refused before any fetch")
        row = self.state_of("url:deadbeef")
        self.assertEqual(row["extraction_state"], "failed")
        self.assertIn("denied: scheme", row["extraction_detail"])

    def test_detail_never_leaks_urls_or_query_secrets(self):
        secret_url = "https://example.com/private/download?token=SECRET123"
        resource_id = self.url_row(message_id="122", url=secret_url)
        fetcher = FakeFetcher(c6_result(denied_reason="timeout"))
        cx.extract_pending_content(self.conn, fetcher=fetcher)
        detail = self.state_of(resource_id)["extraction_detail"]
        self.assertNotIn("SECRET123", detail)
        self.assertNotIn("/private/download", detail)
        self.assertIn("example.com", detail, "host-only evidence is enough")

    def test_limit_bounds_the_batch(self):
        for index in range(3):
            self.url_row(message_id=str(140 + index),
                         url="https://example.com/item{}".format(index))
        fetcher = FakeFetcher(c6_result(denied_reason="timeout"))
        summary = cx.extract_pending_content(self.conn, limit=2, fetcher=fetcher)
        self.assertEqual(summary["examined"], 2)
        self.assertEqual(len(fetcher.calls), 2)

    def test_relevant_status_is_untouched_by_extraction(self):
        resource_id = self.url_row()
        self.conn.execute(
            "UPDATE resources SET status = 'relevant' WHERE resource_id = ?",
            (resource_id,),
        )
        self.conn.commit()
        fetcher = FakeFetcher(c6_result(
            ok=True, status=200, content_type="text/plain", bytes=5, text="hello",
        ))
        cx.extract_pending_content(self.conn, fetcher=fetcher)
        row = self.state_of(resource_id)
        self.assertEqual(row["status"], "relevant",
                         "extraction records evidence; it never rewrites decisions")
        self.assertEqual(row["extraction_state"], "ok")


class MediaExtractionTests(ExtractionCase):
    def test_media_bytes_verified_without_external_model_or_disk_copy(self):
        resource_id = self.media_row()
        fetcher = FakeFetcher(c6_result(
            ok=True, final_url="https://pbs.twimg.com/media/synthetic.jpg",
            status=200, content_type="image/jpeg", bytes=52341,
        ))
        cx.extract_pending_content(self.conn, fetcher=fetcher)
        row = self.state_of(resource_id)
        self.assertEqual(row["extraction_state"], "ok")
        self.assertIn("52341 bytes", row["extraction_detail"])
        self.assertIn("existing group-filter review path", row["extraction_detail"])
        self.assertIn("nothing sent to any external model", row["extraction_detail"])
        _, kwargs = fetcher.calls[0]
        self.assertNotIn("dest_dir", kwargs, "media bytes are never written to disk here")

    def test_media_stays_pending_under_the_deny_all_stub(self):
        resource_id = self.media_row(message_id="123")
        summary = cx.extract_pending_content(self.conn, fetcher=_contract_stub_fetch())
        self.assertEqual(summary["pending"], 1)
        self.assertEqual(self.state_of(resource_id)["extraction_state"], "pending")

    def test_media_without_fetchable_url_is_explicitly_unsupported(self):
        message = {
            "id": "124",
            "time": 124,
            "sender_id": "77",
            "text": "",
            "urls": [],
            "attachment": {"photo": {"width": 100}},
        }
        gm.persist_fetch(self.conn, PROFILE, one_batch(message), "100")
        cx.extract_pending_content(self.conn, fetcher=FakeFetcher(c6_result(ok=True)))
        row = self.state_of("media:124")
        self.assertEqual(row["extraction_state"], "unsupported")
        self.assertIn("no fetchable media URL", row["extraction_detail"])

    def test_non_https_media_target_is_refused_before_fetch(self):
        resource_id = self.media_row(
            message_id="125", url="http://pbs.twimg.com/media/downgrade.jpg"
        )
        fetcher = FakeFetcher(c6_result(ok=True))
        cx.extract_pending_content(self.conn, fetcher=fetcher)
        self.assertEqual(fetcher.calls, [])
        row = self.state_of(resource_id)
        self.assertEqual(row["extraction_state"], "failed")
        self.assertIn("non-https", row["extraction_detail"])


if __name__ == "__main__":
    unittest.main()
