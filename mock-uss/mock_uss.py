#!/usr/bin/env python3
import json
import math
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse, urlencode
from urllib.request import Request, urlopen


HOST = "0.0.0.0"
PORT = int(os.getenv("MOCK_USS_PORT", "9100"))

DSS_BASE_URL = os.getenv("DSS_BASE_URL", "http://localhost:8082/").rstrip("/") + "/"
DSS_AUTH_URL = os.getenv("DSS_AUTH_URL", "http://localhost:8085/token")
DSS_SELF_AUDIENCE = os.getenv("DSS_SELF_AUDIENCE", "localhost")

MOCK_USS_BASE_URL = os.getenv("MOCK_USS_BASE_URL", f"http://host.docker.internal:{PORT}")
ISA_DURATION_SEC = int(os.getenv("ISA_DURATION_SEC", "21600"))


FLIGHT_SEEDS = [
    {
        "id": "3a98d7a7-6fdc-4b74-8b51-4b7d3f9df6a3",
        "lat": 33.6846,
        "lng": -117.8265,
        "alt": 120.0,
        "heading": 90.0,
        "speed": 12.0,
        "aircraft_type": "Multirotor",
        "phase": 0.0,
    },
    {
        "id": "7ec9f3f7-3f6d-4c17-9482-4f54554b7f63",
        "lat": 33.6884,
        "lng": -117.8358,
        "alt": 135.0,
        "heading": 210.0,
        "speed": 10.0,
        "aircraft_type": "FixedWing",
        "phase": 1.3,
    },
]


def iso_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def make_position(seed):
    t = time.time() / 60.0
    wobble = 0.002
    lat = seed["lat"] + wobble * math.sin(t + seed["phase"])
    lng = seed["lng"] + wobble * math.cos(t + seed["phase"])
    alt = seed["alt"] + 5.0 * math.sin(t * 0.7 + seed["phase"])
    return lat, lng, alt


def build_flight(seed):
    lat, lng, alt = make_position(seed)
    timestamp = {"value": iso_now(), "format": "RFC3339"}
    position = {
        "lat": lat,
        "lng": lng,
        "alt": alt,
        "accuracy_h": "HA10m",
        "accuracy_v": "VA10m",
        "extrapolated": False,
        "pressure_altitude": alt,
        "height": {"reference": "TakeoffLocation", "distance": 0.0},
    }
    current_state = {
        "timestamp": timestamp,
        "timestamp_accuracy": 1.0,
        "operational_status": "Airborne",
        "position": position,
        "track": seed["heading"],
        "speed": seed["speed"],
        "speed_accuracy": "SA1mps",
        "vertical_speed": 0.0,
    }
    return {
        "id": seed["id"],
        "aircraft_type": seed["aircraft_type"],
        "current_state": current_state,
        "simulated": True,
        "recent_positions": [],
    }


def parse_view(view_str):
    try:
        parts = [float(v) for v in view_str.split(",")]
        if len(parts) != 4:
            return None
        return parts
    except Exception:
        return None


def filter_flights_by_view(flights, view):
    if not view:
        return flights
    min_lat, min_lng, max_lat, max_lng = view
    filtered = []
    for flight in flights:
        pos = flight["current_state"]["position"]
        if min_lat <= pos["lat"] <= max_lat and min_lng <= pos["lng"] <= max_lng:
            filtered.append(flight)
    return filtered


def request_token():
    params = {
        "grant_type": "client_credentials",
        "intended_audience": DSS_SELF_AUDIENCE,
        "scope": "rid.service_provider rid.display_provider",
        "issuer": DSS_SELF_AUDIENCE,
    }
    url = DSS_AUTH_URL + "?" + urlencode(params)
    with urlopen(url, timeout=10) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return payload.get("access_token")


def register_isa():
    token = request_token()
    if not token:
        raise RuntimeError("No access_token returned from dummy-oauth")

    now = datetime.now(timezone.utc).replace(microsecond=0)
    time_start = now.isoformat().replace("+00:00", "Z")
    time_end = (now + timedelta(seconds=ISA_DURATION_SEC)).isoformat().replace("+00:00", "Z")

    min_lat = min(seed["lat"] for seed in FLIGHT_SEEDS) - 0.01
    max_lat = max(seed["lat"] for seed in FLIGHT_SEEDS) + 0.01
    min_lng = min(seed["lng"] for seed in FLIGHT_SEEDS) - 0.01
    max_lng = max(seed["lng"] for seed in FLIGHT_SEEDS) + 0.01

    payload = {
        "extents": {
            "volume": {
                "outline_polygon": {
                    "vertices": [
                        {"lat": min_lat, "lng": min_lng},
                        {"lat": min_lat, "lng": max_lng},
                        {"lat": max_lat, "lng": max_lng},
                        {"lat": max_lat, "lng": min_lng},
                    ]
                },
                "altitude_lower": {"value": 0, "reference": "W84", "units": "M"},
                "altitude_upper": {"value": 200, "reference": "W84", "units": "M"},
            },
            "time_start": {"value": time_start, "format": "RFC3339"},
            "time_end": {"value": time_end, "format": "RFC3339"},
        },
        "uss_base_url": MOCK_USS_BASE_URL,
    }

    isa_id = str(uuid.uuid4())
    url = f"{DSS_BASE_URL}rid/v2/dss/identification_service_areas/{isa_id}"
    req = Request(
        url,
        method="PUT",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    with urlopen(req, timeout=10) as resp:
        body = resp.read().decode("utf-8")
        return resp.status, body


class MockUSSHandler(BaseHTTPRequestHandler):
    def _send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "" or path == "/":
            self._send_json(
                200,
                {
                    "service": "mock-uss",
                    "base_url": MOCK_USS_BASE_URL,
                    "dss_base_url": DSS_BASE_URL,
                },
            )
            return

        if path == "/health":
            self._send_json(200, {"status": "ok"})
            return

        if path == "/uss/flights":
            query = parse_qs(parsed.query)
            view_raw = query.get("view", [None])[0]
            view = parse_view(view_raw) if view_raw else None

            flights = [build_flight(seed) for seed in FLIGHT_SEEDS]
            flights = filter_flights_by_view(flights, view)
            response = {
                "timestamp": {"value": iso_now(), "format": "RFC3339"},
                "flights": flights,
            }
            self._send_json(200, response)
            return

        if path.startswith("/uss/flights/") and path.endswith("/details"):
            parts = path.split("/")
            if len(parts) >= 4:
                flight_id = parts[3]
            else:
                self._send_json(404, {"message": "Not found"})
                return

            matching = next((seed for seed in FLIGHT_SEEDS if seed["id"] == flight_id), None)
            if not matching:
                self._send_json(404, {"message": "Flight not found"})
                return

            details = {
                "details": {
                    "id": flight_id,
                    "operation_description": "Mock USS demo flight",
                    "operator_id": "mock-operator",
                    "operator_location": {
                        "position": {"lat": matching["lat"], "lng": matching["lng"]}
                    },
                    "uas_id": {
                        "serial_number": f"MOCK-{flight_id}",
                        "registration_id": "MOCK-REG-001",
                        "utm_id": "MOCK-UTM-001",
                    },
                }
            }
            self._send_json(200, details)
            return

        self._send_json(404, {"message": "Not found"})


def main():
    try:
        status, body = register_isa()
        print(f"[mock-uss] ISA registered: HTTP {status}")
        if body:
            print(body[:500])
    except Exception as exc:
        print(f"[mock-uss] ISA registration failed: {exc}")

    server = HTTPServer((HOST, PORT), MockUSSHandler)
    print(f"[mock-uss] listening on {HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
