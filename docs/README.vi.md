# FXNews — Hệ thống trading theo tin tức Forex + tư vấn LLM

Hệ thống mới, tách riêng khỏi dự án Node.js cũ ở thư mục gốc (code cũ **không bị xoá/sửa**,
chỉ port logic sang Python). Gồm 2 phần độc lập:

```

├── backend/    Python 3.12+ · FastAPI · SQLite · httpx   (port 8000)
└── frontend/   React 19 · Vite · TypeScript · lightweight-charts (port 5173)
```

## 1. Luồng dữ liệu

```
 ┌─────────────── nguồn tin (poll định kỳ) ───────────────┐
 │ ForexFactory calendar JSON (nfs.faireconomy.media)      │  mỗi 10'
 │ RSS headline: FXStreet, ... (cấu hình trong .env)       │  mỗi 3'
 └──────────────────────────┬──────────────────────────────┘
                            ▼
              services/ingest.py  — chuẩn hoá → RawNews, dedupe theo hash, lưu SQLite
                            ▼
              analysis/mapping.py — tin → đồng tiền → các cặp liên quan (+ dấu base/quote)
              analysis/rules.py   — điểm định hướng bằng luật (surprise actual vs forecast, keyword)
                            ▼
              analysis/advisor.py — gom context (tin + lịch + nến gần nhất) → OpenRouter LLM
                                    → JSON: bias / confidence / entry idea / invalidation / kịch bản
                            ▼
          ┌─────────────────┼──────────────────────┐
          ▼                 ▼                      ▼
   SQLite (signals)   SSE /api/stream        notify/ (Discord + Telegram webhook)
                            ▼
                  frontend: news feed · chart (TradingView lightweight-charts) · AI panel
```

Hai loại phân tích tự động:

