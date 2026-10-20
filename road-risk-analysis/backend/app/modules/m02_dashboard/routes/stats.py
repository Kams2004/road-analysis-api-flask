import asyncio
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, case
from pydantic import BaseModel, Field
from typing import Dict, List, Optional
from datetime import datetime, timedelta

from app.core.db.session import get_db
from app.modules.m06_hazards.models.detection import ACCEPTED_STATUSES, Detection, ReviewStatus
from app.modules.m06_hazards.models.hazard import Hazard, HazardStatus
from app.modules.m07_ingestion.models.job import Job, JobStatus
from app.modules.m11_community.models.signalement import Signalement

router = APIRouter()


class StatsRecentJob(BaseModel):
    """A recent processing job (summary)."""
    id: str = Field(..., description="Job id.")
    filename: Optional[str] = Field(description="Source file name.")
    status: str = Field(..., description="`pending`, `processing`, `done`, `failed` or `cancelled`.")
    detections: Optional[int] = Field(description="Detections produced so far.")
    created_at: str = Field(..., description="Creation time, ISO 8601 (UTC).")
    finished_at: Optional[str] = Field(description="Completion time, ISO 8601 (UTC), or null.")


class StatsOut(BaseModel):
    """All-time detection and job counters."""
    total_detections: int = Field(..., description="All detections.")
    pending_validations: int = Field(..., description="Detections in `pending` or `location_pending`.")
    validated_zones: int = Field(..., description="Detections accepted (`validated` + `corrected`).")
    rejected: int = Field(..., description="Detections `rejected`.")
    detection_rate: float = Field(..., description=(  # validated / (validated + rejected), 0-100
        "Acceptance rate, percent (0–100): accepted / (accepted + rejected); 0 when nothing reviewed."),
        examples=[87.5])
    total_jobs: int = Field(..., description="All processing jobs.")
    active_jobs: int = Field(..., description="Jobs `pending` or `processing`.")
    by_type: Dict[str, int] = Field(..., description="Detection count per detection type.",  # type → count
                                    examples=[{"pothole": 120, "speed_bump": 34}])
    by_status: Dict[str, int] = Field(..., description="Detection count per review status.",  # review_status → count
                                      examples=[{"pending": 12, "validated": 80, "rejected": 9}])
    recent_jobs: List[StatsRecentJob] = Field(..., description="The 5 most recent jobs, newest first.")  # last 5 jobs


@router.get("/", response_model=StatsOut, summary="Get overall counters")
async def get_stats(db: AsyncSession = Depends(get_db)):
    """
    All-time counters: detections by review status and type, acceptance rate, job totals and the 5
    most recent jobs. See `GET /stats/dashboard` for the period-based console home page.
    """
    # ── counts ────────────────────────────────────────────────────────────
    total = (await db.execute(select(func.count()).select_from(Detection))).scalar() or 0

    status_rows = (await db.execute(
        select(Detection.review_status, func.count())
        .group_by(Detection.review_status)
    )).all()
    by_status = {str(r[0].value): r[1] for r in status_rows}

    pending   = by_status.get("pending",   0) + by_status.get("location_pending", 0)
    validated = by_status.get("validated", 0) + by_status.get("corrected", 0)
    rejected  = by_status.get("rejected",  0)

    reviewed = validated + rejected
    rate = round((validated / reviewed) * 100, 1) if reviewed > 0 else 0.0

    # ── by detection type ─────────────────────────────────────────────────
    type_rows = (await db.execute(
        select(Detection.type, func.count()).group_by(Detection.type)
    )).all()
    by_type = {r[0]: r[1] for r in type_rows}

    # ── jobs ──────────────────────────────────────────────────────────────
    total_jobs = (await db.execute(select(func.count()).select_from(Job))).scalar() or 0
    active_jobs = (await db.execute(
        select(func.count()).select_from(Job)
        .where(Job.status.in_([JobStatus.pending, JobStatus.processing]))
    )).scalar() or 0

    recent = (await db.execute(
        select(Job).order_by(Job.created_at.desc()).limit(5)
    )).scalars().all()

    recent_jobs = [
        {
            "id": j.id,
            "filename": j.filename,
            "status": j.status.value,
            "detections": j.detections,
            "created_at": j.created_at.isoformat(),
            "finished_at": j.finished_at.isoformat() if j.finished_at else None,
        }
        for j in recent
    ]

    return StatsOut(
        total_detections=total,
        pending_validations=pending,
        validated_zones=validated,
        rejected=rejected,
        detection_rate=rate,
        total_jobs=total_jobs,
        active_jobs=active_jobs,
        by_type=by_type,
        by_status=by_status,
        recent_jobs=recent_jobs,
    )


