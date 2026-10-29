"""
Daily event files of an "events" source (SFTP): the CSV reader, which files a check
fetches, the bulk insert-or-update, one file's import and one whole check — without a
server, OSRM or MinIO.
"""
from datetime import date, datetime
from types import SimpleNamespace

import pytest

from app.modules.m08_historical import event_feed as ef
from app.modules.m08_historical import matcher
from app.modules.m08_historical.models.event_file import EventFile
from app.modules.m08_historical.sources import ymane_csv

HEADER = ("exceptionid,vehicledesc,driver_name,startdatetime,enddatetime,exception_type_name,totalduration,"
          "totaldistance,level,threshold,maxvalue,startgps,vmax,unitid,customer_affiliate_name,transporter_name,"
          "evidence\n")


def csv(*lines: str) -> bytes:
    return (HEADER + "\n".join(lines) + "\n").encode()


ROW_CM = ('1,LTTR 995 AR,CLE.SODITRANS,2026-10-27T05:01:38,2026-10-27T05:02:16,Speeding,0.010556,0.43935,1,'
          '30.0,45.0,"9.12739753723,4.01793193817",41.0,TEMCM168,CAMEROUN,SODITRANS-GIE,"{""a"": 1}"')
ROW_MG = ('2,8214 TCC,UNKNOWN DRIVER,2026-10-27T17:25:14,2026-10-27T17:25:15,Harsh Braking,0.000278,0,3,'
          '6.0,11.0,"(49.1542320251,-18.4344997406)",11.0,TMSA033,MADAGASIKARA,ENAC,')
ROW_CORNER = ('3,0775 AK,Jean,2026-10-27T10:00:00,,Harsh Cornering,,,2,0.4,0.6,"(47.05,-20.01)",,TMSA008,'
              'Madagasikara,MIRAY TRANS,')


# ── reader ───────────────────────────────────────────────────────────────────

def test_reads_both_gps_formats_longitude_first_types_units_and_context():
    r = ymane_csv.parse(csv(ROW_CM, ROW_MG, ROW_CORNER))
    assert (r.rows, r.invalid, len(r.events)) == (3, 0, 3)
    cm, mg, corner = r.events
    assert (cm["latitude"], cm["longitude"]) == (4.01793193817, 9.12739753723)
    assert (mg["latitude"], mg["longitude"]) == (-18.4344997406, 49.1542320251)
    assert cm["event_type"] == "SPEEDING" and cm["source"] == "ymane" and cm["source_event_id"] == "1"
    assert cm["speed_mps"] == round(41 / 3.6, 3) and cm["speed_limit_mps"] == round(30 / 3.6, 3)
    assert cm["occurred_at"] == datetime(2026, 10, 27, 5, 1, 38) and cm["duration_s"] == 38.0
    assert (cm["country"], cm["transporter"], cm["severity"]) == ("CAMEROUN", "SODITRANS-GIE", "1")
    assert mg["event_type"] == "HARD_BRAKING"
    assert mg["speed_limit_mps"] is None                      # threshold of a harsh event is not a speed
    assert corner["event_type"] == "HARSH_CORNERING" and corner["country"] == "MADAGASIKARA"
    assert corner["ended_at"] is None and corner["duration_s"] is None
    assert r.by_type() == {"SPEEDING": 1, "HARD_BRAKING": 1, "HARSH_CORNERING": 1}
    assert cm["raw"]["exception_type_name"] == "Speeding" and cm["raw"]["transporter_name"] == "SODITRANS-GIE"


