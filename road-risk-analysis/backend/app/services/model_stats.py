"""
Live quality metrics of the AI validation (RRAP-05-012/016, RRAP-13-016),
computed from what humans decided about the AI's decisions — GET /models/stats.

Per window (7 / 30 days, by detection creation time) and per model version
(detector weights + card, classifier weights + card):

  counts            auto_accept / auto_reject / review (ai_decision), audit samples
  workload_pct      share of detections that needed a human: `review` + audit samples
  audit             human verdict on audit samples: decided, agreed, agreement_rate
  auto_accept_precision   audited auto-accepts confirmed as the predicted class
  auto_reject_precision   audited auto-rejects confirmed as not a hazard
  reopened          automatic decisions a human reopened, and how they were re-decided

A human verdict is the current status when the last reviewer is not
"system": accepted (validated / corrected / resolved) with the predicted
class kept (traffic signs: and the predicted sign code — subtype — kept)
→ agrees with auto_accept; rejected → agrees with auto_reject.
Detections created before the AI validation mode (ai_decision NULL) are
ignored. Pure aggregation (compute) + one query (load_rows), so the rules
are unit-tested without a database.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.detection import Detection
from app.models.detection_review import DetectionReview

WINDOWS = (7, 30)
ACCEPTED = {"validated", "corrected", "resolved"}
AUTO = ("auto_accept", "auto_reject")
SUBTYPE_IS_CLASS = {"traffic_sign"}    # the subtype is the predicted class (sign code)


@dataclass
class Row:
    id: str
    created_at: datetime
    ai_decision: Optional[str]
    audit_sample: bool
    review_status: str
    reviewed_by: Optional[str]
    type: Optional[str] = None
    predicted_type: Optional[str] = None
    model_name: Optional[str] = None
    model_version: Optional[str] = None
    model_card_version: Optional[str] = None
    verifier_model: Optional[str] = None
    verifier_card_version: Optional[str] = None
    subtype: Optional[str] = None
    predicted_subtype: Optional[str] = None


def human_verdict(r: Row) -> Optional[str]:
    """accept | reject | reclassified | None (no human decision yet)."""
    if not r.reviewed_by or r.reviewed_by == "system":
        return None
    if r.review_status in ACCEPTED:
        if r.predicted_type and r.type and r.type != r.predicted_type:
            return "reclassified"
        if r.type in SUBTYPE_IS_CLASS and r.predicted_subtype and (r.subtype or None) != r.predicted_subtype:
            return "reclassified"
        return "accept"
    if r.review_status == "rejected":
        return "reject"
    return None


def agrees(ai_decision: str, verdict: str) -> bool:
    return (ai_decision == "auto_accept" and verdict == "accept") or \
           (ai_decision == "auto_reject" and verdict == "reject")


def _rate(num: int, den: int) -> Optional[float]:
    return round(num / den, 4) if den else None


def _block(rows: list[Row], reopened: set[str]) -> dict[str, Any]:
    counts = {"auto_accept": 0, "auto_reject": 0, "review": 0}
    audit_samples = 0
    audit = {"decided": 0, "agreed": 0}
    precision = {d: {"decided": 0, "agreed": 0} for d in AUTO}
    reo = {"count": 0, "decided": 0, "agreed": 0}
    for r in rows:
        if r.ai_decision in counts:
            counts[r.ai_decision] += 1
        verdict = human_verdict(r)
        if r.audit_sample and r.ai_decision in AUTO:
            audit_samples += 1
            if verdict is not None:
                ok = agrees(r.ai_decision, verdict)
                audit["decided"] += 1
                audit["agreed"] += ok
                precision[r.ai_decision]["decided"] += 1
                precision[r.ai_decision]["agreed"] += ok
        elif r.ai_decision in AUTO and r.id in reopened:
            reo["count"] += 1
            if verdict is not None:
                reo["decided"] += 1
                reo["agreed"] += agrees(r.ai_decision, verdict)
    total = len(rows)
    needing_human = counts["review"] + audit_samples
    return {
        "total": total,
        "auto_accept": counts["auto_accept"],
        "auto_reject": counts["auto_reject"],
        "review": counts["review"],
        "audit_samples": audit_samples,
        "workload_pct": round(100 * needing_human / total, 1) if total else None,
        "audit": {**audit, "agreement_rate": _rate(audit["agreed"], audit["decided"])},
        "auto_accept_precision": _rate(precision["auto_accept"]["agreed"], precision["auto_accept"]["decided"]),
        "auto_accept_audited": precision["auto_accept"]["decided"],
        "auto_reject_precision": _rate(precision["auto_reject"]["agreed"], precision["auto_reject"]["decided"]),
        "auto_reject_audited": precision["auto_reject"]["decided"],
        "reopened": {**reo, "agreement_rate": _rate(reo["agreed"], reo["decided"])},
    }


def _model_key(r: Row) -> tuple:
    return (r.model_name, r.model_version, r.model_card_version, r.verifier_model, r.verifier_card_version)


def compute(rows: Iterable[Row], reopened: set[str], now: datetime,
            windows: Iterable[int] = WINDOWS) -> dict[str, Any]:
    rows = [r for r in rows if r.ai_decision]
    out = []
    for days in windows:
        since = now - timedelta(days=days)
        win = [r for r in rows if r.created_at >= since]
        groups: dict[tuple, list[Row]] = {}
        for r in win:
            groups.setdefault(_model_key(r), []).append(r)
        by_model = [
            {"model_name": k[0], "model_version": k[1], "model_card_version": k[2],
             "verifier_model": k[3], "verifier_card_version": k[4], **_block(g, reopened)}
            for k, g in sorted(groups.items(), key=lambda kv: -len(kv[1]))
        ]
        out.append({"days": days, "since": since, **_block(win, reopened), "by_model": by_model})
    return {"generated_at": now, "windows": out}


async def load_rows(db: AsyncSession, since: datetime) -> tuple[list[Row], set[str]]:
    cols = (Detection.id, Detection.created_at, Detection.ai_decision, Detection.audit_sample,
            Detection.review_status, Detection.reviewed_by, Detection.type, Detection.predicted_type,
            Detection.model_name, Detection.model_version, Detection.model_card_version,
            Detection.verifier_model, Detection.verifier_card_version,
            Detection.subtype, Detection.predicted_subtype)
    result = await db.execute(select(*cols).where(Detection.created_at >= since, Detection.ai_decision.isnot(None)))
    rows = []
    for rec in result.all():
        d = dict(rec._mapping)
        rs = d["review_status"]
        d["review_status"] = getattr(rs, "value", rs)
        d["audit_sample"] = bool(d["audit_sample"])
        rows.append(Row(**d))
    reopened: set[str] = set()
    auto_ids = [r.id for r in rows if r.ai_decision in AUTO and not r.audit_sample]
    for i in range(0, len(auto_ids), 5000):
        chunk = auto_ids[i:i + 5000]
        reopened.update((await db.execute(select(DetectionReview.detection_id).where(
            DetectionReview.detection_id.in_(chunk), DetectionReview.action == "reopen"))).scalars().all())
    return rows, reopened


async def stats(db: AsyncSession, now: Optional[datetime] = None) -> dict[str, Any]:
    now = now or datetime.utcnow()
    rows, reopened = await load_rows(db, now - timedelta(days=max(WINDOWS)))
    return compute(rows, reopened, now)
