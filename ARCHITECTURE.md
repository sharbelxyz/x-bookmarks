# Complete Architecture: Group Resource Radar

> Generated at: 2026-09-08T07:40:03+00:00
> Canonical file: /Users/mshrmnsr/claude1/x-bookmarks/ARCHITECTURE.md
> Source/config fingerprint: 55ecf01c907a10c48afc51b9edc24515be4a57b73655a10aa882b8944f89f8b3
> Inventory is source-derived; explanatory prose is maintained in references/architecture-guide.md.
> Refresh: viewer startup, every 60 seconds while running, and after scanner runs. Stopped/asleep services cannot refresh.
> Drift checks do not prove every narrative claim or third-party service behavior. Review affected contracts after changes.

## Contents

- Live Snapshot
- System Boundaries
- Pipeline and Calls
- Modules and Dependencies
- HTTP and CLI Interfaces
- Configuration and Constants
- Database Schema
- Test Inventory
- Explanatory Guide: capture through operations
- Source Inventory

<!-- runtime-snapshot:start -->
## Live Snapshot

Timestamped observations, not a guarantee of current health. Attempted pending hydration can coexist with a passing strict gate.

| Signal | Observed |
|---|---|
| Status snapshot at | 2026-09-08T07:34:25+00:00 |
| Capture scope / fetch incomplete | ["all-senders-v1", false] |
| Resources, all sources | 27633 |
| Status counts | {"irrelevant": 25313, "pending_hydration": 0, "pending_review": 0, "relevant": 2132, "unavailable": 188} |
| Latest run | {"finished_at": "2026-09-08T07:34:25+00:00", "outcome": "ok", "started_at": "2026-09-08T07:33:01+00:00"} |
| Last recorded strict check | {"pass": true, "strict": true, "verified_at": "2026-09-08T07:34:25+00:00"} |
| Recent run outcomes (up to 48) | {"error": 47, "ok": 1} |
| Cron / launchd observation | {"scanner_installed": true, "viewer_loaded": true} |
| Dashboard generated at | 2026-09-08T07:34:25+00:00 |
| Group coverage, not whole ledger | {"days": 56, "imported": 1221, "newest": "2026-09-08T07:10:12+00:00", "oldest": "2026-07-15T03:00:04+00:00", "relevant": 911, "resources": 2041} |
| Dashboard payload rows | 3262 |
| Indexed tools | 492 |
| Unreviewed / without usable repo facts | [447, 111] |
| Curated verdicts / recorded outcomes | [29, 0] |

| Resource source | State | Rows |
|---|---|---|
| bookmark | irrelevant | 56 |
| bookmark | relevant | 33 |
| bookmark-archive | irrelevant | 24315 |
| bookmark-archive | relevant | 1188 |
| group | irrelevant | 942 |
| group | relevant | 911 |
| group | unavailable | 188 |

| Table | Rows including synthetic bookmarks |
|---|---|
| message_resources | 27760 |
| messages | 27760 |
| metadata | 13 |
| resources | 27633 |
| runs | 447 |
| senders | 11 |

<!-- runtime-snapshot:end -->

## System Boundaries

| Surface | Owner/location |
|---|---|
| Live scanner, server, SQLite and config | /Users/mshrmnsr/claude1/x-bookmarks |
| Durable resource ledger | /Users/mshrmnsr/claude1/x-bookmarks/data/group-monitor/group-monitor.sqlite3 |
| Research, historical workbook and handoffs | /Users/mshrmnsr/conductor/workspaces/tensor algebra/la-paz |
| Related SaaS checkout, not this scanner deployment | /Users/mshrmnsr/claude1/x-bookmarks-saas |
| Telegram callback | Existing VPS Atlas bot; Mac pulls decisions. Remote deployment needs separate verification. |
| Canonical architecture | This file; the workspace architecture path is a link, not a second independent document. |

## Pipeline and Calls

Source order, not a promise every branch executes. Sync performs an initial export before final verification; newer output is not proof of a successful run.

### group_filter_loop.py / run_workflow

