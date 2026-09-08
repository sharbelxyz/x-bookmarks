## Explanatory Guide

### A. Product and User

The radar turns an all-participant private X group and personal bookmarks into a project-oriented briefing. The owner wants individual evidence-backed decisions, not a generic link roundup. High expected ROI can justify substantial setup. The active profile covers AI agents, communications, marketplace operations, apps, documents/LMS, Saudi/Arabic workflows, infrastructure, creative production and automation.

Capture, relevance classification, repository enrichment and ranking are automated. Comprehensive due diligence on every tool, installation, production migration and proof of effectiveness are not. A captured link, a relevant post, a curated recommendation and a successful trial are different levels of evidence.

### B. Checkouts and Deployment

The Python x-bookmarks checkout owns the live radar. The la-paz workspace owns dated research batches, the historical workbook, screenshots, audit reports and handoffs. x-bookmarks-saas is a separate Express/PostgreSQL/Chrome-extension product, not the observed local group scanner. Its public source is included in the fingerprint/inventory without merging its database into SQLite.

The SaaS source separates auth/JWT, API routers, validation/rate limiting, Knex migrations, sync workers, AI categorization and HTML views. Its package defines start/dev/migrate/test commands. Its documented flow is extension bookmark fetch -> authenticated upload -> worker categorization -> PostgreSQL -> dashboard/SSE progress. Deployment, provider availability and extension installation need separate live verification; source presence is not a running integration.

The Mac owns capture, filtering, ranking, exports and the viewer. The existing Atlas bot on the VPS supplies Telegram notification/callback infrastructure. The Mac pulls its decision log; the VPS is not the main scanner host.

### C. Capture and Resume

```text
All group senders -> authenticated capture -> SQLite occurrence ledger
                                           -> embedded tweet / bird hydration
Personal bookmarks and archive ------------> source-aware resources
                                           -> project rules -> bounded Codex review
GitHub facts + curated verdicts + outcomes -> action lanes / tool index / ranking
                                           -> HTML + JSON + CSV + Markdown exports
                                           -> strict gate -> Telegram delta
Local browser -> guarded decision APIs ----> authored config -> next export
Owner Telegram tap -> existing VPS bot ----> remote log -> Mac pull -> same verdict writer
Source/config/schema -> generator ---------> one canonical architecture file + history
```

Group capture uses the authenticated private X conversation endpoint reused from service.py. Owner metadata identifies credentials and reporting labels, not a sender allowlist. Pagination walks newest to oldest until the checkpoint, then persistence processes messages chronologically. The bootstrap continues after prior audited history, not the group's entire lifetime.

Cursor advancement requires a completed walk. A page cap or transport failure must not advance it. Replays and notification rebaselines should be intentional. These guards do not prove upstream endpoint completeness or support for every attachment type. Credential contents must never appear in reports.

### D. Identity and Provenance

Messages are occurrences; resources deduplicate by tweet ID, canonical URL hash or linkless-note message ID. message_resources preserves reshares and notification state. senders stores attribution. resources.source identifies origin, while occurrence joins are needed for multi-source provenance. Bookmark imports create synthetic message IDs alongside numeric group IDs; total message counts are not group-only counts.

The September 5 audit reproduced a bookmark-first/group-later ingestion failure: persist_fetch parses the existing bookmark:<id> as an integer and rolls back the group transaction. This is an audited defect, not an intended identity contract. Confirm its fix with a cross-source regression before declaring that scenario supported.

### E. Content and Hydration

Embedded tweet payloads avoid unnecessary secondary reads. Otherwise bird hydrates linked posts through configured accounts with bounded concurrency, retained errors and backoff. Sparse tweet media can be supplied to visual review. Native media-only group messages without text, URL or embedded tweet were not converted to resources in the September audit.

Only X tweets have the implemented hydration path. A generic web URL can be classified from its address and shared text without fetching its page. Relevance is not proof the destination was read. Unavailable is inferred from retrieval failures, not independently verified deletion.

### F. Rules and Model Boundary

Positive keyword matches score against the profile and may accept a resource without a model call. Approved negative terms operate on rule misses. Ambiguous live items go to Codex with a strict JSON schema, exact input/output IDs and bounded batches. The prompt prohibits browsing, tool use and acting on the resource; the process is read-only and ephemeral.

Filesystem read-only mode is not itself proof of complete network/tool isolation. Untrusted post text must remain evidence, not instructions. Limited images are downloaded from allowed hosts. The legacy decision_source value 'claude' also labels current Codex decisions, so that field alone is not accurate model provenance.

### G. Bookmark Archive

Live bookmarks reuse the account fetch path. Historical import reads a local categorized archive without new X reads. The owner approved importing it after an earlier proposal not to. Archive rule misses terminate as irrelevant/archive-rules to avoid a massive semantic backlog; they were not all model-reviewed. Requeueing should remain deliberate and bounded.

