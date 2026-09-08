# Autonomous Group Share Filter

This monitor watches the configured private X group and captures resources from
every sender. Owner identity is retained only as dashboard metadata; it never
controls whether a message is hydrated or classified.

## Resume Point

The initial cursor is message `2077186156320436638`. It is the latest owner
message that contains any X resource already represented in the prior workspace
audits. That message contains tweet `2077086240214524145` from the July 15 audit.

The cursor was recovered by matching all 530 previously analyzed X IDs against
the live group timeline. New processing begins after that message.

## Pipeline

1. Read every sender's group messages after the durable capture cursor.
2. Commit complete message text, links, sender profiles, and resource relations to
   SQLite in one transaction.
3. Advance the capture cursor only after the historical boundary is reached.
4. Use the tweet payload embedded in the DM message; use Agent Reach's active
   `bird` backend only when that attachment is absent.
5. Apply high-recall AI and project rules from `config/group-filter-profile.json`.
6. Give ambiguous rows to an ephemeral, read-only Codex review with a strict JSON
   schema. Sparse-text rows include trusted X image thumbnails mapped to their
   resource IDs, so visual tools and methods are not rejected as context-free.
7. Rebuild the HTML dashboard and data exports.
8. Notify once per new relevant group share, including known resources reshared by
   another member.

Hydration failures stay retryable with their attempt count and last error. After
three explicit deleted/not-found results, a resource becomes `unavailable`; it is
still retained in the database. Three consecutive due batches with no successful
read become a `stuck` LoopSmith outcome instead of a false healthy run.

## Why a run can never hang forever

A run is allowed to take as long as it needs, but it is guaranteed to end:

1. **Per-call timeouts** — each X read (30 s), each group page fetch (15 s), and
   each semantic review call (bounded by the remaining budget) has its own
   timeout, so one slow dependency cannot stall the run.
2. **Soft deadline (28 min)** — checked between review batches; the run stops
   asking for more batches and reports `stuck` with the queue intact.
3. **Hard deadline (29.5 min, `SIGALRM`)** — the soft deadline is only consulted
   *between* stages, so a call that blocks in the middle of one would never reach
   it. `SIGALRM` fires wherever the process is blocked. This matters because the
   worker lock is an `flock` held by the running process: without it, one hung
   run would silently refuse every later cron run for as long as it hung. Both
   deadlines stay inside the 30-minute cap the LoopSpec declares.
4. **Lock release is automatic** — `flock` is released by the kernel when the
   process exits for any reason, so a crashed or killed run never blocks the next.
5. **Visible if it does die** — the dashboard shows a "Stale" pill and a banner
   with a **Scan now** button once the last successful run is over 90 minutes old,
   and `/api/health` reports `stale` plus `age_seconds` for scripted checks.

Regression tests cover points 3 and 4: `test_hard_deadline_interrupts_a_blocked_stage`
blocks for 30 s against a 1 s guard and must be interrupted, and
`test_hard_deadline_releases_the_worker_lock` proves the next run can take the
lock straight afterwards.

## Artifacts

All runtime state remains in this project:

- `data/group-monitor/group-monitor.sqlite3` - durable source of truth
- `data/group-monitor/relevant.csv` - spreadsheet-friendly feed
- `data/group-monitor/relevant.jsonl` - machine-readable feed
- `data/group-monitor/all-resources.csv` - every resource and terminal state
- `data/group-monitor/unavailable.jsonl` - retained deleted/unreadable resources
- `data/group-monitor/latest.md` - latest 100 relevant resources
- `data/group-monitor/dashboard.html` - searchable all-sender HTML dashboard
- `data/group-monitor/dashboard-data.json` - the same document as JSON, polled by the live page
- `data/group-monitor/status.json` - cursor and queue counts (the live page polls this)
- `data/group-monitor/verification.json` - last invariant check
- `data/group-monitor/cron.log` - scheduled-run log

Credentials remain in the existing ignored `data/accounts.json`; no credential is
copied into the profile, LoopSpec, prompt, outputs, or registry.

## Commands

```bash
python3 scripts/group_monitor.py sync
python3 scripts/group_monitor.py prepare-review --limit 80
python3 scripts/group_monitor.py apply-decisions data/group-monitor/decisions.json
python3 scripts/group_monitor.py verify --strict
python3 scripts/group_monitor.py status
python3 scripts/group_monitor.py notify        # preview only
python3 scripts/group_monitor.py notify --live # operational delta notification
python3 scripts/manage_radar_server.py open     # live dashboard (falls back to the file)
```

The twice-hourly schedule is managed without touching unrelated crontab entries:

```bash
python3 scripts/manage_group_filter_schedule.py install
python3 scripts/manage_group_filter_schedule.py status
python3 scripts/manage_group_filter_schedule.py uninstall
```

`uninstall` is the kill switch. It backs up the current crontab before changing it.

## Tools and verdicts

The ledger's unit is the **post**, but a post can link fifty tools — before this
existed, only the first link of each post was visible, which hid 283 of 556
links. `build_tool_index` in `group_monitor.py` collapses every external link
across every post into one row per tool (`github.com/owner/repo`, case-insensitive,
markdown junk like `superpowers](https:` stripped), counts how many posts mention
it, and joins it against `config/verdicts.json`.

`config/verdicts.json` holds the hand-checked calls: `must_try` (with rank, why,
and a concrete first step), `excluded` (with the reason), and `already_have`.
It is plain data — edit it and run `python3 scripts/group_monitor.py export` to
change what the dashboard shows; no code change needed. A verdict whose tool is
only *named* in a post (`npx skills add owner/tool`, no URL) still appears, with
`0 mentions`, so the curation is complete by construction rather than by luck.

