"""
Cloud sources as an input source: drive browsing (provider mapping), import
batch lifecycle (download → verify → job hand-over, resume, retry, cancel,
failures), progress maths and dedupe. Providers are faked with
httpx.MockTransport; the database with a tiny in-memory session.
"""
import hashlib
import os
import itertools
from datetime import datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest

from app.modules.m07_ingestion.models.cloud_connection import CloudConnection
from app.modules.m07_ingestion.models.cloud_import import CloudImportBatch, CloudImportItem
from app.modules.m07_ingestion.models.job import Job
from app.modules.m07_ingestion import cloud_import as ci
from app.modules.m07_ingestion import cloud_providers as cp
from app.modules.m07_ingestion.cloud_storage import CloudError


def _mock(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ── browsing: provider mapping ───────────────────────────────────────────────

async def test_google_browse_maps_entries_and_search():
    seen = []

    def handler(req: httpx.Request):
        seen.append(req)
        return httpx.Response(200, json={"nextPageToken": "NEXT", "files": [
            {"id": "f1", "name": "Dashcam", "mimeType": "application/vnd.google-apps.folder"},
            {"id": "v1", "name": "run.mp4", "mimeType": "video/mp4", "size": "2048", "md5Checksum": "abc",
             "modifiedTime": "2026-10-01T10:00:00Z", "thumbnailLink": "https://lh3.googleusercontent.com/t",
             "videoMediaMetadata": {"durationMillis": "61500", "width": 1920, "height": 1080}},
            {"id": "i1", "name": "photo.JPG", "mimeType": "image/jpeg", "size": "10"},
            {"id": "d1", "name": "notes", "mimeType": "application/vnd.google-apps.document"},
            {"id": "p1", "name": "report.pdf", "mimeType": "application/pdf", "size": "5"},
        ]})

    async with _mock(handler) as c:
        page = await cp.PROVIDERS["google_drive"].browse("tok", "F'1", query="ru'n", client=c)
    q = parse_qs(seen[0].url.query.decode())
    assert q["q"][0] == "'F\\'1' in parents and trashed = false and name contains 'ru\\'n'"
    assert q["orderBy"] == ["folder,name_natural"]
    assert page.next_page_token == "NEXT"
    by = {e.id: e for e in page.entries}
    assert by["f1"].kind == "folder" and by["f1"].media is None and by["f1"].version is None
    v = by["v1"]
    assert (v.kind, v.media, v.size, v.duration_s, v.width, v.version, v.checksum) == \
        ("file", "video", 2048, 61.5, 1920, "abc", "abc")
    assert by["i1"].media == "image"
    assert by["d1"].media is None and by["p1"].media is None          # shown but not importable
    assert not cp.importable(by["p1"]) and cp.importable(v)


async def test_onedrive_browse_maps_thumbnails_and_validates_page_token():
    def handler(req: httpx.Request):
        assert req.url.path == "/v1.0/me/drive/items/F/children"
        assert parse_qs(req.url.query.decode())["$expand"] == ["thumbnails"]
        return httpx.Response(200, json={"value": [
            {"id": "s", "name": "Sub", "folder": {"childCount": 3}},
            {"id": "v", "name": "clip.mov", "size": 99, "cTag": "c1", "eTag": "e1",
             "lastModifiedDateTime": "2026-10-02T00:00:00Z",
             "file": {"mimeType": "video/quicktime", "hashes": {"quickXorHash": "x"}},
             "video": {"duration": 4000, "width": 1280, "height": 720},
             "thumbnails": [{"medium": {"url": "https://public.example/thumb.jpg"}}]},
            {"id": "pkg", "name": "Notebook", "package": {"type": "oneNote"}},       # neither file nor folder
        ], "@odata.nextLink": "https://graph.microsoft.com/v1.0/next"})

    p = cp.PROVIDERS["onedrive"]
    async with _mock(handler) as c:
        page = await p.browse("tok", "F", client=c)
    assert [e.id for e in page.entries] == ["s", "v"]
    s, v = page.entries
    assert s.child_count == 3
    assert (v.version, v.duration_s, v.thumbnail_url, v.checksum) == ("c1", 4.0, "https://public.example/thumb.jpg", None)
    assert page.next_page_token == "https://graph.microsoft.com/v1.0/next"
    assert p.thumbnail_host_ok(v.thumbnail_url) == (True, False)       # pre-authenticated: no bearer sent
    with pytest.raises(CloudError, match="page token"):
        await p.browse("tok", "F", page_token="https://evil.example/steal")


async def test_breadcrumbs_both_providers():
    def google(req: httpx.Request):
        fid = req.url.path.rsplit("/", 1)[-1]
        data = {"root": {"id": "ROOT"}, "C": {"id": "C", "name": "2026", "parents": ["B"]},
                "B": {"id": "B", "name": "Dashcam", "parents": ["ROOT"]}}
        return httpx.Response(200, json=data[fid])

    def onedrive(req: httpx.Request):
        iid = req.url.path.rsplit("/", 1)[-1]
        data = {"C": {"id": "C", "name": "2026", "parentReference": {"id": "B"}},
                "B": {"id": "B", "name": "Dashcam", "parentReference": {"id": "R"}},
                "R": {"id": "R", "name": "root", "root": {}}}
        return httpx.Response(200, json=data[iid])

    async with _mock(google) as c:
        crumbs = await cp.PROVIDERS["google_drive"].path("t", "C", c)
    assert [(x.id, x.name) for x in crumbs] == [("B", "Dashcam"), ("C", "2026")]
    async with _mock(onedrive) as c:
        crumbs = await cp.PROVIDERS["onedrive"].path("t", "C", c)
    assert [x.name for x in crumbs] == ["Dashcam", "2026"]
    assert await cp.PROVIDERS["onedrive"].path("t", None) == []


async def test_walk_media_recurses_and_records_paths():
    tree = {
        "root": [{"id": "a", "name": "a.mp4", "mimeType": "video/mp4"},
                 {"id": "S", "name": "Sub", "mimeType": cp.GOOGLE_FOLDER_MIME},
                 {"id": "x", "name": "x.txt", "mimeType": "text/plain"}],
        "S": [{"id": "b", "name": "b.jpg", "mimeType": "image/jpeg"}],
    }

    def handler(req: httpx.Request):
        q = parse_qs(req.url.query.decode())["q"][0]
        folder = q.split("'")[1]
        return httpx.Response(200, json={"files": tree[folder]})

    p = cp.PROVIDERS["google_drive"]
    async with _mock(handler) as c:
        deep = await p.walk_media("t", None, client=c)
        flat = await p.walk_media("t", None, recursive=False, client=c)
    assert [(e.id, e.path) for e in deep] == [("a", None), ("b", "Sub")]
    assert [e.id for e in flat] == ["a"]


async def test_ids_that_could_rewrite_provider_urls_are_refused():
    for bad in ("../me", "a/b", "x?alt=1", "a b", ""):
        with pytest.raises(CloudError, match="Invalid"):
            await cp.PROVIDERS["google_drive"].metadata("t", bad)
        with pytest.raises(CloudError, match="Invalid"):
            cp.PROVIDERS["onedrive"]._download_request(bad)
    assert cp.check_id("D4648F06C91D9D3D!54927") and cp.check_id("1AbC-d_E")


def test_available_providers_reports_configuration():
    from app.core.settings_service import CloudSettings
    out = {p["id"]: p for p in cp.available_providers(CloudSettings(google_client_id="g", google_client_secret="s"))}
    assert set(out) == {"google_drive", "onedrive", "sftp"}
    assert out["google_drive"]["configured"] and not out["onedrive"]["configured"]
    assert out["sftp"]["configured"] and out["sftp"]["auth"] == "server" and out["google_drive"]["auth"] == "oauth"
    assert out["onedrive"]["search_recursive"] is True


# ── progress maths ───────────────────────────────────────────────────────────

def test_percent_and_eta():
    assert ci.percent(50, 200) == 25.0
    assert ci.percent(300, 200) == 100.0
    assert ci.percent(10, None) is None and ci.percent(10, 0) is None    # indeterminate
    assert ci.eta_seconds(100, 1100, 50.0) == 20.0
    assert ci.eta_seconds(100, None, 50.0) is None and ci.eta_seconds(100, 1000, 0) is None
    assert [ci.backoff_s(i) for i in range(7)] == [1, 2, 4, 8, 16, 30, 30]


def test_progress_tracker_smooths_speed():
    t = itertools.count()                                  # 0, 1, 2 … seconds
    tr = ci.ProgressTracker(10_000, start_bytes=1000, alpha=0.5, min_interval=0.5, clock=lambda: float(next(t)))
    assert tr.speed is None and tr.percent == 10.0
    assert tr.update(2000)                                 # 1000 B in 1 s
    assert tr.speed == 1000.0
    assert tr.update(5000)                                 # 3000 B/s → EMA 2000
    assert tr.speed == 2000.0
    assert tr.eta == 2.5 and tr.percent == 50.0
    tr2 = ci.ProgressTracker(100, clock=lambda: 0.0)
    assert not tr2.update(50) and tr2.speed is None        # too soon for a sample


def _view(status, **kw):
    return {"status": status, "bytes_total": kw.get("total"), "bytes_done": kw.get("done", 0),
            "speed_bps": kw.get("speed"), "percent": kw.get("pct"), "job_progress": kw.get("jp")}


def test_batch_rollup():
    items = [
        _view("downloading", total=1000, done=250, speed=100.0, pct=25.0),
        _view("processing", total=1000, done=1000, jp=0.5),
        _view("done", total=500, done=500),
        _view("skipped_duplicate", total=9999),
    ]
    s = ci.summarize(items)
    assert s["status"] == "running" and s["active"]
    assert s["bytes_total"] == 2500 and s["bytes_done"] == 1750
    assert s["speed_bps"] == 100.0 and s["download_eta_s"] == 7.5
    # (0.125 + 0.75 + 1) / 3 items (the duplicate does not count)
    assert s["progress"] == 62.5
    assert s["counts"] == {"downloading": 1, "processing": 1, "done": 1, "skipped_duplicate": 1}


@pytest.mark.parametrize("statuses,expected", [
    (["queued", "queued"], "queued"),
    (["queued", "skipped_duplicate"], "queued"),
    (["queued", "done"], "running"),
    (["done", "skipped_duplicate"], "done"),
    (["done", "failed"], "completed_with_errors"),
    (["failed", "failed"], "failed"),
    (["cancelled", "cancelled"], "cancelled"),
    (["done", "cancelled"], "completed_with_errors"),
    ([], "done"),
])
def test_batch_status(statuses, expected):
    assert ci.batch_status(statuses) == expected


def test_effective_status_follows_the_job():
    assert ci.effective_status("downloading", None) == "downloading"
    assert ci.effective_status("queued_for_processing", "pending") == "queued_for_processing"
    assert ci.effective_status("queued_for_processing", "processing") == "processing"
    assert ci.effective_status("queued_for_processing", "done") == "done"
    assert ci.effective_status("queued_for_processing", "failed") == "failed"
    assert ci.effective_status("queued_for_processing", None, has_job=False) == "done"   # job deleted later


def test_cancel_and_retry_actions():
    assert ci.cancel_action("queued") == "now"
    assert ci.cancel_action("downloading") == "signal" and ci.cancel_action("uploading_to_storage") == "signal"
    assert ci.cancel_action("processing") == "job" and ci.cancel_action("queued_for_processing") == "job"
    assert ci.cancel_action("done") is None and ci.cancel_action("skipped_duplicate") is None
    assert ci.retry_action("failed", has_job=False) == "download"
    assert ci.retry_action("cancelled", has_job=True) == "job"
    assert ci.retry_action("done", has_job=True) is None and ci.retry_action("downloading", False) is None


# ── dedupe ───────────────────────────────────────────────────────────────────

T0 = datetime(2026, 10, 1)


def test_import_state_by_version():
    done_v1 = ci.Prior("v1", "done", "job1", "it1", "b1", T0)
    assert ci.import_state("v1", [done_v1]).state == "imported"
    assert ci.import_state("v1", [done_v1]).job_id == "job1"
    changed = ci.import_state("v2", [done_v1])
    assert changed.state == "changed" and changed.job_id == "job1"
    assert ci.import_state("v1", [ci.Prior("v1", "failed", at=T0)]).state == "none"     # failed → may import again
    assert ci.import_state("v1", [ci.Prior("v1", "downloading", at=T0)]).state == "importing"
    legacy = ci.Prior(None, "done", "jobL", at=T0, legacy=True)                         # folder sync before imports
    assert ci.import_state("v9", [legacy]).state == "imported"
    newest = ci.import_state("v1", [done_v1, ci.Prior("v1", "processing", "job2", at=T0 + timedelta(hours=1))])
    assert (newest.state, newest.job_id) == ("importing", "job2")


def test_plan_items_skips_duplicates_unless_reimport():
    a = cp.DriveEntry("a", "a.mp4", "file", "video/mp4", version="v1")
    b = cp.DriveEntry("b", "b.mp4", "file", "video/mp4", version="v2")
    priors = {"a": [ci.Prior("v1", "done", "jobA", at=T0)], "b": [ci.Prior("v1", "done", "jobB", at=T0)]}
    plan = ci.plan_items([a, b, a], priors)
    assert [(p.entry.id, p.status, p.duplicate_of_job_id) for p in plan] == [
        ("a", "skipped_duplicate", "jobA"), ("b", "queued", None)]               # b changed → imported again
    assert [p.status for p in ci.plan_items([a], priors, reimport=True)] == ["queued"]


# ── worker: batch lifecycle ──────────────────────────────────────────────────

class FakeSession:
    def __init__(self, *objs):
        self.objs, self.added, self.commits = {}, [], 0
        for o in objs:
            self.add(o)

    async def get(self, model, key):
        return self.objs.get((model, key))

    def add(self, obj):
        self.added.append(obj)
        if getattr(obj, "id", None) is not None:
            self.objs[(type(obj), obj.id)] = obj

    async def commit(self):
        self.commits += 1

    async def flush(self):
        pass

    async def rollback(self):
        pass


class FakeRedis:
    def __init__(self):
        self.kv, self.h = {}, {}

    def hset(self, key, mapping):
        self.h.setdefault(key, {}).update(mapping)

    def expire(self, key, ttl):
        pass

    def get(self, key):
        return self.kv.get(key)

    def set(self, key, value, ex=None):
        self.kv[key] = value

    def delete(self, key):
        self.kv.pop(key, None)
        self.h.pop(key, None)


PAYLOAD = bytes(range(256)) * 40          # 10 240 bytes


def _setup(tmp_path, *, size=len(PAYLOAD), checksum=None, provider="google_drive"):
    conn = CloudConnection(id="c1", provider=provider, name="Fleet", enabled=True, status="active",
                           refresh_token_enc="enc", files_imported_total=0)
    batch = CloudImportBatch(id="b1", connection_id="c1", provider=provider, origin="manual",
                             models="pothole,signs", frame_skip=3, cancel_requested=False, created_by="op@x")
    item = CloudImportItem(id="i1", batch_id="b1", connection_id="c1", file_id="FILE", name="run.mp4",
                           status="queued", bytes_total=size, bytes_done=0, attempts=0, checksum=checksum)
    db = FakeSession(conn, batch, item)
    return db, conn, batch, item


def _deps(tmp_path, handler, r=None, **kw):
    enqueued = []

    async def enqueue(db, job, actor):
        enqueued.append((job, actor))

    async def token(db, conn, client):
        return "AT"

    async def no_duplicate(db, fp):
        return None

    kw.setdefault("find_duplicate", no_duplicate)
    clock = itertools.count()
    deps = ci.WorkerDeps(redis=r if r is not None else FakeRedis(), client=_mock(handler), token=token,
                         sleep=lambda s: _noop(), clock=lambda: float(next(clock)), enqueue=enqueue,
                         temp_dir=tmp_path, **kw)
    return deps, enqueued


async def _noop():
    return None


async def test_import_item_downloads_verifies_and_hands_over(tmp_path):
    md5 = hashlib.md5(PAYLOAD).hexdigest()
    db, conn, batch, item = _setup(tmp_path, checksum=md5)

    def handler(req):
        assert req.headers["authorization"] == "Bearer AT" and "range" not in req.headers
        return httpx.Response(200, content=PAYLOAD)

    deps, enqueued = _deps(tmp_path, handler)
    assert await ci.run_item(db, "i1", deps) == "queued_for_processing"
    assert item.status == "queued_for_processing" and item.attempts == 1 and item.bytes_done == len(PAYLOAD)
    assert item.dest_path.endswith(".mp4") and open(item.dest_path, "rb").read() == PAYLOAD
    assert not (tmp_path / "import_i1.mp4.part").exists()
    (job, actor), = enqueued
    assert isinstance(job, Job) and item.job_id == job.id and actor == "op@x"
    assert (job.source_type, job.source_ref, job.connection_id, job.enabled_models, job.frame_skip) == \
        ("google_drive", "FILE", "c1", "pothole,signs", 3)
    assert conn.files_imported_total == 1
    live = deps.redis.h[ci.live_key("i1")]
    assert live["phase"] == "queued_for_processing"


async def test_import_of_already_processed_content_is_skipped(tmp_path):
    """Same bytes as a processed job (content fingerprint): skipped_duplicate, no job, file removed."""
    from app.modules.m07_ingestion import fingerprint
    db, conn, batch, item = _setup(tmp_path)
    seen = []
    previous = Job(id="J-OLD", filename="old.mp4", status="done")

    async def find(db_, fp):
        seen.append(fp)
        return previous

    deps, enqueued = _deps(tmp_path, lambda req: httpx.Response(200, content=PAYLOAD), find_duplicate=find)
    assert await ci.run_item(db, "i1", deps) == "skipped_duplicate"
    assert item.duplicate_of_job_id == "J-OLD" and enqueued == [] and item.job_id is None
    assert seen == ["sha256:" + hashlib.sha256(PAYLOAD).hexdigest()]
    assert not os.path.exists(item.dest_path)
    assert fingerprint.file_fingerprint  # same function used by uploads

    # the batch asked to re-import: processed again
    db, conn, batch, item = _setup(tmp_path)
    batch.options = {"reimport": True}
    deps, enqueued = _deps(tmp_path, lambda req: httpx.Response(200, content=PAYLOAD), find_duplicate=find)
    assert await ci.run_item(db, "i1", deps) == "queued_for_processing"
    (job, _), = enqueued
    assert job.content_fingerprint == seen[-1]


async def test_transient_error_resumes_with_range(tmp_path):
    db, _, _, item = _setup(tmp_path)
    calls = []

    class Flaky(httpx.ByteStream):
        def __iter__(self):
            yield PAYLOAD[:4096]
            raise httpx.ReadError("connection reset")

        async def __aiter__(self):
            yield PAYLOAD[:4096]
            raise httpx.ReadError("connection reset")

    def handler(req):
        calls.append(req.headers.get("range"))
        if len(calls) == 1:
            return httpx.Response(200, stream=Flaky(b""))
        assert req.headers["range"] == "bytes=4096-"
        return httpx.Response(206, content=PAYLOAD[4096:])

    deps, _ = _deps(tmp_path, handler)
    assert await ci.run_item(db, "i1", deps) == "queued_for_processing"
    assert calls == [None, "bytes=4096-"]
    assert open(item.dest_path, "rb").read() == PAYLOAD


async def test_server_ignoring_range_restarts_from_zero(tmp_path):
    db, _, _, item = _setup(tmp_path)
    (tmp_path / "import_i1.mp4.part").write_bytes(b"stale-partial")
    deps, _ = _deps(tmp_path, lambda req: httpx.Response(200, content=PAYLOAD))
    assert await ci.run_item(db, "i1", deps) == "queued_for_processing"
    assert open(item.dest_path, "rb").read() == PAYLOAD


async def test_permanent_error_fails_readably_and_retry_resets(tmp_path):
    db, _, _, item = _setup(tmp_path)
    deps, enqueued = _deps(tmp_path, lambda req: httpx.Response(404, json={"error": {"message": "File not found"}}))
    assert await ci.run_item(db, "i1", deps) == "failed"
    assert "404" in item.error and "File not found" in item.error
    assert item.finished_at is not None and not enqueued
    ci.reset_for_retry(item)
    assert (item.status, item.error, item.finished_at) == ("queued", None, None)


async def test_transient_errors_exhaust_retries(tmp_path):
    db, _, _, item = _setup(tmp_path)
    hits = []

    def handler(req):
        hits.append(1)
        return httpx.Response(503, json={"error": {"message": "backend error"}})

    deps, _ = _deps(tmp_path, handler)
    assert await ci.run_item(db, "i1", deps) == "failed"
    assert len(hits) == ci.MAX_TRANSIENT_RETRIES + 1 and "503" in item.error


async def test_expired_token_is_refreshed(tmp_path):
    db, _, _, item = _setup(tmp_path)
    tokens = iter(["OLD", "NEW"])

    async def token(db, conn, client):
        return next(tokens)

    def handler(req):
        if req.headers["authorization"] == "Bearer OLD":
            return httpx.Response(401, json={"error": {"message": "expired"}})
        return httpx.Response(200, content=PAYLOAD)

    deps, _ = _deps(tmp_path, handler)
    deps.token = token
    assert await ci.run_item(db, "i1", deps) == "queued_for_processing"


async def test_checksum_and_size_mismatch_fail(tmp_path):
    db, _, _, item = _setup(tmp_path, checksum="0" * 32)
    deps, enqueued = _deps(tmp_path, lambda req: httpx.Response(200, content=PAYLOAD))
    assert await ci.run_item(db, "i1", deps) == "failed"
    assert "Checksum mismatch" in item.error and not enqueued
    assert not (tmp_path / "import_i1.mp4.part").exists()

    db, _, _, item = _setup(tmp_path, size=len(PAYLOAD) + 1)
    deps, _ = _deps(tmp_path, lambda req: httpx.Response(200, content=PAYLOAD))
    assert await ci.run_item(db, "i1", deps) == "failed"
    assert "announced" in item.error


async def test_cancel_signal_stops_download(tmp_path):
    db, _, _, item = _setup(tmp_path)
    r = FakeRedis()

    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            for i in range(10):
                if i == 2:
                    r.set(ci.cancel_key("i1"), "1")         # cancel requested mid-way
                yield PAYLOAD[i * 1024:(i + 1) * 1024]

    deps, enqueued = _deps(tmp_path, lambda req: httpx.Response(200, stream=Slow()), r=r)
    assert await ci.run_item(db, "i1", deps) == "cancelled"
    assert item.finished_at is not None and not enqueued
    assert not (tmp_path / "import_i1.mp4.part").exists()
    assert ci.live_key("i1") not in r.h


async def test_cancelled_or_handed_over_items_are_skipped(tmp_path):
    db, _, batch, item = _setup(tmp_path)
    batch.cancel_requested = True
    deps, _ = _deps(tmp_path, lambda req: pytest.fail("no download expected"))
    assert await ci.run_item(db, "i1", deps) == "cancelled"
    item.status = "queued_for_processing"
    assert await ci.run_item(db, "i1", deps) == "queued_for_processing"
    assert await ci.run_item(db, "missing", deps) == "missing"


async def test_unusable_connection_fails_item(tmp_path):
    db, conn, _, item = _setup(tmp_path)
    conn.status = "error"
    deps, _ = _deps(tmp_path, lambda req: pytest.fail("no download expected"))
    assert await ci.run_item(db, "i1", deps) == "failed"
    assert "reconnected" in item.error


def test_item_view_merges_live_counters_and_job():
    item = CloudImportItem(id="i", batch_id="b", file_id="f", name="a.mp4", status="downloading",
                           bytes_total=1000, bytes_done=100, attempts=1, created_at=T0)
    v = ci.item_view(item, None, {"bytes_done": "400", "bytes_total": "1000", "speed_bps": "100"})
    assert (v["status"], v["bytes_done"], v["percent"], v["speed_bps"], v["eta_s"]) == \
        ("downloading", 400, 40.0, 100.0, 6.0)
    item.status, item.job_id, item.bytes_done = "queued_for_processing", "j", 1000
    job = SimpleNamespace(status=SimpleNamespace(value="processing"), total_frames=200, processed=50,
                          detections=3, error=None)
    v = ci.item_view(item, job)
    assert (v["status"], v["job_progress"], v["percent"], v["eta_s"]) == ("processing", 0.25, 100.0, None)
    job.status, job.error = SimpleNamespace(value="failed"), "Cannot open source media"
    assert ci.item_view(item, job)["error"] == "Cannot open source media"


async def test_watched_folder_sync_creates_a_batch_of_new_files(tmp_path, monkeypatch):
    from app.modules.m07_ingestion import cloud_storage
    from app.workers import celery_app  # noqa: F401 — loads job_tasks in its normal order
    from app.workers import job_tasks

    conn = CloudConnection(id="c1", provider="google_drive", name="Fleet", enabled=True, status="active",
                           refresh_token_enc="enc", folder_id="W", auto_import=True,
                           auto_import_models=["pothole"], auto_import_frame_skip=4, files_imported_total=0)
    db = FakeSession(conn)

    async def token(db, conn, cfg=None, client=None):
        return "AT"

    async def priors(db, connection_id, file_ids):
        return {"old": [ci.Prior(None, "done", "jobOld", at=T0, legacy=True)]}

    monkeypatch.setattr(cloud_storage, "connection_token", token)
    monkeypatch.setattr(ci, "prior_imports", priors)

    def handler(req):
        return httpx.Response(200, json={"files": [
            {"id": "old", "name": "old.mp4", "mimeType": "video/mp4", "modifiedTime": "2026-01-01"},
            {"id": "new", "name": "new.mp4", "mimeType": "video/mp4", "modifiedTime": "2026-02-01"},
            {"id": "doc", "name": "doc.pdf", "mimeType": "application/pdf"},
        ]})

    sent = []
    async with _mock(handler) as client:
        n = await job_tasks.sync_connection(db, "c1", "system", client=client,
                                            dispatch_fn=lambda it: sent.append(it) or True)
    assert n == 1 and [it.file_id for it in sent] == ["new"]
    batch = next(o for o in db.added if isinstance(o, CloudImportBatch))
    assert (batch.origin, batch.models, batch.frame_skip) == ("sync", "pothole", 4)
    assert conn.last_sync_status == "ok" and conn.last_sync_files == 1


async def test_onedrive_shared_with_me_and_remote_drive():
    """A folder shared by another person (OneDrive for Business / SharePoint): listed under the
    virtual 'Shared with me', browsed / downloaded through /drives/<driveId>/items/<itemId>."""
    DRIVE = "b!AbC-dEf_123"
    seen = []

    def handler(req: httpx.Request):
        seen.append(req.url.path)
        if req.url.path == "/v1.0/me/drive/root/children":
            return httpx.Response(200, json={"value": [{"id": "own", "name": "Mine", "folder": {}}]})
        if req.url.path == "/v1.0/me/drive/items/own/children":
            return httpx.Response(200, json={"value": []})
        if req.url.path == "/v1.0/me/drive/sharedWithMe":
            return httpx.Response(200, json={"value": [{"id": "x", "name": "Road Analysis IA", "remoteItem": {
                "id": "01RAI", "name": "Road Analysis IA", "folder": {"childCount": 2},
                "parentReference": {"driveId": DRIVE}}}]})
        if req.url.path == f"/v1.0/drives/{DRIVE}/items/01RAI/children":
            return httpx.Response(200, json={"value": [
                {"id": "01VID", "name": "LTTR.mp4", "size": 5, "file": {"mimeType": "video/mp4"},
                 "parentReference": {"driveId": DRIVE, "id": "01RAI"}}]})
        if req.url.path == f"/v1.0/drives/{DRIVE}/items/01RAI":
            return httpx.Response(200, json={"id": "01RAI", "name": "Road Analysis IA",
                                             "parentReference": {"driveId": DRIVE, "id": "01PARENT"}})
        if req.url.path == f"/v1.0/drives/{DRIVE}/items/01PARENT":
            return httpx.Response(403, json={"error": {"code": "accessDenied"}})
        return httpx.Response(404)

    p = cp.PROVIDERS["onedrive"]
    async with _mock(handler) as c:
        root = await p.browse("t", None, client=c)
        assert [e.id for e in root.entries] == ["shared", "own"] and root.entries[0].virtual
        shared = await p.browse("t", "shared", client=c)
        folder = shared.entries[0]
        assert (folder.id, folder.name, folder.kind) == (f"{DRIVE}~01RAI", "Road Analysis IA", "folder")
        inside = await p.browse("t", folder.id, client=c)
        assert inside.entries[0].id == f"{DRIVE}~01VID"
        crumbs = await p.path("t", folder.id, c)              # parent not visible: stops at the share
        assert [x.id for x in crumbs] == ["shared", f"{DRIVE}~01RAI"]
        # a recursive walk from the own root never enters "Shared with me"
        seen.clear()
        await p.walk_media("t", None, client=c)
        assert seen == ["/v1.0/me/drive/root/children", "/v1.0/me/drive/items/own/children"]
    url, _ = p._download_request(f"{DRIVE}~01VID")
    assert url == f"https://graph.microsoft.com/v1.0/drives/{DRIVE}/items/01VID/content"
    assert p._download_request("01OWN")[0].endswith("/me/drive/items/01OWN/content")   # own drive unchanged
