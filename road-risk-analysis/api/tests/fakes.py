"""Test doubles for the OSRM client — no network access, fully deterministic."""
from typing import Callable, List, Optional, Sequence, Tuple

from app.services.osrm.client import OSRMResult, OSRMUnavailableError

LatLon = Tuple[float, float]


class FakeOSRMClient:
    """
    Drop-in replacement for OSRMClient. Each method call is recorded, and
    responses/exceptions are supplied by the test as callables (so tests can
    return different data depending on how many times the method was called,
    or just always the same canned response).
    """

    def __init__(
        self,
        match_responses: Optional[List] = None,
        route_responses: Optional[List] = None,
        nearest_responses: Optional[List] = None,
    ):
        self._match_responses = list(match_responses or [])
        self._route_responses = list(route_responses or [])
        self._nearest_responses = list(nearest_responses or [])
        self.match_calls: List[dict] = []
        self.route_calls: List[dict] = []
        self.nearest_calls: List[dict] = []

    async def match(self, points: Sequence[LatLon], timestamps=None, radiuses=None) -> OSRMResult:
        self.match_calls.append({"points": list(points), "timestamps": timestamps, "radiuses": radiuses})
        return _pop_or_raise(self._match_responses)

    async def route(self, points: Sequence[LatLon]) -> OSRMResult:
        self.route_calls.append({"points": list(points)})
        return _pop_or_raise(self._route_responses)

    async def nearest(self, point: LatLon) -> OSRMResult:
        self.nearest_calls.append({"point": point})
        return _pop_or_raise(self._nearest_responses)


def _pop_or_raise(queue: list):
    if not queue:
        raise AssertionError("FakeOSRMClient received more calls than canned responses")
    item = queue.pop(0)
    if isinstance(item, Exception):
        raise item
    return item


def ok_result(data: dict, latency_ms: float = 10.0) -> OSRMResult:
    return OSRMResult(data, latency_ms)


def match_response(
    matched: LatLon,
    name: Optional[str] = "Test Road",
    confidence: float = 0.9,
    bearing_after: Optional[float] = 90.0,
    code: str = "Ok",
) -> OSRMResult:
    lat, lon = matched
    data = {
        "code": code,
        "matchings": [
            {
                "confidence": confidence,
                "legs": [
                    {
                        "steps": [
                            {
                                "name": name,
                                "distance": 100.0,
                                "maneuver": {"bearing_after": bearing_after, "bearing_before": bearing_after},
                            }
                        ]
                    }
                ],
            }
        ]
        if code == "Ok"
        else [],
        "tracepoints": [{"location": [lon, lat], "name": name}] if code == "Ok" else [],
    }
    return ok_result(data)


def route_window_response(steps: List[dict]) -> OSRMResult:
    """
    `steps` is a list of {"name": str, "distance": float, "coords": [(lat,lon), ...], "bearing": float}.
    Builds a single-leg /route response whose full geometry is the
    concatenation of each step's coords.
    """
    full_coords: List[List[float]] = []
    step_objs = []
    for s in steps:
        coords = [[lon, lat] for lat, lon in s["coords"]]
        if full_coords and full_coords[-1] == coords[0]:
            full_coords.extend(coords[1:])
        else:
            full_coords.extend(coords)
        step_objs.append(
            {
                "name": s["name"],
                "distance": s["distance"],
                "geometry": {"coordinates": coords},
                "maneuver": {"bearing_after": s.get("bearing", 0.0)},
            }
        )
    data = {
        "code": "Ok",
        "routes": [
            {
                "geometry": {"coordinates": full_coords},
                "legs": [{"steps": step_objs}],
            }
        ],
    }
    return ok_result(data)


def nearest_response(point: LatLon, name: Optional[str] = "Test Road", code: str = "Ok") -> OSRMResult:
    lat, lon = point
    data = {
        "code": code,
        "waypoints": [{"location": [lon, lat], "name": name}] if code == "Ok" else [],
    }
    return ok_result(data)
