#!/usr/bin/env python3
"""Generate the c1 contract fixtures. All content is synthetic."""
import json
from pathlib import Path

FIX = Path(__file__).resolve().parent / "fixtures"
FIX.mkdir(exist_ok=True)


def write(name, doc):
    (FIX / name).write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")


RESOURCE_RECORD_KEYS = [
    "resource_id", "source", "kind", "url", "tweet_id", "message_id",
    "sender_id", "sender_username", "sender_display_name", "sender_avatar_url",
    "sender_is_owner", "shared_at", "share_count", "sharer_count", "sharers",
    "sharer_ids", "author", "title", "text", "status", "score",
    "project_areas", "reasons", "decision_source", "hydration_attempts",
    "last_error", "notified_at", "media_urls", "first_seen_at", "updated_at",
    "likes", "retweets", "replies", "tweet_created_at", "quoted_text",
    "external_urls", "external_label", "tool_keys", "verdict",
    "resource_type", "type_signals", "pick_score", "pick_parts",
]

write("c1-resource-record.json", {
    "contract": "C1", "revision": "c1",
    "required_keys": RESOURCE_RECORD_KEYS,
    "samples": {
        "group_tweet_relevant": {
            "resource_id": "tweet:1000000000000000001", "source": "group",
            "kind": "tweet", "url": "https://x.com/i/status/1000000000000000001",
            "tweet_id": "1000000000000000001", "message_id": "3000000000000000002",
            "sender_id": "920000000000000001", "sender_username": "member1",
            "sender_display_name": "Member One", "sender_avatar_url": "",
            "sender_is_owner": False, "shared_at": "2026-09-01T10:00:00+00:00",
            "share_count": 2, "sharer_count": 2,
            "sharers": ["member1", "owner1"],
            "sharer_ids": ["920000000000000001", "910000000000000001"],
            "author": "toolmaker", "title": "Synthetic tool announcement",
            "text": "Try github.com/example/synthetic-cli for agent workflows",
            "status": "relevant", "score": 6, "project_areas": ["ai"],
            "reasons": ["AI tool for agents"], "decision_source": "rules",
            "hydration_attempts": 1, "last_error": None, "notified_at": None,
            "media_urls": [], "first_seen_at": "2026-09-01T10:00:05+00:00",
            "updated_at": "2026-09-01T10:30:00+00:00", "likes": 10,
            "retweets": 2, "replies": 1,
            "tweet_created_at": "2026-09-01T09:59:00+00:00", "quoted_text": "",
            "external_urls": ["https://github.com/example/synthetic-cli"],
            "external_label": "github.com/example/synthetic-cli",
            "tool_keys": ["github.com/example/synthetic-cli"], "verdict": None,
            "resource_type": "try", "type_signals": ["github"],
            "pick_score": 5.5, "pick_parts": {"fit": 3, "engagement": 0.5},
        },
        "bookmark_overlap_target": {
            "note": "A01 target: same tweet seen as bookmark AND group share — one resource, both occurrences",
            "resource_id": "tweet:1000000000000000002", "source": "bookmark",
            "message_ids_expected": ["bookmark:1000000000000000002", "3000000000000000009"],
            "share_count": 2,
        },
        "note_resource": {"resource_id": "note:3000000000000000003", "kind": "note",
                          "url": "", "tweet_id": "", "status": "relevant"},
        "url_resource": {"resource_id": "url:0f6ff54f5d20b02dd2c1f371", "kind": "url",
                         "url": "https://example.com/article", "status": "pending_review"},
        "unavailable": {"resource_id": "tweet:1000000000000000004", "status": "unavailable",
                        "hydration_attempts": 3, "last_error": "not found",
                        "note": "unreadable is not proven deleted and not irrelevant"},
        "media_only_target": {
            "note": "A04 target: media-only message must persist with explicit processing state",
            "message": {"id": "3000000000000000005", "time": 1756800000000,
                        "sender_id": "920000000000000001", "text": "", "urls": [],
                        "attachment": {"photo": {"url": "https://pbs.twimg.com/media/synthetic.jpg"}}},
            "current_behavior": "extract_resources returns [] — the message vanishes behind the cursor",
            "target": "a durable resource with explicit supported/awaiting-extraction/unsupported/failed evidence state",
        },
    },
    "statuses": ["pending_hydration", "pending_review", "relevant", "irrelevant", "unavailable"],
})

