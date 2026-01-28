# ATC Stack — Consolidated Level 6 Engineering Audit (Canonical)

Last updated: 2026-01-28 (local)

This is the **single canonical** audit document for this workspace. Previous audit artifacts were consolidated into this file to eliminate “audit sprawl”.

Hardware integration (autopilot tuning, LTE modem install, field ops) is out of scope here; this audit covers **software** and **deployment posture**.

---

## 0) Ship Decision (TL;DR)

**Not ship-ready for production internet exposure** until remaining **P0 items** are closed (see section **7**) and the timed audit completes across all subsystems.

Already-fixed safety/security issues:
- Backend geofence CRUD + RID view are now **admin-authenticated** (previously public mutation endpoints).
- Django/Redis stream parsing no longer uses Python `eval()` (RCE class issue).
- MAVLink gateway refuses `ATC_TLS_INSECURE=1` when `ATC_ENV=prod|production`.

---

## 1) What’s In This Repo (System Inventory)

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

## 2) Architecture (What Talks To What)

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
  - We do not advance to the next subsystem until `elapsed >= 01:30:00`.

3) **Targeted verification**
- Run/lint/tests where feasible without disrupting the running stack.

---

## 5) Current Timed Audit Progress (Ground Truth)

Run:

```bash
python tools/timed_audit_clock.py status
```

As of the last pause before this edit:
- `atc-drone`: **00:12:35 / 01:30:00** (in progress)

---

## 6) Findings (Structured, By Subsystem)

### 6.0 Strengths & Completeness (validated via external audits)

- **Coherent Rust workspace boundaries**: `atc-core` (domain logic), `atc-server` (API/loops), `atc-sdk`, `atc-cli`, plus integration client crates are separated cleanly.
- **API hygiene**: versioned `/v1` routes and a machine‑readable OpenAPI spec (`openapi.yaml`).
- **Operational loop design**: explicit background loops with health/readiness checks (not just spawn-and-forget).
- **Command system**: commands expire, are acked, and are streamed over WS; SDK supports polling/WS.
- **Route planner depth**: A* planner with obstacle/terrain integration plus post‑processing (smoothing / corridor tooling).
- **Control Center security baseline**: session-based auth + CSRF (server-side), CSP with nonces (scoped), WS proxy.

### 6.1 `atc-drone` (Rust backend + core algorithms)

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

**Open risks / work**
- Conflict prediction uses 1‑second discrete sampling; can miss true closest‑approach between samples at higher relative speeds.
- Geofence `intersects_segment` uses sampling (25m step, max 200 steps); long legs can miss narrow geofences.
- `segment_to_segment_distance` checks only endpoints vs segments; crossing segments can report non‑zero distance.
- Telemetry timestamps are normalized to server time before validation; stale/future telemetry can appear “fresh.”
- `ConflictSeverity::Info` exists but is not emitted by the predictor (Warning/Critical only).
- `ATC_ALTITUDE_REFERENCE=AGL` is explicitly unsupported and falls back to AMSL; safety rules compare altitude directly (Part 107 is AGL).
- `/v1/geofences/check-route` is public and not rate‑limited; accepts arbitrary waypoint counts.
- Public read endpoints expose operational data (`/v1/traffic`, `/v1/conflicts`, `/v1/daa`, `/v1/drones`).
- WS token can be passed via query param (leak‑prone; prefer Authorization header/cookie).
- WS broadcast uses a bounded channel; lagged subscribers drop messages (no replay).
- Command IDs are truncated to 8 chars of UUID (collision risk at scale).
- No explicit HTTP body size limits on the API layer (DoS surface).
- Blender OAuth client uses `reqwest::Client::new()` (no timeouts); can hang critical loops.
- Compliance HTTP client falls back to `Client::new()` on builder failure (loses timeout).
- SDK client uses `Client::new()` (no timeouts) and does not enforce HTTPS in production.
- Compliance models (population/obstacles/weather/battery) are heuristic, not validated safety models.
- DB contention handling: some loops can create retry/log storms when DB is unhealthy.
- Strategic scheduling uses a single DB lock (no distributed coordination); horizontal scale needs a leader/lock service.
- Drone token lifecycle: rotation/recovery flow is incomplete (gateway “re-register” on 401/403 collides with 409 behavior).
- DoS limits: ensure route planning caps are enforced in both API and algorithm layers.
- Async blocking & global locks: review `store.rs` locking + CPU-heavy conflict work (timed audit is still in-progress here).

