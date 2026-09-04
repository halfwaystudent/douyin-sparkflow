#!/usr/bin/env bash
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/halfwaystudent/douyin-sparkflow.git}"
BRANCH="${BRANCH:-main}"
APP_ROOT="${APP_ROOT:-/opt/douyin-sparkflow}"
ACTION="${ACTION:-install}"
DEFAULT_SCHEDULE="${DEFAULT_SCHEDULE:-10:00-18:00/20m}"

if [ "$(id -u)" -ne 0 ]; then
  SUDO="sudo"
else
  SUDO=""
fi

run_root() {
  if [ -n "$SUDO" ]; then
    sudo "$@"
  else
    "$@"
  fi
}

log() {
  printf '\n[install-server] %s\n' "$*"
}

install_base_tools() {
  if command -v curl >/dev/null 2>&1 && command -v git >/dev/null 2>&1 && command -v gpg >/dev/null 2>&1; then
    return
  fi
  if command -v apt-get >/dev/null 2>&1; then
    run_root apt-get update
    run_root apt-get install -y ca-certificates curl git gnupg
  elif command -v yum >/dev/null 2>&1; then
    run_root yum install -y ca-certificates curl git
  else
    echo "Install curl, git, and ca-certificates first." >&2
    exit 1
  fi
}

install_docker_debian() {
  . /etc/os-release
  local docker_id="${ID}"
  if [ "$docker_id" = "debian" ] || [ "$docker_id" = "ubuntu" ]; then
    run_root install -m 0755 -d /etc/apt/keyrings
    curl -fsSL "https://download.docker.com/linux/${docker_id}/gpg" | run_root tee /etc/apt/keyrings/docker.asc >/dev/null
    run_root chmod a+r /etc/apt/keyrings/docker.asc
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/${docker_id} ${VERSION_CODENAME} stable" | run_root tee /etc/apt/sources.list.d/docker.list >/dev/null
    run_root apt-get update
    run_root apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  else
    run_root apt-get install -y docker.io docker-compose-plugin
  fi
}

ensure_docker() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    return
  fi
  log "Installing Docker and Compose plugin"
  if command -v apt-get >/dev/null 2>&1; then
    install_docker_debian
  elif command -v yum >/dev/null 2>&1; then
    run_root yum install -y docker docker-compose-plugin
  else
    echo "Docker is not installed. Please install Docker with the Compose plugin first." >&2
    exit 1
  fi
  run_root systemctl enable --now docker || true
}

prepare_repo() {
  run_root mkdir -p "$(dirname "$APP_ROOT")"
  local runtime_config_backup=""
  if [ -f "$APP_ROOT/DouYinSparkFlow/config.json" ]; then
    runtime_config_backup="$(mktemp)"
    run_root cp "$APP_ROOT/DouYinSparkFlow/config.json" "$runtime_config_backup"
  fi

  if [ -d "$APP_ROOT/.git" ]; then
    log "Updating existing repository at $APP_ROOT"
    run_root git -C "$APP_ROOT" fetch origin "$BRANCH"
    run_root git -C "$APP_ROOT" checkout -B "$BRANCH" "origin/$BRANCH"
    run_root git -C "$APP_ROOT" reset --hard "origin/$BRANCH"
  else
    if [ -e "$APP_ROOT" ]; then
      echo "$APP_ROOT exists but is not a git checkout. Back up runtime data and move the directory aside before installing." >&2
      exit 1
    fi
    log "Cloning $REPO_URL#$BRANCH into $APP_ROOT"
    run_root git clone --branch "$BRANCH" "$REPO_URL" "$APP_ROOT"
  fi

  if [ -n "$runtime_config_backup" ]; then
    run_root mkdir -p "$APP_ROOT/DouYinSparkFlow"
    run_root cp "$runtime_config_backup" "$APP_ROOT/DouYinSparkFlow/config.json"
    rm -f "$runtime_config_backup"
    log "Restored runtime config.json after repository update"
  fi
}

set_env_value() {
  local file="$1"
  local key="$2"
  local value="$3"
  local tmp_file
  tmp_file="$(mktemp)"
  if [ -f "$file" ]; then
    awk -v key="$key" -v value="$value" '
      BEGIN { replaced = 0 }
      $0 ~ "^" key "=" { print key "=" value; replaced = 1; next }
      { print }
      END { if (!replaced) print key "=" value }
    ' "$file" > "$tmp_file"
  else
    printf '%s=%s\n' "$key" "$value" > "$tmp_file"
  fi
  run_root cp "$tmp_file" "$file"
  rm -f "$tmp_file"
}

