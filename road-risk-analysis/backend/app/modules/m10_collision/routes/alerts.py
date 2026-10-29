"""
/alerts — collision alerts of live vehicles (Module 10, docs/COLLISION-PREVENTION.md).

  GET  /alerts/active                    fleet view
  GET  /alerts/{vehicle_id}              one vehicle's alerts (polling fallback)
  POST /alerts/{vehicle_id}/refresh      debug: run the alert engine now
  POST /alerts/{conflict_id}/feedback    driver verdict: false_alert | useful
  WS   /alerts/ws                        instant delivery (ws_router, mounted without the
                                         router guard: a WebSocket has no Request — it
                                         authenticates itself with the same headers)

WebSocket protocol (JSON text frames)
  client → {"vehicle_id": "…"}            subscribe (or switch vehicle); also ?vehicle_id=… in the URL
  client → {"type": "ping"}               → {"type": "pong", "server_time": …}
  server → {"type": "snapshot", "vehicle_id", "alerts": [ConflictAlert…], "server_time"}
                                         on (re)subscription — the same list as GET /alerts/{vehicle_id}
  server → {"type": "alert", "vehicle_id", "other_vehicle_id", "alert": ConflictAlert}
                                         every transition (NEW, ESCALATED, DE_ESCALATED, RESOLVED)
  server → {"type": "ping", "server_time"}   after keepalive_s without any other message
  server → {"type": "error", "code", "message"} then close (4401 not authenticated, 4403 forbidden,
                                         4400 no vehicle_id within subscribe_timeout_s)
Multi-process safe: transitions are published on Redis pub/sub `collision:alerts:{vehicle_id}`
by whichever process evaluated them; each socket subscribes to its vehicle's channel.
"""
import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, actor_name, get_principal, resolve_principal
from app.core.config import settings
from app.core.db.session import get_db
from app.core.storage.redis_client import get_redis
from app.modules.m10_collision import alert_service, road_state_service
from app.modules.m10_collision.alerts.models import ConflictAlert
from app.modules.m10_collision.models.alert_feedback import AlertFeedback
from app.modules.m10_collision.schemas import AlertFeedbackIn, AlertFeedbackOut
from app.api.openapi import errors, legacy

router = APIRouter()
ws_router = APIRouter()
logger = logging.getLogger(__name__)

KEEPALIVE_S = 20.0
SUBSCRIBE_TIMEOUT_S = 10.0
_POLL_S = 1.0
CONFLICT_ID = re.compile(r"^CF-\d{8}-[0-9a-f]{8}$")

_VEHICLE_ID = Path(..., description="Vehicle id (from `POST /vehicles/register`).",
                   examples=["5d0c3b1e-8a5f-4b8e-9d2c-1f3e7a9b6c40"])
_NO_TELEMETRY = errors(legacy(404, "Vehicle 5d0c3b1e-8a5f-4b8e-9d2c-1f3e7a9b6c40 has no live telemetry"))


# ── Fleet-wide view — read-only, no recompute (static path registered
# before /{vehicle_id} or FastAPI would treat "active" as a vehicle_id) ────

@router.get("/active", response_model=List[ConflictAlert], summary="List active alerts fleet-wide")
async def list_active_alerts():
    """
    Read-only fleet view: every alert that is not `RESOLVED`, across all live vehicles, each conflict
    once (both vehicles of a pair share the alert), most severe first. Levels: `SAFE` < `MONITOR` <
    `WARNING` < `HIGH_WARNING` < `CRITICAL`.
    """
    return await alert_service.get_active_alerts_fleet_wide()


# ── Read path for one vehicle — cache-first, kept warm by the background
# task fired from POST /vehicles/{id}/ping (see app/modules/m10_collision/routes/vehicles.py).
# Never triggers computation or notification. ──────────────────────────────

