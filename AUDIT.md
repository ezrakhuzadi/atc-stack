# ATC Stack — Consolidated Level 6 Engineering Audit (Canonical)

Last updated: 2026-01-30 (local)

This is the **single canonical** audit document for this workspace. Previous audit artifacts were consolidated into this file to eliminate “audit sprawl”.

Hardware integration (autopilot tuning, LTE modem install, field ops) is out of scope here; this audit covers **software** and **deployment posture**.

---

## Table of Contents

- [0) Executive Summary (Ship Decision + Status)](#0-executive-summary-ship-decision--status)
- [1) System Inventory (What’s In This Repo)](#1-system-inventory-whats-in-this-repo)
- [2) Architecture & Trust Boundaries](#2-architecture--trust-boundaries)
- [3) Threat Model (Practical)](#3-threat-model-practical)
- [4) Audit Methodology & Evidence](#4-audit-methodology--evidence)
- [5) Findings (By Subsystem)](#5-findings-by-subsystem)
  - [5.1 `atc-drone` (Rust backend + core algorithms)](#51-atc-drone-rust-backend--core-algorithms)
  - [5.2 `atc-frontend` (Node UI + proxy)](#52-atc-frontend-node-ui--proxy)
  - [5.3 `atc-blender` + `interuss-dss` (Django + DSS sandbox)](#53-atc-blender--interuss-dss-django--dss-sandbox)
  - [5.4 `terrain-api` + Overpass + offline datasets](#54-terrain-api--overpass--offline-datasets)
  - [5.5 `mavlink-gateway` (Autopilot bridge, LTE reality)](#55-mavlink-gateway-autopilot-bridge-lte-reality)
  - [5.6 Safety Assurance & Validation Coverage (Missing)](#56-safety-assurance--validation-coverage-missing)
  - [5.7 External Audit Claims Not Merged (Resolved / Not Found)](#57-external-audit-claims-not-merged-resolved--not-found)
- [8) Launch Gates & Roadmap (P0–P3)](#8-launch-gates--roadmap-p0p3)
  - [8.1 Launch Gates (definition of “damn near ready to ship”)](#81-launch-gates-definition-of-damn-near-ready-to-ship)
  - [8.2 Correction Roadmap (P0–P3) — With Status](#82-correction-roadmap-p0p3--with-status)
- [9) Next Step](#9-next-step)

## 0) Executive Summary (Ship Decision + Status)

**Ship decision:** **Not ship-ready for production internet exposure** until remaining **P0 items** are closed (see section **8**) and the timed audit completes across all subsystems.

**Grades (excluding unfinished MAVLink hardware/autopilot integration)**
- **Senior project (engineering + scope):** A- (~92%) *today*, mostly because the end-to-end system exists and is unusually cohesive.
- **Production “near-launch” readiness:** ~55% *today* (fails P0 safety/security/ops gates).
- **If all P0 + P1 gates are fixed with real verification evidence:** A / ~95% as a senior project; ~85–90% as a launch-ready product.

Verified fixed in code (so far):
- `atc-drone`: geofence CRUD + RID view are **admin-authenticated** in `atc-drone/crates/atc-server/src/api/routes.rs`.
- `mavlink-gateway`: refuses `ATC_TLS_INSECURE=1` when `ATC_ENV=prod|production` (re-verify again during the `mavlink-gateway` timed audit).

Verified from code review (spot checks; still requires full timed audit sign-off):
- `atc-blender`: no remaining `eval()` usage in the codebase for Redis-loaded state (search + review).
- `terrain-api`: missing/nodata elevations return `None`/`null` (see `terrain-api/app.py`).

**Timed audit progress (ground truth)**

Run:
```bash
python tools/timed_audit_clock.py status
```

As of the last pause before this edit:
- `atc-drone`: **DONE**
- `atc-frontend`: **IN-PROGRESS**
- `atc-blender`: **IN-PROGRESS**
- `interuss-dss`: **IN-PROGRESS**

---

## 1) System Inventory (What’s In This Repo)

This repo is a reproducible unified Docker Compose stack with git submodules:

- `atc-drone/` (Rust): ATC server, core algorithms, CLI, SDK
- `atc-frontend/` (Node/Express): UI + reverse proxy + WS proxy  
  - `atc-frontend/models-src/` now contains the drone model assets (merged from the prior `drone-models/` folder)
- `atc-blender/` (Python/Django): “Flight Blender” adapter + DSS workflows
- `interuss-dss/` (upstream): local DSS sandbox (CockroachDB + DSS core)
- `terrain-api/` (Python): local DEM sampler service
- `mock-uss/` (Python): demo USS for local sandbox flows
- `mavlink-gateway/` (Python): optional autopilot → ATC telemetry + command bridge

Supporting stack files:
- `docker-compose.yml`, `docker-compose.dev.yml`, `.env.example`, `DATA.md`, `README.md`
- Audit tooling: `tools/timed_audit_clock.py`, `tools/ship_audit_scan.py`

Note: earlier duplicate sibling clones outside `Project/atc-stack/` were removed; **this folder is the source of truth**.

---

## 2) Architecture & Trust Boundaries

**Primary data flows**

1) **Drone telemetry → ATC**
- Autopilot → `mavlink-gateway` → `atc-drone` `POST /v1/telemetry`

2) **ATC state → UI**
- Browser → `atc-frontend` → proxied to `atc-drone` HTTP + WebSocket endpoints

3) **ATC ↔ Flight Blender ↔ DSS**
- `atc-drone` background loops call `atc-blender` APIs (and indirectly DSS services via Blender)
- `atc-blender` interacts with `interuss-dss` for RID/USS flows

4) **Terrain / obstacles**
- `atc-drone` route planning / safety checks call:
  - `terrain-api` for elevation
  - `overpass` for OSM building/obstacle lookups (local)

**Trust boundaries (high level)**
- **Public browser traffic** terminates at `atc-frontend` (session cookies)
- **Drone traffic** terminates at `atc-drone` (bearer tokens)
- **Admin actions** are protected by an admin token (server-side injected by frontend proxy)
- **Blender/DSS** are internal services but become part of the trusted computing base

---

## 3) Threat Model (Practical)

Assume:
- Attackers can reach the UI and any public endpoints you expose.
- Attackers can create accounts if signup is enabled, or steal session cookies.
- Drones are intermittently connected (LTE flaps), and replay/stale command risk is real.
- Any “demo-mode bypass” flags accidentally enabled in production are catastrophic.

Primary “blast radius” events:
- Unauthenticated mutation of **geofences**, **RID viewport**, or other global state.
- Credential/secrets leakage from compose/env, logs, or frontend assets.
- DoS via unbounded route planning inputs, RID queries, or DB lock storms.
- Command safety failures due to clock skew, stale commands, altitude reference mismatch.

---

## 4) Audit Methodology & Evidence

This audit uses three layers:

1) **Full-file scan (no partial reads)**
- Script reads every file as bytes and records `sha256`, size, and some pattern hits:
  - `python tools/ship_audit_scan.py . --out /tmp/ship_audit_scan.json`
- This is *evidence of coverage*, not proof of semantic understanding of third-party bundles.

2) **Timed deep-read per subsystem (90 minutes each)**
- Enforced by `tools/timed_audit_clock.py` to survive interruptions and avoid “cheating”.
- Rules:
  - We only count time explicitly “ticked” as active work.
  - We pause before any unrelated tasks.
  - We may interleave subsystems when needed, but **no subsystem is considered “DONE”** until `elapsed >= 01:30:00`.

3) **Targeted verification**
- Run/lint/tests where feasible without disrupting the running stack.

---

## 5) Findings (By Subsystem)

### 5.0 Strengths & Completeness (validated via external audits)

- **Coherent Rust workspace boundaries**: `atc-core` (domain logic), `atc-server` (API/loops), `atc-sdk`, `atc-cli`, plus integration client crates are separated cleanly.
- **API hygiene**: versioned `/v1` routes and a machine‑readable OpenAPI spec (`openapi.yaml`).
- **Operational loop design**: explicit background loops with health/readiness checks (not just spawn-and-forget).
- **Command system**: commands expire, are acked, and are streamed over WS; SDK supports polling/WS.
- **Route planner depth**: A* planner with obstacle/terrain integration plus post‑processing (smoothing / corridor tooling).
- **Control Center security baseline**: session-based auth + CSRF (server-side), CSP with nonces (scoped), WS proxy.

### 5.1 `atc-drone` (Rust backend + core algorithms)

**What it is**
- The system of record for drone state, flight plans, geofences, conflict detection, command queueing, and integration loops.

**Key interfaces**
- HTTP: `/v1/*` (telemetry ingest, query state, planning endpoints)
- WS: live updates proxied through `atc-frontend`
- External deps: SQLite (today), `atc-blender`, `terrain-api`, Overpass

**Already fixed**
- Admin-only enforcement for:
  - `POST/PUT/DELETE /v1/geofences...`
  - `POST /v1/rid/view`
  - Implemented in `atc-drone/crates/atc-server/src/api/routes.rs`
- SQLite command expiry filtering now parses RFC3339 correctly (`datetime(expires_at) ...`) and has a regression test:
  - `atc-drone/crates/atc-server/src/persistence/commands.rs`
- Geometry correctness fixes (no sampling shortcuts in safety checks):
  - `atc-drone/crates/atc-core/src/models.rs` `Geofence::intersects_segment` now uses exact segment–polygon intersection (local ENU) plus altitude band overlap (no sampling).
  - `atc-drone/crates/atc-core/src/spatial.rs` `segment_to_segment_distance` now detects true crossings (distance=0 on intersection).
- Conflict prediction now uses continuous-time CPA (no 1s miss-between-samples risk):
  - `atc-drone/crates/atc-core/src/conflict.rs` `predict_conflict` now computes the conflict window analytically and minimizes distance within that window (constant-velocity model).
  - Added regression unit test `detects_near_miss_between_whole_seconds`.
- Altitude semantics: AGL limits enforced when terrain is required:
  - `atc-drone/crates/atc-server/src/api/altitude_validation.rs` enforces SafetyRules min/max altitude against AGL (`altitude_amsl - terrain_elevation`) when `ATC_TERRAIN_REQUIRE=1` (fails closed if terrain fetch fails).
  - `atc-drone/crates/atc-server/src/route_planner.rs` uses `state.rules().max_altitude_m` for the route-engine `faa_limit_agl` (removes the hardcoded `500.0`).
- Telemetry time semantics (no “normalize to now”):
  - `atc-drone/crates/atc-server/src/api/routes.rs` now validates the client-provided timestamp as-is (rejects too-old/too-far-future) and uses server receipt time for `last_update`.
- Operational read endpoints now require admin auth (no public live ops data):
  - `atc-drone/crates/atc-server/src/api/routes.rs` moved `/v1/drones`, `/v1/traffic`, `/v1/conflicts`, `/v1/daa`, `/v1/flights`, and `/v1/ws` behind `require_admin`.
- DoS hardening for large/expensive requests:
  - `atc-drone/crates/atc-server/src/main.rs` applies a global body limit (`DefaultBodyLimit`) for JSON endpoints.
  - `/v1/geofences/check-route` is now admin-authenticated, rate-limited, and validates waypoint counts + numeric ranges.

**Open risks / work**
- `ConflictSeverity::Info` exists but is not emitted by the predictor (Warning/Critical only).
- WS token can be passed via query param (leak‑prone; prefer Authorization header/cookie).
- WS broadcast uses a bounded channel; lagged subscribers drop messages (no replay).
- Command IDs are truncated to 8 chars of UUID (collision risk at scale).
- Blender OAuth client uses `reqwest::Client::new()` (no timeouts); can hang critical loops.
- Compliance HTTP client falls back to `Client::new()` on builder failure (loses timeout).
- SDK client uses `Client::new()` (no timeouts) and does not enforce HTTPS in production.
- Compliance models (population/obstacles/weather/battery) are heuristic, not validated safety models.
- DB contention handling: some loops can create retry/log storms when DB is unhealthy.
- Strategic scheduling uses a single DB lock (no distributed coordination); horizontal scale needs a leader/lock service.
- Drone token lifecycle: rotation/recovery flow is incomplete (gateway “re-register” on 401/403 collides with 409 behavior).
- DoS limits: ensure route planning caps are enforced in both API and algorithm layers.
- Async blocking & global locks: review `store.rs` locking + CPU-heavy conflict work (timed audit is still in-progress here).

**Detailed findings (write-as-we-go; do not delete)**

F-DRONE-001 — **P0 / Safety (FIXED)**: Conflict prediction could miss the true CPA (1s discrete sampling)
- Where: `atc-drone/crates/atc-core/src/conflict.rs` (`ConflictDetector::predict_conflict`)
- Why it matters: two aircraft can pass closest approach between samples; the system can fail to emit a Warning/Critical that should exist.
- Fix (implemented):
  - `predict_conflict` now uses a continuous-time constant-velocity model:
    - computes the time window where both horizontal and vertical thresholds are simultaneously satisfied (solving the horizontal quadratic + vertical linear interval), and
    - finds the closest approach within that window analytically (minimizing 3D distance in the interval).
  - Added unit test `detects_near_miss_between_whole_seconds` to prevent regressions where conflicts only occur between integer seconds.
- Verify:
  - `cargo test -p atc-core` includes the regression test and must pass.
  - Recommended follow-up: add randomized/property tests and integration coverage in `atc-drone/crates/atc-server/tests/conflict_test.rs`.

F-DRONE-002 — **P0 / Safety (FIXED)**: Geofence intersection could be missed (sampling-based)
- Where: `atc-drone/crates/atc-core/src/models.rs` (`Geofence::intersects_segment`), `atc-drone/crates/atc-core/src/route_engine.rs` (`geofence_blocks_segment`)
- Why it matters: long legs clamp to max 200 samples; narrow geofences can be crossed between samples → false “clear route”.
- Fix (implemented):
  - `Geofence::intersects_segment` now:
    - clips the segment to the parametric interval where altitude is inside the geofence altitude band, and
    - checks endpoint-in-polygon OR segment–polygon-edge intersection using a local ENU projection (no sampling).
  - Added unit tests in `atc-drone/crates/atc-core/src/models.rs`:
    - `geofence_intersects_segment_detects_crossing_between_samples`
    - `geofence_intersects_segment_requires_horizontal_and_altitude_overlap_at_same_time`
- Verify:
  - `cargo test -p atc-core` includes the new tests and must pass.
  - Recommended follow-up: property-based fuzzing for segment-edge intersection and additional boundary-touching cases.

F-DRONE-003 — **P0 / Safety (FIXED)**: Strategic deconfliction could accept intersecting routes (bad segment distance)
- Where: `atc-drone/crates/atc-core/src/spatial.rs` (`segment_to_segment_distance`)
- Why it matters: current implementation checks endpoint-to-segment distances only; crossing segments (an “X”) can return non-zero distance → false “no conflict”.
- Fix (implemented):
  - `segment_to_segment_distance` now:
    - projects segments to local ENU meters,
    - detects true segment intersection (distance=0), and
    - falls back to endpoint-to-segment distances otherwise.
  - Added unit test in `atc-drone/crates/atc-core/src/spatial.rs`:
    - `segment_to_segment_distance_detects_crossing_segments`
- Verify:
  - `cargo test -p atc-core` includes the new test and must pass.
  - Recommended follow-up: add tests for colinear overlap, parallel offset, and endpoint touch.

F-DRONE-004 — **P0 / Safety + Product correctness (FIXED)**: Altitude reference / altitude limits were inconsistent (AGL vs AMSL)
- Where:
  - `atc-drone/crates/atc-server/src/config.rs` (logs “AGL unsupported; using AMSL”)
  - `atc-drone/crates/atc-server/src/altitude.rs` (only WGS84↔AMSL)
  - Rules/validation: `atc-drone/crates/atc-core/src/rules.rs`, `atc-drone/crates/atc-server/src/api/flights.rs`, `atc-drone/crates/atc-server/src/api/routes.rs`
  - Planner ceiling: `atc-drone/crates/atc-server/src/route_planner.rs` (`RouteEngineConfig::faa_limit_agl`)
- Why it matters: “400 ft limit” is AGL; comparing directly to AMSL/HAE is wrong away from sea level. You can incorrectly allow or reject plans.
- Fix (implemented):
  - Altitudes are still normalized to AMSL internally (WGS84↔AMSL conversion), but SafetyRules min/max altitude are enforced as **AGL** in production:
    - `atc-drone/crates/atc-server/src/api/altitude_validation.rs` validates AGL (`altitude_amsl - terrain_elevation`) when `ATC_TERRAIN_REQUIRE=1` and fails closed on terrain fetch failure.
  - The A* route engine’s AGL ceiling now uses the configured rules:
    - `atc-drone/crates/atc-server/src/route_planner.rs` sets `faa_limit_agl = state.rules().max_altitude_m` (removes hardcoded `500.0`).
  - Note: `ATC_ALTITUDE_REFERENCE` remains an *input* altitude reference selector (WGS84 vs AMSL). AGL is enforced as a limit using terrain, not as an input reference.
- Verify:
  - With `ATC_ENV=production` (or `ATC_TERRAIN_REQUIRE=1`), submit a route with a waypoint at `altitude_amsl = terrain + max_agl + 10` and confirm a blocking `altitude_agl` violation appears.
  - Recommended follow-up: end-to-end tests with a mocked terrain provider over non-zero terrain elevations.

F-DRONE-005 — **P0 / Safety (FIXED)**: Telemetry timestamps were normalized to “now” before validation (could mask stale/bad telemetry)
- Where: `atc-drone/crates/atc-server/src/api/routes.rs` (`receive_telemetry` timestamp validation + receipt-time stamping)
- Why it matters: stale/future telemetry can appear “fresh”, defeating timeout/lost logic and weakening conflict prediction time semantics.
- Fix (implemented):
  - Removed `normalize_telemetry_timestamp`.
  - `receive_telemetry` now:
    - validates the client-provided timestamp against `telemetry_max_future_s` / `telemetry_max_age_s`, and
    - overwrites the stored timestamp with server receipt time **after** validation so `last_update` reflects server time (does not trust client clocks).
- Verify:
  - Unit tests: `cargo test -p atc-server` must pass.
  - Manual: send telemetry with a timestamp older than `ATC_TELEMETRY_MAX_AGE_S` and confirm it returns 400 and does not update drone `last_update`.

F-DRONE-006 — **P0 / Security + Privacy (FIXED)**: Operational “read” endpoints were public
- Where: `atc-drone/crates/atc-server/src/api/routes.rs`
- Why it matters: live locations/conflicts are sensitive operational data; public access enables surveillance and targeting.
- Fix (implemented):
  - `/v1/drones`, `/v1/traffic`, `/v1/conflicts`, `/v1/conformance`, `/v1/daa`, `/v1/flights`, and `/v1/ws` are now behind `auth::require_admin`.
  - `atc-frontend` now attaches `ATC_ADMIN_TOKEN` on those reads and on the WS proxy upstream.
- Verify:
  - `curl /v1/drones` (no Authorization header) returns 401/403.
  - Control Center still loads fleet/traffic/conflicts through its proxy with `ATC_ADMIN_TOKEN` set.

F-DRONE-007 — **P0 / Security (FIXED)**: Missing global request body size limits (DoS surface)
- Where: `atc-drone/crates/atc-server/src/main.rs` (app builder), multiple JSON POST endpoints
- Why it matters: large JSON bodies can exhaust memory/CPU before per-field validation kicks in.
- Fix (implemented):
  - Added a global Axum body limit using `DefaultBodyLimit::max(1 * 1024 * 1024)` so oversized payloads fail fast with 413.
- Verify:
  - Manual: send a payload larger than 1MiB to any JSON endpoint and confirm `413 Payload Too Large`.
  - Recommended follow-up: add an API regression test that asserts 413.

F-DRONE-008 — **P1 / Security (FIXED)**: WebSocket token accepted via query param (leak-prone)
- Where:
  - `atc-drone/crates/atc-server/src/api/ws.rs` (no query token; Authorization only)
  - `atc-drone/crates/atc-server/src/api/commands.rs` (no query token; Authorization only)
- Why it matters: query strings leak via logs/proxies/history; harder to secure.
- Fix (implemented):
  - Server now accepts tokens only via `Authorization: Bearer …` for WebSocket upgrades.
  - Legacy query-string `?token=...` is no longer parsed/accepted.
- Verify:
  - Manual: WS upgrade with only `?token=...` is rejected; upgrade with `Authorization: Bearer ...` succeeds.

F-DRONE-009 — **P1 / Security/Scale (FIXED)**: Drone token lookup is O(N) and tokens are stored in plaintext
- Where:
  - `atc-drone/crates/atc-server/src/state/store.rs` (token auth + lookup)
  - `atc-drone/crates/atc-server/migrations/002_drone_token_hash.sql` (DB column + index)
  - `atc-drone/crates/atc-server/src/persistence/drone_tokens.rs` (hash upsert)
- Why it matters: O(N) token lookup becomes a hot path (command fetch/WS auth), and plaintext tokens increase blast radius if the DB leaks.
- Fix (implemented):
  - Added `drone_tokens.session_token_hash` and indexed it.
  - Server now stores **only token hashes** (SHA-256 hex) in memory + DB, and writes `session_token=''` to wipe plaintext.
  - Added an O(1) reverse index `token_hash -> drone_id` and switched `drone_id_for_token()` to use it.
  - On startup, if legacy rows still have a plaintext `session_token`, the server backfills `session_token_hash` and wipes `session_token`.
- Verify:
  - `cargo test -p atc-server` (includes token hash + backfill regression tests).
  - DB spot check: `SELECT session_token, session_token_hash FROM drone_tokens;` should show empty `session_token` and populated `session_token_hash`.

F-DRONE-010 — **P1 / Correctness (FIXED)**: Command IDs are truncated (collision risk)
- Where: `atc-drone/crates/atc-server/src/api/commands.rs` (`CMD-` + first 8 chars of UUID)
- Why it matters: collisions can cause incorrect ack/routing or DB primary key failures at scale.
- Fix: issue-command IDs now use the full UUID (`CMD-<uuid>`) rather than a truncated prefix.
- Verify: manual (or add a unit test) that issued command IDs are full UUIDs and remain unique.

F-DRONE-011 — **P1 / Reliability (FIXED)**: Flight plan state transitions are not persisted
- Where: `atc-drone/crates/atc-server/src/loops/mission_loop.rs` (mission execution status updates)
- Why it matters: after restart, flight status can revert; operator UI/analytics become inconsistent; unsafe automation can happen if stale plans re-activate.
- Fix (implemented):
  - Mission loop now persists `status` and `arrival_time` transitions using `state.add_flight_plan(...)` (DB upsert + in-memory update).
  - Mission loop no longer holds DashMap locks across `.await` calls (it processes cloned plans and writes back only on transitions).
  - Added regression tests asserting Approved→Active and Active→Completed transitions are persisted to the DB.
- Verify:
  - `cargo test -p atc-server` (includes `loops::mission_loop` persistence tests).

F-DRONE-012 — **P2 / UX + Debuggability**: “ATC-drone down but geofences still visible” is expected with Blender TTL (needs explicit explanation in UI)
- Where: `atc-drone/crates/atc-server/src/loops/geofence_sync_loop.rs` syncs geofences to Blender with `GEOFENCE_TTL_HOURS=6`
- Why it matters: operators can misinterpret stale geofences as current ATC state.
- Fix: surface source + freshness in the UI (local vs Blender vs conflict geofence) and add “data stale” banners when `atc-drone` is unreachable.
- Verify: kill `atc-drone` container and confirm UI clearly indicates stale geofence source/freshness.

F-DRONE-013 — **P0 / Security (FIXED)**: Fail closed on placeholder/shared-secret defaults in production
- Where:
  - `atc-drone/crates/atc-server/src/main.rs` rejects:
    - `ATC_ADMIN_TOKEN=change-me-admin`
    - `ATC_REGISTRATION_TOKEN=change-me-registration-token`
    - `ATC_WS_TOKEN=change-me-ws-token`
  - Also fails fast when `ATC_REQUIRE_WS_TOKEN` is enabled but `ATC_WS_TOKEN` is missing (prevents “WS locked by empty expected token” misconfig).
- Why it matters: “change-me-*” tokens are widely known; leaving them in production makes auth effectively useless.
- Fix: implemented fail-closed checks for the known placeholder defaults for admin, registration, and WS tokens.
- Verify:
  - Boot server with `ATC_ENV=production` and placeholder tokens → process exits with a clear error.
  - Boot server with `ATC_ENV=production`, `ATC_REQUIRE_WS_TOKEN=true`, but no `ATC_WS_TOKEN` → process exits with a clear error.

F-DRONE-014 — **P1 / Reliability + Safety (FIXED)**: Disabling command ACK timeouts can create “never-clearing” command queues
- Where:
  - `atc-drone/crates/atc-server/src/main.rs` (production config validation)
  - `atc-drone/crates/atc-server/src/state/store.rs` (`enqueue_command` now enforces expiry + queue caps)
  - `atc-drone/crates/atc-server/src/persistence/commands.rs` (`delete_stale_commands` still no-ops when `ack_timeout_secs<=0`, but commands now always have an `expires_at`)
- Why it matters: unacked commands can accumulate indefinitely (especially commands with `expires_at=NULL`), causing stuck drones and memory/DB growth.
- Fix (implemented):
  - Server now rejects `ATC_COMMAND_ACK_TIMEOUT_SECS <= 0` when `ATC_ENV` is not development.
  - `enqueue_command` now assigns a default `expires_at` when missing (and adds an ACK-timeout buffer for HOLD commands) so commands are never “immortal”.
  - Added a per-drone pending-command queue cap via `ATC_MAX_PENDING_COMMANDS_PER_DRONE` (default 100); new commands are rejected once the cap is hit.
  - Added regression tests verifying default expiry assignment and queue-cap enforcement.
- Verify:
  - `cargo test -p atc-server` (includes `state::store` command expiry/cap tests).

F-DRONE-015 — **P1 / Security + DoS (FIXED)**: `/v1/geofences/check-route` was public and accepted unbounded waypoint arrays
- Where: `atc-drone/crates/atc-server/src/api/routes.rs`, `atc-drone/crates/atc-server/src/api/geofences.rs` (`check_route`)
- Why it matters: attacker can submit huge waypoint lists → O(segments × geofences) CPU burn.
- Fix (implemented):
  - Moved `/v1/geofences/check-route` behind `require_admin` and the “expensive” rate limiter.
  - Added strict request validation in `check_route`:
    - `2 <= waypoints.len() <= route_planner_max_waypoints`
    - lat/lon finite and in range; altitude finite.
- Verify:
  - Unit test `create_geofence_and_check_route` (in `atc-drone/crates/atc-server/src/api/tests.rs`) now uses admin auth.
  - Recommended follow-up: add a negative test for `waypoints.len() > route_planner_max_waypoints` returning 400.

F-DRONE-016 — **P1 / Reliability (FIXED)**: OAuth token fetch has no HTTP timeout (can hang critical loops)
- Where: `atc-drone/crates/atc-server/src/blender_auth.rs` (`BlenderAuthManager::new` uses `reqwest::Client::new()`, `fetch_oauth_token` uses `.send().await` without timeout)
- Why it matters: a hung token request can stall Blender-dependent loops indefinitely; supervision restarts loops but doesn’t fix a permanently hung network call.
- Fix: `BlenderAuthManager` now builds a reqwest client with `connect_timeout` + request timeout so token fetch cannot hang indefinitely.
- Verify: simulate a blackholed token URL; process should not hang and readiness should reflect degraded external integration.

F-DRONE-017 — **P1 / Reliability + Security (FIXED)**: SDK client uses no network timeouts and allows plain HTTP
- Where: `atc-drone/crates/atc-sdk/src/client.rs` (`AtcClient::new` uses `reqwest::Client::new()`; `base_url` is unconstrained)
- Why it matters: client calls can hang forever; allowing `http://` risks token exposure if deployed outside local dev.
- Fix:
  - `AtcClient` now builds reqwest with `connect_timeout` + request timeout.
  - WebSocket connect is wrapped in a timeout.
  - `AtcClient::new(...)` now rejects `http://` unless the caller explicitly opts in via `AtcClient::new_insecure_http(...)`.
- Verify:
  - Unit tests cover URL scheme validation and insecure opt-in.
  - Manual: connect to a blackholed endpoint should time out (HTTP + WS) rather than hang indefinitely.

F-DRONE-018 — **P0 / Safety (FIXED)**: Conflict loop can issue a reroute that was *not* validated against obstacles/geofences
- Where: `atc-drone/crates/atc-server/src/loops/conflict_loop.rs` (now: `match plan_airborne_route(...).await { Some => REROUTE, None => HOLD }`)
- Why it matters: if route planning fails (terrain/obstacle fetch failure, algorithm failure, etc.), the fallback still issues a REROUTE based on simple offsets that can violate geofences/obstacles and create secondary conflicts.
- Fix (implemented): fail closed on planner failure:
  - If route planning fails, default to `HOLD` (failsafe) rather than generating an unvalidated “simple avoidance” reroute.
- Verify:
  - Manual code review confirms the planner result is now matched (no fallback reroute).
  - Recommended follow-up: forced-failure integration test (break terrain/overpass) should not issue an unsafe reroute; instead it should HOLD and raise an advisory explaining degraded planning.

F-DRONE-019 — **P1 / Safety (FIXED)**: Conformance “exit geofence” reroute is not validated
- Where:
  - `atc-drone/crates/atc-server/src/loops/conformance_loop.rs` (plans a validated exit route or HOLD)
  - `atc-drone/crates/atc-server/src/route_planner.rs` (supports ignoring the breached geofence during exit planning)
- Why it matters: the “exit point” is computed geometrically and may route through other restricted areas or fail to actually clear the geofence depending on geometry/projection.
- Fix (implemented):
  - Generate the exit point as a goal, then use `plan_airborne_route` to compute a validated short path to the exit.
  - Fail closed: if planning fails, issue a HOLD instead of an unvalidated reroute.
  - Ignore the breached geofence during exit planning so a drone already inside can still plan its way out while avoiding *other* active geofences.
- Verify:
  - Manual: simulate a geofence breach near another geofence; conformance recovery should HOLD unless a valid exit route is found.
  - Recommended follow-up: add a concave geofence + nearby secondary geofence scenario and assert we never command an unsafe “exit point only” reroute.

F-DRONE-020 — **P0 / Safety + Compliance (FIXED)**: Route planner uses a single authoritative AGL ceiling (no hardcoded 500m)
- Where: `atc-drone/crates/atc-server/src/route_planner.rs` (`RouteEngineConfig.faa_limit_agl` is derived from `state.rules().max_altitude_m`)
- Why it matters: `RouteEngineConfig` default is `121.0` (Part 107 ~400ft) but conflict/airborne planning can exceed that, creating illegal/unsafe guidance.
- Fix (implemented):
  - `plan_airborne_route` sets `faa_limit_agl: state.rules().max_altitude_m.max(0.0)` (removes the hardcoded `500.0`).
  - Ceiling is now controlled by `ATC_RULES_MAX_ALTITUDE_M` (AGL semantics when `ATC_TERRAIN_REQUIRE=1`).
- Verify:
  - `cargo test -p atc-core` includes AGL ceiling tests in the route engine.
  - Recommended: add an integration test that plans a route with a waypoint above the ceiling and asserts it is rejected/adjusted under production terrain semantics.

F-DRONE-021 — **P0 / Security (FIXED)**: `owner_id` is effectively unauthenticated and can be spoofed via telemetry
- Where: `atc-drone/crates/atc-server/src/state/store.rs` (`update_telemetry` writes `drone_owners` from `telemetry.owner_id` and `DroneState::update` accepts it)
- Why it matters: any drone with a valid session token can change its `owner_id`, undermining owner-based filtering and any future RBAC. It can also cause cross-tenant data exposure if owner_id is ever used as an authorization boundary.
- Fix (implemented):
  - `update_telemetry` now treats `owner_id` as control-plane identity and overwrites `telemetry.owner_id` from server state (`drone_owners` cache or the existing `DroneState`) before updating/broadcasting.
  - Telemetry can no longer set or change ownership.
- Verify:
  - Unit test `telemetry_cannot_spoof_owner_id` (in `atc-drone/crates/atc-server/src/api/tests.rs`) ensures telemetry with a spoofed `owner_id` does not change the stored owner.

F-DRONE-022 — **P1 / Safety (FIXED)**: Geofence validation is incomplete (can accept malformed polygons)
- Where:
  - `atc-drone/crates/atc-core/src/models.rs` (`Geofence::validate`)
  - `atc-drone/crates/atc-server/src/state/store.rs` (invalid geofences are skipped on load/ingest)
- Why it matters: malformed polygons (NaNs/out-of-range coords, self-intersections, duplicate vertices, zero area) can cause incorrect containment/intersection results, weakening a core safety control.
- Fix (implemented):
  - Enforce strict geofence polygon validation:
    - finite + range checks for every vertex
    - explicit closure (`first == last`)
    - no duplicate vertices / no zero-length edges
    - non-zero area
    - no self-intersections
  - Require finite altitudes.
  - Skip invalid geofences loaded from DB or received as external geofences (logged + dropped).
- Verify:
  - Unit tests added in `atc-drone/crates/atc-core/src/models.rs` for invalid polygons and a known-good polygon.

F-DRONE-023 — **P0 / Safety (FIXED)**: Airborne conflict reroute planning can silently drop terrain constraints
- Where: `atc-drone/crates/atc-server/src/route_planner.rs` (`plan_airborne_route` now returns `None` when `terrain_require=1` and terrain fetch fails / returns no data)
- Why it matters: when terrain fetch fails, reroute planning proceeds with `terrain=None` (effectively assuming 0m terrain), even if `ATC_TERRAIN_REQUIRE=1` in production. This can generate unsafe/illegal guidance.
- Fix (implemented): honor `config.terrain_require` in `plan_airborne_route`: if terrain is required and fetch fails (or returns no data), return `None` so the caller falls back to HOLD.
- Verify:
  - Manual code review confirms terrain fetch errors no longer downgrade to `terrain=None` when `terrain_require=1`.
  - Recommended follow-up: simulate a down terrain-api and confirm conflict reroute becomes HOLD + advisory, not a reroute that ignores terrain.

F-DRONE-024 — **P0 / Safety + Reliability (FIXED)**: SQLite command expiry filtering was broken (RFC3339 vs `datetime('now')`)
- Status: FIXED in `atc-drone/crates/atc-server/src/persistence/commands.rs` (uses `datetime(expires_at)`), covered by `expired_commands_are_not_loaded_or_retained`.
- Where (original bug): `atc-drone/crates/atc-server/src/persistence/commands.rs`
  - `load_all_pending_commands`: `expires_at > datetime('now')`
  - `delete_expired_commands`: `expires_at < datetime('now')`
- Why it mattered: `expires_at` is stored as RFC3339 (contains a `'T'`), but SQLite’s `datetime('now')` uses a space. Lexicographic comparison made many expired commands look “not expired” until the *date* changed. After restarts (or if the conflict loop is unhealthy and doesn’t purge in-memory queues), drones could poll and receive stale commands that should have been expired.
- Fix: parse the stored timestamp in SQL (`datetime(expires_at) > datetime('now')` / `<`), or migrate `issued_at`/`expires_at` to INTEGER epoch seconds and compare numerically (preferred long-term).
- Verify: regression test inserts an expired command *on the same day* and asserts it is not returned by `load_all_pending_commands` and is deleted by `delete_expired_commands`.

F-DRONE-025 — **P0 / Security + Privacy (FIXED)**: WebSocket can silently become public in production if `ATC_WS_TOKEN` is unset
- Where:
  - `atc-drone/crates/atc-server/src/config.rs` (`require_ws_token` now defaults to `true` outside development)
  - `atc-drone/crates/atc-server/src/api/ws.rs` (auth check depends on config above)
- Why it matters: if `ATC_ENV != development` but `ATC_WS_TOKEN` is accidentally omitted, `/v1/ws` becomes effectively unauthenticated and can leak live operational state (positions, conflicts, etc.) to any network client that can reach the server.
- Fix (implemented): fail closed outside development:
  - Default `ATC_REQUIRE_WS_TOKEN=1` when `ATC_ENV != development`
  - `/v1/ws` accepts either `ATC_WS_TOKEN` *or* `ATC_ADMIN_TOKEN` (so the control center proxy can still connect even if no separate WS token is configured)
  - `ATC_WS_TOKEN=change-me-ws-token` is still rejected in non-dev.
- Verify:
  - With `ATC_ENV=production` and no `ATC_WS_TOKEN`, WS handshake without `Authorization` should be rejected (401).
  - With `ATC_ENV=production`, WS handshake with `Authorization: Bearer $ATC_ADMIN_TOKEN` should succeed.
  - Follow-up hardening (P1): remove query-param token support entirely to reduce leakage via logs.

F-DRONE-026 — **P1 / Safety + Reliability (FIXED)**: Active HOLD state is in-memory only and not reconstructed after restart
- Where:
  - `atc-drone/crates/atc-server/src/state/store.rs` (`load_from_database` now rebuilds `active_holds`)
  - `atc-drone/crates/atc-server/src/persistence/commands.rs` (loads acknowledged HOLD/RESUME commands with `acked_at`)
- Why it matters: after a restart, the system forgets which drones are still under an acknowledged HOLD, so it can:
  - show incorrect status (Holding → Active) after the next telemetry update
  - issue new commands while a HOLD is still supposed to be in effect (increasing thrash/instability)
- Fix (implemented):
  - On startup, ATC now replays acknowledged HOLD/RESUME effects (in ack order) to reconstruct `active_holds` using `hold_until = acked_at + duration_secs` (capped by `expires_at`).
  - Added a regression test that issues HOLD → ack → reload state from the same DB → verifies the HOLD remains active.
- Verify:
  - `cargo test -p atc-server` (includes `state::store::tests::load_from_database_reconstructs_active_hold_state`).

F-DRONE-027 — **P0 / Safety + Reliability (FIXED)**: “last_update” uses untrusted telemetry timestamps (time semantics are wrong system-wide)
- Where:
  - Model: `atc-drone/crates/atc-core/src/models.rs` (`DroneState::update(..., received_at)` / `from_telemetry(..., received_at)`)
  - Ingest: `atc-drone/crates/atc-server/src/state/store.rs` (`update_telemetry` uses `received_at = Utc::now()` and passes it into the DroneState update)
  - Timeouts: `atc-drone/crates/atc-server/src/state/store.rs` (`check_timeouts` uses `now - drone.last_update`)
  - External sync: `atc-drone/crates/atc-server/src/loops/blender_sync_loop.rs` (uses `last_update` to decide what changed)
- Why it matters: a drone clock can drift, be wrong, or be attacker-controlled. Using `telemetry.timestamp` as the authoritative “freshness” clock can:
  - mark a live drone LOST (if it sends old timestamps),
  - keep a dead drone “fresh” (if it sends future-ish timestamps and you normalize),
  - suppress Blender sync (if timestamps don’t change),
  - generally break every “is this current?” decision.
- Fix: separate timestamps:
  - `received_at` (server receipt time) — authoritative for timeouts/health/sync throttling
  - `source_timestamp` (drone-provided) — informational; used for drift detection and maybe kinematics if trustworthy
  - Stop mutating timestamps (ties to F-DRONE-005), and add explicit clock-drift handling.
- Verify:
  - Unit test `stale_telemetry_timestamp_does_not_trigger_timeout` (in `atc-drone/crates/atc-server/src/api/tests.rs`) ensures stale telemetry timestamps do not immediately mark a drone LOST.
  - Recommended follow-up: surface clock drift in status (requires tracking `source_timestamp` separately).

F-DRONE-028 — **P1 / Safety (FIXED)**: External RID tracks “fail open” on timestamp parse (stale tracks can look fresh)
- Where: `atc-drone/crates/atc-server/src/loops/rid_sync_loop.rs` (RID observations without a valid timestamp are dropped)
- Why it matters: if Blender/DSS sends malformed timestamps (or schema shifts), tracks are assigned `last_update = now`, so they can persist and be fed into conflict detection as “fresh” even when they’re not. This can create false conflicts and unnecessary evasive commands.
- Fix (implemented):
  - RID observations now require a parseable timestamp; otherwise they are dropped (fail closed) instead of being assigned `Utc::now()`.
  - Added a regression unit test that invalid timestamps are rejected.
- Verify:
  - Unit test `normalize_observation_rejects_invalid_timestamp` (in `atc-drone/crates/atc-server/src/loops/rid_sync_loop.rs`) passes.

F-DRONE-029 — **P1 / Product correctness + Safety (FIXED)**: Blender flight declarations are imported without ATC validation and with weak input checks
- Where:
  - `atc-drone/crates/atc-server/src/loops/flight_declaration_sync_loop.rs` (import validation + timestamp parsing)
  - `atc-drone/crates/atc-server/src/api/flights.rs` (external plans excluded from scheduling by default)
  - `atc-drone/crates/atc-core/src/models.rs` (`FlightPlanMetadata.external_source`)
- Why it matters:
  - Imported waypoints were only checked for “finite” (not lat/lon range); malformed data can pollute DB and UI.
  - Imported plans were persisted via `state.add_flight_plan`, which can cause scheduling side-effects (local strategic scheduling may be blocked by untrusted external plans).
  - Departure/created timestamps could fall back to `Utc::now()` if parsing failed, making old/invalid declarations appear current.
- Fix (implemented):
  - Blender declaration imports now validate waypoint count, lat/lon ranges, altitude finiteness, and apply the existing `route_planner_max_waypoints` cap before persisting.
  - Timestamp parsing is more robust (supports common non-RFC3339 formats) and invalid/missing start times cause the declaration to be skipped (no silent “now” fallback).
  - Imported plans are tagged as external via `metadata.external_source="blender"`.
  - External plans are excluded from local strategic scheduling/conflict checks by default; enable with `ATC_INCLUDE_EXTERNAL_PLANS_IN_SCHEDULING=1`.
- Verify:
  - `cargo test -p atc-server` (covers invalid coords/time in declaration import and scheduling include/exclude behavior).

F-DRONE-030 — **P1 / Security + Reliability (FIXED)**: Flight plan and compliance inputs lack hard size caps (CPU/DoS risk)
- Where:
  - `atc-drone/crates/atc-server/src/api/flights.rs` (flight plan validation now caps route points)
  - `atc-drone/crates/atc-server/src/api/routes.rs` (`/v1/compliance/evaluate` now caps route points)
- Why it matters: `validate_flight_plan` loops over every point and also does segment-by-segment geofence checks (sampling-based), which can be made extremely expensive if `trajectory_log` is very large. Even with admin auth, this can turn a single bad request into an outage.
- Fix (implemented):
  - A global request body limit of 1MiB is enforced (F-DRONE-007).
  - Flight plan and compliance-evaluate now reject requests whose extracted route exceeds `route_planner_max_waypoints` (covers both `waypoints` and `trajectory_log`).
  - Added regression tests covering both endpoints.
- Verify:
  - `cargo test -p atc-server` (includes `flight_plan_rejects_too_many_route_points` and `compliance_evaluate_rejects_too_many_route_points`).

F-DRONE-031 — **P1 / Safety**: Strategic deconfliction with trajectories can still miss conflicts (time discretization)
- Where: `atc-drone/crates/atc-core/src/spatial.rs` (`check_timed_conflict` samples time with a step clamped to 0.5–2.0s)
- Why it matters: the strategic scheduler can approve plans whose trajectories violate separation between samples (especially at higher relative speeds). This defeats the purpose of strategic scheduling as a pre-flight safety gate.
- Fix: move to continuous-time checks:
  - Use analytic closest-approach on relative motion in a local ENU frame, or
  - bound-based segment/segment checks with adaptive time steps around predicted CPA.
- Verify: regression tests for “near miss between samples”, plus property tests comparing against a reference implementation.

F-DRONE-032 — **P2 / Launch readiness**: Integration tests exist but are not run in CI (and safety gates aren’t enforced)
- Where:
  - `atc-drone/crates/atc-server/tests/*` (many tests are `#[ignore]` and require a running server)
  - `atc-drone/.github/workflows/ci.yml` (`cargo test --all` only; no ignored tests; no `fmt`/`clippy`)
- Why it matters: the repo has a lot of safety-critical behavior (conflict detection, command dispatch, geofence enforcement). If CI doesn’t run the “real” end-to-end tests, regressions will land silently and you won’t know until demo/deployment.
- Fix:
  - Add a CI job that spins up the stack (or at least `atc-server`) and runs the ignored integration tests.
  - Add `cargo fmt --check` and `cargo clippy -- -D warnings` as gates.
  - Treat key safety scenarios as non-ignored tests (or move them into a deterministic harness that CI can run).
- Verify: CI fails when a known regression is introduced (e.g., CPA miss case, geofence intersection case); CI passes on clean main.

### 5.2 `atc-frontend` (Node UI + proxy)

**What it is**
- User-facing console (sessions + CSRF) plus HTTP/WS proxy to backend and Blender.

**Already fixed**
- Proxy injects admin token only for specific admin operations and requires `authority`:
  - Geofence CRUD + `POST /v1/rid/view`

**Open risks / work**
- “Default users” / guest login now fails closed in production: close **F-FRONTEND-005**.
- WebSocket Origin hardening (CSWSH) should be explicit.
- Login throttling / brute force defense.
- CSP still allows `style-src 'unsafe-inline'` (planner also allows `script-src 'unsafe-inline'`); inline script attributes have been removed.

**Detailed findings (write-as-we-go; do not delete)**

F-FRONTEND-001 — **P0 / Security (FIXED)**: XSS surface via `innerHTML` + inline `onclick` with untrusted IDs (drone IDs, statuses, etc.)
- Where:
  - `atc-frontend/static/js/fleet.js:160`–`190` (`contentEl.innerHTML` uses `${droneId}` inside `onclick="Fleet.holdDrone('${droneId}')"` / `resumeDrone`)
  - `atc-frontend/static/js/missions.js:239`–`264` (builds HTML strings with `onclick="window.location.href='${detailsHref}'"` etc)
  - Many other pages use `innerHTML = \`...\`` patterns (`rg "innerHTML =" atc-frontend/static/js`)
- Why it matters: drone IDs and some status fields can be attacker-controlled (registration / telemetry). Injecting `'` / `"` / HTML can break attributes and execute in the browser. This is amplified because the CSP currently allows inline script attributes (see F-FRONTEND-002).
  - Note: `encodeURIComponent(...)` does **not** encode `'` (apostrophe). So patterns like `onclick="window.location.href='...${encodeURIComponent(id)}...'"` are still injectable if `id` contains `'`.
- Fix:
  - Eliminate inline `onclick=` handlers; attach handlers with `addEventListener` and keep data in `data-*` attributes only.
  - When dynamic HTML is necessary, use safe DOM APIs (`textContent`) or sanitize strictly; default to escaping (`escapeHtml`) for any user/remote-controlled values.
  - Treat **drone_id**, **mission ids**, and any **string fields from backend** as untrusted.
- Status:
  - **DONE**: removed inline `onclick=` and replaced with `addEventListener` and safe links:
    - `atc-frontend/static/js/fleet.js`
    - `atc-frontend/static/js/missions.js`
- Verify:
  - Add an end-to-end security test that registers a drone with an ID containing quotes/HTML and asserts no script execution (Cypress/Playwright).
  - Add a lint rule or grep gate in CI forbidding `onclick="` and requiring escaping for `innerHTML` templating.

F-FRONTEND-002 — **P0 / Security (FIXED)**: CSP weakens XSS defense (`script-src-attr 'unsafe-inline'`, `style-src 'unsafe-inline'`), and `JSON.stringify` is injected into a `<script>` context unsafely
- Where:
  - `atc-frontend/server.js:371`–`425` (CSP header; includes `script-src-attr 'unsafe-inline'` and `style-src 'unsafe-inline'`; planner assets additionally allow `'unsafe-inline' 'unsafe-eval'` scripts).
  - `atc-frontend/views/layouts/main.ejs:43`–`57` (`window.APP_USER = <%- JSON.stringify(user || null) %>;` etc)
- Why it matters:
  - Allowing inline script attributes means many classic XSS payloads execute even if inline `<script>` tags are blocked.
  - Unescaped `JSON.stringify(...)` inside a `<script>` tag can be broken with `</script>` sequences if any embedded values are attacker-controlled (signup name/email/ID). With `script-src-attr 'unsafe-inline'`, breaking out of the `<script>` tag can still yield executable inline handlers.
- Fix:
  - Remove `script-src-attr 'unsafe-inline'` and refactor inline handlers into JS (`addEventListener`).
  - Replace `JSON.stringify` templating with a safe serializer for `<script>` context (escape `<` as `\\u003c`, `</script` defenses). Common options: `serialize-javascript` or a small local helper.
  - Remove `style-src 'unsafe-inline'` from the main app CSP:
    - Use nonce-based `<style nonce="...">` where inline `<style>` blocks exist.
    - Keep legacy inline `style="..."` support via `style-src-attr 'unsafe-inline'` (needed for existing templates and Cesium UI behavior).
  - Keep the “planner” CSP exception tightly scoped; consider migrating planner to nonce-based scripts instead of `unsafe-inline`.
- Status:
  - **DONE**: removed `script-src-attr 'unsafe-inline'` from CSP and eliminated inline event handlers across the UI.
  - **DONE**: replaced raw `<%- JSON.stringify(...) %>` in `views/layouts/main.ejs` with `safeJson(...)` (server-side helper that escapes `<` etc for `<script>` context).
  - **DONE**: removed `style-src 'unsafe-inline'` from the main app CSP and added nonces to inline `<style>` blocks.
  - **TODO (P1)**: remove `style-src-attr 'unsafe-inline'` by migrating remaining `style="..."` usage to CSS classes (and confirm Cesium does not require inline styles).
- Verify:
  - Add a CSP regression test in CI that checks response headers for the expected policy (no `unsafe-inline` attributes).
  - Add a unit test that renders `layouts/main.ejs` with a name containing `</script>` and asserts output is still a single script block (or escapes).
  - Added lightweight grep gate script: `atc-frontend/tools/security-smoke.js` (run in CI/container via Node).

F-FRONTEND-003 — **P1 / Security (FIXED)**: Logout is a GET (CSRF-able) + login does not regenerate the session (session fixation class)
- Where:
  - `atc-frontend/views/partials/header.ejs` (logout is now a POST form with CSRF token)
  - `atc-frontend/server.js` (logout is now `POST /logout`; `GET /logout` returns 405)
  - `atc-frontend/server.js` (login/signup/guest login regenerate the session before setting `req.session.user`)
- Why it matters:
  - Logout-by-GET can be triggered cross-site (nuisance attack) and violates typical CSRF posture.
  - Without session regeneration on login, a session fixation attack is possible in some deployment setups (especially if cookies can be set/forced through other bugs).
- Fix:
  - Make logout a POST with CSRF token and disallow logout-by-GET.
  - Call `req.session.regenerate` on successful login/signup/guest login before setting `req.session.user`.
- Verify:
  - Manual: GET `/logout` returns 405; POST `/logout` requires CSRF.
  - Manual: session ID changes across login (new session created on login).

F-FRONTEND-004 — **P1 / Security (FIXED)**: No brute-force protection on login / signup
- Where:
  - `atc-frontend/server.js` (`POST /login` and `POST /signup`)
  - `atc-frontend/server.js` (in-memory auth throttling helpers)
- Why it matters: password guessing is practical on an internet-exposed console; bcrypt is also CPU-expensive and can become a DoS vector without throttling.
- Fix:
  - Add in-memory throttling:
    - login lockouts on repeated failures (per-IP and per-existing-username), returning 429 + `Retry-After`.
    - signup request rate limiting per-IP, returning 429 + `Retry-After` before bcrypt hashing work.
  - Consider requiring admin creation of accounts for production (disable self-signup).
- Verify:
  - Manual: repeated bad logins eventually return 429 (with `Retry-After`); successful login clears the lockout for that IP/user.
  - Manual: repeated signup submissions eventually return 429 (with `Retry-After`).

F-FRONTEND-005 — **P1 / Security + Launch readiness (FIXED)**: Default users + guest login are one env-var away from shipping to prod
- Where:
  - `atc-frontend/server.js:324`–`349` (default `admin@example.com/admin123` and `guest@example.com/guest123` seeded when `ATC_ALLOW_DEFAULT_USERS=1` or when bootstrap env vars are missing and defaults are allowed)
  - `atc-frontend/server.js:600`–`621` (guest one-click login)
- Why it matters: this is a common “demo to prod” trap; if a deploy accidentally keeps defaults, the UI is compromised immediately.
- Status: fixed in `atc-frontend/server.js`:
  - In production (`NODE_ENV=production`):
    - process refuses to start if `ATC_ALLOW_DEFAULT_USERS=1`
    - process refuses to start when user DB is empty unless explicit `ATC_BOOTSTRAP_ADMIN_EMAIL` + `ATC_BOOTSTRAP_ADMIN_PASSWORD` are set
    - process refuses to start if bootstrap passwords match known placeholders (`admin123`, `guest123`)
    - process refuses to start if existing `admin`/`guest` accounts still use known default passwords
    - guest one-click login is disabled (route not registered) and login page hides the guest option
- Fix:
  - Fail closed in production: if `NODE_ENV=production`, refuse to start when `ATC_ALLOW_DEFAULT_USERS=1`, and refuse to enable guest login.
  - Require explicit bootstrap admin credentials in production (no fallback).
  - Add a startup warning/error if any credential matches known placeholders (e.g., `admin123`, `guest123`).
- Verify:
  - Add a “production config” startup test in CI that asserts process exits when defaults are enabled.

F-FRONTEND-006 — **P1 / Security (FIXED)**: WebSocket proxy does not validate `Origin` (CSWSH hardening) and role logic is inconsistent (`admin` not treated as authority)
- Where:
  - `atc-frontend/server.js` (`buildAtcWsPath` role handling)
  - `atc-frontend/server.js` (WS upgrade handler validates `Origin`)
- Why it matters:
  - If cookies are sent on cross-site WebSocket handshakes (browser variance + SameSite behavior), lack of Origin validation enables CSWSH.
  - Admin users unexpectedly lose “authority” behavior in WS filters (product correctness / access-control consistency).
- Fix:
  - Enforce Origin validation for WS upgrades:
    - In production, reject missing/invalid Origin.
    - Default: require same host (and `X-Forwarded-Proto` match when present).
    - Optional allowlist via `ATC_WS_ALLOWED_ORIGINS` (comma-separated origins).
  - Treat `admin` as authority (same as `authority`) when building WS query filters.
  - Consider requiring a per-session WS token (or short-lived signed token) in addition to cookies.
- Verify:
  - Manual: attempt WS connection from a different Origin and assert rejection (403).
  - Manual: confirm `admin` users can subscribe with `owner_id` filters like `authority`.

F-FRONTEND-007 — **P2 / Product + Reliability**: Map keeps showing stale geofences when `atc-drone` is down (matches observed user report)
- Where:
  - `atc-frontend/static/js/map.js:1133`–`1145` (`fetchGeofences` swallows errors and does not clear existing geofence entities)
- Why it matters: the UI can present stale airspace restrictions as “active” even when backend is unreachable; operators can be misled during outages.
- Fix:
  - On fetch failure (exception or non-2xx), mark geofence layer stale and clear entities (or clearly label stale + disable actions).
  - Apply the same pattern to other polled layers (traffic/conflicts) so outage behavior is consistent.
- Verify:
  - E2E test: load map, stop backend, ensure geofences are cleared or “STALE” indicator appears within one refresh interval.

F-FRONTEND-008 — **P2 / Product correctness**: Role checks treat `authority` and `admin` inconsistently across UI pages and actions
- Where:
  - `atc-frontend/routes/control.js:72`–`74` and `:88`–`90` (authority-only views; admin excluded)
  - `atc-frontend/static/js/geofences.js:31`–`32` (`canManage` only checks role === `authority`)
  - `atc-frontend/server.js:1405`–`1408` (`buildAtcWsPath` excludes admin)
- Why it matters: “admin” should generally be a superset role; inconsistent checks cause confusing UX and may lead to operators using lower-privileged accounts incorrectly.
- Fix:
  - Normalize role checks: treat `admin` as `authority` everywhere (or define clear RBAC scopes and implement consistently).
  - Add a single helper (server + client) for `isAuthorityOrAdmin()`.
- Verify:
  - Add UI tests that admin can access authority pages and perform authority actions.

F-FRONTEND-009 — **P2 / Security**: Proxy allowlist checks do not canonicalize paths (encoded path separator risk) and proxy-side authorization contains “accept unknown drone” behavior
- Where:
  - `atc-frontend/server.js:896`–`933` (regex allowlist on `requestPath`)
  - `atc-frontend/server.js:1006`–`1033` (`canAccessDrone` returns `true` when drone not found or has no `owner_id`)
  - `atc-frontend/server.js:1077`–`1120` (proxy gate logic for commands/flights/intents)
- Why it matters:
  - If the upstream router treats `%2F` as `/`, a path may pass allowlist as a single segment but route differently upstream (class of allowlist bypass).
  - Allowing operations on “unknown/unowned” drones is risky when the proxy injects admin privileges for some endpoints; it must match the backend’s ownership/auth model exactly.
- Fix:
  - Canonicalize/normalize URL paths before allowlist checks (reject any encoded slashes/backslashes, dot-segments, or non-normalized paths).
  - Make proxy-side ownership decisions explicit and conservative: if a drone is unknown, treat as forbidden (unless a deliberate “unowned allowed” mode is enabled).
  - Prefer enforcing ownership in `atc-drone` (server-side RBAC) and keep proxy as a convenience layer, not a security boundary.
- Verify:
  - Add tests with encoded path separators and dot segments to assert the proxy rejects them.
  - Add tests that operator accounts cannot issue commands to drones they do not own.

F-FRONTEND-010 — **P1 / Product + Reliability (FIXED)**: Planner waypoint callbacks are duplicated / overwritten, contributing to brittle “waypoint state” bugs (including the reported “remove S clears all” incident)
- Where:
  - `atc-frontend/static/planner/index.html` (`window.onWaypointsCleared` is now defined once)
- Why it matters: duplicated global callbacks cause divergent state updates (validation UI, internal route state, marker labels). This is exactly the kind of hidden coupling that leads to “one click nukes everything” behaviors.
- Fix:
  - Consolidate planner callbacks into a single source of truth (one `onWaypointsCleared`, one `onWaypointAdded`, etc).
  - Make “clear all” and “remove single stop” flows explicitly distinct; avoid using global “clear everything” callbacks for partial edits.
  - Add an explicit planner state machine (or at least a single `resetPlannerState({ scope: 'all' | 'routeInputs' | 'mapWaypoints' })`) to prevent accidental full resets.
- Verify:
  - UI test: create A→…→S, remove S, assert A→… remain.
  - UI test: clear all, assert all cleared (map + inputs + validation panel).

F-FRONTEND-011 — **P2 / Security + Ops**: Container hardening is minimal (runs as root; dev-oriented mounts)
- Where:
  - `atc-frontend/Dockerfile` (no non-root user; uses `npm install` rather than production-oriented `npm ci --omit=dev`)
  - `atc-frontend/docker-compose.yml` (bind-mounts `server.js`, `views`, `static`, etc; dev ergonomics, not production posture)
- Why it matters: if the UI container is compromised (XSS leading to SSRF / RCE in Node deps, or a dependency exploit), running as root and with broad mounts increases blast radius and persistence.
- Fix:
  - Run as a non-root user in the image; use a read-only filesystem where possible; drop Linux capabilities.
  - Split dev vs prod compose; remove live mounts in production.
  - Use `npm ci --omit=dev` for reproducible production installs.
- Verify:
  - Container security scan (Trivy/Grype) as a CI job.
  - Confirm container runs with `USER node` (or similar) and that app still works.

F-FRONTEND-012 — **P2 / Security + Reliability (FIXED)**: WS upgrade handler silently ignores unknown paths (socket leak / DoS risk)
- Where:
  - `atc-frontend/server.js:1417`–`1425` (`server.on("upgrade")` returns early when `url.pathname !== atcWsProxyPath` without rejecting/closing the socket)
- Why it matters: when you register an `upgrade` handler, you generally need to explicitly reject unknown upgrades. Returning without closing can leave sockets hanging, allowing cheap connection-flood DoS.
- Fix:
  - For any upgrade request not matching the proxy path, call `rejectUpgrade(socket, 404, "Not Found")` and close immediately.
  - Add basic per-IP rate limiting for upgrades (or rely on a reverse proxy) if this is internet-exposed.
- Verify:
  - Manual: `ws://host/garbage` closes immediately with a non-101 response.

F-FRONTEND-013 — **P2 / Reliability + Ops**: Session storage can grow without explicit TTL/reaping (file-store default) and `/csrf` can create sessions on demand
- Where:
  - `atc-frontend/server.js:466`–`474` (file session store fallback; no explicit TTL/reap config)
  - `atc-frontend/server.js:532`–`536` (`GET /csrf` creates/stores CSRF token in session)
- Why it matters: in non-Redis deployments (or misconfigured Redis), persistent file sessions can accumulate and fill disk. A public `/csrf` endpoint can also be hit repeatedly to create sessions and churn storage.
- Fix:
  - Prefer Redis in production, and explicitly configure session TTL and cleanup (both Redis key TTL and file-store reaping options).
  - Consider requiring authentication for `/csrf` (or only issuing tokens after login) if you don’t need it for unauthenticated flows.
  - Add per-IP request rate limiting for `/csrf` and other unauthenticated endpoints.
- Verify:
  - Load test that hits `/csrf` repeatedly and confirms sessions are reaped/TTL’d and disk usage stays bounded.

F-FRONTEND-014 — **P1 / Security + Product (FIXED)**: Role model is incomplete (e.g., `viewer` can still perform state-changing actions)
- Where:
  - `atc-frontend/server.js:817`–`1348` (Blender proxy endpoints use `requireAuth` but not `requireRole`; non-authority roles can still create declarations)
  - `atc-frontend/server.js:1077`–`1200` (ATC proxy gates on “authority vs not”; does not distinguish `viewer` vs `operator`)
  - `atc-frontend/server.js:600`–`621` (guest account role is `viewer`, but UI + proxy do not consistently enforce read-only)
- Why it matters: the UI appears to intend a “guest/viewer” mode, but current enforcement is mostly “authority vs everyone else.” That makes the viewer account materially more powerful than intended and expands the blast radius of credential compromise.
- Fix (implemented):
  - Define RBAC explicitly: at minimum `viewer` (read-only), `operator` (owns drones, can submit plans for own drones), `authority`/`admin` (airspace + global controls).
  - `atc-frontend/server.js` now blocks `viewer` from state-changing ATC proxy calls (while still allowing compute-only endpoints like route planning).
  - `atc-frontend/server.js` now blocks `viewer` from creating/deleting Flight Blender flight declarations.
  - Added a regression test: `atc-frontend/tools/rbac-smoke.js` (wired into `npm test`).
- Verify:
  - `atc-frontend/tools/rbac-smoke.js` asserts a `viewer` session receives 403 on:
    - `POST /api/atc/v1/drones/register`
    - `POST /api/blender/flight-declarations`
    - `DELETE /api/blender/flight-declarations/:id`

F-FRONTEND-015 — **P1 / Product (User-facing bug) (FIXED)**: Removing a stop (or clearing a single Start/End input) can clear *all* waypoints due to an unsafe `clearWaypoints()` call ordering
- Where:
  - `atc-frontend/static/planner/src/planner.js:520`–`541` (`clearWaypoints()` always calls `root.onWaypointsCleared()`)
  - `atc-frontend/static/planner/index.html` (`resyncWaypointsFromInputs()` sets `suppressWaypointSync=true` before calling `FlightPlanner.clearWaypoints()`)
- Why it matters: this matches the observed behavior (“had waypoints up to S → clicked X on S → all disappeared”). It destroys operator input and makes the planner feel unreliable.
- Fix:
  - Do not call `FlightPlanner.clearWaypoints()` in `removeStopInput` / `clearSingleInput` directly.
  - Instead, update input state first and call a single “sync” function that sets `suppressWaypointSync=true` before touching map state (the existing `resyncWaypointsFromInputs()` already does this).
  - Consider adding a dedicated `FlightPlanner.setWaypoints([...])` API to avoid the clear→re-add churn (and callback side effects).
- Verify:
  - UI test: create A→…→S, click remove on S, assert other waypoints remain and map markers update correctly.
  - UI test: clear Start value, assert only Start cleared (or defined expected behavior), without deleting other stops.

F-FRONTEND-016 — **P0 / Security (FIXED)**: Reflected XSS via unescaped route param interpolation into HTML attributes (`missionId`)
- Where:
  - `atc-frontend/views/mission-detail.ejs:13` (`data-mission-id="${missionId}"` inside a JS template literal passed as `body`)
- Why it matters: `missionId` comes from the URL path (`/control/missions/:id`) and is inserted into an HTML attribute without escaping. A crafted URL can break out of the attribute and execute script in the victim’s browser (logged-in operator/authority), giving full same-origin access to the console and its proxies.
- Fix:
  - Stop building page markup as JS template literals with `${...}` interpolation for untrusted values.
  - Preferred: convert `mission-detail.ejs` to a normal EJS template body (no `body: \`...\``), and render `missionId` with `<%= %>` escaping.
  - Minimal: HTML-escape `missionId` before interpolation (must escape at least `& < > " '`), and avoid inserting it into raw HTML attributes if possible (pass via a JSON script tag with safe serialization instead).
- Status:
  - **DONE**: HTML-escape `missionId` on the server before interpolating into `data-mission-id` in `atc-frontend/views/mission-detail.ejs`.
- Verify:
  - E2E test: visit `/control/missions/%22%20onmouseover%3Dalert(1)%20x%3D%22` and assert no JS executes and the page renders safely.

F-FRONTEND-017 — **P0 / Security (FIXED)**: Planner XSS via unescaped `innerHTML` from untrusted sources (drone IDs + geocoder results)
- Where:
  - `atc-frontend/static/planner/index.html:1489`–`1501` (`droneSelect.innerHTML = options.join('')` with `${id}` and `${status}` unescaped)
  - `atc-frontend/static/planner/index.html:1600`–`1632` (`showAutocomplete` injects `r.displayName` into `.place-name`/`.place-address` via `innerHTML` without escaping)
- Why it matters:
  - `drone_id` is attacker-controlled (registration / telemetry) and can contain quotes/HTML.
  - Geocoder results are external/untrusted input. If an attacker can influence the geocoder response (or if an upstream dataset contains unexpected markup), this becomes a same-origin XSS.
  - The planner CSP is intentionally permissive (`unsafe-inline`/`unsafe-eval` for `/assets/planner/*`), so one injection is game over for the whole console origin.
- Fix:
  - Replace `innerHTML` construction with DOM creation (`document.createElement('option')`, `.value = …`, `.textContent = …`).
  - In autocomplete, set `.textContent` for name/address (never interpolate into HTML); keep the full result object in a JS-side map keyed by an integer index rather than `dataset.results = JSON.stringify(...)`.
  - Add a “dangerous string” test harness: treat any `"<>&'` in IDs/display names as hostile and ensure it renders as text.
- Status:
  - **DONE**: `droneSelect` now uses DOM option creation (no `innerHTML`) and previous selection restoration avoids `querySelector` injection.
  - **DONE**: autocomplete dropdown now uses DOM nodes + `textContent` and stores result objects in a `WeakMap` instead of `dataset.results`.
  - **DONE**: removed inline `onclick=` usage from planner UI elements (now uses `addEventListener`).
- Verify:
  - Register a drone with ID like `bad\"><img src=x onerror=alert(1)>` and confirm opening the planner does not execute JS and the dropdown renders escaped text.
  - Mock the geocoder result `displayName` to include `</div><img …>` and confirm it renders as text.

### 5.3 `atc-blender` + `interuss-dss` (Django + DSS sandbox)

**What it is**
- “Adapter” tier translating ATC concepts into Blender/DSS flows.

**Already fixed**
- Removed Python `eval()` on Redis-loaded state (replaced with JSON + safe fallback parsing).
- `atc-blender` fails closed on dangerous auth flags / placeholder secrets in non-debug:
  - `flight_blender/settings.py` requires `DJANGO_SECRET_KEY` (or legacy `SECRET_KEY`) when `IS_DEBUG=0`, rejects placeholder/weak values, and forbids `BYPASS_AUTH_TOKEN_VERIFICATION` when `IS_DEBUG=0`.

**Open risks / work**
- JWKS issuer validation is now implemented (see **F-BLENDER-002**), but you still need to configure the expected issuers in production via environment variables.
- Key material clarity: separate Django `SECRET_KEY` from JWT signing key(s).
- Ensure DSS schema/boot is stable under the selected CockroachDB version and migrations (avoid “backfill” footguns).
- Blocking `time.sleep()` calls inside Django views (RID/USS paths) can tie up workers and create DoS risk.
- Assertions used as request validation in views/helpers can be disabled with `-O` (validation can disappear).
- Redis `KEYS` usage for session/track enumeration is O(N); replace with `SCAN`.
- Entry points use `uvicorn --reload` (dev‑only) in normal runs; should be split for prod.
- `start_flight_blender.sh` removes **all** Docker containers/volumes on the host (unsafe helper).
- Longitude is negated in surveillance track generation (`lng = -lon_dd`), mirroring tracks across the prime meridian.

**Detailed findings (write-as-we-go; do not delete)**

F-BLENDER-001 — **P0 / Correctness + Safety (FIXED)**: Surveillance track generation flips longitude sign (and mixes altitude units)
- Where:
  - `atc-blender/surveillance_monitoring_operations/utils.py:96`–`116` (`lng=-latest_observation.lon_dd`; `AircraftPosition.alt=latest_observation.altitude_mm`)
  - Data model: `atc-blender/surveillance_monitoring_operations/data_definitions.py:98`–`105` documents `lat/lng` in degrees and `alt` in meters.
- Why it matters: negating longitude mirrors tracks across the prime meridian; the system can display/compute positions in the wrong place. Altitude appears to be passed as **mm** in some places where consumers expect **meters**, causing 1000× errors.
- Fix:
  - Remove longitude negation: use `lng=latest_observation.lon_dd`.
  - Normalize altitude consistently: treat `altitude_mm` as **millimeters** and convert to meters for emitted track output (`LatLangAltPoint.alt`, `AircraftPosition.alt`, `pressure_altitude`).
  - Ensure timestamps flow through to track generation; if timestamps are missing or non-increasing, fall back to `delta_time_secs=1.0` so track fusion does not crash.
  - Add unit tests for a known point in the western hemisphere (lon < 0 stays < 0), and for altitude conversion (1000mm → 1.0m).
- Verify:
  - Regression test feeding a sample observation `(lat=33.6846, lon=-117.8265, alt_mm=10000)` and asserting emitted track uses `lon=-117.8265` and altitude in meters.
  - Unit test: `atc-blender/tests/test_surveillance_tracks.py`

F-BLENDER-002 — **P1 / Security + Availability (FIXED)**: Auth middleware fetches JWKS on every request and does not validate issuer
- Where:
  - `atc-blender/auth_helper/utils.py:44`–`139` (`requires_scopes` fetches Passport + DSS JWKS for each request)
- Why it matters:
  - Per-request JWKS fetch makes every API call depend on external JWKS availability and can amplify load on the JWKS server (and your own service under latency).
  - `jwt.decode(... options={"require": ["exp","iss","aud"]})` requires `iss` but does not validate it against an expected issuer value.
- Status:
  - **DONE**: JWKS fetch is cached with TTL + backoff (no longer fetched on every request), and “keys unavailable” fails closed with 503.
  - **DONE**: validates issuer (`iss`) against an allowlist so tokens from unexpected issuers are rejected.
- Fix:
  - Cache JWKS with TTL + background refresh + backoff; fail closed with 503 if keys are unavailable rather than 400.
  - Validate issuer (`iss`) explicitly against a configured allowlist (Passport issuer + DSS issuer).
    - Config: `PASSPORT_ISSUER`, `DSS_AUTH_ISSUER`, and optional comma-separated `JWT_ALLOWED_ISSUERS`.
  - Consider using `PyJWKClient` with caching or a small local JWKS cache implementation.
- Verify:
  - Load test demonstrating JWKS is fetched at most once per TTL window under concurrency.
  - Unit test: `atc-blender/tests/test_issuer_allowlist.py`.

F-BLENDER-003 — **P1 / Reliability + DoS (FIXED)**: `time.sleep()` inside Django views blocks worker threads
- Where:
  - `atc-blender/rid_operations/views.py` (removed sleeps from request path)
  - `atc-blender/uss_operations/views.py` (removed sleeps from request path)
- Why it matters: synchronous sleeps tie up worker processes, increasing tail latency and enabling request-flood DoS (especially with low worker counts).
- Fix:
  - Remove blocking sleeps from Django views; endpoints now return immediately with best-effort data rather than blocking worker threads.
- Verify:
  - Manual: concurrent requests no longer stall due to fixed sleeps in the request path.

F-BLENDER-004 — **P1 / Correctness + Security (FIXED)**: Assertions used for request validation are unsafe and can disappear under `-O`
- Where:
  - `atc-blender/geo_fence_operations/views.py:67`
  - `atc-blender/flight_feed_operations/views.py:137`
  - Multiple asserts in helpers (e.g., `atc-blender/rid_operations/dss_rid_helper.py`, `atc-blender/scd_operations/dss_scd_helper.py`)
- Why it matters: `assert` is not a validation mechanism in production (it can be stripped). It also returns 500-style failures rather than clean 4xx responses.
- Status:
  - **DONE**: replaced all validation-related `assert` statements with explicit checks in request handlers and DSS/constraints helpers.
  - **DONE**: added regression test `atc-blender/tests/test_no_assert_validation.py` to prevent reintroducing assert-based validation.
- Fix (implemented):
  - Use Content-Type parsing (supports `application/json; charset=utf-8`) and return 415 on non-JSON/missing Content-Type.
  - Return structured errors (no `None` tokens / KeyErrors) when DSS audience/token config is missing.
- Verify:
  - Unit test: `atc-blender/tests/test_no_assert_validation.py`.

F-BLENDER-005 — **P1 / Performance + Reliability (FIXED)**: Redis `KEYS` is used for track enumeration and one path returns malformed `ActiveTrack.observations`
- Where:
  - `atc-blender/common/redis_stream_operations.py:247`–`269` (`self.redis.keys(pattern)`)
  - `atc-blender/common/redis_stream_operations.py:261`–`266` stores `observations` as a raw string rather than a list (in `get_all_active_tracks_in_session`)
- Why it matters:
  - `KEYS` is O(N) and can block Redis under load.
  - Returning observations as a string can crash downstream code expecting `list[dict]`, causing intermittent runtime errors.
- Fix (implemented):
  - `get_all_active_tracks_in_session` now uses Redis `SCAN` (via `scan_iter`) when available instead of `KEYS`.
  - `ActiveTrack.observations` is now parsed the same way as `get_active_track` (JSON with `ast.literal_eval` fallback), and is always a list.
- Verify:
  - Unit test: `atc-blender/tests/test_redis_stream_operations.py`.

F-BLENDER-006 — **P0 / Ops Hazard (FIXED)**: Helper script can delete **all** Docker containers/volumes on the host
- Where:
  - `atc-blender/start_flight_blender.sh` (runs `docker rm -f $(docker ps -a -q)` and `docker volume rm $(docker volume ls -q)`, and stops system PostgreSQL)
- Why it matters: running this script on a shared machine can destroy unrelated workloads and data. This is a “footgun” that should not exist in a safety-adjacent repo.
- Fix:
  - Remove the global cleanup commands entirely (no `docker rm -f $(docker ps -a -q)` and no `docker volume rm $(docker volume ls -q)`).
  - Use project-scoped compose cleanup only: default `docker compose down --remove-orphans`; optional `--reset` runs `docker compose down -v --remove-orphans`.
  - Do not stop host services (`systemctl stop postgresql`) from a project script.
- Verify:
  - Manual review + a unit “shellcheck” gate; ensure the script only touches resources labeled for this compose project.
  - `bash -n atc-blender/start_flight_blender.sh` passes.

F-BLENDER-007 — **P2 / Security + Hygiene**: `dump.rdb` is present in the repo (potential data leak / confusion)
- Where:
  - `atc-blender/dump.rdb`
- Why it matters: Redis snapshots can include sensitive operational data and should not be committed. Even if benign, it confuses “source of truth” and bloats the repo.
- Fix:
  - Remove from repo and add to `.gitignore`; document how to capture/debug data safely (sanitized exports).
- Verify:
  - CI check forbids committing `.rdb` dumps.

F-BLENDER-008 — **P1 / Security (FIXED)**: GeoZone import-by-URL can be used as an SSRF primitive (token-holder can fetch arbitrary URLs)
- Where:
  - `atc-blender/geo_fence_operations/views.py:295`–`339` (`GeoZoneSourcesOperations.put` accepts a URL and queues `download_geozone_source`)
  - `atc-blender/geo_fence_operations/tasks.py:22`–`43` (`download_geozone_source` does `requests.get(geo_zone_url, ...)`)
- Why it matters: URLValidator confirms “is a URL” but does not prevent internal network targets (e.g., `http://169.254.169.254/` cloud metadata, service mesh IPs, localhost). If this endpoint is reachable with a stolen token, it can be used to probe internal services.
- Fix (implemented):
  - Added a shared SSRF guard: `atc-blender/geo_fence_operations/url_safety.py` (`validate_public_url`).
  - Restricted allowed schemes to `https` (with `http` permitted only when `IS_DEBUG=1`).
  - Blocks localhost, IP-literals in private/link-local/reserved ranges, and DNS names that resolve to disallowed IP ranges.
  - Task fetch now enforces timeouts, a max download size (`GEOZONE_MAX_DOWNLOAD_BYTES`, default 5MB), rejects non-JSON content types, and follows only a limited number of redirects (`GEOZONE_MAX_REDIRECTS`, default 3) with re-validation at each hop.
- Verify:
  - Unit test: `atc-blender/tests/test_geozone_url_safety.py`.

F-BLENDER-009 — **P1 / Security + Launch readiness (FIXED)**: Django “secure by default” deployment settings are missing/implicit
- Where:
  - `atc-blender/flight_blender/settings.py` (no `SECURE_SSL_REDIRECT`, `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`, `SECURE_HSTS_SECONDS`, etc)
- Why it matters: even if auth is correct, missing deployment hardening can leak session cookies over HTTP, allow clickjacking/XSS amplification, and generally fails the expectations of a production web service.
- Status:
  - **DONE**: enabled Django deployment hardening defaults when `IS_DEBUG=0` (with env overrides).
  - **DONE**: added regression test to ensure these settings remain enabled for production.
- Fix (implemented):
  - Added `SECURE_SSL_REDIRECT`, secure cookie flags, HSTS, referrer policy, no-sniff, and clickjacking protection in `atc-blender/flight_blender/settings.py`.
  - Defaults can be overridden via env vars (`SECURE_SSL_REDIRECT`, `SECURE_HSTS_SECONDS`, `SECURE_HSTS_PRELOAD`, etc).
- Verify:
  - Unit test: `atc-blender/tests/test_settings_deploy_hardening.py`.
  - Manual: `python manage.py check --deploy` reports no issues for `IS_DEBUG=0`.

F-BLENDER-010 — **P1 / Reliability (FIXED)**: Query parsing can throw 500s on malformed input (missing 4xx validation)
- Where:
  - `atc-blender/geo_fence_operations/views.py:220`–`230` (`view_port = [float(i) for i in view.split(\",\")]` without validation)
  - Similar patterns exist in other “view” bbox endpoints (RID/SCD helpers call parsing utilities; ensure they all fail 400, not 500)
- Why it matters: a single malformed request can crash a request handler (500) and may be used for noisy DoS; it also makes the system brittle when clients send bad data.
- Status:
  - **DONE**: added bounded parsers `parse_view_lat_lng` / `parse_view_minx_miny` with max-length checks.
  - **DONE**: updated list/query endpoints to use the shared parsers and raise 400 on invalid `view`.
- Fix (implemented):
  - Centralized parsing/validation in `atc-blender/rid_operations/view_port_ops.py`.
  - `GeoFence` list + `FlightDeclaration` list now return 400 instead of 500 on malformed `view`.
- Verify:
  - Unit test: `atc-blender/tests/test_view_port_parsing.py`.

F-BLENDER-011 — **P2 / Reproducibility + Ops**: Runtime images use mutable tags and dev-oriented mounts
- Where:
  - `atc-blender/docker-compose.yml` and `atc-blender/docker-compose-dev.yml` use `valkey/valkey:latest` and mount the project into the container
- Why it matters: “latest” tags change over time and can break deployments; bind-mounting source code/venv is great for dev but not a shippable production posture.
- Fix:
  - Pin images by version (or digest) for Redis/Valkey and Postgres; provide separate dev vs prod compose profiles.
  - In production, remove source mounts and build immutable images; store state in named volumes only.
- Verify:
  - Rebuilding the stack from scratch on a clean machine produces the same container versions and behavior.

F-BLENDER-012 — **P1 / Correctness (FIXED)**: Speed calculation can divide by zero (duplicate / out-of-order timestamps)
- Where:
  - `atc-blender/surveillance_monitoring_operations/utils.py:30`–`47` (`speed_mts_per_sec = distance / delta_time_secs` without guarding `delta_time_secs <= 0`)
- Why it matters: duplicate timestamps or out-of-order observations can make `delta_time_secs` zero/negative → crash or `inf` speed, producing invalid RID output and breaking downstream consumers.
- Fix:
  - Treat non-positive deltas as “no movement” (`speed=0`, `vertical_speed=0`) so track fusion does not crash or emit `inf`.
- Verify:
  - Unit test with two observations sharing the same timestamp does not crash and yields finite outputs.
  - Unit test: `atc-blender/tests/test_surveillance_tracks.py` (`test_duplicate_timestamps_do_not_crash`)

F-BLENDER-013 — **P1 / Correctness + Reliability (FIXED)**: GeoFence RTree index clearing is inconsistent (stale entries / wrong intersections)
- Where:
  - `atc-blender/geo_fence_operations/rtree_geo_fence_helper.py:55`–`93`
    - `generate_geo_fence_index` swaps bounds to `[lat_min, lon_min, lat_max, lon_max]` before inserting (line ~71)
    - `clear_rtree_index` deletes using the **unswapped** `fence.bounds` order (line ~91–93), so deletions don’t match inserted rectangles
  - Index storage path is shared on disk: `atc-blender/common/data_definitions.py:94` (`/tmp/blender_geofence_idx`)
- Why it matters: the index persists on disk across requests. If “clear” doesn’t actually delete, intersections can include stale geofences (including out-of-window or deleted fences), producing incorrect geofence query results and confusing downstream safety decisions.
- Status:
  - **DONE**: switched GeoFence RTree indexes to in-memory per-request computation and made `clear_rtree_index` reset the in-memory index.
  - **DONE**: removed hash-based IDs in the index (uses per-build sequential integers).
- Verify:
  - Unit test: `atc-blender/tests/test_rtree_in_memory.py`.

F-BLENDER-014 — **P1 / Correctness + Scalability (FIXED)**: File-backed RTree indexes at fixed `/tmp/*` paths are unsafe under multi-worker concurrency (and ID collisions are possible)
- Where:
  - Index basepaths are fixed and shared: `atc-blender/common/data_definitions.py:92`–`96`
  - File-backed RTree usage:
    - `atc-blender/flight_declaration_operations/flight_declarations_rtree_helper.py:27`–`30`
    - `atc-blender/rid_operations/rtree_helper.py:43`–`46`
    - `atc-blender/geo_fence_operations/rtree_geo_fence_helper.py:15`–`17`
  - IDs are derived from `sha256(... ) % 10**8` in multiple places, allowing collisions at scale.
- Why it matters: in production you typically run multiple worker processes/containers. Shared on-disk indexes without per-process isolation + locking can corrupt or cross-contaminate results, leading to missed intersections (unsafe approvals) or false positives (unnecessary blocks).
- Status:
  - **DONE**: switched RTree helpers (GeoFence/FlightDeclaration/OperationalIntents) to in-memory `index.Index()` rather than fixed `/tmp/*` basepaths.
  - **DONE**: replaced `% 10**8` IDs with per-build sequential integers to avoid collisions within an index build.
- Verify:
  - Unit test: `atc-blender/tests/test_rtree_in_memory.py`.

F-BLENDER-015 — **P1 / Security (FIXED)**: Signed-telemetry public key URLs are fetched without SSRF protections or timeouts
- Where:
  - `atc-blender/flight_feed_operations/pki_helper.py:147`–`161` (`s.get(current_public_key.url)` with no timeout, no allowlist, no scheme/IP filtering)
  - Public key management endpoints are exposed via DRF:
    - `atc-blender/flight_feed_operations/views.py:579`–`588` (`SignedTelmetryPublicKeyList/Detail`)
    - `atc-blender/flight_feed_operations/urls.py:11`–`15`
- Why it matters: a token-holder (or compromised admin path) can register a key URL like `http://169.254.169.254/...` and cause the service to fetch internal resources (SSRF) or hang worker threads (no timeout), potentially impacting availability of RID/USS endpoints.
- Status:
  - **DONE**: added shared safe JSON downloader (SSRF guard + redirect validation + timeouts + size limit).
  - **DONE**: updated signed-telemetry JWKS fetch path to use the safe downloader.
- Fix (implemented):
  - Added `atc-blender/common/http_download.py` and wired it into `MessageVerifier.get_public_keys`.
  - Enforces `https` by default (allows `http` only when `IS_DEBUG=1`), blocks localhost/private/link-local, validates redirects, and caps download size.
- Verify:
  - Unit test: `atc-blender/tests/test_http_download.py`.

F-BLENDER-016 — **P1 / Security + Correctness (FIXED)**: Key material is conflated (`SECRET_KEY` used as Django secret *and* treated as an RSA private key for JOSE/JWKS)
- Where:
  - `atc-blender/flight_blender/settings.py`:
    - `DJANGO_SECRET_KEY` (preferred) or legacy `SECRET_KEY` for Django.
    - `OIDC_SIGNING_PRIVATE_KEY_PEM` for JOSE signing + JWKS publishing.
  - `atc-blender/flight_feed_operations/views.py` (`/signing_public_key` returns `settings.OIDC_SIGNING_PUBLIC_JWKS`)
  - `atc-blender/flight_feed_operations/pki_helper.py` (`sign_json_via_jose` loads `OIDC_SIGNING_PRIVATE_KEY_PEM`)
- Why it matters: this is brittle and dangerous configuration:
  - If `SECRET_KEY` is a normal Django secret, the JOSE/JWKS logic silently fails.
  - If `SECRET_KEY` is set to an RSA private key PEM, you’ve coupled unrelated security domains (Django signing + JOSE signing) and increased blast radius of key compromise.
- Fix:
  - Split configuration: `DJANGO_SECRET_KEY` for Django, and a dedicated `OIDC_SIGNING_PRIVATE_KEY_PEM`/`JWKS` keypair for JOSE/JWK publishing.
  - Update `public_key_view` and `sign_json_via_jose` to use the dedicated signing key(s), not Django’s secret.
  - Add explicit startup checks: if JOSE signing is enabled, fail fast unless the signing key parses as PEM and the public key endpoint returns valid JWKS.
- Verify:
  - Startup check fails when `OIDC_SIGNING_PRIVATE_KEY_PEM` is set but invalid (startup refuses to boot).
  - Unit test validates that `/signing_public_key` returns a valid JWKS document when `OIDC_SIGNING_PRIVATE_KEY_PEM` is configured.

F-BLENDER-017 — **P2 / Ops + Reliability**: Default container entrypoints run Uvicorn with `--reload` (dev-only) and mixed with `--workers`
- Where:
  - `atc-blender/entrypoints/no-database/entrypoint.sh` ends with `uvicorn ... --workers 3 --reload`
  - `atc-blender/entrypoints/with-database/entrypoint.sh` also uses `--reload`
  - `atc-blender/docker-compose.yml` uses `command: ./entrypoints/no-database/entrypoint.sh` (so `--reload` is the default compose path)
- Why it matters: auto-reload is meant for dev; it increases CPU usage, can cause unexpected restarts, and is generally not compatible with production process supervision. Combining `--reload` with multiple workers is also an error-prone configuration.
- Fix:
  - Use the dedicated prod entrypoint by default (`entrypoint-prod.sh`) in the main compose, or gate `--reload` behind `IS_DEBUG=1`.
  - Add a CI check that production compose never uses `--reload`.
- Verify:
  - Production compose starts with stable worker count and no reload watcher; logs confirm expected mode.

F-BLENDER-018 — **P0 / Security (FIXED)**: Django secret placeholder detection was incomplete; `atc-stack` default secret could slip into non-debug deployments
- Where:
  - `atc-stack/.env.example:72` defaults `BLENDER_SECRET_KEY=change-me-flight-blender-secret-key`
  - `atc-stack/docker-compose.yml:349` passes `DJANGO_SECRET_KEY=${BLENDER_SECRET_KEY:-change-me-flight-blender-secret-key}`
  - `atc-stack/atc-blender/flight_blender/settings.py` now rejects placeholder/weak `DJANGO_SECRET_KEY` values when `IS_DEBUG=0` (including any containing `change-me`, and short secrets)
- Why it matters:
  - Django’s `SECRET_KEY` protects session integrity and other signing operations. Shipping with a known placeholder means attacker-forgeable cookies/tokens.
  - This also makes the “sandbox vs production” boundary blurry: `IS_DEBUG=0` does **not** guarantee secrets are non-placeholder.
- Fix:
  - Tighten the production guard in `flight_blender/settings.py`:
    - reject any `DJANGO_SECRET_KEY` containing common placeholder patterns (`change-me`, `example`, etc) and enforce a minimum length check.
  - Defense in depth: add a CI/prod-profile guard in `atc-stack` that fails if `BLENDER_SECRET_KEY` is still the example default (even though Blender now fails fast at runtime).
- Verify:
  - With `IS_DEBUG=0`, setting `DJANGO_SECRET_KEY=change-me-flight-blender-secret-key` causes a hard startup failure.

F-BLENDER-019 — **P1 / Ops + Reliability (FIXED)**: `ALLOWED_HOSTS` defaults are not aligned with this stack’s deployment topology (easy BadHost failures)
- Where:
  - `atc-stack/atc-blender/flight_blender/settings.py:60`–`63` uses `ALLOWED_HOSTS=["*"]` only when `IS_DEBUG=1`, otherwise defaults to `"openutm.net"`
  - `atc-stack/docker-compose.yml:349`–`375` exposes Flight Blender on `localhost:8000` (Host header `localhost`) and does not pass `ALLOWED_HOSTS`
- Why it matters:
  - If someone tries to run “non-debug” (`IS_DEBUG=0`) for a production-like test in this stack, Django may reject requests with `DisallowedHost`, causing confusing failures.
  - For real deployments you want `ALLOWED_HOSTS` explicit and environment-specific, not a hardcoded default domain.
- Status:
  - **DONE**: `docker-compose.yml` now passes `ALLOWED_HOSTS` explicitly (via `BLENDER_ALLOWED_HOSTS`) for local stack profiles.
  - **DONE**: docs updated to call out `BLENDER_ALLOWED_HOSTS` for production-like `IS_DEBUG=0` runs.
- Verify:
  - With `BLENDER_IS_DEBUG=0` and `BLENDER_ALLOWED_HOSTS` including your hostnames, `/ping` responds without `DisallowedHost`.

**interuss-dss findings (write-as-we-go; do not delete)**

F-DSS-001 — **P0 / Launch readiness (FIXED)**: The bundled DSS is explicitly a *local sandbox* (insecure DB, dummy OAuth, test keys) and must not be treated as production-ready
- Where:
  - `atc-stack/docker-compose.yml:179`–`300` (CockroachDB `--insecure`, `local-dss-dummy-oauth`, and mounted `build/test-certs`)
- Why it matters: the stack currently uses:
  - CockroachDB in insecure mode (no TLS/auth at the DB layer),
  - a dummy OAuth server issuing tokens from a local private key file,
  - test certificates/keys mounted into containers.
  This is fine for demos, but catastrophic if someone “just deploys compose” to production.
- Fix:
  - Make the DSS sandbox **opt-in**:
    - `local-dss-*` and `mock-uss` are now behind the `dss` compose profile (not started by default).
    - Host port bindings for DSS/Dummy OAuth are bound to `127.0.0.1` only.
  - Add a hard “refuse to start in production” guard:
    - `local-dss-*` and `local-dss-dummy-oauth` exit immediately when `ATC_ENV=production`.
  - Run Flight Blender standalone by default:
    - `USSP_NETWORK_ENABLED` now defaults to `0` in `docker-compose.yml`.
- Verify:
  - `docker compose up -d` does **not** start any `local-dss-*` containers and does not publish DSS/Dummy OAuth ports.
  - With `ATC_ENV=production`, `docker compose --profile dss up` causes DSS/Dummy OAuth containers to refuse startup.

F-DSS-002 — **P1 / Reliability (FIXED)**: DSS schema bootstrapping uses `-db_version latest` (reproducibility risk)
- Where:
  - `atc-stack/docker-compose.yml:220`–`240` (`db-manager ... -db_version latest`)
- Why it matters: “latest” can change out from under you as the upstream DSS evolves; it can introduce drift between schema expectations and the pinned DSS image version.
- Status:
  - **DONE**: pinned RID/SCD schema bootstrapping versions via `DSS_RID_DB_VERSION` / `DSS_SCD_DB_VERSION` (defaults aligned with the `interuss/dss` image in `docker-compose.yml`).
- Fix (implemented):
  - `local-dss-rid-bootstrapper` now uses `-db_version ${DSS_RID_DB_VERSION:-4.0.0}`.
  - `local-dss-scd-bootstrapper` now uses `-db_version ${DSS_SCD_DB_VERSION:-3.1.0}`.
- Verify:
  - Fresh bring-up from scratch yields identical schema and a green healthcheck across runs.

F-DSS-003 — **P2 / Security + Supply chain**: DSS container image includes test certs and runs without an explicit non-root user
- Where:
  - `interuss-dss/Dockerfile` (final stage does not set `USER`; copies `build/test-certs` into image)
- Why it matters: for a production deployment you generally want:
  - minimal runtime contents (no test keys/certs),
  - non-root containers,
  - explicit separation between build/test artifacts and prod artifacts.
  For this stack it’s acceptable because it’s a local sandbox, but it reinforces that “local-dss” is not a production posture.
- Fix:
  - Use an upstream DSS production image/config or build a hardened image profile (no test-certs; non-root; minimal tools).
- Verify:
  - Container runs as non-root and still passes health checks in a hardened profile.

F-DSS-004 — **P0 / Security (FIXED)**: Dummy OAuth issues tokens from a bundled private key and is bound to a host port in the demo stack
- Where:
  - `atc-stack/docker-compose.yml:278`–`300` (`local-dss-dummy-oauth` binds `8085:8085` and uses `/var/test-certs/auth2.key`)
  - `interuss-dss/build/test-certs/auth2.key` (private key shipped for local testing)
  - `interuss-dss/cmds/dummy-oauth/Dockerfile` (copies `build/test-certs` into the image; no `USER` set → runs as root)
- Why it matters: anyone who can reach port 8085 can mint valid-looking tokens for the local DSS. That’s acceptable only for isolated local sandboxing; it is catastrophic if exposed to any untrusted network.
- Fix:
  - Dummy OAuth is now dev-only and opt-in:
    - behind the `dss` compose profile
    - host binding is `127.0.0.1:8085` (not a public interface)
  - Dummy OAuth refuses to start when `ATC_ENV=production`.
  - For real deployments, replace with a real OIDC provider and rotated keys.
- Verify:
  - `docker compose up -d` does not publish port 8085.
  - With `ATC_ENV=production`, `docker compose --profile dss up` causes dummy OAuth to refuse startup.

F-DSS-005 — **P1 / Security + Transport (FIXED)**: Demo DSS runs with HTTP and insecure CockroachDB flags (must be hardened for production)
- Where:
  - `atc-stack/docker-compose.yml:179`–`197` (`cockroach start --insecure`)
  - `atc-stack/docker-compose.yml:242`–`256` (`core-service -enable_http`)
- Why it matters: network-layer protections (TLS, auth, least exposure) are part of safety/security posture; “working demo” settings will be copied forward unless explicitly gated.
- Fix:
  - The local DSS is now explicitly a dev-only sandbox:
    - behind the `dss` compose profile,
    - host port bindings are loopback-only (`127.0.0.1:*`),
    - all DSS sandbox services refuse to start when `ATC_ENV=production`.
  - For real deployments: use a separate production deployment that enables DB+service TLS and removes all insecure flags.
- Verify:
  - With `ATC_ENV=production`, `docker compose --profile dss up` refuses startup.
  - `docker compose --profile dss config` shows loopback-only host bindings (no public interface exposure).

F-DSS-006 — **P1 / Reliability + Maintainability**: DSS Compose config uses deprecated/legacy flags and will break when upgrading DSS versions
- Where:
  - `atc-stack/docker-compose.yml:223`–`255` uses legacy flags like `-cockroach_host` and deprecated `-enable_http`
  - Upstream DSS itself documents newer flags:
    - `atc-stack/interuss-dss/cmds/core-service/main.go:45`–`50` (`allow_http_base_urls`, deprecated `enable_http`, `public_endpoint`)
    - `atc-stack/interuss-dss/NEXT_RELEASE_NOTES.md:56`–`66` (migration notes: datastore_* flag rename, `public_endpoint` mandatory, new `aux` schema)
- Why it matters:
  - The moment you bump DSS images, the stack may stop booting (missing required flags) or start with silent behavior changes (deprecated flags).
  - It also makes audit/ops harder because “the right flags” differ between docs and this Compose.
- Fix:
  - Update Compose to match current DSS CLI:
    - replace `-cockroach_host ...` with the `--datastore_*` flags expected by modern DSS,
    - replace `-enable_http` with `-allow_http_base_urls` (dev-only),
    - include `-public_endpoint http://...` even for dev so upgrades don’t immediately break.
  - When upgrading DSS beyond versions that predate AUX support, add an AUX schema migration job (`-schemas_dir=/db-schemas/aux_`) and validate the pool metadata endpoint.
- Verify:
  - Bumping DSS to a newer image tag does not require rework beyond updating the explicit pinned schema versions, and logs contain **no** “deprecated flag” warnings.

F-DSS-007 — **P1 / Reproducibility (FIXED)**: The stack mixes DSS *runtime* artifacts (pinned upstream images) with *source* (submodule) in a way that can drift
- Where:
  - DSS core and schema bootstrap jobs use prebuilt images: `atc-stack/docker-compose.yml:220`–`277` (`image: interuss/dss:v0.15.0`)
  - Dummy OAuth is built from the local `interuss-dss` submodule: `atc-stack/docker-compose.yml:278`–`289` (`build: context: ./interuss-dss`)
  - Submodule wiring: `atc-stack/.gitmodules` (points `interuss-dss` at `https://github.com/interuss/dss.git`)
- Why it matters:
  - You can end up with a dummy OAuth binary built from one DSS commit while the DSS core-service you actually run comes from a different published image (different behavior/flags/claims).
  - When someone reads this repo to audit the DSS code, they may be auditing source that is **not** what’s running in Compose.
- Fix:
  - Enforced a single version pin across both runtime images and source:
    - `docker-compose.yml` now uses `interuss/dss:${DSS_IMAGE_TAG}` for all DSS runtime containers.
    - The `interuss-dss` submodule is pinned to the matching upstream release tag (`interuss/dss/v${DSS_IMAGE_TAG#v}`) so building dummy OAuth uses the same DSS release as the runtime images.
  - Added `tools/check_dss_pin.sh` which fails fast if the DSS image tag and submodule tag drift.
- Verify:
  - `./tools/check_dss_pin.sh` passes.
  - `docker compose config` shows a single, consistent DSS version strategy, and the stack boots cleanly after `git clone --recurse-submodules`.

F-DSS-008 — **P1 / Ops + Data hygiene**: No DSS eviction/cleanup job is configured → expired RID/SCD entries can accumulate indefinitely
- Where:
  - There is no Compose service running `db-manager evict` on a schedule in `atc-stack/docker-compose.yml`.
  - DSS provides an eviction tool: `atc-stack/interuss-dss/cmds/db-manager/cleanup/README.md:1` (instructions and flags).
- Why it matters:
  - Even in “demo” mode, long-running stacks accumulate expired RID ISAs/subscriptions and SCD objects, which can degrade performance and complicate debugging.
  - In any real deployment, predictable data retention is part of reliability.
- Fix:
  - Add an eviction mechanism appropriate to your deployment:
    - Kubernetes: a CronJob running `db-manager evict` with pinned TTLs,
    - Docker Compose: a periodic job container (or document a host cron calling `docker compose exec ... db-manager evict`).
  - Start with “list-only” mode; enable `--delete` only after confirming results.
- Verify:
  - A long-running (24h+) deployment does not show unbounded DB growth, and old entries disappear after the configured TTL.

F-DSS-009 — **P2 / Verification**: No automated DSS interoperability checks (prober / USS qualifier) are integrated into this stack
- Where:
  - DSS pooling docs explicitly recommend verifying deployments with InterUSS monitoring tools:
    - `atc-stack/interuss-dss/docs/operations/pooling-crdb.md:104` (prober + USS qualifier)
    - `atc-stack/interuss-dss/docs/operations/pooling.md:198`–`201` (prober + USS qualifier)
  - This stack has no scripts/CI that run those checks against `local-dss-core`.
- Why it matters:
  - DSS is a “coordination backbone” dependency. If it’s misconfigured (audience, keys, time sync, DB TLS, pooling), the whole UTM interoperability story silently degrades.
  - For a “near-launch” posture you want at least one automated DSS sanity pass you can run before demos/releases.
- Fix:
  - Add a `tools/dss_check.sh` (or CI job) that runs the official InterUSS prober/qualifier against the configured DSS endpoints and fails if basic scenarios don’t pass.
  - For production: include this as a release gate when upgrading DSS schemas/images.
- Verify:
  - The DSS check is runnable in CI and/or locally and produces a pass/fail artifact you can attach to releases.

F-DSS-010 — **P1 / Security + Config hardening (FIXED)**: DSS auth/audience configuration is easy to misconfigure; “missing accepted audiences” becomes “accept tokens without aud”
- Where:
  - DSS core warns but does not fail fast when audiences are missing:
    - `atc-stack/interuss-dss/cmds/core-service/main.go:237`–`241` (`logger.Warn("missing required --accepted_jwt_audiences")`)
  - Audience enforcement accepts tokens with empty audience when configured with `[""]`:
    - `atc-stack/interuss-dss/pkg/auth/auth.go:134`–`135` (comment: empty string allows no aud claim)
    - `atc-stack/interuss-dss/pkg/auth/auth.go:208`–`210` (aud check)
- Why it matters:
  - In a production DSS deployment, *audience* is one of the main guardrails preventing token replay across ecosystems.
  - If you accidentally omit `accepted_jwt_audiences`, it’s possible to accept tokens with no `aud` claim (depending on issuer behavior), weakening auth.
- Fix:
  - The stack now treats `accepted_jwt_audiences` as mandatory (even in the local sandbox):
    - `docker-compose.yml` sets `-accepted_jwt_audiences` from `DSS_ACCEPTED_JWT_AUDIENCES` (with a safe default),
    - startup guard refuses to boot `local-dss-core` if audiences are empty or contain an empty entry (no leading/trailing comma, no `,,`).
  - For real deployments: enforce real JWT issuance with correct `aud` in your OIDC provider.
- Verify:
  - Negative test: set `DSS_ACCEPTED_JWT_AUDIENCES=` → `local-dss-core` refuses to start.
  - Negative test: set `DSS_ACCEPTED_JWT_AUDIENCES=,` or `DSS_ACCEPTED_JWT_AUDIENCES=a,,b` → refuses to start.

F-DSS-011 — **P1 / Reliability**: JWKS refresh failure panics and can take down the DSS (availability risk if using JWKS)
- Where:
  - Key refresh worker panics on refresh error:
    - `atc-stack/interuss-dss/pkg/auth/auth.go:157`–`174` (`logger.Panic("failed to refresh key", ...)`)
  - JWKS usage is supported as a key source:
    - `atc-stack/interuss-dss/cmds/core-service/main.go:94`–`103` (`auth.JWKSResolver`)
- Why it matters:
  - In production you typically verify tokens via a JWKS endpoint. Temporary network/DNS issues should not crash the entire service.
  - Panics turn transient control-plane blips into full outages.
- Fix:
  - In a hardened deployment:
    - prefer static public key files if appropriate, or ensure JWKS endpoint is HA and network-reachable.
  - In a hardened fork/upstream PR:
    - on refresh error, log and keep the prior keys; retry on next tick, and add alerting/metrics instead of panicking.
- Verify:
  - Simulate JWKS endpoint outage; DSS remains up and continues validating tokens with previously cached keys (or rejects new tokens while staying healthy, depending on policy).

### 5.4 `terrain-api` + Overpass + offline datasets

**What it is**
- Local services providing DEM elevation and OSM-derived obstacle/building footprints.

**Open risks / work**
- Safety semantics for missing DEM tiles: missing tiles must not silently return “0m” as “safe”.
- Rate limiting / bounding: cap `max_points` and enforce request pacing (some already exists via env).
- Overpass: ensure DB directory is persistent and that “init” operations do not clobber a running instance.

### 5.5 `mavlink-gateway` (Autopilot bridge, LTE reality)

**What it is**
- Sends telemetry to ATC and polls ATC for commands; executes HOLD/RESUME/REROUTE/ALTITUDE_CHANGE.

**Already fixed/guarded**
- TLS insecure mode is guarded: refuses to start in production with `ATC_TLS_INSECURE=1`.

**Open risks / work**
- LTE flaps: add exponential backoff + jitter to avoid thundering herd retries.
- Command expiry handling: do not execute stale reroutes after long comms gaps.
- Altitude reference correctness end-to-end (MSL/AMSL/geoid offsets) must be verified before autonomy.

### 5.6 Safety Assurance & Validation Coverage (Missing)

**Why this matters:** current findings list correctness risks (sampling-based geometry/CPA, AGL ambiguity, timestamp normalization), but there is **insufficient verification evidence** that the safety logic is correct under worst‑case conditions. The gaps below are about *how to prove correctness* and *where in the codebase to anchor those proofs*.

**Open risks / work (specific, code‑anchored):**
- **Geometry correctness now has a non-sampling implementation, but still needs stronger proof:**  
  - `Geofence::intersects_segment` is now exact (no sampling) and has unit tests in `atc-drone/crates/atc-core/src/models.rs`.
  - `segment_to_segment_distance` now detects crossings and has a unit test in `atc-drone/crates/atc-core/src/spatial.rs`.
  Recommended: add property‑based tests (and optionally cross-check against a reference geometry crate like `geo`) in a dedicated `atc-drone/crates/atc-core/tests/geometry.rs`.
- **Conflict CPA verification remains incomplete (even though the 1s sampling is removed):**  
  - `atc-drone/crates/atc-core/src/conflict.rs` now uses a continuous-time CPA model and includes a regression test (`detects_near_miss_between_whole_seconds`).
  - Recommended: add integration coverage in `atc-drone/crates/atc-server/tests/conflict_test.rs` plus property-based fuzzing of relative-motion cases (including near-zero relative velocities, high-speed passes, and vertical-only convergences).
- **Telemetry time semantics need explicit verification:** `receive_telemetry` in `atc-drone/crates/atc-server/src/api/routes.rs` validates client timestamps against `ATC_TELEMETRY_MAX_FUTURE_S` / `ATC_TELEMETRY_MAX_AGE_S`, then stamps server receipt time to avoid trusting client clocks. Add API-level tests (e.g., `atc-drone/crates/atc-server/tests/telemetry_test.rs` or `src/api/tests.rs`) for too-old/too-far-future timestamps and confirm `last_update` behaves as intended.
- **Altitude reference (AGL/AMSL) lacks end‑to‑end tests:** conversion happens in `atc-drone/crates/atc-server/src/altitude.rs`, `state/store.rs`, and `route_planner.rs`. Add unit tests for conversion and integration tests that inject terrain to validate AGL ceilings (route planner + compliance).
- **Scenario regression harness is not tied to safety outcomes:** leverage `atc-drone/crates/atc-cli` scenarios plus `tools/e2e_demo.sh` to define deterministic safety scenarios (conflict prediction, geofence intersections, AGL violations) and gate them in CI.
- **CI safety gates are absent:** add CI workflows (per repo) to run the above tests, `cargo test --all`, and the ignored integration tests (`atc-drone/crates/atc-server/tests/*`). Save artifacts (logs + JSON outputs) as evidence for safety review.

### 5.7 External Audit Claims Not Merged (Resolved / Not Found)

- **Geofence CRUD public**: now admin‑authenticated in `atc-drone` routes.
- **RID view update public**: now admin‑authenticated in `atc-drone` routes.
- **`eval()` on Redis track data**: replaced with JSON + `ast.literal_eval` fallback.
- **Terrain API returns `0.0` for missing**: current `terrain-api` returns `None` for missing/nodata.
- **Blender client panics on HTTP client creation**: current clients use `Client::builder().timeout(...).unwrap_or_else(Client::new)` (no panic).

---

## 8) Launch Gates & Roadmap (P0–P3)

### 8.1 Launch Gates (definition of “damn near ready to ship”)

This section is the **release checklist**. If (and only if) every gate below is satisfied (with evidence), then the system is “damn near ready to launch” as a **software** product (excluding the unfinished hardware MAVLink/autopilot implementation).

#### Gate A — P0 Safety (software-only)

Pass criteria (must all be true):
- **Continuous CPA** (no 1s “miss between samples”): close **F-DRONE-001**
- **Robust geofence geometry** (no sampling as a safety gate): close **F-DRONE-002**
- **Correct segment/segment distance** (no “X crossing missed”): close **F-DRONE-003**
- **Altitude semantics are explicit + AGL works end-to-end** (or fails safe when terrain missing): close **F-DRONE-004**, **F-DRONE-020**
- **Telemetry time semantics are correct** (stale telemetry can’t look fresh): close **F-DRONE-005**, **F-DRONE-027**
- **Autonomous reroutes are validated** (geofence/terrain/obstacles) or fall back safely to HOLD: close **F-DRONE-018**, **F-DRONE-023**
- **(If used) Blender surveillance tracks are not mirrored / unit-broken**: close **F-BLENDER-001**

Evidence required:
- Unit + property tests for geometry/CPA (see **5.6**)
- Deterministic end-to-end safety scenarios (CLI + `tools/e2e_demo.sh`) gated in CI (see **5.6**)
- Explicit altitude reference documentation for every API payload (telemetry + plans) and at least one integration test proving AGL ceilings

#### Gate B — P0 Security (internet exposure)

Pass criteria (must all be true):
- **No public operational “read” endpoints** leaking live ops data unless explicitly intended and anonymized: close **F-DRONE-006**
- **Request body limits + input caps** exist at the API edge: close **F-DRONE-007**
- **Placeholder/default secrets are rejected in prod** (admin/ws/registration + Blender `DJANGO_SECRET_KEY`): close **F-DRONE-013**, **F-BLENDER-018**
- **Owner/tenant spoofing is impossible** (telemetry cannot mutate ownership): close **F-DRONE-021**
- **WebSocket cannot silently become public** in prod configs: close **F-DRONE-025**
- **Frontend XSS is closed** (planner + mission detail + inline handlers) and CSP is tightened accordingly: close **F-FRONTEND-001**, **F-FRONTEND-002**, **F-FRONTEND-016**, **F-FRONTEND-017**
- **Default users/guest login fail closed** in prod: close **F-FRONTEND-005**
- **DSS is not shipped in insecure demo posture** (dummy OAuth/test keys/insecure DB are dev-only): close **F-DSS-001**, **F-DSS-004**

Evidence required:
- End-to-end XSS regression tests (at least one malicious `drone_id` + one malicious mission id URL)
- CI check that fails if any “change-me” placeholder secrets are present in prod profile
- A documented “prod exposure map” (which ports/routes are exposed) with `docker compose config` output or k8s manifests

#### Gate C — P0 Ops / Reliability (can run unattended)

Pass criteria (must all be true):
- **No host-destructive helper scripts** are present/enabled: close **F-BLENDER-006**
- **Blender is deployable with `IS_DEBUG=0`** without BadHost surprises (ALLOWED_HOSTS explicit): close **F-BLENDER-019**
- **DB failure backoff exists** for write-heavy loops (no log/CPU storms): close the related loop findings in `atc-drone` (see **8.2 P0 #6**)
- **All network clients have timeouts + sane retries** (SDK, Blender auth, DSS/USS calls): close **F-DRONE-016**, **F-DRONE-017**, and Blender network-call hardening findings like **F-BLENDER-015**
- **Data retention/cleanup** exists where required (telemetry retention; DSS eviction if self-hosted): close **F-DSS-008** and add a retention policy for ATC telemetry tables

Evidence required:
- A 24h soak test (local or staging) with metrics/log review: no unbounded growth, no crash loops, stable CPU/mem
- A “prod profile” compose/k8s config that disables dev-only reload modes and removes insecure services/ports

#### Gate D — P1 Verification / Release Process (you can prove it works)

Pass criteria (must all be true):
- CI runs lint/format/test gates across Rust + Python + Node (see **5.6** + roadmap **8.2 P2**)
- Ignored integration tests are executed in CI (or replaced by an equivalent harness)
- DSS interoperability checks exist if DSS is part of the shipped topology: close **F-DSS-009**

Evidence required:
- CI artifacts: test reports + logs + (ideally) scenario JSON outputs attached to releases

### 8.2 Correction Roadmap (P0–P3) — With Status

Legend:
- **DONE** = implemented and verified in this repo
- **TODO** = not done yet
- **DEFER** = explicitly deferred (must be acknowledged in ship decision)

### P0 (must fix before any production exposure)

1) Lock down backend mutation endpoints — **DONE**
   - `atc-drone/crates/atc-server/src/api/routes.rs`
   - `atc-frontend/server.js` token injection + role gate

2) Fail closed on placeholder/shared-secret defaults in production — **DONE**
   - `atc-drone/crates/atc-server/src/main.rs` now rejects placeholder admin/registration/WS tokens in non-development.
   - Keep `.env.example` demo-only; production deploy docs must require replacing placeholders (still recommended).

3) Fail closed on default users / guest login in production — **DONE**
   - `atc-frontend/server.js` now fails closed in prod and disables guest one-click login.
   - `atc-frontend/util/user-store.js` adds `countUsers()` to support “DB empty” bootstrap checks.

4) Stop per-request JWKS fetch in Blender auth (cache + TTL + backoff) — **DONE**
   - `atc-blender/auth_helper/utils.py` now caches JWKS (TTL + backoff) and uses a single forced refresh only when `kid` is missing.
   - `atc-blender/tests/test_jwks_cache.py` covers TTL caching + backoff behavior.
   - Note: issuer allowlist validation is now **DONE** (see **F-BLENDER-002**).

