"""
VIASafe traffic-sign taxonomy — the sign codes a `traffic_sign` detection's
`subtype` can take (two-stage pipeline: the crop classifier's classes).

Source, first available wins (`current()`):
  1. `taxonomy.yaml` next to the sign classifier weights (MODEL_SIGNS_CLASSIFIER),
     shipped by the training project (ai/training/traffic-signs/);
  2. the classes of the classifier's model card;
  3. the built-in list below.
Names missing from the file / card come from the built-in list, else are
humanized from the code ("speed_limit_50" → "Speed limit 50 km/h").

Accepted `taxonomy.yaml` shapes (keys `classes`, `codes` or `signs`):

    version: v1                      # or taxonomy_version
    families:                        # optional: list of {code, name_en, name_fr} or {code: {name_en, name_fr}}
      - {code: prohibition, name_en: Prohibition, name_fr: Interdiction}
    classes:                         # list of {code, name_en, name_fr, family} …
      - {code: speed_limit_30, name_en: Speed limit 30 km/h, name_fr: Limitation 30 km/h, family: speed_limit}
    # … or a mapping  code: {name_en, name_fr, family}
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

UNKNOWN_SIGN = "unknown_sign"
TRAFFIC_LIGHT = "traffic_light"
NOT_A_SIGN = "not_a_sign"
# Classifier classes meaning "this crop is not a traffic sign" (sticker,
# billboard, person, vehicle, tree …): a confident prediction of one of them —
# or of the card's `thresholds.reject_class` — rejects the detection
# (ai_validation, code ai_not_a_sign). Never stored as a subtype, never in the
# taxonomy. `unknown_sign` is different: a sign the classifier cannot name →
# review.
NEGATIVE_CODES = frozenset({NOT_A_SIGN, "background", "not_sign", "not_traffic_sign", "no_sign"})


def is_negative(code: Optional[str], reject_class: Optional[str] = None) -> bool:
    return bool(code) and (code in NEGATIVE_CODES or code == reject_class)

FAMILIES: list[dict[str, str]] = [
    {"code": "priority",    "name_en": "Priority",           "name_fr": "Priorité"},
    {"code": "prohibition", "name_en": "Prohibition",        "name_fr": "Interdiction"},
    {"code": "speed_limit", "name_en": "Speed limits",       "name_fr": "Limitations de vitesse"},
    {"code": "mandatory",   "name_en": "Mandatory",          "name_fr": "Obligation"},
    {"code": "warning",     "name_en": "Warning",            "name_fr": "Danger"},
    {"code": "information", "name_en": "Information",        "name_fr": "Indication"},
    {"code": "signals",     "name_en": "Traffic signals",    "name_fr": "Signaux lumineux"},
    {"code": "other",       "name_en": "Other",              "name_fr": "Autre"},
]

_B = [  # (code, family, name_en, name_fr)
    ("stop", "priority", "Stop", "Stop"),
    ("give_way", "priority", "Give way", "Cédez le passage"),
    ("priority_road", "priority", "Priority road", "Route prioritaire"),
    ("end_priority_road", "priority", "End of priority road", "Fin de route prioritaire"),
    ("no_entry", "prohibition", "No entry", "Sens interdit"),
    ("no_vehicles", "prohibition", "No vehicles", "Circulation interdite"),
    ("no_overtaking", "prohibition", "No overtaking", "Interdiction de dépasser"),
    ("no_parking", "prohibition", "No parking", "Stationnement interdit"),
    ("no_stopping", "prohibition", "No stopping", "Arrêt et stationnement interdits"),
    ("no_left_turn", "prohibition", "No left turn", "Interdiction de tourner à gauche"),
    ("no_right_turn", "prohibition", "No right turn", "Interdiction de tourner à droite"),
    ("no_u_turn", "prohibition", "No U-turn", "Demi-tour interdit"),
    ("no_trucks", "prohibition", "No trucks", "Interdit aux poids lourds"),
    ("no_horn", "prohibition", "No horn", "Signaux sonores interdits"),
    ("end_prohibition", "prohibition", "End of all prohibitions", "Fin de toutes les interdictions"),
    ("end_speed_limit", "speed_limit", "End of speed limit", "Fin de limitation de vitesse"),
    *[(f"speed_limit_{v}", "speed_limit", f"Speed limit {v} km/h", f"Limitation {v} km/h")
      for v in range(10, 140, 10)],
    ("keep_right", "mandatory", "Keep right", "Contournement obligatoire par la droite"),
    ("keep_left", "mandatory", "Keep left", "Contournement obligatoire par la gauche"),
    ("turn_right_ahead", "mandatory", "Turn right ahead", "Obligation de tourner à droite"),
    ("turn_left_ahead", "mandatory", "Turn left ahead", "Obligation de tourner à gauche"),
    ("ahead_only", "mandatory", "Ahead only", "Direction obligatoire tout droit"),
    ("roundabout_mandatory", "mandatory", "Roundabout", "Sens giratoire obligatoire"),
    ("pedestrians", "warning", "Pedestrians", "Passage de piétons"),
    ("children", "warning", "Children", "Endroit fréquenté par les enfants"),
    ("curve_left", "warning", "Curve to the left", "Virage à gauche"),
    ("curve_right", "warning", "Curve to the right", "Virage à droite"),
    ("double_bend", "warning", "Double bend", "Succession de virages"),
    ("left_hairpin_curve", "warning", "Hairpin bend to the left", "Virage en épingle à gauche"),
    ("road_works", "warning", "Road works", "Travaux"),
    ("men_at_work", "warning", "Men at work", "Travaux"),
    ("slippery_road", "warning", "Slippery road", "Chaussée glissante"),
    ("falling_rocks", "warning", "Falling rocks", "Chute de pierres"),
    ("narrow_road", "warning", "Road narrows", "Chaussée rétrécie"),
    ("narrow_bridge", "warning", "Narrow bridge", "Pont étroit"),
    ("speed_bump", "warning", "Speed bump", "Ralentisseur"),
    ("uneven_road", "warning", "Uneven road", "Chaussée déformée"),
    ("crossroads", "warning", "Crossroads", "Intersection"),
    ("roundabout_ahead", "warning", "Roundabout ahead", "Carrefour giratoire"),
    ("traffic_signals_ahead", "warning", "Traffic signals ahead", "Feux tricolores"),
    ("animals", "warning", "Animals", "Passage d'animaux"),
    ("steep_descent", "warning", "Steep descent", "Descente dangereuse"),
    ("steep_ascent", "warning", "Steep ascent", "Montée dangereuse"),
    ("two_way_traffic", "warning", "Two-way traffic", "Circulation à double sens"),
    ("level_crossing", "warning", "Level crossing", "Passage à niveau"),
    ("general_danger", "warning", "Danger", "Autres dangers"),
    ("pedestrian_crossing", "information", "Pedestrian crossing", "Passage pour piétons"),
    ("parking", "information", "Parking", "Parking"),
    ("one_way", "information", "One-way road", "Sens unique"),
    ("dead_end", "information", "Dead end", "Impasse"),
    ("hospital", "information", "Hospital", "Hôpital"),
    ("bus_stop", "information", "Bus stop", "Arrêt de bus"),
    (TRAFFIC_LIGHT, "signals", "Traffic light", "Feu tricolore"),
    (UNKNOWN_SIGN, "other", "Unknown sign", "Panneau non identifié"),
]
BUILTIN: dict[str, dict[str, str]] = {c: {"code": c, "family": f, "name_en": en, "name_fr": fr} for c, f, en, fr in _B}

_SPEED = re.compile(r"^speed_limit_(\d+)$")


def humanize(code: str, lang: str = "en") -> str:
    """Display name of a code without a taxonomy entry."""
    m = _SPEED.match(code)
    if m:
        return f"Limitation {m.group(1)} km/h" if lang == "fr" else f"Speed limit {m.group(1)} km/h"
    text = code.replace("_", " ").strip()
    return text[:1].upper() + text[1:] if text else code


def guess_family(code: str) -> str:
    if code in BUILTIN:
        return BUILTIN[code]["family"]
    if _SPEED.match(code) or code.startswith("end_speed"):
        return "speed_limit"
    if code.startswith(("no_", "end_")):
        return "prohibition"
    return "other"


def entry(code: str, **given: Optional[str]) -> dict[str, str]:
    """A complete class entry: given values, else built-in, else humanized."""
    base = BUILTIN.get(code, {})
    return {
        "code": code,
        "name_en": given.get("name_en") or base.get("name_en") or humanize(code, "en"),
        "name_fr": given.get("name_fr") or base.get("name_fr") or humanize(code, "fr"),
        "family": given.get("family") or base.get("family") or guess_family(code),
    }


def _families(raw: Any) -> list[dict[str, str]]:
    known = {f["code"]: f for f in FAMILIES}
    items: list[tuple[str, dict]] = []
    if isinstance(raw, dict):
        items = [(str(k), v if isinstance(v, dict) else {}) for k, v in raw.items()]
    elif isinstance(raw, list):
        items = [(str(v.get("code")), v) for v in raw if isinstance(v, dict) and v.get("code")]
    out = []
    for code, v in items:
        base = known.get(code, {})
        out.append({"code": code,
                    "name_en": str(v.get("name_en") or base.get("name_en") or humanize(code)),
                    "name_fr": str(v.get("name_fr") or base.get("name_fr") or humanize(code, "fr"))})
    return out


def parse(raw: Any) -> dict[str, Any]:
    """Normalise the content of a taxonomy.yaml (raises ValueError when unusable)."""
    if not isinstance(raw, dict):
        raise ValueError("taxonomy must be a mapping")
    classes_raw = next((raw[k] for k in ("classes", "codes", "signs") if k in raw), None)
    items: list[tuple[str, dict]] = []
    if isinstance(classes_raw, dict):
        items = [(str(k), v if isinstance(v, dict) else {"name_en": v} if isinstance(v, str) else {})
                 for k, v in classes_raw.items()]
    elif isinstance(classes_raw, list):
        for v in classes_raw:
            if isinstance(v, str):
                items.append((v, {}))
            elif isinstance(v, dict) and v.get("code"):
                items.append((str(v["code"]), v))
    items = [(c, v) for c, v in items if c not in NEGATIVE_CODES]
    if not items:
        raise ValueError("taxonomy has no classes")
    classes = [entry(code, **{k: (str(v[k]) if v.get(k) is not None else None)
                              for k in ("name_en", "name_fr", "family")}) for code, v in items]
    version = raw.get("taxonomy_version", raw.get("version"))
    families = _families(raw.get("families")) or []
    return {"taxonomy_version": str(version) if version is not None else None,
            "classes": classes, "families": families}


def _finish(tax: dict[str, Any], source: str) -> dict[str, Any]:
    """Ensure traffic_light / unknown_sign exist and every family is described."""
    codes = {c["code"] for c in tax["classes"]}
    for extra in (TRAFFIC_LIGHT, UNKNOWN_SIGN):
        if extra not in codes:
            tax["classes"].append(entry(extra))
    families = list(tax.get("families") or [])
    have = {f["code"] for f in families}
    known = {f["code"]: f for f in FAMILIES}
    for c in tax["classes"]:
        if c["family"] not in have:
            families.append(dict(known.get(c["family"]) or {"code": c["family"], "name_en": humanize(c["family"]),
                                                              "name_fr": humanize(c["family"], "fr")}))
            have.add(c["family"])
    return {**tax, "families": families, "source": source}


_cache: dict[tuple[str, float, int], tuple[Optional[dict], Optional[str]]] = {}


def load_file(path: Path) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """(taxonomy, error) of a taxonomy.yaml; (None, None) when it does not exist."""
    try:
        st = path.stat()
    except OSError:
        return None, None
    key = (str(path), st.st_mtime, st.st_size)
    if key not in _cache:
        try:
            import yaml
            _cache[key] = (parse(yaml.safe_load(path.read_text(encoding="utf-8"))), None)
        except Exception as exc:   # yaml error, bad shape
            logger.warning(f"Sign taxonomy {path} ignored: {exc}")
            _cache[key] = (None, str(exc))
    return _cache[key]


def taxonomy_path(classifier_weights: Optional[str]) -> Optional[Path]:
    return Path(classifier_weights).parent / "taxonomy.yaml" if classifier_weights else None


def build(classifier_weights: Optional[str], card: Any = None) -> dict[str, Any]:
    """Taxonomy for a classifier install (see module docstring)."""
    path = taxonomy_path(classifier_weights)
    if path is not None:
        tax, _err = load_file(path)
        if tax is not None:
            if tax["taxonomy_version"] is None and card is not None:
                tax = {**tax, "taxonomy_version": getattr(card, "taxonomy_version", None)}
            reject = getattr(getattr(card, "thresholds", None), "reject_class", None)
            return _finish({**tax, "classes": [dict(c) for c in tax["classes"] if not is_negative(c["code"], reject)]},
                           "file")
    if card is not None and getattr(card, "classes", None):
        return _finish({"taxonomy_version": getattr(card, "taxonomy_version", None),
                        "classes": [entry(c) for c in card.classes
                                    if not is_negative(c, getattr(card.thresholds, "reject_class", None))],
                        "families": []}, "card")
    return _finish({"taxonomy_version": None, "classes": [dict(v) for v in BUILTIN.values()],
                    "families": [dict(f) for f in FAMILIES]}, "builtin")


def current() -> dict[str, Any]:
    """Taxonomy of the installed sign classifier (or the built-in one)."""
    from app.core.config import settings
    from app.modules.m05_ai_detection import model_registry
    return build(settings.MODEL_SIGNS_CLASSIFIER or None, model_registry.slot_card("signs_classifier"))
