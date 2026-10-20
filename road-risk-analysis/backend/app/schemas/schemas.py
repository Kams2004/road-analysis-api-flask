import logging
import os
from pydantic import BaseModel, Field, field_validator, model_validator
from datetime import datetime
from typing import Any, Literal, Optional, List
from app.models.job import JobStatus
from app.models.detection import ReviewStatus
from app.models.validation_label import SEVERITY_MIN, SEVERITY_MAX
from app.models.signalement import SignalementType, SignalementStatus
from app.models.vehicle import VehicleType

_schemas_logger = logging.getLogger(__name__)
SPEED_MPS_CEILING = 100.0


class JobOut(BaseModel):
    """A video/image processing job (Module 7). Statuses: `pending` (queued) → `processing` →
    `done` | `failed` | `cancelled`."""
    id:             str = Field(description="Job id (UUID).", examples=["7d0c5b9e-2f1a-4c3d-8e6b-1a2b3c4d5e6f"])
    filename:       str = Field(description="Original file name.", examples=["dashcam_2026-03-02_0958.mp4"])
    status:         JobStatus = Field(description="`pending`, `processing`, `done`, `failed` or `cancelled`.",
                                      examples=["processing"])
    enabled_models: Optional[str] = Field(description="Comma list of the models run (`pothole`, `signs`, "
                                                      "`speedbump`, `vehicles`).", examples=["pothole,signs"])
    total_frames:   int = Field(description="Frames in the media (0 until known).", examples=[5400])
    processed:      int = Field(description="Frames processed so far.", examples=[2700])
    detections:     int = Field(description="Detections produced so far.", examples=[14])
    error:          Optional[str] = Field(description="Failure reason (status `failed`).")
    created_at:     datetime = Field(description="Submission time (UTC).")
    finished_at:    Optional[datetime] = Field(description="End of processing (UTC), whatever the outcome.")

    # Module 7 — source, lifecycle and metrics
    source_type:         str                      = Field("upload", description="`upload`, `google_drive`, "
                                                          "`onedrive` or `stream`.")
    source_ref:          Optional[str]            = Field(None, description="Provider file id (cloud imports).")
    source_name:         Optional[str]            = Field(None, description="Source file / segment name.")
    connection_id:       Optional[str]            = Field(None, description="Cloud connection it was imported from.")
    stream_id:           Optional[str]            = Field(None, description="Stream it was recorded from.")
    submitted_by:        Optional[str]            = Field(None, description="User (or `system`) that created it.")
    media_retained:      bool                     = Field(False, description="The source file is still on disk "
                                                          "(needed to retry an upload).")
    started_at:          Optional[datetime]       = Field(None, description="Start of processing (UTC).")
    cancel_requested:    bool                     = Field(False, description="A cancel was requested while "
                                                          "processing; the worker stops at its next check.")
    cancelled_at:        Optional[datetime]       = Field(None, description="Cancellation time (UTC).")
    cancelled_by:        Optional[str]            = Field(None, description="Who cancelled it.")
    attempts:            int                      = Field(1, description="Current attempt number.")
    max_attempts:        int                      = Field(3, description="Retries are refused once `attempts` "
                                                          "reaches this.")
    avg_confidence:      Optional[float]          = Field(None, description="Mean confidence of its detections (0–1).")
    frame_skip:          Optional[int]            = Field(None, description="One frame out of N was processed.")
    detections_by_model: Optional[dict[str, int]] = Field(None, description="Detections per model.",
                                                          examples=[{"pothole": 10, "signs": 4}])
    fps:                 Optional[float]          = Field(None, description="Video frame rate.")
    duration_s:          Optional[float]          = Field(None, description="Video duration, in seconds.")
    processing_seconds:  Optional[float]          = Field(None, description="Processing time so far, in seconds "
                                                          "((finished_at or now) − started_at).")
    progress:            float                    = Field(0.0, description="processed / total_frames, 0–1.",
                                                          examples=[0.5])

    class Config:
        from_attributes = True

    @model_validator(mode="before")
    @classmethod
    def _computed(cls, data):
        if isinstance(data, dict):
            return data
        from app.services.job_service import progress, processing_seconds
        out = {name: getattr(data, name, None) for name in cls.model_fields
               if name not in ("processing_seconds", "progress", "media_retained")}
        out = {k: v for k, v in out.items() if v is not None or k in (
            "enabled_models", "error", "finished_at")}
        out["processing_seconds"] = processing_seconds(data)
        out["progress"] = progress(data)
        path = getattr(data, "source_path", None)
        out["media_retained"] = bool(path and os.path.exists(path))
        return out


class JobEventOut(BaseModel):
    """One lifecycle event of a job."""
    id:      int = Field(description="Event id.")
    at:      datetime = Field(description="When it happened (UTC).")
    event:   str = Field(description="`created`, `queued`, `started`, `cancel_requested`, `cancelled`, "
                                     "`failed`, `retried`, `done` …", examples=["queued"])
    actor:   Optional[str] = Field(description="User, or `system` for the worker.")
    details: Optional[dict] = Field(description="Event-specific data (task id, attempt, error …).",
                                    examples=[{"task_id": "b1f…", "attempt": 1}])

    class Config:
        from_attributes = True


class JobDetailOut(JobOut):
    """A job with its lifecycle events."""
    events: List[JobEventOut] = Field([], description="Lifecycle events, newest first.")


class JobListOut(BaseModel):
    """A page of jobs."""
    total: int = Field(description="Number of matching jobs (before paging).", examples=[57])
    items: List[JobOut] = Field(description="The jobs of this page.")


class JobCapacityOut(BaseModel):
    """Processing capacity and current load."""
    max_concurrent_jobs: int = Field(description="Jobs processed at the same time (setting "
                                                 "`processing.max_concurrent_jobs`).", examples=[2])
    running:             int = Field(description="Jobs in status `processing`.", examples=[1])
    pending:             int = Field(description="Jobs in status `pending` (queued).", examples=[3])
    worker_concurrency:  int = Field(description="Worker processes of the deployment.", examples=[2])


class JobSummaryOut(BaseModel):
    """Job counts per status (every status present)."""
    by_status: dict[str, int] = Field(description="All jobs, per status.",
                                      examples=[{"pending": 1, "processing": 1, "done": 40, "failed": 2,
                                                 "cancelled": 0}])
    last_24h:  dict[str, int] = Field(description="Jobs created in the last 24 hours, per status.",
                                      examples=[{"pending": 1, "processing": 1, "done": 5, "failed": 0,
                                                 "cancelled": 0}])


class ClassCandidateOut(BaseModel):
    """One alternative proposed by a crop classifier."""
    code:  str   = Field(description="Class code (traffic signs: VIASafe sign code).", examples=["speed_limit_50"])
    score: float = Field(description="Classifier probability (0–1).", examples=[0.82])


