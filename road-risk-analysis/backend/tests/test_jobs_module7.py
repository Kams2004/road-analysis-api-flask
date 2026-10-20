"""
Module 7 — job lifecycle rules, capacity slots, frame sampling, retry rules
and cloud source listing/filtering. Pure functions plus httpx.MockTransport
for the Google Drive / OneDrive / OAuth calls (no network, no database).
"""
from datetime import datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from pydantic import ValidationError

from app.models.job import JobStatus
from app.schemas.schemas import JobOut
from app.services import cloud_storage as cs
from app.services import job_service as js
from app.services import job_slots
from app.services.settings_service import CloudSettings, ProcessingSettings, SECRET_FIELDS, redact


# ── frame sampling ───────────────────────────────────────────────────────────

def test_frame_skip_override_then_source_then_default():
    proc = ProcessingSettings(frame_skip_default=7, frame_skip_by_source={"upload": 5, "stream": 10})
    assert js.resolve_frame_skip(proc, "upload") == 5
    assert js.resolve_frame_skip(proc, "stream") == 10
    assert js.resolve_frame_skip(proc, "onedrive") == 7          # not listed → default
    assert js.resolve_frame_skip(proc, "stream", 2) == 2         # explicit override wins


def test_per_model_sampling():
    skips = js.model_skips(["pothole", "signs", "speedbump"], 5, {"signs": 10})
    assert skips == {"pothole": 5, "signs": 10, "speedbump": 5}
    assert js.models_for_frame(0, skips) == ["pothole", "signs", "speedbump"]   # first frame always
    assert js.models_for_frame(5, skips) == ["pothole", "speedbump"]
    assert js.models_for_frame(10, skips) == ["pothole", "signs", "speedbump"]
    assert js.models_for_frame(3, skips) == []


def test_processing_settings_validation():
    with pytest.raises(ValidationError):
        ProcessingSettings(max_concurrent_jobs=0)
    with pytest.raises(ValidationError):
        ProcessingSettings(frame_skip_by_model={"pothole": 61})
    with pytest.raises(ValidationError):
        ProcessingSettings(frame_skip_by_source={"ftp": 5})
    p = ProcessingSettings()
    assert (p.max_concurrent_jobs, p.max_attempts, p.frame_skip_default, p.retain_failed_hours) == (2, 3, 5, 72)
    assert p.frame_skip_by_source == {"upload": 5, "google_drive": 5, "onedrive": 5, "sftp": 5, "stream": 10}


def test_cloud_settings_secrets_are_redacted():
    assert SECRET_FIELDS["cloud"] == {"google_client_secret", "microsoft_client_secret"}
    out = redact("cloud", CloudSettings(google_client_id="g", google_client_secret="s"))
    assert "google_client_secret" not in out and out["google_client_secret_set"] is True
    assert out["microsoft_client_secret_set"] is False
    assert out["redirect_uri"].endswith("/settings/cloud/callback")
    assert out["google_configured"] is True and out["microsoft_configured"] is False


def test_parse_models():
    assert js.parse_models(None) == ["pothole", "signs", "speedbump"]
    assert js.parse_models(" Signs,pothole ") == ["pothole", "signs"]
    with pytest.raises(ValueError):
        js.parse_models("pothole,cars")
    with pytest.raises(ValueError):
        js.parse_models(",")


# ── cancel / retry transitions ───────────────────────────────────────────────

def test_cancel_plan():
    assert js.cancel_plan(JobStatus.pending) == "revoke"
    assert js.cancel_plan("processing") == "flag"
    for st in ("done", "failed", "cancelled"):
        assert js.cancel_plan(st) is None


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_retry_allowed(status):
    assert js.retry_refusal(status, 1, 3, has_reviewed=False, media_available=True) is None
    assert js.retry_refusal(status, 2, 3, has_reviewed=False, media_available=True) is None


@pytest.mark.parametrize("status", ["pending", "processing", "done"])
def test_retry_refused_for_live_or_done_jobs(status):
    assert js.retry_refusal(status, 1, 3, has_reviewed=False, media_available=True).code == "invalid_transition"


