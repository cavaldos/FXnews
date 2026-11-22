"""Read side of the dataset: event rows with outcomes/predictions, and per-model accuracy."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.backtest.models import HORIZONS, BtEvent, BtOutcome, BtPrediction
from app.config import get_settings

IMPACTS = ("high", "medium", "low")


def outcome_dict(o: BtOutcome) -> dict:
    return {
        "symbol": o.symbol,
        "interval": o.interval,
        "price_at_release": o.price_at_release,
        "returns": {h: getattr(o, f"ret_{h}") for h in HORIZONS},
        "prices": {h: getattr(o, f"price_{h}") for h in HORIZONS},
        "directions": {h: getattr(o, f"dir_{h}") for h in HORIZONS},
        "max_up_1h": o.max_up_1h,
        "max_down_1h": o.max_down_1h,
        "direction": o.direction,
        "direction_horizon": o.direction_horizon,
        "complete": o.complete,
    }


def prediction_dict(p: BtPrediction) -> dict:
    return {
        "id": p.id,
        "symbol": p.symbol,
        "direction": p.direction,
        "confidence": p.confidence,
        "horizon": p.horizon,
        "reasoning": p.reasoning,
        "latency_ms": p.latency_ms,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


def event_dict(e: BtEvent, outcomes: list[BtOutcome], predictions: list[BtPrediction]) -> dict:
    by_model: dict[str, list[dict]] = defaultdict(list)
    for p in predictions:
        by_model[p.model].append(prediction_dict(p))
    return {
        "id": e.id,
        "time": e.time.isoformat(),
        "kind": e.kind,
        "source": e.source,
        "currency": e.currency,
        "country": e.country,
        "title": e.title,
        "summary": e.summary,
        "url": e.url,
        "impact": e.impact,
        "category": e.category,
        "actual": e.actual,
        "forecast": e.forecast,
        "previous": e.previous,
        "symbols": e.symbols,
        "outcomes": [outcome_dict(o) for o in outcomes],
        "predictions": dict(by_model),
    }


def get_event(db: Session, event_id: int) -> dict | None:
    """One event in the same shape as the items of ``list_events``; None when it does not exist."""
    e = db.get(BtEvent, event_id)
    if e is None:
        return None
    outcomes = db.scalars(select(BtOutcome).where(BtOutcome.event_id == event_id).order_by(BtOutcome.id)).all()
    predictions = db.scalars(select(BtPrediction).where(BtPrediction.event_id == event_id).order_by(BtPrediction.id)).all()
    return event_dict(e, list(outcomes), list(predictions))


def _event_filter(stmt, start: datetime | None, end: datetime | None, impact: list[str] | None,
                  symbol: str | None, kind: str | None):
    if start:
        stmt = stmt.where(BtEvent.time >= start)
    if end:
        stmt = stmt.where(BtEvent.time < end)
    if impact:
        stmt = stmt.where(BtEvent.impact.in_([i.lower() for i in impact]))
    if symbol:
        stmt = stmt.where(BtEvent.symbols_csv.like(f"%,{symbol.upper()},%"))
    if kind:
        stmt = stmt.where(BtEvent.kind == kind)
    return stmt


def list_events(db: Session, start: datetime | None, end: datetime | None, *, impact: list[str] | None = None,
                symbol: str | None = None, kind: str | None = None, limit: int = 1000, offset: int = 0) -> dict:
    base = _event_filter(select(BtEvent), start, end, impact, symbol, kind)
    total = db.scalar(_event_filter(select(func.count(BtEvent.id)), start, end, impact, symbol, kind)) or 0
    events = db.scalars(base.order_by(BtEvent.time.desc(), BtEvent.id.desc()).limit(limit).offset(offset)).all()
    ids = [e.id for e in events]
    outcomes: dict[int, list[BtOutcome]] = defaultdict(list)
    predictions: dict[int, list[BtPrediction]] = defaultdict(list)
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for o in db.scalars(select(BtOutcome).where(BtOutcome.event_id.in_(chunk))):
            if not symbol or o.symbol == symbol.upper():
                outcomes[o.event_id].append(o)
        for p in db.scalars(select(BtPrediction).where(BtPrediction.event_id.in_(chunk)).order_by(BtPrediction.id)):
            if not symbol or p.symbol == symbol.upper():
                predictions[p.event_id].append(p)
    models = sorted({p.model for ps in predictions.values() for p in ps})
    return {
        "total": total,
        "models": models,
        "events": [event_dict(e, sorted(outcomes[e.id], key=lambda o: o.id), predictions[e.id]) for e in events],
    }


def _rate(n: int, hits: int) -> float | None:
    return round(hits / n, 4) if n else None


class _Tally:
    __slots__ = ("n", "hits", "dir_n", "dir_hits")

    def __init__(self) -> None:
        self.n = self.hits = self.dir_n = self.dir_hits = 0

    def add(self, predicted: str, actual: str) -> None:
        hit = predicted == actual
        self.n += 1
        self.hits += hit
        if actual != "flat":  # the market really moved: did the model get the way right?
            self.dir_n += 1
            self.dir_hits += hit

    def out(self) -> dict:
        return {"n": self.n, "hits": self.hits, "hit_rate": _rate(self.n, self.hits),
                "directional_n": self.dir_n, "directional_hits": self.dir_hits,
                "directional_hit_rate": _rate(self.dir_n, self.dir_hits)}


def summary(db: Session, start: datetime | None, end: datetime | None, *, impact: list[str] | None = None,
            symbol: str | None = None, kind: str | None = None, horizon: str | None = None) -> dict:
    """Per-model hit rate overall, by impact and by horizon.

    A prediction is a hit when its direction equals the outcome's direction label at that horizon (same event and
    symbol). ``directional_*`` ignores outcomes labelled flat. ``primary_horizon`` drives the headline numbers.
    """
    primary = horizon if horizon in HORIZONS else get_settings().backtest_primary_horizon
    ev = _event_filter(select(BtEvent.id), start, end, impact, symbol, kind)
    ev_ids = db.scalars(ev).all()
    impact_by_event = dict(db.execute(_event_filter(select(BtEvent.id, BtEvent.impact), start, end, impact, symbol, kind)).all())

    outcomes: dict[tuple[int, str], BtOutcome] = {}
    for i in range(0, len(ev_ids), 500):
        for o in db.scalars(select(BtOutcome).where(BtOutcome.event_id.in_(ev_ids[i:i + 500]))):
            outcomes[(o.event_id, o.symbol)] = o

    tallies: dict[str, dict] = {}
    conf_sum: dict[str, list[int]] = defaultdict(list)
    pred_counts: Counter[str] = Counter()
    for i in range(0, len(ev_ids), 500):
        for p in db.scalars(select(BtPrediction).where(BtPrediction.event_id.in_(ev_ids[i:i + 500]))):
            if symbol and p.symbol != symbol.upper():
                continue
            pred_counts[p.model] += 1
            if p.confidence is not None:
                conf_sum[p.model].append(p.confidence)
            outcome = outcomes.get((p.event_id, p.symbol))
            if outcome is None:
                continue
            slot = tallies.setdefault(p.model, {"overall": _Tally(), "by_impact": defaultdict(_Tally),
                                                "by_horizon": defaultdict(_Tally),
                                                "by_impact_horizon": defaultdict(lambda: defaultdict(_Tally))})
            imp = impact_by_event.get(p.event_id, "low")
            for h in HORIZONS:
                actual = getattr(outcome, f"dir_{h}")
                if actual is None:
                    continue
                slot["by_horizon"][h].add(p.direction, actual)
                slot["by_impact_horizon"][imp][h].add(p.direction, actual)
                if h == primary:
                    slot["overall"].add(p.direction, actual)
                    slot["by_impact"][imp].add(p.direction, actual)

    models = []
    for model in sorted(set(pred_counts) | set(tallies)):
        slot = tallies.get(model)
        confs = conf_sum.get(model) or []
        models.append({
            "model": model,
            "predictions": pred_counts[model],
            "avg_confidence": round(sum(confs) / len(confs), 1) if confs else None,
            "overall": slot["overall"].out() if slot else _Tally().out(),
            "by_impact": {i: slot["by_impact"][i].out() for i in IMPACTS if i in slot["by_impact"]} if slot else {},
            "by_horizon": {h: slot["by_horizon"][h].out() for h in HORIZONS if h in slot["by_horizon"]} if slot else {},
            "by_impact_horizon": {i: {h: slot["by_impact_horizon"][i][h].out() for h in HORIZONS if h in slot["by_impact_horizon"][i]}
                                  for i in IMPACTS if i in slot["by_impact_horizon"]} if slot else {},
        })

    # Dataset facts + naive baselines (always-the-most-common-label) so a model's hit rate has something to beat.
    labels: dict[str, Counter[str]] = {h: Counter() for h in HORIZONS}
    for o in outcomes.values():
        for h in HORIZONS:
            label = getattr(o, f"dir_{h}")
            if label:
                labels[h][label] += 1
    baseline = {h: {"n": sum(c.values()), "majority": c.most_common(1)[0][0], "hit_rate": _rate(sum(c.values()), c.most_common(1)[0][1]),
                    "distribution": dict(c)} for h, c in labels.items() if c}
    return {
        "primary_horizon": primary,
        "events": len(ev_ids),
        "events_by_impact": dict(Counter(impact_by_event.values())),
        "events_with_outcomes": len({k[0] for k in outcomes}),
        "outcomes": len(outcomes),
        # candle interval each outcome was priced from (finest that covered the event); coarse intervals have no
        # +5m/+15m numbers, so per-horizon ``n`` can be smaller than ``outcomes``
        "outcomes_by_interval": dict(Counter(o.interval for o in outcomes.values())),
        "baseline_by_horizon": baseline,
        "models": models,
    }