@router.get("/{vehicle_id}", response_model=List[ConflictAlert], summary="Get a vehicle's alerts")
async def get_vehicle_alerts(vehicle_id: str = _VEHICLE_ID):
    """
    **Mobile** — the vehicle's current alerts as computed by the last pipeline run triggered by its
    pings, most severe first (an unknown or idle vehicle gets `[]`). Read-only: never computes or
    notifies. Lifecycle: `DETECTED` (first tick above `SAFE`) → `ACTIVE` → `RESOLVED`; `transition` says
    what changed on the last tick (`NEW`, `ESCALATED`, `DE_ESCALATED`, `UNCHANGED`, `RESOLVED`).
    `level` is `physical_level` (physics alone) modulated by the context (`context_index`,
    `context_level_boost`, `context_reasons`). Polling fallback of the WebSocket `/alerts/ws`, which
    pushes every transition as it happens.
    """
    return await alert_service.get_alerts(vehicle_id)


# ── Debug: force a fresh alert-engine pass for this vehicle (does trigger
# notifications for any real transition found) ──────────────────────────────

@router.post("/{vehicle_id}/refresh", response_model=List[ConflictAlert],
             summary="Run the alert engine for a vehicle", responses=_NO_TELEMETRY)
async def refresh_vehicle_alerts(
    vehicle_id: str = _VEHICLE_ID,
    db: AsyncSession = Depends(get_db),
):
    """
    **Debug** — runs an alert-engine pass for this vehicle now and returns every alert it touched (new,
    updated or resolved; `[]` = nothing changed). Unlike the GET, this **does** send notifications for
    real transitions.
    """
    try:
        return await alert_service.process_vehicle(vehicle_id, db)
    except road_state_service.NoTelemetryError:
        raise HTTPException(404, f"Vehicle {vehicle_id} has no live telemetry")


# ── Driver feedback on one alert ─────────────────────────────────────────────

@router.post("/{conflict_id}/feedback", response_model=AlertFeedbackOut, summary="Give feedback on an alert",
             responses=errors(legacy(422, "Unknown conflict_id format")))
