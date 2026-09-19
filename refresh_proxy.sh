#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ROOT_OVERRIDE="${APP_ROOT:-}"
ENV_FILE="$SCRIPT_DIR/.env"

read_env_value() {
  local key="$1"
  if [ ! -f "$ENV_FILE" ]; then
    return 0
  fi
  local raw
  raw="$(grep -E "^${key}=" "$ENV_FILE" | tail -n 1 | cut -d= -f2- || true)"
  raw="${raw%\"}"
  raw="${raw#\"}"
  raw="${raw%\'}"
  raw="${raw#\'}"
  printf '%s' "$raw"
}

APP_ROOT="${APP_ROOT_OVERRIDE:-$SCRIPT_DIR}"
PROXY_SUB_URL="${PROXY_SUB_URL:-$(read_env_value PROXY_SUB_URL)}"
PROXY_USER_AGENT="${PROXY_USER_AGENT:-$(read_env_value PROXY_USER_AGENT)}"

CONFIG_DIR="$APP_ROOT/proxy"
CONFIG_FILE="$CONFIG_DIR/config.yaml"
EXAMPLE_CONFIG="$CONFIG_DIR/config.example.yaml"
USER_AGENT="${PROXY_USER_AGENT:-clash-verge/1.7.7}"

mkdir -p "$CONFIG_DIR"

if [ -n "${PROXY_SUB_URL:-}" ]; then
  tmp_file="$(mktemp)"
  curl -fsSL -A "$USER_AGENT" "$PROXY_SUB_URL" -o "$tmp_file"
  mv "$tmp_file" "$CONFIG_FILE"
  echo "Proxy subscription refreshed: $CONFIG_FILE"
elif [ ! -f "$CONFIG_FILE" ]; then
  if [ ! -f "$EXAMPLE_CONFIG" ]; then
    echo "Missing $CONFIG_FILE and $EXAMPLE_CONFIG. Set PROXY_SUB_URL or provide a Mihomo config." >&2
    exit 1
  fi
  cp "$EXAMPLE_CONFIG" "$CONFIG_FILE"
  echo "PROXY_SUB_URL is empty. Created a DIRECT-only proxy config from config.example.yaml."
else
  echo "PROXY_SUB_URL is empty. Keeping existing proxy/config.yaml."
fi

ensure_line() {
  local key="$1"
  local value="$2"
  if grep -q "^${key}:" "$CONFIG_FILE"; then
    if sed --version >/dev/null 2>&1; then
      sed -i "s#^${key}:.*#${key}: ${value}#" "$CONFIG_FILE"
    else
      sed -i '' "s#^${key}:.*#${key}: ${value}#" "$CONFIG_FILE"
    fi
  else
    printf '%s: %s\n' "$key" "$value" >> "$CONFIG_FILE"
  fi
}

ensure_line "mixed-port" "7890"
ensure_line "allow-lan" "true"
ensure_line "bind-address" "'*'"
ensure_line "external-controller" "'0.0.0.0:9090'"

if command -v docker >/dev/null 2>&1 && [ -f "$APP_ROOT/docker-compose.yml" ]; then
  if docker compose version >/dev/null 2>&1; then
    proxy_id="$(docker compose -f "$APP_ROOT/docker-compose.yml" ps -q proxy 2>/dev/null || true)"
    if [ -n "${proxy_id:-}" ]; then
      docker compose -f "$APP_ROOT/docker-compose.yml" restart proxy
    fi
  else
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx mihomo; then
      docker restart mihomo
    fi
  fi
fi
