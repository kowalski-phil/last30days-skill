#!/usr/bin/env python3
"""Trend Radar — synthesize a last30days raw dump into a themed brief.

Reads a raw last30days markdown dump (or runs the pipeline fresh), sends it
through OpenRouter with a trend-radar synthesis prompt, and writes a
structured brief to data/briefs/.

Designed for the "trend radar" job-to-be-done: spot emerging patterns in
finance/AI discourse before the mainstream catches on.

Usage:
    # Synthesize from existing raw markdown (free — no retrieval cost)
    python3 scripts/trend_radar.py "agentic FP&A" --from-raw research/agentic-fp-a-raw.md

    # Run last30days fresh, then synthesize
    python3 scripts/trend_radar.py "agentic FP&A" --search=linkedin,x,youtube,reddit,hackernews

    # Preview the prompt without calling the LLM
    python3 scripts/trend_radar.py "agentic FP&A" --from-raw research/agentic-fp-a-raw.md --dry-run
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

MIN_PYTHON = (3, 12)
if sys.version_info[:2] < MIN_PYTHON:
    sys.stderr.write("trend_radar requires Python 3.12+\n")
    raise SystemExit(1)

# Force UTF-8 on stdout/stderr so social-media emoji and non-cp1252 chars
# don't crash Windows file writes / prints (mirrors last30days.py).
for _stream in (sys.stdout, sys.stderr):
    _reconfig = getattr(_stream, "reconfigure", None)
    if _reconfig is not None:
        try:
            _reconfig(encoding="utf-8", errors="replace")
        except Exception:
            pass

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

from lib import env, providers  # noqa: E402

BRIEFS_DIR = PROJECT_ROOT / "data" / "briefs"
RESEARCH_DIR = PROJECT_ROOT / "research"
DEFAULT_MODEL = "openai/gpt-4.1"
DEFAULT_SEARCH = "linkedin,x,youtube,reddit,hackernews"


# ---------------------------------------------------------------------------
# Synthesis prompt
# ---------------------------------------------------------------------------

SYNTHESIS_PROMPT = """You are a senior finance-AI analyst writing a trend-radar brief for CFOs, FP&A leaders, and finance-operations executives at mid-to-large enterprises.

Your job: read the raw research dump below (real posts from LinkedIn, X, YouTube, Reddit, Hacker News — ranked by engagement and relevance over the last 30 days), then write a STRUCTURED BRIEFING that helps a finance leader spot emerging patterns they can act on.

# Topic
__TOPIC__

# Today's date
__TODAY__

# Hard rules
- This is a trend-radar brief, NOT a summary. A summary lists what people said; a trend radar names the 3–5 underlying SHIFTS in the conversation and explains what's new, contested, or inflecting.
- Every theme MUST be grounded in specific evidence from the dump. Cite 2–4 items per theme using the format: `([Source] Title — URL)`.
- Do NOT invent quotes, metrics, vendor names, or people. If it is not in the dump, it does not go in the brief.
- If the dump is thin or contradictory on a point, say so. Vague claims ("many people are talking about X") are banned — cite specific items or cut the claim.
- Counter-signals (voices pushing back, gaps between marketing and reality, failed deployments, skeptical takes) are GOLD. Surface them explicitly.
- Velocity labels — use only when supported by evidence:
  - `rising`: multiple items in the last 2 weeks, new angle, or net-new vendor/person entering the conversation
  - `steady`: continuous discourse, no clear inflection
  - `cresting`: heavy vendor marketing, fatigue signals, or "this is becoming buzzword" commentary
- Tone: sharp, opinionated, skeptical of vendor marketing. Write like a CFO reads it — time-constrained, allergic to fluff, respects concrete evidence.

# Output schema — produce markdown EXACTLY in this shape, no preamble, no closing remarks:

# Trend Radar: __TOPIC__ — __TODAY__

## TL;DR
[3 sentences. What is the single most important shift this month? What is the unresolved question? Who moved?]

## Themes

### 1. [Named theme in punchy language, e.g. "Every vendor is now an 'agent' vendor"]
- **The story:** [2–3 sentences explaining the underlying pattern, not a restatement of one post]
- **Evidence:**
  - ([Source] Title — URL): [1 line on why it matters]
  - ([Source] Title — URL): [1 line on why it matters]
  - ([Source] Title — URL): [1 line on why it matters]
- **Velocity:** rising | steady | cresting

### 2. [...]
[same structure — 3 to 5 themes total]

## Counter-signals
[Voices pushing back, skeptical takes, gaps between marketing claims and real deployments. 2–4 items, each with the same (Source — Title — URL) citation format and a 1-line framing.]

