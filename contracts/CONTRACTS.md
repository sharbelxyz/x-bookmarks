# Frozen Interface Contracts — run-20260906-2000, revision c1

Published by Chat 07 on 2026-09-06 (Asia/Riyadh). Extracted from the actual
live source (branch `glassmorphism-dashboard`, mostly untracked files — see
`../BASELINE.json`), not from prose assumptions.

**Rules.** These freeze the EXTERNAL surface between lanes, not implementation
choices. Changes are additive/backward-compatible only; breaking changes need
a new revision from Chat 07 plus acknowledgment and re-run of contract tests
by affected lanes. Fixtures under `fixtures/` are machine-checkable; tests
under `tests/current/` must pass in every lane before submitting a package;
tests under `tests/targets/` encode the POST-FIX acceptance semantics and are
**expected red on the baseline** (they are the audit reproductions — see the
status table at the bottom). Fixture conformance is a development aid; final
acceptance requires the real integrated provider.

Terminology: "frozen" = keep exactly; "additive" = new optional
fields/routes allowed, existing ones unchanged; "target" = the agreed
post-fix semantics a provider must reach.

---

## C1. Resource identity and occurrence — provider 01 → consumers 02/04/06

**Frozen identity scheme** (`scripts/group_monitor.py`, `extract_resources` L560,
`scripts/ingest_bookmarks.py` L174):

| ID | Form | Meaning |
|---|---|---|
| resource_id | `tweet:<digits>` | an X post (embedded or linked) |
| resource_id | `url:<sha256(canonical_url)[:24]>` | a non-tweet URL after `_clean_url` normalization (tracking keys stripped) |
| resource_id | `note:<message_id>` | text-only message with no URL/attachment |
| message_id | `<digits>` | real group DM message |
| message_id | `bookmark:<tweet_id>` | synthetic message for an imported/live bookmark occurrence (sender_id `bookmark`) |
| tool key | `host/path` via `resource_typing.tool_key()` | tool-level identity; DISTINCT from resource_id; one tool ↔ many resources |

**Frozen storage** (SQLite, `connect_db` L175-275): tables `metadata`,
`messages`, `senders`, `resources`, `message_resources`, `runs`; columns as in
baseline source; `resources.source ∈ {group, bookmark, archive…}` (TEXT,
default `group`); `resources.status ∈ {pending_hydration, pending_review,
relevant, irrelevant, unavailable}` (VALID_STATUSES L80). Index
`idx_message_resources_resource` is essential and must survive any migration.
Occurrences live in `message_resources(message_id, resource_id, notified_at)`
— **every** occurrence is preserved; `resources.first_message_id` /
`last_message_id` are conveniences, not the provenance record.

**Frozen record projection** (`resource_to_dict` L1376): consumers rely on
these exact keys — see `fixtures/c1-resource-record.json`. Additive keys
allowed; never rename/retype existing ones. `share_count`, `sharers`,
`sharer_ids` carry multi-occurrence provenance to the UI/CSV.

**Targets (A01/A04)** — `tests/targets/test_c1_targets_identity.py`:
- T-A01: bookmark-first→group-later and group-first→bookmark-later both
  succeed: one logical resource, BOTH occurrences in `message_resources`,
  group visibility correct, no exception from mixed-type ordering
  (`max(..., key=int)` L755 is the defect site). Ordering rule must be
  explicit and typed (numeric IDs and `bookmark:` IDs never int-compared).
- T-A04: a native media-only message (no text, no URL, no embedded tweet)
  yields a durable resource with an explicit processing state. **Additive
  states allowed** for this (e.g. new `kind` value `media` and/or new
  processing-state field); consumers must treat unknown kinds as displayable
  "unsupported/pending evidence", never drop them. New states must be
  distinct from relevance; "unreadable" ≠ "irrelevant" ≠ "deleted".

## C2. Decision and outcome persistence — provider 02 → consumers 03/04/06

**Frozen API** (`scripts/radar_server.py`):
- `record_verdict(body: dict) -> (int, dict)` L329. Request fields: `key`
  (required, `host/owner/name`-ish, must contain `/`), `verdict` (required,
  `must_try|must_read|excluded|already_have|clear`), optional `name`, `why`,
  `what`, `first_step`, `lane`, `reason_code`, `resource_type`, `stars`,
  `license`, `last_push`. Type/verdict compatibility via
  `resource_typing.verdict_fits_type` → HTTP 409 + `error` + `hint` on
  mismatch. Success 200: `{ok, action ∈ added|replaced|cleared|"not present",
  key, verdict, total, note}`. `must_try` gets auto `rank` = max+1.
