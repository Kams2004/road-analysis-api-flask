"""
"Not seen" video passes → resolution `video_absence`.

When a processing job is done (job_service.schedule_hazard_lifecycle →
Celery task evaluate_job_hazards), its trajectory (job_track_points) is
checked against every ACTIVE hazard whose policy enables absence resolution
and whose type's model ran in that job (hazard_lifecycle.type_models). The
job is a VALID pass of a hazard (evaluate_pass, pure) when:

  1. its closest confirmed (OCR) GPS fix is within pass_max_distance_m;
  2. the job produced no detection of that type within the hazard radius
     (that would be a sighting, not an absence);
  3. the speed there is ≤ pass_max_speed_kmh (OSD speed, else derived);
  4. the travel heading there is within pass_heading_tolerance_deg of the
     heading at one of the hazard's original sightings; when no original
     heading is known, either direction is accepted unless the policy
     require_same_direction;
  5. the recording time there is known (unless pass_require_recording_time
     is off) and AFTER the hazard's last_seen_at;
  6. with policy pass_daylight_only, it lies within daylight_hours
     (local time, hazard_lifecycle.timezone);
  7. the camera actually covered the spot (pass_require_approach): an
     earlier confirmed fix of the track is ≥ pass_approach_min_m farther from
     the spot than the closest one and has the spot ahead (within 90° of the
     travel heading) — a video that starts at or after the spot never filmed
     it;
  8. the job is not one that produced the hazard's own detections.

Each valid pass is one `not_seen` event (once per job and hazard, details
pass_number / threshold / job name: "Not seen in pass 1/3 (job X)"). After
policy absences_to_resolve valid passes since the last sighting a confirmed
hazard is resolved with source `video_absence`; a CANDIDATE cluster (never
validated) is dismissed (lifecycle.service.dismiss). Open clusters of every
type whose policy enables absence_resolution are checked (all built-in
types by default).

Recording time of a track point = its own `recorded_at` (OSD time of the
fix, set by the processor), else job recording start + its video_time_s;
the start is derived from the job's detections (recording time −
video_time_s, median) — older track points carry no clock of their own.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from math import atan2, cos, degrees, radians, sin
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.m06_hazards.models.detection import Detection
from app.modules.m06_hazards.models.hazard import OPEN_STATUSES, Hazard, HazardStatus, ResolutionSource
from app.modules.m06_hazards.lifecycle import events
from app.modules.m06_hazards.lifecycle.policy import policy_for_hazard
from app.modules.m06_hazards.lifecycle.settings import HazardLifecycleSettings
from app.modules.m06_hazards.lifecycle.timeutil import has_true_recording_time, in_daylight, to_utc_naive
from app.core.spatial import _haversine_m

logger = logging.getLogger(__name__)

CONFIRMED_SOURCES = ("ocr",)
HEADING_MIN_MOVE_M = 5.0     # displacement needed to trust a bearing
HEADING_WINDOW = 5           # track points looked at on each side


@dataclass
class TrackPt:
    latitude: float
    longitude: float
    speed_kmh: Optional[float] = None
    video_time_s: Optional[float] = None
    source: str = "ocr"
    frame_number: int = 0
    recorded_at: Optional[datetime] = None   # OSD time of the fix (aware), None for older jobs


@dataclass
class PassRules:
    max_distance_m: float = 30.0
    max_speed_kmh: float = 60.0
    heading_tolerance_deg: float = 60.0
    require_same_direction: bool = False
    daylight_only: bool = False
    daylight_hours: list = field(default_factory=lambda: [6, 18])
    timezone: str = "Africa/Douala"
    require_approach: bool = False
    approach_min_m: float = 10.0


@dataclass
class PassResult:
    passed: bool                 # the vehicle came within max_distance_m (confirmed GPS)
    valid: bool                  # … and it is a valid "not seen" pass
    reason: str                  # valid | not_passed | detected | too_fast | unknown_speed | unknown_heading |
                                 # wrong_direction | unknown_reference_heading | not_approaching | unknown_time | before_last_seen |
                                 # not_daylight
    distance_m: Optional[float] = None
    speed_kmh: Optional[float] = None
    heading_deg: Optional[float] = None
    recorded_at: Optional[datetime] = None
    point_index: Optional[int] = None

    def as_details(self) -> dict:
        return {"distance_m": self.distance_m, "speed_kmh": self.speed_kmh, "heading_deg": self.heading_deg,
                "reason": self.reason}


# ── pure geometry ────────────────────────────────────────────────────────────

def bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing from point 1 to point 2, degrees in [0, 360)."""
    p1, p2 = radians(lat1), radians(lat2)
    dl = radians(lon2 - lon1)
    x = sin(dl) * cos(p2)
    y = cos(p1) * sin(p2) - sin(p1) * cos(p2) * cos(dl)
    return (degrees(atan2(x, y)) + 360.0) % 360.0