5) Separate Django secret from JWT signing key — **DONE**
   - `atc-blender/flight_blender/settings.py` now uses `DJANGO_SECRET_KEY` for Django and `OIDC_SIGNING_PRIVATE_KEY_PEM` for JOSE/JWKS.
   - `atc-blender/flight_feed_operations/views.py` `/signing_public_key` now serves `OIDC_SIGNING_PUBLIC_JWKS` derived from the signing key, not Django secret.
   - `atc-blender/flight_feed_operations/pki_helper.py` `sign_json_via_jose` uses `OIDC_SIGNING_PRIVATE_KEY_PEM`.

6) DB-failure backoff for write-heavy loops — **DONE**
   - `atc-drone/crates/atc-server/src/loops/telemetry_persist_loop.rs` uses `Backoff` to avoid tight retry loops when DB writes fail.
   - `atc-drone/crates/atc-server/src/loops/operational_intent_expiry_loop.rs` uses `Backoff` to avoid tight retry loops when DB operations fail.

7) Drone token rotation / recovery flow — **DONE**
   - `atc-drone` exposes `POST /v1/admin/drones/:drone_id/token/rotate` (admin-auth) to rotate a drone session token.
   - `atc-drone/crates/atc-sdk` adds `AtcClient::rotate_drone_token_admin` to consume the endpoint (gateway/client-side wiring still required where applicable).

