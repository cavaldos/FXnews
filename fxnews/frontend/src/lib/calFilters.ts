import { useCallback, useMemo } from "react";
import type { Impact, NewsOut, ReactionHorizon, ReactionLabel } from "../api/types";
import { useStoredString } from "../hooks/useLocalStorage";
import { dayKey } from "./format";
import { currenciesOf, itemTime } from "./news";
import { HORIZON_STORAGE_KEY, parseHorizon } from "./reaction";

export const IMPACTS: Impact[] = ["High", "Medium", "Low", "Holiday"];

export interface CalFilters {
  q: string;
  /** Empty list = any. */
  impacts: string[];
  currencies: string[];
  sources: string[];
  hasActual: boolean;
  showCal: boolean;
  showNews: boolean;
}

const nyDayFmt = new Intl.DateTimeFormat("en-CA", {
  timeZone: "America/New_York",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
});

/** YYYY-MM-DD of an instant in New York (same shape as `dayKey`). Holidays are dated by their New York day. */
export function nyDayKey(ms: number): string {
  return nyDayFmt.format(new Date(ms));
}

/** Items that fall in [fromMs, toMs): holidays by their New York day, everything else by instant. */
export function inRange(
  items: NewsOut[],
  fromMs: number,
  toMs: number,
): NewsOut[] {
  const fromKey = dayKey(fromMs);
  const toKey = dayKey(toMs);
  return items.filter((n) => {
    const t = itemTime(n);
    if (n.impact === "Holiday") {
      const k = nyDayKey(t);
      return k >= fromKey && k < toKey;
    }
    return t >= fromMs && t < toMs;
  });
}

export function applyFilters(items: NewsOut[], f: CalFilters): NewsOut[] {
  const q = f.q.trim().toLowerCase();
  return items.filter((n) => {
    if (n.kind === "calendar" ? !f.showCal : !f.showNews) return false;
    if (q && !n.title.toLowerCase().includes(q)) return false;
    if (f.impacts.length && !(n.impact && f.impacts.includes(n.impact)))
      return false;
    if (
      f.currencies.length &&
      !currenciesOf(n).some((c) => f.currencies.includes(c))
    )
      return false;
    if (f.sources.length && !f.sources.includes(n.source)) return false;
    if (f.hasActual && !n.actual) return false;
    return true;
  });
}

/** Number of filters that differ from the defaults (everything shown). */
export function activeFilterCount(f: CalFilters): number {
  return (
    (f.q.trim() ? 1 : 0) +
    (f.impacts.length ? 1 : 0) +
    (f.currencies.length ? 1 : 0) +
    (f.sources.length ? 1 : 0) +
    (f.hasActual ? 1 : 0) +
    (f.showCal && f.showNews ? 0 : 1)
  );
}

function parseList(raw: string): string[] {
  try {
    const v: unknown = JSON.parse(raw);
    return Array.isArray(v)
      ? v.filter((x): x is string => typeof x === "string")
      : [];
  } catch {
    return [];
  }
}

/** Old single "minimum impact" preference ('', 'Medium', 'High') as an impact list. */
function legacyImpacts(): string {
  try {
    const old = localStorage.getItem("fxnews.cal.impact");
    if (old === "High") return JSON.stringify(["High"]);
    if (old === "Medium") return JSON.stringify(["High", "Medium"]);
  } catch {
    /* ignore */
  }
  return "[]";
}