class DetectionOut(BaseModel):
    """An AI detection (Modules 5 & 6) with its evidence, position and review state."""
    id:            str = Field(description="Detection id (UUID).", examples=["3f2b7c1e-9a4d-4e0b-8c51-2f6d7a9e1b30"])
    job_id:        str = Field(description="Job that produced it.")
    type:          str = Field(description="Class (possibly corrected by a validator): `pothole`, "
                                           "`traffic_sign`, `speed_bump` …", examples=["pothole"])
    subtype:       Optional[str] = Field(description="Subclass, e.g. the traffic-sign code (`speed_limit_50`, "
                                                     "`stop`, `traffic_light`, `unknown_sign` …; see "
                                                     "`GET /models/signs/taxonomy`).")
    confidence:    float = Field(description="Model confidence (0–1).", examples=[0.87])
    frame_number:  int = Field(description="Frame index in the source video.", examples=[1520])
    raw_gps_text:  Optional[str] = Field(description="GPS text read by OCR on the frame's OSD (shown to validators).",
                                         examples=["0515.4260,N,01013.5383,E,028KM/H"])
    latitude:      Optional[float] = Field(description="WGS84 latitude (`null` = unknown, must be fixed before "
                                                       "acceptance).", examples=[4.0512])
    longitude:     Optional[float] = Field(description="WGS84 longitude.", examples=[9.7085])
    speed_kmh:     Optional[float] = Field(description="Vehicle speed read on the OSD, in km/h.", examples=[28.0])
    vehicle_id:    Optional[str] = Field(description="Vehicle that recorded the video, when known.")
    captured_at:   Optional[datetime] = Field(description="Capture time from the OSD (UTC).")
    image_url:        Optional[str] = Field(description="Annotated detection frame.")
    crop_url:         Optional[str] = Field(description="Clean, unannotated crop (training sample).")
    context_clip_url: Optional[str] = Field(description="Short video clip around the detection — present when "
                                                        "the location was not confirmed.")
    frame_clean_url:  Optional[str] = Field(None, description="Clean full frame without any box drawn (detector "
                                                              "training image); `null` for detections stored "
                                                              "before it existed.")
    frame_width:      Optional[int] = Field(None, description="Width of the frame in pixels (box coordinates are "
                                                              "in this frame).", examples=[720])
    frame_height:     Optional[int] = Field(None, description="Height of the frame in pixels.", examples=[576])
    corrected_bbox:   Optional[list[float]] = Field(None, description="Box moved / resized by a validator, "
                                                                      "`[x1, y1, x2, y2]` in frame pixels "
                                                                      "(`bbox_*` keep the model's box); `null` "
                                                                      "= the predicted box is right.",
                                                    examples=[[212.0, 388.5, 301.0, 440.0]])
    location_name: Optional[str] = Field(description="Reverse-geocoded place name.", examples=["Rue Joss, Douala"])
    rpm:           Optional[int] = Field(description="Engine RPM read on the OSD, when present.")
    review_status: ReviewStatus = Field(description="`location_pending`, `pending`, `validated`, `corrected`, "
                                                    "`rejected` or `resolved` (see the *Detections* tag).",
                                        examples=["pending"])
    reviewed_by:   Optional[str] = Field(description="Last reviewer (`system` for automatic decisions).")
    reviewed_at:   Optional[datetime] = Field(description="Last review time (UTC).")
    review_note:   Optional[str] = Field(description="Reviewer's note.")
    resolved_at:   Optional[datetime] = Field(description="When it was resolved (UTC).")
    created_at:    datetime = Field(description="Creation time (UTC).")

    # Module 6 — location quality, traceability, class correction, rejection, hazard
    gps_interpolated:  Optional[bool]  = Field(None, description="Position interpolated from neighbouring frames.")
    location_status:   Optional[str]   = Field(None, description="`confirmed`, `approximate` (interpolated proposal) "
                                                                 "or `missing`.", examples=["confirmed"])
    location_source:   Optional[str]   = Field(None, description="`ocr`, `interpolated`, `map_pin`, `trajectory`, "
                                                                 "`ocr_text` or `none`.", examples=["ocr"])
    country:           Optional[str]   = Field(None, description="Reverse-geocoded country.", examples=["Cameroon"])
    city:              Optional[str]   = Field(None, description="Reverse-geocoded city.", examples=["Douala"])
    road:              Optional[str] = Field(None, description="Reverse-geocoded street.", examples=["Rue Joss"])
    locality:          Optional[str] = Field(None, description="Quartier / neighbourhood / suburb.", examples=["Akwa"])
    district:          Optional[str] = Field(None, description="Arrondissement / city district.", examples=["Douala Ier"])
    town:              Optional[str] = Field(None, description="Town / city / village.", examples=["Douala"])
    region:            Optional[str] = Field(None, description="Region / state.", examples=["Littoral"])
    display_location:  Optional[str] = Field(None, description="Short human-readable place: street, quartier, town.",
                                             examples=["Rue Joss, Akwa, Douala"])
    bbox_x1:           Optional[float] = Field(None, description="Bounding box left, in frame pixels.")
    bbox_y1:           Optional[float] = Field(None, description="Bounding box top, in frame pixels.")
    bbox_x2:           Optional[float] = Field(None, description="Bounding box right, in frame pixels.")
    bbox_y2:           Optional[float] = Field(None, description="Bounding box bottom, in frame pixels.")
    video_time_s:      Optional[float] = Field(None, description="Position in the video, in seconds.")
    model_name:        Optional[str]   = Field(None, description="Model that produced it.")
    model_version:     Optional[str]   = Field(None, description="Weights version (`file@sha256[:12]`).")
    model_card_version: Optional[str]  = Field(None, description="Detector model card `name:version`, when the "
                                                                 "weights came with a card.", examples=["pothole_det:v1"])
    verifier_score:    Optional[float] = Field(None, description="Two-stage pipeline: crop classifier probability "
                                                                 "that the box really is this class (0–1); `null` "
                                                                 "when no classifier is installed for the type.",
                                               examples=[0.94])
    verifier_model:    Optional[str]   = Field(None, description="Classifier weights version (`file@sha256[:12]`).")
    verifier_card_version: Optional[str] = Field(None, description="Classifier model card `name:version`.",
                                                 examples=["pothole_cls:v1"])
    ai_decision:       Optional[str]   = Field(None, description="Decision of the AI validation mode at creation: "
                                                                 "`auto_accept`, `auto_reject` or `review` (`null` "
                                                                 "for detections created before it). With "
                                                                 "`audit_sample` it is only a suggestion.",
                                               examples=["auto_accept"])
    ai_decision_reason: Optional[str]  = Field(None, description="Short explanation (thresholds that fired, "
                                                                 "manual-mode suggestion …).",
                                               examples=["detector 0.91 ≥ 0.80, classifier 0.97 ≥ 0.90"])
    audit_sample:      Optional[bool]  = Field(None, description="`true` = an automatic decision drawn for a human "
                                                                 "audit: left pending, the AI decision is a "
                                                                 "suggestion to confirm or overturn.")
    class_candidates:  Optional[list[ClassCandidateOut]] = Field(
        None, description="Two-stage traffic-sign pipeline: the crop classifier's top-3 sign codes with their "
                          "probability, best first (`verifier_score` = the first score). Offered as one-click "
                          "choices when correcting the subtype. `null` for other types and for the legacy "
                          "single-stage sign model.",
        examples=[[{"code": "speed_limit_50", "score": 0.82}, {"code": "speed_limit_30", "score": 0.11},
                   {"code": "speed_limit_60", "score": 0.04}]])
    ai_details:        Optional[dict[str, Any]] = Field(
        None, description="Evidence of rule-based pipelines — parked vehicles (`type` `parked_vehicle`): `verdict`, "
                          "`severity` (`low`…`critical`; detections have no severity column), `vehicle_class`, "
                          "`score` (= `verifier_score`) and `score_components`, `motion`, `fallback`, "
                          "`reasons` [{`code`, `value`, `threshold`, `passed`, `detail`}] (one per rule, `passed` "
                          "`null` = skipped), `features`, `target_position`, `road_distance_m`. `null` otherwise.",
        examples=[{"verdict": "parked", "subtype": "roadside", "severity": "low", "vehicle_class": "car",
                   "score": 0.86, "reasons": [{"code": "target_speed", "value": 1.2, "threshold": 13.0,
                                               "passed": True, "detail": ""}]}])
    recorded_at:       Optional[datetime] = Field(None, description="Video recording time of the detection "
                                                                    "(timezone-aware): OSD date-time of the frame, "
                                                                    "else extrapolated from the video's nearest OSD "
                                                                    "time; `null` when unknown.")
    predicted_type:    Optional[str]   = Field(None, description="Class originally predicted by the model.")
    predicted_subtype: Optional[str]   = Field(None, description="Subclass originally predicted by the model "
                                                                 "(traffic signs: the VIASafe sign code, "
                                                                 "`traffic_light`, or `unknown_sign` when the "
                                                                 "classifier was not sure).")
    rejection_code:    Optional[str]   = Field(None, description="Rejection reason code (status `rejected`).",
                                               examples=["low_confidence"])
    rejection_source:  Optional[str]   = Field(None, description="`human` or `auto`.")
    rejection_label:   Optional[str]   = Field(None, description="Label of the rejection reason.")
    hazard_id:         Optional[str]   = Field(None, description="Hazard it belongs to (accepted detections).")
    alert_priority:    Optional[str]   = Field(None, description="Mobile queries only (`/detections/nearby`, "
                                                                 "`/detections/along-route`): alert priority of "
                                                                 "this hazard type/subtype (`low`, `normal`, "
                                                                 "`high`), from `hazard_lifecycle` policies.",
                                               examples=["high"])
    hazard_code:       Optional[str]   = Field(None, description="Code of that hazard.", examples=["HZ-000012"])

    class Config:
        from_attributes = True


