"""Graphiti-backed store: deterministic writes + hybrid (BM25 + embedding) recall. No LLM anywhere.

Graphiti's usual ``add_episode`` / ``add_triplet`` run an LLM to extract, dedupe and *invalidate* facts. That is
exactly what must not happen here: an LLM would inject its own reading, and two opposite reactions to the same
indicator on different dates are both true history (neither invalidates the other). So nodes and edges are built by
``facts.py`` and saved with Graphiti's bulk writer; the LLM client handed to Graphiti refuses every call.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

os.environ.setdefault("GRAPHITI_TELEMETRY_ENABLED", "false")  # before graphiti_core is imported: no phoning home

from graphiti_core import Graphiti  # noqa: E402
from graphiti_core.cross_encoder.client import CrossEncoderClient  # noqa: E402
from graphiti_core.edges import EntityEdge  # noqa: E402
from graphiti_core.embedder.client import EmbedderClient  # noqa: E402
from graphiti_core.llm_client.client import LLMClient  # noqa: E402
from graphiti_core.llm_client.config import LLMConfig  # noqa: E402
from graphiti_core.nodes import EntityNode  # noqa: E402
from graphiti_core.search.search_config_recipes import EDGE_HYBRID_SEARCH_RRF  # noqa: E402
from graphiti_core.search.search_filters import ComparisonOperator, DateFilter, SearchFilters  # noqa: E402
from graphiti_core.utils.bulk_utils import add_nodes_and_edges_bulk  # noqa: E402

from app.backtest.models import HORIZON_MINUTES  # noqa: E402
from app.config import Settings, get_settings  # noqa: E402
from app.memory.facts import EDGE_REACTION, MemoryRecord, group_id_for  # noqa: E402

# A reaction fact is only fully known once its longest horizon has elapsed.
KNOWLEDGE_DELAY = timedelta(minutes=HORIZON_MINUTES["1d"])


class LLMDisabledError(RuntimeError):
    pass


class NoLLMClient(LLMClient):
    """Handed to Graphiti so that no code path can silently ask a model to rewrite / judge memory."""

    def __init__(self) -> None:
        super().__init__(LLMConfig(api_key="disabled", model="disabled"), cache=False)

    async def _generate_response(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise LLMDisabledError("fxnews memory is written deterministically; LLM calls are disabled")


class NoReranker(CrossEncoderClient):
    async def rank(self, query: str, passages: list[str]) -> list[tuple[str, float]]:
        raise LLMDisabledError("cross-encoder reranking is disabled; use the RRF recipe")


_TOKEN = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?")


class HashEmbedder(EmbedderClient):
    """Local, deterministic embedding: signed feature hashing of word unigrams + bigrams, L2-normalised.

    Weak semantics compared to a real model, but free, offline and stable; the BM25 half of the hybrid search carries
    exact names (symbols, indicators). Switch to ``MEMORY_EMBEDDER=openai`` for real semantic recall.
    """

    def __init__(self, dim: int = 384) -> None:
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        tokens = _TOKEN.findall(text.lower())
        features = tokens + [f"{a} {b}" for a, b in zip(tokens, tokens[1:])]
        vec = [0.0] * self.dim
        for feat in features:
            digest = hashlib.blake2b(feat.encode(), digest_size=8).digest()
            idx = int.from_bytes(digest[:4], "little") % self.dim
            vec[idx] += 1.0 if digest[4] & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    async def create(self, input_data: str | list[str] | Iterable[int] | Iterable[Iterable[int]]) -> list[float]:
        if isinstance(input_data, str):
            return self.embed(input_data)
        items = list(input_data)  # type: ignore[arg-type]
        return self.embed(" ".join(str(i) for i in items))

    async def create_batch(self, input_data_list: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in input_data_list]


def make_embedder(settings: Settings) -> EmbedderClient:
    if settings.memory_embedder == "hash":
        return HashEmbedder(settings.memory_embedding_dim)
    if settings.memory_embedder == "openai":
        from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig

        return OpenAIEmbedder(OpenAIEmbedderConfig(
            embedding_model=settings.memory_embedding_model, embedding_dim=settings.memory_embedding_dim,
            api_key=settings.memory_embedding_api_key or None, base_url=settings.memory_embedding_base_url or None,
        ))
    raise ValueError(f"unknown MEMORY_EMBEDDER {settings.memory_embedder!r} (hash | openai)")


def make_driver(settings: Settings):
    if settings.memory_backend == "neo4j":
        from graphiti_core.driver.neo4j_driver import Neo4jDriver

        return Neo4jDriver(settings.memory_neo4j_uri, settings.memory_neo4j_user, settings.memory_neo4j_password)
    if settings.memory_backend == "falkordb":
        from graphiti_core.driver.falkordb_driver import FalkorDriver  # needs `pip install graphiti-core[falkordb]`

        return FalkorDriver(host=settings.memory_falkordb_host, port=settings.memory_falkordb_port)
    raise ValueError(f"unknown MEMORY_BACKEND {settings.memory_backend!r} (neo4j | falkordb)")


@dataclass
class RecalledFact:
    symbol: str
    fact: str
    valid_at: datetime | None
    attributes: dict[str, Any]


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def to_graph(records: list[MemoryRecord]) -> tuple[list[EntityNode], list[EntityEdge]]:
    now = datetime.now(UTC)
    nodes: dict[str, EntityNode] = {}
    edges: dict[str, EntityEdge] = {}
    for rec in records:
        for n in rec.nodes:
            nodes[n.uuid] = EntityNode(uuid=n.uuid, name=n.name, group_id=rec.group_id, labels=[n.label],
                                       summary=n.summary, attributes=dict(n.attributes), created_at=now)
        for e in rec.edges:
            edges[e.uuid] = EntityEdge(uuid=e.uuid, group_id=rec.group_id, source_node_uuid=e.source_uuid,
                                       target_node_uuid=e.target_uuid, created_at=now, name=e.name, fact=e.fact,
                                       valid_at=e.valid_at, invalid_at=None, episodes=[],
                                       attributes=dict(e.attributes))
    return list(nodes.values()), list(edges.values())


class MemoryStore:
    def __init__(self, settings: Settings | None = None, *, graphiti: Graphiti | None = None) -> None:
        self.settings = settings or get_settings()
        self.graphiti = graphiti or Graphiti(
            graph_driver=make_driver(self.settings), llm_client=NoLLMClient(),
            embedder=make_embedder(self.settings), cross_encoder=NoReranker(),
        )

    async def __aenter__(self) -> MemoryStore:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self.graphiti.close()

    async def init_schema(self) -> None:
        await self.graphiti.build_indices_and_constraints()

    async def write(self, records: list[MemoryRecord]) -> int:
        """Upsert records (MERGE on deterministic uuids). Returns the number of edges written."""
        if not records:
            return 0
        nodes, edges = to_graph(records)
        await add_nodes_and_edges_bulk(self.graphiti.driver, [], [], nodes, edges, self.graphiti.embedder)
        return len(edges)

    async def recall(self, query: str, symbol: str, *, as_of: datetime | None = None, limit: int = 8) -> list[RecalledFact]:
        """Past market reactions of ``symbol`` relevant to ``query`` (e.g. the title of the release being analysed).

        ``as_of``: only facts whose 1d horizon had already elapsed at that moment -- use the release time when
        building a prompt for a backtest, so the model never sees the future.
        """
        config = EDGE_HYBRID_SEARCH_RRF.model_copy(deep=True)
        config.limit = limit
        filters = SearchFilters(edge_types=[EDGE_REACTION])
        if as_of is not None:
            cutoff = _utc(as_of) - KNOWLEDGE_DELAY
            filters.valid_at = [[DateFilter(date=cutoff, comparison_operator=ComparisonOperator.less_than_equal)]]
        results = await self.graphiti.search_(query, config=config, group_ids=[group_id_for(symbol)],
                                              search_filter=filters)
        return [RecalledFact(symbol=symbol.upper(), fact=e.fact, valid_at=e.valid_at, attributes=dict(e.attributes))
                for e in results.edges]


def format_for_prompt(facts: list[RecalledFact]) -> str:
    """Prompt block. Facts only: past releases and the measured price move, newest first."""
    if not facts:
        return ""
    ordered = sorted(facts, key=lambda f: f.valid_at or datetime.min.replace(tzinfo=UTC), reverse=True)
    lines = ["Past market reactions (measured price moves after similar releases; no model opinions):"]
    lines += [f"- {f.fact}" for f in ordered]
    return "\n".join(lines)
