"""
Live training data (docs/TRAINING-DATA.md, app.services.training_data).

Every decision on a detection (accept / correct / reject — human or automatic)
keeps one `training_samples` row (id = detection id) and, per detection type
and video frame, one `training_frames` row: the detector sample (clean frame +
YOLO label of every accepted box of that type on the frame). Reopening a
detection marks its sample `superseded`. The objects in the MinIO `datasets`
bucket are written by app.services.training_data.sync from these rows
(sync_status pending → synced | failed, retried by the beat task
`sync_training_data`), so a MinIO outage never blocks a validation.
"""
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class TrainingSample(Base):
    """One decided detection = one classifier crop + one box of a detector frame."""
    __tablename__ = "training_samples"

    id:             Mapped[str] = mapped_column(String, ForeignKey("detections.id", ondelete="CASCADE"),
                                                primary_key=True)          # = detection id
    detection_type: Mapped[str] = mapped_column(String, index=True)
    frame_key:      Mapped[str] = mapped_column(String, index=True)        # training_frames.frame_key
    group_key:      Mapped[str] = mapped_column(String)                    # leakage group (job + minute)
    job_id:         Mapped[str] = mapped_column(String, index=True)
    source_name:    Mapped[str | None] = mapped_column(String, nullable=True)   # video file name

    # decision
    decision:     Mapped[str] = mapped_column(String)          # accepted | corrected | rejected
    state:        Mapped[str] = mapped_column(String, default="active", index=True)   # active | superseded
    decided_by:   Mapped[str] = mapped_column(String)          # human | system
    reviewer:     Mapped[str | None] = mapped_column(String, nullable=True)
    reviewed_at:  Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reason_code:  Mapped[str | None] = mapped_column(String, nullable=True)   # rejection_code
    corrections:  Mapped[list | None] = mapped_column(JSON, nullable=True)    # ["box", "class", "location"]

    # labels
    class_name:       Mapped[str | None] = mapped_column(String, nullable=True)   # detector class (None = rejected)
    class_index:      Mapped[int | None] = mapped_column(Integer, nullable=True)
    classifier_class: Mapped[str] = mapped_column(String)                         # crop folder (not_pothole …)
    bbox_x1: Mapped[float | None] = mapped_column(Float, nullable=True)           # effective box (corrected
    bbox_y1: Mapped[float | None] = mapped_column(Float, nullable=True)           # one when corrected), pixels
    bbox_x2: Mapped[float | None] = mapped_column(Float, nullable=True)
    bbox_y2: Mapped[float | None] = mapped_column(Float, nullable=True)
    box_corrected: Mapped[bool] = mapped_column(Boolean, default=False)

    # provenance
    frame_number:  Mapped[int | None] = mapped_column(Integer, nullable=True)
    video_time_s:  Mapped[float | None] = mapped_column(Float, nullable=True)
    recorded_at:   Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    latitude:      Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude:     Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence:    Mapped[float | None] = mapped_column(Float, nullable=True)
    model_version: Mapped[str | None] = mapped_column(String, nullable=True)
    model_card_version: Mapped[str | None] = mapped_column(String, nullable=True)

    # objects in the datasets bucket + sync state
    crop_key:      Mapped[str | None] = mapped_column(String, nullable=True)
    sync_status:   Mapped[str] = mapped_column(String, default="pending", index=True)   # pending | synced | failed
    sync_attempts: Mapped[int] = mapped_column(Integer, default=0)
    sync_error:    Mapped[str | None] = mapped_column(String, nullable=True)
    synced_at:     Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at:    Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at:    Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class TrainingFrame(Base):
    """Detector sample of one detection type on one video frame."""
    __tablename__ = "training_frames"
    __table_args__ = (UniqueConstraint("detection_type", "frame_key", name="uq_training_frames_type_key"),)

    id:             Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    detection_type: Mapped[str] = mapped_column(String, index=True)
    frame_key:      Mapped[str] = mapped_column(String)          # <video>-<job8>__z<minute>__t<ms>
    group_key:      Mapped[str] = mapped_column(String)
    job_id:         Mapped[str] = mapped_column(String, index=True)
    frame_number:   Mapped[int | None] = mapped_column(Integer, nullable=True)
    video_time_s:   Mapped[float | None] = mapped_column(Float, nullable=True)
    image_width:    Mapped[int | None] = mapped_column(Integer, nullable=True)
    image_height:   Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_object:  Mapped[str | None] = mapped_column(String, nullable=True)   # clean frame, detections bucket

    image_key:  Mapped[str | None] = mapped_column(String, nullable=True)       # datasets bucket
    label_key:  Mapped[str | None] = mapped_column(String, nullable=True)
    labels:     Mapped[list | None] = mapped_column(JSON, nullable=True)        # boxes written in label_key
    n_boxes:    Mapped[int] = mapped_column(Integer, default=0)
    status:     Mapped[str] = mapped_column(String, default="active")           # active | empty | no_image

    # Box review in Label Studio (platform-push): reviewed boxes override the decisions' boxes.
    review_status:  Mapped[str] = mapped_column(String, default="needs_box_review", index=True)
    reviewed_boxes: Mapped[list | None] = mapped_column(JSON, nullable=True)
    box_reviewer:   Mapped[str | None] = mapped_column(String, nullable=True)
    box_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # dirty = its objects must be (re)written by the sync (a decision changed on the frame)
    dirty:         Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    sync_attempts: Mapped[int] = mapped_column(Integer, default=0)
    sync_error:    Mapped[str | None] = mapped_column(String, nullable=True)
    synced_at:  Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
