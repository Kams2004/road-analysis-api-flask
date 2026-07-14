from pydantic import BaseModel, field_validator
from datetime import datetime
from typing import Optional, List
from app.models.job import JobStatus
from app.models.detection import ReviewStatus
from app.models.validation_label import SEVERITY_MIN, SEVERITY_MAX
from app.models.signalement import SignalementType, SignalementStatus


class JobOut(BaseModel):
    id:             str
    filename:       str
    status:         JobStatus
    enabled_models: Optional[str]      # which models were run, e.g. "pothole,signs"
    total_frames:   int
    processed:      int
    detections:     int
    error:          Optional[str]
    created_at:     datetime
    finished_at:    Optional[datetime]

    class Config:
        from_attributes = True


class DetectionOut(BaseModel):
    id:            str
    job_id:        str
    type:          str
    subtype:       Optional[str]
    confidence:    float
    frame_number:  int
    raw_gps_text:  Optional[str]   # exact OCR output — shown to validator
    latitude:      Optional[float] # None = needs correction before validation
    longitude:     Optional[float]
    speed_kmh:     Optional[float]
    vehicle_id:    Optional[str]
    captured_at:   Optional[datetime]
    image_url:        Optional[str]   # annotated detection frame
    crop_url:         Optional[str]   # clean unannotated crop — training sample
    context_clip_url: Optional[str]   # ±3s video clip — only present when GPS was missing
    location_name: Optional[str]
    rpm:           Optional[int]
    review_status: ReviewStatus
    reviewed_by:   Optional[str]
    reviewed_at:   Optional[datetime]
    review_note:   Optional[str]
    resolved_at:   Optional[datetime]
    created_at:    datetime

    class Config:
        from_attributes = True


class DetectionListOut(BaseModel):
    total: int
    items: List[DetectionOut]


class ReviewIn(BaseModel):
    status:         ReviewStatus
    reviewed_by:    str
    note:           Optional[str] = None
    label:          Optional[str] = None   # required when validating
    severity_score: Optional[int] = None   # 0-3; required when validating a pothole

    @field_validator("severity_score")
    @classmethod
    def check_range(cls, v):
        if v is not None and not (SEVERITY_MIN <= v <= SEVERITY_MAX):
            raise ValueError(f"severity_score must be between {SEVERITY_MIN} and {SEVERITY_MAX}")
        return v


class ValidationLabelOut(BaseModel):
    id:               str
    detection_id:     str
    detection_type:   str
    label:            str
    severity_score:   int
    model_confidence: float
    crop_url:         Optional[str]
    labeled_by:       str
    labeled_at:       datetime

    class Config:
        from_attributes = True


class LocationCorrectIn(BaseModel):
    """
    Validator submits the corrected raw GPS text as read from the frame image.
    Format must be valid NMEA: e.g. '0515.4260,N,01013.5383,E,028KM/H'
    The server transforms it to decimal lat/lon and saves both.
    """
    raw_gps_text: str
    reviewed_by:  str


# ─── Spatial query schemas (used by the mobile app) ──────────────────────────

class NearbyQueryIn(BaseModel):
    """
    Circle query: return detections within `radius_m` metres of the given point.
    """
    latitude:  float
    longitude: float
    radius_m:  float = 2000.0   # default 2 km


class RouteWaypoint(BaseModel):
    latitude:  float
    longitude: float


class AlongRouteQueryIn(BaseModel):
    """
    Corridor query: return detections that lie within `corridor_m` metres of
    any segment of the supplied polyline.
    `waypoints` must contain at least 2 points (the full OSRM route geometry).
    """
    waypoints:   List[RouteWaypoint]
    corridor_m:  float = 60.0   # default 60 m corridor each side of the route


class DatasetExportIn(BaseModel):
    detection_type: str   # pothole | traffic_sign | speed_bump
    created_by:     str


class DatasetOut(BaseModel):
    id:             str
    detection_type: str
    version:        int
    total_samples:  int
    class_counts:   dict
    manifest_url:   Optional[str]
    metadata_url:   Optional[str]
    created_by:     str
    created_at:     datetime

    class Config:
        from_attributes = True


# ─── Signalement schemas ──────────────────────────────────────────────────────

class SignalementIn(BaseModel):
    type:        SignalementType
    latitude:    float
    longitude:   float
    description: Optional[str] = None
    reported_by: Optional[str] = None


class SignalementOut(BaseModel):
    id:              str
    type:            SignalementType
    status:          SignalementStatus
    latitude:        float
    longitude:       float
    location_name:   Optional[str]
    blocked_bearing: Optional[float]
    confirmations:   int
    not_there_votes: int
    description:     Optional[str]
    image_url:       Optional[str]
    audio_url:       Optional[str]
    reported_by:     Optional[str]
    moderated_by:    Optional[str]
    moderated_at:    Optional[datetime]
    moderation_note: Optional[str]
    reported_at:     datetime

    class Config:
        from_attributes = True


class SignalementListOut(BaseModel):
    total: int
    items: List[SignalementOut]


class SignalementModerationIn(BaseModel):
    status:       SignalementStatus   # annule | rejete
    moderated_by: str
    note:         Optional[str] = None


class SignalementNearbyIn(BaseModel):
    latitude:  float
    longitude: float
    radius_m:  float = 5000.0


# ─── Clustering schemas ───────────────────────────────────────────────────────────────

class ClusterQueryIn(BaseModel):
    type:    Optional[str] = None
    subtype: Optional[str] = None
    job_id:  Optional[str] = None


class ClusterOut(BaseModel):
    cluster_id:    int
    centroid_lat:  float
    centroid_lon:  float
    count:         int
    resolved_count: int
    is_resolved:   bool
    detection_ids: List[str]
    detection_coords: List[List[float]] = []  # [[lat, lon], ...]


class ClusteredDetectionsOut(BaseModel):
    radius_m:         float
    total_detections: int
    total_clusters:   int
    active_clusters:  int
    resolved_clusters: int
    clusters:         List[ClusterOut]


class ResolveClusterIn(BaseModel):
    """
    Sent when a vehicle passes through a cluster zone.
    `new_detection_count` is the number of detections the new job found
    in the same zone (0 = no detections at all).
    The cluster is resolved when new_detection_count < original cluster count.
    """
    detection_ids:       List[str]   # the original cluster member IDs
    new_detection_count: int = 0
    resolved_by:         Optional[str] = None
