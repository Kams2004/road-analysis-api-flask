"""
/reports — generate, list, download and delete PDF reports (Module 14).

Generating needs `reports:add` plus the view right of the module the report
reads (hazards → detections, community → signalements, historical,
driver_behaviour, collision → collisions). Files are stored in the private
MinIO bucket `reports` and only served through GET /reports/{id}/download.
"""
import asyncio
import logging
from datetime import date, datetime
from types import SimpleNamespace
from typing import Any, Dict, List, Literal, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, get_principal
from app.api.openapi import errors, legacy
from app.core import settings_service
from app.core.config import settings
from app.core.db.session import get_db
from app.modules.m09_driver_behaviour.analytics import resolve_window
from app.modules.m09_driver_behaviour.settings import BEHAVIOUR_TYPES
from app.modules.m14_reports import collectors, pathfilter, pdf
from app.modules.m14_reports.i18n import translator
from app.modules.m14_reports.models.report import GeneratedReport

logger = logging.getLogger(__name__)
router = APIRouter()

ReportType = Literal["hazards", "community", "historical", "driver_behaviour", "collision"]


# ── Schemas ──────────────────────────────────────────────────────────────────

class PlaceIn(BaseModel):
    """A place of a from → to path."""
    lat: float = Field(..., ge=-90, le=90, description="Latitude (degrees).", examples=[4.0511])
    lon: float = Field(..., ge=-180, le=180, description="Longitude (degrees).", examples=[9.7679])
    label: str = Field("", max_length=160, description="Name shown in the report.", examples=["Douala"])


class PathIn(BaseModel):
    """Path filter: one of a predefined road section, a from → to pair (driving route) or a polyline."""
    section_id: Optional[str] = Field(None, description="Road section id (settings `routes`).",
                                      examples=["douala-bafoussam-n5"])
    from_: Optional[PlaceIn] = Field(None, alias="from", description="Start place (with `to`).")
    to: Optional[PlaceIn] = Field(None, description="Destination place (with `from`).")
    waypoints: Optional[List[Tuple[float, float]]] = Field(None, description=(
        "Explicit polyline `[[lat, lon], …]` (at least 2 points) — no routing."))
    name: Optional[str] = Field(None, max_length=160, description="Name shown in the report.")

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _one(self):
        if not (self.section_id or (self.from_ and self.to) or self.waypoints):
            raise ValueError("path: give section_id, from + to, or waypoints")
        return self

    def spec(self) -> dict:
        d: Dict[str, Any] = {"name": self.name}
        if self.waypoints:
            d["waypoints"] = [list(p) for p in self.waypoints]
        elif self.section_id:
            d["section_id"] = self.section_id
        else:
            d["from"], d["to"] = self.from_.model_dump(), self.to.model_dump()
        return d


class ReportIn(BaseModel):
    """What to put in the report."""
    report_type: ReportType = Field(..., description=(
        "`hazards` (AI hazards), `community` (community reports), `historical` (historical risk), "
        "`driver_behaviour` or `collision` (collision prevention)."), examples=["hazards"])
    title: Optional[str] = Field(None, max_length=160, description="Custom title (default: the report type).")
    period: Optional[str] = Field("all", description="`7d`, `30d`, `90d`, `12m` or `all`. Ignored with dates.")
    date_from: Optional[date] = Field(None, description="First day (inclusive, UTC).")
    date_to: Optional[date] = Field(None, description="Last day (inclusive, UTC).")
    types: List[str] = Field([], description=(
        "Only these types (empty = all): hazard types (`pothole`, `parked_vehicle`, `traffic_sign`, "
        "`speed_bump` …), community report types, historical event types or behaviour types — see "
        "`GET /reports/types`."), examples=[["pothole"]])
    statuses: List[str] = Field([], description=(
        "Hazards: `active` / `resolved`; community reports: `actif` / `annule` / `rejete` (empty = all)."))
    zone_id: Optional[int] = Field(None, description="Only inside this cluster zone (`GET /reports/options`).")
    town: Optional[str] = Field(None, max_length=120, description=(
        "Only hazards / community reports whose location mentions this town or locality."))
    path: Optional[PathIn] = Field(None, description="Only items within `corridor_m` of this path, in path order.")
    corridor_m: Optional[float] = Field(None, ge=5, le=5000, description=(
        "Corridor half-width around the path, metres (default: settings `mobile.route_corridor_m`)."))
    include_images: bool = Field(True, description="Embed image previews (whole picture, never cropped).")
    max_images: int = Field(60, ge=0, le=300, description="At most this many previews.")
    language: Optional[Literal["en", "fr", "es"]] = Field(None, description="Report language (default: yours).")


