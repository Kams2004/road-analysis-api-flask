"""
PDF rendering of a report document (see collectors.py for the model) with
reportlab: header / footer on every page, filters and KPIs, charts
(reportlab.graphics), a schematic map of the path and the items, and tables
whose image cells show the WHOLE picture scaled down (aspect preserved, never
cropped) with a clickable link to the full image in the web console.
"""
from __future__ import annotations

import io
import math
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple
from xml.sax.saxutils import escape

from reportlab.graphics.charts.barcharts import HorizontalBarChart, VerticalBarChart
from reportlab.graphics.charts.legends import Legend
from reportlab.graphics.shapes import Circle, Drawing, Line, PolyLine, Rect, String
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (Flowable, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

from app.modules.m03_maps.osrm import geo

BRAND = colors.HexColor("#1d4ed8")
INK = colors.HexColor("#0f172a")
MUTED = colors.HexColor("#64748b")
GRID = colors.HexColor("#e2e8f0")
SOFT = colors.HexColor("#f1f5f9")

PALETTE = ["#2563eb", "#dc2626", "#f59e0b", "#059669", "#7c3aed", "#db2777", "#0891b2", "#65a30d",
           "#ea580c", "#475569"]
KEY_COLOURS = {
    "pothole": "#dc2626", "speed_bump": "#f59e0b", "traffic_sign": "#2563eb", "parked_vehicle": "#7c3aed",
    "SPEEDING": "#dc2626", "HARD_BRAKING": "#f59e0b", "HARD_ACCELERATION": "#7c3aed", "HARSH_CORNERING": "#0891b2",
    "high": "#dc2626", "medium": "#f59e0b", "low": "#059669",
}

_CHAR_FIX = {"→": "->", "←": "<-", "≥": ">=", "≤": "<=", "…": "...", "’": "'", "‘": "'", "“": '"', "”": '"',
             "−": "-", "✓": "v", "•": "-", "·": "-", "–": "-", "—": "-"}


def clean(text: Any) -> str:
    """Text safe for the built-in Helvetica font (Latin-1 / cp1252)."""
    s = "" if text is None else str(text)
    for a, b in _CHAR_FIX.items():
        s = s.replace(a, b)
    return s.encode("cp1252", "replace").decode("cp1252")


def para_text(text: Any) -> str:
    return escape(clean(text)).replace("\n", "<br/>")


def _colour(key: str, i: int = 0):
    return colors.HexColor(KEY_COLOURS.get(key, PALETTE[i % len(PALETTE)]))


# ── styles ───────────────────────────────────────────────────────────────────

_ss = getSampleStyleSheet()
S_TITLE = ParagraphStyle("t", parent=_ss["Title"], fontName="Helvetica-Bold", fontSize=18, leading=22,
                         textColor=INK, alignment=TA_LEFT, spaceAfter=2)
S_SUB = ParagraphStyle("s", parent=_ss["Normal"], fontSize=9, textColor=MUTED, leading=12)
S_H2 = ParagraphStyle("h2", parent=_ss["Heading2"], fontName="Helvetica-Bold", fontSize=12.5, leading=16,
                      textColor=BRAND, spaceBefore=10, spaceAfter=5, keepWithNext=1)
S_BODY = ParagraphStyle("b", parent=_ss["Normal"], fontSize=9, leading=12, textColor=INK)
S_SMALL = ParagraphStyle("sm", parent=S_BODY, fontSize=7.5, leading=9.5, textColor=MUTED)
S_NOTE = ParagraphStyle("note", parent=S_SMALL, keepWithNext=1)
S_CELL = ParagraphStyle("c", parent=S_BODY, fontSize=7.5, leading=9.2)
S_HEAD = ParagraphStyle("hd", parent=S_CELL, fontName="Helvetica-Bold", textColor=colors.white)
S_KPI_V = ParagraphStyle("kv", parent=S_BODY, fontName="Helvetica-Bold", fontSize=13, leading=15, textColor=INK)
S_KPI_L = ParagraphStyle("kl", parent=S_BODY, fontSize=7, leading=8.5, textColor=MUTED)

PAGE_W, PAGE_H = A4
MARGIN = 14 * mm
CONTENT_W = PAGE_W - 2 * MARGIN


def link_para(text: str, url: str, style=S_CELL) -> Paragraph:
    return Paragraph(f'<link href="{escape(url, {chr(34): "&quot;"})}" color="#1d4ed8"><u>{para_text(text)}</u></link>',
                     style)


# ── image cell ───────────────────────────────────────────────────────────────

def preview_jpeg(data: bytes, max_px: int = 480) -> Optional[Tuple[bytes, int, int]]:
    """Whole image scaled down to fit max_px (aspect preserved, no crop) → (jpeg, w, h)."""
    try:
        from PIL import Image as PILImage
        im = PILImage.open(io.BytesIO(data))
        im = im.convert("RGB")
        im.thumbnail((max_px, max_px))
        out = io.BytesIO()
        im.save(out, "JPEG", quality=72, optimize=True)
        return out.getvalue(), im.width, im.height
    except Exception:
        return None


class LinkedImage(Flowable):
    """An image fitted inside (max_w × max_h), letterboxed, clickable."""

    def __init__(self, jpeg: bytes, px_w: int, px_h: int, max_w: float, max_h: float, url: Optional[str]):
        super().__init__()
        scale = min(max_w / px_w, max_h / px_h)
        self.w, self.h = px_w * scale, px_h * scale
        self.max_w, self.max_h = max_w, max_h
        self.jpeg, self.url = jpeg, url

    def wrap(self, aw, ah):
        return self.max_w, self.max_h

    def draw(self):
        from reportlab.lib.utils import ImageReader
        x = (self.max_w - self.w) / 2
        y = (self.max_h - self.h) / 2
        self.canv.setFillColor(SOFT)
        self.canv.rect(0, 0, self.max_w, self.max_h, stroke=0, fill=1)
        self.canv.drawImage(ImageReader(io.BytesIO(self.jpeg)), x, y, self.w, self.h)
        if self.url:
            self.canv.linkURL(self.url, (x, y, x + self.w, y + self.h), relative=1)


def image_cell(cell: dict, width: float) -> List[Flowable]:
    out: List[Flowable] = []
    data = cell.get("image")
    if data:
        prev = preview_jpeg(data)
        if prev:
            out.append(LinkedImage(prev[0], prev[1], prev[2], width - 2 * mm, 22 * mm, cell.get("link")))
    if cell.get("link"):
        out.append(link_para(cell.get("label") or "Open", cell["link"], S_SMALL))
    return out or [Paragraph("-", S_CELL)]


# ── tables ───────────────────────────────────────────────────────────────────

def make_table(columns: Sequence[Tuple[str, float]], rows: Sequence[Sequence[Any]]) -> Table:
    total = sum(w for _, w in columns)
    widths = [w / total * CONTENT_W for _, w in columns]
    data = [[Paragraph(para_text(c), S_HEAD) for c, _ in columns]]
    for r in rows:
        line = []
        for cell, w in zip(r, widths):
            if isinstance(cell, dict) and "image" in cell:
                line.append(image_cell(cell, w))
            elif isinstance(cell, dict) and cell.get("link"):
                line.append(link_para(cell.get("text", ""), cell["link"]))
            else:
                line.append(Paragraph(para_text(cell), S_CELL))
        data.append(line)
    tbl = Table(data, colWidths=widths, repeatRows=1)
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), BRAND),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, SOFT]),
        ("LINEBELOW", (0, 0), (-1, -1), 0.25, GRID),
        ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
    ]))
    return tbl


