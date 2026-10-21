"""
OSD layouts — data-driven dashcam overlay profiles + pure text parsing (RRAP-05-014).

Standard library only (no cv2 / OCR): the pothole dataset kit keeps a verbatim copy of
this file (ai/training/pothole-dataset/potholeds/osd/layouts.py) — change both together.

A profile mirrors the OSD configuration screen of a dashcam equipment: every overlay
text field has a kind, an enabled flag and the X/Y of its top-left anchor on the
device's 0–1000 grid (per-mille of the frame width / height). The OCR region of a
field is derived from its anchor (see `derive_rois`):

    x0 = x − x_margin          y0 = y − y_margin
    x1 = x of the next enabled field on the same line (else 1000, or x + width)
    y1 = y + line_height

Built-in profiles (see docs/OSD-LAYOUTS.md):
  total   — "TOTAL" equipment (CAMTRACK-style, small text at the top):
            '24/04/2026 08:33:09[ACC:ON]'   '0515.3490,N,01013.5519,E,016KM/H'
            'LTTR458AV'                     'CAMTRAD'
            'RPM:0r/min'                    'BOUBAKARI_BABIGUIR.CAMTRAD'
  besafe  — "Besafe" equipment (large text, GPS on top, date + plate at the bottom):
            '0348.2130,N,01014.8332,E,051KM/H ACC:ON'
            'ID: Name:'
            '20-10-2026 14:29:42'           'LTTR704AC CAMACO'

  ola_energy — "Dashcam mainly used at Ola Energy" (Besafe-like, the bottom-right field is
            the camera channel 'CH2' instead of the plate) — no sample video verified yet.

Field kinds: date_time, gps, vehicle_no, user_def, rpm, driver_id, driver_name, channel
(channel is parsed — 'CH2' — but never used as the vehicle id).
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Optional

FIELD_KINDS = ("date_time", "gps", "vehicle_no", "user_def", "rpm", "driver_id", "driver_name", "channel")
# fields that do not change during a recording: once read the same way several times their
# region is not OCR'd any more (performance)
STATIC_KINDS = frozenset({"vehicle_no", "user_def", "driver_id", "driver_name", "channel"})
DEFAULT_DATE_FORMATS = ("DD/MM/YYYY", "DD-MM-YYYY", "YYYY-MM-DD")
AUTO = "auto"
FULL_FRAME = "full_frame"   # fallback pseudo-profile: whole frame OCR + regex search
NO_OSD = "none"             # no overlay found: OCR stopped for the job

# Cameroon (with margin) — fixes outside are OCR garbage (settings osd.gps_box)
CAMEROON_BOX = (1.0, 13.5, 8.0, 16.5)   # lat_min, lat_max, lon_min, lon_max

BUILTIN_PROFILES: dict[str, dict] = {
    "total": {
        "label": "TOTAL (small text, top)",
        "aliases": ["camtrack_top", "camtrack"],
        "line_height": 50,
        "fields": [
            {"field": "date_time",   "enabled": True,  "x": 50,  "y": 50},
            {"field": "gps",         "enabled": True,  "x": 500, "y": 50},
            {"field": "vehicle_no",  "enabled": True,  "x": 50,  "y": 100},
            {"field": "driver_id",   "enabled": False, "x": 150, "y": 100},
            {"field": "user_def",    "enabled": True,  "x": 500, "y": 100},
            {"field": "rpm",         "enabled": True,  "x": 50,  "y": 150},
            {"field": "driver_name", "enabled": True,  "x": 500, "y": 150},
        ],
    },
    "besafe": {
        "label": "Besafe (large text, date + plate at the bottom)",
        "aliases": ["camaco_big", "camaco"],
        "line_height": 65,
        "fields": [
            {"field": "gps",         "enabled": True, "x": 20,  "y": 40},
            {"field": "driver_name", "enabled": True, "x": 20,  "y": 100},
            {"field": "date_time",   "enabled": True, "x": 20,  "y": 900},
            {"field": "vehicle_no",  "enabled": True, "x": 500, "y": 900},
        ],
    },
    "ola_energy": {
        "label": "Ola Energy dashcam (large text, date + channel at the bottom)",
        "aliases": ["ola"],
        "line_height": 65,
        "fields": [
            {"field": "gps",         "enabled": True, "x": 20,  "y": 50},
            {"field": "driver_name", "enabled": True, "x": 20,  "y": 100},
            {"field": "date_time",   "enabled": True, "x": 20,  "y": 900},
            {"field": "channel",     "enabled": True, "x": 500, "y": 900},
        ],
    },
}


# ── profiles ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Roi:
    """OCR region of one overlay field, as frame ratios (x0, y0, x1, y1)."""
    field: str
    box: tuple[float, float, float, float]

    @property
    def static(self) -> bool:
        return self.field in STATIC_KINDS


@dataclass
class Profile:
    name: str
    label: str
    rois: list[Roi]
    date_formats: tuple[str, ...] = DEFAULT_DATE_FORMATS
    gps_regex: Optional[str] = None
    plate_regex: Optional[str] = None
    timezone: Optional[str] = None
    detect: bool = True                 # candidate of the auto-detection
    aliases: tuple[str, ...] = ()
    builtin: bool = False

    @property
    def kinds(self) -> set[str]:
        return {r.field for r in self.rois}


def derive_rois(fields: Iterable[dict], line_height: float = 50, x_margin: float = 20,
                y_margin: float = 15) -> list[Roi]:
    """OCR regions of the enabled fields from their anchors (0–1000 grid)."""
    on = [f for f in fields if f.get("enabled", True)]
    out = []
    for f in on:
        x, y = float(f["x"]), float(f["y"])
        lh = float(f.get("line_height") or line_height)
        if f.get("width"):
            x1 = x + float(f["width"])
        else:
            nxt = [float(g["x"]) for g in on if g is not f and float(g["x"]) > x
                   and abs(float(g["y"]) - y) < lh / 2]
            x1 = min(nxt) if nxt else 1000.0
        box = (max(0.0, x - x_margin) / 1000, max(0.0, y - y_margin) / 1000,
               min(1000.0, x1) / 1000, min(1000.0, y + lh) / 1000)
        out.append(Roi(f["field"], tuple(round(v, 4) for v in box)))  # type: ignore[arg-type]
    return out


def compile_profile(name: str, spec: dict, builtin: bool = False) -> Profile:
    fields = spec.get("fields") or []
    for f in fields:
        if f.get("field") not in FIELD_KINDS:
            raise ValueError(f"profile {name!r}: unknown field kind {f.get('field')!r}")
    return Profile(
        name=name, label=spec.get("label") or name,
        rois=derive_rois(fields, spec.get("line_height") or 50, spec.get("x_margin", 20), spec.get("y_margin", 15)),
        date_formats=tuple(spec.get("date_formats") or DEFAULT_DATE_FORMATS),
        gps_regex=spec.get("gps_regex") or None, plate_regex=spec.get("plate_regex") or None,
        timezone=spec.get("timezone") or None, detect=spec.get("detect", True),
        aliases=tuple(spec.get("aliases") or ()), builtin=builtin,
    )


def full_frame_profile() -> Profile:
    """Fallback: one region = the whole frame, every field searched by regex."""
    return Profile(name=FULL_FRAME, label="Full frame (fallback)", rois=[Roi("any", (0.0, 0.0, 1.0, 1.0))],
                   detect=False)


def effective_profiles(custom: Optional[dict[str, dict]] = None) -> dict[str, Profile]:
    """Built-in profiles + the configured ones (same name or alias → replaces the built-in)."""
    specs = {k: dict(v) for k, v in BUILTIN_PROFILES.items()}
    overridden = set()
    for name, spec in (custom or {}).items():
        target = resolve_name(name, specs) or name
        if target in BUILTIN_PROFILES and not spec.get("aliases"):
            spec = {**spec, "aliases": BUILTIN_PROFILES[target].get("aliases", [])}
        specs[target] = spec
        overridden.add(target)
    return {n: compile_profile(n, s, builtin=n in BUILTIN_PROFILES and n not in overridden)
            for n, s in specs.items()}


def resolve_name(name: Optional[str], profiles: dict) -> Optional[str]:
    """Profile name of a name or alias (case-insensitive), `auto` / None → None, unknown → None."""
    if not name:
        return None
    key = name.strip().lower()
    if key == AUTO:
        return None
    for n, p in profiles.items():
        aliases = p.aliases if isinstance(p, Profile) else (p or {}).get("aliases", [])
        if key == n.lower() or key in [a.lower() for a in aliases]:
            return n
    return None


# ── text normalisation ───────────────────────────────────────────────────────

def norm(text: str) -> str:
    """OCR normalisation of GPS-like text (letters ↔ digits, split numbers, dropped decimal dot)."""
    text = (text or "").upper().strip()
    text = re.sub(r"[OQD](?=\d)", "0", text)
    text = re.sub(r"(?<=\d)[OQD]", "0", text)
    text = re.sub(r"(?<=\d)[|I](?=\d)", "1", text)
    text = re.sub(r"\bS(?=\d)", "0", text)
    text = re.sub(r"(\d)S([.,\d])", r"\g<1>5\2", text)
    text = re.sub(r"(\d{4}),(\d{3,4})\b", r"\1.\2", text)
    text = re.sub(r"(\d)\s+(\d)", r"\1\2", text)
    text = re.sub(r"(\d[.,])\s+(\d)", r"\1\2", text)
    # dropped decimal dot: 8 digits before N/S (ddmm mmmm), 9 digits before E/W (dddmm mmmm)
    text = re.sub(r"(?<![\d.,])(\d{4})(\d{4})(?=[.,\s]*[NS])", r"\1.\2", text)
    text = re.sub(r"(?<![\d.,])(\d{5})(\d{4})(?=[.,\s]*[EW])", r"\1.\2", text)
    text = re.sub(r"\b(\d{4}[.,]\d{3,4})\s*,\s*([EW])", r"0\1,\2", text)
    return text


def fix_separators(raw: str) -> str:
    """Dots or nothing used as field separators → commas (decimal dots untouched):
    '0409.2839.N.01131.9380.E.039KM/H' and '0515.3177N01013.5650E035KM/H' → comma separated."""
    r = re.sub(r"(?<=\d)\.(?=[A-Za-z])", ",", raw or "")
    r = re.sub(r"(?<=[A-Za-z])\.(?=[\dA-Za-z])", ",", r)
    r = re.sub(r"(\d)([NS])(\d)", r"\1,\2,\3", r)
    r = re.sub(r"(\d)([EW])(\d)", r"\1,\2,\3", r)
    return r


# ── GPS ──────────────────────────────────────────────────────────────────────

_GPS_RE = re.compile(
    r"(\d{3,4}[.,]\s*\d{4})\s*[,\s]?\s*([NS])"
    r"\s*[,\s]?\s*(\d{4,5}[.,]\s*\d{4})\s*[,\s]?\s*([EW06CGO])",
    re.IGNORECASE,
)
_LAT_RE = re.compile(r"(\d{3,4}[.,]\d{4})\s*[,\s]?\s*([NS])", re.IGNORECASE)
_LON_RE = re.compile(r"(\d{4,5}[.,]\d{4})\s*[,\s]?\s*([EW])", re.IGNORECASE)
# OSD speed is zero-padded (016KM/H, 051KM/H): 3 digits required (1–2 digits = misread);
# read right after the E/W of the longitude, else before 'KM' (OCR: 'KM H', 'KM/', 'HMH', 'KM/L' …)
_SPEED_AFTER_EW = re.compile(r"[EW]\s*[,.]?\s*([0-9OQ]{3})(?![0-9])")
_SPEED_RE = re.compile(r"(?<![\d.])([0-9OQ]{3})\s*[KRH]\s*M", re.IGNORECASE)


def nmea_to_decimal(value: str, direction: str) -> float:
    v = float(value)
    deg = int(v / 100)
    dec = deg + (v - deg * 100) / 60.0
    return round(-dec if direction.upper() in ("S", "W") else dec, 6)


def _pad(lat_str: str, lon_str: str) -> tuple[str, str]:
    lat_str = lat_str.replace(",", ".").replace(" ", "")
    lon_str = lon_str.replace(",", ".").replace(" ", "")
    if len(lat_str.split(".")[0]) == 3:
        lat_str = "0" + lat_str
    if len(lon_str.split(".")[0]) == 4:
        lon_str = "0" + lon_str
    return lat_str, lon_str


def in_box(lat: float, lon: float, box) -> bool:
    if not box:
        return True
    return box[0] <= lat <= box[1] and box[2] <= lon <= box[3]


def parse_speed(text: str) -> Optional[float]:
    t = norm(text)
    for rx in (_SPEED_AFTER_EW, _SPEED_RE):
        m = rx.search(t)
        if m:
            v = float(m.group(1).replace("O", "0").replace("Q", "0"))
            if 0 <= v <= 200:
                return v
    return None


def _repairs(num: str, box) -> list[str]:
    """The reading + (with a plausibility box) the leading digit read as 0 — a leading 0
    is often OCR'd as 9 or 4 ('9407.0866' → '0407.0866')."""
    return [num, "0" + num[1:]] if box and num[:1] not in ("0", "") else [num]


