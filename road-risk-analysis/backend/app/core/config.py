from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # PostgreSQL
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/road_analysis"
    # Connection pool per process (API worker / Celery worker): pool + overflow connections at most.
    # Keep (API_WORKERS + Celery processes) × (size + overflow) under Postgres max_connections (100).
    DB_POOL_SIZE:      int = 5
    DB_MAX_OVERFLOW:   int = 10
    DB_POOL_TIMEOUT_S: int = 30      # wait for a free connection, then 500 instead of hanging
    DB_POOL_RECYCLE_S: int = 1800    # renew connections (server-side idle timeouts, failover)

    # Per-client-IP limit of the sensitive public endpoints (login, sign-up, password reset,
    # MFA, Label Studio images): requests per minute, 0 = off. Counted in Redis (fail-open).
    # CPU threads of the PaddleOCR engine (dashcam overlay text) per worker process; its default (10)
    # competes with the detectors when several jobs run at once.
    OCR_CPU_THREADS:           int = 2
    # Per worker process (one video job): PyTorch threads when OMP_NUM_THREADS is not set, and
    # OpenCV (video decoding) threads. Keep WORKER_CONCURRENCY × (torch + OCR + cv2) ≈ the cores.
    WORKER_TORCH_THREADS:      int = 4
    WORKER_CV2_THREADS:        int = 2

    RATE_LIMIT_AUTH_PER_MIN:   int = 30
    RATE_LIMIT_PUBLIC_PER_MIN: int = 600

    # MinIO
    MINIO_ENDPOINT:          str  = "localhost:9000"
    MINIO_ACCESS_KEY:        str  = "minioadmin"
    MINIO_SECRET_KEY:        str  = "minioadmin"
    MINIO_BUCKET_DETECTIONS:    str  = "detections"
    MINIO_BUCKET_DATASETS:      str  = "datasets"
    MINIO_BUCKET_SIGNALEMENTS:  str  = "signalements"
    MINIO_BUCKET_REPORTS:       str  = "reports"      # generated PDF reports (Module 14), never public
    MINIO_BUCKET_EVENTS:        str  = "events-raw"   # daily event files as received (gzip), never public
    MINIO_SECURE:            bool = False

    # Models paths
    MODEL_POTHOLE:     str = "/models/model_pothole.pt"
    MODEL_SIGNS:       str = "/models/model_signs.pt"
    MODEL_SPEEDBUMP:   str = "/models/model_speedbump.pt"
    # Two-stage pothole pipeline (RRAP-05-010/011): crop classifier that
    # verifies each detector box (P(pothole)). A `<weights>.json` model card
    # next to each .pt is read when present (version, thresholds, imgsz …).
    # Missing / unloadable classifier → single-stage pipeline, as before.
    MODEL_POTHOLE_CLASSIFIER: str = ""   # empty → MODEL_CLASSIFIER (legacy name)
    MODEL_CLASSIFIER:  str = "/models/model_pothole_classifier.pt"   # deprecated alias
    # Two-stage traffic-sign pipeline: MODEL_SIGNS is then a 2-class detector
    # (traffic_sign, traffic_light) and this classifier names each sign crop
    # with a VIASafe sign code (card: per-class thresholds, crop_pad_ratio,
    # taxonomy_version; optional taxonomy.yaml in the same folder). With the
    # legacy multi-class MODEL_SIGNS (or no classifier) → single stage, as before.
    MODEL_SIGNS_CLASSIFIER: str = "/models/model_signs_classifier.pt"
    # Parked / roadside vehicles (app.modules.m05_ai_detection.parked_vehicle): a
    # stock COCO-pretrained YOLOv8 (no fine-tuning) — only its vehicle classes
    # are used (settings `parked_vehicle.vehicle_classes`); "parked?" is decided
    # by rules over the track. Missing file → the `vehicles` model is unavailable.
    MODEL_VEHICLES: str = "/models/model_vehicles.pt"

    # Processing
    TEMP_DIR:            str   = "/tmp/road_analysis"
    FRAME_SKIP:          int   = 5
    CONF_THRESHOLD:      float = 0.25
    IOU_THRESHOLD:       float = 0.4
    DEDUP_DISTANCE_PX:   int   = 50
    # Time zone of the dashcam OSD date-time (camera local time) — used to
    # store detections.recorded_at timezone-aware.
    OSD_TIMEZONE:        str   = "Africa/Douala"

    # Celery / Redis
    CELERY_BROKER_URL:      str = "redis://localhost:6379/0"
    CELERY_RESULT_BACKEND:  str = "redis://localhost:6379/1"

    # Live vehicle telemetry (separate DB index from Celery's broker/backend)
    REDIS_URL:              str = "redis://localhost:6379/2"
    VEHICLE_LIVE_TTL_S:     int = 45   # a vehicle drops off the live set if it stops pinging for this long
    VEHICLE_LAST_SEEN_THROTTLE_S: int = 20  # min interval between Postgres last_seen writes per vehicle

    # Clustering — the radii live in the settings section `clustering`
    # (app.modules.m06_hazards.clustering); this is only the last-resort fallback.
    CLUSTER_RADIUS_M: float = 50.0   # default cluster radius in metres
    # Boundary search of the Cluster configuration page (Nominatim search
    # with polygon_geojson): country filter (ISO codes, comma separated,
    # "" = worldwide), endpoint, timeout.
    CLUSTERING_BOUNDARY_COUNTRYCODES: str = "cm"
    CLUSTERING_BOUNDARY_URL:          str = "https://nominatim.openstreetmap.org/search"
    CLUSTERING_BOUNDARY_TIMEOUT_S:    float = 12.0

    # Reverse geocoding (app.modules.m03_maps.geocoding) — Nominatim / OpenStreetMap.
    # Usage policy: identifying User-Agent (with a contact), ≤ 1 request/s,
    # results cached. GEOCODING_CONTACT (an e-mail) is appended to the
    # User-Agent and sent as `email=`. Disable to keep the platform offline.
    GEOCODING_ENABLED:      bool  = True
    GEOCODING_URL:          str   = "https://nominatim.openstreetmap.org/reverse"
    GEOCODING_CONTACT:      str   = ""
    GEOCODING_LANGUAGE:     str   = ""      # Accept-Language ("" = local names)
    GEOCODING_TIMEOUT_S:    float = 6.0
    GEOCODING_MIN_INTERVAL_S: float = 1.0
    # Back-fill of detections stored before structured addresses existed:
    # at API start-up, at most this many lookups in the background (0 = off).
    GEOCODING_BACKFILL_ON_STARTUP: int = 300

    # Watchdog — beat runs it every 5 min (app.workers.job_tasks.watchdog_stale_jobs): a processing
    # job whose worker heartbeat (Redis, at least every 30 s while it works) is older than this is
    # stale (worker killed / container recreated). A long job that keeps beating is never touched.
    STALE_JOB_MINUTES: int = 10
    # Cancelling a processing job whose worker has not beaten for this long ends it at once
    # (nobody would ever read the cancel request).
    ORPHAN_JOB_SECONDS: int = 180

    # Module 7 — hard upper bound of parallel jobs: the worker's --concurrency
    # (docker-compose.yml reads the same variable). The runtime limit is
    # settings section `processing.max_concurrent_jobs` (≤ this value in practice).
    WORKER_CONCURRENCY: int = 4

    # ── OSRM road-intelligence layer (collision-prevention Step 2) ─────────
    # Defaults to the public OSRM demo server so the integration works out of
    # the box in dev/demo — same server the mobile app already uses for
    # navigation (see fetchRoute in mobile/app/home/map.tsx). Point
    # this at a self-hosted instance once a Cameroon extract is prepared;
    # see osrm/README.md.
    OSRM_BASE_URL:   str   = "https://router.project-osrm.org"
    OSRM_PROFILE:    str   = "driving"
    OSRM_TIMEOUT_S:  float = 5.0

    # Direction: max degrees between vehicle heading and road bearing to call
    # it FORWARD (or within tolerance of the 180°-flipped bearing → REVERSE).
    OSRM_HEADING_TOLERANCE_DEG: float = 45.0

    # Skip re-matching against OSRM if the vehicle hasn't moved far/long
    # enough since its last GOOD/DEGRADED match — avoids hammering OSRM for
    # near-identical consecutive pings (stopped bus, red light, etc.).
    # Deliberately above the mobile app's 3s ping interval (not equal to it)
    # — real-world timing jitter (network latency, JS timer drift) means a
    # ping meant to land "every 3s" routinely arrives a bit later, and an
    # exact-3.0s threshold made cache-reuse miss far more often than
    # intended for an otherwise-stationary vehicle.
    OSRM_MATCH_MIN_INTERVAL_S:  float = 5.0
    OSRM_MATCH_MIN_DISTANCE_M:  float = 15.0
    # Below this, two consecutive points are treated as "the same point" —
    # not fed to /match as a 2-point trace, since OSRM scores a near-zero-
    # distance trace at confidence 0 (see map_matching.py's comment at the
    # call site), which is worse than the single-point /nearest fallback.
    OSRM_MATCH_MIN_TRACE_DISTANCE_M: float = 3.0

    # Per-point search radius given to OSRM's /match (metres) when the
    # device doesn't report its own GPS accuracy.
    OSRM_MATCH_DEFAULT_RADIUS_M: float = 25.0

    # Telemetry older than this is not treated as a current position for
    # map-matching purposes (device offline, clock skew, queued/replayed ping).
    OSRM_STALE_TELEMETRY_S: float = 30.0

    # How far behind/ahead of the matched point to fetch road geometry for —
    # this window is what "current segment" + "upcoming segments" are sliced
    # from (see app/modules/m03_maps/osrm/map_matching.py for why: OSRM has no native
    # concept of a junction-to-junction "segment" over HTTP, so we build one
    # from a bounded route window instead).
    #
    # AHEAD is sized to comfortably cover the Step 3 trajectory horizon (see
    # TRAJECTORY_HORIZONS_S below) at the fastest configured vehicle speed —
    # 45 m/s (moto) × 15s ≈ 675m — plus headroom, so the trajectory predictor
    # rarely has to truncate a prediction just because Step 2 didn't fetch
    # enough road ahead of the vehicle.
    OSRM_SEGMENT_WINDOW_BEHIND_M: float = 150.0
    OSRM_SEGMENT_WINDOW_AHEAD_M:  float = 750.0

    # Map-match confidence thresholds (OSRM's /match "confidence" is 0..1).
    OSRM_CONFIDENCE_GOOD_THRESHOLD:     float = 0.5
    OSRM_CONFIDENCE_DEGRADED_THRESHOLD: float = 0.15

    # Redis TTLs/throttles for the road-aware state pipeline. Vehicle state
    # (changes every ping) is kept separate from road-network lookups
    # (changes rarely) — see app/modules/m10_collision/road_state_service.py.
    ROAD_STATE_TTL_S:          int = 60   # cached RoadAwareVehicleState per vehicle
    ROAD_STATE_DB_THROTTLE_S:  int = 20   # min interval between Postgres road-state writes per vehicle
    OSRM_ROUTE_CACHE_TTL_S:    int = 600  # cached OSRM route-window responses, keyed by rounded position

    # ── Trajectory prediction (collision-prevention Step 3) ────────────────
    # Single source of truth for prediction offsets — nothing else in the
    # codebase should hardcode a horizon value.
    TRAJECTORY_HORIZONS_S: list[float] = [1, 2, 3, 5, 8, 10, 15]

    # Telemetry age bands. <= FRESH is treated as current; between FRESH and
    # STALE the prediction still runs but is marked DEGRADED with falling
    # confidence; beyond STALE we stop extrapolating motion altogether
    # (frozen at the last known position) rather than guess where a vehicle
    # went several seconds of unknown behaviour ago.
    TRAJECTORY_FRESH_TELEMETRY_S: float = 4.0
    TRAJECTORY_STALE_TELEMETRY_S: float = 10.0

    # Physical plausibility limits — protect against sensor-error speeds
    # ("500 km/h") corrupting a prediction. Per-vehicle-type ceiling with a
    # fallback default for unrecognised types.
    TRAJECTORY_MAX_SPEED_MPS_BY_TYPE: dict[str, float] = {
        "bus": 33.0,      # ~120 km/h
        "taxi": 36.0,     # ~130 km/h
        "truck": 30.0,    # ~108 km/h
        "private": 40.0,  # ~144 km/h
        "moto": 45.0,     # ~162 km/h
    }
    TRAJECTORY_MAX_SPEED_MPS_DEFAULT: float = 40.0
    TRAJECTORY_MAX_ACCEL_MPS2: float = 3.5   # hard acceleration ceiling
    TRAJECTORY_MAX_DECEL_MPS2: float = 6.0   # hard braking ceiling (magnitude)

    # Acceleration derived from consecutive speed samples (when the device
    # doesn't report acceleration directly) is only trusted within this
    # sample age — beyond it, treat as no signal and fall back to constant
    # velocity rather than extrapolate from a stale Δv/Δt.
    TRAJECTORY_ACCEL_SAMPLE_MAX_AGE_S: float = 12.0
    TRAJECTORY_ACCEL_SAMPLE_MIN_DT_S:  float = 0.5  # avoid noise amplification from a near-zero Δt

    TRAJECTORY_TTL_S: int = 15  # cached Trajectory per vehicle — roughly one ping interval

    # ── Collision/conflict detection (collision-prevention Step 4) ─────────
    # Only vehicles within this radius of each other's *current* position are
    # even considered as candidates before the more expensive pairwise
    # trajectory comparison runs — mirrors the same "don't compare everything
    # against everything" discipline as Step 2's OSRM windowing. Sized a bit
    # past the widest realistic closing distance over the trajectory horizon
    # (two vehicles at ~45 m/s closing for 15s could approach from ~675m out).
    COLLISION_CANDIDATE_RADIUS_M: float = 700.0

    # Risk thresholds: BOTH the distance-at-closest-approach AND time-to-
    # closest-approach conditions must hold for CRITICAL/HIGH (a car 10m away
    # but not closing for another 40s isn't critical; one 80m away closing in
    # 2s is still worth flagging as HIGH via the distance band it falls in).
    COLLISION_CRITICAL_DISTANCE_M: float = 15.0
    COLLISION_CRITICAL_TTC_S:      float = 4.0
    COLLISION_HIGH_DISTANCE_M:     float = 30.0
    COLLISION_HIGH_TTC_S:          float = 8.0
    COLLISION_MODERATE_DISTANCE_M: float = 60.0
    COLLISION_LOW_DISTANCE_M:      float = 120.0  # beyond this: not reported as a conflict at all

    # Below this confidence (the lower of the two vehicles' trajectory
    # confidences — see Step 3), a conflict is still computed but capped at
    # this quality rather than trusted at face value; too low to report at
    # all it's dropped entirely, matching "do not create false precision".
    COLLISION_MIN_CONFIDENCE_TO_REPORT: float = 0.15

    # A stricter bar than COLLISION_MIN_CONFIDENCE_TO_REPORT, used only to
    # decide whether "these two vehicles share no road segment at all" is
    # trustworthy enough to dampen an otherwise-high distance/TTC severity
    # (see app/modules/m10_collision/collision/detector.py — this system has no
    # elevation data, so an overpass and the road beneath it otherwise look
    # like an imminent head-on). Set to OSRM's own GOOD-match threshold:
    # below that, a "different segment" reading could just as easily be a
    # noisy/degraded match as a genuinely different road, so it isn't
    # trusted enough to suppress anything.
    COLLISION_SEGMENT_TRUST_CONFIDENCE: float = 0.5

    COLLISION_TTL_S: int = 15  # cached VehicleConflictReport per vehicle, mirrors TRAJECTORY_TTL_S

    # ── Prevention & alert engine (collision-prevention Step 5) ────────────
    # TTC bands for alert severity — configuration, not a hardcoded
    # assumption (per spec). A conflict whose time-to-closest-approach is
    # above ALERT_SAFE_TTC_S is not alerted; below that it escalates through
    # the bands down to CRITICAL. Step 4's own distance-based risk_level acts
    # as a floor underneath these (see app/modules/m10_collision/alerts/engine.py) so the
    # two classification layers can never disagree in a way that understates
    # urgency (e.g. a very close pair with a coincidentally large computed
    # TTC still gets treated with real urgency).
    ALERT_SAFE_TTC_S:         float = 15.0  # above this: MONITOR at most
    ALERT_WARNING_TTC_S:      float = 8.0   # above this (and <= SAFE): WARNING
    ALERT_HIGH_WARNING_TTC_S: float = 3.0   # above this (and <= WARNING): HIGH_WARNING; at/below: CRITICAL

    # Cached ConflictAlert per vehicle pair. Deliberately longer than
    # COLLISION_TTL_S so a single missed/slow re-detection cycle doesn't
    # spuriously RESOLVE an alert that's still genuinely active.
    ALERT_TTL_S: int = 90
    # Per-vehicle set of currently-active alert pair-keys, used to detect
    # when a previously-alerted pair has stopped appearing at all (the
    # trigger for a RESOLVED transition) — see app/modules/m10_collision/alert_service.py.
    ALERT_ACTIVE_PAIRS_TTL_S: int = 90
    # Both vehicles in a pair land on the same alert record via their own,
    # independent ping-triggered pipelines — this short-lived per-pair lock
    # stops two concurrent evaluate()s (e.g. both vehicles' pings arriving
    # together) from both reading the same `previous` state and both firing
    # a transition/notification off it. A skipped evaluation is caught by
    # the next tick, so this stays short. See app/modules/m10_collision/alert_service.py.
    ALERT_PAIR_LOCK_TTL_S: int = 3

    # ── Historical data & AI dataset preparation (Step 6.1) ────────────────
    # Aggregation cell size for zone risk profiles — coarse enough that a
    # cell actually accumulates more than one or two events, fine enough to
    # stay locally meaningful (a whole city would wash out any signal).
    HISTORICAL_GRID_CELL_SIZE_M: float = 150.0
    # Default corridor width for "risk along a planned route" queries —
    # matches the existing detections/signalements corridor convention
    # (see AlongRouteQueryIn in app/modules/m06_hazards/schemas.py).
    HISTORICAL_ROUTE_CORRIDOR_M: float = 60.0
    # Weighted-event-count at which a zone's historical_risk_score saturates
    # to 1.0 — deliberately simple/explainable (a normalized count, not a
    # learned score; see app/modules/m08_historical/domain.py's ZoneRiskProfile
    # docstring). Per-type weights live in historical_service.py.
    HISTORICAL_RISK_SATURATION_WEIGHT: float = 20.0

    # Ymane source (app/modules/m08_historical/sources/ymane.py). Credentials
    # intentionally have no real default — set via .env, never commit them.
    # See that module's docstring for the current confirmed request/response
    # shape. The account used during development is currently suspended
    # (repeated logins during manual testing tripped some abuse threshold on
    # Ymane's side) — HISTORICAL_YMANE_SYNC_ENABLED defaults to False for
    # exactly that reason: the periodic sync task (app/workers/celery_app.py)
    # is fully built and safe to leave scheduled, but does nothing until this
    # is explicitly flipped on, so it can never itself hammer a
    # possibly-still-suspended account. Flip it on once the account is
    # confirmed restored.
    YMANE_BASE_URL:       str   = "https://biprod.camtrack.net/ymane"
    YMANE_LOGIN_PATH:     str   = "/noauths/alhumm2-no"
    YMANE_EXCEPTION_PATH: str   = "/api/v2/limitdetailsexception"
    YMANE_USERNAME:       str   = ""
    YMANE_PASSWORD:       str   = ""
    YMANE_TIMEOUT_S:      float = 15.0

    # Periodic sync (Celery Beat, see app/workers/celery_app.py sync_ymane_events).
    # One login per run, reused across all paginated pages within that run —
    # the problem this replaces was calling /historical/import repeatedly
    # on-demand (each call = a fresh login), not the pagination itself.
    HISTORICAL_YMANE_SYNC_ENABLED: bool = False
    HISTORICAL_YMANE_SYNC_INTERVAL_S: float = 6 * 3600.0  # every 6 hours — incident density doesn't need to be fresher
    # First run ever (no prior "ymane" rows to checkpoint from) looks back
    # this far; every run after that starts from MAX(occurred_at) instead.
    HISTORICAL_YMANE_SYNC_INITIAL_LOOKBACK_HOURS: float = 24.0
    # After this many consecutive failed runs, the task stops calling Ymane
    # entirely (logs and returns) until someone resets the failure counter —
    # deliberately low, given we already know this account can be suspended
    # by too many attempts.
    HISTORICAL_YMANE_SYNC_MAX_CONSECUTIVE_FAILURES: int = 2

    # ── Platform security / users (Module 1) ───────────────────────────────
    # Master switch. When False every route stays open exactly as before
    # (used by docker-compose.demo.yml so the simulator and an unconfigured
    # phone keep working); login, users and roles still function.
    AUTH_ENFORCED: bool = True
    # Encrypts secrets stored in the system_settings table (SMTP password,
    # CAPTCHA secret, Google key). MUST be set to a long random value in
    # production — changing it later makes stored secrets unreadable.
    SECRET_KEY: str = "dev-insecure-secret-change-me"
    # Base URL of the web console, used to build password-reset links.
    WEB_BASE_URL: str = "http://localhost:3000"      # public console address: e-mail and report links
    # Base of the Google / Microsoft OAuth redirect URI when it must differ from WEB_BASE_URL:
    # Microsoft accepts plain http only for localhost, so a server without HTTPS connects its
    # drives once through an SSH tunnel (http://localhost:3000). Empty = WEB_BASE_URL.
    CLOUD_OAUTH_BASE_URL: str = ""
    # Comma-separated browser origins allowed by CORS. The web console goes
    # through its server-side proxy and the native app is not subject to
    # CORS, so only dev origins are needed here by default.
    CORS_ORIGINS: str = "http://localhost:3000,http://localhost:8081"
    # First administrator, created on startup only when no user exists yet.
    BOOTSTRAP_ADMIN_EMAIL:    str = ""
    BOOTSTRAP_ADMIN_PASSWORD: str = ""
    # Self-registration from the mobile app (POST /auth/mobile/register):
    # attempts allowed per client IP per hour (Redis counter).
    MOBILE_REGISTER_PER_HOUR: int = 5

    class Config:
        env_file = ".env"
        extra = "ignore"

    @property
    def pothole_classifier_path(self) -> str:
        """MODEL_POTHOLE_CLASSIFIER, falling back to the legacy MODEL_CLASSIFIER."""
        return self.MODEL_POTHOLE_CLASSIFIER or self.MODEL_CLASSIFIER


settings = Settings()
