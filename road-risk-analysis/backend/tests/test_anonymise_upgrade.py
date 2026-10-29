"""
Anonymity upgrade (app/core/db/upgrade.py ANONYMISE_*, mirrored in migrations/anonymise_events.sql):
the driver / vehicle identifiers stored before are erased once — `raw` reduced to the whitelisted
fields, the identity columns overwritten then dropped, the daily summaries keyed by driver /
vehicle dropped — and nothing re-creates them on the next start (idempotent).

VIASAFE_TEST_PG_URL=postgresql+asyncpg://… also runs the statements on a throwaway PostgreSQL
(the test creates and drops its own tables there).
"""
import os
import re
from pathlib import Path

import pytest

from app.core.db import upgrade as U
from app.modules.m08_historical.anonymity import RAW_KEYS

ALL_IDS = {("historical_events", "driver_external_id"), ("historical_events", "vehicle_external_id"),
           ("vehicles", "plate"), ("vehicles", "driver_external_id")}
MIRROR = Path(__file__).resolve().parents[1] / "migrations" / "anonymise_events.sql"


def test_erasure_runs_while_identity_columns_exist_then_only_no_ops():
    first = U.anonymise_statements(ALL_IDS | {("historical_events", "raw")}, "json")
    raw_update, null_events, *rest = first
    assert raw_update.startswith("UPDATE historical_events SET raw") and raw_update.endswith("::json")
    assert null_events == ("UPDATE historical_events SET driver_external_id = NULL, vehicle_external_id = NULL "
                           "WHERE driver_external_id IS NOT NULL OR vehicle_external_id IS NOT NULL")
    drops = [s for s in first if "DROP COLUMN" in s]
    assert drops == ["ALTER TABLE historical_events DROP COLUMN IF EXISTS driver_external_id",
                     "ALTER TABLE historical_events DROP COLUMN IF EXISTS vehicle_external_id",
                     "ALTER TABLE vehicles DROP COLUMN IF EXISTS plate",
                     "ALTER TABLE vehicles DROP COLUMN IF EXISTS driver_external_id"]
    # values overwritten before their column is dropped
    for table, col in ALL_IDS:
        set_at = next(i for i, s in enumerate(first) if s.startswith(f"UPDATE {table} SET") and f"{col} = NULL" in s)
        assert set_at < first.index(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {col}")
    assert first[-3:] == U.ANONYMISE_ROLLUPS
    # once dropped: only the idempotent DROP TABLE IF EXISTS of the old summaries
    assert U.anonymise_statements({("historical_events", "raw")}) == U.ANONYMISE_ROLLUPS
    # a partly done erasure finishes what is left
    assert U.anonymise_statements({("vehicles", "plate")}) == [
        "UPDATE vehicles SET plate = NULL WHERE plate IS NOT NULL",
        "ALTER TABLE vehicles DROP COLUMN IF EXISTS plate", *U.ANONYMISE_ROLLUPS]


def test_raw_whitelist_sql_and_jsonb_columns():
    sql = U.anonymise_raw_sql("jsonb")
    assert sql.endswith("::jsonb") and "jsonb_typeof(raw::jsonb) = 'object'" in sql
    listed = re.findall(r"'([a-z_]+)'", sql.split(" IN (")[1])
    assert listed == list(RAW_KEYS)
    for identity in ("vehicledesc", "driver_name", "unitid", "vehicleid", "driverid", "speedid", "evidence",
                     "usercoments", "restoreduserid", "aidetectiondetails"):
        assert identity not in RAW_KEYS


def test_no_upgrade_statement_recreates_an_identity():
    lists = [v for k, v in vars(U).items() if k.isupper() and isinstance(v, list)]
    for stmts in lists:
        for s in stmts:
            low = s.lower()
            if "add column" in low or "create index" in low:
                assert not re.search(r"\b(plate|driver_external_id|vehicle_external_id)\b", low), s


def test_mirror_sql_matches():
    sql = MIRROR.read_text()
    for k in RAW_KEYS:
        assert f"'{k}'" in sql
    for s in ("UPDATE historical_events SET driver_external_id = NULL", "DROP COLUMN driver_external_id",
              "UPDATE vehicles SET plate = NULL", "DROP COLUMN plate", *U.ANONYMISE_ROLLUPS):
        assert s in sql


@pytest.mark.skipif(not os.getenv("VIASAFE_TEST_PG_URL"), reason="needs VIASAFE_TEST_PG_URL (PostgreSQL)")
@pytest.mark.parametrize("raw_type", ["json", "jsonb"])
async def test_erasure_on_postgresql(raw_type):
    from sqlalchemy.ext.asyncio import create_async_engine
    engine = create_async_engine(os.environ["VIASAFE_TEST_PG_URL"])
    async with engine.begin() as c:
        for t in ("historical_events", "vehicles", "event_daily", "event_daily_hour", "event_daily_cell"):
            await c.exec_driver_sql(f"DROP TABLE IF EXISTS {t}")
        await c.exec_driver_sql(
            f"CREATE TABLE historical_events (id VARCHAR PRIMARY KEY, driver_external_id VARCHAR, "
            f"vehicle_external_id VARCHAR, country VARCHAR, raw {raw_type})")
        await c.exec_driver_sql("CREATE INDEX ix_historical_events_driver ON historical_events (driver_external_id)")
        await c.exec_driver_sql("CREATE TABLE vehicles (id VARCHAR PRIMARY KEY, plate VARCHAR, driver_external_id VARCHAR)")
        await c.exec_driver_sql("CREATE TABLE event_daily (day DATE, driver VARCHAR, vehicle VARCHAR, n INTEGER)")
        await c.exec_driver_sql(
            "INSERT INTO historical_events VALUES "
            "('1', 'Jean', 'LTTR 995 AR', 'CAMEROUN', '{\"exceptionid\": \"1\", \"vehicledesc\": \"LTTR 995 AR\", "
            "\"driver_name\": \"Jean\", \"unitid\": \"TEMCM168\", \"Vmax\": \"41\", \"evidence\": \"{}\"}'), "
            "('2', NULL, NULL, NULL, 'null'), ('3', NULL, 'X', NULL, NULL)")
        await c.exec_driver_sql("INSERT INTO vehicles VALUES ('v1', 'LTTR 995 AR', 'D-1'), ('v2', NULL, NULL)")

    async def existing():
        async with engine.connect() as c:
            return await U._existing_columns(c)

    for _ in range(2):                                          # the second start finds nothing to do
        async with engine.begin() as c:
            for s in U.anonymise_statements(await existing(), await U._column_type(c, "historical_events", "raw")):
                await c.exec_driver_sql(s)
    cols = await existing()
    assert not {c for c in ALL_IDS if c in cols}
    async with engine.connect() as c:
        rows = (await c.exec_driver_sql("SELECT id, raw::text FROM historical_events ORDER BY id")).all()
        assert (await c.exec_driver_sql("SELECT to_regclass('event_daily')")).scalar() is None
        assert (await c.exec_driver_sql("SELECT count(*) FROM vehicles")).scalar() == 2
    import json
    assert json.loads(rows[0][1]) == {"exceptionid": "1", "vmax": "41"}
    assert json.loads(rows[1][1]) == {} and json.loads(rows[2][1]) == {}
    async with engine.begin() as c:
        for t in ("historical_events", "vehicles"):
            await c.exec_driver_sql(f"DROP TABLE IF EXISTS {t}")
    await engine.dispose()