def test_retry_refusal_codes_and_order():
    assert js.retry_refusal("failed", 3, 3, has_reviewed=True, media_available=False).code == "retry_limit"
    assert js.retry_refusal("failed", 1, 3, has_reviewed=True, media_available=False).code == "has_reviewed_detections"
    assert js.retry_refusal("failed", 1, 3, has_reviewed=False, media_available=False).code == "source_unavailable"
    assert js.retry_refusal("cancelled", 1, 1, has_reviewed=False, media_available=True).code == "retry_limit"


def test_media_available(tmp_path):
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    job = SimpleNamespace(source_path=str(f), source_type="upload", source_ref=None)
    assert js.media_available(job)
    job.source_path = str(tmp_path / "gone.mp4")
    assert not js.media_available(job)
    cloud = SimpleNamespace(source_path=None, source_type="google_drive", source_ref="abc")
    assert js.media_available(cloud, connection_usable=True)      # re-downloadable
    assert not js.media_available(cloud, connection_usable=False)
    stream = SimpleNamespace(source_path=None, source_type="stream", source_ref="s1")
    assert not js.media_available(stream, connection_usable=True)


def test_connection_usable():
    ok = SimpleNamespace(enabled=True, status="active", refresh_token_enc="x")
    assert js.connection_usable(ok)
    assert not js.connection_usable(None)
    assert not js.connection_usable(SimpleNamespace(enabled=False, status="active", refresh_token_enc="x"))
    assert not js.connection_usable(SimpleNamespace(enabled=True, status="error", refresh_token_enc="x"))
    # SFTP: no OAuth token; usable while enabled even after a failed listing (the server may be back).
    sftp = dict(provider="sftp", refresh_token_enc=None, sftp_host="h", sftp_password_key="K")
    assert js.connection_usable(SimpleNamespace(enabled=True, status="error", **sftp))
    assert not js.connection_usable(SimpleNamespace(enabled=False, status="disabled", **sftp))
    assert not js.connection_usable(SimpleNamespace(enabled=True, status="active", **{**sftp, "sftp_password_key": None}))


def _job(**kw):
    base = dict(id="j1", filename="a.mp4", status=JobStatus.failed, enabled_models="pothole", total_frames=100,
                processed=40, detections=3, error="boom", created_at=datetime(2026, 1, 1), finished_at=None,
                source_type="upload", source_ref=None, source_name="a.mp4", connection_id=None, stream_id=None,
                submitted_by="u@x", source_path=None, started_at=None, cancel_requested=False, cancelled_at=None,
                cancelled_by=None, attempts=1, max_attempts=3, avg_confidence=0.6, frame_skip=5,
                detections_by_model={"pothole": 3}, fps=25.0, duration_s=4.0, celery_task_id="t")
    base.update(kw)
    return SimpleNamespace(**base)


def test_reset_for_retry():
    job = _job(cancel_requested=True, cancelled_at=datetime.utcnow(), started_at=datetime.utcnow())
    js.reset_for_retry(job)
    assert job.status == JobStatus.pending and job.attempts == 2
    assert (job.processed, job.detections, job.total_frames) == (0, 0, 0)
    assert job.error is None and job.started_at is None and job.cancelled_at is None
    assert job.cancel_requested is False and job.avg_confidence is None and job.detections_by_model is None


def test_progress_and_processing_seconds():
    t0 = datetime(2026, 1, 1, 12, 0, 0)
    job = _job(status=JobStatus.processing, started_at=t0)
    assert js.progress(job) == 0.4
    assert js.processing_seconds(job, now=t0 + timedelta(seconds=90)) == 90.0
    job.status, job.finished_at = JobStatus.done, t0 + timedelta(seconds=30)
    assert js.progress(job) == 1.0 and js.processing_seconds(job) == 30.0
    assert js.processing_seconds(_job(status=JobStatus.pending)) is None
    assert js.progress(_job(total_frames=0, status=JobStatus.processing)) == 0.0


