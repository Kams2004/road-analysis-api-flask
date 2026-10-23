"""OSD layouts (RRAP-05-014): pure parsing of both equipment formats, ROIs derived from
the device anchors, layout auto-detection and neighbour-frame retries with a fake OCR."""
from datetime import datetime, timedelta

import numpy as np
import pytest

from app.modules.m05_ai_detection import osd_layouts as L
from app.modules.m05_ai_detection import osd_parser
from app.modules.m05_ai_detection.osd_parser import GpsVoter, OsdReader, parse_gps_text
from app.modules.m05_ai_detection.osd_settings import OsdSettings, layout_for_job, validate_layout_option
from app.modules.m06_hazards import detection_workflow as wf

BOX = L.CAMEROON_BOX


# ── GPS ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,lat,lon,speed", [
    ("0515.3490,N,01013.5519,E,016KM/H", 5.255817, 10.225865, 16.0),            # total
    ("0348.2130,N,01014.8332,E,051KM/H ACC:ON", 3.80355, 10.24722, 51.0),       # besafe
    ("0405.1621.N.01132.7658.E.059KM/H", 4.086035, 11.546097, 59.0),            # dots as separators
    ("0515.8983N01013.7303E000KM/H", 5.264972, 10.228838, 0.0),                 # no separators
    ("05156127N.01013.5550E009KM/H", 5.260212, 10.225917, 9.0),                 # dropped decimal dot
    ("516.2659.N.01014.2637E.000KM/H", 5.271098, 10.237728, 0.0),               # leading 0 lost
    ("348.4653.N.Q1Q16.9697.E.059KM/H ACC:0N", 3.807755, 10.282828, 59.0),      # Q for 0 (big font)
    ("0348.4653.N.Q1Q16.9697.E.Q59RM H ACC:0P", 3.807755, 10.282828, 59.0),     # speed 'Q59RM H'
    ("9407.0866.N.01131.6763.E.057KM/H", 4.11811, 11.527938, 57.0),            # leading 0 read as 9
    ("4351.5783.N.Q1QQ4.7767.E.057KM H ACC:0N", 3.859638, 10.079612, 57.0),     # leading 0 read as 4
    ("0403.8309.N.01133.1972.E.043KM/L", 4.063848, 11.553287, 43.0),            # 'KM/L'
])
def test_parse_gps_both_layouts(text, lat, lon, speed):
    assert L.parse_gps_text(text, BOX) == (lat, lon, speed)


def test_parse_gps_rejects_garbage():
    assert L.parse_gps_text("0000.0000.N.00000.0000.E.000KM/H", BOX) == (None, None, None)   # no fix
    assert L.parse_gps_text("4830.0000,N,00220.0000,E,010KM/H", BOX) == (None, None, None)   # Paris
    assert L.parse_gps_text("0575.1000,N,01013.5519,E,016KM/H", BOX) == (None, None, None)   # 75 minutes
    assert L.parse_gps_text("0515.5113,N,01013.5458,E,V1KM/H", BOX)[2] is None               # speed misread
    assert L.parse_gps_text("", BOX) == (None, None, None)
    # without a box (location-correction endpoint) a 9 stays a 9
    assert parse_gps_text("9407.0866,N,01131.6763,E") == (None, None, None)


def test_custom_gps_regex():
    rx = r"LAT(?P<lat>\d{4}\.\d{4})(?P<ns>[NS])LON(?P<lon>\d{5}\.\d{4})(?P<ew>[EW])SPD(?P<speed>\d+)"
    assert L.parse_gps_text("LAT0515.3490NLON01013.5519ESPD16", BOX, rx) == (5.255817, 10.225865, 16.0)


# ── date & time ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("24/04/2026 08:33:09[ACC:ON]", "2026-04-24T08:33:09"),
    ("24/04/20260833.30[ACC:0N]", "2026-04-24T08:33:30"),     # space + colon lost (the empty recorded_at)
    ("24/04/2026083330[ACC:ON]", "2026-04-24T08:33:30"),
    ("15/10/2026 1552:22[ACC:0N]", "2026-10-15T15:52:22"),
    ("15/10/2026 15-52:22[ACC:0N1", "2026-10-15T15:52:22"),
    ("20-10-2026 14:29:42", "2026-10-20T14:29:42"),           # besafe
    ("20-10-202614:34:39 | LITR7Q4AC CAMACO", "2026-10-20T14:34:39"),
    ("2026-10-20 14:29:42", "2026-10-20T14:29:42"),
    ("2O-1O-2O26 14:29:42", "2026-10-20T14:29:42"),
])
def test_parse_datetime(text, expected):
    assert L.parse_datetime(text).isoformat() == expected


