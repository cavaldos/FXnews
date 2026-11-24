import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import type { NewsOut, ReactionHorizon, ReactionLabel } from "../api/types";
import { useReactionPanel } from "../context/reaction";
import { useNowFast, useNowSlow } from "../hooks/useClock";
import { useStoredString } from "../hooks/useLocalStorage";
import { nextUpcomingId, nyDayKey, timeState } from "../lib/calFilters";
import { flag, fmtDate, fmtTime, formatSpan, impactClass } from "../lib/format";
import { currenciesOf, impactRank, itemTime } from "../lib/news";
import {
  ensureReactions,
  horizonOf,
  reactionTooltip,
  useReactionLookup,
  useReactionVersion,
  type ReactionEntry,
} from "../lib/reaction";
import { ReactionLabel as ReactionLabelView } from "./ReactionLabel";
import { SymbolLink } from "./SymbolLink";
import { BiasBadge, EmptyState, ImpactBadge } from "./widgets";

type SortKey =
  | "time"
  | "currency"
  | "title"
  | "kind"
  | "impact"
  | "actual"
  | "forecast"
  | "previous"
  | "reaction"
  | "source"
  | "symbols"
  | "bias";
type Dir = "asc" | "desc";

interface Col {
  key: SortKey | null;
  label: string;
  cls?: string;
}

const BIAS_RANK = { bullish: 1, neutral: 0, bearish: -1 } as const;

function currencyOf(n: NewsOut): string {
  return n.currency ?? currenciesOf(n)[0] ?? "";
}

/** "1.2%", "-0.5K", "$3.4B", "250|-" -> number, or null if there is no leading number. */
function numeric(s?: string | null): number | null {
  if (!s) return null;
  const m = s.replace(/[,\s$€£¥]/g, "").match(/^[-+]?\d*\.?\d+/);
  if (!m) return null;
  const v = parseFloat(m[0]);
  const suffix = s
    .replace(/[,\s$€£¥]/g, "")
    .slice(m[0].length)[0]
    ?.toUpperCase();
  const mult =
    suffix === "K"
      ? 1e3
      : suffix === "M"
        ? 1e6
        : suffix === "B"
          ? 1e9
          : suffix === "T"
            ? 1e12
            : 1;
  return v * mult;
}

type Cmp = number | string | null;

function sortValue(
  n: NewsOut,
  key: SortKey,
  reactionOf: (n: NewsOut) => number | null,
): Cmp {
  switch (key) {
    case "time":
      return itemTime(n);
    case "currency":
      return currencyOf(n) || null;
    case "title":
      return n.title.toLowerCase();
    case "kind":
      return n.kind;
    case "impact":
      return n.impact ? impactRank(n.impact) : null;
    case "actual":
    case "forecast":
    case "previous": {
      const raw = n[key];
      return numeric(raw) ?? (raw ? raw.toLowerCase() : null);
    }
    case "reaction":
      return reactionOf(n);
    case "source":
      return n.source.toLowerCase();
    case "symbols":
      return n.symbols.length ? n.symbols.join(",") : null;
    case "bias":
      return n.rule_bias ? BIAS_RANK[n.rule_bias] : null;
  }
}

function compare(a: Cmp, b: Cmp): number {
  if (typeof a === "number" && typeof b === "number") return a - b;
  if (typeof a === "number") return -1;
  if (typeof b === "number") return 1;
  return String(a).localeCompare(String(b));
}

function sortItems(
  items: NewsOut[],
  key: SortKey,
  dir: Dir,
  reactionOf: (n: NewsOut) => number | null,
): NewsOut[] {
  const sign = dir === "asc" ? 1 : -1;
  return [...items].sort((x, y) => {
    const a = sortValue(x, key, reactionOf);
    const b = sortValue(y, key, reactionOf);
    // Empty cells always sink to the bottom, whichever way we sort. Ties: newest first.
    if (a === null || b === null)
      return a === b ? itemTime(y) - itemTime(x) : a === null ? 1 : -1;
    return sign * compare(a, b) || itemTime(y) - itemTime(x) || y.id - x.id;
  });
}

function parseSort(raw: string): { key: SortKey; dir: Dir } {
  const [key, dir] = raw.split(":");
  const keys: SortKey[] = [
    "time",
    "currency",
    "title",
    "kind",
    "impact",
    "actual",
    "forecast",
    "previous",
    "reaction",
    "source",
    "symbols",
    "bias",
  ];
  return {
    key: keys.includes(key as SortKey) ? (key as SortKey) : "time",
    dir: dir === "desc" ? "desc" : "asc",
  };
}

