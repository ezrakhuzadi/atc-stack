# Offline Data (Nationwide US)

This stack can run fully offline for **terrain** and **building/obstacle** lookups by pre-downloading:

- **OSM extract (.osm.pbf)** for a local Overpass instance
- **Copernicus DEM GeoTIFF tiles** for `terrain-api`

## Download

From the repo root (`atc-stack/`):

```bash
python3 tools/fetch_offline_data.py all --extent us50 --jobs 12
```

Notes:
- `--extent us50` = CONUS + Alaska + Hawaii.
- `--extent us` also includes Puerto Rico + some territories.
- Terrain tiles that return `404` are skipped (they are often ocean-only tiles not present in the Copernicus bucket).

## Docker compose wiring

`docker-compose.yml` reads these env vars (see `.env` for this machine’s current settings):
- `ATC_OSM_PBF` (e.g., `us-latest.osm.pbf`)
- `ATC_OSM_DIR` (e.g., `/mnt/atc-data/osm`)
- `ATC_TERRAIN_DIR` (e.g., `/mnt/atc-data/terrain/copernicus`)
- `ATC_OVERPASS_DB_DIR` (e.g., `/mnt/atc-data/overpass`)

You can override these via environment variables, e.g.:

```bash
export ATC_OSM_PBF=california-latest.osm.pbf
export ATC_OVERPASS_DB_DIR=/mnt/atc-data/overpass-ca
docker compose up -d overpass
```

## First-time Overpass build

Initializing Overpass from `us-latest.osm.pbf` can take a long time (and lots of disk). The DB is persisted under `ATC_OVERPASS_DB_DIR`.

You can build the DB once (without starting the whole stack) via:

```bash
tools/init_overpass_db.sh
```
