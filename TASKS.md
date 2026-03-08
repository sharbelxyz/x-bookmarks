# TASKS.md — X Bookmark Manager

> Last updated: 2026-03-08
> Branch: working copy (main repo at `/Users/mshrmnsr/claude1/x-bookmarks/`)

---

## ✅ Done

### Session 1 — Glassmorphism redesign
- [x] Full CSS overhaul of `app/dashboard.html` (~400 lines replaced)
  - Deep navy backgrounds, rgba() glass layers, `backdrop-filter: blur()`
  - Gradient buttons, glow shadows, animated active nav indicator
  - CSS custom properties design token system (4px spacing scale)
  - Responsive breakpoints (sidebar collapse 860px, grid stack 600px)
- [x] Fixed TypeError: `sourceFilter` null reference in `updateDashboard()` — added null guard
- [x] Updated `HANDOFF.md` (removed credentials, updated counts)
- [x] Committed all changes (13 files, +4972/-38 lines)
- [x] Created PR → https://github.com/sharbelxyz/x-bookmarks/pull/1

### Session 2 — No terminal dependency + premium design
- [x] Added 4 new API endpoints to `scripts/dashboard.py`:
  - `POST /api/harvest/full` — full historical bookmark harvest (background thread)
  - `POST /api/reclassify` — re-classify "Other" tweets via LLM (background thread)
  - `GET  /api/export?format=csv|json` — download all bookmarks as file
  - `POST /api/build-app` — build macOS .app bundle (background thread)
- [x] Full CSS overhaul → **Precision Dark** design (Linear × Craft aesthetic):
  - Near-true-black (`#080809`), solid layered surfaces (no glassmorphism on cards)
  - Violet accent (`#7c6af8`), crisp 1px borders
  - 36px tabular stat numbers (`font-variant-numeric: tabular-nums`)
  - UPPERCASE section labels, flat solid buttons, tight filter pills
  - Frosted topbar only (`backdrop-filter` scoped to topbar)
- [x] Added **Tools page** (8th page, new nav item) with 4 action cards:
  - Full Harvest, Reclassify Other, Export CSV/JSON, Build macOS App
  - Each card has status indicator + polls `/api/status` every 3s while running
- [x] Added "Tools" to page titles map in `switchPage()`
- [x] Verified all **8 pages**: zero JS console errors (Playwright-confirmed)

---

## 🔲 In Progress / Next

- [ ] **Commit session 2 changes** — `app/dashboard.html` + `scripts/dashboard.py` + docs
- [ ] **Push + open PR** for the no-terminal + premium redesign

---

## 🧊 Backlog / Future

- [ ] Test `Export CSV/JSON` endpoint end-to-end (needs live data)
- [ ] Test `Build macOS App` endpoint (needs `scripts/build_app.py` to exist)
- [ ] Add favicon to kill the 404 on every page load
- [ ] Consider adding keyboard shortcut `T` to jump to Tools page
- [ ] Pywebview native window smoke-test of the new design
- [ ] Further visual tweaks after user sees it in native macOS window

---

## Architecture Reference

| File | Role |
|---|---|
| `app/dashboard.html` | Single-file frontend — all CSS/HTML/JS (~2,100 lines) |
| `scripts/dashboard.py` | Flask API server on port 8743, background threads |
| `scripts/llm_provider.py` | Multi-provider LLM (Ollama/Gemini/Claude) |
| `data/tweets.json` | 24,554 tweets |
| `data/config.json` | Accounts + conversation config |
| `data/growth_scores.json` | Pre-computed growth scores |

**Constraint**: No `innerHTML` anywhere — pre-commit hook will reject it. All DOM via `createElement` + `textContent`.
