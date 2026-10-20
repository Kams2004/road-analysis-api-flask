"""
Mobile accounts & Google sign-in (docs/spec-mobile-app.md, "Backend — mobile
accounts").

The first part is pure: Google ID-token checks over an httpx.MockTransport,
the settings section, the rate limiter. The second part drives the real
FastAPI app against a PostgreSQL database and only runs when
RUN_DB_TESTS=1 (point DATABASE_URL at a throwaway database — the tests create
users, API clients and signalements).
"""
import os
import time
import uuid

import httpx
import pytest

from app.modules.m11_community.models.signalement import SignalementType
from app.modules.m01_security.schemas import AuthConfigOut, CaptchaPublic, MobileRegisterIn
from app.modules.m01_security import google_signin as gs
from app.core import rate_limit
from app.core.settings_service import SECTIONS, GoogleSigninSettings, redact

CFG = GoogleSigninSettings(enabled=True, web_client_id="web.apps.googleusercontent.com",
                           android_client_id="android.apps.googleusercontent.com")


def _claims(**over):
    base = {"aud": "android.apps.googleusercontent.com", "iss": "https://accounts.google.com",
            "sub": "1234567890", "email": "Jane.Doe@Gmail.com", "email_verified": "true",
            "exp": str(int(time.time()) + 600), "name": "Jane Doe"}
    base.update(over)
    return base


# ── Google ID token ──────────────────────────────────────────────────────────

def test_check_claims_accepts_valid_token():
    out = gs.check_claims(_claims(), CFG)
    assert out == {"sub": "1234567890", "email": "jane.doe@gmail.com", "name": "Jane Doe"}
    assert gs.check_claims(_claims(aud="web.apps.googleusercontent.com", iss="accounts.google.com",
                                   email_verified=True), CFG)["sub"] == "1234567890"


@pytest.mark.parametrize("over,reason", [
    ({"aud": "someone-else"}, "audience"),
    ({"iss": "https://evil.example"}, "issuer"),
    ({"email_verified": "false"}, "email_not_verified"),
    ({"exp": str(int(time.time()) - 1)}, "expired"),
    ({"exp": None}, "expiry"),
    ({"email": "not-an-email"}, "claims"),
    ({"sub": ""}, "claims"),
])
def test_check_claims_refusals(over, reason):
    with pytest.raises(gs.GoogleTokenInvalid) as exc:
        gs.check_claims(_claims(**over), CFG)
    assert str(exc.value) == reason


def test_unconfigured_google_has_no_audience():
    assert not GoogleSigninSettings().configured()
    assert not GoogleSigninSettings(web_client_id="x").configured()          # disabled
    assert not GoogleSigninSettings(enabled=True).configured()               # no client id
    with pytest.raises(gs.GoogleTokenInvalid):
        gs.check_claims(_claims(), GoogleSigninSettings(enabled=True))


def _mock(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_verify_id_token_calls_tokeninfo():
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json=_claims())

    async with _mock(handler) as client:
        out = await gs.verify_id_token(CFG, "tok-123", client)
    assert out["email"] == "jane.doe@gmail.com"
    assert seen["url"].startswith(gs.TOKENINFO_URL) and "id_token=tok-123" in seen["url"]


async def test_verify_id_token_rejected_and_unavailable():
    async with _mock(lambda r: httpx.Response(400, json={"error": "invalid_token"})) as client:
        with pytest.raises(gs.GoogleTokenInvalid):
            await gs.verify_id_token(CFG, "bad", client)
    async with _mock(lambda r: httpx.Response(503)) as client:
        with pytest.raises(gs.GoogleUnavailable):
            await gs.verify_id_token(CFG, "tok", client)

    def boom(request):
        raise httpx.ConnectError("down", request=request)

    async with _mock(boom) as client:
        with pytest.raises(gs.GoogleUnavailable):
            await gs.verify_id_token(CFG, "tok", client)
    with pytest.raises(gs.GoogleTokenInvalid):
        await gs.verify_id_token(CFG, "", None)