def test_no_driver_or_vehicle_is_kept_not_even_in_raw():
    for e in ymane_csv.parse(csv(ROW_CM, ROW_MG, ROW_CORNER)).events:
        assert not [k for k in e if "driver" in k or "vehicle" in k]
        assert not {"vehicledesc", "driver_name", "unitid", "evidence", "speedid", "usercoments"} & set(e["raw"])
        flat = repr(e)
        for identity in ("LTTR 995 AR", "CLE.SODITRANS", "TEMCM168", "8214 TCC", "TMSA033", "0775 AK", "Jean"):
            assert identity not in flat
    # a full export header (every provider column) keeps only the whitelisted ones
    from app.modules.m08_historical.anonymity import RAW_KEYS, clean_raw
    full = {"exceptionid": "1", "vehicledesc": "LTTR 995 AR", "driver_name": "X", "unitid": "U", "speedid": "S_U",
            "evidence": "{}", "usercoments": "call Paul", "restoreduserid": "42", "aidetectiondetails": "{}",
            "vmax": "41", "transporter_name": "T", "Level": "2", "empty": ""}
    assert clean_raw(full) == {"exceptionid": "1", "vmax": "41", "transporter_name": "T", "level": "2"}
    assert "vehicledesc" not in RAW_KEYS and "driver_name" not in RAW_KEYS


def test_bad_lines_are_skipped_with_a_reason_and_duplicates_kept_once():
    bad_gps = ROW_CM.replace('"9.12739753723,4.01793193817"', '"0,0"').replace("1,LTTR", "7,LTTR", 1)
    bad_time = ROW_CM.replace("2026-10-27T05:01:38", "yesterday").replace("1,LTTR", "8,LTTR", 1)
    r = ymane_csv.parse(csv(ROW_CM, bad_gps, bad_time, ROW_CM.replace("Speeding", "Harsh Acceleration")))
    assert r.invalid == 2 and [p["reason"] for p in r.problems] == [
        "exception 7: invalid or missing startgps", "exception 8: invalid startdatetime"]
    assert len(r.events) == 1 and r.events[0]["event_type"] == "HARD_ACCELERATION"      # last one wins


def test_not_an_export_is_refused():
    with pytest.raises(ymane_csv.CsvFormatError, match="startgps"):
        ymane_csv.parse(b"exceptionid,startdatetime,exception_type_name\n1,2026-10-27T00:00:00,Speeding\n")
    with pytest.raises(ymane_csv.CsvFormatError):
        ymane_csv.parse(b"\x00\x01binary")


def test_latin1_file_and_utc_offsets():
    data = csv(ROW_CM.replace("SODITRANS-GIE", "SOCIÉTÉ").replace("2026-10-27T05:01:38", "2026-10-27T06:01:38+01:00"))
    e = ymane_csv.parse(data.decode().encode("latin-1")).events[0]
    assert e["transporter"] == "SOCIÉTÉ" and e["occurred_at"] == datetime(2026, 10, 27, 5, 1, 38)


# ── which files a check fetches ──────────────────────────────────────────────

def rf(name, size=100, mtime=datetime(2026, 10, 28, 3)):
    return ef.RemoteFile(name, f"/upload/{name}", size, mtime)


def known(status="imported", size=100, mtime=datetime(2026, 10, 28, 3), attempts=1):
    return SimpleNamespace(status=status, size=size, mtime=mtime, attempts=attempts)


def test_file_names_and_plan():
    assert ef.day_of("2026-10-27.csv") == date(2026, 10, 27) == ef.day_of("2026_10_27.CSV")
    assert ef.day_of("2026_10_27 (1).csv") is None and ef.day_of("notes.csv") is None
    assert ef.day_of("2026-02-30.csv") is None
    remote = [rf("2026-10-27.csv"), rf("2026-10-25.csv"), rf("2026-10-26.csv", size=200), rf("readme.txt"),
              rf("2026-10-24.csv"), rf("2026-10-23.csv")]
    k = {"2026-10-26.csv": known(),                                   # regenerated (size changed)
         "2026-10-25.csv": known(),                                   # unchanged
         "2026-10-24.csv": known("failed", attempts=2),               # retried
         "2026-10-23.csv": known("failed", attempts=ef.MAX_ATTEMPTS)}  # gave up
    assert [f.name for f in ef.plan(remote, k)] == ["2026-10-24.csv", "2026-10-26.csv", "2026-10-27.csv"]
    assert [f.name for f in ef.plan(remote, k, force={"2026-10-25.csv"})] == [
        "2026-10-24.csv", "2026-10-25.csv", "2026-10-26.csv", "2026-10-27.csv"]
    many = [rf(f"2026-01-{d:02d}.csv") for d in range(1, 32)] + [rf("2026-02-01.csv")]
    assert len(ef.plan(many, {})) == ef.MAX_FILES_PER_RUN and ef.plan(many, {})[0].name == "2026-01-01.csv"


