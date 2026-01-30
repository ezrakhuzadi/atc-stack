#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: tools/overpass_rebuild_and_cutover.sh <command>

Commands:
  build                 Download OSM PBF (if missing) and build an Overpass DB into the staging directory.
  cutover               Swap staging -> final, update .env, then restart overpass + atc-drone.
  build-and-cutover      Run build, then cutover.
  status                Print build/cutover status (files + current .env settings).

Environment (defaults shown):
  ATC_DATA_ROOT=/mnt/atc-data
  ATC_OSM_DIR=$ATC_DATA_ROOT/osm
  ATC_BUILD_OSM_REGION=us
  ATC_BUILD_OSM_PBF=us-latest.osm.pbf
  ATC_OVERPASS_FINAL_DB_DIR=$ATC_DATA_ROOT/overpass
  ATC_OVERPASS_STAGING_DB_DIR=$ATC_DATA_ROOT/overpass-next
  STACK_ENV_FILE=<repo>/.env
  OVERPASS_IMAGE=wiktorn/overpass-api:latest
EOF
}

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

DATA_ROOT="${ATC_DATA_ROOT:-/mnt/atc-data}"
OSM_DIR="${ATC_OSM_DIR:-$DATA_ROOT/osm}"
BUILD_REGION="${ATC_BUILD_OSM_REGION:-us}"
BUILD_PBF="${ATC_BUILD_OSM_PBF:-us-latest.osm.pbf}"
FINAL_DB_DIR="${ATC_OVERPASS_FINAL_DB_DIR:-$DATA_ROOT/overpass}"
STAGING_DB_DIR="${ATC_OVERPASS_STAGING_DB_DIR:-$DATA_ROOT/overpass-next}"
STACK_ENV_FILE="${STACK_ENV_FILE:-$REPO_ROOT/.env}"
OVERPASS_IMAGE="${OVERPASS_IMAGE:-wiktorn/overpass-api:latest}"

cmd="${1:-}"
if [[ -z "$cmd" ]]; then
  usage
  exit 2
fi

status() {
  echo "[status] repo: $REPO_ROOT"
  echo "[status] data_root: $DATA_ROOT"
  echo "[status] osm_dir: $OSM_DIR"
  echo "[status] build_region: $BUILD_REGION"
  echo "[status] build_pbf: $BUILD_PBF"
  echo "[status] staging_db_dir: $STAGING_DB_DIR"
  echo "[status] final_db_dir: $FINAL_DB_DIR"
  echo "[status] stack_env_file: $STACK_ENV_FILE"

  if [[ -f "$OSM_DIR/$BUILD_PBF" ]]; then
    echo "[status] osm pbf: present ($(du -h "$OSM_DIR/$BUILD_PBF" | awk '{print $1}'))"
  else
    echo "[status] osm pbf: missing ($OSM_DIR/$BUILD_PBF)"
  fi

  if [[ -f "$STAGING_DB_DIR/init_done" ]]; then
    echo "[status] staging db: ready (found $STAGING_DB_DIR/init_done)"
  else
    echo "[status] staging db: not ready (missing $STAGING_DB_DIR/init_done)"
  fi

  if [[ -f "$FINAL_DB_DIR/init_done" ]]; then
    echo "[status] final db: ready (found $FINAL_DB_DIR/init_done)"
  else
    echo "[status] final db: not ready (missing $FINAL_DB_DIR/init_done)"
  fi

  if [[ -f "$STACK_ENV_FILE" ]]; then
    local active_pbf active_db
    active_pbf="$(grep -E '^ATC_OSM_PBF=' "$STACK_ENV_FILE" | tail -n 1 | cut -d= -f2- || true)"
    active_db="$(grep -E '^ATC_OVERPASS_DB_DIR=' "$STACK_ENV_FILE" | tail -n 1 | cut -d= -f2- || true)"
    echo "[status] current .env ATC_OSM_PBF=${active_pbf:-<unset>}"
    echo "[status] current .env ATC_OVERPASS_DB_DIR=${active_db:-<unset>}"
  else
    echo "[status] current .env: missing ($STACK_ENV_FILE)"
  fi
}

