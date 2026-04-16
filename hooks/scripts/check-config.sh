#!/bin/bash
set -euo pipefail

# Check last30days configuration status from project-level .env

# Find project root (where .env.example lives)
SCRIPT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
PROJECT_ENV="$SCRIPT_DIR/.env"

if [[ ! -f "$PROJECT_ENV" ]]; then
  echo "/last30days: No .env found. Copy the template and add your keys:"
  echo "  cp .env.example .env"
  echo ""
  echo "Hacker News + Polymarket work without any keys."
  exit 0
fi

# Load env file into variables for inspection (without exporting)
while IFS='=' read -r key value; do
  [[ "$key" =~ ^[[:space:]]*# ]] && continue
  [[ -z "$key" ]] && continue
  key=$(echo "$key" | xargs)
  value=$(echo "$value" | xargs | sed 's/^["'\''"]//;s/["'\''"]$//')
  if [[ -n "$key" && -n "$value" ]]; then
    eval "ENV_${key}=\"${value}\""
  fi
done < "$PROJECT_ENV"

SOURCE_COUNT=2  # HN + Polymarket always free

HAS_OPENROUTER="${ENV_OPENROUTER_API_KEY:-${OPENROUTER_API_KEY:-}}"
HAS_APIFY="${ENV_APIFY_API_TOKEN:-${APIFY_API_TOKEN:-}}"

if [[ -n "$HAS_OPENROUTER" ]]; then
  SOURCE_COUNT=$((SOURCE_COUNT + 1))
fi
if [[ -n "$HAS_APIFY" ]]; then
  SOURCE_COUNT=$((SOURCE_COUNT + 4))  # Reddit, X, YouTube, Instagram via Apify
fi

echo "/last30days: Ready — config loaded from .env"