# ── storage ──────────────────────────────────────────────────────────────────

def test_upsert_updates_the_event_but_keeps_its_road_matching():
    from sqlalchemy.dialects import postgresql
    sql = str(ef.upsert_statement([{"id": "x", "source": "ymane", "source_event_id": "1"}])
              .compile(dialect=postgresql.dialect()))
    assert "ON CONFLICT ON CONSTRAINT uq_historical_event_source DO UPDATE SET" in sql
    assert "country = excluded.country" in sql and "RETURNING (xmax = 0)" in sql
    assert "road_name = excluded" not in sql and "imported_at = excluded" not in sql


def test_far_from_any_known_road_is_not_matched():
    from app.modules.m08_historical.domain import HistoricalEvent, HistoricalEventType

    class Osrm:
        def __init__(self, dist):
            self.dist = dist

        async def nearest(self, point):
            return SimpleNamespace(data={"code": "Ok", "waypoints": [
                {"location": [9.7, 4.05], "name": "N3", "distance": self.dist}]})

    ev = HistoricalEvent(source="ymane", source_event_id="1", event_type=HistoricalEventType.SPEEDING,
                         occurred_at=datetime(2026, 10, 27), latitude=-18.43, longitude=49.15)
    import asyncio
    far = asyncio.run(matcher.match_event(ev, client=Osrm(900_000)))
    near = asyncio.run(matcher.match_event(ev, client=Osrm(12)))
    assert far.road_name is None and far.grid_cell_id
    assert near.road_name == "N3" and near.match_confidence == 1.0


async def test_ingest_archives_matches_only_new_events_and_skips_an_unchanged_file(monkeypatch):
    archived, upserts, matched = [], [], []
    monkeypatch.setattr(ef, "_archive", lambda key, data: archived.append(key))

    async def existing(db, source, ids):
        return {"1"}

    async def upsert(db, events, file_id):
        upserts.append([e["source_event_id"] for e in events])
        return 2, 1

    async def match(ev):
        matched.append(ev.source_event_id)
        return SimpleNamespace(matched_latitude=1.0, matched_longitude=2.0, road_name="R", road_segment_id="s",
                               match_confidence=1.0)

    monkeypatch.setattr(ef, "_existing_ids", existing)
    monkeypatch.setattr(ef, "upsert", upsert)
    f = EventFile(id="f1", connection_id="c1", name="2026-10-27.csv", path="/upload/2026-10-27.csv",
                  day=date(2026, 10, 27), status="pending", attempts=1)
    data = csv(ROW_CM, ROW_MG, ROW_CORNER)
    assert await ef.ingest(None, "c1", f, data, matcher=match) == "imported"
    assert archived == ["c1/2026/10/2026-10-27.csv.gz"] and f.archive_key == archived[0]
    assert sorted(matched) == ["2", "3"]                       # "1" was already stored
    assert upserts == [["1", "2", "3"]]
    assert (f.status, f.rows, f.inserted, f.updated, f.invalid) == ("imported", 3, 2, 1, 0)
    assert f.by_type == {"SPEEDING": 1, "HARD_BRAKING": 1, "HARSH_CORNERING": 1} and f.sha256

    assert await ef.ingest(None, "c1", f, data, matcher=match) == "unchanged"
    assert len(upserts) == 1