| Source line | Call |
|---|---|
| 336 | monitor.utc_now |
| 346 | monitor.connect_db |
| 348 | monitor.load_profile |
| 349 | monitor.exclusive_run_lock |
| 351 | monitor.get_meta |
| 374 | monitor.sync_once |
| 410 | telegram_decisions.pull |
| 427 | ingest_bookmarks.ingest_live |
| 437 | monitor.resource_to_dict |
| 437 | monitor.select_resource_rows |
| 439 | enrich_tools.enrich |
| 440 | monitor.build_tool_index |
| 440 | monitor.load_verdicts |
| 449 | monitor.get_meta |
| 485 | monitor.get_meta |
| 493 | monitor.set_meta |
| 520 | monitor.prepare_review_batch |
| 553 | run_codex_review |
| 574 | monitor.apply_decisions |
| 581 | monitor.utc_now |
| 617 | monitor.export_relevant |
| 618 | monitor.verify |
| 640 | monitor.notify_relevant |
| 681 | monitor.utc_now |
| 686 | record_loopsmith |
| 710 | append_journal |
| 713 | refresh_architecture |

### group_monitor.py / sync_once

| Source line | Call |
|---|---|
| 2637 | fetch_group_messages |
| 2638 | persist_fetch |
| 2657 | hydrate_pending |
| 2660 | apply_rule_classification |
| 2661 | export_relevant |
| 2662 | status_snapshot |

## Modules and Dependencies

| File | Role | Local imports |
|---|---|---|
| scripts/content_extraction.py | reachable dependency | safe_fetch.py |
| scripts/dashboard.py | outside radar graph; legacy/standalone | json_filelock.py, service.py |
| scripts/dashboard_renderer.py | reachable dependency | resource_typing.py |
| scripts/decision_store.py | reachable dependency | none |
| scripts/enrich_tools.py | cli: Repository evidence refresh | group_monitor.py |
| scripts/export_safety.py | reachable dependency | none |
| scripts/fetch_bookmarks_api.py | outside radar graph; legacy/standalone | x_api_auth.py |
| scripts/generate_architecture.py | cli: Canonical architecture maintenance | none |
| scripts/group_filter_loop.py | cron: Bounded capture/classification worker | enrich_tools.py, group_monitor.py, ingest_bookmarks.py, run_health.py, telegram_decisions.py |
| scripts/group_monitor.py | cli: Pipeline stages, export and verification | content_extraction.py, dashboard_renderer.py, export_safety.py, notify_buttons.py, recommend_eligibility.py, resource_typing.py, service.py, telegram_decisions.py |
| scripts/http_guards.py | reachable dependency | none |
| scripts/ingest_bookmarks.py | cli: Live and historical bookmark import | group_monitor.py, service.py |
| scripts/json_filelock.py | reachable dependency | none |
| scripts/learn_negatives.py | cli: Propose negative terms; not a scheduled training stage | group_monitor.py |
| scripts/llm_provider.py | reachable dependency | none |
| scripts/manage_group_filter_schedule.py | cli: Scanner lifecycle | none |
| scripts/manage_radar_server.py | cli: Viewer lifecycle | none |
| scripts/notify_buttons.py | reachable dependency | vps_config.py |
| scripts/radar_backup.py | outside radar graph; legacy/standalone | run_health.py |
| scripts/radar_server.py | launchd: Viewer, decision APIs and architecture heartbeat | decision_store.py, http_guards.py, resource_typing.py, run_health.py |
| scripts/recommend_eligibility.py | reachable dependency | group_monitor.py, resource_typing.py, safe_fetch.py |
| scripts/resource_typing.py | reachable dependency | none |
| scripts/run_health.py | reachable dependency | none |
| scripts/safe_fetch.py | reachable dependency | none |
| scripts/scheduler.py | outside radar graph; legacy/standalone | json_filelock.py |
| scripts/service.py | reachable dependency | json_filelock.py, llm_provider.py |
| scripts/telegram_decisions.py | cli: Pull and apply existing owner callbacks | decision_store.py, radar_server.py, vps_config.py |
| scripts/vps_config.py | reachable dependency | none |
| scripts/x_api_auth.py | outside radar graph; legacy/standalone | none |

Static reachability is not scheduler proof. Legacy service.py remains a live X capture/bookmark dependency.

## Runtime Files

