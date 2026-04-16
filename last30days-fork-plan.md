# last30days Fork Plan: OpenRouter + Apify Edition

## What This Document Is

A planning document for forking [mvanhorn/last30days-skill](https://github.com/mvanhorn/last30days-skill) and replacing its data collection and LLM layers with OpenRouter (unified LLM gateway) and Apify (unified scraping platform). Import this into your project root as a reference for Claude Code when you start building.

---

## The Original Skill — What It Does

`/last30days` is a research skill that answers: *"What are real people actually saying about [TOPIC] in the last 30 days?"*

It searches social platforms (Reddit, X, YouTube, LinkedIn, Hacker News, Polymarket, GitHub, and more), scores results by real engagement metrics (upvotes, likes, views, prediction market odds), and synthesizes everything into a briefing.

The key insight: it ranks by what real people engage with — not by what editors or SEO algorithms surface.

---

## Why Fork

The original skill requires API keys from multiple providers:
- OpenAI (for Reddit discovery via Responses API + web_search)
- xAI (for X/Twitter via Responses API + x_search)
- ScrapeCreators (for TikTok, Instagram, Threads, Pinterest — we won't use most of these)
- Plus optional: Brave Search, Exa, browser cookies for X auth

**Our approach:** Centralize everything through two services we already use and trust:
- **OpenRouter** — one API key, access to any LLM (Claude, GPT, Gemini, etc.)
- **Apify** — one API key, access to scrapers for every platform via their actor marketplace

---

## Architecture: Original vs. Fork

### Original

```
OpenAI Responses API (search + LLM)  ──→ Reddit discovery
xAI Responses API (search + LLM)     ──→ X/Twitter search
ScrapeCreators API                    ──→ TikTok, IG, Threads, Pinterest (original)
yt-dlp                                ──→ YouTube
Public APIs                           ──→ Hacker News (Algolia), Polymarket (Gamma)
Browser cookies                       ──→ X auth fallback
        │
        ▼
   normalize → score → dedupe → cluster → rerank → render
```

### Our Fork

```
Apify actors (per platform)           ──→ Reddit, X, YouTube, LinkedIn
Public APIs                           ──→ Hacker News (Algolia), Polymarket (Gamma)
OpenRouter (LLM calls only)           ──→ Planning, reranking, synthesis, rendering
        │
        ▼
   normalize → score → dedupe → cluster → rerank → render
   (this entire pipeline stays untouched)
```

---

## The Processing Pipeline (DO NOT TOUCH)

The beauty of this repo is its clean separation of concerns. The entire downstream pipeline works on a canonical `SourceItem` format and doesn't care where the data came from. These modules stay as-is:

| Module | What it does |
|---|---|
| `schema.py` | Defines the `SourceItem` dataclass |
| `normalize.py` | Orchestrates per-source normalizers into canonical format |
| `relevance.py` | Token-overlap scoring for query relevance |
| `dedupe.py` | Removes near-duplicate items (0.7 similarity threshold) |
| `fusion.py` | Reciprocal Rank Fusion across subqueries and sources |
| `rerank.py` | LLM-based relevance scoring (scoring logic stays, LLM routing changes) |
| `cluster.py` | Groups results by theme using text similarity |
| `signals.py` | Annotates relevance, engagement, freshness |
| `snippet.py` | Extracts text excerpts |
| `render.py` | Outputs markdown/JSON in compact/full/context formats |
| `grounding.py` | Optional fact grounding via web search |
| `pipeline.py` | Orchestrates the full research workflow |
| `query.py` | Query handling |
| `log.py` | Logging |

---

## The Canonical Data Format: SourceItem

This is the contract between the scraping layer and the processing pipeline. Every Apify wrapper you write must output objects that match this shape:

```python
SourceItem(
    item_id: str,                    # Unique ID (e.g., Reddit post ID, tweet ID)
    source: str,                     # "Reddit", "X", "YouTube", "LinkedIn", etc.
    url: str,                        # Direct link to the content
    title: str,                      # Post/video title
    body: str,                       # Full text content
    snippet: str,                    # Short excerpt
    published_at: str | None,        # ISO date, e.g. "2026-04-10"
    date_confidence: "high"|"med"|"low",
    author: str | None,              # Username/handle
    container: str | None,           # Subreddit, channel, hashtag, etc.
    engagement: dict,                # Platform-specific metrics (see below)
    relevance_hint: float,           # 0-1, initial relevance estimate
    why_relevant: str,               # Brief explanation
    metadata: dict                   # Platform-specific extras (see below)
)
```

### Engagement Dict — Examples Per Platform

```python
# Reddit
{"upvotes": 342, "comments": 47}

# X/Twitter
{"likes": 1200, "retweets": 89, "replies": 34}

# YouTube
{"views": 45000, "likes": 890, "comments": 120}

# LinkedIn
{"likes": 230, "comments": 45, "reposts": 12}

# Hacker News
{"points": 287, "comments": 134}

# Polymarket
{"market_odds": 0.73, "volume": 450000}
```

### Metadata Dict — Platform-Specific Extras

This is where you store anything useful that doesn't fit the standard fields:

```python
# Reddit: top comments from the thread
{"top_comments": ["comment 1 text", "comment 2 text", ...]}

# YouTube: transcript excerpt, channel info
{"transcript": "first 500 chars...", "channel": "channel name"}

# GitHub: stars, language, last release
{"stars": 12400, "language": "Python", "last_release": "v3.2.1"}
```

**The key principle:** The engagement and metadata dicts are flexible. Stuff whatever the Apify actor returns into the right bucket. The scoring pipeline reads specific keys but handles missing ones gracefully. If an Apify actor returns data in a different shape than expected, just transform it in your normalizer function — that's what normalizers are for.

---

## What Needs to Change: Module-by-Module

### Layer 1: Scraping — Rewrite or Replace

These modules currently use provider-specific APIs. Replace them with Apify actor wrappers.

| Original Module | Current Approach | Your Approach |
|---|---|---|
| `openai_reddit.py` | OpenAI Responses API + web_search | Apify Reddit actor |
| `reddit_public.py` | Reddit public JSON endpoints | Apify Reddit actor (consolidate) |
| `reddit_enrich.py` | Enriches threads with comments | Apify can return comments — fold into Reddit wrapper |
| `xai_x.py` | xAI Responses API + x_search | Apify X/Twitter actor |
| `bird_x.py` | Vendored Node.js Bird client | Delete — Apify handles this |
| `youtube_yt.py` | yt-dlp CLI tool | Apify YouTube actor |
| `instagram.py` | ScrapeCreators API | Delete — project focus is B2B, IG dropped |
| *(new)* `linkedin.py` | Not in original | **New module** — Apify LinkedIn actor |
| `tiktok.py` | ScrapeCreators API | Delete — not needed |
| `threads.py` | ScrapeCreators API | Delete — not needed |
| `pinterest.py` | ScrapeCreators API | Delete — not needed |
| `bluesky.py` | Direct API + app password | Delete — not needed |
| `chrome_cookies.py` | Browser cookie extraction for X auth | Delete |
| `safari_cookies.py` | Browser cookie extraction for X auth | Delete |
| `cookie_extract.py` | Cookie extraction utilities | Delete |

**Modules that stay as-is (free public APIs):**
- `hackernews.py` — Uses Algolia API, free, no auth needed
- `polymarket.py` — Uses Gamma API, free, no auth needed
- `github.py` — Uses `gh` CLI, already authenticated on your machine

### Layer 2: LLM Calls — Reroute to OpenRouter

These modules make LLM calls for planning, reranking, and synthesis. Reroute them through OpenRouter.

| Module | What to change |
|---|---|
| `providers.py` | Central LLM routing — point all `generate_json()` / completion calls at OpenRouter's API endpoint. Model selection becomes an OpenRouter model string (e.g., `anthropic/claude-sonnet-4-20250514`). |
| `planner.py` | Uses LLM for query planning — will work once providers.py routes through OpenRouter |
| `rerank.py` | Uses LLM for relevance scoring — same, just needs providers.py change |
| `resolve.py` | Uses web search for entity resolution — may need a lightweight Apify web search actor or Brave/Serper key |

### Layer 3: Configuration — Simplify

| Module | What to change |
|---|---|
| `env.py` | Strip out all the individual API key detection. Replace with two keys: `OPENROUTER_API_KEY` and `APIFY_API_TOKEN`. Keep the HN/Polymarket/GitHub detection (those are free). |
| `setup_wizard.py` | Massively simplify — you only need to configure 2 keys instead of 6+. Or just delete and document the `.env` setup in a README. |
| `quality_nudge.py` | Update source availability checks. Instead of checking for X cookies and yt-dlp, check if Apify token is valid and which actors are accessible. |
| `models.py` | Model version caching — OpenRouter handles this. Simplify or remove. |

### Layer 4: Delete (No Longer Needed)

- `vendor/` directory (Node.js Bird client for X)
- `chrome_cookies.py`, `safari_cookies.py`, `cookie_extract.py`
- `bird_x.py`
- `tiktok.py`, `threads.py`, `pinterest.py`, `bluesky.py`
- Any ScrapeCreators-specific utilities

---

## Writing an Apify Wrapper — The Pattern

Each platform needs a thin wrapper that:
1. Calls the Apify actor
2. Waits for results
3. Transforms the output into `SourceItem` objects

Here's the general shape (not rigid — adapt to whatever Apify returns):

```python
"""
Wrapper for [Platform] via Apify.
Calls the Apify actor, transforms results into SourceItem objects.
"""

from apify_client import ApifyClient
from .schema import SourceItem

def search_platform(topic: str, date_range: tuple, apify_token: str) -> list[SourceItem]:
    client = ApifyClient(apify_token)

    # 1. Run the actor with search parameters
    run = client.actor("actor-id-from-apify-store").call(
        run_input={
            # Actor-specific input — varies per actor
            # Check Apify store docs for each one
        }
    )

    # 2. Collect results
    raw_items = list(client.dataset(run["defaultDatasetId"]).iterate_items())

    # 3. Transform to SourceItem
    results = []
    for item in raw_items:
        results.append(SourceItem(
            item_id=item.get("id", ""),
            source="Platform",
            url=item.get("url", ""),
            title=item.get("title", ""),
            body=item.get("text", ""),
            snippet=item.get("text", "")[:200],
            published_at=item.get("date"),          # normalize to YYYY-MM-DD
            date_confidence="high",
            author=item.get("author"),
            container=item.get("subreddit")          # or channel, hashtag, etc.
            engagement={...},                        # map platform metrics
            relevance_hint=0.5,
            why_relevant="",
            metadata={...}                           # anything extra
        ))

    return results
```

**Don't over-engineer this.** Each wrapper is ~30-50 lines. The Apify actor does the heavy lifting. You just map fields. If Apify returns something unexpected, transform it. That's what this layer is for.

---

## Apify Actor Research Needed

Browse the [Apify Store](https://apify.com/store) and find actors for each platform. Things to look for:

- **Input format:** What search parameters does the actor accept? (keyword, date range, max results)
- **Output format:** What fields does it return? (This determines your normalizer mapping)
- **Rate limits / pricing:** Apify charges per compute unit. Some actors are cheap, some aren't.
- **Reliability:** Check ratings, recent updates, and user reviews in the store

Platforms to find actors for:
- [ ] Reddit (search + comments)
- [ ] X/Twitter (search by keyword + date range)
- [ ] YouTube (search + metadata, ideally transcripts)
- [ ] LinkedIn (post search by keyword/topic — this is the new addition, not in the original skill)
- [ ] General web search (as a fallback for entity resolution)

---

## Environment File (.env)

The fork needs only this:

```env
# Required
OPENROUTER_API_KEY=sk-or-...
APIFY_API_TOKEN=apify_api_...

# Optional — model preferences
OPENROUTER_MODEL=anthropic/claude-sonnet-4-20250514

# Optional — keep if you want GitHub integration
# (uses gh CLI, no extra key needed if already authenticated)
```

---

## Execution Order: How to Build This

This isn't a strict plan — it's a suggested sequence. Vibe code your way through it.

**Phase 1: Get it running with what's free**
- Fork and clone the repo
- Import this document into the project root
- Get the original skill running with just Hacker News + Polymarket (zero config)
- Understand the data flow by watching it work end-to-end

**Phase 2: OpenRouter for LLM**
- Modify `providers.py` to route through OpenRouter
- Modify `env.py` to read `OPENROUTER_API_KEY`
- Test that planning and reranking still work
- This is the smallest change with the biggest payoff — everything downstream benefits

**Phase 3: Apify for one platform (start with Reddit)**
- Find the right Reddit actor on Apify Store
- Write the wrapper, map to SourceItem
- Replace `openai_reddit.py` with your Apify version
- Test end-to-end: `/last30days some topic` should return Reddit + HN + Polymarket results

**Phase 4: Add platforms one by one**

YouTube is split into two sub-phases because transcripts are expensive and add complexity.

**Phase 4a — YouTube search (metadata only)**
- Find an Apify actor that searches YouTube and returns video metadata: `video_id`, `url`, `title`, `description`, `channel_name`, `date`, `engagement` (views, likes, comments)
- No transcripts in this phase — search-only flow first
- Write the wrapper, map to the format `_normalize_youtube` expects in `normalize.py`
- Replace `youtube_yt.py` (which uses local yt-dlp CLI)
- Test end-to-end: `/last30days some topic --search=youtube,reddit,hackernews,polymarket` should return YouTube videos in the results
- Validate cost in the Apify console before moving to 4b

**Phase 4b — YouTube transcripts (optional enrichment)**
- Find a separate Apify transcript actor (e.g. `pintostudio/youtube-transcript-scraper` or similar)
- For the top N videos (N controlled by depth profile: quick=0, default=2, deep=8), call the transcript actor
- Populate `transcript_snippet` and `transcript_highlights` fields
- This is what gives YouTube its real research value — letting the LLM rerank by what's actually said in the video, not just the title

**Phase 4c onwards — other platforms**
- X/Twitter
- LinkedIn (new — not in original, you're adding this)
- ~~Instagram~~ — dropped; project focus is B2B, Instagram is not a fit channel

**Phase 5: Clean up**
- Delete dead modules (cookies, bird_x, vendor/, tiktok, threads, pinterest, bluesky)
- Simplify setup_wizard or replace with a README
- Update quality_nudge for Apify-based source detection
- Remove any references to deleted platforms in planner.py, pipeline.py, etc.

---

## Sharing with Early AI-dopters

Once your fork works:
- Push to your GitHub (it's MIT licensed, you're good)
- Share the repo link in the community
- Others clone your fork, add their own `.env` with their OpenRouter + Apify keys
- Consider writing a brief setup guide in the README — the original one is detailed, yours can be simpler since you only need 2 keys

---

## Open Questions to Resolve During Build

- **Entity resolution:** The original uses OpenAI web_search to discover X handles and subreddits before searching. You'll need an alternative — either an Apify web search actor, or Brave Search API (free 2k/month), or just skip auto-resolution and let the user provide handles via flags.
- **Apify async vs sync:** Apify actors can run async. For parallel platform searches, you may want to fire all actors simultaneously and collect results. Check if the `apify-client` Python SDK supports this cleanly.
- **Cost:** Apify charges per compute unit. Run a few test searches and check what a typical `/last30days` query costs across all platforms. Compare to the current approach (OpenAI + xAI API calls).
- **Date filtering:** Some Apify actors support date range filtering natively, others don't. If an actor returns all-time results, you'll filter in the normalizer. The pipeline already handles date filtering in `normalize.py`.

---

*Fork source: [mvanhorn/last30days-skill](https://github.com/mvanhorn/last30days-skill) (MIT License, Copyright 2026 Matt Van Horn)*
