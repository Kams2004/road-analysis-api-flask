"""
Catalogue of permission modules and the default roles (RRAP-01-009).

Each module maps to one area of the web console / API. The web sidebar and
action buttons read the same keys from GET /auth/me, so the menu always
reflects what the backend will actually allow (RRAP-01-007).
"""
from dataclasses import dataclass

ACTIONS = ("view", "add", "edit", "delete")


@dataclass(frozen=True)
class Module:
    key: str
    label: str
    group: str
    actions: tuple[str, ...] = ACTIONS


# Every module exposes the full View/Add/Edit/Delete set so administrators can
# compose any role from scratch. Keys are referenced by the API guards in
# app/main.py and by the web console menu — rename them only together.
MODULES: tuple[Module, ...] = (
    Module("dashboard",        "Dashboard",                        "Operations"),
    Module("map",              "Live map",                         "Operations"),
    Module("jobs",             "Upload, jobs & cloud imports",     "Operations"),
    Module("streams",          "Video streams (RTSP)",             "Operations"),
    Module("detections",       "AI hazard detection & hazards",    "Hazards"),
    Module("validations",      "Human validation",                 "Hazards"),
    Module("datasets",         "Training datasets & exports",      "Hazards"),
    Module("signalements",     "Community reporting",              "Hazards"),
    Module("navigate",         "Route navigation",                 "Risk"),
    Module("collisions",       "Collision prevention",             "Risk"),
    Module("road_risk",        "Road risk assessment (ARR)",       "Risk"),
    Module("historical",       "Historical risk analysis",         "Risk"),
    Module("driver_behaviour", "Driver behaviour",                 "Risk"),
    Module("reports",          "Reports & exports",                "Risk"),
    Module("settings",         "Platform settings",                "Administration"),
    Module("users",            "Users",                            "Administration"),
    Module("roles",            "Roles & permissions",              "Administration"),
    Module("api_clients",      "API clients",                      "Administration"),
    Module("audit",            "Audit trail",                      "Administration"),
)

MODULE_KEYS = {m.key for m in MODULES}
MODULE_BY_KEY = {m.key: m for m in MODULES}

ADMIN_ROLE_NAME = "Administrator"


# Seeded once, when the roles table is empty. Only the Administrator role is
# built in (it always holds everything); every other role is created by an
# administrator in Roles & Permissions.
DEFAULT_ROLES: list[dict] = [
    {
        "name": ADMIN_ROLE_NAME,
        "description": "Full access to every module, including users, roles and settings.",
        "is_system": True,
        "permissions": {m.key: m.actions for m in MODULES},
    },
]
