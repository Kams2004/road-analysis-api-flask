# VIASafe backend (FastAPI + Celery)

The API (`app.main:app`) and the Celery worker / beat (`app.workers.celery_app`)
share one package, `app/`, organised by **functional module of the CAMTRACK
requirements** (RRAP-NN-xxx = module NN).

```
app/
  main.py            FastAPI app: lifespan (DB init, seeds, back-fills), CORS, audit middleware
  cli.py             python -m app.cli … (admin, API clients, rebuild-hazards, geocode back-fill)
  export_openapi.py  python -m app.export_openapi → ../docs/openapi.json
  api/               deps.py (auth / permission guards), openapi.py (tags, security, operationIds),
                     router.py (mounts every module router with its access rules), health.py
  core/              shared infrastructure (below)
  modules/           m01 … m13, one package per functional module (below)
  workers/           celery_app.py (app, task routes, beat schedule) + task modules
  demo/              seeders, simulators and purge of demo data
```

## Module map

| Module | RRAP | Package | Main files |
|--------|------|---------|------------|
| 1 — Platform Security, Access & User Management | RRAP-01 | `app/modules/m01_security` | `routes/{auth,users,roles}.py`, `auth_service.py`, `captcha.py`, `google_signin.py`, `models/{user,auth_token}.py`, `schemas.py` |
| 2 — Dashboard & Decision Support | RRAP-02 | `app/modules/m02_dashboard` | `routes/stats.py` |
| 3 — Mapping, Routes & Geospatial Intelligence | RRAP-03 | `app/modules/m03_maps` | `routes/{map_config,route_sections}.py`, `map_tiles.py`, `geocoding.py`, `geocoding_backfill.py`, `osrm/` (client, geo, map matching) |
| 4 — Road Risk Assessment (ARR) | RRAP-04 | — | not built yet |
| 5 — AI Hazard & Road Infrastructure Detection | RRAP-05 | `app/modules/m05_ai_detection` | `routes/ai_models.py`, `model_registry.py`, `model_stats.py`, `ai_validation.py`, `static_check.py`, `sign_taxonomy.py`, `osd_parser.py`, `parked_vehicle/` |
| 6 — Hazard Management, Clustering, Validation & Evidence | RRAP-06 | `app/modules/m06_hazards` | `routes/{detections,hazards,rejection_reasons,datasets,clustering,cluster_config,cluster_areas}.py`, `detection_workflow.py`, `hazard_service.py`, `lifecycle/`, `clustering/`, `training_data/`, `dataset_service.py`, `models/` (11 tables), `schemas.py` |
| 7 — Video/Data Ingestion & Processing Jobs | RRAP-07 | `app/modules/m07_ingestion` | `routes/{jobs,streams,cloud_connections,cloud_imports}.py`, `pipeline.py` (the video processor), `job_service.py`, `job_slots.py`, `watchdog.py`, `stream_capture.py`, `cloud_import.py`, `cloud_providers.py`, `cloud_storage.py`, `sftp_core.py`, `sftp_source.py`, `models/` (6 tables), `schemas/{jobs,cloud_imports}.py` |
| 8 — Historical Road Risk Analytics | RRAP-08 | `app/modules/m08_historical` | `routes/historical.py`, `historical_service.py`, `domain.py`, `grid.py`, `matcher.py`, `anonymity.py`, `rollups.py`, `aggregates.py`, `event_feed.py`, `sources/`, `models/` |
| 9 — Exception patterns (formerly Driver Behaviour; anonymous) | RRAP-09 | `app/modules/m09_exception_patterns` | `routes/exception_patterns.py`, `views.py`, `analytics.py`, `service.py`, `settings.py` (section `driver_behaviour`) |
| 10 — Predictive Cooperative Collision Prevention | RRAP-10 | `app/modules/m10_collision` | `routes/{vehicles,road_state,trajectory,collisions,alerts}.py`, `vehicle_service.py`, `road_state_service.py`, `trajectory_service.py` + `trajectory/`, `collision_service.py` + `collision/`, `alert_service.py` + `alerts/`, `staleness_guard.py`, `models/{vehicle,vehicle_road_state}.py`, `schemas.py` |
| 11 — Community Reporting & Dynamic Road Events | RRAP-11 | `app/modules/m11_community` | `routes/signalements.py`, `signalement_service.py`, `signalement_votes.py`, `spatial_signalement.py`, `models/{signalement,signalement_vote}.py`, `schemas.py` |
| 12 — External Applications & API Integration | RRAP-12 | `app/modules/m12_integration` | `routes/{api_clients,mobile_config}.py`, `mobile_config.py`, `models/api_client.py` (OpenAPI: `app/api/openapi.py`, `app/export_openapi.py`) |
| 13 — Configuration, Audit, Reporting & Administration | RRAP-13 | `app/modules/m13_admin` | `routes/{platform_settings,audit_logs}.py`, `models/{system_setting,audit_log}.py` |

