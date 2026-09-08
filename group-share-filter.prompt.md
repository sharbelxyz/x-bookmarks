# group-share-filter runbook

LoopSpec: `/Users/mshrmnsr/claude1/x-bookmarks/group-share-filter.loop.json`

The deployed workflow is `scripts/group_filter_loop.py`. It executes these
bounded stages from a cold start:

1. Capture private-group messages after the durable cursor and retain resources
   from every sender. Sender ownership is attribution only, never a filter.
2. Read up to 60 due X resources through Agent Reach's active `bird` backend.
3. Apply deterministic high-recall AI and project rules.
4. Submit at most four batches of 20 ambiguous rows to the authenticated Codex
   CLI in read-only, ephemeral, low-effort mode with a strict JSON schema. Sparse
   rows include up to 20 trusted X image attachments with exact resource mappings.
5. Validate that decision IDs exactly equal batch IDs, apply the decisions,
   export CSV/JSONL/Markdown plus the self-contained HTML dashboard, and run
   strict deterministic verification.
6. Record a Telegram delta only after the historical baseline is armed; record
   every run through the LoopSmith journal and registry adapter.

The workflow has a 28-minute internal deadline under the LoopSpec's 30-minute
cap. It never advances an incomplete capture cursor, silently rejects a rule
miss, acts on a referenced resource, or writes credentials to an artifact.
Three consecutive due hydration batches with zero successes produce a `stuck`
outcome and operational alert.