class ReportOut(BaseModel):
    """A generated report."""
    id: str = Field(..., description="Report id.")
    report_type: str = Field(..., description="Report type.")
    title: str = Field(..., description="Title.")
    filters: Dict[str, Any] = Field(..., description="Filters as requested (plus the resolved path).")
    summary: Dict[str, Any] = Field(..., description="Item counts.")
    status: str = Field(..., description="`ready` or `failed`.")
    error: Optional[str] = Field(None, description="Why it failed.")
    size_bytes: Optional[int] = Field(None, description="PDF size.")
    page_count: Optional[int] = Field(None, description="PDF pages.")
    methodology: Dict[str, Any] = Field(..., description="Methodology / versions at generation time (RRAP-13-013).")
    created_by: Optional[str] = Field(None, description="Who generated it.")
    created_at: datetime = Field(..., description="When (UTC).")
    download_url: str = Field(..., description="Relative URL of the PDF.")

    @classmethod
    def of(cls, r: GeneratedReport) -> "ReportOut":
        return cls(id=r.id, report_type=r.report_type, title=r.title, filters=r.filters or {},
                   summary=r.summary or {}, status=r.status, error=r.error, size_bytes=r.size_bytes,
                   page_count=r.page_count, methodology=r.methodology or {}, created_by=r.created_by,
                   created_at=r.created_at, download_url=f"/reports/{r.id}/download")


class ReportListOut(BaseModel):
    """Generated reports, newest first."""
    total: int = Field(..., description="Reports matching.")
    items: List[ReportOut] = Field(..., description="This page.")


class TypeOption(BaseModel):
    """One selectable type."""
    value: str = Field(..., description="Value to send in `types`.")
    count: Optional[int] = Field(None, description="Items of this type currently stored.")


class ReportTypeOut(BaseModel):
    """A report type and its filter options."""
    key: str = Field(..., description="Report type.")
    module: str = Field(..., description="Permission module whose view right generating needs.")
    allowed: bool = Field(..., description="The caller may generate it.")
    types: List[TypeOption] = Field(..., description="Values for the `types` filter.")
    statuses: List[str] = Field(..., description="Values for the `statuses` filter.")
    supports_path: bool = Field(..., description="The path filter applies.")
    supports_town: bool = Field(..., description="The town filter applies.")
    total: Optional[int] = Field(None, description="Items currently stored.")


class SectionOut(BaseModel):
    """A predefined road section."""
    id: str = Field(..., description="Section id.")
    name: str = Field(..., description="Name.")


class ZoneOptOut(BaseModel):
    """A cluster zone usable as area filter."""
    id: int = Field(..., description="Zone id.")
    name: str = Field(..., description="Name.")
    kind: str = Field(..., description="`agglomeration`, `road_section` or `custom`.")


class OptionsOut(BaseModel):
    """Values for the report filters."""
    sections: List[SectionOut] = Field(..., description="Road sections (settings `routes`).")
    zones: List[ZoneOptOut] = Field(..., description="Active cluster zones.")
    towns: List[str] = Field(..., description="Towns found on hazards.")
    default_corridor_m: float = Field(..., description="Default corridor (settings `mobile.route_corridor_m`).")


# ── helpers ──────────────────────────────────────────────────────────────────

def _can(principal: Principal, module: str, action: str = "view") -> bool:
    if principal.kind != "user":
        return not settings.AUTH_ENFORCED
    return principal.can(module, action)


