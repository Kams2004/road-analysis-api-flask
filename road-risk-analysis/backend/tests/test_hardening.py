"""Security / load hardening: rate-limit rules, trusted client IP, capped page sizes, index upgrade."""
from types import SimpleNamespace

from app.core import audit, rate_limit
from app.core.db import upgrade


def _req(peer: str, xff: str | None = None):
    headers = {"x-forwarded-for": xff} if xff else {}
    return SimpleNamespace(client=SimpleNamespace(host=peer), headers=headers)


def test_forwarded_for_is_only_trusted_from_the_internal_network():
    assert audit.client_ip(_req("172.18.0.5", "41.202.1.2, 10.0.0.1")) == "41.202.1.2"   # web console proxy
    assert audit.client_ip(_req("127.0.0.1", "41.202.1.2")) == "41.202.1.2"
    assert audit.client_ip(_req("41.202.9.9", "1.2.3.4")) == "41.202.9.9"                 # direct caller: forged
    assert audit.client_ip(_req("41.202.9.9")) == "41.202.9.9"


def test_rate_limit_rules():
    auth, public = "RATE_LIMIT_AUTH_PER_MIN", "RATE_LIMIT_PUBLIC_PER_MIN"
    assert rate_limit.rule_for("POST", "/auth/login") == auth
    assert rate_limit.rule_for("POST", "/auth/login/") == auth
    assert rate_limit.rule_for("POST", "/auth/mobile/register") == auth
    assert rate_limit.rule_for("POST", "/auth/password/forgot") == auth
    assert rate_limit.rule_for("POST", "/auth/password/reset") == auth
    assert rate_limit.rule_for("GET", "/labelstudio/image/pothole/x.jpg") == public
    assert rate_limit.rule_for("GET", "/auth/me") is None
    assert rate_limit.rule_for("POST", "/auth/logout") is None
    assert rate_limit.rule_for("POST", "/auth/loginx") is None
    assert rate_limit.rule_for("GET", "/detections/") is None


def test_index_statements_are_recognised_for_skipping():
    for stmt in upgrade.SCALE_INDEX_STATEMENTS:
        assert upgrade._CREATE_INDEX.match(stmt)
    assert upgrade._CREATE_INDEX.match("CREATE INDEX IF NOT EXISTS ix_a ON t (c)").group(1) == "ix_a"


def test_list_endpoints_cap_the_page_size():
    from app.main import app
    schema = app.openapi()
    for path in ("/detections/", "/signalements/"):
        for op in schema["paths"].get(path, {}).values():
            for prm in op.get("parameters", []):
                if prm["name"] == "limit":
                    assert prm["schema"].get("maximum", 0) <= 1000, path
                if prm["name"] == "skip":
                    assert prm["schema"].get("minimum") == 0, path