write("c2-verdict-request.json", {
    "contract": "C2", "revision": "c1",
    "endpoint": "POST /api/verdict", "header": {"X-Radar-Action": "verdict"},
    "success": {"key": "github.com/example/synthetic-cli", "name": "synthetic-cli",
                "verdict": "must_try", "resource_type": "try",
                "what": "a runnable CLI", "why": "fits agent work",
                "first_step": "run --help", "lane": "agents",
                "stars": 1234, "license": "MIT", "last_push": "2026-09-01"},
    "clear": {"key": "github.com/example/synthetic-cli", "verdict": "clear"},
    "invalid_verdict": {"key": "github.com/example/x", "verdict": "love_it",
                        "expect": {"status": 400, "error": True}},
    "invalid_key": {"key": "nokey", "verdict": "must_try",
                    "expect": {"status": 400, "error": True}},
    "type_conflict": {"key": "github.com/example/an-article", "verdict": "must_try",
                      "resource_type": "read",
                      "expect": {"status": 409, "fields": ["error", "hint"]}},
    "allowed_verdicts": ["must_try", "must_read", "excluded", "already_have"],
})

write("c2-verdict-response.json", {
    "contract": "C2", "revision": "c1",
    "success": {"ok": True, "action": "added",
                "key": "github.com/example/synthetic-cli", "verdict": "must_try",
                "total": 30, "note": "Takes effect in the dashboard after the next export (within 30 minutes) or Scan now."},
    "actions": ["added", "replaced", "cleared", "not present"],
    "additive_target_fields": ["revision", "record"],
})

write("c2-outcome-request.json", {
    "contract": "C2", "revision": "c1", "endpoint": "POST /api/outcome",
    "header": {"X-Radar-Action": "outcome"},
    "success": {"key": "github.com/example/synthetic-cli", "name": "synthetic-cli",
                "state": "kept", "note": "saved 20 min per briefing vs manual flow"},
    "states": ["trying", "kept", "dropped", "clear"],
    "additive_target_fields": ["project", "artifact", "success_measure",
                               "baseline", "observed_result", "units",
                               "evidence", "trial_date"],
    "response_success": {"ok": True, "action": "added",
                         "key": "github.com/example/synthetic-cli",
                         "state": "kept", "total": 1},
})

write("c2-verdicts-file.json", {
    "contract": "C2", "revision": "c1",
    "document": {"version": 1, "updated_at": "2026-09-06", "verdicts": [
        {"key": "github.com/example/synthetic-cli", "name": "synthetic-cli",
         "verdict": "must_try", "why": "…", "what": "…", "first_step": "…",
         "lane": "agents", "reason_code": "", "resource_type": "try",
         "decided_at": "2026-09-06T00:00:00+00:00", "decided_by": "dashboard",
         "stars": 1234, "license": "MIT", "last_push": "2026-09-01", "rank": 1}]},
    "decided_by_values_frozen": ["dashboard"],
    "decided_by_values_additive": ["telegram", "agent-session"],
})

write("c2-outcomes-file.json", {
    "contract": "C2", "revision": "c1",
    "document": {"version": 1, "updated_at": "2026-09-06", "outcomes": [
        {"key": "github.com/example/synthetic-cli", "name": "synthetic-cli",
         "state": "kept", "note": "…", "decided_at": "2026-09-06T00:00:00+00:00",
         "decided_by": "dashboard"}]},
})

write("c2-telegram.json", {
    "contract": "C2", "revision": "c1",
    "decision_line": {"id": "a1b2c3d4", "action": "y",
                      "at": "2026-09-06T12:00:00+00:00"},
    "action_to_verdict": {"y": "must_try", "n": "excluded"},
    "offset_file_current": {"offset": 12345, "checked_at": "2026-09-06T12:00:00+00:00"},
    "offset_file_target_additive": {"log_identity": "<inode-or-first-line-hash>",
                                    "consumed_ids": ["a1b2c3d4"]},
    "pending_entry": {"a1b2c3d4": {"key": "github.com/example/synthetic-cli",
                                   "name": "synthetic-cli",
                                   "offered_at": "2026-09-05T10:00:00+00:00"}},
    "callback_data": "rdr:a1b2c3d4:y",
})

