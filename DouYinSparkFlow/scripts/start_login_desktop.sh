#!/usr/bin/env bash
set -euo pipefail

export DISPLAY="${DISPLAY:-:99}"
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-/ms-playwright}"
export LOGIN_DESKTOP_API_PORT="${LOGIN_DESKTOP_API_PORT:-18090}"
export LOGIN_DESKTOP_VNC_PORT="${LOGIN_DESKTOP_VNC_PORT:-5901}"
export LOGIN_DESKTOP_WEB_PORT="${LOGIN_DESKTOP_WEB_PORT:-8788}"

mkdir -p /data/login-profile
mkdir -p /app/logs/login_desktop
if [ -n "${LOGIN_DESKTOP_API_TOKEN_FILE:-}" ]; then
  mkdir -p "$(dirname "${LOGIN_DESKTOP_API_TOKEN_FILE}")"
  if [ ! -s "${LOGIN_DESKTOP_API_TOKEN_FILE}" ]; then
    umask 077
    head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' > "${LOGIN_DESKTOP_API_TOKEN_FILE}"
  fi
fi

pkill -f "Xvfb ${DISPLAY}" >/dev/null 2>&1 || true
pkill -f "x11vnc .*${LOGIN_DESKTOP_VNC_PORT}" >/dev/null 2>&1 || true
pkill -f "websockify --web=/usr/share/novnc ${LOGIN_DESKTOP_WEB_PORT}" >/dev/null 2>&1 || true
rm -f "/tmp/.X99-lock"
rm -f "/tmp/.X11-unix/X99"

run_forever() {
  local name="$1"
  shift
  local log_file="/app/logs/login_desktop/${name}.log"
  : > "${log_file}"
  while true; do
    printf '[%s] starting %s\n' "$(date -Is)" "${name}" >> "${log_file}"
    set +e
    "$@" >> "${log_file}" 2>&1
    local status=$?
    set -e
    printf '[%s] %s exited with status %s; restarting in 2s\n' "$(date -Is)" "${name}" "${status}" >> "${log_file}"
    sleep 2
  done
}

Xvfb "${DISPLAY}" -screen 0 1600x1000x24 -ac +extension RANDR > /app/logs/login_desktop/xvfb.log 2>&1 &
for _ in $(seq 1 30); do
  if [ -S /tmp/.X11-unix/X99 ]; then
    break
  fi
  sleep 0.5
done

fluxbox > /app/logs/login_desktop/fluxbox.log 2>&1 &
run_forever x11vnc x11vnc -display "${DISPLAY}" -forever -shared -rfbport "${LOGIN_DESKTOP_VNC_PORT}" -localhost -nopw -nap -wait 50 -defer 50 &
run_forever novnc websockify --web=/usr/share/novnc "${LOGIN_DESKTOP_WEB_PORT}" "127.0.0.1:${LOGIN_DESKTOP_VNC_PORT}" &

exec python /app/login_desktop_server.py
