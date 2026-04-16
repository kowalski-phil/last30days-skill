"""LinkedIn post search via Apify actor (harvestapi/linkedin-post-search).

New source — not in the original upstream repo. LinkedIn is the primary B2B
research channel so this is a key addition for this fork.

Actor: https://apify.com/harvestapi/linkedin-post-search
Pricing: ~$1.50-2.00 per 1,000 posts. No cookies / login required.
Rating: 5.0/5 as of 2026-04-15.

Returns list[dict] in the shape that normalize._normalize_linkedin expects:
    {
        "id", "text", "url",
        "author_name", "author_headline", "author_company",
        "author_profile_url",
        "date" (YYYY-MM-DD),
        "engagement" {likes, comments, shares, reactions},
        "relevance", "why_relevant"
    }

Apify URL convention: actor IDs use ``~`` between username and actor name
in API URLs (e.g. harvestapi~linkedin-post-search).
"""

from __future__ import annotations

import sys
import time
from typing import Any

from . import http

ACTOR_ID = "harvestapi~linkedin-post-search"
APIFY_RUN_URL = f"https://api.apify.com/v2/acts/{ACTOR_ID}/runs"

# Depth-aware limits. LinkedIn posts are denser per item than tweets, so
# we need fewer of them to reach equivalent research value.
DEPTH_LIMITS = {
    "quick": 25,
    "default": 60,
    "deep": 150,
}

# Polling interval and timeout for async runs. LinkedIn actor tends to
# take longer than X because the site is harder to scrape.
POLL_INTERVAL = 4  # seconds
MAX_WAIT = 240  # seconds


def _log(msg: str) -> None:
    sys.stderr.write(f"[ApifyLinkedIn] {msg}\n")
    sys.stderr.flush()


def _build_input(
    query: str,
    depth: str = "default",
) -> dict[str, Any]:
    """Build the Apify actor input payload for harvestapi/linkedin-post-search.

    The actor supports a `postedLimit` enum for date filtering. We use
    ``month`` for a ~30-day window (the longest option below 3 months).
    The pipeline's downstream date filter will further tighten this to the
    exact from_date..to_date window.
    """
    limit = DEPTH_LIMITS.get(depth, DEPTH_LIMITS["default"])

    return {
        "searchQueries": [query],
        "maxItems": limit,
        "postedLimit": "month",  # server-side recency filter
        "sortBy": "relevance",
    }


