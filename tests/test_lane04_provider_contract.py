"""Executable C5 provider contract checks — lane 04 → consumers 06/07.

Portable on purpose: it locates the run's `contracts/fixtures/` by walking up
from this file, so Chat 07 can drop it into the integration tree (or promote
it into `contracts/tests/targets/`) unchanged. If the fixtures are not
reachable it still verifies the provider's own guarantees.

What consumers may rely on (revision c1 + additive):
* every UNREVIEWED tool entry from build_tool_index carries
  `review_eligibility` with `lane ∈ review|evidence_pending|blocked`,
  `reasons: [str]`, nullable `evidence` (frozen keys `source_url, checked_at,
  extraction_state, confidence`), nullable `project_fit` (frozen keys
  `project, benefit, first_step, success_measure`);
* decided tools carry `review_eligibility: null` — authored verdicts and
  generated proposals never blur;
* relevant records WITHOUT tool keys carry the same block (methods/notes);
* all 26 frozen tool-entry keys from c5-tool-entry.json remain present;
* non-review lanes always explain themselves (non-empty reasons);
* dashboard-data payload stays JSON-serializable with the new fields.
"""

import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import group_monitor as gm  # noqa: E402
import recommend_eligibility as rec  # noqa: E402
from resource_typing import tool_key  # noqa: E402


def find_fixtures_dir():
    probe = Path(__file__).resolve()
    for parent in probe.parents:
        candidate = parent / "contracts" / "fixtures"
        if (candidate / "c5-eligibility-entry.json").exists():
            return candidate
    return None


FIXTURES = find_fixtures_dir()

PROFILE = {
    "selection": {
        "project_areas": {
            "agents": {"label": "Agents", "description": "agent systems",
                        "weight": 4, "keywords": []},
        }
    }
}

LANES = ("review", "evidence_pending", "blocked")
EVIDENCE_FROZEN = ("source_url", "checked_at", "extraction_state", "confidence")
PROJECT_FIT_FROZEN = ("project", "benefit", "first_step", "success_measure")
EXTRACTION_STATES = ("ok", "failed", "pending", "unsupported")
CONFIDENCES = ("high", "medium", "low")


def corpus():
    """Mixed corpus: repo with facts, repo without, service, article, video,
    note, Saudi item, decided item — the shapes 06 will actually render."""
    def rec_(url, rid, rtype, title, text, areas, source="group"):
        urls = [url] if url else []
        return {
            "resource_id": rid, "source": source, "status": "relevant",
            "resource_type": rtype, "title": title, "text": text,
            "external_urls": urls,
            "tool_keys": [k for k in (tool_key(u) for u in urls) if k],
            "project_areas": list(areas), "share_count": 1,
            "pick_score": 4.0, "shared_at": "2026-09-05T10:00:00+00:00",
            "updated_at": "2026-09-05T10:05:00+00:00", "verdict": None,
        }

    return [
        rec_("https://github.com/example/synthetic-cli", "tweet:1", "try",
             "cli", "open source cli, pip install", ["agents"]),
        rec_("https://github.com/example/unfetched", "tweet:2", "try",
             "unfetched repo", "another tool", ["agents"]),
        rec_("https://service.example/product", "tweet:3", "try",
             "hosted service", "web app with a free tier", ["agents"]),
        rec_("https://blog.example/ar/article", "tweet:4", "read",
             "مقال", "مقال قصير", ["agents"]),
        rec_("https://youtube.com/watch?v=a&list=b", "tweet:5", "learn",
             "Course", "A long, concrete, step-by-step series with enough detail in "
             "the captured description to be judged on its own merits.", ["agents"]),
        rec_("", "note:6", "reference", "",
             "A reusable method with plenty of concrete captured detail to review.",
             ["agents"]),
        rec_("https://github.com/example/decided", "tweet:7", "try",
             "decided tool", "already judged", ["agents"]),
    ]


META = {
    "example/synthetic-cli": {
        "slug": "example/synthetic-cli", "fetched_at": "2026-09-06T00:00:00+00:00",
        "ok": True, "description": "does a thing", "stars": 500,
        "pushed_at": "2026-09-01", "archived": False, "license": "MIT",
    }
}


def build_tools(records):
    table = {}
    for slug, value in META.items():
        table[slug.lower()] = value
        table["github.com/{}".format(slug).lower()] = value
    verdicts = {
        "github.com/example/decided": {
            "key": "github.com/example/decided", "verdict": "already_have",
            "name": "decided",
        }
    }
    with mock.patch.object(gm, "load_tool_meta", return_value=table):
        return gm.build_tool_index(
            records, verdicts, outcomes={}, profile=PROFILE,
            evidence_store=rec.WebEvidenceStore(Path(tempfile.mkdtemp()) / "we.json"),
        )


