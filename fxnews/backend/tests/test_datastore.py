"""Data import / export: SQLite + JSON.gz round trips, merge (id remap, dedupe), replace, backups, guards."""

import gzip
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from app.backtest.models import BtEvent, BtOutcome, BtPrediction
from app.config import get_settings
from app.db import init_db, session_scope
from app.main import app
from app.models import Analysis, NewsItem, Signal
from app.services import datastore
from app.services.datastore import DataError

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    init_db()
    wipe_all()
    for p in datastore.backup_dir().glob("*"):
        p.unlink()
    monkeypatch.setattr(get_settings(), "data_backup_keep", 10)
    yield
    wipe_all()


def wipe_all() -> None:
    with session_scope() as s:
        for m in (BtPrediction, BtOutcome, BtEvent, Signal, Analysis, NewsItem):
            s.execute(delete(m))


def seed(prefix: str = "a", offset: int = 0) -> None:
    """2 news items, 1 analysis (linked) + signal, 1 bt event with 1 outcome and 2 predictions."""
    with session_scope() as s:
        n1 = NewsItem(dedupe_key=f"{prefix}-n1", kind="headline", source="fxstreet", title="Fed holds", currency="USD",
                      event_time=T0 + timedelta(days=offset), raw={"x": [1, 2]})
        n2 = NewsItem(dedupe_key=f"{prefix}-n2", kind="calendar", source="ff", title="CPI", currency="USD",
                      event_time=T0 + timedelta(days=10 + offset), updated_at=T0)
        s.add_all([n1, n2])
        s.flush()
        an = Analysis(kind="headline", news_id=n1.id, model="m1", summary="ok", trades=[{"symbol": "EURUSD"}],
                      created_at=T0 + timedelta(days=offset))
        s.add(an)
        s.flush()
        n1.analysis_id = an.id
        s.add(Signal(analysis_id=an.id, news_id=n1.id, symbol="EURUSD", bias="bullish", confidence=70, source="llm",
                     created_at=T0 + timedelta(days=offset)))
        ev = BtEvent(dedupe_key=f"{prefix}-e1", time=T0 + timedelta(days=offset), kind="calendar", source="tv",
                     title="NFP", impact="high", raw={"k": 1}, updated_at=T0)
        s.add(ev)
        s.flush()
        s.add(BtOutcome(event_id=ev.id, symbol="EURUSD", interval="5m", price_at_release=1.1, ret_1h=0.2, computed_at=T0))
        s.add_all([BtPrediction(event_id=ev.id, symbol="EURUSD", model=m, direction="up", confidence=60, created_at=T0)
                   for m in ("p:a", "p:b")])


def counts() -> dict[str, int]:
    with session_scope() as s:
        return {m.__tablename__: s.scalar(select(func.count()).select_from(m))
                for m in (NewsItem, Analysis, Signal, BtEvent, BtOutcome, BtPrediction)}


def snapshot() -> dict:
    """Natural-key view of the data (ids excluded) to compare states."""
    with session_scope() as s:
        news = {n.dedupe_key: (n.title, n.raw, n.event_time) for n in s.scalars(select(NewsItem))}
        ev = {e.id: e.dedupe_key for e in s.scalars(select(BtEvent))}
        nk = {n.id: n.dedupe_key for n in s.scalars(select(NewsItem))}
        an = {(a.kind, nk.get(a.news_id), a.model, a.created_at, tuple(map(str, a.trades))) for a in s.scalars(select(Analysis))}
        ak = {a.id: nk.get(a.news_id) for a in s.scalars(select(Analysis))}
        sig = {(ak.get(g.analysis_id), nk.get(g.news_id), g.symbol, g.confidence) for g in s.scalars(select(Signal))}
        out = {(ev[o.event_id], o.symbol, o.ret_1h) for o in s.scalars(select(BtOutcome))}
        pred = {(ev[p.event_id], p.symbol, p.model) for p in s.scalars(select(BtPrediction))}
        links = {n.dedupe_key: ak.get(n.analysis_id) for n in s.scalars(select(NewsItem)) if n.analysis_id}
    return {"news": news, "an": an, "sig": sig, "out": out, "pred": pred, "links": links}


