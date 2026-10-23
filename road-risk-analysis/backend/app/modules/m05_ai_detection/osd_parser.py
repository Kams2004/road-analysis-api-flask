"""
OSD reader — PaddleOCR on the dashcam overlay, per equipment layout (RRAP-05-014).

The overlay regions and text formats come from the layout profiles of
`osd_layouts` (built-in `total` and `besafe` + the ones configured in the
`osd` settings section). `OsdReader` (one per job):

  - layout: forced (job option / settings by source) or auto-detected on the
    first frames with content (black / start-up frames skipped): every candidate
    profile reads its regions, the one with the most valid GPS + date-time parses
    wins. Nothing parses → full-frame OCR + regex search (`full_frame`), and when
    that finds nothing either, OCR stops for the job (`none`).
  - only the regions of the profile are OCR'd; fields that do not change during a
    recording (plate, company, driver) stop being read once stable.
  - uncertain GPS / date-time (no parse, low OCR confidence, outside the GPS box,
    rejected by the GpsVoter jump check, time inconsistent with the video clock):
      1. image variants of the region (2× upscale, per-channel max, inverted) —
         the text colour of dashcam overlays changes with the scene and a
         transition can hide part of it;
      2. the same region on neighbour frames (−1 s, +1 s, −2 s, +2 s): GPS
         interpolated between both sides (else the nearest side), time shifted by
         the offset — source `ocr_neighbour`;
      3. else the GpsVoter window median (source `interpolated`).

Also exports the pure parsers used elsewhere (parse_gps_text, _fix_dot_separators).
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, time as dtime, timedelta
from typing import Callable, Optional

import numpy as np

from app.modules.m05_ai_detection import osd_layouts as L
from app.modules.m05_ai_detection.osd_layouts import (  # noqa: F401  (public re-exports)
    CAMEROON_BOX, FrameRead, Profile, fix_separators, parse_datetime, parse_partial,
)

logger = logging.getLogger(__name__)

# ── OCR init ──────────────────────────────────────────────────────────────────

_ocr_engine = None
_ocr_unavailable = False


def _init_ocr() -> bool:
    global _ocr_engine, _ocr_unavailable
    if _ocr_unavailable:
        return False
    if _ocr_engine is not None:
        return True
    try:
        import os
        os.environ.setdefault("PPOCR_LOGGING_LEVEL", "ERROR")
        logging.getLogger("ppocr").setLevel(logging.WARNING)
        from paddleocr import PaddleOCR
        from app.core.config import settings
        _ocr_engine = PaddleOCR(use_angle_cls=False, lang="en", show_log=False,
                                cpu_threads=max(1, settings.OCR_CPU_THREADS))
        logger.info("OCR backend: PaddleOCR ready")
        return True
    except Exception as e:
        logger.error(f"PaddleOCR unavailable: {e}")
        _ocr_unavailable = True
        return False


# ── data class ────────────────────────────────────────────────────────────────

@dataclass
class OSDData:
    raw_gps_text:  Optional[str]      = None
    latitude:      Optional[float]    = None
    longitude:     Optional[float]    = None
    speed_kmh:     Optional[float]    = None
    vehicle_id:    Optional[str]      = None
    timestamp:     Optional[datetime] = None   # camera local time (naive), see settings.OSD_TIMEZONE
    location_name: Optional[str]      = None
    rpm:           Optional[int]      = None
    gps_interpolated: bool            = False  # True when value came from the voter window
    company:       Optional[str]      = None
    driver_name:   Optional[str]      = None
    layout:        Optional[str]      = None
    gps_source:    Optional[str]      = None   # ocr | ocr_neighbour | interpolated
    time_source:   Optional[str]      = None   # ocr | ocr_neighbour
    channel:       Optional[str]      = None   # camera channel shown by some equipment ('CH2')
    reused:        bool               = False  # copy of the last read (osd.read_interval_s): no new fix


def reset_last() -> None:
    """No state — kept for API compatibility."""


# ── public parsers (compatibility names) ──────────────────────────────────────

def parse_gps_text(raw: str, box=None) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """NMEA GPS text → (lat, lon, speed_kmh); (None, None, None) when unparseable.
    Used by the location-correction endpoint (no plausibility box there)."""
    return L.parse_gps_text(raw, box)


_fix_dot_separators = fix_separators


# ── image helpers ─────────────────────────────────────────────────────────────

VARIANTS = ("up2", "chmax", "inv")


def _crop(frame: np.ndarray, roi: tuple) -> np.ndarray:
    h, w = frame.shape[:2]
    x0, y0 = max(0, int(roi[0] * w)), max(0, int(roi[1] * h))
    x1, y1 = min(w, int(roi[2] * w)), min(h, int(roi[3] * h))
    return frame[y0:y1, x0:x1]


def has_content(frame: np.ndarray) -> bool:
    """False for black / uniform start-up frames."""
    if frame is None or frame.size == 0:
        return False
    small = frame[::8, ::8]
    return float(small.std()) > 6.0


def prepare(frame: np.ndarray, box: tuple, variant: Optional[str] = None) -> np.ndarray:
    """Region crop ready for OCR: field-only recordings (720×288) stretched back to
    the display aspect, optional colour-transition variant, black border."""
    import cv2
    img = _crop(frame, box)
    if img.size == 0:
        return img
    h, w = frame.shape[:2]
    if h and w / h >= 2.2:
        img = cv2.resize(img, (img.shape[1], img.shape[0] * 2), interpolation=cv2.INTER_LINEAR)
    if variant in ("up2", "chmax", "inv"):
        img = cv2.resize(img, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    if variant == "chmax" and img.ndim == 3:
        g = img.max(axis=2)
        img = cv2.cvtColor(g, cv2.COLOR_GRAY2BGR)
    elif variant == "inv":
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
        img = cv2.cvtColor(255 - g, cv2.COLOR_GRAY2BGR)
    return cv2.copyMakeBorder(img, 10, 10, 10, 10, cv2.BORDER_CONSTANT, value=(0, 0, 0))


def ocr_image(img: np.ndarray) -> tuple[str, float]:
    """PaddleOCR → (text upper-cased, min confidence of its segments; 0 when empty)."""
    if img is None or img.size == 0 or not _init_ocr():
        return "", 0.0
    try:
        result = _ocr_engine.ocr(img, cls=False)
        if not result or not result[0]:
            return "", 0.0
        segs = [ln[1] for ln in result[0] if ln and ln[1][0].strip()]
        if not segs:
            return "", 0.0
        return " ".join(s[0] for s in segs).upper().strip(), float(min(s[1] for s in segs))
    except Exception as e:
        logger.debug(f"PaddleOCR error: {e}")
        return "", 0.0


def ocr_region(frame: np.ndarray, box: tuple, variant: Optional[str] = None) -> tuple[str, float]:
    return ocr_image(prepare(frame, box, variant))


# ── GPS temporal voter ───────────────────────────────────────────────────────

class GpsVoter:
    """
    Rolling window of recent valid GPS reads: a fix that jumps more than MAX_JUMP
    degrees from the previous one is an outlier; when a frame has no valid fix the
    window median is returned (gps_interpolated=True).
    """
    WINDOW = 5
    MAX_JUMP = 0.01  # degrees per frame-skip interval

    def __init__(self):
        self._lats: list[float] = []
        self._lons: list[float] = []

    def reset(self):
        self._lats.clear()
        self._lons.clear()

    def accepts(self, lat: Optional[float], lon: Optional[float]) -> bool:
        if lat is None or lon is None:
            return False
        if not self._lats:
            return True
        return abs(lat - self._lats[-1]) <= self.MAX_JUMP and abs(lon - self._lons[-1]) <= self.MAX_JUMP

    def update(self, lat: Optional[float], lon: Optional[float]
               ) -> tuple[Optional[float], Optional[float], bool]:
        """(best_lat, best_lon, interpolated)."""
        if lat is not None and lon is not None and not self.accepts(lat, lon):
            lat, lon = None, None
        if lat is not None and lon is not None:
            self._lats.append(lat)
            self._lons.append(lon)
            if len(self._lats) > self.WINDOW:
                self._lats.pop(0)
                self._lons.pop(0)
            return lat, lon, False
        if self._lats:
            return sorted(self._lats)[len(self._lats) // 2], sorted(self._lons)[len(self._lons) // 2], True
        return None, None, False


# ── reader ────────────────────────────────────────────────────────────────────

OcrFn = Callable[[np.ndarray, tuple, Optional[str]], tuple[str, float]]
NeighbourFn = Callable[[float], Optional[np.ndarray]]

LOW_CONFIDENCE = 0.6
TIME_TOLERANCE_S = 3.0


# Fields only stored for information (rpm on the detection): read every SLOW_INTERVAL_S of video.
SLOW_KINDS = frozenset({"rpm"})
SLOW_INTERVAL_S = 10.0


class OsdReader:
    """Per-job OSD reading (see module docstring). `ocr(frame, box, variant)` can be
    replaced (tests); `read(frame, t, neighbours)` with `neighbours(offset_s)` → frame."""

    def __init__(self, profiles: Optional[dict[str, Profile]] = None, *, forced: Optional[str] = None,
                 method: str = "auto", by_vehicle: Optional[dict[str, str]] = None, box=CAMEROON_BOX,
                 voter: Optional[GpsVoter] = None, probe_frames: int = 5, full_frame_fallback: bool = True,
                 ocr: Optional[OcrFn] = None, neighbour_offsets=(-1.0, 1.0, -2.0, 2.0),
                 neighbour_min_interval_s: float = 2.0, max_probe_reads: int = 40,
                 min_interval_s: float = 0.0):
        self.profiles = profiles if profiles is not None else L.effective_profiles()
        # The overlay text changes once per second: frames closer than this (video time) to the
        # last read reuse it instead of running the OCR again (the costliest step per frame).
        self.min_interval_s = max(0.0, float(min_interval_s))
        self._last_read: Optional[tuple[float, OSDData]] = None
        self._slow: dict[int, tuple[float, FrameRead]] = {}   # roi index → (t, last read) of SLOW_KINDS
        self.box = tuple(box) if box else None
        self.voter = voter if voter is not None else GpsVoter()
        self.ocr = ocr or ocr_region
        self.by_vehicle = {k.upper().replace(" ", ""): v for k, v in (by_vehicle or {}).items()}
        self.probe_frames, self.full_frame_fallback = probe_frames, full_frame_fallback
        self.max_probe_reads = max_probe_reads
        self.neighbour_offsets, self.neighbour_min_interval_s = tuple(neighbour_offsets), neighbour_min_interval_s
        self.events: list[dict] = []
        self.profile: Optional[Profile] = None
        self.layout: Optional[str] = None
        self.method = method
        self.detector = L.LayoutDetector([n for n, p in self.profiles.items() if p.detect])
        self._probe_reads = 0
        self._fallback_frames = self._fallback_hits = 0
        self.statics = L.StaticFields()
        self._kind_votes: dict[str, Counter] = {}
        self._last_time: Optional[tuple[float, datetime]] = None     # (video t, OSD time) of the last good read
        self._conflict: Optional[tuple[float, datetime]] = None
        self._dates: Counter = Counter()
        self.max_time = datetime.now() + timedelta(days=1)   # OSD clocks are local time: 1 day margin
        self._last_good: dict[str, float] = {}                       # field → video t of the last good read
        self._last_retry: dict[str, float] = {}
        self.stats = Counter()
        name = L.resolve_name(forced, self.profiles) if forced else None
        if name:
            self._resolve(name, method if method != "auto" else "forced")
        elif forced and forced.lower() == L.NO_OSD:
            self._resolve(L.NO_OSD, method if method != "auto" else "forced")

    # ── layout ───────────────────────────────────────────────────────────────

    @property
    def timezone(self) -> Optional[str]:
        return self.profile.timezone if self.profile else None

    def _resolve(self, name: str, method: str) -> None:
        self.layout, self.method = name, method
        self.profile = (L.full_frame_profile() if name == L.FULL_FRAME else
                        None if name == L.NO_OSD else self.profiles[name])
        self.statics = L.StaticFields()
        ev = {"layout": name, "method": method,
              "label": self.profile.label if self.profile else "no overlay found"}
        if self.detector.frames:
            ev["detection"] = self.detector.summary()
        if self.profile is not None:
            ev["rois"] = {r.field: list(r.box) for r in self.profile.rois}
            if self.profile.timezone:
                ev["timezone"] = self.profile.timezone
        self.events.append(ev)
        logger.info(f"OSD layout: {name} ({method})")

    def pop_events(self) -> list[dict]:
        out, self.events = self.events, []
        return out

    def finish(self) -> None:
        """End of the media: a layout still being probed is decided now."""
        if self.layout is None:
            self._decide(final=True)

    def _decide(self, final: bool = False) -> None:
        best = self.detector.best()
        if best:
            self._resolve(best, "auto")
        elif self.full_frame_fallback and not final:
            self._resolve(L.FULL_FRAME, "fallback")
        else:
            self._resolve(L.NO_OSD, "fallback" if self.full_frame_fallback else "auto")

    # ── reading ──────────────────────────────────────────────────────────────

    def read(self, frame: np.ndarray, t: Optional[float] = None,
             neighbours: Optional[NeighbourFn] = None) -> OSDData:
        self.stats["frames"] += 1
        t = float(self.stats["frames"]) if t is None else float(t)
        if self.layout == L.NO_OSD:
            return self._finalise(FrameRead(), t)
        if self.layout is None:
            return self._probe(frame, t)
        if self._last_read is not None and 0 <= t - self._last_read[0] < self.min_interval_s:
            self.stats["reused"] += 1
            t0, last = self._last_read
            ts = last.timestamp + timedelta(seconds=t - t0) if last.timestamp is not None else None
            return replace(last, timestamp=ts, reused=True)
        read = self._read_profile(frame, t, neighbours)
        if self.layout == L.FULL_FRAME:
            self._fallback_frames += 1
            self._fallback_hits += read.score > 0
            if self._fallback_frames >= 2 * self.probe_frames and not self._fallback_hits:
                self._resolve(L.NO_OSD, "fallback")
        out = self._finalise(read, t)
        self._last_read = (t, out)
        return out

    def _probe(self, frame: np.ndarray, t: float) -> OSDData:
        self._probe_reads += 1
        if not has_content(frame):
            if self._probe_reads >= self.max_probe_reads:
                self._decide()
            return self._finalise(FrameRead(), t)
        reads = {}
        for name in self.detector.candidates:
            p = self.profiles[name]
            texts = {i: self.ocr(frame, r.box, None)[0] for i, r in enumerate(p.rois)}
            reads[name] = L.parse_frame(p, texts, self.box)
            if reads[name].timestamp is not None and reads[name].timestamp > self.max_time:
                reads[name].timestamp = None
        self.detector.add(reads)
        best = max(reads.values(), key=lambda r: r.score, default=FrameRead())
        for r in reads.values():
            plate = (r.vehicle_id or "").upper()
            if plate in self.by_vehicle:
                forced = L.resolve_name(self.by_vehicle[plate], self.profiles)
                if forced:
                    self._resolve(forced, "vehicle")
                    break
        if self.layout is None and (self.detector.frames >= self.probe_frames
                                    or self._probe_reads >= self.max_probe_reads):
            self._decide()
        if self.layout in self.profiles and self.layout in reads:
            best = reads[self.layout]
        for r in reads.values():
            self._vote_date(r.timestamp)
        if best.latitude is not None and not self.voter.accepts(best.latitude, best.longitude):
            best.latitude = best.longitude = None
        return self._finalise(best, t, gps_source="ocr", time_source="ocr")

    def _read_roi(self, frame, i: int, roi: L.Roi, variant=None) -> tuple[FrameRead, float]:
        text, conf = self.ocr(frame, roi.box, variant)
        sub = FrameRead()
        kind = roi.field
        L.parse_field(kind, text, self.profile, sub, self.box)
        sub.texts["text"] = text
        if sub.timestamp is not None and sub.timestamp > self.max_time:
            sub.timestamp = None                  # a recording is never dated in the future (misread digit)
        if kind == "gps" and sub.raw_gps_text is None and text:
            sub.raw_gps_text = text
        return sub, conf

    def _gps_ok(self, sub: FrameRead) -> bool:
        return sub.latitude is not None and self.voter.accepts(sub.latitude, sub.longitude)

    def _time_value(self, ts: Optional[datetime], text: str, t: float) -> Optional[datetime]:
        """Reading accepted for video time t (date repaired when only the date was misread,
        time of day alone when the date is unreadable), else None. No anchor yet → as read."""
        if self._last_time is None:
            return ts
        lt, lts = self._last_time
        exp = lts + timedelta(seconds=t - lt)
        if ts is not None and abs((ts - exp).total_seconds()) <= TIME_TOLERANCE_S:
            return ts
        tod = (ts.hour, ts.minute, ts.second) if ts is not None else L.parse_time_of_day(text)
        if tod:
            for dd in (0, -1, 1):
                c = datetime.combine(exp.date() + timedelta(days=dd), dtime(*tod))
                if abs((c - exp).total_seconds()) <= TIME_TOLERANCE_S:
                    return c
        return None

    def _vote_date(self, ts: Optional[datetime]) -> None:
        """Dates read so far: when most readings disagree with the anchor's date (a day misread
        on the anchor), the anchor moves to the majority date (same time of day)."""
        if ts is None:
            return
        self._dates[ts.date()] += 1
        if self._last_time is None:
            return
        lt, lts = self._last_time
        best, n = self._dates.most_common(1)[0]
        if best != lts.date() and n >= 2 and n >= self._dates[lts.date()] + 2:
            self._last_time = (lt, datetime.combine(best, lts.time()))
            self.stats["time_date_fixed"] += 1

    def _time_conflict(self, ts: Optional[datetime], t: float) -> Optional[datetime]:
        """A reading inconsistent with the anchor: when two such readings agree with each other
        the anchor was the wrong one → re-anchor on them."""
        if ts is None:
            return None
        prev, self._conflict = self._conflict, (t, ts)
        if prev and abs((ts - prev[1]).total_seconds() - (t - prev[0])) <= TIME_TOLERANCE_S:
            self.stats["time_reanchored"] += 1
            return ts
        return None

    def _read_profile(self, frame, t: float, neighbours: Optional[NeighbourFn]) -> FrameRead:
        read = FrameRead()
        gps_src = time_src = None
        for i, roi in enumerate(self.profile.rois):
            if roi.static and self.statics.stable(i):
                continue
            if roi.field in SLOW_KINDS and i in self._slow and t - self._slow[i][0] < SLOW_INTERVAL_S:
                _merge(read, self._slow[i][1])          # informative only (rpm): not re-read every second
                continue
            sub, conf = self._read_roi(frame, i, roi)
            if roi.field in SLOW_KINDS:
                self._slow[i] = (t, sub)
            if roi.field in ("gps", "any") and sub.raw_gps_text and not read.raw_gps_text:
                read.raw_gps_text = sub.raw_gps_text
            if roi.field == "gps":
                sub, gps_src = self._settle_gps(frame, i, roi, sub, conf, t, neighbours)
            elif roi.field == "date_time":
                sub, time_src = self._settle_time(frame, i, roi, sub, conf, t, neighbours)
            elif roi.field == "any":
                gps_src = "ocr" if self._gps_ok(sub) else None
                time_src = "ocr" if sub.timestamp is not None else None
            if roi.static:
                self.statics.add(i, (sub.vehicle_id, sub.company, sub.driver_name, sub.driver_id, sub.channel))
            _merge(read, sub)
        if read.latitude is not None and not self._gps_ok(read):
            read.latitude = read.longitude = None
        return self._finalise(read, t, gps_source=gps_src, time_source=time_src, raw=True)

    def _may_retry(self, fieldname: str, t: float) -> bool:
        last_good = self._last_good.get(fieldname)
        last_retry = self._last_retry.get(fieldname)
        if last_good is not None and t - last_good < self.neighbour_min_interval_s / 2:
            return False
        if last_retry is not None and t - last_retry < self.neighbour_min_interval_s:
            return False
        return True

    def _settle_gps(self, frame, i, roi, sub: FrameRead, conf: float, t: float,
                    neighbours) -> tuple[FrameRead, Optional[str]]:
        if self._gps_ok(sub) and conf >= LOW_CONFIDENCE:
            self._last_good["gps"] = t
            return sub, "ocr"
        fallback = sub if self._gps_ok(sub) else None
        for v in VARIANTS:                        # colour transitions: same frame, other renderings
            alt, c2 = self._read_roi(frame, i, roi, v)
            self.stats["gps_variant_reads"] += 1
            if self._gps_ok(alt) and (fallback is None or c2 >= LOW_CONFIDENCE):
                alt.raw_gps_text = alt.raw_gps_text or sub.raw_gps_text
                self._last_good["gps"] = t
                self.stats["gps_variant_fixes"] += 1
                return alt, "ocr"
        if fallback is not None:
            self._last_good["gps"] = t
            return fallback, "ocr"
        if neighbours is None or not self._may_retry("gps", t):
            return sub, None
        self._last_retry["gps"] = t
        found: dict[float, FrameRead] = {}
        for off in self.neighbour_offsets:
            nf = neighbours(off)
            if nf is None:
                continue
            self.stats["gps_neighbour_reads"] += 1
            for v in (None, "chmax"):
                alt, _ = self._read_roi(nf, i, roi, v)
                if alt.latitude is not None and _near(alt, self.voter, abs(off)):
                    found[off] = alt
                    break
            if any(o < 0 for o in found) and any(o > 0 for o in found):
                break
        if not found:
            return sub, None
        before = [o for o in found if o < 0]
        after = [o for o in found if o > 0]
        if before and after:
            a, b = found[max(before)], found[min(after)]
            oa, ob = max(before), min(after)
            w = -oa / (ob - oa)
            res = FrameRead(latitude=round(a.latitude + w * (b.latitude - a.latitude), 6),
                            longitude=round(a.longitude + w * (b.longitude - a.longitude), 6),
                            speed_kmh=a.speed_kmh if a.speed_kmh is not None else b.speed_kmh,
                            raw_gps_text=sub.raw_gps_text or a.raw_gps_text)
        else:
            off = min(found, key=abs)
            res = found[off]
            res.raw_gps_text = sub.raw_gps_text or res.raw_gps_text
        self.stats["gps_neighbour_fixes"] += 1
        return res, "ocr_neighbour"

    def _settle_time(self, frame, i, roi, sub: FrameRead, conf: float, t: float,
                     neighbours) -> tuple[FrameRead, Optional[str]]:
        raw_ts = sub.timestamp
        self._vote_date(raw_ts)
        v = self._time_value(raw_ts, sub.texts.get("text", ""), t)
        if v is not None and conf >= LOW_CONFIDENCE:
            return self._good_time(sub, v, t), "ocr"
        fallback = v
        for var in VARIANTS:
            alt, c2 = self._read_roi(frame, i, roi, var)
            self.stats["time_variant_reads"] += 1
            self._vote_date(alt.timestamp)
            v2 = self._time_value(alt.timestamp, alt.texts.get("text", ""), t)
            if v2 is not None and (fallback is None or c2 >= LOW_CONFIDENCE):
                self.stats["time_variant_fixes"] += 1
                return self._good_time(sub, v2, t), "ocr"
        if fallback is not None:
            return self._good_time(sub, fallback, t), "ocr"
        if neighbours is not None and self._last_time is not None and self._may_retry("time", t):
            self._last_retry["time"] = t
            for off in self.neighbour_offsets:
                nf = neighbours(off)
                if nf is None:
                    continue
                self.stats["time_neighbour_reads"] += 1
                alt, _ = self._read_roi(nf, i, roi)
                v2 = self._time_value(alt.timestamp, alt.texts.get("text", ""), t + off)
                if v2 is not None:
                    self.stats["time_neighbour_fixes"] += 1
                    return self._good_time(sub, v2 - timedelta(seconds=round(off)), t), "ocr_neighbour"
        v = self._time_conflict(raw_ts, t)
        if v is not None:
            return self._good_time(sub, v, t), "ocr"
        sub.timestamp = None
        return sub, None

    def _good_time(self, sub: FrameRead, ts: datetime, t: float) -> FrameRead:
        sub.timestamp = ts
        self._last_time = (t, ts)
        self._last_good["time"] = t
        self._conflict = None
        return sub

    def _finalise(self, read: FrameRead, t: float, gps_source: Optional[str] = None,
                  time_source: Optional[str] = None, raw: bool = False):
        if raw:                                        # called from _read_profile: keep FrameRead
            read._gps_source, read._time_source = gps_source, time_source   # type: ignore[attr-defined]
            return read
        if hasattr(read, "_gps_source"):
            gps_source, time_source = read._gps_source, read._time_source   # type: ignore[attr-defined]
        lat, lon, interpolated = self.voter.update(read.latitude, read.longitude)
        if lat is None:
            gps_source = None
        elif interpolated:
            gps_source = "interpolated"
        elif gps_source is None:
            gps_source = "ocr"
        ts = read.timestamp
        if ts is not None and self._last_time is None and self.layout is not None:
            self._last_time = (t, ts)
        self.stats["gps_" + (gps_source or "missing")] += 1
        self.stats["time_" + ((time_source or "ocr") if ts is not None else "missing")] += 1
        for k in ("vehicle_id", "company", "driver_name", "channel"):
            v = getattr(read, k)
            if v:
                self._kind_votes.setdefault(k, Counter())[v] += 1
        return OSDData(
            raw_gps_text=read.raw_gps_text or None, latitude=lat, longitude=lon, speed_kmh=read.speed_kmh,
            timestamp=ts, vehicle_id=self._majority("vehicle_id") or read.vehicle_id,
            company=self._majority("company") or read.company, rpm=read.rpm,
            driver_name=self._majority("driver_name") or read.driver_name,
            channel=self._majority("channel") or read.channel,
            gps_interpolated=interpolated, layout=self.layout, gps_source=gps_source,
            time_source=(time_source or "ocr") if ts is not None else None,
        )

    def _majority(self, kind: str) -> Optional[str]:
        c = self._kind_votes.get(kind)
        return c.most_common(1)[0][0] if c else None

    def summary(self) -> dict:
        """Job event details: layout + read statistics."""
        return {"layout": self.layout, "method": self.method, "vehicle_id": self._majority("vehicle_id"),
                "company": self._majority("company"), "channel": self._majority("channel"),
                "reads": dict(self.stats)}


def _merge(read: FrameRead, sub: FrameRead) -> None:
    for k in ("raw_gps_text", "latitude", "longitude", "speed_kmh", "timestamp", "vehicle_id", "company",
              "rpm", "driver_name", "driver_id", "channel"):
        if getattr(read, k) is None and getattr(sub, k) is not None:
            setattr(read, k, getattr(sub, k))


def _near(r: FrameRead, voter: GpsVoter, dt_s: float) -> bool:
    """A neighbour fix is consistent with the window (jump limit scaled by its time offset)."""
    if not voter._lats:
        return True
    lim = voter.MAX_JUMP * max(1.0, dt_s)
    return abs(r.latitude - voter._lats[-1]) <= lim and abs(r.longitude - voter._lons[-1]) <= lim


# ── stateless compatibility API ───────────────────────────────────────────────

def extract_osd(frame: np.ndarray, voter: Optional[GpsVoter] = None, layout: str = "total") -> OSDData:
    """One frame read with one layout (default `total`). Jobs use an OsdReader."""
    if not _init_ocr():
        return OSDData()
    return OsdReader(forced=layout, voter=voter or GpsVoter()).read(frame)
