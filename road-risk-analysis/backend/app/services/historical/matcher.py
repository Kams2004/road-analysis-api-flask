"""
Map-matches a HistoricalEvent onto the road network the same way Step 2
matches live telemetry — reusing the *same* OSRM client (app.services.osrm),
never a second integration. Uses /nearest (a single point) rather than
/match — historical events are one-off points, not a continuous trace like
live vehicle telemetry, so there's no history to run OSRM's HMM against.

road_segment_id here is intentionally cheap, not Step 2's route-window
concept: full route-window matching (Step 2's map_matching._build_road_context)
means an extra OSRM /route call per point, wasteful when importing
potentially thousands of historical events in one batch. Instead this reuses
Step 2's own stable_segment_id() the same way Step 2's degenerate fallback
path does — a hash of (road name, matched point) — good enough to group
"events on roughly this same named road", not a substitute for Step 2's
richer live segment concept.
"""
import logging
from typing import Optional

from app.core.config import settings
from app.services.historical.grid import grid_cell_id
from app.services.historical.models import HistoricalEvent, MapMatchedHistoricalEvent
from app.services.osrm.client import OSRMClient, OSRMUnavailableError, get_osrm_client
from app.services.osrm.models import stable_segment_id

logger = logging.getLogger(__name__)


async def match_event(
    event: HistoricalEvent,
    client: Optional[OSRMClient] = None,
    cell_size_m: Optional[float] = None,
) -> MapMatchedHistoricalEvent:
    """Never raises — a failed/unavailable OSRM lookup still returns a
    MapMatchedHistoricalEvent (grid cell computed from the raw coordinate;
    road name/segment left None) so one bad OSRM call doesn't abort an
    entire import batch."""
    cell_size_m = cell_size_m or settings.HISTORICAL_GRID_CELL_SIZE_M
    cell = grid_cell_id(event.latitude, event.longitude, cell_size_m)

    client = client or get_osrm_client()
    try:
        result = await client.nearest((event.latitude, event.longitude))
    except OSRMUnavailableError as e:
        logger.warning("OSRM unavailable while matching historical event %s: %s", event.source_event_id, e)
        return MapMatchedHistoricalEvent(event=event, grid_cell_id=cell)

    data = result.data
    if data.get("code") != "Ok" or not data.get("waypoints"):
        return MapMatchedHistoricalEvent(event=event, grid_cell_id=cell)

    wp = data["waypoints"][0]
    lon, lat = wp["location"]
    road_name = wp.get("name") or None
    distance_to_road = wp.get("distance", 0.0) or 0.0

    return MapMatchedHistoricalEvent(
        event=event,
        grid_cell_id=cell,
        matched_latitude=lat,
        matched_longitude=lon,
        road_name=road_name,
        road_segment_id=stable_segment_id(road_name, (lon, lat), (lon, lat)),
        # No GPS-accuracy/radius signal on a historical point the way live
        # telemetry has one — confidence here is just "how far was the raw
        # coordinate from the nearest road", a cruder proxy than Step 2's.
        match_confidence=1.0 if distance_to_road < 25 else 0.5,
    )