def export_json(client, query: str = "") -> bytes:
    r = client.get(f"/api/data/export?format=json{query}")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"] and ".json.gz" in r.headers["content-disposition"]
    return r.content


def post_import(client, body: bytes, query: str = "", **kw):
    return client.post(f"/api/data/import?{query}", content=body, headers={"Content-Type": "application/octet-stream"}, **kw)


# ── export ───────────────────────────────────────────────────────────────


def test_tables_overview(client):
    seed()
    r = client.get("/api/data/tables")
    assert r.status_code == 200
    body = r.json()
    rows = {t["name"]: t["rows"] for t in body["tables"]}
    assert rows == {"news_items": 2, "analyses": 1, "signals": 1, "bt_events": 1, "bt_outcomes": 1, "bt_predictions": 2}
    assert all(t["label"] for t in body["tables"])
    assert body["db_path"].endswith("test.db") and body["size_bytes"] > 0


def test_sqlite_export_roundtrip(client):
    seed()
    r = client.get("/api/data/export?format=sqlite")
    assert r.status_code == 200
    assert r.headers["content-disposition"].startswith("attachment")
    assert r.content.startswith(b"SQLite format 3\x00")
    import re
    assert re.search(r"fxnews-\d{8}-\d{6}\.db", r.headers["content-disposition"])
    before = snapshot()
    wipe_all()
    assert counts()["news_items"] == 0
    res = post_import(client, r.content, "mode=replace").json()
    assert res["format"] == "sqlite" and res["tables"]["news_items"]["inserted"] == 2
    assert snapshot() == before


def test_json_export_shape_and_filter(client):
    seed()
    raw = export_json(client)
    doc = json.loads(gzip.decompress(raw))
    assert doc["format"] == "fxnews-export" and doc["version"] == 1 and doc["exported_at"]
    assert set(doc["schema"]) == set(doc["tables"]) == {"news_items", "analyses", "signals", "bt_events", "bt_outcomes", "bt_predictions"}
    assert "dedupe_key" in doc["schema"]["news_items"]
    n1 = next(n for n in doc["tables"]["news_items"] if n["dedupe_key"] == "a-n1")
    assert n1["raw"] == {"x": [1, 2]} and n1["event_time"].startswith("2026-11-01T12:00:00")
    # table selection + date range (n2 is 10 days later, children follow the event time)
    doc = json.loads(gzip.decompress(export_json(client, "&tables=news_items,bt_outcomes&from=2026-11-01&to=2026-11-02")))
    assert set(doc["tables"]) == {"news_items", "bt_outcomes"}
    assert [n["dedupe_key"] for n in doc["tables"]["news_items"]] == ["a-n1"]
    assert len(doc["tables"]["bt_outcomes"]) == 1
    doc = json.loads(gzip.decompress(export_json(client, "&tables=bt_outcomes&from=2026-12-01")))
    assert doc["tables"]["bt_outcomes"] == []
    assert client.get("/api/data/export?format=json&tables=nope").status_code == 422
    assert client.get("/api/data/export?format=json&from=garbage").status_code == 422


def test_json_roundtrip_replace(client):
    seed()
    before = snapshot()
    raw = export_json(client)
    wipe_all()
    seed("zzz", 5)  # unrelated data that replace must wipe
    res = post_import(client, raw, "mode=replace").json()
    assert res["mode"] == "replace" and res["backup"].endswith(".db")
    assert counts() == {"news_items": 2, "analyses": 1, "signals": 1, "bt_events": 1, "bt_outcomes": 1, "bt_predictions": 2}
    assert snapshot() == before


