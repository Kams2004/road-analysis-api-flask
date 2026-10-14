"""
Ymane (Camtrack biprod) historical-event source.

Integration status as of writing
──────────────────────────────────
CONFIRMED working (a real request/response pair was captured from the
user's own machine — see conversation history):
  - Login: POST {YMANE_BASE_URL}{YMANE_LOGIN_PATH} with
    {"is1": <username>, "is2": <password>} returns
    {"access_token": ..., "expires_in": ..., ...}.
  - The exception endpoint is a POST, takes datedebut/datefin/
    lastexceptionid/nbrow as query params, AND a JSON filter body.
  - Authorization header: the captured working request used
    "Bearer <token>". An earlier session had found a raw (no "Bearer ")
    token worked and "Bearer " 401'd — that finding no longer reproduces
    either way from this environment (see below), so the more recent,
    *actually observed success* wins here. If this ever 401s in
    production, try dropping the "Bearer " prefix first.
  - The filter body matters: an all-empty-lists + alarm/alert/record=False
    body (this module's previous default) returned nothing even when the
    endpoint was reachable — the working request set alarm/alert/record
    to true AND passed an explicit, non-empty listidtypeexception array.
    Empty lists are apparently NOT "no filter", they're "match nothing".
  - The shape of one event record (previously fully unknown) is now
    confirmed from a real sample:
      exceptionid, affiliateid, transporterid, vehicleid, driverid,
      startdatetime, enddatetime (epoch milliseconds),
      exceptiontype (int code), totalduration, totaldistance, level,
      threshold, maxvalue, distanceunderexception, startgps, endgps
      (both "lat,lon" as a single comma-joined string, not separate
      fields), vmax (observed value, km/h in every sample seen),
      unitid, evidence (a JSON-encoded string with photo/video links).

STILL NOT CONFIRMED:
  - What each `exceptiontype` numeric code actually means. Ymane's own
    OpenAPI schema doesn't document a decode table, and none has been
    obtained. Every sample seen so far carries a nonzero `vmax` and the
    endpoint's own name ("limit exception details") both point at these
    being speed-limit exceptions — _map_exception_type below uses exactly
    that signal and nothing more speculative. The raw `exceptiontype` is
    always preserved in `raw` so a real decode table can replace this
    heuristic in one place later without touching anything else.
  - This environment's own network cannot reach the endpoint successfully
    at all (every attempt here 500s, regardless of header format or
    filter body, while the user's own machine succeeds) — looks like an
    IP allowlist, geo-restriction, or WAF rule on Ymane's side, not
    something fixable by changing this code. Production deployment may or
    may not be affected the same way; verify once deployed.
"""
import json
import logging
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from app.core.config import settings
from app.services.historical.models import HistoricalEvent, HistoricalEventType
from app.services.historical.sources.base import HistoricalEventSource

logger = logging.getLogger(__name__)

_PAGE_SIZE = 200

# The exact exception-type catalogue from the one confirmed-working request.
# Ymane apparently treats an empty listidtypeexception as "match nothing"
# rather than "no filter" (see module docstring) — this is a real fleet's
# configured exception rule IDs, not a documented "all types" sentinel, so a
# different Ymane account may need a different list here.
_KNOWN_EXCEPTION_TYPE_IDS = [
    35, 5, 6, 7, 16, 20, 22, 13, 33, 23, 2, 3, 37, 24, 32, 39,
    4, 19, 11, 12, 36, 10, 27, 15, 1, 26, 29, 41, 38, 40, 9, 8, 17,
]


class YmaneAuthError(Exception):
    pass


class YmaneRequestError(Exception):
    pass


def _map_exception_type(exceptiontype: Any, vmax: Any) -> HistoricalEventType:
    """Best-effort category for a raw exceptiontype code — see the module
    docstring for what is and isn't confirmed about these codes. Never
    guesses a specific category beyond what the data actually signals."""
    try:
        if vmax is not None and float(vmax) > 0:
            return HistoricalEventType.SPEEDING
    except (TypeError, ValueError):
        pass
    return HistoricalEventType.OTHER


