import uuid
import enum
from datetime import datetime
from sqlalchemy import Boolean, String, Float, DateTime, Text, Integer, Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column
from app.core.db.session import Base


class SignalementType(str, enum.Enum):
    """Kind of community report (values are French identifiers):

    | Value | Meaning |
    |---|---|
    | `embouteillage` | traffic jam |
    | `police` | police check / presence |
    | `accident` | accident |
    | `danger` | generic danger on the road |
    | `route_fermee` | road closed (see `blocked_bearing`) |
    | `voie_bloquee` | lane blocked |
    | `probleme_de_carte` | map problem (wrong/missing road on the map) |
    | `mauvais_temps` | bad weather |
    | `prix_carburant` | fuel price |
    | `assistance_route` | roadside assistance needed |
    | `debogage` | debugging / test report |
    | `nid_de_poule` | pothole |
    | `obstacle` | obstacle on the road |
    | `inondation` | flooding |
    | `autre` | other |
    | `vehicule_panne` | broken-down vehicle |
    | `dos_d_ane` | speed bump |
    | `route_glissante` | slippery road |
    | `eboulement` | landslide / rockfall |
    | `travaux` | road works |
    | `animaux` | animals on the road |
    | `vehicule_dangereux` | dangerous vehicle |

    How long a report stays fresh depends on its type
    (`community.expiry_hours_by_type`, default `community.default_expiry_hours`).
    """
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
    # Mobile "Report a hazard" tiles
    nid_de_poule       = "nid_de_poule"
    obstacle           = "obstacle"
    inondation         = "inondation"
    autre              = "autre"
    # Mobile report menu (v2)
    vehicule_panne     = "vehicule_panne"
    dos_d_ane          = "dos_d_ane"
    route_glissante    = "route_glissante"
    eboulement         = "eboulement"
    travaux            = "travaux"
    animaux            = "animaux"
    vehicule_dangereux = "vehicule_dangereux"


class SignalementStatus(str, enum.Enum):
    """Moderation status of a report: `actif` (active — shown while fresh),
    `annule` (cancelled — taken down by an administrator or hidden by the
    community after `community.hide_after_absent_votes` counted "not there"
    votes; restorable), `rejete` (rejected by an administrator as false or
    abusive; restorable)."""
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
    audio_url:       Mapped[str | None]    = mapped_column(String, nullable=True)

    # Community votes (see signalement_votes): only *counted* votes — cast
    # from within community.vote_max_distance_m, one per voter — feed
    # confirmations / not_there_votes, which mirror present / absent_count.
    present_count:      Mapped[int]             = mapped_column(Integer, default=0, server_default="0")
    absent_count:       Mapped[int]             = mapped_column(Integer, default=0, server_default="0")
    invalid_vote_count: Mapped[int]             = mapped_column(Integer, default=0, server_default="0")
    last_confirmed_at:  Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Devices that passed by (signalement_passes) within the ignore window
    # without voting; ≥ community.ignore_threshold → uncertain.
    ignore_count:       Mapped[int]             = mapped_column(Integer, default=0, server_default="0")
    uncertain:          Mapped[bool]            = mapped_column(Boolean, default=False, server_default="false")

    reported_at: Mapped[datetime]       = mapped_column(DateTime, default=datetime.utcnow)

    # Clustering (global Cluster Configuration, app.modules.m06_hazards.clustering): a
    # new report of the same type within the radius resolved at its position
    # is merged into the existing active report — it is kept (evidence) with
    # merged_into_id set and hidden from the app; the primary counts the
    # reports (report_count) and its freshness restarts at last_reported_at.
    merged_into_id:      Mapped[str | None]      = mapped_column(String, nullable=True, index=True)
    report_count:        Mapped[int]             = mapped_column(Integer, default=1, server_default="1")
    last_reported_at:    Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cluster_zone_id:     Mapped[int | None]      = mapped_column(Integer, nullable=True)
    cluster_environment: Mapped[str | None]      = mapped_column(String, nullable=True)
    cluster_radius_m:    Mapped[float | None]    = mapped_column(Float, nullable=True)
