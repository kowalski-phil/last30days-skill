"""Minimal setup helper for the fork.

The upstream repo had a 500-line wizard that auto-detected browser cookies,
ran OAuth device flows, and negotiated with multiple API providers. This fork
consolidates all of that into two environment variables, so the "wizard" is
just a friendly check of whether those two keys are set.

Setup flow:
    1. Copy .env.example to .env
    2. Fill in OPENROUTER_API_KEY and APIFY_API_TOKEN
    3. Done
"""

from __future__ import annotations

from typing import Any


def is_first_run(config: dict[str, Any]) -> bool:
    """True if the user hasn't configured either required key yet."""
    return not (config.get("OPENROUTER_API_KEY") or config.get("APIFY_API_TOKEN"))


def check_setup(config: dict[str, Any]) -> dict[str, Any]:
    """Return a status dict describing which keys are present.

    Returns:
        {
            "openrouter_ok": bool,
            "apify_ok": bool,
            "env_file_exists": bool,
            "env_file_path": str | None,
            "missing": list[str],
        }
    """
    from . import env as env_mod

    env_path = env_mod.CONFIG_FILE
    env_exists = bool(env_path and env_path.exists())

    openrouter_ok = bool(config.get("OPENROUTER_API_KEY"))
    apify_ok = bool(config.get("APIFY_API_TOKEN"))

    missing = []
    if not openrouter_ok:
        missing.append("OPENROUTER_API_KEY")
    if not apify_ok:
        missing.append("APIFY_API_TOKEN")

    return {
        "openrouter_ok": openrouter_ok,
        "apify_ok": apify_ok,
        "env_file_exists": env_exists,
        "env_file_path": str(env_path) if env_path else None,
        "missing": missing,
    }


def get_setup_status_text(status: dict[str, Any]) -> str:
    """Build a human-readable setup status message."""
    lines: list[str] = []
    lines.append("last30days setup status:")
    lines.append("")

    path = status.get("env_file_path") or ".env"
    if status.get("env_file_exists"):
        lines.append(f"  .env file:          found at {path}")
    else:
        lines.append(f"  .env file:          NOT FOUND — expected at {path}")
        lines.append("                      Run: cp .env.example .env")

    lines.append(
        f"  OPENROUTER_API_KEY: {'set' if status.get('openrouter_ok') else 'missing'}"
    )
    lines.append(
        f"  APIFY_API_TOKEN:    {'set' if status.get('apify_ok') else 'missing'}"
    )

    missing = status.get("missing") or []
    if missing:
        lines.append("")
        lines.append("Next steps:")
        if "OPENROUTER_API_KEY" in missing:
            lines.append(
                "  - OpenRouter: get a key at https://openrouter.ai/keys "
                "and add OPENROUTER_API_KEY=sk-or-... to your .env"
            )
        if "APIFY_API_TOKEN" in missing:
            lines.append(
                "  - Apify: get a token at https://console.apify.com/account/integrations "
                "and add APIFY_API_TOKEN=apify_api_... to your .env"
            )
    else:
        lines.append("")
        lines.append("All required keys are set. You're ready to run:")
        lines.append('  python3 scripts/last30days.py "your topic" --emit=compact')

    return "\n".join(lines)