- `record_outcome(body) -> (int, dict)` L422. Fields: `key`, `state`
  (`trying|kept|dropped|clear`), optional `name`, `note`. Success 200:
  `{ok, action, key, state, total}`.
- `record_negative_term(body)` L482: `term` (single token 3-40 chars),
  `action ∈ add|remove`; 409 when the term is already a positive term.
- Readers: `group_monitor.load_verdicts()` (keyed by lowercased tool_key),
  `load_outcomes()`. Document shapes frozen in
  `fixtures/c2-verdicts-file.json` / `c2-outcomes-file.json`:
  `{version:1, updated_at:"YYYY-MM-DD", verdicts:[…]}` /
  `{version:1, updated_at, outcomes:[…]}`.
- Files: `config/verdicts.json`, `config/outcomes.json`, mode 0600, written
  via `<name>.json.tmp` + `os.replace`. Verdicts and outcomes stay SEPARATE
  files: clearing a verdict must never erase a trial.

**Targets (A02/A05/A06/A10)** — `tests/targets/test_c2_targets_persistence.py`:
- T-A02: two OS processes writing different keys concurrently → both survive;
  same-key concurrency has deterministic documented conflict behavior.
  Inter-process locking (e.g. `json_filelock`-style flock; lock ORDER per C3)
  + unique temp files. Threading locks alone are insufficient.
- T-A06: corrupt existing JSON → write REJECTED (4xx/5xx with `error`), file
  bytes unchanged, recoverable prior version retained; absence ≠ corruption.