def _start_run(actor_input: dict[str, Any], token: str) -> dict[str, Any]:
    """Start an Apify actor run and return the run metadata."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    return http.post(APIFY_RUN_URL, actor_input, headers=headers, timeout=30)


def _poll_run(run_id: str, token: str) -> str:
    """Poll until the run finishes. Returns the default dataset ID."""
    url = f"https://api.apify.com/v2/actor-runs/{run_id}"
    headers = {"Authorization": f"Bearer {token}"}
    elapsed = 0

    while elapsed < MAX_WAIT:
        resp = http.get(url, headers=headers, timeout=15)
        data = resp.get("data", resp)
        status = data.get("status")

        if status == "SUCCEEDED":
            return data.get("defaultDatasetId", "")
        if status in ("FAILED", "ABORTED", "TIMED-OUT"):
            raise RuntimeError(f"Apify run {run_id} ended with status: {status}")

        time.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL

    raise RuntimeError(f"Apify run {run_id} timed out after {MAX_WAIT}s")


def _fetch_dataset(dataset_id: str, token: str) -> list[dict[str, Any]]:
    """Fetch all items from an Apify dataset."""
    url = f"https://api.apify.com/v2/datasets/{dataset_id}/items"
    headers = {"Authorization": f"Bearer {token}"}
    resp = http.get(url, headers=headers, timeout=30)

    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict) and isinstance(resp.get("data"), list):
        return resp["data"]
    return []


def _parse_date(value: Any) -> str | None:
    """Convert various LinkedIn date formats to YYYY-MM-DD.

    The harvestapi/linkedin-post-search actor returns ``postedAt`` as a
    dict:
        {"timestamp": 1774862369292, "date": "2026-03-30T09:19:29.292Z", ...}

    So callers should pass either that dict or one of its primitive fields.
    We handle:
      - dict with ``date`` or ``timestamp`` keys
      - ISO 8601 string
      - Unix epoch (seconds or ms) as int/float
    """
    if not value:
        return None
    if isinstance(value, dict):
        return _parse_date(value.get("date")) or _parse_date(value.get("timestamp"))
    if isinstance(value, (int, float)):
        try:
            from datetime import datetime, timezone
            ts = float(value)
            # Heuristic: LinkedIn epochs come in ms; detect and convert
            if ts > 1e12:
                ts /= 1000
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            return dt.strftime("%Y-%m-%d")
        except (ValueError, TypeError, OSError):
            return None
    if isinstance(value, str):
        # ISO 8601 - "2026-04-10T12:34:56.000Z" or "2026-04-10"
        if len(value) >= 10 and value[4] == "-" and value[7] == "-":
            return value[:10]
    return None


def _split_headline_company(info: str) -> tuple[str, str]:
    """Split LinkedIn's mashed 'info' field into (headline, company).

    Examples this handles:
        "CFO at Example Corp"                → ("CFO", "Example Corp")
        "MBA | Leading Bid Mgmt @ Basware"   → ("MBA | Leading Bid Mgmt", "Basware")
        "Senior SRE"                         → ("Senior SRE", "")
    """
    if not info:
        return "", ""
    # LinkedIn commonly uses " at " or " @ " to separate role from employer
    for sep in (" @ ", " at "):
        if sep in info:
            role, _, company = info.partition(sep)
            return role.strip(), company.strip()
    return info.strip(), ""


def _extract_author(raw: dict[str, Any]) -> tuple[str, str, str, str]:
    """Return (name, headline, company, profile_url) from the harvestapi
    author shape:
        {"name": "Jane Doe", "info": "CFO @ Example Corp",
         "linkedinUrl": "https://...", "publicIdentifier": "janedoe", ...}

    Also tolerates alternative shapes from other LinkedIn actors
    (headline, companyName, currentCompany, etc.) in case we swap actors.
    """
    author = raw.get("author") or raw.get("profile") or raw.get("poster") or {}
    if not isinstance(author, dict):
        author = {}

    name = (
        author.get("name")
        or author.get("fullName")
        or author.get("full_name")
        or f"{author.get('firstName', '') or ''} {author.get('lastName', '') or ''}".strip()
        or raw.get("authorName")
        or ""
    )

    # Prefer explicit fields if present (other actors), otherwise parse the
    # harvestapi "info" mashup.
    headline = (
        author.get("headline")
        or author.get("position")
        or author.get("title")
        or raw.get("authorHeadline")
        or ""
    )
    company = author.get("company") or author.get("companyName") or ""
    if not company:
        current = author.get("currentCompany")
        if isinstance(current, dict):
            company = current.get("name") or ""
    if not company:
        company = raw.get("authorCompany") or ""

    if not headline and not company:
        info = str(author.get("info") or "").strip()
        if info:
            headline, company = _split_headline_company(info)

    profile_url = (
        author.get("linkedinUrl")
        or author.get("profileUrl")
        or author.get("url")
        or raw.get("authorUrl")
        or ""
    )

    return str(name).strip(), str(headline).strip(), str(company).strip(), str(profile_url).strip()


def _coerce_int(value: Any) -> int:
    """Safely coerce a value to int, returning 0 for lists/dicts/None/invalid."""
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def _sum_reactions(reactions: Any) -> int:
    """Sum counts from LinkedIn's reactions list.

    harvestapi returns: [{"type": "LIKE", "count": 27}, {"type": "LOVE", "count": 3}, ...]
    """
    if isinstance(reactions, int):
        return reactions
    if isinstance(reactions, list):
        total = 0
        for r in reactions:
            if isinstance(r, dict):
                total += _coerce_int(r.get("count"))
        return total
    return 0


def _normalize_item(raw: dict[str, Any], index: int) -> dict[str, Any]:
    """Convert an Apify LinkedIn post into the dict format
    normalize._normalize_linkedin will convert to a SourceItem.

    Based on harvestapi/linkedin-post-search output schema:
        - content:      post body text
        - linkedinUrl:  post URL
        - postedAt:     dict {timestamp, date, postedAgoShort, ...}
        - engagement:   dict {likes: int, comments: int, shares: int,
                              reactions: list[{type, count}]}
        - author:       dict {name, info, linkedinUrl, publicIdentifier, ...}
        - article:      optional dict (for LinkedIn article posts)
    """
    post_id = str(raw.get("id") or raw.get("entityId") or raw.get("postId") or f"LI{index + 1}")

    # Post body: prefer `content`, fall back to header text for image-only
    # posts that have no body text
    text = str(raw.get("content") or "").strip()
    if not text:
        header = raw.get("header")
        if isinstance(header, dict):
            text = str(header.get("text") or "").strip()

    # Article posts have richer context in the `article` sub-dict
    article = raw.get("article")
    if isinstance(article, dict):
        article_title = str(article.get("title") or "").strip()
        article_desc = str(article.get("description") or "").strip()
        if article_title and article_title not in text:
            text = f"{article_title}\n\n{text}".strip() if text else article_title
        if article_desc and article_desc not in text:
            text = f"{text}\n\n{article_desc}".strip() if text else article_desc

    url = str(raw.get("linkedinUrl") or raw.get("url") or raw.get("postUrl") or "")

    engagement_raw = raw.get("engagement") or raw.get("stats") or {}
    if not isinstance(engagement_raw, dict):
        engagement_raw = {}

    likes = _coerce_int(
        engagement_raw.get("likes")
        or engagement_raw.get("likesCount")
        or raw.get("likesCount")
        or raw.get("numLikes")
    )
    comments_count = _coerce_int(
        engagement_raw.get("comments")
        or engagement_raw.get("commentsCount")
        or raw.get("commentsCount")
        or raw.get("numComments")
    )
    # `comments` at the top level is the comment BODIES list — not a count.
    # Only fall back to its length if engagement.comments was absent.
    if comments_count == 0 and isinstance(raw.get("comments"), list):
        comments_count = len(raw["comments"])

    shares = _coerce_int(
        engagement_raw.get("shares")
        or engagement_raw.get("sharesCount")
        or engagement_raw.get("reposts")
        or raw.get("sharesCount")
        or raw.get("numShares")
    )

    # Reactions is a LIST of {type, count} — sum them.
    reactions = _sum_reactions(engagement_raw.get("reactions") or raw.get("reactions"))

    name, headline, company, profile_url = _extract_author(raw)

    # `postedAt` is a dict on harvestapi; _parse_date handles both dicts
    # and primitive values (for other actors).
    date_str = _parse_date(
        raw.get("postedAt")
        or raw.get("postedDate")
        or raw.get("createdAt")
        or raw.get("publishedAt")
        or raw.get("date")
    )

    return {
        "id": f"LI{index + 1}",
        "raw_id": post_id,
        "text": text[:1000],  # LinkedIn posts can be long — more generous cap than X
        "url": url,
        "author_name": name,
        "author_headline": headline,
        "author_company": company,
        "author_profile_url": profile_url,
        "date": date_str,
        "engagement": {
            "likes": likes,
            "comments": comments_count,
            "shares": shares,
            "reactions": reactions,
        },
        "relevance": _compute_relevance(likes, comments_count, shares, reactions),
        "why_relevant": "Apify LinkedIn search",
    }


def _compute_relevance(likes: int, comments: int, shares: int, reactions: int) -> float:
    """Estimate relevance from engagement signals.

    LinkedIn engagement numbers skew much smaller than X — a LinkedIn post
    with 50 reactions and 10 comments is substantial. Scale accordingly.
    """
    total = max(likes, reactions)  # `reactions` is a superset of `likes` when available
    likes_component = min(1.0, max(0.0, total / 300.0))
    comments_component = min(1.0, max(0.0, comments / 50.0))
    shares_component = min(1.0, max(0.0, shares / 30.0))
    return round(
        (likes_component * 0.5)
        + (comments_component * 0.3)
        + (shares_component * 0.2),
        3,
    )


def search_linkedin_apify(
    topic: str,
    from_date: str,
    to_date: str,
    depth: str = "default",
    token: str = "",
) -> list[dict[str, Any]]:
    """Search LinkedIn posts via Apify.

    Args:
        topic: Search topic (supports LinkedIn Boolean operators)
        from_date: Start date (YYYY-MM-DD) — used for client-side filter
        to_date: End date (YYYY-MM-DD) — used for client-side filter
        depth: 'quick', 'default', or 'deep'
        token: Apify API token

    Returns:
        List of normalized item dicts matching normalize._normalize_linkedin's
        expected shape.
    """
    if not token:
        _log("No APIFY_API_TOKEN — skipping LinkedIn")
        return []

    actor_input = _build_input(topic, depth)
    _log(f"Starting Apify run for '{topic}' (depth={depth}, window {from_date}..{to_date})")

    try:
        run_resp = _start_run(actor_input, token)
    except Exception as exc:
        _log(f"Failed to start run: {type(exc).__name__}: {exc}")
        return []

    run_data = run_resp.get("data", run_resp)
    run_id = run_data.get("id")

    if not run_id:
        _log(f"No run ID in response: {run_resp}")
        return []

    _log(f"Run started: {run_id}, polling...")
    try:
        dataset_id = _poll_run(run_id, token)
    except Exception as exc:
        _log(f"Polling failed: {type(exc).__name__}: {exc}")
        return []

    if not dataset_id:
        _log("No dataset ID returned")
        return []

    raw_items = _fetch_dataset(dataset_id, token)
    _log(f"Got {len(raw_items)} raw items from Apify")

    results = [_normalize_item(item, i) for i, item in enumerate(raw_items)]

    # Date filter — keep items in the window or with unknown dates
    # (LinkedIn relative dates like "1 day ago" parse to None and should
    # still be included since they're definitely recent)
    filtered = []
    for item in results:
        d = item.get("date")
        if d is None or (from_date <= d <= to_date):
            filtered.append(item)

    # Sort by combined engagement as a quality proxy
    def _score(it: dict[str, Any]) -> int:
        e = it.get("engagement", {})
        return (
            int(e.get("likes", 0))
            + int(e.get("reactions", 0))
            + 3 * int(e.get("comments", 0))
            + 3 * int(e.get("shares", 0))
        )

    filtered.sort(key=_score, reverse=True)

    # Re-index after sorting
    for i, item in enumerate(filtered):
        item["id"] = f"LI{i + 1}"

    _log(f"Returning {len(filtered)} items after date filter")
    return filtered
