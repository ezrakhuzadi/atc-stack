# mavlink-gateway

Bridge that:

- reads MAVLink telemetry (serial/UDP) and forwards it to `atc-drone` over HTTP
- polls `atc-drone` for queued ATC commands and translates them into basic MAVLink actions

## Environment

- `ATC_SERVER_URL` (default: `http://localhost:3000`)
- `ATC_SERVER_CA_CERT_PATH` (optional; CA PEM path for backend TLS, e.g. `/certs/ca.pem`)
- `ATC_TLS_INSECURE` (optional; set to `1` to skip TLS verification for dev/self-signed)
- `ATC_DRONE_ID` (default: `DRONE0001`)
- `ATC_OWNER_ID` (optional)
- `ATC_SESSION_TOKEN` (optional; if set, used directly)
- `ATC_REGISTRATION_TOKEN` (optional; used to register and mint `ATC_SESSION_TOKEN`)
- `MAVLINK_ENDPOINT` (default: `udp:0.0.0.0:14550`)
  - Examples:
    - `udp:0.0.0.0:14550` (SITL/telemetry over UDP)
    - `serial:/dev/ttyAMA0:57600` (Raspberry Pi UART)
- `ATC_TELEMETRY_HZ` (default: `5`)
- `ATC_COMMAND_POLL_HZ` (default: `2`)
- `ATC_TARGET_REACHED_M` (default: `12`)

## Supported command mapping

The gateway currently supports:

- `HOLD`: switches to a best-effort hold mode (tries `LOITER`, `HOLD`, `POSHOLD`, `BRAKE`, `ALT_HOLD`)
- `RESUME`: returns to the previous mode (if known) or continues a guided target
- `ALTITUDE_CHANGE`: sets a guided target at the current lat/lon with the requested altitude
- `REROUTE`: switches to guided/offboard and steps through waypoint targets (advances when within `ATC_TARGET_REACHED_M`)

## Run (host)

```bash
python3 -m pip install -r requirements.txt
ATC_SERVER_URL=http://your-atc-host:3000 \
ATC_DRONE_ID=DRONE0001 \
ATC_REGISTRATION_TOKEN=change-me-registration-token \
MAVLINK_ENDPOINT=serial:/dev/ttyAMA0:57600 \
python3 main.py
```

## Run (docker compose)

The stack exposes this service behind the `mavlink` profile:

```bash
docker compose --profile mavlink up -d --build mavlink-gateway
```