def test_parse_datetime_invalid_and_formats():
    assert L.parse_datetime("99/99/2026 08:38:00") is None
    assert L.parse_datetime("24/04/2026 25:00:00") is None
    assert L.parse_datetime("24/04/2026") is None
    assert L.parse_datetime("04/24/2026 08:00:00", ["MM/DD/YYYY"]) == datetime(2026, 4, 24, 8)
    assert L.parse_time_of_day("-10-202612:48:56") == (12, 48, 56)


# ── plate, company, channel, driver ──────────────────────────────────────────

def test_plate_company_channel_driver():
    assert L.parse_plate("LTTR458AV") == "LTTR458AV"
    assert L.parse_plate("LITR7Q4AC CAMACO") == "LTTR704AC"      # region code + digit fix
    assert L.parse_plate("LITR185AS") == "LTTR185AS"
    assert L.parse_plate("CAMTRAD") is None and L.parse_plate("RPM:0r/min") is None
    assert L.parse_plate("CH2") is None
    assert L.parse_company("LTTR704AC CAMACO", after_plate=True) == "CAMACO"
    assert L.parse_company("CAMTRAD") == "CAMTRAD"
    assert L.parse_channel("CH2") == "CH2" and L.parse_channel("CH 02") == "CH2"
    assert L.parse_channel("LTTR704AC CAMACO") is None
    assert L.parse_driver_name("ID: Name:") is None
    assert L.parse_driver_name("BOUBAKARI_BABIGUIR.CAMTRAD") == "BOUBAKARI_BABIGUIR.CAMTRAD"
    assert L.parse_rpm("RPM:0r/min") == 0


# ── profiles ─────────────────────────────────────────────────────────────────

def test_rois_derived_from_device_anchors():
    p = L.effective_profiles()
    total = {r.field: r.box for r in p["total"].rois}
    assert total["date_time"] == (0.03, 0.035, 0.5, 0.1)          # up to the GPS anchor x=500
    assert total["gps"] == (0.48, 0.035, 1.0, 0.1)
    assert "driver_id" not in total                                # disabled on the device
    assert total["vehicle_no"][2] == 0.5                            # not cut at the disabled driver id (x=150)
    besafe = {r.field: r.box for r in p["besafe"].rois}
    assert besafe["date_time"] == (0.0, 0.885, 0.5, 0.965) and besafe["vehicle_no"][0] == 0.48
    assert {r.field for r in p["ola_energy"].rois} == {"gps", "driver_name", "date_time", "channel"}
    assert L.resolve_name("camaco_big", p) == "besafe" and L.resolve_name("CAMTRACK_TOP", p) == "total"
    assert L.resolve_name("auto", p) is None and L.resolve_name("nope", p) is None


def test_custom_profile_overrides_builtin():
    custom = {"besafe": {"label": "B2", "line_height": 80,
                         "fields": [{"field": "gps", "x": 20, "y": 40}, {"field": "date_time", "x": 20, "y": 900}]},
              "acme": {"fields": [{"field": "gps", "x": 0, "y": 0, "width": 400}]}}
    p = L.effective_profiles(custom)
    assert p["besafe"].label == "B2" and not p["besafe"].builtin and "camaco_big" in p["besafe"].aliases
    assert p["total"].builtin
    assert p["acme"].rois[0].box == (0.0, 0.0, 0.4, 0.05)


# ── reader with a fake OCR ───────────────────────────────────────────────────

