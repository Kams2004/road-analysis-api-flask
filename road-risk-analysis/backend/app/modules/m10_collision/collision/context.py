"""
Context Risk Index — pure functions, no I/O (the Redis / Postgres plumbing is in
app/modules/m10_collision/context_service.py, the settings in context_settings.py).

Two halves:

  Precomputation (every few minutes, beat task) — per ~150 m grid cell
  (app.modules.m08_historical.grid, the cell size of the historical events):
    build_history        recency-faded, severity-weighted fleet exceptions per cell
                         (HARD_BRAKING / HARSH_CORNERING count double) and habitual speeding
    infer_local_limits   the local speed limit: median `speed_limit_mps` of the SPEEDING
                         events of the cell, of the cell and its 8 neighbours when the cell
                         alone has fewer than speed_limit_min_events
    spread_points        validated hazards / visible community reports copied into every cell
                         their radius can reach, with their position (exact distance at lookup)
    build_place_cells    → {cell_id: PlaceCell}

  Hot path (per assessed pair, no database):
    compute_context(place cell at the conflict point, the two parties, now)
      → ContextResult(index C ∈ [0, 1], factor values, reasons)
  each factor ∈ [0, 1], C = min(1, Σ weight × factor); reasons carry a stable `code`
  (HISTORY_HARSH_BRAKING, VEHICLE_SPEEDING, …), an English `label` and the numbers behind it.
  How C changes the alert level is detector.apply_context.
"""
import bisect
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Tuple

from pydantic import BaseModel, Field

from app.modules.m08_historical.grid import grid_cell_center, grid_cell_id, grid_cell_ids_in_radius
from app.modules.m03_maps.osrm import geo
from app.modules.m10_collision.collision.context_settings import ContextSettings

LatLon = Tuple[float, float]

HISTORY_CODES = {
    "HARD_BRAKING": ("HISTORY_HARSH_BRAKING", "Frequent harsh braking at this place"),
    "HARSH_CORNERING": ("HISTORY_HARSH_CORNERING", "Frequent harsh cornering at this place"),
    "ACCIDENT": ("HISTORY_ACCIDENTS", "Accidents recorded at this place"),
}
HAZARD_CODES = {"pothole": ("POTHOLE_NEARBY", "Pothole"), "speed_bump": ("SPEED_BUMP_NEARBY", "Speed bump")}
REPORT_LABELS = {
    "ACCIDENT_REPORTED": "Accident reported nearby",
    "ROADWORK_REPORTED": "Roadwork / closed or blocked lane reported nearby",
    "FLOODING_REPORTED": "Flooding reported nearby",
    "CONGESTION_REPORTED": "Traffic jam reported nearby",
    "HAZARD_REPORTED": "Road hazard reported nearby",
}


# ── data shapes ──────────────────────────────────────────────────────────────

class ContextReason(BaseModel):
    """One explanation of the Context Risk Index — `code` is stable (translate it in the app),
    `label` is an English default."""
    code: str = Field(..., description=(
        "Stable code: `HISTORY_HARSH_BRAKING`, `HISTORY_HARSH_CORNERING`, `HISTORY_ACCIDENTS`, "
        "`HISTORY_EVENTS`, `HABITUAL_SPEEDING`, `VEHICLE_SPEEDING`, `POTHOLE_NEARBY`, `SPEED_BUMP_NEARBY`, "
        "`HAZARD_NEARBY`, `ACCIDENT_REPORTED`, `ROADWORK_REPORTED`, `FLOODING_REPORTED`, "
        "`CONGESTION_REPORTED`, `HAZARD_REPORTED`, `RISKY_DRIVER`, `NIGHT`, `POOR_GPS`, `STALE_DATA`."),
        examples=["HISTORY_HARSH_BRAKING"])
    label: str = Field(..., description="English explanation.", examples=["Frequent harsh braking at this place"])
    factor: str = Field(..., description=(
        "Factor of the index: `history`, `habitual_speeding`, `vehicle_speeding`, `hazards`, `reports`, "
        "`driver`, `night`, `poor_gps`, `stale_data`."))
    value: float = Field(..., description="Value of the factor, 0–1.")
    contribution: float = Field(..., description="Share of the index this reason accounts for (weight × value).")
    params: Dict[str, Any] = Field(default_factory=dict, description=(
        "Numbers behind the reason (event counts, speeds in km/h, distances in metres, vehicle id …)."))


