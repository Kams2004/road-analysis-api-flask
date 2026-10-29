"""
Daily Ymane exception file (one CSV per day, `YYYY-MM-DD.csv`, dropped on an SFTP server
around 03:00 for the previous day) → rows of `historical_events`. Pure: no database, no
network — app.modules.m08_historical.event_feed fetches, archives and stores.

Columns used (`raw` keeps only the non-identifying columns — anonymity.RAW_KEYS):

    exceptionid                  → source_event_id (source "ymane": the same ids as the Ymane
                                   API, so both never duplicate each other)
    exception_type_name          → event_type   Speeding / Harsh Acceleration / Harsh Braking /
                                                 Harsh Cornering (anything else: OTHER)
    startdatetime, enddatetime   → occurred_at, ended_at  (UTC, no zone in the file)
    startgps                     → longitude, latitude — LONGITUDE FIRST, as "9.12,4.01" or
                                   "(49.15,-18.43)"
    vmax, threshold              → speed_mps, speed_limit_mps (km/h; the limit only for Speeding,
                                   for harsh events `threshold` / `maxvalue` are acceleration values)
    level                        → severity ("1" low, "2" medium, "3" high)
    totalduration (hours), totaldistance (km) → duration_s, distance_km
    customer_affiliate_name      → country (upper case), transporter_name → transporter

Never read: the driver and vehicle columns (driver_name, vehicledesc, unitid …) — the events are
anonymous (app/modules/m08_historical/anonymity.py).
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from app.modules.m08_historical.anonymity import clean_raw
from app.modules.m08_historical.domain import HistoricalEventType

SOURCE = "ymane"
REQUIRED = ("exceptionid", "exception_type_name", "startdatetime", "startgps")
MAX_PROBLEMS = 20            # skipped lines reported per file
KMH = 3.6

TYPES = {
    "speeding": HistoricalEventType.SPEEDING,
    "harsh acceleration": HistoricalEventType.HARD_ACCELERATION,
    "harsh braking": HistoricalEventType.HARD_BRAKING,
    "harsh cornering": HistoricalEventType.HARSH_CORNERING,
}
_GPS = re.compile(r"^\(?\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\)?$")


class CsvFormatError(ValueError):
    """The file is not an exception export (missing columns, not text)."""


@dataclass
class ParseResult:
    events: list[dict] = field(default_factory=list)
    rows: int = 0                                         # data lines read
    problems: list[dict] = field(default_factory=list)    # first MAX_PROBLEMS skipped lines
    invalid: int = 0

    def skip(self, line: int, reason: str) -> None:
        self.invalid += 1
        if len(self.problems) < MAX_PROBLEMS:
            self.problems.append({"line": line, "reason": reason})

    def by_type(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for e in self.events:
            out[e["event_type"]] = out.get(e["event_type"], 0) + 1
        return out


def event_type(name: Optional[str]) -> str:
    return TYPES.get(" ".join((name or "").lower().split()), HistoricalEventType.OTHER).value


def parse_gps(value: Optional[str]) -> Optional[tuple[float, float]]:
    """(lat, lon) from "lon,lat" / "(lon,lat)"; None when missing, malformed, (0,0) or out of range."""
    m = _GPS.match((value or "").strip())
    if not m:
        return None
    lon, lat = float(m.group(1)), float(m.group(2))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None
    return lat, lon


def parse_time(value: Optional[str]) -> Optional[datetime]:
    v = (value or "").strip()
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:                         # stored naive UTC like every other timestamp
        from datetime import timezone
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _num(value: Optional[str]) -> Optional[float]:
    try:
        v = float((value or "").strip())
    except ValueError:
        return None
    return v if v == v else None                      # NaN → None


def _text(value: Optional[str]) -> Optional[str]:
    v = " ".join((value or "").split())
    return v or None


def decode(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def parse(data: bytes) -> ParseResult:
    """Every usable line of the file as a historical_events row (without grid / map matching).
    A line appearing twice (same exceptionid) is kept once, the last one wins."""
    if b"\x00" in data[:4096]:
        raise CsvFormatError("This is not a text (CSV) file.")
    reader = csv.DictReader(io.StringIO(decode(data), newline=""))
    header = [h.strip().lower() for h in (reader.fieldnames or [])]
    missing = [c for c in REQUIRED if c not in header]
    if missing:
        raise CsvFormatError(f"Not an exception export: column(s) {', '.join(missing)} missing.")
    reader.fieldnames = header
    out = ParseResult()
    by_id: dict[str, dict] = {}
    for row in reader:
        out.rows += 1
        line = reader.line_num
        ev_id = _text(row.get("exceptionid"))
        if not ev_id:
            out.skip(line, "no exceptionid")
            continue
        start = parse_time(row.get("startdatetime"))
        if start is None:
            out.skip(line, f"exception {ev_id}: invalid startdatetime")
            continue
        pos = parse_gps(row.get("startgps"))
        if pos is None:
            out.skip(line, f"exception {ev_id}: invalid or missing startgps")
            continue
        etype = event_type(row.get("exception_type_name"))
        end = parse_time(row.get("enddatetime"))
        hours = _num(row.get("totalduration"))
        vmax = _num(row.get("vmax"))
        threshold = _num(row.get("threshold"))
        country = _text(row.get("customer_affiliate_name"))
        by_id[ev_id] = {
            "source": SOURCE,
            "source_event_id": ev_id,
            "event_type": etype,
            "occurred_at": start,
            "ended_at": end,
            "latitude": pos[0],
            "longitude": pos[1],
            "speed_mps": round(vmax / KMH, 3) if vmax is not None and vmax > 0 else None,
            "speed_limit_mps": (round(threshold / KMH, 3) if etype == HistoricalEventType.SPEEDING.value
                                and threshold is not None and threshold > 0 else None),
            "severity": _text(row.get("level")),
            "country": country.upper() if country else None,
            "transporter": _text(row.get("transporter_name")),
            "duration_s": (round(hours * 3600, 1) if hours is not None and hours >= 0
                           else (end - start).total_seconds() if end and end >= start else None),
            "distance_km": _num(row.get("totaldistance")),
            "raw": clean_raw({k: v for k, v in row.items() if k}),
        }
    out.events = list(by_id.values())
    return out
