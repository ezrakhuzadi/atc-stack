#!/usr/bin/env python3
from __future__ import annotations

import logging
import math
import os
import time
from dataclasses import dataclass, field
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


def env_bool(name: str, default: bool = False) -> bool:
    raw = env(name, "1" if default else "0")
    return raw.lower() in ("1", "true", "yes", "y", "on")


def env_float(name: str, default: float) -> float:
    raw = env(name, str(default))
    try:
        return float(raw)
    except Exception:  # noqa: BLE001
        return default


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


@dataclass
class CommandState:
    current_mode: str = ""
    previous_mode: str = ""
    hold_until: Optional[float] = None
    reroute_queue: list[tuple[float, float, float]] = field(default_factory=list)
    active_target: Optional[tuple[float, float, float]] = None


class AtcClient:
    def __init__(
        self,
        base_url: str,
        drone_id: str,
        owner_id: str,
        registration_token: str,
        session_token: str,
        ca_cert_path: str,
        tls_insecure: bool,
        timeout_s: float,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.drone_id = drone_id
        self.owner_id = owner_id
        self.registration_token = registration_token
        self.session_token = session_token
        self.timeout_s = timeout_s
        self.http = requests.Session()
        self.verify = False if tls_insecure else (ca_cert_path or True)

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
            verify=self.verify,
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
            verify=self.verify,
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
                verify=self.verify,
            )

        if resp.status_code not in (200, 202):
            raise RuntimeError(f"telemetry failed: {resp.status_code} {resp.text[:200]}")

    def get_next_command(self) -> Optional[dict]:
        token = self.ensure_session_token()
        resp = self.http.get(
            f"{self.base_url}/v1/commands/next",
            params={"drone_id": self.drone_id},
            headers={"Authorization": f"Bearer {token}"},
            timeout=self.timeout_s,
            verify=self.verify,
        )

        if resp.status_code in (401, 403) and self.registration_token:
            logging.warning("commands rejected (auth); re-registering token")
            self.session_token = ""
            token = self.ensure_session_token()
            resp = self.http.get(
                f"{self.base_url}/v1/commands/next",
                params={"drone_id": self.drone_id},
                headers={"Authorization": f"Bearer {token}"},
                timeout=self.timeout_s,
                verify=self.verify,
            )

        if resp.status_code != 200:
            raise RuntimeError(f"command poll failed: {resp.status_code} {resp.text[:200]}")

        payload = resp.json()
        return payload if isinstance(payload, dict) else None

    def ack_command(self, command_id: str) -> None:
        token = self.ensure_session_token()
        resp = self.http.post(
            f"{self.base_url}/v1/commands/ack",
            headers={"Authorization": f"Bearer {token}"},
            json={"command_id": command_id},
            timeout=self.timeout_s,
            verify=self.verify,
        )

        if resp.status_code in (401, 403) and self.registration_token:
            logging.warning("command ack rejected (auth); re-registering token")
            self.session_token = ""
            token = self.ensure_session_token()
            resp = self.http.post(
                f"{self.base_url}/v1/commands/ack",
                headers={"Authorization": f"Bearer {token}"},
                json={"command_id": command_id},
                timeout=self.timeout_s,
                verify=self.verify,
            )

        if resp.status_code not in (200, 201):
            raise RuntimeError(f"command ack failed: {resp.status_code} {resp.text[:200]}")


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


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    return 2.0 * r * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def pick_mode(mapping: dict, candidates: list[str]) -> Optional[str]:
    if not mapping:
        return None
    normalized = {str(name).upper(): str(name) for name in mapping.keys()}
    for candidate in candidates:
        key = candidate.strip().upper()
        if key in normalized:
            return normalized[key]
    return None


def set_mode_any(mav, candidates: list[str]) -> Optional[str]:
    mapping = mav.mode_mapping() or {}
    mode_name = pick_mode(mapping, candidates)
    if not mode_name:
        return None
    mode_id = mapping[mode_name]
    mav.mav.set_mode_send(
        mav.target_system,
        mavutil.mavlink.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED,
        mode_id,
    )
    return mode_name