def test_plain_json_is_accepted(client):
    seed()
    plain = gzip.decompress(export_json(client))
    wipe_all()
    res = post_import(client, plain, "mode=merge").json()
    assert res["format"] == "json" and res["tables"]["news_items"]["inserted"] == 2


# ── merge ────────────────────────────────────────────────────────────────


def test_merge_remaps_ids_and_is_idempotent(client):
    seed()
    before = snapshot()
    raw = export_json(client)
    wipe_all()
    # filler rows so the imported rows get different ids than in the file
    with session_scope() as s:
        for i in range(5):
            s.add(NewsItem(dedupe_key=f"filler{i}", kind="headline", source="x", title="f"))
            s.add(BtEvent(dedupe_key=f"fe{i}", time=T0, kind="headline", source="x", title="f", impact="low"))
    res = post_import(client, raw).json()
    assert res["mode"] == "merge"
    assert res["tables"]["news_items"] == {"inserted": 2, "updated": 0, "skipped": 0}
    assert res["tables"]["bt_predictions"]["inserted"] == 2
    after = snapshot()
    for k in ("an", "sig", "out", "pred", "links"):
        assert after[k] == before[k], k
    assert counts()["news_items"] == 7
    # importing the same file again changes nothing
    res2 = post_import(client, raw).json()
    for name, c in res2["tables"].items():
        assert c["inserted"] == 0 and c["updated"] == 0, name
    assert counts() == {"news_items": 7, "analyses": 1, "signals": 1, "bt_events": 6, "bt_outcomes": 1, "bt_predictions": 2}
    assert snapshot() == after


def test_merge_newer_row_wins_older_is_skipped(client):
    seed()
    raw = export_json(client)
    with session_scope() as s:
        s.execute(NewsItem.__table__.update().where(NewsItem.dedupe_key == "a-n2").values(title="local newer", updated_at=T0 + timedelta(days=1)))
        s.execute(BtEvent.__table__.update().where(BtEvent.dedupe_key == "a-e1").values(title="local older", updated_at=T0 - timedelta(days=1)))
    res = post_import(client, raw).json()
    assert res["tables"]["news_items"]["updated"] == 0
    assert res["tables"]["bt_events"]["updated"] == 1
    with session_scope() as s:
        assert s.scalar(select(NewsItem.title).where(NewsItem.dedupe_key == "a-n2")) == "local newer"
        assert s.scalar(select(BtEvent.title).where(BtEvent.dedupe_key == "a-e1")) == "NFP"
    assert counts()["bt_events"] == 1


def test_merge_selected_table_only_and_orphans(client):
    seed()
    raw = export_json(client)
    wipe_all()
    # bt_outcomes alone: its events are not present -> rows are skipped with a warning, no events are created
    res = post_import(client, raw, "tables=bt_outcomes").json()
    assert set(res["tables"]) == {"bt_outcomes"} and res["tables"]["bt_outcomes"]["skipped"] == 1
    assert any("bt_events" in w for w in res["warnings"])
    assert counts()["bt_events"] == 0 and counts()["bt_outcomes"] == 0
    # events first, then outcomes alone: they attach to the existing events
    post_import(client, raw, "tables=bt_events")
    res = post_import(client, raw, "tables=bt_outcomes").json()
    assert res["tables"]["bt_outcomes"]["inserted"] == 1 and counts()["bt_outcomes"] == 1


def test_merge_sqlite_upload_remaps(client):
    seed()
    before = snapshot()
    db = client.get("/api/data/export?format=sqlite").content
    wipe_all()
    with session_scope() as s:
        s.add(NewsItem(dedupe_key="filler", kind="headline", source="x", title="f"))
    res = post_import(client, db).json()
    assert res["format"] == "sqlite" and res["tables"]["analyses"]["inserted"] == 1
    after = snapshot()
    for k in ("an", "sig", "out", "pred", "links"):
        assert after[k] == before[k], k


