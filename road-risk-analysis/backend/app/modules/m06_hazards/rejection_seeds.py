"""
Built-in rejection reasons (seeded at start-up, is_custom = False) and their
kind — what a rejection with the reason teaches the training data
(docs/TRAINING-DATA.md, app.modules.m06_hazards.training_data.layout):

    false_positive  AI mistake → negative training example
    real_redundant  the object is real but redundant → positive sample
    unusable        cannot be judged → excluded from training

Seeding is idempotent: missing reasons are added, and the label, description
and kind of the built-in ones are kept in step with this table (the code
never changes, so past rejections keep their meaning — e.g. bike_as_stop is
now labelled "Vehicle / motorbike part").
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.m06_hazards.models.rejection_reason import RejectionReason

FP, REDUNDANT, UNUSABLE = "false_positive", "real_redundant", "unusable"

# (detection_type, code, label, description, kind)
SEED_REASONS: list[tuple[str, str, str, str, str]] = [
    # ── applies to all types ───────────────────────────────────────────────
    ("all", "not_road_side",  "Not road-side",      "Detection is outside the road area (farm land, courtyard, private property)", FP),
    ("all", "image_quality",  "Poor image quality", "Frame is too blurry, dark, or overexposed to confirm", UNUSABLE),
    ("all", "duplicate",      "Duplicate detection", "Same hazard already captured in a nearby frame", REDUNDANT),
    ("all", "out_of_frame",   "Out of frame",       "Object is partially cut off and cannot be confirmed", UNUSABLE),
    ("all", "low_confidence", "Low confidence",     "Automatically rejected: confidence below the configured threshold", UNUSABLE),
    ("all", "ai_verifier",    "Rejected by AI verifier", "Automatically rejected: the crop classifier judged it is not this hazard type", FP),
    # ── pothole ────────────────────────────────────────────────────────────
    ("pothole", "shadow",           "Shadow / lighting",   "Dark patch caused by shadow, not a pothole", FP),
    ("pothole", "water_reflection", "Water reflection",    "Puddle reflection misidentified as a pothole", FP),
    ("pothole", "manhole",          "Manhole / drain",     "Manhole cover or drain grating, not a pothole", FP),
    ("pothole", "road_marking",     "Road marking",        "Painted road marking misidentified as a pothole", FP),
    ("pothole", "dirt_patch",       "Dirt / gravel patch", "Dirt or gravel patch on the road surface", FP),
    ("pothole", "ai_off_road",      "Off the road area",   "Automatically rejected: the box lies above the road-area line (trees, sky, roadside), not on the road surface", FP),
    ("pothole", "ai_box_too_large", "Box too large / roadside", "Automatically rejected: the box is too large for a pothole, or a wide box touching the image side (roadside earth, bushes, building edge)", FP),
    ("pothole", "crack_or_tyre_marks", "Crack / tyre marks", "Surface crack or tyre / skid marks misidentified as a pothole", FP),
    ("pothole", "road_patch",       "Road patch / tar repair edge", "Patched area or edge of a tar repair, not a hole", FP),
    ("pothole", "broken_edge",      "Broken road edge / shoulder", "Crumbling road edge or shoulder, not a pothole in the carriageway", FP),
    ("pothole", "vegetation",       "Vegetation / bush",   "Grass, bush or leaves misidentified as a pothole", FP),
    ("pothole", "debris",           "Object / debris (sand, stones)", "Sand, stones or another object on the road misidentified as a pothole", FP),
    # ── traffic sign ──────────────────────────────────────────────────────
    ("traffic_sign", "ai_not_a_sign",    "Not a traffic sign", "Automatically rejected: the sign classifier judged the crop is not a traffic sign (sticker, billboard, person, vehicle, tree …)", FP),
    ("traffic_sign", "person_as_sign",   "Person as sign",   "A pedestrian or person misidentified as a traffic sign", FP),
    ("traffic_sign", "bike_as_stop",     "Vehicle / motorbike part", "A vehicle or motorbike part (light, mirror, plate) misidentified as a sign", FP),
    ("traffic_sign", "pole_as_sign",     "Pole / post",       "Utility pole or fence post misidentified as a sign", FP),
    ("traffic_sign", "street_light",     "Street light",      "Street light or lamp post misidentified as a sign", FP),
    ("traffic_sign", "sign_not_visible", "Sign not visible",  "Sign is present but too distant or obstructed to read", UNUSABLE),
    ("traffic_sign", "billboard",        "Advertising board / shop sign", "Advertising board, shop or company sign misidentified as a traffic sign", FP),
    ("traffic_sign", "vehicle_sticker",  "Sticker or plate on a vehicle", "Sticker, logo or number plate on a vehicle misidentified as a traffic sign", FP),
    ("traffic_sign", "tree_as_sign",     "Tree / vegetation", "Tree, foliage or vegetation misidentified as a traffic sign", FP),
    # ── speed bump ────────────────────────────────────────────────────────
    ("speed_bump", "road_edge",           "Road edge / curb",  "Road edge or kerb misidentified as a speed bump", FP),
    ("speed_bump", "ditch",               "Ditch / trench",    "Drainage ditch or trench across the road", FP),
    ("speed_bump", "speed_bump_ok",       "Already validated", "Speed bump detected but previously validated nearby", REDUNDANT),
    ("speed_bump", "pothole_or_damage",   "Pothole / damaged road", "Pothole or damaged road surface misidentified as a speed bump", FP),
    ("speed_bump", "marking_or_crossing", "Road marking / pedestrian crossing", "Painted road marking or zebra crossing misidentified as a speed bump", FP),
    # ── parked vehicle (rules of app.modules.m05_ai_detection.parked_vehicle) ────
    # "not parked" verdicts are negatives of the parked-vehicle classifier.
    ("parked_vehicle", "ai_off_road",       "Off the road",       "Automatically rejected: the vehicle stands too far from the road (yard, garage, fuel station, car park)", FP),
    ("parked_vehicle", "ai_not_grounded",   "Not a real vehicle", "Automatically rejected: the vehicle does not stand on the ground (billboard / poster, car on a carrier, reflection)", FP),
    ("parked_vehicle", "ai_traffic_queue",  "Traffic queue",      "Automatically rejected: vehicle stopped in traffic ahead of us, not parked", FP),
    ("parked_vehicle", "ai_moving_vehicle", "Moving vehicle",     "Rejected: the vehicle was moving (same direction or oncoming), not parked", FP),
    ("parked_vehicle", "not_a_vehicle",     "Not a vehicle",      "The box is not a vehicle (object, shadow, building, sign …)", FP),
]


def seed_kind(code: str | None, detection_type: str | None = None) -> str | None:
    """Kind of a built-in reason code (type-specific first, then `all`); None if unknown."""
    if not code:
        return None
    by_type = next((k for t, c, _, _, k in SEED_REASONS if c == code and t == detection_type), None)
    return by_type or next((k for t, c, _, _, k in SEED_REASONS if c == code and t == "all"), None) \
        or next((k for _, c, _, _, k in SEED_REASONS if c == code), None)


async def seed_rejection_reasons(db: AsyncSession) -> None:
    """Add the missing built-in reasons and keep label / description / kind of
    the existing built-in ones in step with SEED_REASONS. Idempotent."""
    rows = {(r.detection_type, r.code): r for r in (await db.execute(select(RejectionReason))).scalars()}
    for det_type, code, label, description, kind in SEED_REASONS:
        row = rows.get((det_type, code))
        if row is None:
            db.add(RejectionReason(detection_type=det_type, code=code, label=label,
                                   description=description, is_custom=False, kind=kind))
        elif not row.is_custom and (row.label, row.description, row.kind) != (label, description, kind):
            row.label, row.description, row.kind = label, description, kind
    await db.commit()
