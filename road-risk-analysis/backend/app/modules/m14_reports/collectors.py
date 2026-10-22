"""
Data of each report type. `gather_*` read the database (async); `build_*` are
pure and turn rows into the document model rendered by pdf.py:

    {"title", "subtitle", "filters": [(label, value)], "kpis": [(label, value)],
     "map": {"path": [(lat, lon)] | None, "points": [(lat, lon, n, colour_key)], "legend": [(label, key)]} | None,
     "blocks": [{"kind": "heading" | "para" | "chart" | "table", …}], "methodology": [str]}

Table cells are strings, {"text", "link"} or {"image": bytes | None, "link", "label"}.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.m09_driver_behaviour import analytics as BA
from app.modules.m09_driver_behaviour.settings import BEHAVIOUR_TYPES, DriverBehaviourSettings
from app.modules.m14_reports.pathfilter import Path

logger = logging.getLogger(__name__)

REPORT_TYPES = ("hazards", "community", "historical", "driver_behaviour", "collision")
DATA_MODULE = {"hazards": "detections", "community": "signalements", "historical": "historical",
               "driver_behaviour": "driver_behaviour", "collision": "collisions"}
MAX_ROWS = 2000


@dataclass
class Ctx:
    report_type: str
    t: Callable[[str], str]
    window: BA.Window
    types: List[str] = field(default_factory=list)
    statuses: List[str] = field(default_factory=list)
    zone: Optional[Any] = None              # object with .name and .shape (Shape)
    town: Optional[str] = None
    path: Optional[Path] = None
    include_images: bool = True
    max_images: int = 60
    web_base: str = "http://localhost:3000"
    lang: str = "en"
    generated_by: Optional[str] = None
    now: datetime = field(default_factory=datetime.utcnow)

    def place_ok(self, lat, lon, town_text: Optional[str] = None, town_applicable: bool = True) -> bool:
        if lat is None or lon is None:
            return self.zone is None and self.path is None and not (self.town and town_applicable)
        if self.zone is not None and not self.zone.shape.contains(lat, lon):
            return False
        if self.town and town_applicable and self.town.lower() not in (town_text or "").lower():
            return False
        return True

    def bbox(self) -> Optional[Tuple[float, float, float, float]]:
        boxes = []
        if self.path is not None:
            boxes.append(self.path.bbox)
        if self.zone is not None and self.zone.shape.bbox:
            boxes.append(tuple(self.zone.shape.bbox))
        if not boxes:
            return None
        return (max(b[0] for b in boxes), max(b[1] for b in boxes), min(b[2] for b in boxes), min(b[3] for b in boxes))

    def place_filter(self, items: Sequence, lat_of, lon_of, town_of=lambda x: None,
                     town_applicable: bool = True) -> List[Tuple[Optional[float], Optional[float], Any]]:
        """[(km, offset_m, item)] after the zone / town / path filters (path order when a path is set)."""
        kept = [it for it in items if self.place_ok(lat_of(it), lon_of(it), town_of(it), town_applicable)]
        if self.path is None:
            return [(None, None, it) for it in kept]
        return self.path.keep(kept, lat_of, lon_of)


# ── shared bits ──────────────────────────────────────────────────────────────

def _d(ts: Optional[datetime], with_time: bool = False) -> str:
    if ts is None:
        return "-"
    return ts.strftime("%Y-%m-%d %H:%M" if with_time else "%Y-%m-%d")


def filters_block(ctx: Ctx, extra: Sequence[Tuple[str, str]] = ()) -> List[Tuple[str, str]]:
    t, w = ctx.t, ctx.window
    if w.start is None:
        period = f"{t('all_time')} (-> {_d(w.end)})"
    else:
        period = f"{_d(w.start)} -> {_d(w.end)}" + (f" ({w.label})" if w.label not in ("custom",) else "")
    out = [(t("period"), period),
           (t("types"), ", ".join(t(x) for x in ctx.types) if ctx.types else t("all_types"))]
    if ctx.statuses:
        out.append((t("statuses"), ", ".join(t(s) for s in ctx.statuses)))
    out.append((t("zone"), ctx.zone.name if ctx.zone is not None else t("none")))
    if ctx.town:
        out.append((t("town"), ctx.town))
    if ctx.path is not None:
        p = ctx.path
        out.append((t("path"), f"{p.name} - {p.length_m / 1000:.1f} km"))
        out.append((t("corridor"), f"{p.corridor_m:.0f} m"))
    else:
        out.append((t("path"), t("none")))
    out.extend(extra)
    return out


def _map(ctx: Ctx, items: Sequence[Tuple[Optional[float], Any]], lat_of, lon_of, key_of) -> Optional[dict]:
    pts = []
    for i, (_, it) in enumerate(items, 1):
        lat, lon = lat_of(it), lon_of(it)
        if lat is not None and lon is not None:
            pts.append((lat, lon, i, key_of(it)))
    if not pts and ctx.path is None:
        return None
    keys = list(dict.fromkeys(p[3] for p in pts))
    return {"path": ctx.path.points if ctx.path else None, "points": pts[:400],
            "legend": [(ctx.t(k), k) for k in keys]}


def _console(ctx: Ctx, path: str) -> str:
    return ctx.web_base.rstrip("/") + path


def _chart_counts(title: str, counter: Dict[str, int], t, horizontal: bool = True) -> dict:
    items = sorted(counter.items(), key=lambda kv: -kv[1])[:12]
    return {"kind": "chart", "chart": {"type": "bar", "title": title, "horizontal": horizontal,
                                       "categories": [t(k) for k, _ in items],
                                       "series": [{"name": title, "values": [v for _, v in items]}]}}


async def fetch_images(urls: Dict[str, Optional[str]], limit: int) -> Dict[str, bytes]:
    """Read up to `limit` stored images (MinIO) — failures are skipped."""
    from app.core.storage.minio_service import read_object_url
    out: Dict[str, bytes] = {}
    for key, url in list(urls.items())[:limit]:
        if not url:
            continue
        data = await asyncio.to_thread(read_object_url, url)
        if data:
            out[key] = data
    return out


# ── 1. AI hazards ────────────────────────────────────────────────────────────

async def gather_hazards(db: AsyncSession, ctx: Ctx) -> dict:
    from app.modules.m06_hazards.models.detection import Detection, ReviewStatus
    from app.modules.m06_hazards.models.hazard import PUBLIC_STATUSES, Hazard
    statuses = [s for s in (ctx.statuses or PUBLIC_STATUSES) if s in PUBLIC_STATUSES] or list(PUBLIC_STATUSES)
    q = select(Hazard).where(Hazard.status.in_(statuses))
    if ctx.types:
        q = q.where(Hazard.type.in_(ctx.types))
    last = func.coalesce(Hazard.last_seen_at, Hazard.last_detected_at, Hazard.created_at)
    if ctx.window.start is not None:
        q = q.where(last >= ctx.window.start)
    q = q.where(func.coalesce(Hazard.first_detected_at, Hazard.created_at) < ctx.window.end)
    if ctx.town:
        like = f"%{ctx.town}%"
        q = q.where(or_(Hazard.town.ilike(like), Hazard.locality.ilike(like), Hazard.city.ilike(like),
                        Hazard.display_location.ilike(like)))
    box = ctx.bbox()
    if box:
        q = q.where(Hazard.latitude.between(box[0], box[2]), Hazard.longitude.between(box[1], box[3]))
    rows = list((await db.execute(q.order_by(last.desc()).limit(MAX_ROWS * 2))).scalars().all())
    kept = ctx.place_filter(rows, lambda h: h.latitude, lambda h: h.longitude, town_applicable=False)[:MAX_ROWS]
    ids = [h.id for _, _, h in kept]
    covers: Dict[str, tuple] = {}
    if ids:
        for chunk in range(0, len(ids), 500):
            part = ids[chunk:chunk + 500]
            res = (await db.execute(
                select(Detection.hazard_id, Detection.id, Detection.image_url, Detection.crop_url)
                .where(Detection.hazard_id.in_(part), Detection.review_status != ReviewStatus.rejected,
                       or_(Detection.image_url.isnot(None), Detection.crop_url.isnot(None)))
                .order_by(Detection.hazard_id, Detection.confidence.desc())
                .distinct(Detection.hazard_id))).all()
            covers.update({hid: (did, img, crop) for hid, did, img, crop in res})
    images = {}
    if ctx.include_images:
        images = await fetch_images({h.id: (covers.get(h.id) or (None, None, None))[1]
                                     or (covers.get(h.id) or (None, None, None))[2]
                                     for _, _, h in kept}, ctx.max_images)
    return build_hazards(ctx, kept, covers, images)


def build_hazards(ctx: Ctx, kept: Sequence[tuple], covers: Dict[str, tuple], images: Dict[str, bytes]) -> dict:
    t = ctx.t
    hz = [h for _, _, h in kept]
    by_type = Counter(h.type for h in hz)
    by_status = Counter(h.status for h in hz)
    by_town = Counter((h.town or h.city or "-") for h in hz)
    dets = sum(h.detection_count or 0 for h in hz)
    confs = [h.avg_confidence for h in hz if h.avg_confidence]
    kpis = [(t("hazards"), str(len(hz))), (t("active"), str(by_status.get("active", 0))),
            (t("resolved"), str(by_status.get("resolved", 0))), (t("total_detections"), str(dets)),
            (t("avg_conf"), f"{sum(confs) / len(confs) * 100:.0f}%" if confs else "-"),
            (t("recurring"), str(sum(1 for h in hz if getattr(h, "recurring_spot", False))))]
    if ctx.path is not None:
        kpis.append((t("path_length"), f"{ctx.path.length_m / 1000:.1f} km"))
    blocks: List[dict] = []
    if len(by_type) > 1:
        blocks.append(_chart_counts(t("by_type"), by_type, t))
        if len(by_town) > 1:
            blocks.append(_chart_counts(t("by_town"), dict(by_town.most_common(10)), t))
    with_path = ctx.path is not None
    cols = [("#", 8)] + ([(t("km_along"), 13)] if with_path else []) + [
        (t("code"), 22), (t("type"), 24), (t("location"), 38 if with_path else 46), (t("status"), 16),
        (t("detections"), 21), (t("last_seen"), 20), (t("image"), 38)]
    rows = []
    for i, (km, off, h) in enumerate(kept, 1):
        cover = covers.get(h.id)
        det_id = cover[0] if cover else None
        link = _console(ctx, f"/detections?hazard={h.id}" + (f"&detection={det_id}" if det_id else ""))
        sub = f" / {h.subtype}" if h.subtype else ""
        loc = h.display_location or ", ".join(x for x in (h.road, h.locality or h.town) if x) or h.location_name or ""
        loc = f"{loc}\n{h.latitude:.5f}, {h.longitude:.5f}".strip()
        row = [str(i)] + ([f"{km:.1f}"] if with_path else []) + [
            {"text": h.code, "link": link}, f"{t(h.type)}{sub}", loc, t(h.status),
            str(h.detection_count or 0), _d(h.last_seen_at or h.last_detected_at),
            {"image": images.get(h.id), "link": link, "label": t("open_image")}]
        rows.append(row)
    blocks.append({"kind": "heading", "text": t("hazard_list")})
    if rows:
        blocks.append({"kind": "para", "text": t("image_note"), "small": True})
        blocks.append({"kind": "table", "columns": cols, "rows": rows})
    else:
        blocks.append({"kind": "para", "text": t("no_data")})
    return {
        "title": t("hazards_title"), "filters": filters_block(ctx), "kpis": kpis,
        "map": _map(ctx, [(k, h) for k, _, h in kept], lambda h: h.latitude, lambda h: h.longitude, lambda h: h.type),
        "blocks": blocks,
        "methodology": [
            "Hazards are confirmed clusters of AI detections (codes HZ-...), statuses active / resolved; "
            "unreviewed candidate clusters (CL-...) are excluded.",
            "Period filter: hazards seen (last sighting) in the period.",
        ] + (["Path filter: hazards whose position lies within the corridor of the path, ordered by distance "
              "along the path (km)."] if ctx.path else []),
        "summary": {"items": len(hz), "by_type": dict(by_type), "by_status": dict(by_status)},
    }


# ── 2. Community reports ─────────────────────────────────────────────────────

async def gather_community(db: AsyncSession, ctx: Ctx) -> dict:
    from app.modules.m11_community.models.signalement import Signalement
    q = select(Signalement).where(Signalement.merged_into_id.is_(None))
    if ctx.types:
        q = q.where(Signalement.type.in_(ctx.types))
    if ctx.statuses:
        q = q.where(Signalement.status.in_(ctx.statuses))
    if ctx.window.start is not None:
        q = q.where(Signalement.reported_at >= ctx.window.start)
    q = q.where(Signalement.reported_at < ctx.window.end)
    box = ctx.bbox()
    if box:
        q = q.where(Signalement.latitude.between(box[0], box[2]), Signalement.longitude.between(box[1], box[3]))
    rows = list((await db.execute(q.order_by(Signalement.reported_at.desc()).limit(MAX_ROWS * 2))).scalars().all())
    kept = ctx.place_filter(rows, lambda s: s.latitude, lambda s: s.longitude, lambda s: s.location_name)[:MAX_ROWS]
    images = {}
    if ctx.include_images:
        images = await fetch_images({s.id: s.image_url for _, _, s in kept}, ctx.max_images)
    return build_community(ctx, kept, images)


def _val(x) -> str:
    return getattr(x, "value", x)


def build_community(ctx: Ctx, kept: Sequence[tuple], images: Dict[str, bytes]) -> dict:
    t = ctx.t
    sg = [s for _, _, s in kept]
    by_type = Counter(_val(s.type) for s in sg)
    by_status = Counter(_val(s.status) for s in sg)
    kpis = [(t("reports"), str(len(sg))), (t("actif"), str(by_status.get("actif", 0))),
            (t("annule"), str(by_status.get("annule", 0))), (t("rejete"), str(by_status.get("rejete", 0))),
            (t("uncertain"), str(sum(1 for s in sg if getattr(s, "uncertain", False))))]
    blocks: List[dict] = []
    if len(by_type) > 1:
        blocks.append(_chart_counts(t("by_type"), by_type, t))
    with_path = ctx.path is not None
    cols = [("#", 8)] + ([(t("km_along"), 13)] if with_path else []) + [
        (t("type"), 28), (t("location"), 44 if with_path else 52), (t("status"), 16), (t("reported_at"), 24),
        (t("votes"), 18), (t("image"), 38)]
    rows = []
    for i, (km, _, s) in enumerate(kept, 1):
        link = _console(ctx, f"/signalements?report={s.id}")
        loc = f"{s.location_name or ''}\n{s.latitude:.5f}, {s.longitude:.5f}".strip()
        if s.description:
            loc += f"\n{s.description[:120]}"
        img = {"image": images.get(s.id), "link": link,
               "label": t("open_image") if s.image_url else t("open_console")}
        rows.append([str(i)] + ([f"{km:.1f}"] if with_path else []) + [
            {"text": t(_val(s.type)), "link": link}, loc, t(_val(s.status)), _d(s.reported_at, True),
            f"{s.present_count or 0} / {s.absent_count or 0}", img])
    blocks.append({"kind": "heading", "text": t("report_list")})
    if rows:
        blocks.append({"kind": "para", "text": t("image_note"), "small": True})
        blocks.append({"kind": "table", "columns": cols, "rows": rows})
    else:
        blocks.append({"kind": "para", "text": t("no_data")})
    return {
        "title": t("community_title"), "filters": filters_block(ctx), "kpis": kpis,
        "map": _map(ctx, [(k, s) for k, _, s in kept], lambda s: s.latitude, lambda s: s.longitude,
                    lambda s: _val(s.type)),
        "blocks": blocks,
        "methodology": ["Community reports (signalements) from the mobile app; duplicates merged into another report "
                        "are excluded. Validation is by the community's present / absent votes."],
        "summary": {"items": len(sg), "by_type": dict(by_type), "by_status": dict(by_status)},
    }


# ── 3. Historical risk (Module 4) ────────────────────────────────────────────

async def gather_historical(db: AsyncSession, ctx: Ctx) -> dict:
    from app.modules.m08_historical import historical_service as hs
    rows = await hs.load_rows(db, ctx.window.start, ctx.window.end, ctx.bbox())
    if ctx.types:
        rows = [r for r in rows if r.event_type in ctx.types]
    kept = ctx.place_filter(rows, lambda r: r.latitude, lambda r: r.longitude, town_applicable=False)
    return build_historical(ctx, [r for _, _, r in kept])


def build_historical(ctx: Ctx, rows: Sequence) -> dict:
    from app.core.config import settings
    from app.modules.m08_historical import historical_service as hs
    t = ctx.t
    cells = hs._aggregate_by_cell(list(rows))
    by_type = Counter(r.event_type for r in rows)
    kpis = [(t("events"), str(len(rows))), (t("cells"), str(len(cells))),
            (t("high_risk_cells"), str(sum(1 for c in cells if c.historical_risk_score >= hs.HIGH_RISK_SCORE))),
            (t("low_coverage"), str(sum(1 for c in cells if c.total_events < hs.LOW_COVERAGE_EVENTS))),
            (t("max_score"), f"{max((c.historical_risk_score for c in cells), default=0):.2f}"),
            (t("accidents"), str(by_type.get("ACCIDENT", 0)))]
    blocks: List[dict] = []
    if rows:
        start = ctx.window.start or min(r.occurred_at for r in rows)
        unit = BA.bucket_unit(start, ctx.window.end)
        series = hs.trend_series(rows, start, ctx.window.end, unit)
        blocks.append({"kind": "chart", "chart": {
            "type": "bar", "title": f"{t('trend')} ({unit})", "categories": [_d(s["bucket"]) for s in series],
            "series": [{"name": t("events"), "values": [s["events"] for s in series]},
                       {"name": t("risk_points"), "values": [s["risk_points"] for s in series]}]}})
        blocks.append(_chart_counts(t("events_by_type"), by_type, t))
    with_path = ctx.path is not None
    ordered = cells
    kms: Dict[str, float] = {}
    if with_path:
        for c in cells:
            kms[c.grid_cell_id] = ctx.path.km_along(c.center_latitude, c.center_longitude)
    top = sorted(ordered, key=lambda c: -c.historical_risk_score)[:40]
    if with_path:
        top = sorted(top, key=lambda c: kms[c.grid_cell_id])
    cols = [("#", 8)] + ([(t("km_along"), 13)] if with_path else []) + [
        (t("road"), 50), (t("location"), 30), (t("score"), 16), (t("coverage"), 18), (t("events"), 15),
        (t("period"), 36)]
    table = []
    for i, c in enumerate(top, 1):
        link = _console(ctx, f"/historical?lat={c.center_latitude:.5f}&lon={c.center_longitude:.5f}")
        table.append([str(i)] + ([f"{kms[c.grid_cell_id]:.1f}"] if with_path else []) + [
            {"text": c.road_name or c.grid_cell_id, "link": link},
            f"{c.center_latitude:.5f}, {c.center_longitude:.5f}", f"{c.historical_risk_score:.2f}",
            t(hs.coverage_level(c.total_events)), str(c.total_events),
            f"{_d(c.earliest_event_at)} -> {_d(c.latest_event_at)}"])
    blocks.append({"kind": "heading", "text": t("route_risk") if with_path else t("top_zones")})
    if table:
        blocks.append({"kind": "table", "columns": cols, "rows": table})
    else:
        blocks.append({"kind": "para", "text": t("no_data")})
    return {
        "title": t("historical_title"), "filters": filters_block(ctx), "kpis": kpis,
        "map": _map(ctx, [(None, c) for c in top], lambda c: c.center_latitude, lambda c: c.center_longitude,
                    lambda c: "high" if c.historical_risk_score >= 0.75 else "medium"
                    if c.historical_risk_score >= hs.HIGH_RISK_SCORE else "low"),
        "blocks": blocks,
        "methodology": [
            f"Cell score R = min(1, sum(weight(type) x events) / {settings.HISTORICAL_RISK_SATURATION_WEIGHT:g}) "
            f"on stable {settings.HISTORICAL_GRID_CELL_SIZE_M:g} m grid cells.",
            "Weights: " + ", ".join(f"{k} {v:g}" for k, v in hs.weights().items()) + ".",
            f"Coverage: low < {hs.LOW_COVERAGE_EVENTS} events - a low score there is not evidence of safety.",
        ],
        "summary": {"items": len(rows), "cells": len(cells), "by_type": dict(by_type)},
    }


# ── 4. Driver behaviour (Module 5) ───────────────────────────────────────────

async def gather_driver_behaviour(db: AsyncSession, ctx: Ctx) -> dict:
    from app.modules.m09_driver_behaviour import service
    cfg = await service.get_settings(db)
    types = [x for x in ctx.types if x in BEHAVIOUR_TYPES] or list(BEHAVIOUR_TYPES)
    ctx.types = types
    box = ctx.bbox()
    ev = await service.load_events(db, ctx.window.start, ctx.window.end, types, bbox=box)
    prev = None
    if ctx.window.has_previous:
        prev = await service.load_events(db, ctx.window.prev_start, ctx.window.start, types, bbox=box)
    pick = lambda rows: ctx.place_filter(rows, lambda r: r.latitude, lambda r: r.longitude, town_applicable=False)
    kept = pick(ev)
    prev_kept = [r for _, _, r in pick(prev)] if prev is not None else None
    return build_driver_behaviour(ctx, kept, prev_kept, cfg)


def build_driver_behaviour(ctx: Ctx, kept: Sequence[tuple], prev: Optional[Sequence],
                           cfg: DriverBehaviourSettings) -> dict:
    t = ctx.t
    ev = [r for _, _, r in kept]
    types = ctx.types or list(BEHAVIOUR_TYPES)
    ov = BA.overview(ev, prev, cfg, types)
    change = f" ({ov['change_pct']:+.0f}%)" if ov.get("change_pct") is not None else ""
    kpis = [(t("events"), f"{ov['total_events']}{change}"), (t("high_sev"), str(ov["high_severity_events"])),
            (t("drivers"), str(ov["drivers"])), (t("vehicles"), str(ov["vehicles"])),
            ("Score", f"{ov['avg_driver_score']:.0f}" if ov.get("avg_driver_score") is not None else "-")]
    blocks: List[dict] = [{"kind": "heading", "text": t("behaviour_kpis")}]
    trows = []
    for s in ov["by_type"]:
        trows.append([t(s["event_type"]), str(s["count"]), f"{s['share'] * 100:.0f}%", str(s["high_severity"]),
                      _num(s["avg_speed_kmh"]), _num(s["avg_limit_kmh"]), _num(s["avg_excess_pct"]),
                      f"{s['peak_hour']}h" if s["peak_hour"] is not None else "-",
                      ", ".join(r["road_name"] for r in s["top_roads"][:2]) or "-"])
    blocks.append({"kind": "table", "columns": [
        (t("type"), 30), (t("count"), 14), (t("share"), 13), (t("high_sev"), 16), (t("avg_speed"), 18),
        (t("avg_limit"), 18), (t("avg_excess"), 18), (t("peak_hour"), 15), (t("road"), 40)], "rows": trows})
    if ev:
        tr = BA.trends(ev, ctx.window.start, ctx.window.end, cfg, types)
        blocks.append({"kind": "chart", "chart": {
            "type": "bar", "stacked": True, "title": f"{t('trend')} ({tr['unit']})",
            "categories": [_d(s["bucket"]) for s in tr["series"]],
            "series": [{"name": t(x), "values": [s[x] for s in tr["series"]], "key": x} for x in types]}})
        blocks.append({"kind": "chart", "chart": {
            "type": "bar", "stacked": True, "title": t("by_hour"), "categories": [str(h["hour"]) for h in tr["by_hour"]],
            "series": [{"name": t(x), "values": [h[x] for h in tr["by_hour"]], "key": x} for x in types]}})
    for kind, title in (("driver", "driver_ranking"), ("vehicle", "vehicle_ranking")):
        ranked = BA.rank_entities(ev, prev, kind, cfg)[:25]
        if not ranked:
            continue
        blocks.append({"kind": "heading", "text": t(title)})
        rows = []
        for r in ranked:
            link = _console(ctx, f"/driver-behaviour?{kind}={r['id']}")
            delta = f"{r['score_delta']:+.0f} ({t(r['trend'])})" if r.get("score_delta") is not None else "-"
            rows.append([str(r["rank"]), {"text": r["id"], "link": link}, f"{r['score']:.0f}", t(r["level"]),
                         str(r["events"])] + [str(r["by_type"][x]) for x in BEHAVIOUR_TYPES] + [delta])
        blocks.append({"kind": "table", "columns": [
            (t("rank"), 8), (t("id"), 32), ("Score", 14), (t("level"), 18), (t("events"), 15),
            ("SPD", 12), ("BRK", 12), ("ACC", 12), ("CRN", 12), (t("previous"), 28)], "rows": rows})
    zones = BA.zones(ev, cfg)[:20]
    blocks.append({"kind": "heading", "text": t("behaviour_zones")})
    if zones:
        with_path = ctx.path is not None
        rows = []
        for z in zones:
            link = _console(ctx, f"/driver-behaviour?lat={z['latitude']:.5f}&lon={z['longitude']:.5f}")
            km = ctx.path.km_along(z["latitude"], z["longitude"]) if with_path else None
            rows.append([str(z["rank"])] + ([f"{km:.1f}"] if with_path else []) + [
                {"text": z["road_name"] or z["cell_id"], "link": link},
                f"{z['latitude']:.5f}, {z['longitude']:.5f}", t(z["dominant_type"]), str(z["events"]),
                str(z["drivers"]), f"{z['score']:.0f}"])
        blocks.append({"kind": "table", "columns": [("#", 8)] + ([(t("km_along"), 13)] if with_path else []) + [
            (t("road"), 46), (t("location"), 32), (t("dominant"), 30), (t("events"), 15), (t("drivers"), 16),
            ("Score", 14)], "rows": rows})
    else:
        blocks.append({"kind": "para", "text": t("no_data")})
    if not ev:
        blocks.insert(0, {"kind": "para", "text": t("no_data")})
    m = BA.methodology(cfg)
    return {
        "title": t("behaviour_title"), "filters": filters_block(ctx), "kpis": kpis,
        "map": _map(ctx, [(None, z) for z in zones], lambda z: z["latitude"], lambda z: z["longitude"],
                    lambda z: z["dominant_type"]),
        "blocks": blocks,
        "methodology": [f"{m['version']}: {m['formula']}",
                        "Weights: " + ", ".join(f"{k} {v:g}" for k, v in cfg.type_weights.items())
                        + "; severity: " + ", ".join(f"{k} x{v:g}" for k, v in cfg.severity_multipliers.items())
                        + f"; saturation {cfg.saturation_points:g} points.",
                        f"Zones: {cfg.zone_cell_size_m:g} m cells with >= {cfg.zone_min_events} events.",
                        "SPD/BRK/ACC/CRN = speeding / harsh braking / hard acceleration / harsh cornering."]
        + m["notes"],
        "summary": {"items": len(ev), "drivers": ov["drivers"], "vehicles": ov["vehicles"],
                    "by_type": {s["event_type"]: s["count"] for s in ov["by_type"]}},
    }


def _num(v) -> str:
    return "-" if v is None else f"{v:.0f}"


# ── 5. Collision prevention ──────────────────────────────────────────────────

async def gather_collision(db: AsyncSession, ctx: Ctx) -> dict:
    from app.modules.m10_collision.models.vehicle import Vehicle
    vehicles = list((await db.execute(select(Vehicle))).scalars().all())
    alerts: List[dict] = []
    try:
        from app.core.storage.redis_client import get_redis
        r = get_redis()
        async for key in r.scan_iter(match="alert:*", count=500):
            k = key.decode() if isinstance(key, bytes) else key
            if k.startswith(("alert:lock:", "alert:active_pairs:")):
                continue
            raw = await r.get(k)
            if raw:
                try:
                    alerts.append(json.loads(raw))
                except ValueError:
                    pass
            if len(alerts) >= 2000:
                break
    except Exception as e:  # Redis down: the fleet part still renders
        logger.warning("collision report: alerts unavailable (%s)", e)
    return build_collision(ctx, vehicles, alerts)


def build_collision(ctx: Ctx, vehicles: Sequence, alerts: Sequence[dict]) -> dict:
    t = ctx.t
    start = ctx.window.start
    active = [v for v in vehicles if v.last_seen and (start is None or v.last_seen >= start)]
    by_type = Counter(_val(v.vehicle_type) for v in vehicles)
    live = [a for a in alerts if a.get("status") != "RESOLVED" and a.get("level") != "SAFE"]
    if ctx.path is not None or ctx.zone is not None:
        def _loc(a):
            loc = a.get("conflict_location") or [None, None]
            return loc[0], loc[1]
        live = [a for _, _, a in ctx.place_filter(live, lambda a: _loc(a)[0], lambda a: _loc(a)[1],
                                                  town_applicable=False)]
    by_level = Counter(a.get("level") for a in live)
    kpis = [(t("registered"), str(len(vehicles))), (t("active_period"), str(len(active))),
            (t("active_alerts"), str(len(live)))]
    blocks: List[dict] = [{"kind": "para", "text": t("collision_note")}]
    if vehicles:
        blocks.append(_chart_counts(t("vehicles_by_type"), by_type, t))
    if live:
        blocks.append(_chart_counts(t("alerts_by_level"), by_level, lambda x: x))
        rows = []
        for a in sorted(live, key=lambda a: a.get("time_to_conflict_s") or 1e9)[:100]:
            loc = a.get("conflict_location")
            rows.append([a.get("level", "-"), a.get("status", "-"), f"{a.get('vehicle_a_id', '')[:8]} / "
                         f"{a.get('vehicle_b_id', '')[:8]}", _num(a.get("time_to_conflict_s")),
                         _num(a.get("minimum_predicted_distance_m")),
                         f"{loc[0]:.5f}, {loc[1]:.5f}" if loc else "-"])
        blocks.append({"kind": "table", "columns": [(t("level"), 26), (t("status"), 22), (t("vehicles"), 40),
                                                     ("TTC (s)", 16), ("d min (m)", 18), (t("location"), 40)],
                       "rows": rows})
    elif not vehicles:
        blocks.append({"kind": "para", "text": t("no_data")})
    return {
        "title": t("collision_title"), "filters": filters_block(ctx), "kpis": kpis, "map": None,
        "blocks": blocks,
        "methodology": ["Alert levels SAFE < MONITOR < WARNING < HIGH_WARNING < CRITICAL from predicted "
                        "time-to-conflict and minimum distance (settings section `collision`)."],
        "summary": {"items": len(live), "vehicles": len(vehicles)},
    }


GATHER = {"hazards": gather_hazards, "community": gather_community, "historical": gather_historical,
          "driver_behaviour": gather_driver_behaviour, "collision": gather_collision}
