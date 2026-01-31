#!/usr/bin/env bash
set -euo pipefail

# Runs the deterministic safety regression harness:
# - Brings the stack up (optionally build)
# - Optionally resets server state for determinism
# - Runs the curl-based smoke scenario + (optional) atc-cli golden demo

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

export E2E_RESET="${E2E_RESET:-1}"
export E2E_RUN_CLI_DEMO="${E2E_RUN_CLI_DEMO:-1}"

exec "$ROOT_DIR/tools/e2e_demo.sh"

