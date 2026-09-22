from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # PostgreSQL
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/road_analysis"

    # MinIO
    MINIO_ENDPOINT:          str  = "localhost:9000"
    MINIO_ACCESS_KEY:        str  = "minioadmin"
    MINIO_SECRET_KEY:        str  = "minioadmin"
    MINIO_BUCKET_DETECTIONS:    str  = "detections"
    MINIO_BUCKET_DATASETS:      str  = "datasets"
    MINIO_BUCKET_SIGNALEMENTS:  str  = "signalements"
    MINIO_SECURE:            bool = False

    # Models paths
    MODEL_POTHOLE:     str = "/models/model_pothole.pt"
    MODEL_SIGNS:       str = "/models/model_signs.pt"
    MODEL_SPEEDBUMP:   str = "/models/model_speedbump.pt"
    MODEL_CLASSIFIER:  str = "/models/model_pothole_classifier.pt"

    # Processing
    TEMP_DIR:            str   = "/tmp/road_analysis"
    FRAME_SKIP:          int   = 5
    CONF_THRESHOLD:      float = 0.25
    IOU_THRESHOLD:       float = 0.4
    DEDUP_DISTANCE_PX:   int   = 50

    # Celery / Redis
    CELERY_BROKER_URL:      str = "redis://localhost:6379/0"
    CELERY_RESULT_BACKEND:  str = "redis://localhost:6379/1"

    # Live vehicle telemetry (separate DB index from Celery's broker/backend)
    REDIS_URL:              str = "redis://localhost:6379/2"
    VEHICLE_LIVE_TTL_S:     int = 45   # a vehicle drops off the live set if it stops pinging for this long
    VEHICLE_LAST_SEEN_THROTTLE_S: int = 20  # min interval between Postgres last_seen writes per vehicle

    # Clustering
    CLUSTER_RADIUS_M: float = 50.0   # default cluster radius in metres

    # Watchdog
    STALE_JOB_MINUTES: int = 60

    # ── OSRM road-intelligence layer (collision-prevention Step 2) ─────────
    # Defaults to the public OSRM demo server so the integration works out of
    # the box in dev/demo — same server the mobile app already uses for
    # navigation (see fetchRoute in viasafe-mobile/app/home/map.tsx). Point
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
    # from (see app/services/osrm/map_matching.py for why: OSRM has no native
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
    # (changes rarely) — see app/services/road_state_service.py.
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

    COLLISION_TTL_S: int = 15  # cached VehicleConflictReport per vehicle, mirrors TRAJECTORY_TTL_S

    # ── Prevention & alert engine (collision-prevention Step 5) ────────────
    # TTC bands for alert severity — configuration, not a hardcoded
    # assumption (per spec). A conflict whose time-to-closest-approach is
    # above ALERT_SAFE_TTC_S is not alerted; below that it escalates through
    # the bands down to CRITICAL. Step 4's own distance-based risk_level acts
    # as a floor underneath these (see app/services/alerts/engine.py) so the
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
    # trigger for a RESOLVED transition) — see app/services/alert_service.py.
    ALERT_ACTIVE_PAIRS_TTL_S: int = 90

    # ── Historical data & AI dataset preparation (Step 6.1) ────────────────
    # Aggregation cell size for zone risk profiles — coarse enough that a
    # cell actually accumulates more than one or two events, fine enough to
    # stay locally meaningful (a whole city would wash out any signal).
    HISTORICAL_GRID_CELL_SIZE_M: float = 150.0
    # Default corridor width for "risk along a planned route" queries —
    # matches the existing detections/signalements corridor convention
    # (see AlongRouteQueryIn in app/schemas/schemas.py).
    HISTORICAL_ROUTE_CORRIDOR_M: float = 60.0
    # Weighted-event-count at which a zone's historical_risk_score saturates
    # to 1.0 — deliberately simple/explainable (a normalized count, not a
    # learned score; see app/services/historical/models.py's ZoneRiskProfile
    # docstring). Per-type weights live in historical_service.py.
    HISTORICAL_RISK_SATURATION_WEIGHT: float = 20.0

    # Ymane source (app/services/historical/sources/ymane.py). Credentials
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

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
