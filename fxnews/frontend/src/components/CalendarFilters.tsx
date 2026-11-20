import { useEffect, useRef, useState } from "react";
import type { CalFilters } from "../lib/calFilters";
import { activeFilterCount, IMPACTS } from "../lib/calFilters";
import { flag } from "../lib/format";

interface MultiProps {
  label: string;
  options: string[];
  selected: string[];
  onChange: (next: string[]) => void;
  renderOption?: (v: string) => string;
}

/** Notion-style filter chip: click to open a checklist popover. Empty selection = no filtering. */
function MultiSelect({
  label,
  options,
  selected,
  onChange,
  renderOption,
}: MultiProps) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node))
        setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const toggle = (v: string) =>
    onChange(
      selected.includes(v) ? selected.filter((x) => x !== v) : [...selected, v],
    );
  // Keep selected values listed even if the current range has no such item.
  const all = [...new Set([...options, ...selected])];

  return (
    <div className="cf-multi" ref={ref}>
      <button
        type="button"
        className={`cf-btn${selected.length ? " on" : ""}`}
        aria-haspopup="listbox"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        {label}
        {selected.length ? (
          <span className="cf-count">{selected.length}</span>
        ) : null}
        <span className="cf-caret" aria-hidden>
          ▾
        </span>
      </button>
      {open ? (
        <div
          className="cf-pop"
          role="listbox"
          aria-label={label}
          aria-multiselectable
        >
          {all.length === 0 ? <div className="cf-empty">No options</div> : null}
          {all.map((v) => (
            <label key={v} className="cf-opt">
              <input
                type="checkbox"
                checked={selected.includes(v)}
                onChange={() => toggle(v)}
              />
              <span>{renderOption ? renderOption(v) : v}</span>
            </label>
          ))}
          {selected.length ? (
            <button
              type="button"
              className="cf-reset"
              onClick={() => onChange([])}
            >
              Reset
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

interface Props {
  filters: CalFilters;
  onChange: (p: Partial<CalFilters>) => void;
  onClear: () => void;
  /** Option lists derived from the loaded data. */
  currencies: string[];
  sources: string[];
  shown: number;
  total: number;
}

/** Filter bar shared by the Calendar and Table views. */
export function CalendarFilters({
  filters,
  onChange,
  onClear,
  currencies,
  sources,
  shown,
  total,
}: Props) {
  const active = activeFilterCount(filters);
  return (
    <div className="cf-bar">
      <input
        type="search"
        className="cf-search"
        placeholder="Search title…"
        aria-label="Search title"
        value={filters.q}
        onChange={(e) => onChange({ q: e.target.value })}
      />
      <div className="cal-seg" role="group" aria-label="Show">
        <button
          type="button"
          className={filters.showCal ? "on" : ""}
          onClick={() => onChange({ showCal: !filters.showCal })}
        >
          Economic
        </button>
        <button
          type="button"
          className={filters.showNews ? "on" : ""}
          onClick={() => onChange({ showNews: !filters.showNews })}
        >
          Headlines
        </button>
      </div>
      <MultiSelect
        label="Impact"
        options={IMPACTS}
        selected={filters.impacts}
        onChange={(impacts) => onChange({ impacts })}
      />
      <MultiSelect
        label="Currency"
        options={currencies}
        selected={filters.currencies}
        onChange={(c) => onChange({ currencies: c })}
        renderOption={(c) => `${flag(c)} ${c}`.trim()}
      />
      <MultiSelect
        label="Source"
        options={sources}
        selected={filters.sources}
        onChange={(s) => onChange({ sources: s })}
      />
      <div className="cal-seg" role="group" aria-label="Actual">
        <button
          type="button"
          className={filters.hasActual ? "on" : ""}
          aria-pressed={filters.hasActual}
          onClick={() => onChange({ hasActual: !filters.hasActual })}
        >
          Has actual
        </button>
      </div>
      {active > 0 ? (
        <button type="button" className="cf-clear" onClick={onClear}>
          Clear · {active} {active === 1 ? "filter" : "filters"}
        </button>
      ) : null}
      <span className="spacer" />
      <span className="cf-rows">
        {shown === total ? `${total} rows` : `${shown} of ${total} rows`}
      </span>
    </div>
  );
}
