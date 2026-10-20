"""Request / response schemas of community reports (Module 11 — /signalements)."""
from pydantic import BaseModel, Field
from datetime import datetime
from typing import Literal, Optional, List
from app.modules.m11_community.models.signalement import SignalementType, SignalementStatus


# ─── Signalement schemas ──────────────────────────────────────────────────────

class SignalementIn(BaseModel):
    """Fields of a community report (the create endpoint takes them as multipart form fields)."""
    type:        SignalementType = Field(..., description="Report type.", examples=["police"])
    latitude:    float = Field(..., description="WGS84 latitude (degrees).", examples=[4.0511])
    longitude:   float = Field(..., description="WGS84 longitude (degrees).", examples=[9.7679])
    description: Optional[str] = Field(None, description="Free-text comment from the reporter.")
    reported_by: Optional[str] = Field(None, description="Reporter name (replaced by the signed-in user's e-mail).")


class SignalementOut(BaseModel):
    """A community report (*signalement*) with its moderation state, community-vote tallies and
    freshness. Counts only include **counted** votes (cast within `community.vote_max_distance_m`, one
    per voter)."""
    id:              str = Field(..., description="Report id (UUID).")
    type:            SignalementType = Field(..., description="Report type.", examples=["police"])
    status:          SignalementStatus = Field(..., description=(
        "`actif` (active), `annule` (cancelled by an admin or hidden by the community), `rejete` (rejected "
        "by an admin)."), examples=["actif"])
    latitude:        float = Field(..., description="WGS84 latitude (degrees).", examples=[4.0511])
    longitude:       float = Field(..., description="WGS84 longitude (degrees).", examples=[9.7679])
    location_name:   Optional[str] = Field(description="Place name reverse-geocoded at creation.",
                                           examples=["Akwa, Douala"])
    blocked_bearing: Optional[float] = Field(description=(
        "Blocked direction of travel, degrees clockwise from north (0–360), for closures."))
    confirmations:   int = Field(description="Legacy mirror of `present_count`.")
    not_there_votes: int = Field(description="Legacy mirror of `absent_count`.")
    description:     Optional[str] = Field(description="Reporter's comment.")
    image_url:       Optional[str] = Field(description="URL of the attached photo, if any.")
    audio_url:       Optional[str] = Field(description="URL of the attached voice note, if any.")
    reported_by:     Optional[str] = Field(description="Reporter (signed-in user's e-mail, or the name sent).")
    moderated_by:    Optional[str] = Field(description=(
        "Who last changed the status (`community` when hidden automatically by votes)."))
    moderated_at:    Optional[datetime] = Field(description="When the status was last changed (UTC).")
    moderation_note: Optional[str] = Field(description="Moderation note.")
    reported_at:     datetime = Field(description="Creation time (UTC).")
    # Community votes — counted votes only (cast from within
    # community.vote_max_distance_m, one per voter); invalid = too far / no position.
    present_count:      int = Field(0, description="Counted \"still there\" votes.")
    absent_count:       int = Field(0, description=(
        "Counted \"not there\" votes; at `community.hide_after_absent_votes` the report is set to `annule`."))
    invalid_vote_count: int = Field(0, description="Votes not counted because cast too far away or without a position.")
    last_confirmed_at:  Optional[datetime] = Field(None, description="Time of the last counted \"still there\" vote (UTC).")
    # Devices that passed nearby without voting (community.ignore_window_minutes)
    ignore_count:       int = Field(0, description=(
        "Devices that passed by within `community.ignore_window_minutes` without voting."))
    uncertain:          bool = Field(False, description=(
        "`ignore_count` reached `community.ignore_threshold` (and fewer than "
        "`community.keep_visible_present_votes` present votes)."))
    # Clustering (global Cluster Configuration — GET /clustering/config)
    merged_into_id:     Optional[str] = Field(None, description=(
        "Set when this report was merged into an existing active report of the same type within the "
        "clustering radius (that report's id); merged reports are not listed by the app searches."))
    report_count:       int = Field(1, description="Reports clustered into this one (itself included).")
    last_reported_at:   Optional[datetime] = Field(None, description=(
        "Time of the latest report merged into this one (UTC); restarts the freshness."))
    cluster_radius_m:   Optional[float] = Field(None, description=(
        "Clustering radius resolved at the report's position for its type, metres."))
    cluster_environment: Optional[Literal["urban", "rural"]] = Field(None, description=(
        "Profile of the resolution: `urban` (in agglomeration) or `rural` (out of agglomeration)."))
    cluster_zone_id:    Optional[int] = Field(None, description="Cluster zone of the resolution (null = unzoned).")
    # Freshness (community.expiry_hours_by_type + confirm_extends_minutes)
    expires_at:         Optional[datetime] = Field(None, description=(
        "When the report stops being shown: `reported_at` + the type's expiry, or `last_confirmed_at` + "
        "`community.confirm_extends_minutes`, whichever is later (UTC)."))
    expired:            bool = Field(False, description="`expires_at` is in the past.")

    class Config:
        from_attributes = True


