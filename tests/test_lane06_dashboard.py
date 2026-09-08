"""Lane 06 unit tests — renderer template/payload invariants (no browser).

Browser-level acceptance lives in tests/lane06_browser/ (measure.py,
browser_checks.py, real_server_check.py) and needs Playwright + Chrome:

    env -i HOME="$LANE/home" TMPDIR="$LANE/tmp/" XDG_CACHE_HOME="$LANE/cache" \
      PATH="$RUN/harness/shims:/usr/bin:/bin" \
      PYTHONPATH="$RUN/harness:$HOME_REAL/Library/Python/3.9/lib/python/site-packages" \
      /usr/bin/python3 tests/lane06_browser/browser_checks.py --out <evidence-dir>

This file stays green inside the plain harness so `discover -s tests` works
everywhere.
"""

import json
import re
import sys
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
LANE = TESTS_DIR.parent
sys.path.insert(0, str(LANE / "scripts"))
sys.path.insert(0, str(TESTS_DIR))

import dashboard_renderer as dr  # noqa: E402
import lane06_fixtures as fx  # noqa: E402



def _find_run_root(start):
    # Integration-tree repair by Chat 07 (2026-09-07): lanes sit at
    # <run>/workers/NN but integration at <run>/integration, so a fixed
    # parents[] hop cannot serve both. Walk up to the contracts dir instead
    # (same pattern as test_lane04_provider_contract).
    for parent in [start] + list(start.parents):
        if (parent / "contracts" / "fixtures").is_dir():
            return parent
    return start.parents[3]

class TemplateSemantics(unittest.TestCase):
    """A13: pressed-filter semantics, focus visibility, media fallbacks."""

    def test_no_tablist_roles_remain(self):
        self.assertNotIn('role="tablist"', dr.PAGE_TEMPLATE)
        self.assertNotIn("role='tablist'", dr.PAGE_TEMPLATE)

    def test_static_filter_buttons_carry_aria_pressed(self):
        for chunk in re.findall(r'<button class="(?:status-tab|tool-tab|chip-toggle)"[^>]*>', dr.PAGE_TEMPLATE):
            self.assertIn("aria-pressed", chunk, chunk)

    def test_focus_visible_outline_is_solid(self):
        self.assertIn("outline: 3px solid var(--focus)", dr.PAGE_TEMPLATE)

    def test_no_nowrap_on_repo_fact_links(self):
        block = dr.PAGE_TEMPLATE.split(".repo-fact a, .repo-fact .repo-name", 1)[1].split("}", 1)[0]
        self.assertNotIn("nowrap", block)
        self.assertIn("overflow-wrap: anywhere", block)

    def test_media_failure_note_exists(self):
        self.assertIn("media-failed", dr.PAGE_TEMPLATE)
        self.assertIn("image unavailable", dr.PAGE_TEMPLATE)

    def test_skip_link_and_live_regions(self):
        for marker in ('class="skip-link"', 'id="sr-status"', 'id="sr-alert"', 'role="alert"'):
            self.assertIn(marker, dr.PAGE_TEMPLATE, marker)

    def test_identifier_direction_isolated(self):
        self.assertIn(".idtext { direction: ltr; unicode-bidi: isolate; }", dr.PAGE_TEMPLATE)

    def test_legacy_strings_preserved(self):
        # Pinned by the pre-existing suite and by muscle memory.
        for marker in ("Focus now", "Caught up", "focus-window-select", "ranked from all data",
                       "status.json", "dashboard-data.json", "Group Resource Radar"):
            self.assertIn(marker, dr.PAGE_TEMPLATE, marker)


