from types import SimpleNamespace

from app.api.deps import effective_permissions
from app.core.security import (
    decrypt_secret, encrypt_secret, hash_password, hash_token, password_problems,
    totp_now, verify_password, verify_totp,
)

# RFC 6238 Appendix B (SHA-1), secret "12345678901234567890", truncated to 6 digits.
RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


def test_totp_matches_rfc6238_vectors():
    assert totp_now(RFC_SECRET, at=59) == "287082"
    assert totp_now(RFC_SECRET, at=1111111109) == "081804"
    assert totp_now(RFC_SECRET, at=1234567890) == "005924"


def test_totp_drift_window_and_replay_protection():
    at = 1234567890
    code = totp_now(RFC_SECRET, at=at)
    step = verify_totp(RFC_SECRET, code, at=at + 30)       # one step of clock drift
    assert step == at // 30
    assert verify_totp(RFC_SECRET, code, last_step=step, at=at) is None   # replay refused
    assert verify_totp(RFC_SECRET, code, at=at + 120) is None             # too old
    assert verify_totp(RFC_SECRET, "12345", at=at) is None
    assert verify_totp(RFC_SECRET, "abcdef", at=at) is None


def test_password_hash_roundtrip():
    h = hash_password("Correct horse 1")
    assert h.startswith("scrypt$")
    assert h != hash_password("Correct horse 1")        # salted
    assert verify_password("Correct horse 1", h)
    assert not verify_password("correct horse 1", h)
    assert not verify_password("x", None)
    assert not verify_password("x", "garbage")


def test_password_policy():
    assert password_problems("abc", 8) == ["at least 8 characters", "at least one digit"]
    assert password_problems("abcdefg1", 8) == []
    assert "at least one letter" in password_problems("12345678", 8)


def test_secret_encryption_roundtrip():
    enc = encrypt_secret("app-password")
    assert enc != "app-password"
    assert decrypt_secret(enc) == "app-password"
    assert decrypt_secret("not-a-token") is None
    assert decrypt_secret(None) is None


def test_token_hash_is_stable_sha256():
    assert hash_token("abc") == hash_token("abc")
    assert len(hash_token("abc")) == 64


def _perm(module, **flags):
    return SimpleNamespace(module=module, **{f"can_{a}": flags.get(a, False) for a in ("view", "add", "edit", "delete")})


def test_effective_permissions_union_of_active_roles():
    validator = SimpleNamespace(is_active=True, is_system=False,
                                permissions=[_perm("validations", view=True, edit=True), _perm("jobs", view=True)])
    operator = SimpleNamespace(is_active=True, is_system=False, permissions=[_perm("jobs", add=True)])
    disabled = SimpleNamespace(is_active=False, is_system=False, permissions=[_perm("users", delete=True)])
    user = SimpleNamespace(roles=[validator, operator, disabled])
    perms = effective_permissions(user)
    assert perms["validations"] == {"view", "edit"}
    assert perms["jobs"] == {"view", "add"}
    assert "users" not in perms


def test_system_role_grants_everything():
    admin = SimpleNamespace(is_active=True, is_system=True, permissions=[])
    perms = effective_permissions(SimpleNamespace(roles=[admin]))
    assert perms["users"] == {"view", "add", "edit", "delete"}
    assert perms["road_risk"] == {"view", "add", "edit", "delete"}
