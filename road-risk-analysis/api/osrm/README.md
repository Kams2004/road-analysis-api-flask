# OSRM road-intelligence layer

Step 2 of the collision-prevention module (see project docs) map-matches
vehicle telemetry against a road network via OSRM. By default the backend
talks to the public OSRM demo server (`OSRM_BASE_URL` in
`app/core/config.py`, default `https://router.project-osrm.org`) — the same
server the mobile app already uses for turn-by-turn navigation
(`viasafe-mobile/app/home/map.tsx`). That's fine as a zero-setup fallback,
but it's rate-limited, has no SLA, and serves whatever OSM snapshot the
OSRM project happens to host — not specifically Cameroon.

For anything beyond a quick check, run your own OSRM instance against a
Cameroon extract — the image is **self-preparing**: it downloads and
compiles the Cameroon dataset automatically on first boot, so deployment is
just starting the container.

## How the data is obtained

Source: [Geofabrik](https://download.geofabrik.de/africa/cameroon.html),
which republishes OpenStreetMap data pre-split by country/region — much
smaller than a worldwide extract (a few hundred MB vs. tens of GB) and
sufficient for Cameroon-only routing.

```
https://download.geofabrik.de/africa/cameroon-latest.osm.pbf
```

## How the data is prepared — automatically, on deployment

`osrm/Dockerfile` builds a small image on top of the official
`osrm-backend` image, adding `osrm/docker-entrypoint.sh` as its entrypoint.
That script, every time the container starts:

1. Checks `/data` (bind-mounted from `osrm/data/`) for an already-compiled
   `cameroon-latest.osrm`.
2. **If it's missing** (first deploy, or a fresh volume): downloads
   `cameroon-latest.osm.pbf` from Geofabrik, then runs the compile pipeline
   — `osrm-extract` (car profile) → `osrm-partition` → `osrm-customize`,
   using the MLD (multi-level Dijkstra) algorithm, the same algorithm it
   then starts `osrm-routed` with.
3. **If it's already there**: skips straight to starting `osrm-routed`.

So `docker-compose up -d osrm` is the entire deployment step — nothing to
run by hand first. The trade-off is time, not effort: that first boot does
a ~220MB download and several minutes of compilation before the service
starts answering requests (the healthcheck's `start_period` is set to 30
minutes to avoid a false-unhealthy flap during this). Every restart after
that reuses the compiled data sitting in the mounted volume and starts in
seconds.

### Pre-baking instead (recommended for a production first-deploy)

If you'd rather not do that compile step on a live production box (CPU load,
deploy-time latency), prepare the data on a build machine or your own
workstation first, then ship `osrm/data/` already populated:

```bash
./osrm/prepare_cameroon.sh
```

This runs the exact same download + extract/partition/customize pipeline
via the official `osrm-backend` image directly (no local OSRM install
needed), writing into `osrm/data/`. The entrypoint detects the resulting
`cameroon-latest.osrm` on next deploy and skips preparation entirely.

## Where the processed data is stored

`osrm/data/` (gitignored — this is compiled, regeneratable data, not
source). Both `docker-compose.osrm.yml` and `docker-compose.demo.yml` mount
it into the `osrm` service at `/data`.

## How the OSRM container is started

Production (layered on top of the main stack, without changing it):

```bash
docker-compose -f docker-compose.yml -f docker-compose.osrm.yml up -d osrm
```

Demo/dev stack — already wired in directly:

```bash
docker-compose -f docker-compose.demo.yml up -d osrm
```

Either way it exposes the OSRM HTTP API on host port 5001 (mapped from the
container's 5000) and on `http://osrm:5000` to other containers on the same
compose network.

## How the backend connects to OSRM

Set `OSRM_BASE_URL` (see `app/core/config.py` / `.env`):

```
OSRM_BASE_URL=http://osrm:5000
```

`docker-compose.demo.yml`'s `api` service already sets this. Nothing else
changes — `app/services/osrm/client.py` talks to whatever `OSRM_BASE_URL`
points at, so switching between the public demo server and a self-hosted
Cameroon instance is a one-line config change, not a code change.

## Replacing/updating the dataset later

Delete the compiled dataset and restart — the entrypoint will re-download
and recompile against a fresh OSM snapshot:

```bash
rm osrm/data/cameroon-latest.osrm* osrm/data/cameroon-latest.osm.pbf
docker-compose -f docker-compose.yml -f docker-compose.osrm.yml restart osrm
```

(Or re-run `./osrm/prepare_cameroon.sh`, which always starts fresh, then
restart the service the same way.)

## Region and mirror overrides

Two environment variables on the `osrm` service (see `docker-compose.osrm.yml`
/ `docker-compose.demo.yml`), both optional:

- `OSRM_REGION` (default `cameroon`) — must match a Geofabrik `africa/`
  region filename stem.
- `OSRM_OSM_URL` — overrides the derived Geofabrik URL entirely, e.g. to
  point at a mirror or a pre-downloaded internal host.

## Note on stable identifiers

`app/services/osrm/models.py` deliberately does not treat OSRM/OSM ids as
durable business keys, precisely because a dataset refresh like this can
shift them — see that file's docstring for the reasoning.