| Path | Role |
|---|---|
| data/group-monitor/group-monitor.sqlite3 | Durable resources, occurrences, schema, cursor and run metadata |
| data/group-monitor/dashboard.html | Self-contained rendered UI |
| data/group-monitor/dashboard-data.json | JSON twin used for live updates; capped payload |
| data/group-monitor/status.json | Cursor, queue and export readiness snapshot |
| data/group-monitor/verification.json | Last persisted strict invariant check |
| data/group-monitor/autonomous-runs.jsonl | Run outcomes and stage details; inspect independently of viewer health |
| data/group-monitor/relevant.csv, all-resources.csv | Filtered and full spreadsheet exports; untrusted text caveat applies |
| data/group-monitor/relevant.jsonl, unavailable.jsonl, latest.md | Machine-readable records, retrieval failures and latest relevant summary |
| data/group-monitor/tool-meta.json | Last-good GitHub evidence cache |
| data/group-monitor/pending-decisions.json, telegram-offset.json | Callback ID map and remote-log checkpoint |
| data/group-monitor/negative-proposals.json | Candidate exclusion rules awaiting approval |
| data/group-monitor/review-batch.json, decisions-current.json | Most recent semantic batch and returned decisions |
| data/group-monitor/worker.lock, cron.log, server.log | Worker exclusion and local runtime logs |
| config/group-filter-profile.json | Group scope, project areas, relevance weights and approved negatives |
| config/group-filter-decisions.schema.json | Semantic output contract |
| config/verdicts.json, config/outcomes.json | Authored recommendations and separate trial results |
| config/architecture-scope.json | Explicit related checkout locations for architecture coverage |
| references/architecture-guide.md | Maintained explanatory source for this generated file |
| group-share-filter.loop.json, group-share-filter.prompt.md | Automation contract and operating instructions |
| _versions/ARCHITECTURE.md.*.md | Content-addressed previous architecture documents |

## HTTP and CLI Interfaces

| Route | Methods | Handler/output |
|---|---|---|
| / | GET, HEAD | dashboard.html |
| /all-resources.csv | GET, HEAD | all-resources.csv |
| /api/health | GET, HEAD | Viewer liveness, export age and queue readiness |
| /api/negative-term | POST | Validated action; see handler source |
| /api/outcome | POST | Validated action; see handler source |
| /api/run | POST | Validated action; see handler source |
| /api/verdict | POST | Validated action; see handler source |
| /dashboard-data.json | GET, HEAD | dashboard-data.json |
| /dashboard.html | GET, HEAD | dashboard.html |
| /latest.md | GET, HEAD | latest.md |
| /negative-proposals.json | GET, HEAD | negative-proposals.json |
| /relevant-sheet.csv | GET, HEAD | relevant-sheet.csv |
| /relevant.csv | GET, HEAD | relevant.csv |
| /relevant.jsonl | GET, HEAD | relevant.jsonl |
| /status.json | GET, HEAD | status.json |
| /verification.json | GET, HEAD | verification.json |

| CLI | Declared arguments/commands |
|---|---|
| group_filter_loop.py | --max-batches, --no-notify, --no-record |
| radar_server.py | --data-dir, --health, --host, --port |
| group_monitor.py | --concurrency, --db, --force-retry, --limit, --live, --max-hydrate, --max-pages, --out, --profile, --replay-bootstrap, --strict, apply-decisions, baseline, decisions, export, extract-content, notify, prepare-review, requeue, resource_ids, status, sync, verify |
| enrich_tools.py | --all, --budget-seconds, --limit |
| ingest_bookmarks.py | --archive, --limit, --live, --no-export |
| learn_negatives.py | --limit, --print |
| telegram_decisions.py | --pull, --self-test |
| manage_radar_server.py | action |
| manage_group_filter_schedule.py | action |
| generate_architecture.py | --check, --out, --refresh |

## Configuration and Constants

Restricted AST evaluation supplies these values; numeric descriptions are not duplicated.

### group_filter_loop.py

| Constant | Current value |
|---|---|
| MAX_BATCHES | 4 |
| MAX_SUPERVISED_BATCHES | 20 |
| REVIEW_BATCH_SIZE | 20 |
| MAX_DURATION_SECONDS | 1680 |
| HARD_DEADLINE_SECONDS | 1770 |
| ENRICH_LIMIT_PER_RUN | 20 |
| ENRICH_BUDGET_SECONDS | 90 |
| MAX_REVIEW_IMAGES | 20 |
| MAX_IMAGE_BYTES | 8388608 |
| TRUSTED_IMAGE_HOSTS | ["pbs.twimg.com", "video.twimg.com"] |

