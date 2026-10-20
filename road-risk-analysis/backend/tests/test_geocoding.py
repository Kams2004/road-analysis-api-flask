"""Structured reverse geocoding (app.services.geocoding) — pure parsing of
Nominatim answers for Cameroonian places, the cache / throttle wrapper with a
fake upstream (no network), and the hazard address aggregation."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services import geocoding
from app.services.geocoding import apply_to, has_address, parse_address
from app.services.hazard_service import address_member, aggregate


# Shapes of real Nominatim /reverse answers (jsonv2, addressdetails=1).
DOUALA_AKWA = {
    "road": "Rue Joss", "suburb": "Akwa", "city_district": "Douala Ier", "city": "Douala",
    "county": "Wouri", "state": "Littoral", "ISO3166-2-lvl4": "CM-LT", "country": "Cameroun",
    "country_code": "cm",
}
DOUALA_ARRONDISSEMENT_AS_CITY = {
    "road": "Boulevard de la République", "neighbourhood": "Ndogbong", "suburb": "Bonamoussadi",
    "city": "Douala III", "county": "Wouri", "state": "Littoral", "country": "Cameroun",
}
YAOUNDE_QUARTER = {
    "road": "Avenue Kennedy", "quarter": "Centre administratif", "suburb": "Yaoundé Ier",
    "city": "Yaoundé", "county": "Mfoundi", "state": "Centre", "country": "Cameroun",
}
BAFOUSSAM_TOWN = {
    "road": "N5", "town": "Bafoussam", "county": "Mifi", "state": "Ouest", "country": "Cameroun",
}
RURAL_VILLAGE = {"village": "Ebebda", "county": "Lekié", "state": "Centre", "country": "Cameroun"}
HIGHWAY_ONLY = {"road": "N3", "county": "Sanaga-Maritime", "state": "Littoral", "country": "Cameroun"}


def test_parse_douala_street_quarter_district():
    a = parse_address(DOUALA_AKWA)
    assert (a["road"], a["locality"], a["district"], a["town"], a["region"], a["country"]) == \
        ("Rue Joss", "Akwa", "Douala Ier", "Douala", "Littoral", "Cameroun")
    assert a["display"] == "Rue Joss, Akwa, Douala"
    assert a["city"] == "Douala"
    # Legacy label unchanged: street, suburb, region, country.
    assert a["location_name"] == "Rue Joss, Akwa, Littoral, Cameroun"


def test_parse_arrondissement_in_city_tag():
    a = parse_address(DOUALA_ARRONDISSEMENT_AS_CITY)
    assert a["town"] == "Douala"            # "Douala III" → the city …
    assert a["district"] == "Douala III"    # … and the arrondissement as district
    assert a["locality"] == "Ndogbong"      # neighbourhood is more precise than suburb
    assert a["display"] == "Boulevard de la République, Ndogbong, Douala"


def test_parse_quarter_preferred():
    a = parse_address(YAOUNDE_QUARTER)
    assert a["locality"] == "Centre administratif"
    assert a["district"] == "Mfoundi"       # no city_district → department
    assert a["display"] == "Avenue Kennedy, Centre administratif, Yaoundé"


def test_parse_town_without_quarter():
    a = parse_address(BAFOUSSAM_TOWN)
    assert (a["road"], a["locality"], a["town"], a["region"]) == ("N5", None, "Bafoussam", "Ouest")
    assert a["display"] == "N5, Bafoussam"


def test_parse_rural_and_highway():
    a = parse_address(RURAL_VILLAGE)
    assert a["town"] == "Ebebda" and a["display"] == "Ebebda, Lekié, Centre"
    h = parse_address(HIGHWAY_ONLY)
    assert h["town"] is None and h["district"] == "Sanaga-Maritime"
    assert h["display"] == "N3, Sanaga-Maritime, Littoral"


def test_parse_empty_falls_back_on_display_name():
    a = parse_address({}, "Somewhere, Littoral, Cameroun")
    assert a["display"] == "Somewhere, Littoral, Cameroun"
    assert a["location_name"] == "Somewhere"
    assert not any(a[k] for k in ("road", "locality", "town"))
    assert parse_address(None)["display"] is None


def test_locality_equal_to_town_is_dropped():
    a = parse_address({"suburb": "Kribi", "town": "Kribi", "state": "Sud", "country": "Cameroun"})
    assert a["locality"] is None and a["display"] == "Kribi, Sud"


def test_has_address_and_apply_to():
    assert not has_address(geocoding.empty_address())
    geo = parse_address(DOUALA_AKWA)
    assert has_address(geo)
    obj = SimpleNamespace(location_name="OSD text", city=None, country=None)
    apply_to(obj, geo, keep_location_name=True)
    assert obj.display_location == "Rue Joss, Akwa, Douala"
    assert (obj.road, obj.locality, obj.town, obj.city) == ("Rue Joss", "Akwa", "Douala", "Douala")
    assert obj.location_name == "OSD text"


def test_reverse_geocode_cache_and_disabled(monkeypatch):
    calls = []

    def fake_lookup(lat, lon):
        calls.append((lat, lon))
        return parse_address(DOUALA_AKWA)

    geocoding.cache_clear()
    monkeypatch.setattr(geocoding, "_lookup", fake_lookup)
    monkeypatch.setattr(geocoding.settings, "GEOCODING_ENABLED", True)
    a = geocoding.reverse_geocode_details(4.05112, 9.70431)
    b = geocoding.reverse_geocode_details(4.05118, 9.70436)   # same ~110 m cell
    assert a["display"] == b["display"] == "Rue Joss, Akwa, Douala"
    assert len(calls) == 1
    monkeypatch.setattr(geocoding.settings, "GEOCODING_ENABLED", False)
    assert geocoding.reverse_geocode_details(3.8, 11.5)["display"] is None
    assert len(calls) == 1
    geocoding.cache_clear()


def test_failed_lookup_is_cached_briefly(monkeypatch):
    calls = []
    geocoding.cache_clear()
    monkeypatch.setattr(geocoding, "_lookup", lambda lat, lon: calls.append(1) or None)
    monkeypatch.setattr(geocoding.settings, "GEOCODING_ENABLED", True)
    assert geocoding.reverse_geocode_details(4.0, 9.7)["town"] is None
    assert geocoding.reverse_geocode_details(4.0, 9.7)["town"] is None
    assert len(calls) == 1      # negative cache (10 min) — an outage is not hammered
    geocoding.cache_clear()


def _member(lat, lon, conf=0.8, **addr):
    t0 = datetime(2026, 10, 1, 8, 0)
    base = dict(latitude=lat, longitude=lon, confidence=conf, captured_at=t0, created_at=t0,
                location_name=None, city=None, country=None, road=None, locality=None, district=None,
                town=None, region=None, display_location=None)
    base.update(addr)
    return SimpleNamespace(**base)


def test_hazard_address_from_most_central_member():
    far = _member(4.0510, 9.7040, display_location="Rue A, Akwa, Douala", town="Douala", locality="Akwa",
                  road="Rue A", location_name="Rue A, Akwa, Littoral, Cameroun", city="Douala", country="Cameroun")
    mid = _member(4.0505, 9.7045, display_location="Rue B, Bonanjo, Douala", town="Douala", locality="Bonanjo",
                  road="Rue B", city="Douala", country="Cameroun")
    other = _member(4.0500, 9.7050)
    agg = aggregate([far, mid, other])
    assert agg["display_location"] == "Rue B, Bonanjo, Douala"
    assert (agg["road"], agg["locality"], agg["town"]) == ("Rue B", "Bonanjo", "Douala")
    assert agg["city"] == "Douala"


def test_hazard_address_falls_back_on_legacy_names():
    legacy = _member(3.0, 11.0, location_name="Rue A, Yaoundé", city="Yaoundé", country="Cameroon")
    bare = _member(3.0001, 11.0001)
    assert address_member([legacy, bare], 3.00005, 11.00005) is legacy
    agg = aggregate([legacy, bare])
    assert agg["location_name"] == "Rue A, Yaoundé" and agg["display_location"] is None
    assert address_member([bare], 3.0, 11.0) is None


def test_hazard_address_tie_broken_by_confidence():
    a = _member(4.0, 9.7, conf=0.6, display_location="X", town="T")
    b = _member(4.0, 9.7, conf=0.9, display_location="Y", town="T")
    assert address_member([a, b], 4.0, 9.7) is b
    assert aggregate([a, b])["first_detected_at"] == datetime(2026, 10, 1, 8, 0)
    assert aggregate([a, b])["last_detected_at"] - datetime(2026, 10, 1, 8, 0) == timedelta(0)