class PlaceCell(BaseModel):
    """Precomputed context of one grid cell (Redis `collision:ctx:cell:{cell_id}`)."""
    cell_id: str
    history: float = 0.0                       # 0..1
    history_events: int = 0                    # events in the window (all types)
    history_counts: Dict[str, int] = {}
    history_dominant: Optional[str] = None     # type contributing most points
    speeding: float = 0.0                      # 0..1
    speeding_events: int = 0
    local_limit_mps: Optional[float] = None
    limit_samples: int = 0
    hazards: List[Tuple[str, float, float]] = []          # (type, lat, lon)
    reports: List[Tuple[str, str, float, float]] = []     # (type, kind, lat, lon)
    score: float = 0.0                         # place part of the index at the cell centre
    reasons: List[ContextReason] = []


@dataclass
class Party:
    """What the index needs to know about one vehicle of the pair."""
    vehicle_id: str
    speed_mps: Optional[float] = None
    accuracy_m: Optional[float] = None         # as reported (None = not reported, never "poor")
    telemetry_age_s: float = 0.0
    driver_score: Optional[float] = None       # 0..100, behaviour score of its driver / plate
    driver_high: bool = False                  # score ≥ driver_behaviour.high_risk_score
    driver_ref: Optional[str] = None


class ContextResult(BaseModel):
    index: float = 0.0
    factors: Dict[str, float] = {}
    reasons: List[ContextReason] = []


class EventAgg(NamedTuple):
    """One grouped row of fleet exceptions (raw events or daily summaries)."""
    lat: float
    lon: float
    event_type: str
    severity: str          # low | medium | high | unknown
    when: Any              # date or datetime of the group
    n: int


@dataclass
class _Factor:
    value: float = 0.0
    parts: List[Tuple[str, str, Dict[str, Any], float]] = field(default_factory=list)  # code, label, params, share


# ── precomputation ───────────────────────────────────────────────────────────

def recency(when, now: datetime, half_life_days: float) -> float:
    if isinstance(when, datetime):
        t = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
    elif isinstance(when, date):
        t = datetime(when.year, when.month, when.day, 12, tzinfo=timezone.utc)
    else:
        t = datetime.fromisoformat(str(when)[:10]).replace(hour=12, tzinfo=timezone.utc)
    now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (now - t).total_seconds() / 86400.0)
    return 0.5 ** (age_days / half_life_days) if half_life_days > 0 else 1.0


def build_history(rows: Iterable[EventAgg], cfg: ContextSettings, severity_multiplier, now: datetime,
                  cell_size_m: float, into: Optional[Dict[str, dict]] = None) -> Dict[str, dict]:
    """{cell_id: {points, speeding_points, counts{type: n}, type_points{type: pts}}} —
    points = Σ history_type_weight × severity multiplier × recency × n. `into` accumulates
    several chunks (the window is read a month at a time)."""
    cells: Dict[str, dict] = into if into is not None else {}
    for r in rows:
        n = int(r.n or 0)
        if n <= 0:
            continue
        cid = grid_cell_id(r.lat, r.lon, cell_size_m)
        c = cells.setdefault(cid, {"points": 0.0, "speeding_points": 0.0, "counts": defaultdict(int),
                                   "type_points": defaultdict(float)})
        w = recency(r.when, now, cfg.history_half_life_days) * severity_multiplier(r.severity or "unknown") * n
        c["counts"][r.event_type] += n
        pts = cfg.history_type_weights.get(r.event_type, 0.0) * w
        c["points"] += pts
        c["type_points"][r.event_type] += pts
        if r.event_type == "SPEEDING":
            c["speeding_points"] += w
    return cells


