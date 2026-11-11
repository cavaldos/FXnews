# Notion Column Definitions

This document defines the current Notion schema used by the project and marks which columns are core, optional, or legacy.

Latest schema decisions accepted for the refactor:
- Drop `Source` and keep `Source URL` as the canonical provenance field.
- Keep `Recommended Action` as the execution hint paired with `Bias`.
- Drop `Verification Status` and `Verification Notes` from Notion.
- Keep `Check` as the validation verdict field and `Result` as the compact audit detail.
- Drop `Analysis Status`, `AI Consensus Score`, `Risk Level`, and `Per-Model Results` from Notion.
- Keep Notion lean: store signal inputs, core metadata, one actionable trade hint, and one validation verdict.

Active database:
- Name: `News Trade Signals (1)`
- Database ID: `319f312e-ab25-800d-88ba-c8db22538fe6`
- Columns found: `20`

## 1. Core columns

These are the columns the project actively uses for signal creation, analysis, and result tracking.

| Column | Type | Used by code | Definition | Recommendation |
| --- | --- | --- | --- | --- |
| `Event` | `title` | Yes | Primary signal title. Example: `US jobs data forces risk-off move`. | Keep |
| `Symbol` | `multi_select` | Yes | Trade instruments tied to the event. Should contain the final MT5 symbol set used for trading and result checks. | Keep, but clean options |
| `Time (ET)` | `date` | Yes | Canonical event timestamp in New York time. This is the main time field used by pipeline logic. | Keep |
| `Bias` | `select` | Yes | Directional expectation for the signal: bullish, bearish, or neutral. | Keep |
| `Impact` | `select` | Yes | Event severity: high, medium, low. | Keep |
| `Check` | `select` | Yes | Final real-market verdict after validation: `Win`, `Loss`, or `Tie` when symbol-level outcomes are evenly split. | Keep |
| `Source URL` | `url` | Yes | Original article or calendar source link. Used for dedupe and audit trail. | Keep |
| `Details` | `rich_text` | Yes | Free-text explanation of the signal, context, and reasoning. | Keep |
| `Source` | `select` | Yes | Publisher or source family: Reuters, Bloomberg, CNBC, WSJ, Fed, Other. This is currently populated by code, but duplicates information that can usually be inferred from the URL. | Remove in refactor |
| `Status` | `select` | Yes | Manual workflow status for the signal lifecycle: new, watching, traded, skipped. | Keep |
| `Result` | `rich_text` | Yes | Compact intraday validation detail, including per-symbol outcomes and the majority roll-up used for `Check`. | Keep |

## 2. Phase 2 AI analysis columns

These columns belong to the AI consensus layer.

| Column | Type | Used by code | Definition | Recommendation |
| --- | --- | --- | --- | --- |
| `AI Consensus Score` | `number` | Yes | Numeric confidence score from the multi-model consensus process. | Remove in refactor |
| `Per-Model Results` | `rich_text` | Yes | Compact audit log of each model's output plus the final consensus. Useful for debugging, but not part of the target Notion schema. | Remove in refactor |
| `Analysis Status` | `select` | Yes | AI processing state: pending, analyzed, conflict, re-analyze. | Remove in refactor |
| `Recommended Action` | `select` | Yes | Final trade action for execution: strong buy, buy, hold, sell, strong sell. This complements `Bias` by telling the trader what to do. | Keep |
| `Risk Level` | `select` | Yes | Final risk rating from the consensus layer. | Remove in refactor |

## 3. Display and helper columns

These columns are not part of the core trading logic, but they can still be useful in the Notion UI.

| Column | Type | Used by code | Definition | Recommendation |
| --- | --- | --- | --- | --- |
| `Time (NY)` | `formula` | No | Display-only formatting of `Time (ET)` in New York time so it matches TradingView when chart timezone is set to `New York`. | Optional |

Timezone note:
- System standard timezone is `America/New_York`
- `Time (ET)` is the canonical field used by generation and validation
- TradingView should be set to `New York` when comparing directly with Notion rows
| `id` | `unique_id` | No | Notion-generated row identifier for UI convenience. The app uses Notion `page.id`, not this field. | Optional |

## 4. Legacy verification columns

These come from the older verification flow and are only partly aligned with the current project direction.

| Column | Type | Used by code | Definition | Recommendation |
| --- | --- | --- | --- | --- |
| `Verification Status` | `select` | Legacy | Old forex-only verification label. Removed from active Notion schema. | Remove |
| `Verification Notes` | `rich_text` | Legacy | Old forex-only verification notes. Removed from active Notion schema. | Remove |

Important mismatch:
- The code still supports `Verification Score`, but the active Notion database does not have that column.
- If Phase 3 stays in the refactor, either add `Verification Score` back or remove that code path.

## 5. Suspected redundant columns

These columns do not currently add much value to the active workflow.

| Column | Type | Why it looks redundant | Recommendation |
| --- | --- | --- | --- |
| `Source` | `select` | A single label is weak provenance and adds little if `Source URL` already exists. It also breaks down when one signal is built from multiple articles. | Remove |
| `Analysis Status` | `select` | Workflow state can live outside Notion or in page content; it does not need to be a top-level property in the lean schema. | Remove |
| `AI Consensus Score` | `number` | Useful for analysis, but not necessary in the final lean Notion schema. | Remove |
| `Per-Model Results` | `rich_text` | Model-by-model traces are useful for debugging but too verbose for the final lean schema. | Remove |
| `Recommended Action` | `select` | It gives the explicit execution hint that `Bias` alone does not fully communicate. It should be kept aligned with `Bias`. | Keep |
| `Risk Level` | `select` | Risk can be inferred or stored elsewhere; not required as a top-level Notion property. | Remove |
| `Result` | `rich_text` | This is now the compact validation audit trail paired with `Check`. | Keep |
| `CreateAt` | `date` | Not used by code. It also duplicates what a Notion `Created time` property would do better. | Replace with native `Created time` or remove |