Shared infrastructure, `app/core/`:

| File | Role |
|------|------|
| `config.py` | environment settings (`settings`) |
| `security.py`, `permissions.py` | hashing / tokens, permission-module catalogue |
| `db/session.py`, `db/upgrade.py`, `db/registry.py` | engine, `Base`, `get_db`; idempotent start-up schema upgrades; the model registry |
| `settings_service.py` | runtime platform settings — the section registry edited in Settings → Platform |
| `storage/minio_service.py`, `storage/redis_client.py` | object storage and Redis clients |
| `audit.py` | audit-trail helpers (`record`, `record_now`) |
| `email_service.py`, `rate_limit.py`, `spatial.py` | SMTP, Redis rate limits, Haversine SQL helpers |

Celery, `app/workers/`: `celery_app.py` defines the app, task routes and beat
schedule; `job_tasks.py` (process_video, watchdog, media clean-up, cloud sync,
stream capture), `import_tasks.py`, `hazard_tasks.py`, `training_tasks.py`
register the other tasks and call into the modules (process_video runs
`app.modules.m07_ingestion.pipeline`).

## Module layout and conventions

Every module package uses the same names (only what it needs):

```
app/modules/mNN_name/
  __init__.py   docstring: RRAP sections implemented and what is inside
  routes/       one FastAPI router per resource (`router = APIRouter()`)
  models/       one file per SQLAlchemy model (or closely related group)
  schemas.py    pydantic request / response bodies (schemas/ when there are several)
  *.py          services and domain logic, named after what they do
  <engine>/     sub-package for a larger engine (lifecycle/, clustering/, osrm/ …)
```

- **Imports** are absolute (`from app.modules.m06_hazards import hazard_service`).
  A module may import another module's services or models; shared code that
  several modules need belongs in `app/core/`. Keep imports of heavy or
  circular dependencies inside functions, as the existing code does.
- **Adding a route**: create `routes/<resource>.py` in the module with a
  `router`, then import it in `app/api/router.py` and mount it in
  `register_routes()` with its prefix, an OpenAPI tag (declared in
  `app/api/openapi.py`) and its access rule (`module_guard(...)` — view / add /
  edit / delete of a permission module, `client_routes` for API-key callers).
  Every handler needs a summary; `tests/test_openapi.py` checks it, and
  `python -m app.export_openapi` refreshes `../docs/openapi.json`.
- **Adding a model**: create `models/<name>.py` subclassing
  `app.core.db.session.Base`, and add its import to
  `app/core/db/registry.py` (`import_all_models()` is what `create_all`, the
  start-up upgrade and the demo seeders rely on). Columns added to an existing
  table also go in `app/core/db/upgrade.py` (idempotent `ALTER … IF NOT EXISTS`)
  with a mirror SQL file in `migrations/`.
- **Adding a Celery task**: define it in a task module of `app/workers/` with an
  explicit, stable name — `@celery_app.task(name="app.workers.celery_app.<task>")`
  — add its queue to `task_routes` (and `beat_schedule` if periodic) in
  `celery_app.py`, and import the task module at the end of `celery_app.py`.
  Task names are persisted in the broker and referenced by beat: never rename
  one, even if the function moves.
- **Adding a runtime setting**: add a section model to `app/core/settings_service.py`.
- **Adding a module** (e.g. m04 ARR): create
  `app/modules/mNN_<name>/` with the layout above and a docstring, then wire its
  routers, models and tasks as described. Update the map in this README.

## Running

```bash
# light test suite (no DB; ultralytics-dependent tests error out without it)
~/.cache/viasafe-testvenv/bin/python -m pytest -q
# OpenAPI document
~/.cache/viasafe-testvenv/bin/python -m app.export_openapi
```

The containers (`infra/docker-compose*.yml`) run `uvicorn app.main:app`,
`celery -A app.workers.celery_app worker|beat` and `python -m app.demo.<script>`.