def neighbour_ids(cell_id: str) -> List[str]:
    """The cell and its 8 neighbours (same grid)."""
    size, la, lo = cell_id.split(":")
    la, lo = int(la), int(lo)
    return [f"{size}:{la + i}:{lo + j}" for i in (-1, 0, 1) for j in (-1, 0, 1)]


def median_from_histogram(hist: Dict[float, int]) -> Optional[float]:
    total = sum(hist.values())
    if total <= 0:
        return None
    keys = sorted(hist)
    lo_rank, hi_rank = (total - 1) // 2, total // 2
    seen, lo_v, hi_v = 0, None, None
    for k in keys:
        nxt = seen + hist[k]
        if lo_v is None and lo_rank < nxt:
            lo_v = k
        if hi_v is None and hi_rank < nxt:
            hi_v = k
            break
        seen = nxt
    return (lo_v + hi_v) / 2.0


def infer_local_limits(limit_hist: Dict[str, Dict[float, int]], min_events: int) -> Dict[str, Tuple[float, int]]:
    """{cell_id: (limit_mps, samples)} from {cell_id: {limit_mps: count}} of SPEEDING events.
    A cell with fewer than `min_events` samples pools its 8 neighbours; still too few → no
    limit. Cells without own samples but with enough around them get one too (a road
    crossing a quiet cell)."""
    candidates = set(limit_hist)
    for cid in list(limit_hist):
        candidates.update(neighbour_ids(cid))
    out: Dict[str, Tuple[float, int]] = {}
    for cid in candidates:
        own = limit_hist.get(cid, {})
        hist = dict(own)
        if sum(own.values()) < min_events:
            hist = defaultdict(int)
            for nid in neighbour_ids(cid):
                for k, v in limit_hist.get(nid, {}).items():
                    hist[k] += v
        n = sum(hist.values())
        if n >= min_events:
            m = median_from_histogram(hist)
            if m is not None and m > 0:
                out[cid] = (round(m, 2), n)
    return out


def spread_points(points: Iterable[tuple], radius_m: float, cell_size_m: float) -> Dict[str, List[tuple]]:
    """Every point (…, lat, lon) copied into each cell its radius can reach — a superset, the
    exact distance is checked at lookup (place_factors)."""
    out: Dict[str, List[tuple]] = defaultdict(list)
    for p in points:
        lat, lon = p[-2], p[-1]
        for cid in grid_cell_ids_in_radius(lat, lon, radius_m, cell_size_m):
            out[cid].append(tuple(p))
    return out


def build_place_cells(history: Dict[str, dict], limits: Dict[str, Tuple[float, int]],
                      hazards: Dict[str, List[tuple]], reports: Dict[str, List[tuple]],
                      cfg: ContextSettings) -> Dict[str, PlaceCell]:
    """Merge the per-cell parts; the riskiest `max_cells` are kept (bounded Redis size)."""
    cells: Dict[str, PlaceCell] = {}
    for cid in set(history) | set(limits) | set(hazards) | set(reports):
        h = history.get(cid)
        cell = PlaceCell(cell_id=cid)
        if h:
            cell.history = round(min(1.0, h["points"] / cfg.history_saturation), 4)
            cell.history_events = int(sum(h["counts"].values()))
            cell.history_counts = {k: int(v) for k, v in h["counts"].items()}
            tp = {k: v for k, v in h["type_points"].items() if v > 0}
            cell.history_dominant = max(tp, key=tp.get) if tp else None
            cell.speeding = round(min(1.0, h["speeding_points"] / cfg.speeding_saturation), 4)
            cell.speeding_events = int(h["counts"].get("SPEEDING", 0))
        if cid in limits:
            cell.local_limit_mps, cell.limit_samples = limits[cid]
        cell.hazards = [tuple(x) for x in hazards.get(cid, [])][:50]
        cell.reports = [tuple(x) for x in reports.get(cid, [])][:50]
        center = grid_cell_center(cid)
        res = combine(place_factors(cell, center, cfg), cfg)
        cell.score, cell.reasons = res.index, res.reasons
        cells[cid] = cell
    if len(cells) > cfg.max_cells:
        keep = sorted(cells.values(), key=lambda c: (-c.score, -c.history_events))[:cfg.max_cells]
        cells = {c.cell_id: c for c in keep}
    return cells