def _lang(principal: Principal, wanted: Optional[str]) -> str:
    if wanted:
        return wanted
    lang = getattr(getattr(principal, "user", None), "language", None)
    return getattr(lang, "value", lang) or "en"


async def _type_options(db: AsyncSession) -> Dict[str, Tuple[List[TypeOption], Optional[int]]]:
    from app.modules.m06_hazards.models.hazard import PUBLIC_STATUSES, Hazard
    from app.modules.m08_historical.domain import HistoricalEventType
    from app.modules.m08_historical.models.historical_event import HistoricalEventRecord as E
    from app.modules.m10_collision.models.vehicle import Vehicle
    from app.modules.m11_community.models.signalement import Signalement, SignalementType

    async def counts(stmt) -> Dict[str, int]:
        return {getattr(k, "value", k): n for k, n in (await db.execute(stmt)).all()}

    hz = await counts(select(Hazard.type, func.count()).where(Hazard.status.in_(PUBLIC_STATUSES)).group_by(Hazard.type))
    known_hz = ["pothole", "speed_bump", "traffic_sign", "parked_vehicle"]
    sg = await counts(select(Signalement.type, func.count()).where(Signalement.merged_into_id.is_(None))
                      .group_by(Signalement.type))
    ev = await counts(select(E.event_type, func.count()).group_by(E.event_type))
    vehicles = (await db.execute(select(func.count()).select_from(Vehicle))).scalar_one()
    opt = lambda values, c: [TypeOption(value=v, count=c.get(v, 0)) for v in values]
    return {
        "hazards": (opt(list(dict.fromkeys(known_hz + sorted(hz))), hz), sum(hz.values())),
        "community": (opt([t.value for t in SignalementType], sg), sum(sg.values())),
        "historical": (opt([t.value for t in HistoricalEventType], ev), sum(ev.values())),
        "driver_behaviour": (opt(list(BEHAVIOUR_TYPES), ev), sum(ev.get(t, 0) for t in BEHAVIOUR_TYPES)),
        "collision": ([], vehicles),
    }


_STATUSES = {"hazards": ["active", "resolved"], "community": ["actif", "annule", "rejete"]}


async def _get(db: AsyncSession, report_id: str) -> GeneratedReport:
    row = await db.get(GeneratedReport, report_id)
    if row is None:
        raise HTTPException(404, "Report not found.")
    return row


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/types", response_model=List[ReportTypeOut], summary="List report types and their filter options")
async def report_types(db: AsyncSession = Depends(get_db), principal: Principal = Depends(get_principal)):
    """The five report types, the values of their `types` / `statuses` filters (with stored counts) and whether the caller may generate each."""
    opts = await _type_options(db)
    return [ReportTypeOut(key=k, module=collectors.DATA_MODULE[k], allowed=_can(principal, collectors.DATA_MODULE[k]),
                          types=opts[k][0], statuses=_STATUSES.get(k, []), total=opts[k][1],
                          supports_path=k != "collision", supports_town=k in ("hazards", "community"))
            for k in collectors.REPORT_TYPES]


@router.get("/options", response_model=OptionsOut, summary="Get the values of the report filters")
async def report_options(db: AsyncSession = Depends(get_db)):
    """Road sections, cluster zones and towns for the area / path filters, and the default corridor width."""
    from app.modules.m06_hazards.models.cluster_zone import ClusterZone
    from app.modules.m06_hazards.models.hazard import Hazard
    routes = await settings_service.get_cached(db, "routes")
    mobile = await settings_service.get_cached(db, "mobile")
    zones = (await db.execute(select(ClusterZone.id, ClusterZone.name, ClusterZone.kind)
                              .where(ClusterZone.is_active.is_(True)).order_by(ClusterZone.name))).all()
    towns = (await db.execute(select(distinct(Hazard.town)).where(Hazard.town.isnot(None))
                              .order_by(Hazard.town).limit(300))).scalars().all()
    return OptionsOut(sections=[SectionOut(id=s.id, name=s.name) for s in routes.sections],
                      zones=[ZoneOptOut(id=i, name=n, kind=k) for i, n, k in zones],
                      towns=[t for t in towns if t], default_corridor_m=float(mobile.route_corridor_m))


