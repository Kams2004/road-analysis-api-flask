import uuid
import enum
from datetime import datetime
from sqlalchemy import String, Float, DateTime, Text, Integer, Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column
from app.db.session import Base


class SignalementType(str, enum.Enum):
    embouteillage      = "embouteillage"
    police             = "police"
    accident           = "accident"
    danger             = "danger"
    route_fermee       = "route_fermee"
    voie_bloquee       = "voie_bloquee"
    probleme_de_carte  = "probleme_de_carte"
    mauvais_temps      = "mauvais_temps"
    prix_carburant     = "prix_carburant"
    assistance_route   = "assistance_route"
    debogage           = "debogage"


class SignalementStatus(str, enum.Enum):
    actif    = "actif"
    annule   = "annule"
    rejete   = "rejete"


class Signalement(Base):
    __tablename__ = "signalements"

    id:          Mapped[str]            = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    type:        Mapped[SignalementType] = mapped_column(SAEnum(SignalementType))
    status:      Mapped[SignalementStatus] = mapped_column(SAEnum(SignalementStatus), default=SignalementStatus.actif)

    # Geolocation (required)
    latitude:    Mapped[float]          = mapped_column(Float)
    longitude:   Mapped[float]          = mapped_column(Float)

    # Optional fields from user
    description: Mapped[str | None]     = mapped_column(Text, nullable=True)
    image_url:   Mapped[str | None]     = mapped_column(String, nullable=True)
    reported_by: Mapped[str | None]     = mapped_column(String, nullable=True)  # user id or username

    # Admin moderation
    moderated_by:  Mapped[str | None]     = mapped_column(String, nullable=True)
    moderated_at:  Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    moderation_note: Mapped[str | None]   = mapped_column(Text, nullable=True)

    location_name: Mapped[str | None]    = mapped_column(String, nullable=True)
    blocked_bearing: Mapped[float | None]  = mapped_column(Float, nullable=True)  # degrees 0-360, closure direction
    confirmations:   Mapped[int]           = mapped_column(Integer, default=0)
    not_there_votes: Mapped[int]           = mapped_column(Integer, default=0)

    reported_at: Mapped[datetime]       = mapped_column(DateTime, default=datetime.utcnow)