# ── factors ──────────────────────────────────────────────────────────────────

def _months(days: int) -> int:
    return max(1, round(days / 30.4))


def place_factors(cell: Optional[PlaceCell], point: LatLon, cfg: ContextSettings) -> Dict[str, _Factor]:
    """history, habitual_speeding, hazards and reports at `point` (exact hazard / report distances)."""
    out = {f: _Factor() for f in ("history", "habitual_speeding", "hazards", "reports")}
    if cell is None:
        return out
    if cell.history > 0:
        code, label = HISTORY_CODES.get(cell.history_dominant or "", ("HISTORY_EVENTS", "Fleet exceptions recorded at this place"))
        months = _months(cfg.history_window_days)
        n_dom = cell.history_counts.get(cell.history_dominant or "", cell.history_events)
        out["history"] = _Factor(cell.history, [(code, f"{label} ({n_dom} in {months} months)",
                                                 {"events": cell.history_events, "dominant_type": cell.history_dominant,
                                                  "dominant_events": n_dom, "months": months,
                                                  "by_type": dict(cell.history_counts)}, 1.0)])
    if cell.speeding > 0:
        params: Dict[str, Any] = {"events": cell.speeding_events}
        if cell.local_limit_mps:
            params["local_limit_kmh"] = round(cell.local_limit_mps * 3.6)
        out["habitual_speeding"] = _Factor(cell.speeding, [(
            "HABITUAL_SPEEDING", f"Speeding is habitual here ({cell.speeding_events} events)", params, 1.0)])

    near: Dict[str, List[float]] = defaultdict(list)
    for typ, lat, lon in cell.hazards:
        if typ not in cfg.hazard_types:
            continue
        d = geo.haversine_m(point[0], point[1], lat, lon)
        if d <= cfg.hazard_radius_m:
            near[typ].append(d)
    total = sum(len(v) for v in near.values())
    if total:
        value = min(1.0, total / cfg.hazard_saturation)
        parts = []
        for typ, ds in sorted(near.items()):
            code, noun = HAZARD_CODES.get(typ, ("HAZARD_NEARBY", typ.replace("_", " ").capitalize()))
            parts.append((code, f"{noun} within {round(min(ds))} m" + (f" ({len(ds)})" if len(ds) > 1 else ""),
                          {"count": len(ds), "nearest_m": round(min(ds)), "radius_m": round(cfg.hazard_radius_m)},
                          len(ds) / total))
        out["hazards"] = _Factor(value, parts)

    kinds: Dict[str, List[float]] = defaultdict(list)
    for typ, kind, lat, lon in cell.reports:
        d = geo.haversine_m(point[0], point[1], lat, lon)
        if d <= cfg.report_radius_m:
            kinds[kind].append(d)
    if kinds:
        weights = {k: cfg.report_kind_weights.get(k, 0.5) * len(ds) for k, ds in kinds.items()}
        wsum = sum(weights.values())
        if wsum > 0:
            parts = [(k, f"{REPORT_LABELS.get(k, 'Report nearby')} ({round(min(ds))} m)",
                      {"count": len(ds), "nearest_m": round(min(ds)), "radius_m": round(cfg.report_radius_m)},
                      weights[k] / wsum)
                     for k, ds in sorted(kinds.items(), key=lambda kv: -weights[kv[0]])]
            out["reports"] = _Factor(min(1.0, wsum), parts)
    return out


