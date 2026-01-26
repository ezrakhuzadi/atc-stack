# Offline Data (Nationwide US)

This stack can run fully offline for **terrain** and **building/obstacle** lookups by pre-downloading:

- **OSM extract (.osm.pbf)** for a local Overpass instance
- **Copernicus DEM GeoTIFF tiles** for `terrain-api`

## Download

From `Project/`:

```bash
python tools/fetch_nationwide_data.py all --extent us50 --jobs 12
```

Notes:
- `--extent us50` = CONUS + Alaska + Hawaii.
- `--extent us` also includes Puerto Rico + some territories.
- Terrain tiles that return `404` are skipped (they are often ocean-only tiles not present in the Copernicus bucket).

## Docker compose wiring

`docker-compose.unified.yml` defaults to:
- `ATC_OSM_PBF=us-latest.osm.pbf`
- `ATC_OSM_DIR=/mnt/atc-data/osm`
- `ATC_TERRAIN_DIR=/mnt/atc-data/terrain/copernicus`
- `ATC_OVERPASS_DB_DIR=/mnt/atc-data/overpass-us`

You can override these via environment variables, e.g.:

```bash
export ATC_OSM_PBF=california-latest.osm.pbf
export ATC_OVERPASS_DB_DIR=/mnt/atc-data/overpass-ca
docker compose -f docker-compose.unified.yml up -d overpass
```

## First-time Overpass build

Initializing Overpass from `us-latest.osm.pbf` can take a long time (and lots of disk). The DB is persisted under `ATC_OVERPASS_DB_DIR`.

You can build the DB once (without starting the whole stack) via:

```bash
tools/init_overpass_db.sh
```
