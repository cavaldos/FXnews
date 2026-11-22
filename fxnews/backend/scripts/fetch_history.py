"""Fetch historical events (calendar + headlines) and price outcomes for 2026, and 2025 when available.

Usage (from fxnews/backend):
    python scripts/fetch_history.py                       # 2026-01-01 -> now, then 2025 (best effort)
    python scripts/fetch_history.py --years 2026          # only 2026
    python scripts/fetch_history.py --from 2025-06-01 --to 2025-08-31
    python scripts/fetch_history.py --offline             # recompute outcomes only, no live fetch

Runs month by month (2026 first, then 2025 newest -> oldest). Re-runnable: events are deduped and finished
outcomes are skipped. A failing month is logged and does not stop the rest.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.backtest import ingest  # noqa: E402
from app.db import init_db, utcnow  # noqa: E402
from app.market import candles  # noqa: E402


def month_ranges(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    out, cur = [], start.replace(day=1)
    while cur < end:
        nxt = cur.replace(year=cur.year + 1, month=1) if cur.month == 12 else cur.replace(month=cur.month + 1)
        out.append((max(cur, start), min(nxt, end)))
        cur = nxt
    return out


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


async def run(ranges: list[tuple[datetime, datetime]], live: bool) -> int:
    init_db()
    totals = {"events": 0, "outcomes": 0, "failed_months": 0}
    try:
        for s, e in ranges:
            label = s.strftime("%Y-%m")
            try:
                r = await ingest.backfill(s, e, live=live, progress=lambda m, lb=label: log(f"[{lb}] {m}"))
            except Exception as exc:  # isolate month
                totals["failed_months"] += 1
                log(f"[{label}] FAILED: {exc!r}")
                continue
            totals["events"] += r.events_inserted + r.events_updated
            totals["outcomes"] += r.outcomes_created + r.outcomes_updated
            log(f"[{label}] collected={r.collected} +{r.events_inserted} new/{r.events_updated} upd, "
                f"outcomes +{r.outcomes_created}/{r.outcomes_updated} upd, "
                f"no_data={r.outcomes_skipped_no_data}, not_covered={r.outcomes_skipped_not_covered}")
            for err in r.errors[:5]:
                log(f"    ! {err}")
    finally:
        await candles.aclose()
    log(f"DONE {totals}")
    return 1 if totals["failed_months"] else 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--years", nargs="+", type=int, default=[2026, 2025])
    p.add_argument("--from", dest="start", help="YYYY-MM-DD (overrides --years)")
    p.add_argument("--to", dest="end", help="YYYY-MM-DD exclusive (default: now / end of year)")
    p.add_argument("--offline", action="store_true", help="no live RSS/FF fetch; use stored data")
    a = p.parse_args()
    now = utcnow().replace(tzinfo=UTC)

    def d(s: str) -> datetime:
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=UTC)

    ranges: list[tuple[datetime, datetime]] = []
    if a.start:
        ranges = month_ranges(d(a.start), min(d(a.end) if a.end else now, now))
    else:
        for y in a.years:
            ranges += list(reversed(month_ranges(datetime(y, 1, 1, tzinfo=UTC), min(datetime(y + 1, 1, 1, tzinfo=UTC), now))))
    return asyncio.run(run(ranges, live=not a.offline))


if __name__ == "__main__":
    raise SystemExit(main())
