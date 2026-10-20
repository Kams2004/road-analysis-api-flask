"""
Lifecycle history of a processing job (RRAP-07-005/010): one row per state
change, written by app.modules.m07_ingestion.job_service. User actions are additionally
recorded in audit_logs.
"""
from datetime import datetime
from sqlalchemy import String, DateTime, Integer, JSON, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from app.core.db.session import Base

# created | queued | started | progress_failed | failed | cancel_requested | cancelled | retried | done
# | hazard_lifecycle (sightings / "not seen" passes evaluated, app.modules.m06_hazards.lifecycle.video_pass)
JOB_EVENTS = ("created", "queued", "started", "progress_failed", "failed",
              "cancel_requested", "cancelled", "retried", "done", "hazard_lifecycle")


class JobEvent(Base):
    __tablename__ = "job_events"

    id:      Mapped[int]         = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id:  Mapped[str]         = mapped_column(String, ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    at:      Mapped[datetime]    = mapped_column(DateTime, default=datetime.utcnow)
    event:   Mapped[str]         = mapped_column(String)
    actor:   Mapped[str | None]  = mapped_column(String, nullable=True)   # e-mail or "system"
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
