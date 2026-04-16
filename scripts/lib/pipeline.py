"""v3.0.0 orchestration pipeline."""

from __future__ import annotations

import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from shutil import which
from typing import Any

from . import (
    apify_linkedin,
    apify_reddit,
    apify_x,
    apify_youtube,
    dates,
    dedupe,
    entity_extract,
    env,
    github,
    grounding,
    hackernews,
    normalize,
    perplexity,
    planner,
    polymarket,
    providers,
    query,
    reddit,
    reddit_public,
    rerank,
    schema,
    signals,
    snippet,
    youtube_yt,
)
from .cluster import cluster_candidates
from .fusion import weighted_rrf

DEPTH_SETTINGS = {
    "quick": {"per_stream_limit": 6, "pool_limit": 15, "rerank_limit": 12},
    "default": {"per_stream_limit": 12, "pool_limit": 40, "rerank_limit": 40},
    "deep": {"per_stream_limit": 20, "pool_limit": 60, "rerank_limit": 60},
}

SEARCH_ALIAS = {
    "hn": "hackernews",
    "web": "grounding",
    "li": "linkedin",
}

MAX_SOURCE_FETCHES: dict[str, int] = {"x": 2, "linkedin": 2}

MOCK_AVAILABLE_SOURCES = [
    "reddit",
    "x",
    "linkedin",
    "youtube",
    "hackernews",
    "polymarket",
    "grounding",
    "github",
    "perplexity",
]


def normalize_requested_sources(sources: list[str] | None) -> list[str] | None:
    if not sources:
        return None
    normalized = []
    for source in sources:
        key = SEARCH_ALIAS.get(source.lower(), source.lower())
        if key not in normalized:
            normalized.append(key)
    return normalized


def available_sources(config: dict[str, Any], requested_sources: list[str] | None = None) -> list[str]:
    available: list[str] = []
    # reddit_public needs no API key - always available
    available.append("reddit")
    if env.get_x_source(config):
        available.append("x")
    if config.get("APIFY_API_TOKEN"):
        available.append("linkedin")
    if config.get("APIFY_API_TOKEN") or which("yt-dlp"):
        available.append("youtube")
    available.extend(["hackernews", "polymarket"])
    if config.get("GITHUB_TOKEN") or which("gh"):
        available.append("github")
    if config.get("BRAVE_API_KEY") or config.get("EXA_API_KEY") or config.get("SERPER_API_KEY") or config.get("PARALLEL_API_KEY"):
        available.append("grounding")
    # Perplexity Sonar: opt-in additive source via INCLUDE_SOURCES=perplexity
    include_sources = (config.get("INCLUDE_SOURCES") or "").lower().split(",")
    if config.get("OPENROUTER_API_KEY") and "perplexity" in include_sources:
        available.append("perplexity")
    return available


def diagnose(config: dict[str, Any], requested_sources: list[str] | None = None) -> dict[str, Any]:
    requested_sources = normalize_requested_sources(requested_sources)
    google_key = _google_key(config)
    x_status = env.get_x_source_status(config)
    native_web_backend = None
    if config.get("BRAVE_API_KEY"):
        native_web_backend = "brave"
    elif config.get("EXA_API_KEY"):
        native_web_backend = "exa"
    elif config.get("SERPER_API_KEY"):
        native_web_backend = "serper"
    elif config.get("PARALLEL_API_KEY"):
        native_web_backend = "parallel"
    providers_status = {
        "openrouter": bool(config.get("OPENROUTER_API_KEY")),
        "apify": bool(config.get("APIFY_API_TOKEN")),
    }
    return {
        "providers": providers_status,
        "local_mode": not any(providers_status.values()),
        "reasoning_provider": (config.get("LAST30DAYS_REASONING_PROVIDER") or "auto").lower(),
        "x_backend": x_status["source"],
        "native_web_backend": native_web_backend,
        "has_github": bool(config.get("GITHUB_TOKEN") or which("gh")),
        "available_sources": available_sources(config, requested_sources),
    }