def _from_parts(lat_s: str, ns: str, lon_s: str, ew: str, box) -> tuple[Optional[float], Optional[float]]:
    ew = ew.upper() if ew.upper() in ("E", "W") else "E"
    lat_str, lon_str = _pad(lat_s, lon_s)
    for la in _repairs(lat_str, box):
        for lo in _repairs(lon_str, box):
            if int(float(la.split(".")[0]) / 100) > 90 or int(float(lo.split(".")[0]) / 100) > 180:
                continue
            if float(la) % 100 >= 60 or float(lo) % 100 >= 60:       # minutes must be < 60
                continue
            lat, lon = nmea_to_decimal(la, ns), nmea_to_decimal(lo, ew)
            if in_box(lat, lon, box):
                return lat, lon
    return None, None


def parse_gps_text(raw: str, box=None, gps_regex: Optional[str] = None
                   ) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """NMEA-like OSD text → (lat, lon, speed_kmh) or (None, None, None). Tolerates dots / no
    separators, a dropped decimal dot, a lost leading 0, O/Q/D read for 0, trailing 'ACC:ON'.
    `gps_regex` (profile): custom pattern with named groups lat, ns, lon, ew (+ optional speed)."""
    if not raw:
        return None, None, None
    if gps_regex:
        m = (re.search(gps_regex, raw.upper(), re.IGNORECASE)
             or re.search(gps_regex, norm(fix_separators(raw)), re.IGNORECASE))
        if m:
            try:
                lat, lon = _from_parts(m.group("lat"), m.group("ns"), m.group("lon"), m.group("ew"), box)
                if lat is not None:
                    gd = m.groupdict()
                    sp = float(gd["speed"]) if gd.get("speed") else parse_speed(raw)
                    return lat, lon, sp if sp is None or 0 <= sp <= 200 else None
            except (ValueError, IndexError):
                pass
    m, text = None, ""
    for cand in (fix_separators(raw), raw, fix_separators(norm(raw))):
        text = norm(cand)
        m = _GPS_RE.search(text)
        if m:
            break
    if not m:
        return None, None, None
    try:
        lat, lon = _from_parts(m.group(1), m.group(2), m.group(3), m.group(4), box)
    except (ValueError, IndexError):
        return None, None, None
    if lat is None:
        return None, None, None
    return lat, lon, parse_speed(text)


