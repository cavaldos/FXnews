"""Event + observed outcome -> memory records. Pure functions (no I/O, no LLM).

Bias guard: the builder reads ONLY the whitelisted columns below from ``BtEvent`` / ``BtOutcome``. It never receives a
prediction, analysis, signal or rule bias, and it ignores ``BtEvent.raw`` (free-form source payload) and, unless
``include_summary`` is set, the article summary (editorial opinion). Everything stored is a measured number or a field
of the published release.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.analysis.rules import parse_economic_value
from app.backtest.models import HORIZONS, BtEvent, BtOutcome

# Columns the memory may read. Anything else on the rows is ignored on purpose.
EVENT_FIELDS: tuple[str, ...] = (
    "dedupe_key", "time", "kind", "source", "currency", "country", "title", "url", "impact", "category",
    "actual", "forecast", "previous",
)
OUTCOME_FIELDS: tuple[str, ...] = (
    "symbol", "interval", "candle_source", "price_at_release", "max_up_1h", "max_down_1h", "direction",
    "direction_horizon", "complete",
    *(f"{p}_{h}" for p in ("ret", "price", "dir") for h in HORIZONS),
)
# Words that would mean a model's / analyst's call slipped into a stored text. Facts are generated from numbers only,
# so these must never appear; the check is a last line of defence against future edits of this module.
FORBIDDEN_WORDS = re.compile(r"\b(bias|bullish|bearish|predict\w*|forecasted by|expect\w*|confidence|signal|"
                             r"recommend\w*|should|likely)\b", re.IGNORECASE)

NAMESPACE = uuid.UUID("6f1d3c2e-9a51-4b8e-bf3a-0c7d2e5a9f10")
EDGE_REACTION = "MARKET_REACTION"
EDGE_INSTANCE = "INSTANCE_OF"
LABEL_SYMBOL, LABEL_EVENT, LABEL_INDICATOR = "Symbol", "MarketEvent", "Indicator"


@dataclass(frozen=True)
class NodeSpec:
    uuid: str
    name: str
    label: str
    summary: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EdgeSpec:
    uuid: str
    name: str
    source_uuid: str
    target_uuid: str
    fact: str
    valid_at: datetime
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MemoryRecord:
    """Everything one (event, symbol) outcome contributes to the graph partition of that symbol."""

    group_id: str
    nodes: list[NodeSpec]
    edges: list[EdgeSpec]


class BiasLeakError(ValueError):
    """A stored text contains prediction / opinion vocabulary."""


def group_id_for(symbol: str) -> str:
    return "fx_" + re.sub(r"[^A-Za-z0-9_-]", "_", symbol.upper())


def stable_uuid(group_id: str, key: str) -> str:
    """Deterministic uuid: re-syncing the same outcome overwrites (MERGE on uuid) instead of duplicating."""
    return str(uuid.uuid5(NAMESPACE, f"{group_id}|{key}"))


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.3f}%"


def _norm_title(title: str) -> str:
    return re.sub(r"\s+", " ", title).strip()


def _check_clean(text: str, title: str) -> str:
    """Raise if the GENERATED part of ``text`` carries opinion words. The published title itself is news (a headline
    may well say "expected"), so it is masked out before the check."""
    match = FORBIDDEN_WORDS.search(text.replace(title, " "))
    if match:
        raise BiasLeakError(f"memory text contains opinion word {match.group(0)!r}: {text[:120]}")
    return text


def is_clean(text: str, title: str = "") -> bool:
    """True when the generated part of ``text`` (published ``title`` masked) has no opinion vocabulary."""
    return not FORBIDDEN_WORDS.search(text.replace(title, " ") if title else text)


def _event_view(event: BtEvent) -> dict[str, Any]:
    return {name: getattr(event, name) for name in EVENT_FIELDS}


def _outcome_view(outcome: BtOutcome) -> dict[str, Any]:
    return {name: getattr(outcome, name) for name in OUTCOME_FIELDS}


def surprise(actual: str | None, forecast: str | None) -> float | None:
    """actual - forecast in the release's own unit (K, %, ...), None when either is missing / unparsable."""
    a, f = parse_economic_value(actual), parse_economic_value(forecast)
    return None if a is None or f is None else round(a - f, 6)


