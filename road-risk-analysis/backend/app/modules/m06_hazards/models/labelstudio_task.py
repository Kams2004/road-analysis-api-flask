"""
Box review in the server's Label Studio (app.modules.m06_hazards.training_data.labelstudio,
docs/TRAINING-DATA.md): one row per training frame sent to Label Studio — its task
there and the annotation last written back, so "Fetch corrections" applies each
annotation once (an annotation edited later in Label Studio is applied again).
"""
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db.session import Base


class LabelStudioTask(Base):
    __tablename__ = "labelstudio_tasks"
    __table_args__ = (UniqueConstraint("detection_type", "frame_key", name="uq_labelstudio_tasks_type_key"),)

    id:             Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    detection_type: Mapped[str] = mapped_column(String, index=True)
    frame_key:      Mapped[str] = mapped_column(String)                      # training_frames.frame_key
    project_id:     Mapped[int] = mapped_column(Integer)
    task_id:        Mapped[int | None] = mapped_column(Integer, nullable=True)
    sent_at:        Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    applied_key:    Mapped[str | None] = mapped_column(String, nullable=True)  # "<annotation id>:<updated_at>"
    applied_at:     Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