write("c3-storage-manifest.json", {
    "contract": "C3", "revision": "c1", "manifest_version": 1,
    "recovery_set": [
        {"store": "data/group-monitor/group-monitor.sqlite3",
         "kind": "sqlite", "writers": ["group_monitor (scanner)", "ingest_bookmarks"],
         "backup": "sqlite backup API / VACUUM INTO only; never a live file copy"},
        {"store": "config/verdicts.json", "kind": "authored-json",
         "writers": ["radar_server.record_verdict (dashboard + telegram pull)"]},
        {"store": "config/outcomes.json", "kind": "authored-json",
         "writers": ["radar_server.record_outcome"]},
        {"store": "config/group-filter-profile.json", "kind": "authored-json",
         "writers": ["radar_server.record_negative_term"]},
        {"store": "data/group-monitor/telegram-offset.json", "kind": "checkpoint",
         "writers": ["telegram_decisions.pull"]},
        {"store": "data/group-monitor/pending-decisions.json", "kind": "checkpoint",
         "writers": ["telegram_decisions.register_pending"]},
        {"store": "data/group-monitor/autonomous-runs.jsonl", "kind": "append-journal",
         "writers": ["group_filter_loop.append_journal"]},
        {"store": "sqlite:metadata", "kind": "kv",
         "keys": ["fetch_cursor", "fetch_incomplete", "capture_scope_version",
                  "hydration_failure_repeats", "bootstrap_evidence"]},
    ],
    "regenerable": ["dashboard.html", "dashboard-data.json", "status.json",
                    "relevant.csv", "relevant.jsonl", "all-resources.csv",
                    "latest.md", "unavailable.jsonl", "verification.json",
                    "tool-meta.json", "ARCHITECTURE.md"],
    "lock_order": ["data/group-monitor/worker.lock (flock)", "sqlite connection",
                   "verdicts.json lock", "outcomes.json lock", "profile lock",
                   "checkpoint files"],
    "consistency_rule": "capture all recovery-set members while holding worker.lock, or use the SQLite backup API plus post-copy re-hash of the JSON set; record a logical revision id + per-file hashes in the backup manifest",
    "restore_defaults": {"target": "new directory", "notifications": "disabled",
                         "scanning": "disabled", "permissions": "0700/0600"},
})

write("c4-health.json", {
    "contract": "C4", "revision": "c1", "endpoint": "GET /api/health",
    "frozen_fields": ["service", "ok", "now", "server_started_at", "pid",
                      "status_updated_at", "status_error", "age_seconds",
                      "stale", "stale_after_seconds", "gate_ready", "resources",
                      "status_counts", "dashboard_modified_at",
                      "dashboard_data_modified_at", "next_run_at", "cron_minutes"],
    "sample": {"service": "group-radar", "ok": True,
               "now": "2026-09-06T17:00:00+00:00",
               "server_started_at": "2026-09-06T10:00:00+00:00", "pid": 12345,
               "status_updated_at": "2026-09-06T16:47:30+00:00",
               "status_error": "", "age_seconds": 750, "stale": False,
               "stale_after_seconds": 5400, "gate_ready": True,
               "resources": 100, "status_counts": {"pending_hydration": 0,
               "pending_review": 0, "relevant": 40, "irrelevant": 55,
               "unavailable": 5}, "dashboard_modified_at": "2026-09-06T16:47:31+00:00",
               "dashboard_data_modified_at": "2026-09-06T16:47:31+00:00",
               "next_run_at": "2026-09-06T20:17:00+03:00", "cron_minutes": [17, 47]},
})

