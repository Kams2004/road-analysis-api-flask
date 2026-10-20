"""Predefined road sections (settings section `routes`) and the geocoding
label helpers of /routes."""
import pytest
from pydantic import ValidationError

from app.api.routes.route_sections import _city, _place, _short_name
from app.services.settings_service import SECTIONS, RoutesSettings


def test_default_sections_cover_main_corridors():
    data = RoutesSettings().model_dump()
    ids = [s["id"] for s in data["sections"]]
    assert "douala-yaounde-n3" in ids and "yaounde-bertoua-n10" in ids and len(ids) == 6
    first = data["sections"][0]
    assert set(first) == {"id", "name", "from", "to"}           # serialised with `from`, not `from_`
    assert first["from"]["label"] == "Douala"
    assert SECTIONS["routes"] is RoutesSettings


def test_round_trip_and_validation():
    data = RoutesSettings().model_dump()
    assert RoutesSettings(**data).model_dump() == data
    dup = {"sections": [data["sections"][0], data["sections"][0]]}
    with pytest.raises(ValidationError):
        RoutesSettings(**dup)
    bad = {"sections": [{**data["sections"][0], "from": {"lat": 120, "lon": 9, "label": "x"}}]}
    with pytest.raises(ValidationError):
        RoutesSettings(**bad)
    blank = {"sections": [{**data["sections"][0], "name": "   "}]}
    with pytest.raises(ValidationError):
        RoutesSettings(**blank)
    assert RoutesSettings(sections=[]).sections == []


def test_geocoding_labels():
    assert _city({"city": "Douala III"}) == "Douala"
    assert _city({"town": "Kribi"}) == "Kribi"
    rev = {"address": {"suburb": "Ndoghem", "city": "Douala III", "state": "Littoral"}, "display_name": "x"}
    assert _short_name(rev) == "Ndoghem, Douala"
    p = _place({"name": "Ndogbong", "lat": "4.06", "lon": "9.75", "display_name": "Ndogbong, Makepe, Douala V",
                "address": {"suburb": "Ndogbong", "town": "Makepe", "city": "Douala V", "state": "Littoral"}})
    assert (p.label, p.secondary) == ("Ndogbong", "Douala, Littoral")