@router.post("/", response_model=ReportOut, status_code=201, summary="Generate a PDF report",
             responses=errors(
                 legacy(403, "You need the detections view right to generate this report.",
                        summary="data module not allowed"),
                 legacy(404, "Cluster zone 12 not found.", summary="unknown zone"),
                 legacy(422, "Unknown road section 'x'.", summary="unknown section"),
                 legacy(422, "Unknown period '5d' — expected 7d, 30d, 90d, 12m or all", summary="unknown period"),
                 legacy(502, "Routing service unavailable: connection refused", summary="no route"),
                 legacy(502, "Report storage unavailable: …", summary="storage down")))
async def generate_report(body: ReportIn, db: AsyncSession = Depends(get_db),
                          principal: Principal = Depends(get_principal)):
    """
    Builds the PDF server-side and stores it (MinIO bucket `reports`) for later download. Filters:
    `types` (multi), `statuses`, period, cluster zone / town, and a **path** — a road section, a
    from → to pair (driving route through OSRM) or a polyline — keeping only items within
    `corridor_m` of it, listed in path order with their distance along the path (km) and drawn on a
    schematic map. Images are previews of the whole picture (never cropped) linking to the full image
    in the web console.
    """
    module = collectors.DATA_MODULE[body.report_type]
    if not _can(principal, module):
        raise HTTPException(403, f"You need the {module} view right to generate this report.")
    lang = _lang(principal, body.language)
    t = translator(lang)
    now = datetime.utcnow()
    try:
        window = resolve_window(body.period, body.date_from, body.date_to, now, "all")
    except ValueError as e:
        raise HTTPException(422, str(e))

    zone = None
    if body.zone_id is not None:
        from app.modules.m06_hazards.clustering.zones import compile_shape
        from app.modules.m06_hazards.models.cluster_zone import ClusterZone
        z = await db.get(ClusterZone, body.zone_id)
        if z is None:
            raise HTTPException(404, f"Cluster zone {body.zone_id} not found.")
        zone = SimpleNamespace(id=z.id, name=z.name, shape=compile_shape(z.geometry, z.corridor_width_m))

    path = None
    corridor = body.corridor_m
    if body.path is not None and body.report_type != "collision":
        if corridor is None:
            corridor = float((await settings_service.get_cached(db, "mobile")).route_corridor_m)
        routes = await settings_service.get_cached(db, "routes")
        try:
            path = await pathfilter.resolve(body.path.spec(), corridor, routes.sections)
        except pathfilter.PathError as e:
            msg = str(e)
            raise HTTPException(502 if msg.startswith("Routing service") else 422, msg)

    actor = principal.label if principal.kind == "user" else None
    ctx = collectors.Ctx(report_type=body.report_type, t=t, window=window, types=list(body.types),
                         statuses=list(body.statuses), zone=zone, town=(body.town or "").strip() or None,
                         path=path, include_images=body.include_images, max_images=body.max_images,
                         web_base=settings.WEB_BASE_URL, lang=lang, generated_by=actor, now=now)
    data = await collectors.GATHER[body.report_type](db, ctx)
    title = body.title or (data["title"] + (f" - {path.name}" if path else ""))
    data["title"] = title

    row = GeneratedReport(report_type=body.report_type, title=title, created_by=actor, created_at=now)
    db.add(row)
    await db.flush()
    pdf_bytes = await asyncio.to_thread(pdf.render, data, t=t, report_id=row.id, generated_at=now,
                                        generated_by=actor)
    filters = body.model_dump(mode="json", by_alias=True, exclude={"include_images", "max_images"})
    filters["window"] = {"start": window.start.isoformat() if window.start else None, "end": window.end.isoformat()}
    if path is not None:
        filters["resolved_path"] = path.describe()
    if zone is not None:
        filters["zone_name"] = zone.name
    from app.modules.m09_driver_behaviour.settings import METHODOLOGY_VERSION
    row.filters = filters
    row.summary = data.get("summary", {})
    row.methodology = {"report_version": "rep-1", "driver_behaviour": METHODOLOGY_VERSION,
                       "historical_saturation_weight": settings.HISTORICAL_RISK_SATURATION_WEIGHT,
                       "historical_cell_size_m": settings.HISTORICAL_GRID_CELL_SIZE_M,
                       "notes": data.get("methodology", [])}
    row.size_bytes = len(pdf_bytes)
    row.page_count = pdf.page_count(pdf_bytes)
    row.object_name = f"{now:%Y/%m}/{row.id}.pdf"
    from app.core.storage.minio_service import upload_report
    try:
        await asyncio.to_thread(upload_report, pdf_bytes, row.object_name)
    except Exception as e:
        await db.rollback()
        logger.exception("report upload failed")
        raise HTTPException(502, f"Report storage unavailable: {e}")
    row.status = "ready"
    await db.commit()
    await db.refresh(row)
    return ReportOut.of(row)


