"""
Observation identity (docs/CLUSTERS.md § Observation identity).

The same footage processed several times (the full video, then clips cut
from it, re-uploads under another name …) yields the SAME observation more
than once: same camera, same recording instant, same object on the frame.
Those are one observation, not several sightings. Two detections are the
same observation when:

  type      same detection type (current or predicted class), and the same
            subtype for distinct-subtype types (traffic signs);
  camera    same OSD vehicle id (both unknown counts as the same);
  instant   same recording time — both `recorded_at` (OSD camera time)
            within ±INSTANT_TOLERANCE_S; when neither has one (footage whose
            OSD shows no readable time), the same OSD GPS reading
            (`gps_key`: the two coordinate fields of raw_gps_text — a fix
            changes every second while moving); one with and one without a
            time are different;
  region    same place on the frame: normalised box IoU ≥ MIN_IOU, or
            centres closer than MAX_CENTRE_DIST (fraction of the frame).

The file / job name plays no part. The processor does not store a detection
that repeats an existing one (any review status) — `duplicates_skipped` in
the job's stats — and `python -m app.cli dedupe-observations` removes the
duplicates already stored (`dedupe`).
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Any, Iterable, Optional, Sequence

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

INSTANT_TOLERANCE_S = 1.0
MIN_IOU = 0.3
MAX_CENTRE_DIST = 0.03
_NUMBER = re.compile(r"\d+\.\d+")


# ── pure rule ─────────────────────────────────────────────────────────────────

def gps_key(raw: Optional[str]) -> Optional[tuple[str, str]]:
    """The two coordinate fields of an OSD GPS text ("0515.3583,N,01013.5471,E,…"
    → ("0515.3583", "01013.5471")); None when unreadable."""
    nums = _NUMBER.findall(raw or "")
    return (nums[0], nums[1]) if len(nums) >= 2 else None


def _kinds(det: Any, attr: str) -> set:
    out = {getattr(det, attr, None)}
    out.add(getattr(det, f"predicted_{attr}", None))
    return {k for k in out if k is not None} or {None}


def _box(det: Any) -> Optional[tuple[float, float, float, float]]:
    box = (det.bbox_x1, det.bbox_y1, det.bbox_x2, det.bbox_y2)
    if any(v is None for v in box):
        return None
    w, h = getattr(det, "frame_width", None), getattr(det, "frame_height", None)
    if w and h:
        return box[0] / w, box[1] / h, box[2] / w, box[3] / h
    return box  # type: ignore[return-value]


def _iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def same_region(a: Any, b: Any) -> bool:
    ba, bb = _box(a), _box(b)
    if ba is None or bb is None:
        return False
    if _iou(ba, bb) >= MIN_IOU:
        return True
    normalised = all(getattr(d, "frame_width", None) and getattr(d, "frame_height", None) for d in (a, b))
    if not normalised:
        return False
    ca = ((ba[0] + ba[2]) / 2, (ba[1] + ba[3]) / 2)
    cb = ((bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2)
    return ((ca[0] - cb[0]) ** 2 + (ca[1] - cb[1]) ** 2) ** 0.5 <= MAX_CENTRE_DIST


def same_instant(a: Any, b: Any) -> bool:
    ta, tb = a.recorded_at, b.recorded_at
    if ta is not None and tb is not None:
        return abs((ta - tb).total_seconds()) <= INSTANT_TOLERANCE_S
    if ta is not None or tb is not None:
        return False
    ka = gps_key(a.raw_gps_text)
    return ka is not None and ka == gps_key(b.raw_gps_text)


def same_observation(a: Any, b: Any, distinct_subtype_types: Sequence[str] = ("traffic_sign",)) -> bool:
    """The identity rule of the module docstring."""
    if not (a.type == b.type or _kinds(a, "type") & _kinds(b, "type")):
        return False
    if a.type in distinct_subtype_types and (a.subtype or None) != (b.subtype or None) \
            and not (_kinds(a, "subtype") & _kinds(b, "subtype")) - {None}:
        return False
    if (a.vehicle_id or None) != (b.vehicle_id or None):
        return False
    return same_instant(a, b) and same_region(a, b)


# ── processor lookup ─────────────────────────────────────────────────────────

async def find_existing(db: AsyncSession, obs: Any, distinct_subtype_types: Sequence[str] = ("traffic_sign",)):
    """A stored detection (any review status) that is the same observation as
    `obs` (an unsaved Detection or any object with the same fields), or None.
    One indexed query: same type and recorded_at ± 1 s ((type, recorded_at)
    index); without a recording time, the stored detections of that type
    without one whose GPS text has the same coordinates."""
    from app.modules.m06_hazards.models.detection import Detection
    q = select(Detection).where(or_(Detection.type == obs.type, Detection.predicted_type == obs.type),
                                Detection.vehicle_id.is_not_distinct_from(obs.vehicle_id or None))
    if obs.recorded_at is not None:
        tol = timedelta(seconds=INSTANT_TOLERANCE_S)
        q = q.where(Detection.recorded_at.between(obs.recorded_at - tol, obs.recorded_at + tol))
    else:
        key = gps_key(obs.raw_gps_text)
        if key is None:
            return None
        q = q.where(Detection.recorded_at.is_(None), Detection.raw_gps_text.contains(key[0]),
                    Detection.raw_gps_text.contains(key[1]))
    for d in (await db.execute(q.order_by(Detection.created_at))).scalars().all():
        if d.id != getattr(obs, "id", None) and same_observation(obs, d, distinct_subtype_types):
            return d
    return None


# ── clean-up of stored duplicates ────────────────────────────────────────────

def groups(dets: Sequence[Any], distinct_subtype_types: Sequence[str] = ("traffic_sign",)) -> list[list[Any]]:
    """Groups (≥ 2 members) of detections that are the same observation
    (transitively), each sorted by creation time."""
    parent = list(range(len(dets)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    buckets: dict = {}
    for i, d in enumerate(dets):
        inst = ("t", None) if d.recorded_at is not None else ("g", gps_key(d.raw_gps_text))
        if inst == ("g", None):
            continue
        buckets.setdefault((d.predicted_type or d.type, d.vehicle_id or None, inst[0], inst[1]), []).append(i)
    for idx in buckets.values():
        idx.sort(key=lambda i: (dets[i].recorded_at.timestamp() if dets[i].recorded_at is not None else 0))
        for n, i in enumerate(idx):
            for j in idx[n + 1:]:
                a, b = dets[i], dets[j]
                if a.recorded_at is not None and \
                        (b.recorded_at - a.recorded_at).total_seconds() > INSTANT_TOLERANCE_S:
                    break
                if same_observation(a, b, distinct_subtype_types):
                    parent[find(j)] = find(i)
    out: dict = {}
    for i in range(len(dets)):
        out.setdefault(find(i), []).append(dets[i])
    return [sorted(g, key=lambda d: (d.created_at, d.id)) for g in out.values() if len(g) > 1]


def human_reviewed(det: Any, user_reviewed_ids: Iterable[str] = ()) -> bool:
    """A person decided on it: reviewed by someone other than `system`, or a
    user transition in its history."""
    return bool(det.reviewed_by and det.reviewed_by != "system") or det.id in set(user_reviewed_ids)


def keeper(group: Sequence[Any], user_reviewed_ids: Iterable[str] = ()) -> Any:
    """The detection a duplicate group keeps: the earliest created one that a
    person reviewed (its decision, history and training sample stay intact),
    else the earliest created one."""
    ids = set(user_reviewed_ids)
    human = [d for d in group if human_reviewed(d, ids)]
    return min(human or group, key=lambda d: (d.created_at, d.id))


EVIDENCE_FIELDS = ("image_url", "crop_url", "frame_clean_url", "context_clip_url")


async def dedupe(db: AsyncSession, *, dry_run: bool = False, commit=None, log: Optional[list] = None) -> dict:
    """Delete the duplicate observations already stored (module docstring):
    per group the `keeper` stays; every other member is deleted — its hazard
    timeline references, validation label and training sample move to the
    keeper or go — and its cluster is recomputed. Returns {groups,
    deleted, conflicts, objects (evidence object names exclusively theirs,
    to delete after the commit), crop_keys (training crops), frames}. """
    from sqlalchemy import delete, update
    from app.modules.m06_hazards import hazard_service
    from app.modules.m06_hazards.models.detection import Detection
    from app.modules.m06_hazards.models.detection_review import DetectionReview
    from app.modules.m06_hazards.models.hazard import Hazard
    from app.modules.m06_hazards.models.hazard_lifecycle import HazardEvent
    from app.modules.m06_hazards.models.training_sample import TrainingFrame, TrainingSample
    from app.modules.m06_hazards.models.validation_label import ValidationLabel
    from app.core.settings_service import ValidationSettings, get_section
    rules: ValidationSettings = await get_section(db, "validation")  # type: ignore[assignment]
    log = log if log is not None else []
    dets = (await db.execute(select(Detection))).scalars().all()
    found = groups(dets, list(rules.distinct_subtype_types))
    users = set((await db.execute(select(DetectionReview.detection_id).where(
        DetectionReview.actor_type == "user"))).scalars().all())
    out = {"groups": len(found), "deleted": 0, "conflicts": 0, "objects": [], "crop_keys": [], "frames": []}
    doomed_urls: set = set()
    for group in found:
        keep = keeper(group, users)
        dups = [d for d in group if d is not keep]
        decisions = {d.review_status for d in group if human_reviewed(d, users)}
        if len(decisions) > 1:
            out["conflicts"] += 1
            log.append(f"conflict: {[(d.id[:8], str(getattr(d.review_status, 'value', d.review_status))) for d in group]}"
                       f" — kept {keep.id[:8]}")
        hazard_ids = {d.hazard_id for d in dups if d.hazard_id}
        for d in dups:
            await db.execute(update(HazardEvent).where(HazardEvent.detection_id == d.id)
                             .values(detection_id=keep.id))
            if (await db.execute(select(ValidationLabel.id).where(ValidationLabel.detection_id == keep.id))).first():
                await db.execute(delete(ValidationLabel).where(ValidationLabel.detection_id == d.id))
            else:
                await db.execute(update(ValidationLabel).where(ValidationLabel.detection_id == d.id)
                                 .values(detection_id=keep.id))
            sample = await db.get(TrainingSample, d.id)
            if sample is not None:
                if sample.crop_key:
                    out["crop_keys"].append(sample.crop_key)
                out["frames"].append((sample.detection_type, sample.frame_key))
            doomed_urls.update(u for u in (getattr(d, f) for f in EVIDENCE_FIELDS) if u)
            log.append(f"duplicate {d.id[:8]} (job {d.job_id[:8]}, frame {d.frame_number}, "
                       f"{getattr(d.review_status, 'value', d.review_status)}) of {keep.id[:8]} "
                       f"(job {keep.job_id[:8]}) → deleted")
            await db.delete(d)
            out["deleted"] += 1
        await db.flush()
        for hid in hazard_ids:
            hazard = await db.get(Hazard, hid)
            if hazard is not None:
                await hazard_service.recompute(db, hazard)
        if commit is not None:
            await commit()
    if out["frames"]:
        for t, key in set(out["frames"]):
            await db.execute(update(TrainingFrame).where(TrainingFrame.detection_type == t,
                                                         TrainingFrame.frame_key == key).values(dirty=True))
        if commit is not None:
            await commit()
    # evidence objects still referenced by a remaining detection or training frame stay
    if doomed_urls:
        still: set = set()
        for f in EVIDENCE_FIELDS:
            col = getattr(Detection, f)
            still.update((await db.execute(select(col).where(col.in_(doomed_urls)))).scalars().all())
        out["objects"] = sorted(doomed_urls - still)
        sources = set((await db.execute(select(TrainingFrame.source_object))).scalars().all())
        out["objects"] = [u for u in out["objects"] if _object_name(u) not in sources]
    return out


def _object_name(url: str) -> Optional[str]:
    from app.core.config import settings
    from app.modules.m06_hazards.training_data.layout import object_name
    return object_name(url, settings.MINIO_BUCKET_DETECTIONS)


def delete_objects(urls: Iterable[str], crop_keys: Iterable[str]) -> int:
    """Remove evidence objects (detections bucket) and training crops
    (datasets bucket); errors are logged, never raised. Returns how many."""
    from app.core.config import settings
    from app.core.storage.minio_service import get_minio
    n = 0
    try:
        client = get_minio()
    except Exception as exc:                             # noqa: BLE001
        logger.warning(f"dedupe: MinIO unavailable, evidence objects kept: {exc}")
        return 0
    for bucket, names in ((settings.MINIO_BUCKET_DETECTIONS, [_object_name(u) for u in urls]),
                          (settings.MINIO_BUCKET_DATASETS, list(crop_keys))):
        for name in names:
            if not name:
                continue
            try:
                client.remove_object(bucket, name)
                n += 1
            except Exception as exc:                     # noqa: BLE001
                logger.warning(f"dedupe: could not remove {bucket}/{name}: {exc}")
    return n
