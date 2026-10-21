import uuid
import enum
from datetime import datetime
from sqlalchemy import Index, String, Float, DateTime, Integer, Boolean, ForeignKey, JSON, Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column
from app.core.db.session import Base
from app.modules.m06_hazards.models import hazard as _hazard  # noqa: F401 — registers the hazard_id FK target


class ReviewStatus(str, enum.Enum):
    """Detection workflow (RRAP-06-005/008) — transitions are enforced in
    app.modules.m06_hazards.detection_workflow, never by assigning this field directly."""
    location_pending = "location_pending"  # no confirmed location yet
    pending          = "pending"           # confirmed location, awaiting a decision
    validated        = "validated"         # accepted as predicted
    corrected        = "corrected"         # accepted after class or location correction
    rejected         = "rejected"          # not a hazard (human or automatic rule)
    resolved         = "resolved"          # accepted hazard later resolved


ACCEPTED_STATUSES = (ReviewStatus.validated, ReviewStatus.corrected)


# Plain strings in the DB (not native enums) so new values never need a migration.
class LocationStatus(str, enum.Enum):
    confirmed   = "confirmed"
    approximate = "approximate"   # interpolated by the GPS voter
    missing     = "missing"


class LocationSource(str, enum.Enum):
    ocr          = "ocr"            # OSD text read by the processor
    interpolated = "interpolated"   # GPS voter window
    ocr_neighbour = "ocr_neighbour" # OSD read on frames ±1–2 s around (overlay unreadable on the frame)
    map_pin      = "map_pin"        # validator dropped a pin
    trajectory   = "trajectory"     # validator picked a point of the vehicle track
    ocr_text     = "ocr_text"       # validator typed the OSD text
    none         = "none"


# Sources that mean a human moved the detection — accepting it then counts as "corrected".
MANUAL_LOCATION_SOURCES = (LocationSource.map_pin.value, LocationSource.trajectory.value, LocationSource.ocr_text.value)


