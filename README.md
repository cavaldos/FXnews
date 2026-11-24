# NewsTrade (FXNews)

A news-driven Forex analysis system. It collects economic-calendar events and headlines, maps them to currency
pairs, scores their likely direction with deterministic rules, asks an LLM for a trading scenario, pushes the result
to a live dashboard and Discord, and later checks each prediction against the actual price move. Every verified
outcome is stored as a long-term "market memory" that future analyses can retrieve.

> This is a research and decision-support tool, not financial advice. LLM output and rule scores can be wrong.
> Verify every idea yourself before trading.

The project has two parts at the repository root:

| Part | Stack | Default port |
|---|---|---|
| `backend/` | Python 3.12+, FastAPI, SQLAlchemy, SQLite (WAL), httpx, pydantic | 8000 |
| `frontend/` | React 19, Vite, TypeScript, lightweight-charts | 5173 |

[`docs/README.vi.md`](docs/README.vi.md) (in Vietnamese) holds the detailed field-level API contract and the
environment reference.

---

## 1. Features

### 1.1 Data collection
- **Economic calendar**: ForexFactory's public JSON feed (this week and next), polled every `FF_POLL_MINUTES`
  (default 10). The feed has forecast and previous values but no actual value, so surprise is only known when
  another source reports it.
- **Headlines**: generic RSS/Atom ingestion (FXStreet, CNBC Finance by default; extendable with `RSS_FEEDS`),
  polled every `RSS_POLL_MINUTES` (default 3).
- **Deduplication** by content hash; each item is normalised to a `RawNews` record before storage.
- **Pre-event alerts**: high-impact events starting within `PRE_EVENT_ALERT_MINUTES` (default 30) trigger an
  automatic "what if actual beats/misses forecast" analysis.

### 1.2 Mapping and rule-based scoring
- `analysis/mapping.py` maps each item to currencies and then to the related pairs, with the sign (base or quote
  side) of each currency's effect on the pair.
- `analysis/rules.py` produces a deterministic bias from surprise (actual vs forecast, with higher- or
  lower-is-better semantics per indicator) and from keyword rules. It also computes a `recommended_action`.
- Rule output is stored with each item (`rule_bias`) and is always available, even without an LLM.

### 1.3 LLM advisor
- `analysis/advisor.py` builds a context from the news item, the calendar, and the latest candles, then asks an LLM
  for JSON: bias, confidence, entry idea, invalidation level, and scenarios, per pair.
- Three analysis kinds:

| Kind | Trigger | Output |
|---|---|---|
| `pre_event` | High-impact calendar event is close | If-actual-above / if-actual-below scenarios per pair |
| `headline` | New headline matches a watched currency | Per-pair bias, confidence, entry idea |
| `ad_hoc` | User asks in the UI (`POST /api/advice`) | Advice for a pair plus a free-form question |

- Automatic LLM spending is capped by `HEADLINE_COOLDOWN_MINUTES` (per currency), `MAX_HEADLINE_ANALYSES_PER_CYCLE`,
  `MAX_AUTO_LLM_CALLS_PER_HOUR`, and a per-event retry limit (3 attempts).
- Analyses and their signals are stored in SQLite and streamed to the UI.

### 1.4 Multi-provider LLM layer
- `app/llm/` is a provider-agnostic client: **OpenRouter**, **Groq**, **Hive**, the **Claude Code CLI**, and
  **OpenCode**. Models are addressed as `provider:model`.
- Configuration lives in `backend/llm.yaml` (template `llm.ex.yaml`, never committed). Each provider sets `model`,
  `thinking` level, `rotation` (`failover` or `round_robin`), and a list of `api_keys` or `accounts`.
- Keys or accounts that are refused (401/402/403, rate or usage limits) are cooled down and skipped, then reused
  automatically. Environment variables such as `OPENROUTER_API_KEYS` are merged into the YAML list.

### 1.5 Live delivery
- **Server-Sent Events** at `GET /api/stream` push `news` and `analysis` events. `app/events.py` is a small in-process
  pub/sub broadcaster.
- **Discord** and **Telegram** notifications (`app/notify/`), configured in `backend/notify.yaml` (same shape as
  `llm.yaml`): a list of named accounts per channel, every enabled account gets every message. Two rules decide
  what is sent: a new headline at or above `min_impact`, and a reminder `pre_event_lead_minutes` before a
  high-impact calendar release. The LLM trade ideas are filtered by `min_confidence` and `notify_pre_event`.
  The **Notifications** page of the UI edits the file (and tests each account); `POST /api/notify/test` sends a
  test message.