class DetectionListOut(BaseModel):
    """A list of detections."""
    total: int = Field(description="Number of matching detections (before paging, for paginated endpoints).",
                       examples=[134])
    items: List[DetectionOut] = Field(description="The detections.")


class ReviewIn(BaseModel):
    """Legacy body of PATCH /detections/{id}/review (validated | rejected)."""
    status:         ReviewStatus = Field(description="`validated` or `rejected` (other values → 422).",
                                         examples=["validated"])
    reviewed_by:    Optional[str] = Field(None, description="Reviewer name (used when the caller is not a signed-in user).")
    note:           Optional[str] = Field(None, description="Reviewer's note.")
    label:          Optional[str] = Field(None, description="Training label — required when validating.",
                                          examples=["pothole"])
    severity_score: Optional[int] = Field(None, description="Severity 0–3 — required when validating a pothole.",
                                          examples=[2])

    @field_validator("severity_score")
    @classmethod
    def check_range(cls, v):
        if v is not None and not (SEVERITY_MIN <= v <= SEVERITY_MAX):
            raise ValueError(f"severity_score must be between {SEVERITY_MIN} and {SEVERITY_MAX}")
        return v


class ValidationLabelOut(BaseModel):
    """Training label of an accepted or corrected detection (classifier dataset)."""
    id:               str = Field(description="Label id (UUID).")
    detection_id:     str = Field(description="Detection it labels.")
    detection_type:   str = Field(description="Detection type.", examples=["pothole"])
    label:            str = Field(description="Class name chosen by the validator.", examples=["pothole"])
    severity_score:   int = Field(description="Severity 0–3.", examples=[2])
    model_confidence: float = Field(description="Model confidence of the detection (0–1).", examples=[0.87])
    crop_url:         Optional[str] = Field(description="Training crop.")
    labeled_by:       str = Field(description="Validator.")
    labeled_at:       datetime = Field(description="Labelling time (UTC).")

    class Config:
        from_attributes = True


class LocationCorrectIn(BaseModel):
    """
    Validator submits the corrected raw GPS text as read from the frame image.
    Format must be valid NMEA: e.g. '0515.4260,N,01013.5383,E,028KM/H'
    The server transforms it to decimal lat/lon and saves both.
    """
    raw_gps_text: str = Field(description="GPS text as read on the frame's OSD (NMEA).",
                              examples=["0515.4260,N,01013.5383,E,028KM/H"])
    reviewed_by:  Optional[str] = Field(None, description="Reviewer name (used when the caller is not a signed-in user).")


# ─── Spatial query schemas (used by the mobile app) ──────────────────────────

class NearbyQueryIn(BaseModel):
    """
    Circle query: return detections within `radius_m` metres of the given point.
    """
    latitude:  float = Field(description="WGS84 latitude of the centre.", examples=[4.0511])
    longitude: float = Field(description="WGS84 longitude of the centre.", examples=[9.7679])
    radius_m:  Optional[float] = Field(None, description="Radius in metres; default and maximum: the "
                                                         "`mobile.visibility_radius_m` setting.", examples=[2000])


class RouteWaypoint(BaseModel):
    """A point of the route polyline."""
    latitude:  float = Field(description="WGS84 latitude.", examples=[4.0511])
    longitude: float = Field(description="WGS84 longitude.", examples=[9.7679])


class AlongRouteQueryIn(BaseModel):
    """
    Corridor query: return detections that lie within `corridor_m` metres of
    any segment of the supplied polyline.
    `waypoints` must contain at least 2 points (the full OSRM route geometry).
    """
    waypoints:   List[RouteWaypoint] = Field(description="Route polyline, at least 2 points, in driving order.",
                                             examples=[[{"latitude": 4.0511, "longitude": 9.7679},
                                                        {"latitude": 4.0620, "longitude": 9.7350}]])
    corridor_m:  Optional[float] = Field(None, description="Half-width of the corridor in metres (each side of the "
                                                           "route); default: the `mobile.route_corridor_m` setting.",
                                         examples=[50])


class DatasetExportIn(BaseModel):
    """Request to build a new dataset version."""
    detection_type: str = Field(description="`pothole`, `traffic_sign` or `speed_bump`.", examples=["pothole"])
    created_by:     str = Field(description="Name recorded as the dataset's author.", examples=["j.doe"])


class DatasetOut(BaseModel):
    """An immutable, versioned training dataset."""
    id:             str = Field(description="Dataset id (UUID).")
    detection_type: str = Field(description="Detection type.", examples=["pothole"])
    version:        int = Field(description="Version number, per detection type (1, 2, …).", examples=[3])
    total_samples:  int = Field(description="Number of labelled samples.", examples=[1250])
    class_counts:   dict = Field(description="Samples per label.", examples=[{"pothole": 1100, "crack": 150}])
    manifest_url:   Optional[str] = Field(description="Manifest JSON (one entry per sample).")
    metadata_url:   Optional[str] = Field(description="Metadata JSON (version info, class distribution).")
    created_by:     str = Field(description="Author.")
    created_at:     datetime = Field(description="Creation time (UTC).")

    class Config:
        from_attributes = True


