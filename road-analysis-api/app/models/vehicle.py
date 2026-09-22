import uuid
import enum
from datetime import datetime
from sqlalchemy import String, DateTime, Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column
from app.db.session import Base


class VehicleType(str, enum.Enum):
    bus     = "bus"
    taxi    = "taxi"
    truck   = "truck"
    private = "private"
    moto    = "moto"


class Vehicle(Base):
    """
    Durable registry of devices that have opted into live telemetry sharing.
    Current position/speed/heading are NOT stored here — they live in Redis
    (see app.services.vehicle_service) since they change every few seconds
    and go stale on their own; this table only tracks identity.
    """
    __tablename__ = "vehicles"

    id:           Mapped[str]        = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    vehicle_type: Mapped[VehicleType] = mapped_column(SAEnum(VehicleType), default=VehicleType.private)
    created_at:   Mapped[datetime]   = mapped_column(DateTime, default=datetime.utcnow)
    last_seen:    Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