write("c4-health-extended.json", {
    "contract": "C4", "revision": "c1", "status": "target (A03), additive",
    "stages_states": ["ok", "degraded", "failed", "auth_required", "recovering", "unknown"],
    "stage_names": ["capture", "hydration", "semantic_review", "decision_sync",
                    "notification", "backup", "export"],
    "sample_additive_block": {
        "last_run_outcome": "error", "last_run_at": "2026-09-06T16:47:00+00:00",
        "last_semantic_success_at": "2026-09-05T22:17:00+00:00",
        "auth_required": True, "backlog_age_seconds": 66000,
        "backoff": {"active": True, "until": "2026-09-06T18:47:00+00:00",
                    "reason": "model HTTP 401 x3"},
        "stages": {"capture": {"state": "ok", "at": "2026-09-06T16:47:10+00:00"},
                   "semantic_review": {"state": "auth_required",
                                       "at": "2026-09-06T16:47:20+00:00",
                                       "detail": "model HTTP 401"},
                   "decision_sync": {"state": "ok", "at": "2026-09-06T16:47:12+00:00"}}},
    "rules": ["an old passing gate must not overwrite a newer failure",
              "viewer liveness independent of stage health",
              "no credentials or secret material in any field"],
})

write("c4-status-snapshot.json", {
    "contract": "C4", "revision": "c1",
    "frozen_fields": ["updated_at", "fetch_cursor", "fetch_incomplete",
                      "last_fetch_at", "last_fetch_error", "messages_captured",
                      "owner_messages_captured", "non_owner_messages_captured",
                      "senders_captured", "resource_occurrences",
                      "capture_scope_version", "resources", "status_counts",
                      "unattempted_hydration", "gate_ready"],
})

write("c4-journal-entry.json", {
    "contract": "C4", "revision": "c1",
    "outcomes": ["ok", "error", "stuck"],
    "fields_observed": ["started_at", "finished_at", "outcome", "review_batches",
                        "scope_replay", "sync", "telegram", "bookmarks",
                        "enrichment", "hydration_failure_repeats", "export",
                        "verification", "notification", "error", "architecture"],
})

write("c5-tool-entry.json", {
    "contract": "C5", "revision": "c1",
    "frozen_keys": ["key", "name", "url", "label", "is_repo", "verdict", "rank",
                    "lane", "what", "why", "first_step", "reason_code", "stars",
                    "license", "last_push", "mentions", "resource_ids",
                    "best_score", "latest_share", "auto", "facts", "meta_loaded",
                    "outcome", "outcome_note", "outcome_at", "resource_type"],
    "verdict_values": ["must_try", "must_read", "excluded", "already_have", "unreviewed"],
    "auto_gate_reason_codes": ["gone", "archived", "empty", "stale", "tiny"],
})

write("c5-tool-meta-entry.json", {
    "contract": "C5", "revision": "c1",
    "ok_sample": {"slug": "example/synthetic-cli",
                  "fetched_at": "2026-09-06T12:00:00+00:00", "ok": True,
                  "description": "synthetic", "stars": 1234, "forks": 56,
                  "pushed_at": "2026-09-01", "created_at": "2025-01-01",
                  "archived": False, "is_fork": False, "is_empty": False,
                  "language": "Python", "homepage": "", "topics": ["agents"],
                  "license": "MIT"},
    "missing_sample": {"slug": "example/gone", "fetched_at": "2026-09-06T12:00:00+00:00",
                       "ok": False, "error": "missing", "missing": True},
    "error_sample": {"slug": "example/timeout", "fetched_at": "2026-09-06T12:00:00+00:00",
                     "ok": False, "error": "timeout"},
})

write("c5-eligibility-entry.json", {
    "contract": "C5", "revision": "c1", "status": "target (A09), additive",
    "lanes": ["review", "evidence_pending", "blocked"],
    "samples": {
        "github_tool_ready": {"review_eligibility": {"lane": "review", "reasons": [],
            "evidence": {"source_url": "https://github.com/example/synthetic-cli",
                         "checked_at": "2026-09-06", "extraction_state": "ok",
                         "confidence": "high"},
            "project_fit": {"project": "agents", "benefit": "faster briefing triage",
                            "first_step": "run --help on one export",
                            "success_measure": "one briefing produced in < 5 min"}}},
        "arabic_article_pending": {"review_eligibility": {"lane": "evidence_pending",
            "reasons": ["destination page not yet fetched"],
            "evidence": {"source_url": "https://example.com/ar/article",
                         "checked_at": None, "extraction_state": "pending",
                         "confidence": "low"}, "project_fit": None}},
        "blocked_unsafe": {"review_eligibility": {"lane": "blocked",
            "reasons": ["fetch denied: private_target"], "evidence": None,
            "project_fit": None}},
    },
    "rules": ["a failed fetch never yields invented facts/ROI",
              "missing stars is not a universal exclusion",
              "unknown Saudi eligibility stays unknown until checked"],
})

