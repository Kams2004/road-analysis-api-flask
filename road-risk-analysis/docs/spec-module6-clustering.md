# Module 6 — Global Cluster Configuration (RRAP-06-009 / 010 / 011)

One clustering configuration for the whole platform. Every AI detection and
every community report (signalement) is clustered with the radius resolved
from this configuration **at its own position and for its own type**. No
module carries its own cluster distance any more; per-type values exist only
inside this configuration.

Code: `app/services/clustering/` (settings, zones, resolver, boundaries,
service), routes `app/api/routes/clustering.py`; web page `/clustering`
(`app/clustering/page.tsx`, `components/clustering/*`).

## 1. Configuration (settings section `clustering`)

| Field | Meaning | Default |
|---|---|---|
| `urban.default_radius_m` | In agglomeration — radius for every type without its own value | 30 m |
| `urban.type_radius` | Per AI-detection type (`pothole`, `traffic_sign`, `speed_bump`, `parked_vehicle`, …) | `{}` |
| `urban.signalement_radius` | Per community-report type (`SignalementType`) | jams 150, closures/works/police/flood 100, accident 75 |
| `rural.*` | Out of agglomeration, same fields | 60 m; jams 500, closures/works 300, police 250, flood/landslide 200, accident 150 |
| `unzoned_environment` | Profile used where no zone contains the point | `rural` |
| `merge_signalements` | Merge a new report into an active one of the same type within the radius | `true` |
| `default_corridor_width_m` | Default total width of a road-section corridor | 300 m |

Why 30 / 60 m: urban 30 m keeps two potholes on neighbouring corners apart
while absorbing the 5–15 m spread of dash-cam OSD GPS; rural 60 m matches the
former ~55 m processor de-duplication at highway speed (GPS lag, frame
sampling). Radii are validated between 1 and 10 000 m.

Stored as a settings section (validated pydantic model, JSON in
`system_settings`, audited, 10 s cache like every section) rather than by
widening the one-row `cluster_config` table: profiles are nested maps, which
the settings machinery already validates, merges and caches.

## 2. Zones (`cluster_zones`)

| Kind | Geometry | Default environment / priority |
|---|---|---|
| `agglomeration` | Polygon / MultiPolygon — usually an OSM boundary ("Communauté urbaine de Douala") | urban / 10 |
| `road_section` | LineString (OSRM route of a tronçon) + `corridor_width_m` (half on each side) | rural / 0 |
| `custom` | Polygon drawn by hand (or a migrated circular area) | urban / 10 |

Columns: `name, kind, environment, geometry (GeoJSON), corridor_width_m,
bbox_min/max_lat/lon (pre-filter, padded by half the corridor), area_km2,
center_lat/lon, circle_radius_km (migrated circles), source
(nominatim | drawn | route | legacy_area), source_ref ("relation/3832073",
route key), source_meta, default_radius_m, type_radius, signalement_radius
(null = inherit), priority, is_active, country, city, created_by, timestamps`.

PostgreSQL here has no PostGIS, so geometry is GeoJSON and containment runs
in Python (ray casting with holes / multipolygons; exact point-to-polyline
distance for corridors) on a cached snapshot of the active zones, after a
bbox pre-filter. A Douala–Yaoundé corridor (~730 vertices after
simplification) costs ~0.1 ms per hit, ~0.1 µs per bbox miss.

## 3. Resolution order

```
zone   = active zones containing the point, highest priority first,
         then smallest area (corridor area = length × width), then lowest id
radius = zone.<type override>            (source zone_type)
       → zone.default_radius_m           (zone_default)
       → profile[zone.environment].<type> (profile_type)
       → profile[zone.environment].default_radius_m (profile_default)
no zone → profile = unzoned_environment (rural by default)
```

Detections use `type_radius`, community reports `signalement_radius`.
Anything set on a zone wins over the environment profile (the zone is the
more specific place). Road sections have a lower default priority than
agglomerations, so a corridor that crosses Douala never turns Douala rural.

`GET /cluster-zones/resolve?lat&lon[&category&type]` returns the resolution
for every type at a point ("Test a point" tab).

## 4. Where it is applied

| Consumer | Before | Now |
|---|---|---|
| Hazard clustering (`hazard_service.attach / candidate_for`, queue hints, rebuild) | smallest circular area else global radius | `ClusterContext.resolve()` → resolver; new hazards store `radius_m`, `cluster_zone_id`, `cluster_zone_name`, `cluster_environment` |
| Processor per-job geo de-dup (`workers/processor.py`) | hard-coded `GEO_DEDUP_DEG = 0.0005` (~55 m box) | haversine ≤ radius resolved at the new detection's position for its type (pixel fallback without GPS unchanged) |
| Hazard lifecycle (sightings, resolved-hazard reopen, video passes, recurring) | `ctx.radius_for` / `hazard.radius_m` | same calls, now resolver-backed; `recurring_radius_m = null` still means the hazard's own radius |
| `POST /detections/clusters` (on-the-fly) | global radius | radius resolved at each cluster's seed; each cluster reports `radius_m` |
| Community reports `POST /signalements/` | no merging | radius resolved per report type; a report within it of an active, fresh, unmerged report of the same type is stored with `merged_into_id`, hidden from the app's nearby / along-route lists; the primary gets `report_count + 1` and `last_reported_at` (freshness restarts). Resolution stored in `cluster_radius_m`, `cluster_environment`, `cluster_zone_id` |