def vehicle_speeding_factor(parties: List[Party], local_limit_mps: Optional[float], cfg: ContextSettings) -> _Factor:
    """0 up to speeding_tolerance_pct above the local limit, 1 at tolerance + full_excess."""
    if not local_limit_mps:
        return _Factor()
    best = _Factor()
    for p in parties:
        if p.speed_mps is None:
            continue
        excess = (p.speed_mps - local_limit_mps) / local_limit_mps * 100.0
        if excess <= cfg.speeding_tolerance_pct:
            continue
        value = min(1.0, (excess - cfg.speeding_tolerance_pct) / cfg.speeding_full_excess_pct)
        value = max(value, 0.1)
        if value > best.value:
            best = _Factor(value, [("VEHICLE_SPEEDING",
                                    f"Vehicle at {round(p.speed_mps * 3.6)} km/h where the usual limit is "
                                    f"{round(local_limit_mps * 3.6)} km/h (+{round(excess)} %)",
                                    {"vehicle_id": p.vehicle_id, "speed_kmh": round(p.speed_mps * 3.6),
                                     "local_limit_kmh": round(local_limit_mps * 3.6), "excess_pct": round(excess)},
                                    1.0)])
    return best


def driver_factor(parties: List[Party]) -> _Factor:
    risky = [p for p in parties if p.driver_high and p.driver_score is not None]
    if not risky:
        return _Factor()
    p = max(risky, key=lambda x: x.driver_score)
    return _Factor(min(1.0, p.driver_score / 100.0), [(
        "RISKY_DRIVER", f"High-risk driving behaviour score ({p.driver_score:.0f}/100)",
        {"vehicle_id": p.vehicle_id, "score": p.driver_score, "ref": p.driver_ref}, 1.0)])


def is_night(local_hour: int, cfg: ContextSettings) -> bool:
    s, e = cfg.night_start_hour, cfg.night_end_hour
    return (local_hour >= s or local_hour < e) if s > e else (s <= local_hour < e)


def condition_factors(now: datetime, tz_name: str, parties: List[Party], fresh_after_s: float,
                      cfg: ContextSettings) -> Dict[str, _Factor]:
    from zoneinfo import ZoneInfo
    out = {f: _Factor() for f in ("night", "poor_gps", "stale_data")}
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
    now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    local = now.astimezone(tz)
    if is_night(local.hour, cfg):
        out["night"] = _Factor(1.0, [("NIGHT", "Night driving", {"local_time": local.strftime("%H:%M")}, 1.0)])
    poor = [p for p in parties if p.accuracy_m is not None and p.accuracy_m > cfg.poor_gps_accuracy_m]
    if poor:
        worst = max(poor, key=lambda p: p.accuracy_m)
        out["poor_gps"] = _Factor(1.0, [("POOR_GPS", f"Imprecise GPS position (±{round(worst.accuracy_m)} m)",
                                         {"vehicle_id": worst.vehicle_id, "accuracy_m": round(worst.accuracy_m)}, 1.0)])
    stale = [p for p in parties if p.telemetry_age_s > fresh_after_s]
    if stale:
        worst = max(stale, key=lambda p: p.telemetry_age_s)
        out["stale_data"] = _Factor(1.0, [("STALE_DATA", f"Delayed position data ({worst.telemetry_age_s:.0f} s old)",
                                           {"vehicle_id": worst.vehicle_id, "age_s": round(worst.telemetry_age_s, 1)},
                                           1.0)])
    return out


