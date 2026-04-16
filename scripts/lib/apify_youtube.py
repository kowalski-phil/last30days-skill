"""YouTube search via Apify actor (streamers/youtube-scraper).

Replaces youtube_yt.py (yt-dlp CLI) with an Apify-based wrapper.

Phases:
- 4a: search-only — returns video metadata, no transcripts.
- 4b: with transcripts — same actor downloads English subtitles in
  plaintext, then we feed the text into the existing
  extract_transcript_highlights() helper from youtube_yt.

Returns list[dict] in the shape that pipeline → normalize._normalize_youtube
expects, namely:
    {
        "video_id", "url", "title", "description", "channel_name",
        "date" (YYYY-MM-DD), "engagement" (dict),
        "relevance", "why_relevant", "transcript_snippet",
        "transcript_highlights"
    }

Apify URL convention: actor IDs use ``~`` between username and actor name
in API URLs (e.g. streamers~youtube-scraper).
"""

from __future__ import annotations

import re
import sys
import time
from typing import Any

from . import http
from .youtube_yt import extract_transcript_highlights

ACTOR_ID = "streamers~youtube-scraper"
APIFY_RUN_URL = f"https://api.apify.com/v2/acts/{ACTOR_ID}/runs"

# Depth-aware limits for video counts
DEPTH_LIMITS = {
    "quick": 6,
    "default": 10,
    "deep": 25,
}

# How many transcripts to actually request per run, by depth.
# Matches the original youtube_yt.py limits.
TRANSCRIPT_LIMITS = {
    "quick": 0,
    "default": 2,
    "deep": 8,
}

# Cap on transcript length (in words) before passing to highlight extraction
TRANSCRIPT_MAX_WORDS = 5000

# Polling interval and timeout for async runs
POLL_INTERVAL = 3  # seconds
MAX_WAIT = 240  # seconds — transcripts add overhead


def _log(msg: str) -> None:
    sys.stderr.write(f"[ApifyYouTube] {msg}\n")
    sys.stderr.flush()


def _build_input(
    query: str,
    from_date: str,
    depth: str = "default",
    with_transcripts: bool = True,
) -> dict[str, Any]:
    """Build the Apify actor input payload for streamers/youtube-scraper."""
    limit = DEPTH_LIMITS.get(depth, DEPTH_LIMITS["default"])

    payload: dict[str, Any] = {
        "searchQueries": [query],
        "maxResults": limit,
        "maxResultsShorts": 0,       # skip Shorts
        "maxResultStreams": 0,       # skip live streams
        # Server-side YouTube date filter — equivalent to the "Upload date:
        # This month" dropdown in the YouTube UI. Required for low-velocity
        # topics: without it, the relevance ranker returns evergreen classics
        # from 2023-2025 and the client-side date filter drops everything.
        "dateFilter": "month",
        "oldestPostDate": from_date, # belt-and-braces client-side cutoff
        "sortingOrder": "relevance",
        "downloadSubtitles": with_transcripts,
    }
    if with_transcripts:
        payload["subtitlesLanguage"] = "en"
        payload["subtitlesFormat"] = "plaintext"
    return payload


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


