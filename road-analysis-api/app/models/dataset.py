import uuid
from datetime import datetime
from sqlalchemy import String, Integer, DateTime, ForeignKey, JSON
from sqlalchemy.orm import Mapped, mapped_column
from app.db.session import Base


class Dataset(Base):
    """
    Immutable versioned snapshot of validated labels for a detection type.
    Created by POST /datasets/export.

    Once created, a dataset version is never modified — retrain always
    creates a new version.
    """
    __tablename__ = "datasets"

    id:             Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    detection_type: Mapped[str] = mapped_column(String)          # pothole | traffic_sign | speed_bump
    version:        Mapped[int] = mapped_column(Integer)         # auto-incremented per detection_type
    total_samples:  Mapped[int] = mapped_column(Integer, default=0)
    class_counts:   Mapped[dict] = mapped_column(JSON, default=dict)  # {"pothole": 120, "not_pothole": 80}
    manifest_url:   Mapped[str | None] = mapped_column(String, nullable=True)  # manifest.json in MinIO
    metadata_url:   Mapped[str | None] = mapped_column(String, nullable=True)  # metadata.json in MinIO
    created_by:     Mapped[str] = mapped_column(String)
    created_at:     Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class DatasetSample(Base):
    """
    One row per ValidationLabel included in a Dataset version.
    crop_url points to the frozen copy in datasets/{type}/v{N}/crops/.
    """
    __tablename__ = "dataset_samples"

    id:                  Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    dataset_id:          Mapped[str] = mapped_column(String, ForeignKey("datasets.id"))
    validation_label_id: Mapped[str] = mapped_column(String, ForeignKey("validation_labels.id"))
    label:               Mapped[str] = mapped_column(String)
    severity_score:      Mapped[int] = mapped_column(Integer)
    model_confidence:    Mapped[float] = mapped_column(String)
    crop_url:            Mapped[str | None] = mapped_column(String, nullable=True)  # frozen copy
