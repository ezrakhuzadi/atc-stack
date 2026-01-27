# mavlink-gateway

Tiny bridge that reads MAVLink telemetry (serial/UDP) and forwards it to `atc-drone` over HTTP.

## Environment

- `ATC_SERVER_URL` (default: `http://localhost:3000`)
- `ATC_DRONE_ID` (default: `DRONE0001`)
- `ATC_OWNER_ID` (optional)
- `ATC_SESSION_TOKEN` (optional; if set, used directly)
- `ATC_REGISTRATION_TOKEN` (optional; used to register and mint `ATC_SESSION_TOKEN`)
- `MAVLINK_ENDPOINT` (default: `udp:0.0.0.0:14550`)
  - Examples:
    - `udp:0.0.0.0:14550` (SITL/telemetry over UDP)
    - `serial:/dev/ttyAMA0:57600` (Raspberry Pi UART)
- `ATC_TELEMETRY_HZ` (default: `5`)

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