### resource_typing.py

| Constant | Current value |
|---|---|
| FIT_PER_AREA | 3.0 |
| FIT_AI_BONUS | 1.0 |
| RESHARE_PER_SHARE | 4.0 |
| RESHARE_PER_MEMBER | 3.0 |
| REPO_LINK_BONUS | 3.0 |
| ENGAGEMENT_CAP | 2.0 |
| HEALTH_CAP | 4.0 |
| STALE_HEALTH_PENALTY | 3.0 |
| STALE_AFTER_DAYS | 365 |
| RECENCY_HALF_LIFE_DAYS | 7.0 |
| TYPE_BONUS | {"learn": 2.5, "other": 0.0, "read": 2.0, "reference": 1.5, "try": 3.0} |
| VERDICT_BONUS | {"already_have": -4.0, "excluded": -12.0, "must_read": 5.0, "must_try": 6.0} |
| VERDICT_FOR_TYPE | {"learn": ["already_have", "excluded", "must_read"], "other": ["already_have", "excluded", "must_read", "must_try"], "read": ["already_have", "excluded", "must_read"], "reference": ["already_have", "excluded", "must_read", "must_try"], "try": ["already_have", "excluded", "must_try"]} |

### group_monitor.py

| Constant | Current value |
|---|---|
| CAPTURE_SCOPE_VERSION | all-senders-v1 |
| VALID_STATUSES | ["irrelevant", "pending_hydration", "pending_review", "relevant", "unavailable"] |
| DASHBOARD_SCHEDULE | {"cadenceMinutes": 30, "cronMinutes": [17, 47], "staleAfterMinutes": 90} |
| DASHBOARD_BOOKMARK_CAP | 2000 |
| MAX_TOOLS_FOR_VERDICT_INHERITANCE | 3 |
| AUTO_STALE_DAYS | 365 |
| AUTO_MIN_STARS | 25 |

### radar_server.py

| Constant | Current value |
|---|---|
| DEFAULT_HOST | 127.0.0.1 |
| DEFAULT_PORT | 8765 |
| STALE_AFTER_SECONDS | 5400 |
| RUN_COOLDOWN_SECONDS | 600 |
| MAX_FILE_BYTES | 67108864 |
| ALLOWED_VERDICTS | ["already_have", "excluded", "must_read", "must_try"] |
| ALLOWED_OUTCOMES | ["dropped", "kept", "trying"] |
| ARCHITECTURE_REFRESH_SECONDS | 60 |

### enrich_tools.py

| Constant | Current value |
|---|---|
| TTL_DAYS | {"candidate": 30, "rejected": 90, "reviewed": 7} |
| PER_CALL_TIMEOUT | 25 |
| DEFAULT_LIMIT | 40 |
| DEFAULT_BUDGET_SECONDS | 240 |

### ingest_bookmarks.py

| Constant | Current value |
|---|---|
| SOURCE_LIVE | bookmark |
| SOURCE_ARCHIVE | bookmark-archive |
| LIVE_FETCH_LIMIT | 100 |
| COMMIT_EVERY | 1000 |

### telegram_decisions.py

| Constant | Current value |
|---|---|
| CALLBACK_PREFIX | rdr |
| CALLBACK_LIMIT | 64 |
| ID_LENGTH | 8 |
| SSH_TIMEOUT | 30 |
| ACTION_TO_VERDICT | {"n": "excluded", "y": "must_try"} |
| PENDING_RETENTION_DAYS | 30 |

### learn_negatives.py

| Constant | Current value |
|---|---|
| MIN_EXCLUDED_HITS | 5 |
| MAX_RELEVANT_HITS | 1 |
| MIN_LOG_ODDS | 1.5 |
| MAX_PROPOSALS | 25 |

### Active Project Profile

| Setting | Value |
|---|---|
| capture_scope | all_senders |
| minimum_score / ai_weight | [3, 4] |
| Configured bookmark accounts | 2 |
| AI terms / approved negative terms | [27, 0] |