async def test_a_check_imports_new_files_and_records_failures(monkeypatch):
    from app.modules.m07_ingestion import cloud_providers, sftp_source
    from app.modules.m07_ingestion.models.cloud_connection import CloudConnection

    conn = CloudConnection(id="c1", provider="sftp", name="Ymane", purpose="events", enabled=True, status="active",
                           sftp_host="h", sftp_username="u", sftp_password_enc="x", files_imported_total=0)
    files = {"2026-10-27.csv": csv(ROW_CM), "2026-10-26.csv": b"garbage,without,columns\n1,2,3\n"}

    class Session:
        base = "/upload"
        log = SimpleNamespace(status=lambda m: None)

        def listdir(self, folder):
            return [SimpleNamespace(filename=n, st_size=len(d), st_mtime=1793156400, st_mode=0o100644)
                    for n, d in files.items()] + [SimpleNamespace(filename="x.txt", st_size=1, st_mtime=1, st_mode=0)]

    monkeypatch.setattr(ef, "read_file", lambda s, f: files[f.name])
    monkeypatch.setattr(cloud_providers, "get_provider", lambda name: SimpleNamespace(
        run_session=lambda target, op: op(Session())))
    monkeypatch.setattr(sftp_source, "make_log", lambda cid, source="api": SimpleNamespace(
        status=lambda m: None, error=lambda m: None))

    async def ingest(db, cid, f, data, matcher=None, touched=None):
        if b"exceptionid" not in data:
            raise ymane_csv.CsvFormatError("Not an exception export")
        f.status, f.inserted, f.updated, f.rows = "imported", 1, 0, 1
        return "imported"

    monkeypatch.setattr(ef, "ingest", ingest)

    class Db:
        def __init__(self):
            self.rows = {("c", "c1"): conn}

        async def get(self, model, key):
            return self.rows.get(("c" if model is CloudConnection else "f", key))

        async def execute(self, q):
            items = [v for (k, _), v in self.rows.items() if k == "f"]
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: items))

        def add(self, o):
            o.id = o.id or o.name
            self.rows[("f", o.id)] = o

        async def commit(self):
            pass

        async def rollback(self):
            pass

    db = Db()
    out = await ef.sync_connection(db, "c1")
    assert (out["listed"], out["fetched"], out["imported"], out["failed"]) == (2, 2, 1, 1)
    stored = {o.name: o for (k, _), o in db.rows.items() if k == "f"}
    assert stored["2026-10-26.csv"].status == "failed" and "exception export" in stored["2026-10-26.csv"].error
    assert stored["2026-10-27.csv"].status == "imported" and stored["2026-10-27.csv"].day == date(2026, 10, 27)
    assert conn.last_sync_status == "error" and conn.files_imported_total == 1

    # Next hour: nothing changed → only the failed file is retried.
    out = await ef.sync_connection(db, "c1")
    assert (out["fetched"], out["imported"]) == (1, 0)

    conn.purpose = "videos"
    assert (await ef.sync_connection(db, "c1")) == {"skipped": True}


# ── completeness / alerts ────────────────────────────────────────────────────

def f(day, status="imported", n=10, error=None):
    return SimpleNamespace(day=day, status=status, inserted=n, updated=0, name=f"{day}.csv", error=error)


def test_a_day_is_due_the_next_day_at_the_alert_hour():
    assert ef.expected_until(datetime(2026, 10, 29, 9, 59), None) == date(2026, 10, 27)
    assert ef.expected_until(datetime(2026, 10, 29, 10, 0), None) == date(2026, 10, 28)
    assert ef.expected_until(datetime(2026, 10, 29, 4, 0), 3) == date(2026, 10, 28)


def test_health_missing_failed_pending_and_complete_until():
    files = [f(date(2026, 10, 20)), f(date(2026, 10, 21)), f(date(2026, 10, 23), "failed", error="bad"),
             f(date(2026, 10, 24), "pending"), f(date(2026, 10, 25))]
    h = ef.health(files, datetime(2026, 10, 27, 11), None)          # due until the 26th
    assert h["first_day"] == date(2026, 10, 20) and h["complete_until"] == date(2026, 10, 21)
    assert h["missing"] == [date(2026, 10, 22), date(2026, 10, 26)] and h["failed"] == [date(2026, 10, 23)]
    days = ef.day_statuses(files, h["first_day"], h["due_until"], date(2026, 10, 19), date(2026, 10, 28))
    assert [d.status for d in days] == ["none", "imported", "imported", "missing", "failed", "pending",
                                        "imported", "missing", "waiting", "waiting"]
    assert days[1].events == 10 and days[4].error == "bad"
    assert ef.health([], datetime(2026, 10, 27), None)["missing"] == []          # no file yet: nothing expected