class Detection(Base):
    __tablename__ = "detections"
    # observation identity lookups (app.modules.m06_hazards.observations): type + recording instant
    __table_args__ = (Index("ix_detections_type_recorded_at", "type", "recorded_at"),)

    id:           Mapped[str]   = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    job_id:       Mapped[str]   = mapped_column(String, ForeignKey("jobs.id"))

    # Detection info
    type:         Mapped[str]        = mapped_column(String)
    subtype:      Mapped[str | None] = mapped_column(String, nullable=True)
    confidence:   Mapped[float]      = mapped_column(Float)
    frame_number: Mapped[int]        = mapped_column(Integer)

    # Raw OCR text from GPS ROI — always saved as-is, even if incomplete
    raw_gps_text: Mapped[str | None] = mapped_column(String, nullable=True)

    # Transformed decimal coordinates — None when raw_gps_text was unreadable
    # Can be corrected later via PATCH /{id}/location
    latitude:      Mapped[float | None]    = mapped_column(Float, nullable=True)
    longitude:     Mapped[float | None]    = mapped_column(Float, nullable=True)
    gps_interpolated: Mapped[bool]         = mapped_column(Boolean, default=False)  # True = from voter window
    # Estimated ground position of the OBJECT (latitude/longitude above = the
    # recording vehicle, kept as evidence): vehicle fix + range along the GPS
    # heading (app.modules.m06_hazards.object_position). Clustering uses it when set.
    # source: estimated | vehicle (no estimate → the vehicle position is used) | NULL (not computed).
    object_latitude:  Mapped[float | None] = mapped_column(Float, nullable=True)
    object_longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    object_position_source: Mapped[str | None] = mapped_column(String, nullable=True)

    # Other OSD fields
    speed_kmh:     Mapped[float | None]    = mapped_column(Float, nullable=True)
    vehicle_id:    Mapped[str | None]      = mapped_column(String, nullable=True)
    captured_at:   Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Video recording time (timezone-aware): OSD date-time of the stored frame,
    # else extrapolated from the nearest OSD time of the video; NULL when unknown.
    recorded_at:   Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    location_name: Mapped[str | None]      = mapped_column(String, nullable=True)
    rpm:           Mapped[int | None]      = mapped_column(Integer, nullable=True)

    # MinIO
    image_url:        Mapped[str | None] = mapped_column(String, nullable=True)
    crop_url:         Mapped[str | None] = mapped_column(String, nullable=True)  # clean unannotated crop — training sample
    context_clip_url: Mapped[str | None] = mapped_column(String, nullable=True)  # ±N s clip, when the location is not confirmed
    # Clean full frame (no box drawn) + its size — the detector training image
    # (image_url keeps the annotated frame shown to validators).
    frame_clean_url:  Mapped[str | None] = mapped_column(String, nullable=True)
    frame_width:      Mapped[int | None] = mapped_column(Integer, nullable=True)
    frame_height:     Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Box moved / resized by a validator, [x1, y1, x2, y2] in frame pixels;
    # bbox_* keep the model's box. NULL = the predicted box is right.
    corrected_bbox:   Mapped[list | None] = mapped_column(JSON, nullable=True)

    # Review — cannot be accepted until the location is confirmed
    review_status: Mapped[ReviewStatus]    = mapped_column(SAEnum(ReviewStatus), default=ReviewStatus.pending)
    reviewed_by:   Mapped[str | None]      = mapped_column(String, nullable=True)
    reviewed_at:   Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    review_note:   Mapped[str | None]      = mapped_column(String, nullable=True)
    resolved_at:   Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rejection_code:   Mapped[str | None]   = mapped_column(String, nullable=True)  # rejection_reasons.code
    rejection_source: Mapped[str | None]   = mapped_column(String, nullable=True)  # human | auto

    # Location quality (RRAP-06-012) — see LocationStatus / LocationSource
    location_status: Mapped[str | None] = mapped_column(String, nullable=True)
    location_source: Mapped[str | None] = mapped_column(String, nullable=True)
    country:         Mapped[str | None] = mapped_column(String, nullable=True)
    city:            Mapped[str | None] = mapped_column(String, nullable=True)
    # Structured reverse-geocoded address (app.modules.m03_maps.geocoding): street,
    # quartier, arrondissement, town, region and a short display label.
    # geocoded_at = when it was looked up (NULL = never → back-fill candidate).
    road:             Mapped[str | None] = mapped_column(String, nullable=True)
    locality:         Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    district:         Mapped[str | None] = mapped_column(String, nullable=True)
    town:             Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    region:           Mapped[str | None] = mapped_column(String, nullable=True)
    display_location: Mapped[str | None] = mapped_column(String, nullable=True)
    geocoded_at:      Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Traceability (RRAP-05-003/05-010): where in the video, which box, which weights
    bbox_x1:       Mapped[float | None] = mapped_column(Float, nullable=True)
    bbox_y1:       Mapped[float | None] = mapped_column(Float, nullable=True)
    bbox_x2:       Mapped[float | None] = mapped_column(Float, nullable=True)
    bbox_y2:       Mapped[float | None] = mapped_column(Float, nullable=True)
    video_time_s:  Mapped[float | None] = mapped_column(Float, nullable=True)
    model_name:    Mapped[str | None]   = mapped_column(String, nullable=True)
    model_version: Mapped[str | None]   = mapped_column(String, nullable=True)  # weights file @ sha256[:12]
    model_card_version: Mapped[str | None] = mapped_column(String, nullable=True)  # detector card "name:version"

    # Two-stage pipeline & AI validation mode (RRAP-05-010/011/012, 06-007, 13-016,
    # app.modules.m05_ai_detection.ai_validation): crop classifier score and the automatic decision.
    verifier_score:        Mapped[float | None] = mapped_column(Float, nullable=True)   # classifier P(class)
    verifier_model:        Mapped[str | None]   = mapped_column(String, nullable=True)  # classifier file @ sha256[:12]
    verifier_card_version: Mapped[str | None]   = mapped_column(String, nullable=True)  # classifier card "name:version"
    ai_decision:           Mapped[str | None]   = mapped_column(String, nullable=True, index=True)  # auto_accept | auto_reject | review
    ai_decision_reason:    Mapped[str | None]   = mapped_column(String, nullable=True)
    audit_sample:          Mapped[bool]         = mapped_column(Boolean, default=False, server_default="false")
    # Two-stage traffic-sign pipeline: classifier top-3 [{"code", "score"}] (NULL otherwise)
    class_candidates:      Mapped[list | None]  = mapped_column(JSON, nullable=True)
    # Rule-based pipelines (parked vehicles): verdict, severity, vehicle class,
    # score components, reasons [{code, value, threshold, passed}], features.
    ai_details:            Mapped[dict | None]  = mapped_column(JSON, nullable=True)

    # Original model output — type/subtype above hold the (possibly corrected) class
    predicted_type:    Mapped[str | None] = mapped_column(String, nullable=True)
    predicted_subtype: Mapped[str | None] = mapped_column(String, nullable=True)

    hazard_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("hazards.id", ondelete="SET NULL"), nullable=True, index=True)

    created_at:   Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