| Project area | Label | Weight | Keyword count |
|---|---|---|---|
| ai-agent-systems | AI agents and research infrastructure | 3 | 21 |
| apps-frontend-release | Apps, frontend, and release engineering | 3 | 29 |
| automation-research-tools | Automation and research tools | 3 | 12 |
| creative-brand-media | Creative, brand, and media production | 3 | 13 |
| documents-knowledge-learning | Documents, knowledge, and LMS | 3 | 14 |
| hermes-communications | Hermes communications | 3 | 14 |
| infrastructure-security | Infrastructure and security | 3 | 23 |
| marketplace-product-ops | Marketplace and product operations | 3 | 20 |
| saudi-arabic-career-legal | Saudi, Arabic, career, and legal workflows | 3 | 21 |

## Database Schema

Read from a consistent read-only SQLite transaction. Counts are snapshots; schema is drift-checked.

### message_resources

| Column | Type | NOT NULL | Default | Primary-key position |
|---|---|---|---|---|
| message_id | TEXT | 1 | None | 1 |
| resource_id | TEXT | 1 | None | 2 |
| notified_at | TEXT | 0 | None | 0 |

Indexes: idx_message_resources_resource, sqlite_autoindex_message_resources_1

| From | Parent table | To | On delete |
|---|---|---|---|
| resource_id | resources | resource_id | CASCADE |
| message_id | messages | message_id | CASCADE |

### messages

| Column | Type | NOT NULL | Default | Primary-key position |
|---|---|---|---|---|
| message_id | TEXT | 0 | None | 1 |
| conversation_id | TEXT | 1 | None | 0 |
| sent_at_ms | INTEGER | 0 | None | 0 |
| sender_id | TEXT | 1 | None | 0 |
| is_owner | INTEGER | 1 | None | 0 |
| text | TEXT | 1 | None | 0 |
| urls_json | TEXT | 1 | None | 0 |
| captured_at | TEXT | 1 | None | 0 |

Indexes: idx_messages_sender, sqlite_autoindex_messages_1

### metadata

| Column | Type | NOT NULL | Default | Primary-key position |
|---|---|---|---|---|
| key | TEXT | 0 | None | 1 |
| value | TEXT | 1 | None | 0 |

Indexes: sqlite_autoindex_metadata_1

### resources

| Column | Type | NOT NULL | Default | Primary-key position |
|---|---|---|---|---|
| resource_id | TEXT | 0 | None | 1 |
| kind | TEXT | 1 | None | 0 |
| canonical_url | TEXT | 0 | None | 0 |
| tweet_id | TEXT | 0 | None | 0 |
| first_message_id | TEXT | 1 | None | 0 |
| last_message_id | TEXT | 1 | None | 0 |
| sender_id | TEXT | 1 | None | 0 |
| source_text | TEXT | 1 | None | 0 |
| status | TEXT | 1 | None | 0 |
| hydration_attempts | INTEGER | 1 | 0 | 0 |
| next_retry_at | TEXT | 0 | None | 0 |
| last_error | TEXT | 0 | None | 0 |
| payload_json | TEXT | 0 | None | 0 |
| title | TEXT | 0 | None | 0 |
| author | TEXT | 0 | None | 0 |
| content_text | TEXT | 0 | None | 0 |
| score | INTEGER | 0 | None | 0 |
| project_areas_json | TEXT | 0 | None | 0 |
| reasons_json | TEXT | 0 | None | 0 |
| decision_source | TEXT | 0 | None | 0 |
| first_seen_at | TEXT | 1 | None | 0 |
| updated_at | TEXT | 1 | None | 0 |
| notified_at | TEXT | 0 | None | 0 |
| source | TEXT | 1 | 'group' | 0 |
| extraction_state | TEXT | 0 | None | 0 |
| extraction_detail | TEXT | 0 | None | 0 |
| extraction_checked_at | TEXT | 0 | None | 0 |

Indexes: idx_resources_source, idx_resources_last_message, idx_resources_status, sqlite_autoindex_resources_1

### runs

| Column | Type | NOT NULL | Default | Primary-key position |
|---|---|---|---|---|
| id | INTEGER | 0 | None | 1 |
| started_at | TEXT | 1 | None | 0 |
| finished_at | TEXT | 0 | None | 0 |
| outcome | TEXT | 1 | None | 0 |
| details_json | TEXT | 1 | None | 0 |

Indexes: 

### senders

