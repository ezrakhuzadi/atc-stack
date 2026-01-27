#!/usr/bin/env python3
from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import requests

try:
    from pymavlink import mavutil
except Exception as exc:  # noqa: BLE001
    raise SystemExit(
        "pymavlink is required. Install with: python3 -m pip install -r requirements.txt"
    ) from exc


def env(name: str, default: str = "") -> str:
    value = os.environ.get(name, default)
    return value.strip() if isinstance(value, str) else default


def now_rfc3339() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class TelemetryState:
    lat: Optional[float] = None
    lon: Optional[float] = None
    altitude_m: Optional[float] = None
    heading_deg: Optional[float] = None
    speed_mps: Optional[float] = None

    def ready(self) -> bool:
        return self.lat is not None and self.lon is not None and self.altitude_m is not None


class AtcClient:
    def __init__(
        self,
        base_url: str,
        drone_id: str,
        owner_id: str,
        registration_token: str,
        session_token: str,
        timeout_s: float,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.drone_id = drone_id
        self.owner_id = owner_id
        self.registration_token = registration_token
        self.session_token = session_token
        self.timeout_s = timeout_s
        self.http = requests.Session()

    def ensure_session_token(self) -> str:
        if self.session_token:
            return self.session_token
        if not self.registration_token:
            raise RuntimeError("Set ATC_SESSION_TOKEN or ATC_REGISTRATION_TOKEN")

        payload: dict[str, object] = {"drone_id": self.drone_id}
        if self.owner_id:
            payload["owner_id"] = self.owner_id

        resp = self.http.post(
            f"{self.base_url}/v1/drones/register",
            headers={"X-Registration-Token": self.registration_token},
            json=payload,
            timeout=self.timeout_s,
        )
        if resp.status_code != 201:
            raise RuntimeError(f"register failed: {resp.status_code} {resp.text[:200]}")

        token = (resp.json() or {}).get("session_token")
        if not isinstance(token, str) or not token.strip():
            raise RuntimeError("register response missing session_token")

        self.session_token = token.strip()
        return self.session_token

    def send_telemetry(self, state: TelemetryState) -> None:
        token = self.ensure_session_token()

        payload: dict[str, object] = {
            "drone_id": self.drone_id,
            "lat": float(state.lat),
            "lon": float(state.lon),
            "altitude_m": float(state.altitude_m),
            "heading_deg": float(state.heading_deg or 0.0),
            "speed_mps": float(state.speed_mps or 0.0),
            "timestamp": now_rfc3339(),
        }
        if self.owner_id:
            payload["owner_id"] = self.owner_id

        resp = self.http.post(
            f"{self.base_url}/v1/telemetry",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
            timeout=self.timeout_s,
        )

        if resp.status_code in (401, 403) and self.registration_token:
            logging.warning("telemetry rejected (auth); re-registering token")
            self.session_token = ""
            token = self.ensure_session_token()
            resp = self.http.post(
                f"{self.base_url}/v1/telemetry",
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
                timeout=self.timeout_s,
            )

        if resp.status_code not in (200, 202):
            raise RuntimeError(f"telemetry failed: {resp.status_code} {resp.text[:200]}")


def parse_global_position_int(msg, state: TelemetryState) -> None:
    try:
        state.lat = float(msg.lat) / 1e7
        state.lon = float(msg.lon) / 1e7
        state.altitude_m = float(msg.alt) / 1000.0

        if getattr(msg, "hdg", None) not in (None, 65535):
            state.heading_deg = float(msg.hdg) / 100.0

        vx = getattr(msg, "vx", None)
        vy = getattr(msg, "vy", None)
        if vx is not None and vy is not None:
            speed = math.sqrt(float(vx) ** 2 + float(vy) ** 2) / 100.0
            if math.isfinite(speed):
                state.speed_mps = speed
    except Exception as exc:  # noqa: BLE001
        logging.debug("failed to parse GLOBAL_POSITION_INT: %s", exc)


def parse_vfr_hud(msg, state: TelemetryState) -> None:
    try:
        heading = getattr(msg, "heading", None)
        if heading is not None and math.isfinite(float(heading)):
            state.heading_deg = float(heading)

        groundspeed = getattr(msg, "groundspeed", None)
        if groundspeed is not None and math.isfinite(float(groundspeed)):
            state.speed_mps = float(groundspeed)
    except Exception as exc:  # noqa: BLE001
        logging.debug("failed to parse VFR_HUD: %s", exc)


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    atc_url = env("ATC_SERVER_URL", "http://localhost:3000")
    drone_id = env("ATC_DRONE_ID", "DRONE0001")
    owner_id = env("ATC_OWNER_ID", "")
    registration_token = env("ATC_REGISTRATION_TOKEN", "")
    session_token = env("ATC_SESSION_TOKEN", "")
    mavlink_endpoint = env("MAVLINK_ENDPOINT", "udp:0.0.0.0:14550")
    telemetry_hz = float(env("ATC_TELEMETRY_HZ", "5") or 5.0)
    timeout_s = float(env("ATC_HTTP_TIMEOUT_S", "5") or 5.0)
    send_interval_s = 1.0 / telemetry_hz if telemetry_hz > 0 else 0.2

    logging.info("ATC: %s drone_id=%s", atc_url, drone_id)
    logging.info("MAVLink: %s", mavlink_endpoint)

    atc = AtcClient(
        base_url=atc_url,
        drone_id=drone_id,
        owner_id=owner_id,
        registration_token=registration_token,
        session_token=session_token,
        timeout_s=timeout_s,
    )

    mav = mavutil.mavlink_connection(mavlink_endpoint, autoreconnect=True)
    logging.info("waiting for MAVLink heartbeat...")
    heartbeat = mav.wait_heartbeat(timeout=30)
    if heartbeat is None:
        logging.error("no MAVLink heartbeat received within 30s")
        return 1
    logging.info("heartbeat ok (sys=%s comp=%s)", mav.target_system, mav.target_component)

    state = TelemetryState()
    next_send = 0.0

    while True:
        msg = mav.recv_match(blocking=True, timeout=1.0)
        if msg is not None:
            msg_type = msg.get_type()
            if msg_type == "GLOBAL_POSITION_INT":
                parse_global_position_int(msg, state)
            elif msg_type == "VFR_HUD":
                parse_vfr_hud(msg, state)

        now = time.monotonic()
        if now < next_send:
            continue
        next_send = now + send_interval_s

        if not state.ready():
            continue

        try:
            atc.send_telemetry(state)
        except Exception as exc:  # noqa: BLE001
            logging.warning("telemetry send failed: %s", exc)
            time.sleep(1.0)


if __name__ == "__main__":
    raise SystemExit(main())