Changing the configuration never re-clusters history silently. It applies
to what is clustered afterwards (≤ 10 s in other processes, immediately in
the process that saved it). `POST /clustering/rebuild-hazards?confirm=true`
(or `python -m app.cli rebuild-hazards`) recreates every hazard with the
current configuration — hazard codes are renumbered and the lifecycle
timeline of the old hazards is discarded; the console asks for confirmation.

## 5. API (tag "Cluster configuration", permission module `settings`)

| Method | Path | Right |
|---|---|---|
| GET / PUT | `/clustering/config` | view / edit |
| POST | `/clustering/rebuild-hazards?confirm=true` | edit |
| GET | `/cluster-zones/?kind&environment&active&q&geometry` | view |
| GET | `/cluster-zones/geojson?kind&environment&active` | view |
| GET | `/cluster-zones/boundary-search?q=Douala[&limit&countrycodes]` | view |
| GET | `/cluster-zones/resolve?lat&lon[&category&type]` | view |
| POST | `/cluster-zones/road-section` (`route_key` or `start`/`end`, `corridor_width_m`, `preview`) | edit |
| POST | `/cluster-zones/` | edit |
| GET / PATCH / DELETE | `/cluster-zones/{zone_id}` | view / edit / edit |

The `settings` module is used (as for the former cluster radius / areas)
rather than a new permission module, so existing roles keep working without
re-seeding. Zone writes are audited as `cluster_zone.create|update|delete`,
configuration changes as `clustering.update`, rebuilds as
`clustering.rebuild_hazards`.

Deprecated wrappers (still working, `deprecated: true` in OpenAPI):
`GET/PUT /cluster-config/` (GET = default radius of the unzoned profile, PUT
sets both profiles' default) and `/cluster-areas/` (circular `custom` zones,
id = zone id).

### Boundary search

Nominatim `search?format=jsonv2&polygon_geojson=1&countrycodes=cm`
(`CLUSTERING_BOUNDARY_COUNTRYCODES`, `CLUSTERING_BOUNDARY_URL`,
`CLUSTERING_BOUNDARY_TIMEOUT_S`), identifying User-Agent (+
`GEOCODING_CONTACT`), ≤ 1 request/s, 24 h cache. Polygons are simplified with
Douglas-Peucker (0.0005°, doubled until ≤ 2000 vertices: Douala 3039 → 431
vertices). A result that is only a point (a city node without boundary)
becomes a 64-vertex circle sized from its bounding box (1–30 km), flagged
`approximate`; real boundaries are listed first.

## 6. Migration (idempotent)

`app/db/upgrade.py` `CLUSTER_ZONE_COLUMNS` (mirror:
`migrations/add_cluster_zones.sql`) adds the hazard / signalement columns;
`cluster_zones` comes from `create_all`. At API start-up
`clustering.service.migrate_legacy` runs once — the presence of the settings
row `clustering` is the marker:

* `cluster_config.radius_m` → both profiles' default, except when absent or
  equal to the old built-in 50 m (created on first read, so indistinguishable
  from "never set") → urban 30 m / rural 60 m;
* each `cluster_areas` row → a `custom` urban zone (64-vertex circle, same
  radius → `default_radius_m`, same per-type radii, `source=legacy_area`).

The legacy tables are left untouched and no longer read.

## 7. Web console

Sidebar → *Cluster configuration* (under Hazard Track, `settings:view`), also
listed in Settings → Module 6. The old "cluster radius" and "cluster areas"
cards are replaced by a link to the page.

* **Profiles** — Urban / Out of agglomeration tabs: default radius, per
  detection type, per report type (empty = inherits, shown as placeholder);
  unzoned environment, report merging, default corridor width.
* **Zones** — list with search / kind filter / active toggle / radius
  summary; hover ↔ map highlight. *Agglomeration*: type "Douala", hover a
  candidate to preview its outline, pick it, set name / environment /
  overrides. *Road section*: a predefined tronçon or two places, corridor
  width, preview, save. *Draw*: click-to-add-vertex polygon (drag to move,
  right-click to remove); existing outlines can be redrawn.
* **Test a point** — click the map: zone, environment and the radius per
  type with its source.
* *Rebuild hazards* (confirmation) re-clusters history.