@router.get("/", response_model=ReportListOut, summary="List generated reports")
async def list_reports(report_type: Optional[str] = Query(None, description="Only this report type."),
                       offset: int = Query(0, ge=0, description="Rows to skip."),
                       limit: int = Query(25, ge=1, le=200, description="Page size."),
                       db: AsyncSession = Depends(get_db)):
    """History of generated reports, newest first."""
    q = select(GeneratedReport)
    cq = select(func.count()).select_from(GeneratedReport)
    if report_type:
        q = q.where(GeneratedReport.report_type == report_type)
        cq = cq.where(GeneratedReport.report_type == report_type)
    total = (await db.execute(cq)).scalar_one()
    rows = (await db.execute(q.order_by(GeneratedReport.created_at.desc()).offset(offset).limit(limit))).scalars().all()
    return ReportListOut(total=total, items=[ReportOut.of(r) for r in rows])


@router.get("/{report_id}", response_model=ReportOut, summary="Get a generated report",
            responses=errors(legacy(404, "Report not found.")))
async def get_report(report_id: str, db: AsyncSession = Depends(get_db)):
    """Metadata of one report (filters, counts, methodology)."""
    return ReportOut.of(await _get(db, report_id))


@router.get("/{report_id}/download", summary="Download a report PDF", response_class=Response,
            responses={200: {"content": {"application/pdf": {}}, "description": "The PDF file."},
                       **errors(legacy(404, "Report not found."),
                                legacy(502, "Report storage unavailable: …", summary="storage down"))})
async def download_report(report_id: str, db: AsyncSession = Depends(get_db)):
    """The PDF file (`Content-Disposition: attachment`)."""
    row = await _get(db, report_id)
    if row.status != "ready" or not row.object_name:
        raise HTTPException(404, "Report not found.")
    from app.core.storage.minio_service import get_report as read_pdf
    try:
        data = await asyncio.to_thread(read_pdf, row.object_name)
    except Exception as e:
        raise HTTPException(502, f"Report storage unavailable: {e}")
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in row.title.lower())[:60].strip("-") or "report"
    return Response(content=data, media_type="application/pdf", headers={
        "Content-Disposition": f'attachment; filename="{safe}-{row.created_at:%Y%m%d-%H%M}.pdf"',
        "Cache-Control": "private, no-store"})


@router.delete("/{report_id}", status_code=204, summary="Delete a generated report",
               responses=errors(legacy(404, "Report not found.")))
async def delete_report(report_id: str, db: AsyncSession = Depends(get_db)):
    """Removes the report and its stored PDF."""
    row = await _get(db, report_id)
    if row.object_name:
        from app.core.storage.minio_service import remove_report
        await asyncio.to_thread(remove_report, row.object_name)
    await db.delete(row)
    await db.commit()
    return Response(status_code=204)
