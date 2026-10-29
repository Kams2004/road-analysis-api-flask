import uuid
from datetime import date, datetime

from sqlalchemy import JSON, Date, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db.session import Base


class EventFile(Base):
    """
    One daily event file found on an "events" source (an SFTP server whose purpose is
    `events`) — app/modules/m08_historical/event_feed.py. Tracks what was imported so each
    hourly check only fetches new or changed files (name + size + mtime, then sha256), and
    keeps the per-file counts shown in the web console.
    """
    __tablename__ = "event_files"
    __table_args__ = (UniqueConstraint("connection_id", "name", name="uq_event_files_connection_name"),)

    id:            Mapped[str]             = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    connection_id: Mapped[str]             = mapped_column(String, ForeignKey("cloud_connections.id", ondelete="CASCADE"),
                                                           index=True)
    name:          Mapped[str]             = mapped_column(String)                  # 2026-10-27_ymane.csv
    path:          Mapped[str]             = mapped_column(String)                  # remote path
    day:           Mapped[date]            = mapped_column(Date, index=True)        # the day the file covers
    source:        Mapped[str | None]      = mapped_column(String, nullable=True)   # provider: 2026-10-28_besafe.csv → besafe
    size:          Mapped[int | None]      = mapped_column(Integer, nullable=True)
    mtime:         Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sha256:        Mapped[str | None]      = mapped_column(String, nullable=True)
    status:        Mapped[str]             = mapped_column(String, default="pending")  # pending | imported | failed
    rows:          Mapped[int]             = mapped_column(Integer, default=0)      # data lines read
    inserted:      Mapped[int]             = mapped_column(Integer, default=0)
    updated:       Mapped[int]             = mapped_column(Integer, default=0)
    invalid:       Mapped[int]             = mapped_column(Integer, default=0)      # lines skipped (see problems)
    problems:      Mapped[list | None]     = mapped_column(JSON, nullable=True)     # first few skipped lines + why
    by_type:       Mapped[dict | None]     = mapped_column(JSON, nullable=True)     # {"SPEEDING": 947, ...}
    archive_key:   Mapped[str | None]      = mapped_column(String, nullable=True)   # events-raw bucket (gzip)
    attempts:      Mapped[int]             = mapped_column(Integer, default=0)
    error:         Mapped[str | None]      = mapped_column(String, nullable=True)
    found_at:      Mapped[datetime]        = mapped_column(DateTime, default=datetime.utcnow)
    imported_at:   Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