class ClientContract(unittest.TestCase):
    """A10/A09/C4 client behaviors that are checkable from source."""

    def test_saves_are_not_applied_optimistically(self):
        # State mutates only inside the `result.ok` branch of submit* — the
        # word "optimistic" is not the test; ordering is: postAction first.
        script = dr.PAGE_TEMPLATE
        verdict_body = script.split("async function submitVerdict", 1)[1].split("async function submitOutcome", 1)[0]
        self.assertLess(verdict_body.index("await postAction"), verdict_body.index("decisions.verdicts.set"))
        outcome_body = script.split("async function submitOutcome", 1)[1].split("function handleSaveFailure", 1)[0]
        self.assertLess(outcome_body.index("await postAction"), outcome_body.index("decisions.outcomes.set"))

    def test_readback_route_and_fallback_present(self):
        self.assertIn("api/decisions", dr.PAGE_TEMPLATE)
        self.assertIn("decisions.readback = 'unavailable'", dr.PAGE_TEMPLATE)
        self.assertIn("next export", dr.PAGE_TEMPLATE)

    def test_conflict_and_revision_fields_used(self):
        self.assertIn("expected_revision", dr.PAGE_TEMPLATE)
        self.assertIn("current_revision", dr.PAGE_TEMPLATE)

    def test_eligibility_lanes_covered(self):
        for marker in ("review_eligibility", "evidence_pending", "blocked",
                       "destination page not fetched yet", "GitHub facts not fetched yet"):
            self.assertIn(marker, dr.PAGE_TEMPLATE, marker)

    def test_health_stage_states_covered(self):
        for state in ("degraded", "failed", "auth_required", "recovering", "unknown"):
            self.assertIn(state, dr.PAGE_TEMPLATE, state)
        self.assertIn("needs your sign-in", dr.PAGE_TEMPLATE)

    def test_search_is_debounced(self):
        self.assertIn("searchTimer = setTimeout", dr.PAGE_TEMPLATE)


class PayloadPassthrough(unittest.TestCase):
    def test_review_eligibility_survives_payload_build(self):
        payload = fx.build_payload()
        carrying = [t for t in payload["tools"] if t.get("review_eligibility")]
        self.assertEqual(len(carrying), 3)
        lanes = sorted(t["review_eligibility"]["lane"] for t in carrying)
        self.assertEqual(lanes, ["blocked", "evidence_pending", "review"])

    def test_fixture_resources_conform_to_c1_required_keys(self):
        run_root = _find_run_root(Path(__file__).resolve().parent)
        fixture = run_root / "contracts" / "fixtures" / "c1-resource-record.json"
        required = json.loads(fixture.read_text(encoding="utf-8"))["required_keys"]
        for record in fx.build_resources():
            for key in required:
                self.assertIn(key, record, "{} missing {}".format(record["resource_id"], key))

    def test_fixture_tools_conform_to_c5_frozen_keys(self):
        run_root = _find_run_root(Path(__file__).resolve().parent)
        fixture = run_root / "contracts" / "fixtures" / "c5-tool-entry.json"
        frozen = json.loads(fixture.read_text(encoding="utf-8"))["frozen_keys"]
        for tool in fx.build_tools():
            for key in frozen:
                self.assertIn(key, tool, "{} missing {}".format(tool["key"], key))

    def test_rendered_page_embeds_payload_once_and_escapes(self):
        payload = fx.build_payload()
        payload["resources"][0]["title"] = "AI </script> injection probe"
        html = dr.render_dashboard_from_payload(payload)
        self.assertEqual(html.count('<script id="dashboard-data"'), 1)
        self.assertIn("AI <\\/script> injection probe", html)
        self.assertNotIn("</script> injection probe", html)


class FixtureServerSemantics(unittest.TestCase):
    """The lane's C2 stand-in must not drift from the frozen semantics."""

    def test_verdict_validation_and_revision(self):
        from lane06_browser.fixture_server import FixtureState, make_handler  # noqa: F401
        state = FixtureState(media_base="")
        start_revision = state.revision
        documents = state.decisions_documents()
        self.assertEqual(documents["revision"], start_revision)
        self.assertIn("verdicts_document", documents)
        self.assertIn("outcomes_document", documents)
        self.assertEqual(documents["verdicts_document"]["version"], 1)
        # Payload-authored verdicts are mirrored so read-back agrees with page.
        keys = {e["key"] for e in documents["verdicts_document"]["verdicts"]}
        self.assertIn("github.com/shortlist/tool-number-01", keys)
        outcome_keys = {e["key"] for e in documents["outcomes_document"]["outcomes"]}
        self.assertIn("github.com/shortlist/tool-number-01", outcome_keys)


if __name__ == "__main__":
    unittest.main()
