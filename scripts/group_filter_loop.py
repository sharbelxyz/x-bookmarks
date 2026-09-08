#!/usr/bin/env python3
"""Bounded autonomous workflow for the private-group resource filter."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
import group_monitor as monitor
import run_health


CODEX_CANDIDATES = [
    Path.home() / "Library" / "Application Support" / "com.conductor.app" / "bin" / "codex",
    Path("/opt/homebrew/bin/codex"),
    Path("/usr/local/bin/codex"),
]
CODEX = next((path for path in CODEX_CANDIDATES if path.exists()), CODEX_CANDIDATES[0])
SCHEMA = ROOT / "config" / "group-filter-decisions.schema.json"
SPEC = ROOT / "group-share-filter.loop.json"
LOOPSMITH_RECORD = (
    Path.home() / ".codex" / "skills" / "loopsmith" / "scripts" / "loopsmith-run.py"
)
RUN_JOURNAL = monitor.DATA_DIR / "autonomous-runs.jsonl"
MAX_BATCHES = 4
MAX_SUPERVISED_BATCHES = 20
REVIEW_BATCH_SIZE = 20
MAX_DURATION_SECONDS = 28 * 60
# The soft deadline above is only consulted between batches, so a call that
# blocks forever in the middle of a stage would never reach it — and because the
# worker lock is an flock held by this process, every later cron run would be
# refused for as long as this one hangs. SIGALRM fires no matter where the
# process is blocked, so the run always ends and always releases the lock.
HARD_DEADLINE_SECONDS = MAX_DURATION_SECONDS + 90
MAX_REVIEW_IMAGES = 20
# Enrichment runs every cycle but stays small: ~1.5 s per repo, so 20 repos is
# about 30 s against a 28-minute budget, and the backlog drains steadily.
ENRICH_LIMIT_PER_RUN = 20
ENRICH_BUDGET_SECONDS = 90
MAX_IMAGE_BYTES = 8 * 1024 * 1024
TRUSTED_IMAGE_HOSTS = {"pbs.twimg.com", "video.twimg.com"}


class StuckRun(RuntimeError):
    pass


def arm_hard_deadline(seconds=HARD_DEADLINE_SECONDS):
    """Guarantee this process exits even if a stage blocks indefinitely.

    Returns a callable that disarms the alarm. On platforms without SIGALRM the
    guard is a no-op and the soft deadline remains the only limit.
    """
    if not hasattr(signal, "SIGALRM"):
        return lambda: None

    def _fire(_signum, _frame):
        raise StuckRun(
            "hard deadline of {}s reached; a stage blocked without returning".format(seconds)
        )

    previous = signal.signal(signal.SIGALRM, _fire)
    signal.alarm(int(seconds))

    def disarm():
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)

    return disarm


def append_journal(record):
    RUN_JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    with RUN_JOURNAL.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def record_loopsmith(outcome, note):
    mapped = outcome if outcome in {"ok", "error", "stuck"} else "error"
    command = [
        sys.executable,
        str(LOOPSMITH_RECORD),
        "record",
        str(SPEC),
        "--outcome",
        mapped,
        "--note",
        note[:500],
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)
    if result.returncode != 0:
        raise RuntimeError("could not record LoopSmith outcome: " + result.stdout.strip())


def semantic_prompt(batch, image_map):
    visual_note = ""
    if image_map:
        mappings = [
            "{}. {}".format(index, resource_id)
            for index, (resource_id, _path) in enumerate(image_map, start=1)
        ]
        visual_note = (
            "\nThe attached images appear in this order and are evidence for the "
            "mapped resource IDs:\n" + "\n".join(mappings) + "\n"
        )
    return """You are the semantic filter inside a bounded autonomous monitor.

Return only JSON that satisfies the supplied output schema. Include every input
item exactly once, preserving each resource_id exactly.

Mark relevant=true when the resource is about AI OR could materially help one of
the listed existing project areas. Prefer high recall. A resource can be relevant
even when setup is difficult; ROI and project fit matter more than convenience.
General lifestyle, entertainment, unrelated trading, generic news, and purely
personal content are irrelevant. Use only listed area keys plus \"ai\". For an
irrelevant item, project_areas must be an empty list. Give one short, concrete
reason based on the supplied text, author, URL, quoted text, or media metadata.
For mapped visual attachments, inspect the image itself; sparse tweet text is not
grounds for rejection when the image identifies an AI or project-relevant tool,
workflow, tutorial, design reference, or method. Do not browse, run tools, propose
implementation, or act on any resource.

