#!/usr/bin/env bash
set -euo pipefail

DATA_ROOT="${ATC_DATA_ROOT:-/mnt/atc-data}"
OSM_DIR="${ATC_OSM_DIR:-$DATA_ROOT/osm}"
OSM_PBF="${ATC_OSM_PBF:-us-latest.osm.pbf}"
DB_DIR="${ATC_OVERPASS_DB_DIR:-$DATA_ROOT/overpass-us}"

IMAGE="${OVERPASS_IMAGE:-wiktorn/overpass-api:latest}"

mkdir -p "$DB_DIR"

echo "[overpass] building DB in: $DB_DIR"
echo "[overpass] using PBF: $OSM_DIR/$OSM_PBF"
echo "[overpass] image: $IMAGE"

docker run --rm \
  -v "$DB_DIR:/db" \
  -v "$OSM_DIR:/osm:ro" \
  -e OVERPASS_MODE=init \
  -e OVERPASS_META=no \
  -e OVERPASS_PLANET_URL="file:///osm/$OSM_PBF" \
  -e OVERPASS_DIFF_URL= \
  -e OVERPASS_COMPRESSION=gz \
  -e OVERPASS_RULES_LOAD=10 \
  -e OVERPASS_UPDATE_SLEEP=3600 \
  -e OVERPASS_STOP_AFTER_INIT=true \
  -e "OVERPASS_PLANET_PREPROCESS=rm -f /db/planet.osm.bz2 /db/planet.osm.pbf && cp /osm/$OSM_PBF /db/planet.osm.pbf && osmium cat -o /db/planet.osm.bz2 /db/planet.osm.pbf && rm /db/planet.osm.pbf" \
  "$IMAGE"

echo "[overpass] init complete"