def run(
    *,
    topic: str,
    config: dict[str, Any],
    depth: str,
    requested_sources: list[str] | None = None,
    mock: bool = False,
    x_handle: str | None = None,
    x_related: list[str] | None = None,
    web_backend: str = "auto",
    external_plan: dict | None = None,
    subreddits: list[str] | None = None,
    lookback_days: int = 30,
    github_user: str | None = None,
    github_repos: list[str] | None = None,
) -> schema.Report:
    settings = DEPTH_SETTINGS[depth]
    requested_sources = normalize_requested_sources(requested_sources)
    from_date, to_date = dates.get_date_range(lookback_days)

    if mock:
        runtime = providers.mock_runtime(config, depth)
        reasoning_provider = None
        available = list(requested_sources or MOCK_AVAILABLE_SOURCES)
    else:
        runtime, reasoning_provider = providers.resolve_runtime(config, depth)
        available = available_sources(config, requested_sources)
        if requested_sources:
            available = [source for source in available if source in requested_sources]
    if web_backend == "none":
        available = [s for s in available if s != "grounding"]
    elif web_backend in ("brave", "exa", "serper") and "grounding" not in available:
        available.append("grounding")
    if not available:
        raise RuntimeError("No sources are available for this run.")

    if external_plan:
        # External plan provided (e.g., from Claude Code via --plan flag).
        # Parse it through the same sanitizer to validate structure.
        plan = planner._sanitize_plan(
            external_plan, topic, available, requested_sources, depth,
        )
        print(f"[Planner] Using external plan ({len(plan.subqueries)} subqueries)", file=sys.stderr)
    else:
        plan = planner.plan_query(
            topic=topic,
            available_sources=available,
            requested_sources=requested_sources,
            depth=depth,
            provider=None if mock else reasoning_provider,
            model=None if mock else runtime.planner_model,
            context=config.get("_auto_resolve_context", ""),
        )

    # Safety net: ensure grounding appears in all subqueries even if the planner
    # omits it. This is redundant when the planner includes grounding via
    # SOURCE_CAPABILITIES, but kept as a fallback.
    if web_backend != "none" and "grounding" in available:
        for sq in plan.subqueries:
            if "grounding" not in sq.sources:
                sq.sources.append("grounding")

    bundle = schema.RetrievalBundle(artifacts={"grounding": []})

    # Project-mode or person-mode GitHub: run once before the main subquery loop
    _github_custom_done = False
    _github_enriched_repos: set[str] = set()

    # Project mode takes priority over person mode
    if github_repos and "github" in available:
        try:
            project_items = github.search_github_project(
                github_repos, from_date, to_date,
                depth=depth, token=config.get("GITHUB_TOKEN"),
            )
            if project_items:
                normalized = _normalize_score_dedupe(
                    "github", project_items, from_date, to_date,
                    freshness_mode=plan.freshness_mode,
                    ranking_query=f"What are {', '.join(github_repos)} doing on GitHub?",
                )
                primary_label = plan.subqueries[0].label if plan.subqueries else "primary"
                bundle.add_items(primary_label, "github", normalized)
                _github_custom_done = True
                _github_enriched_repos = {r.lower() for r in github_repos}
        except Exception as exc:
            bundle.errors_by_source["github"] = f"Project-mode failed: {exc}"

    _github_person_done = False
    if github_user and "github" in available and not _github_custom_done:
        try:
            person_items = github.search_github_person(
                github_user, from_date, to_date,
                depth=depth, token=config.get("GITHUB_TOKEN"),
            )
            if person_items:
                normalized = _normalize_score_dedupe(
                    "github", person_items, from_date, to_date,
                    freshness_mode=plan.freshness_mode,
                    ranking_query=f"What is @{github_user} doing on GitHub?",
                )
                # Use the first subquery's label so RRF can look up the weight
                primary_label = plan.subqueries[0].label if plan.subqueries else "primary"
                bundle.add_items(primary_label, "github", normalized)
                _github_person_done = True
        except Exception as exc:
            bundle.errors_by_source["github"] = f"Person-mode failed: {exc}"

    # Thread-safe set prevents redundant fetches after a source returns 429
    rate_limited_sources: set[str] = set()
    rate_limit_lock = threading.Lock()

    futures = {}
    # Per-source fetch budget prevents redundant API calls
    source_fetch_count: dict[str, int] = {}
    stream_count = sum(
        1
        for subquery in plan.subqueries
        for source in subquery.sources
        if source in available
    )
    max_workers = max(4, min(16, stream_count or 1))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for subquery in plan.subqueries:
            for source in subquery.sources:
                if source not in available:
                    continue
                # Skip GitHub keyword search if person-mode already ran
                if source == "github" and (_github_person_done or _github_custom_done):
                    continue
                # Enforce per-source fetch cap
                cap = MAX_SOURCE_FETCHES.get(source)
                if cap is not None:
                    current = source_fetch_count.get(source, 0)
                    if current >= cap:
                        continue
                    source_fetch_count[source] = current + 1
                futures[
                    executor.submit(
                        _retrieve_stream,
                        topic=topic,
                        subquery=subquery,
                        source=source,
                        config=config,
                        depth=depth,
                        date_range=(from_date, to_date),
                        runtime=runtime,
                        mock=mock,
                        rate_limited_sources=rate_limited_sources,
                        rate_limit_lock=rate_limit_lock,
                        web_backend=web_backend,
                        raw_topic=topic,
                        subreddits=subreddits,
                    )
                ] = (subquery, source)

        for future in as_completed(futures):
            subquery, source = futures[future]
            try:
                raw_items, artifact = future.result()
            except Exception as exc:
                # Share 429 signal so pending futures skip this source
                if _is_rate_limit_error(exc):
                    with rate_limit_lock:
                        rate_limited_sources.add(source)
                    bundle.errors_by_source[source] = str(exc)
                    continue
                # Retry once for transient 5xx errors
                if _is_transient_error(exc):
                    time.sleep(3)
                    try:
                        raw_items, artifact = _retrieve_stream(
                            topic=topic, subquery=subquery, source=source,
                            config=config, depth=depth, date_range=(from_date, to_date),
                            runtime=runtime, mock=mock,
                            rate_limited_sources=rate_limited_sources,
                            rate_limit_lock=rate_limit_lock,
                            web_backend=web_backend,
                            raw_topic=topic,
                            subreddits=subreddits,
                        )
                    except Exception as retry_exc:
                        bundle.errors_by_source[source] = f"{exc} (retried once, still failed: {retry_exc})"
                        continue
                else:
                    bundle.errors_by_source[source] = str(exc)
                    continue
            normalized = _normalize_score_dedupe(
                source, raw_items, from_date, to_date,
                freshness_mode=plan.freshness_mode,
                ranking_query=subquery.ranking_query,
            )
            normalized = normalized[: settings["per_stream_limit"]]
            bundle.add_items(subquery.label, source, normalized)
            if artifact:
                bundle.artifacts.setdefault("grounding", []).append(artifact)

    # Phase 2: retry thin sources with simplified query
    # Note: _github_skip_sources tells the retry to not re-run GitHub keyword search
    # when project-mode or person-mode already provided authoritative data.
    _github_skip_retry = {"github"} if (_github_person_done or _github_custom_done) else set()
    _retry_thin_sources(
        topic=topic,
        bundle=bundle,
        plan=plan,
        config=config,
        depth=depth,
        date_range=(from_date, to_date),
        runtime=runtime,
        mock=mock,
        rate_limited_sources=rate_limited_sources,
        rate_limit_lock=rate_limit_lock,
        settings=settings,
        web_backend=web_backend,
        skip_sources=_github_skip_retry,
    )

    # Clear errors for sources that returned items despite partial failures.
    # A source that 429'd on one subquery but succeeded on another is not "errored".
    for source in list(bundle.errors_by_source):
        if bundle.items_by_source.get(source):
            del bundle.errors_by_source[source]

    items_by_source = _finalize_items_by_source(bundle.items_by_source)
    candidates = weighted_rrf(bundle.items_by_source_and_query, plan, pool_limit=settings["pool_limit"])
    ranked_candidates = rerank.rerank_candidates(
        topic=topic,
        plan=plan,
        candidates=candidates,
        provider=None if mock else reasoning_provider,
        model=None if mock else runtime.rerank_model,
        shortlist_size=settings["rerank_limit"],
    )
    rerank.score_fun(
        topic=topic,
        candidates=ranked_candidates,
        provider=None if mock else reasoning_provider,
        model=None if mock else runtime.rerank_model,
    )

    # Phase 3: post-rerank GitHub star enrichment
    if "github" in available and not mock:
        github.enrich_candidates_with_stars(
            ranked_candidates,
            token=config.get("GITHUB_TOKEN"),
            already_enriched=_github_enriched_repos,
        )

    clusters = cluster_candidates(ranked_candidates, plan)
    warnings = _warnings(items_by_source, ranked_candidates, bundle.errors_by_source)

    return schema.Report(
        topic=topic,
        range_from=from_date,
        range_to=to_date,
        generated_at=datetime.now(timezone.utc).isoformat(),
        provider_runtime=runtime,
        query_plan=plan,
        clusters=clusters,
        ranked_candidates=ranked_candidates,
        items_by_source=items_by_source,
        errors_by_source=bundle.errors_by_source,
        warnings=warnings,
        artifacts=bundle.artifacts,
    )


