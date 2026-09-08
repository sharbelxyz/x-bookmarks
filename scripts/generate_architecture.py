#!/usr/bin/env python3
"""Generate the canonical architecture from source, config and read-only state.

--check compares exact structure and numbers, ignoring only marked observations.
--refresh publishes only changed content, preserving the previous document.
"""
from __future__ import annotations

import argparse
import ast
import datetime as dt
import fcntl
import hashlib
import json
import operator
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
DATA_DIR = ROOT / "data" / "group-monitor"
DB_PATH = DATA_DIR / "group-monitor.sqlite3"
OUTPUT = ROOT / "ARCHITECTURE.md"
GUIDE = ROOT / "references" / "architecture-guide.md"
SCOPE_PATH = ROOT / "config" / "architecture-scope.json"
CRON_TAG = "loopsmith:group-share-filter"
LAUNCHD_LABEL = "com.mshrmnsr.group-radar-server"
SNAPSHOT_START = "<!-- runtime-snapshot:start -->"
SNAPSHOT_END = "<!-- runtime-snapshot:end -->"
ENTRY_POINTS = [
    ("group_filter_loop.py", "cron", "Bounded capture/classification worker"),
    ("radar_server.py", "launchd", "Viewer, decision APIs and architecture heartbeat"),
    ("group_monitor.py", "cli", "Pipeline stages, export and verification"),
    ("enrich_tools.py", "cli", "Repository evidence refresh"),
    ("ingest_bookmarks.py", "cli", "Live and historical bookmark import"),
    ("learn_negatives.py", "cli", "Propose negative terms; not a scheduled training stage"),
    ("telegram_decisions.py", "cli", "Pull and apply existing owner callbacks"),
    ("manage_radar_server.py", "cli", "Viewer lifecycle"),
    ("manage_group_filter_schedule.py", "cli", "Scanner lifecycle"),
    ("generate_architecture.py", "cli", "Canonical architecture maintenance"),
]


def read_json(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return {}


def run(command: List[str], timeout: int = 8) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
        return result.stdout if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def literal_constants(path: Path) -> Dict[str, Any]:
    """Restricted AST evaluation: never execute project imports or expressions."""
    values: Dict[str, Any] = {}
    operations = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}
    def evaluate(node: ast.AST) -> Any:
        if isinstance(node, ast.Name):
            return values[node.id]
        if isinstance(node, ast.BinOp) and type(node.op) in operations:
            return operations[type(node.op)](evaluate(node.left), evaluate(node.right))
        return ast.literal_eval(node)
    for node in tree(path).body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        for target in targets:
            if isinstance(target, ast.Name):
                try:
                    values[target.id] = evaluate(node.value)
                except (ValueError, TypeError, KeyError, AttributeError):
                    pass
    return values


def local_module_imports(path: Path) -> Set[str]:
    found = set()
    for node in ast.walk(tree(path)):
        names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module] if isinstance(node, ast.ImportFrom) and node.module else []
        found.update(name.split(".")[0] + ".py" for name in names if (SCRIPTS / (name.split(".")[0] + ".py")).exists())
    return found


def dependency_graph() -> Dict[str, Set[str]]:
    return {p.name: local_module_imports(p) for p in sorted(SCRIPTS.glob("*.py"))}


def reachable_from(seeds: Iterable[str], graph: Dict[str, Set[str]]) -> Set[str]:
    seen, queue = set(), list(seeds)
    while queue:
        name = queue.pop()
        if name not in seen and name in graph:
            seen.add(name)
            queue.extend(graph[name])
    return seen