def test_replace_selected_tables_only(client):
    seed()
    raw = export_json(client)
    with session_scope() as s:
        s.add(NewsItem(dedupe_key="extra", kind="headline", source="x", title="extra"))
        s.add(BtEvent(dedupe_key="extra-e", time=T0, kind="headline", source="x", title="extra", impact="low"))
    res = post_import(client, raw, "mode=replace&tables=news_items").json()
    # replacing news_items pulls in the tables that reference it
    assert {"news_items", "analyses", "signals"} <= set(res["tables"])
    assert res["tables"]["news_items"]["deleted"] == 3
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(NewsItem)) == 2
        assert s.scalar(select(func.count()).select_from(BtEvent)) == 2  # untouched
    assert any("analyses" in w for w in res["warnings"])


# ── validation / rollback / backups ──────────────────────────────────────


def test_validation_errors(client):
    seed()
    assert post_import(client, b"").status_code == 422
    assert post_import(client, b"hello world").status_code == 422
    assert post_import(client, b'{"a": 1}').status_code == 422  # not an fxnews export
    assert post_import(client, b"\x1f\x8bnotgzip").status_code == 422
    bad_mode = post_import(client, b"{}", "mode=wipe")
    assert bad_mode.status_code == 422
    doc = {"format": "fxnews-export", "version": 1, "tables": {"news_items": [{"title": "x"}]}}
    r = post_import(client, json.dumps(doc).encode())
    assert r.status_code == 422 and "dedupe_key" in r.json()["detail"]
    assert post_import(client, json.dumps(doc).encode(), "tables=bogus").status_code == 422
    doc["version"] = 99
    assert post_import(client, json.dumps(doc).encode()).status_code == 422
    assert counts()["news_items"] == 2  # nothing changed
    assert list(datastore.backup_dir().glob("*.db")) == []  # rejected before any backup


def test_unknown_tables_and_columns_are_warnings(client):
    doc = {"format": "fxnews-export", "version": 1, "schema": {},
           "tables": {"news_items": [{"dedupe_key": "u1", "kind": "headline", "source": "s", "title": "t", "bogus_col": 1}],
                      "mystery": [{"a": 1}]}}
    res = post_import(client, json.dumps(doc).encode()).json()
    assert res["tables"]["news_items"]["inserted"] == 1
    joined = " ".join(res["warnings"])
    assert "mystery" in joined and "bogus_col" in joined


def test_import_rolls_back_on_error(client, monkeypatch):
    seed()
    raw = export_json(client)
    wipe_all()
    real = datastore._prepare_row
    calls = {"n": 0}

    def boom(spec, *a, **kw):
        if spec.name == "bt_outcomes":
            calls["n"] += 1
            raise RuntimeError("kaboom")
        return real(spec, *a, **kw)

    monkeypatch.setattr(datastore, "_prepare_row", boom)
    r = post_import(client, raw)
    assert r.status_code == 500 and "rolled back" in r.json()["detail"]
    assert calls["n"] == 1
    assert counts() == dict.fromkeys(counts(), 0)  # news/analyses inserted before the failure were rolled back
    assert len(list(datastore.backup_dir().glob("*.db"))) == 1  # the pre-import backup is kept


def test_auto_backup_created_listed_and_pruned(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "data_backup_keep", 2)
    seed()
    raw = export_json(client)
    names = []
    for _ in range(3):
        res = post_import(client, raw).json()
        names.append(res["backup"])
    assert len(set(names)) == 3
    listed = client.get("/api/data/backups").json()
    assert len(listed) == 2  # pruned to the newest 2
    assert {b["name"] for b in listed} == set(names[1:])
    assert all(b["size_bytes"] > 0 and b["created_at"] for b in listed)
    # the backup is a valid, downloadable SQLite file containing the data
    r = client.get(f"/api/data/backups/{names[-1]}")
    assert r.status_code == 200 and r.content.startswith(b"SQLite format 3\x00")


