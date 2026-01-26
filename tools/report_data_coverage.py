#!/usr/bin/env python3
"""
Report current offline data coverage for ATC Drone.

- OSM extracts present in /mnt/atc-data/osm
- Copernicus DEM tiles present in /mnt/atc-data/terrain/copernicus
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


TILE_RE = re.compile(r"^Copernicus_DSM_COG_10_([NS])(\d{2})_00_([EW])(\d{3})_00_DEM\.tif$")


def _human_gb(bytes_: int) -> str:
    return f"{bytes_ / (1024**3):.2f} GiB"


def _list_osm(osm_dir: Path) -> None:
    pbf_files = sorted(osm_dir.glob("*.osm.pbf"))
    if not pbf_files:
        print(f"[osm] none found in {osm_dir}")
        return
    total = sum(p.stat().st_size for p in pbf_files)
    print(f"[osm] {len(pbf_files)} file(s), total {_human_gb(total)} in {osm_dir}")
    for p in pbf_files:
        print(f"  - {p.name} ({_human_gb(p.stat().st_size)})")


def _list_terrain(terrain_dir: Path) -> None:
    tif_files = sorted(terrain_dir.glob("Copernicus_DSM_COG_10_*_DEM.tif"))
    if not tif_files:
        print(f"[terrain] none found in {terrain_dir}")
        return

    lats: List[int] = []
    lons: List[int] = []
    total = 0
    missing_pattern = 0

    for p in tif_files:
        total += p.stat().st_size
        m = TILE_RE.match(p.name)
        if not m:
            missing_pattern += 1
            continue
        ns = m.group(1)
        lat = int(m.group(2))
        ew = m.group(3)
        lon = int(m.group(4))

        lats.append(lat if ns == "N" else -lat)
        lons.append(lon if ew == "E" else -lon)

    print(f"[terrain] {len(tif_files)} tile(s), total {_human_gb(total)} in {terrain_dir}")
    if lats and lons:
        print(
            f"[terrain] approx bbox: lat {min(lats)}..{max(lats)+1}, lon {min(lons)}..{max(lons)+1} (degrees)"
        )
    if missing_pattern:
        print(f"[terrain] warning: {missing_pattern} tile(s) didn't match expected naming pattern")


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description="Report current offline OSM + terrain coverage")
    parser.add_argument(
        "--data-root",
        default=os.getenv("ATC_DATA_ROOT", "/mnt/atc-data"),
        help="Root directory holding osm/, terrain/",
    )
    args = parser.parse_args(list(argv))
    root = Path(args.data_root)
    _list_osm(root / "osm")
    _list_terrain(root / "terrain" / "copernicus")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
