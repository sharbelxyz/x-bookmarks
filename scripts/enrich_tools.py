#!/usr/bin/env python3
"""Fetch and cache objective repository facts for the tools the group shares.

This is the evidence layer under the review queue. It is deliberately dumb:
it asks GitHub for stars, last push, archived flag, licence and description,
and writes them to a cache. It makes no judgement — judgement stays with the
user, in `config/verdicts.json`.

Why it exists: deciding whether a tool is worth trying is ~90% objective
legwork (is it real, is it maintained, is it licensed usably) and ~10% personal
fit. Automating the 90% is what makes a one-tap verdict possible.

Design constraints, all deliberate:

* **Bounded.** At most ``--limit`` repos per run and an overall wall-clock
  budget, so it can be called from the twice-hourly loop without ever
  threatening its deadline.
* **Resumable.** The cache is written after every fetch, so a killed run keeps
  everything it already learned.
* **Refreshing.** Entries expire, so a repo that gets archived after being
  recommended stops looking healthy. Reviewed tools refresh sooner than the
  long tail, because a stale "must try" is actively misleading.
* **Never hangs.** Every subprocess call has a timeout, and the run stops when
  the budget is spent. It exits rather than waiting.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


os.umask(0o077)

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data" / "group-monitor"
CACHE_PATH = DATA_DIR / "tool-meta.json"

# cron gives a minimal PATH; Homebrew binaries are not on it.
RUNTIME_PATH = os.pathsep.join(
    ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
)

# gh keeps its token in the macOS keychain, which cron cannot read — every
# cron-context fetch 401'd while interactive runs succeeded. The token sidecar
# (written once via `gh auth token`, 0600, gitignored, outside the directory the
# radar server is allowed to serve) closes that gap. Env always wins over it.
TOKEN_PATH = ROOT / "data" / "gh-token"


def _gh_env() -> Dict[str, str]:
    env = {**os.environ, "PATH": RUNTIME_PATH}
    if not env.get("GH_TOKEN") and not env.get("GITHUB_TOKEN"):
        try:
            token = TOKEN_PATH.read_text(encoding="utf-8").strip()
        except OSError:
            token = ""
        if token:
            env["GH_TOKEN"] = token
    return env


REPO_HOSTS = ("github.com/",)
GH_FIELDS = (
    "nameWithOwner,description,stargazerCount,forkCount,pushedAt,createdAt,"
    "isArchived,isFork,isEmpty,primaryLanguage,homepageUrl,repositoryTopics"
)

# A recommendation that has gone stale is worse than no recommendation, so
# anything already reviewed is re-checked far more often than the long tail.
TTL_DAYS = {"reviewed": 7, "candidate": 30, "rejected": 90}

DEFAULT_LIMIT = 40
DEFAULT_BUDGET_SECONDS = 240
PER_CALL_TIMEOUT = 25


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(moment: dt.datetime) -> str:
    return moment.isoformat(timespec="seconds")


def parse_iso(value: Any) -> Optional[dt.datetime]:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def load_cache(path: Path = CACHE_PATH) -> Dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return payload.get("tools", {}) if isinstance(payload, dict) else {}


def save_cache(cache: Dict[str, Any], path: Path = CACHE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    document = {"updated_at": iso(utc_now()), "count": len(cache), "tools": cache}
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    path.chmod(0o600)


def repo_slug(key: str) -> str:
    """'github.com/owner/repo' -> 'owner/repo'; '' when it is not a GitHub repo."""
    text = str(key or "")
    if not text.startswith(REPO_HOSTS):
        return ""
    parts = [part for part in text.split("/") if part][1:3]
    if len(parts) != 2:
        return ""
    if not re.match(r"^[A-Za-z0-9._-]+$", parts[0]) or not re.match(r"^[A-Za-z0-9._-]+$", parts[1]):
        return ""
    return "/".join(parts)


def fetch_repo(slug: str, timeout: int = PER_CALL_TIMEOUT) -> Dict[str, Any]:
    """One `gh repo view`. Returns a record, including for the not-found case."""
    stamp = iso(utc_now())
    try:
        result = subprocess.run(
            ["gh", "repo", "view", slug, "--json", GH_FIELDS],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=_gh_env(),
        )
    except subprocess.TimeoutExpired:
        return {"slug": slug, "fetched_at": stamp, "ok": False, "error": "timeout"}
    except OSError as exc:
        return {"slug": slug, "fetched_at": stamp, "ok": False, "error": str(exc)[:200]}

    if result.returncode != 0:
        message = (result.stderr or result.stdout or "").strip()[-200:]
        gone = "could not resolve" in message.lower() or "not found" in message.lower()
        return {
            "slug": slug,
            "fetched_at": stamp,
            "ok": False,
            "error": "missing" if gone else message,
            "missing": gone,
        }
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"slug": slug, "fetched_at": stamp, "ok": False, "error": "bad json"}

    return {
        "slug": payload.get("nameWithOwner") or slug,
        "fetched_at": stamp,
        "ok": True,
        "description": payload.get("description") or "",
        "stars": payload.get("stargazerCount"),
        "forks": payload.get("forkCount"),
        "pushed_at": (payload.get("pushedAt") or "")[:10],
        "created_at": (payload.get("createdAt") or "")[:10],
        "archived": bool(payload.get("isArchived")),
        "is_fork": bool(payload.get("isFork")),
        "is_empty": bool(payload.get("isEmpty")),
        "language": (payload.get("primaryLanguage") or {}).get("name") or "",
        "homepage": payload.get("homepageUrl") or "",
        "topics": [t.get("name") for t in (payload.get("repositoryTopics") or []) if t.get("name")][:8],
    }


def fetch_license(slug: str, timeout: int = PER_CALL_TIMEOUT) -> str:
    """`gh repo view` does not reliably fill licenceInfo, so ask the licence API."""
    try:
        result = subprocess.run(
            ["gh", "api", "repos/{}/license".format(slug), "--jq", ".license.spdx_id"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=_gh_env(),
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def is_expired(entry: Dict[str, Any], tier: str, now: Optional[dt.datetime] = None) -> bool:
    # A transient failure (auth outage, timeout, rate limit) is not a fact about
    # the repo and must retry on the next pass, not sit out the tier TTL — a
    # 401 spell once parked 13 fresh repos for a month. "missing" IS a fact
    # (the repo is gone) and keeps its TTL like any other answer.
    if not entry.get("ok") and not entry.get("missing"):
        return True
    now = now or utc_now()
    fetched = parse_iso(entry.get("fetched_at"))
    if fetched is None:
        return True
    return (now - fetched).days >= TTL_DAYS.get(tier, 30)


def select_queue(
    tools: Iterable[Dict[str, Any]],
    cache: Dict[str, Any],
    limit: int,
    now: Optional[dt.datetime] = None,
) -> List[str]:
    """Which repos to fetch next: reviewed refreshes first, then best candidates."""
    now = now or utc_now()
    reviewed, candidates, rejected = [], [], []
    for tool in tools:
        slug = repo_slug(tool.get("key", ""))
        if not slug:
            continue
        entry = cache.get(slug)
        verdict = tool.get("verdict") or "unreviewed"
        if verdict in {"must_try", "already_have"}:
            tier, bucket = "reviewed", reviewed
        elif verdict == "excluded":
            tier, bucket = "rejected", rejected
        else:
            tier, bucket = "candidate", candidates
        if entry is None or is_expired(entry, tier, now):
            bucket.append((-float(tool.get("best_score") or 0.0), slug))
    ordered: List[str] = []
    for bucket in (reviewed, candidates, rejected):
        bucket.sort()
        ordered.extend(slug for _score, slug in bucket)
    deduped: List[str] = []
    for slug in ordered:
        if slug not in deduped:
            deduped.append(slug)
    return deduped[:limit]


def enrich(
    tools: Iterable[Dict[str, Any]],
    limit: int = DEFAULT_LIMIT,
    budget_seconds: int = DEFAULT_BUDGET_SECONDS,
    cache_path: Path = CACHE_PATH,
) -> Dict[str, Any]:
    cache = load_cache(cache_path)
    queue = select_queue(tools, cache, limit)
    deadline = time.monotonic() + budget_seconds
    fetched = failed = missing = 0
    for slug in queue:
        if time.monotonic() >= deadline:
            break
        record = fetch_repo(slug)
        if record.get("ok"):
            record["license"] = fetch_license(slug)
            fetched += 1
        elif record.get("missing"):
            missing += 1
        else:
            failed += 1
            previous = cache.get(slug)
            if previous and previous.get("ok"):
                # Keep the last good facts; only move the clock so we retry later.
                previous["fetched_at"] = record["fetched_at"]
                previous["last_error"] = record.get("error", "")
                cache[slug] = previous
                save_cache(cache, cache_path)
                continue
        cache[slug] = record
        save_cache(cache, cache_path)
    return {
        "queued": len(queue),
        "fetched": fetched,
        "missing": missing,
        "failed": failed,
        "cached_total": len(cache),
        "remaining": max(0, len(queue) - fetched - missing - failed),
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--budget-seconds", type=int, default=DEFAULT_BUDGET_SECONDS)
    parser.add_argument("--all", action="store_true", help="drain the whole queue (supervised)")
    args = parser.parse_args(argv)

    sys.path.insert(0, str(ROOT / "scripts"))
    import group_monitor as monitor

    conn = monitor.connect_db()
    try:
        records = [monitor.resource_to_dict(row) for row in monitor.select_resource_rows(conn)]
        tools = monitor.build_tool_index(records, monitor.load_verdicts())
    finally:
        conn.close()

    limit = 10_000 if args.all else args.limit
    budget = 3600 if args.all else args.budget_seconds
    result = enrich(tools, limit=limit, budget_seconds=budget)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
