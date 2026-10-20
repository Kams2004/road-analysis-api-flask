"""
Module 6 — global Cluster Configuration (RRAP-06-009 / 010 / 011).

ONE configuration decides the clustering radius of everything the platform
groups by distance: AI detections (hazard clustering, the per-job geo
de-duplication of the video processor, hazard re-sightings / recurring
spots) and community reports (signalements: a new report of the same type
within the radius is merged into the existing one). No module carries its
own cluster distance; per-type values exist only inside this configuration.

Pieces
  settings.py    ClusteringSettings — settings section `clustering`: an
                 `urban` (in agglomeration) and a `rural` (out of
                 agglomeration) profile, each with a default radius and
                 per-detection-type / per-report-type overrides, plus the
                 environment of places outside every zone.
  zones.py       pure geometry: point-in-polygon (holes, multipolygons),
                 road corridors (buffered polylines), bbox, area,
                 Douglas-Peucker simplification, circles.
  resolver.py    pure resolution: resolve(snapshot, lat, lon, category,
                 kind) → Resolution(radius_m, zone, environment, source).
  boundaries.py  Nominatim boundary search ("Douala" → its OSM polygon).
  service.py     DB layer: snapshot of settings + active zones with a short
                 in-process cache (≤ 10 s staleness across processes,
                 invalidated at once in the process that changes it), the
                 one-time migration of the legacy cluster_config / cluster
                 areas, and helpers used by the routes.

Resolution order (documented in docs/spec-module6-clustering.md)
  1. zone = among the ACTIVE zones containing the point (an agglomeration
     polygon, a custom polygon, or a road-section corridor), the one with
     the highest `priority`, then the smallest area, then the lowest id.
     Default priorities: agglomeration / custom 10, road_section 0 — a
     corridor through a city never overrides the city.
  2. radius = zone override for the type → zone default radius → the
     zone environment's profile override for the type → that profile's
     default. No zone → the `unzoned_environment` profile (rural by default:
     anything outside every agglomeration is "out of agglomeration").

PostgreSQL here has no PostGIS: geometries are stored as GeoJSON with bbox
columns; the bbox pre-filter and point-in-polygon run in Python on the
cached snapshot (a few zones × a few thousand vertices — microseconds per
point).
"""