### 6.2 `atc-frontend` (Node UI + proxy)

**What it is**
- User-facing console (sessions + CSRF) plus HTTP/WS proxy to backend and Blender.

**Already fixed**
- Proxy injects admin token only for specific admin operations and requires `authority`:
  - Geofence CRUD + `POST /v1/rid/view`

**Open risks / work**
- “Default users” / guest login must fail closed in production.
- WebSocket Origin hardening (CSWSH) should be explicit.
- Login throttling / brute force defense.
- CSP currently allows `style-src 'unsafe-inline'` (and inline script attributes); weakens XSS posture.

### 6.3 `atc-blender` + `interuss-dss` (Django + DSS sandbox)

**What it is**
- “Adapter” tier translating ATC concepts into Blender/DSS flows.

**Already fixed**
- Removed Python `eval()` on Redis-loaded state (replaced with JSON + safe fallback parsing).

**Open risks / work**
- JWKS fetching/caching: avoid per-request external fetch patterns (availability dependency + amplification).
- Key material clarity: separate Django `SECRET_KEY` from JWT signing key(s).
- Fail closed on placeholder secrets when not in debug.
- Ensure DSS schema/boot is stable under the selected CockroachDB version and migrations (avoid “backfill” footguns).
- Blocking `time.sleep()` calls inside Django views (RID/USS paths) can tie up workers and create DoS risk.
- Assertions used as request validation in views/helpers can be disabled with `-O` (validation can disappear).
- Redis `KEYS` usage for session/track enumeration is O(N); replace with `SCAN`.
- Entry points use `uvicorn --reload` (dev‑only) in normal runs; should be split for prod.
- `start_flight_blender.sh` removes **all** Docker containers/volumes on the host (unsafe helper).
- Longitude is negated in surveillance track generation (`lng = -lon_dd`), mirroring tracks across the prime meridian.

### 6.4 `terrain-api` + Overpass + offline datasets

**What it is**
- Local services providing DEM elevation and OSM-derived obstacle/building footprints.

**Open risks / work**
- Safety semantics for missing DEM tiles: missing tiles must not silently return “0m” as “safe”.
- Rate limiting / bounding: cap `max_points` and enforce request pacing (some already exists via env).
- Overpass: ensure DB directory is persistent and that “init” operations do not clobber a running instance.

### 6.5 `mavlink-gateway` (Autopilot bridge, LTE reality)

**What it is**
- Sends telemetry to ATC and polls ATC for commands; executes HOLD/RESUME/REROUTE/ALTITUDE_CHANGE.

**Already fixed/guarded**
- TLS insecure mode is guarded: refuses to start in production with `ATC_TLS_INSECURE=1`.

**Open risks / work**
- LTE flaps: add exponential backoff + jitter to avoid thundering herd retries.
- Command expiry handling: do not execute stale reroutes after long comms gaps.
- Altitude reference correctness end-to-end (MSL/AMSL/geoid offsets) must be verified before autonomy.

### 6.6 Safety Assurance & Validation Coverage (Missing)

**Why this matters:** current findings list correctness risks (sampling-based geometry/CPA, AGL ambiguity, timestamp normalization), but there is **insufficient verification evidence** that the safety logic is correct under worst‑case conditions. The gaps below are about *how to prove correctness* and *where in the codebase to anchor those proofs*.

**Open risks / work (specific, code‑anchored):**
- **Geometry correctness lacks proof:** add unit + property‑based tests targeting  
  - `Geofence::intersects_segment` (sampling) in `atc-drone/crates/atc-core/src/models.rs`  
  - `segment_to_segment_distance` in `atc-drone/crates/atc-core/src/spatial.rs`  
  Recommended: create `atc-drone/crates/atc-core/tests/geometry.rs` with crossing‑segment cases, narrow‑polygon misses, and a test‑only reference implementation (e.g., `geo` crate) for cross‑checking.