def angle_diff(a: float, b: float) -> float:
    d = abs(a - b) % 360.0
    return 360.0 - d if d > 180.0 else d


def heading_at(points: Sequence[TrackPt], i: int, window: int = HEADING_WINDOW,
               min_move_m: float = HEADING_MIN_MOVE_M) -> Optional[float]:
    """Travel direction at points[i]: bearing between the nearest points on
    each side (up to `window` away) that are ≥ min_move_m apart; None when
    the vehicle did not move enough (stopped, single fix)."""
    if not points:
        return None
    n = len(points)
    for k in range(1, window + 1):
        a, b = points[max(0, i - k)], points[min(n - 1, i + k)]
        if _haversine_m(a.latitude, a.longitude, b.latitude, b.longitude) >= min_move_m:
            return bearing(a.latitude, a.longitude, b.latitude, b.longitude)
    return None


def speed_at(points: Sequence[TrackPt], i: int) -> Optional[float]:
    """OSD speed of the fix, else derived from its neighbours' positions and times."""
    p = points[i]
    if p.speed_kmh is not None:
        return float(p.speed_kmh)
    a, b = points[max(0, i - 1)], points[min(len(points) - 1, i + 1)]
    if a is b or a.video_time_s is None or b.video_time_s is None or b.video_time_s <= a.video_time_s:
        return None
    d = _haversine_m(a.latitude, a.longitude, b.latitude, b.longitude)
    return round(d / (b.video_time_s - a.video_time_s) * 3.6, 1)


def closest_confirmed(points: Sequence[TrackPt], lat: float, lon: float) -> tuple[Optional[int], Optional[float]]:
    best_i, best_d = None, None
    for i, p in enumerate(points):
        if p.source not in CONFIRMED_SOURCES:
            continue
        d = _haversine_m(lat, lon, p.latitude, p.longitude)
        if best_d is None or d < best_d:
            best_i, best_d = i, d
    return best_i, best_d


def approached(points: Sequence[TrackPt], i: int, lat: float, lon: float, min_m: float) -> bool:
    """The vehicle came towards the spot before its closest fix `i`: the
    nearest earlier confirmed fix that is ≥ min_m farther away had the spot
    ahead (within 90° of its heading; unknown heading → accepted)."""
    d_i = _haversine_m(lat, lon, points[i].latitude, points[i].longitude)
    for j in range(i - 1, -1, -1):
        p = points[j]
        if p.source not in CONFIRMED_SOURCES:
            continue
        if _haversine_m(lat, lon, p.latitude, p.longitude) >= d_i + min_m:
            h = heading_at(points, j)
            return h is None or angle_diff(h, bearing(p.latitude, p.longitude, lat, lon)) <= 90.0
    return False


def evaluate_pass(points: Sequence[TrackPt], hazard_lat: float, hazard_lon: float, *,
                  last_seen_at: Optional[datetime], ref_headings: Sequence[float],
                  start_time: Optional[datetime], rules: PassRules, detected_nearby: bool = False) -> PassResult:
    """Is this trajectory a valid "not seen" pass of the hazard? (rules in the module docstring)"""
    i, dist = closest_confirmed(points, hazard_lat, hazard_lon)
    if i is None or dist is None or dist > rules.max_distance_m:
        return PassResult(False, False, "not_passed", distance_m=round(dist, 1) if dist is not None else None)
    res = PassResult(True, False, "valid", distance_m=round(dist, 1), point_index=i)
    if detected_nearby:
        res.reason = "detected"
        return res
    res.speed_kmh = speed_at(points, i)
    if res.speed_kmh is None:
        res.reason = "unknown_speed"
        return res
    if res.speed_kmh > rules.max_speed_kmh:
        res.reason = "too_fast"
        return res
    h = heading_at(points, i)
    res.heading_deg = round(h, 1) if h is not None else None
    if ref_headings:
        if h is None:
            res.reason = "unknown_heading"
            return res
        if min(angle_diff(h, r) for r in ref_headings) > rules.heading_tolerance_deg:
            res.reason = "wrong_direction"
            return res
    elif rules.require_same_direction:
        res.reason = "unknown_reference_heading"
        return res
    if rules.require_approach and not approached(points, i, hazard_lat, hazard_lon, rules.approach_min_m):
        res.reason = "not_approaching"
        return res
    if points[i].recorded_at is not None:
        res.recorded_at = to_utc_naive(points[i].recorded_at, rules.timezone)
    elif start_time is None:
        res.reason = "unknown_time"
        return res
    else:
        res.recorded_at = start_time + timedelta(seconds=points[i].video_time_s or 0.0)
    if last_seen_at is not None and res.recorded_at <= last_seen_at:
        res.reason = "before_last_seen"
        return res
    if rules.daylight_only and not in_daylight(res.recorded_at, rules.timezone, rules.daylight_hours):
        res.reason = "not_daylight"
        return res
    res.valid = True
    return res


