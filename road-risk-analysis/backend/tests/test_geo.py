from app.services.osrm import geo


def test_haversine_one_degree_latitude():
    # ~111.19 km per degree of latitude, everywhere on Earth.
    d = geo.haversine_m(0.0, 0.0, 1.0, 0.0)
    assert 111_000 < d < 111_400


def test_haversine_zero_for_same_point():
    assert geo.haversine_m(4.05, 9.7, 4.05, 9.7) == 0.0


def test_bearing_due_north():
    b = geo.bearing_deg(0.0, 0.0, 1.0, 0.0)
    assert b == 0.0 or b == 360.0


def test_bearing_due_east():
    b = geo.bearing_deg(0.0, 0.0, 0.0, 1.0)
    assert abs(b - 90.0) < 0.5


def test_destination_round_trip():
    lat, lon = 4.05, 9.7
    dest_lat, dest_lon = geo.destination(lat, lon, 90.0, 1000.0)
    back = geo.haversine_m(lat, lon, dest_lat, dest_lon)
    assert abs(back - 1000.0) < 1.0


def test_heading_diff_wraps_correctly():
    assert geo.heading_diff_deg(10, 350) == 20
    assert geo.heading_diff_deg(0, 180) == 180
    assert geo.heading_diff_deg(90, 90) == 0


def test_project_point_onto_polyline_at_vertex():
    coords = [(4.0, 9.0), (4.0, 9.01), (4.0, 9.02)]
    dist, idx = geo.project_point_onto_polyline((4.0, 9.01), coords)
    expected = geo.haversine_m(*coords[0], *coords[1])
    assert abs(dist - expected) < 1.0
    assert idx in (0, 1)


def test_project_point_onto_polyline_at_start():
    coords = [(4.0, 9.0), (4.0, 9.01)]
    dist, idx = geo.project_point_onto_polyline((4.0, 9.0), coords)
    assert dist < 1.0
    assert idx == 0