## 6. Current allowed values in Notion

### `Bias`
- `🟢 Bullish`
- `🔴 Bearish`
- `🟡 Neutral`

### `Impact`
- `🔥 High`
- `⚡ Medium`
- `💧 Low`

### `Status`
- `📋 New`
- `👀 Watching`
- `✅ Traded`
- `⏭️ Skipped`

### `Check`
- `Win`
- `Loss`
- `Tie`

### `Analysis Status`
- `⏳ Pending`
- `✅ Analyzed`
- `⚠️ Conflict`
- `🔄 Re-analyze`

Note:
- This column exists in the current database, but the refactor direction is to remove it.

### `Recommended Action`
- `🟢 Strong Buy`
- `🔵 Buy`
- `⚪ Hold`
- `🟠 Sell`
- `🔴 Strong Sell`

Note:
- This column stays in the active schema as the explicit execution hint paired with `Bias`.

### `Risk Level`
- `🟢 Low`
- `🟡 Medium`
- `🔴 High`

Note:
- This column exists in the current database, but the refactor direction is to remove it.

### `Verification Status`
- Legacy field removed from the active database.

## 7. Important refactor notes

### 7.1 Symbol options are polluted by legacy assets

The current `Symbol` select options contain old stock and ETF names such as:
- `SPY`, `QQQ`, `NVDA`, `SMH`, `SOXX`, `DIA`, `IWM`, `EEM`, `FXI`, `XLF`, `GLD`

But the current project rules say signals should only use the MT5 list:
- Forex: `EURUSD`, `GBPUSD`, `USDJPY`, `AUDUSD`, `NZDUSD`, `USDCAD`, `USDCHF`, `GBPJPY`, `EURJPY`, `EURGBP`, `AUDJPY`, `EURAUD`
- Indices: `US500`, `US30`, `NAS100`, `DE40`, `UK100`, `JP225`, `FR40`
- Commodities: `XAUUSD`, `XAGUSD`, `USOIL`, `UKOIL`, `NGAS`
- Crypto: `BTCUSD`, `ETHUSD`

Recommendation:
- Clean the `Symbol` options so Notion only exposes the current MT5 instrument list.

### 7.2 Validation fields are now simplified

The database should now keep only:
- `Check` for the final win/loss/tie verdict
- `Result` for the compact per-symbol audit detail

Recommendation:
- remove `Verification Status`
- remove `Verification Notes`
- keep validation logic aligned around `Check` + `Result`

### 7.3 `Source` should not be a first-class column

Because one signal may be assembled from multiple articles or feeds:
- `Source` as a single select field is too lossy
- `Source URL` should be treated as the canonical provenance field for now
- if multi-source provenance becomes important later, store extra links inside `Details` or page content rather than reviving `Source`

### 7.4 `Recommended Action` should stay aligned with `Bias`

For this project, the user's direction is:
- `Bias` remains the directional thesis
- `Recommended Action` is the explicit execution hint
- the two fields should agree, with `Recommended Action` making the next step clearer for the trader

### 7.5 Output fields should stay out of the lean Notion schema

The current direction is to treat Notion primarily as the signal registry, not the full processing ledger.

That means these output-oriented fields should be removed from top-level properties:
- `Analysis Status`
- `AI Consensus Score`
- `Per-Model Results`
- `Risk Level`

If needed later, their values can live in:
- page body content
- exported reports
- logs / JSON artifacts
- a separate analysis database

## 8. Recommended target schema after refactor

### Keep as required
- `Event`
- `Symbol`
- `Time (ET)`
- `Bias`
- `Impact`
- `Recommended Action`
- `Check`
- `Source URL`
- `Details`
- `Status`
- `Result`

### Keep as optional
- `Time (NY)`
- `id`

### Drop or replace
- `Source`
- `Analysis Status`
- `AI Consensus Score`
- `Per-Model Results`
- `Risk Level`
- `Verification Status`
- `Verification Notes`
- `CreateAt`
- legacy `Symbol` options that are not in the MT5 list
- `Verification Score` code path if that column will not return

## 9. Suggested one-line definitions for product use

If you want short human-friendly definitions inside Notion docs, use these:

- `Event`: The name of the market-moving event or headline.
- `Symbol`: The MT5 instruments most directly affected by the event.
- `Time (ET)`: The official event time in New York time.
- `Bias`: The expected price direction before or after analysis.
- `Impact`: The estimated market importance of the event.
- `Recommended Action`: The concrete trade stance to take right now.
- `Check`: Final validation verdict after comparing real price action with the AI call.
- `Source URL`: The original article or calendar link.
- `Details`: Reasoning, context, and trade notes for the signal.
- `Result`: Compact per-symbol validation detail behind `Check`.
- `Status`: The manual workflow state of the signal.
- `Time (NY)`: New York chart-time display of the event time.
- `id`: Notion's row number for manual tracking.
- `CreateAt`: Manual created date field, currently not used by the app.

## 10. Updated target schema chosen so far

Based on the latest refactor decisions, the preferred lean schema is:

- `Event`
- `Symbol`
- `Time (ET)`
- `Bias`
- `Impact`
- `Recommended Action`
- `Check`
- `Source URL`
- `Details`
- `Status`
- `Result`

Optional support columns:
- `Time (NY)`
- `id`

Columns now marked for removal:
- `Source`
- `Analysis Status`
- `AI Consensus Score`
- `Per-Model Results`
- `Risk Level`
- `Verification Status`
- `Verification Notes`
- `CreateAt`
