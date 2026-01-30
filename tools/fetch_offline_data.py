#!/usr/bin/env python3
"""
Fetch offline data for the ATC stack:

- OSM PBF (Geofabrik) for local Overpass
- Copernicus DEM (AWS S3) GeoTIFF tiles for terrain-api

This script uses only the Python standard library.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import hashlib
import os
import random
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple


GEOFABRIK_URLS: Dict[str, str] = {
    "us": "https://download.geofabrik.de/north-america/us-latest.osm.pbf",
    "california": "https://download.geofabrik.de/north-america/us/california-latest.osm.pbf",
}

COPERNICUS_BASE_URL = "https://copernicus-dem-30m.s3.amazonaws.com"


@dataclasses.dataclass(frozen=True)
class TileSpec:
    lat: int
    lon: int

    def key(self) -> str:
        ns = "N" if self.lat >= 0 else "S"
        ew = "E" if self.lon >= 0 else "W"
        return f"{ns}{abs(self.lat):02d}_00_{ew}{abs(self.lon):03d}_00"

    def name(self) -> str:
        return f"Copernicus_DSM_COG_10_{self.key()}_DEM"

    def url(self) -> str:
        name = self.name()
        return f"{COPERNICUS_BASE_URL}/{name}/{name}.tif"

    def filename(self) -> str:
        return f"{self.name()}.tif"


def _rng_delay_s(attempt: int, base_s: float = 0.5, max_s: float = 10.0) -> float:
    exp = min(max_s, base_s * (2**attempt))
    return exp * (0.75 + random.random() * 0.5)


def _http_head(url: str, timeout_s: float) -> urllib.response.addinfourl:
    req = urllib.request.Request(url, method="HEAD")
    return urllib.request.urlopen(req, timeout=timeout_s)


def _download_with_resume(
    url: str,
    dest: Path,
    timeout_s: float,
    retries: int,
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")

    for attempt in range(retries + 1):
        try:
            remote_len: Optional[int] = None
            try:
                with _http_head(url, timeout_s=timeout_s) as head:
                    cl = head.headers.get("Content-Length")
                    if cl and cl.isdigit():
                        remote_len = int(cl)
            except Exception:
                remote_len = None

            if dest.exists() and dest.is_file():
                if remote_len is None or dest.stat().st_size == remote_len:
                    return

            start = tmp.stat().st_size if tmp.exists() else 0
            if remote_len is not None and start >= remote_len and start > 0:
                tmp.replace(dest)
                return

            req = urllib.request.Request(url)
            if start > 0:
                req.add_header("Range", f"bytes={start}-")

            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                if start > 0 and getattr(resp, "status", None) == 200:
                    start = 0
                    tmp.unlink(missing_ok=True)
                mode = "ab" if start > 0 else "wb"
                with tmp.open(mode) as f:
                    shutil.copyfileobj(resp, f, length=1024 * 1024)

            if remote_len is not None and tmp.exists() and tmp.stat().st_size != remote_len:
                raise IOError(f"incomplete download: {tmp.stat().st_size} != {remote_len}")

            tmp.replace(dest)
            return
        except urllib.error.HTTPError as e:
            if e.code == 416 and tmp.exists() and tmp.stat().st_size > 0:
                tmp.replace(dest)
                return
            if attempt >= retries:
                raise
        except Exception:
            if attempt >= retries:
                raise
        time.sleep(_rng_delay_s(attempt))


def _md5sum(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_osm(region: str, out_dir: Path, timeout_s: float, retries: int) -> Path:
    if region not in GEOFABRIK_URLS:
        raise SystemExit(f"Unknown OSM region '{region}'. Known: {', '.join(sorted(GEOFABRIK_URLS))}")
    url = GEOFABRIK_URLS[region]
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / Path(urllib.parse.urlparse(url).path).name
    md5_url = f"{url}.md5"
    md5_dest = out_dir / f"{dest.name}.md5"

    print(f"[osm] downloading: {url}")
    _download_with_resume(url, dest=dest, timeout_s=timeout_s, retries=retries)

    try:
        _download_with_resume(md5_url, dest=md5_dest, timeout_s=timeout_s, retries=retries)
    except Exception:
        md5_dest.unlink(missing_ok=True)

    if md5_dest.exists():
        md5_text = md5_dest.read_text(encoding="utf-8", errors="ignore").strip()
        # Format: "<md5>  <filename>"
        m = re.match(r"^([0-9a-fA-F]{32})\s+\*?(.+)$", md5_text)
        if m:
            expected = m.group(1).lower()
            actual = _md5sum(dest)
            if actual != expected:
                raise SystemExit(f"[osm] md5 mismatch for {dest.name}: expected {expected}, got {actual}")
            print(f"[osm] md5 ok: {dest.name}")
        else:
            print(f"[osm] warning: could not parse md5 file: {md5_dest.name}")
    else:
        print("[osm] warning: md5 file not available; skipped checksum")

    return dest


def _tiles_from_ranges(lat_min: int, lat_max: int, lon_min: int, lon_max: int) -> Iterator[TileSpec]:
    for lat in range(lat_min, lat_max + 1):
        for lon in range(lon_min, lon_max + 1):
            yield TileSpec(lat=lat, lon=lon)


def tiles_for_extent(extent: str) -> List[TileSpec]:
    tiles: Set[TileSpec] = set()

    # Contiguous US
    if extent in {"conus", "us50", "us"}:
        tiles.update(_tiles_from_ranges(lat_min=24, lat_max=49, lon_min=-125, lon_max=-66))

    # Alaska (includes Aleutians on both sides of the dateline)
    if extent in {"alaska", "us50", "us"}:
        tiles.update(_tiles_from_ranges(lat_min=51, lat_max=71, lon_min=-179, lon_max=-129))
        tiles.update(_tiles_from_ranges(lat_min=51, lat_max=71, lon_min=172, lon_max=179))

    # Hawaii
    if extent in {"hawaii", "us50", "us"}:
        tiles.update(_tiles_from_ranges(lat_min=18, lat_max=22, lon_min=-161, lon_max=-154))

    # Puerto Rico + USVI (optional)
    if extent in {"pr", "us"}:
        tiles.update(_tiles_from_ranges(lat_min=17, lat_max=18, lon_min=-68, lon_max=-64))

    if extent in {"all", "us"}:
        # Guam + Northern Mariana Islands
        tiles.update(_tiles_from_ranges(lat_min=13, lat_max=20, lon_min=144, lon_max=146))
        # American Samoa
        tiles.update(_tiles_from_ranges(lat_min=-15, lat_max=-13, lon_min=-172, lon_max=-169))

    if not tiles:
        raise SystemExit(
            f"Unknown extent '{extent}'. Try: conus, alaska, hawaii, pr, us50, us, all"
        )

    return sorted(tiles, key=lambda t: (t.lat, t.lon))


def fetch_terrain(
    extent: str,
    out_dir: Path,
    jobs: int,
    timeout_s: float,
    retries: int,
    limit: int,
    fail_on_missing: bool,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    all_tiles = tiles_for_extent(extent)
    if limit > 0:
        all_tiles = all_tiles[:limit]

    def task(tile: TileSpec) -> Tuple[TileSpec, str, Optional[str]]:
        dest = out_dir / tile.filename()
        if dest.exists() and dest.stat().st_size > 0:
            return (tile, "exists", None)
        try:
            _download_with_resume(tile.url(), dest=dest, timeout_s=timeout_s, retries=retries)
            return (tile, "downloaded", None)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return (tile, "missing", None)
            return (tile, "error", f"{e.__class__.__name__}: {e}")
        except Exception as e:
            return (tile, "error", f"{e.__class__.__name__}: {e}")

    print(f"[terrain] tiles planned: {len(all_tiles)} (extent={extent}, jobs={jobs})")
    failures: List[Tuple[TileSpec, str]] = []
    missing: List[TileSpec] = []
    downloaded = 0
    existed = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, jobs)) as executor:
        futures = [executor.submit(task, tile) for tile in all_tiles]
        completed = 0
        for future in concurrent.futures.as_completed(futures):
            tile, status, err = future.result()
            completed += 1
            if status == "downloaded":
                downloaded += 1
            elif status == "exists":
                existed += 1
            elif status == "missing":
                missing.append(tile)
            elif err:
                failures.append((tile, err))
            if completed == 1 or completed % 50 == 0 or completed == len(all_tiles):
                print(f"[terrain] progress: {completed}/{len(all_tiles)}")

    print(
        f"[terrain] done: downloaded={downloaded}, existed={existed}, missing={len(missing)}, failed={len(failures)}"
    )

    if missing and fail_on_missing:
        raise SystemExit(f"[terrain] {len(missing)} tile(s) were missing (404)")

    if failures:
        print("[terrain] failures:")
        for tile, err in failures[:50]:
            print(f"  - {tile.filename()}: {err}")
        raise SystemExit(f"[terrain] download failed for {len(failures)} tile(s)")


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description="Fetch offline OSM + terrain data for the ATC stack")
    parser.add_argument(
        "--data-root",
        default=os.getenv("ATC_DATA_ROOT", "/mnt/atc-data"),
        help="Root directory holding osm/, terrain/, overpass/ (default: /mnt/atc-data)",
    )
    parser.add_argument("--timeout-s", type=float, default=60.0, help="Per-request timeout seconds")
    parser.add_argument("--retries", type=int, default=3, help="Retries per file")

    subparsers = parser.add_subparsers(dest="cmd", required=True)

    osm_p = subparsers.add_parser("osm", help="Download OSM PBF (Geofabrik)")
    osm_p.add_argument("--region", default="us", choices=sorted(GEOFABRIK_URLS.keys()))

    terr_p = subparsers.add_parser("terrain", help="Download Copernicus DEM tiles")
    terr_p.add_argument(
        "--extent",
        default="us50",
        help="Extent: conus, alaska, hawaii, pr, us50 (default), us, all",
    )
    terr_p.add_argument("--jobs", type=int, default=max(1, os.cpu_count() or 4), help="Parallel downloads")
    terr_p.add_argument("--limit", type=int, default=0, help="Limit number of tiles (debug)")
    terr_p.add_argument(
        "--fail-on-missing",
        action="store_true",
        help="Fail if a Copernicus tile returns 404 (default: skip missing tiles, often ocean-only)",
    )

    all_p = subparsers.add_parser("all", help="Download both OSM and terrain")
    all_p.add_argument("--region", default="us", choices=sorted(GEOFABRIK_URLS.keys()))
    all_p.add_argument(
        "--extent",
        default="us50",
        help="Extent: conus, alaska, hawaii, pr, us50 (default), us, all",
    )
    all_p.add_argument("--jobs", type=int, default=max(1, os.cpu_count() or 4), help="Parallel downloads")
    all_p.add_argument("--limit", type=int, default=0, help="Limit number of tiles (debug)")
    all_p.add_argument(
        "--fail-on-missing",
        action="store_true",
        help="Fail if a Copernicus tile returns 404 (default: skip missing tiles, often ocean-only)",
    )

    args = parser.parse_args(list(argv))
    data_root = Path(args.data_root)

    osm_dir = data_root / "osm"
    terrain_dir = data_root / "terrain" / "copernicus"

    if args.cmd == "osm":
        fetch_osm(args.region, out_dir=osm_dir, timeout_s=args.timeout_s, retries=args.retries)
        return 0
    if args.cmd == "terrain":
        fetch_terrain(
            args.extent,
            out_dir=terrain_dir,
            jobs=args.jobs,
            timeout_s=args.timeout_s,
            retries=args.retries,
            limit=args.limit,
            fail_on_missing=args.fail_on_missing,
        )
        return 0
    if args.cmd == "all":
        fetch_osm(args.region, out_dir=osm_dir, timeout_s=args.timeout_s, retries=args.retries)
        fetch_terrain(
            args.extent,
            out_dir=terrain_dir,
            jobs=args.jobs,
            timeout_s=args.timeout_s,
            retries=args.retries,
            limit=args.limit,
            fail_on_missing=args.fail_on_missing,
        )
        return 0

    raise SystemExit("unreachable")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