# ─── Signalement schemas ──────────────────────────────────────────────────────

class SignalementIn(BaseModel):
    """Fields of a community report (the create endpoint takes them as multipart form fields)."""
    type:        SignalementType = Field(..., description="Report type.", examples=["police"])
    latitude:    float = Field(..., description="WGS84 latitude (degrees).", examples=[4.0511])
    longitude:   float = Field(..., description="WGS84 longitude (degrees).", examples=[9.7679])
    description: Optional[str] = Field(None, description="Free-text comment from the reporter.")
    reported_by: Optional[str] = Field(None, description="Reporter name (replaced by the signed-in user's e-mail).")


class SignalementOut(BaseModel):
    """A community report (*signalement*) with its moderation state, community-vote tallies and
    freshness. Counts only include **counted** votes (cast within `community.vote_max_distance_m`, one
    per voter)."""
    id:              str = Field(..., description="Report id (UUID).")
    type:            SignalementType = Field(..., description="Report type.", examples=["police"])
    status:          SignalementStatus = Field(..., description=(
        "`actif` (active), `annule` (cancelled by an admin or hidden by the community), `rejete` (rejected "
        "by an admin)."), examples=["actif"])
    latitude:        float = Field(..., description="WGS84 latitude (degrees).", examples=[4.0511])
    longitude:       float = Field(..., description="WGS84 longitude (degrees).", examples=[9.7679])
    location_name:   Optional[str] = Field(description="Place name reverse-geocoded at creation.",
                                           examples=["Akwa, Douala"])
    blocked_bearing: Optional[float] = Field(description=(
        "Blocked direction of travel, degrees clockwise from north (0–360), for closures."))
    confirmations:   int = Field(description="Legacy mirror of `present_count`.")
    not_there_votes: int = Field(description="Legacy mirror of `absent_count`.")
    description:     Optional[str] = Field(description="Reporter's comment.")
    image_url:       Optional[str] = Field(description="URL of the attached photo, if any.")
    audio_url:       Optional[str] = Field(description="URL of the attached voice note, if any.")
    reported_by:     Optional[str] = Field(description="Reporter (signed-in user's e-mail, or the name sent).")
    moderated_by:    Optional[str] = Field(description=(
        "Who last changed the status (`community` when hidden automatically by votes)."))
    moderated_at:    Optional[datetime] = Field(description="When the status was last changed (UTC).")
    moderation_note: Optional[str] = Field(description="Moderation note.")
    reported_at:     datetime = Field(description="Creation time (UTC).")
    # Community votes — counted votes only (cast from within
    # community.vote_max_distance_m, one per voter); invalid = too far / no position.
    present_count:      int = Field(0, description="Counted \"still there\" votes.")
    absent_count:       int = Field(0, description=(
        "Counted \"not there\" votes; at `community.hide_after_absent_votes` the report is set to `annule`."))
    invalid_vote_count: int = Field(0, description="Votes not counted because cast too far away or without a position.")
    last_confirmed_at:  Optional[datetime] = Field(None, description="Time of the last counted \"still there\" vote (UTC).")
    # Devices that passed nearby without voting (community.ignore_window_minutes)
    ignore_count:       int = Field(0, description=(
        "Devices that passed by within `community.ignore_window_minutes` without voting."))
    uncertain:          bool = Field(False, description=(
        "`ignore_count` reached `community.ignore_threshold` (and fewer than "
        "`community.keep_visible_present_votes` present votes)."))
    # Clustering (global Cluster Configuration — GET /clustering/config)
    merged_into_id:     Optional[str] = Field(None, description=(
        "Set when this report was merged into an existing active report of the same type within the "
        "clustering radius (that report's id); merged reports are not listed by the app searches."))
    report_count:       int = Field(1, description="Reports clustered into this one (itself included).")
    last_reported_at:   Optional[datetime] = Field(None, description=(
        "Time of the latest report merged into this one (UTC); restarts the freshness."))
    cluster_radius_m:   Optional[float] = Field(None, description=(
        "Clustering radius resolved at the report's position for its type, metres."))
    cluster_environment: Optional[Literal["urban", "rural"]] = Field(None, description=(
        "Profile of the resolution: `urban` (in agglomeration) or `rural` (out of agglomeration)."))
    cluster_zone_id:    Optional[int] = Field(None, description="Cluster zone of the resolution (null = unzoned).")
    # Freshness (community.expiry_hours_by_type + confirm_extends_minutes)
    expires_at:         Optional[datetime] = Field(None, description=(
        "When the report stops being shown: `reported_at` + the type's expiry, or `last_confirmed_at` + "
        "`community.confirm_extends_minutes`, whichever is later (UTC)."))
    expired:            bool = Field(False, description="`expires_at` is in the past.")

    class Config:
        from_attributes = True


class SignalementDetailOut(SignalementOut):
    """A report plus the caller's own vote."""
    my_vote: Optional[Literal["present", "absent"]] = Field(None, description=(
        "The caller's current counted vote (signed-in user, else `device_id`), or null."))


_VOTER_POS = "Voter's current WGS84 {} (degrees); without a position the vote is stored but not counted."


class SignalementVoteIn(BaseModel):
    """A "Still there?" vote. Counted only when the position is within `community.vote_max_distance_m`
    metres of the report; one counted vote per voter."""
    vote:      Literal["present", "absent"] = Field(..., description=(
        "`present` = still there, `absent` = not there any more."), examples=["present"])
    latitude:  Optional[float] = Field(None, description=_VOTER_POS.format("latitude"), examples=[4.0512])
    longitude: Optional[float] = Field(None, description=_VOTER_POS.format("longitude"), examples=[9.7681])
    device_id: Optional[str]   = Field(None, description=(  # vehicle / device id (identifies an anonymous voter)
        "Device or vehicle id identifying an anonymous voter (ignored for identity when a user is signed "
        "in). Truncated to 128 characters."), examples=["veh-3f2a"])


class SignalementLegacyVoteIn(BaseModel):
    """Optional body of /confirm and /not-there (older app versions send none)."""
    latitude:  Optional[float] = Field(None, description=_VOTER_POS.format("latitude"))
    longitude: Optional[float] = Field(None, description=_VOTER_POS.format("longitude"))
    device_id: Optional[str]   = Field(None, description="Device id identifying an anonymous voter.")


class SignalementVoteResultOut(BaseModel):
    """Outcome of a vote."""
    signalement: SignalementOut = Field(..., description="The report after the vote (status may now be `annule`).")
    my_vote:     Optional[Literal["present", "absent"]] = Field(None, description=(
        "The voter's current counted vote — this one if counted, else their earlier counted vote, if any."))
    counted:     bool = Field(..., description="Whether this vote counts.")
    reason:      Optional[Literal["too_far", "duplicate", "no_position"]] = Field(None, description=(
        "Why the vote does not count: `too_far` (beyond `community.vote_max_distance_m`), `no_position` "
        "(no/invalid position). `duplicate` marks a vote replaced by a later one. Null when counted."))
    distance_m:  Optional[float] = Field(None, description="Distance from the voter to the report, metres.")


