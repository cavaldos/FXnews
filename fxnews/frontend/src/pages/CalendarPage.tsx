import {
  Fragment,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { Impact, NewsOut } from "../api/types";
import { CalendarFilters } from "../components/CalendarFilters";
import { CalendarTable } from "../components/CalendarTable";
import { Modal } from "../components/Modal";
import { UiButton } from "../components/UI/UiButton";
import { ErrorNote, Spinner } from "../components/widgets";
import { useNowSlow } from "../hooks/useClock";
import { useReactionPanel } from "../context/reaction";
import { useFetch } from "../hooks/useFetch";
import { useStoredString } from "../hooks/useLocalStorage";
import {
  applyFilters,
  inRange,
  nextUpcomingId,
  nyDayKey,
  REACTION_LABELS,
  timeState,
  useCalFilters,
  useReactionFilter,
  useReactionHorizon,
} from "../lib/calFilters";
import { HORIZONS, LABEL_GLYPH, LABEL_TEXT } from "../lib/reaction";
import { dayKey, fmtTime } from "../lib/format";
import { currenciesOf, itemTime, mergeNews } from "../lib/news";
import { fetchNewsRange } from "../lib/newsRange";

type View = "month" | "week";
type Mode = "calendar" | "table";
type TableRange = "month" | "week" | "custom";

const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const MONTHS = [
  "January",
  "February",
  "March",
  "April",
  "May",
  "June",
  "July",
  "August",
  "September",
  "October",
  "November",
  "December",
];
const CHIP_H = 21;
const CELL_HEAD_H = 28;
const DAY_MS = 86_400_000;
/** Holiday events are dated by their New York calendar day; fetch a day either side so none is cut off at the edge. */
const HOLIDAY_PAD_MS = DAY_MS;

function impactClass(impact?: Impact | null): string {
  return `imp-${(impact ?? "none").toLowerCase()}`;
}

function startOfDay(d: Date): Date {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate());
}

function addDays(d: Date, n: number): Date {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate() + n);
}

/** Monday of the week containing `d` (local). */
function weekStart(d: Date): Date {
  return addDays(startOfDay(d), -((d.getDay() + 6) % 7));
}

interface Range {
  start: Date;
  /** Exclusive end (also a local midnight). */
  end: Date;
  days: Date[];
}

function rangeOf(anchor: Date, view: View): Range {
  let start: Date;
  let end: Date;
  if (view === "week") {
    start = weekStart(anchor);
    end = addDays(start, 7);
  } else {
    start = weekStart(new Date(anchor.getFullYear(), anchor.getMonth(), 1));
    end = addDays(
      weekStart(new Date(anchor.getFullYear(), anchor.getMonth() + 1, 0)),
      7,
    );
  }
  const days: Date[] = [];
  for (let d = start; d < end; d = addDays(d, 1)) days.push(d);
  return { start, end, days };
}

const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

function parseDate(v: string, fallback: Date): Date {
  if (!DATE_RE.test(v)) return fallback;
  const d = new Date(
    Number(v.slice(0, 4)),
    Number(v.slice(5, 7)) - 1,
    Number(v.slice(8, 10)),
  );
  return Number.isNaN(d.getTime()) ? fallback : d;
}

function toInput(d: Date): string {
  return dayKey(d.getTime());
}

interface Span {
  start: Date;
  /** Exclusive end (local midnight). */
  end: Date;
}

function tableSpan(
  anchor: Date,
  mode: TableRange,
  fromMs: number,
  toMs: number,
): Span {
  if (mode === "week") {
    const start = weekStart(anchor);
    return { start, end: addDays(start, 7) };
  }
  if (mode === "month") {
    return {
      start: new Date(anchor.getFullYear(), anchor.getMonth(), 1),
      end: new Date(anchor.getFullYear(), anchor.getMonth() + 1, 1),
    };
  }
  const start = new Date(Math.min(fromMs, toMs));
  const last = new Date(Math.max(fromMs, toMs));
  return { start, end: addDays(last, 1) };
}

function msOf(span: Span): [number, number] {
  return [span.start.getTime(), span.end.getTime()];
}

