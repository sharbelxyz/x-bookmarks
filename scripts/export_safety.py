#!/usr/bin/env python3
"""Human-safe spreadsheet export helper (contract C7 / audit A07, lane 05).

The radar's CSV exports carry text authored by ANY group sender. A cell that
begins with ``=``, ``+``, ``-`` or ``@`` is interpreted as a FORMULA by
spreadsheet applications when the file is opened/imported, which turns a
hostile group message into code evaluated on the reviewer's machine (classic
CSV/formula injection: ``=HYPERLINK(...)`` exfil links, DDE-style payloads).
CSV *quoting* does not help — quoting is a parsing concern, formula
interpretation happens after parsing.

Strategy frozen at contract revision c1 (fixtures/c7-csv-columns.json):

* ``relevant.csv`` / ``all-resources.csv`` / ``relevant.jsonl`` stay RAW
  machine formats — original text byte-preserved, never neutralized.
* A SEPARATE human-safe artifact (``relevant-sheet.csv``) is generated for
  the spreadsheet consumer. String cells whose first character is one of
  ``= + - @ TAB CR`` get a single leading apostrophe (``'``).

Consumer-specific behavior of the apostrophe prefix (documented, not
universal formula safety):

* **Google Sheets** (target consumer): a leading apostrophe marks the cell
  as text and is hidden; the reviewer sees the original content, inert.
* **Apple Numbers** (target consumer): the apostrophe is shown literally as
  part of the text; content is inert. Cosmetic cost accepted — this file is
  the human view, the raw truth lives in the machine formats.
* **Microsoft Excel**: NOT a tested/claimed consumer for this artifact.
  The apostrophe convention is honored by Excel in practice, but nothing
  here is verified against Excel's locale-dependent import paths — treat it
  as unsupported.

Numbers (int/float, excluding bool) pass through unprefixed so numeric
columns (score, pick_score, share_count) stay sortable; a hostile value can
only reach a string field. The file is written atomically with a UTF-8 BOM
(``utf-8-sig``) so all three applications detect Unicode (Arabic content)
correctly; the BOM is another deliberate divergence from the raw exports.

Interactive verification in Numbers/Sheets requires a GUI session and is
NOT performed by the isolated lane; see the lane handoff for the exact
outstanding checks before this artifact is declared consumer-verified.
"""

from __future__ import annotations

import csv
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

# Contract c1 freezes the minimum trigger set (= + - @ \t \r). OWASP's CSV
# Injection page additionally lists line feed and the full-width variants
# ＝ ＋ － ＠ (locale-dependent); neutralizing them too is additive hardening
# — strictly more cells protected, none less. Kept as data so tests and
# documentation cannot drift from the implementation.
FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r", "\n",
                    "＝", "＋", "－", "＠")

HUMAN_SHEET_NAME = "relevant-sheet.csv"


def neutralize_cell(value: Any) -> Any:
    """Return the value safe for a human spreadsheet cell.

    Strings starting with a formula trigger get a leading apostrophe; all
    other strings and all non-string scalars pass through unchanged (the
    csv module renders them; numbers stay numbers). The original string is
    always recoverable by stripping one leading apostrophe when the second
    character is a trigger.
    """
    if isinstance(value, str) and value and value[0] in FORMULA_TRIGGERS:
        return "'" + value
    return value


def neutralize_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {key: neutralize_cell(value) for key, value in row.items()}


def write_human_sheet_csv(
    path: "Path | str",
    fieldnames: Sequence[str],
    rows: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    """Atomically write the human-safe CSV; returns a small summary dict.

    ``rows`` are the ALREADY-FLATTENED dicts the raw CSV writer receives —
    this helper never re-derives export semantics, so the two artifacts
    cannot disagree about content, only about neutralization.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    neutralized_cells = 0
    row_count = 0
    descriptor, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(fieldnames))
            writer.writeheader()
            for row in rows:
                out = {}
                for key in fieldnames:
                    original = row.get(key, "")
                    safe = neutralize_cell(original)
                    if safe is not original:
                        neutralized_cells += 1
                    out[key] = safe
                writer.writerow(out)
                row_count += 1
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return {"path": str(path), "rows": row_count, "neutralized_cells": neutralized_cells}


def unneutralize_cell(value: str) -> str:
    """Inverse for tests/tooling: strip the one apostrophe this module adds."""
    if (
        isinstance(value, str)
        and len(value) >= 2
        and value[0] == "'"
        and value[1] in FORMULA_TRIGGERS
    ):
        return value[1:]
    return value


_NUMERIC = re.compile(r"^-?\d+(\.\d+)?([eE][+-]?\d+)?$")


def self_check(rows: List[Dict[str, Any]], fieldnames: Sequence[str], parsed_back: List[List[str]]) -> List[str]:
    """Machine-verifiable safety property for tests: after parsing the human
    CSV, no data cell may begin with a formula trigger. Pure numerals (our
    own numeric columns, e.g. ``-1.5``) are exempt — the consumers parse
    them as numbers, which are inert. Returns violations."""
    problems = []
    for row_index, parsed_row in enumerate(parsed_back):
        for column_index, cell in enumerate(parsed_row):
            if cell and cell[0] in FORMULA_TRIGGERS and not _NUMERIC.match(cell):
                problems.append(
                    "row {} column {} still starts with {!r}".format(
                        row_index, fieldnames[column_index] if column_index < len(fieldnames) else column_index,
                        cell[0],
                    )
                )
    return problems