class SignalementPassIn(BaseModel):
    """A pass-by impression: the device came near a displayed report."""
    device_id: Optional[str]   = Field(None, description=(  # required unless a user is signed in
        "Device id — required unless a user is signed in (`422 device_id_required`)."), examples=["veh-3f2a"])
    latitude:  Optional[float] = Field(None, description="Device's WGS84 latitude (degrees).")
    longitude: Optional[float] = Field(None, description="Device's WGS84 longitude (degrees).")


class SignalementPassResultOut(BaseModel):
    """Outcome of a pass-by impression."""
    signalement: SignalementOut = Field(..., description="The report after the pass (`ignore_count`, `uncertain`).")
    counted:     bool = Field(..., description="The pass was within `community.vote_max_distance_m`.")
    distance_m:  Optional[float] = Field(None, description="Distance from the device to the report, metres.")
    duplicate:   bool = Field(False, description=(
        "A pass was already recorded for this device in the current ignore window."))


class SignalementPassOut(BaseModel):
    """A recorded pass-by (admin view)."""
    id:          str = Field(..., description="Pass id.")
    device_id:   Optional[str] = Field(description="Device id.")
    user_id:     Optional[str] = Field(description="Signed-in user id, if any.")
    voter:       Optional[str] = Field(None, description="Display label: e-mail if signed in, else device id.")
    latitude:    Optional[float] = Field(description="WGS84 latitude (degrees).")
    longitude:   Optional[float] = Field(description="WGS84 longitude (degrees).")
    distance_m:  Optional[float] = Field(description="Distance to the report, metres.")
    counted:     bool = Field(..., description="Within `community.vote_max_distance_m`.")
    voted:       bool = Field(..., description="This device/user also voted on the report (so not an ignore).")
    created_at:  datetime = Field(..., description="When the pass was recorded (UTC).")


class SignalementVoteOut(BaseModel):
    """A stored vote (admin view), counted or not."""
    id:              str = Field(..., description="Vote id.")
    vote:            str = Field(..., description="`present` or `absent`.", examples=["absent"])
    voter_user_id:   Optional[str] = Field(description="Signed-in voter's user id.")
    voter_email:     Optional[str] = Field(None, description="Signed-in voter's e-mail.")
    voter_device_id: Optional[str] = Field(description="Voter's device id.")
    voter:           Optional[str] = Field(None, description="Display label: e-mail if signed in, else device id.")
    latitude:        Optional[float] = Field(description="Voter's WGS84 latitude (degrees).")
    longitude:       Optional[float] = Field(description="Voter's WGS84 longitude (degrees).")
    distance_m:      Optional[float] = Field(description="Distance to the report, metres (null without position).")
    counted:         bool = Field(..., description="Whether the vote counts.")
    reason:          Optional[str] = Field(description="`too_far`, `no_position`, `duplicate` (replaced) or null.")
    created_at:      datetime = Field(..., description="When the vote was cast (UTC).")


class SignalementVoteListOut(BaseModel):
    """Votes and pass-bys of one report, with its tallies."""
    total:              int = Field(..., description="Number of votes in `items`.")
    present_count:      int = Field(..., description="Counted \"still there\" votes.")
    absent_count:       int = Field(..., description="Counted \"not there\" votes.")
    invalid_vote_count: int = Field(..., description="Votes with reason `too_far` or `no_position`.")
    vote_max_distance_m: int = Field(..., description="Current `community.vote_max_distance_m`, metres.")
    ignore_count:       int = Field(0, description="Devices that passed in the ignore window without voting.")
    uncertain:          bool = Field(False, description="The report is flagged uncertain.")
    items:              List[SignalementVoteOut] = Field(..., description="Votes, newest first.")
    passes:             List[SignalementPassOut] = Field([], description="Pass-bys, newest first.")


class FlaggedVoterOut(BaseModel):
    """A voter with invalid votes (possible false testimony)."""
    voter:           Optional[str] = Field(description="Display label: e-mail if signed in, else device id.")
    voter_user_id:   Optional[str] = Field(description="User id (signed-in voter).")
    voter_email:     Optional[str] = Field(description="User e-mail (signed-in voter).")
    voter_device_id: Optional[str] = Field(description="Device id.")
    invalid_votes:   int = Field(..., description="Votes not counted because `too_far` or `no_position`.")
    too_far_votes:   int = Field(..., description="Votes cast beyond `community.vote_max_distance_m`.")
    total_votes:     int = Field(..., description="All votes by this voter.")
    reports:         int = Field(..., description="Distinct reports voted on.")
    last_vote_at:    datetime = Field(..., description="Most recent vote (UTC).")


class SignalementListOut(BaseModel):
    """A page of community reports."""
    total: int = Field(..., description="Number of matching reports (before paging).")
    items: List[SignalementOut] = Field(..., description="The reports.")


class SignalementModerationIn(BaseModel):
    """Moderation decision on a report."""
    status:       SignalementStatus = Field(..., description=(  # annule | rejete
        "`annule` (cancel) or `rejete` (reject) takes the report down; `actif` restores it."),
        examples=["rejete"])
    moderated_by: str = Field(..., description=(
        "Moderator name (replaced by the signed-in user's e-mail)."), examples=["ops@camtrack.cm"])
    note:         Optional[str] = Field(None, description="Reason / note stored as `moderation_note`.",
                                        examples=["Duplicate of an existing report."])


class SignalementNearbyIn(BaseModel):
    """Position around which to list visible reports."""
    latitude:  float = Field(..., description="WGS84 latitude (degrees).", examples=[4.0511])
    longitude: float = Field(..., description="WGS84 longitude (degrees).", examples=[9.7679])
    radius_m:  Optional[float] = Field(None, description=(  # default & cap: mobile.visibility_radius_m
        "Search radius, metres. Default and maximum: `mobile.visibility_radius_m`."), examples=[1000])


# ─── Vehicle telemetry schemas ────────────────────────────────────────────────

class VehicleRegisterIn(BaseModel):
    """Register (or re-register) a vehicle/device for live telemetry."""
    vehicle_id:   Optional[str] = Field(None, description=(  # client resends its stored id to re-register after reinstall/backend reset
        "Id previously issued to this device — resend it to re-register after a reinstall or a backend "
        "reset (the same id is kept, created if unknown). Omit to get a new UUID."),
        examples=["5d0c3b1e-8a5f-4b8e-9d2c-1f3e7a9b6c40"])
    vehicle_type: VehicleType = Field(VehicleType.private, description=(
        "Kind of vehicle (`bus`, `taxi`, `truck`, `private`, `moto`); updated on re-registration."),
        examples=["taxi"])


class VehicleOut(BaseModel):
    """A registered vehicle identity (live position is not stored here)."""
    id:           str = Field(..., description="Vehicle id — use it in `/vehicles/{vehicle_id}/ping`.")
    vehicle_type: VehicleType = Field(..., description="Kind of vehicle.")
    created_at:   datetime = Field(..., description="Registration time (UTC).")
    last_seen:    Optional[datetime] = Field(description=(
        "Last ping time (UTC), updated at most every `VEHICLE_LAST_SEEN_THROTTLE_S` seconds; null if never pinged."))

    class Config:
        from_attributes = True