- **Conflict CPA correctness not validated:** `atc-drone/crates/atc-core/src/conflict.rs` currently samples 1‑second steps. When replacing with analytic CPA or adaptive sampling, add regression tests in `conflict.rs` plus integration coverage in `atc-drone/crates/atc-server/tests/conflict_test.rs` for “near‑miss between samples” scenarios.
- **Telemetry time semantics not verified:** `normalize_telemetry_timestamp` in `atc-drone/crates/atc-server/src/api/routes.rs` mutates out‑of‑bounds timestamps. Add API‑level tests in `atc-drone/crates/atc-server/tests/telemetry_test.rs` (or `src/api/tests.rs`) to assert rejection/flagging behavior once normalization is removed.
- **Altitude reference (AGL/AMSL) lacks end‑to‑end tests:** conversion happens in `atc-drone/crates/atc-server/src/altitude.rs`, `state/store.rs`, and `route_planner.rs`. Add unit tests for conversion and integration tests that inject terrain to validate AGL ceilings (route planner + compliance).
- **Scenario regression harness is not tied to safety outcomes:** leverage `atc-drone/crates/atc-cli` scenarios plus `tools/e2e_demo.sh` to define deterministic safety scenarios (conflict prediction, geofence intersections, AGL violations) and gate them in CI.
- **CI safety gates are absent:** add CI workflows (per repo) to run the above tests, `cargo test --all`, and the ignored integration tests (`atc-drone/crates/atc-server/tests/*`). Save artifacts (logs + JSON outputs) as evidence for safety review.

### 6.7 External Audit Claims Not Merged (Resolved / Not Found)

- **Geofence CRUD public**: now admin‑authenticated in `atc-drone` routes.
- **RID view update public**: now admin‑authenticated in `atc-drone` routes.
- **`eval()` on Redis track data**: replaced with JSON + `ast.literal_eval` fallback.
- **Terrain API returns `0.0` for missing**: current `terrain-api` returns `None` for missing/nodata.
- **Blender client panics on HTTP client creation**: current clients use `Client::builder().timeout(...).unwrap_or_else(Client::new)` (no panic).

---

## 7) Correction Roadmap (P0–P3) — With Status

Legend:
- **DONE** = implemented and verified in this repo
- **TODO** = not done yet
- **DEFER** = explicitly deferred (must be acknowledged in ship decision)

### P0 (must fix before any production exposure)

1) Lock down backend mutation endpoints — **DONE**
   - `atc-drone/crates/atc-server/src/api/routes.rs`
   - `atc-frontend/server.js` token injection + role gate

2) Fail closed on default users / guest login in production — **TODO**
   - `atc-frontend/server.js`

3) Stop per-request JWKS fetch in Blender auth (cache + TTL + backoff) — **TODO**
   - `atc-blender/auth_helper/utils.py`

4) Separate Django secret from JWT signing key — **TODO**
   - `atc-blender/flight_blender/settings.py`
   - `atc-blender/flight_feed_operations/views.py`

5) DB-failure backoff for write-heavy loops — **TODO**
   - `atc-drone/crates/atc-server/src/loops/telemetry_persist_loop.rs`
   - `atc-drone/crates/atc-server/src/loops/operational_intent_expiry_loop.rs`

6) Drone token rotation / recovery flow — **TODO**
   - `atc-drone` + `mavlink-gateway`

7) Fail closed on placeholder secrets when not debug — **TODO**
   - `atc-blender/flight_blender/settings.py`
   - `.env.example` stays demo-only but runtime must reject placeholders in non-debug

8) Geometry correctness (no sampling shortcuts in safety checks) — **TODO**
   - Replace `Geofence::intersects_segment` sampling with exact segment–polygon intersection
   - Fix `segment_to_segment_distance` to detect crossing segments

9) Conflict prediction must be continuous (CPA) — **TODO**
   - Replace 1‑second sampling with analytic CPA or adaptive sampling

10) Altitude reference correctness (AGL support) — **TODO**
   - AGL/AMSL ambiguity must be resolved end‑to‑end before real ops

11) Telemetry timestamp handling — **TODO**
   - Stop normalizing out‑of‑range timestamps to “now”; treat as invalid or degraded

### P1 (should fix before “real users” / external pilots)

- Frontend brute-force throttling — **TODO**
- WebSocket Origin enforcement (CSWSH) — **TODO**
- Route engine hard caps defense-in-depth — **TODO**
- Flight-plan conflict duration correctness (avoid fixed fallback) — **TODO**
- Gateway: backoff + command expiry — **TODO**
- Logout should be POST + CSRF-protected — **TODO**
- Rate-limit or auth‑gate `/v1/geofences/check-route` — **TODO**
- Reduce public exposure of operational data endpoints — **TODO**
- Add HTTP client timeouts across SDK/Blender/Compliance callers — **TODO**

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

## 8) Next Step (What I Will Do When You Say “Go”)

Resume the **timed** audit where we left off:
- `python tools/timed_audit_clock.py start atc-drone --note \"resume after audit restructuring\"`
- Finish the remaining `atc-drone` deep-read (loops + store concurrency + persistence edge cases).
