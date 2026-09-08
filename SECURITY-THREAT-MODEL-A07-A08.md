# Threat model — HTTP boundary, fetching, exports (lane 05, run-20260906-2000)

Scope: audit issues A07 (export formula injection) and A08 (HTTP boundary),
plus the C6 safe-fetch surface consumed by lanes 01/04. Concise by design;
the tests are the executable form of this document.

## Assets

- Authored decision stores: `config/verdicts.json`, `config/outcomes.json`,
  `config/group-filter-profile.json` (user judgement; must never change from
  an unauthorized or malformed request).
- Private group content in SQLite/dashboard exports (loopback-only privacy).
- The reviewer's machine (spreadsheet apps execute formulas; the fetcher
  runs on the owner's Mac inside the home network).
- Service availability (worker threads, disk, log volume).

## Trust boundaries

1. **Group content is untrusted input** from ANY sender: text, URLs, media
   references. It flows into SQLite, exports, the dashboard, and (via
   lanes 01/04) into automatic fetching. It is data, never instructions.
2. **The browser is semi-trusted**: the served dashboard is ours, but any
   other page the owner visits can issue requests toward loopback. The
   browser attaches Host/Origin/Sec-Fetch-* honestly — those headers are the
   boundary markers.
3. **Local processes are trusted** (OS user boundary). A loopback client
   without browser headers is an authorized non-browser client by design —
   documented, not accidental. The custom `X-Radar-Action` header remains a
   preflight-forcing measure, NOT authentication; nothing here pretends the
   dashboard is safe to expose beyond loopback.
4. **Remote web servers are untrusted** (safe-fetch): they control DNS
   answers, redirects, sizes, encodings, content types, timing.

## Threats and mitigations (implemented in this lane)

| # | Threat | Vector | Mitigation | Test |
|---|--------|--------|-----------|------|
| T1 | DNS rebinding | hostile page's name re-resolves to 127.0.0.1; its "same-origin" fetches hit the radar with the custom header attached | exact loopback Host allowlist incl. actual port, every verb; forwarded headers never consulted | `test_lane05_http_adversarial.HostHeaderAttacks`, C7 target `test_hostile_host_header_is_rejected` |
| T2 | Cross-origin forgery | form/fetch from another origin mutating verdicts | Origin/Referer policy + `Sec-Fetch-Site` belt; `null` origins rejected; documented missing-Origin rule (see `http_guards` docstring) | `OriginPolicyAttacks`, C7 target `test_cross_origin_mutation_is_rejected` |
| T3 | Handler crash / desync via body | non-object JSON, bad UTF-8, truncated/oversized/slow bodies, chunked TE, CL games | object-shape enforcement, byte+time budgets, controlled 400/408/501, `Connection: close` on every guard rejection | `BodyAttacks`, C7 targets (non-object, content-type) |
| T4 | Stored-state pollution | nested containers / oversized strings smuggled into raw-persisted verdict fields | scalar+length hygiene before handlers; business validation stays in `record_*` (which validates before writing) | `test_nested_container_in_raw_persisted_field`, `test_huge_string_field_rejected` |
| T5 | File disclosure | traversal, encoded paths, symlinked artifacts, sensitive names (SQLite, worker.lock, cron.log, .env) | fixed ROUTES allowlist (no decoding), resolve+parent check, symlink escape test | `StaticSurface` |
| T6 | SSRF via shared URLs | loopback/RFC1918/link-local/metadata/CGNAT targets, IPv4-mapped + NAT64 embeddings, mixed DNS answers, redirect pivots, check-then-use races | `safe_fetch`: policy on every resolved address, literal-IP fast path, pinned-IP connect, per-hop re-validation | `test_lane05_safe_fetch.PolicyUnit`, `NoNetworkDenials`, redirect tests |
| T7 | Resource exhaustion via fetch | endless bodies, decompression bombs, stalled servers, redirect loops | byte budget on DECODED output, bounded streaming zlib, total wall-clock budget, redirect budget | oversize/gzip-bomb/slow/unresponsive tests |
| T8 | Spreadsheet formula injection | `=HYPERLINK`, `+cmd|...`, `@SUM`, tab/CR/LF-leading, full-width variants in group text opened by the reviewer | separate human-safe `relevant-sheet.csv` with apostrophe neutralization (OWASP-listed mitigation); raw machine formats keep original bytes | `test_lane05_export_safety` |
| T9 | Fetched content as instructions | HTML/JS/scripts in fetched bodies | bytes only: hash-named file or text; nothing executed or interpreted; consumers reminded via C6 contract | design property + no-execution code path |

## Non-goals / explicitly out of scope

- Authentication on loopback (would be security theater at this boundary;
  exposure beyond loopback stays forbidden instead).
- Protecting against the local OS user or other local processes.
- Excel as a spreadsheet consumer (unsupported; documented).
- Host allowlisting inside `safe_fetch` (per-feature trust policy stays at
  call sites — the trusted-image precedent keeps its own host pinning).

## Residual risks (stated, not hidden)

- Apostrophe neutralization verified machine-side only; Numbers/Sheets not
  interactively exercised from this isolated lane (GUI). Outstanding check
  listed in the handoff.
- `safe_fetch` exercised against loopback fixtures only (harness denies
  external network); first real-provider fetch happens at integration.
- Non-standard ports on public hosts are allowed by C6 (any port on a
  global address); tightening to 80/443 would need a contract revision.
- Slowloris on request HEADERS is bounded at 300 s (connection timeout),
  not eliminated; loopback-only exposure makes this a local-DoS curiosity.
- Full-width trigger neutralization exceeds the frozen c1 minimum set —
  flagged to 07 in case any consumer relies on exact c1-minimum behavior.

Primary references consulted (2026-09-06): OWASP CSV Injection page
(trigger set incl. full-width variants; apostrophe-prefix mitigation and its
Excel caveat; "no universal CSV sanitization strategy" — hence the split
raw/human artifacts), RFC 9110 Host semantics, WHATWG Fetch Metadata
(`Sec-Fetch-Site` value semantics).
