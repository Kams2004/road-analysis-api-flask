import uuid
import enum
from datetime import datetime
from sqlalchemy import String, DateTime, Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column
from app.core.db.session import Base


class VehicleType(str, enum.Enum):
    """Kind of connected vehicle: `bus`, `taxi`, `truck`, `private` (private
    car, the default) or `moto` (motorcycle / moto-taxi)."""
    bus     = "bus"
    taxi    = "taxi"
    truck   = "truck"
    private = "private"
    moto    = "moto"


class Vehicle(Base):
    """
    Durable registry of devices that have opted into live telemetry sharing.
    Current position/speed/heading are NOT stored here — they live in Redis
    (see app.modules.m10_collision.vehicle_service) since they change every few seconds
    and go stale on their own; this table only tracks identity.
    """
    __tablename__ = "vehicles"

    id:           Mapped[str]        = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    vehicle_type: Mapped[VehicleType] = mapped_column(SAEnum(VehicleType), default=VehicleType.private)
    created_at:   Mapped[datetime]   = mapped_column(DateTime, default=datetime.utcnow)
    last_seen:    Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Optional fleet identity (plate as in the fleet exception files, e.g. "LTTR 995 AR", and the
    # driver id): links the vehicle to its Module 5 behaviour score — the RISKY_DRIVER factor of
    # the collision Context Risk Index (docs/COLLISION-PREVENTION.md).
    plate:              Mapped[str | None] = mapped_column(String, nullable=True)
    driver_external_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # Hash of the owning device's secret (VehicleOut.device_secret, answered once): with an API key,
    # calls about this vehicle need it in X-Device-Secret. Null for vehicles registered before it.
    device_secret_hash: Mapped[str | None] = mapped_column(String, nullable=True)