function spanTitle(span: Span): string {
  const a = span.start;
  const b = addDays(span.end, -1);
  const m = (d: Date) => MONTHS[d.getMonth()].slice(0, 3);
  if (a.getTime() === b.getTime())
    return `${m(a)} ${a.getDate()}, ${a.getFullYear()}`;
  if (a.getFullYear() === b.getFullYear())
    return `${m(a)} ${a.getDate()} – ${m(b)} ${b.getDate()}, ${b.getFullYear()}`;
  return `${m(a)} ${a.getDate()}, ${a.getFullYear()} – ${m(b)} ${b.getDate()}, ${b.getFullYear()}`;
}

function titleOf(anchor: Date, view: View, range: Range): string {
  if (view === "month")
    return `${MONTHS[anchor.getMonth()]} ${anchor.getFullYear()}`;
  const a = range.days[0];
  const b = range.days[6];
  const m = (d: Date) => MONTHS[d.getMonth()].slice(0, 3);
  return a.getMonth() === b.getMonth()
    ? `${MONTHS[a.getMonth()]} ${a.getDate()}–${b.getDate()}, ${b.getFullYear()}`
    : `${m(a)} ${a.getDate()} – ${m(b)} ${b.getDate()}, ${b.getFullYear()}`;
}

function Chip({
  item,
  onSelect,
  now,
  isNext,
}: {
  item: NewsOut;
  onSelect: (n: NewsOut) => void;
  now: number;
  isNext: boolean;
}) {
  const state = timeState(item, now);
  const cur =
    item.kind === "calendar"
      ? (item.currency ?? currenciesOf(item)[0])
      : undefined;
  const time = item.impact === "Holiday" ? "" : fmtTime(itemTime(item));
  return (
    <button
      type="button"
      className={`cal-chip ${impactClass(item.impact)}${item.kind === "headline" ? " headline" : ""}${state === "past" ? " past" : state === "live" ? " live" : ""}${isNext ? " next" : ""}`}
      onClick={() => onSelect(item)}
      title={`${time ? `${time} · ` : ""}${cur ? `${cur} · ` : ""}${item.title}${item.impact ? ` (${item.impact})` : ""}`}
    >
      {cur ? <span className="cal-chip-cur">{cur}</span> : null}
      <span className="cal-chip-title" data-preview-url={item.url && /^https?:\/\//.test(item.url) ? item.url : undefined}>{item.title}</span>
      <span className="cal-chip-time">{time}</span>
    </button>
  );
}

/** Start of the week containing the 1st of `anchor`'s month, or `anchor`'s own day if it is today's month and today is in it. */
function snapToWeek(anchor: Date, today: Date): Date {
  const sameMonth =
    today.getFullYear() === anchor.getFullYear() &&
    today.getMonth() === anchor.getMonth();
  return sameMonth
    ? startOfDay(today)
    : weekStart(new Date(anchor.getFullYear(), anchor.getMonth(), 1));
}

interface Props {
  onSelect: (n: NewsOut) => void;
  /** Bumped by the parent on each calendar SSE update; triggers a refetch. */
  calendarRev?: number;
}

/** Notion-style month / week calendar (or sortable table) of economic-calendar events and headlines. */
export function CalendarPage({ onSelect, calendarRev = 0 }: Props) {
  const [modePref, setModePref] = useStoredString(
    "fxnews.cal.mode",
    "calendar",
  );
  const [viewPref, setViewPref] = useStoredString("fxnews.cal.view", "month");
  const [tRangePref, setTRangePref] = useStoredString(
    "fxnews.cal.tableRange",
    "month",
  );
  const [fromPref, setFromPref] = useStoredString("fxnews.cal.from", "");
  const [toPref, setToPref] = useStoredString("fxnews.cal.to", "");
  const { filters, patch, clear } = useCalFilters();
  const [rxHorizon, setRxHorizon] = useReactionHorizon();
  const [rxFilter, setRxFilter] = useReactionFilter();
  const reactionPanel = useReactionPanel();
  const mode: Mode = modePref === "table" ? "table" : "calendar";
  const view: View = viewPref === "week" ? "week" : "month";
  const tRange: TableRange =
    tRangePref === "week"
      ? "week"
      : tRangePref === "custom"
        ? "custom"
        : "month";
  const [anchor, setAnchor] = useState(() => startOfDay(new Date()));
  const [dayOpen, setDayOpen] = useState<string | null>(null);

  const now = useNowSlow();
  const range = useMemo(() => rangeOf(anchor, view), [anchor, view]);
  const customFrom = parseDate(
    fromPref,
    new Date(anchor.getFullYear(), anchor.getMonth(), 1),
  );
  const customTo = parseDate(toPref, startOfDay(new Date()));
  const tbl = tableSpan(
    anchor,
    tRange,
    customFrom.getTime(),
    customTo.getTime(),
  );
  const [fromMs, toMs] = msOf(mode === "table" ? tbl : range);

  const q = useFetch(
    `cal:${fromMs}:${toMs}`,
    async (signal) => {
      const from = fromMs - HOLIDAY_PAD_MS;
      const to = toMs + HOLIDAY_PAD_MS;
      const [events, headlines] = await Promise.all([
        fetchNewsRange("calendar", from, to, signal),
        fetchNewsRange("headline", from, to, signal),
      ]);
      return mergeNews(events, headlines);
    },
    5 * 60_000,
  );
  const { reload } = q;

  // Refetch (keeping the current data on screen) when the parent reports a calendar SSE update.
  const lastRev = useRef(calendarRev);
  useEffect(() => {
    if (lastRev.current === calendarRev) return;
    lastRev.current = calendarRev;
    reload();
  }, [calendarRev, reload]);

  // Items inside the visible range (unfiltered), then the filtered subset shared by both views.
  const ranged = useMemo(
    () => inRange(q.data ?? [], fromMs, toMs),
    [q.data, fromMs, toMs],
  );
  const visible = useMemo(
    () => applyFilters(ranged, filters),
    [ranged, filters],
  );
  const currencyOptions = useMemo(
    () => [...new Set(ranged.flatMap((n) => currenciesOf(n)))].sort(),
    [ranged],
  );
  const sourceOptions = useMemo(
    () => [...new Set(ranged.map((n) => n.source).filter(Boolean))].sort(),
    [ranged],
  );

  const byDay = useMemo(() => {
    const m = new Map<string, NewsOut[]>();
    for (const n of visible) {
      const t = itemTime(n);
      // Holidays belong to their New York date, whatever the viewer's zone; unmatched days are simply not drawn.
      const k = n.impact === "Holiday" ? nyDayKey(t) : dayKey(t);
      const list = m.get(k);
      if (list) list.push(n);
      else m.set(k, [n]);
    }
    for (const list of m.values())
      list.sort((a, b) => itemTime(a) - itemTime(b) || a.id - b.id);
    return m;
  }, [visible]);

  // How many chips fit in a cell (month view); week view shows everything and scrolls.
  const gridRef = useRef<HTMLDivElement>(null);
  const [fit, setFit] = useState(3);
  useLayoutEffect(() => {
    const el = gridRef.current;
    if (!el) return;
    const rows = range.days.length / 7;
    const measure = () => {
      const cellH = el.clientHeight / rows;
      setFit(
        Math.max(
          1,
          Math.floor((cellH - CELL_HEAD_H - CELL_HEAD_H / 1.5) / CHIP_H),
        ),
      );
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [range.days.length, mode]);

  // Week view: the columns scroll on their own, so bring today's NOW line into view once per loaded week.
  const weekScrolled = useRef("");
  const weekKey =
    mode === "calendar" && view === "week" && q.data
      ? `${view}:${fromMs}`
      : "";
  useEffect(() => {
    if (!weekKey) {
      weekScrolled.current = "";
      return;
    }
    if (weekScrolled.current === weekKey) return;
    weekScrolled.current = weekKey;
    const line = gridRef.current?.querySelector<HTMLElement>(
      ".cal-cell.today .cal-nowline",
    );
    const box = line?.parentElement;
    if (!line || !box) return;
    box.scrollTop +=
      line.getBoundingClientRect().top -
      box.getBoundingClientRect().top -
      box.clientHeight / 2;
  }, [weekKey]);

  const step = (dir: -1 | 1) => {
    setDayOpen(null);
    const unit = mode === "table" ? tRange : view;
    if (unit === "custom") {
      const days = Math.round((toMs - fromMs) / DAY_MS);
      setFromPref(toInput(addDays(new Date(fromMs), days * dir)));
      setToPref(toInput(addDays(new Date(fromMs), days * dir + days - 1)));
      return;
    }
    setAnchor((a) =>
      unit === "week"
        ? addDays(a, 7 * dir)
        : new Date(a.getFullYear(), a.getMonth() + dir, 1),
    );
  };

  const goToday = () => {
    setDayOpen(null);
    const today = startOfDay(new Date());
    if (mode === "table" && tRange === "custom") {
      const days = Math.round((toMs - fromMs) / DAY_MS);
      setFromPref(toInput(today));
      setToPref(toInput(addDays(today, days - 1)));
      return;
    }
    setAnchor(today);
  };

  const switchRange = (v: TableRange) => {
    const cur = mode === "table" ? tRange : view;
    if (v === cur) return;
    setDayOpen(null);
    if (v === "custom") {
      // Start from what is on screen.
      setFromPref(toInput(new Date(fromMs)));
      setToPref(toInput(addDays(new Date(toMs), -1)));
    } else if (v === "week") {
      setAnchor((a) => snapToWeek(a, new Date()));
    }
    if (mode === "table") setTRangePref(v);
    else setViewPref(v);
  };

  const switchMode = (m: Mode) => {
    if (m === mode) return;
    setDayOpen(null);
    setModePref(m);
  };

  const setCustom = (which: "from" | "to", value: string) => {
    if (!DATE_RE.test(value)) return;
    if (which === "from") {
      setFromPref(value);
      if (value > toInput(customTo)) setToPref(value);
    } else {
      setToPref(value);
      if (value < toInput(customFrom)) setFromPref(value);
    }
  };

  const todayKey = dayKey(now);
  const nextId = nextUpcomingId(visible, now);
  const openItems = dayOpen ? (byDay.get(dayOpen) ?? []) : [];
  const openDate = dayOpen
    ? range.days.find((d) => dayKey(d.getTime()) === dayOpen)
    : undefined;
  const title =
    mode === "table"
      ? tRange === "month"
        ? `${MONTHS[anchor.getMonth()]} ${anchor.getFullYear()}`
        : spanTitle(tbl)
      : titleOf(anchor, view, range);
  const rangeOptions: [TableRange, string][] =
    mode === "table"
      ? [
          ["month", "Month"],
          ["week", "Week"],
          ["custom", "Custom"],
        ]
      : [
          ["month", "Month"],
          ["week", "Week"],
        ];
  const curRange = mode === "table" ? tRange : view;

  return (
    <main className="cal-page">
      <div className="cal-toolbar">
        <h2 className="cal-title">{title}</h2>
        <div className="cal-nav">
          <UiButton
            size="sm"
            onClick={() => step(-1)}
            aria-label="Previous"
            title="Previous"
          >
            ‹
          </UiButton>
          <UiButton size="sm" onClick={goToday}>
            Today
          </UiButton>
          <UiButton
            size="sm"
            onClick={() => step(1)}
            aria-label="Next"
            title="Next"
          >
            ›
          </UiButton>
        </div>
        {mode === "table" && tRange === "custom" ? (
          <div className="ct-dates">
            <input
              type="date"
              aria-label="From"
              value={toInput(customFrom)}
              max={toInput(customTo)}
              onChange={(e) => setCustom("from", e.target.value)}
            />
            <span>–</span>
            <input
              type="date"
              aria-label="To"
              value={toInput(customTo)}
              min={toInput(customFrom)}
              onChange={(e) => setCustom("to", e.target.value)}
            />
          </div>
        ) : null}
        {q.loading ? <Spinner label="Loading…" /> : null}
        <span className="spacer" />
        {mode === "table" ? (
          <div className="cal-rx">
            <span>Result</span>
            <div className="cal-seg" role="group" aria-label="Result horizon">
              {HORIZONS.map((h) => (
                <button
                  key={h}
                  type="button"
                  className={rxHorizon === h ? "on" : ""}
                  title={`Price move ${h} after the release`}
                  onClick={() => setRxHorizon(h)}
                >
                  {h}
                </button>
              ))}
            </div>
            <div className="cal-seg" role="group" aria-label="Result filter">
              {REACTION_LABELS.map((l) => (
                <button
                  key={l}
                  type="button"
                  className={rxFilter.includes(l) ? "on" : ""}
                  aria-pressed={rxFilter.includes(l)}
                  title={`Only rows where the result is ${LABEL_TEXT[l]}`}
                  onClick={() =>
                    setRxFilter(
                      rxFilter.includes(l)
                        ? rxFilter.filter((x) => x !== l)
                        : [...rxFilter, l],
                    )
                  }
                >
                  {LABEL_GLYPH[l]} {l}
                </button>
              ))}
            </div>
          </div>
        ) : null}
        <UiButton
          size="sm"
          tone="ghost"
          onClick={reactionPanel.openChecker}
          title="Check the price reaction for any symbol and time"
        >
          Check reaction
        </UiButton>
        <div className="cal-seg" role="group" aria-label="Range">
          {rangeOptions.map(([v, label]) => (
            <button
              key={v}
              type="button"
              className={curRange === v ? "on" : ""}
              onClick={() => switchRange(v)}
            >
              {label}
            </button>
          ))}
        </div>
        <div className="cal-seg" role="group" aria-label="View">
          {(["calendar", "table"] as const).map((m) => (
            <button
              key={m}
              type="button"
              className={mode === m ? "on" : ""}
              onClick={() => switchMode(m)}
            >
              {m === "calendar" ? "Calendar" : "Table"}
            </button>
          ))}
        </div>
      </div>

      <CalendarFilters
        filters={filters}
        onChange={patch}
        onClear={clear}
        currencies={currencyOptions}
        sources={sourceOptions}
        shown={visible.length}
        total={ranged.length}
      />

      {q.error && !q.data ? (
        <ErrorNote error={q.error} onRetry={reload} />
      ) : null}

      {mode === "table" ? (
        <div className="cal-board">
          <CalendarTable
            items={visible}
            loading={q.loading}
            fromMs={fromMs}
            toMs={toMs}
            onSelect={onSelect}
            horizon={rxHorizon}
            reactionFilter={rxFilter}
          />
        </div>
      ) : (
        <div className="cal-board">
          <div className="cal-weekdays">
            {WEEKDAYS.map((d) => (
              <div key={d}>{d}</div>
            ))}
          </div>
          <div
            className={`cal-grid ${view}`}
            ref={gridRef}
            style={{
              gridTemplateRows: `repeat(${range.days.length / 7}, minmax(0, 1fr))`,
            }}
          >
            {range.days.map((d) => {
              const key = dayKey(d.getTime());
              const items = byDay.get(key) ?? [];
              const limit = view === "week" ? items.length : fit;
              const shown =
                items.length > limit
                  ? items.slice(0, Math.max(0, limit - 1))
                  : items;
              const more = items.length - shown.length;
              // Week view: a NOW line in today's column, between the items already past and those still to come.
              const nowAt =
                view === "week" && key === todayKey
                  ? items.findIndex((n) => itemTime(n) >= now)
                  : -2;
              const nowPos = nowAt === -1 ? items.length : nowAt;
              const outside =
                view === "month" && d.getMonth() !== anchor.getMonth();
              const cls = `cal-cell${key === todayKey ? " today" : ""}${outside ? " outside" : ""}${d.getDay() === 0 || d.getDay() === 6 ? " weekend" : ""}`;
              return (
                <div key={key} className={cls}>
                  <div className="cal-day">
                    <span className="cal-daynum">
                      {d.getDate() === 1 && view === "month"
                        ? `${MONTHS[d.getMonth()].slice(0, 3)} 1`
                        : d.getDate()}
                    </span>
                  </div>
                  <div className="cal-items">
                    {shown.map((n, i) => (
                      <Fragment key={n.id}>
                        {i === nowPos ? (
                          <div className="cal-nowline" title="Now">
                            <span>{fmtTime(now)}</span>
                          </div>
                        ) : null}
                        <Chip
                          item={n}
                          onSelect={onSelect}
                          now={now}
                          isNext={n.id === nextId}
                        />
                      </Fragment>
                    ))}
                    {nowPos === shown.length ? (
                      <div className="cal-nowline" title="Now">
                        <span>{fmtTime(now)}</span>
                      </div>
                    ) : null}
                    {more > 0 ? (
                      <button
                        type="button"
                        className="cal-more"
                        onClick={() => setDayOpen(key)}
                      >
                        +{more} more
                      </button>
                    ) : null}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {mode === "calendar" && dayOpen && openDate ? (
        <Modal
          onClose={() => setDayOpen(null)}
          title={openDate.toLocaleDateString("en-US", {
            weekday: "long",
            month: "long",
            day: "numeric",
            year: "numeric",
          })}
        >
          <div className="cal-daylist">
            {openItems.map((n) => (
              <Chip
                key={n.id}
                item={n}
                now={now}
                isNext={n.id === nextId}
                onSelect={(item) => {
                  setDayOpen(null);
                  onSelect(item);
                }}
              />
            ))}
          </div>
        </Modal>
      ) : null}
    </main>
  );
}