def send_position_target(mav, lat: float, lon: float, altitude_m: float) -> None:
    lat_int = int(lat * 1e7)
    lon_int = int(lon * 1e7)

    type_mask = (
        mavutil.mavlink.POSITION_TARGET_TYPEMASK_VX_IGNORE
        | mavutil.mavlink.POSITION_TARGET_TYPEMASK_VY_IGNORE
        | mavutil.mavlink.POSITION_TARGET_TYPEMASK_VZ_IGNORE
        | mavutil.mavlink.POSITION_TARGET_TYPEMASK_AX_IGNORE
        | mavutil.mavlink.POSITION_TARGET_TYPEMASK_AY_IGNORE
        | mavutil.mavlink.POSITION_TARGET_TYPEMASK_AZ_IGNORE
        | mavutil.mavlink.POSITION_TARGET_TYPEMASK_YAW_IGNORE
        | mavutil.mavlink.POSITION_TARGET_TYPEMASK_YAW_RATE_IGNORE
    )

    mav.mav.set_position_target_global_int_send(
        0,
        mav.target_system,
        mav.target_component,
        mavutil.mavlink.MAV_FRAME_GLOBAL_INT,
        type_mask,
        lat_int,
        lon_int,
        float(altitude_m),
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
        0.0,
    )


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    atc_env = env("ATC_ENV", "").lower()
    atc_url = env("ATC_SERVER_URL", "http://localhost:3000")
    drone_id = env("ATC_DRONE_ID", "DRONE0001")
    owner_id = env("ATC_OWNER_ID", "")
    registration_token = env("ATC_REGISTRATION_TOKEN", "")
    session_token = env("ATC_SESSION_TOKEN", "")
    ca_cert_path = env("ATC_SERVER_CA_CERT_PATH", "")
    tls_insecure = env_bool("ATC_TLS_INSECURE", False)
    if tls_insecure and atc_env in ("prod", "production"):
        logging.error("refusing to start with ATC_TLS_INSECURE=1 when ATC_ENV=%s", atc_env)
        return 2
    mavlink_endpoint = env("MAVLINK_ENDPOINT", "udp:0.0.0.0:14550")
    telemetry_hz = env_float("ATC_TELEMETRY_HZ", 5.0)
    command_poll_hz = env_float("ATC_COMMAND_POLL_HZ", 2.0)
    timeout_s = env_float("ATC_HTTP_TIMEOUT_S", 5.0)
    target_reached_m = env_float("ATC_TARGET_REACHED_M", 12.0)

    send_interval_s = 1.0 / telemetry_hz if telemetry_hz > 0 else 0.2
    command_poll_interval_s = 1.0 / command_poll_hz if command_poll_hz > 0 else 1.0

    logging.info("ATC: %s drone_id=%s", atc_url, drone_id)
    logging.info("MAVLink: %s", mavlink_endpoint)

    atc = AtcClient(
        base_url=atc_url,
        drone_id=drone_id,
        owner_id=owner_id,
        registration_token=registration_token,
        session_token=session_token,
        ca_cert_path=ca_cert_path,
        tls_insecure=tls_insecure,
        timeout_s=timeout_s,
    )

    mav = mavutil.mavlink_connection(mavlink_endpoint, autoreconnect=True)
    logging.info("waiting for MAVLink heartbeat...")
    heartbeat = mav.wait_heartbeat(timeout=30)
    if heartbeat is None:
        logging.error("no MAVLink heartbeat received within 30s")
        return 1
    logging.info("heartbeat ok (sys=%s comp=%s)", mav.target_system, mav.target_component)

    telemetry = TelemetryState()
    commands = CommandState()
    next_send = 0.0
    next_poll = 0.0

    while True:
        msg = mav.recv_match(blocking=True, timeout=1.0)
        if msg is not None:
            msg_type = msg.get_type()
            if msg_type == "GLOBAL_POSITION_INT":
                parse_global_position_int(msg, telemetry)
            elif msg_type == "VFR_HUD":
                parse_vfr_hud(msg, telemetry)
            elif msg_type == "HEARTBEAT":
                try:
                    commands.current_mode = mav.flightmode or commands.current_mode
                except Exception:  # noqa: BLE001
                    pass

        now = time.monotonic()

        if commands.hold_until is not None and now >= commands.hold_until:
            commands.hold_until = None
            if commands.active_target:
                resumed = set_mode_any(mav, ["GUIDED", "OFFBOARD", "AUTO"])
                if resumed:
                    logging.info("auto-resume to %s (hold expired)", resumed)
            elif commands.previous_mode:
                resumed = set_mode_any(mav, [commands.previous_mode])
                if resumed:
                    logging.info("auto-resume to %s (hold expired)", resumed)
            commands.previous_mode = ""

        if now >= next_poll:
            next_poll = now + command_poll_interval_s
            try:
                cmd = atc.get_next_command()
            except Exception as exc:  # noqa: BLE001
                logging.debug("command poll failed: %s", exc)
                cmd = None

            if isinstance(cmd, dict) and cmd.get("command_id"):
                command_id = str(cmd.get("command_id"))
                command_type = cmd.get("command_type") or {}
                kind = str(command_type.get("type") or "").upper()
                handled = False

                try:
                    if kind == "HOLD":
                        duration = int(command_type.get("duration_secs") or 0)
                        if not commands.previous_mode:
                            commands.previous_mode = commands.current_mode
                        commands.hold_until = now + max(duration, 0)
                        hold_mode = set_mode_any(mav, ["LOITER", "HOLD", "POSHOLD", "BRAKE", "ALT_HOLD"])
                        if hold_mode:
                            logging.info("HOLD -> mode %s (duration=%ss)", hold_mode, duration)
                            handled = True
                        else:
                            logging.warning("HOLD requested but no compatible mode was found")

                    elif kind == "RESUME":
                        commands.hold_until = None
                        resume_mode = commands.previous_mode
                        commands.previous_mode = ""
                        if resume_mode:
                            set_mode_any(mav, [resume_mode])
                            logging.info("RESUME -> mode %s", resume_mode)
                        elif commands.active_target:
                            resumed = set_mode_any(mav, ["GUIDED", "OFFBOARD", "AUTO"])
                            logging.info("RESUME -> mode %s", resumed or "(unchanged)")
                        handled = True

                    elif kind == "ALTITUDE_CHANGE":
                        if telemetry.ready():
                            target_alt = float(command_type.get("target_altitude_m"))
                            set_mode_any(mav, ["GUIDED", "OFFBOARD"])
                            commands.active_target = (float(telemetry.lat), float(telemetry.lon), target_alt)
                            commands.reroute_queue = []
                            logging.info("ALTITUDE_CHANGE -> %.1fm", target_alt)
                            handled = True
                        else:
                            logging.warning("ALTITUDE_CHANGE received but telemetry is not ready")

                    elif kind == "REROUTE":
                        raw = command_type.get("waypoints") or []
                        waypoints: list[tuple[float, float, float]] = []
                        for wp in raw:
                            if not isinstance(wp, dict):
                                continue
                            lat = float(wp.get("lat"))
                            lon = float(wp.get("lon"))
                            alt = float(wp.get("altitude_m"))
                            waypoints.append((lat, lon, alt))
                        if waypoints:
                            set_mode_any(mav, ["GUIDED", "OFFBOARD"])
                            commands.active_target = waypoints[0]
                            commands.reroute_queue = waypoints[1:]
                            logging.info("REROUTE -> %s waypoint(s)", len(waypoints))
                            handled = True
                        else:
                            logging.warning("REROUTE received but no waypoints provided")

                    else:
                        logging.warning("unsupported command type: %s", kind or "(missing)")

                except Exception as exc:  # noqa: BLE001
                    logging.warning("command handler error (%s): %s", kind, exc)
                    handled = False

                if handled:
                    try:
                        atc.ack_command(command_id)
                    except Exception as exc:  # noqa: BLE001
                        logging.warning("command ack failed (%s): %s", command_id, exc)

        if now < next_send:
            continue
        next_send = now + send_interval_s

        if not telemetry.ready():
            continue

        if commands.active_target and commands.hold_until is None:
            try:
                send_position_target(mav, *commands.active_target)
            except Exception as exc:  # noqa: BLE001
                logging.debug("failed to send setpoint: %s", exc)

            dist = haversine_m(
                float(telemetry.lat),
                float(telemetry.lon),
                commands.active_target[0],
                commands.active_target[1],
            )
            if dist <= target_reached_m:
                if commands.reroute_queue:
                    commands.active_target = commands.reroute_queue.pop(0)
                    logging.info("advanced reroute target (remaining=%s)", len(commands.reroute_queue))
                else:
                    commands.active_target = None

        try:
            atc.send_telemetry(telemetry)
        except Exception as exc:  # noqa: BLE001
            logging.warning("telemetry send failed: %s", exc)
            time.sleep(1.0)


if __name__ == "__main__":
    raise SystemExit(main())
