"""
Thin async OSRM HTTP client. Nothing here knows about vehicles, telemetry or
road-aware state — see map_matching.py for that. The collision-prevention
pipeline (and Step 3 later) must go through app.services.road_state_service /
map_matching, never call this client directly, so the OSRM integration stays
swappable in one place.

Implemented on stdlib `urllib` run via `asyncio.to_thread` rather than adding
an async HTTP dependency (httpx/aiohttp) — this project has no such
dependency yet, and the same approach is already used by the demo vehicle
simulator (app/demo/vehicle_simulator.py) to talk to this exact OSRM server.
"""
import json
import logging
import time
import urllib.error
import urllib.request
from typing import List, Optional, Sequence, Tuple
import asyncio

from app.core.config import settings

logger = logging.getLogger(__name__)

LatLon = Tuple[float, float]


class OSRMError(Exception):
    """OSRM responded, but with a non-Ok status or an unusable result."""


class OSRMUnavailableError(Exception):
    """OSRM could not be reached at all (network error, timeout, bad response)."""


class OSRMResult:
    __slots__ = ("data", "latency_ms")

    def __init__(self, data: dict, latency_ms: float):
        self.data = data
        self.latency_ms = latency_ms


def _coords_param(points: Sequence[LatLon]) -> str:
    return ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in points)


class OSRMClient:
    def __init__(
        self,
        base_url: Optional[str] = None,
        profile: Optional[str] = None,
        timeout_s: Optional[float] = None,
    ):
        self.base_url = (base_url or settings.OSRM_BASE_URL).rstrip("/")
        self.profile = profile or settings.OSRM_PROFILE
        self.timeout_s = timeout_s or settings.OSRM_TIMEOUT_S

    def _get_sync(self, path: str) -> OSRMResult:
        url = f"{self.base_url}{path}"
        started = time.monotonic()
        try:
            with urllib.request.urlopen(url, timeout=self.timeout_s) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            # OSRM (at least the public demo server) returns non-2xx status
            # codes for perfectly ordinary "couldn't match/route" outcomes —
            # e.g. a plain HTTP 400 with body {"code":"NoMatch",...} — not
            # just for genuine failures. The body is still a normal OSRM
            # response, so parse it as one rather than treating every 4xx as
            # "OSRM unavailable"; only bail out if the body isn't the JSON
            # we expect (a real, unanticipated failure).
            raw = e.read()
            latency_ms = (time.monotonic() - started) * 1000
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                body = raw.decode(errors="replace")[:500]
                raise OSRMUnavailableError(f"OSRM request failed: HTTP {e.code}: {body or e.reason}") from e
            return OSRMResult(data, latency_ms)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            raise OSRMUnavailableError(f"OSRM request failed: {e}") from e
        latency_ms = (time.monotonic() - started) * 1000

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise OSRMUnavailableError(f"OSRM returned unparseable response: {e}") from e

        return OSRMResult(data, latency_ms)

    async def _get(self, path: str) -> OSRMResult:
        return await asyncio.to_thread(self._get_sync, path)

    async def match(
        self,
        points: Sequence[LatLon],
        timestamps: Optional[Sequence[int]] = None,
        radiuses: Optional[Sequence[float]] = None,
    ) -> OSRMResult:
        """POST-equivalent GET to /match — map-matches a short trace of GPS
        points to the road network. Raises OSRMUnavailableError on network
        failure; returns a result whose data['code'] may still be non-'Ok'
        (e.g. 'NoMatch') — callers must check it."""
        if not points:
            raise ValueError("match() requires at least one point")

        params = "geometries=geojson&overview=full&steps=true&annotations=true"
        if timestamps:
            params += "&timestamps=" + ";".join(str(int(t)) for t in timestamps)
        if radiuses:
            params += "&radiuses=" + ";".join(f"{r:.1f}" for r in radiuses)

        path = f"/match/v1/{self.profile}/{_coords_param(points)}?{params}"
        result = await self._get(path)
        logger.debug("OSRM /match %.0fms code=%s", result.latency_ms, result.data.get("code"))
        return result

    async def route(self, points: Sequence[LatLon]) -> OSRMResult:
        """GET /route — used here to fetch a bounded road-geometry window
        (behind/ahead of a matched point), not for turn-by-turn navigation."""
        if len(points) < 2:
            raise ValueError("route() requires at least two points")

        params = "geometries=geojson&overview=full&steps=true&annotations=true"
        path = f"/route/v1/{self.profile}/{_coords_param(points)}?{params}"
        result = await self._get(path)
        logger.debug("OSRM /route %.0fms code=%s", result.latency_ms, result.data.get("code"))
        return result

    async def nearest(self, point: LatLon) -> OSRMResult:
        """GET /nearest — lightweight fallback when /match can't produce a
        usable result at all (e.g. a single isolated ping with no history)."""
        lat, lon = point
        path = f"/nearest/v1/{self.profile}/{lon:.6f},{lat:.6f}?number=1"
        result = await self._get(path)
        logger.debug("OSRM /nearest %.0fms code=%s", result.latency_ms, result.data.get("code"))
        return result


_default_client: Optional[OSRMClient] = None


def get_osrm_client() -> OSRMClient:
    global _default_client
    if _default_client is None:
        _default_client = OSRMClient()
    return _default_client
