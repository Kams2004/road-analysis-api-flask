"""Request / response schemas of live vehicle telemetry (Module 10 — /vehicles)."""
import logging
from pydantic import BaseModel, Field, field_validator
from datetime import datetime
from typing import Optional, List
from app.modules.m10_collision.models.vehicle import VehicleType

_schemas_logger = logging.getLogger(__name__)
SPEED_MPS_CEILING = 100.0


# ─── Vehicle telemetry schemas ────────────────────────────────────────────────

class VehicleRegisterIn(BaseModel):
    """Register (or re-register) a vehicle/device for live telemetry."""
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
        "GPS horizontal accuracy reported by the device, metres."), examples=[6.0])
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
    speed_mps:    Optional[float] = Field(description="Speed, m/s.", examples=[13.9])
    heading:      Optional[float] = Field(description="Heading, degrees clockwise from north (0–360).")
    accuracy_m:   Optional[float] = Field(None, description="GPS horizontal accuracy, metres.")
    acceleration_mps2: Optional[float] = Field(None, description="Device-reported acceleration, m/s².")
    ts:           datetime = Field(..., description="Timestamp of the latest ping.")
    distance_m:   Optional[float] = Field(None, description=(  # populated for /nearby queries
        "Distance from the query position, metres (only in `/vehicles/nearby`)."))


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
