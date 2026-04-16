"""Reddit search via Apify actor (trudax/reddit-scraper-lite).

Replaces openai_reddit.py, reddit_public.py, and reddit_enrich.py with a
single Apify-based module.  Returns list[dict] in the same shape the
pipeline already expects from reddit_public.

Apify URL convention: actor IDs use ``~`` between username and actor name
in API URLs (e.g. trudax~reddit-scraper-lite), even though the store URL
uses ``/``.
"""

from __future__ import annotations

import sys
import time
from typing import Any

from . import http

ACTOR_ID = "trudax~reddit-scraper-lite"
APIFY_RUN_URL = f"https://api.apify.com/v2/acts/{ACTOR_ID}/runs"

# Depth-aware limits
DEPTH_LIMITS = {
    "quick": 10,
    "default": 25,
    "deep": 50,
}

# Polling interval and timeout for async runs
POLL_INTERVAL = 3  # seconds
MAX_WAIT = 120  # seconds


def _log(msg: str) -> None:
    sys.stderr.write(f"[ApifyReddit] {msg}\n")
    sys.stderr.flush()


def _build_input(
    query: str,
    from_date: str,
    to_date: str,
    depth: str = "default",
    subreddits: list[str] | None = None,
) -> dict[str, Any]:
    """Build the Apify actor input payload for trudax/reddit-scraper-lite."""
    limit = DEPTH_LIMITS.get(depth, DEPTH_LIMITS["default"])

    actor_input: dict[str, Any] = {
        "searches": [query],
        "searchPosts": True,
        "searchComments": False,
        "searchCommunities": False,
        "searchUsers": False,
        "sort": "relevance",
        "time": "month",
        "maxItems": limit,
        "maxPostCount": limit,
        "maxComments": 10,
        "skipComments": False,
        "postDateLimit": from_date,
        "proxy": {"useApifyProxy": True},
    }

    if subreddits:
        # Lite scraper uses single subreddit field; pick the first one
        sub = subreddits[0].lstrip("r/").strip()
        actor_input["searchCommunityName"] = sub

    return actor_input


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

    # The endpoint returns a JSON array directly, or wrapped in {"data": [...]}
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict) and isinstance(resp.get("data"), list):
        return resp["data"]
    return []


def _normalize_item(raw: dict[str, Any], index: int) -> dict[str, Any]:
    """Convert an Apify Reddit result to the dict format pipeline expects."""
    # Handle various field names the actor might return
    score = int(raw.get("score", 0) or raw.get("upVotes", 0) or 0)
    num_comments = int(raw.get("numberOfComments", 0) or raw.get("numComments", 0) or raw.get("num_comments", 0) or 0)
    subreddit = raw.get("subreddit", "") or raw.get("communityName", "") or ""
    subreddit = subreddit.lstrip("r/")

    title = raw.get("title", "") or ""
    body = raw.get("body", "") or raw.get("selftext", "") or raw.get("text", "") or ""
    author = raw.get("author", "") or raw.get("username", "") or "[deleted]"
    url = raw.get("url", "") or raw.get("link", "") or ""

    # Parse date
    date_str = None
    created = raw.get("createdAt") or raw.get("created_utc") or raw.get("date")
    if created:
        try:
            if isinstance(created, (int, float)):
                from datetime import datetime, timezone
                dt = datetime.fromtimestamp(float(created), tz=timezone.utc)
                date_str = dt.strftime("%Y-%m-%d")
            elif isinstance(created, str) and len(created) >= 10:
                date_str = created[:10]  # "2026-04-10T..." → "2026-04-10"
        except (ValueError, TypeError, OSError):
            pass

    # Extract top comments if present
    top_comments = []
    comments = raw.get("comments", [])
    if isinstance(comments, list):
        for c in comments[:10]:
            if isinstance(c, dict):
                top_comments.append({
                    "score": int(c.get("score", 0) or c.get("upVotes", 0) or 0),
                    "excerpt": (c.get("body", "") or c.get("text", "") or "")[:200],
                    "author": c.get("author", "") or c.get("username", "") or "",
                })

    return {
        "id": f"R{index + 1}",
        "title": title.strip(),
        "url": url,
        "score": score,
        "num_comments": num_comments,
        "subreddit": subreddit,
        "author": author if author not in ("[deleted]", "[removed]") else "[deleted]",
        "selftext": body[:500] if body else "",
        "date": date_str,
        "engagement": {
            "score": score,
            "num_comments": num_comments,
        },
        "relevance": _compute_relevance(score, num_comments),
        "why_relevant": "Apify Reddit search",
        "metadata": {},
        "top_comments": top_comments if top_comments else None,
    }


def _compute_relevance(score: int, num_comments: int) -> float:
    score_component = min(1.0, max(0.0, score / 500.0))
    comments_component = min(1.0, max(0.0, num_comments / 200.0))
    return round((score_component * 0.6) + (comments_component * 0.4), 3)


def search_reddit_apify(
    topic: str,
    from_date: str,
    to_date: str,
    depth: str = "default",
    subreddits: list[str] | None = None,
    token: str = "",
) -> list[dict[str, Any]]:
    """Search Reddit via Apify. Drop-in replacement for search_reddit_public.

    Args:
        topic: Search topic
        from_date: Start date (YYYY-MM-DD)
        to_date: End date (YYYY-MM-DD)
        depth: 'quick', 'default', or 'deep'
        subreddits: Optional list of subreddit names
        token: Apify API token

    Returns:
        List of normalized item dicts matching the pipeline's expected format.
    """
    if not token:
        _log("No APIFY_API_TOKEN — skipping Reddit")
        return []

    actor_input = _build_input(topic, from_date, to_date, depth, subreddits)
    _log(f"Starting Apify run for '{topic}' (depth={depth})")

    # Start the run
    run_resp = _start_run(actor_input, token)
    run_data = run_resp.get("data", run_resp)
    run_id = run_data.get("id")

    if not run_id:
        _log(f"Failed to start Apify run: {run_resp}")
        return []

    # Poll until complete
    _log(f"Run started: {run_id}, polling...")
    dataset_id = _poll_run(run_id, token)

    if not dataset_id:
        _log("No dataset ID returned")
        return []

    # Fetch results
    raw_items = _fetch_dataset(dataset_id, token)
    _log(f"Got {len(raw_items)} raw items from Apify")

    # Normalize
    results = [_normalize_item(item, i) for i, item in enumerate(raw_items)]

    # Date filter
    filtered = []
    for item in results:
        d = item.get("date")
        if d is None or (from_date <= d <= to_date):
            filtered.append(item)

    # Sort by engagement
    filtered.sort(key=lambda x: x.get("engagement", {}).get("score", 0), reverse=True)

    # Re-index
    for i, item in enumerate(filtered):
        item["id"] = f"R{i + 1}"

    _log(f"Returning {len(filtered)} items after date filter")
    return filtered
