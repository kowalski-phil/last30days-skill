"""Normalization of source-specific payloads into the v3 generic item model."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from . import dates, schema


def filter_by_date_range(
    items: list[schema.SourceItem],
    from_date: str,
    to_date: str,
    require_date: bool = False,
) -> list[schema.SourceItem]:
    """Keep only items within the requested window."""
    filtered: list[schema.SourceItem] = []
    for item in items:
        if not item.published_at:
            if not require_date:
                filtered.append(item)
            continue
        if item.published_at < from_date or item.published_at > to_date:
            continue
        filtered.append(item)
    return filtered


def normalize_source_items(
    source: str,
    items: list[dict[str, Any]],
    from_date: str,
    to_date: str,
    freshness_mode: str = "balanced_recent",
) -> list[schema.SourceItem]:
    """Normalize raw source items, filter by date range, with evergreen fallback for how_to queries."""
    source = source.lower()
    normalizers = {
        "reddit": _normalize_reddit,
        "x": _normalize_x,
        "linkedin": _normalize_linkedin,
        "youtube": _normalize_youtube,
        "hackernews": _normalize_hackernews,
        "polymarket": _normalize_polymarket,
        "grounding": _normalize_grounding,
        "github": _normalize_github,
        "perplexity": _normalize_grounding,
    }
    normalizer = normalizers.get(source)
    if normalizer is None:
        raise ValueError(f"Unsupported source: {source}")
    normalized = [normalizer(source, item, index, from_date, to_date) for index, item in enumerate(items)]
    require_date = source == "grounding"
    filtered = filter_by_date_range(normalized, from_date, to_date, require_date=require_date)
    if filtered:
        return filtered
    if freshness_mode == "evergreen_ok" and source == "youtube":
        if require_date:
            return [item for item in normalized if item.published_at]
        return normalized
    return filtered


def _domain_from_url(url: str) -> str | None:
    if not url:
        return None
    domain = urlparse(url).netloc.strip().lower()
    return domain or None


def _date_confidence(item: dict[str, Any], from_date: str, to_date: str, default: str = "low") -> str:
    if item.get("date_confidence"):
        return str(item["date_confidence"])
    date_value = item.get("date")
    if not date_value:
        return default
    return dates.get_date_confidence(str(date_value), from_date, to_date)


def _source_item(
    *,
    item_id: str,
    source: str,
    title: str,
    body: str,
    url: str,
    published_at: str | None,
    date_confidence: str,
    relevance_hint: float,
    why_relevant: str,
    author: str | None = None,
    container: str | None = None,
    engagement: dict[str, float | int] | None = None,
    snippet: str = "",
    metadata: dict[str, Any] | None = None,
) -> schema.SourceItem:
    return schema.SourceItem(
        item_id=item_id,
        source=source,
        title=title.strip() or body.strip()[:160] or item_id,
        body=body.strip(),
        url=url.strip(),
        author=(author or "").strip() or None,
        container=(container or "").strip() or None,
        published_at=published_at,
        date_confidence=date_confidence,
        engagement=engagement or {},
        relevance_hint=max(0.0, min(1.0, float(relevance_hint or 0.0))),
        why_relevant=why_relevant.strip(),
        snippet=snippet.strip(),
        metadata=metadata or {},
    )


def _normalize_reddit(
    source: str,
    item: dict[str, Any],
    index: int,
    from_date: str,
    to_date: str,
) -> schema.SourceItem:
    top_comments = item.get("top_comments") or []
    comment_text = " ".join(
        str(comment.get("excerpt") or "").strip()
        for comment in top_comments[:3]
        if isinstance(comment, dict)
    )
    body = "\n".join(
        part
        for part in [
            str(item.get("title") or "").strip(),
            str(item.get("selftext") or "").strip(),
            comment_text,
        ]
        if part
    )
    return _source_item(
        item_id=str(item.get("id") or f"R{index + 1}"),
        source=source,
        title=str(item.get("title") or ""),
        body=body,
        url=str(item.get("url") or ""),
        author=None,
        container=str(item.get("subreddit") or ""),
        published_at=item.get("date"),
        date_confidence=_date_confidence(item, from_date, to_date),
        engagement=item.get("engagement") or {},
        relevance_hint=item.get("relevance", 0.5),
        why_relevant=str(item.get("why_relevant") or ""),
        snippet=comment_text or str(item.get("selftext") or "")[:400],
        metadata={
            "top_comments": top_comments,
            "comment_insights": item.get("comment_insights") or [],
        },
    )


def _normalize_x(
    source: str,
    item: dict[str, Any],
    index: int,
    from_date: str,
    to_date: str,
) -> schema.SourceItem:
    text = str(item.get("text") or "").strip()
    return _source_item(
        item_id=str(item.get("id") or f"X{index + 1}"),
        source=source,
        title=text[:140] or f"X post {index + 1}",
        body=text,
        url=str(item.get("url") or ""),
        author=str(item.get("author_handle") or "").lstrip("@"),
        published_at=item.get("date"),
        date_confidence=_date_confidence(item, from_date, to_date),
        engagement=item.get("engagement") or {},
        relevance_hint=item.get("relevance", 0.5),
        why_relevant=str(item.get("why_relevant") or ""),
    )


def _normalize_linkedin(
    source: str,
    item: dict[str, Any],
    index: int,
    from_date: str,
    to_date: str,
) -> schema.SourceItem:
    text = str(item.get("text") or "").strip()
    name = str(item.get("author_name") or "").strip()
    headline = str(item.get("author_headline") or "").strip()
    company = str(item.get("author_company") or "").strip()
    # Stash LinkedIn-specific context (headline, company, profile URL) in
    # metadata so the reranker/renderer can surface them without polluting
    # the generic SourceItem shape.
    metadata: dict[str, Any] = {}
    if headline:
        metadata["author_headline"] = headline
    if company:
        metadata["author_company"] = company
    profile_url = str(item.get("author_profile_url") or "").strip()
    if profile_url:
        metadata["author_profile_url"] = profile_url
    return _source_item(
        item_id=str(item.get("id") or f"LI{index + 1}"),
        source=source,
        title=text[:160] or f"LinkedIn post {index + 1}",
        body=text,
        url=str(item.get("url") or ""),
        author=name,
        container=company or None,
        published_at=item.get("date"),
        date_confidence=_date_confidence(item, from_date, to_date),
        engagement=item.get("engagement") or {},
        relevance_hint=item.get("relevance", 0.5),
        why_relevant=str(item.get("why_relevant") or ""),
        metadata=metadata,
    )


def _normalize_youtube(
    source: str,
    item: dict[str, Any],
    index: int,
    from_date: str,
    to_date: str,
) -> schema.SourceItem:
    transcript = str(item.get("transcript_snippet") or "").strip()
    description = str(item.get("description") or "").strip()
    title = str(item.get("title") or "").strip()
    highlights = item.get("transcript_highlights") or []
    metadata: dict[str, Any] = {}
    if highlights:
        metadata["transcript_highlights"] = highlights
    return _source_item(
        item_id=str(item.get("video_id") or item.get("id") or f"YT{index + 1}"),
        source=source,
        title=title,
        body="\n".join(part for part in [title, description, transcript] if part),
        url=str(item.get("url") or ""),
        author=str(item.get("channel_name") or ""),
        published_at=item.get("date"),
        date_confidence=_date_confidence(item, from_date, to_date, default="high"),
        engagement=item.get("engagement") or {},
        relevance_hint=item.get("relevance", 0.5),
        why_relevant=str(item.get("why_relevant") or ""),
        snippet=transcript,
        metadata=metadata,
    )


def _normalize_hackernews(
    source: str,
    item: dict[str, Any],
    index: int,
    from_date: str,
    to_date: str,
) -> schema.SourceItem:
    top_comments = item.get("top_comments") or []
    comment_text = " ".join(
        str(comment.get("text") or "").strip()
        for comment in top_comments[:3]
        if isinstance(comment, dict)
    )
    title = str(item.get("title") or "").strip()
    body = "\n".join(part for part in [title, str(item.get("text") or "").strip(), comment_text] if part)
    return _source_item(
        item_id=str(item.get("id") or f"HN{index + 1}"),
        source=source,
        title=title or f"HN story {index + 1}",
        body=body,
        url=str(item.get("url") or item.get("hn_url") or ""),
        author=str(item.get("author") or ""),
        container="Hacker News",
        published_at=item.get("date"),
        date_confidence=_date_confidence(item, from_date, to_date, default="high"),
        engagement=item.get("engagement") or {},
        relevance_hint=item.get("relevance", 0.5),
        why_relevant=str(item.get("why_relevant") or ""),
        snippet=comment_text,
        metadata={
            "hn_url": item.get("hn_url"),
            "top_comments": top_comments,
            "comment_insights": item.get("comment_insights") or [],
        },
    )


def _normalize_polymarket(
    source: str,
    item: dict[str, Any],
    index: int,
    from_date: str,
    to_date: str,
) -> schema.SourceItem:
    title = str(item.get("title") or "").strip()
    question = str(item.get("question") or "").strip()
    engagement = {
        "volume": item.get("volume1mo") or item.get("volume24hr") or 0,
        "liquidity": item.get("liquidity") or 0,
    }
    return _source_item(
        item_id=str(item.get("id") or f"PM{index + 1}"),
        source=source,
        title=title or question or f"Polymarket event {index + 1}",
        body="\n".join(part for part in [title, question, str(item.get("price_movement") or "")] if part),
        url=str(item.get("url") or ""),
        author=None,
        container="Polymarket",
        published_at=item.get("date"),
        date_confidence=_date_confidence(item, from_date, to_date, default="high"),
        engagement=engagement,
        relevance_hint=item.get("relevance", 0.5),
        why_relevant=str(item.get("why_relevant") or ""),
        snippet=str(item.get("price_movement") or ""),
        metadata={
            "question": question,
            "end_date": item.get("end_date"),
            "outcome_prices": item.get("outcome_prices") or [],
            "outcomes_remaining": item.get("outcomes_remaining"),
        },
    )



def _normalize_github(
    source: str,
    item: dict[str, Any],
    index: int,
    from_date: str,
    to_date: str,
) -> schema.SourceItem:
    title = str(item.get("title") or "").strip()
    snippet_text = str(item.get("snippet") or "").strip()
    top_comments = item.get("metadata", {}).get("top_comments") or []
    comment_text = " ".join(
        str(comment.get("excerpt") or "").strip()
        for comment in top_comments[:3]
        if isinstance(comment, dict)
    )
    body = "\n".join(part for part in [title, snippet_text, comment_text] if part)
    metadata = item.get("metadata") or {}
    return _source_item(
        item_id=str(item.get("id") or f"GH{index + 1}"),
        source=source,
        title=title or f"GitHub item {index + 1}",
        body=body,
        url=str(item.get("url") or ""),
        author=str(item.get("author") or ""),
        container=str(item.get("container") or ""),
        published_at=item.get("date"),
        date_confidence=_date_confidence(item, from_date, to_date, default="high"),
        engagement=item.get("engagement") or {},
        relevance_hint=item.get("relevance", 0.5),
        why_relevant=str(item.get("why_relevant") or ""),
        snippet=comment_text or snippet_text[:400],
        metadata={
            "top_comments": top_comments,
            "labels": metadata.get("labels") or [],
            "state": metadata.get("state", ""),
            "is_pr": metadata.get("is_pr", False),
        },
    )

def _normalize_grounding(
    source: str,
    item: dict[str, Any],
    index: int,
    from_date: str,
    to_date: str,
) -> schema.SourceItem:
    title = str(item.get("title") or "").strip()
    snippet = str(item.get("snippet") or "").strip()
    url = str(item.get("url") or "").strip()
    return _source_item(
        item_id=str(item.get("id") or f"W{index + 1}"),
        source=source,
        title=title or _domain_from_url(url) or f"Web result {index + 1}",
        body="\n".join(part for part in [title, snippet] if part),
        url=url,
        author=None,
        container=str(item.get("source_domain") or _domain_from_url(url) or ""),
        published_at=item.get("date"),
        date_confidence=_date_confidence(item, from_date, to_date),
        engagement=item.get("engagement") or {},
        relevance_hint=item.get("relevance", 0.5),
        why_relevant=str(item.get("why_relevant") or ""),
        snippet=snippet,
        metadata=item.get("metadata") or {},
    )