def test_job_out_computed_fields():
    t0 = datetime(2026, 1, 1, 12, 0, 0)
    out = JobOut.model_validate(_job(status=JobStatus.done, started_at=t0, finished_at=t0 + timedelta(seconds=12)))
    assert out.progress == 1.0 and out.processing_seconds == 12.0
    assert out.media_retained is False and out.detections_by_model == {"pothole": 3}
    legacy = JobOut.model_validate(_job(attempts=None, max_attempts=None, source_type=None, cancel_requested=None))
    assert (legacy.attempts, legacy.max_attempts, legacy.source_type, legacy.cancel_requested) == (1, 3, "upload", False)


def test_readable_error():
    assert js.readable_error(ValueError("bad frame")) == "ValueError: bad frame"
    assert js.readable_error(js.PermanentJobError("Source media is gone")) == "Source media is gone"
    assert len(js.readable_error(RuntimeError("x" * 900))) == 500


# ── capacity slots ───────────────────────────────────────────────────────────

class FakeRedis:
    def __init__(self):
        self.kv, self.z = {}, {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.kv:
            return None
        self.kv[key] = value
        return True

    def get(self, key):
        return self.kv.get(key)

    def delete(self, key):
        self.kv.pop(key, None)

    def zremrangebyscore(self, key, lo, hi):
        z = self.z.setdefault(key, {})
        for m in [m for m, s in z.items() if s <= hi]:
            del z[m]

    def zscore(self, key, member):
        return self.z.get(key, {}).get(member)

    def zcard(self, key):
        return len(self.z.get(key, {}))

    def zadd(self, key, mapping, xx=False):
        z = self.z.setdefault(key, {})
        for m, s in mapping.items():
            if xx and m not in z:
                continue
            z[m] = s

    def zrem(self, key, member):
        self.z.get(key, {}).pop(member, None)

    def zrange(self, key, a, b):
        return sorted(self.z.get(key, {}), key=self.z[key].get) if key in self.z else []


def test_slots_enforce_capacity():
    r = FakeRedis()
    assert job_slots.acquire(r, "a", 2, now=100)
    assert job_slots.acquire(r, "b", 2, now=100)
    assert not job_slots.acquire(r, "c", 2, now=100)      # C reached → re-queue
    assert job_slots.acquire(r, "a", 2, now=101)          # re-entrant
    job_slots.release(r, "a")
    assert job_slots.acquire(r, "c", 2, now=102)
    assert sorted(job_slots.holders(r)) == ["b", "c"]
    assert r.get(job_slots.LOCK_KEY) is None              # lock always released


def test_slots_capacity_change_applies_immediately():
    r = FakeRedis()
    for j in ("a", "b", "c"):
        assert job_slots.acquire(r, j, 3, now=0)
    assert not job_slots.acquire(r, "d", 2, now=1)       # lowered C: nothing new starts
    job_slots.release(r, "a")
    assert not job_slots.acquire(r, "d", 2, now=1)       # still 2 running
    job_slots.release(r, "b")
    assert job_slots.acquire(r, "d", 2, now=1)


def test_stale_slots_are_reclaimed_and_heartbeat_keeps_them():
    r = FakeRedis()
    assert job_slots.acquire(r, "dead", 1, now=0, stale_s=60)
    assert job_slots.acquire(r, "live", 2, now=0, stale_s=60)
    job_slots.heartbeat(r, "live", now=50)
    assert job_slots.acquire(r, "new", 2, now=70, stale_s=60)   # "dead" expired, "live" kept
    assert sorted(job_slots.holders(r)) == ["live", "new"]
    job_slots.heartbeat(r, "ghost", now=80)                     # xx: never creates a slot
    assert "ghost" not in job_slots.holders(r)


def test_slot_lock_contention_returns_false(monkeypatch):
    monkeypatch.setattr(job_slots.time, "sleep", lambda s: None)
    r = FakeRedis()
    r.set(job_slots.LOCK_KEY, "someone-else")
    assert not job_slots.acquire(r, "a", 5, now=0)
    assert r.get(job_slots.LOCK_KEY) == "someone-else"     # never steals another holder's lock


# ── cloud sources: filtering ─────────────────────────────────────────────────

def test_is_video_and_select_new_files():
    files = [
        cs.CloudFile("1", "b.mp4", "video/mp4", modified="2026-01-02"),
        cs.CloudFile("2", "notes.pdf", "application/pdf", modified="2026-01-01"),
        cs.CloudFile("3", "a.MOV", None, modified="2026-01-01"),      # no MIME → extension
        cs.CloudFile("4", "done.mp4", "video/mp4", modified="2026-01-01"),
        cs.CloudFile("5", "c.avi", "video/x-msvideo", modified="2026-01-03"),
        cs.CloudFile("1", "b.mp4", "video/mp4", modified="2026-01-02"),  # listed twice
    ]
    new = cs.select_new_files(files, imported_refs=["4"])
    assert [f.id for f in new] == ["3", "1", "5"]                    # oldest first, no dup, no pdf
    assert [f.id for f in cs.select_new_files(files, ["4"], limit=2)] == ["3", "1"]
    assert cs.select_new_files(files, ["1", "3", "4", "5"]) == []


def _mock(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_google_list_videos_paginates_and_queries_folder():
    seen = []

    def handler(req: httpx.Request):
        seen.append(req)
        assert req.headers["authorization"] == "Bearer tok"
        q = parse_qs(req.url.query.decode())
        if "pageToken" not in q:
            return httpx.Response(200, json={"nextPageToken": "p2", "files": [
                {"id": "a", "name": "a.mp4", "mimeType": "video/mp4", "size": "10", "modifiedTime": "2026-01-01"}]})
        return httpx.Response(200, json={"files": [
            {"id": "b", "name": "b.mov", "mimeType": "video/quicktime"},
            {"id": "c", "name": "c.txt", "mimeType": "text/plain"}]})

    async with _mock(handler) as client:
        files = await cs.list_videos("google_drive", "tok", "FOLDER'1", client)
    assert [f.id for f in files] == ["a", "b"] and files[0].size == 10
    q = parse_qs(seen[0].url.query.decode())["q"][0]
    assert q == "'FOLDER\\'1' in parents and mimeType contains 'video/' and trashed = false"


async def test_onedrive_list_videos_filters_mime_and_follows_next_link():
    def handler(req: httpx.Request):
        if req.url.path.endswith("/items/F/children"):
            return httpx.Response(200, json={
                "value": [
                    {"id": "1", "name": "clip.mp4", "file": {"mimeType": "video/mp4"}, "size": 5},
                    {"id": "2", "name": "sub", "folder": {"childCount": 2}},
                    {"id": "3", "name": "pic.jpg", "file": {"mimeType": "image/jpeg"}},
                ],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/next-page"})
        assert req.url.path == "/v1.0/next-page"
        return httpx.Response(200, json={"value": [{"id": "4", "name": "x.mkv", "file": {"mimeType": "video/x-matroska"}}]})

    async with _mock(handler) as client:
        files = await cs.list_videos("onedrive", "tok", "F", client)
    assert [f.id for f in files] == ["1", "4"]


async def test_list_folders_both_providers():
    def google(req):
        q = parse_qs(req.url.query.decode())["q"][0]
        assert q.startswith("'root' in parents and mimeType = 'application/vnd.google-apps.folder'")
        return httpx.Response(200, json={"files": [{"id": "f2", "name": "zeta"}, {"id": "f1", "name": "Alpha"}]})

    def onedrive(req):
        assert req.url.path == "/v1.0/me/drive/root/children"
        return httpx.Response(200, json={"value": [{"id": "d1", "name": "Dash", "folder": {}},
                                                   {"id": "x", "name": "v.mp4", "file": {"mimeType": "video/mp4"}}]})

    async with _mock(google) as c:
        assert await cs.list_folders("google_drive", "t", None, c) == [{"id": "f1", "name": "Alpha"},
                                                                     {"id": "f2", "name": "zeta"}]
    async with _mock(onedrive) as c:
        assert await cs.list_folders("onedrive", "t", None, c) == [{"id": "d1", "name": "Dash"}]


async def test_download_streams_to_file(tmp_path):
    def handler(req):
        assert req.url.path == "/drive/v3/files/abc" and parse_qs(req.url.query.decode())["alt"] == ["media"]
        return httpx.Response(200, content=b"x" * 3000)

    dest = tmp_path / "v.mp4"
    async with _mock(handler) as c:
        assert await cs.download("google_drive", "t", "abc", str(dest), c) == 3000
    assert dest.read_bytes() == b"x" * 3000


async def test_provider_error_is_readable():
    def handler(req):
        return httpx.Response(403, json={"error": {"code": 403, "message": "insufficient scope"}})

    async with _mock(handler) as c:
        with pytest.raises(cs.CloudError, match="403.*insufficient scope"):
            await cs.list_videos("google_drive", "t", None, c)


# ── cloud sources: OAuth ─────────────────────────────────────────────────────

CFG = CloudSettings(google_client_id="gid", google_client_secret="gsec",
                    microsoft_client_id="mid", microsoft_client_secret="msec", microsoft_tenant="contoso")


def test_state_roundtrip_and_tamper():
    state, nonce = cs.make_state("conn-1")
    assert cs.parse_state(state) == ("conn-1", nonce)
    assert cs.parse_state(state.replace("conn-1", "conn-2")) is None
    assert cs.parse_state("garbage") is None


def test_authorize_urls():
    g = urlparse(cs.authorize_url("google_drive", CFG, "S"))
    gq = parse_qs(g.query)
    assert g.netloc == "accounts.google.com" and gq["access_type"] == ["offline"] and gq["prompt"] == ["consent"]
    assert gq["scope"] == ["https://www.googleapis.com/auth/drive.readonly openid email"] and gq["state"] == ["S"]
    m = urlparse(cs.authorize_url("onedrive", CFG, "S"))
    assert m.path == "/contoso/oauth2/v2.0/authorize"
    assert parse_qs(m.query)["scope"] == ["offline_access Files.Read User.Read"]
    with pytest.raises(cs.CloudError):
        cs.authorize_url("google_drive", CloudSettings(), "S")   # client not configured


async def test_exchange_code_and_account_email():
    def handler(req: httpx.Request):
        if req.url.host == "login.microsoftonline.com":
            form = parse_qs(req.content.decode())
            assert req.url.path == "/contoso/oauth2/v2.0/token"
            assert form["grant_type"] == ["authorization_code"] and form["code"] == ["C"]
            assert form["client_secret"] == ["msec"] and "offline_access" in form["scope"][0]
            return httpx.Response(200, json={"access_token": "AT", "refresh_token": "RT"})
        assert req.url.path == "/v1.0/me"
        return httpx.Response(200, json={"mail": None, "userPrincipalName": "op@contoso.com"})

    async with _mock(handler) as c:
        tokens = await cs.exchange_code("onedrive", CFG, "C", c)
        assert (tokens.access_token, tokens.refresh_token) == ("AT", "RT")
        assert await cs.account_email("onedrive", "AT", c) == "op@contoso.com"


async def test_exchange_code_without_refresh_token_fails():
    def handler(req):
        return httpx.Response(200, json={"access_token": "AT"})

    async with _mock(handler) as c:
        with pytest.raises(cs.CloudError, match="refresh token"):
            await cs.exchange_code("google_drive", CFG, "C", c)


async def test_connection_token_stores_rotated_refresh_token():
    from app.core.security import decrypt_secret, encrypt_secret

    def handler(req):
        assert parse_qs(req.content.decode())["refresh_token"] == ["OLD"]
        return httpx.Response(200, json={"access_token": "AT2", "refresh_token": "NEW"})

    conn = SimpleNamespace(provider="onedrive", refresh_token_enc=encrypt_secret("OLD"))
    async with _mock(handler) as c:
        assert await cs.connection_token(None, conn, CFG, c) == "AT2"
    assert decrypt_secret(conn.refresh_token_enc) == "NEW"