/** Calendar filters persisted in localStorage (`fxnews.cal.*`); shared by the calendar and table views. */
export function useCalFilters(): {
  filters: CalFilters;
  patch: (p: Partial<CalFilters>) => void;
  clear: () => void;
} {
  const [q, setQ] = useStoredString("fxnews.cal.q", "");
  const [impacts, setImpacts] = useStoredString(
    "fxnews.cal.impacts",
    legacyImpacts(),
  );
  const [currencies, setCurrencies] = useStoredString(
    "fxnews.cal.currencies",
    "[]",
  );
  const [sources, setSources] = useStoredString("fxnews.cal.sources", "[]");
  const [hasActual, setHasActual] = useStoredString(
    "fxnews.cal.hasActual",
    "0",
  );
  const [showCal, setShowCal] = useStoredString("fxnews.cal.showCalendar", "1");
  const [showNews, setShowNews] = useStoredString(
    "fxnews.cal.showHeadlines",
    "1",
  );

  const filters = useMemo<CalFilters>(
    () => ({
      q,
      impacts: parseList(impacts),
      currencies: parseList(currencies),
      sources: parseList(sources),
      hasActual: hasActual === "1",
      showCal: showCal === "1",
      showNews: showNews === "1",
    }),
    [q, impacts, currencies, sources, hasActual, showCal, showNews],
  );

  const patch = useCallback(
    (p: Partial<CalFilters>) => {
      if (p.q !== undefined) setQ(p.q);
      if (p.impacts) setImpacts(JSON.stringify(p.impacts));
      if (p.currencies) setCurrencies(JSON.stringify(p.currencies));
      if (p.sources) setSources(JSON.stringify(p.sources));
      if (p.hasActual !== undefined) setHasActual(p.hasActual ? "1" : "0");
      if (p.showCal !== undefined) setShowCal(p.showCal ? "1" : "0");
      if (p.showNews !== undefined) setShowNews(p.showNews ? "1" : "0");
    },
    [
      setQ,
      setImpacts,
      setCurrencies,
      setSources,
      setHasActual,
      setShowCal,
      setShowNews,
    ],
  );

  const clear = useCallback(
    () =>
      patch({
        q: "",
        impacts: [],
        currencies: [],
        sources: [],
        hasActual: false,
        showCal: true,
        showNews: true,
      }),
    [patch],
  );

  return { filters, patch, clear };
}

/** Items within this distance of "now" count as live. */
export const LIVE_MS = 5 * 60_000;

export type TimeState = "past" | "live" | "upcoming" | "allday";

/** Where an item sits relative to `now`; holidays are all-day and never count as past/upcoming. */
export function timeState(n: NewsOut, now: number): TimeState {
  if (n.impact === "Holiday") return "allday";
  const d = itemTime(n) - now;
  if (Math.abs(d) <= LIVE_MS) return "live";
  return d < 0 ? "past" : "upcoming";
}

/** Id of the earliest non-holiday item strictly after `now`. Ties go to the lowest id, the same order the table and day lists use, so the accent lands on the first upcoming row. */
export function nextUpcomingId(
  items: NewsOut[],
  now: number,
): number | undefined {
  let best: NewsOut | undefined;
  for (const n of items) {
    if (n.impact === "Holiday") continue;
    const t = itemTime(n);
    if (t <= now) continue;
    if (!best) {
      best = n;
      continue;
    }
    const bt = itemTime(best);
    if (t < bt || (t === bt && n.id < best.id)) best = n;
  }
  return best?.id;
}

export const REACTION_LABELS: ReactionLabel[] = ["up", "down", "sideways"];

/** Horizon of the table's "Actual result" column, persisted (`fxnews.cal.reactionHorizon`). */
export function useReactionHorizon(): [ReactionHorizon, (h: ReactionHorizon) => void] {
  const [raw, setRaw] = useStoredString(HORIZON_STORAGE_KEY, "1h");
  return [parseHorizon(raw), setRaw];
}

/** Table filter on the reaction label (empty = any), persisted (`fxnews.cal.reactionFilter`). */
export function useReactionFilter(): [ReactionLabel[], (l: ReactionLabel[]) => void] {
  const [raw, setRaw] = useStoredString("fxnews.cal.reactionFilter", "[]");
  const labels = useMemo(
    () => parseList(raw).filter((x): x is ReactionLabel => REACTION_LABELS.includes(x as ReactionLabel)),
    [raw],
  );
  const set = useCallback((l: ReactionLabel[]) => setRaw(JSON.stringify(l)), [setRaw]);
  return [labels, set];
}