read_env_value() {
  local file="$1"
  local key="$2"
  if [ ! -f "$file" ]; then
    return 0
  fi
  grep -E "^${key}=" "$file" | tail -n 1 | cut -d= -f2- || true
}

remove_legacy_host_cron() {
  local current_file filtered_file backup_dir backup_file
  current_file="$(mktemp)"
  filtered_file="$(mktemp)"

  if ! run_root crontab -l > "$current_file" 2>/dev/null; then
    rm -f "$current_file" "$filtered_file"
    return
  fi

  awk '
    function is_legacy_sparkflow_job(line) {
      return line ~ /main\.py --doTask/ \
        && line ~ /docker ps --format/ \
        && line ~ /docker exec/ \
        && line ~ /douyin-web/
    }
    !is_legacy_sparkflow_job($0) { print }
  ' "$current_file" > "$filtered_file"

  if cmp -s "$current_file" "$filtered_file"; then
    rm -f "$current_file" "$filtered_file"
    return
  fi

  backup_dir="$APP_ROOT/backups"
  backup_file="$backup_dir/host-crontab-$(date +%Y%m%d-%H%M%S).bak"
  run_root mkdir -p "$backup_dir"
  run_root cp "$current_file" "$backup_file"
  run_root crontab "$filtered_file"
  rm -f "$current_file" "$filtered_file"
  log "Removed legacy Docker-based SparkFlow host cron jobs; backup: $backup_file"
}

write_default_cron() {
  local cron_file="$APP_ROOT/state/cron/root"
  if [ -s "$cron_file" ]; then
    return
  fi
  if [ "$DEFAULT_SCHEDULE" != "10:00-18:00/20m" ]; then
    echo "DEFAULT_SCHEDULE=$DEFAULT_SCHEDULE will be saved to .env. The initial cron file uses the built-in 10:00-18:00/20m schedule; adjust it from the Web UI after first login." >&2
  fi
  cat > /tmp/douyin-sparkflow-cron <<'CRON'
*/20 10-17 * * * env SPARKFLOW_TRIGGER_LABEL='scheduled send' bash /app/scripts/run_scheduled_task.sh >> /var/log/douyin-sparkflow.log 2>&1
0 18 * * * env SPARKFLOW_TRIGGER_LABEL='scheduled send' bash /app/scripts/run_scheduled_task.sh >> /var/log/douyin-sparkflow.log 2>&1
20 18 * * * env SPARKFLOW_MANUAL_RUN=1 SPARKFLOW_MANUAL_UNSENT_ONLY=1 PYTHONUNBUFFERED=1 SPARKFLOW_TRIGGER_LABEL='unsent fallback' bash /app/scripts/run_scheduled_task.sh >> /var/log/douyin-sparkflow.log 2>&1
CRON
  run_root cp /tmp/douyin-sparkflow-cron "$cron_file"
  rm -f /tmp/douyin-sparkflow-cron
}

