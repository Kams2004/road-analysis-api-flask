"""
The OpenAPI document (RRAP-12-006 / RRAP-12-014) must describe every route:
summary, exactly one known tag, a security requirement derived from the
guards, and the committed docs/openapi.json must match the live schema.
"""
import json
from pathlib import Path

import pytest

from app.api.openapi import TAG_NAMES
from app.main import app

METHODS = ("get", "put", "post", "delete", "patch", "head", "options")
EXPORT = Path(__file__).resolve().parent.parent / "docs" / "openapi.json"

# Endpoints anyone may call without credentials. Adding a route here is a
# security decision — keep the list explicit.
PUBLIC = {
    ("get", "/health"),
    ("get", "/auth/config"),
    ("post", "/auth/login"),
    ("post", "/auth/google"),
    ("post", "/auth/mfa/verify"),
    ("post", "/auth/mfa/setup"),       # challenge token in the body, or Bearer
    ("post", "/auth/mfa/enable"),      # idem
    ("post", "/auth/password/forgot"),
    ("post", "/auth/password/reset/check"),
    ("post", "/auth/password/reset"),
}


@pytest.fixture(scope="module")
def schema() -> dict:
    app.openapi_schema = None
    return app.openapi()


def _operations(schema):
    for path, item in schema["paths"].items():
        for method, op in item.items():
            if method in METHODS:
                yield method, path, op


def test_schema_builds(schema):
    assert schema["openapi"].startswith("3.")
    assert schema["info"]["title"] == "VIASafe Road Risk Analysis Platform API"
    assert {"BearerAuth", "ApiKeyAuth"} <= set(schema["components"]["securitySchemes"])
    assert len(list(_operations(schema))) > 100


def test_tags_are_declared_and_ordered(schema):
    assert [t["name"] for t in schema["tags"]] == list(TAG_NAMES)
    assert all(t.get("description") for t in schema["tags"])
    used = {op["tags"][0] for _, _, op in _operations(schema)}
    assert used == set(TAG_NAMES), f"declared but unused: {set(TAG_NAMES) - used}"


def test_every_operation_is_documented(schema):
    problems = []
    for method, path, op in _operations(schema):
        where = f"{method.upper()} {path}"
        if not (op.get("summary") or "").strip():
            problems.append(f"{where}: no summary")
        if len(op.get("tags", [])) != 1 or op["tags"][0] not in TAG_NAMES:
            problems.append(f"{where}: tags {op.get('tags')} (need exactly one of TAG_NAMES)")
        if "**Authentication:**" not in op.get("description", ""):
            problems.append(f"{where}: no authentication note")
        if not any(code.startswith("2") for code in op.get("responses", {})):
            problems.append(f"{where}: no success response")
    assert not problems, "\n".join(problems)


def test_operation_ids_are_unique(schema):
    ids = [op["operationId"] for _, _, op in _operations(schema)]
    assert len(ids) == len(set(ids))


def test_security_matches_guards(schema):
    problems = []
    for method, path, op in _operations(schema):
        where = f"{method.upper()} {path}"
        security = op.get("security")
        if security is None:
            problems.append(f"{where}: no security requirement")
            continue
        public = security == [] or {} in security
        if public and (method, path) not in PUBLIC:
            problems.append(f"{where}: documented as public but not in PUBLIC")
        if not public and (method, path) in PUBLIC:
            problems.append(f"{where}: in PUBLIC but requires credentials")
        if not public:
            if "401" not in op["responses"]:
                problems.append(f"{where}: no 401 response")
        perm = op.get("x-permission")
        if perm:
            first = perm.split(" | ")[0]
            if f"`{first}`" not in op["description"]:
                problems.append(f"{where}: permission {perm} not stated in the description")
    assert not problems, "\n".join(problems)


def test_known_permissions(schema):
    from app.core.permissions import MODULE_KEYS
    for method, path, op in _operations(schema):
        for perm in filter(None, (op.get("x-permission") or "").split(" | ")):
            module, _, action = perm.partition(":")
            assert module in MODULE_KEYS and action in ("view", "add", "edit", "delete"), (method, path, perm)


def test_api_key_routes_are_documented(schema):
    """Spot-check the mobile allow-list: these must accept X-API-Key."""
    for method, path in [("post", "/detections/nearby"), ("post", "/vehicles/{vehicle_id}/ping"),
                         ("post", "/signalements/{signalement_id}/vote"), ("get", "/mobile-config/"),
                         ("post", "/auth/mobile/login")]:
        op = schema["paths"][path][method]
        assert any("ApiKeyAuth" in req for req in op["security"]), (method, path)
    op = schema["paths"]["/users/"]["get"]
    assert op["security"] == [{"BearerAuth": []}]
    assert op["x-permission"] == "users:view"


def test_error_responses_use_shared_models(schema):
    comps = schema["components"]["schemas"]
    assert {"ErrorResponse", "ErrorDetail", "LegacyErrorResponse"} <= set(comps)


def test_exported_file_is_up_to_date(schema):
    assert EXPORT.exists(), "docs/openapi.json missing — run `python -m app.export_openapi`"
    exported = json.loads(EXPORT.read_text(encoding="utf-8"))
    hint = "docs/openapi.json is stale — run `python -m app.export_openapi`"
    live = json.loads(json.dumps(schema))     # same JSON normalisation as the export
    assert set(exported["paths"]) == set(live["paths"]), hint
    assert exported["paths"] == live["paths"], hint
    assert exported["components"] == live["components"], hint
    assert exported["tags"] == live["tags"], hint
