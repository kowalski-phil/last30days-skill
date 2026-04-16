# last30days — OpenRouter + Apify edition

**A simplified fork of [mvanhorn/last30days-skill](https://github.com/mvanhorn/last30days-skill).**

The original skill is a research engine that searches social platforms, scores results by real engagement (upvotes, likes, views, prediction-market odds), and synthesizes what real people are saying about any topic over the last 30 days. This fork keeps that engine and changes two things:

1. **Simpler setup.** The upstream skill needs six or more API keys (OpenAI, xAI, ScrapeCreators, Brave, browser cookies, yt-dlp). This fork needs **two**: one [OpenRouter](https://openrouter.ai) key for all LLM work, and one [Apify](https://apify.com) token for all social-data collection.
2. **Tighter source set, plus LinkedIn.** The fork removes the consumer/entertainment-heavy sources (TikTok, Instagram, Threads, Pinterest, Bluesky, Truth Social, Xiaohongshu) and adds one the upstream does not have: **LinkedIn**. If you need TikTok or Instagram for brand or creator research, the upstream repo still supports them — this fork trades breadth for a smaller setup.

Full credit to [@mvanhorn](https://github.com/mvanhorn) for the pipeline, the philosophy, and the MIT license that makes forks like this possible.

---

## What's different from upstream

### Sources — 6, not 13+

| Source | Notes |
|--------|-------|
| **Reddit** | Full threads + top comments via Apify |
| **X / Twitter** | Engagement-ranked posts via Apify |
| **YouTube** | Search results + optional transcripts (2 in default depth, 8 in deep) via Apify |
| **LinkedIn** | **New in this fork.** Professional posts with native 30-day server-side date filter, via Apify |
| **Hacker News** | Free Algolia API, no key required |
| **Polymarket** | Free Gamma API, prediction-market odds |

Dropped from upstream: TikTok, Instagram, Threads, Pinterest, Bluesky, Truth Social, Xiaohongshu, Perplexity Sonar, Brave Web Search, browser-cookie X auth.

### Two keys, not six

The upstream `.env` has entries for `OPENAI_API_KEY`, `XAI_API_KEY`, `SCRAPECREATORS_API_KEY`, `BRAVE_API_KEY`, plus Gemini and others. This fork reduces that to:

```env
# Required
OPENROUTER_API_KEY=sk-or-...
APIFY_API_TOKEN=apify_api_...

# Optional — override the default synthesis model
OPENROUTER_MODEL=google/gemini-3.1-flash-lite-preview
```

### Everything is project-level

The upstream skill writes to `~/.config/last30days/` and `~/.local/share/last30days/`. This fork keeps everything inside the repo directory — config at `.env`, database at `data/research.db`, briefs at `data/briefs/`, research dumps at `research/`. Nothing touches your home directory.

### New: trend-radar synthesis layer

The pipeline gives you a raw, ranked dump of evidence. This fork adds `scripts/trend_radar.py`, a second-stage synthesis that turns the raw dump into a structured brief with named themes, counter-signals, a watchlist, and content hooks. See **Trend-radar synthesis** below.

---

## Install

Python 3.12+ required.

```bash
git clone https://github.com/<your-username>/last30days.git
cd last30days
cp .env.example .env
# Edit .env — add your OPENROUTER_API_KEY and APIFY_API_TOKEN
pip install requests apify-client
```

Check the setup:

```bash
python3 scripts/last30days.py setup
```

---

## Use

### Before you search — read this first

> **The tool is only as good as the prompt, and the prompt is only as good as the person doing the prompting.**

A query like `"AI training"` will return 50 items across biology, GPU economics, SQL tutorials, and crypto drama — eight disconnected conversations, not one. A query like `"agentic FP&A"`, or `"regenerative agriculture carbon credits"`, or `"indie game solo-dev burnout"` will return a themed set of real signal. The difference isn't the engine — it's the framing.

Whatever your field — finance, climate, gaming, B2B SaaS, hardware, research — **define who you are and what job you want the tool to do before you pick a query.** Broad queries in any domain produce noise. Specific queries rooted in a real audience and a real job-to-be-done produce signal.

Spend 20 minutes on the strategic session the first time: **[docs/before-you-search.md](docs/before-you-search.md)**.

### Raw research run

```bash
# Default: all 6 sources, last 30 days, compact output
python3 scripts/last30days.py "<your topic>"

# Pick sources
python3 scripts/last30days.py "<your topic>" --search=linkedin,x,youtube,reddit,hackernews

# Deep mode — more items per source, more transcripts
python3 scripts/last30days.py "<your topic>" --deep

# Emit JSON for downstream tools
python3 scripts/last30days.py "<your topic>" --emit=json
```

Raw output is saved to `research/<slug>-raw.md` (gitignored).

### Trend-radar synthesis

Turn a raw dump into a themed brief:

```bash
# Run retrieval + synthesis in one shot
python3 scripts/trend_radar.py "<your topic>"

# Synthesize from an existing raw dump (free — no retrieval cost)
python3 scripts/trend_radar.py "<your topic>" --from-raw research/<slug>-raw.md

# Preview the prompt without calling the LLM
python3 scripts/trend_radar.py "<your topic>" --from-raw research/<slug>-raw.md --dry-run
```

Brief output is saved to `data/briefs/<slug>-trend-radar-<date>.md` (gitignored).

The brief has a fixed schema: **TL;DR → 3–5 named themes with citations and velocity labels → counter-signals → watchlist (people, vendors, metrics to verify) → content hooks.** The prompt is audience-agnostic — works the same for any niche, as long as the query is tight enough.

### Other existing features (inherited from upstream)

These were not touched by this fork and still work:

- **`--mock`** — run the full pipeline on fixtures in `fixtures/` without calling any API
- **Watchlist** — `python3 scripts/watchlist.py` persists topics for recurring research
- **GitHub person-mode** — pass `--github-user=<handle>` on a person topic to pull PR velocity and repo activity from the GitHub CLI
- **ELI5 mode** — a synthesis-style toggle that rewrites the compact brief in plain language (set `ELI5_MODE=true` in `.env`)

---

## Architecture at a glance

```
Topic → Plan → Retrieve → Normalize → Dedupe → Fuse → Rerank → Cluster → Render → Raw brief
                                                                                     │
                                                                            trend_radar.py
                                                                                     │
                                                                           Trend-radar brief
```

- **Planner, reranker, synthesis** — all LLM calls route through OpenRouter (`scripts/lib/providers.py`)
- **Reddit, X, YouTube, LinkedIn** — Apify actor wrappers under `scripts/lib/apify_*.py`
- **Hacker News, Polymarket, GitHub** — free public APIs, no keys
- **SourceItem contract** (`scripts/lib/schema.py`) is the shared shape every source normalizes into. The downstream pipeline (dedupe, fusion, rerank, cluster, render) is untouched from upstream.

See `CLAUDE.md` at the repo root for the in-depth architecture notes.

---

## Cost

Typical costs per query (rough, 2026 pricing):

- **Full default run across all 6 sources:** ~$0.50 in Apify compute (LinkedIn is the single most expensive actor at ~$1.50/1k posts)
- **Synthesis call** (GPT-4.1 via OpenRouter): ~$0.05–0.15 per brief
- **Hacker News, Polymarket, GitHub:** free

A solo user running 4 standing topics daily sits around $50–100/month in retrieval plus synthesis.

---

## What's private vs. pushed

- `research/` and `data/briefs/` — the **folders** are in the repo, but the **files** (raw dumps and synthesized briefs) are personal and gitignored
- `.env` is gitignored (secrets)
- `release-notes.md` from upstream is gitignored (superseded by this fork)

---

## License

MIT, inherited from the original. See [LICENSE](LICENSE).

Original repo: [mvanhorn/last30days-skill](https://github.com/mvanhorn/last30days-skill).