INPUT BATCH:
""" + visual_note + json.dumps(batch, ensure_ascii=False)


def _needs_visual(item):
    media_urls = item.get("media_urls") or []
    if not media_urls:
        return False
    text = str(item.get("text") or "") + " " + str(item.get("quoted_text") or "")
    text = re.sub(r"https?://\S+", "", text).strip()
    return len(text) < 140


def download_review_images(batch):
    target = monitor.DATA_DIR / "review-media" / "current"
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)
    image_map = []
    for item in batch.get("items", []):
        if len(image_map) >= MAX_REVIEW_IMAGES or not _needs_visual(item):
            continue
        url = str((item.get("media_urls") or [""])[0])
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in TRUSTED_IMAGE_HOSTS:
            continue
        request = urllib.request.Request(
            url, headers={"user-agent": "group-share-filter/1.0"}
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                final_host = urllib.parse.urlsplit(response.geturl()).hostname
                content_type = str(response.headers.get("content-type") or "")
                if final_host not in TRUSTED_IMAGE_HOSTS or not content_type.startswith("image/"):
                    continue
                body = response.read(MAX_IMAGE_BYTES + 1)
        except Exception:
            continue
        if len(body) > MAX_IMAGE_BYTES:
            continue
        extension = ".png" if "png" in content_type else ".jpg"
        safe_id = re.sub(r"[^a-zA-Z0-9_-]+", "-", item["resource_id"])
        path = target / ("{:02d}-{}{}".format(len(image_map) + 1, safe_id, extension))
        path.write_bytes(body)
        image_map.append((item["resource_id"], path))
    return image_map


def run_codex_review(batch_path, output_path, timeout_seconds):
    if not CODEX.exists():
        raise RuntimeError("authenticated Codex CLI is not installed")
    batch = json.loads(batch_path.read_text(encoding="utf-8"))
    expected_ids = [item["resource_id"] for item in batch.get("items", [])]
    image_map = download_review_images(batch)
    command = [
        str(CODEX),
        "exec",
        "-C",
        str(ROOT),
        "-s",
        "read-only",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "-c",
        "model_reasoning_effort=low",
        "--output-schema",
        str(SCHEMA),
        "-o",
        str(output_path),
    ]
    for _resource_id, image_path in image_map:
        command.extend(["-i", str(image_path)])
    command.append("-")
    result = subprocess.run(
        command,
        input=semantic_prompt(batch, image_map),
        capture_output=True,
        text=True,
        timeout=max(60, timeout_seconds),
        check=False,
        env={**os.environ, "NO_COLOR": "1"},
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "Codex semantic review failed")[-1000:]
        raise RuntimeError(detail.strip())
    decisions = json.loads(output_path.read_text(encoding="utf-8"))
    actual_ids = [item.get("resource_id") for item in decisions.get("decisions", [])]
    if len(actual_ids) != len(set(actual_ids)):
        raise RuntimeError("Codex returned duplicate resource decisions")
    if set(actual_ids) != set(expected_ids) or len(actual_ids) != len(expected_ids):
        raise RuntimeError("Codex decision IDs do not exactly match the review batch")
    return decisions


def refresh_architecture():
    """Documentation failure must not change the scanner's recorded result."""
    try:
        result = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "generate_architecture.py"), "--refresh"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=30, check=False,
        )
        return {"refreshed": result.returncode == 0, "detail": (result.stderr or result.stdout)[-400:]}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"refreshed": False, "detail": str(exc)[:400]}


def _stage(health, name, state, detail=None, summary=None):
    """Record a stage observation. A health-recording failure must never break
    the pipeline it describes, so problems are noted in the summary instead."""
    try:
        health.update_stage(name, state, detail=detail)
    except Exception as exc:  # noqa: BLE001
        if summary is not None:
            summary.setdefault("health_errors", []).append(
                "{}: {}".format(name, str(exc)[:120])
            )