### 1.6 Market data
- **Candles**: TradingView's chart websocket is the primary source, so candles match the chart a user sees.
  Yahoo Finance is the first fallback and Twelve Data (with `TWELVEDATA_API_KEY`) the second. A stale-cache safety
  net serves the last good data when upstream rate-limits.
- Results are cached by TTL per interval, and concurrent identical requests share one upstream call
  (single-flight).
- **Quotes** endpoint for the current price of several symbols.
- The frontend falls back to an embedded TradingView widget when the candle API fails.

### 1.7 Reaction measurement and backtest
- `app/market/reaction.py` and `app/backtest/outcomes.py` compute what price did after an event: return and an
  `up` / `down` / `sideways` label at 5m, 15m, 1h, 4h, and 1d, plus the first-hour range. Thresholds depend on
  asset class.
- **Backtest** (`app/backtest/`) builds a dataset of historical events and headlines (`ingest.py`, `sources.py`,
  `headline_history.py`: investinglive sitemap, FXStreet via Wayback, CNBC sitemap, GDELT, optional Marketaux and
  Finnhub). It then runs one or more LLM models over each event (`predict.py`) and scores them against the recorded
  outcomes (`stats.py`).
- Long-running jobs (backfill, predictions) run as background tasks tracked in an in-memory job registry
  (`jobs.py`, max 30 jobs). The UI polls their status and can cancel them.
- **LLM Lab** lets you compare providers and prompts interactively.

### 1.8 Long-term market memory
- `app/memory/` records only **observed facts**: the event (title, time, impact, actual, forecast, previous) and
  the price outcome for each related symbol. It never records predictions, bias, or signals, so model opinions
  cannot feed back into memory.
- Facts are written deterministically without an LLM. A keyword filter drops text containing opinion words such as
  `bullish` or `confidence`.
- Storage: SQLite tables `memory_nodes`, `memory_edges`, and `memory_sync`. Optional Neo4j with Graphiti for
  semantic search (`docker-compose.memory.yml`). Retrieval is hybrid BM25 plus embeddings and has no LLM in the loop.
- **Point-in-time recall**: `recall(..., as_of=T)` returns only facts known by `T`, so backtests cannot see the
  future.

### 1.9 Data management
- The **Data** page and `/api/data` export the database as a consistent SQLite snapshot, or as gzipped JSON with
  optional date range and table selection.
- Import supports `merge` (upsert by natural key, newer row wins, foreign keys remapped) and `replace`. Both run in
  one transaction and snapshot the live DB first.
- Named backups can be created, listed, downloaded, and restored (`/api/data/backups`).

### 1.10 Web interface
Single-page React app with a dashboard (news feed, price chart with news markers, analysis and signal panels,
advice panel, quotes, calendar filters) and separate pages for **Calendar**, **Backtest**, **Memory** (symbol graph
with outcome coloring and sync), **LLM Lab**, and **Data**. UI preferences (symbol, interval, model, theme, font)
persist in `localStorage`.

---

## 2. Architecture

### 2.1 High-level data flow

```
 ┌──────────────── Sources (polled) ─────────────────┐
 │ ForexFactory JSON  (every 10 min)                  │
 │ RSS headlines      (every 3 min)                   │
 │ Market data: TradingView WS → Yahoo → Twelve Data  │
 └───────────────────────┬───────────────────────────┘
                         ▼
 services/ingest.py   normalise → dedupe (hash) → upsert SQLite (news_items)
                         ▼
 analysis/mapping.py  item → currencies → pairs (+ sign)
 analysis/rules.py    deterministic bias + recommended_action
                         ▼
 analysis/advisor.py  context (news + calendar + candles) → LLM (llm/) → JSON
                         ▼
          analyses + signals (SQLite)
          ├──► events.py broadcaster → SSE /api/stream → React UI
          └──► notify/ (Discord + Telegram, backend/notify.yaml)

 Outcome loop:  market/reaction.py + backtest/outcomes.py → bt_outcomes
                         └──► memory/ (facts only) → memory_* tables [+ Neo4j/Graphiti]
```