def source_files() -> Dict[str, Path]:
    files = {}
    for pattern in ("scripts/**/*.py", "scripts/**/*.sh", "tests/**/*.py", "config/*.json", "references/**/*.md", "app/*.html", "app/*.js", ".github/workflows/*.yml", ".github/workflows/*.yaml"):
        for path in ROOT.glob(pattern):
            files[str(path.relative_to(ROOT))] = path
    for name in ("README.md", "GROUP_FILTER.md", "SKILL.md", "SERVICE_GUIDE.md", "build_app.py", "group-share-filter.loop.json", "group-share-filter.prompt.md", ".gitignore"):
        if (ROOT / name).is_file():
            files[name] = ROOT / name
    scope = read_json(SCOPE_PATH)
    workspace = Path(scope.get("workspace_root") or "/nonexistent")
    workbook = workspace / ".context/research/unified-sheet/build_unified_resource_workbook.py"
    if workbook.is_file():
        files["research/build_unified_resource_workbook.py"] = workbook
    related = Path(scope.get("related_saas_root") or "/nonexistent")
    for pattern in ("package.json", "src/**/*.js", "src/**/*.html", "extension/*.js", "extension/*.html", "extension/manifest.json"):
        for path in related.glob(pattern):
            files["related-saas/" + str(path.relative_to(related))] = path
    return dict(sorted(files.items()))