def _alert_quiet(kind, domain, message):
    """Queue a deduplicated dry-run alert; alerting must never fail the run."""
    try:
        run_health.prepare_alert(monitor.DATA_DIR, kind, domain, message)
    except Exception:  # noqa: BLE001
        pass


BACKUP_POLICY_PATH = ROOT / "config" / "backup-policy.json"


def _run_backup_policy(outcome):
    """Bounded backup after a verified-good run, gated by authored config.

    Implemented-but-not-activated by design: without an operator-authored
    config/backup-policy.json ({"enabled": true, ...}) nothing runs, and cron
    is never touched — backups ride inside the existing scheduled run. The
    result is reported beside the run outcome and never mutates it.
    """
    try:
        policy = json.loads(BACKUP_POLICY_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"enabled": False}
    if not isinstance(policy, dict) or not policy.get("enabled"):
        return {"enabled": False}
    if outcome != "ok":
        return {"enabled": True, "skipped": "run outcome was " + outcome}
    interval = float(policy.get("interval_hours", 24)) * 3600
    max_seconds = min(600, int(policy.get("max_seconds", 120)))
    dest = Path(policy.get("dest") or (monitor.DATA_DIR / "backups"))
    latest = dest / "LATEST"
    try:
        latest_id = latest.read_text(encoding="utf-8").strip()
        manifest = json.loads(
            (dest / latest_id / "backup-manifest.json").read_text(encoding="utf-8")
        )
        created = run_health.parse_iso(manifest.get("created_at"))
        if created is not None and (time.time() - created.timestamp()) < interval:
            return {"enabled": True, "skipped": "fresh backup exists",
                    "latest": latest_id}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    try:
        result = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "radar_backup.py"),
             "--root", str(ROOT), "create", "--dest", str(dest)],
            capture_output=True, text=True, timeout=max_seconds, check=False,
        )
        report = {"enabled": True, "returncode": result.returncode,
                  "detail": (result.stdout or result.stderr)[-400:]}
    except (OSError, subprocess.TimeoutExpired) as exc:
        report = {"enabled": True, "returncode": -1, "detail": str(exc)[:400]}
    if report.get("returncode") == 0 and policy.get("restore_check"):
        try:
            latest_id = latest.read_text(encoding="utf-8").strip()
            check = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "radar_backup.py"),
                 "--root", str(ROOT), "check", str(dest / latest_id)],
                capture_output=True, text=True, timeout=max_seconds, check=False,
            )
            report["restore_check"] = {"returncode": check.returncode,
                                       "detail": (check.stdout or check.stderr)[-300:]}
        except (OSError, subprocess.TimeoutExpired) as exc:
            report["restore_check"] = {"returncode": -1, "detail": str(exc)[:300]}
    return report


