#!/usr/bin/env bash
set -euo pipefail
umask 077

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
  raw="${raw%$'\r'}"
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

# The controller API must not be open to every container on the network. The
# secret lives under state/ so a subscription refresh that replaces config.yaml
# writes the same value back instead of rotating it on every refresh. It is
# resolved before the subscription download so a rejected secret never leaves
# a freshly downloaded config without one.
SECRET_FILE="$APP_ROOT/state/proxy/controller-secret"
PROXY_CONTROLLER_SECRET="${PROXY_CONTROLLER_SECRET:-$(read_env_value PROXY_CONTROLLER_SECRET)}"
if [ -n "${PROXY_CONTROLLER_SECRET:-}" ]; then
  controller_secret="$PROXY_CONTROLLER_SECRET"
  echo "Proxy controller secret: using PROXY_CONTROLLER_SECRET."
elif [ -s "$SECRET_FILE" ]; then
  controller_secret="$(tr -d '\r\n' < "$SECRET_FILE")"
  echo "Proxy controller secret: keeping $SECRET_FILE."
else
  mkdir -p "$(dirname "$SECRET_FILE")"
  controller_secret="$(od -An -tx1 -N16 /dev/urandom | tr -d ' \n')"
  (umask 077 && printf '%s\n' "$controller_secret" > "$SECRET_FILE")
  chmod 600 "$SECRET_FILE" 2>/dev/null || true
  echo "Proxy controller secret: generated $SECRET_FILE."
fi
# The value is written into a single-quoted YAML scalar through sed, so keep it
# to characters that need no escaping in either.
if ! printf '%s' "$controller_secret" | grep -Eq '^[A-Za-z0-9._~-]+$'; then
  echo "Proxy controller secret may only contain letters, digits and . _ ~ -" >&2
  exit 1
fi

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
    local updated_file
    updated_file="$(mktemp "${CONFIG_FILE}.tmp.XXXXXX")"
    if ! sed "s#^${key}:.*#${key}: ${value}#" "$CONFIG_FILE" > "$updated_file"; then
      rm -f "$updated_file"
      return 1
    fi
    mv "$updated_file" "$CONFIG_FILE"
  else
    printf '%s: %s\n' "$key" "$value" >> "$CONFIG_FILE"
  fi
}

ensure_line "mixed-port" "7890"
ensure_line "allow-lan" "true"
ensure_line "bind-address" "'*'"
ensure_line "external-controller" "'0.0.0.0:9090'"
ensure_line "secret" "'$controller_secret'"
chmod 600 "$CONFIG_FILE"

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