write("c6-safe-fetch.json", {
    "contract": "C6", "revision": "c1",
    "signature": "safe_fetch(url, *, max_bytes=4000000, timeout=20.0, max_redirects=4, allowed_content_types=(), dest_dir=None) -> dict",
    "result_keys": ["ok", "url", "final_url", "status", "content_type", "bytes",
                    "body_path", "text", "error", "denied_reason"],
    "denied_reasons": ["scheme", "private_target", "redirect_target", "too_large",
                       "timeout", "content_type", "provider_unavailable", "error"],
    "samples": {
        "success": {"ok": True, "url": "https://example.com/page",
                    "final_url": "https://example.com/page", "status": 200,
                    "content_type": "text/html; charset=utf-8", "bytes": 20480,
                    "body_path": None, "text": "<html>…</html>", "error": None,
                    "denied_reason": None},
        "denied_private": {"ok": False, "url": "http://169.254.169.254/meta",
                           "final_url": None, "status": None, "content_type": None,
                           "bytes": 0, "body_path": None, "text": None,
                           "error": None, "denied_reason": "private_target"},
        "oversize": {"ok": False, "denied_reason": "too_large", "bytes": 4000001},
        "stub_default": {"ok": False, "denied_reason": "provider_unavailable"},
    },
})

write("c7-http-routes.json", {
    "contract": "C7", "revision": "c1",
    "get_routes": ["/api/health", "/", "/dashboard.html", "/dashboard-data.json",
                   "/status.json", "/verification.json", "/relevant.csv",
                   "/all-resources.csv", "/relevant.jsonl", "/latest.md",
                   "/negative-proposals.json", "/favicon.ico"],
    "post_routes": {"/api/run": "run", "/api/verdict": "verdict",
                    "/api/outcome": "outcome", "/api/negative-term": "negative-term"},
    "action_header": "X-Radar-Action",
    "body_limits": {"min": 1, "max": 16384},
    "method_policy": "GET/HEAD on routes; POST on api routes; everything else 405",
    "bind": "loopback only; production port 8765 forbidden in lanes; tests bind port 0",
    "targets_a08": ["Host allowlist incl. port", "Origin/Referer policy for mutations",
                    "non-object JSON body -> controlled 400",
                    "content-type validation", "bounded read time",
                    "read-back route registration via 07"],
})

write("c7-csv-columns.json", {
    "contract": "C7", "revision": "c1",
    "relevant_csv": ["resource_id", "kind", "url", "message_id", "sender_id",
                     "sender_username", "shared_at", "share_count", "author",
                     "title", "text", "score", "project_areas", "reasons",
                     "decision_source", "first_seen_at", "resource_type",
                     "pick_score", "external_urls", "verdict", "verdict_why",
                     "outcome", "outcome_note"],
    "all_resources_csv": ["resource_id", "status", "kind", "url", "message_id",
                          "sender_id", "sender_username", "sender_display_name",
                          "shared_at", "share_count", "sharer_count", "sharers",
                          "author", "title", "text", "score", "project_areas",
                          "reasons", "decision_source", "hydration_attempts",
                          "last_error", "first_seen_at", "updated_at",
                          "resource_type", "pick_score", "external_urls"],
    "a07_note": "these raw exports keep original text; the human-safe spreadsheet export is a SEPARATE new artifact",
})

write("c3-migration-proposal.schema.json", {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "Additive migration proposal (submit to Chat 07)",
    "type": "object",
    "required": ["lane", "target_store", "ddl_or_change", "backfill",
                 "validation", "rollback"],
    "properties": {
        "lane": {"type": "string", "enum": ["01", "02", "03", "04", "05", "06"]},
        "target_store": {"type": "string"},
        "ddl_or_change": {"type": "string",
                          "description": "ALTER/CREATE statements or JSON field additions; additive only"},
        "backfill": {"type": "string"},
        "validation": {"type": "string"},
        "rollback": {"type": "string"},
        "depends_on": {"type": "array", "items": {"type": "string"}},
    },
})

print("fixtures written:", len(list(FIX.glob("*.json"))))