The HTML payload includes group-source resources plus a capped, ranked subset of relevant imported resources. SQLite and all-resources.csv contain the full ledger. The Source filter therefore cannot expose every non-relevant archived row. Synthetic bookmark IDs also interact with numeric notification cutovers; live bookmark alerts need a dedicated test before they are promised.

### H. Evidence Enrichment

enrich_tools uses gh to cache repository descriptions, stars, maintenance state and licensing. Requests are bounded and resumable; refresh TTL depends on review state. Transient failures retain last-good evidence. These facts are not an install audit, security review, cost calculation or demonstrated project ROI.

Automatic archived/stale/tiny/missing exclusions are policies, not universal quality judgments. Curated verdicts override them, so archival does not necessarily revoke an old must-try. Non-GitHub resources without successful repository facts cannot enter the same evidence-gated review queue. A separate review path is needed for useful articles, services and tutorials.

### I. Taxonomy and Ranking

Action lanes separate try, learn, read, reference and other. Exact weights and compatibility sets are generated above. Scoring combines project fit, action type, reshares, repository presence/health, recency, engagement and verdicts. The recency formula is exp(-age/days), an exponential scale despite the legacy constant name saying half-life. All-data mode removes its recency contribution.

Engagement is capped; broad roundups can still benefit from breadth of matched areas. Scores are prioritization heuristics, not measured returns. Tool identity indexes every external link, including curated zero-mention tools. Verdict inheritance is restricted to smaller posts to avoid endorsing a whole roundup through one link.

### J. Decisions, Outcomes and Learning

config/verdicts.json stores curated recommendations and exclusions. A research agent's authored recommendation is not owner authorization to install. config/outcomes.json independently records trying/kept/dropped and notes. Clearing a verdict must not erase trial history.

learn_negatives proposes discriminative content terms with Arabic normalization and support/contamination guards. Approval is required. The proposal generator is a standalone CLI, not currently a recurring training stage. Browser skip/read/dismiss state is separate from durable decisions.

September audit risks: server and scanner processes have separate in-process locks while writing the same verdict file and fixed temporary path; simultaneous writes can lose a decision. Malformed decision JSON is replaced with an empty document on the next write instead of failing closed. These need transactional regression tests, not merely atomic-rename assertions.

### K. Dashboard

dashboard_renderer builds self-contained HTML and a JSON twin. Views include Focus now, tool verdicts and a three-item review queue, suggested rules, new resources, action lanes, activity and the full stream. Client-side filters cover source, sender, project, type, status and handled state. Resource search normalizes Arabic spellings; the tool search is a separate implementation.

Group is the default stream source; briefing and tool views do not share all stream filters. Pagination limits rendered rows, not initial transfer/parsing. A long initial page, mobile overflow and incomplete ARIA tab semantics were observed in the dated browser audit. Desktop/mobile screenshots are evidence of that build, not permanent guarantees.

### L. Refresh and Persistence

The served page polls status and retrieves new payloads. Visible tabs stage updates; hidden tabs may apply them. Template changes require reload. Local-file opening cannot replace the served write APIs.

Verdict and outcome saves persist config and update the active tab optimistically. Reloading before the next export can show older values. There is no transactional config-to-export push. Queue skips and handled state are browser-local; a saved trial outcome does not automatically update every Focus now action.

### M. HTTP and Trust Boundaries

The server binds loopback, exposes an explicit file allowlist, rejects symlink escapes and adds no-store/CSP headers. Action routes require a custom header. SQLite, arbitrary files and account data are not published routes. The generated route inventory derives from handlers, including outcome and negative-term APIs.

The September audit found no Host/Origin allowlist. A custom header is useful but does not cover every rebinding threat. Non-object JSON bodies are not consistently rejected before handlers call .get. Public exposure would require authentication, origin checks, request bounds and a separate security review. Loopback binding is not user authentication.

### N. Telegram

Newly relevant group occurrences are selected after the cutover and summarized. Notifications can offer a decision for one unreviewed tool. Short callback IDs map to local tool keys. The existing VPS bot owns polling; a second poller for the same token must not be started.

The Mac pulls the log by byte offset and applies verdict validation. A real owner tap was not evidenced in the inspected state. The audit reproduced a rotation bug: the old offset is used for the read before rotation is handled, allowing the new log to be skipped. Rejected decisions are checkpointed too. Callback yes maps to must_try without passing resource_type, bypassing the conditional lane check.

### O. Transactions and Performance

SQLite schema setup is additive. idx_message_resources_resource supports per-resource occurrence lookup and prevents the severe export slowdown seen when the archive was imported. The worker flock serializes full scanner runs, not every CLI/server mutation or export. Atomic rename prevents torn individual files, not lost read-modify-write updates between processes.

