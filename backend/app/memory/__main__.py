"""CLI.

python -m app.memory sync [--from 2026-01-01] [--symbols EURUSD XAUUSD] [--graph]   # outcomes -> SQLite (+ Graphiti)
python -m app.memory init                                # create Graphiti indices / constraints (once per graph DB)
python -m app.memory push-graph [--symbols EURUSD]       # copy SQLite memory into Graphiti (e.g. after an import)
python -m app.memory recall "US Non-Farm Payrolls" --symbol EURUSD [--as-of 2026-11-07T13:30] [--limit 8]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.db import init_db


async def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.memory")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    p_sync = sub.add_parser("sync")
    p_sync.add_argument("--from", dest="start", default=None)
    p_sync.add_argument("--to", dest="end", default=None)
    p_sync.add_argument("--symbols", nargs="+", default=None)
    p_sync.add_argument("--graph", action="store_true", help="also write to Graphiti (Neo4j / FalkorDB)")
    p_push = sub.add_parser("push-graph")
    p_push.add_argument("--symbols", nargs="+", default=None)
    p_rec = sub.add_parser("recall")
    p_rec.add_argument("query")
    p_rec.add_argument("--symbol", required=True)
    p_rec.add_argument("--as-of", dest="as_of", default=None, help="only facts fully known at this time (backtests)")
    p_rec.add_argument("--limit", type=int, default=8)
    args = parser.parse_args(argv)

    from app.memory.facts import group_id_for
    from app.memory.sync import push_graph, sync
    from app.routers.backtest import parse_bound

    init_db()
    if args.cmd == "sync" and not args.graph:
        result = await sync(None, start=parse_bound(args.start), end=parse_bound(args.end, end=True),
                            symbols=args.symbols, progress=lambda m: print(f"... {m}", file=sys.stderr))
        print(json.dumps(result.to_dict(), indent=2))
        return 0

    from app.memory.store import MemoryStore, format_for_prompt

    async with MemoryStore() as store:
        if args.cmd == "init":
            await store.init_schema()
            print("indices ready")
        elif args.cmd == "sync":
            result = await sync(store, start=parse_bound(args.start), end=parse_bound(args.end, end=True),
                                symbols=args.symbols, progress=lambda m: print(f"... {m}", file=sys.stderr))
            print(json.dumps(result.to_dict(), indent=2))
        elif args.cmd == "push-graph":
            groups = [group_id_for(s) for s in args.symbols] if args.symbols else None
            print(json.dumps({"graph_edges": await push_graph(store, group_ids=groups)}))
        else:
            facts = await store.recall(args.query, args.symbol, as_of=parse_bound(args.as_of), limit=args.limit)
            print(format_for_prompt(facts) or "(no matching memory)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main(sys.argv[1:])))
