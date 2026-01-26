#!/usr/bin/env python3
"""
Route Planner validation + stress harness for ATC-Drone.

Targets:
  POST {base_url}/v1/routes/plan

This script intentionally uses only the Python standard library so it can run
anywhere the stack runs.
"""

from __future__ import annotations

import argparse
import dataclasses
import http.cookiejar
import json
import math
import os
import random
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, Iterable, List, Optional, Tuple


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dl / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return r * c


def offset_by_bearing(lat: float, lon: float, distance_m: float, bearing_deg: float) -> Tuple[float, float]:
    r = 6371000.0
    brng = math.radians(bearing_deg)
    phi1 = math.radians(lat)
    lam1 = math.radians(lon)
    d = distance_m / r

    phi2 = math.asin(math.sin(phi1) * math.cos(d) + math.cos(phi1) * math.sin(d) * math.cos(brng))
    lam2 = lam1 + math.atan2(math.sin(brng) * math.sin(d) * math.cos(phi1), math.cos(d) - math.sin(phi1) * math.sin(phi2))
    return (math.degrees(phi2), math.degrees(lam2))


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


@dataclasses.dataclass(frozen=True)
class Waypoint:
    lat: float
    lon: float
    alt_m: float
    speed_mps: Optional[float] = None

    def to_json(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"lat": self.lat, "lon": self.lon, "altitude_m": self.alt_m}
        if self.speed_mps is not None:
            payload["speed_mps"] = self.speed_mps
        return payload


@dataclasses.dataclass(frozen=True)
class TestCase:
    name: str
    waypoints: List[Waypoint]
    params: Dict[str, Any]
    expect_ok: Optional[bool] = None
    notes: str = ""

    def request_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"waypoints": [wp.to_json() for wp in self.waypoints]}
        payload.update(self.params)
        return payload


def post_json(
    url: str,
    payload: Dict[str, Any],
    timeout_s: float,
    opener: Optional[urllib.request.OpenerDirector] = None,
) -> Tuple[int, str]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        open_fn = opener.open if opener is not None else urllib.request.urlopen
        with open_fn(req, timeout=timeout_s) as resp:
            status = int(getattr(resp, "status", 200))
            return status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raw = e.read()
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
        return int(e.code), text


def parse_json_maybe(text: str) -> Tuple[Optional[Any], Optional[str]]:
    raw = text.strip()
    if not raw:
        return None, "empty body"
    try:
        return json.loads(raw), None
    except Exception as err:  # noqa: BLE001
        return None, f"json parse failed: {err}"


def summarize_errors(payload: Any) -> List[str]:
    if not isinstance(payload, dict):
        return ["non-object response"]
    errors = payload.get("errors")
    if isinstance(errors, list):
        return [str(e) for e in errors]
    msg = payload.get("error") or payload.get("message")
    if isinstance(msg, str) and msg.strip():
        return [msg.strip()]
    return []