function TimeCell({ item }: { item: NewsOut }) {
  const t = itemTime(item);
  if (item.impact === "Holiday") {
    const k = nyDayKey(t);
    return (
      <>
        <span className="ct-date">{`${k.slice(8, 10)}/${k.slice(5, 7)}`}</span>{" "}
        <span className="ct-hm dim">all day</span>
      </>
    );
  }
  return (
    <>
      <span className="ct-date">{fmtDate(t)}</span>{" "}
      <span className="ct-hm">{fmtTime(t)}</span>
    </>
  );
}

function Timeline({ item, now }: { item: NewsOut; now: number }) {
  const state = timeState(item, now);
  if (state === "allday") return <span className="dim">all day</span>;
  if (state === "live") return <span className="ct-live">LIVE</span>;
  const d = itemTime(item) - now;
  return <>{d < 0 ? `\u2212${formatSpan(-d)}` : `in ${formatSpan(d)}`}</>;
}

/** Divider row between past and upcoming items; owns the per-second clock so only it re-renders each second. */
function NowRow({ colSpan }: { colSpan: number }) {
  const now = useNowFast();
  const d = new Date(now);
  const p = (n: number) => String(n).padStart(2, "0");
  return (
    <tr className="ct-now" data-now="1">
      <td colSpan={colSpan}>
        <span>{`NOW \u00b7 ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`}</span>
      </td>
    </tr>
  );
}

