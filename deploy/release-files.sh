#!/usr/bin/env bash
# Release exactly the files that changed between two commits to a running
# server install, with backup, verification and one-command rollback.
#
#   deploy/release-files.sh plan     <base> <target>          # list files, no changes
#   deploy/release-files.sh pack     <base> <target> <out.tgz> # build bundle + manifest
#   deploy/release-files.sh apply    <bundle.tgz> [app_dir]    # ON THE SERVER
#   deploy/release-files.sh rollback [backup_dir] [app_dir]    # ON THE SERVER
#   deploy/release-files.sh status   [app_dir]                 # ON THE SERVER
#
# "apply" refuses to run when:
#   * a send/patrol browser run is in progress (scheduler busy);
#   * a live file differs from <base> (someone edited the server by hand) —
#     compared after CRLF->LF normalisation, so line endings alone do not block;
#   * the bundle checksum does not match its manifest.
# It backs up every touched file, installs the new ones atomically, verifies
# each git blob hash, converts the touched files to LF, restarts ONLY the web
# container, checks /login, records the release in RELEASE, and rolls back
# automatically if any step after the first write fails.
set -euo pipefail

APP_DIR_DEFAULT=/opt/douyin-sparkflow/DouYinSparkFlow
BACKUP_ROOT=${BACKUP_ROOT:-/opt/douyin-sparkflow/backups}
WEB_CONTAINER=${WEB_CONTAINER:-douyin-web}
SCHEDULER_CONTAINER=${SCHEDULER_CONTAINER:-douyin-scheduler}
SUBDIR=DouYinSparkFlow

die() { echo "ERROR: $*" >&2; exit 1; }

blob() {  # git blob hash of a file with CRLF normalised to LF
  python3 - "$1" <<'PY'
import hashlib, sys
b = open(sys.argv[1], 'rb').read().replace(b'\r\n', b'\n')
print(hashlib.sha1(b'blob %d\0' % len(b) + b).hexdigest())
PY
}

changed_files() {
  git diff --name-only --diff-filter=ACMR "$1" "$2" -- "$SUBDIR" | sed "s#^$SUBDIR/##"
}

deleted_files() {
  git diff --name-only --diff-filter=D "$1" "$2" -- "$SUBDIR" | sed "s#^$SUBDIR/##"
}

cmd_plan() {
  local base=$1 target=$2
  echo "release $(git rev-parse --short "$base") -> $(git rev-parse --short "$target")"
  changed_files "$base" "$target" | sed 's/^/  update /'
  deleted_files "$base" "$target" | sed 's/^/  delete (not automated, do by hand) /'
}

cmd_pack() {
  local base=$1 target=$2 out=$3 tmp
  [ -n "$(deleted_files "$base" "$target")" ] && echo "note: deleted files are not removed by apply"
  tmp=$(mktemp -d)
  mkdir -p "$tmp/files"
  {
    echo "base $(git rev-parse "$base")"
    echo "target $(git rev-parse "$target")"
    while read -r f; do
      [ -n "$f" ] || continue
      mkdir -p "$tmp/files/$(dirname "$f")"
      git show "$target:$SUBDIR/$f" > "$tmp/files/$f"
      base_blob=$(git rev-parse -q --verify "$base:$SUBDIR/$f" 2>/dev/null || echo NEW)
      echo "file $f $base_blob $(git rev-parse "$target:$SUBDIR/$f")"
    done < <(changed_files "$base" "$target")
  } > "$tmp/MANIFEST"
  (cd "$tmp/files" && find . -type f | sort | xargs sha256sum) > "$tmp/SHA256SUMS"
  tar -C "$tmp" -czf "$out" MANIFEST SHA256SUMS files
  rm -rf "$tmp"
  echo "bundle $out sha256 $(sha256sum "$out" | cut -c1-64)"
  echo "files $(grep -c '^file ' <(tar -xzOf "$out" MANIFEST))"
}

scheduler_busy() {
  docker exec "$SCHEDULER_CONTAINER" sh -c 'ps -eo args' 2>/dev/null | grep -E 'main.py|chrom' | grep -v grep >/dev/null
}

restore_from() {
  local backup=$1 app=$2
  while read -r f; do
    [ -n "$f" ] || continue
    install -m 644 -D "$backup/$f" "$app/$f.rollback" && mv -f "$app/$f.rollback" "$app/$f"
  done < <(cd "$backup" && find . -type f ! -name 'RELEASE*' ! -name 'NEW_FILES' | sed 's#^\./##')
  if [ -f "$backup/NEW_FILES" ]; then
    while read -r f; do [ -n "$f" ] && rm -f "$app/$f"; done < "$backup/NEW_FILES"
  fi
  [ -f "$backup/RELEASE.before" ] && cp -f "$backup/RELEASE.before" "$app/RELEASE" || true
}

