"""Environment and API key management for last30days skill."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

# Project root: two levels up from this file (scripts/lib/env.py → repo root)
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Config: project-level .env at the repo root
# Override with LAST30DAYS_CONFIG_DIR for testing
_config_override = os.environ.get('LAST30DAYS_CONFIG_DIR')
if _config_override == "":
    CONFIG_DIR = None
    CONFIG_FILE = None
elif _config_override:
    CONFIG_DIR = Path(_config_override)
    CONFIG_FILE = CONFIG_DIR / ".env"
else:
    CONFIG_DIR = PROJECT_ROOT
    CONFIG_FILE = PROJECT_ROOT / ".env"

AuthSource = Literal["api_key", "none"]
AuthStatus = Literal["ok", "missing"]

AUTH_SOURCE_API_KEY: AuthSource = "api_key"
AUTH_SOURCE_NONE: AuthSource = "none"

AUTH_STATUS_OK: AuthStatus = "ok"
AUTH_STATUS_MISSING: AuthStatus = "missing"


@dataclass(frozen=True)
class OpenAIAuth:
    token: str | None
    source: AuthSource
    status: AuthStatus


def _check_file_permissions(path: Path) -> None:
    """Warn to stderr if a secrets file has overly permissive permissions."""
    try:
        mode = path.stat().st_mode
        # Check if group or other can read (bits 0o044)
        if mode & 0o044:
            sys.stderr.write(
                f"[last30days] WARNING: {path} is readable by other users. "
                f"Run: chmod 600 {path}\n"
            )
            sys.stderr.flush()
    except OSError as exc:
        sys.stderr.write(f"[last30days] WARNING: could not stat {path}: {exc}\n")
        sys.stderr.flush()


def load_env_file(path: Path) -> dict[str, str]:
    """Load environment variables from a file."""
    env = {}
    if not path or not path.exists():
        return env
    _check_file_permissions(path)

    with open(path, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '=' in line:
                key, _, value = line.partition('=')
                key = key.strip()
                value = value.strip()
                # Remove quotes if present
                if value and value[0] in ('"', "'") and value[-1] == value[0]:
                    value = value[1:-1]
                if key and value:
                    env[key] = value
    return env


def get_openai_auth(file_env: dict[str, str]) -> OpenAIAuth:
    """Resolve OpenAI auth from API key."""
    api_key = os.environ.get('OPENAI_API_KEY') or file_env.get('OPENAI_API_KEY')
    if api_key:
        return OpenAIAuth(
            token=api_key,
            source=AUTH_SOURCE_API_KEY,
            status=AUTH_STATUS_OK,
        )
    return OpenAIAuth(
        token=None,
        source=AUTH_SOURCE_NONE,
        status=AUTH_STATUS_MISSING,
    )


def get_config() -> dict[str, Any]:
    """Load configuration from project-level .env file.

    Priority (highest wins):
      1. Environment variables (os.environ)
      2. Project .env file at repo root
    """
    file_env = load_env_file(CONFIG_FILE) if CONFIG_FILE else {}

    openai_auth = get_openai_auth(file_env)

    config = {
        'OPENAI_API_KEY': openai_auth.token,
        'OPENAI_AUTH_SOURCE': openai_auth.source,
        'OPENAI_AUTH_STATUS': openai_auth.status,
    }

    keys = [
        ('LAST30DAYS_REASONING_PROVIDER', 'auto'),
        ('LAST30DAYS_PLANNER_MODEL', None),
        ('LAST30DAYS_RERANK_MODEL', None),
        ('APIFY_API_TOKEN', None),
        ('BRAVE_API_KEY', None),
        ('SERPER_API_KEY', None),
        ('EXA_API_KEY', None),
        ('PARALLEL_API_KEY', None),
        ('GITHUB_TOKEN', None),
        ('OPENROUTER_API_KEY', None),
        ('OPENROUTER_MODEL', None),
        ('INCLUDE_SOURCES', None),
    ]

    for key, default in keys:
        config[key] = os.environ.get(key) or file_env.get(key, default)

    if CONFIG_FILE and CONFIG_FILE.exists():
        config['_CONFIG_SOURCE'] = f'project:{CONFIG_FILE}'
    else:
        config['_CONFIG_SOURCE'] = 'env_only'

    return config


# ---------------------------------------------------------------------------
# Source availability checks
# ---------------------------------------------------------------------------


def config_exists() -> bool:
    """Check if project .env exists."""
    if CONFIG_FILE:
        return CONFIG_FILE.exists()
    return False


def is_apify_available(config: dict[str, Any]) -> bool:
    """Check if Apify scraping platform is available."""
    return bool(config.get('APIFY_API_TOKEN'))


def is_hackernews_available() -> bool:
    """Always True — HN uses free Algolia API, no key needed."""
    return True


def is_polymarket_available() -> bool:
    """Always True — Gamma API is free, no key needed."""
    return True


def is_reddit_available(config: dict[str, Any]) -> bool:
    """Check if Reddit search is available (via Apify)."""
    return bool(config.get('APIFY_API_TOKEN'))


def is_linkedin_available(config: dict[str, Any]) -> bool:
    """Check if LinkedIn post search is available (via Apify)."""
    return bool(config.get('APIFY_API_TOKEN'))


def is_youtube_available(config: dict[str, Any]) -> bool:
    """Check if YouTube search is available (via Apify)."""
    return bool(config.get('APIFY_API_TOKEN'))


def get_x_source(config: dict[str, Any]) -> str | None:
    """Determine if X/Twitter source is available. Always Apify in this fork."""
    if config.get('APIFY_API_TOKEN'):
        return 'apify'
    return None


def get_x_source_status(config: dict[str, Any]) -> dict[str, Any]:
    """Return X backend status for diagnose() output."""
    return {
        "source": "apify" if config.get('APIFY_API_TOKEN') else None,
    }
