#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-$ROOT_DIR/docker-compose.yml}"

ATC_SERVER_PORT="${ATC_SERVER_PORT:-3000}"
ATC_FRONTEND_PORT="${ATC_FRONTEND_PORT:-5050}"
ATC_BASE_URL="${ATC_BASE_URL:-http://localhost:${ATC_SERVER_PORT}}"
FRONTEND_BASE_URL="${FRONTEND_BASE_URL:-http://localhost:${ATC_FRONTEND_PORT}}"

ATC_REGISTRATION_TOKEN="${ATC_REGISTRATION_TOKEN:-change-me-registration-token}"
ATC_ADMIN_TOKEN="${ATC_ADMIN_TOKEN:-change-me-admin}"

log() { printf "[e2e] %s\n" "$*"; }

SECONDS=0

wait_http() {
  local url="$1"
  local tries="${2:-60}"
  local sleep_s="${3:-2}"
  for ((i=1; i<=tries; i++)); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep "$sleep_s"
  done
  return 1
}

json_field() {
  local field="$1"
  python - <<PY
import json,sys
payload=json.load(sys.stdin)
value=payload.get("$field")
print(value if isinstance(value,str) else "")
PY
}

post_json() {
  local url="$1"
  local data="$2"
  curl -fsS -H 'content-type: application/json' -d "$data" "$url"
}

post_json_register() {
  local url="$1"
  local data="$2"
  curl -fsS -H 'content-type: application/json' -H "X-Registration-Token: $ATC_REGISTRATION_TOKEN" -d "$data" "$url"
}

post_json_auth() {
  local url="$1"
  local token="$2"
  local data="$3"
  curl -fsS -H 'content-type: application/json' -H "authorization: Bearer $token" -d "$data" "$url"
}

post_json_admin() {
  local url="$1"
  local data="$2"
  curl -fsS -H 'content-type: application/json' -H "authorization: Bearer $ATC_ADMIN_TOKEN" -d "$data" "$url"
}

get_auth() {
  local url="$1"
  local token="$2"
  curl -fsS -H "authorization: Bearer $token" "$url"
}

log "compose: $COMPOSE_FILE"
log "atc: $ATC_BASE_URL"
log "frontend: $FRONTEND_BASE_URL"

E2E_BUILD="${E2E_BUILD:-0}"
if [[ "$E2E_BUILD" == "1" ]]; then
  log "bringing stack up (build + recreate as needed)"
  docker compose -f "$COMPOSE_FILE" up -d --build >/dev/null
else
  log "bringing stack up (no-build, no-recreate)"
  set +e
  out="$(docker compose -f "$COMPOSE_FILE" up -d --no-build --no-recreate 2>&1)"
  status="$?"
  set -e
  if [[ "$status" -ne 0 ]]; then
    printf "%s\n" "$out" >&2
    log "compose failed (missing images?). Re-run with: E2E_BUILD=1 $0"
    exit "$status"
  fi
fi

ready_start="$SECONDS"
log "waiting for ATC /ready..."
wait_http "$ATC_BASE_URL/ready" 60 2 || { log "ATC not ready: $ATC_BASE_URL/ready"; exit 1; }
log "ATC ready in $((SECONDS - ready_start))s"

E2E_RESET="${E2E_RESET:-0}"
if [[ "$E2E_RESET" == "1" ]]; then
  log "resetting ATC state..."
  post_json_admin "$ATC_BASE_URL/v1/admin/reset" '{"confirm":"RESET","require_idle":false}' >/dev/null
fi

ui_start="$SECONDS"
log "waiting for frontend /login..."
wait_http "$FRONTEND_BASE_URL/login" 60 2 || { log "frontend not reachable: $FRONTEND_BASE_URL/login"; exit 1; }
log "frontend reachable in $((SECONDS - ui_start))s"

register_start="$SECONDS"
log "registering 2 drones"
register_a="$(post_json_register "$ATC_BASE_URL/v1/drones/register" "{\"drone_id\":\"E2E_DRONE_A\",\"owner_id\":\"e2e\"}" \
  | python -c 'import json,sys; print(json.load(sys.stdin).get("session_token",""))' \
  )"
register_b="$(post_json_register "$ATC_BASE_URL/v1/drones/register" "{\"drone_id\":\"E2E_DRONE_B\",\"owner_id\":\"e2e\"}" \
  | python -c 'import json,sys; print(json.load(sys.stdin).get("session_token",""))' \
  )"
