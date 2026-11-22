"""ForexFactory dates without a UTC offset are US Eastern wall-clock time, never the server's zone."""

import os
import time
from datetime import UTC, datetime

import pytest

from app.sources import forexfactory as ff


def event_time(date):
    ev = ff.parse_event({"title": "CPI m/m", "country": "USD", "impact": "High", "date": date})
    return ev.event_time if ev else None


def test_offsetless_winter_date_is_new_york_standard_time():
    assert event_time("2026-11-11T08:30:00") == datetime(2026, 11, 11, 13, 30, tzinfo=UTC)  # EST, UTC-5


def test_offsetless_summer_date_is_new_york_daylight_time():
    assert event_time("2026-07-15T08:30:00") == datetime(2026, 7, 15, 12, 30, tzinfo=UTC)  # EDT, UTC-4


def test_date_only_is_midnight_new_york():
    assert event_time("2026-11-11") == datetime(2026, 11, 11, 5, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "date,expected",
    [
        ("2026-11-11T08:30:00-05:00", datetime(2026, 11, 11, 13, 30, tzinfo=UTC)),  # the real feed format
        ("2026-11-11T14:30:00+01:00", datetime(2026, 11, 11, 13, 30, tzinfo=UTC)),
        ("2026-11-11T13:30:00Z", datetime(2026, 11, 11, 13, 30, tzinfo=UTC)),
    ],
)
def test_explicit_offsets_are_respected(date, expected):
    assert event_time(date) == expected


@pytest.fixture
def server_tz():
    old = os.environ.get("TZ")

    def set_tz(name):
        os.environ["TZ"] = name
        time.tzset()

    yield set_tz
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


@pytest.mark.parametrize("tz", ["UTC", "Asia/Ho_Chi_Minh", "America/Los_Angeles", "Australia/Sydney"])
def test_result_does_not_depend_on_the_server_timezone(server_tz, tz):
    server_tz(tz)
    assert event_time("2026-11-11T08:30:00") == datetime(2026, 11, 11, 13, 30, tzinfo=UTC)
    assert event_time("2026-07-15T08:30:00") == datetime(2026, 7, 15, 12, 30, tzinfo=UTC)


def test_without_tz_database_offsetless_dates_are_skipped_but_offset_ones_still_parse(monkeypatch):
    monkeypatch.setattr(ff, "FF_NAIVE_TZ", None)
    assert event_time("2026-11-11T08:30:00") is None
    assert event_time("2026-11-11T08:30:00-05:00") == datetime(2026, 11, 11, 13, 30, tzinfo=UTC)


def test_garbage_dates_are_still_skipped():
    assert event_time("tomorrow-ish") is None
    assert ff.parse_event({"title": "CPI", "country": "USD"}) is None
