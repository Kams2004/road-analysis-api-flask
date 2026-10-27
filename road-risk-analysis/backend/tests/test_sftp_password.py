"""
SFTP password entered in the web console: stored encrypted, decrypted only to
authenticate, never returned / logged / audited / echoed by a 422; legacy
servers keep reading SFTP_PASSWORD_<KEY> from infra/secrets/sftp.env.
"""
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import SecretStr

from app.core import redact
from app.core.security import encrypt_secret
from app.modules.m07_ingestion import sftp_core as core
from app.modules.m07_ingestion import sftp_source as ss
from app.modules.m07_ingestion.models.cloud_connection import CloudConnection
from app.modules.m07_ingestion.routes import cloud_connections as routes

PW = "s3cr#t pass=word"


def _conn(**kw):
    base = dict(id="c1", provider="sftp", name="srv", enabled=True, status="active", sftp_host="h", sftp_port=22,
                sftp_username="u", sftp_base_path=None, sftp_password_key=None, sftp_password_enc=None,
                sftp_host_key=None, sftp_host_key_fp=None, sftp_host_key_at=None)
    base.update(kw)
    return SimpleNamespace(**base)


# ── storage / connect time ───────────────────────────────────────────────────

def test_saved_password_round_trip_and_unreadable_after_key_change(monkeypatch):
    enc = encrypt_secret(PW)
    assert PW not in enc
    creds = core.saved_credentials(enc)
    assert creds.method == "password" and creds.password == PW
    assert PW not in repr(creds) and PW not in str(creds)

    from app.core import security
    monkeypatch.setattr(security.settings, "SECRET_KEY", "another-key")
    with pytest.raises(core.SftpError, match="enter the password again"):
        core.saved_credentials(enc)


def test_target_carries_only_the_encrypted_password():
    enc = encrypt_secret(PW)
    t = ss.SftpTarget.from_connection(_conn(sftp_password_enc=enc))
    assert t.password_enc == enc and t.password_key is None
    assert enc not in repr(t) and PW not in repr(t)
    # A new password reopens the pooled session (other signature), the old one does not leak in it.
    t2 = ss.SftpTarget.from_connection(_conn(sftp_password_enc=encrypt_secret("other")))
    assert t.signature != t2.signature and enc not in str(t.signature)
    # Legacy key still accepted; nothing at all is refused.
    assert ss.SftpTarget.from_connection(_conn(sftp_password_key="CAMTRACK")).password_key == "CAMTRACK"
    with pytest.raises(core.SftpError, match="No password saved"):
        ss.SftpTarget.from_connection(_conn())


def test_connection_usable_with_a_saved_password_only():
    from app.modules.m07_ingestion import job_service as js
    assert js.connection_usable(_conn(sftp_password_enc="x"))
    assert not js.connection_usable(_conn())


# ── API output / audit ───────────────────────────────────────────────────────

def test_output_and_snapshot_never_contain_the_password():
    enc = encrypt_secret(PW)
    c = _conn(sftp_password_enc=enc, created_by="a", created_at=None, last_sync_at=None, last_sync_status=None,
              last_sync_error=None, last_sync_files=0, files_imported_total=0, auto_import=False,
              auto_import_models=None, auto_import_frame_skip=None, account_email=None, folder_id=None,
              folder_name=None, refresh_token_enc=None)
    info = routes._sftp_out(c)
    assert info.password_saved and info.credentials == "password" and info.password_key is None
    dumped = info.model_dump_json() + str(routes._snapshot(c))
    assert PW not in dumped and enc not in dumped
    assert routes._snapshot(c)["sftp_password_saved"] is True


def test_password_input_is_write_only_and_length_checked_without_echo():
    body = routes.SftpIn(host="h", username="u", password=PW)
    assert PW not in repr(body) and PW not in str(body.model_dump())
    assert routes._password(body.password) == PW
    assert routes._password(SecretStr("")) is None and routes._password(None) is None
    with pytest.raises(HTTPException) as e:
        routes._password(SecretStr("x" * (routes.PASSWORD_MAX + 1)))
    assert "xxxx" not in str(e.value.detail)


def test_validation_errors_drop_secret_inputs():
    errors = [
        {"type": "string_type", "loc": ("body", "password"), "msg": "bad", "input": PW, "ctx": {"x": PW}},
        {"type": "model_type", "loc": ("body",), "msg": "bad", "input": {"host": "h", "password": PW,
                                                                         "nested": [{"api_key": PW}]}},
        {"type": "int_parsing", "loc": ("body", "port"), "msg": "bad", "input": "abc"},
    ]
    out = redact.validation_errors(errors)
    assert PW not in str(out)
    assert out[1]["input"]["host"] == "h" and out[2]["input"] == "abc"


# ── handlers (fake DB / log / pool) ──────────────────────────────────────────

class _Db:
    def __init__(self, *objs):
        self.objs = {o.id: o for o in objs}
        self.added = []

    def add(self, o):
        self.added.append(o)

    async def flush(self):
        for o in self.added:
            if getattr(o, "id", None) is None:
                o.id, o.created_at = "new", datetime(2026, 10, 27)
            self.objs[o.id] = o

    async def get(self, model, key):
        return self.objs.get(key)

    async def commit(self):
        pass


@pytest.fixture
def quiet(monkeypatch):
    audits, logs = [], []
    monkeypatch.setattr(routes.audit, "record", lambda *a, **kw: audits.append(kw.get("details")))
    monkeypatch.setattr(routes.sftp_source, "make_log", lambda cid: SimpleNamespace(
        status=logs.append, error=logs.append))

    async def no_link(conn):
        return {}
    monkeypatch.setattr(routes, "_link", no_link)
    monkeypatch.setattr(routes.sftp_source.POOL, "close", lambda *a: None)
    monkeypatch.setattr(routes, "actor_name", lambda p, label: "admin")
    return audits, logs


async def test_create_and_edit_store_it_encrypted_and_never_log_it(quiet):
    audits, logs = quiet
    principal = SimpleNamespace(label="admin")
    db = _Db()
    out = await routes.create_sftp(routes.SftpIn(host="h", username="u", password=PW), None, db, principal)
    conn = db.objs["new"]
    assert out.sftp.password_saved and conn.sftp_password_enc and conn.sftp_password_key is None
    assert core.saved_credentials(conn.sftp_password_enc).password == PW

    with pytest.raises(HTTPException) as e:
        await routes.create_sftp(routes.SftpIn(host="h", username="u"), None, _Db(), principal)
    assert e.value.status_code == 422

    old = conn.sftp_password_enc
    await routes.update_sftp("new", routes.SftpPatch(name="renamed"), None, db, principal)
    assert conn.sftp_password_enc == old                       # empty / absent password = keep it
    await routes.update_sftp("new", routes.SftpPatch(password="n3w"), None, db, principal)
    assert core.saved_credentials(conn.sftp_password_enc).password == "n3w"
    assert audits[-1]["sftp_password"] == "changed"
    with pytest.raises(HTTPException):                        # nothing left to authenticate with
        await routes.update_sftp("new", routes.SftpPatch(clear_password=True), None, db, principal)

    trail = str(audits) + str(logs)
    assert PW not in trail and "n3w" not in trail and old not in trail


def test_model_has_the_encrypted_column():
    assert "sftp_password_enc" in CloudConnection.__table__.columns
