"""
Out-of-order write protection for the ping-triggered background pipeline
(see app/services/staleness_guard.py's module docstring for the scenario
this guards against). Tests the pure comparison directly — no Redis — same
convention as tests/test_alerts.py and tests/test_collision.py: the Redis
I/O wrapper around it (skip_if_stale) is "live-verified separately", this
file covers the decision logic it's built on.
"""
from datetime import datetime, timedelta, timezone

from app.services.staleness_guard import is_stale

NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
EARLIER = NOW - timedelta(seconds=5)
LATER = NOW + timedelta(seconds=5)


def test_no_cached_value_is_not_stale():
    # Nothing written yet for this key — first writer always proceeds.
    assert is_stale(None, NOW) is False


def test_empty_string_is_not_stale():
    assert is_stale("", NOW) is False


def test_new_write_older_than_cached_is_stale():
    # Cached value is NEWER than the incoming write — the incoming write is
    # the straggler (e.g. a slower OSRM round-trip for an older ping) and
    # should be skipped.
    cached_ts_raw = LATER.isoformat()
    assert is_stale(cached_ts_raw, NOW) is True


def test_new_write_newer_than_cached_is_not_stale():
    cached_ts_raw = EARLIER.isoformat()
    assert is_stale(cached_ts_raw, NOW) is False


def test_equal_timestamps_are_not_stale():
    # An idempotent retry carrying the exact same telemetry timestamp should
    # still be allowed to write (strict > only, not >=).
    cached_ts_raw = NOW.isoformat()
    assert is_stale(cached_ts_raw, NOW) is False


def test_unparseable_cached_value_fails_open():
    assert is_stale("not-a-timestamp", NOW) is False


def test_naive_vs_aware_mismatch_fails_open_without_raising():
    # The incoming `ts` on a ping is optional and unvalidated for timezone
    # awareness (see VehiclePingIn.ts) — a comparison against a naive
    # datetime must not crash the pipeline; it should just not protect.
    naive_cached = datetime(2026, 1, 1, 12, 0, 5).isoformat()  # no tzinfo
    assert is_stale(naive_cached, NOW) is False


def test_naive_new_ts_against_aware_cached_fails_open_without_raising():
    naive_new_ts = datetime(2026, 1, 1, 12, 0, 0)  # no tzinfo
    cached_ts_raw = LATER.isoformat()
    assert is_stale(cached_ts_raw, naive_new_ts) is False
