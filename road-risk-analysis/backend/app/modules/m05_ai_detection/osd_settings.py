"""
Settings section `osd` (system_settings, GET/PUT /settings/osd) — dashcam overlay
layouts (RRAP-05-014). See docs/OSD-LAYOUTS.md.

A profile mirrors the OSD configuration screen of an equipment: one entry per
overlay field (`date_time`, `gps`, `vehicle_no`, `user_def`, `rpm`, `driver_id`,
`driver_name`, `channel`) with its enabled flag and the X/Y of its top-left anchor on the
device's 0–1000 grid; the OCR regions are derived from them
(osd_layouts.derive_rois). `profiles` adds equipment or overrides a built-in one
(same name or alias). The layout of a job: job option `osd_layout`, else
`by_source` (stream / connection id, then source type), else `default_layout`;
`auto` detects it on the first frames, and `by_vehicle` forces a profile as soon
as a detected plate matches.
"""
from __future__ import annotations

import re
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from app.modules.m05_ai_detection import osd_layouts as L

FieldKind = Literal["date_time", "gps", "vehicle_no", "user_def", "rpm", "driver_id", "driver_name", "channel"]
RESERVED = (L.AUTO, L.NO_OSD, L.FULL_FRAME)
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{1,39}$")


class OsdField(BaseModel):
    field:       FieldKind
    enabled:     bool = True
    x:           float = Field(ge=0, le=1000)                       # anchor, per-mille of the frame width
    y:           float = Field(ge=0, le=1000)                       # anchor, per-mille of the frame height
    line_height: Optional[float] = Field(None, gt=0, le=500)        # per-mille; null = the profile's
    width:       Optional[float] = Field(None, gt=0, le=1000)       # per-mille; null = up to the next field


class OsdProfileSettings(BaseModel):
    label:        str = ""
    line_height:  float = Field(50, gt=0, le=500)
    x_margin:     float = Field(20, ge=0, le=200)
    y_margin:     float = Field(15, ge=0, le=200)
    fields:       list[OsdField] = Field(min_length=1)
    date_formats: list[str] = Field(default_factory=lambda: list(L.DEFAULT_DATE_FORMATS))
    gps_regex:    Optional[str] = None      # named groups lat, ns, lon, ew (+ speed)
    plate_regex:  Optional[str] = None
    timezone:     Optional[str] = None      # IANA zone of the camera clock (default settings.OSD_TIMEZONE)
    detect:       bool = True               # candidate of the auto-detection
    aliases:      list[str] = []

    @field_validator("fields")
    @classmethod
    def _unique_fields(cls, v: list[OsdField]):
        kinds = [f.field for f in v]
        dup = {k for k in kinds if kinds.count(k) > 1}
        if dup:
            raise ValueError(f"fields: {sorted(dup)} listed twice")
        if not any(f.enabled for f in v):
            raise ValueError("fields: at least one field must be enabled")
        return v

    @field_validator("date_formats")
    @classmethod
    def _formats(cls, v: list[str]):
        for f in v:
            u = f.upper()
            if "DD" not in u or "MM" not in u or "YY" not in u or re.search(r"[^DMY/\-. ]", u):
                raise ValueError(f"date format {f!r}: use DD, MM, YYYY (or YY) and the separators / - .")
        return v or list(L.DEFAULT_DATE_FORMATS)

    @field_validator("gps_regex")
    @classmethod
    def _gps_regex(cls, v):
        if v:
            try:
                rx = re.compile(v)
            except re.error as e:
                raise ValueError(f"gps_regex: {e}")
            missing = {"lat", "ns", "lon", "ew"} - set(rx.groupindex)
            if missing:
                raise ValueError(f"gps_regex: named groups missing {sorted(missing)}")
        return v or None

    @field_validator("plate_regex")
    @classmethod
    def _plate_regex(cls, v):
        if v:
            try:
                re.compile(v)
            except re.error as e:
                raise ValueError(f"plate_regex: {e}")
        return v or None

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v):
        if v:
            from zoneinfo import ZoneInfo
            try:
                ZoneInfo(v)
            except Exception:
                raise ValueError(f"timezone: unknown zone {v!r}")
        return v or None


class OsdSettings(BaseModel):
    default_layout:      str = L.AUTO
    profiles:            dict[str, OsdProfileSettings] = {}
    by_vehicle:          dict[str, str] = {}     # plate → layout
    by_source:           dict[str, str] = {}     # stream id / connection id / source type → layout
    gps_box:             Optional[list[float]] = Field(default_factory=lambda: list(L.CAMEROON_BOX))
    probe_frames:        int = Field(5, ge=1, le=50)
    full_frame_fallback: bool = True
    neighbour_retry:     bool = True             # re-read GPS / time on frames ±1–2 s when unreadable

    @field_validator("gps_box")
    @classmethod
    def _box(cls, v):
        if v is None:
            return None
        if len(v) != 4 or not (-90 <= v[0] < v[1] <= 90) or not (-180 <= v[2] < v[3] <= 180):
            raise ValueError("gps_box: [lat_min, lat_max, lon_min, lon_max]")
        return v

    @model_validator(mode="after")
    def _names(self):
        for name in self.profiles:
            if not _NAME_RE.match(name) or name in RESERVED:
                raise ValueError(f"profiles: invalid name {name!r} (lower-case letters, digits, _ -; "
                                 f"not {', '.join(RESERVED)})")
        names = self.available()
        for where, value in [("default_layout", self.default_layout), *(
                (f"by_vehicle.{k}", v) for k, v in self.by_vehicle.items()), *(
                (f"by_source.{k}", v) for k, v in self.by_source.items())]:
            if value.lower() not in (L.AUTO, L.NO_OSD) and L.resolve_name(value, names) is None:
                raise ValueError(f"{where}: unknown layout {value!r} (available: auto, none, {', '.join(names)})")
        return self

    def available(self) -> dict[str, L.Profile]:
        return L.effective_profiles({k: v.model_dump() for k, v in self.profiles.items()})


def layout_for_job(osd: OsdSettings, requested: Optional[str], source_type: Optional[str],
                   source_ids: tuple = ()) -> tuple[Optional[str], str]:
    """(layout name or None = auto-detect, method) of a job: job option → source → default."""
    profiles = osd.available()
    candidates = [(requested, "job")]
    candidates += [(osd.by_source.get(i), "source") for i in source_ids if i]
    candidates += [(osd.by_source.get(source_type or ""), "source"), (osd.default_layout, "default")]
    for value, method in candidates:
        if not value or value.lower() == L.AUTO:
            if value and method == "job":
                return None, "auto"
            continue
        if value.lower() == L.NO_OSD:
            return L.NO_OSD, method
        name = L.resolve_name(value, profiles)
        if name:
            return name, method
    return None, "auto"


def validate_layout_option(osd: OsdSettings, value: Optional[str]) -> Optional[str]:
    """Job option `osd_layout` → canonical value (None for auto); ValueError when unknown."""
    if value is None or value.strip().lower() in ("", L.AUTO):
        return None
    if value.strip().lower() == L.NO_OSD:
        return L.NO_OSD
    name = L.resolve_name(value, osd.available())
    if name is None:
        raise ValueError(f"Unknown osd_layout {value!r} (available: auto, none, {', '.join(osd.available())})")
    return name