def validate_response(case: TestCase, status: int, data: Any) -> List[str]:
    issues: List[str] = []
    if not isinstance(data, dict):
        issues.append("response is not a JSON object")
        return issues

    if "ok" not in data or not isinstance(data.get("ok"), bool):
        issues.append("missing/invalid 'ok' field")
        return issues

    ok = bool(data.get("ok"))
    if status >= 500:
        issues.append(f"http {status}")
        return issues

    if ok and status != 200:
        issues.append(f"ok=true but http {status}")
    if (not ok) and status not in (400, 422):
        issues.append(f"ok=false but http {status}")

    waypoints = data.get("waypoints")
    if ok:
        if not isinstance(waypoints, list) or len(waypoints) < 2:
            issues.append("ok=true but waypoints is missing/too short")
        else:
            start = case.waypoints[0]
            end = case.waypoints[-1]
            first = waypoints[0] if isinstance(waypoints[0], dict) else {}
            last = waypoints[-1] if isinstance(waypoints[-1], dict) else {}
            try:
                d_start = haversine_m(start.lat, start.lon, float(first.get("lat")), float(first.get("lon")))
                d_end = haversine_m(end.lat, end.lon, float(last.get("lat")), float(last.get("lon")))
                if d_start > 250:
                    issues.append(f"first waypoint far from start ({d_start:.0f}m)")
                if d_end > 250:
                    issues.append(f"last waypoint far from end ({d_end:.0f}m)")
            except Exception:  # noqa: BLE001
                issues.append("failed to validate endpoint proximity")

            for idx, wp in enumerate(waypoints[: min(len(waypoints), 300)]):
                if not isinstance(wp, dict):
                    issues.append(f"waypoints[{idx}] not an object")
                    break
                try:
                    lat = float(wp.get("lat"))
                    lon = float(wp.get("lon"))
                    alt = float(wp.get("altitude_m"))
                except Exception:  # noqa: BLE001
                    issues.append(f"waypoints[{idx}] missing/invalid lat/lon/altitude_m")
                    break
                if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
                    issues.append(f"waypoints[{idx}] out-of-range lat/lon")
                    break
                if not math.isfinite(alt):
                    issues.append(f"waypoints[{idx}] altitude not finite")
                    break
                if alt < -2000 or alt > 30000:
                    issues.append(f"waypoints[{idx}] altitude suspicious ({alt:.1f}m)")
                    break
    else:
        errs = summarize_errors(data)
        if not errs:
            issues.append("ok=false but no error strings found")

    if case.expect_ok is not None and ok != case.expect_ok:
        issues.append(f"expected ok={case.expect_ok} but got ok={ok}")

    return issues


def region_points(
    rng: random.Random,
    center_lat: float,
    center_lon: float,
    min_dist_m: float,
    max_dist_m: float,
) -> Tuple[float, float]:
    bearing = rng.uniform(0.0, 360.0)
    dist = rng.uniform(min_dist_m, max_dist_m)
    return offset_by_bearing(center_lat, center_lon, dist, bearing)


def make_short_route(
    rng: random.Random,
    center: Tuple[float, float],
    min_len_m: float,
    max_len_m: float,
    alt_m: float,
) -> Tuple[Waypoint, Waypoint]:
    c_lat, c_lon = center
    s_lat, s_lon = region_points(rng, c_lat, c_lon, 0, 1500.0)
    # pick end relative to start to keep bbox small
    bearing = rng.uniform(0.0, 360.0)
    dist = rng.uniform(min_len_m, max_len_m)
    e_lat, e_lon = offset_by_bearing(s_lat, s_lon, dist, bearing)
    return Waypoint(s_lat, s_lon, alt_m), Waypoint(e_lat, e_lon, alt_m)


def make_medium_route(
    rng: random.Random,
    center: Tuple[float, float],
    min_len_m: float,
    max_len_m: float,
    alt_m: float,
) -> Tuple[Waypoint, Waypoint]:
    c_lat, c_lon = center
    s_lat, s_lon = region_points(rng, c_lat, c_lon, 0, 5000.0)
    bearing = rng.uniform(0.0, 360.0)
    dist = rng.uniform(min_len_m, max_len_m)
    e_lat, e_lon = offset_by_bearing(s_lat, s_lon, dist, bearing)
    return Waypoint(s_lat, s_lon, alt_m), Waypoint(e_lat, e_lon, alt_m)


def make_long_route(
    rng: random.Random,
    start_center: Tuple[float, float],
    end_center: Tuple[float, float],
    alt_m: float,
) -> Tuple[Waypoint, Waypoint]:
    s_lat, s_lon = region_points(rng, start_center[0], start_center[1], 0, 5000.0)
    e_lat, e_lon = region_points(rng, end_center[0], end_center[1], 0, 5000.0)
    return Waypoint(s_lat, s_lon, alt_m), Waypoint(e_lat, e_lon, alt_m)