SCENES = {
    "total": [((0.05, 0.05, 0.35, 0.07), "24/04/2026 08:33:09[ACC:ON]"),
              ((0.50, 0.05, 0.85, 0.07), "0515.3490,N,01013.5519,E,016KM/H"),
              ((0.05, 0.10, 0.15, 0.12), "LTTR458AV"), ((0.50, 0.10, 0.60, 0.12), "CAMTRAD"),
              ((0.05, 0.15, 0.15, 0.17), "RPM:0r/min"), ((0.50, 0.15, 0.80, 0.17), "BOUBAKARI_BABIGUIR.CAMTRAD")],
    "besafe": [((0.02, 0.05, 0.90, 0.09), "0348.2130,N,01014.8332,E,051KM/H ACC:ON"),
               ((0.02, 0.11, 0.20, 0.15), "ID: Name:"),
               ((0.02, 0.92, 0.45, 0.96), "20-10-2026 14:29:42"),
               ((0.52, 0.92, 0.90, 0.96), "LTTR704AC CAMACO")],
    "ola_energy": [((0.02, 0.06, 0.90, 0.10), "0348.2130,N,01014.8332,E,051KM/H ACC:ON"),
                   ((0.02, 0.11, 0.20, 0.15), "ID: Name:"),
                   ((0.02, 0.92, 0.45, 0.96), "20-10-2026 14:29:42"),
                   ((0.52, 0.92, 0.60, 0.96), "CH2")],
}


class FakeOcr:
    """Returns the scene texts whose centre lies in the region; `hide` blanks a text on
    some frames (colour transition)."""

    def __init__(self):
        self.scene_of: dict[int, list] = {}
        self.calls = 0

    def frame(self, scene: str, hide: tuple = (), replace: dict | None = None):
        f = np.random.default_rng(len(self.scene_of)).integers(0, 255, (576, 720, 3), dtype=np.uint8)
        lines = [(b, "" if any(h in t for h in hide) else (replace or {}).get(t, t)) for b, t in SCENES[scene]]
        self.scene_of[id(f)] = lines
        return f

    def __call__(self, frame, box, variant=None):
        self.calls += 1
        out = [t for (x0, y0, x1, y1), t in self.scene_of[id(frame)]
               if t and box[0] <= (x0 + x1) / 2 <= box[2] and box[1] <= (y0 + y1) / 2 <= box[3]]
        return " ".join(out), 0.95 if out else 0.0


@pytest.mark.parametrize("scene", ["total", "besafe", "ola_energy"])
def test_auto_detection_picks_the_layout(scene):
    ocr = FakeOcr()
    r = OsdReader(ocr=ocr, probe_frames=3)
    black = np.zeros((576, 720, 3), np.uint8)
    assert r.read(black).latitude is None and r.layout is None        # start-up frame skipped
    for _ in range(3):
        osd = r.read(ocr.frame(scene))
    assert r.layout == scene and r.method == "auto"
    ev = r.pop_events()[0]
    assert ev["layout"] == scene and ev["detection"]["frames"] == 3
    osd = r.read(ocr.frame(scene))
    assert osd.latitude is not None and osd.timestamp is not None and osd.layout == scene
    if scene == "total":
        assert (osd.vehicle_id, osd.company, osd.driver_name) == ("LTTR458AV", "CAMTRAD", "BOUBAKARI_BABIGUIR.CAMTRAD")
    elif scene == "besafe":
        assert (osd.vehicle_id, osd.company, osd.speed_kmh) == ("LTTR704AC", "CAMACO", 51.0)
    else:
        assert osd.channel == "CH2" and osd.vehicle_id is None


def test_detector_scoring_and_ties():
    d = L.LayoutDetector(["total", "besafe"])
    d.add({"total": L.FrameRead(latitude=5.0, longitude=10.0),
           "besafe": L.FrameRead(latitude=5.0, longitude=10.0, timestamp=datetime(2026, 1, 1))})
    assert d.best() == "besafe"
    d2 = L.LayoutDetector(["total", "besafe"])
    d2.add({"total": L.FrameRead(latitude=5.0), "besafe": L.FrameRead(latitude=5.0)})
    assert d2.best() == "total"                                     # tie → candidate order
    assert L.LayoutDetector(["total"]).best() is None


def test_static_fields_stop_being_read():
    ocr = FakeOcr()
    r = OsdReader(ocr=ocr, forced="total")
    for i in range(6):
        r.read(ocr.frame("total"), t=i)
    before = ocr.calls
    osd = r.read(ocr.frame("total"), t=7)
    assert ocr.calls - before == 2                                  # date, gps (plate/company/driver stable,
    assert osd.vehicle_id == "LTTR458AV" and osd.company == "CAMTRAD"   # rpm read every SLOW_INTERVAL_S)
    before = ocr.calls
    r.read(ocr.frame("total"), t=7 + osd_parser.SLOW_INTERVAL_S)
    assert ocr.calls - before >= 3                                  # + rpm again (statics may be re-checked)


