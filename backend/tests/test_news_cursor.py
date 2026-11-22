"""GET /api/news keyset pagination on (event_time, id)."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import ingest
from app.sources.base import RawNews
from tests.support import wipe_db


@pytest.fixture
def client():
    wipe_db()
    yield TestClient(app)
    wipe_db()


def seed():
    base = (datetime.now(UTC) - timedelta(hours=6)).replace(microsecond=0)
    tie = base + timedelta(hours=3)  # many rows share this exact timestamp
    items = []
    for i in range(5):
        items.append(RawNews(kind="calendar", source="forexfactory", title=f"USD event {i}", currency="USD",
                             impact="High", event_time=tie))
    for i in range(3):
        items.append(RawNews(kind="headline", source="fxstreet", title=f"Tie headline {i}", url=f"https://x/tie{i}",
                             event_time=tie))
    for i in range(4):
        items.append(RawNews(kind="headline", source="fxstreet", title=f"Older headline {i}", url=f"https://x/old{i}",
                             event_time=base + timedelta(minutes=i)))
    for i in range(3):  # no timestamp at all: sorted last
        items.append(RawNews(kind="headline", source="fxstreet", title=f"Undated {i}", url=f"https://x/nodate{i}",
                             event_time=None))
    ingest.store_items(items)
    return len(items)


def cursor_of(row):
    if row["event_time"] is None:
        return {"before_id": row["id"]}
    return {"before": row["event_time"], "before_id": row["id"]}


def walk(client, limit, **filters):
    seen, params = [], {}
    for _ in range(100):
        page = client.get("/api/news", params={"limit": limit, **filters, **params}).json()
        if not page:
            return seen
        seen += page
        params = cursor_of(page[-1])
    raise AssertionError("pagination did not terminate")


@pytest.mark.parametrize("limit", [1, 2, 3, 5, 7, 100])
def test_paging_with_event_time_and_id_visits_every_row_exactly_once(client, limit):
    total = seed()
    everything = client.get("/api/news", params={"limit": 500}).json()
    assert len(everything) == total
    paged = walk(client, limit)
    assert [r["id"] for r in paged] == [r["id"] for r in everything]  # same rows, same order, no gaps/dupes


def test_paging_works_with_filters(client):
    seed()
    expected = client.get("/api/news", params={"limit": 500, "kind": "calendar"}).json()
    assert len(expected) == 5
    assert [r["id"] for r in walk(client, 2, kind="calendar")] == [r["id"] for r in expected]
    headlines = client.get("/api/news", params={"limit": 500, "kind": "headline"}).json()
    assert [r["id"] for r in walk(client, 4, kind="headline")] == [r["id"] for r in headlines]


def test_undated_rows_are_last_and_reachable_through_before_id_alone(client):
    seed()
    allrows = client.get("/api/news", params={"limit": 500}).json()
    assert [r["event_time"] is None for r in allrows] == [False] * 12 + [True] * 3
    last_dated = allrows[11]
    page = client.get("/api/news", params={"limit": 50, **cursor_of(last_dated)}).json()
    assert [r["id"] for r in page] == [r["id"] for r in allrows[12:]]  # the undated tail follows the dated rows
    tail = client.get("/api/news", params={"limit": 50, "before_id": allrows[12]["id"]}).json()
    assert [r["id"] for r in tail] == [r["id"] for r in allrows[13:]]


def test_before_alone_keeps_its_old_strict_meaning(client):
    seed()
    allrows = client.get("/api/news", params={"limit": 500}).json()
    tie_time = allrows[0]["event_time"]
    page = client.get("/api/news", params={"limit": 500, "before": tie_time}).json()
    assert page and all(r["event_time"] is not None and r["event_time"] < tie_time for r in page)


def test_before_id_must_be_positive(client):
    assert client.get("/api/news", params={"before_id": 0}).status_code == 422