def test_google_settings_section_and_public_config():
    assert SECTIONS["google_signin"] is GoogleSigninSettings
    assert redact("google_signin", CFG)["configured"] is True
    assert redact("google_signin", GoogleSigninSettings())["configured"] is False
    cfg = AuthConfigOut(captcha=CaptchaPublic(provider="disabled", site_key=""), languages=["en"],
                        password_min_length=8)
    assert cfg.google.enabled is False and cfg.google.android_client_id == ""


# ── misc ─────────────────────────────────────────────────────────────────────

def test_new_signalement_types_for_mobile_tiles():
    for v in ("nid_de_poule", "accident", "route_fermee", "obstacle", "inondation", "autre"):
        assert SignalementType(v).value == v


def test_signalement_types_v2_for_mobile_menu():
    from app.core.db.upgrade import MOBILE_ENUM_VALUES
    for v in ("vehicule_panne", "dos_d_ane", "route_glissante", "eboulement",
              "travaux", "animaux", "vehicule_dangereux"):
        assert SignalementType(v).value == v
        assert f"ALTER TYPE signalementtype ADD VALUE IF NOT EXISTS '{v}'" in MOBILE_ENUM_VALUES


def test_register_schema_normalises_email():
    body = MobileRegisterIn(full_name="  Jane ", email=" Jane@Example.COM ", password="x")
    assert body.email == "jane@example.com" and body.full_name == "Jane"
    with pytest.raises(ValueError):
        MobileRegisterIn(full_name="J", email="nope", password="x")


class FakeRedis:
    def __init__(self):
        self.counts, self.ttl = {}, {}

    async def incr(self, key):
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def expire(self, key, seconds):
        self.ttl[key] = seconds


async def test_rate_limit_window():
    r = FakeRedis()
    results = [await rate_limit.allow("register:1.2.3.4", 5, 3600, r) for _ in range(7)]
    assert results == [True] * 5 + [False] * 2
    assert r.ttl == {"ratelimit:register:1.2.3.4": 3600}
    assert await rate_limit.allow("register:5.6.7.8", 5, 3600, r)


async def test_rate_limit_fails_open():
    class Down:
        async def incr(self, key):
            raise ConnectionError("redis down")

    assert await rate_limit.allow("x", 1, 60, Down()) is True


# ── API against a real database (RUN_DB_TESTS=1) ─────────────────────────────

db_only = pytest.mark.skipif(os.environ.get("RUN_DB_TESTS") != "1",
                             reason="needs a throwaway PostgreSQL (RUN_DB_TESTS=1)")

PASSWORD = "Mobile-pass1"