# ═════════════════════════════════════════════════════════════════════════════
# GET /stats/dashboard — everything the console home page needs in one call
# ═════════════════════════════════════════════════════════════════════════════

# period → (window length, trend bucket unit). The trend chart shows one bar
# per bucket: days for the short windows, ISO weeks (Monday) for 90 days,
# calendar months for 12 months.
PERIODS: Dict[str, tuple[timedelta, str]] = {
    "7d":  (timedelta(days=7),   "day"),
    "30d": (timedelta(days=30),  "day"),
    "90d": (timedelta(days=90),  "week"),
    "12m": (timedelta(days=365), "month"),
}

RISK_LEVELS = ("very_high", "high", "medium", "low")


def period_window(period: str, now: datetime) -> tuple[datetime, datetime, str]:
    """(start, previous_start, bucket_unit): the current window is
    [start, now), the comparison window is [previous_start, start)."""
    length, unit = PERIODS[period]
    start = now - length
    return start, start - length, unit


def bucket_floor(ts: datetime, unit: str) -> datetime:
    """Same truncation as Postgres date_trunc(unit, ts) for day/week/month."""
    d = ts.replace(hour=0, minute=0, second=0, microsecond=0)
    if unit == "week":
        return d - timedelta(days=d.weekday())
    if unit == "month":
        return d.replace(day=1)
    return d


def bucket_starts(start: datetime, now: datetime, unit: str) -> List[datetime]:
    """Every bucket that intersects [start, now), oldest first."""
    out: List[datetime] = []
    cur = bucket_floor(start, unit)
    while cur < now:
        out.append(cur)
        if unit == "day":
            cur = cur + timedelta(days=1)
        elif unit == "week":
            cur = cur + timedelta(days=7)
        else:
            cur = cur.replace(year=cur.year + 1, month=1) if cur.month == 12 else cur.replace(month=cur.month + 1)
    return out


def hazard_risk_level(detection_count: int, max_confidence: float) -> str:
    """Risk bucket of an active hazard (dashboard + map preview).

    A hazard is riskier the more independent drives confirmed it
    (detection_count — accepted detections clustered into it) and the more
    certain the model was (max_confidence, 0..1):

      very_high : ≥ 5 detections, or ≥ 3 detections with confidence ≥ 0.85
      high      : ≥ 3 detections, or confidence ≥ 0.80
      medium    : ≥ 2 detections, or confidence ≥ 0.60
      low       : everything else (a single, low-confidence sighting)
    """
    n = detection_count or 0
    c = max_confidence or 0.0
    if n >= 5 or (n >= 3 and c >= 0.85):
        return "very_high"
    if n >= 3 or c >= 0.80:
        return "high"
    if n >= 2 or c >= 0.60:
        return "medium"
    return "low"


class KpiValue(BaseModel):
    """A KPI value, with the previous period's value for trend metrics."""
    value: int = Field(..., description="Value for the selected period (or current snapshot).", examples=[42])
    previous: Optional[int] = Field(None, description=(  # None = snapshot metric, no comparison window
        "Value over the previous period of the same length; null for snapshot metrics."), examples=[35])


class DashboardKpis(BaseModel):
    """Headline KPIs of the console home page."""
    detected: KpiValue = Field(..., description="Detections created in the period.")
    validated: KpiValue = Field(..., description="Detections accepted (validated or corrected) in the period, by review time.")
    active_vehicles: KpiValue = Field(..., description="Vehicles currently sending telemetry (snapshot).")
    processing_jobs: KpiValue = Field(..., description="Jobs `pending` or `processing` (snapshot).")