class VehiclePingIn(BaseModel):
    """One telemetry sample. Out-of-range values are rejected with a 422 validation error."""
    latitude:  float = Field(..., description="WGS84 latitude, degrees (−90…90).", examples=[4.0511])
    longitude: float = Field(..., description="WGS84 longitude, degrees (−180…180).", examples=[9.7679])
    speed_mps: Optional[float] = Field(None, description=(  # metres/second, as reported by device GPS
        "Ground speed in **m/s** as reported by the GPS. Negative values are rejected (422); values above "
        "100 m/s (360 km/h, e.g. a fast simulated demo drive) are clamped to 100 instead of rejecting the "
        "whole fix."), examples=[13.9])
    heading:   Optional[float] = Field(None, description=(  # degrees, 0-360, direction of travel
        "Direction of travel, degrees clockwise from north (0–360)."), examples=[87.5])
    accuracy_m: Optional[float] = Field(None, description=(  # device-reported GPS horizontal accuracy, metres
        "GPS horizontal accuracy reported by the device, metres."), examples=[6.0])
    acceleration_mps2: Optional[float] = Field(None, description=(  # device-reported linear acceleration, if available (see Step 3)
        "Longitudinal acceleration in m/s² if the device measures it (−20…20); otherwise the trajectory "
        "predictor derives it from successive speeds."), examples=[0.4])
    ts:        Optional[datetime] = Field(None, description=(  # device timestamp; server time used if omitted
        "Device timestamp of the fix (ISO 8601, UTC if no offset); server time when omitted."))

    @field_validator("latitude")
    @classmethod
    def _lat_range(cls, v: float) -> float:
        if not (-90.0 <= v <= 90.0):
            raise ValueError("latitude out of range")
        return v

    @field_validator("longitude")
    @classmethod
    def _lon_range(cls, v: float) -> float:
        if not (-180.0 <= v <= 180.0):
            raise ValueError("longitude out of range")
        return v

    @field_validator("speed_mps")
    @classmethod
    def _speed_sane(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and v < 0.0:
            raise ValueError("speed_mps out of plausible range")
        if v is not None and v > SPEED_MPS_CEILING:
            # 360 km/h ceiling. Clamped rather than rejected: the position is
            # still worth keeping (the app's fast demo drive reports > 100 m/s,
            # and a 422 there dropped the phone out of the live set entirely).
            _schemas_logger.info("ping speed_mps %.1f clamped to %.0f", v, SPEED_MPS_CEILING)
            return SPEED_MPS_CEILING
        return v

    @field_validator("acceleration_mps2")
    @classmethod
    def _acceleration_sane(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and not (-20.0 <= v <= 20.0):  # ~2g either way — crash-level beyond this, treat as sensor noise
            raise ValueError("acceleration_mps2 out of plausible range")
        return v


class VehicleLiveOut(BaseModel):
    """Latest telemetry of a live vehicle (one that pinged within `VEHICLE_LIVE_TTL_S` seconds)."""
    vehicle_id:   str = Field(..., description="Vehicle id.")
    vehicle_type: VehicleType = Field(..., description="Kind of vehicle.")
    latitude:     float = Field(..., description="WGS84 latitude (degrees).", examples=[4.0511])
    longitude:    float = Field(..., description="WGS84 longitude (degrees).", examples=[9.7679])
    speed_mps:    Optional[float] = Field(description="Speed, m/s.", examples=[13.9])
    heading:      Optional[float] = Field(description="Heading, degrees clockwise from north (0–360).")
    accuracy_m:   Optional[float] = Field(None, description="GPS horizontal accuracy, metres.")
    acceleration_mps2: Optional[float] = Field(None, description="Device-reported acceleration, m/s².")
    ts:           datetime = Field(..., description="Timestamp of the latest ping.")
    distance_m:   Optional[float] = Field(None, description=(  # populated for /nearby queries
        "Distance from the query position, metres (only in `/vehicles/nearby`)."))


class VehicleNearbyIn(BaseModel):
    """Position around which to list live vehicles."""
    latitude:  float = Field(..., description="WGS84 latitude (degrees).", examples=[4.0511])
    longitude: float = Field(..., description="WGS84 longitude (degrees).", examples=[9.7679])
    radius_m:  Optional[float] = Field(None, description=(  # default & cap: mobile.visibility_radius_m
        "Search radius, metres. Default and maximum: `mobile.visibility_radius_m`."), examples=[1000])
    exclude_vehicle_id: Optional[str] = Field(None, description="Leave this vehicle out (typically the caller's own).")


class VehicleNearbyOut(BaseModel):
    """Live vehicles (nearest first for `/vehicles/nearby`)."""
    total: int = Field(..., description="Number of vehicles in `items`.")
    items: List[VehicleLiveOut] = Field(..., description="The vehicles.")


# ─── Clustering schemas ───────────────────────────────────────────────────────────────

class ClusterQueryIn(BaseModel):
    """Optional filters of the on-the-fly clustering."""
    type:    Optional[str] = Field(None, description="Only this detection type.", examples=["pothole"])
    subtype: Optional[str] = Field(None, description="Only this subtype.")
    job_id:  Optional[str] = Field(None, description="Only the detections of this job.")


class ClusterOut(BaseModel):
    """A group of nearby accepted detections."""
    cluster_id:    int = Field(description="Index of the cluster in this answer (not persistent).", examples=[0])
    centroid_lat:  float = Field(description="WGS84 latitude of the centroid.", examples=[4.0512])
    centroid_lon:  float = Field(description="WGS84 longitude of the centroid.", examples=[9.7085])
    count:         int = Field(description="Number of member detections.", examples=[4])
    resolved_count: int = Field(description="Members in status `resolved`.", examples=[0])
    is_resolved:   bool = Field(description="Every member is resolved.")
    detection_ids: List[str] = Field(description="Member detection ids.")
    detection_coords: List[List[float]] = Field([], description="Member positions as `[latitude, longitude]` pairs.",
                                                examples=[[[4.0512, 9.7085], [4.0513, 9.7086]]])
    radius_m:      Optional[float] = Field(None, description=(
        "Clustering radius of this cluster (resolved at its seed detection by the global Cluster "
        "Configuration), metres."), examples=[30.0])


class ClusteredDetectionsOut(BaseModel):
    """Result of the on-the-fly clustering."""
    radius_m:         float = Field(description=(
        "Largest clustering radius used, metres (each cluster carries its own `radius_m`: the global "
        "Cluster Configuration resolved per location and type)."), examples=[60.0])
    total_detections: int = Field(description="Detections clustered.", examples=[120])
    total_clusters:   int = Field(description="Number of clusters.", examples=[35])
    active_clusters:  int = Field(description="Clusters with at least one unresolved member.", examples=[35])
    resolved_clusters: int = Field(description="Clusters whose members are all resolved.", examples=[0])
    clusters:         List[ClusterOut] = Field(description="The clusters.")


class ResolveClusterIn(BaseModel):
    """
    Sent when a vehicle passes through a cluster zone.
    `new_detection_count` is the number of detections the new job found
    in the same zone (0 = no detections at all).
    The cluster is resolved when new_detection_count < original cluster count.
    """
    detection_ids:       List[str] = Field(description="The original cluster member ids (must not be empty).")
    new_detection_count: int = Field(0, description="Detections found in the zone by the new pass.", examples=[0])
    resolved_by:         Optional[str] = Field(None, description="Name recorded as resolver when the caller is not "
                                                                 "a signed-in user.")


# ─── Module 6 — validation workflow & hazards ────────────────────────────────

def _check_severity(v):
    if v is not None and not (SEVERITY_MIN <= v <= SEVERITY_MAX):
        raise ValueError(f"severity_score must be between {SEVERITY_MIN} and {SEVERITY_MAX}")
    return v


_BBOX_DOC = ("Corrected box `[x1, y1, x2, y2]` in frame pixels when the predicted box is wrong (clipped to "
             "the frame); the detection becomes `corrected` and the training sample uses this box.")


class AcceptIn(BaseModel):
    """Body of `POST /detections/{id}/accept` (every field optional)."""
    label:          Optional[str] = Field(None, description="Training label (default: the subtype, else the type).",
                                          examples=["pothole"])
    severity_score: Optional[int] = Field(None, description="Severity 0–3 (default 1).", examples=[2])
    note:           Optional[str] = Field(None, description="Reviewer's note (kept in the history).")
    bbox:           Optional[list[float]] = Field(None, min_length=4, max_length=4, description=_BBOX_DOC,
                                                  examples=[[212.0, 388.5, 301.0, 440.0]])

    @field_validator("severity_score")
    @classmethod
    def _severity_range(cls, v):
        return _check_severity(v)


class CorrectIn(BaseModel):
    """Body of `POST /detections/{id}/correct`: the right class."""
    type:           str = Field(description="Correct detection type (non-blank).", examples=["speed_bump"])
    subtype:        Optional[str] = Field(None, description="Correct subtype (`null` clears it).")
    label:          Optional[str] = Field(None, description="Training label (default: the new subtype, else type).")
    severity_score: Optional[int] = Field(None, description="Severity 0–3 (default 1).", examples=[1])
    note:           Optional[str] = Field(None, description="Reviewer's note.")
    bbox:           Optional[list[float]] = Field(None, min_length=4, max_length=4, description=_BBOX_DOC)

    @field_validator("severity_score")
    @classmethod
    def _severity_range(cls, v):
        return _check_severity(v)

    @field_validator("type")
    @classmethod
    def _type_set(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("type is required")
        return v.strip()


class RejectIn(BaseModel):
    """Body of `POST /detections/{id}/reject`."""
    reason_code: str = Field(description="Rejection reason code for the detection's type or `all` "
                                         "(`GET /rejection-reasons/`).", examples=["shadow"])
    note:        Optional[str] = Field(None, description="Reviewer's note.")


class ReopenIn(BaseModel):
    """Why a final decision is reversed (mandatory, non-blank)."""
    reason: str = Field(description="Reason, kept in the history and the audit trail.",
                        examples=["Wrong decision — the pothole is visible on frame 1522."])

    @field_validator("reason")
    @classmethod
    def _reason_set(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("reason is required")
        return v.strip()


class LocationFixIn(BaseModel):
    """Body of `POST /detections/{id}/location`. Which fields are required depends on `method`."""
    method:         str = Field(description="`map_pin` (needs `latitude`/`longitude`), `trajectory_point` (needs "
                                            "`track_point_id`), `ocr_text` (needs `raw_gps_text`) or "
                                            "`accept_proposed` (accepts the interpolated position).",
                                examples=["map_pin"])
    latitude:       Optional[float] = Field(None, description="WGS84 latitude (`map_pin`).", examples=[4.0512])
    longitude:      Optional[float] = Field(None, description="WGS84 longitude (`map_pin`).", examples=[9.7085])
    track_point_id: Optional[int]   = Field(None, description="Id of a point of the job's vehicle track "
                                                              "(`trajectory_point`).", examples=[8812])
    raw_gps_text:   Optional[str]   = Field(None, description="OSD GPS text, NMEA (`ocr_text`).",
                                            examples=["0515.4260,N,01013.5383,E,028KM/H"])
    note:           Optional[str]   = Field(None, description="Reviewer's note.")


class DetectionReviewOut(BaseModel):
    """One review-history row of a detection."""
    id:          str = Field(description="History row id.")
    from_status: Optional[str] = Field(description="Status before (`null` at creation).", examples=["pending"])
    to_status:   str = Field(description="Status after.", examples=["validated"])
    action:      str = Field(description="`create`, `auto_accept`, `auto_reject`, `accept`, `correct`, `reject`, "
                                         "`location_fix`, `reopen`, `resolve` or `unresolve`.", examples=["accept"])
    actor_type:  str = Field(description="`user` or `system`.", examples=["user"])
    actor:       Optional[str] = Field(description="Who made the change.")
    note:        Optional[str] = Field(description="Note or reason given.")
    details:     Optional[dict] = Field(description="Action-specific data (label, previous/new values, reason code …).")
    created_at:  datetime = Field(description="When it happened (UTC).")

    class Config:
        from_attributes = True


class HazardRefOut(BaseModel):
    """What a queued detection would join (or None = new hazard)."""
    hazard_id:       str = Field(description="Hazard id.")
    hazard_code:     str = Field(description="Hazard code.", examples=["HZ-000012"])
    detection_count: int = Field(description="Detections already in the hazard.", examples=[3])


class QueueItemOut(DetectionOut):
    """A detection of the validation queue."""
    cluster: Optional[HazardRefOut] = Field(None, description="Active hazard it would join if accepted now; `null` = "
                                                              "it would open a new hazard (or position unknown).")


class QueueListOut(BaseModel):
    """A page of the validation queue."""
    total: int = Field(description="Number of matching detections (before paging).", examples=[18])
    items: List[QueueItemOut] = Field(description="The detections of this page.")


class HazardOut(BaseModel):
    """A physical road hazard: the persisted cluster of accepted detections of one type."""
    id:                str = Field(description="Hazard id (UUID).")
    code:              str = Field(description="Human-readable code.", examples=["HZ-000012"])
    type:              str = Field(description="Detection type.", examples=["pothole"])
    subtype:           Optional[str] = Field(description="Subtype (for types clustered per subtype, e.g. "
                                                         "traffic signs).")
    status:            str = Field(description="`active` or `resolved`.", examples=["active"])
    latitude:          float = Field(description="WGS84 latitude of the centroid.", examples=[4.0512])
    longitude:         float = Field(description="WGS84 longitude of the centroid.", examples=[9.7085])
    radius_m:          float = Field(description="Clustering radius applied, in metres.", examples=[50.0])
    cluster_zone_id:   Optional[int] = Field(None, description=(
        "Cluster zone whose configuration gave `radius_m` when the hazard was created (null = unzoned)."))
    cluster_zone_name: Optional[str] = Field(None, description="Name of that zone.", examples=["Douala"])
    cluster_environment: Optional[Literal["urban", "rural"]] = Field(None, description=(
        "Environment profile used: `urban` (in agglomeration) or `rural` (out of agglomeration)."),
        examples=["urban"])
    country:           Optional[str] = Field(description="Country.", examples=["Cameroon"])
    city:              Optional[str] = Field(description="City.", examples=["Douala"])
    location_name:     Optional[str] = Field(description="Place name.")
    road:              Optional[str] = Field(None, description="Street (address of the most central geocoded member).", examples=["Rue Joss"])
    locality:          Optional[str] = Field(None, description="Quartier / neighbourhood / suburb.", examples=["Akwa"])
    district:          Optional[str] = Field(None, description="Arrondissement / city district.", examples=["Douala Ier"])
    town:              Optional[str] = Field(None, description="Town / city / village.", examples=["Douala"])
    region:            Optional[str] = Field(None, description="Region / state.", examples=["Littoral"])
    display_location:  Optional[str] = Field(None, description="Short human-readable place: street, quartier, town.",
                                             examples=["Rue Joss, Akwa, Douala"])
    detection_count:   int = Field(description="Member detections.", examples=[4])
    avg_confidence:    float = Field(description="Mean confidence of the members (0–1).", examples=[0.82])
    max_confidence:    float = Field(description="Highest member confidence (0–1).", examples=[0.93])
    first_detected_at: Optional[datetime] = Field(description="Earliest member capture (UTC).")
    last_detected_at:  Optional[datetime] = Field(description="Latest member capture (UTC).")
    resolved_at:       Optional[datetime] = Field(description="When it was resolved (UTC).")
    resolved_by:       Optional[str] = Field(description="Who resolved it.")
    resolution_note:   Optional[str] = Field(description="Note given when resolving.")
    resolution_source: Optional[str] = Field(None, description="How it was resolved: `manual` (console user), "
                                                               "`video_absence` (valid video passes without it), "
                                                               "`votes` (drivers' \"not there\" votes) or `expiry` "
                                                               "(not seen for the policy's expiry); `null` while "
                                                               "active.", examples=["video_absence"])
    last_seen_at:      Optional[datetime] = Field(None, description="Recording time of the latest sighting (UTC): "
                                                                    "member or later detection, or a \"still "
                                                                    "there\" vote when the policy extends on votes.")
    expires_at:        Optional[datetime] = Field(None, description="When it expires without a new sighting (UTC) = "
                                                                    "`last_seen_at` + the policy's expiry hours; "
                                                                    "`null` = never (persistent types).")
    absence_count:     int = Field(0, description="Valid \"not seen\" video passes since the last sighting "
                                                  "(console only).", examples=[1])
    recurring_spot:    bool = Field(False, description="Opened where ≥ `hazard_lifecycle.recurring_min_occurrences` "
                                                       "hazards of the same type were resolved in the last "
                                                       "`recurring_window_days` days.")
    recurrence_count:  int = Field(0, description="Hazards of the same type resolved at this spot in the window "
                                                  "when it was opened.", examples=[3])
    created_at:        datetime = Field(description="Creation time (UTC).")
    updated_at:        datetime = Field(description="Last change (UTC).")
    cover_crop_url:    Optional[str] = Field(None, description="Best evidence: clean crop of the member with the "
                                                               "highest confidence (hazard list / detail).")
    cover_image_url:   Optional[str] = Field(None, description="Annotated frame of that member.")

    class Config:
        from_attributes = True


class DetectionDetailOut(DetectionOut):
    """A detection with its review history and hazard."""
    history: List[DetectionReviewOut] = Field([], description="Review history, newest first.")
    hazard:  Optional[HazardOut] = Field(None, description="The hazard it belongs to, if any.")


class HazardListOut(BaseModel):
    """A page of hazards."""
    total: int = Field(description="Number of matching hazards (before paging).", examples=[42])
    items: List[HazardOut] = Field(description="The hazards of this page.")


class HazardMemberOut(BaseModel):
    """A detection that belongs to a hazard (short form)."""
    id:            str = Field(description="Detection id.")
    type:          str = Field(description="Detection type.", examples=["pothole"])
    subtype:       Optional[str] = Field(description="Subtype.")
    review_status: ReviewStatus = Field(description="Review status (`validated`, `corrected` or `resolved` for "
                                                    "members).", examples=["validated"])
    confidence:    float = Field(description="Model confidence (0–1).", examples=[0.87])
    latitude:      Optional[float] = Field(description="WGS84 latitude.")
    longitude:     Optional[float] = Field(description="WGS84 longitude.")
    captured_at:   Optional[datetime] = Field(description="Capture time (UTC).")
    created_at:    datetime = Field(description="Creation time (UTC).")
    crop_url:      Optional[str] = Field(description="Clean crop.")
    image_url:     Optional[str] = Field(description="Annotated frame.")
    job_id:        str = Field(description="Job that produced it.")
    recorded_at:   Optional[datetime] = Field(None, description="Video recording time (timezone-aware).")
    location_status: Optional[str] = Field(None, description="`confirmed`, `approximate` or `missing`.")
    location_name: Optional[str] = Field(None, description="Legacy place name.")
    ai_decision:   Optional[str] = Field(None, description="`auto_accept`, `auto_reject` or `review`.")
    ai_decision_reason: Optional[str] = Field(None, description="Short explanation of the AI decision.")
    verifier_score: Optional[float] = Field(None, description="Crop classifier probability (0–1).")
    audit_sample:  Optional[bool] = Field(None, description="Automatic decision drawn for a human audit.")
    reviewed_by:   Optional[str] = Field(None, description="Last reviewer (`system` = automatic).")
    context_clip_url: Optional[str] = Field(None, description="Short video clip around the detection.")
    road:              Optional[str] = Field(None, description="Reverse-geocoded street.", examples=["Rue Joss"])
    locality:          Optional[str] = Field(None, description="Quartier / neighbourhood / suburb.", examples=["Akwa"])
    district:          Optional[str] = Field(None, description="Arrondissement / city district.", examples=["Douala Ier"])
    town:              Optional[str] = Field(None, description="Town / city / village.", examples=["Douala"])
    region:            Optional[str] = Field(None, description="Region / state.", examples=["Littoral"])
    display_location:  Optional[str] = Field(None, description="Short human-readable place: street, quartier, town.",
                                             examples=["Rue Joss, Akwa, Douala"])

    class Config:
        from_attributes = True


class HazardDetailOut(HazardOut):
    """A hazard with its member detections."""
    detections: List[HazardMemberOut] = Field([], description="Member detections, newest capture first.")


class HazardResolveIn(BaseModel):
    """Body of `POST /hazards/{id}/resolve`."""
    note: Optional[str] = Field(None, description="Resolution note (e.g. the repair reference).",
                                examples=["Repaired by the city roads department."])


class TrackPointOut(BaseModel):
    """A GPS fix of the vehicle track, read on a frame's OSD."""
    id:           int = Field(description="Track point id (use as `track_point_id`).", examples=[8812])
    video_time_s: Optional[float] = Field(description="Position in the video, in seconds.", examples=[50.6])
    frame_number: int = Field(description="Frame index.", examples=[1518])
    latitude:     float = Field(description="WGS84 latitude.", examples=[4.0512])
    longitude:    float = Field(description="WGS84 longitude.", examples=[9.7085])
    speed_kmh:    Optional[float] = Field(description="Speed read on the OSD, km/h.", examples=[28.0])

    class Config:
        from_attributes = True


class TrajectoryOut(BaseModel):
    """The vehicle track around a detection."""
    detection_time_s: Optional[float] = Field(description="Detection's position in the video, in seconds "
                                                          "(`null` = unknown, whole track returned).", examples=[50.7])
    points:           List[TrackPointOut] = Field(description="Track points ordered by frame.")