def test_overlay_read_at_most_once_per_interval():
    ocr = FakeOcr()
    r = OsdReader(ocr=ocr, forced="total", min_interval_s=1.0)
    first = r.read(ocr.frame("total"), t=10.0)
    before = ocr.calls
    again = r.read(ocr.frame("total"), t=10.5)                     # same second: no OCR
    assert ocr.calls == before and again.reused and not first.reused
    assert again.latitude == first.latitude
    if first.timestamp is not None:
        assert (again.timestamp - first.timestamp).total_seconds() == 0.5
    r.read(ocr.frame("total"), t=11.0)                              # one second later: read again
    assert ocr.calls > before


def test_forced_and_by_vehicle():
    ocr = FakeOcr()
    r = OsdReader(ocr=ocr, forced="camaco_big", method="job")
    assert r.layout == "besafe" and r.method == "job"
    r2 = OsdReader(ocr=ocr, by_vehicle={"LTTR704AC": "ola_energy"})
    r2.read(ocr.frame("besafe"))
    assert r2.layout == "ola_energy" and r2.method == "vehicle"


def test_full_frame_fallback_then_none():
    ocr = FakeOcr()
    r = OsdReader(ocr=lambda f, b, v=None: ("", 0.0), probe_frames=2)
    for i in range(2):
        r.read(ocr.frame("total"), t=i)
    assert r.layout == L.FULL_FRAME and r.method == "fallback"
    for i in range(4):
        r.read(ocr.frame("total"), t=2 + i)
    assert r.layout == L.NO_OSD
    # full frame parses everything it finds
    p = L.full_frame_profile()
    read = L.parse_frame(p, {0: "24/04/2026 08:33:09[ACC:ON] 0515.3490,N,01013.5519,E,016KM/H LTTR458AV"}, BOX)
    assert read.latitude and read.timestamp and read.vehicle_id == "LTTR458AV"


def test_neighbour_frames_fill_an_unreadable_overlay():
    ocr = FakeOcr()
    r = OsdReader(ocr=ocr, forced="total")
    r.read(ocr.frame("total"), t=0.0)                                 # anchor (08:33:09)
    hidden = ocr.frame("total", hide=("0515", "24/04"))              # colour transition hides GPS + time
    before = ocr.frame("total", replace={"0515.3490,N,01013.5519,E,016KM/H": "0515.3480,N,01013.5509,E,016KM/H",
                                         "24/04/2026 08:33:09[ACC:ON]": "24/04/2026 08:33:12[ACC:ON]"})
    after = ocr.frame("total", replace={"0515.3490,N,01013.5519,E,016KM/H": "0515.3500,N,01013.5529,E,016KM/H",
                                        "24/04/2026 08:33:09[ACC:ON]": "24/04/2026 08:33:14[ACC:ON]"})
    nb = {-1.0: before, 1.0: after}
    osd = r.read(hidden, t=4.0, neighbours=lambda off: nb.get(off))
    assert osd.gps_source == "ocr_neighbour" and not osd.gps_interpolated
    assert osd.latitude == pytest.approx(L.nmea_to_decimal("0515.3490", "N"), abs=2e-6)   # midpoint
    assert osd.time_source == "ocr_neighbour" and osd.timestamp == datetime(2026, 4, 24, 8, 33, 13)
    # nothing readable anywhere → voter window (interpolated)
    osd = r.read(ocr.frame("total", hide=("0515", "24/04")), t=10.0, neighbours=lambda off: None)
    assert osd.gps_source == "interpolated" and osd.gps_interpolated and osd.timestamp is None