Exports are written individually rather than as a single generation transaction. Compare generatedAt, status and verification timestamps separately. A server-backed paginated query layer and unified transactional decision store would be future work, not descriptions of today's implementation.

### P. Reliability and Health

Cron starts the scanner twice hourly while the Mac is awake. Subprocess timeouts, a soft budget, SIGALRM hard guard and worker locking limit hangs. A hydration failure threshold can stop unrelated semantic review. Model-auth failures, capture failures, Telegram errors and genuinely idle successful runs must remain distinguishable.

/api/health reports viewer availability, export age and queue readiness; it does not independently prove the latest run succeeded. A passing verification artifact may predate a failure. The generated snapshot includes recent journal outcome counts to expose the distinction. Job completion is not external-action proof.

### Q. Privacy, Export and Recovery

Ignored account/environment files and restricted runtime permissions protect local data. Private-group text remains private even when individual links are public. Sending review batches or notifications transmits selected content outside the Mac; minimize unrelated messages and maintain explicit retention expectations.

CSV exports contained untrusted text without dedicated spreadsheet-formula neutralization in the September audit. A text-safe spreadsheet export is needed for adversarial input. No project-managed, restore-tested backup of both SQLite and authored config was found in the inspected scripts. Uncommitted/untracked work and ignored data are not backed up just because a remote branch exists. Architecture history is not a database backup.

### R. Research and Handoffs

The workspace retains dated evaluations, source records, metadata and a workbook builder. Its historical unified workbook defines 383 submissions and 363 unique submitted links across five batches, not current radar totals or unique software products. Different posts about the same tool remain distinct source records.

Master handoff and ledger preserve progress, decisions and open work. Their numbers are dated. This file is the canonical technical architecture, linked from the workspace. Absorption is distinct from review, commit, deployment and owner acceptance.

### S. Automatic Architecture Maintenance

The generator hashes explicit public source/config paths, parses imports/routes/CLI arguments/constants, reads SQLite schema in a read-only transaction and includes this maintained guide. Numeric settings are compared exactly. Only the generation timestamp and marked runtime observations are excluded from structural checks.

The existing viewer runs a bounded refresh subprocess at startup and every 60 seconds; a fresh process sees changed generator code. The scanner refreshes after success or failure. A file lock serializes generators, unique temporary files and os.replace publish atomically, and prior distinct documents are retained under _versions/. Nothing publishes the file externally.

This is bounded eventual freshness, not instantaneous or infallible correctness. An asleep Mac, stopped service, invalid source/config or refresh error can delay updates. Errors are logged. Fingerprints identify the inputs, and tests cover derived contracts, but changed behavior still requires reviewing this guide. Historical audit findings above must be revalidated after their related code changes. Old versions have no automatic deletion policy.

### T. Operations

Run from the Python runtime root:

```bash
/usr/bin/python3 scripts/generate_architecture.py --refresh
/usr/bin/python3 scripts/generate_architecture.py --check
/usr/bin/python3 -m unittest discover -s tests
curl --max-time 10 -fsS http://127.0.0.1:8765/api/health
```

Also read the latest run journal. group_monitor.py verify --strict normally writes status/verification. export rebuilds outputs without a group scan; a full group_filter_loop.py pass can fetch private content, apply queued owner decisions and notify Telegram. Do not use it as a harmless health probe.

Scanner stop: scripts/manage_group_filter_schedule.py uninstall. Viewer stop: scripts/manage_radar_server.py uninstall. They are independent. Stopping the viewer stops its documentation heartbeat; scanner-final refresh remains while scanning runs. Stopping both leaves the last dated architecture. Do not erase the ledger, outcomes or credentials during shutdown.

### U. Audit Boundary

The September 5 audit covered local radar code, runtime observations, desktop/mobile UI, research continuity and the related SaaS source boundary. It did not revalidate every historical recommendation, execute a real owner Telegram action, verify the remote bot deployment end to end, authenticate SaaS production, conduct penetration testing or prove disaster recovery. Synthetic reproductions used temporary databases/config and did not alter production decisions.

The detailed findings and product ratings are in the research workspace at outputs/audit-2026-09-05/FULL-AUDIT.md. Audit reports are dated evidence; this architecture continues to refresh independently.

## Parallel-run integration additions (2026-09-07, integrated by Chat 07)

The following explanatory prose was proposed by the implementation lanes of parallel run `run-20260906-2000` and folded in at integration. New modules: `decision_store` (inter-process decision persistence), `content_extraction` (C6-bounded url/media evidence), `safe_fetch` (target-validated fetching), `http_guards` (Host/Origin/body validation), `export_safety` (spreadsheet-safe sheet), `run_health` (stage health/backoff), `radar_backup` (consistent backup/restore), `recommend_eligibility` (typed review eligibility).

### Lane 01 — capture, provenance and content extraction