def test_a_reimported_day_counts_once_imported():
    files = [f(date(2026, 10, 20), "failed"), f(date(2026, 10, 20))]
    assert ef.health(files, datetime(2026, 10, 21, 12), None)["complete_until"] == date(2026, 10, 20)


def test_alert_email_text():
    from app.workers.event_feed_tasks import alert_text
    subject, text = alert_text("Ymane", [date(2026, 10, 26)], [date(2026, 10, 23)], "https://roadrisk.camtrack.net/",
                               "c1")
    assert subject == "[VIASafe] Event data missing for Ymane (2026-10-26…)"
    assert "Missing (no file on the server): 2026-10-26" in text and "Could not be imported: 2026-10-23" in text
    assert "https://roadrisk.camtrack.net/cloud-sources?connection=c1" in text


# ── several providers a day, processing hour ─────────────────────────────────

def test_the_provider_is_read_from_the_file_name():
    assert ef.source_of("2026-10-28_besafe.csv") == "besafe" and ef.source_of("2026-10-28_YMANE.csv") == "ymane"
    assert ef.source_of("2026-10-28.csv") == "ymane" and ef.source_of("2026_10_28.csv") == "ymane"
    assert ef.day_of("2026-10-28_besafe.csv") == date(2026, 10, 28)
    for bad in ("2026-10-28_.csv", "2026-10-28_be safe.csv", "report_2026-10-28.csv", "2026-10-28_besafe.txt"):
        assert ef.day_of(bad) is None


def test_a_file_waits_for_its_processing_time_unless_processed_now():
    remote = [ef.RemoteFile("2026-10-28_ymane.csv", "/in/2026-10-28_ymane.csv", 10, None),
              ef.RemoteFile("2026-10-28_besafe.csv", "/in/2026-10-28_besafe.csv", 10, None),
              ef.RemoteFile("2026-10-27_besafe.csv", "/in/2026-10-27_besafe.csv", 10, None)]
    early = datetime(2026, 10, 29, 3, 30)                                  # before 04:00 UTC on D+1
    assert [x.name for x in ef.plan(remote, {}, now=early)] == ["2026-10-27_besafe.csv"]
    assert [x.name for x in ef.plan(remote, {}, now=datetime(2026, 10, 29, 4))] == [
        "2026-10-27_besafe.csv", "2026-10-28_besafe.csv", "2026-10-28_ymane.csv"]
    assert [x.name for x in ef.plan(remote, {}, force={"2026-10-28_besafe.csv"}, now=early)] == [
        "2026-10-27_besafe.csv", "2026-10-28_besafe.csv"]
    assert ef.due_at(date(2026, 10, 28), 6) == datetime(2026, 10, 29, 6)


def test_a_day_is_complete_only_with_every_active_provider():
    def g(day, source, status="imported"):
        return SimpleNamespace(id=f"{day}{source}", day=day, source=source, status=status, inserted=5, updated=0,
                               name=f"{day}_{source}.csv", error=None)
    files = [g(date(2026, 10, 20), "ymane"),                                       # besafe not started yet
             g(date(2026, 10, 21), "ymane"), g(date(2026, 10, 21), "besafe"),
             g(date(2026, 10, 22), "ymane"),                                       # besafe missing
             g(date(2026, 10, 23), "ymane"), g(date(2026, 10, 23), "besafe")]
    h = ef.health(files, datetime(2026, 10, 24, 11), None)                       # due until the 23rd
    assert h["missing"] == [date(2026, 10, 22)] and h["missing_sources"] == {"2026-10-22": ["besafe"]}
    assert h["complete_until"] == date(2026, 10, 21)
    days = ef.day_statuses(files, h["first_day"], h["due_until"], date(2026, 10, 20), date(2026, 10, 23))
    assert [d.status for d in days] == ["imported", "imported", "missing", "imported"]
    assert days[1].events == 10 and days[2].sources == {"besafe": "missing", "ymane": "imported"}