async def alert_feedback(
    body: AlertFeedbackIn,
    conflict_id: str = Path(..., description="`conflict_id` of the alert.", examples=["CF-20261029-3fa2b9c1"]),
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """
    **Mobile** — the driver says whether an alert was a `false_alert` or `useful` (optional `comment`).
    Idempotent: one answer per conflict × vehicle, a new answer replaces the previous one. The alert's
    level and context at that moment are stored with it (alerts themselves expire); the answers feed
    the false-alert rate of `GET /collision/metrics`.
    """
    if not CONFLICT_ID.match(conflict_id):
        raise HTTPException(422, "Unknown conflict_id format")
    alert = next((a for a in await alert_service.get_alerts(body.vehicle_id) if a.conflict_id == conflict_id), None)
    if alert is not None and body.vehicle_id not in (alert.vehicle_a_id, alert.vehicle_b_id):
        alert = None
    now = datetime.utcnow()
    values = dict(verdict=body.verdict, comment=(body.comment or "").strip() or None, updated_at=now,
                  submitted_by=actor_name(principal, principal.label))
    if alert is not None:
        values.update(level=alert.level.value,
                      physical_level=alert.physical_level.value if alert.physical_level else None,
                      context_index=alert.context_index, context_level_boost=alert.context_level_boost,
                      other_vehicle_id=alert.vehicle_b_id if alert.vehicle_a_id == body.vehicle_id else alert.vehicle_a_id)

    async def upsert() -> AlertFeedback:
        row = (await db.execute(select(AlertFeedback).where(
            AlertFeedback.conflict_id == conflict_id, AlertFeedback.vehicle_id == body.vehicle_id))).scalar_one_or_none()
        if row is None:
            row = AlertFeedback(conflict_id=conflict_id, vehicle_id=body.vehicle_id, created_at=now)
            db.add(row)
        for k, v in values.items():
            setattr(row, k, v)
        await db.commit()
        await db.refresh(row)
        return row

    try:
        return await upsert()
    except IntegrityError:                 # the same answer sent twice at once
        await db.rollback()
        return await upsert()


# ── WebSocket: instant delivery of alert transitions ─────────────────────────

class WsAuthError(Exception):
    def __init__(self, close_code: int, code: str, message: str):
        super().__init__(message)
        self.close_code, self.code, self.message = close_code, code, message


async def authorize_ws(headers) -> Principal:
    """Same credentials as the REST endpoints: an API key (mobile app — every client may
    subscribe) and / or a user Bearer token (collisions:view when AUTH_ENFORCED)."""
    from app.core.db.session import AsyncSessionLocal
    try:
        async with AsyncSessionLocal() as db:
            principal = await resolve_principal(headers, db)
    except HTTPException as e:
        detail = e.detail if isinstance(e.detail, dict) else {"code": "not_authenticated", "message": str(e.detail)}
        raise WsAuthError(4401, detail.get("code", "not_authenticated"), detail.get("message", ""))
    if principal.kind == "client" or not settings.AUTH_ENFORCED:
        return principal
    if principal.kind == "anonymous":
        raise WsAuthError(4401, "not_authenticated", "Please sign in.")
    if not principal.can("collisions", "view"):
        raise WsAuthError(4403, "forbidden", "You do not have permission for this action.")
    return principal


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def stream_alerts(websocket, r, vehicle_id: Optional[str], keepalive_s: float = KEEPALIVE_S,
                        subscribe_timeout_s: float = SUBSCRIBE_TIMEOUT_S, poll_s: float = _POLL_S) -> None:
    """Fan-out of one socket: snapshot on (re)subscription, every published transition of the
    vehicle's channel, keepalive pings. Returns when the client disconnects."""
    inbox: asyncio.Queue = asyncio.Queue()
    closed = object()

    async def reader():
        try:
            while True:
                text = await websocket.receive_text()
                try:
                    msg = json.loads(text)
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    await inbox.put(msg)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            await inbox.put(closed)

    pubsub = r.pubsub()
    current: Optional[str] = None
    last_sent = time.monotonic()
    started = last_sent

    async def send(payload: dict):
        nonlocal last_sent
        await websocket.send_text(json.dumps(payload))
        last_sent = time.monotonic()

    async def subscribe(vid: str):
        nonlocal current
        if current == vid:
            return
        if current is not None:
            await pubsub.unsubscribe(alert_service.alert_channel(current))
        await pubsub.subscribe(alert_service.alert_channel(vid))
        current = vid
        alerts = await alert_service.get_alerts(vid)
        await send({"type": "snapshot", "vehicle_id": vid, "server_time": _now_iso(),
                    "alerts": [json.loads(a.model_dump_json()) for a in alerts]})

    task = asyncio.create_task(reader())
    try:
        if vehicle_id:
            await subscribe(vehicle_id)
        while True:
            while not inbox.empty():
                msg = inbox.get_nowait()
                if msg is closed:
                    return
                if msg.get("vehicle_id"):
                    await subscribe(str(msg["vehicle_id"])[:64])
                elif msg.get("type") == "ping":
                    await send({"type": "pong", "server_time": _now_iso()})
            if current is None:
                if time.monotonic() - started > subscribe_timeout_s:
                    await send({"type": "error", "code": "vehicle_id_required",
                                "message": 'Send {"vehicle_id": "…"} (or ?vehicle_id=…) to subscribe.'})
                    await websocket.close(code=4400)
                    return
                try:
                    msg = await asyncio.wait_for(inbox.get(), timeout=poll_s)
                    await inbox.put(msg)
                except asyncio.TimeoutError:
                    pass
            else:
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=poll_s)
                if msg and msg.get("type") == "message":
                    data = msg["data"]
                    await websocket.send_text(data if isinstance(data, str) else data.decode())
                    last_sent = time.monotonic()
            if time.monotonic() - last_sent >= keepalive_s:
                await send({"type": "ping", "server_time": _now_iso()})
    except (WebSocketDisconnect, RuntimeError):
        return
    finally:
        task.cancel()
        try:
            if current is not None:
                await pubsub.unsubscribe()
            close = getattr(pubsub, "aclose", None) or pubsub.close
            await close()
        except Exception:
            pass


@ws_router.websocket("/ws")
async def alerts_ws(websocket: WebSocket, vehicle_id: Optional[str] = None):
    """WS /alerts/ws — see the module docstring for the protocol."""
    await websocket.accept()
    try:
        await authorize_ws(websocket.headers)
    except WsAuthError as e:
        await websocket.send_text(json.dumps({"type": "error", "code": e.code, "message": e.message}))
        await websocket.close(code=e.close_code)
        return
    await stream_alerts(websocket, get_redis(), vehicle_id, KEEPALIVE_S, SUBSCRIBE_TIMEOUT_S)
