#!/bin/sh
# Self-preparing OSRM entrypoint. On first boot — no compiled dataset yet in
# the mounted /data volume — downloads the configured OSM region extract
# from Geofabrik and compiles it (extract -> partition -> customize, MLD
# algorithm). On every later restart, the compiled dataset is already there
# and this skips straight to starting osrm-routed. See osrm/README.md.
#
# POSIX /bin/sh, not bash — the upstream osrm-backend image is Alpine-based
# and doesn't ship bash.
set -eu

DATA_DIR=/data
REGION="${OSRM_REGION:-cameroon}"
OSM_URL="${OSRM_OSM_URL:-https://download.geofabrik.de/africa/${REGION}-latest.osm.pbf}"
PBF="${DATA_DIR}/${REGION}-latest.osm.pbf"
DATASET="${DATA_DIR}/${REGION}-latest.osrm"
# osrm-extract/-partition/-customize never actually write a bare "<name>.osrm"
# file — only "<name>.osrm.*" sidecar files — so checking for DATASET itself
# always looks "missing" and would recompile on every restart. `.mldgr` is
# written last, only by a *completed* osrm-customize run (the final MLD
# graph), so it's the right marker for "the whole pipeline finished", not
# just "extract finished" (which some earlier sidecar file would only prove).
READY_MARKER="${DATASET}.mldgr"

if [ ! -f "${READY_MARKER}" ]; then
    echo "[osrm-entrypoint] No compiled dataset at ${READY_MARKER} — preparing one now."
    echo "[osrm-entrypoint] This happens once; later restarts reuse the compiled data in the mounted volume."

    if [ ! -f "${PBF}" ]; then
        echo "[osrm-entrypoint] Downloading ${OSM_URL}"
        wget --tries=3 -O "${PBF}.tmp" "${OSM_URL}"
        mv "${PBF}.tmp" "${PBF}"
    else
        echo "[osrm-entrypoint] Reusing already-downloaded ${PBF}"
    fi

    echo "[osrm-entrypoint] Extracting (car profile)"
    osrm-extract -p /opt/car.lua "${PBF}"

    echo "[osrm-entrypoint] Partitioning (MLD)"
    osrm-partition "${DATASET}"

    echo "[osrm-entrypoint] Customizing"
    osrm-customize "${DATASET}"

    echo "[osrm-entrypoint] Dataset ready: ${DATASET}"
else
    echo "[osrm-entrypoint] Found existing compiled dataset (${READY_MARKER}) — skipping download/preparation."
fi

echo "[osrm-entrypoint] Starting osrm-routed"
exec osrm-routed --algorithm mld --max-matching-size 100 "${DATASET}"
