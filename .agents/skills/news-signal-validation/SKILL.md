---
name: news-signal-validation
description: "Check whether historical news-based trade calls were correct by fetching symbol price data and comparing the move against the original bias over a defined time window."
---

# News Signal Validation

Use this skill when the goal is to audit past calls:
- load an old signal
- fetch price data for its symbol(s)
- compare the move after the event against the original bias
- decide whether the call was correct, incorrect, or inconclusive

This skill is separate from signal generation. It does not create new trade ideas.

Validation-owned Notion fields now are:
- `Check` -> final verdict for real market move vs AI call (`Win`, `Loss`, `Tie`)
- `Result` -> compact per-symbol audit text

Legacy fields removed from Notion:
- `Verification Status`
- `Verification Notes`

## Input Requirements

Each signal should have at least:
- `Event`
- `Symbol`
- `Time (ET)`
- `Bias`

Helpful but optional:
- `Impact`
- `Source URL`
- `Details`

## Core Validation Logic

Validate the original bias against actual market movement in the selected symbol.

Default intraday rules:
- start from signal time `Time (ET)`
- prefetch OHLC data for all related symbols before scoring any signal
- save the fetched raw OHLC files for the run under `verification-data/validation-runs/<run-id>/`
- use the first tradable bar at or after the signal time as baseline
- allow an extended lookahead so after-hours / weekend signals can wait for the next market session
- default reaction window: `90` minutes
- default threshold: `0.05%`
- determine the actual post-event move first: `up`, `down`, or inconclusive
- compare that observed move with the Notion `Bias`
- otherwise mark `Pass` / inconclusive

Multi-symbol rule:
- compare the number of symbol-level `win` vs `loss` outcomes
- if `win` count > `loss` count -> signal-level `Check = Win`
- if `loss` count > `win` count -> signal-level `Check = Loss`
- if `win` count = `loss` count and both sides are non-zero -> signal-level `Check = Tie`
- if no symbol is decisive (`0-0`), leave `Check` empty and keep the audit detail in `Result`

## What to Return

Preferred output for the refactor:
- update Notion `Check`
- update Notion `Result`
- JSON report
- CLI summary
- rebuild loss-memory after meaningful validation changes so generation can learn from repeated losses

## Commands

Current validation scripts for this skill.

Intraday reaction check:

```bash
npm run signals:result
npm run signals:result-all
```

Behavior:
- writes `Check` and `Result` back to Notion by default
- use `--dry-run` to preview without writing
- still prefetches and saves raw OHLC data locally before validation

Direct script usage:

```bash
node .agents/skills/news-signal-validation/scripts/check-result.js --pending --dry-run
node .agents/skills/news-signal-validation/scripts/check-result.js --id <pageId>
node .agents/skills/news-signal-validation/scripts/check-result.js --pending --window-minutes 120
```

Legacy forex-only verification still exists but should be treated as older workflow support:

```bash
npm run signals:verify
npm run signals:verify-dry
npm run signals:verify-report
```

## Validation Workflow

### 1. Load target signals

Pick one of:
- all pending historical signals
- one specific signal by page id
- a filtered subset by date / event type / symbol

### 2. Normalize the bias

Interpret bias as direction on the selected symbol:
- `Bullish` -> expected up move
- `Bearish` -> expected down move
- `Neutral` -> no directional claim, usually `Pass`

### 3. Prefetch market data

For each symbol:
- map MT5 symbol to provider symbol
- fetch OHLC candles covering all relevant signal windows first
- save raw files locally for audit before comparing against Notion
- prefer `1m` candles, with fallback to higher intervals if needed

### 4. Check the actual move after the event

Evaluate:
- baseline close
- max high inside the window
- min low inside the window
- upside and downside percentage move from baseline

Then derive the observed move:
- `up` if upside move is the dominant decisive move
- `down` if downside move is the dominant decisive move
- inconclusive if the move is flat, tied, or there is no usable bar

### 5. Compare against Notion bias

For each symbol:
- observed `up` vs `Bullish` -> `win`
- observed `down` vs `Bearish` -> `win`
- opposite direction -> `loss`
- inconclusive -> `pass`

Then classify each symbol:
- `win`
- `loss`
- `pass`

Then roll up to Notion:
- `Check = Win`
- `Check = Loss`
- `Check = Tie` when symbol wins and losses are equal
- no `Check` value when the event is inconclusive with no decisive symbol

### 6. Report clearly

A good validation result should include:
- signal id / event
- original bias
- symbols checked
- window used
- threshold used
- prefetch run directory / manifest
- per-symbol outcome
- final `Check` verdict when there is a majority
- `Result` detail text for audit trail

## Validation Rules

- do not look beyond the configured window
- do not use future information outside the validation period
- if the signal time is invalid or still in the future, leave `Check` empty
- if the symbol has no supported data source, leave `Check` empty and keep the reason in `Result`
- keep the raw reason when data fetch fails

## File Map

- Intraday validation: `src/verification/intradayResultChecker.js`
- Yahoo price provider: `src/prices/yahooChart.js`
- MT5 -> Yahoo symbol map: `src/prices/mt5ToYahoo.js`
- Legacy verification: `src/verification/signalVerifier.js`
- Legacy verify CLI: `.agents/skills/news-signal-validation/scripts/verify-signals.js`
- Intraday check CLI: `.agents/skills/news-signal-validation/scripts/check-result.js`

Base directory for this skill: file:///Users/bourbon/Code/Workspace/NewsTrade/.agents/skills/news-signal-validation
