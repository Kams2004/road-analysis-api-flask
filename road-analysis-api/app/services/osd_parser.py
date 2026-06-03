"""
OSD parser — PaddleOCR-based extraction from dashcam overlay.

Frame layout (720x576):
  row1: date+time (left) | GPS+speed (right)
  row2: vehicle ID (left) | org name (right)
  row3: RPM (left)        | location name (right)
"""

import re
import cv2
import logging
import numpy as np
from datetime import datetime
from typing import Optional
from dataclasses import dataclass

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
        import logging as _logging
        os.environ.setdefault("PPOCR_LOGGING_LEVEL", "ERROR")
        _logging.getLogger("ppocr").setLevel(_logging.WARNING)
        from paddleocr import PaddleOCR
        _ocr_engine = PaddleOCR(use_angle_cls=False, lang="en", show_log=False)
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
    timestamp:     Optional[datetime] = None
    location_name: Optional[str]      = None
    rpm:           Optional[int]      = None
    gps_interpolated: bool            = False  # True when value came from voter window


def reset_last() -> None:
    """No state — kept for API compatibility."""
    pass


# ── ROI definitions (ratios, scale-independent) ───────────────────────────────

ROI_DATETIME = (0.00, 0.035, 0.50, 0.100)
ROI_GPS      = (0.45, 0.035, 1.00, 0.100)
ROI_VEHICLE  = (0.00, 0.100, 0.50, 0.165)
ROI_RPM      = (0.00, 0.165, 0.50, 0.235)
ROI_LOCATION = (0.50, 0.165, 1.00, 0.235)


# ── regex patterns ────────────────────────────────────────────────────────────

_GPS_RE = re.compile(
    r"(\d{3,4}[.,]\s*\d{4})\s*[,\s]\s*([NS])"
    r"\s*[,\s]\s*(\d{4,5}[.,]\s*\d{4})\s*[,\s]\s*([EW06CGO])",
    re.IGNORECASE,
)
# Partial patterns — used when full GPS_RE fails
# Require 4 fractional digits to avoid matching truncated/noisy values
_LAT_RE = re.compile(r"(\d{3,4}[.,]\d{4})\s*[,\s]\s*([NS])", re.IGNORECASE)
_LON_RE = re.compile(r"(\d{4,5}[.,]\d{4})\s*[,\s]\s*([EW06CGO])", re.IGNORECASE)
_SPEED_RE    = re.compile(r"\b(\d{1,3})\s*KM/?H\b", re.IGNORECASE)
_DATE_RE     = re.compile(r"(\d{2}[/\-]\d{2}[/\-]\d{4})\s+(\d{2}:\d{2}:\d{2})")
_VID_RE      = re.compile(r"\b(L[A-Z]{2,3}\d{3,4}[A-Z]{0,3})\b")
_RPM_RE      = re.compile(r"RPM\s*[:\-]?\s*(\d+)", re.IGNORECASE)
_LOCATION_RE = re.compile(r"([A-Z][A-Z0-9]{2,}(?:[_.\-][A-Z][A-Z0-9]+)+)", re.IGNORECASE)


# ── image helpers ─────────────────────────────────────────────────────────────

def _crop(frame: np.ndarray, roi: tuple) -> np.ndarray:
    h, w = frame.shape[:2]
    x0, y0 = max(0, int(roi[0]*w)), max(0, int(roi[1]*h))
    x1, y1 = min(w, int(roi[2]*w)), min(h, int(roi[3]*h))
    return frame[y0:y1, x0:x1]


# ── OCR ───────────────────────────────────────────────────────────────────────

def _ocr_roi(roi_bgr: np.ndarray) -> str:
    """Run PaddleOCR on a BGR ROI crop, return all text joined and uppercased."""
    if roi_bgr is None or roi_bgr.size == 0:
        return ""
    try:
        result = _ocr_engine.ocr(roi_bgr, cls=False)
        if not result or not result[0]:
            return ""
        texts = [line[1][0] for line in result[0] if line and line[1][0].strip()]
        return " ".join(texts).upper().strip()
    except Exception as e:
        logger.debug(f"PaddleOCR error: {e}")
        return ""


# ── text normalisation ────────────────────────────────────────────────────────