class C5EligibilityProviderContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.records = corpus()
        cls.tools = build_tools(cls.records)

    def assert_block_shape(self, block, where):
        self.assertIn(block["lane"], LANES, where)
        self.assertIsInstance(block["reasons"], list, where)
        for reason in block["reasons"]:
            self.assertIsInstance(reason, str, where)
        evidence = block["evidence"]
        if evidence is not None:
            for key in EVIDENCE_FROZEN:
                self.assertIn(key, evidence, where)
            self.assertIn(evidence["extraction_state"], EXTRACTION_STATES, where)
            self.assertIn(evidence["confidence"], CONFIDENCES, where)
        fit = block["project_fit"]
        if fit is not None:
            for key in PROJECT_FIT_FROZEN:
                self.assertIn(key, fit, where)

    def test_every_unreviewed_tool_gets_a_wellformed_block(self):
        unreviewed = [t for t in self.tools if t["verdict"] == "unreviewed"]
        self.assertGreaterEqual(len(unreviewed), 5)
        for tool in unreviewed:
            block = tool.get("review_eligibility")
            self.assertIsNotNone(block, tool["key"])
            self.assert_block_shape(block, tool["key"])
            if block["lane"] != "review":
                self.assertTrue(block["reasons"], tool["key"])

    def test_decided_tools_carry_null_eligibility(self):
        decided = next(t for t in self.tools if t["key"] == "github.com/example/decided")
        self.assertIsNone(decided["review_eligibility"])

    def test_record_level_block_for_toolless_resources(self):
        note = next(r for r in self.records if r["resource_id"] == "note:6")
        block = note.get("review_eligibility")
        self.assertIsNotNone(block)
        self.assert_block_shape(block, "note:6")
        self.assertEqual(block["lane"], "review")

    def test_frozen_tool_entry_keys_survive_annotation(self):
        if FIXTURES is None:
            frozen = ["key", "name", "url", "label", "is_repo", "verdict", "rank",
                      "lane", "what", "why", "first_step", "reason_code", "stars",
                      "license", "last_push", "mentions", "resource_ids",
                      "best_score", "latest_share", "auto", "facts", "meta_loaded",
                      "outcome", "outcome_note", "outcome_at", "resource_type"]
        else:
            frozen = json.loads(
                (FIXTURES / "c5-tool-entry.json").read_text()
            )["frozen_keys"]
        record_built = [t for t in self.tools if t["mentions"] > 0]
        for tool in record_built:
            for key in frozen:
                self.assertIn(key, tool, "{} missing {}".format(tool["key"], key))

    def test_fixture_sample_scenarios_reproduce(self):
        if FIXTURES is None:
            self.skipTest("contracts/fixtures not reachable from this tree")
        samples = json.loads(
            (FIXTURES / "c5-eligibility-entry.json").read_text()
        )["samples"]
        by_key = {t["key"]: t for t in self.tools}
        ready = by_key["github.com/example/synthetic-cli"]["review_eligibility"]
        self.assertEqual(ready["lane"],
                         samples["github_tool_ready"]["review_eligibility"]["lane"])
        self.assertIsNotNone(ready["project_fit"])
        pending = by_key["blog.example/ar/article"]["review_eligibility"]
        self.assertEqual(pending["lane"], "evidence_pending")
        self.assertIn("destination page not yet fetched", pending["reasons"])
        # blocked_unsafe: a policy-denied fetch
        store = rec.WebEvidenceStore(Path(tempfile.mkdtemp()) / "we.json")
        store._entries = {"svc.example/x": {
            "url": "https://svc.example/x", "checked_at": "2026-09-06T00:00:00+00:00",
            "extraction_state": "failed", "denied_reason": "private_target",
            "attempts": 1}}
        store._loaded = True
        blocked = rec.compute_review_eligibility(
            {"verdict": "unreviewed", "key": "svc.example/x",
             "url": "https://svc.example/x", "resource_type": "try", "facts": None},
            [], store.get("svc.example/x"),
            now=dt.datetime(2026, 9, 6, tzinfo=dt.timezone.utc),
        )
        self.assertEqual(blocked["lane"], "blocked")
        self.assertIn("fetch denied: private_target", blocked["reasons"])
        self.assertIsNone(blocked["evidence"])

    def test_payload_remains_json_serializable(self):
        json.dumps(self.tools, ensure_ascii=False, default=None)
        json.dumps(self.records, ensure_ascii=False, default=None)

    def test_no_fabricated_facts_for_unfetched_repo(self):
        tool = next(t for t in self.tools if t["key"] == "github.com/example/unfetched")
        block = tool["review_eligibility"]
        self.assertEqual(block["lane"], "evidence_pending")
        self.assertIsNone(tool["facts"])
        self.assertIsNone(block["project_fit"])  # no fit claims without evidence
        self.assertEqual(tool["verdict"], "unreviewed")  # and no auto exclusion


if __name__ == "__main__":
    unittest.main()
