# atc-stack

Reproducible unified Docker Compose stack for:
- `atc-drone` (Rust backend)
- `atc-frontend` (Node/Express UI + proxy)
- `atc-blender` (Django Flight Blender)
- `interuss-dss` (local DSS sandbox)
- `terrain-api` (local Copernicus DEM elevation service)
- `mock-uss` (tiny demo USS)

## Quickstart

1) Clone with submodules:

```bash
git clone --recurse-submodules https://github.com/ezrakhuzadi/atc-stack.git
cd atc-stack
```

2) Create your environment file:

```bash
cp .env.example .env
```

Edit `.env` and replace all `change-me-*` values.

3) (Optional) Provide data mounts

This stack expects host data directories at:
- `./data/osm` (contains your `.osm.pbf`, default `us-latest.osm.pbf`)
- `./data/overpass-us` (Overpass DB directory)
- `./data/terrain/copernicus` (Copernicus DEM tiles)

`data/` is ignored by git.

4) Start the stack:

```bash
docker compose up -d
```

## Development (Flight Blender bind mounts)

The base `docker-compose.yml` is intentionally reproducible (no bind-mounting app code into containers).

If you want to iterate on Flight Blender locally with code mounted into the container, use:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
```

## Security note

Secrets are loaded from `.env` and are not committed. The defaults in `.env.example` are **demo-only** (not production-hard). In particular, `BLENDER_BYPASS_AUTH_TOKEN_VERIFICATION=1` is intended only for local sandboxes.
