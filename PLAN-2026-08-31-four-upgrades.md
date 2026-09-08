# Non-stop implementation plan — four upgrades + full archive import

Date: 2026-08-31
Status: executing (no stops, no questions — every decision below is already made)

## What this delivers

1. **Outcome tracking** — record what you actually tried and kept, durably.
2. **Learned negatives** — mine 785 exclusions for rules you approve once.
3. **Bookmark ingest** — live capture **plus the full 25,505-item archive** (your explicit call).
4. **Telegram decisions** — inline buttons that record a verdict from your phone.

## The two problems the archive import creates, and how they are solved

The archive is already hydrated (`categorized_tweets.json` carries text, author, media, counts), so importing costs no X reads. But it is 15× the current ledger, which breaks two things:

**Problem 1 — payload blowup.** `dashboard-data.json` is 3.8 MB at 1,665 resources. All 27k would be ~60 MB; the page would be unusable.
**Solution:** the payload carries **every group resource** plus **only bookmark resources the rules marked relevant**, capped at the top 2,000 by score. The full archive stays in SQLite and both CSVs, and becomes reachable through a new `GET /api/search` endpoint on the loopback server. The briefing stays a briefing; the archive becomes a searchable corpus.

**Problem 2 — review-queue blowup.** The rules layer intentionally sends anything it cannot confirm to the semantic reviewer. For the archive that is ~20,000 rows: days of Codex calls and real cost.
**Solution:** archive rows that no rule accepts terminate as `irrelevant` with `decision_source='archive-rules'`, instead of entering `pending_review`. This is a deliberate, documented deviation from the group's high-recall policy, justified because a bulk historical import is not a live feed. Live bookmarks (new saves) keep the normal high-recall path. A `--semantic` flag exists to reprocess any slice later.

**Notification safety:** the import arms a bookmark cutover before inserting, so 25,505 rows cannot produce a single Telegram alert.

---

## Phase 0 — Schema foundation

`scripts/group_monitor.py` → `connect_db()`, using the existing `PRAGMA table_info` guard already used for `message_resources.notified_at`:
- `resources.source TEXT NOT NULL DEFAULT 'group'`
- `CREATE INDEX idx_resources_source`
- `select_resource_rows()` and `resource_to_dict()` expose `source`
- `verify()` asserts every resource has a non-empty source

Additive, idempotent, runs once, old rows default to `group`.

## Phase 1 — Outcome tracking

- `config/outcomes.json` — `{version, updated_at, outcomes:[{key, state, note, decided_at, decided_by}]}`, `state ∈ {trying, kept, dropped}`. Separate from `verdicts.json` so an outcome survives a verdict being changed or cleared; in `config/` because `data/` is gitignored and outcomes are precious.
- `POST /api/outcome` in `radar_server.py` — an exact structural copy of `record_verdict()`: header guard `X-Radar-Action: outcome`, ≤16 KB body, strict validation, tmp + `os.replace`, `chmod 600`, `clear` to undo, rejects never touch the file.
- `load_outcomes()` mirrors `load_verdicts()`; `build_tool_index()` attaches `outcome`.
- Dashboard: **Trying / Kept / Dropped** + optional note on `must_try` rows; header shows `N must try · N tried · N kept` — the accountability number that makes the feature worth having.
- Both CSVs gain `outcome`, `outcome_note`.

## Phase 2 — Learned negatives

- `scripts/learn_negatives.py` — smoothed **log-odds** of a term appearing in excluded/irrelevant content versus relevant content. Terms common in both (e.g. "ai") score ~0 and are ignored; raw frequency would not do this.
- Corpus: `content_text`/`title` of irrelevant rows and hand-excluded tools — the **content**, not the reviewer's prose reasons (those reflect the model's vocabulary, not the subject).
- Guards: ≥5 excluded occurrences, low relevant contamination, never propose a term already in `ai_terms` or any project-area keyword list, ≥3 chars, EN + AR.
- Dashboard **Suggested rules**: *"'fitness' — 9 excluded, 0 relevant. Always exclude?"* → **Apply / Dismiss**.
- Applying writes `selection.negative_terms`. `score_resource()` stays additive; a separate `negative_gate()` fires **only when no project area matched**, mirroring `auto_gate()`: marked `auto`, reason recorded, always overridable by a hand verdict. Rules still never silently reject.

## Phase 3 — Bookmarks: live capture + full archive

