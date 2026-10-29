"""Request / response schemas of live vehicle telemetry (Module 10 — /vehicles)."""
import logging
from pydantic import BaseModel, Field, field_validator
from datetime import datetime
from typing import Dict, List, Literal, Optional
from app.modules.m10_collision.models.vehicle import VehicleType

_schemas_logger = logging.getLogger(__name__)
SPEED_MPS_CEILING = 100.0


# ─── Vehicle telemetry schemas ────────────────────────────────────────────────

class VehicleRegisterIn(BaseModel):
    """Register (or re-register) a vehicle/device for live telemetry. Anonymous: nothing identifies
    the vehicle or its driver — the `plate` / `driver_external_id` sent by older app versions are
    accepted and ignored."""
    vehicle_id:   Optional[str] = Field(None, description=(  # client resends its stored id to re-register after reinstall/backend reset
        "Id previously issued to this device — resend it to re-register after a reinstall or a backend "
        "reset (the same id is kept, created if unknown). Omit to get a new UUID."),
        examples=["5d0c3b1e-8a5f-4b8e-9d2c-1f3e7a9b6c40"])
    vehicle_type: VehicleType = Field(VehicleType.private, description=(
        "Kind of vehicle (`bus`, `taxi`, `truck`, `private`, `moto`); updated on re-registration."),
        examples=["taxi"])


class VehicleOut(BaseModel):
    """A registered vehicle identity (live position is not stored here)."""
    id:           str = Field(..., description="Vehicle id — use it in `/vehicles/{vehicle_id}/ping`.")
    vehicle_type: VehicleType = Field(..., description="Kind of vehicle.")
    created_at:   datetime = Field(..., description="Registration time (UTC).")
    last_seen:    Optional[datetime] = Field(description=(
        "Last ping time (UTC), updated at most every `VEHICLE_LAST_SEEN_THROTTLE_S` seconds; null if never pinged."))
    device_secret: Optional[str] = Field(None, description=(
        "Secret of the device that owns the vehicle — answered ONCE, when the vehicle is created (or a "
        "vehicle registered before device secrets is claimed). Send it as `X-Device-Secret` on every call "
        "about this vehicle (ping, its alerts, trajectory, road state, conflicts, alert WebSocket); "
        "with an API key, calls without it are refused (403 `device_secret_invalid`)."))

    class Config:
        from_attributes = True