def parse_partial(raw: str, box=None) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Lat and lon found independently (when the full pattern does not match)."""
    text = norm(fix_separators(raw or ""))
    lat = lon = None
    m = _LAT_RE.search(text)
    if m:
        s, _ = _pad(m.group(1), "00000.0000")
        try:
            lat = nmea_to_decimal(s, m.group(2)) if float(s) % 100 < 60 else None
        except ValueError:
            lat = None
    m = _LON_RE.search(text)
    if m:
        _, s = _pad("0000.0000", m.group(1))
        try:
            lon = nmea_to_decimal(s, m.group(2)) if float(s) % 100 < 60 else None
        except ValueError:
            lon = None
    if lat is None or lon is None or not in_box(lat, lon, box):
        return None, None, parse_speed(text)
    return lat, lon, parse_speed(text)


# ── date & time ──────────────────────────────────────────────────────────────

_DATE_TOKENS = {"DD": r"(?P<d>[0-3]\d)", "MM": r"(?P<m>[01]\d)", "YYYY": r"(?P<y>20\d\d)", "YY": r"(?P<yy>\d\d)"}
_DATE_SEP = r"\s?[/\-.\\|]\s?"
_TIME_GAP = set(" :.;,'-_")
_date_res: dict[str, re.Pattern] = {}


def _date_regex(fmt: str) -> re.Pattern:
    if fmt not in _date_res:
        parts = re.findall(r"YYYY|YY|DD|MM|[^YMD]+", fmt.upper())
        _date_res[fmt] = re.compile(r"(?<!\d)" + "".join(_DATE_TOKENS.get(p, _DATE_SEP) for p in parts))
    return _date_res[fmt]


def _dt_norm(text: str) -> str:
    t = (text or "").upper()
    t = re.sub(r"[OQD](?=\d)", "0", t)
    t = re.sub(r"(?<=\d)[OQD]", "0", t)
    return re.sub(r"(?<=\d)[I|L](?=\d)", "1", t)


def _time_after(text: str, i: int) -> Optional[str]:
    """The 6 digits HHMMSS following position i, whatever OCR made of the separators
    ('08:33:09', '0833.09', '083309', '08 33:09' …)."""
    digits, gap = "", 0
    for ch in text[i:i + 24]:
        if ch.isdigit():
            digits += ch
            gap = 0
            if len(digits) == 6:
                return digits
        elif ch in _TIME_GAP and gap < 3:
            gap += 1
        else:
            break
    return None


_TOD_RE = re.compile(r"([0-2]\d)\s?[:.]\s?([0-5]\d)\s?[:.]\s?([0-5]\d)(?!\d)")


def parse_time_of_day(text: str) -> Optional[tuple[int, int, int]]:
    """Time of day alone ('…-202612:48:56' with a broken date) → (h, m, s) or None."""
    m = _TOD_RE.search(_dt_norm(text))
    if m and int(m.group(1)) < 24:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    return None


def parse_datetime(text: str, formats: Iterable[str] = DEFAULT_DATE_FORMATS) -> Optional[datetime]:
    """OSD date + time ('24/04/2026 08:33:09', '20-10-2026 14:29:42', '2026-10-20 14:29:42' …)
    → naive datetime (camera local time) or None. Separators may be lost or misread by OCR."""
    t = _dt_norm(text)
    for fmt in formats:
        for m in _date_regex(fmt).finditer(t):
            hms = _time_after(t, m.end())
            if not hms:
                continue
            g = m.groupdict()
            year = int(g["y"]) if g.get("y") else 2000 + int(g["yy"])
            try:
                ts = datetime(year, int(g["m"]), int(g["d"]), int(hms[:2]), int(hms[2:4]), int(hms[4:]))
            except ValueError:
                continue
            if 2000 <= ts.year <= 2100:
                return ts
    return None


# ── plate, company, driver, rpm ──────────────────────────────────────────────

_TO_DIGIT = {"O": "0", "Q": "0", "D": "0", "I": "1", "L": "1", "Z": "2", "S": "5", "G": "6", "B": "8", "T": "7"}
_TO_LETTER = {"0": "O", "1": "I", "2": "Z", "5": "S", "6": "G", "8": "B"}
_RPM_RE = re.compile(r"RPM\s*[:\-.]?\s*(\d+)", re.IGNORECASE)
_WORD_RE = re.compile(r"[A-Z][A-Z0-9&_.\-]{2,}")
_STOP = {"ACC", "ON", "OFF", "ID", "NAME", "RPM", "KM", "KMH", "MIN", "GPS", "N", "E", "S", "W"}


def _plate_token(tok: str) -> tuple[Optional[str], int]:
    """Best reading of a token as letters(2–4) + digits(3–4) + letters(0–3) → (plate, substitutions)."""
    best: tuple[Optional[str], int] = (None, 99)
    n = len(tok)
    for a in range(2, 5):
        for b in range(3, 5):
            c = n - a - b
            if not 0 <= c <= 3:
                continue
            subs, out = 0, []
            for i, ch in enumerate(tok):
                want_digit = a <= i < a + b
                if want_digit and not ch.isdigit():
                    ch2 = _TO_DIGIT.get(ch)
                elif not want_digit and not ch.isalpha():
                    ch2 = _TO_LETTER.get(ch)
                else:
                    ch2 = ch
                if ch2 is None:
                    break
                subs += ch2 != ch
                out.append(ch2)
            else:
                if tok[0].isalpha() and sum(ch.isdigit() for ch in tok) >= 2 and subs < best[1]:
                    best = ("".join(out), subs)
    return best


# Cameroon registration region codes (first two letters) — a plate starting otherwise
# gets one look-alike letter fixed when that gives a known code ('LITR185AS' → 'LTTR185AS')
PLATE_REGIONS = frozenset({"AD", "CE", "EN", "ES", "LT", "NO", "NW", "OU", "SU", "SW", "CA", "IT", "CD", "CMR"})
_LOOKALIKE = {"I": "T", "T": "I", "1": "T", "J": "T", "0": "O", "D": "O", "Q": "O", "U": "O", "5": "S",
              "F": "E", "H": "N", "V": "W", "C": "O"}


def _fix_region(plate: str) -> str:
    if plate[:2] in PLATE_REGIONS:
        return plate
    for i in (1, 0):
        alt = _LOOKALIKE.get(plate[i])
        if alt:
            cand = plate[:i] + alt + plate[i + 1:]
            if cand[:2] in PLATE_REGIONS:
                return cand
    return plate


def parse_plate(text: str, plate_regex: Optional[str] = None) -> Optional[str]:
    """Vehicle registration (e.g. LTTR458AV, LTTR704AC) — OCR confusions fixed by position."""
    t = (text or "").upper()
    if plate_regex:
        m = re.search(plate_regex, t)
        if m:
            return (m.group(1) if m.groups() else m.group(0)).replace(" ", "")
        return None
    best: tuple[Optional[str], int] = (None, 99)
    for tok in re.findall(r"[A-Z0-9]{5,11}", t):
        plate, subs = _plate_token(tok)
        if plate and subs <= 2 and subs < best[1]:
            best = (plate, subs)
    return _fix_region(best[0]) if best[0] else None


def parse_company(text: str, after_plate: bool = False, plate_regex: Optional[str] = None) -> Optional[str]:
    """Company / user-defined text: the word after the plate (Besafe 'LTTR704AC CAMACO'),
    or the first plain word of the region (TOTAL user_def 'CAMTRAD')."""
    t = (text or "").upper()
    if after_plate:
        plate_tok = None
        for tok in re.findall(r"[A-Z0-9]{5,11}", t):
            if parse_plate(tok, plate_regex):
                plate_tok = tok
                break
        if plate_tok is None:
            return None
        t = t[t.index(plate_tok) + len(plate_tok):]
    for w in _WORD_RE.findall(t):
        w = w.strip("._-")
        if len(w) >= 3 and w not in _STOP and not re.search(r"\d", w):
            return w
    return None


def parse_rpm(text: str) -> Optional[int]:
    m = _RPM_RE.search(_dt_norm(text))
    if m:
        rpm = int(m.group(1))
        return rpm if 0 <= rpm <= 10000 else None
    return None


def parse_driver_name(text: str) -> Optional[str]:
    """Driver name ('BOUBAKARI_BABIGUIR.CAMTRAD', or Besafe 'ID: 123 Name: JOHN')."""
    t = (text or "").upper().strip()
    m = re.search(r"NAME\s*[:.]?\s*(.*)$", t)
    if m:
        t = m.group(1)
    t = re.sub(r"\bI\s?D\s*[:.]\s*\S*", " ", t).strip(" :|")
    t = re.sub(r"\s+", " ", t)
    return t if len(t) >= 3 and re.search(r"[A-Z]{3}", t) else None


_CHANNEL_RE = re.compile(r"(?<![A-Z0-9])C\s?H\s*[-:.]?\s*([0-9OQ]{1,2})(?![0-9])")


def parse_channel(text: str) -> Optional[str]:
    """Camera channel ('CH2', 'CH 02') → 'CH2'."""
    m = _CHANNEL_RE.search((text or "").upper())
    return f"CH{int(m.group(1).replace('O', '0').replace('Q', '0'))}" if m else None


def parse_driver_id(text: str) -> Optional[str]:
    t = (text or "").upper()
    m = re.search(r"I\s?D\s*[:.]?\s*([A-Z0-9\-]{2,})", t)
    if m and m.group(1) != "NAME":
        return m.group(1)
    return None


# ── one frame ────────────────────────────────────────────────────────────────

@dataclass
class FrameRead:
    """Fields parsed from the OCR texts of one frame with one profile."""
    raw_gps_text: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    speed_kmh: Optional[float] = None
    timestamp: Optional[datetime] = None
    vehicle_id: Optional[str] = None
    company: Optional[str] = None
    rpm: Optional[int] = None
    driver_name: Optional[str] = None
    driver_id: Optional[str] = None
    channel: Optional[str] = None
    texts: dict = field(default_factory=dict)

    @property
    def score(self) -> float:
        """Auto-detection score: valid GPS fix + valid date-time (+ a little for a plate or a
        channel, which tells apart layouts that share GPS / time positions, e.g. besafe / ola_energy)."""
        return ((self.latitude is not None) + (self.timestamp is not None)
                + 0.25 * (self.vehicle_id is not None) + 0.25 * (self.channel is not None))


def parse_field(kind: str, text: str, profile: Profile, read: FrameRead, box=CAMEROON_BOX) -> None:
    """Parse the OCR text of one region into `read` (fields already set are kept)."""
    if not text:
        return
    every = kind == "any"
    if kind == "gps" or every:
        lat, lon, speed = parse_gps_text(text, box, profile.gps_regex)
        if lat is None and kind == "gps":
            lat, lon, speed = parse_partial(text, box)
        if read.latitude is None and lat is not None:
            read.latitude, read.longitude = lat, lon
            read.raw_gps_text = text
        if read.speed_kmh is None:
            read.speed_kmh = speed if speed is not None else parse_speed(text)
        if kind == "gps" and read.raw_gps_text is None:
            read.raw_gps_text = text
    if (kind == "date_time" or every) and read.timestamp is None:
        read.timestamp = parse_datetime(text, profile.date_formats)
    if (kind == "vehicle_no" or every) and read.vehicle_id is None:
        read.vehicle_id = parse_plate(text, profile.plate_regex)
        if read.company is None and read.vehicle_id:
            read.company = parse_company(text, after_plate=True, plate_regex=profile.plate_regex)
    if kind == "user_def" and read.company is None:
        read.company = parse_company(text)
    if (kind == "rpm" or every) and read.rpm is None:
        read.rpm = parse_rpm(text)
    if kind == "driver_name" and read.driver_name is None:
        read.driver_name = parse_driver_name(text)
        read.driver_id = read.driver_id or parse_driver_id(text)
    if kind == "channel" and read.channel is None:
        read.channel = parse_channel(text)
    if kind == "driver_id" and read.driver_id is None:
        read.driver_id = parse_driver_id(text) or (text.strip() or None)


def parse_frame(profile: Profile, texts: dict[int, str], box=CAMEROON_BOX) -> FrameRead:
    """texts: OCR text per ROI index of the profile → parsed fields."""
    read = FrameRead(texts=dict(texts))
    for i, roi in enumerate(profile.rois):
        parse_field(roi.field, texts.get(i, ""), profile, read, box)
    return read


# ── auto-detection ───────────────────────────────────────────────────────────

class LayoutDetector:
    """Scores the candidate profiles on the first frames with content: the profile with
    the most valid parses (GPS fix + date-time, a plate breaks ties) wins."""

    def __init__(self, candidates: list[str]):
        self.candidates = list(candidates)
        self.scores: dict[str, float] = {n: 0.0 for n in candidates}
        self.hits: dict[str, dict[str, int]] = {n: {"gps": 0, "datetime": 0} for n in candidates}
        self.frames = 0

    def add(self, reads: dict[str, FrameRead]) -> None:
        self.frames += 1
        for name, r in reads.items():
            self.scores[name] = self.scores.get(name, 0.0) + r.score
            h = self.hits.setdefault(name, {"gps": 0, "datetime": 0})
            h["gps"] += r.latitude is not None
            h["datetime"] += r.timestamp is not None

    def best(self) -> Optional[str]:
        """Winner (ties → candidate order), None when no profile parsed anything."""
        best, top = None, 0.0
        for n in self.candidates:
            if self.scores.get(n, 0.0) > top:
                best, top = n, self.scores[n]
        return best

    def summary(self) -> dict:
        return {"frames": self.frames, "scores": {k: round(v, 2) for k, v in self.scores.items()},
                "hits": self.hits}


class StaticFields:
    """Majority vote of the fields that do not change during a recording (plate, company,
    driver): a region read the same way `stable_after` times is not OCR'd any more."""

    def __init__(self, stable_after: int = 5):
        self.stable_after = stable_after
        self.votes: dict[int, Counter] = {}

    def add(self, roi_index: int, value) -> None:
        self.votes.setdefault(roi_index, Counter())[value] += 1

    def stable(self, roi_index: int) -> bool:
        c = self.votes.get(roi_index)
        if not c:
            return False
        value, n = c.most_common(1)[0]
        return n >= self.stable_after and n >= 0.7 * sum(c.values())

    def majority(self, roi_index: int):
        c = self.votes.get(roi_index)
        if not c:
            return None
        named = [(v, n) for v, n in c.most_common() if v is not None]
        return named[0][0] if named else None