8) Fail closed on placeholder secrets when not debug — **DONE**
   - `atc-blender/flight_blender/settings.py` now rejects placeholder/weak `DJANGO_SECRET_KEY` values when `IS_DEBUG=0` (including any containing `change-me`, and short secrets).
   - `.env.example` stays demo-only but non-debug runtime fails fast if placeholders are used.

9) Geometry correctness (no sampling shortcuts in safety checks) — **DONE**
   - `Geofence::intersects_segment` now uses exact segment–polygon intersection (local ENU) plus altitude overlap clipping (no sampling).
   - `segment_to_segment_distance` now detects true crossings (distance=0 on intersection).
   - Unit tests added in `atc-drone/crates/atc-core/src/models.rs` and `atc-drone/crates/atc-core/src/spatial.rs`.

10) Conflict prediction must be continuous (CPA) — **DONE**
   - `atc-drone/crates/atc-core/src/conflict.rs` now uses an analytic constant-velocity CPA model (no 1-second sampling).
   - Regression test added: `detects_near_miss_between_whole_seconds`.

11) Altitude reference correctness (AGL support) — **DONE**
   - `atc-drone/crates/atc-server/src/api/altitude_validation.rs` enforces SafetyRules altitude bands as AGL when `ATC_TERRAIN_REQUIRE=1` (fails closed on terrain fetch failure).
   - `atc-drone/crates/atc-server/src/route_planner.rs` now sets `RouteEngineConfig::faa_limit_agl = state.rules().max_altitude_m` (removes hardcoded `500.0`).
   - Note: `ATC_ALTITUDE_REFERENCE` remains WGS84/AMSL input conversion; AGL is enforced via terrain-derived limits.

