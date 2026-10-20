"""
Effective lifecycle policy of a hazard — pure.

    policy = settings.policies[type]   (else settings.default_policy)
           ⊕ policy.subtype_overrides[subtype]
           ⊕ heavy vehicle: expiry_hours ← heavy_vehicle_expiry_hours
"""
from dataclasses import dataclass
from typing import Iterable, Optional

from app.services.hazards.lifecycle.settings import HazardLifecycleSettings, TypePolicy


@dataclass(frozen=True)
class EffectivePolicy:
    type: str
    subtype: Optional[str]
    expiry_hours: Optional[float]
    absence_resolution: bool
    absences_to_resolve: int
    require_same_direction: bool
    pass_daylight_only: bool
    votes_enabled: bool
    not_there_to_resolve: int
    still_there_extends: bool
    reopen_on_sighting: bool
    reopen_window_hours: Optional[float]
    recurring_enabled: bool
    show_to_drivers: bool
    alert_priority: str
    heavy_vehicle: bool = False


def type_policy(cfg: HazardLifecycleSettings, det_type: str) -> TypePolicy:
    return cfg.policies.get(det_type) or cfg.default_policy


def is_heavy(vehicle_class: Optional[str], cfg: HazardLifecycleSettings) -> bool:
    return bool(vehicle_class) and vehicle_class.lower() in {c.lower() for c in cfg.heavy_vehicle_classes}


def resolve_policy(cfg: HazardLifecycleSettings, det_type: str, subtype: Optional[str] = None,
                   vehicle_class: Optional[str] = None) -> EffectivePolicy:
    base = type_policy(cfg, det_type)
    fields = base.model_dump(exclude={"subtype_overrides"})
    if subtype and subtype in base.subtype_overrides:
        fields.update(base.subtype_overrides[subtype])
    heavy = is_heavy(vehicle_class, cfg)
    expiry = fields["expiry_hours"]
    if heavy and fields.get("heavy_vehicle_expiry_hours"):
        expiry = fields["heavy_vehicle_expiry_hours"]
    return EffectivePolicy(
        type=det_type, subtype=subtype, expiry_hours=float(expiry) if expiry is not None else None,
        absence_resolution=bool(fields["absence_resolution"]),
        absences_to_resolve=int(fields["absences_to_resolve"]),
        require_same_direction=bool(fields["require_same_direction"]),
        pass_daylight_only=bool(fields["pass_daylight_only"]),
        votes_enabled=bool(fields["votes_enabled"]),
        not_there_to_resolve=int(fields["not_there_to_resolve"]),
        still_there_extends=bool(fields["still_there_extends"]),
        reopen_on_sighting=bool(fields["reopen_on_sighting"]),
        reopen_window_hours=(float(fields["reopen_window_hours"]) if fields.get("reopen_window_hours") is not None
                             else (float(expiry) if expiry is not None else None)),
        recurring_enabled=bool(fields["recurring_enabled"]),
        show_to_drivers=bool(fields["show_to_drivers"]),
        alert_priority=str(fields["alert_priority"]),
        heavy_vehicle=heavy,
    )


def vehicle_class_of(det) -> Optional[str]:
    """`vehicle_class` from a detection's evidence details (parked vehicles:
    detections.ai_details), if any."""
    for attr in ("ai_details", "details"):
        details = getattr(det, attr, None)
        if isinstance(details, dict) and details.get("vehicle_class"):
            return str(details["vehicle_class"])
    return None


def hazard_vehicle_class(members: Iterable, cfg: HazardLifecycleSettings) -> Optional[str]:
    """The class that drives a hazard's policy: a heavy class if any member is
    heavy, else the most recent known class."""
    classes = [vehicle_class_of(m) for m in members]
    known = [c for c in classes if c]
    heavy = next((c for c in known if is_heavy(c, cfg)), None)
    return heavy or (known[0] if known else None)


def policy_for_hazard(cfg: HazardLifecycleSettings, hazard, members: Iterable = ()) -> EffectivePolicy:
    """A hazard's subtype is only set for distinct-subtype types; otherwise the
    most recent member's subtype selects the override."""
    members = list(members)
    subtype = hazard.subtype or next((m.subtype for m in members if getattr(m, "subtype", None)), None)
    return resolve_policy(cfg, hazard.type, subtype, hazard_vehicle_class(members, cfg))