def _normalize_score_dedupe(
    source: str,
    raw_items: list[dict],
    from_date: str,
    to_date: str,
    freshness_mode: str,
    ranking_query: str,
) -> list[schema.SourceItem]:
    """Normalize, annotate, prune, dedupe, and extract snippets for a batch of raw items."""
    normalized = normalize.normalize_source_items(
        source, raw_items, from_date, to_date,
        freshness_mode=freshness_mode,
    )
    normalized = signals.annotate_stream(normalized, ranking_query, freshness_mode)
    normalized = signals.prune_low_relevance(normalized)
    normalized = dedupe.dedupe_items(normalized)
    for item in normalized:
        item.snippet = snippet.extract_best_snippet(item, ranking_query)
    return normalized


def _finalize_items_by_source(items_by_source_raw: dict[str, list[schema.SourceItem]]) -> dict[str, list[schema.SourceItem]]:
    finalized = {}
    for source, items in items_by_source_raw.items():
        items = sorted(items, key=lambda item: item.local_rank_score or 0.0, reverse=True)
        finalized[source] = dedupe.dedupe_items(items)
    return finalized


def _warnings(
    items_by_source: dict[str, list[schema.SourceItem]],
    candidates: list[schema.Candidate],
    errors_by_source: dict[str, str],
) -> list[str]:
    warnings: list[str] = []
    if not candidates:
        warnings.append("No candidates survived retrieval and ranking.")
    if len(candidates) < 5:
        warnings.append("Evidence is thin for this topic.")
    top_sources = {
        source
        for candidate in candidates[:5]
        for source in schema.candidate_sources(candidate)
    }
    if len(top_sources) <= 1 and len(candidates) >= 3:
        warnings.append("Top evidence is highly concentrated in one source.")
    if errors_by_source:
        warnings.append(f"Some sources failed: {', '.join(sorted(errors_by_source))}")
    if not items_by_source:
        warnings.append("No source returned usable items.")
    return warnings