def release_text(ev: dict[str, Any]) -> str:
    when = _utc(ev["time"]).strftime("%Y-%m-%d %H:%M UTC")
    head = f"{_norm_title(ev['title'])} ({ev.get('currency') or '-'}, {ev['impact']} impact, {ev['kind']}) at {when}"
    numbers = [f"{k} {ev[k]}" for k in ("actual", "forecast", "previous") if ev.get(k)]
    s = surprise(ev.get("actual"), ev.get("forecast"))
    if s is not None:
        numbers.append(f"actual minus forecast {s:+g}")
    return head + (": " + ", ".join(numbers) if numbers else "")


def reaction_text(ev: dict[str, Any], oc: dict[str, Any]) -> str:
    moves = []
    for h in HORIZONS:
        ret = oc.get(f"ret_{h}")
        if ret is not None:
            moves.append(f"{h} {_pct(ret)} ({oc.get(f'dir_{h}') or 'n/a'})")
    parts = [f"{oc['symbol']} after {release_text(ev)}.", f"Price at release {oc['price_at_release']:g}."]
    parts.append("Observed move: " + ("; ".join(moves) if moves else "no horizon available") + ".")
    if oc.get("max_up_1h") is not None or oc.get("max_down_1h") is not None:
        parts.append(f"First-hour range {_pct(oc.get('max_up_1h'))} / {_pct(oc.get('max_down_1h'))}.")
    return " ".join(parts)


def _scalars(values: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in values.items():
        if value is None:
            continue
        out[key] = _utc(value).isoformat() if isinstance(value, datetime) else value
    return out


def build_record(event: BtEvent, outcome: BtOutcome, *, include_summary: bool = False) -> MemoryRecord:
    """The memory record of one observed outcome. Raises ``BiasLeakError`` if a text would carry an opinion."""
    ev, oc = _event_view(event), _outcome_view(outcome)
    if event.id != outcome.event_id:
        raise ValueError("outcome does not belong to event")
    gid = group_id_for(oc["symbol"])
    title = _norm_title(ev["title"])
    valid_at = _utc(ev["time"])

    symbol = NodeSpec(stable_uuid(gid, f"symbol:{oc['symbol']}"), oc["symbol"], LABEL_SYMBOL,
                      summary=f"Instrument {oc['symbol']}")
    event_summary = release_text(ev)
    if include_summary and event.summary:
        event_summary += " | " + event.summary.strip()[:500]  # opt-in: published article text, not a model's
    event_node = NodeSpec(
        stable_uuid(gid, f"event:{ev['dedupe_key']}"), f"{title} @ {valid_at:%Y-%m-%d %H:%M}", LABEL_EVENT,
        summary=_check_clean(event_summary, title) if not include_summary else event_summary,
        attributes=_scalars({**{k: ev[k] for k in EVENT_FIELDS if k != "title"}, "title": title,
                             "surprise": surprise(ev.get("actual"), ev.get("forecast"))}),
    )
    reaction = EdgeSpec(
        stable_uuid(gid, f"reaction:{ev['dedupe_key']}:{oc['symbol']}"), EDGE_REACTION,
        event_node.uuid, symbol.uuid, _check_clean(reaction_text(ev, oc), title), valid_at,
        attributes=_scalars({k: v for k, v in oc.items() if k != "complete"}),
    )
    nodes, edges = [symbol, event_node], [reaction]

    if ev["kind"] == "calendar":  # recurring release: link every occurrence to one indicator node
        ind_key = f"{(ev.get('country') or ev.get('currency') or '').upper()}:{title.lower()}"
        indicator = NodeSpec(stable_uuid(gid, f"indicator:{ind_key}"), title, LABEL_INDICATOR,
                             summary=f"Economic release {title} ({ev.get('country') or ev.get('currency') or '-'})",
                             attributes=_scalars({"country": ev.get("country"), "currency": ev.get("currency")}))
        nodes.append(indicator)
        edges.append(EdgeSpec(stable_uuid(gid, f"instance:{ev['dedupe_key']}"), EDGE_INSTANCE, event_node.uuid,
                              indicator.uuid, _check_clean(release_text(ev), title), valid_at))
    return MemoryRecord(gid, nodes, edges)