# Proposed explanatory changes for the canonical architecture guide (07 integrates)

1. **Occurrence identity and ordering (C1).** Document the two message-id
   types: all-digit group DM ids (numeric chronology; they alone drive the
   fetch cursor and notification cutovers) and synthetic occurrence ids
   `<origin>:<suffix>` (provenance links with no DM-sequence position).
   `last_message_id` now uses the typed rule in
   `group_monitor.message_order_key`: any group id outranks every synthetic
   id; group ids compare numerically; synthetic ids compare lexically among
   themselves. `first_message_id` keeps the first arrival;
   `message_resources` remains the complete provenance record.

2. **Group visibility derives from occurrences.** `resources.source` is the
   first-origin label and is never overwritten by later occurrences (in either
   direction). The briefing membership test is `in_group` (source is a group
   source OR >=1 all-digit occurrence), computed in `select_resource_rows` as
   `group_share_count` and projected by `resource_to_dict`.

3. **Native media capture (A04).** `fetch_group_messages` now retains the raw
   non-tweet attachment as `attachment_raw`; `extract_resources` emits a
   `media:<message_id>` resource (kind `media`) for message-level
   photo/video/animated_gif attachments, and an explicit `unsupported` row for
   unrecognized attachment shapes. Media resources persist their metadata in
   `payload_json` ({media, attachment_keys, _source: dm_media}) so a later
   extraction retry needs no re-fetch of the DM.

4. **Processing state is not relevance.** New resources columns
   `extraction_state` / `extraction_detail` / `extraction_checked_at`
   (additive; NULL on legacy rows) record whether linked content was actually
   read: `pending|ok|unsupported|failed`, the same vocabulary as the C5
   evidence contract. URL resources start `pending` ("destination content not
   fetched yet") — link text is not proof the destination was read. Content
   extraction itself goes through the C6 safe-fetch provider (lane 05); until
   integrated, the deny-all stub keeps every fetch `provider_unavailable`.

### Lane 02 — durable decisions, outcomes and Telegram recovery

# Proposed architecture-guide updates from lane 02 (for 07 to apply at integration)

Workers do not write the canonical guide or the generated file; this is the
prose 07 should fold in when it runs `generate_architecture.py --refresh`
against its integrated scope.

## Decision persistence (replaces the "atomic write" description)

`config/verdicts.json` and `config/outcomes.json` are written through
`scripts/decision_store.py`, which is the single validated persistence path
for every writer (dashboard HTTP, Telegram pull, agent sessions):

- an `fcntl.flock` sidecar lock (`<name>.json.lock`, never deleted) covers the
  whole read → validate → write cycle, so separate OS processes cannot lose
  each other's edits; waits are bounded and surface as HTTP 503;
- a missing file is a valid empty state, but a corrupt or structurally invalid
  one fails closed — the write is refused and the existing bytes are left
  byte-identical;
- publication is a unique temp file + fsync + `os.replace` + directory fsync,
  so a killed writer leaves either the old or the new document, never a torn
  one;
- the outgoing version is archived to `config/_history/<stem>/<stamp>-r<rev>.json`
  (last 20) before each replace;
- each successful publish bumps an integer `revision` inside the document.
  Mutations return the authoritative stored record plus that revision, and an
  optional `expected_revision` yields `409 {error, current_revision}`.

Lock order is unchanged (worker.lock → SQLite → verdicts → outcomes → profile
→ checkpoints); writers take one document lock at a time.

## Fresh read-back

`radar_server.read_decisions()` serves both authored documents plus their
revisions (proposed route `GET /api/decisions`), so a save is visible on
reload and in a second tab immediately, without waiting for the next scanner
export. A corrupt document is reported as an error rather than shown as empty
history; the static/`file://` dashboard stays honestly read-only.

## Telegram decision sync

The checkpoint is log identity (inode + hash of the first N bytes) plus
`consumed_ids`, not a byte offset. Rotation, in-place truncation and same-size
rewrite are detected before consuming from the stale offset and trigger a
re-read from the start; only complete newline-terminated lines are consumed;
received and rejected events are journaled to
`data/group-monitor/telegram-received.jsonl` and `telegram-rejected.jsonl`
before the checkpoint advances; application is idempotent via stable event ids
recorded both in the checkpoint and on the verdict (`source_event`). The VPS
remains the only Telegram poller and the pull remains read-only over ssh.
Destructive upstream truncation before any pull remains an acknowledged,
documented gap rather than a claimed guarantee.

## Outcomes

Outcomes stay a separate file from verdicts (clearing a recommendation never
erases a trial) and can now record a measured trial: `project`, `artifact`,
`success_measure`, `baseline`, `observed_result`, `units`, `evidence`,
`trial_date`. Unmeasured values are absent, never defaulted; a recommendation
alone never creates an outcome.