def build_cases(seed: int, random_cases: int, long_cases: int) -> List[TestCase]:
    rng = random.Random(seed)

    # Region centers (roughly) - CA-only Overpass dataset.
    LA = (34.0522, -118.2437)
    SF = (37.7749, -122.4194)
    SD = (32.7157, -117.1611)

    cases: List[TestCase] = []

    # --- Validation/error-shaping cases ---
    cases.append(
        TestCase(
            name="invalid/empty-waypoints",
            waypoints=[],
            params={},
            expect_ok=False,
        )
    )
    cases.append(
        TestCase(
            name="invalid/one-waypoint",
            waypoints=[Waypoint(34.0, -118.0, 50.0)],
            params={},
            expect_ok=False,
        )
    )
    cases.append(
        TestCase(
            name="invalid/zero-distance",
            waypoints=[Waypoint(34.0, -118.0, 50.0), Waypoint(34.0, -118.0, 50.0)],
            params={},
            expect_ok=False,
        )
    )
    cases.append(
        TestCase(
            name="invalid/out-of-range-lat",
            waypoints=[Waypoint(999.0, -118.0, 50.0), Waypoint(34.0, -118.0, 50.0)],
            params={},
            expect_ok=False,
            notes="should fail cleanly (no panic) even if upstream providers reject bbox",
        )
    )
    cases.append(
        TestCase(
            name="invalid/negative-params",
            waypoints=[Waypoint(34.05, -118.25, 80.0), Waypoint(34.06, -118.26, 80.0)],
            params={
                "lane_radius_m": -50.0,
                "lane_spacing_m": -10.0,
                "sample_spacing_m": -5.0,
                "safety_buffer_m": -60.0,
                "max_lane_radius_m": -500.0,
                "lane_expansion_step_m": -10.0,
            },
            expect_ok=None,
            notes="robustness test: server should not panic",
        )
    )

    # --- Known regression-ish routes ---
    # Irvine -> nearby (from logs) - typically succeeds fast.
    cases.append(
        TestCase(
            name="smoke/irvine-short",
            waypoints=[Waypoint(33.68489, -117.82517, 28.0), Waypoint(33.64307, -117.83833, 54.4)],
            params={"max_lane_radius_m": 1200.0},
            expect_ok=None,
        )
    )
    # Irvine -> downtown LA-ish (from logs) - often slow/fails; exercises segmented path.
    cases.append(
        TestCase(
            name="stress/irvine-to-la",
            waypoints=[Waypoint(33.68489, -117.82517, 28.0), Waypoint(34.04295, -118.26666, 88.7)],
            params={"max_lane_radius_m": 1800.0},
            expect_ok=None,
            notes="segmented mode; good for truncation / lane-radius limits",
        )
    )

    # --- Random short/medium routes in dense metros ---
    param_grid = [
        {"lane_radius_m": 90.0, "max_lane_radius_m": 900.0, "sample_spacing_m": 5.0},
        {"lane_radius_m": 120.0, "max_lane_radius_m": 1200.0, "sample_spacing_m": 6.0},
        {"lane_radius_m": 150.0, "max_lane_radius_m": 1500.0, "sample_spacing_m": 7.5},
    ]

    for idx in range(random_cases):
        region = rng.choice([("la", LA), ("sf", SF), ("sd", SD)])
        tag, center = region
        if rng.random() < 0.75:
            start, end = make_short_route(rng, center, 600.0, 2800.0, alt_m=rng.uniform(40.0, 120.0))
            route_kind = "short"
        else:
            start, end = make_medium_route(rng, center, 3500.0, 9000.0, alt_m=rng.uniform(40.0, 120.0))
            route_kind = "medium"
        params = dict(rng.choice(param_grid))
        if rng.random() < 0.25:
            params["safety_buffer_m"] = rng.choice([20.0, 60.0, 120.0])
        cases.append(
            TestCase(
                name=f"random/{tag}/{route_kind}/{idx:03d}",
                waypoints=[start, end],
                params=params,
                expect_ok=None,
            )
        )

    # --- Long routes between metros (segmented) ---
    for idx in range(long_cases):
        start_center, end_center, tag = rng.choice(
            [
                (LA, SF, "la-sf"),
                (SD, LA, "sd-la"),
                (SF, SD, "sf-sd"),
            ]
        )
        start, end = make_long_route(rng, start_center, end_center, alt_m=rng.uniform(60.0, 150.0))
        cases.append(
            TestCase(
                name=f"random/long/{tag}/{idx:03d}",
                waypoints=[start, end],
                params={"max_lane_radius_m": 2000.0, "sample_spacing_m": 10.0},
                expect_ok=None,
            )
        )

    return cases