restart_web_and_check() {
  docker restart "$WEB_CONTAINER" >/dev/null
  for _ in $(seq 1 20); do
    sleep 1
    code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8787/login || true)
    [ "$code" = 200 ] && return 0
  done
  return 1
}

cmd_apply() {
  local bundle=$1 app=${2:-$APP_DIR_DEFAULT} work backup target base
  [ -d "$app" ] || die "app dir $app not found"
  work=$(mktemp -d)
  # shellcheck disable=SC2064  # expand now: $work is local and gone at EXIT
  trap "rm -rf '$work'" EXIT
  tar -C "$work" -xzf "$bundle"
  (cd "$work/files" && sha256sum -c --quiet ../SHA256SUMS) || die "bundle checksum mismatch"
  base=$(awk '$1=="base"{print $2}' "$work/MANIFEST")
  target=$(awk '$1=="target"{print $2}' "$work/MANIFEST")

  scheduler_busy && die "a send or patrol is running; try again later"

  local drift=0
  while read -r _ f base_blob _new; do
    if [ "$base_blob" = NEW ]; then
      [ -e "$app/$f" ] && { echo "drift: $f exists but is new in this release"; drift=1; }
    else
      [ -e "$app/$f" ] || { echo "drift: $f missing on server"; drift=1; continue; }
      [ "$(blob "$app/$f")" = "$base_blob" ] || { echo "drift: $f differs from ${base:0:7}"; drift=1; }
    fi
  done < <(grep '^file ' "$work/MANIFEST")
  [ $drift = 0 ] || die "server files differ from the release base; refusing to overwrite"

  backup="$BACKUP_ROOT/release-${target:0:7}-$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$backup"
  [ -f "$app/RELEASE" ] && cp "$app/RELEASE" "$backup/RELEASE.before"
  while read -r _ f base_blob _new; do
    if [ "$base_blob" = NEW ]; then echo "$f" >> "$backup/NEW_FILES"
    else install -m 644 -D "$app/$f" "$backup/$f"; fi
  done < <(grep '^file ' "$work/MANIFEST")
  cp "$work/MANIFEST" "$backup/RELEASE.manifest"
  echo "$backup" > "$BACKUP_ROOT/LAST_RELEASE_BACKUP"

  rollback_now() { echo "!! $1 — rolling back"; restore_from "$backup" "$app"; restart_web_and_check || true; die "rolled back to $backup"; }

  while read -r _ f _base new_blob; do
    install -m 644 -D "$work/files/$f" "$app/$f.new" || rollback_now "write $f failed"
    mv -f "$app/$f.new" "$app/$f"
    [ "$(git hash-object "$app/$f" 2>/dev/null || blob "$app/$f")" = "$new_blob" ] || rollback_now "hash mismatch on $f"
  done < <(grep '^file ' "$work/MANIFEST")

  printf 'commit %s\nbase %s\nreleased %s\nbackup %s\n' "$target" "$base" "$(date -Is)" "$backup" > "$app/RELEASE"
  restart_web_and_check || rollback_now "web did not come back on /login"
  echo "released ${target:0:7} ($(grep -c '^file ' "$work/MANIFEST") files); backup $backup"
}

cmd_rollback() {
  local backup=${1:-$(cat "$BACKUP_ROOT/LAST_RELEASE_BACKUP")} app=${2:-$APP_DIR_DEFAULT}
  [ -d "$backup" ] || die "backup $backup not found"
  scheduler_busy && die "a send or patrol is running; try again later"
  restore_from "$backup" "$app"
  restart_web_and_check || die "web did not come back on /login after rollback"
  echo "rolled back using $backup"
}

cmd_status() {
  local app=${1:-$APP_DIR_DEFAULT}
  [ -f "$app/RELEASE" ] && cat "$app/RELEASE" || echo "no RELEASE file (installed before release-files.sh)"
}

case "${1:-}" in
  plan) shift; cmd_plan "$@" ;;
  pack) shift; cmd_pack "$@" ;;
  apply) shift; cmd_apply "$@" ;;
  rollback) shift; cmd_rollback "$@" ;;
  status) shift; cmd_status "$@" ;;
  *) sed -n '2,12p' "$0"; exit 2 ;;
esac