def source_revision() -> str:
    digest = hashlib.sha256()
    for name, path in source_files().items():
        digest.update(name.encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def server_routes() -> List[Dict[str, str]]:
    constants = literal_constants(SCRIPTS / "radar_server.py")
    routes = [{"path": path, "method": "GET, HEAD", "serves": value[0]} for path, value in constants.get("ROUTES", {}).items()]
    for node in ast.walk(tree(SCRIPTS / "radar_server.py")):
        if isinstance(node, ast.FunctionDef) and node.name in {"do_GET", "do_POST"}:
            found = {n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.startswith("/api/")}
            for path in sorted(found):
                if node.name == "do_POST":
                    routes.append({"path": path, "method": "POST", "serves": "Validated action; see handler source"})
                elif path == "/api/health":
                    routes.append({"path": path, "method": "GET, HEAD", "serves": "Viewer liveness, export age and queue readiness"})
    return sorted(routes, key=lambda r: (r["path"], r["method"]))


def db_facts() -> Dict[str, Any]:
    facts: Dict[str, Any] = {"present": False, "tables": [], "sources": [], "statuses": []}
    if not DB_PATH.exists():
        return facts
    conn = sqlite3.connect(DB_PATH.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
            name = row["name"]
            quoted = '"' + name.replace('"', '""') + '"'
            facts["tables"].append({
                "name": name,
                "rows": conn.execute("SELECT COUNT(*) FROM " + quoted).fetchone()[0],
                "columns": [dict(r) for r in conn.execute("PRAGMA table_info(" + quoted + ")")],
                "indexes": [dict(r) for r in conn.execute("PRAGMA index_list(" + quoted + ")")],
                "foreign_keys": [dict(r) for r in conn.execute("PRAGMA foreign_key_list(" + quoted + ")")],
            })
        facts["sources"] = [dict(r) for r in conn.execute("SELECT source,status,count(*) AS n FROM resources GROUP BY source,status ORDER BY source,status")]
        facts["statuses"] = [dict(r) for r in conn.execute("SELECT status,count(*) AS n FROM resources GROUP BY status ORDER BY status")]
        facts["present"] = True
        return facts
    finally:
        conn.close()


def schedule_facts() -> Dict[str, Any]:
    # Do not export unrelated cron lines, which can contain credentials.
    active = any("group_filter_loop.py" in line and not line.lstrip().startswith("#") for line in run(["crontab", "-l"]).splitlines())
    return {"scanner_installed": active, "viewer_loaded": LAUNCHD_LABEL in run(["launchctl", "list"])}


def test_facts() -> Dict[str, Any]:
    classes = {}
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        for node in tree(path).body:
            if isinstance(node, ast.ClassDef):
                names = [n.name for n in node.body if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")]
                if names:
                    classes[path.name + ":" + node.name] = names
    return {"count": sum(map(len, classes.values())), "classes": classes}


def call_inventory(filename: str, function: str) -> List[List[str]]:
    wanted = next(n for n in tree(SCRIPTS / filename).body if isinstance(n, ast.FunctionDef) and n.name == function)
    calls = []
    for node in sorted(ast.walk(wanted), key=lambda n: (getattr(n, "lineno", 0), getattr(n, "col_offset", 0))):
        if not isinstance(node, ast.Call):
            continue
        name = ast.unparse(node.func)
        if name.startswith(("monitor.", "telegram_decisions.", "ingest_bookmarks.", "enrich_tools.")) or name in {"run_codex_review", "refresh_architecture", "append_journal", "record_loopsmith", "fetch_group_messages", "persist_fetch", "hydrate_pending", "apply_rule_classification", "export_relevant", "status_snapshot"}:
            calls.append([str(node.lineno), name])
    return calls


def cell(value: Any) -> str:
    if isinstance(value, (dict, list, tuple, set)):
        value = json.dumps(value, ensure_ascii=True, sort_keys=True, default=sorted)
    return str(value).replace("|", "\\|").replace("\n", " ")


def table(headers: List[str], rows: Iterable[Iterable[Any]]) -> List[str]:
    return ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"] + ["| " + " | ".join(cell(v) for v in row) + " |" for row in rows] + [""]


def recent_runs() -> List[Dict[str, Any]]:
    path = DATA_DIR / "autonomous-runs.jsonl"
    if not path.exists():
        return []
    with path.open("rb") as handle:
        handle.seek(max(0, path.stat().st_size - 1024 * 1024))
        lines = handle.read().decode("utf-8", errors="replace").splitlines()
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
            if isinstance(row, dict) and "outcome" in row:
                rows.append(row)
        except ValueError:
            pass
    return rows[-48:]


def snapshot(db: Dict[str, Any]) -> List[str]:
    status = read_json(DATA_DIR / "status.json")
    dashboard = read_json(DATA_DIR / "dashboard-data.json")
    verification = read_json(DATA_DIR / "verification.json")
    runs = recent_runs()
    latest = runs[-1] if runs else {}
    stats = {}
    for row in runs:
        stats[row.get("outcome", "unknown")] = stats.get(row.get("outcome", "unknown"), 0) + 1
    tools = dashboard.get("tools", [])
    unreviewed = [t for t in tools if t.get("verdict") == "unreviewed"]
    lines = [SNAPSHOT_START, "## Live Snapshot", "",
             "Timestamped observations, not a guarantee of current health. Attempted pending hydration can coexist with a passing strict gate.", ""]
    lines += table(["Signal", "Observed"], [
        ["Status snapshot at", status.get("updated_at", "unavailable")],
        ["Capture scope / fetch incomplete", [status.get("capture_scope_version"), status.get("fetch_incomplete")]],
        ["Resources, all sources", status.get("resources", "unknown")],
        ["Status counts", status.get("status_counts", {})],
        ["Latest run", {k: latest.get(k) for k in ("started_at", "finished_at", "outcome")}],
        ["Last recorded strict check", {k: verification.get(k) for k in ("verified_at", "pass", "strict")}],
        ["Recent run outcomes (up to 48)", stats],
        ["Cron / launchd observation", schedule_facts()],
        ["Dashboard generated at", dashboard.get("generatedAt", "unavailable")],
        ["Group coverage, not whole ledger", dashboard.get("coverage", {})],
        ["Dashboard payload rows", len(dashboard.get("resources", []))],
        ["Indexed tools", len(tools)],
        ["Unreviewed / without usable repo facts", [len(unreviewed), sum(not (t.get("facts") or {}).get("ok") for t in unreviewed)]],
        ["Curated verdicts / recorded outcomes", [len(read_json(ROOT / "config/verdicts.json").get("verdicts", [])), len(read_json(ROOT / "config/outcomes.json").get("outcomes", []))]],
    ])
    lines += table(["Resource source", "State", "Rows"], ([r["source"], r["status"], r["n"]] for r in db["sources"]))
    lines += table(["Table", "Rows including synthetic bookmarks"], ([t["name"], t["rows"]] for t in db["tables"]))
    return lines + [SNAPSHOT_END, ""]


def build_document() -> str:
    revision = source_revision()
    scope = read_json(SCOPE_PATH)
    graph = dependency_graph()
    live = reachable_from([x[0] for x in ENTRY_POINTS], graph)
    db, tests = db_facts(), test_facts()
    lines = ["# Complete Architecture: Group Resource Radar", "",
             "> Generated at: " + dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
             "> Canonical file: " + str(OUTPUT),
             "> Source/config fingerprint: " + revision,
             "> Inventory is source-derived; explanatory prose is maintained in references/architecture-guide.md.",
             "> Refresh: viewer startup, every 60 seconds while running, and after scanner runs. Stopped/asleep services cannot refresh.",
             "> Drift checks do not prove every narrative claim or third-party service behavior. Review affected contracts after changes.", "",
             "## Contents", "", "- Live Snapshot", "- System Boundaries", "- Pipeline and Calls", "- Modules and Dependencies",
             "- HTTP and CLI Interfaces", "- Configuration and Constants", "- Database Schema", "- Test Inventory",
             "- Explanatory Guide: capture through operations", "- Source Inventory", ""]
    lines += snapshot(db)
    lines += ["## System Boundaries", ""]
    lines += table(["Surface", "Owner/location"], [
        ["Live scanner, server, SQLite and config", str(ROOT)],
        ["Durable resource ledger", str(DB_PATH)],
        ["Research, historical workbook and handoffs", scope.get("workspace_root", "not configured")],
        ["Related SaaS checkout, not this scanner deployment", scope.get("related_saas_root", "not configured")],
        ["Telegram callback", "Existing VPS Atlas bot; Mac pulls decisions. Remote deployment needs separate verification."],
        ["Canonical architecture", "This file; the workspace architecture path is a link, not a second independent document."],
    ])
    lines += ["## Pipeline and Calls", "", "Source order, not a promise every branch executes. Sync performs an initial export before final verification; newer output is not proof of a successful run.", ""]
    for filename, function in [("group_filter_loop.py", "run_workflow"), ("group_monitor.py", "sync_once")]:
        lines += ["### " + filename + " / " + function, ""] + table(["Source line", "Call"], call_inventory(filename, function))
    lines += ["## Modules and Dependencies", ""]
    kinds = {name: (kind, why) for name, kind, why in ENTRY_POINTS}
    lines += table(["File", "Role", "Local imports"], ([
        "scripts/" + name,
        (kinds[name][0] + ": " + kinds[name][1]) if name in kinds else "reachable dependency" if name in live else "outside radar graph; legacy/standalone",
        ", ".join(sorted(graph[name])) or "none",
    ] for name in sorted(graph)))
    lines += ["Static reachability is not scheduler proof. Legacy service.py remains a live X capture/bookmark dependency.", ""]
    lines += ["## Runtime Files", ""]
    lines += table(["Path", "Role"], [
        ["data/group-monitor/group-monitor.sqlite3", "Durable resources, occurrences, schema, cursor and run metadata"],
        ["data/group-monitor/dashboard.html", "Self-contained rendered UI"],
        ["data/group-monitor/dashboard-data.json", "JSON twin used for live updates; capped payload"],
        ["data/group-monitor/status.json", "Cursor, queue and export readiness snapshot"],
        ["data/group-monitor/verification.json", "Last persisted strict invariant check"],
        ["data/group-monitor/autonomous-runs.jsonl", "Run outcomes and stage details; inspect independently of viewer health"],
        ["data/group-monitor/relevant.csv, all-resources.csv", "Filtered and full spreadsheet exports; untrusted text caveat applies"],
        ["data/group-monitor/relevant.jsonl, unavailable.jsonl, latest.md", "Machine-readable records, retrieval failures and latest relevant summary"],
        ["data/group-monitor/tool-meta.json", "Last-good GitHub evidence cache"],
        ["data/group-monitor/pending-decisions.json, telegram-offset.json", "Callback ID map and remote-log checkpoint"],
        ["data/group-monitor/negative-proposals.json", "Candidate exclusion rules awaiting approval"],
        ["data/group-monitor/review-batch.json, decisions-current.json", "Most recent semantic batch and returned decisions"],
        ["data/group-monitor/worker.lock, cron.log, server.log", "Worker exclusion and local runtime logs"],
        ["config/group-filter-profile.json", "Group scope, project areas, relevance weights and approved negatives"],
        ["config/group-filter-decisions.schema.json", "Semantic output contract"],
        ["config/verdicts.json, config/outcomes.json", "Authored recommendations and separate trial results"],
        ["config/architecture-scope.json", "Explicit related checkout locations for architecture coverage"],
        ["references/architecture-guide.md", "Maintained explanatory source for this generated file"],
        ["group-share-filter.loop.json, group-share-filter.prompt.md", "Automation contract and operating instructions"],
        ["_versions/ARCHITECTURE.md.*.md", "Content-addressed previous architecture documents"],
    ])
    lines += ["## HTTP and CLI Interfaces", ""]
    lines += table(["Route", "Methods", "Handler/output"], ([r["path"], r["method"], r["serves"]] for r in server_routes()))
    cli_rows = []
    for filename, _, _ in ENTRY_POINTS:
        options = set()
        for node in ast.walk(tree(SCRIPTS / filename)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in {"add_argument", "add_parser"}:
                options.update(n.value for n in node.args if isinstance(n, ast.Constant) and isinstance(n.value, str))
        cli_rows.append([filename, ", ".join(sorted(options))])
    lines += table(["CLI", "Declared arguments/commands"], cli_rows)
    lines += ["## Configuration and Constants", "", "Restricted AST evaluation supplies these values; numeric descriptions are not duplicated.", ""]
    groups = {
        "group_filter_loop.py": ["MAX_BATCHES", "MAX_SUPERVISED_BATCHES", "REVIEW_BATCH_SIZE", "MAX_DURATION_SECONDS", "HARD_DEADLINE_SECONDS", "ENRICH_LIMIT_PER_RUN", "ENRICH_BUDGET_SECONDS", "MAX_REVIEW_IMAGES", "MAX_IMAGE_BYTES", "TRUSTED_IMAGE_HOSTS"],
        "resource_typing.py": ["FIT_PER_AREA", "FIT_AI_BONUS", "RESHARE_PER_SHARE", "RESHARE_PER_MEMBER", "REPO_LINK_BONUS", "ENGAGEMENT_CAP", "HEALTH_CAP", "STALE_HEALTH_PENALTY", "STALE_AFTER_DAYS", "RECENCY_HALF_LIFE_DAYS", "TYPE_BONUS", "VERDICT_BONUS", "VERDICT_FOR_TYPE"],
        "group_monitor.py": ["CAPTURE_SCOPE_VERSION", "VALID_STATUSES", "DASHBOARD_SCHEDULE", "DASHBOARD_BOOKMARK_CAP", "MAX_TOOLS_FOR_VERDICT_INHERITANCE", "AUTO_STALE_DAYS", "AUTO_MIN_STARS"],
        "radar_server.py": ["DEFAULT_HOST", "DEFAULT_PORT", "STALE_AFTER_SECONDS", "RUN_COOLDOWN_SECONDS", "MAX_FILE_BYTES", "ALLOWED_VERDICTS", "ALLOWED_OUTCOMES", "ARCHITECTURE_REFRESH_SECONDS"],
        "enrich_tools.py": ["TTL_DAYS", "PER_CALL_TIMEOUT", "DEFAULT_LIMIT", "DEFAULT_BUDGET_SECONDS"],
        "ingest_bookmarks.py": ["SOURCE_LIVE", "SOURCE_ARCHIVE", "LIVE_FETCH_LIMIT", "COMMIT_EVERY"],
        "telegram_decisions.py": ["CALLBACK_PREFIX", "CALLBACK_LIMIT", "ID_LENGTH", "SSH_TIMEOUT", "ACTION_TO_VERDICT", "PENDING_RETENTION_DAYS"],
        "learn_negatives.py": ["MIN_EXCLUDED_HITS", "MAX_RELEVANT_HITS", "MIN_LOG_ODDS", "MAX_PROPOSALS"],
    }
    for filename, names in groups.items():
        constants = literal_constants(SCRIPTS / filename)
        lines += ["### " + filename, ""] + table(["Constant", "Current value"], ([name, constants.get(name, "non-literal; inspect source")] for name in names))
    profile = read_json(ROOT / "config/group-filter-profile.json")
    selection = profile.get("selection", {})
    lines += ["### Active Project Profile", ""]
    lines += table(["Setting", "Value"], [
        ["capture_scope", profile.get("conversation", {}).get("capture_scope")],
        ["minimum_score / ai_weight", [selection.get("minimum_score"), selection.get("ai_weight")]],
        ["Configured bookmark accounts", len(profile.get("owners", []))],
        ["AI terms / approved negative terms", [len(selection.get("ai_terms", [])), len(selection.get("negative_terms", []))]],
    ])
    lines += table(["Project area", "Label", "Weight", "Keyword count"], ([key, value.get("label"), value.get("weight"), len(value.get("keywords", []))] for key, value in sorted(selection.get("project_areas", {}).items())))
    lines += ["## Database Schema", "", "Read from a consistent read-only SQLite transaction. Counts are snapshots; schema is drift-checked.", ""]
    if not db["present"]:
        lines += ["Database absent; connect_db() declares the bootstrap schema.", ""]
    for entity in db["tables"]:
        lines += ["### " + entity["name"], ""]
        lines += table(["Column", "Type", "NOT NULL", "Default", "Primary-key position"], ([r["name"], r["type"], r["notnull"], r["dflt_value"], r["pk"]] for r in entity["columns"]))
        lines += ["Indexes: " + ", ".join(r["name"] for r in entity["indexes"]), ""]
        if entity["foreign_keys"]:
            lines += table(["From", "Parent table", "To", "On delete"], ([r["from"], r["table"], r["to"], r["on_delete"]] for r in entity["foreign_keys"]))
    lines += ["## Test Inventory", "", "{} test methods discovered. Discovery is not proof of passing tests; consult the dated audit.".format(tests["count"]), ""]
    lines += table(["Module / class", "Methods"], ([name, len(methods)] for name, methods in sorted(tests["classes"].items())))
    lines += [GUIDE.read_text(encoding="utf-8").strip(), "", "## Source Inventory", "",
              "Only explicit public source/config paths are inspected. Account/environment files, private messages and credential values are never copied here.", ""]
    lines += table(["Scope-relative path", "SHA-256 prefix"], ([name, hashlib.sha256(path.read_bytes()).hexdigest()[:12]] for name, path in source_files().items()))
    if source_revision() != revision:
        raise RuntimeError("Source changed during generation; retry after edits settle")
    return "\n".join(lines) + "\n"


def comparable(text: str) -> str:
    """Ignore only named runtime observations, never blanket-mask numbers."""
    text = re.sub(re.escape(SNAPSHOT_START) + r".*?" + re.escape(SNAPSHOT_END), "<!-- runtime snapshot omitted -->", text, flags=re.S)
    return re.sub(r"^> Generated at:.*\n", "", text, flags=re.M).strip()


def content_without_timestamp(text: str) -> str:
    return re.sub(r"^> Generated at:.*\n", "", text, flags=re.M).strip()


def write_document(document: str, out: Path) -> bool:
    old = out.read_text(encoding="utf-8") if out.exists() else ""
    if content_without_timestamp(old) == content_without_timestamp(document):
        return False
    out.parent.mkdir(parents=True, exist_ok=True)
    if old:
        history = out.parent / "_versions"
        history.mkdir(mode=0o700, exist_ok=True)
        digest = hashlib.sha256(old.encode("utf-8")).hexdigest()
        archived = history / (out.name + "." + digest + ".md")
        if not archived.exists():
            shutil.copy2(out, archived)
            archived.chmod(0o600)
    descriptor, temporary = tempfile.mkstemp(prefix=out.name + ".", dir=str(out.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(document)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, out)
        out.chmod(0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--refresh", action="store_true", help="idempotent, serialized refresh")
    parser.add_argument("--out", type=Path, default=OUTPUT)
    args = parser.parse_args(argv)
    if args.check:
        document = build_document()
        current = args.out.read_text(encoding="utf-8") if args.out.exists() else ""
        correct = comparable(current) == comparable(document)
        print("ARCHITECTURE.md is structurally current" if correct else "ARCHITECTURE.md differs from source/config/schema; run generate_architecture.py --refresh")
        return 0 if correct else 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with (args.out.parent / ".architecture.lock").open("a") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another architecture refresh is in progress; no concurrent write")
            return 0
        document = build_document()
        changed = write_document(document, args.out)
    print(json.dumps({"written": str(args.out), "changed": changed, "lines": document.count("\n")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