def test_restore_backup_while_running(client):
    seed()
    before = snapshot()
    b = client.post("/api/data/backups").json()
    assert b["name"].endswith(".db")
    with session_scope() as s:
        s.add(NewsItem(dedupe_key="after-backup", kind="headline", source="x", title="later"))
        s.execute(delete(Signal))
    assert counts()["news_items"] == 3
    r = client.post(f"/api/data/backups/{b['name']}/restore")
    assert r.status_code == 200
    body = r.json()
    assert body["restored"] == b["name"] and body["backup"] != b["name"]
    assert snapshot() == before
    assert counts()["news_items"] == 2
    # the state before the restore was itself backed up
    assert body["backup"] in {x["name"] for x in client.get("/api/data/backups").json()}
    # the app keeps working on the restored DB
    with session_scope() as s:
        s.add(NewsItem(dedupe_key="post-restore", kind="headline", source="x", title="again"))
    assert counts()["news_items"] == 3


def test_path_traversal_rejected(client):
    for bad in ("../test.db", "..", "a/b.db", "x.txt", ".hidden.db", "..%2F..%2Ftest.db", "%2e%2e%2ftest.db", "a\\b.db"):
        assert client.get(f"/api/data/backups/{bad}").status_code in (400, 404), bad
        assert client.post(f"/api/data/backups/{bad}/restore").status_code in (400, 404), bad
    for bad in ("../test.db", "../../etc/passwd", "/etc/passwd", "a/../../b.db", "..db", "x.db/"):
        with pytest.raises(DataError) as e:
            datastore.backup_path(bad)
        assert e.value.status == 400
    with pytest.raises(DataError) as e:
        datastore.backup_path("missing.db")
    assert e.value.status == 404
    # a real file outside the backups dir is not reachable by name
    assert (datastore.db_path()).is_file()
    assert client.get(f"/api/data/backups/{datastore.db_path().name}").status_code == 404


def test_size_limit(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "data_import_max_mb", 1)
    r = post_import(client, b"{" + b" " * (1024 * 1024 + 10))
    assert r.status_code == 413


# ── CSRF guard ───────────────────────────────────────────────────────────


def test_csrf_guard_allows_same_origin_and_blocks_cross_site(client):
    seed()
    raw = export_json(client)
    headers = {"Content-Type": "application/octet-stream"}
    ok = client.post("/api/data/import?mode=merge", content=raw,
                     headers={**headers, "Host": "localhost:5173", "Origin": "http://localhost:5173", "Sec-Fetch-Site": "same-origin"})
    assert ok.status_code == 200
    ok = client.post("/api/data/import?mode=merge", content=raw,
                     headers={**headers, "Host": "localhost:8000", "Origin": "http://localhost:5173", "Sec-Fetch-Site": "same-site"})
    assert ok.status_code == 200
    evil = client.post("/api/data/import?mode=replace", content=raw, headers={**headers, "Origin": "http://evil.example"})
    assert evil.status_code == 403
    assert client.post("/api/data/backups", headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.post("/api/data/backups/x.db/restore", headers={"Origin": "http://evil.example"}).status_code == 403
    assert counts()["news_items"] == 2


def test_cli_export_import(tmp_path, capsys):
    seed()
    out = tmp_path / "e.json.gz"
    assert datastore._cli(["export", "--format", "json", "--tables", "news_items", "-o", str(out)]) == 0
    assert json.loads(gzip.decompress(out.read_bytes()))["tables"]["news_items"]
    db = tmp_path / "e.db"
    assert datastore._cli(["export", "-o", str(db)]) == 0
    assert db.read_bytes().startswith(b"SQLite format 3\x00")
    assert datastore._cli(["import", str(out)]) == 0
    assert datastore._cli(["backup", "--label", "cli"]) == 0
    assert datastore._cli(["import", str(tmp_path / "nope.bin")]) != 0
    assert sqlite3.connect(str(db)).execute("select count(*) from news_items").fetchone()[0] == 2