| Loại | Khi nào | Nội dung |
|---|---|---|
| `pre_event` | Tin lịch High impact sắp ra trong `PRE_EVENT_ALERT_MINUTES` (mặc định 30') | Kịch bản: nếu actual > forecast thì…, nếu < thì… cho từng cặp |
| `headline` | Headline RSS mới khớp đồng tiền/chủ đề theo dõi | Bias từng cặp + confidence + ý tưởng vào lệnh |
| `ad_hoc` | Người dùng hỏi trên UI (`POST /api/advice`) | Tư vấn theo cặp + câu hỏi tự do |

> Lưu ý: feed JSON của ForexFactory **không có trường `actual`** (chỉ forecast/previous).
> Kết quả thực tế được nắm qua headline RSS (FXStreet đăng gần như ngay khi ra số), nên LLM
> nhận cả lịch lẫn headline để suy luận. `rules.py` vẫn hỗ trợ surprise nếu nguồn nào có actual.

## 2. Nguồn dữ liệu (free)

| Mục đích | Nguồn | Key |
|---|---|---|
| Lịch kinh tế | ForexFactory JSON `ff_calendar_thisweek.json` / `nextweek` | không |
| Headline | RSS FXStreet news + CNBC Finance (cấu hình thêm qua `RSS_FEEDS`) | không |
| Nến OHLC | Yahoo Finance chart API (`EURUSD=X`, `GC=F`…) — đã dùng ở dự án cũ | không |
| Vẽ chart | TradingView **lightweight-charts** (Apache-2.0) + marker tin tức trên nến | không |
| LLM | OpenRouter (OpenAI-compatible) — đổi model theo từng request / từng cặp | `OPENROUTER_API_KEY` |
| Thông báo | Discord webhook + Telegram bot (nhiều account/kênh) | `backend/notify.yaml` (mẫu: `notify.ex.yaml`) |

Đã kiểm tra (11/2026): investinglive/forexlive RSS trả 403, investing.com RSS timeout, fxempire feed không cập nhật,
Stooq có chặn bot. Yahoo chart API đôi khi trả 429 theo IP → backend trả cache cũ và chuyển sang Twelve Data
(nếu có `TWELVEDATA_API_KEY`); frontend tự chuyển sang widget TradingView nhúng nếu API nến lỗi.

## 3. Phân công module

| Module | File | Người làm |
|---|---|---|
| Cấu hình, DB, schema API, app | `app/config.py, db.py, models.py, schemas.py, main.py, events.py` | lead |
| Nguồn ForexFactory | `app/sources/forexfactory.py` | lead |
| Map tin → cặp, luật | `app/analysis/mapping.py, rules.py`, `app/market/symbols.py` | lead |
| OpenRouter + advisor | `app/analysis/llm.py, advisor.py` | lead |
| Discord + Telegram | `app/notify/discord.py`, `telegram.py`, `config.py`, `messages.py` | lead |
| Ingest + scheduler, routers news/signals/advice/stream/system | `app/services/*`, `app/routers/*` | lead |
| Nguồn RSS | `app/sources/rss.py` | agent backend |
| Nến + quote | `app/market/candles.py`, `app/routers/market.py` | agent backend |
| Toàn bộ frontend | `frontend/` | agent frontend |

## 4. API contract (prefix `/api`)

Định nghĩa chính xác nằm ở `backend/app/schemas.py` — frontend bám theo file đó.

| Method | Path | Mô tả |
|---|---|---|
| GET | `/health` | `{status, version, llm_enabled, discord_enabled}` |
| GET | `/symbols` | `SymbolOut[]` — danh mục cặp hỗ trợ |
| GET | `/news?kind=&currency=&impact=&symbol=&limit=50&before=` | `NewsOut[]` mới nhất trước |
| GET | `/news/{id}` | `NewsOut` |
| GET | `/calendar?days=7&past_days=1&currency=&impact=` | `NewsOut[]` (kind=calendar) theo thời gian tăng dần |
| GET | `/signals?symbol=&limit=50` | `SignalOut[]` |
| GET | `/analyses?symbol=&limit=20` | `AnalysisOut[]` mới nhất trước (ad_hoc của symbol + phân tích có trade cho symbol) |
| GET | `/analyses/{id}` | `AnalysisOut` |
| POST | `/news/{id}/analyze` body `{model?}` | chạy LLM cho 1 tin → `AnalysisOut` |
| POST | `/advice` body `AdviceRequest` | tư vấn ad-hoc → `AnalysisOut` |
| GET | `/models` | `ModelOut[]` model OpenRouter gợi ý + default |
| GET | `/candles?symbol=EURUSD&interval=15m&range=5d` | `CandlesOut` |
| GET | `/quotes?symbols=EURUSD,XAUUSD` | `QuoteOut[]` |
| GET | `/stream` | SSE: event `news` (NewsOut), `analysis` (AnalysisOut) |
| POST | `/ingest/run` | chạy ngay 1 vòng thu thập → `{inserted, analyzed}` |
| POST | `/notify/test` | gửi tin test lên các account đang bật (thân body `{channel?}` để test 1 kênh) |
| GET | `/notify/settings` | cấu hình thông báo (secret chỉ trả về "đã đặt") |
| POST | `/notify/settings` | lưu cấu hình vào `backend/notify.yaml`: rules + danh sách account |

Thời gian: mọi timestamp trong API là ISO-8601 UTC (`...Z`); nến dùng unix seconds (đúng định dạng lightweight-charts).

## 5. Chạy

```bash
# backend
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # điền OPENROUTER_API_KEY, DISCORD_WEBHOOK_URL (tuỳ chọn: TELEGRAM_*)
uvicorn app.main:app --reload --port 8000 --timeout-graceful-shutdown 3

# frontend
cd frontend
npm install
npm run dev                 # http://localhost:5173 (proxy /api → :8000)
```

- `--timeout-graceful-shutdown 3` là bắt buộc khi dev: nếu không, một tab đang mở `/api/stream` (SSE) sẽ chặn
  Ctrl-C / `--reload` ("Waiting for connections to close") vì kết nối SSE không bao giờ tự đóng.
- API không có đăng nhập nên chỉ nên truy cập từ máy local. Server từ chối `Host` lạ (chống DNS rebinding,
  `ALLOWED_HOSTS`, mặc định `localhost`, `127.0.0.1`) và từ chối request ghi (POST/PUT/DELETE) có `Origin`
  ngoài `CORS_ORIGINS` hoặc bị trình duyệt đánh dấu `Sec-Fetch-Site: cross-site`. `curl` và proxy của Vite
  (không gửi Origin lạ) vẫn chạy bình thường. Khi chạy sau Docker/LAN, thêm hostname vào `ALLOWED_HOSTS` và
  origin của UI vào `CORS_ORIGINS` (cả hai là JSON list trong `.env`).
- `POST /api/ingest/run` bỏ qua feed ForexFactory nếu vừa lấy chưa tới `FF_POLL_MINUTES` (feed bị giới hạn tốc
  độ); thêm `?force=true` để ép lấy ngay. Lý do bỏ qua nằm trong `errors` của kết quả.
- Multi-provider LLM layer (`app/llm`, backtest / LLM Lab; models are `provider:model`): configured by
  `backend/llm.yaml` (committed template: `backend/llm.ex.yaml`, `cp llm.ex.yaml llm.yaml`, never committed;
  `LLM_CONFIG` points elsewhere; restart after editing). Each provider has `model`, `thinking`, `rotation`
  (`failover` | `round_robin`) and `api_keys: [{name, key}, ...]`. Keys that are placeholders (`sk-or-v1-...`) count
  as "no key" (`GET /api/llm/models` shows it in the provider's `problem`). Explicitly set env variables still work:
  `OPENROUTER_API_KEY(S)`, `GROQ_API_KEY(S)`, `HIVE_API_KEY(S)` are appended to the YAML key list, and
  `OPENROUTER_MODEL`, `GROQ_THINKING`, `LLM_TIMEOUT_SECONDS`, ... override the file. `claude_code` / `opencode` need
  no key; for several logins add `accounts` (`claude_code`: `{name, config_dir}` = `CLAUDE_CONFIG_DIR`; `opencode`:
  `{name, data_home[, config_home]}` = `XDG_DATA_HOME`), they rotate and fail over on rate/usage-limit or auth errors
  like keys. `opencode` runs `-m <model>#<thinking>` and, with `isolated: true` (default), loads none of the machine's
  opencode plugins/MCP/skills/AGENTS.md. A bad file is reported (file + field) by `/api/llm/models` and `/api/llm/chat`.
- Chi phí LLM tự động được giới hạn bởi `HEADLINE_COOLDOWN_MINUTES` (mặc định 10: mỗi đồng tiền chính tối đa một
  headline được tự phân tích mỗi 10 phút), `MAX_HEADLINE_ANALYSES_PER_CYCLE` và ngân sách
  `MAX_AUTO_LLM_CALLS_PER_HOUR` (mặc định 20, đếm cả pre-event).

## 6. Bộ nhớ dài hạn (Graphiti) — `backend/app/memory/`

Lưu **sự thật đã quan sát** để LLM truy xuất ở các lần dự đoán sau: mỗi cặp (sự kiện, symbol) có kết quả
cuối cùng (`bt_outcomes.complete`) thành một fact `MARKET_REACTION` trong graph của symbol đó (`group_id = fx_<SYMBOL>`).

| Lưu | Không bao giờ lưu |
|---|---|
| symbol, tiêu đề, giờ, impact, kind, currency/country, actual / forecast / previous, actual − forecast | `bt_predictions`, `analyses`, `signals`, `news_items.rule_bias` |
| giá lúc ra tin, return + nhãn up/down/flat ở 5m/15m/1h/4h/1d, biên độ giờ đầu | `bt_events.raw`, summary bài báo (trừ khi `MEMORY_INCLUDE_SUMMARY=true`) |

- Ghi **tất định**, không qua LLM: không dùng `add_episode`/`add_triplet` của Graphiti (hai hàm đó gọi LLM để
  trích xuất và *vô hiệu hoá* fact cũ). Graphiti nhận một LLM client từ chối mọi lời gọi.
- Văn bản sinh ra bị kiểm tra từ khoá ý kiến (`bullish`, `likely`, `confidence`…); có thì bỏ fact đó.
- uuid tất định → sync lại chỉ ghi đè, không nhân bản. Fact lịch sử không bao giờ bị đánh dấu hết hiệu lực.
- `recall(..., as_of=T)` chỉ trả fact đã biết đủ tại T (sự kiện ≤ T − 1 ngày) → backtest không nhìn thấy tương lai.

```bash
cd backend
pip install -r requirements.txt
NEO4J_PASSWORD=change-me docker compose -f docker-compose.memory.yml up -d   # MEMORY_NEO4J_PASSWORD=change-me trong .env
python -m app.memory init                                   # tạo index (1 lần)
python -m app.memory sync --from 2026-01-01 --graph         # outcome đã hoàn tất -> SQLite + graph (chạy lại được)
python -m app.memory recall "Non-Farm Payrolls" --symbol EURUSD --as-of 2026-11-07T13:30
```

Trong code: `MemoryStore().recall(title, symbol, as_of=release_time)` rồi `format_for_prompt(facts)` để chèn vào prompt.

**Nơi lưu:** nguồn chính là SQLite (`memory_nodes`, `memory_edges`, `memory_sync`), nên tab **Memory** và trang
**Data** (export/import) chạy được mà không cần Neo4j. Graphiti/Neo4j là bản sao để tìm kiếm ngữ nghĩa:
`python -m app.memory sync --graph` hoặc, sau khi import, `python -m app.memory push-graph`.

**Tab Memory:** graph theo từng symbol (symbol ở giữa, indicator màu xanh dương gom các lần công bố, mỗi sự kiện tô
màu theo hướng giá ở horizon đã chọn, kích thước theo độ lớn biến động). Lọc theo text / loại tin / hướng, nút
*Sync outcomes* (`POST /api/memory/sync`). API: `GET /api/memory/overview`, `GET /api/memory/graph?symbol=EURUSD`.

## 7. Tận dụng từ dự án cũ

| Cũ (Node) | Mới (Python) |
|---|---|
| `src/news/forexfactoryCalendar.js` | `app/sources/forexfactory.py` |
| `src/analysis/calendarSignalGenerator.js` (surprise, higher/lower-is-better) | `app/analysis/rules.py` |
| `src/analysis/newsSignalGenerator.js` (regex chủ đề → symbol, keyword bias) | `app/analysis/mapping.py`, `rules.py` |
| `src/prices/mt5ToYahoo.js`, `yahooChart.js` | `app/market/symbols.py`, `candles.py` |
| `src/analysis/recommendedAction.js` | `rules.recommended_action()` |
| `frontend/src/style/global.css` (theme tối) | `frontend` dùng lại token màu |