Verdicts also appear as a chip on stream rows and as two extra columns
(`verdict`, `verdict_why`) in `relevant.csv` and `all-resources.csv`.

### How a newly shared tool gets prioritised

Deciding whether a tool is worth trying is roughly 90% objective legwork (is it
real, maintained, usably licensed) and 10% personal fit. The pipeline automates
the 90% and leaves the 10% to a click.

1. **Facts** — `scripts/enrich_tools.py` asks GitHub for stars, last push,
   archived flag, licence, language and description, and caches them in
   `data/group-monitor/tool-meta.json`. It runs inside every loop pass, capped
   at 20 repos / 90 s, so the backlog drains steadily without threatening the
   deadline. Entries expire — 7 days for anything already reviewed, 30 for
   candidates, 90 for rejects — because a "must try" that has since been
   archived is worse than no recommendation at all.
2. **Auto-gates** — `auto_gate()` turns facts into verdicts *only where no
   judgement is involved*: archived, empty, no commit in a year, under 25 stars,
   or the repo is gone. These are marked `auto: true` and a hand-written verdict
   always overrides them.
3. **Review queue** — whatever survives is a genuine judgement call. The
   dashboard's **Review queue** tab shows at most **three** at a time, ranked by
   score, with the evidence already assembled, and three buttons: **Must try /
   Not for me / Skip for now**. One click POSTs to `/api/verdict`, which
   validates strictly and rewrites `config/verdicts.json` atomically.

No model ever asserts "you should try this". The AI in this system is used for
exactly one thing — deciding whether an ambiguous post is *relevant* — and never
for recommending. Priority ordering is the arithmetic `pick_score`; the
recommendation itself is always a human decision.

`POST /api/verdict` accepts `{key, verdict}` where verdict is `must_try`,
`excluded`, `already_have`, or `clear` to undo. It requires the header
`X-Radar-Action: verdict`, is loopback-only like the rest of the server, and
never touches the file when validation fails.

## Live Dashboard

The dashboard is a briefing built for glancing, not a list. Every run recomputes,
per resource, a lane (`resource_type`: software & tools / practices & guides /
research & news / uncategorized, from deterministic EN+AR+ZH keyword, verb, and
URL-host signals in `scripts/resource_typing.py`) and a `pick_score` (group
reshares, project fit, concrete repo links, software over news, 7-day recency,
capped engagement — virality alone never wins). Above the full stream the page
shows one **Focus now** card with three ranked items (#1 has the single primary
action), a collapsed "new since you caught up" line, the three lanes collapsed
(three items each; a lane you open stays open), and one pulse sentence with the
14-day chart folded away. Times use a 12-hour clock. Uncategorized items never
get a lane but remain in the stream under the Type filter.

Opened from disk (`file://`) the page reloads itself every 15 minutes. Served over
loopback it stays current without stealing attention: the page polls
`status.json` every 60 seconds (every 5 minutes while hidden) and fetches
`dashboard-data.json` when a run has produced new data. While you are looking at
the page the new data is staged behind an "Apply · N new" control in the live
pill; if the tab was hidden it is applied silently. A "Stale" pill and banner
appear when the last run is more than 90 minutes old; the banner's "Scan now"
button starts one bounded run of `scripts/group_filter_loop.py` through
`POST /api/run` (loopback only, custom header required, 10-minute cooldown,
refused while the worker lock is held).

```bash
python3 scripts/manage_radar_server.py install    # launchd service on http://127.0.0.1:8765/
python3 scripts/manage_radar_server.py open       # open the live dashboard
python3 scripts/manage_radar_server.py status     # plist, process, and /api/health
python3 scripts/manage_radar_server.py restart    # after changing radar_server.py
python3 scripts/manage_radar_server.py uninstall  # kill switch for the server only
```

`scripts/radar_server.py` is stdlib-only, read-only, loopback-only, and serves a
fixed whitelist (`/`, `dashboard-data.json`, `status.json`, `verification.json`,
`relevant.csv`, `all-resources.csv`, `relevant.jsonl`, `latest.md`, `/api/health`).
It never exposes the SQLite database, credentials, or a directory listing, sends
`Cache-Control: no-store` and a strict CSP, and logs only errors to
`data/group-monitor/server.log`. It has nothing to do with the cron loop: removing
one never affects the other.

Three states are kept in the browser only (localStorage, per Mac; nothing is
written back to the ledger): **Done** ("I looked at it / tried it"), **Not for
me**, and **Caught up**. Done and Not-for-me items leave Focus now, the lanes,
and the new-list, and are hidden from the stream unless the "Done / Not for me"
filter says otherwise (Undo is one click). "Caught up" is explicit: only shares
after that moment count as new; there is no automatic read-state, so leaving the
page open on a second monitor never marks anything seen and never floods.

Notifications start disarmed. After the supervised historical catch-up and strict
verification, `python3 scripts/group_monitor.py baseline` acknowledges existing
matches and arms future delta notifications at the current durable message cursor.
Resources recovered later from an older rate-limit retry are also acknowledged
against that cutover, while newer group shares remain eligible. Notification
state belongs to each message-resource relation, so a later reshare of a known
resource can still produce one new alert. This prevents a historical flood without
delaying new-share alerts.

## Selection Profile

The tracked profile selects every explicit AI resource plus matches for these
current project lanes: AI agents, Hermes communications, marketplace operations,
apps and releases, document/LMS work, Saudi/Arabic/legal/career workflows,
infrastructure/security, creative production, and research automation.

Rules only auto-accept. A rule miss remains `pending_review`, so adding a project
keyword improves speed but omitting one does not create a silent false negative.