@pytest.fixture
async def api(monkeypatch):
    from app.modules.m11_community.routes import signalements as sig_routes
    from app.core.security import hash_token, new_token
    from app.core.db.session import AsyncSessionLocal, engine, init_db
    from app.main import app
    from app.modules.m12_integration.models.api_client import ApiClient

    await init_db()
    key = new_token("vsk_")
    async with AsyncSessionLocal() as db:
        db.add(ApiClient(name="pytest mobile", key_prefix=key[:10], key_hash=hash_token(key), created_by="pytest"))
        await db.commit()

    async def always(*a, **k):
        return True

    monkeypatch.setattr(rate_limit, "allow", always)
    monkeypatch.setattr(sig_routes, "reverse_geocode", lambda lat, lon: None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        client.key = {"X-API-Key": key}
        yield client
    await engine.dispose()


def _email():
    return f"m{uuid.uuid4().hex[:10]}@example.com"


async def _register(api, email, **extra):
    body = {"full_name": "Mobile Tester", "email": email, "password": PASSWORD, **extra}
    return await api.post("/auth/mobile/register", json=body, headers=api.key)


@db_only
async def test_register_and_login(api):
    email = _email()
    r = await api.post("/auth/mobile/register", json={"full_name": "X", "email": email, "password": PASSWORD})
    assert r.status_code == 401 and r.json()["detail"]["code"] == "api_key_required"

    r = await _register(api, email, phone="+237600000000", language="fr")
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["status"] == "ok" and out["token"]
    assert out["user"]["account_type"] == "mobile" and out["user"]["roles"] == []
    assert out["user"]["permissions"] == {} and out["user"]["language"] == "fr"

    r = await _register(api, email.upper())
    assert r.status_code == 409 and r.json()["detail"]["code"] == "email_taken"
    r = await _register(api, _email(), password="short")
    assert r.status_code == 422 and r.json()["detail"]["code"] == "weak_password"

    r = await api.post("/auth/mobile/login", json={"identifier": email, "password": PASSWORD})
    assert r.status_code == 401
    r = await api.post("/auth/mobile/login", json={"identifier": email, "password": "wrong-1"}, headers=api.key)
    assert r.status_code == 401 and r.json()["detail"]["code"] == "invalid_credentials"
    r = await api.post("/auth/mobile/login", json={"identifier": email, "password": PASSWORD}, headers=api.key)
    assert r.status_code == 200 and r.json()["status"] == "ok"


@db_only
async def test_register_rate_limited(api, monkeypatch):
    async def never(*a, **k):
        return False

    monkeypatch.setattr(rate_limit, "allow", never)
    r = await _register(api, _email())
    assert r.status_code == 429 and r.json()["detail"]["code"] == "rate_limited"


@db_only
async def test_mobile_login_enforces_mfa_for_staff(api):
    from app.core.db.session import AsyncSessionLocal
    from app.core.security import hash_password
    from app.modules.m01_security.models.user import User

    email = _email()
    async with AsyncSessionLocal() as db:
        db.add(User(email=email, full_name="Staff", password_hash=hash_password(PASSWORD), mfa_enabled=True))
        await db.commit()
    r = await api.post("/auth/mobile/login", json={"identifier": email, "password": PASSWORD}, headers=api.key)
    assert r.status_code == 200
    assert r.json()["status"] == "mfa_required" and r.json()["challenge_token"] and not r.json()["token"]


@db_only
async def test_combined_principal(api):
    email = _email()
    token = (await _register(api, email)).json()["token"]
    both = {**api.key, "Authorization": f"Bearer {token}"}

    assert (await api.get("/auth/me", headers=api.key)).status_code == 401       # key alone: no user
    r = await api.get("/auth/me", headers=both)
    assert r.status_code == 200 and r.json()["email"] == email
    r = await api.patch("/auth/me", json={"language": "es"}, headers=both)
    assert r.status_code == 200 and r.json()["language"] == "es"

    # the key + user still cannot reach console routes or non-listed auth routes
    assert (await api.get("/users/", headers=both)).status_code in (401, 403)
    assert (await api.post("/auth/activity", json={"screen": "x"}, headers=both)).status_code == 403

    # …but the mobile user can change their own password
    r = await api.post("/auth/me/password", json={"current_password": PASSWORD, "new_password": "Xx-123456"},
                       headers=both)
    assert r.status_code == 200, r.text
    r = await api.post("/auth/me/password", json={"current_password": "Xx-123456", "new_password": "Yy-123456"},
                       headers=api.key)
    assert r.status_code == 401                                                  # key alone: no user

    # reports are stamped with the signed-in user, not the client-sent value
    form = {"type": "nid_de_poule", "latitude": "4.05", "longitude": "9.7", "reported_by": "spoofed"}
    r = await api.post("/signalements/", data=form, headers=both)
    assert r.status_code == 201, r.text
    assert r.json()["reported_by"] == email and r.json()["type"] == "nid_de_poule"
    r = await api.post("/signalements/", data={**form, "type": "inondation"}, headers=api.key)
    assert r.status_code == 201 and r.json()["reported_by"] == "spoofed"

    r = await api.post("/auth/logout", headers=both)
    assert r.status_code == 200
    # stale token + valid key degrades to the plain client
    assert (await api.get("/auth/me", headers=both)).status_code == 401
    assert (await api.post("/signalements/", data=form, headers=both)).json()["reported_by"] == "spoofed"


async def _set_google(enabled: bool):
    from app.core.db.session import AsyncSessionLocal
    from app.core.settings_service import update_section
    async with AsyncSessionLocal() as db:
        await update_section(db, "google_signin", {**CFG.model_dump(), "enabled": enabled}, "pytest")


@db_only
async def test_google_sign_in(api, monkeypatch):
    from app.core.db.session import AsyncSessionLocal
    from app.modules.m01_security.models.user import User, UserStatus
    from sqlalchemy import select

    await _set_google(False)
    r = await api.post("/auth/google", json={"id_token": "x"}, headers=api.key)
    assert r.status_code == 400 and r.json()["detail"]["code"] == "google_not_configured"
    assert (await api.get("/auth/config")).json()["google"]["enabled"] is False

    await _set_google(True)
    cfg = (await api.get("/auth/config", headers=api.key)).json()["google"]
    assert cfg["enabled"] and cfg["android_client_id"] == CFG.android_client_id

    email, sub = _email(), uuid.uuid4().hex
    calls = []

    async def fake_verify(cfg, id_token, client=None):
        calls.append(id_token)
        if id_token == "bad":
            raise gs.GoogleTokenInvalid("audience")
        return {"sub": sub, "email": email, "name": "Google User"}

    monkeypatch.setattr(gs, "verify_id_token", fake_verify)
    r = await api.post("/auth/google", json={"id_token": "bad"}, headers=api.key)
    assert r.status_code == 401 and r.json()["detail"]["code"] == "invalid_google_token"

    r = await api.post("/auth/google", json={"id_token": "good"}, headers=api.key)
    assert r.status_code == 200, r.text
    assert r.json()["user"]["account_type"] == "mobile" and r.json()["user"]["full_name"] == "Google User"
    r = await api.post("/auth/google", json={"id_token": "good"})                 # web, no key
    assert r.status_code == 200 and r.json()["user"]["email"] == email

    async with AsyncSessionLocal() as db:
        users = (await db.execute(select(User).where(User.email == email))).scalars().all()
        assert len(users) == 1 and users[0].google_sub == sub and users[0].password_hash is None
        users[0].status = UserStatus.inactive
        await db.commit()
    r = await api.post("/auth/google", json={"id_token": "good"}, headers=api.key)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "account_inactive"

    # an existing password account gets linked to its Google identity
    email2 = _email()
    await _register(api, email2)
    email, sub = email2, uuid.uuid4().hex
    r = await api.post("/auth/google", json={"id_token": "good"}, headers=api.key)
    assert r.status_code == 200 and r.json()["user"]["email"] == email2
    async with AsyncSessionLocal() as db:
        assert (await db.execute(select(User.google_sub).where(User.email == email2))).scalar_one() == sub
    await _set_google(False)


@db_only
async def test_auth_events_are_audited(api):
    from app.core.db.session import AsyncSessionLocal
    from app.modules.m13_admin.models.audit_log import AuditLog
    from sqlalchemy import select

    email = _email()
    await _register(api, email)
    await api.post("/auth/mobile/login", json={"identifier": email, "password": "nope-123"}, headers=api.key)
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(AuditLog.action, AuditLog.outcome)
                                 .where(AuditLog.actor_label == email))).all()
    assert ("auth.register", "success") in rows
    assert ("auth.login", "success") in rows and ("auth.login", "failure") in rows


def test_mobile_password_change_is_allow_listed_for_api_key_plus_user():
    """POST /auth/me/password must pass the /auth API-key allow-list and
    accept the combined principal (API key + the user's Bearer token)."""
    from app.api import router as api_router
    from app.api.deps import account_user
    from app.modules.m01_security.routes import auth as auth_routes

    assert ("POST", "/auth/me/password") in api_router._AUTH_CLIENT
    route = next(r for r in auth_routes.router.routes
                 if getattr(r, "path", "").endswith("/me/password") and "POST" in getattr(r, "methods", set()))
    calls = {d.call for d in route.dependant.dependencies}
    assert account_user in calls