def combine(factors: Dict[str, _Factor], cfg: ContextSettings) -> ContextResult:
    """C = min(1, Σ weight × value); reasons sorted by contribution (largest first)."""
    total = 0.0
    values: Dict[str, float] = {}
    reasons: List[ContextReason] = []
    for name, f in factors.items():
        v = max(0.0, min(1.0, f.value))
        values[name] = round(v, 4)
        contrib = cfg.weight(name) * v
        total += contrib
        for code, label, params, share in f.parts:
            c = contrib * share
            if c >= cfg.min_reason_contribution:
                reasons.append(ContextReason(code=code, label=label, factor=name, value=round(v, 3),
                                             contribution=round(c, 3), params=params))
    reasons.sort(key=lambda r: -r.contribution)
    return ContextResult(index=round(min(1.0, total), 4), factors=values, reasons=reasons)


def compute_context(cell: Optional[PlaceCell], point: LatLon, parties: List[Party], now: datetime,
                    tz_name: str, fresh_after_s: float, cfg: ContextSettings) -> ContextResult:
    """The whole index at `point` for this pair of parties."""
    if not cfg.enabled:
        return ContextResult()
    factors = place_factors(cell, point, cfg)
    factors["vehicle_speeding"] = vehicle_speeding_factor(parties, cell.local_limit_mps if cell else None, cfg)
    factors["driver"] = driver_factor(parties)
    factors.update(condition_factors(now, tz_name, parties, fresh_after_s, cfg))
    return combine(factors, cfg)


def widening(index: float, cfg: ContextSettings) -> float:
    """Scale applied to every distance / TTC threshold: 1 … 1 + max_widening."""
    if not cfg.enabled:
        return 1.0
    return 1.0 + cfg.max_widening * max(0.0, min(1.0, index))


def boost_steps(index: float, cfg: ContextSettings) -> int:
    return cfg.max_boost_steps if cfg.enabled and index >= cfg.boost_threshold else 0


def raise_level(physical_rank: int, widened_rank: int, index: float, cfg: ContextSettings,
                max_rank: int = 4) -> Tuple[int, int]:
    """(final rank, applied boost) from the physical rank (0 = no conflict … 4 = CRITICAL) and
    the rank reached with widened thresholds. Context never creates (rank 0 stays 0) nor
    lowers a level; the boost adds at most max_boost_steps, never above max_rank."""
    if physical_rank <= 0:
        return physical_rank, 0
    rank = max(physical_rank, widened_rank)
    steps = boost_steps(index, cfg)
    boosted = min(max_rank, rank + steps)
    return boosted, boosted - rank


def calibration_report(index_by_cell: Dict[str, float], outcomes: Dict[str, int], min_events: int) -> dict:
    """Is the place index higher where harsh braking / accidents concentrate? Cells with
    ≥ min_events outcome events ("hot") vs the other indexed cells: mean index of both, their
    ratio and the probability that a hot cell ranks above a cold one (AUC, 0.5 = no signal)."""
    cells = set(index_by_cell) | set(outcomes)
    hot = [index_by_cell.get(c, 0.0) for c in cells if outcomes.get(c, 0) >= min_events]
    cold = [index_by_cell.get(c, 0.0) for c in cells if outcomes.get(c, 0) < min_events]

    def mean(xs):
        return sum(xs) / len(xs) if xs else None

    auc = None
    if hot and cold:
        cold_sorted = sorted(cold)
        wins = 0.0
        for h in hot:
            lo = bisect.bisect_left(cold_sorted, h)
            hi = bisect.bisect_right(cold_sorted, h)
            wins += lo + 0.5 * (hi - lo)
        auc = round(wins / (len(hot) * len(cold)), 3)
    mh, mc = mean(hot), mean(cold)
    return {
        "hot_cells": len(hot), "other_cells": len(cold), "min_events": min_events,
        "mean_index_hot": round(mh, 4) if mh is not None else None,
        "mean_index_other": round(mc, 4) if mc is not None else None,
        "ratio": round(mh / mc, 2) if mh is not None and mc else None,
        "auc": auc,
        "verdict": ("ok" if auc is not None and auc >= 0.6 and (mc is None or mh > mc) else
                    "insufficient data" if auc is None else "weak"),
    }
