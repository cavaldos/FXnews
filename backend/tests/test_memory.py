"""Long-term memory: only observed facts reach the graph, never a model's prediction / bias."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import delete, select

from app.backtest.models import BtEvent, BtOutcome, BtPrediction
from app.db import init_db, session_scope
from app.memory import facts, store as store_mod, sync as sync_mod
from app.memory.facts import EDGE_INSTANCE, EDGE_REACTION, BiasLeakError, build_record, group_id_for
from app.memory.models import MemoryEdge, MemoryNode, MemorySync
from app.memory.store import HashEmbedder, LLMDisabledError, MemoryStore, NoLLMClient, format_for_prompt, to_graph

T0 = datetime(2026, 11, 6, 13, 30, tzinfo=UTC)
LLM_OPINION = "LLM thinks EURUSD is very bullish, strong buy signal"


def _event(**kw) -> BtEvent:
    base = dict(id=1, dedupe_key="nfp-2026-11", time=T0, kind="calendar", source="forexfactory", currency="USD",
                country="US", title="Non-Farm Payrolls", impact="high", actual="250K", forecast="180K", previous="150K",
                summary="Analysts are bullish on the dollar", raw={"bias": "bullish", "llm": LLM_OPINION})
    base.update(kw)
    return BtEvent(**base)


def _outcome(**kw) -> BtOutcome:
    base = dict(id=1, event_id=1, symbol="EURUSD", interval="5m", candle_source="tradingview", price_at_release=1.08,
                ret_5m=-0.1, ret_15m=-0.2, ret_1h=-0.35, ret_4h=-0.5, ret_1d=-0.02,
                dir_5m="down", dir_15m="down", dir_1h="down", dir_4h="down", dir_1d="flat",
                max_up_1h=0.05, max_down_1h=-0.42, direction="down", direction_horizon="1h", complete=True,
                computed_at=T0 + timedelta(days=1, minutes=5))
    base.update(kw)
    return BtOutcome(**base)


# ── fact builder ─────────────────────────────────────────────────────────

def test_record_holds_release_numbers_and_measured_move():
    rec = build_record(_event(), _outcome())
    assert rec.group_id == "fx_EURUSD"
    assert {n.label for n in rec.nodes} == {"Symbol", "MarketEvent", "Indicator"}
    reaction = next(e for e in rec.edges if e.name == EDGE_REACTION)
    assert "actual 250K, forecast 180K, previous 150K" in reaction.fact
    assert "actual minus forecast +70" in reaction.fact
    assert "1h -0.350% (down)" in reaction.fact and "First-hour range +0.050% / -0.420%" in reaction.fact
    assert reaction.valid_at == T0
    assert reaction.attributes["ret_1h"] == -0.35
    assert any(e.name == EDGE_INSTANCE for e in rec.edges)


def test_record_never_carries_opinions_raw_payload_or_summary():
    rec = build_record(_event(), _outcome())
    texts = [e.fact for e in rec.edges] + [n.summary for n in rec.nodes] + [str(n.attributes) for n in rec.nodes]
    texts += [str(e.attributes) for e in rec.edges]
    blob = " ".join(texts).lower()
    for word in ("bullish", "bearish", "bias", "analysts", "llm", "signal"):
        assert word not in blob


def test_uuids_are_deterministic_so_resync_overwrites():
    a, b = build_record(_event(), _outcome()), build_record(_event(), _outcome(ret_1h=-0.4))
    assert [n.uuid for n in a.nodes] == [n.uuid for n in b.nodes]
    assert [e.uuid for e in a.edges] == [e.uuid for e in b.edges]
    other = build_record(_event(), _outcome(symbol="USDJPY"))
    assert other.edges[0].uuid != a.edges[0].uuid


def test_headline_wording_is_news_not_a_leak():
    ev = _event(kind="headline", title="Fed expected to hold rates, dollar bullish", actual=None, forecast=None,
                previous=None, country=None)
    rec = build_record(ev, _outcome())
    assert len(rec.edges) == 1  # no indicator for one-off headlines
    assert "Fed expected to hold rates" in rec.edges[0].fact


def test_generated_text_with_opinion_word_is_rejected(monkeypatch):
    monkeypatch.setattr(facts, "reaction_text", lambda ev, oc: "EURUSD likely to rise")
    with pytest.raises(BiasLeakError):
        build_record(_event(), _outcome())


def test_memory_code_never_touches_prediction_tables():
    """No identifier / import in app/memory refers to predictions, analyses, signals or the rule bias."""
    import ast

    forbidden = {"BtPrediction", "Analysis", "Signal", "NewsItem", "rule_bias", "advisor", "predict", "app.models"}
    for path in Path(facts.__file__).parent.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node, ast.Name):
                names = [node.id]
            elif isinstance(node, ast.Attribute):
                names = [node.attr]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [a.name for a in node.names]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            hit = forbidden.intersection(names)
            assert not hit, f"{path.name} references {hit}"


# ── store ────────────────────────────────────────────────────────────────

def test_hash_embedder_is_deterministic_and_normalised():
    emb = HashEmbedder(64)
    v1, v2 = emb.embed("Non-Farm Payrolls EURUSD"), emb.embed("Non-Farm Payrolls EURUSD")
    assert v1 == v2 and len(v1) == 64
    assert abs(sum(x * x for x in v1) - 1.0) < 1e-9


async def test_graphiti_gets_an_llm_that_refuses_every_call():
    with pytest.raises(LLMDisabledError):
        await NoLLMClient()._generate_response([])


class FakeGraphiti:
    def __init__(self):
        self.driver, self.embedder = object(), HashEmbedder(32)
        self.search_calls = []

    async def search_(self, query, *, config, group_ids, search_filter):
        self.search_calls.append((query, config, group_ids, search_filter))
        rec = build_record(_event(), _outcome())
        nodes, edges = to_graph([rec])
        return type("R", (), {"edges": [e for e in edges if e.name == EDGE_REACTION]})()

    async def close(self):
        pass


async def test_write_saves_without_llm_and_recall_hides_the_future(monkeypatch):
    saved = {}

    async def fake_bulk(driver, ep_nodes, ep_edges, nodes, edges, embedder):
        saved.update(nodes=nodes, edges=edges)

    monkeypatch.setattr(store_mod, "add_nodes_and_edges_bulk", fake_bulk)
    graphiti = FakeGraphiti()
    async with MemoryStore(graphiti=graphiti) as store:
        n = await store.write([build_record(_event(), _outcome()), build_record(_event(), _outcome())])
        assert n == 2 and len(saved["edges"]) == 2  # duplicates merged by uuid
        assert all(e.invalid_at is None for e in saved["edges"])  # history is never invalidated

        as_of = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
        got = await store.recall("Non-Farm Payrolls", "eurusd", as_of=as_of, limit=5)
    _, config, groups, flt = graphiti.search_calls[0]
    assert groups == [group_id_for("EURUSD")] and config.limit == 5
    assert flt.edge_types == [EDGE_REACTION]
    assert flt.valid_at[0][0].date == as_of - timedelta(days=1)
    block = format_for_prompt(got)
    assert block.startswith("Past market reactions") and "EURUSD after Non-Farm Payrolls" in block


# ── sync / SQLite copy / API / export ────────────────────────────────────

class CaptureStore:
    def __init__(self, fail: bool = False):
        self.records, self.fail = [], fail

    async def write(self, records):
        if self.fail:
            raise ConnectionError("neo4j down")
        self.records.extend(records)
        return sum(len(r.edges) for r in records)


@pytest.fixture
def db():
    init_db()
    with session_scope() as s:
        for table in (MemorySync, MemoryEdge, MemoryNode, BtPrediction, BtOutcome, BtEvent):
            s.execute(delete(table))
    yield


def _seed():
    with session_scope() as s:
        s.add(_event(raw={}))
        s.add(_event(id=2, dedupe_key="hl-1", kind="headline", title="Dollar slips after data", time=T0 + timedelta(hours=2),
                     actual=None, forecast=None, previous=None, raw={}))
        s.add(_outcome())
        s.add(_outcome(id=2, event_id=2, ret_1h=0.2, dir_1h="up", direction="up"))
        s.add(_outcome(id=3, symbol="USDJPY", complete=False))  # not final yet
        s.add(BtPrediction(event_id=1, symbol="EURUSD", model="x:y", direction="up", confidence=90,
                           reasoning=LLM_OPINION))


async def test_sync_writes_sqlite_and_graph_once_and_ignores_predictions(db):
    _seed()
    store = CaptureStore()
    r1 = await sync_mod.sync(store)
    assert (r1.facts, r1.edges, r1.graph_edges) == (2, 3, 3)
    assert {r.group_id for r in store.records} == {"fx_EURUSD"}
    with session_scope() as s:
        facts_text = " ".join(e.fact for e in s.execute(select(MemoryEdge)).scalars())
        assert s.execute(select(MemoryNode)).scalars().all()  # nodes stored
    assert "bullish" not in facts_text.lower() and "Non-Farm Payrolls" in facts_text

    r2 = await sync_mod.sync(store)
    assert r2.facts == 0  # idempotent

    with session_scope() as s:  # outcome recomputed -> re-sent, still one row per uuid
        oc = s.execute(select(BtOutcome).where(BtOutcome.id == 1)).scalar_one()
        oc.computed_at = T0 + timedelta(days=3)
    r3 = await sync_mod.sync(None)
    assert r3.facts == 1
    with session_scope() as s:
        assert len(s.execute(select(MemoryEdge)).scalars().all()) == 3


async def test_graph_failure_does_not_stop_sqlite(db):
    _seed()
    r = await sync_mod.sync(CaptureStore(fail=True))
    assert r.facts == 2 and r.graph_edges == 0 and "neo4j down" in r.graph_error


async def test_records_rebuilt_from_sqlite_match_the_original(db):
    _seed()
    await sync_mod.sync(None)
    with session_scope() as s:
        recs = sync_mod.records_from_db(s)
    assert [r.group_id for r in recs] == ["fx_EURUSD"]
    original = build_record(_event(raw={}), _outcome())
    rebuilt = {e.uuid: e for e in recs[0].edges}
    for e in original.edges:
        assert rebuilt[e.uuid].fact == e.fact


async def test_api_overview_graph_and_sync(db):
    from fastapi.testclient import TestClient

    from app.main import app

    _seed()
    client = TestClient(app)
    assert client.get("/api/memory/overview").json()["pending"] == 2
    out = client.post("/api/memory/sync", json={}).json()
    assert out["facts"] == 2 and out["graph_error"] is None

    ov = client.get("/api/memory/overview").json()
    assert ov["pending"] == 0
    assert ov["symbols"][0]["symbol"] == "EURUSD"
    assert (ov["symbols"][0]["up"], ov["symbols"][0]["down"]) == (1, 1)

    g = client.get("/api/memory/graph", params={"symbol": "EURUSD"}).json()
    labels = sorted(n["label"] for n in g["nodes"])
    assert labels == ["Indicator", "MarketEvent", "MarketEvent", "Symbol"]
    assert {e["type"] for e in g["edges"]} == {"MARKET_REACTION", "INSTANCE_OF"}
    assert g["total"] == 2 and not g["truncated"]

    only_hl = client.get("/api/memory/graph", params={"symbol": "EURUSD", "kind": "headline"}).json()
    assert [n["label"] for n in only_hl["nodes"] if n["label"] != "Symbol"] == ["MarketEvent"]
    q = client.get("/api/memory/graph", params={"symbol": "EURUSD", "q": "payrolls", "limit": 1}).json()
    assert q["total"] == 1


async def test_memory_tables_round_trip_through_json_export(db, tmp_path):
    from app.services import datastore

    _seed()
    await sync_mod.sync(None)
    dump = tmp_path / "mem.json.gz"
    with dump.open("wb") as fh:
        for chunk in datastore.iter_json_export(["memory_nodes", "memory_edges"]):
            fh.write(chunk)
    with session_scope() as s:
        s.execute(delete(MemoryEdge))
        s.execute(delete(MemoryNode))
    datastore.import_file(dump, mode="merge")
    with session_scope() as s:
        assert len(s.execute(select(MemoryEdge)).scalars().all()) == 3
        assert len(s.execute(select(MemoryNode)).scalars().all()) == 4


# ── recall for the advisor prompt ────────────────────────────────────────


def test_recall_for_advisor_is_facts_only_and_respects_as_of():
    from app.memory.recall import memory_block, recall

    init_db()
    with session_scope() as db:
        db.execute(delete(MemoryEdge))
        rec = build_record(_event(), _outcome())
        for e in rec.edges:
            db.add(MemoryEdge(uuid=e.uuid, group_id=rec.group_id, name=e.name, source_uuid=e.source_uuid,
                              target_uuid=e.target_uuid, fact=e.fact, valid_at=e.valid_at, attributes=e.attributes))
        db.flush()
        assert [e.name for e in recall(db, "US Non-Farm Payrolls today", "EURUSD")] == [EDGE_REACTION]
        assert recall(db, "Non-Farm Payrolls", "USDJPY") == []  # other symbol
        assert recall(db, "ECB speech", "EURUSD") == []  # nothing similar
        assert recall(db, "Non-Farm Payrolls", "EURUSD", as_of=T0 + timedelta(hours=1)) == []  # 1d not elapsed yet
        lines = memory_block(db, "Non-Farm Payrolls", ["EURUSD", "USDJPY"])
        assert len(lines) == 1 and "Observed move" in lines[0] and LLM_OPINION not in lines[0]
        db.execute(delete(MemoryEdge))


def test_tampered_rows_with_opinion_never_reach_prompt_or_graph():
    from app.memory.recall import recall

    init_db()
    with session_scope() as db:
        db.execute(delete(MemoryEdge)); db.execute(delete(MemoryNode))
        rec = build_record(_event(), _outcome())
        for n in rec.nodes:
            db.add(MemoryNode(uuid=n.uuid, group_id=rec.group_id, label=n.label, name=n.name, summary=n.summary,
                              attributes=n.attributes))
        for e in rec.edges:  # an imported file smuggles a model's call into the fact text
            db.add(MemoryEdge(uuid=e.uuid, group_id=rec.group_id, name=e.name, source_uuid=e.source_uuid,
                              target_uuid=e.target_uuid, fact=e.fact + " " + LLM_OPINION, valid_at=e.valid_at,
                              attributes=e.attributes))
        db.flush()
        assert recall(db, "Non-Farm Payrolls", "EURUSD") == []
        reaction_edges = [e for r in sync_mod.records_from_db(db) for e in r.edges if e.name == EDGE_REACTION]
        assert reaction_edges == []
        db.execute(delete(MemoryEdge)); db.execute(delete(MemoryNode))