| Column | Type | NOT NULL | Default | Primary-key position |
|---|---|---|---|---|
| sender_id | TEXT | 0 | None | 1 |
| username | TEXT | 0 | None | 0 |
| display_name | TEXT | 0 | None | 0 |
| avatar_url | TEXT | 0 | None | 0 |
| is_owner | INTEGER | 1 | 0 | 0 |
| updated_at | TEXT | 1 | None | 0 |

Indexes: sqlite_autoindex_senders_1

## Test Inventory

342 test methods discovered. Discovery is not proof of passing tests; consult the dated audit.

| Module / class | Methods |
|---|---|
| test_architecture_refresh.py:ArchitectureRefreshTests | 12 |
| test_group_monitor.py:ArchitectureDocTests | 5 |
| test_group_monitor.py:AutoGateTests | 4 |
| test_group_monitor.py:BookmarkIngestTests | 4 |
| test_group_monitor.py:EnrichmentTests | 6 |
| test_group_monitor.py:GroupMonitorTests | 25 |
| test_group_monitor.py:ManageRadarServerTests | 1 |
| test_group_monitor.py:NegativeRuleTests | 6 |
| test_group_monitor.py:OutcomeTests | 3 |
| test_group_monitor.py:RadarServerTests | 9 |
| test_group_monitor.py:ResourceTypingTests | 3 |
| test_group_monitor.py:ScoringV2Tests | 6 |
| test_group_monitor.py:TaxonomySeparationTests | 5 |
| test_group_monitor.py:TelegramDecisionTests | 4 |
| test_integration_e2e_lifecycle.py:E2ELifecycle | 1 |
| test_lane01_extraction.py:MediaExtractionTests | 4 |
| test_lane01_extraction.py:StubConformanceTests | 2 |
| test_lane01_extraction.py:UrlExtractionTests | 8 |
| test_lane01_ingestion.py:ContentCoverageFixtureTests | 8 |
| test_lane01_ingestion.py:CrossSourceProvenanceTests | 4 |
| test_lane01_ingestion.py:OrderingRuleTests | 1 |
| test_lane01_ingestion.py:PayloadVisibilityTests | 2 |
| test_lane02_persistence.py:A02TwoRealProcesses | 4 |
| test_lane02_persistence.py:A06CorruptionAndAbsence | 4 |
| test_lane02_persistence.py:A10RevisionAndReadBack | 6 |
| test_lane02_persistence.py:ReadBackOverHTTP | 1 |
| test_lane02_persistence.py:SeparationOfFiles | 1 |
| test_lane02_persistence.py:ServerSideTyping | 4 |
| test_lane02_telegram.py:A05DurableRetention | 9 |
| test_lane02_telegram.py:A05RotationTruncation | 8 |
| test_lane02_telegram.py:OutcomeTrialFields | 5 |
| test_lane03_backup_restore.py:BackupCreateTests | 7 |
| test_lane03_backup_restore.py:BackupPolicyHookTests | 4 |
| test_lane03_backup_restore.py:HistoryRotationRaceTests | 2 |
| test_lane03_backup_restore.py:RealIntegratedStorageTests | 2 |
| test_lane03_backup_restore.py:VerifyAndRestoreTests | 8 |
| test_lane03_health_backoff.py:AlertTests | 1 |
| test_lane03_health_backoff.py:BackoffTests | 4 |
| test_lane03_health_backoff.py:BuildHealthIntegrationTests | 2 |
| test_lane03_health_backoff.py:CapacityClassTests | 3 |
| test_lane03_health_backoff.py:ComposeTests | 4 |
| test_lane03_health_backoff.py:LoopStageDrillTests | 9 |
| test_lane03_health_backoff.py:ScrubAndClassifyTests | 2 |
| test_lane03_health_backoff.py:StageStoreTests | 2 |
| test_lane03_real_storage.py:ProviderAvailabilityTests | 1 |
| test_lane03_real_storage.py:RealIntegratedStorageBackupTests | 3 |
| test_lane04_eligibility.py:AuthoredStateSurvivesTests | 3 |
| test_lane04_eligibility.py:BoundedWorkTests | 4 |
| test_lane04_eligibility.py:FairnessTests | 3 |
| test_lane04_eligibility.py:MixedTypeAcceptanceTests | 7 |
| test_lane04_eligibility.py:NoFabricationTests | 4 |
| test_lane04_eligibility.py:ShortlistTests | 4 |
| test_lane04_eligibility.py:UntrustedContentTests | 3 |
| test_lane04_provider_contract.py:C5EligibilityProviderContract | 7 |
| test_lane05_export_safety.py:ExportIntegration | 3 |
| test_lane05_export_safety.py:NeutralizeUnit | 7 |
| test_lane05_http_adversarial.py:BodyAttacks | 10 |
| test_lane05_http_adversarial.py:HostHeaderAttacks | 7 |
| test_lane05_http_adversarial.py:LegitimateClients | 4 |
| test_lane05_http_adversarial.py:OriginPolicyAttacks | 6 |
| test_lane05_http_adversarial.py:StaticSurface | 5 |
| test_lane05_safe_fetch.py:FetchBehavior | 16 |
| test_lane05_safe_fetch.py:NoNetworkDenials | 7 |
| test_lane05_safe_fetch.py:PolicyUnit | 2 |
| test_lane05_safe_fetch.py:TlsBehavior | 2 |
| test_lane06_dashboard.py:ClientContract | 6 |
| test_lane06_dashboard.py:FixtureServerSemantics | 1 |
| test_lane06_dashboard.py:PayloadPassthrough | 4 |
| test_lane06_dashboard.py:TemplateSemantics | 8 |

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

