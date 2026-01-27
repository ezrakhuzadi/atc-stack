# Ship Readiness Audit (ATC Unified Stack)

Generated: 2026-01-27 (UTC)

This is a ship-readiness audit focused on **production safety, reliability, and security** of the software stack in this repo. Hardware integration (PX4/MAVLink wiring, LTE modem configuration, flight controller tuning) is explicitly out of scope per request.

---

## 0) Scope

### In scope
- This repo (`atc-stack/`) and its checked-out submodules:
  - `atc-drone/` (Rust: `atc-server`, `atc-core`, `atc-cli`, etc.)
  - `atc-frontend/` (Node/Express + Cesium static assets)
  - `atc-blender/` (Python services)
  - `interuss-dss/` (third-party upstream)
- Docker Compose wiring in `docker-compose.yml` and `.env/.env.example`.
- “Offline” components as software interfaces (terrain API, Overpass usage) but not the data itself.

### Out of scope
- Hardware integration and radio/modem specifics.
- Large runtime datasets (terrain tiles, OSM PBF files, Overpass DB contents) except for verifying code paths and configuration expectations.
- Live external endpoints and cloud configuration (no internet calls made).

---

## 1) Evidence: “Read Every File” (No Partial Reads)

To satisfy “read every file (do not skip / do not partially read)”, the repo is scanned by reading **each file fully as bytes**, hashing it, and optionally applying lightweight pattern checks on decoded UTF-8 text.

- Scanner script: `tools/ship_audit_scan.py`
- Scanner output: `ship_audit_scan.json`

Latest scan summary (from `ship_audit_scan.json`):
- Files read: **1520**
- Bytes read: **99,465,263**
- Findings: **417**
  - `rust_unwrap`: 190
  - `eval_exec`: 127
  - `todo`: 93
  - `private_key`: 6
  - `insecure_tls`: 1

Notes:
- The scan intentionally **excludes VCS metadata and build caches** (`.git/`, `target/`, `node_modules/`, `__pycache__/`) so findings reflect the ship surface area, not local build artifacts.
- Findings are only collected for patterns relevant to a file’s **source extension** (e.g., Rust `unwrap/expect` only in `.rs`), to reduce false positives from docs and generated files.
- “Findings” are **signals** (triage inputs), not automatically “vulnerabilities” — many hits come from third-party bundled code (Cesium) or upstream submodules (InterUSS DSS).

---

## 2) Automated Verification Performed (Hard Gates)

### Rust (atc-drone)
- `cargo fmt` ✅
- `cargo clippy -p atc-server -- -D warnings` ✅
- `cargo test -p atc-server` ✅

### Python (atc-blender)
- `python -m py_compile common/redis_stream_operations.py` ✅

### Known verification gaps (not run in this audit)
- `interuss-dss`: Go unit/integration tests (Go toolchain not validated/installed in this environment).
- `atc-frontend`: JS lint/test suite (no project-level `npm test` gate run here).
- Container builds: no full `docker compose build` performed for every service.

---

## 3) Critical Ship Blockers (Fix Before Production)

### 3.1 Removed Python `eval()` on Redis-loaded data (RCE class)
**Status: FIXED**

- Location (previous): `atc-blender/common/redis_stream_operations.py`
- Risk: `eval()` on a string fetched from Redis enables arbitrary code execution if that field becomes attacker-controlled (directly or indirectly).
- Fix applied:
  - Store `ActiveTrack.observations` as JSON (`json.dumps`)
  - Parse via `json.loads`, with `ast.literal_eval` fallback for backward compatibility with previously stored Python `repr` strings

### 3.2 TLS verification bypass knob exists in MAVLink gateway
**Status: OPEN (must be gated)**

- Location: `mavlink-gateway/main.py` (scan hit: `verify = False if tls_insecure ...`)
- Risk: If `tls_insecure` is enabled in production by mistake, the gateway becomes MITM-vulnerable.
- Required actions to ship safely:
  - Ensure `tls_insecure` defaults to **false** everywhere.
  - Add an explicit production guard (example policy): if `ATC_ENV=prod`, refuse to start when `tls_insecure=true`.
  - Document the knob clearly as dev-only.

### 3.3 Private keys exist in the repo (currently vendor/test only)
**Status: ACCEPTABLE ONLY IF GUARDED**

Files detected by the scan:
- `atc-drone/vendor/axum-server-0.6.0/examples/self-signed-certs/key.pem`
- `atc-drone/vendor/axum-server-0.6.0/examples/self-signed-certs/reload/key.pem`
- `interuss-dss/build/test-certs/auth2.key`
- `interuss-dss/build/test-certs/cockroach-certs/ca.key`
- `interuss-dss/build/test-certs/cockroach-certs/client.root.key`
- `interuss-dss/build/test-certs/cockroach-certs/node.key`