def _is_rate_limit_error(exc: Exception) -> bool:
    """Detect 429 rate-limit errors by status code or message text."""
    if hasattr(exc, "status_code") and getattr(exc, "status_code", None) == 429:
        return True
    return "429" in str(exc)


def _is_transient_error(exc: Exception) -> bool:
    """Detect 5xx server errors that are worth retrying."""
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and 500 <= status < 600:
        return True
    msg = str(exc)
    return any(code in msg for code in ("500", "502", "503", "504"))



def _retry_thin_sources(
    *,
    topic: str,
    bundle: schema.RetrievalBundle,
    plan: schema.QueryPlan,
    config: dict[str, Any],
    depth: str,
    date_range: tuple[str, str],
    runtime: schema.ProviderRuntime,
    mock: bool,
    rate_limited_sources: set[str],
    rate_limit_lock: threading.Lock,
    settings: dict[str, Any],
    web_backend: str = "auto",
    skip_sources: set[str] | None = None,
) -> None:
    """Retry sources with thin results using simplified core subject query."""
    if depth == "quick":
        return

    planned_sources: list[str] = []
    for subquery in plan.subqueries:
        for source in subquery.sources:
            if source not in planned_sources:
                planned_sources.append(source)
    _skip = skip_sources or set()
    thin_sources = [
        source
        for source in planned_sources
        if len(bundle.items_by_source.get(source, [])) < 3
        and source not in bundle.errors_by_source
        and source not in _skip
    ]

    if not thin_sources:
        return

    core = query.extract_core_subject(topic, max_words=3)
    if not core:
        return
    # Note: we intentionally do NOT skip when core == topic. For short topics
    # like "Kanye West", the 3-word core IS the topic — but the planner may
    # have sent a different (worse) query to the source. Retrying with the
    # raw core subject is still valuable.

    from_date, to_date = date_range

    # Create a retry subquery with the simplified core subject
    retry_subquery = schema.SubQuery(
        label="retry",
        search_query=core,
        ranking_query=f"What recent evidence from the last 30 days matters for {core}?",
        sources=thin_sources,
        weight=0.3,
    )

    def _retry_one_source(source: str) -> tuple[str, list[schema.SourceItem]]:
        raw_items, _artifact = _retrieve_stream(
            topic=topic,
            subquery=retry_subquery,
            source=source,
            config=config,
            depth=depth,
            date_range=date_range,
            runtime=runtime,
            mock=mock,
            rate_limited_sources=rate_limited_sources,
            rate_limit_lock=rate_limit_lock,
            web_backend=web_backend,
            raw_topic=topic,
        )
        normalized = _normalize_score_dedupe(
            source,
            raw_items,
            from_date,
            to_date,
            freshness_mode=plan.freshness_mode,
            ranking_query=retry_subquery.ranking_query,
        )
        return source, normalized[:settings["per_stream_limit"]]

    retryable = [s for s in thin_sources if s not in rate_limited_sources]

    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=min(4, len(retryable) or 1)) as executor:
        futures = {executor.submit(_retry_one_source, s): s for s in retryable}
        for future in as_completed(futures):
            source = futures[future]
            try:
                source, normalized = future.result()
                existing_urls = {item.url for item in bundle.items_by_source.get(source, []) if item.url}
                new_items = [item for item in normalized if item.url not in existing_urls]

                if new_items:
                    bundle.items_by_source.setdefault(source, []).extend(new_items)
                    primary_label = plan.subqueries[0].label if plan.subqueries else "primary"
                    bundle.items_by_source_and_query.setdefault((primary_label, source), []).extend(new_items)
            except Exception as exc:
                print(f"[Pipeline] Retry failed for {source}: {type(exc).__name__}: {exc}", file=sys.stderr)