_SRT_TIMESTAMP_RE = re.compile(
    r"\d+\s*\n?\d{1,2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{1,2}:\d{2}:\d{2}[,.]\d{3}"
)
_SRT_INDEX_RE = re.compile(r"^\d+\s*$", re.MULTILINE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def _clean_srt(text: str) -> str:
    """Strip SRT timing codes, indices, and HTML tags so the LLM sees only words.

    The actor's `plaintext` format is supposed to do this, but in practice
    we still occasionally see SRT artifacts — defensive cleanup.
    """
    if not text:
        return ""
    text = _SRT_TIMESTAMP_RE.sub(" ", text)
    text = _SRT_INDEX_RE.sub(" ", text)
    text = _HTML_TAG_RE.sub(" ", text)
    text = _WHITESPACE_RE.sub(" ", text)
    return text.strip()


def _extract_transcript(subtitles: Any) -> str:
    """Pull a plain-text transcript from the Apify subtitles array.

    Prefers user-uploaded English captions, falls back to auto-generated.
    Returns "" if no usable subtitles are present.
    """
    if not isinstance(subtitles, list) or not subtitles:
        return ""

    # Prefer English non-auto-generated, then English auto, then anything.
    # The streamers/youtube-scraper actor returns plaintext content under
    # the "plaintext" field (not "srt") when subtitlesFormat=plaintext.
    user_en = None
    auto_en = None
    fallback = None
    for sub in subtitles:
        if not isinstance(sub, dict):
            continue
        lang = (sub.get("language") or "").lower()
        kind = (sub.get("type") or "").lower()
        text = sub.get("plaintext") or sub.get("srt") or sub.get("text") or ""
        if not text:
            continue
        if lang.startswith("en"):
            if "auto" not in kind and user_en is None:
                user_en = text
            elif auto_en is None:
                auto_en = text
        elif fallback is None:
            fallback = text

    raw = user_en or auto_en or fallback or ""
    cleaned = _clean_srt(raw)

    # Cap to TRANSCRIPT_MAX_WORDS to bound LLM input size
    words = cleaned.split()
    if len(words) > TRANSCRIPT_MAX_WORDS:
        cleaned = " ".join(words[:TRANSCRIPT_MAX_WORDS]) + "..."
    return cleaned


def _parse_date(value: Any) -> str | None:
    """Convert various Apify date formats to YYYY-MM-DD."""
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
        # ISO format like "2026-04-10T12:34:56Z" or just "2026-04-10"
        if len(value) >= 10 and value[4] == "-" and value[7] == "-":
            return value[:10]
    return None


def _normalize_item(raw: dict[str, Any], index: int, topic: str = "") -> dict[str, Any]:
    """Convert an Apify YouTube result to the dict format pipeline expects."""
    video_id = str(raw.get("id") or raw.get("videoId") or f"YT{index + 1}")
    title = str(raw.get("title", "") or "").strip()
    description = str(raw.get("text", "") or raw.get("description", "") or "").strip()
    channel = str(raw.get("channelName", "") or raw.get("channel", "") or "").strip()
    url = str(raw.get("url", "") or "")

    views = int(raw.get("viewCount", 0) or 0)
    likes = int(raw.get("likes", 0) or 0)
    comments = int(raw.get("commentsCount", 0) or 0)

    date_str = _parse_date(raw.get("date"))

    transcript = _extract_transcript(raw.get("subtitles"))
    highlights: list[str] = []
    if transcript and topic:
        try:
            highlights = extract_transcript_highlights(transcript, topic, limit=5)
        except Exception:
            highlights = []

    return {
        "video_id": video_id,
        "url": url,
        "title": title,
        "description": description,
        "channel_name": channel,
        "date": date_str,
        "engagement": {
            "views": views,
            "likes": likes,
            "comments": comments,
        },
        "relevance": _compute_relevance(views, likes, comments),
        "why_relevant": "Apify YouTube search",
        "transcript_snippet": transcript,
        "transcript_highlights": highlights,
    }


def _compute_relevance(views: int, likes: int, comments: int) -> float:
    """Estimate relevance from engagement signals."""
    views_component = min(1.0, max(0.0, views / 100_000.0))
    likes_component = min(1.0, max(0.0, likes / 5_000.0))
    comments_component = min(1.0, max(0.0, comments / 500.0))
    return round(
        (views_component * 0.5)
        + (likes_component * 0.3)
        + (comments_component * 0.2),
        3,
    )


def search_youtube_apify(
    topic: str,
    from_date: str,
    to_date: str,
    depth: str = "default",
    token: str = "",
) -> dict[str, Any]:
    """Search YouTube via Apify. Returns dict with 'items' key for compatibility
    with pipeline.py which calls youtube_yt.parse_youtube_response(result).

    Transcripts: requested when depth's TRANSCRIPT_LIMITS > 0 (default and deep).
    Subtitles add ~30s per video to actor runtime, so quick mode skips them.

    Args:
        topic: Search topic
        from_date: Start date (YYYY-MM-DD)
        to_date: End date (YYYY-MM-DD)
        depth: 'quick', 'default', or 'deep'
        token: Apify API token

    Returns:
        Dict with 'items' key containing list of normalized video dicts.
    """
    if not token:
        _log("No APIFY_API_TOKEN — skipping YouTube")
        return {"items": []}

    with_transcripts = TRANSCRIPT_LIMITS.get(depth, 0) > 0
    actor_input = _build_input(topic, from_date, depth, with_transcripts=with_transcripts)
    transcript_note = "with transcripts" if with_transcripts else "metadata only"
    _log(f"Starting Apify run for '{topic}' (depth={depth}, {transcript_note})")

    try:
        run_resp = _start_run(actor_input, token)
    except Exception as exc:
        _log(f"Failed to start run: {type(exc).__name__}: {exc}")
        return {"items": []}

    run_data = run_resp.get("data", run_resp)
    run_id = run_data.get("id")

    if not run_id:
        _log(f"No run ID in response: {run_resp}")
        return {"items": []}

    _log(f"Run started: {run_id}, polling...")
    try:
        dataset_id = _poll_run(run_id, token)
    except Exception as exc:
        _log(f"Polling failed: {type(exc).__name__}: {exc}")
        return {"items": []}

    if not dataset_id:
        _log("No dataset ID returned")
        return {"items": []}

    raw_items = _fetch_dataset(dataset_id, token)
    _log(f"Got {len(raw_items)} raw items from Apify")

    results = [_normalize_item(item, i, topic=topic) for i, item in enumerate(raw_items)]
    if with_transcripts:
        n_with = sum(1 for r in results if r.get("transcript_snippet"))
        _log(f"Extracted transcripts for {n_with}/{len(results)} videos")

    # Date filter — keep items in the window or with unknown dates
    filtered = []
    for item in results:
        d = item.get("date")
        if d is None or (from_date <= d <= to_date):
            filtered.append(item)

    # Sort by views (engagement proxy)
    filtered.sort(
        key=lambda x: x.get("engagement", {}).get("views", 0),
        reverse=True,
    )

    _log(f"Returning {len(filtered)} items after date filter")
    return {"items": filtered}
