# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A fork of [mvanhorn/last30days-skill](https://github.com/mvanhorn/last30days-skill) — a multi-source AI research engine that aggregates what real people are saying about any topic across social platforms in the last 30 days, scored by real engagement (upvotes, likes, views) rather than editorial algorithms.

**This fork replaces the original's scattered API keys with two unified services:**
- **OpenRouter** — single LLM gateway for all planning, reranking, and synthesis (replaces direct OpenAI/xAI/Gemini calls)
- **Apify** — single scraping platform for all social data collection (replaces ScrapeCreators, yt-dlp, Bird client, browser cookies)

See `last30days-fork-plan.md` for the full migration plan and rationale.

Python 3.12+ required; runtime dependency is `requests>=2.32,<3` (plus `apify-client` once Apify wrappers are added).

## Everything Is Project-Level

**Nothing in this project reads from or writes to global/system locations.** No `~/.config/`, no `~/.local/share/`, no `~/.claude/skills/`, no `~/.codex/`. Everything lives inside this repo directory:

- **Config:** `.env` in the project root (copy from `.env.example`)
- **Database:** `data/research.db` (auto-created on first `--store` run)
- **Briefings:** `data/briefs/` (auto-created)
- **Output:** `--save-dir` defaults to current directory

To set up: `cp .env.example .env` and add your keys.

## Commands

```bash
# Run research
python3 scripts/last30days.py "topic" --emit=compact
python3 scripts/last30days.py "topic" --emit=json --search=hackernews,polymarket --deep
python3 scripts/last30days.py "topic" --mock          # Use fixtures, no API calls
python3 scripts/last30days.py "topic" --debug          # Enable HTTP logging

# Tests
python3 -m pytest                                      # All tests (50+ files)
python3 -m pytest tests/test_dates_v3.py -v            # Single test file
python3 -m pytest --cov=scripts --cov-report=term-missing  # With coverage
```

## Architecture

### Pipeline (8 stages, orchestrated by `scripts/lib/pipeline.py`)

```
Topic → Plan → Retrieve → Normalize → Dedupe → Fuse → Rerank → Cluster → Render → Report
```

1. **Planning** (`planner.py`) — Classifies intent, selects sources, generates `SubQuery` objects. LLM calls route through OpenRouter.
2. **Retrieval** — Parallel per-source calls via `ThreadPoolExecutor` with timeout budgets. Apify actors replace per-provider API modules (once migrated).
3. **Normalization** (`normalize.py`) — Raw responses → canonical `SourceItem` schema.
4. **Deduplication** (`dedupe.py`) — Near-duplicate removal via cosine similarity.
5. **Fusion** (`fusion.py`) — Weighted Reciprocal Rank Fusion across subqueries → global `Candidate` scores.
6. **Reranking** (`rerank.py`, `signals.py`) — LLM or heuristic relevance scoring, engagement/freshness signals.
7. **Clustering** (`cluster.py`) — Entity-based grouping (same story from multiple sources = one cluster).
8. **Rendering** (`render.py`) — Output as compact markdown, full markdown, JSON, or context snippet.

**The downstream pipeline (steps 3-8) is untouched by this fork.** Only the retrieval layer (step 2) and LLM routing (steps 1, 6) change.

### Data Model (`schema.py`) — Immutable frozen dataclasses

- **`SubQuery`** — Single retrieval unit (search_query, ranking_query, sources, weight)
- **`QueryPlan`** — Planner output (intent, freshness_mode, subqueries)
- **`SourceItem`** — Normalized result from any source — this is the contract between scraping and pipeline
- **`Candidate`** — Globally scored item after fusion/rerank
- **`Cluster`** — Ranked group of related candidates across sources
- **`Report`** — Final output container

### Entry Points

| Script | Purpose |
|--------|---------|
| `scripts/last30days.py` | Main CLI — research engine |
| `scripts/watchlist.py` | Persistent topic watchlists |
| `scripts/briefing.py` | Synthesize watchlist into a single brief |
| `scripts/store.py` | SQLite research persistence (writes to `data/`) |

## Fork Migration: What Changes vs. What Stays

### Done: LLM routing → OpenRouter

`providers.py` now routes through OpenRouter as the primary LLM provider. Auto-detection priority: OpenRouter → Gemini → OpenAI → xAI → deterministic fallback. Set `OPENROUTER_MODEL` in `.env` to override the default model (`google/gemini-3.1-flash-lite-preview`).

### Done: Config → project-level

`env.py` reads `.env` from the project root. No global paths. Codex auth, browser cookie extraction, and ScrapeCreators config have been removed. Compatibility stubs exist in `env.py` for `pipeline.py` references to removed platforms — these return "not available" so the pipeline skips them.

### TODO: Scraping → Apify actors

| Original Module | Replacement |
|---|---|
| `openai_reddit.py`, `reddit_public.py`, `reddit_enrich.py` | Single Apify Reddit wrapper |
| `xai_x.py`, `bird_x.py` | Apify X/Twitter wrapper |
| `youtube_yt.py` | Apify YouTube wrapper |
| `instagram.py` | Apify Instagram wrapper |
| *(new)* `linkedin.py` | **New** — Apify LinkedIn wrapper (not in original) |

### TODO: Delete dead modules

- `bird_x.py`, `tiktok.py`, `threads.py`, `pinterest.py`, `bluesky.py`, `truthsocial.py`, `xiaohongshu_api.py`
- `cookie_extract.py`, `chrome_cookies.py`, `safari_cookies.py`
- `vendor/` directory (Node.js Bird client)
- `xquik.py`

### Modules that STAY AS-IS

- **Pipeline:** `normalize.py`, `dedupe.py`, `fusion.py`, `cluster.py`, `signals.py`, `snippet.py`, `render.py`, `pipeline.py`, `schema.py`, `relevance.py`, `query.py`, `log.py`
- **Free public APIs:** `hackernews.py` (Algolia), `polymarket.py` (Gamma), `github.py` (gh CLI)
- **Infrastructure:** `dates.py`, `http.py`, `ui.py`

## Config & Environment

Project-level `.env` at repo root. Copy from `.env.example`:

```env
# Required
OPENROUTER_API_KEY=sk-or-...
APIFY_API_TOKEN=apify_api_...

# Optional — model preference
OPENROUTER_MODEL=google/gemini-3.1-flash-lite-preview
```

**Protected by `.gitignore`** — `.env`, `data/`, and `output/` are never committed.

## Critical Rules

- **`lib/__init__.py` must stay as a bare package marker** — comment only, NO eager imports. Prevents circular dependencies.
- **The SourceItem contract is sacred.** Every Apify wrapper must output objects matching the `SourceItem` dataclass in `schema.py`. The entire downstream pipeline depends on this shape.
- **Source modules are one-per-file** — fetch raw data → return list of dicts → pipeline normalizes to `SourceItem`.
- **`--mock` flag** uses fixture files from `fixtures/` for testing without API keys.
- **Depth profiles** (`quick`/`default`/`deep`) control per-stream limits and pool sizes.
