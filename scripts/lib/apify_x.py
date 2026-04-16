"""X/Twitter search via Apify actor (apidojo/tweet-scraper, aka Tweet Scraper V2).

Replaces xai_x.py and bird_x.py with a single Apify-based module.

Returns list[dict] in the shape that pipeline → normalize._normalize_x expects:
    {
        "id", "text", "url", "author_handle",
        "date" (YYYY-MM-DD), "engagement" (dict of likes/reposts/replies/quotes),
        "relevance", "why_relevant"
    }

Actor docs: https://apify.com/apidojo/tweet-scraper
Pricing: ~$0.40 per 1,000 tweets. Minimum 50 tweets per query.

Apify URL convention: actor IDs use ``~`` between username and actor name
in API URLs (e.g. apidojo~tweet-scraper), even though the store URL uses ``/``.
"""

from __future__ import annotations

import sys
import time
from typing import Any

from . import http

ACTOR_ID = "apidojo~tweet-scraper"
APIFY_RUN_URL = f"https://api.apify.com/v2/acts/{ACTOR_ID}/runs"

# Depth-aware limits. Actor enforces a 50-tweet minimum per query, so even
# quick mode has to request at least that many.
DEPTH_LIMITS = {
    "quick": 50,
    "default": 100,
    "deep": 200,
}

# Polling interval and timeout for async runs
POLL_INTERVAL = 3  # seconds
MAX_WAIT = 180  # seconds


def _log(msg: str) -> None:
    sys.stderr.write(f"[ApifyX] {msg}\n")
    sys.stderr.flush()


def _build_input(
    query: str,
    from_date: str,
    to_date: str,
    depth: str = "default",
) -> dict[str, Any]:
    """Build the Apify actor input payload for apidojo/tweet-scraper.

    The actor supports two ways to filter by date:
      1. `start` / `end` fields directly
      2. Twitter advanced-search operators embedded in searchTerms
         (e.g. "AI since:2026-03-15 until:2026-04-14")

    We use (1) since it's cleaner; (2) is a fallback if the actor ever
    ignores the explicit fields.
    """
    limit = DEPTH_LIMITS.get(depth, DEPTH_LIMITS["default"])

    return {
        "searchTerms": [query],
        "start": from_date,
        "end": to_date,
        "maxItems": limit,
        "sort": "Top",
        "tweetLanguage": "en",
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
    """Convert Apify tweet createdAt to YYYY-MM-DD.

    The actor typically returns ISO 8601 (e.g. "2026-04-10T12:34:56.000Z")
    or Twitter's legacy format ("Fri Apr 10 12:34:56 +0000 2026").
    """
    if not value:
        return None
    if isinstance(value, (int, float)):
        try:
            from datetime import datetime, timezone
            dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
            return dt.strftime("%Y-%m-%d")
        except (ValueError, TypeError, OSError):
            return None
    if isinstance(value, str):
        # ISO 8601 - most common
        if len(value) >= 10 and value[4] == "-" and value[7] == "-":
            return value[:10]
        # Twitter legacy: "Fri Apr 10 12:34:56 +0000 2026"
        try:
            from datetime import datetime
            dt = datetime.strptime(value, "%a %b %d %H:%M:%S %z %Y")
            return dt.strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            return None
    return None


def _extract_handle(raw: dict[str, Any]) -> str:
    """Pull the author handle out of whatever shape the actor returns."""
    # apidojo/tweet-scraper nests author under "author" or "user"
    author = raw.get("author") or raw.get("user") or {}
    if isinstance(author, dict):
        handle = author.get("userName") or author.get("screen_name") or author.get("username") or ""
    else:
        handle = ""
    # Top-level fallbacks
    if not handle:
        handle = raw.get("userName") or raw.get("screen_name") or raw.get("username") or ""
    return str(handle).lstrip("@").strip()


def _normalize_item(raw: dict[str, Any], index: int) -> dict[str, Any]:
    """Convert an Apify tweet into the dict format pipeline._normalize_x expects."""
    tweet_id = str(raw.get("id") or raw.get("tweetId") or f"X{index + 1}")
    text = str(raw.get("text") or raw.get("full_text") or "").strip()
    url = str(raw.get("url") or raw.get("twitterUrl") or "")

    # Engagement — apidojo returns these as *Count at top level
    likes = int(raw.get("likeCount", 0) or raw.get("favoriteCount", 0) or 0)
    reposts = int(raw.get("retweetCount", 0) or 0)
    replies = int(raw.get("replyCount", 0) or 0)
    quotes = int(raw.get("quoteCount", 0) or 0)
    bookmarks = int(raw.get("bookmarkCount", 0) or 0)

    author_handle = _extract_handle(raw)

    # Construct URL if missing but we have handle + id
    if not url and author_handle and tweet_id:
        url = f"https://x.com/{author_handle}/status/{tweet_id}"

    date_str = _parse_date(raw.get("createdAt") or raw.get("created_at") or raw.get("date"))

    engagement = {
        "likes": likes,
        "reposts": reposts,
        "replies": replies,
        "quotes": quotes,
    }
    if bookmarks:
        engagement["bookmarks"] = bookmarks

    return {
        "id": f"X{index + 1}",
        "text": text[:500],
        "url": url,
        "author_handle": author_handle,
        "date": date_str,
        "engagement": engagement,
        "relevance": _compute_relevance(likes, reposts, replies),
        "why_relevant": "Apify X/Twitter search",
    }


def _compute_relevance(likes: int, reposts: int, replies: int) -> float:
    """Estimate relevance from engagement signals."""
    likes_component = min(1.0, max(0.0, likes / 2000.0))
    reposts_component = min(1.0, max(0.0, reposts / 300.0))
    replies_component = min(1.0, max(0.0, replies / 150.0))
    return round(
        (likes_component * 0.5)
        + (reposts_component * 0.3)
        + (replies_component * 0.2),
        3,
    )


def search_x_apify(
    topic: str,
    from_date: str,
    to_date: str,
    depth: str = "default",
    token: str = "",
) -> list[dict[str, Any]]:
    """Search X/Twitter via Apify. Drop-in replacement for xai_x.search_x +
    parse_x_response.

    Args:
        topic: Search topic (supports Twitter advanced-search operators)
        from_date: Start date (YYYY-MM-DD)
        to_date: End date (YYYY-MM-DD)
        depth: 'quick', 'default', or 'deep'
        token: Apify API token

    Returns:
        List of normalized item dicts matching normalize._normalize_x's
        expected shape.
    """
    if not token:
        _log("No APIFY_API_TOKEN — skipping X")
        return []

    actor_input = _build_input(topic, from_date, to_date, depth)
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
    filtered = []
    for item in results:
        d = item.get("date")
        if d is None or (from_date <= d <= to_date):
            filtered.append(item)

    # Sort by combined engagement (likes + 2×reposts + 2×replies) as a quality proxy
    def _score(it: dict[str, Any]) -> int:
        e = it.get("engagement", {})
        return int(e.get("likes", 0)) + 2 * int(e.get("reposts", 0)) + 2 * int(e.get("replies", 0))

    filtered.sort(key=_score, reverse=True)

    # Re-index after sorting
    for i, item in enumerate(filtered):
        item["id"] = f"X{i + 1}"

    _log(f"Returning {len(filtered)} items after date filter")
    return filtered