def kpi_row(kpis: Sequence[Tuple[str, str]]) -> Table:
    per_row = 4 if len(kpis) > 4 else max(len(kpis), 1)
    cells = [[Paragraph(para_text(v), S_KPI_V), Paragraph(para_text(l), S_KPI_L)] for l, v in kpis]
    grid = [cells[i:i + per_row] for i in range(0, len(cells), per_row)]
    for g in grid:
        g += [""] * (per_row - len(g))
    tbl = Table(grid, colWidths=[CONTENT_W / per_row] * per_row)
    tbl.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.5, GRID), ("INNERGRID", (0, 0), (-1, -1), 0.5, GRID),
        ("BACKGROUND", (0, 0), (-1, -1), SOFT), ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return tbl


def filter_table(filters: Sequence[Tuple[str, str]]) -> Table:
    data = [[Paragraph(f"<b>{para_text(k)}</b>", S_CELL), Paragraph(para_text(v), S_CELL)] for k, v in filters]
    tbl = Table(data, colWidths=[38 * mm, CONTENT_W - 38 * mm])
    tbl.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.25, GRID), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                             ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]))
    return tbl


# ── charts ───────────────────────────────────────────────────────────────────

def chart(spec: dict) -> Drawing:
    cats = [clean(c)[:22] for c in spec["categories"]]
    series = spec["series"]
    horizontal = spec.get("horizontal", False)
    height = (max(40 * mm, (len(cats) * 7 + 18) * mm / 1.6)) if horizontal else 62 * mm
    d = Drawing(CONTENT_W, height)
    d.add(String(0, height - 10, clean(spec.get("title", "")), fontName="Helvetica-Bold", fontSize=9,
                 fillColor=INK))
    c = HorizontalBarChart() if horizontal else VerticalBarChart()
    legend_w = 0 if len(series) == 1 else 38 * mm
    c.x, c.y = (45 * mm if horizontal else 12 * mm), (6 * mm if horizontal else 14 * mm)
    c.width = CONTENT_W - c.x - legend_w - 4 * mm
    c.height = height - c.y - 16
    c.data = [list(s["values"]) or [0] for s in series]
    c.categoryAxis.categoryNames = cats
    c.categoryAxis.labels.fontSize = 6.5
    c.categoryAxis.labels.fontName = "Helvetica"
    c.valueAxis.labels.fontSize = 6.5
    c.valueAxis.valueMin = 0
    vmax = max([max(v) if v else 0 for v in c.data] + [1])
    if spec.get("stacked"):
        vmax = max([sum(col) for col in zip(*c.data)] + [1])
        c.categoryAxis.style = "stacked"
    c.valueAxis.valueMax = vmax * 1.1 if vmax > 4 else 5
    c.valueAxis.visibleGrid = True
    c.valueAxis.gridStrokeColor = GRID
    c.barSpacing = 1
    c.groupSpacing = 4
    if not horizontal and len(cats) > 14:
        c.categoryAxis.labels.angle = 60
        c.categoryAxis.labels.boxAnchor = "ne"
        c.categoryAxis.labels.fontSize = 5.5
        step = max(1, len(cats) // 24)
        c.categoryAxis.categoryNames = [n if i % step == 0 else "" for i, n in enumerate(cats)]
    for i, s in enumerate(series):
        c.bars[i].fillColor = _colour(s.get("key", ""), i)
        c.bars[i].strokeColor = None
    d.add(c)
    if len(series) > 1:
        lg = Legend()
        lg.x, lg.y = CONTENT_W - legend_w + 2 * mm, height - 18
        lg.fontSize, lg.fontName = 7, "Helvetica"
        lg.alignment = "right"
        lg.columnMaximum = 8
        lg.colorNamePairs = [(_colour(s.get("key", ""), i), clean(s["name"])[:24]) for i, s in enumerate(series)]
        d.add(lg)
    return d


# ── schematic map ────────────────────────────────────────────────────────────

def _nice_step(length_km: float) -> float:
    for s in (0.5, 1, 2, 5, 10, 20, 25, 50, 100, 200):
        if length_km / s <= 10:
            return s
    return 500


def map_drawing(spec: dict, t, height: float = 95 * mm) -> Optional[Drawing]:
    path = spec.get("path") or []
    pts = spec.get("points") or []
    coords = list(path) + [(p[0], p[1]) for p in pts]
    if not coords:
        return None
    lats = [c[0] for c in coords]
    lons = [c[1] for c in coords]
    lat0, lat1, lon0, lon1 = min(lats), max(lats), min(lons), max(lons)
    k = math.cos(math.radians((lat0 + lat1) / 2))
    span_x = max((lon1 - lon0) * k, 1e-4)
    span_y = max(lat1 - lat0, 1e-4)
    pad = 10 * mm
    legend_h = 10 * mm
    w, h = CONTENT_W, height
    scale = min((w - 2 * pad) / span_x, (h - 2 * pad - legend_h) / span_y)
    ox = pad + ((w - 2 * pad) - span_x * scale) / 2
    oy = legend_h + pad + ((h - 2 * pad - legend_h) - span_y * scale) / 2

    def xy(lat, lon):
        return ox + (lon - lon0) * k * scale, oy + (lat - lat0) * scale

    d = Drawing(w, h)
    d.add(Rect(0, 0, w, h, fillColor=colors.HexColor("#f8fafc"), strokeColor=GRID, strokeWidth=0.6))
    if len(path) >= 2:
        flat = []
        step = max(1, len(path) // 1500)
        for lat, lon in path[::step] + [path[-1]]:
            flat += list(xy(lat, lon))
        d.add(PolyLine(flat, strokeColor=colors.HexColor("#93c5fd"), strokeWidth=5))
        d.add(PolyLine(flat, strokeColor=BRAND, strokeWidth=1.8))
        cum = geo.cumulative_distances_m(path)
        total_km = cum[-1] / 1000
        km_step = _nice_step(total_km)
        km = km_step
        while km < total_km:
            lat, lon = geo.point_at_distance(path, cum, km * 1000)
            x, y = xy(lat, lon)
            d.add(Circle(x, y, 2.2, fillColor=colors.white, strokeColor=BRAND, strokeWidth=0.8))
            d.add(String(x + 3, y + 3, f"{km:g} km", fontSize=6, fillColor=MUTED, fontName="Helvetica"))
            km += km_step
        for (lat, lon), lab in ((path[0], "A"), (path[-1], "B")):
            x, y = xy(lat, lon)
            d.add(Circle(x, y, 5, fillColor=INK, strokeColor=colors.white, strokeWidth=1))
            d.add(String(x - 2.6, y - 2.6, lab, fontSize=7, fillColor=colors.white, fontName="Helvetica-Bold"))
    keys = {}
    for lat, lon, n, key in pts:
        col = keys.setdefault(key, _colour(key, len(keys)))
        x, y = xy(lat, lon)
        d.add(Circle(x, y, 3.4, fillColor=col, strokeColor=colors.white, strokeWidth=0.7))
        if len(pts) <= 80:
            d.add(String(x + 4, y - 2, str(n), fontSize=5.5, fillColor=INK, fontName="Helvetica"))
    # legend + scale
    x = 4 * mm
    for label, key in spec.get("legend", []):
        col = keys.get(key, _colour(key))
        d.add(Circle(x + 2, 4.5 * mm, 2.6, fillColor=col, strokeColor=None))
        txt = clean(label)[:26]
        d.add(String(x + 6, 3.5 * mm, txt, fontSize=6.5, fillColor=INK, fontName="Helvetica"))
        x += (len(txt) * 3.3 + 14)
    km_w = (w - 2 * pad) / scale * 111.32 if scale else 0
    bar_km = _nice_step(km_w) if km_w else 0
    if bar_km:
        bar_px = bar_km / 111.32 * scale
        bx = w - pad - bar_px
        d.add(Line(bx, 4 * mm, bx + bar_px, 4 * mm, strokeColor=INK, strokeWidth=1))
        d.add(String(bx, 5.2 * mm, f"{bar_km:g} km", fontSize=6, fillColor=INK, fontName="Helvetica"))
    return d


# ── document ─────────────────────────────────────────────────────────────────

def render(doc_model: dict, *, t, report_id: str, generated_at: datetime, generated_by: Optional[str]) -> bytes:
    buf = io.BytesIO()
    title = clean(doc_model["title"])
    stamp = f"{t('generated')} {generated_at:%Y-%m-%d %H:%M} UTC" + (f" {t('by')} {generated_by}" if generated_by else "")

    def on_page(canv, doc):
        canv.saveState()
        canv.setFillColor(BRAND)
        canv.rect(0, PAGE_H - 11 * mm, PAGE_W, 11 * mm, stroke=0, fill=1)
        canv.setFillColor(colors.white)
        canv.setFont("Helvetica-Bold", 9)
        canv.drawString(MARGIN, PAGE_H - 7 * mm, "VIASafe")
        canv.setFont("Helvetica", 8)
        canv.drawRightString(PAGE_W - MARGIN, PAGE_H - 7 * mm, title)
        canv.setFillColor(MUTED)
        canv.setFont("Helvetica", 7)
        canv.drawString(MARGIN, 8 * mm, clean(f"{t('report_id')} {report_id[:8]} - {stamp}"))
        canv.drawRightString(PAGE_W - MARGIN, 8 * mm, f"{t('page')} {doc.page}")
        canv.restoreState()

    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=MARGIN, rightMargin=MARGIN, topMargin=17 * mm,
                            bottomMargin=15 * mm, title=title, author="VIASafe", subject=t("app"),
                            creator="VIASafe Road Risk Analysis Platform")
    story: List[Flowable] = [
        Paragraph(para_text(title), S_TITLE),
        Paragraph(para_text(f"{t('app')} - {stamp}"), S_SUB),
        Spacer(1, 4 * mm),
        Paragraph(para_text(t("filters")), S_H2),
        filter_table(doc_model.get("filters", [])),
    ]
    if doc_model.get("kpis"):
        story += [Paragraph(para_text(t("summary")), S_H2), kpi_row(doc_model["kpis"])]
    m = doc_model.get("map")
    if m:
        drawing = map_drawing(m, t)
        if drawing is not None:
            story += [KeepTogether([Paragraph(para_text(t("map")), S_H2), drawing,
                                    Paragraph(para_text(t("map_path_note") if m.get("path") else t("map_note")),
                                              S_SMALL)])]
    for b in doc_model.get("blocks", []):
        kind = b["kind"]
        if kind == "heading":
            story.append(Paragraph(para_text(b["text"]), S_H2))
        elif kind == "para":
            story.append(Paragraph(para_text(b["text"]), S_NOTE if b.get("small") else S_BODY))
        elif kind == "chart":
            story += [Spacer(1, 2 * mm), chart(b["chart"]), Spacer(1, 2 * mm)]
        elif kind == "table":
            story.append(make_table(b["columns"], b["rows"]))
            story.append(Spacer(1, 3 * mm))
    if doc_model.get("methodology"):
        story.append(Paragraph(para_text(t("methodology")), S_H2))
        for line in doc_model["methodology"]:
            story.append(Paragraph(para_text(f"- {line}"), S_SMALL))
    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    return buf.getvalue()


def page_count(pdf: bytes) -> int:
    return len(re.findall(rb"/Type\s*/Page(?![s\w])", pdf))
