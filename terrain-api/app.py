import math
import os
from functools import lru_cache
from typing import Dict, List, Tuple

import numpy as np
import rasterio
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

DATA_DIR = os.getenv("TERRAIN_DATA_DIR", "/data/terrain")
CACHE_SIZE = int(os.getenv("TERRAIN_CACHE_SIZE", "8"))

app = FastAPI()


def parse_list(value: str) -> List[float]:
    if not value:
        return []
    return [float(item) for item in value.split(",") if item.strip()]


def tile_key(lat: float, lon: float) -> str:
    # Copernicus 1x1 degree tiles are named by the SW corner.
    lat_floor = math.floor(lat)
    lon_floor = math.floor(lon)
    ns = "N" if lat_floor >= 0 else "S"
    ew = "E" if lon_floor >= 0 else "W"
    return f"{ns}{abs(lat_floor):02d}_00_{ew}{abs(lon_floor):03d}_00"


def tile_path(key: str) -> str:
    name = f"Copernicus_DSM_COG_10_{key}_DEM"
    return os.path.join(DATA_DIR, f"{name}.tif")


@lru_cache(maxsize=CACHE_SIZE)
def open_dataset(path: str) -> rasterio.DatasetReader:
    return rasterio.open(path)


def sample_tile(path: str, points: List[Tuple[float, float]]) -> List[float]:
    dataset = open_dataset(path)
    nodata = dataset.nodata
    samples = dataset.sample(points)
    values = []
    for sample in samples:
        value = float(sample[0])
        if nodata is not None and value == nodata:
            value = 0.0
        if not np.isfinite(value):
            value = 0.0
        values.append(value)
    return values


class ElevationRequest(BaseModel):
    latitude: List[float]
    longitude: List[float]


def elevation_from_lists(lats: List[float], lons: List[float]) -> Dict[str, List[float]]:
    if not lats or not lons:
        raise HTTPException(status_code=400, detail="latitude and longitude are required")
    if len(lats) != len(lons):
        raise HTTPException(status_code=400, detail="latitude and longitude lengths differ")

    elevations = [0.0] * len(lats)
    grouped: Dict[str, List[Tuple[int, float, float]]] = {}

    for idx, (lat, lon) in enumerate(zip(lats, lons)):
        if not math.isfinite(lat) or not math.isfinite(lon):
            continue
        key = tile_key(lat, lon)
        grouped.setdefault(key, []).append((idx, lon, lat))

    for key, entries in grouped.items():
        path = tile_path(key)
        if not os.path.exists(path):
            continue
        coords = [(lon, lat) for _, lon, lat in entries]
        values = sample_tile(path, coords)
        for (idx, _, _), value in zip(entries, values):
            elevations[idx] = value

    return {"elevation": elevations}


@app.get("/v1/elevation")
def elevation(latitude: str = "", longitude: str = "") -> Dict[str, List[float]]:
    lats = parse_list(latitude)
    lons = parse_list(longitude)
    return elevation_from_lists(lats, lons)


@app.post("/v1/elevation")
def elevation_post(req: ElevationRequest) -> Dict[str, List[float]]:
    return elevation_from_lists(req.latitude, req.longitude)


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}