## Source Inventory

Only explicit public source/config paths are inspected. Account/environment files, private messages and credential values are never copied here.

| Scope-relative path | SHA-256 prefix |
|---|---|
| .gitignore | 57085585523d |
| GROUP_FILTER.md | 1bf3b8ecc709 |
| README.md | 5aa94681824a |
| SERVICE_GUIDE.md | f47d2c9883ac |
| SKILL.md | 3d882102b712 |
| app/dashboard.html | ba007c098812 |
| app/index.html | 99f1b2532d3e |
| build_app.py | dca0dc4681fe |
| config/architecture-scope.json | 88a5fd9cf090 |
| config/group-filter-decisions.schema.json | 42834f8b7dd6 |
| config/group-filter-profile.json | b3dfbf25711f |
| config/outcomes.json | a099f3cef96e |
| config/verdicts.json | 380d1f7a1085 |
| config/vps.json | b8f9928432b0 |
| group-share-filter.loop.json | c8112c269735 |
| group-share-filter.prompt.md | 7a59baaf1bcd |
| references/architecture-guide.md | 36eb4b00eb18 |
| references/auth-setup.md | 250cf5480485 |
| related-saas/extension/background.js | 5f30829dd03c |
| related-saas/extension/manifest.json | e3c773a2aa78 |
| related-saas/extension/popup.html | 4158f9f67969 |
| related-saas/extension/popup.js | ffa87c669c64 |
| related-saas/package.json | 199f5178e4e0 |
| related-saas/src/api/bookmarks.routes.js | e84b6716b00e |
| related-saas/src/api/sync.routes.js | fd122cf03362 |
| related-saas/src/api/user.routes.js | fea44c171afe |
| related-saas/src/auth/jwt.js | 8ed1eef1c446 |
| related-saas/src/auth/middleware.js | 8b4b808fb0dd |
| related-saas/src/auth/routes.js | 7578cc86167d |
| related-saas/src/config/env.js | a72ff075923f |
| related-saas/src/db/connection.js | 5e16d48abee2 |
| related-saas/src/db/knexfile.js | 8f76c36a6796 |
| related-saas/src/db/migrations/20260301_001_create_users.js | c9599289e014 |
| related-saas/src/db/migrations/20260301_002_create_bookmarks.js | eda45b928466 |
| related-saas/src/db/migrations/20260301_003_create_sync_jobs.js | ea099b13af26 |
| related-saas/src/db/migrations/20260301_004_create_categories.js | 7539936c67fd |
| related-saas/src/db/migrations/20260306_005_make_tokens_nullable.js | 92cd4eafb6fd |
| related-saas/src/db/migrations/20260306_006_add_missing_indexes.js | 0c78239a0fdb |
| related-saas/src/index.js | 809d9dee6c7e |
| related-saas/src/lib/logger.js | a69c8a3abd00 |
| related-saas/src/lib/validate.js | 2ce876255044 |
| related-saas/src/middleware/rate-limit.js | 02d84d09cafc |
| related-saas/src/services/ai-categorizer.js | 18c6447065d7 |
| related-saas/src/web/views/dashboard.html | 36c48a831b64 |
| related-saas/src/web/views/login.html | 6e71526d47bc |
| related-saas/src/web/views/privacy.html | 9a4549c89cf0 |
| related-saas/src/web/views/terms.html | 8680d8a48cc3 |
| related-saas/src/workers/sync-worker.js | 41a539a49b62 |
| related-saas/src/workers/worker-manager.js | 8006031e8cbc |
| research/build_unified_resource_workbook.py | 2c110f3206c3 |
| scripts/content_extraction.py | a14cccdaffae |
| scripts/dashboard.py | 880f09c0c309 |
| scripts/dashboard_renderer.py | 3808cfaa4901 |
| scripts/decision_store.py | 5819e4b39b0b |
| scripts/enrich_tools.py | 3befb5664f75 |
| scripts/export_safety.py | 21f63eaa120f |
| scripts/fetch_bookmarks_api.py | 2ba437bc0e2e |
| scripts/generate_architecture.py | 51c0c958d0b2 |
| scripts/group_filter_loop.py | beb1111b1f42 |
| scripts/group_monitor.py | d1e2a35ee3c1 |
| scripts/http_guards.py | da5ec3af0810 |
| scripts/ingest_bookmarks.py | d5eca8292264 |
| scripts/json_filelock.py | 3564a6b445ae |
| scripts/learn_negatives.py | dec95a57254f |
| scripts/llm_provider.py | 31340c394127 |
| scripts/manage_group_filter_schedule.py | 632e0ed1fcc4 |
| scripts/manage_radar_server.py | 007277482a2d |
| scripts/notify_buttons.py | 7cf090507e05 |
| scripts/radar_backup.py | 5106bac71359 |
| scripts/radar_server.py | 8c8f49851ac8 |
| scripts/recommend_eligibility.py | b8cd8424c99b |
| scripts/resource_typing.py | 738f16be6212 |
| scripts/run_health.py | 19c1ab866a98 |
| scripts/run_pipeline.sh | f24e27571a4b |
| scripts/safe_fetch.py | c2108038162c |
| scripts/scheduler.py | 3d7164315894 |
| scripts/service.py | 683bd3236c37 |
| scripts/sync_scheduler.sh | c8a273c45841 |
| scripts/telegram_decisions.py | 9c580a6df6f9 |
| scripts/vps_config.py | cb673e2eb5e1 |
| scripts/x_api_auth.py | 82167b31d7ee |
| tests/lane06_browser/__init__.py | e3b0c44298fc |
| tests/lane06_browser/browser_checks.py | ba7386af43aa |
| tests/lane06_browser/fixture_server.py | 72bfdf386b6f |
| tests/lane06_browser/measure.py | 576407ddc149 |
| tests/lane06_browser/preview.py | 66463a622681 |
| tests/lane06_browser/real_server_check.py | 37080b4fe1b4 |
| tests/lane06_fixtures.py | 2f42e69757c2 |
| tests/test_architecture_refresh.py | 38cfe346df1b |
| tests/test_group_monitor.py | fa135d0155fa |
| tests/test_integration_e2e_lifecycle.py | 15f12b9496a6 |
| tests/test_lane01_extraction.py | 6e79e3687def |
| tests/test_lane01_ingestion.py | 926da4a323d7 |
| tests/test_lane02_persistence.py | 8a8060d8de08 |
| tests/test_lane02_telegram.py | fee0eea6e3fa |
| tests/test_lane03_backup_restore.py | e04eacb5415e |
| tests/test_lane03_health_backoff.py | 910a2417ab09 |
| tests/test_lane03_real_storage.py | 91f3521ac974 |
| tests/test_lane04_eligibility.py | 6dd269f3feb7 |
| tests/test_lane04_provider_contract.py | bbcd693228ec |
| tests/test_lane05_export_safety.py | 7224bbaa9d5f |
| tests/test_lane05_http_adversarial.py | 4516d2e048d9 |
| tests/test_lane05_safe_fetch.py | 9b50fa9a846a |
| tests/test_lane06_dashboard.py | 80e697af9576 |