def _retrieve_stream(
    *,
    topic: str,
    subquery: schema.SubQuery,
    source: str,
    config: dict[str, Any],
    depth: str,
    date_range: tuple[str, str],
    runtime: schema.ProviderRuntime,
    mock: bool,
    rate_limited_sources: set[str] | None = None,
    rate_limit_lock: threading.Lock | None = None,
    web_backend: str = "auto",
    raw_topic: str = "",
    subreddits: list[str] | None = None,
) -> tuple[list[dict], dict]:
    # Early exit if source was rate-limited by a sibling future
    if rate_limited_sources is not None and source in rate_limited_sources:
        return [], {}
    from_date, to_date = date_range
    if mock:
        return _mock_stream_results(source, subquery)
    if source == "grounding":
        return grounding.web_search(
            subquery.search_query, date_range, config, backend=web_backend)
    if source == "reddit":
        reddit_query = raw_topic or subquery.search_query
        apify_token = config.get("APIFY_API_TOKEN")
        # Apify Reddit first (if token available)
        if apify_token:
            try:
                results = apify_reddit.search_reddit_apify(
                    reddit_query, from_date, to_date, depth=depth,
                    subreddits=subreddits, token=apify_token,
                )
                if results:
                    return results, {}
            except Exception as exc:
                sys.stderr.write(
                    f"[Reddit] Apify search failed ({type(exc).__name__}: {exc}), "
                    f"falling back to public JSON\n"
                )
        # Fallback: free public Reddit JSON (no API key needed)
        try:
            public_results = reddit_public.search_reddit_public(
                reddit_query, from_date, to_date, depth=depth,
                subreddits=subreddits,
            )
            if public_results:
                return public_results, {}
        except Exception as exc:
            sys.stderr.write(
                f"[Reddit] Public search also failed ({type(exc).__name__}: {exc})\n"
            )
        return [], {}
    if source == "x":
        apify_token = config.get("APIFY_API_TOKEN")
        if not apify_token:
            return [], {}
        x_query = raw_topic or subquery.search_query
        try:
            results = apify_x.search_x_apify(
                x_query, from_date, to_date, depth=depth, token=apify_token,
            )
            return results, {}
        except Exception as exc:
            sys.stderr.write(
                f"[X] Apify search failed ({type(exc).__name__}: {exc})\n"
            )
            return [], {}
    if source == "linkedin":
        apify_token = config.get("APIFY_API_TOKEN")
        li_query = raw_topic or subquery.search_query
        if not apify_token:
            return [], {}
        try:
            results = apify_linkedin.search_linkedin_apify(
                li_query, from_date, to_date, depth=depth, token=apify_token,
            )
            return results, {}
        except Exception as exc:
            sys.stderr.write(
                f"[LinkedIn] Apify search failed ({type(exc).__name__}: {exc})\n"
            )
            return [], {}
    if source == "youtube":
        yt_query = raw_topic or subquery.search_query
        apify_token = config.get("APIFY_API_TOKEN")
        # Apify YouTube first (if token available)
        if apify_token:
            try:
                result = apify_youtube.search_youtube_apify(
                    yt_query, from_date, to_date, depth=depth, token=apify_token,
                )
                items = result.get("items", [])
                if items:
                    return items, {}
            except Exception as exc:
                sys.stderr.write(
                    f"[YouTube] Apify search failed ({type(exc).__name__}: {exc}), "
                    f"falling back to yt-dlp\n"
                )
        # Fallback: yt-dlp if installed locally
        result = None
        if which("yt-dlp"):
            try:
                result = youtube_yt.search_and_transcribe(yt_query, from_date, to_date, depth=depth)
            except Exception:
                result = None
        if result is None:
            result = {"items": []}
        items = youtube_yt.parse_youtube_response(result)
        return items, {}
    if source == "hackernews":
        result = hackernews.search_hackernews(subquery.search_query, from_date, to_date, depth=depth)
        return hackernews.parse_hackernews_response(result, query=subquery.search_query), {}
    if source == "polymarket":
        result = polymarket.search_polymarket(subquery.search_query, from_date, to_date, depth=depth)
        return polymarket.parse_polymarket_response(result, topic=subquery.search_query), {}
    if source == "github":
        result = github.search_github(subquery.search_query, from_date, to_date, depth=depth, token=config.get("GITHUB_TOKEN"))
        return result, {}
    if source == "perplexity":
        return perplexity.search(subquery.search_query, date_range, config, deep=config.get("_deep_research", False))
    raise RuntimeError(f"Unsupported source: {source}")


