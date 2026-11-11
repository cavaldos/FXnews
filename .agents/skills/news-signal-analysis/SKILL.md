---
name: news-signal-analysis
description: "Fetch market-moving news/calendar events, map them to MT5 symbols, and produce a directional bias for whether to look long or short."
---

# News Signal Analysis

Use this skill when the goal is to turn financial news into a clean trade idea:
- get the news
- pick the correct primary symbol
- decide whether the bias is bullish or bearish
- translate that bias into a practical long/short stance

Default operating mode for this skill:
- prefer only high-trade-value setups
- exclude neutral signals by default
- if an item initially looks neutral, run a deeper directional pass first
- if the bias is still unclear after deeper analysis, skip the item instead of saving noise
- only save a signal when the directional bias is clear enough to trade, not just slightly leaning
- when the same story appears across multiple trusted outlets, merge that evidence into one trade idea instead of treating each URL as a separate signal

Current project direction:
- Notion should stay lean
- top-level properties should store the signal definition plus an execution hint in `Recommended Action`

## Notion Target Schema

Required columns:
- `Event`
- `Symbol`
- `Time (ET)`
- `Bias`
- `Impact`
- `Recommended Action`
- `Source URL` (primary canonical link)
- `Details`
- `Status`

Optional display-only columns:
- `Time (NY)`
- `id`
- `Check` (filled later by validation, not at signal creation time)

Do not rely on these columns going forward:
- `Source`
- `Analysis Status`
- `AI Consensus Score`
- `Per-Model Results`
- `Risk Level`
- `Result`

## Bias to Trade Mapping

Interpret `Bias` directly as the trade direction on the stored symbol:
- `Bullish` -> look for `long`
- `Bearish` -> look for `short`
- `Neutral` -> no trade / wait

Do not create a second action field just to say long or short. `Bias` is the action hint.

Practical rule:
- the preferred output of this skill is directional signals only (`Bullish` or `Bearish`)
- `Neutral` is a fallback state for internal reasoning, not the desired saved output

## Recommended Action Mapping

Always fill `Recommended Action` when saving a signal.

Default execution mapping:
- `Bullish` + strong high-impact/corroborated setup -> `Strong Buy`
- `Bullish` directional setup -> `Buy`
- `Bearish` + strong high-impact/corroborated setup -> `Strong Sell`
- `Bearish` directional setup -> `Sell`
- `Neutral` or unclear -> `Hold` and skip saving by default

Rule:
- `Bias` explains the directional thesis
- `Recommended Action` tells the trader what to do right now in the database

## Allowed MT5 Symbols

Forex:
- `EURUSD`, `GBPUSD`, `USDJPY`, `AUDUSD`, `NZDUSD`, `USDCAD`, `USDCHF`, `GBPJPY`, `EURJPY`, `EURGBP`, `AUDJPY`, `EURAUD`

Indices:
- `US500`, `DE40`, `UK100`, `JP225`, `FR40`

Commodities:
- `XAUUSD`, `XAGUSD`, `USOIL`, `NGAS`

Crypto:
- `BTCUSD`, `ETHUSD`

Rules:
- use only directly affected symbols
- keep 1 primary symbol per signal by default
- prefer the clearest transmission channel from headline to instrument
- do not keep duplicate same-family symbols in one signal; keep the higher-validated representative only
- current family winners from validation are `USOIL` over `UKOIL`, and `US500` over `NAS100` / `US30`
- use the loss-memory file to penalize or skip headline patterns that repeatedly lost in validation

## Analysis Workflow

### 1. Gather news

Collect market-moving items from:
- Reuters
- Bloomberg
- CNBC
- WSJ
- FT
- AP
- Barron's
- Nikkei
- Axios / Business Insider / CNN Business when the story is genuinely market-moving
- central banks / macro calendars
- official macro agencies like BLS / BEA / EIA / OPEC / Treasury when they are the primary source of the catalyst

Cross-source rule:
- one market-moving story can be validated from multiple trusted sources
- do not require a single-source article if Reuters/Bloomberg/CNBC/WSJ all cover the same catalyst differently
- keep one primary URL in `Source URL`
- put corroborating sources or extra links into `Details`
- if multiple sources materially disagree on direction or transmission, skip the signal until the bias is clear

Useful commands:

```bash
npm run signals:dry-run
npm run signals:collect
npm run signals:tomorrow -- --dry-run
```