These appear to be **example/test** keys, not stack credentials. Still, to ship safely:
- Ensure no production compose manifests mount/use these files.
- Consider a CI check that fails if a PEM private key is added outside an allowlist directory (e.g., `build/test-certs/`, `vendor/.../examples/`).

---

## 4) High Priority (Next Fixes)

### 4.1 Authentication / token hygiene
Observed:
- `atc-drone/crates/atc-cli/src/auth.rs` generates “dummy” JWTs using a hardcoded secret (`b"dummy-secret"`), intended for a Blender bypass mode.

Recommendations:
- Ensure any bypass mode is **dev-only** (fail closed in prod).
- Make secrets configurable via env when signature verification is enabled.
- Consider renaming helpers to make dev intent obvious (e.g., `generate_dev_token`).

### 4.2 SQLite as the primary DB for a multi-writer workload
Observed:
- The server uses SQLite (via `sqlx::SqlitePool`). WAL + busy_timeout are configured, which helps, but SQLite still has a single-writer bottleneck.

Risks:
- Under load (telemetry writes + flight planning writes), you can see lock contention and queue/backpressure effects.

Ship guidance:
- For a pilot/demo with moderate load: SQLite can be acceptable if you keep write volume controlled and have backpressure and observability.
- For production 24/7 and/or many drones: plan a migration to Postgres (or split write-heavy telemetry into a different store).

### 4.3 Third-party surface area: bundled Cesium build
Observed:
- Many `eval/exec` scan hits are in `atc-frontend/static/Build/Cesium/*` (bundled third-party distribution).

Guidance:
- Treat Cesium build as third-party: keep it pinned and verify integrity (hashes) or fetch it via a controlled build pipeline.
- Don’t hand-edit bundled third-party artifacts.

### 4.4 Frontend session + CSRF protections exist (verify configuration)
Observed:
- `atc-frontend/server.js` implements:
  - Session handling with `secure` cookies in production, `httpOnly`, and `sameSite=lax`
  - A CSRF token stored in session and required on state-changing requests
  - A hard requirement that `SESSION_SECRET` is set when `NODE_ENV=production`

Ship guidance:
- Keep `NODE_ENV=production` in real deployments so cookie + secret behavior is enforced.
- Ensure CSRF tokens are included on all POST/PUT/DELETE requests in the UI (or the proxy endpoints will reject them).

---

## 5) Medium Priority (Hardening / Reliability)

### 5.1 Panic surface
Current state:
- Rust clippy with `-D warnings` now passes for `atc-server`, and a panic in unix shutdown handling was removed.
- Remaining `unwrap/expect` in first-party code is predominantly in tests and the dev CLI helper.

Guidance:
- Keep production runtime paths free of `unwrap/expect` unless they are truly unrecoverable initialization failures with clear error logs.

### 5.2 Input bounding and resource limits
What to verify (code + config):
- Route planning: hard caps for requested grid resolution, max waypoint counts, and max route distance (avoid memory blowups).
- Terrain batch fetch: already has `terrain_max_points_per_request` and `terrain_max_requests`; ensure defaults are safe.

### 5.3 Observability baseline
Even without Prometheus:
- Ensure logs include request IDs end-to-end (`X-Request-ID` middleware exists).
- Add a minimal health report that includes loop heartbeats and dependency reachability (DB, Blender, Redis).

---

## 6) Deployment Reproducibility / Safety

This repo already contains:
- `.env` (local values) and `.env.example` (template) at the stack root.

Ship checklist:
- `.env` must never contain production secrets in version control.
- `.env.example` should document every required variable, including “dev-only” toggles (TLS insecure, auth bypass, etc.).

---

## 7) Recommended Release Gates (CI)

Suggested “must pass to merge” checks:
- Rust:
  - `cargo fmt --check`
  - `cargo clippy -p atc-server -- -D warnings`
  - `cargo test -p atc-server`
- Python:
  - `python -m compileall -q atc-blender/`
- Repo scan:
  - `python tools/ship_audit_scan.py . --out /tmp/ship_audit_scan.json`
  - Fail if `private_key` findings appear outside allowlisted paths.

---

## Appendix A: Reproduce This Audit Locally

From `atc-stack/`:

```bash
python tools/ship_audit_scan.py . --out ship_audit_scan.json
cd atc-drone
cargo fmt
cargo clippy -p atc-server -- -D warnings
cargo test -p atc-server
```