prepare_runtime_files() {
  local env_file="$APP_ROOT/.env"
  if [ ! -f "$env_file" ]; then
    run_root cp "$APP_ROOT/.env.example" "$env_file"
  fi

  set_env_value "$env_file" "APP_ROOT" "$APP_ROOT"
  set_env_value "$env_file" "DEFAULT_SCHEDULE" "$DEFAULT_SCHEDULE"

  for key in TZ WEB_BIND_ADDRESS WEB_PORT SPARKFLOW_SESSION_COOKIE_SECURE DOCKER_API_VERSION LOGIN_DESKTOP_BIND_ADDRESS LOGIN_DESKTOP_WEB_PORT LOGIN_DESKTOP_PUBLIC_URL PROXY_BIND_ADDRESS PROXY_HTTP_PORT PROXY_CONTROLLER_PORT PROXY_SUB_URL PROXY_USER_AGENT PLAYWRIGHT_BASE_IMAGE NODE_RUNTIME_IMAGE HTTP_PROXY_BUILD HTTPS_PROXY_BUILD ALL_PROXY_BUILD PIP_INDEX_URL PIP_TRUSTED_HOST; do
    if [ -n "${!key:-}" ]; then
      set_env_value "$env_file" "$key" "${!key}"
    fi
  done

  local current_sub
  current_sub="$(read_env_value "$env_file" PROXY_SUB_URL)"
  if [ -z "$current_sub" ] && [ -t 0 ]; then
    printf 'Proxy subscription URL (optional, hidden; press Enter to skip): '
    read -r -s input_sub || true
    printf '\n'
    if [ -n "${input_sub:-}" ]; then
      set_env_value "$env_file" "PROXY_SUB_URL" "$input_sub"
    fi
  fi

  run_root mkdir -p \
    "$APP_ROOT/proxy" \
    "$APP_ROOT/state/cron" \
    "$APP_ROOT/state/login-profile" \
    "$APP_ROOT/state/browser-profiles" \
    "$APP_ROOT/DouYinSparkFlow/logs"

  if [ ! -f "$APP_ROOT/proxy/config.yaml" ]; then
    run_root cp "$APP_ROOT/proxy/config.example.yaml" "$APP_ROOT/proxy/config.yaml"
  fi
  write_default_cron
}

compose_up() {
  cd "$APP_ROOT"
  log "Refreshing proxy configuration"
  run_root bash "$APP_ROOT/refresh_proxy.sh"
  log "Starting proxy container first"
  run_root env DOCKER_BUILDKIT=1 COMPOSE_DOCKER_CLI_BUILD=1 docker compose up -d proxy
  log "Waiting for proxy to be reachable on 127.0.0.1:7890"
  local tries=0
  while [ "$tries" -lt 30 ]; do
    if curl -fsS -x http://127.0.0.1:7890 https://www.google.com -o /dev/null 2>/dev/null; then
      log "Proxy is reachable"
      break
    fi
    tries=$((tries + 1))
    sleep 2
  done
  if [ "$tries" -ge 30 ]; then
    echo "Proxy did not become reachable on 127.0.0.1:7890; the build may fail downloading external packages." >&2
  fi
  log "Building and starting remaining containers"
  run_root env DOCKER_BUILDKIT=1 COMPOSE_DOCKER_CLI_BUILD=1 \
    HTTP_PROXY_BUILD=http://127.0.0.1:7890 \
    HTTPS_PROXY_BUILD=http://127.0.0.1:7890 \
    ALL_PROXY_BUILD=socks5://127.0.0.1:7890 \
    docker compose up -d --build web login-desktop scheduler
}

print_summary() {
  local env_file="$APP_ROOT/.env"
  local web_port login_port login_bind host_ip
  web_port="$(read_env_value "$env_file" WEB_PORT)"
  login_port="$(read_env_value "$env_file" LOGIN_DESKTOP_WEB_PORT)"
  login_bind="$(read_env_value "$env_file" LOGIN_DESKTOP_BIND_ADDRESS)"
  host_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  host_ip="${host_ip:-127.0.0.1}"
  echo
  echo "Douyin SparkFlow is running."
  echo "Web UI: http://${host_ip}:${web_port:-8787}"
  if [ "${login_bind:-127.0.0.1}" = "127.0.0.1" ]; then
    echo "Login desktop is local-only: http://127.0.0.1:${login_port:-8788}/vnc.html?autoconnect=1&resize=scale&view_only=0"
    echo "For remote access, create an SSH tunnel: ssh -L ${login_port:-8788}:127.0.0.1:${login_port:-8788} <user>@${host_ip}"
  else
    echo "Login desktop: http://${host_ip}:${login_port:-8788}/vnc.html?autoconnect=1&resize=scale&view_only=0"
    echo "Warning: public noVNC access should be protected by a firewall or VPN."
  fi
  echo
  echo "Runtime files preserved outside git: .env, state/, proxy/config.yaml, DouYinSparkFlow/logs/, usersData.json, webui_settings.json."
  echo "Update later with: ACTION=update bash $APP_ROOT/deploy/install-server.sh"
}

main() {
  case "$ACTION" in
    install|update) ;;
    *) echo "ACTION must be install or update" >&2; exit 1 ;;
  esac
  install_base_tools
  ensure_docker
  prepare_repo
  prepare_runtime_files
  remove_legacy_host_cron
  compose_up
  print_summary
}

main "$@"