`scripts/ingest_bookmarks.py`:
- `--live` — `service.fetch_bookmarks()` (`scripts/service.py:466`, already shells to `bird bookmarks --json` with the same credentials), `source='bookmark'`, normal high-recall path.
- `--archive` — stream `data/categorized_tweets.json` (25,505), `source='bookmark-archive'`, rules-only terminal classification, batched inserts in one transaction per 1,000 with progress, fully resumable and idempotent.
- Synthetic message rows (`bookmark:<tweet_id>`) keep the `message_resources` foreign key and existing joins intact.
- Reuses `extract_resources()`, `_tweet_content()`, and the payload conventions of `normalize_dm_attachment()`, so typing, scoring and enrichment need no changes.
- Live capture runs inside the loop, bounded and `try/except`, like enrichment.
- Dashboard: **Source** filter (Group / Bookmarks / Archive / All) defaulting to **Group**, so the briefing is unchanged until you ask for more.
- `GET /api/search?q=&source=&limit=` on the loopback server for the full corpus, reading SQLite read-only.

## Phase 4 — Telegram decisions

**64-byte `callback_data` limit drives the design** — the tool key never travels; a short id does.
1. **Send** (Mac): `notify_with_buttons()` attaches `inline_keyboard` — Must try / Not for me / Open. Callback data `rdr:<8-char-id>:<action>` (~20 B). The id→key map is written to `data/group-monitor/pending-decisions.json`. A new function, so the shared Atlas notifier keeps its current behaviour for every other caller.
2. **Receive** (VPS, ~25 lines): a `case 'rdr':` in the existing `switch (action)` at `bot.js:1814`, appending to `/opt/autonomous-loop/data/radar-decisions.jsonl`, answering the callback, editing the message. Behind the existing owner check, wrapped in try/catch so a radar bug cannot crash the Atlas bot. `bot.js` backed up first; `atlas-pa/ARCHITECTURE.md` updated per the standing rule.
3. **Pull** (Mac, each pass): `telegram_decisions.py` reads new lines by byte offset over SSH, using the host and key named in `config/vps.json` (gitignored; overridable with `RADAR_VPS_HOST` / `RADAR_VPS_KEY`), resolves id→key, and applies through the **same validation path as `/api/verdict`** — one write path, no divergence.

---

## Verification gate — after every phase

```bash
/usr/bin/python3 -m unittest discover -s tests
/usr/bin/python3 scripts/group_monitor.py verify --strict
/usr/bin/python3 scripts/generate_architecture.py --check
/usr/bin/python3 ~/.codex/skills/loopsmith/scripts/loopspec-lint.py group-share-filter.loop.json \
  --registry ~/assistant/loopsmith-registry.md --allow-existing
/usr/bin/python3 scripts/group_filter_loop.py      # the real cron path
curl -s http://127.0.0.1:8765/api/health
```

Endpoint guard matrix by curl (GET→405, no header→400, bad body→400, file byte-identical after each rejection). UI checked in Playwright at 1440×900 and 390×844: zero console errors, zero overflow, every new control exercised for real and reverted.

## Rollback

Every phase is independently revertible: new config files can be deleted, the schema change is additive with a default, `bot.js` restores from backup, and both kill switches stay untouched. A full archive rollback is `DELETE FROM resources WHERE source='bookmark-archive'` plus its synthetic messages.

## Non-negotiables carried through

- No stage may fail an otherwise-good run — every new stage is `try/except` and bounded.
- Nothing may hang — new subprocess/network work carries its own timeout.
- The group capture cursor, notification cutover and hard-deadline guards are not touched.
- `ARCHITECTURE.md` regenerates itself; drift check must pass at the end of every phase.

---

# Scoring audit, 2026-08-31 — v1 was wrong, v2 fixes it

Asked whether the priority method was actually right, I measured it instead of defending it. It was not.

## What the audit found

**1. The stated design and the real behaviour were opposites.** I described reshare as the strongest signal and engagement as "deliberately weakest". Measured over 1,947 relevant items:

| term | share of average score (v1) |
|---|---|
| engagement (likes) | **50.3%** |
| fit | 20.6% |
| type | 14.4% |
| reshare | **1.5%** |

Small coefficients were not enough. Reshare fires on 3% of items and repo-link on 11%; engagement fires on 100% with a wide range, so it dominated by always being present.

**2. Proof it mattered.** The v1 top-10 contained **openGym (a fitness tracker) at #2** and **SearchPhone (phone OSINT) at #4** — both items I had explicitly hand-excluded. Verdicts never fed the ranking at all.

**3. Enrichment was collected and ignored.** 344 repos had stars/last-push/archived facts cached; `compute_pick_score` referenced none of them. Tweet likes were being used as a quality proxy while real repository health sat unused.

**4. Reading material was actively buried.** Type bonuses were tool 3.0 / practice 2.0 / **research 0.5** — the lowest of any lane, applied to exactly the "latest practices" the system exists to surface. There was no `must_read` verdict.