def _google_key(config: dict[str, Any]) -> str | None:
    return config.get("GOOGLE_API_KEY") or config.get("GEMINI_API_KEY") or config.get("GOOGLE_GENAI_API_KEY")




def _mock_stream_results(source: str, subquery: schema.SubQuery) -> tuple[list[dict], dict]:
    payloads = {
        "reddit": [
            {
                "id": "R1",
                "title": f"{subquery.search_query} discussion thread",
                "url": "https://reddit.com/r/example/comments/1",
                "subreddit": "example",
                "date": dates.get_date_range(5)[0],
                "engagement": {"score": 120, "num_comments": 48, "upvote_ratio": 0.91},
                "selftext": f"Community discussion about {subquery.search_query}.",
                "top_comments": [{"excerpt": "Strong firsthand feedback from users."}],
                "relevance": 0.82,
                "why_relevant": "Mock Reddit result",
            }
        ],
        "x": [
            {
                "id": "X1",
                "text": f"People on X are discussing {subquery.search_query} right now.",
                "url": "https://x.com/example/status/1",
                "author_handle": "example",
                "date": dates.get_date_range(2)[0],
                "engagement": {"likes": 200, "reposts": 35, "replies": 18, "quotes": 4},
                "relevance": 0.79,
                "why_relevant": "Mock X result",
            }
        ],
        "grounding": [
            {
                "id": "WB1",
                "title": f"{subquery.search_query} article",
                "url": "https://example.com/article",
                "source_domain": "example.com",
                "snippet": f"Recent web reporting about {subquery.search_query}.",
                "date": dates.get_date_range(7)[0],
                "relevance": 0.88,
                "why_relevant": "Brave web search",
            }
        ],
    }
    if source == "grounding":
        return payloads.get(source, []), {
            "label": subquery.label,
            "mock": True,
            "webSearchQueries": [subquery.search_query],
            "resultCount": 1,
        }
    return payloads.get(source, []), {}