def run_case(
    url: str,
    timeout_s: float,
    case: TestCase,
    opener: Optional[urllib.request.OpenerDirector] = None,
) -> Dict[str, Any]:
    payload = case.request_payload()
    start = time.perf_counter()
    try:
        status, text = post_json(url, payload, timeout_s, opener=opener)
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        data, parse_err = parse_json_maybe(text)
        issues: List[str] = []
        if parse_err:
            issues.append(parse_err)
        else:
            issues.extend(validate_response(case, status, data))

        ok_val = data.get("ok") if isinstance(data, dict) else None
        errors = summarize_errors(data) if isinstance(data, dict) else []
        return {
            "name": case.name,
            "notes": case.notes,
            "expect_ok": case.expect_ok,
            "request": payload,
            "route_distance_m": (
                haversine_m(case.waypoints[0].lat, case.waypoints[0].lon, case.waypoints[-1].lat, case.waypoints[-1].lon)
                if len(case.waypoints) >= 2
                else None
            ),
            "http_status": status,
            "elapsed_ms": elapsed_ms,
            "ok": ok_val,
            "errors": errors,
            "nodes_visited": data.get("nodes_visited") if isinstance(data, dict) else None,
            "optimized_points": data.get("optimized_points") if isinstance(data, dict) else None,
            "sample_points": data.get("sample_points") if isinstance(data, dict) else None,
            "hazards_count": (len(data.get("hazards") or []) if isinstance(data, dict) else None),
            "issues": issues,
            "raw_body_prefix": text[:3000],
        }
    except Exception as err:  # noqa: BLE001
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        return {
            "name": case.name,
            "notes": case.notes,
            "expect_ok": case.expect_ok,
            "request": payload,
            "route_distance_m": None,
            "http_status": None,
            "elapsed_ms": elapsed_ms,
            "ok": None,
            "errors": [],
            "nodes_visited": None,
            "optimized_points": None,
            "sample_points": None,
            "hazards_count": None,
            "issues": [f"exception: {err}"],
            "raw_body_prefix": "",
        }


def percentile(values: List[float], pct: float) -> Optional[float]:
    if not values:
        return None
    values_sorted = sorted(values)
    k = int(math.ceil((pct / 100.0) * len(values_sorted))) - 1
    k = clamp(k, 0, len(values_sorted) - 1)
    return values_sorted[int(k)]


