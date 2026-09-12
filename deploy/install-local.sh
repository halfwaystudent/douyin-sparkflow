#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  echo "Docker with the Compose plugin is required." >&2
  exit 1
fi

set_env_value() {
  local file="$1"
  local key="$2"
  local value="$3"
  if grep -q "^${key}=" "$file"; then
    local tmp_file
    tmp_file="$(mktemp)"
    awk -v key="$key" -v value="$value" '
      BEGIN { replaced = 0 }
      $0 ~ "^" key "=" { print key "=" value; replaced = 1; next }
      { print }
      END { if (!replaced) print key "=" value }
    ' "$file" > "$tmp_file"
    mv "$tmp_file" "$file"
  else
    printf '%s=%s\n' "$key" "$value" >> "$file"
  fi
}

if [ ! -f ".env" ]; then
  cp ".env.example" ".env"
fi

proxy_sub_url="${PROXY_SUB_URL:-${1:-}}"
if [ -n "$proxy_sub_url" ]; then
  set_env_value ".env" "PROXY_SUB_URL" "$proxy_sub_url"
fi

mkdir -p proxy state/cron state/login-api state/login-profile state/browser-profiles DouYinSparkFlow/logs
if [ ! -f "proxy/config.yaml" ]; then
  cp "proxy/config.example.yaml" "proxy/config.yaml"
fi
if [ ! -s "state/cron/root" ]; then
  cat > "state/cron/root" <<'CRON'
*/20 10-17 * * * cd /app && python main.py --doTask >> /app/logs/app.log 2>&1
0 18 * * * cd /app && python main.py --doTask >> /app/logs/app.log 2>&1
20 18 * * * cd /app && env SPARKFLOW_MANUAL_RUN=1 SPARKFLOW_MANUAL_UNSENT_ONLY=1 PYTHONUNBUFFERED=1 python main.py --doTask >> /app/logs/app.log 2>&1
CRON
fi

bash ./refresh_proxy.sh
docker compose up -d --build

web_port="$(grep '^WEB_PORT=' .env | sed 's/^WEB_PORT=//')"
web_port="${web_port:-8787}"
url="http://localhost:${web_port}"
echo "Douyin SparkFlow is running: $url"
echo "Next: create the admin password, open the login desktop, scan the QR code, select target friends, and set the send window."

if command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$url" >/dev/null 2>&1 || true
elif command -v open >/dev/null 2>&1; then
  open "$url" >/dev/null 2>&1 || true
fi