build_db() {
  mkdir -p "$OSM_DIR" "$STAGING_DB_DIR"

  if [[ ! -f "$OSM_DIR/$BUILD_PBF" ]]; then
    echo "[build] downloading OSM PBF (this is large): $BUILD_PBF"
    python3 "$REPO_ROOT/tools/fetch_offline_data.py" --data-root "$DATA_ROOT" osm --region "$BUILD_REGION"
  else
    echo "[build] OSM PBF already present: $OSM_DIR/$BUILD_PBF"
  fi

  if [[ -f "$STAGING_DB_DIR/init_done" ]]; then
    echo "[build] staging Overpass DB already initialized: $STAGING_DB_DIR"
    return
  fi

  echo "[build] building Overpass DB in staging dir: $STAGING_DB_DIR (this can take hours)"
  ATC_DATA_ROOT="$DATA_ROOT" \
  ATC_OSM_DIR="$OSM_DIR" \
  ATC_OSM_PBF="$BUILD_PBF" \
  ATC_OVERPASS_DB_DIR="$STAGING_DB_DIR" \
  OVERPASS_IMAGE="$OVERPASS_IMAGE" \
    "$REPO_ROOT/tools/init_overpass_db.sh"
}

cutover() {
  if [[ ! -f "$STAGING_DB_DIR/init_done" ]]; then
    echo "[cutover] staging Overpass DB not ready yet (missing $STAGING_DB_DIR/init_done)"
    exit 1
  fi

  if [[ ! -f "$STACK_ENV_FILE" ]]; then
    echo "[cutover] missing env file: $STACK_ENV_FILE"
    exit 1
  fi

  local ts backup_file
  ts="$(date +%Y%m%d_%H%M%S)"
  backup_file="${STACK_ENV_FILE}.bak.${ts}"
  cp "$STACK_ENV_FILE" "$backup_file"
  echo "[cutover] backed up .env to: $backup_file"

  # Stop Overpass before swapping directories so bind mounts never point at a moving target.
  echo "[cutover] stopping compose overpass service for directory swap..."
  (cd "$REPO_ROOT" && docker compose stop overpass)

  if [[ -d "$FINAL_DB_DIR" ]]; then
    local final_backup
    final_backup="${FINAL_DB_DIR}.bak.${ts}"
    echo "[cutover] backing up existing final db dir: $FINAL_DB_DIR -> $final_backup"
    mv "$FINAL_DB_DIR" "$final_backup"
  fi

  echo "[cutover] swapping DB dir: $STAGING_DB_DIR -> $FINAL_DB_DIR"
  mv "$STAGING_DB_DIR" "$FINAL_DB_DIR"

  # Update .env (keep production always pointed at FINAL_DB_DIR).
  STACK_ENV_FILE="$STACK_ENV_FILE" BUILD_PBF="$BUILD_PBF" FINAL_DB_DIR="$FINAL_DB_DIR" python3 - <<'PY'
import os
import pathlib

env_path = pathlib.Path(os.environ["STACK_ENV_FILE"])
text = env_path.read_text(encoding="utf-8", errors="ignore").splitlines(True)

updates = {
    "ATC_OSM_PBF": os.environ["BUILD_PBF"],
    "ATC_OVERPASS_DB_DIR": os.environ["FINAL_DB_DIR"],
}

out = []
seen = set()
for line in text:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        out.append(line)
        continue
    key = stripped.split("=", 1)[0].strip()
    if key in updates:
        if key in seen:
            continue
        out.append(f"{key}={updates[key]}\n")
        seen.add(key)
        continue
    out.append(line)

for key, value in updates.items():
    if key not in seen:
        out.append(f"{key}={value}\n")

env_path.write_text("".join(out), encoding="utf-8")
PY

  echo "[cutover] updated $STACK_ENV_FILE:"
  echo "         ATC_OSM_PBF=$BUILD_PBF"
  echo "         ATC_OVERPASS_DB_DIR=$FINAL_DB_DIR"

  echo "[cutover] restarting compose overpass service..."
  (cd "$REPO_ROOT" && docker compose up -d --force-recreate overpass)
  echo "[cutover] restarting backend to flush obstacle cache..."
  (cd "$REPO_ROOT" && docker compose up -d --force-recreate atc-drone)
  echo "[cutover] done"
}

case "$cmd" in
  status)
    status
    ;;
  build)
    build_db
    ;;
  cutover)
    cutover
    ;;
  build-and-cutover)
    build_db
    cutover
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    echo "unknown command: $cmd"
    usage
    exit 2
    ;;
esac