def test_time_reanchors_after_a_wrong_first_read_and_repairs_dates():
    ocr = FakeOcr()
    r = OsdReader(ocr=ocr, forced="besafe")
    d = "20-10-2026 14:29:42"
    assert r.read(ocr.frame("besafe", replace={d: "10-10-2025 14:29:42"}), t=0).timestamp == \
        datetime(2025, 10, 10, 14, 29, 42)
    # the date was misread on the anchor: disagreeing readings follow the anchor's date …
    for t in (10, 20):
        at = datetime(2025, 10, 20, 14, 29, 42) + timedelta(seconds=t)
        osd = r.read(ocr.frame("besafe", replace={d: at.strftime("%d-%m-%Y %H:%M:%S")}), t=t)
        assert osd.timestamp == at.replace(day=10)
    # … until the majority date is clearly another one (hysteresis 2)
    osd = r.read(ocr.frame("besafe", replace={d: "20-10-2025 14:30:12"}), t=30)
    assert osd.timestamp == datetime(2025, 10, 20, 14, 30, 12)
    # a reading off by minutes (misread time) is dropped; two consistent ones re-anchor
    osd = r.read(ocr.frame("besafe", replace={d: "20-10-2025 15:00:22"}), t=40)
    assert osd.timestamp is None
    osd = r.read(ocr.frame("besafe", replace={d: "20-10-2025 15:00:32"}), t=50)
    assert osd.timestamp == datetime(2025, 10, 20, 15, 0, 32)
    osd = r.read(ocr.frame("besafe", replace={d: "-10-202515:00:42"}), t=60)
    assert osd.timestamp == datetime(2025, 10, 20, 15, 0, 42)          # time of day + anchor date
    # a date in the future is a misread digit
    assert r.read(ocr.frame("besafe", replace={d: "20-10-2099 15:00:52"}), t=70).timestamp == \
        datetime(2025, 10, 20, 15, 0, 52)


def test_voter_accepts():
    v = GpsVoter()
    assert v.accepts(5.0, 10.0)
    v.update(5.0, 10.0)
    assert v.accepts(5.005, 10.0) and not v.accepts(5.5, 10.0) and not v.accepts(None, None)


# ── settings ─────────────────────────────────────────────────────────────────

def test_settings_validation_and_job_layout():
    s = OsdSettings(by_source={"sftp": "besafe", "stream-1": "total"}, by_vehicle={"LTTR458AV": "camtrack_top"})
    assert layout_for_job(s, None, "upload") == (None, "auto")
    assert layout_for_job(s, None, "sftp") == ("besafe", "source")
    assert layout_for_job(s, None, "sftp", ("stream-1",)) == ("total", "source")
    assert layout_for_job(s, "ola_energy", "sftp") == ("ola_energy", "job")
    assert layout_for_job(s, "auto", "sftp") == (None, "auto")
    assert layout_for_job(OsdSettings(default_layout="none"), None, "upload") == ("none", "default")
    assert validate_layout_option(s, "CAMACO_BIG") == "besafe"
    assert validate_layout_option(s, "auto") is None
    with pytest.raises(ValueError):
        validate_layout_option(s, "acme")
    with pytest.raises(ValueError):
        OsdSettings(default_layout="acme")
    with pytest.raises(ValueError):
        OsdSettings(profiles={"auto": {"fields": [{"field": "gps", "x": 0, "y": 0}]}})
    with pytest.raises(ValueError):
        OsdSettings(profiles={"acme": {"fields": [{"field": "gps", "x": 0, "y": 0},
                                                  {"field": "gps", "x": 0, "y": 900}]}})
    with pytest.raises(ValueError):
        OsdSettings(profiles={"acme": {"fields": [{"field": "gps", "x": 0, "y": 0}], "date_formats": ["HH:MM"]}})
    with pytest.raises(ValueError):
        OsdSettings(profiles={"acme": {"fields": [{"field": "gps", "x": 0, "y": 0}], "gps_regex": "(?P<lat>x)"}})
    acme = OsdSettings(profiles={"acme": {"fields": [{"field": "gps", "x": 0, "y": 0}]}}, default_layout="acme")
    assert "acme" in acme.available() and layout_for_job(acme, None, "upload") == ("acme", "default")


def test_location_status_for_neighbour_reads():
    assert wf.location_status_for(5.0, 10.0, False, "ocr_neighbour") == ("confirmed", "ocr_neighbour")
    assert wf.location_status_for(5.0, 10.0, True, "interpolated") == ("approximate", "interpolated")
    assert wf.location_status_for(5.0, 10.0, False) == ("confirmed", "ocr")
