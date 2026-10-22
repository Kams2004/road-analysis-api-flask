import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db.session import Base


class GeneratedReport(Base):
    """A generated PDF report: its filters, a small summary and where the file
    is stored (MinIO bucket `reports`, object `object_name`). The methodology
    in force at generation time is kept with it (RRAP-13-013)."""
    __tablename__ = "generated_reports"
    __table_args__ = (Index("ix_generated_reports_created_at", "created_at"),)

    id:          Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    report_type: Mapped[str] = mapped_column(String)
    title:       Mapped[str] = mapped_column(String)
    filters:     Mapped[dict] = mapped_column(JSON, default=dict)
    summary:     Mapped[dict] = mapped_column(JSON, default=dict)
    status:      Mapped[str] = mapped_column(String, default="ready")      # ready | failed
    error:       Mapped[str | None] = mapped_column(String, nullable=True)
    object_name: Mapped[str | None] = mapped_column(String, nullable=True)
    size_bytes:  Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_count:  Mapped[int | None] = mapped_column(Integer, nullable=True)
    methodology: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by:  Mapped[str | None] = mapped_column(String, nullable=True)
    created_at:  Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