12) Telemetry timestamp handling — **DONE**
   - `atc-drone/crates/atc-server/src/api/routes.rs` now validates client timestamps and no longer “normalizes to now” before validation.
   - Stored `last_update` uses server receipt time after validation, so timeouts do not trust client clocks.

13) Lock down operational data exposure — **DONE**
   - `atc-drone` now protects `/v1/drones`, `/v1/traffic`, `/v1/conflicts`, `/v1/conformance`, `/v1/daa`, `/v1/flights`, and `/v1/ws` behind `require_admin`.
   - `atc-frontend/server.js` now sends `ATC_ADMIN_TOKEN` on those reads and the WS proxy upstream.

14) Add request body limits + input caps — **DONE**
   - `atc-drone/crates/atc-server/src/main.rs` applies a global body limit layer (`DefaultBodyLimit`) to reject oversized JSON payloads.
   - `/v1/geofences/check-route` is now admin-authenticated, rate-limited, and validates waypoint counts + numeric ranges.

15) Ownership/tenancy correctness (`owner_id` must not be telemetry-writable) — **DONE**
   - `atc-drone/crates/atc-server/src/state/store.rs` `update_telemetry` ignores telemetry-provided `owner_id` and uses the server-owned value.
   - Regression test: `atc-drone/crates/atc-server/src/api/tests.rs` `telemetry_cannot_spoof_owner_id`.

