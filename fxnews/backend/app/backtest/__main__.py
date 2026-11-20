"""CLI: ``python -m app.backtest backfill --from 2026-11-01`` | ``predict --from ... --models a b`` | ``summary``."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime

from app.config import get_settings
from app.db import init_db, utcnow


def _bound(value: str | None, end: bool = False):
    from app.routers.backtest import parse_bound

    return parse_bound(value, end=end)


async def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.backtest")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("backfill", "predict", "summary"):
        p = sub.add_parser(name)
        p.add_argument("--from", dest="start", default=utcnow().strftime("%Y-%m-01"), help="YYYY-MM-DD (default: start of this month)")
        p.add_argument("--to", dest="end", default=None, help="YYYY-MM-DD (inclusive) or ISO datetime (default: now)")
        if name == "predict":
            p.add_argument("--models", nargs="+", default=None, help="provider:model specs (default: BACKTEST_MODELS)")
            p.add_argument("--min-impact", default="medium", choices=["low", "medium", "high"])
            p.add_argument("--limit", type=int, default=None, help="max new calls per model")
        if name == "backfill":
            p.add_argument("--offline", action="store_true", help="do not fetch RSS/FF live; use what is stored")
    args = parser.parse_args(argv)
    init_db()
    start = _bound(args.start)
    end = _bound(args.end, end=True) or utcnow()

    if args.cmd == "backfill":
        from app.backtest import ingest
        from app.market import candles

        result = await ingest.backfill(start, min(end, utcnow()), live=not args.offline, progress=lambda m: print(f"... {m}", file=sys.stderr))
        await candles.aclose()
        print(json.dumps(result.to_dict(), indent=2, default=str))
    elif args.cmd == "predict":
        from app.backtest import predict
        from app.llm import aclose_default

        models = args.models or get_settings().backtest_models
        result = await predict.run_predictions(start, end, models, min_impact=args.min_impact, limit=args.limit,
                                               progress=lambda m: print(f"... {m}", file=sys.stderr))
        await aclose_default()
        print(json.dumps(result.to_dict(), indent=2, default=str))
    else:
        from app.backtest import stats
        from app.db import session_scope

        with session_scope() as db:
            print(json.dumps(stats.summary(db, start, end), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main(sys.argv[1:])))