**5. Roundups inherited verdicts.** A 43-link listicle absorbed the `must_try` verdict of `obra/superpowers` because it linked it, and rode that to #4. My "GMAIL AUTOREG" exclusion key was also invented and matched nothing.

## v2

- Engagement **hard-capped at 2.0** — it can break a tie, never create one.
- Fit raised to 3.0/area; research raised to 2.0 so reading is no longer punished.
- **Repository health** added from the enrichment cache: `log10(stars)`, minus 3 if archived or unpushed for a year.
- **Verdicts feed the score**: must_try +6, must_read +5, already_have −4, excluded −12.
- **Verdict inheritance bounded** to posts linking ≤3 tools.
- `must_read` added as a first-class verdict, endpoint value, tab and chip.

## Validation — against the 12 tools I hand-picked after reading them

| metric | v1 | v2 |
|---|---|---|
| median rank of my picks (of 1,947) | 53 | **7** |
| picks in the top 20 | 3 | **10** |
| picks in the top 50 | 5 | **11** |
| hand-excluded items in the top 100 | openGym at #2 | 1 |

Share of average score after: fit 42.1%, type 23.3%, engagement 21.9%, repo 4.3%, recency 4.3%, health 3.1%, reshare 2.2%.

**Verified:** 73/73 tests (6 new pinning each fix), strict gate pass, drift check current, LoopSpec lint PASS, cron pass 29 s.

## Still imperfect, stated plainly

- Engagement is 21.9% of the average score. That is by construction — it is capped at 2.0 absolute, so the share is high only because total scores are lower now. It cannot lift an unfitted item.
- `fit` counts *matched project areas*, so a broad roundup can still score well on breadth rather than depth. Bounding verdict inheritance fixed the worst case; the general one remains.
- The `must_read` lane is live but empty — no reading has been marked yet.
- Only 3% of items are reshared, so the strongest quality signal is usually silent.

---

# Taxonomy fix — "try" and "read" are no longer the same thing

You said the items I called must-reads were actually must-try things. You were right, and the cause was a modelling error, not a labelling slip.

## What was wrong

The lanes were `tool` / `practice` / `research`, which mixed **what a thing is** with **what it demands of you**. `practice` held all three of these at once:

| Same lane | Actually demands |
|---|---|
| "Stop asking AI to summarise" | a technique to **try** |
| "Nature Medicine: AI predicts 130 diseases" | news to **read** |
| "تعلم كيف تسوي AI Agent من الصفر" | a course to **follow** |

So when I reported "your must-reads", most of the list were things to run. There was also no way to state the difference: `must_read` had just been added as a verdict, but nothing stopped it being applied to a CLI, or `must_try` to a paper.

## What it is now

Lanes are defined by **the action the resource demands**:

| Lane | Meaning | Bonus |
|---|---|---|
| `try` | install, run or apply it | 3.0 |
| `learn` | follow it end to end | 2.5 |
| `read` | consume it once | 2.0 |
| `reference` | look it up when needed | 1.5 |

`VERDICT_FOR_TYPE` makes the pairing explicit and the endpoint **refuses a crossing with HTTP 409**: `must_try` on an article and `must_read` on a CLI are both rejected, and the file is left untouched. The review queue offers "Must read" for read/learn items and "Must try" for the rest, so the wrong button is never presented.

Result on the real ledger: the 344-item `practice` mush became **learn 135 + read 128**, with techniques and prompts correctly moving into `try` (847 → 953).

## Two leaks found and fixed while validating

Spot-checking the new lanes caught the same failure mode twice — a lane word appearing as a *product feature* rather than as the item's nature:

- **YTSage**, a yt-dlp GUI, landed in `learn` because its blurb says "playlist" (what it downloads).
- **witr**, a CLI, landed in `read` because its tagline is "Why is this running" and matched "why"/"explains".

Fix: a third-person product verb ("monitors", "converts", "traces") is the strongest evidence a thing *is* software, so its weight went 2 → 5; "traces" and similar were added; and bare "playlist" was dropped from the learn terms, keeping only the unambiguous playlist-URL signal. Both now classify as `try`, and all seven separation cases still pass.

**Verified:** 78/78 tests (5 new pinning the separation and the 409), strict gate pass, drift check current, LoopSpec lint PASS, cron pass 26 s.

## Honest remainder

- 725 relevant items are still `other` — no lane signal strong enough. They stay in the stream and can still reach Focus now; they are simply not claimed to be one kind or the other.
- The `read` and `learn` lanes are ranked by the same score as `try`, which rewards repo links and health. Those mean less for an article, so ranking *within* the reading lanes is weaker than within `try`.
- No item is marked `must_read` yet. The lane and verdict exist; the judgement is still yours to make.
