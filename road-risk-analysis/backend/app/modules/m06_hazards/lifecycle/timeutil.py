"""
Recording-time helpers — pure. Every lifecycle instant is stored as a naive
UTC datetime, like the rest of the schema.

The recording time of a detection is, in order: `recorded_at` (video
recording time, timezone-aware, set by the processor), the OSD timestamp
`captured_at` (camera local time when settings.osd_time_is_local), then the
row's `created_at` (processing time — an upper bound).
"""
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo


def to_utc_naive(value: Optional[datetime], local_tz: Optional[str] = None) -> Optional[datetime]:
    """Aware → UTC naive; naive → interpreted in `local_tz` (None = already UTC)."""
    if value is None:
        return None
    if value.tzinfo is None:
        if not local_tz:
            return value
        value = value.replace(tzinfo=ZoneInfo(local_tz))
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def local_hour(utc_naive: datetime, tz: str) -> float:
    local = utc_naive.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz))
    return local.hour + local.minute / 60.0


def in_daylight(utc_naive: datetime, tz: str, hours: list[int]) -> bool:
    start, end = hours
    return start <= local_hour(utc_naive, tz) < end


def recording_time(det, tz: str = "Africa/Douala", osd_time_is_local: bool = True) -> Optional[datetime]:
    """Best known recording time of a detection (UTC naive)."""
    rec = getattr(det, "recorded_at", None)
    if rec is not None:
        return to_utc_naive(rec, tz)          # naive recorded_at is read as local time too
    cap = getattr(det, "captured_at", None)
    if cap is not None:
        return to_utc_naive(cap, tz if osd_time_is_local else None)
    return getattr(det, "created_at", None)


def has_true_recording_time(det) -> bool:
    return getattr(det, "recorded_at", None) is not None or getattr(det, "captured_at", None) is not None