class TrendBucket(BaseModel):
    """Counts for one bucket of the trend chart."""
    bucket: datetime = Field(..., description="Bucket start (UTC): day, ISO week (Monday) or month.")
    detected: int = Field(..., description="Detections created.")
    validated: int = Field(..., description="Detections accepted.")
    reports: int = Field(..., description="Community reports filed.")


class TypeCount(BaseModel):
    """Count per detection type."""
    type: str = Field(..., description="Detection type.", examples=["pothole"])
    count: int = Field(..., description="Detections of this type created in the period.")


class RiskCount(BaseModel):
    """Count of active hazards per risk level."""
    level: str = Field(..., description="`very_high`, `high`, `medium` or `low`.", examples=["high"])
    count: int = Field(..., description="Active hazards at this level.")


class DashboardJob(BaseModel):
    """A recent processing job."""
    id: str = Field(..., description="Job id.")
    filename: str = Field(..., description="Source file name.")
    source_type: str = Field(..., description="Where the media came from (`upload`, cloud import, stream …).")
    source_name: Optional[str] = Field(None, description="Name of the cloud connection / stream, if any.")
    status: str = Field(..., description="`pending`, `processing`, `done`, `failed` or `cancelled`.")
    progress: float = Field(..., description="Progress, percent (0–100).")  # 0-100
    detections: int = Field(..., description="Detections produced.")
    created_at: datetime = Field(..., description="Creation time (UTC).")


class MapHazard(BaseModel):
    """An active hazard for the dashboard map."""
    id: str = Field(..., description="Hazard id.")
    code: str = Field(..., description="Human-readable hazard code.")
    type: str = Field(..., description="Hazard (detection) type.", examples=["pothole"])
    lat: float = Field(..., description="WGS84 latitude (degrees).")
    lon: float = Field(..., description="WGS84 longitude (degrees).")
    risk_level: str = Field(..., description="`very_high`, `high`, `medium` or `low` (see the endpoint description).")
    detection_count: int = Field(..., description="Accepted detections clustered into this hazard.")


class DashboardOut(BaseModel):
    """Everything the console home page shows."""
    period: str = Field(..., description="Requested period.", examples=["30d"])
    bucket_unit: str = Field(..., description="Trend bucket: `day` (7d, 30d), `week` (90d) or `month` (12m).")
    start: datetime = Field(..., description="Period start (UTC).")
    end: datetime = Field(..., description="Period end = now (UTC).")
    kpis: DashboardKpis = Field(..., description="Headline KPIs.")
    trends: List[TrendBucket] = Field(..., description="One entry per bucket intersecting the period, oldest first.")
    hazard_types: List[TypeCount] = Field(..., description="Detections created in the period by type, most frequent first.")
    risk_distribution: List[RiskCount] = Field(..., description="Active hazards per risk level (all four levels, always).")
    recent_jobs: List[DashboardJob] = Field(..., description="The 6 most recent jobs, newest first.")
    map_hazards: List[MapHazard] = Field(..., description="Active hazards, most recently detected first (at most 300).")


async def _count_between(db: AsyncSession, col, lo: datetime, hi: datetime, *where) -> int:
    q = select(func.count()).where(col >= lo, col < hi, *where)
    return (await db.execute(q)).scalar() or 0


async def _bucketed(db: AsyncSession, col, unit: str, lo: datetime, hi: datetime, *where) -> Dict[datetime, int]:
    b = func.date_trunc(unit, col)
    rows = (await db.execute(
        select(b, func.count()).where(col >= lo, col < hi, *where).group_by(b)
    )).all()
    return {bucket_floor(r[0], unit): r[1] for r in rows if r[0] is not None}


async def _live_vehicle_count() -> int:
    """Vehicles currently pinging (Redis). Redis down or slow → 0, never an error."""
    try:
        from app.modules.m10_collision import vehicle_service
        return len(await asyncio.wait_for(vehicle_service.query_all_live(), timeout=2.0))
    except Exception:
        return 0