## Watchlist
- **People worth following:** [Named individuals from the dump, each with a 1-line why]
- **Vendors / products in play:** [Named products/companies from the dump, each with a 1-line why]
- **Quoted metrics to verify:** [Headline stats from the dump worth independent verification — e.g. "HPE: 5x faster close"]

## Content hooks
[3–5 post angles a finance content creator could publish this week, each 1–2 sentences, each tied to a specific item in the dump.]

# Raw research dump

__RAW__
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "topic"


def run_last30days(topic: str, sources: str, save_dir: Path) -> Path:
    """Run last30days.py as a subprocess and return the path to the raw markdown."""
    save_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        str(SCRIPT_DIR / "last30days.py"),
        topic,
        "--emit=compact",
        f"--search={sources}",
        f"--save-dir={save_dir}",
    ]
    print(f"[trend_radar] Running last30days: {' '.join(cmd[2:])}", file=sys.stderr)
    subprocess.run(cmd, check=True, cwd=str(PROJECT_ROOT))
    slug = slugify(topic)
    # save_output() writes <slug>-raw.md, or appends date suffix if that exists.
    primary = save_dir / f"{slug}-raw.md"
    if primary.exists():
        return primary
    today = datetime.now().strftime("%Y-%m-%d")
    dated = save_dir / f"{slug}-raw-{today}.md"
    if dated.exists():
        return dated
    raise SystemExit(
        f"Expected raw output at {primary} or {dated}, but neither was written."
    )


def load_raw(path: Path) -> str:
    if not path.exists():
        raise SystemExit(f"Raw file not found: {path}")
    return path.read_text(encoding="utf-8")


def build_prompt(topic: str, raw: str, today: str) -> str:
    return (
        SYNTHESIS_PROMPT
        .replace("__TOPIC__", topic)
        .replace("__TODAY__", today)
        .replace("__RAW__", raw)
    )


def synthesize(prompt: str, model: str, api_key: str) -> str:
    client = providers.OpenRouterClient(api_key)
    return client.generate_text(model, prompt).strip()


def save_brief(topic: str, content: str, save_dir: Path) -> Path:
    save_dir.mkdir(parents=True, exist_ok=True)
    slug = slugify(topic)
    date = datetime.now().strftime("%Y-%m-%d")
    out_path = save_dir / f"{slug}-trend-radar-{date}.md"
    out_path.write_text(content, encoding="utf-8")
    return out_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Synthesize last30days output into a trend-radar brief."
    )
    parser.add_argument("topic", help="Topic (e.g. 'agentic FP&A')")
    parser.add_argument(
        "--from-raw",
        help="Path to existing raw markdown (skips retrieval — free to iterate).",
    )
    parser.add_argument(
        "--search",
        default=DEFAULT_SEARCH,
        help=f"Sources for fresh retrieval (default: {DEFAULT_SEARCH})",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"OpenRouter synthesis model (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--save-dir",
        default=str(BRIEFS_DIR),
        help=f"Output directory (default: {BRIEFS_DIR})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the composed prompt and exit (no LLM call, no retrieval).",
    )
    args = parser.parse_args()

    config = env.get_config()
    api_key = config.get("OPENROUTER_API_KEY")
    if not api_key and not args.dry_run:
        raise SystemExit(
            "OPENROUTER_API_KEY not configured. Check .env at project root."
        )

    if args.from_raw:
        raw_path = Path(args.from_raw).expanduser().resolve()
        raw = load_raw(raw_path)
        print(
            f"[trend_radar] Loaded raw: {raw_path} ({len(raw):,} chars)",
            file=sys.stderr,
        )
    else:
        raw_path = run_last30days(args.topic, args.search, RESEARCH_DIR)
        raw = load_raw(raw_path)
        print(
            f"[trend_radar] Retrieved raw: {raw_path} ({len(raw):,} chars)",
            file=sys.stderr,
        )

    today = datetime.now().strftime("%Y-%m-%d")
    prompt = build_prompt(args.topic, raw, today)

    if args.dry_run:
        print(prompt)
        return

    print(
        f"[trend_radar] Synthesizing with {args.model} "
        f"({len(prompt):,} chars prompt)...",
        file=sys.stderr,
    )
    brief = synthesize(prompt, args.model, api_key)

    out_dir = Path(args.save_dir).expanduser().resolve()
    out_path = save_brief(args.topic, brief, out_dir)
    print(f"[trend_radar] Brief written: {out_path}", file=sys.stderr)
    print(out_path)


if __name__ == "__main__":
    main()
