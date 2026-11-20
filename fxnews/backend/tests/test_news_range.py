"""GET /api/news and GET /api/calendar `from` / `to` event_time range filters."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import ingest
from app.sources.base import RawNews
from tests.support import wipe_db

BASE = datetime(2024, 3, 1, tzinfo=UTC)


@pytest.fixture
def client():
    wipe_db()
    yield TestClient(app)
    wipe_db()


def seed():
    items = [RawNews(kind="calendar", source="forexfactory", title=f"CAL {h}h", currency="USD", impact="High",
                     event_time=BASE + timedelta(hours=h)) for h in range(10)]
    # two calendar rows sharing one timestamp (exercises the id tie-break while paging)
    items += [RawNews(kind="calendar", source="forexfactory", title=f"CAL tie {c}", currency="EUR", impact="Low",
                      event_time=BASE + timedelta(hours=5)) for c in "ab"]
    items += [RawNews(kind="headline", source="fxstreet", title=f"HL {h}h", url=f"https://x/hl{h}",
                      event_time=BASE + timedelta(hours=h)) for h in (2, 4)]
    items.append(RawNews(kind="headline", source="fxstreet", title="HL undated", url="https://x/undated",
                         event_time=None))
    ingest.store_items(items)


def titles(rows):
    return sorted(r["title"] for r in rows)


def walk(client, limit, **params):
    seen, cursor = [], {}
    for _ in range(100):
        page = client.get("/api/news", params={"limit": limit, **params, **cursor}).json()
        if not page:
            return seen
        seen += page
        cursor = {"before": page[-1]["event_time"], "before_id": page[-1]["id"]}
    raise AssertionError("pagination did not terminate")


def test_from_is_inclusive_and_to_is_exclusive(client):
    seed()
    rows = client.get("/api/news", params={"kind": "calendar", "limit": 500,
                                           "from": "2024-03-01T02:00:00Z",
                                           "to": "2024-03-01T05:00:00Z"}).json()
    assert titles(rows) == ["CAL 2h", "CAL 3h", "CAL 4h"]  # 02:00 in, 05:00 (the tie rows) out


def test_offsets_and_naive_values_are_normalised_to_utc(client):
    seed()
    utc = client.get("/api/news", params={"kind": "calendar", "limit": 500,
                                          "from": "2024-03-01T02:00:00Z", "to": "2024-03-01T05:00:00Z"}).json()
    offset = client.get("/api/news", params={"kind": "calendar", "limit": 500,
                                             "from": "2024-03-01T04:00:00+02:00",
                                             "to": "2024-03-01T07:00:00+02:00"}).json()
    naive = client.get("/api/news", params={"kind": "calendar", "limit": 500,
                                            "from": "2024-03-01T02:00:00", "to": "2024-03-01T05:00:00"}).json()
    assert titles(offset) == titles(utc) == titles(naive) == ["CAL 2h", "CAL 3h", "CAL 4h"]


def test_bare_dates_mean_utc_midnight(client):
    seed()
    rows = client.get("/api/news", params={"kind": "calendar", "limit": 500,
                                           "from": "2024-03-01", "to": "2024-03-02"}).json()
    assert len(rows) == 12  # ten hourly rows plus the two tie rows
    later = client.get("/api/news", params={"kind": "calendar", "limit": 500, "from": "2024-03-02"}).json()
    assert later == []


def test_from_alone_is_open_ended_and_drops_undated_rows(client):
    seed()
    rows = client.get("/api/news", params={"kind": "headline", "limit": 500, "from": "2024-01-01"}).json()
    assert titles(rows) == ["HL 2h", "HL 4h"]  # the undated headline has no event_time, so it never matches
    undated = client.get("/api/news", params={"kind": "headline", "limit": 500}).json()
    assert "HL undated" in titles(undated)  # without a range it is still listed


def test_range_without_kind_still_applies_mixed_feed_rule(client):
    seed()
    rows = client.get("/api/news", params={"limit": 500, "from": "2024-03-01T00:00:00Z",
                                           "to": "2024-03-01T03:00:00Z"}).json()
    assert titles(rows) == ["CAL 0h", "CAL 1h", "CAL 2h", "HL 2h"]


def test_paging_inside_a_range_visits_every_row_once(client):
    seed()
    params = {"kind": "calendar", "from": "2024-03-01", "to": "2024-03-02"}
    everything = client.get("/api/news", params={"limit": 500, **params}).json()
    assert len(everything) == 12
    for limit in (1, 2, 5, 7):
        paged = walk(client, limit, **params)
        assert [r["id"] for r in paged] == [r["id"] for r in everything], limit


def test_invalid_range_is_rejected(client):
    assert client.get("/api/news", params={"from": "2024-03-02", "to": "2024-03-01"}).status_code == 422
    assert client.get("/api/news", params={"from": "not-a-date"}).status_code == 422
    assert client.get("/api/calendar", params={"from": "2024-03-02", "to": "2024-03-01"}).status_code == 422


def test_calendar_range_replaces_default_window(client):
    seed()
    rows = client.get("/api/calendar", params={"from": "2024-03-01T00:00:00Z",
                                               "to": "2024-03-01T03:00:00Z"}).json()
    assert titles(rows) == ["CAL 0h", "CAL 1h", "CAL 2h"]
    # days / past_days are ignored once a range is given
    narrow = client.get("/api/calendar", params={"days": 1, "past_days": 0, "from": "2024-03-01T00:00:00Z",
                                                 "to": "2024-03-01T03:00:00Z"}).json()
    assert titles(narrow) == titles(rows)


def test_calendar_default_window_is_unchanged(client):
    seed()
    now = datetime.now(UTC)
    ingest.store_items([RawNews(kind="calendar", source="forexfactory", title="CAL soon", currency="USD",
                                impact="High", event_time=now + timedelta(hours=2))])
    default = client.get("/api/calendar").json()
    assert titles(default) == ["CAL soon"]  # 2024 rows fall outside now -1d .. now +7d
    wider = client.get("/api/calendar", params={"days": 14, "past_days": 14}).json()
    assert "CAL soon" in titles(wider)
