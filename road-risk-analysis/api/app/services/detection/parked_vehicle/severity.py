"""
Severity of a parked vehicle: `severity_by_subtype` (default blocking →
high, wrong_side → medium, roadside → low), raised by `heavy_vehicle_bump`
levels (default 1, capped at `critical`) for a heavy vehicle
(`heavy_vehicle_classes`, default bus / truck — harder to see past, longer
to go around). Detections have no severity column: the value is stored in
`ai_details.severity`.
"""
from __future__ import annotations

from typing import Optional

from .settings import SEVERITY_LEVELS, ParkedVehicleSettings


def severity(subtype: Optional[str], vehicle_class: str, s: ParkedVehicleSettings) -> Optional[str]:
    if subtype is None:
        return None
    base = s.severity_by_subtype.get(subtype, "low")
    level = SEVERITY_LEVELS.index(base)
    if vehicle_class in s.heavy_vehicle_classes:
        level = min(level + s.heavy_vehicle_bump, len(SEVERITY_LEVELS) - 1)
    return SEVERITY_LEVELS[level]