def run_workflow(no_record=False, no_notify=False, max_batches=MAX_BATCHES):
    if max_batches < 1 or max_batches > MAX_SUPERVISED_BATCHES:
        raise ValueError(
            "max_batches must be between 1 and {}".format(MAX_SUPERVISED_BATCHES)
        )
    refusal = run_health.restore_block(monitor.DATA_DIR)
    if refusal:
        # A restored data directory never scans or notifies until an operator
        # reviews it; refusing here beats silently re-running on restored state.
        print("refusing to run: " + refusal, file=sys.stderr)
        run_health.append_event(
            monitor.DATA_DIR, {"kind": "run_refused_restored_dir"}
        )
        return 3
    started_at = monitor.utc_now()
    deadline = time.monotonic() + MAX_DURATION_SECONDS
    disarm_hard_deadline = arm_hard_deadline()
    health = run_health.StageHealthStore(monitor.DATA_DIR)
    backoff = run_health.BackoffStore(monitor.DATA_DIR)
    summary = {"started_at": started_at, "review_batches": []}
    outcome = "error"
    note = "workflow did not finish"
    semantic_success_at = None
    review_blocked = None
    conn = monitor.connect_db()
    try:
        profile = monitor.load_profile()
        with monitor.exclusive_run_lock():
            scope_replay = (
                monitor.get_meta(conn, "capture_scope_version")
                != monitor.CAPTURE_SCOPE_VERSION
            )
            # Capture and live bookmarks share the X backend, so they share
            # one backoff domain. An open circuit skips them — while every
            # stage that does not need X still runs below.
            capture_gate = backoff.check("capture")
            sync_details, sync_ok = None, None
            if not capture_gate["allowed"]:
                summary["sync"] = {
                    "skipped": "capture backoff until {}".format(capture_gate["until"])
                }
                _stage(
                    health,
                    "capture",
                    "auth_required" if capture_gate["failure_class"] == "auth" else "degraded",
                    detail="skipped: {} (until {})".format(
                        capture_gate["reason"], capture_gate["until"]
                    ),
                    summary=summary,
                )
            else:
                try:
                    sync_details, sync_ok = monitor.sync_once(
                        conn,
                        profile,
                        max_pages=50,
                        max_hydrate=60,
                        concurrency=2,
                        since_override=(
                            str(profile["bootstrap"]["resume_after_message_id"])
                            if scope_replay
                            else None
                        ),
                    )
                except Exception as exc:
                    failure_class = run_health.classify_failure(str(exc))
                    backoff.record_failure("capture", failure_class, str(exc))
                    _stage(
                        health,
                        "capture",
                        "auth_required" if failure_class == "auth" else "failed",
                        detail=str(exc),
                        summary=summary,
                    )
                    raise
                summary["scope_replay"] = scope_replay
                summary["sync"] = sync_details

            # Refresh the objective facts behind the review queue. Strictly
            # bounded and best-effort: enrichment is a convenience, so a GitHub
            # outage must never fail an otherwise-good run.
            # Collect any decisions tapped in Telegram since the last pass, and
            # pull in new bookmarks. Both bounded and best-effort: neither may
            # fail an otherwise-good run. One cheap bounded attempt per run
            # cannot retry-storm, so these carry stage states but no circuit.
            try:
                import telegram_decisions

                summary["telegram"] = telegram_decisions.pull()
                _stage(health, "decision_sync", "ok", summary=summary)
            except Exception as exc:  # noqa: BLE001
                summary["telegram"] = {"error": str(exc)[:200]}
                telegram_class = run_health.classify_failure(str(exc))
                _stage(
                    health,
                    "decision_sync",
                    "auth_required" if telegram_class == "auth" else "failed",
                    detail=str(exc),
                    summary=summary,
                )

            if capture_gate["allowed"]:
                try:
                    import ingest_bookmarks

                    summary["bookmarks"] = ingest_bookmarks.ingest_live(conn, profile)
                except Exception as exc:  # noqa: BLE001
                    summary["bookmarks"] = {"error": str(exc)[:200]}
            else:
                summary["bookmarks"] = {"skipped": "capture backoff"}

            try:
                import enrich_tools

                records = [
                    monitor.resource_to_dict(row) for row in monitor.select_resource_rows(conn)
                ]
                summary["enrichment"] = enrich_tools.enrich(
                    monitor.build_tool_index(records, monitor.load_verdicts()),
                    limit=ENRICH_LIMIT_PER_RUN,
                    budget_seconds=ENRICH_BUDGET_SECONDS,
                )
            except Exception as exc:  # noqa: BLE001 - never fail the run for this
                summary["enrichment"] = {"error": str(exc)[:200]}

            if capture_gate["allowed"]:
                if not sync_ok:
                    fetch_error = monitor.get_meta(conn, "last_fetch_error", "") or ""
                    failure_class = run_health.classify_failure(
                        fetch_error or "capture did not reach the durable checkpoint"
                    )
                    backoff.record_failure(
                        "capture",
                        failure_class,
                        fetch_error or "capture did not reach the durable checkpoint",
                    )
                    if failure_class == "auth":
                        capture_state = "auth_required"
                    elif failure_class == "capacity":
                        # More backlog than one bounded run can drain: real
                        # progress was still persisted, so this is degraded,
                        # not failed, and it never opens a circuit.
                        capture_state = "degraded"
                    else:
                        capture_state = "failed"
                    _stage(
                        health,
                        "capture",
                        capture_state,
                        detail=fetch_error or "did not reach the durable checkpoint",
                        summary=summary,
                    )
                    raise RuntimeError("capture did not reach the durable checkpoint")
                if backoff.record_success("capture").get("recovered"):
                    _stage(health, "capture", "recovering",
                           detail="first success after backoff", summary=summary)
                    _alert_quiet("recovered", "capture", "capture recovered")
                else:
                    _stage(health, "capture", "ok", summary=summary)
                # sync_once exported fresh artifacts as part of the pass above.
                _stage(health, "export", "ok", summary=summary)

                hydration = sync_details["hydration"]
                repeats = int(monitor.get_meta(conn, "hydration_failure_repeats", "0") or 0)
                if hydration["attempted"] and not hydration["hydrated"] and hydration["failed"]:
                    repeats += 1
                elif hydration["hydrated"] or not conn.execute(
                    "SELECT 1 FROM resources WHERE status = 'pending_hydration' LIMIT 1"
                ).fetchone():
                    repeats = 0
                with conn:
                    monitor.set_meta(conn, "hydration_failure_repeats", repeats)
                summary["hydration_failure_repeats"] = repeats
                if repeats >= 3:
                    _stage(
                        health,
                        "hydration",
                        "failed",
                        detail="three consecutive due hydration batches failed",
                        summary=summary,
                    )
                    raise StuckRun(
                        "three consecutive due hydration batches failed without one success"
                    )
                _stage(
                    health,
                    "hydration",
                    "degraded" if repeats else "ok",
                    detail=(
                        "{} consecutive due-batch failures".format(repeats)
                        if repeats
                        else None
                    ),
                    summary=summary,
                )

            for batch_number in range(1, max_batches + 1):
                batch_path = monitor.DATA_DIR / "review-batch.json"
                batch_info = monitor.prepare_review_batch(
                    conn, profile, REVIEW_BATCH_SIZE, batch_path
                )
                if batch_info["count"] == 0:
                    break
                review_gate = backoff.check("semantic_review")
                if not review_gate["allowed"]:
                    review_blocked = {
                        "class": review_gate["failure_class"] or "transient",
                        "detail": "circuit open ({}) until {}".format(
                            review_gate["reason"], review_gate["until"]
                        ),
                        "skipped": True,
                    }
                    _stage(
                        health,
                        "semantic_review",
                        "auth_required"
                        if review_gate["failure_class"] == "auth"
                        else "degraded",
                        detail="skipped: {} (until {})".format(
                            review_gate["reason"], review_gate["until"]
                        ),
                        summary=summary,
                    )
                    break
                remaining = int(deadline - time.monotonic())
                if remaining < 90:
                    outcome = "stuck"
                    note = "duration cap reached with semantic review pending"
                    break
                output_path = monitor.DATA_DIR / "decisions-current.json"
                try:
                    run_codex_review(batch_path, output_path, min(600, remaining - 30))
                except (RuntimeError, subprocess.TimeoutExpired) as exc:
                    failure_class = (
                        "timeout"
                        if isinstance(exc, subprocess.TimeoutExpired)
                        else run_health.classify_failure(str(exc))
                    )
                    backoff.record_failure("semantic_review", failure_class, str(exc))
                    review_blocked = {
                        "class": failure_class,
                        "detail": str(exc),
                        "skipped": False,
                    }
                    _stage(
                        health,
                        "semantic_review",
                        "auth_required" if failure_class == "auth" else "failed",
                        detail=str(exc),
                        summary=summary,
                    )
                    break
                applied = monitor.apply_decisions(conn, profile, output_path)
                if backoff.record_success("semantic_review").get("recovered"):
                    _stage(health, "semantic_review", "recovering",
                           detail="first success after backoff", summary=summary)
                    _alert_quiet("recovered", "semantic_review", "semantic review recovered")
                else:
                    _stage(health, "semantic_review", "ok", summary=summary)
                semantic_success_at = monitor.utc_now()
                summary["review_batches"].append(
                    {"batch": batch_number, "input": batch_info["count"], **applied}
                )

            pending = conn.execute(
                "SELECT COUNT(*) FROM resources WHERE status = 'pending_review'"
            ).fetchone()[0]
            if review_blocked:
                summary["review_blocked"] = {
                    "class": review_blocked["class"],
                    "detail": run_health.scrub_detail(review_blocked["detail"]),
                    "skipped": review_blocked["skipped"],
                }
                if review_blocked["skipped"] or review_blocked["class"] == "timeout":
                    outcome = "stuck"
                else:
                    outcome = "error"
                note = (
                    "semantic review {}: {} ({} pending); capture and decision "
                    "sync continued".format(
                        "skipped while circuit is open"
                        if review_blocked["skipped"]
                        else "failed",
                        review_blocked["class"],
                        pending,
                    )
                )
                summary["error"] = note
            elif pending:
                if outcome != "stuck":
                    outcome = "stuck"
                    note = "{} semantic-review resources remain after {} batches".format(
                        pending, max_batches
                    )
            else:
                summary["export"] = monitor.export_relevant(conn, profile)
                verification = monitor.verify(conn, strict=True)
                summary["verification"] = {
                    "pass": verification["pass"],
                    "problems": verification["problems"],
                }
                if not verification["pass"]:
                    _stage(
                        health,
                        "export",
                        "failed",
                        detail="strict verification failed: "
                        + "; ".join(verification["problems"]),
                        summary=summary,
                    )
                    raise RuntimeError("strict verification failed: " + "; ".join(verification["problems"]))
                _stage(health, "export", "ok", detail="verified", summary=summary)
                if no_notify:
                    summary["notification"] = {"skipped": True}
                    _stage(health, "notification", "ok",
                           detail="skipped (--no-notify)", summary=summary)
                else:
                    try:
                        summary["notification"] = monitor.notify_relevant(conn, live=True)
                    except Exception as exc:
                        _stage(health, "notification", "failed",
                               detail=str(exc), summary=summary)
                        raise
                    _stage(health, "notification", "ok", summary=summary)
                outcome = "ok"
                if capture_gate["allowed"]:
                    note = "strict gate passed"
                else:
                    note = "strict gate passed (capture skipped: circuit open)"
    except StuckRun as exc:
        outcome = "stuck"
        note = str(exc)
        summary["error"] = note
    except subprocess.TimeoutExpired:
        outcome = "stuck"
        note = "semantic review exceeded its timeout"
        summary["error"] = note
    except Exception as exc:
        outcome = "error"
        note = str(exc)
        summary["error"] = note
    finally:
        disarm_hard_deadline()
        # The main outcome is FINAL here. Nothing below may change it:
        # cleanup/reporting failures are recorded beside the result, never
        # over it (A03 — cleanup cannot conceal the run result).
        backlog_oldest = "__unchanged__"
        try:
            row = conn.execute(
                "SELECT MIN(first_seen_at) FROM resources "
                "WHERE status IN ('pending_review', 'pending_hydration')"
            ).fetchone()
            backlog_oldest = row[0] if row else None
        except Exception:  # noqa: BLE001
            pass
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        summary["finished_at"] = monitor.utc_now()
        summary["outcome"] = outcome
        summary["note"] = note
        if not no_record:
            try:
                record_loopsmith(outcome, note)
            except Exception as exc:
                summary["record_error"] = str(exc)
        try:
            health.record_run_result(
                outcome,
                started_at,
                summary["finished_at"],
                semantic_success_at=semantic_success_at,
                backlog_oldest_pending_at=backlog_oldest,
            )
        except Exception as exc:  # noqa: BLE001
            summary["health_record_error"] = str(exc)[:200]
        try:
            summary["backup"] = _run_backup_policy(outcome)
        except Exception as exc:  # noqa: BLE001
            summary["backup"] = {"enabled": True, "error": str(exc)[:200]}
        if outcome != "ok":
            _alert_quiet(
                "run_" + outcome,
                (review_blocked or {}).get("class") or "run",
                note,
            )
        try:
            append_journal(summary)
        except Exception as exc:  # noqa: BLE001
            print("journal write failed: {}".format(exc), file=sys.stderr)
        summary["architecture"] = refresh_architecture()
        try:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        except Exception:  # noqa: BLE001
            pass
    return 0 if outcome == "ok" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-record", action="store_true")
    parser.add_argument("--no-notify", action="store_true")
    parser.add_argument(
        "--max-batches",
        type=int,
        default=MAX_BATCHES,
        help="semantic batches for this run (default 4; supervised maximum 20)",
    )
    args = parser.parse_args()
    return run_workflow(
        no_record=args.no_record,
        no_notify=args.no_notify,
        max_batches=args.max_batches,
    )


if __name__ == "__main__":
    raise SystemExit(main())