def recording_start(dets: Sequence, cfg: HazardLifecycleSettings) -> Optional[datetime]:
    """Recording time of video_time_s = 0, from the job's detections (median
    of recording time − video_time_s); None when no detection has both."""
    from app.modules.m06_hazards.lifecycle.service import det_time
    starts = []
    for d in dets:
        if d.video_time_s is None or not has_true_recording_time(d):
            continue
        t = det_time(d, cfg)
        if t is not None:
            starts.append(t - timedelta(seconds=float(d.video_time_s)))
    if not starts:
        return None
    starts.sort()
    return starts[(len(starts) - 1) // 2]      # lower median — robust to one bad OSD clock


def eligible_types(enabled_models: Sequence[str], type_models: dict[str, list[str]]) -> set[str]:
    """Hazard types whose detector ran in a job with these models."""
    enabled = {m.strip().lower() for m in enabled_models if m and m.strip()}
    return {t for t, models in type_models.items() if enabled & {m.lower() for m in models}}


def rules_for(cfg: HazardLifecycleSettings, pol) -> PassRules:
    return PassRules(max_distance_m=cfg.pass_max_distance_m, max_speed_kmh=cfg.pass_max_speed_kmh,
                     heading_tolerance_deg=cfg.pass_heading_tolerance_deg,
                     require_same_direction=pol.require_same_direction, daylight_only=pol.pass_daylight_only,
                     daylight_hours=list(cfg.daylight_hours), timezone=cfg.timezone,
                     require_approach=cfg.pass_require_approach, approach_min_m=cfg.pass_approach_min_m)


# ── database ─────────────────────────────────────────────────────────────────

async def _track(db: AsyncSession, job_id: str, cache: dict) -> list[TrackPt]:
    from app.modules.m07_ingestion.models.job_track_point import JobTrackPoint
    if job_id not in cache:
        rows = (await db.execute(select(JobTrackPoint).where(JobTrackPoint.job_id == job_id)
                                 .order_by(JobTrackPoint.frame_number))).scalars().all()
        cache[job_id] = [TrackPt(r.latitude, r.longitude, r.speed_kmh, r.video_time_s, r.source or "ocr",
                                 r.frame_number, getattr(r, "recorded_at", None)) for r in rows]
    return cache[job_id]


async def reference_headings(db: AsyncSession, members: Sequence, cache: dict, limit: int = 5) -> list[float]:
    """Travel headings at the hazard's original sightings (its member
    detections, newest first), from each one's own job trajectory."""
    out: list[float] = []
    for m in members[:limit]:
        if not m.job_id:
            continue
        pts = await _track(db, m.job_id, cache)
        if len(pts) < 2:
            continue
        i = min(range(len(pts)), key=lambda k: abs(pts[k].frame_number - (m.frame_number or 0)))
        h = heading_at(pts, i)
        if h is not None:
            out.append(h)
    return out


def _within(det, hazard: Hazard, radius_m: float, distinct_subtype_types) -> bool:
    if det.type != hazard.type or det.latitude is None or det.longitude is None:
        return False
    if hazard.type in distinct_subtype_types and (hazard.subtype or None) != (det.subtype or None):
        return False
    from app.modules.m06_hazards.object_position import position
    lat, lon = position(det)  # type: ignore[misc]   # estimated object position, else the vehicle's
    return _haversine_m(lat, lon, hazard.latitude, hazard.longitude) <= radius_m


async def evaluate_job(db: AsyncSession, job_id: str, cfg: Optional[HazardLifecycleSettings] = None) -> dict:
    """Sightings + "not seen" passes of a done job. Idempotent per (job,
    hazard) and per detection; commits. Returns a summary."""
    from app.modules.m07_ingestion.models.job import Job, JobStatus
    from app.modules.m06_hazards import hazard_service
    from app.modules.m07_ingestion import job_service
    from app.modules.m06_hazards.lifecycle import service, sightings
    summary: dict = {"sightings": 0, "passes": 0, "not_seen": 0, "resolved": [], "rejected": {}}
    job = await db.get(Job, job_id)
    status = getattr(job, "status", None)
    if job is None or (status.value if isinstance(status, JobStatus) else status) != JobStatus.done.value:
        summary["skipped"] = "job not done"
        return summary
    cfg = cfg or await service.load_settings(db, fresh=True)
    ctx = await hazard_service.load_context(db)

    summary["sightings"] = await sightings.record_job_sightings(db, job_id, cfg, ctx)
    await db.flush()

    types = eligible_types((job.enabled_models or "").split(","), cfg.type_models)
    cache: dict = {}
    points = await _track(db, job_id, cache)
    if types and points:
        dets = (await db.execute(select(Detection).where(Detection.job_id == job_id))).scalars().all()
        start = recording_start(dets, cfg)
        if start is None and not cfg.pass_require_recording_time:
            start = job.started_at or job.created_at        # upper bound: processing time
        pad = cfg.pass_max_distance_m
        lats, lons = [p.latitude for p in points], [p.longitude for p in points]
        dlat, dlon = hazard_service._bbox(sum(lats) / len(lats), sum(lons) / len(lons), pad)
        hazards = (await db.execute(select(Hazard).where(
            Hazard.status.in_(OPEN_STATUSES), Hazard.type.in_(types),
            Hazard.latitude.between(min(lats) - dlat, max(lats) + dlat),
            Hazard.longitude.between(min(lons) - dlon, max(lons) + dlon),
        ))).scalars().all()
        for hazard in hazards:
            members = await hazard_service.members_of(db, hazard.id)
            pol = policy_for_hazard(cfg, hazard, members)
            if not pol.absence_resolution:
                continue
            if await events.exists(db, hazard.id, ("not_seen", "seen"), job_id=job_id):
                continue
            if any(m.job_id == job_id for m in members):
                continue                                   # its own video: never an absence
            radius = hazard.radius_m or ctx.radius_for(hazard.latitude, hazard.longitude, hazard.type)
            nearby = any(_within(d, hazard, radius, ctx.distinct_subtype_types) for d in dets)
            res = evaluate_pass(points, hazard.latitude, hazard.longitude, last_seen_at=hazard.last_seen_at,
                                ref_headings=await reference_headings(db, members, cache),
                                start_time=start, rules=rules_for(cfg, pol), detected_nearby=nearby)
            if not res.passed:
                continue
            summary["passes"] += 1
            if not res.valid:
                summary["rejected"][res.reason] = summary["rejected"].get(res.reason, 0) + 1
                continue
            confirmed = hazard.status == HazardStatus.active.value
            prior = await service.recount_absences(db, hazard)
            events.record(db, hazard.id, "not_seen", source="video", job_id=job_id, recorded_at=res.recorded_at,
                          details={**res.as_details(), "pass_number": prior + 1,
                                   "threshold": pol.absences_to_resolve, "job_id": job_id,
                                   "job_name": job.source_name or job.filename, "hazard_confirmed": confirmed})
            summary["not_seen"] += 1
            count = await service.recount_absences(db, hazard)
            if count >= pol.absences_to_resolve and not confirmed:
                await service.dismiss(db, hazard, ResolutionSource.video_absence.value, evidence_at=res.recorded_at,
                                      members=members, details={"passes": count, "threshold": pol.absences_to_resolve,
                                                                "job_id": job_id})
                summary.setdefault("dismissed", []).append(hazard.code)
            elif count >= pol.absences_to_resolve:
                await service.resolve(db, hazard, ResolutionSource.video_absence.value, evidence_at=res.recorded_at,
                                      members=members,
                                      note=f"Not seen on {count} valid video pass(es).",
                                      details={"passes": count, "threshold": pol.absences_to_resolve,
                                               "job_id": job_id})
                summary["resolved"].append(hazard.code)
    elif not types:
        summary["skipped"] = "no lifecycle-relevant model in this job"

    job_service.record_event(db, job_id, "hazard_lifecycle", details=summary)
    await db.commit()
    logger.info(f"[Job {job_id}] hazard lifecycle: {summary}")
    return summary
