#!/usr/bin/env bash
# Downloads a Cameroon OSM extract and compiles it into an OSRM dataset
# ready for docker-compose.osrm.yml. See osrm/README.md for context.
#
# Usage: ./osrm/prepare_cameroon.sh
# Safe to re-run — re-downloads/recompiles from scratch each time, which is
# the simplest way to pick up a fresh OSM extract later.

set -euo pipefail

cd "$(dirname "$0")/data"

OSM_URL="https://download.geofabrik.de/africa/cameroon-latest.osm.pbf"
OSRM_IMAGE="ghcr.io/project-osrm/osrm-backend:latest"

echo "==> Downloading Cameroon OSM extract from Geofabrik"
curl -fL -o cameroon-latest.osm.pbf "$OSM_URL"

echo "==> Extracting (car profile)"
docker run --rm -v "$PWD:/data" "$OSRM_IMAGE" \
  osrm-extract -p /opt/car.lua /data/cameroon-latest.osm.pbf

echo "==> Partitioning (MLD algorithm — matches docker-compose.osrm.yml)"
docker run --rm -v "$PWD:/data" "$OSRM_IMAGE" \
  osrm-partition /data/cameroon-latest.osrm

echo "==> Customizing"
docker run --rm -v "$PWD:/data" "$OSRM_IMAGE" \
  osrm-customize /data/cameroon-latest.osrm

echo "==> Done. Start it with:"
echo "    docker-compose -f docker-compose.yml -f docker-compose.osrm.yml up -d osrm"
echo "Then set OSRM_BASE_URL=http://osrm:5000 (or http://localhost:5001 from the host)."