Behavior of these commands now:
- default minimum impact is `high`
- neutral signals are excluded unless you explicitly pass `--include-neutral`

Current scripts for this skill:
- `.agents/skills/news-signal-analysis/scripts/collect-signals.js`
- `.agents/skills/news-signal-analysis/scripts/tomorrow-brief.js`
- `.agents/skills/news-signal-analysis/scripts/add-signal.js`
- `.agents/skills/news-signal-analysis/scripts/query-signals.js`

### 2. Convert the headline into a signal

For each item, fill in:
- `Event`: short, readable headline
- `Symbol`: 1 MT5 symbol with the clearest transmission channel
- `Time (ET)`: event time in New York time
- `Bias`: bullish / bearish
- `Impact`: high / medium / low
- `Recommended Action`: strong buy / buy / hold / sell / strong sell
- `Source URL`: canonical primary link for the story
- `Details`: why the bias exists, what matters, what can invalidate it, and any corroborating sources if relevant
- `Status`: default `📋 New` or `👀 Watching`

### 3. Assign bias correctly

Bias rules:
- use `Bullish` only when the selected symbol should rise
- use `Bearish` only when the selected symbol should fall
- try hard to resolve mixed evidence before accepting `Neutral`
- if the signal still cannot be made directional with reasonable confidence, skip it
- a weak directional lean is not enough; the setup should be clear enough that you would actually consider trading it
- every saved signal must also carry a concrete `Recommended Action`, not just a bias label

Neutral-resolution rules:
- identify the dominant tradable theme first (oil, gold, tech, crypto, FX macro)
- prefer symbols that share the same directional path instead of mixing conflicting assets
- use event-specific overrides for things like oil supply shocks, safe-haven flows, tech selloffs, and USD macro surprises
- for calendar events without an actual-vs-forecast surprise yet, do not force a direction; skip them by default

Examples:
- hotter-than-expected US inflation -> often bearish `US500`, bullish USD, mixed for gold depending on rates impulse
- weaker-than-expected US jobs -> often bullish `XAUUSD`, bearish USD, risk-off for indices if recession fear dominates
- oil supply shock -> bullish `USOIL` / `UKOIL`
- dovish ECB surprise -> bullish `DE40`, bearish `EURUSD`

### 4. Save or report

To save signals:

```bash
npm run signals:collect
```

To add one manually:

```bash
node .agents/skills/news-signal-analysis/scripts/add-signal.js '{
  "event": "Fed official signals slower balance-sheet runoff",
  "symbols": ["US500", "XAUUSD", "EURUSD"],
  "time": "2026-03-09T14:00:00",
  "bias": "🟢 Bullish",
  "impact": "🔥 High",
  "recommendedAction": "🟢 Strong Buy",
  "url": "https://example.com/article",
  "details": "Dovish liquidity signal supports risk assets and weakens USD.",
  "status": "📋 New"
}'
```

## Signal Quality Rules

- prefer one clear signal over three weak ones
- only keep signals with real execution value; high-impact directional setups come first
- avoid assigning symbols just because they are popular
- avoid storing multiple symbols from the same family when one higher-win-rate representative already exists
- avoid mixing symbols that would logically move in opposite directions under the same headline
- let historical loss-memory reduce confidence in repeated failure patterns such as generic earnings recaps or late oil-spike commentary
- do not store duplicate items; dedupe using the primary URL plus any corroborating URLs tied to the same story
- `Details` should explain the transmission mechanism, not repeat the headline
- if a signal stays neutral after deeper analysis, drop it instead of cluttering Notion
- if corroborating coverage exists, prefer one merged high-conviction signal over several one-source copies

## File Map

- News fetch: `src/news/tavily.js`
- Signal generation: `src/analysis/newsSignalGenerator.js`
- Calendar fetch: `src/news/forexfactoryCalendar.js`
- Calendar signal mapping: `src/analysis/calendarSignalGenerator.js`
- Pipeline: `src/pipeline/newsTradePipeline.js`
- Tomorrow brief: `src/pipeline/tomorrowBriefPipeline.js`
- Notion integration: `src/news-trade-db.js`
- CLI scripts: `.agents/skills/news-signal-analysis/scripts/`

Base directory for this skill: file:///Users/bourbon/Code/Workspace/NewsTrade/.agents/skills/news-signal-analysis