/** Result of the price reaction at the chosen horizon for the row's symbol; fetched lazily once the cell scrolls into view. */
function ReactionCell({
  item,
  symbol,
  horizon,
  entry,
  now,
  onSymbol,
}: {
  item: NewsOut;
  symbol: string | undefined;
  horizon: ReactionHorizon;
  entry: ReactionEntry | undefined;
  now: number;
  onSymbol: (s: string) => void;
}) {
  const ref = useRef<HTMLTableCellElement>(null);
  const t = itemTime(item);
  const eligible = Boolean(symbol) && item.impact !== "Holiday" && t <= now;
  useEffect(() => {
    const el = ref.current;
    if (!el || !eligible || !symbol) return;
    if (typeof IntersectionObserver === "undefined") {
      ensureReactions([{ symbol, timeMs: t }]);
      return;
    }
    const io = new IntersectionObserver(
      (hits) => {
        if (hits.some((h) => h.isIntersecting))
          ensureReactions([{ symbol, timeMs: t }]);
      },
      { rootMargin: "300px 0px" },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [eligible, symbol, t]);

  let body;
  if (!symbol || item.impact === "Holiday") body = <span className="dim">—</span>;
  else if (t > now) body = <span className="dim">pending</span>;
  else if (entry?.data && entry.data.status !== "error") {
    const d = entry.data;
    body =
      d.status === "ok" || d.status === "pending" ? (
        <ReactionLabelView h={horizonOf(d, horizon)} />
      ) : (
        <span className="dim">{d.status === "no_data" ? "n/a" : "—"}</span>
      );
  } else if (entry?.state === "error") body = <span className="dim">n/a</span>;
  else body = <span className="dim">…</span>;

  const multi = item.symbols.length > 1;
  return (
    <td
      ref={ref}
      className="ct-rx"
      title={
        symbol && t <= now
          ? (entry?.error ?? reactionTooltip(entry?.data, symbol))
          : undefined
      }
    >
      <span className="ct-rx-cell">
        {body}
        {multi && symbol ? (
          <select
            className="rx-sym"
            aria-label="Reaction symbol"
            value={symbol}
            onChange={(e) => onSymbol(e.target.value)}
            onClick={(e) => e.stopPropagation()}
            onKeyDown={(e) => e.stopPropagation()}
          >
            {item.symbols.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        ) : symbol && item.symbols.length === 1 ? (
          <span className="dim" style={{ fontSize: "var(--fs-xs)" }}>
            {symbol}
          </span>
        ) : null}
      </span>
    </td>
  );
}

interface Props {
  /** Already filtered; sorted here. */
  items: NewsOut[];
  loading: boolean;
  /** Visible range [fromMs, toMs): the NOW line and auto-scroll apply only when it contains now. */
  fromMs: number;
  toMs: number;
  onSelect: (n: NewsOut) => void;
  /** Horizon of the "Actual result" column. */
  horizon?: ReactionHorizon;
  /** Show only rows whose reaction label is in this list (empty = all). */
  reactionFilter?: ReactionLabel[];
}

/** Sortable table view of the calendar items. A row click opens the news detail, like a calendar chip. */
export function CalendarTable({
  items,
  loading,
  fromMs,
  toMs,
  onSelect,
  horizon = "1h",
  reactionFilter = [],
}: Props) {
  // v4: bump resets a stale saved sort (e.g. by reaction) back to the default newest-first
  const [sortRaw, setSortRaw] = useStoredString("fxnews.cal.sort.v4", "time:desc");
  const { key: sortKey, dir } = parseSort(sortRaw);
  const now = useNowSlow();
  const lookup = useReactionLookup();
  const rxVersion = useReactionVersion();
  const reaction = useReactionPanel();
  // Symbol picked per row in the "Actual result" cell (default: the first related symbol).
  const [symPick, setSymPick] = useState<Record<number, string>>({});
  const symOf = (n: NewsOut): string | undefined => {
    const p = symPick[n.id];
    return p && n.symbols.includes(p) ? p : n.symbols[0];
  };
  const entryOf = (n: NewsOut) => {
    const s = symOf(n);
    return s ? lookup(s, itemTime(n)) : undefined;
  };
  const filterKey = reactionFilter.join(",");
  const rxActive = filterKey !== "" || sortKey === "reaction";

  // Sorting / filtering by reaction needs every past row's result, not only the visible ones.
  useEffect(() => {
    if (!rxActive) return;
    ensureReactions(
      items.flatMap((n) => {
        const s = symPick[n.id] && n.symbols.includes(symPick[n.id]) ? symPick[n.id] : n.symbols[0];
        return s && n.impact !== "Holiday" ? [{ symbol: s, timeMs: itemTime(n) }] : [];
      }),
    );
  }, [rxActive, items, symPick]);

  const rows = useMemo(() => {
    const labelOf = (n: NewsOut) =>
      horizonOf(entryOf(n)?.data, horizon)?.label ?? null;
    const shown = filterKey
      ? items.filter((n) => {
          const l = labelOf(n);
          return l !== null && filterKey.split(",").includes(l);
        })
      : items;
    return sortItems(shown, sortKey, dir, (n) => horizonOf(entryOf(n)?.data, horizon)?.return_pct ?? null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items, sortKey, dir, horizon, filterKey, rxVersion, symPick]);
  const nextId = nextUpcomingId(items, now);
  // The NOW divider marks the past/upcoming boundary wherever it lands in the current order
  // (it stays at the time boundary even when the rows are sorted by another column).
  const nowIdx = (() => {
    if (now < fromMs || now >= toMs) return -1;
    const i = rows.findIndex((n) =>
      dir === "asc" ? itemTime(n) >= now : itemTime(n) < now,
    );
    return i === -1 ? rows.length : i;
  })();
  const wrapRef = useRef<HTMLDivElement>(null);
  const scrolledFor = useRef("");

  const jumpToNow = (smooth: boolean) => {
    const wrap = wrapRef.current;
    const line = wrap?.querySelector<HTMLElement>('[data-now="1"]');
    if (!wrap || !line) return;
    const delta =
      line.getBoundingClientRect().top - wrap.getBoundingClientRect().top;
    wrap.scrollTo({
      top: Math.max(0, wrap.scrollTop + delta - 96),
      behavior: smooth ? "smooth" : "auto",
    });
  };

  // Scroll to the NOW line once per range / sort when it first appears.
  const hasLine = nowIdx !== -1 && rows.length > 0;
  const scrollKey = `${fromMs}:${toMs}:${sortRaw}`;
  useEffect(() => {
    if (!hasLine || scrolledFor.current === scrollKey) return;
    scrolledFor.current = scrollKey;
    jumpToNow(false);
  });
  const hasBias = useMemo(() => items.some((n) => n.rule_bias), [items]);

  const cols: Col[] = [
    { key: "time", label: "Time" },
    { key: null, label: "Timeline" },
    { key: "currency", label: "Currency" },
    { key: "title", label: "Event" },
    { key: "kind", label: "Kind" },
    { key: "impact", label: "Impact" },
    { key: "actual", label: "Actual", cls: "num" },
    { key: "forecast", label: "Forecast", cls: "num" },
    { key: "previous", label: "Previous", cls: "num" },
    { key: "reaction", label: `Actual result · ${horizon}` },
    { key: "source", label: "Source" },
    { key: "symbols", label: "Symbols" },
    ...(hasBias ? [{ key: "bias" as const, label: "Sentiment" }] : []),
    { key: null, label: "Check", cls: "ct-act" },
  ];

  const sortBy = (key: SortKey) =>
    setSortRaw(
      key === sortKey
        ? `${key}:${dir === "asc" ? "desc" : "asc"}`
        : `${key}:${key === "title" || key === "currency" || key === "source" ? "asc" : "desc"}`,
    );

  return (
    <div className="ct-wrap" ref={wrapRef}>
      <table className="ct-table">
        <thead>
          <tr>
            {cols.map((c) => (
              <th
                key={c.label}
                className={c.cls}
                aria-sort={
                  c.key && c.key === sortKey
                    ? dir === "asc"
                      ? "ascending"
                      : "descending"
                    : undefined
                }
              >
                {c.key ? (
                  <button
                    type="button"
                    className={c.key === sortKey ? "on" : ""}
                    onClick={() => sortBy(c.key as SortKey)}
                  >
                    {c.label}
                    <span className="ct-arrow" aria-hidden>
                      {c.key === sortKey ? (dir === "asc" ? "▲" : "▼") : ""}
                    </span>
                  </button>
                ) : (
                  <span className="ct-th-static">{c.label}</span>
                )}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((n, i) => {
            const cur = currencyOf(n);
            const state = timeState(n, now);
            const cls = `${impactClass(n.impact)}${state === "past" ? " past" : state === "live" ? " live" : ""}${n.id === nextId ? " next" : ""}`;
            return (
              <Fragment key={n.id}>
                {i === nowIdx ? <NowRow colSpan={cols.length} /> : null}
                <tr
                  className={cls}
                  tabIndex={0}
                  onClick={() => onSelect(n)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      onSelect(n);
                    }
                  }}
                >
                  <td className="ct-time">
                    <TimeCell item={n} />
                  </td>
                  <td className="ct-rel">
                    <Timeline item={n} now={now} />
                  </td>
                  <td className="ct-cur">
                    {cur ? (
                      `${flag(cur)} ${cur}`.trim()
                    ) : (
                      <span className="dim">—</span>
                    )}
                  </td>
                  <td className="ct-title" title={n.title} data-preview-url={n.url && /^https?:\/\//.test(n.url) ? n.url : undefined}>
                    {n.title}
                  </td>
                  <td>
                    <span
                      className={`ct-kind${n.kind === "headline" ? " headline" : ""}`}
                    >
                      {n.kind === "calendar" ? "Economic" : "Headline"}
                    </span>
                  </td>
                  <td>
                    {n.impact ? (
                      <ImpactBadge impact={n.impact} />
                    ) : (
                      <span className="dim">—</span>
                    )}
                  </td>
                  <td className="num strong">
                    {n.actual || <span className="dim">—</span>}
                  </td>
                  <td className="num">
                    {n.forecast || <span className="dim">—</span>}
                  </td>
                  <td className="num">
                    {n.previous || <span className="dim">—</span>}
                  </td>
                  <ReactionCell
                    item={n}
                    symbol={symOf(n)}
                    horizon={horizon}
                    entry={entryOf(n)}
                    now={now}
                    onSymbol={(s) => setSymPick((p) => ({ ...p, [n.id]: s }))}
                  />
                  <td className="ct-src">{n.source}</td>
                  <td className="ct-sym" title={n.symbols.join(", ")}>
                    {n.symbols.length ? (
                      n.symbols.map((s, k) => (
                        <Fragment key={s}>
                          {k > 0 ? ", " : null}
                          <SymbolLink symbol={s} time={itemTime(n)} />
                        </Fragment>
                      ))
                    ) : (
                      <span className="dim">—</span>
                    )}
                  </td>
                  {hasBias ? (
                    <td>
                      {n.rule_bias ? (
                        <BiasBadge bias={n.rule_bias} />
                      ) : (
                        <span className="dim">—</span>
                      )}
                    </td>
                  ) : null}
                  <td className="ct-act">
                    {n.symbols.length && n.impact !== "Holiday" ? (
                      <button
                        type="button"
                        className="rx-check"
                        title="Check the price reaction of each related symbol"
                        onClick={(e) => {
                          e.stopPropagation();
                          reaction.open({
                            time: itemTime(n),
                            symbols: n.symbols,
                            title: `Check reaction · ${n.title}`.slice(0, 90),
                          });
                        }}
                        onKeyDown={(e) => e.stopPropagation()}
                      >
                        Check
                      </button>
                    ) : (
                      <span className="dim">—</span>
                    )}
                  </td>
                </tr>
              </Fragment>
            );
          })}
          {nowIdx === rows.length && rows.length > 0 ? (
            <NowRow colSpan={cols.length} />
          ) : null}
        </tbody>
      </table>
      {hasLine ? (
        <button
          type="button"
          className="ct-jump"
          onClick={() => jumpToNow(true)}
        >
          Jump to now
        </button>
      ) : sortKey !== "time" && now >= fromMs && now < toMs && rows.length > 0 ? (
        <button
          type="button"
          className="ct-jump"
          title="The NOW line is shown only when sorted by time"
          onClick={() => setSortRaw("time:desc")}
        >
          Sort by time (newest first)
        </button>
      ) : null}
      {rows.length === 0 ? (
        <EmptyState>
          {loading ? "Loading…" : "No items match the filters in this range."}
        </EmptyState>
      ) : null}
    </div>
  );
}