16) Conflict/conformance reroute safety fallback — **DONE**
   - Conflict loop: if planning fails, issue `HOLD` (no unvalidated reroute) — see **F-DRONE-018**
   - Conformance loop: “exit geofence” behavior now plans a validated exit route or `HOLD` — see **F-DRONE-019**

### P1 (should fix before “real users” / external pilots)

- Frontend brute-force throttling — **TODO**
- WebSocket Origin enforcement (CSWSH) — **TODO**
- Remove WS query-param tokens (use headers/proxy-only) — **DONE** (see **F-DRONE-008**)
- Route engine hard caps defense-in-depth — **DONE** (see **F-DRONE-030**)
- Flight-plan conflict duration correctness (avoid fixed fallback) — **TODO**
- Gateway: backoff + command expiry — **TODO**
- Logout should be POST + CSRF-protected — **TODO**
- Rate-limit or auth‑gate `/v1/geofences/check-route` — **DONE** (moved to P0)
- Reduce public exposure of operational data endpoints — **DONE** (moved to P0)
- Add HTTP client timeouts across SDK/Blender/Compliance callers — **TODO**
- Persist flight plan status transitions (mission loop) — **DONE** (see **F-DRONE-011**)
- Enforce `ATC_COMMAND_ACK_TIMEOUT_SECS > 0` and cap pending commands per drone — **DONE** (see **F-DRONE-014**)

### P2 (ops readiness)

- Request-ID propagation everywhere — **TODO** (partial)
- CI gates: Rust fmt/clippy/test; Python compile/tests; Node lint/smoke — **TODO**
- CI should run ignored integration tests (telemetry/conflict/geofence) — **TODO**
- Safety validation suite: geometry/CPA/AGL property tests in `atc-core` + API regressions in `atc-server` — **TODO**
- Deterministic safety scenario regression harness (CLI + `tools/e2e_demo.sh`) — **TODO**
- Secrets scanning allowlist for test/vendor cert keys — **TODO**

### P3 (quality / maintainability)

- Eliminate magic numbers into shared constants/helpers — **TODO**
- Reduce O(N) polling loops and full scans in hot paths — **TODO**

---

## 9) Next Step

Resume the **timed** audit where we left off:
- `python tools/timed_audit_clock.py start atc-frontend`
- `python tools/timed_audit_clock.py start atc-blender`
- `python tools/timed_audit_clock.py start interuss-dss`

Then:
- Run the repo sync check (dirty submodules, commits, pushes) so all 4 repos are actually in a consistent state.