def _map_raw_event_fields(raw: Dict[str, Any], source: str = "ymane") -> Optional[HistoricalEvent]:
    """Pure, standalone mapping from one raw Ymane exception record to our
    canonical HistoricalEvent shape, based on the confirmed real field
    names (see module docstring). Kept as a free function (rather than a
    method) so it's directly unit-testable with no network or class
    instantiation required. Never raises — an unparsable record is logged
    and skipped so one bad record doesn't abort an entire import batch."""
    gps = raw.get("startgps") or raw.get("endgps")
    if not gps:
        logger.warning(
            "Ymane event has no startgps/endgps field (keys present: %s) — skipping",
            list(raw.keys()),
        )
        return None
    try:
        lat_str, lon_str = str(gps).split(",", 1)
        lat, lon = float(lat_str), float(lon_str)
    except (TypeError, ValueError):
        logger.warning("Ymane event startgps/endgps %r isn't parsable as 'lat,lon' — skipping", gps)
        return None

    event_id = raw.get("exceptionid")
    event_id = str(event_id) if event_id is not None else str(hash(json.dumps(raw, sort_keys=True, default=str)))

    occurred_at = _parse_ts(raw.get("startdatetime")) or datetime.now(timezone.utc)

    vmax = raw.get("vmax")
    threshold = raw.get("threshold")

    return HistoricalEvent(
        source=source,
        source_event_id=event_id,
        event_type=_map_exception_type(raw.get("exceptiontype"), vmax),
        occurred_at=occurred_at,
        latitude=lat,
        longitude=lon,
        vehicle_external_id=_stringify(raw.get("vehicleid")),
        driver_external_id=_stringify(raw.get("driverid")),
        speed_mps=_to_mps(vmax),
        # 0.0 shows up on every sample seen for fields that don't apply to
        # that exception — treat it as "not present" rather than a real
        # zero-valued speed limit.
        speed_limit_mps=_to_mps(threshold) if threshold else None,
        severity=_stringify(raw.get("level")),
        raw=raw,
    )


class YmaneEventSource(HistoricalEventSource):
    name = "ymane"

    def __init__(
        self,
        base_url: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
    ):
        self.base_url = (base_url or settings.YMANE_BASE_URL).rstrip("/")
        self.username = username or settings.YMANE_USERNAME
        self.password = password or settings.YMANE_PASSWORD
        self._token: Optional[str] = None

    def _post(self, path: str, body: dict, token: Optional[str] = None, query: str = "") -> dict:
        url = f"{self.base_url}{path}{query}"
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"  # see module docstring
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=settings.YMANE_TIMEOUT_S) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body_text = e.read().decode(errors="replace")[:500]
            raise YmaneRequestError(f"Ymane request to {path} failed: HTTP {e.code}: {body_text}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise YmaneRequestError(f"Ymane request to {path} failed: {e}") from e

    def _login(self) -> str:
        if not self.username or not self.password:
            raise YmaneAuthError("YMANE_USERNAME/YMANE_PASSWORD not configured")
        try:
            data = self._post(settings.YMANE_LOGIN_PATH, {"is1": self.username, "is2": self.password})
        except YmaneRequestError as e:
            raise YmaneAuthError(str(e)) from e
        token = data.get("access_token")
        if not token:
            raise YmaneAuthError(f"Ymane login response had no access_token: {data}")
        self._token = token
        return token

    async def fetch(self, start: date, end: date) -> List[HistoricalEvent]:
        token = self._token or self._login()

        events: List[HistoricalEvent] = []
        last_id = 0
        query = (
            f"?datedebut={start.isoformat()}&datefin={end.isoformat()}"
            f"&lastexceptionid={{last_id}}&nbrow={_PAGE_SIZE}"
        )
        # Empty lists + alarm/alert/record=False returns nothing even when
        # reachable (see module docstring) — this mirrors the one confirmed
        # request that actually returned data.
        body = {
            "alarm": True, "alert": True, "record": True,
            "listaffiliateids": [], "listclientids": [], "listdriverids": [],
            "listidtypeexception": _KNOWN_EXCEPTION_TYPE_IDS,
            "listtransporterids": [0], "listvehicleids": [],
        }

        while True:
            try:
                data = self._post(settings.YMANE_EXCEPTION_PATH, body, token=token, query=query.format(last_id=last_id))
            except YmaneRequestError:
                # Token may have expired mid-run — retry once with a fresh login.
                token = self._login()
                data = self._post(settings.YMANE_EXCEPTION_PATH, body, token=token, query=query.format(last_id=last_id))

            page = data.get("data", [])
            if not page:
                break
            for raw in page:
                mapped = _map_raw_event_fields(raw, self.name)
                if mapped is not None:
                    events.append(mapped)

            if not data.get("hasmoreElements"):
                break
            last_id = page[-1].get("exceptionid") or (last_id + len(page))

        logger.info("[YMANE] fetched %d events, mapped %d, %s..%s", len(events), len(events), start, end)
        return events


def _parse_ts(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # Confirmed epoch milliseconds (e.g. 1789001922000) — divide by 1000
        # unless it's implausibly small to be milliseconds already.
        seconds = value / 1000.0 if value > 10_000_000_000 else value
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    if isinstance(value, str):
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


def _stringify(value: Any) -> Optional[str]:
    return None if value is None else str(value)


def _to_mps(value: Any) -> Optional[float]:
    """Every vmax sample seen so far is plausible as km/h (e.g. 75, 61, 45,
    42) — converted to m/s for consistency with the rest of the platform."""
    if value is None:
        return None
    try:
        return float(value) / 3.6
    except (TypeError, ValueError):
        return None