class SignalementDetailOut(SignalementOut):
    """A report plus the caller's own vote."""
    my_vote: Optional[Literal["present", "absent"]] = Field(None, description=(
        "The caller's current counted vote (signed-in user, else `device_id`), or null."))


_VOTER_POS = "Voter's current WGS84 {} (degrees); without a position the vote is stored but not counted."


class SignalementVoteIn(BaseModel):
    """A "Still there?" vote. Counted only when the position is within `community.vote_max_distance_m`
    metres of the report; one counted vote per voter."""
    vote:      Literal["present", "absent"] = Field(..., description=(
        "`present` = still there, `absent` = not there any more."), examples=["present"])
    latitude:  Optional[float] = Field(None, description=_VOTER_POS.format("latitude"), examples=[4.0512])
    longitude: Optional[float] = Field(None, description=_VOTER_POS.format("longitude"), examples=[9.7681])
    device_id: Optional[str]   = Field(None, description=(  # vehicle / device id (identifies an anonymous voter)
        "Device or vehicle id identifying an anonymous voter (ignored for identity when a user is signed "
        "in). Truncated to 128 characters."), examples=["veh-3f2a"])


class SignalementLegacyVoteIn(BaseModel):
    """Optional body of /confirm and /not-there (older app versions send none)."""
    latitude:  Optional[float] = Field(None, description=_VOTER_POS.format("latitude"))
    longitude: Optional[float] = Field(None, description=_VOTER_POS.format("longitude"))
    device_id: Optional[str]   = Field(None, description="Device id identifying an anonymous voter.")


class SignalementVoteResultOut(BaseModel):
    """Outcome of a vote."""
    signalement: SignalementOut = Field(..., description="The report after the vote (status may now be `annule`).")
    my_vote:     Optional[Literal["present", "absent"]] = Field(None, description=(
        "The voter's current counted vote — this one if counted, else their earlier counted vote, if any."))
    counted:     bool = Field(..., description="Whether this vote counts.")
    reason:      Optional[Literal["too_far", "duplicate", "no_position"]] = Field(None, description=(
        "Why the vote does not count: `too_far` (beyond `community.vote_max_distance_m`), `no_position` "
        "(no/invalid position). `duplicate` marks a vote replaced by a later one. Null when counted."))
    distance_m:  Optional[float] = Field(None, description="Distance from the voter to the report, metres.")


class SignalementPassIn(BaseModel):
    """A pass-by impression: the device came near a displayed report."""
    device_id: Optional[str]   = Field(None, description=(  # required unless a user is signed in
        "Device id — required unless a user is signed in (`422 device_id_required`)."), examples=["veh-3f2a"])
    latitude:  Optional[float] = Field(None, description="Device's WGS84 latitude (degrees).")
    longitude: Optional[float] = Field(None, description="Device's WGS84 longitude (degrees).")


class SignalementPassResultOut(BaseModel):
    """Outcome of a pass-by impression."""
    signalement: SignalementOut = Field(..., description="The report after the pass (`ignore_count`, `uncertain`).")
    counted:     bool = Field(..., description="The pass was within `community.vote_max_distance_m`.")
    distance_m:  Optional[float] = Field(None, description="Distance from the device to the report, metres.")
    duplicate:   bool = Field(False, description=(
        "A pass was already recorded for this device in the current ignore window."))


class SignalementPassOut(BaseModel):
    """A recorded pass-by (admin view)."""
    id:          str = Field(..., description="Pass id.")
    device_id:   Optional[str] = Field(description="Device id.")
    user_id:     Optional[str] = Field(description="Signed-in user id, if any.")
    voter:       Optional[str] = Field(None, description="Display label: e-mail if signed in, else device id.")
    latitude:    Optional[float] = Field(description="WGS84 latitude (degrees).")
    longitude:   Optional[float] = Field(description="WGS84 longitude (degrees).")
    distance_m:  Optional[float] = Field(description="Distance to the report, metres.")
    counted:     bool = Field(..., description="Within `community.vote_max_distance_m`.")
    voted:       bool = Field(..., description="This device/user also voted on the report (so not an ignore).")
    created_at:  datetime = Field(..., description="When the pass was recorded (UTC).")


