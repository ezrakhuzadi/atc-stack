# MAVLink Gateway (Drone Integration)

This document is for **drone integrators** who want to connect a real MAVLink autopilot (ArduPilot/PX4) to the ATC stack.

The `mavlink-gateway` runs on a companion computer (Raspberry Pi, Jetson, etc.) or a ground/SITL machine. It bridges:

- **MAVLink ⇄ autopilot** (serial / UDP)
- **HTTP ⇄ `atc-drone`** (telemetry + commands)

> The gateway does **not** replace the flight controller. It sends high-level targets (lat/lon/alt + mode changes). The autopilot decides **how** to fly there.

---

## What it does

### Telemetry uplink (autopilot → ATC)

- Reads MAVLink telemetry and forwards it to `atc-drone`:
  - `POST /v1/telemetry` (Bearer token)

### Command downlink (ATC → autopilot)

- Polls commands from `atc-drone` and acknowledges them:
  - `GET /v1/commands/next?drone_id=...`
  - `POST /v1/commands/ack`
- Translates ATC commands into MAVLink actions:
  - mode changes (best-effort)
  - position targets (lat/lon/alt)

---

## Architecture

```
Autopilot (PX4/ArduPilot)
        ^
        | MAVLink (serial/udp)
        v
  mavlink-gateway  <--- HTTP --->  atc-drone
```

---

## Setup

### Prereqs (ATC side)

- `atc-drone` reachable from the gateway machine
- Auth via **either**:
  - `ATC_SESSION_TOKEN` (preferred: per-drone token), **or**
  - `ATC_REGISTRATION_TOKEN` (gateway registers and mints a session token)

### Prereqs (autopilot side)

- MAVLink enabled and reachable via `MAVLINK_ENDPOINT`
- A mode available that can accept position targets:
  - ArduPilot: typically `GUIDED`
  - PX4: typically `OFFBOARD`
- Your normal failsafes configured (RC loss, RTL, geofence, battery, etc.)

---

## Quickstart (host run)

```bash
python3 -m pip install -r requirements.txt

export ATC_SERVER_URL="http://your-atc-host:3000"
export ATC_DRONE_ID="DRONE0001"
export ATC_REGISTRATION_TOKEN="change-me-registration-token"
export MAVLINK_ENDPOINT="serial:/dev/ttyAMA0:57600"   # example

python3 main.py
```

---

## Quickstart (Docker Compose in atc-stack)

The stack exposes this service behind the `mavlink` profile:

```bash
docker compose --profile mavlink up -d --build mavlink-gateway
```

---

## Configuration

### Networking + IDs

- `ATC_SERVER_URL` (default: `http://localhost:3000`)
- `ATC_DRONE_ID` (default: `DRONE0001`)
- `ATC_OWNER_ID` (optional; enables owner-scoped UI filtering if you use it)

### Auth

Choose one:

- `ATC_SESSION_TOKEN` (optional; if set, used directly)
- `ATC_REGISTRATION_TOKEN` (optional; used to register and mint `ATC_SESSION_TOKEN`)

### MAVLink transport

- `MAVLINK_ENDPOINT` (default: `udp:0.0.0.0:14550`)
  - `udp:0.0.0.0:14550` (SITL / UDP telemetry)
  - `serial:/dev/ttyAMA0:57600` (Raspberry Pi UART)

### Rates / behavior

- `ATC_TELEMETRY_HZ` (default: `5`)
- `ATC_COMMAND_POLL_HZ` (default: `2`)
- `ATC_TARGET_REACHED_M` (default: `12`)
- `ATC_HTTP_TIMEOUT_S` (default: `5`)
- `LOG_LEVEL` (default: `INFO`)

### TLS (optional)

- `ATC_SERVER_CA_CERT_PATH` (optional; CA PEM path for backend TLS, e.g. `/certs/ca.pem`)
- `ATC_TLS_INSECURE` (optional; set to `1` to skip TLS verification for dev/self-signed)

---

## What telemetry is sent

The gateway forwards:

- `drone_id`
- `owner_id` (optional)
- `lat`, `lon`
- `altitude_m`
- `heading_deg` (best-effort)
- `speed_mps` (best-effort)
- `timestamp` (RFC3339 UTC)

Sources (best-effort):
- `GLOBAL_POSITION_INT` (primary)
- `VFR_HUD` (secondary for heading/speed)

---

## Supported ATC → MAVLink command mapping

The gateway currently supports:

- `HOLD`: best-effort hold mode (tries `LOITER`, `HOLD`, `POSHOLD`, `BRAKE`, `ALT_HOLD`)
- `RESUME`: return to previous mode (if known) or continue guided target
- `ALTITUDE_CHANGE`: set a guided target at current lat/lon with requested altitude
- `REROUTE`: switch to guided/offboard and step through waypoint targets (advance within `ATC_TARGET_REACHED_M`)

### How reroute targets are sent

Waypoints are applied by sending `SET_POSITION_TARGET_GLOBAL_INT` (position + altitude target; velocity/accel/yaw ignored).

---

## Safety notes

- Validate in SITL before real flight.
- Ensure autopilot failsafes are configured and tested.
- The gateway does not handle arming/takeoff/landing sequencing; it only forwards telemetry and applies ATC commands as targets/modes.

---

## Troubleshooting

### No MAVLink heartbeat

- Check `MAVLINK_ENDPOINT`
- Verify serial permissions and baud rate
- Confirm the autopilot is emitting MAVLink on that interface

### Telemetry works but commands don’t

- Autopilot may not support requested modes (`GUIDED`/`OFFBOARD`/`LOITER` variants)
- Some autopilots require additional configuration to accept guided/offboard setpoints
