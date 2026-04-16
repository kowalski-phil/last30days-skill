"""Post-research quality score and upgrade nudge.

Reports which core sources are configured and whether any errored
on this run. Post-fork, all social sources (Reddit, X, YouTube, LinkedIn)
run through a single Apify token; the nudge reflects that.
"""

from typing import List


# The core sources this fork considers. LinkedIn is the fork's B2B addition.
CORE_SOURCES = ["hackernews", "polymarket", "reddit", "x", "youtube", "linkedin"]

# Display labels
SOURCE_LABELS = {
    "hackernews": "Hacker News",
    "polymarket": "Polymarket",
    "reddit": "Reddit",
    "x": "X/Twitter",
    "youtube": "YouTube",
    "linkedin": "LinkedIn",
}


def compute_quality_score(config: dict, research_results: dict) -> dict:
    """Compute research quality score.

    Args:
        config: Configuration dict from env.get_config()
        research_results: Dict describing what happened this run.
            Expected keys:
              - active_sources: list of source names that returned data
              - errors_by_source: dict[str, str] of source -> error message

    Returns:
        {
            "score_pct": 0-100,
            "core_active": [...],       # configured + returned data this run
            "core_missing": [...],      # not configured
            "core_errored": [...],      # configured but errored this run
            "nudge_text": str | None,
        }
    """
    has_apify = bool(config.get("APIFY_API_TOKEN"))
    active_sources = set(research_results.get("active_sources") or [])
    errors_by_source = research_results.get("errors_by_source") or {}

    core_active: List[str] = []
    core_missing: List[str] = []
    core_errored: List[str] = []

    # Hacker News and Polymarket are always available (free public APIs).
    # They count as "active" if the run actually produced items for them.
    # Otherwise treat as silently absent — the topic may simply not be a fit.
    for src in ("hackernews", "polymarket"):
        if src in active_sources:
            core_active.append(src)

    # Reddit, X, YouTube, LinkedIn all need an Apify token.
    for src in ("reddit", "x", "youtube", "linkedin"):
        if not has_apify:
            core_missing.append(src)
            continue
        if src in errors_by_source:
            core_errored.append(src)
            continue
        if src in active_sources:
            core_active.append(src)
        # Otherwise: configured, didn't error, just no hits for this topic.
        # Don't flag as missing — it's a legitimate "topic isn't on this channel".

    total = len(CORE_SOURCES)
    score_pct = int(len(core_active) / total * 100) if total else 0

    nudge_text = _build_nudge_text(
        has_apify=has_apify,
        core_missing=core_missing,
        core_errored=core_errored,
    )

    return {
        "score_pct": score_pct,
        "core_active": core_active,
        "core_missing": core_missing,
        "core_errored": core_errored,
        "nudge_text": nudge_text,
    }


def _build_nudge_text(
    *,
    has_apify: bool,
    core_missing: List[str],
    core_errored: List[str],
) -> str | None:
    """Build a nudge message, or return None if nothing needs saying."""
    lines: List[str] = []

    if not has_apify:
        lines.append(
            "Heads up: no APIFY_API_TOKEN in .env — Reddit, X, YouTube, and "
            "LinkedIn are all disabled for this run."
        )
        lines.append(
            "  Fix: get a free token at apify.com and add it to your .env as "
            "APIFY_API_TOKEN=apify_api_..."
        )

    if core_errored:
        labels = ", ".join(SOURCE_LABELS.get(s, s) for s in core_errored)
        lines.append(
            f"Errors this run: {labels}. Check the [Apify...] log lines above "
            "for the actor error message."
        )

    if not lines:
        return None
    return "\n".join(lines)