class SignalementVoteOut(BaseModel):
    """A stored vote (admin view), counted or not."""
    id:              str = Field(..., description="Vote id.")
    vote:            str = Field(..., description="`present` or `absent`.", examples=["absent"])
    voter_user_id:   Optional[str] = Field(description="Signed-in voter's user id.")
    voter_email:     Optional[str] = Field(None, description="Signed-in voter's e-mail.")
    voter_device_id: Optional[str] = Field(description="Voter's device id.")
    voter:           Optional[str] = Field(None, description="Display label: e-mail if signed in, else device id.")
    latitude:        Optional[float] = Field(description="Voter's WGS84 latitude (degrees).")
    longitude:       Optional[float] = Field(description="Voter's WGS84 longitude (degrees).")
    distance_m:      Optional[float] = Field(description="Distance to the report, metres (null without position).")
    counted:         bool = Field(..., description="Whether the vote counts.")
    reason:          Optional[str] = Field(description="`too_far`, `no_position`, `duplicate` (replaced) or null.")
    created_at:      datetime = Field(..., description="When the vote was cast (UTC).")


class SignalementVoteListOut(BaseModel):
    """Votes and pass-bys of one report, with its tallies."""
    total:              int = Field(..., description="Number of votes in `items`.")
    present_count:      int = Field(..., description="Counted \"still there\" votes.")
    absent_count:       int = Field(..., description="Counted \"not there\" votes.")
    invalid_vote_count: int = Field(..., description="Votes with reason `too_far` or `no_position`.")
    vote_max_distance_m: int = Field(..., description="Current `community.vote_max_distance_m`, metres.")
    ignore_count:       int = Field(0, description="Devices that passed in the ignore window without voting.")
    uncertain:          bool = Field(False, description="The report is flagged uncertain.")
    items:              List[SignalementVoteOut] = Field(..., description="Votes, newest first.")
    passes:             List[SignalementPassOut] = Field([], description="Pass-bys, newest first.")


class FlaggedVoterOut(BaseModel):
    """A voter with invalid votes (possible false testimony)."""
    voter:           Optional[str] = Field(description="Display label: e-mail if signed in, else device id.")
    voter_user_id:   Optional[str] = Field(description="User id (signed-in voter).")
    voter_email:     Optional[str] = Field(description="User e-mail (signed-in voter).")
    voter_device_id: Optional[str] = Field(description="Device id.")
    invalid_votes:   int = Field(..., description="Votes not counted because `too_far` or `no_position`.")
    too_far_votes:   int = Field(..., description="Votes cast beyond `community.vote_max_distance_m`.")
    total_votes:     int = Field(..., description="All votes by this voter.")
    reports:         int = Field(..., description="Distinct reports voted on.")
    last_vote_at:    datetime = Field(..., description="Most recent vote (UTC).")


class SignalementListOut(BaseModel):
    """A page of community reports."""
    total: int = Field(..., description="Number of matching reports (before paging).")
    items: List[SignalementOut] = Field(..., description="The reports.")


class SignalementModerationIn(BaseModel):
    """Moderation decision on a report."""
    status:       SignalementStatus = Field(..., description=(  # annule | rejete
        "`annule` (cancel) or `rejete` (reject) takes the report down; `actif` restores it."),
        examples=["rejete"])
    moderated_by: str = Field(..., description=(
        "Moderator name (replaced by the signed-in user's e-mail)."), examples=["ops@camtrack.cm"])
    note:         Optional[str] = Field(None, description="Reason / note stored as `moderation_note`.",
                                        examples=["Duplicate of an existing report."])


class SignalementNearbyIn(BaseModel):
    """Position around which to list visible reports."""
    latitude:  float = Field(..., description="WGS84 latitude (degrees).", examples=[4.0511])
    longitude: float = Field(..., description="WGS84 longitude (degrees).", examples=[9.7679])
    radius_m:  Optional[float] = Field(None, description=(  # default & cap: mobile.visibility_radius_m
        "Search radius, metres. Default and maximum: `mobile.visibility_radius_m`."), examples=[1000])