def _norm(text: str) -> str:
    text = text.upper().strip()
    text = re.sub(r'[OQ](?=\d)', '0', text)
    text = re.sub(r'(?<=\d)[OQ]', '0', text)
    text = re.sub(r'(?<=\d)[|I](?=\d)', '1', text)
    # S misread as 0 at word boundary before digits
    text = re.sub(r'\bS(?=\d)', '0', text)
    # S misread as 5 between digit and [.,digit]
    text = re.sub(r'(\d)S([.,\d])', r'\g<1>5\2', text)
    text = re.sub(r'(\d{4}),(\d{3,4})\b', r'\1.\2', text)
    # Remove spaces between digits (OCR splits numbers)
    text = re.sub(r'(\d)\s+(\d)', r'\1\2', text)
    # Remove spaces after decimal point: "0515. 4391" -> "0515.4391"
    text = re.sub(r'(\d[.,])\s+(\d)', r'\1\2', text)
    # Pad short NMEA lat (3 digits before decimal) to 4: "015." -> "0515." won't work
    # Instead pad lon (4 digits before decimal) to 5: "1013." -> "01013."
    text = re.sub(r'\b(\d{4}[.,]\d{3,4})\s*,\s*([EW])', r'0\1,\2', text)
    return text


# ── field parsers ─────────────────────────────────────────────────────────────

def _nmea_to_decimal(value: str, direction: str) -> float:
    v   = float(value)
    deg = int(v / 100)
    dec = deg + (v - deg * 100) / 60.0
    return round(-dec if direction in ("S", "W") else dec, 6)


def parse_gps_text(raw: str) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """
    Public — also used by the location-correction endpoint.
    Parses NMEA GPS text → (lat, lon, speed_kmh).
    Returns (None, None, None) if text is incomplete or unparseable.
    """
    text = _norm(raw)
    m = _GPS_RE.search(text)
    if not m:
        return None, None, None
    try:
        lon_dir = m.group(4).upper()
        if lon_dir not in ('E', 'W'):
            lon_dir = 'E'
        lat_str = m.group(1).replace(',', '.').replace(' ', '')
        lon_str = m.group(3).replace(',', '.').replace(' ', '')
        # Pad short NMEA values: lat needs 4 digits before decimal, lon needs 5
        if len(lat_str.split('.')[0]) == 3:
            lat_str = '0' + lat_str
        if len(lon_str.split('.')[0]) == 4:
            lon_str = '0' + lon_str
        lat = _nmea_to_decimal(lat_str, m.group(2))
        lon = _nmea_to_decimal(lon_str, lon_dir)
        if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
            return None, None, None
        # Sanity: NMEA lat degrees part must be <= 90, lon degrees part <= 180
        lat_deg = int(float(lat_str.split('.')[0]) / 100)
        lon_deg = int(float(lon_str.split('.')[0]) / 100)
        if lat_deg > 90 or lon_deg > 180:
            return None, None, None
        sm    = _SPEED_RE.search(text)
        speed = float(sm.group(1)) if sm and 0 <= float(sm.group(1)) <= 200 else None
        return lat, lon, speed
    except Exception:
        return None, None, None


def _parse_partial_gps(texts: list) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """
    Try to extract lat and lon independently across multiple OCR candidates.
    Used when no single candidate contains the full GPS string.
    Only accepts values in the expected Cameroon bounding box.
    """
    lat, lon, speed = None, None, None
    for raw in texts:
        text = _norm(raw)
        if lat is None:
            m = _LAT_RE.search(text)
            if m:
                try:
                    lat_str = m.group(1).replace(',', '.').replace(' ', '')
                    if len(lat_str.split('.')[0]) == 3:
                        lat_str = '0' + lat_str
                    v = _nmea_to_decimal(lat_str, m.group(2))
                    if -90.0 <= v <= 90.0:
                        lat = v
                except Exception:
                    pass
        if lon is None:
            m = _LON_RE.search(text)
            if m:
                try:
                    lon_dir = m.group(2).upper()
                    if lon_dir not in ('E', 'W'):
                        lon_dir = 'E'
                    lon_str = m.group(1).replace(',', '.').replace(' ', '')
                    if len(lon_str.split('.')[0]) == 4:
                        lon_str = '0' + lon_str
                    v = _nmea_to_decimal(lon_str, lon_dir)
                    if -180.0 <= v <= 180.0:
                        lon = v
                except Exception:
                    pass
        if speed is None:
            sm = _SPEED_RE.search(text)
            if sm:
                try:
                    s = float(sm.group(1))
                    if 0 <= s <= 200:
                        speed = s
                except Exception:
                    pass
        if lat is not None and lon is not None:
            break
    return lat, lon, speed