### 2.2 Process model
- One FastAPI process runs the API and a background **scheduler**, started in the app `lifespan`
  (`services/scheduler.py`). Each job is an `asyncio` loop that catches and logs its own exceptions:
  - `calendar`: ForexFactory poll (first run is aligned to the feed's refresh time)
  - `rss`: headline poll
  - `pre_event`: checks upcoming high-impact events every 60 s
- Shutdown cancels the loops, drains background notification and analysis tasks, and closes the shared HTTP clients.
- Concurrency for LLM analysis is limited by a semaphore (2 at a time). In-memory cost controls (cooldowns, hourly
  budget, retry counters) reset on restart, which is acceptable for a single-user tool.
- Logging: `httpx` and `httpcore` are kept at WARNING so that URLs containing secrets (Discord webhook tokens,
  `apikey` query parameters) never reach the logs.

### 2.3 Backend layout (`backend/app/`)

| Package / module | Responsibility |
|---|---|
| `main.py` | App factory, lifespan, router registration, security middleware |
| `config.py` | Settings from `.env` (pydantic-settings) |
| `db.py`, `models.py` | SQLAlchemy engine (SQLite, WAL), sessions, ORM models |
| `schemas.py` | Pydantic request and response models (the API contract) |
| `events.py` | In-process pub/sub for SSE |
| `security.py` | Host allow-list, CORS, cross-site POST guard |
| `routers/` | HTTP endpoints: `news`, `signals`, `system`, `market`, `reaction`, `backtest`, `llm_chat`, `memory`, `data`, `link_preview` |
| `services/` | `ingest` (pipeline), `scheduler`, `datastore` (export/import/backup), `serializers`, `link_preview` (SSRF-guarded page previews) |
| `sources/` | `forexfactory.py`, `rss.py`, `base.py` (`RawNews` contract) |
| `analysis/` | `mapping`, `rules`, `advisor` (orchestration), `llm` (OpenRouter JSON helper) |
| `llm/` | Multi-provider client: `client.py`, `config.py`, `keys.py`, `accounts.py`, `providers/` (openrouter, groq, hive, claude_code, opencode, openai_compat) |
| `market/` | `candles` (cache and fallbacks), `tradingview` (websocket), `symbols`, `reaction` |
| `notify/` | Discord + Telegram channels, the YAML config, the message bodies |
| `backtest/` | `ingest`, `sources`, `headline_history`, `predict`, `outcomes`, `stats`, `jobs`, `models`, CLI |
| `memory/` | `facts`, `store` (SQLite and Graphiti), `sync`, `view`, CLI |

### 2.4 Database
SQLite file `backend/data/fxnews.db` (WAL mode). Main tables:

| Group | Tables | Contents |
|---|---|---|
| Live | `news_items` | Calendar events and headlines, with mapping and rule bias |
| Live | `analyses`, `signals` | LLM analyses and per-pair trade signals |
| Backtest | `bt_events`, `bt_predictions`, `bt_outcomes` | Historical events, model predictions, measured price outcomes |
| Memory | `memory_nodes`, `memory_edges`, `memory_sync` | Facts graph and sync state |

Schema is created by `init_db()` at startup. The Data page exports and imports the same tables.

### 2.5 HTTP API
All endpoints are under `/api`. Interactive documentation is served by FastAPI at `/docs`.

| Area | Endpoints |
|---|---|
| System | `GET /api/health`, `POST /api/ingest/run`, `GET`/`POST` `/api/notify/settings`, `POST /api/notify/test`, `GET /api/stream` (SSE) |
| News | `GET /api/news`, `GET /api/news/{id}`, `GET /api/calendar`, `POST /api/news/{id}/analyze` |
| Analysis | `GET /api/signals`, `GET /api/analyses`, `GET /api/analyses/{id}`, `POST /api/advice`, `GET /api/models` |
| Market | `GET /api/symbols`, `GET /api/candles`, `GET /api/quotes` |
| Reaction | `GET /api/reaction`, `POST /api/reaction/batch` |
| Backtest | `GET /api/backtest/events`, `GET /api/backtest/events/{id}`, `POST /api/backtest/events/{id}/verify`, `POST /api/backtest/events/{id}/predict`, `GET /api/backtest/events/{id}/prompt`, `POST /api/backtest/backfill`, `POST /api/backtest/predict`, `GET /api/backtest/jobs`, `GET /api/backtest/jobs/{id}`, `POST /api/backtest/jobs/{id}/cancel`, `GET /api/backtest/summary`, `GET /api/backtest/models`, `GET /api/backtest/presets` |
| LLM Lab | `GET /api/llm/models`, `GET /api/llm/presets`, `POST /api/llm/chat` |
| Memory | `GET /api/memory/overview`, `GET /api/memory/graph?symbol=`, `POST /api/memory/sync` |
| Data | `GET /api/data/tables`, `GET /api/data/export`, `POST /api/data/import`, `GET`/`POST /api/data/backups`, `GET /api/data/backups/{name}`, `POST /api/data/backups/{name}/restore` |
| Utilities | `GET /api/link-preview?url=` |

### 2.6 Security model
The API has no login. It is designed for a local machine:
- **Host allow-list** (`ALLOWED_HOSTS`, default `localhost` and `127.0.0.1`) blocks DNS-rebinding attacks.
- **Cross-site POST guard** rejects state-changing requests whose `Origin` is not in `CORS_ORIGINS`, or whose
  `Sec-Fetch-Site` is `cross-site`. This stops malicious web pages from triggering LLM spend or ingestion.
- **SSRF protection** for link previews: only http/https, every resolved address and every redirect hop must be a
  public unicast IP, and the peer address is re-checked after connecting.
- **Secrets**: `.env` and `llm.yaml` are git-ignored. Key placeholders are detected and reported as "no key".

Run it behind a reverse proxy or on a LAN only after adding authentication and updating `ALLOWED_HOSTS` and
`CORS_ORIGINS`.

### 2.7 Frontend layout (`frontend/src/`)

| Folder | Responsibility |
|---|---|
| `pages/` | Route-level screens: dashboard (in `App.tsx`), `CalendarPage`, `BacktestPage`, `MemoryPage`, `LlmLabPage`, `DataPage` |
| `components/` | Layout columns (`NewsColumn`, `CenterColumn`, `SideColumn`, `Header`), feeds and cards, `PriceChart`, `AdvicePanel`, `ReactionPanel`, `SymbolChartModal`, `backtest/`, `memory/` |
| `api/` | Typed fetch client (`client.ts`) and per-domain modules (`data`, `backtest`, `llm`, `memory`) |
| `hooks/` | `useEventStream` (SSE), `useFetch` (cached fetch with reload), `useAnalysisStore`, `useToasts`, `useLocalStorage` |
| `lib/` | Pure helpers: news merge and sort, chart time, markers, theme, formatting, CSV export |
| `context/` | React contexts for symbol charts, link previews, and reaction data |
| `config/`, `style/` | Theme, font, and per-feature CSS |

The page is chosen from a URL-independent `fxnews.page` preference in `localStorage`. In dev, Vite proxies `/api`
to the backend on port 8000.

---

## 3. Repository layout

```
NewsTrade/
├── README.md                 # this file
├── dev.sh                    # starts backend and frontend together
├── docs/
│   ├── README.vi.md          # detailed guide (Vietnamese)
│   ├── review-backend-core.md
│   └── tasks/                # task specs
├── backend/
│   ├── app/                  # application code (see 2.3)
│   ├── tests/                # pytest suite
│   ├── scripts/              # one-off utilities (e.g. fetch_history.py)
│   ├── data/                 # SQLite DB, exports and backups (runtime)
│   ├── llm.ex.yaml           # LLM config template (copy to llm.yaml)
│   ├── .env.example          # environment template
│   ├── pytest.ini
│   ├── requirements.txt
│   └── docker-compose.memory.yml   # optional Neo4j for Graphiti
└── frontend/
    ├── src/                  # see 2.7
    ├── package.json
    └── vite.config.ts
```

---

## 4. Getting started

### Requirements
- Python 3.12+
- Node.js and npm (a version compatible with Vite and React 19)
- Docker (optional, only for Neo4j-backed memory)

### Quick start

```bash
git clone <repo-url> NewsTrade
cd NewsTrade
./dev.sh
```

`dev.sh` creates the backend virtualenv and installs requirements on first run, copies `.env.example` to `.env` if
missing, installs frontend dependencies, and starts both servers. Open http://localhost:5173. Press Ctrl+C to stop
both.

### Manual setup

```bash
# Backend
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # set OPENROUTER_API_KEY, DISCORD_WEBHOOK_URL, etc.
cp llm.ex.yaml llm.yaml       # optional: multi-provider LLM config
uvicorn app.main:app --reload --port 8000 --timeout-graceful-shutdown 3

# Frontend (second terminal)
cd frontend
npm install
npm run dev                   # http://localhost:5173, proxies /api to :8000
```

`--timeout-graceful-shutdown 3` is required during development. Open SSE connections would otherwise block
`--reload` and Ctrl+C.

### Optional: long-term memory with Neo4j

```bash
cd backend
NEO4J_PASSWORD=change-me docker compose -f docker-compose.memory.yml up -d
# in .env: MEMORY_NEO4J_PASSWORD=change-me
python -m app.memory init
python -m app.memory sync --from 2026-01-01 --graph
python -m app.memory recall "Non-Farm Payrolls" --symbol EURUSD --as-of 2026-11-07T13:30
```

Without Neo4j, memory still works on SQLite; the Memory page and Data page do not need Neo4j.

---

## 5. Configuration highlights

| Variable | Default | Purpose |
|---|---|---|
| `OPENROUTER_API_KEY`, `OPENROUTER_MODEL` | empty | Enables LLM analysis and sets the default model |
| `DISCORD_WEBHOOK_URL` | empty | Fallback Discord webhook (the accounts live in `backend/notify.yaml`) |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | empty | Fallback Telegram account |
| `NOTIFY_CONFIG` | `notify.yaml` | Path of the notification YAML file |
| `SCHEDULER_ENABLED` | `true` | Starts the background polling loops |
| `FF_POLL_MINUTES` / `RSS_POLL_MINUTES` | `10` / `3` | Polling intervals |
| `RSS_FEEDS` | FXStreet, CNBC | Headline sources |
| `PRE_EVENT_ALERT_MINUTES` | `30` | Lead time for pre-event analysis |
| `HEADLINE_COOLDOWN_MINUTES`, `MAX_AUTO_LLM_CALLS_PER_HOUR` | `10`, `20` | Automatic LLM cost limits |
| `TWELVEDATA_API_KEY` | empty | Candle fallback |
| `ALLOWED_HOSTS`, `CORS_ORIGINS` | localhost | Security allow-lists (JSON lists) |
| `LLM_CONFIG` | `llm.yaml` | Path to the multi-provider LLM config |
| `BACKTEST_MODELS`, `HEADLINE_HISTORY_*` | see `.env.example` | Backtest models and history sources |
| `MEMORY_BACKEND`, `MEMORY_NEO4J_*`, `MEMORY_EMBEDDER` | see `.env.example` | Memory storage and embeddings |
| `DATA_BACKUP_DIR`, `DATA_BACKUP_KEEP`, `DATA_IMPORT_MAX_MB` | see `.env.example` | Data export, backup, and import limits |

The full list, with comments, is in `backend/.env.example`.

---

## 6. Testing

```bash
cd backend
source .venv/bin/activate
pytest                 # unit and integration tests (offline)
pytest -m network      # live smoke tests against real internet sources (opt-in)
```

The test suite covers ingestion and cursors, RSS and ForexFactory parsing and timezones, mapping rules, LLM
providers and YAML config, security guards, log hygiene, candles, reaction and backtest maths, memory facts and
recall, data import and export, and link previews. Tests use `respx` to mock HTTP.

Frontend: `npm run lint` and `npm run build` (type-check with `tsc -b`, then Vite build).

---

## 7. Known limitations

- ForexFactory's feed has no actual values, so surprise-based rules depend on headline text.
- Free data sources are unreliable: some RSS feeds return 403 or time out, and Yahoo rate-limits by IP. Check
  `docs/README.vi.md` (section 2) for the current status.
- TradingView's websocket is unofficial and may change without notice.
- In-memory cost controls and job registries reset on restart.
- No authentication. Keep the API on localhost or add an auth layer before exposing it.

---

## 8. Further reading

- [`docs/README.vi.md`](docs/README.vi.md): detailed guide in Vietnamese (data flow, API tables, `.env` reference,
  memory design, legacy-port notes)
- [`docs/review-backend-core.md`](docs/review-backend-core.md) and
  [`docs/tasks/`](docs/tasks/): review notes and task specs

---

## 9. License

Released under the [MIT License](LICENSE).