- T-A05 (with 02's Telegram repair): rotation/truncation of the remote log
  never skips or double-applies decisions; checkpoint = log identity +
  consumed records, not a raw byte offset (`telegram_decisions.fetch_remote`
  L122-164 is the defect site); rejects/unknowns are durably retained with
  reasons; Telegram-applied verdicts resolve `resource_type` server-side.
- T-A10 (with C7 read-back): a successful mutation returns the authoritative
  record + revision, and an agreed fresh read-back endpoint exists
  (**additive route**, registered by 07; proposal: `GET /api/decisions` →
  `{verdicts_document, outcomes_document, revision}`) so save→reload→second
  tab agree without waiting for the next export.
- Revision semantics (additive): responses/documents may add `revision`
  (monotonic int or content hash) + conflict answer `409 {error, current_revision}`
  for `expected_revision` mismatches. Field names frozen as written here.

## C3. Storage / recovery manifest — proposals 01/02, schema owner 07 → consumer 03

Frozen store inventory (`fixtures/c3-storage-manifest.json`, v1): the logical
recovery set is
- `data/group-monitor/group-monitor.sqlite3` (+`-wal`/`-shm`) — ledger; backup
  ONLY via SQLite backup API/`VACUUM INTO`, never file copy of a live DB
- `config/verdicts.json`, `config/outcomes.json`,
  `config/group-filter-profile.json` — authored decisions/rules
- `data/group-monitor/telegram-offset.json`, `pending-decisions.json` —
  decision-sync checkpoints
- `data/group-monitor/autonomous-runs.jsonl` — run journal (append-only)
- metadata keys inside SQLite: `fetch_cursor`, `fetch_incomplete`,
  `capture_scope_version`, `hydration_failure_repeats`
- Exports (`dashboard.html`, `dashboard-data.json`, `status.json`,
  `relevant.*`, `all-resources.csv`, `latest.md`, `unavailable.jsonl`,
  `verification.json`, `tool-meta.json`) are REGENERABLE, not part of the
  minimal recovery set; a restore must be able to regenerate them.

Frozen lock order (deadlock avoidance; taking a later lock while holding an
earlier one is allowed, never the reverse):
`worker.lock` (flock, `exclusive_run_lock`) → SQLite connection →
`verdicts.json` lock → `outcomes.json` lock → profile lock → checkpoint files.
A "consistent logical revision" = all of the above captured while holding
`worker.lock` (scanner quiesced) or via SQLite backup API + post-copy
re-hash of the JSON set. 01/02 propose schema changes as ADDITIVE migrations
through 07 (`fixtures/c3-migration-proposal.schema.json`); 03 builds
backup/restore against this manifest; restores default to a NEW directory
with notifications and scheduling disabled.

## C4. Health — provider 03 → consumer 06

**Frozen envelope** (`radar_server.build_health` L119, served at
`GET /api/health`): exact current fields in `fixtures/c4-health.json`:
`service, ok, now, server_started_at, pid, status_updated_at, status_error,
age_seconds, stale, stale_after_seconds, gate_ready, resources,
status_counts, dashboard_modified_at, dashboard_data_modified_at,
next_run_at, cron_minutes`. All retained.

**Frozen inputs**: `status.json` = `group_monitor.status_snapshot` L1924
(fields in `fixtures/c4-status-snapshot.json`); run journal entries =
`fixtures/c4-journal-entry.json` (`outcome ∈ ok|error|stuck`).

**Target (A03)** — additive block, field names frozen at c1
(`fixtures/c4-health-extended.json`): top-level `stages` object with
per-stage records `{state, at, detail?}` for stages
`capture, hydration, semantic_review, decision_sync, notification, backup,
export`; `state ∈ ok|degraded|failed|auth_required|recovering|unknown`;
plus top-level `last_run_outcome`, `last_run_at`,
`last_semantic_success_at`, `auth_required` (bool), `backlog_age_seconds`
(int|null), `backoff` `{active, until, reason}`. An old passing gate must
never overwrite a newer failure; liveness stays independent of stage health.

## C5. Recommendation / evidence — provider 04 → consumer 06

**Frozen tool entry** (`group_monitor.build_tool_index` L1522; fixture
`c5-tool-entry.json`): keys `key, name, url, label, is_repo, verdict
(∈ must_try|must_read|excluded|already_have|unreviewed), rank, lane, what,
why, first_step, reason_code, stars, license, last_push, mentions,
resource_ids, best_score, latest_share, auto, facts, meta_loaded, outcome,
outcome_note, outcome_at, resource_type`. `facts` = tool-meta record
(`enrich_tools.fetch_repo`; fixture `c5-tool-meta-entry.json`; `ok:false`
carries `error`, optional `missing:true`). Auto-gate reason codes frozen:
`gone|archived|empty|stale|tiny` (`auto_gate` L1502) — policy heuristics,
overridden by any hand verdict.

**Frozen typing** (`resource_typing`): `RESOURCE_TYPES =
(try, learn, read, reference, other)`; `VERDICT_FOR_TYPE` as in source;
scoring v2 constants unchanged by this run except through a measured,
reviewed proposal.

**Target (A09)** — additive eligibility contract
(`fixtures/c5-eligibility-entry.json`), field names frozen at c1: each
unreviewed tool/resource gets `review_eligibility` `{lane ∈ review|
evidence_pending|blocked, reasons: [string], evidence: {source_url,
checked_at, extraction_state ∈ ok|failed|pending|unsupported, confidence ∈
high|medium|low}|null, project_fit: {project, benefit, first_step,
success_measure}|null}`. GitHub-facts success must stop being the only route
into review; failed evidence → visible reason, NEVER fabricated facts.
Generated proposals stay distinct from authored verdicts (C2 files remain
the only authored store).

## C6. Safe fetch — provider 05 → consumers 01/04

New provider module (05 owns implementation): `scripts/safe_fetch.py`
exposing exactly:

```python
def safe_fetch(url: str, *, max_bytes: int = 4_000_000, timeout: float = 20.0,
               max_redirects: int = 4, allowed_content_types: tuple = (),
               dest_dir: str | None = None) -> dict
```

Result (fixture `c6-safe-fetch.json`): `{ok: bool, url, final_url, status:
int|null, content_type, bytes: int, body_path: str|null, text: str|null,
error: str|null, denied_reason: str|null}`. Guarantees: scheme allowlist
(https, http), DNS+redirect re-validation against loopback/private/link-local/
metadata ranges, byte/time/redirect budgets, decompression bounds, no
execution of fetched content, explicit failure (`ok:false` + `denied_reason ∈
scheme|private_target|redirect_target|too_large|timeout|content_type|
provider_unavailable|error`). Until the real provider is integrated,
consumers import `contracts/fixtures/safe_fetch_stub.py`, which conforms to
this signature and DENIES everything with `provider_unavailable` — deny by
default is the frozen fallback. Existing precedent to preserve:
`group_filter_loop.download_review_images` (trusted-host image fetch) may be
migrated to this provider but its https/host/size behavior must not weaken.

## C7. HTTP boundary — provider 05 (guards) + 07 (dispatch) → consumers 02/03/06

**Frozen surface** (`RadarHandler`; fixture `c7-http-routes.json`):
- GET/HEAD: `/api/health` + static ROUTES table (`/`, `/dashboard.html`,
  `/dashboard-data.json`, `/status.json`, `/verification.json`,
  `/relevant.csv`, `/all-resources.csv`, `/relevant.jsonl`, `/latest.md`,
  `/negative-proposals.json`), allowlist-only, symlink-escape checked,
  `Cache-Control: no-store`, CSP on HTML.
- POST: `/api/run|verdict|outcome|negative-term`, each requiring header
  `X-Radar-Action: <action>`; body 1..16384 bytes JSON; other methods 405.
- Client behavior (dashboard JS, frozen): same-origin `fetch` with the
  custom header + `Content-Type: application/json`; static/`file://` mode is
  read-only with an honest "open the served dashboard" message.
- Loopback bind only; production port 8765 — FORBIDDEN in lanes; tests bind
  port 0.

**Targets (A08)** — additive hardening, semantics frozen at c1:
Host header must match an explicit loopback allowlist (incl. actual port);
browser mutations validated via Origin/Referer rules with a documented
policy for missing Origin (no undocumented bypass); JSON body must be an
object → controlled `400 {error}` for list/null/scalar (never an unhandled
exception); content-type checked; bounded read time; unsupported
transfer-encodings rejected. New read-back route(s) from C2-T-A10 register
through 07. Guard failures must not change any stored state.

**Export safety (A07, 05 + 07 wiring)**: raw machine formats
(`relevant.jsonl`, `dashboard-data.json`) keep ORIGINAL text; a separate
human-safe spreadsheet export neutralizes formula-leading cells
(`= + - @ \t \r`) per the chosen consumer (target consumer: Apple Numbers +
Google Sheets import; state anything untested). CSV column sets frozen in
`fixtures/c7-csv-columns.json`.

---

## Cross-review assignments (unchanged from PARALLEL-CONTRACTS.md)

01→02 identity/persistence integration; 02→03 backup consistency;
03→01 failure/checkpoint behavior; 04→06 evidence/decision semantics;
05→04 untrusted evidence handling; 06→05 browser compatibility.
Read-only; findings go to Chat 07.

## Contract test status on the baseline (2026-09-06, all under harness)

| Suite | Result | Meaning |
|---|---|---|
| `tests/current/` | must be all green | frozen-surface regression guard |
| `tests/targets/` | RED = faithful audit reproduction | providers turn these green |

Chat 07 records the actual per-test baseline results in
`../READY.json` → `contract_test_evidence`.

Revision history: **c1** — initial freeze (2026-09-06).

## c1 clarifications (2026-09-07, Chat 07 — no breaking change, no new revision)

1. **C6 `allowed_content_types` matching** (lane 01's question): an entry
   ending in `/` is a PREFIX match (`"image/"` matches `image/png`); any other
   entry is exact equality on the media type. This is what lane 05's provider
   implements (`safe_fetch._content_type_allowed`); lane 01's
   `("image/", "video/")` usage is conforming.
2. **C2-T-A10 read-back route**: `GET /api/decisions` (lane 02's
   `read_decisions()`) is REGISTERED in the integration dispatcher by 07.
   Semantics as in `packages/02/02-pkg-001/proposals/07-route-registration.md`.
3. **C7 export triggers**: lane 05's neutralization set is a strict SUPERSET
   of the c1 minimum (adds LF and full-width ＝＋－＠). Allowed: consumers must
   never assert the exact minimum set.
4. **Targets test repair**: `test_failed_batch_does_not_advance_cursor` now
   injects its failure explicitly (lane 01's proposal, applied verbatim); the
   protected property is unchanged.
5. **C6 dev stub retirement**: with lane 05's `scripts/safe_fetch.py`
   integrated, `scripts/safe_fetch_stub.py` is NOT part of the integration
   tree; `content_extraction` imports the real provider first by design.