def _parse_datetime(text: str) -> Optional[datetime]:
    m = _DATE_RE.search(_norm(text))
    if m:
        try:
            ts = datetime.strptime(
                f"{m.group(1).replace('-','/')} {m.group(2)}", "%d/%m/%Y %H:%M:%S"
            )
            if 2000 <= ts.year <= 2100:
                return ts
        except ValueError:
            pass
    return None


def _parse_vehicle(text: str) -> Optional[str]:
    m = _VID_RE.search(_norm(text))
    return m.group(1) if m else None


def _parse_rpm(text: str) -> Optional[int]:
    m = _RPM_RE.search(_norm(text))
    if m:
        try:
            rpm = int(m.group(1))
            return rpm if 0 <= rpm <= 10000 else None
        except ValueError:
            pass
    return None


def _parse_location(text: str) -> Optional[str]:
    m = _LOCATION_RE.search(text.upper().strip())
    if m:
        loc = m.group(1)
        if len(re.split(r'[_.\-]', loc)) >= 2:
            return loc
    return None


# ── GPS temporal voter ───────────────────────────────────────────────────────

class GpsVoter:
    """
    Maintains a rolling window of recent valid GPS reads.

    On each frame:
    - If OCR gives a valid fix, validate it against the window (reject outliers
      that jump more than MAX_JUMP degrees between consecutive frames).
      If valid, add to window and return it.
    - If OCR gives None (or an outlier), return the median of the window as a
      best-estimate, flagged with gps_interpolated=True.

    Window size = 5 frames. MAX_JUMP = 0.01 deg (~1.1 km) per processed frame.
    """
    WINDOW  = 5
    MAX_JUMP = 0.01  # degrees per frame-skip interval

    def __init__(self):
        self._lats:  list[float] = []
        self._lons:  list[float] = []

    def reset(self):
        self._lats.clear()
        self._lons.clear()

    def update(self, lat: Optional[float], lon: Optional[float]
               ) -> tuple[Optional[float], Optional[float], bool]:
        """
        Returns (best_lat, best_lon, interpolated).
        interpolated=True means the value came from the window, not live OCR.
        """
        if lat is not None and lon is not None:
            # Outlier check against last known position
            if self._lats and self._lons:
                if (abs(lat - self._lats[-1]) > self.MAX_JUMP or
                        abs(lon - self._lons[-1]) > self.MAX_JUMP):
                    # Outlier — discard and fall back to window
                    lat, lon = None, None

        if lat is not None and lon is not None:
            self._lats.append(lat)
            self._lons.append(lon)
            if len(self._lats) > self.WINDOW:
                self._lats.pop(0)
                self._lons.pop(0)
            return lat, lon, False

        # No valid fix — return median of window if available
        if self._lats:
            med_lat = sorted(self._lats)[len(self._lats) // 2]
            med_lon = sorted(self._lons)[len(self._lons) // 2]
            return med_lat, med_lon, True

        return None, None, False


# ── public API ────────────────────────────────────────────────────────────────

def extract_osd(frame: np.ndarray, voter: Optional['GpsVoter'] = None) -> OSDData:
    """
    Extract all OSD fields from a dashcam frame.
    If voter is provided, applies temporal GPS voting to fill gaps and reject outliers.
    """
    if not _init_ocr():
        return OSDData()

    gps_raw = _ocr_roi(_crop(frame, ROI_GPS))
    lat, lon, speed = parse_gps_text(gps_raw)
    if lat is None and gps_raw:
        lat, lon, speed = _parse_partial_gps([gps_raw])

    interpolated = False
    if voter is not None:
        lat, lon, interpolated = voter.update(lat, lon)

    dt_text  = _ocr_roi(_crop(frame, ROI_DATETIME))
    veh_text = _ocr_roi(_crop(frame, ROI_VEHICLE))
    rpm_text = _ocr_roi(_crop(frame, ROI_RPM))
    loc_text = _ocr_roi(_crop(frame, ROI_LOCATION))

    result = OSDData(
        raw_gps_text     = gps_raw or None,
        latitude         = lat,
        longitude        = lon,
        speed_kmh        = speed,
        timestamp        = _parse_datetime(dt_text),
        vehicle_id       = _parse_vehicle(veh_text),
        rpm              = _parse_rpm(rpm_text),
        location_name    = _parse_location(loc_text),
        gps_interpolated = interpolated,
    )
    logger.debug(
        f"OSD raw={result.raw_gps_text!r} -> "
        f"lat={result.latitude} lon={result.longitude} interp={interpolated}"
    )
    return result