@router.get("/dashboard", response_model=DashboardOut, summary="Get the console dashboard")
async def get_dashboard(
    period: str = Query("30d", pattern="^(7d|30d|90d|12m)$", description=(
        "Time window: `7d`, `30d` (daily buckets), `90d` (weekly) or `12m` (monthly), ending now.")),
    db: AsyncSession = Depends(get_db),
):
    """Console home page in one call.

    - kpis.detected / validated: detections created / accepted (validated or
      corrected, by reviewed_at) in the period, with the same-length previous
      period for the trend. active_vehicles (live vehicles) and
      processing_jobs (pending + processing) are snapshots: previous = null.
    - trends: per bucket (day / week / month, see `period`) detections
      created, detections accepted and community reports filed.
    - hazard_types: detections created in the period, by type.
    - risk_distribution / map_hazards: ACTIVE hazards (current state, not
      period-filtered), bucketed by `hazard_risk_level` — detection count and
      max model confidence: `very_high` ≥ 5 detections or ≥ 3 with confidence ≥ 0.85; `high` ≥ 3
      detections or confidence ≥ 0.80; `medium` ≥ 2 detections or confidence ≥ 0.60; `low` otherwise.
      map_hazards is capped at 300.
    - recent_jobs: the 6 most recent jobs.
    """
    now = datetime.utcnow()
    start, prev_start, unit = period_window(period, now)
    accepted = Detection.review_status.in_(ACCEPTED_STATUSES)

    detected = await _count_between(db, Detection.created_at, start, now)
    detected_prev = await _count_between(db, Detection.created_at, prev_start, start)
    validated = await _count_between(db, Detection.reviewed_at, start, now, accepted)
    validated_prev = await _count_between(db, Detection.reviewed_at, prev_start, start, accepted)
    processing = (await db.execute(
        select(func.count()).select_from(Job)
        .where(Job.status.in_([JobStatus.pending, JobStatus.processing]))
    )).scalar() or 0
    vehicles = await _live_vehicle_count()

    det_b = await _bucketed(db, Detection.created_at, unit, start, now)
    val_b = await _bucketed(db, Detection.reviewed_at, unit, start, now, accepted)
    rep_b = await _bucketed(db, Signalement.reported_at, unit, start, now)
    trends = [
        TrendBucket(bucket=b, detected=det_b.get(b, 0), validated=val_b.get(b, 0), reports=rep_b.get(b, 0))
        for b in bucket_starts(start, now, unit)
    ]

    type_rows = (await db.execute(
        select(Detection.type, func.count())
        .where(Detection.created_at >= start, Detection.created_at < now)
        .group_by(Detection.type).order_by(func.count().desc())
    )).all()

    hazards = (await db.execute(
        select(Hazard).where(Hazard.status == HazardStatus.active.value)
        .order_by(Hazard.last_detected_at.desc().nullslast())
    )).scalars().all()
    risk_counts = {lvl: 0 for lvl in RISK_LEVELS}
    map_hazards: List[MapHazard] = []
    for h in hazards:
        lvl = hazard_risk_level(h.detection_count, h.max_confidence)
        risk_counts[lvl] += 1
        if len(map_hazards) < 300:
            map_hazards.append(MapHazard(
                id=h.id, code=h.code, type=h.type, lat=h.latitude, lon=h.longitude,
                risk_level=lvl, detection_count=h.detection_count or 0,
            ))

    jobs = (await db.execute(select(Job).order_by(Job.created_at.desc()).limit(6))).scalars().all()
    recent_jobs = [
        DashboardJob(
            id=j.id, filename=j.filename, source_type=j.source_type or "upload", source_name=j.source_name,
            status=j.status.value,
            progress=100.0 if j.status == JobStatus.done else (
                round(min(100.0, j.processed / j.total_frames * 100), 1) if j.total_frames else 0.0),
            detections=j.detections or 0, created_at=j.created_at,
        )
        for j in jobs
    ]

    return DashboardOut(
        period=period, bucket_unit=unit, start=start, end=now,
        kpis=DashboardKpis(
            detected=KpiValue(value=detected, previous=detected_prev),
            validated=KpiValue(value=validated, previous=validated_prev),
            active_vehicles=KpiValue(value=vehicles),
            processing_jobs=KpiValue(value=processing),
        ),
        trends=trends,
        hazard_types=[TypeCount(type=r[0], count=r[1]) for r in type_rows if r[0]],
        risk_distribution=[RiskCount(level=lvl, count=risk_counts[lvl]) for lvl in RISK_LEVELS],
        recent_jobs=recent_jobs,
        map_hazards=map_hazards,
    )