def print_summary(results: List[Dict[str, Any]]) -> None:
    ok_true = [r for r in results if r.get("ok") is True]
    ok_false = [r for r in results if r.get("ok") is False]
    unknown = [r for r in results if r.get("ok") not in (True, False)]

    by_error: Dict[str, int] = {}
    for r in ok_false:
        for e in r.get("errors") or ["(no error)"]:
            by_error[e] = by_error.get(e, 0) + 1

    lat_all = [float(r["elapsed_ms"]) for r in results if isinstance(r.get("elapsed_ms"), (int, float))]
    lat_ok = [float(r["elapsed_ms"]) for r in ok_true if isinstance(r.get("elapsed_ms"), (int, float))]
    lat_fail = [float(r["elapsed_ms"]) for r in ok_false if isinstance(r.get("elapsed_ms"), (int, float))]

    def fmt_ms(v: Optional[float]) -> str:
        return "n/a" if v is None else f"{v:.0f}ms"

    def latency_block(title: str, values: List[float]) -> str:
        if not values:
            return f"{title}: n/a"
        return (
            f"{title}: avg={statistics.mean(values):.0f}ms "
            f"p50={fmt_ms(percentile(values, 50))} "
            f"p90={fmt_ms(percentile(values, 90))} "
            f"p99={fmt_ms(percentile(values, 99))} "
            f"max={max(values):.0f}ms"
        )

    print("")
    print("=== Route Planner Validation Summary ===")
    print(f"total_cases: {len(results)}")
    print(f"ok_true: {len(ok_true)}")
    print(f"ok_false: {len(ok_false)}")
    print(f"unknown (exceptions/parse errors): {len(unknown)}")
    print(latency_block("latency_all", lat_all))
    print(latency_block("latency_ok", lat_ok))
    print(latency_block("latency_fail", lat_fail))

    top_errors = sorted(by_error.items(), key=lambda kv: kv[1], reverse=True)[:15]
    if top_errors:
        print("")
        print("Top failure reasons:")
        for msg, count in top_errors:
            print(f"  {count:4d}  {msg}")

    bad_issues = [r for r in results if r.get("issues")]
    if bad_issues:
        print("")
        print(f"Cases with validation issues: {len(bad_issues)}")
        for r in bad_issues[:20]:
            print(f"  - {r['name']}: {', '.join(r.get('issues') or [])}")
        if len(bad_issues) > 20:
            print(f"  ... and {len(bad_issues) - 20} more")


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description="ATC route planner validation/stress harness")
    parser.add_argument("--base-url", default=os.environ.get("ATC_URL", "http://localhost:3000"))
    parser.add_argument("--path", default="/v1/routes/plan")
    parser.add_argument("--timeout-s", type=float, default=90.0)
    parser.add_argument("--use-frontend-proxy", action="store_true")
    parser.add_argument("--frontend-url", default=os.environ.get("ATC_FRONTEND_URL", "http://localhost:5050"))
    parser.add_argument("--seed", type=int, default=1337)
    parser.add_argument("--random-cases", type=int, default=60)
    parser.add_argument("--long-cases", type=int, default=10)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--output", default="route_planner_validation_results.json")
    args = parser.parse_args(argv)

    opener: Optional[urllib.request.OpenerDirector] = None
    base_url = args.base_url.rstrip("/")
    if args.use_frontend_proxy:
        cookiejar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookiejar))
        login_url = args.frontend_url.rstrip("/") + "/login/guest"
        try:
            opener.open(urllib.request.Request(login_url, method="POST"), timeout=min(args.timeout_s, 30.0))
        except Exception as err:  # noqa: BLE001
            print(f"[fatal] failed to login via {login_url}: {err}", file=sys.stderr)
            return 2

        if not list(cookiejar):
            print(f"[fatal] guest login succeeded but no cookies were set by {login_url}", file=sys.stderr)
            return 2

        base_url = args.frontend_url.rstrip("/") + "/api/atc"

    url = base_url + args.path
    cases = build_cases(seed=args.seed, random_cases=args.random_cases, long_cases=args.long_cases)

    print(f"Target: {url}")
    print(f"Cases: {len(cases)} (random={args.random_cases}, long={args.long_cases})")
    print(f"Concurrency: {args.concurrency}")
    print(f"Timeout: {args.timeout_s}s")
    print("")

    lock = threading.Lock()
    completed = 0
    started_at = time.perf_counter()
    results: List[Dict[str, Any]] = []

    def on_done() -> None:
        nonlocal completed
        with lock:
            completed += 1
            if completed % 10 == 0 or completed == len(cases):
                elapsed = time.perf_counter() - started_at
                rate = completed / elapsed if elapsed > 0 else 0
                print(f"[progress] {completed}/{len(cases)} ({rate:.2f} req/s)")

    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        futs = [pool.submit(run_case, url, args.timeout_s, case, opener) for case in cases]
        for fut in as_completed(futs):
            res = fut.result()
            results.append(res)
            on_done()

    results.sort(key=lambda r: r.get("name", ""))

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(
            {
                "target": url,
                "seed": args.seed,
                "random_cases": args.random_cases,
                "long_cases": args.long_cases,
                "concurrency": args.concurrency,
                "timeout_s": args.timeout_s,
                "results": results,
            },
            f,
            indent=2,
            sort_keys=True,
        )

    print_summary(results)
    print("")
    print(f"Wrote: {args.output}")

    # Non-zero exit if we saw server errors or malformed responses.
    fatal = [r for r in results if any("http 5" in str(x) for x in (r.get("issues") or [])) or any("json parse failed" in str(x) for x in (r.get("issues") or []))]
    return 2 if fatal else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