log "registration completed in $((SECONDS - register_start))s"

if [[ -z "$register_a" || -z "$register_b" ]]; then
  log "registration failed (missing session_token). Did you set ATC_REGISTRATION_TOKEN correctly?"
  log "expected header X-Registration-Token; current token: ${ATC_REGISTRATION_TOKEN:0:8}..."
  exit 1
fi

ts="$(python - <<'PY'
from datetime import datetime,timezone
print(datetime.now(timezone.utc).isoformat().replace("+00:00","Z"))
PY
)"

telemetry_start="$SECONDS"
log "sending telemetry (create immediate separation violation)"
post_json_auth "$ATC_BASE_URL/v1/telemetry" "$register_a" "{\"drone_id\":\"E2E_DRONE_A\",\"owner_id\":\"e2e\",\"lat\":33.6846,\"lon\":-117.8265,\"altitude_m\":90.0,\"heading_deg\":90.0,\"speed_mps\":0.0,\"timestamp\":\"$ts\"}" >/dev/null
post_json_auth "$ATC_BASE_URL/v1/telemetry" "$register_b" "{\"drone_id\":\"E2E_DRONE_B\",\"owner_id\":\"e2e\",\"lat\":33.6846,\"lon\":-117.8265,\"altitude_m\":90.0,\"heading_deg\":270.0,\"speed_mps\":0.0,\"timestamp\":\"$ts\"}" >/dev/null
log "telemetry submitted in $((SECONDS - telemetry_start))s"

conflict_start="$SECONDS"
log "waiting for conflict to appear..."
for ((i=1; i<=30; i++)); do
  conflicts="$(get_auth "$ATC_BASE_URL/v1/conflicts" "$ATC_ADMIN_TOKEN" || true)"
  count="$(python - "$conflicts" <<'PY'
import json,sys
try:
  data=json.loads(sys.argv[1])
except Exception:
  data=[]
print(len(data) if isinstance(data,list) else 0)
PY
)"
  if [[ "$count" -gt 0 ]]; then
    log "conflict detected ($count)"
    break
  fi
  sleep 1
done

if [[ "${count:-0}" -le 0 ]]; then
  log "no conflicts detected after 30s"
  exit 1
fi

log "conflict detected after $((SECONDS - conflict_start))s"

geofence_start="$SECONDS"
log "creating geofence + validating check-route"
post_json_admin "$ATC_BASE_URL/v1/geofences" '{"name":"E2E Zone","geofence_type":"no_fly_zone","polygon":[[33.0,-117.0],[33.0,-116.9],[33.1,-116.9],[33.1,-117.0],[33.0,-117.0]],"lower_altitude_m":0.0,"upper_altitude_m":120.0}' >/dev/null
route_check="$(post_json_admin "$ATC_BASE_URL/v1/geofences/check-route" '{"waypoints":[{"lat":33.05,"lon":-117.05,"altitude_m":50.0},{"lat":33.05,"lon":-116.95,"altitude_m":50.0}]}' )"
route_conflicts="$(python - "$route_check" <<'PY'
import json,sys
payload=json.loads(sys.argv[1])
print("true" if payload.get("conflicts") is True else "false")
PY
)"
if [[ "$route_conflicts" != "true" ]]; then
  log "expected geofence route conflict=true, got: $route_check"
  exit 1
fi

log "geofence check completed in $((SECONDS - geofence_start))s"

E2E_RUN_CLI_DEMO="${E2E_RUN_CLI_DEMO:-0}"
if [[ "$E2E_RUN_CLI_DEMO" == "1" ]]; then
  if command -v cargo >/dev/null 2>&1; then
    log "running atc-cli demo_scenario (cargo)..."
    (
      cd "$ROOT_DIR/atc-drone"
      cargo run -p atc-cli --bin demo_scenario -- \
        --url "$ATC_BASE_URL" \
        --owner e2e \
        --reset \
        --registration-token "$ATC_REGISTRATION_TOKEN" \
        --admin-token "$ATC_ADMIN_TOKEN"
    )
  else
    log "skipping atc-cli demo_scenario (cargo not found)"
  fi
fi

log "OK (total ${SECONDS}s)"
