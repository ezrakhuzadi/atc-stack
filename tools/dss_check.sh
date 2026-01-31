#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

NETWORK="${DSS_DOCKER_NETWORK:-}"
CORE_CONTAINER="${DSS_CORE_CONTAINER:-local-dss-core}"
OAUTH_CONTAINER="${DSS_OAUTH_CONTAINER:-local-dss-dummy-oauth}"

MONITORING_IMAGE="${DSS_MONITORING_IMAGE:-interuss/monitoring:v0.5.0}"

RESULT_DIR="${DSS_CHECK_RESULT_DIR:-${ROOT_DIR}/data/dss-check}"
RESULT_FILE="${DSS_CHECK_RESULT_FILE:-${RESULT_DIR}/prober-junit.xml}"

log() { printf "[dss_check] %s\n" "$*"; }

discover_network() {
  if [[ -n "$NETWORK" ]]; then
    return 0
  fi

  NETWORK="$(
    docker container inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{println $k}}{{end}}' "$CORE_CONTAINER" 2>/dev/null \
      | head -n 1 || true
  )"

  if [[ -z "$NETWORK" ]]; then
    log "Unable to determine docker network from $CORE_CONTAINER."
    log "Set DSS_DOCKER_NETWORK explicitly and retry."
    return 1
  fi
}

require_running() {
  local name="$1"
  local status
  status="$(docker container inspect -f '{{.State.Status}}' "$name" 2>/dev/null || true)"
  if [[ "$status" != "running" ]]; then
    log "container not running: $name"
    return 1
  fi
  return 0
}

require_network() {
  if ! docker network inspect "$NETWORK" >/dev/null 2>&1; then
    log "docker network missing: $NETWORK"
    log "bring up the stack once (docker compose creates the network), or create it:"
    log "  docker network create $NETWORK"
    return 1
  fi
}

main() {
  if ! require_running "$CORE_CONTAINER" || ! require_running "$OAUTH_CONTAINER"; then
    log "Prerequisite: DSS sandbox must be running."
    log "Start it with:"
    log "  cd \"$ROOT_DIR\" && docker compose --profile dss up -d"
    exit 1
  fi

  discover_network
  require_network

  mkdir -p "$RESULT_DIR"
  : >"$RESULT_FILE"

  log "monitoring image: $MONITORING_IMAGE"
  log "network: $NETWORK"
  log "core: $CORE_CONTAINER"
  log "oauth: $OAUTH_CONTAINER"
  log "junit: $RESULT_FILE"

  docker pull "$MONITORING_IMAGE" >/dev/null

  docker run --rm \
    --network "$NETWORK" \
    -v "$RESULT_FILE:/app/test_result" \
    -w /app/monitoring/prober \
    "$MONITORING_IMAGE" \
    pytest \
    "${@:-.}" \
    -rsx \
    --junitxml=/app/test_result \
    --dss-endpoint "http://$CORE_CONTAINER:8082" \
    --rid-auth "DummyOAuth(http://$OAUTH_CONTAINER:8085/token,sub=fake_uss)" \
    --rid-v2-auth "DummyOAuth(http://$OAUTH_CONTAINER:8085/token,sub=fake_uss)" \
    --scd-auth1 "DummyOAuth(http://$OAUTH_CONTAINER:8085/token,sub=fake_uss)" \
    --scd-auth2 "DummyOAuth(http://$OAUTH_CONTAINER:8085/token,sub=fake_uss2)" \
    --scd-api-version 1.0.0

  log "OK: prober passed"
}

main "$@"