class VehiclePingIn(BaseModel):
    """One telemetry sample. Out-of-range values are rejected with a 422 validation error."""
    latitude:  float = Field(..., description="WGS84 latitude, degrees (−90…90).", examples=[4.0511])
    longitude: float = Field(..., description="WGS84 longitude, degrees (−180…180).", examples=[9.7679])
    speed_mps: Optional[float] = Field(None, description=(  # metres/second, as reported by device GPS
        "Ground speed in **m/s** as reported by the GPS. Negative values are rejected (422); values above "
        "100 m/s (360 km/h, e.g. a fast simulated demo drive) are clamped to 100 instead of rejecting the "
        "whole fix."), examples=[13.9])
    heading:   Optional[float] = Field(None, description=(  # degrees, 0-360, direction of travel
        "Direction of travel, degrees clockwise from north (0–360)."), examples=[87.5])
    accuracy_m: Optional[float] = Field(None, description=(  # device-reported GPS horizontal accuracy, metres
        "GPS horizontal accuracy reported by the device, metres. When omitted the collision engine assumes "
        "`collision.assumed_accuracy_m` (10 m by default)."), examples=[6.0])
    acceleration_mps2: Optional[float] = Field(None, description=(  # device-reported linear acceleration, if available (see Step 3)
        "Longitudinal acceleration in m/s² if the device measures it (−20…20); otherwise the trajectory "
        "predictor derives it from successive speeds."), examples=[0.4])
    ts:        Optional[datetime] = Field(None, description=(  # device timestamp; server time used if omitted
        "Device timestamp of the fix (ISO 8601, UTC if no offset); server time when omitted."))

    @field_validator("latitude")
    @classmethod
    def _lat_range(cls, v: float) -> float:
        if not (-90.0 <= v <= 90.0):
            raise ValueError("latitude out of range")
        return v

    @field_validator("longitude")
    @classmethod
    def _lon_range(cls, v: float) -> float:
        if not (-180.0 <= v <= 180.0):
            raise ValueError("longitude out of range")
        return v

    @field_validator("speed_mps")
    @classmethod
    def _speed_sane(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and v < 0.0:
            raise ValueError("speed_mps out of plausible range")
        if v is not None and v > SPEED_MPS_CEILING:
            # 360 km/h ceiling. Clamped rather than rejected: the position is
            # still worth keeping (the app's fast demo drive reports > 100 m/s,
            # and a 422 there dropped the phone out of the live set entirely).
            _schemas_logger.info("ping speed_mps %.1f clamped to %.0f", v, SPEED_MPS_CEILING)
            return SPEED_MPS_CEILING
        return v

    @field_validator("acceleration_mps2")
    @classmethod
    def _acceleration_sane(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and not (-20.0 <= v <= 20.0):  # ~2g either way — crash-level beyond this, treat as sensor noise
            raise ValueError("acceleration_mps2 out of plausible range")
        return v


class VehicleLiveOut(BaseModel):
    """Latest telemetry of a live vehicle (one that pinged within `VEHICLE_LIVE_TTL_S` seconds)."""
    vehicle_id:   str = Field(..., description="Vehicle id.")
    vehicle_type: VehicleType = Field(..., description="Kind of vehicle.")
    latitude:     float = Field(..., description="WGS84 latitude (degrees).", examples=[4.0511])
    longitude:    float = Field(..., description="WGS84 longitude (degrees).", examples=[9.7679])
    speed_mps:    Optional[float] = Field(description=(
        "Speed, m/s — smoothed over the last 3 pings (GPS jitter rejected, real braking kept)."), examples=[13.9])
    heading:      Optional[float] = Field(description=(
        "Heading, degrees clockwise from north (0–360) — smoothed like `speed_mps`; a stopped vehicle "
        "keeps the last heading it had while moving."))
    accuracy_m:   Optional[float] = Field(None, description="GPS horizontal accuracy, metres.")
    acceleration_mps2: Optional[float] = Field(None, description="Device-reported acceleration, m/s².")
    ts:           datetime = Field(..., description="Timestamp of the latest ping.")
    distance_m:   Optional[float] = Field(None, description=(  # populated for /nearby queries
        "Distance from the query position, metres (only in `/vehicles/nearby`)."))
    display_latitude:  Optional[float] = Field(None, description=(
        "Where to draw the vehicle, latitude: the GPS fix held still while it is stopped and snapped onto "
        "its road while it drives on one (raw fix otherwise). Null before its first processed ping."))
    display_longitude: Optional[float] = Field(None, description="Where to draw the vehicle, longitude.")
    display_snapped:   bool = Field(False, description="`display_*` is on the vehicle's map-matched road.")


class PingAck(BaseModel):
    """Answer to a ping: where to draw the vehicle, its road, and its current alert level."""
    display_latitude:  float = Field(..., description="Where to draw the vehicle (see `VehicleLiveOut`), latitude.")
    display_longitude: float = Field(..., description="Where to draw the vehicle, longitude.")
    snapped:    bool = Field(..., description="The display position is on the vehicle's road.")
    stationary: bool = Field(..., description="The vehicle is stopped (its display position is held still).")
    road:       Optional[List[List[float]]] = Field(None, description=(
        "The map-matched road around the vehicle, ±250 m, as [[lat, lon], …] — the app snaps its own "
        "GPS fixes onto it between pings. Null when the vehicle is not reliably on a mapped road."))
    road_name:  Optional[str] = Field(None, description="Name of that road.")
    alert_level: Optional[str] = Field(None, description=(
        "Highest level of the vehicle's active collision alerts (`MONITOR` … `CRITICAL`), null without any — "
        "lets an app running in the background notify the driver without its WebSocket."))
    alerts: int = Field(0, description="Number of active alerts.")


class VehicleNearbyIn(BaseModel):
    """Position around which to list live vehicles."""
    latitude:  float = Field(..., description="WGS84 latitude (degrees).", examples=[4.0511])
    longitude: float = Field(..., description="WGS84 longitude (degrees).", examples=[9.7679])
    radius_m:  Optional[float] = Field(None, description=(  # default & cap: mobile.visibility_radius_m
        "Search radius, metres. Default and maximum: `mobile.visibility_radius_m`."), examples=[1000])
    exclude_vehicle_id: Optional[str] = Field(None, description="Leave this vehicle out (typically the caller's own).")


class VehicleNearbyOut(BaseModel):
    """Live vehicles (nearest first for `/vehicles/nearby`)."""
    total: int = Field(..., description="Number of vehicles in `items`.")
    items: List[VehicleLiveOut] = Field(..., description="The vehicles.")


# ─── Alert feedback & metrics (docs/COLLISION-PREVENTION.md) ──────────────────

class AlertFeedbackIn(BaseModel):
    """A driver's verdict on one alert."""
    vehicle_id: str = Field(..., min_length=1, max_length=64, description="The vehicle (device) answering.")
    verdict:    Literal["false_alert", "useful"] = Field(..., description="`false_alert` or `useful`.")
    comment:    Optional[str] = Field(None, max_length=1000, description="Free text, optional.")


class AlertFeedbackOut(BaseModel):
    """The stored verdict (one per conflict × vehicle; answering again replaces it)."""
    conflict_id: str = Field(..., description="The alert's `conflict_id`.")
    vehicle_id:  str = Field(..., description="The vehicle that answered.")
    verdict:     str = Field(..., description="`false_alert` or `useful`.")
    comment:     Optional[str] = Field(None, description="Free text.")
    level:       Optional[str] = Field(None, description="Alert level when answered (null if the alert had expired).")
    physical_level: Optional[str] = Field(None, description="Level from physics alone when answered.")
    context_index: Optional[float] = Field(None, description="Context Risk Index when answered.")
    context_level_boost: Optional[int] = Field(None, description="Context boost (0/1) when answered.")
    created_at:  datetime = Field(..., description="First answer (UTC).")
    updated_at:  datetime = Field(..., description="Latest answer (UTC).")

    model_config = {"from_attributes": True}


class MetricsHour(BaseModel):
    hour: datetime = Field(..., description="Start of the hour (UTC).")
    total: int = Field(..., description="Alerts raised (NEW or ESCALATED) in that hour.")
    by_level: Dict[str, int] = Field(..., description="Alerts raised per level.")


class MetricsLatency(BaseModel):
    median: Optional[float] = Field(None, description="Median detection latency, ms (telemetry fix → alert).")
    p90: Optional[float] = Field(None, description="90th percentile, ms.")
    samples: int = Field(0, description="Number of measured alerts.")


class MetricsFeedback(BaseModel):
    total: int = Field(0, description="Answers in the period.")
    false_alert: int = Field(0, description="`false_alert` answers.")
    useful: int = Field(0, description="`useful` answers.")
    false_alert_rate: Optional[float] = Field(None, description="false_alert / total (null without answers).")
    by_level: Dict[str, Dict[str, int]] = Field({}, description="Answers per alert level.")


class CollisionMetricsOut(BaseModel):
    """Collision-prevention activity over a period."""
    period: str = Field(..., description="`1h`, `6h`, `24h` or `7d`.")
    from_: datetime = Field(..., alias="from", description="Start of the first hour counted (UTC).")
    to: datetime = Field(..., description="End of the period (UTC).")
    alerts_total: int = Field(..., description="Alerts raised (transition NEW or ESCALATED).")
    by_level: Dict[str, int] = Field(..., description="Alerts raised per level.")
    by_hour: List[MetricsHour] = Field(..., description="Hourly breakdown.")
    context_boosts: int = Field(..., description="Alerts raised one level by a high context index.")
    context_raised: int = Field(..., description="Alerts whose level is above their physical level (widening or boost).")
    detection_latency_ms: MetricsLatency
    feedback: MetricsFeedback

    model_config = {"populate_by_name": True}
