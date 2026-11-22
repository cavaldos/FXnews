# Review backend core (backend/app)

Phạm vi: `config.py, db.py, models.py, events.py, sources/forexfactory.py, analysis/{mapping,rules,llm,advisor}.py, notify/discord.py, services/{ingest,scheduler,serializers}.py, routers/{news,signals,system}.py, main.py`.

Phương pháp: chỉ đọc code, không sửa file trong repo. Mọi kết luận có script chứng minh (nhãn `[P#]`, danh sách ở cuối) chạy trên DB tạm, tắt scheduler, mock HTTP bằng `respx`. Dữ liệu thật lấy read-only từ backend đang chạy ở `127.0.0.1:8000`.

Lưu ý quy trình: ở script P7 phần A, `POST /api/ingest/run` chạy **trước khi** cài mock, nên đã gọi thật 1 lần tới 2 URL faireconomy và 2 feed RSS (FXStreet, CNBC). Các script còn lại đều mock trước khi gọi. Không gọi Yahoo lần nào trong task review này.

## Tóm tắt

| # | Mức | Lỗi | Vị trí chính |
|---|---|---|---|
| 1 | HIGH | Webhook token Discord bị ghi vào log | `notify/discord.py:82-84`, `main.py:15` |
| 2 | HIGH | `rule_bias` sai hướng giá do gán sai đồng tiền | `rules.py:111-125`, `mapping.py:93-101,128`, `rules.py:46` |
| 3 | HIGH | Lỗi OpenRouter thoáng qua làm mất vĩnh viễn pre-event analysis, Discord nhận tin fallback | `llm.py:90-102`, `ingest.py:173`, `advisor.py:218-224,239-244,298-305` |
| 4 | MEDIUM | Output LLM sai schema làm sập analysis; `/api/advice` trả 404 | `advisor.py:157,160-165`, `llm.py:104`, `signals.py:45` |
| 5 | MEDIUM | Endpoint ghi/tốn tiền nhận request cross-site, không xác thực | `routers/news.py:78`, `system.py:22,27`, `main.py:44-50` |
| 6 | MEDIUM | `/api/ingest/run` bỏ qua guard rate-limit của FF | `system.py:22-24`, `ingest.py:104-118,182` |
| 7 | MEDIUM | `calendar_window` loại mất sự kiện sắp tới | `advisor.py:79-89` |
| 8 | MEDIUM | Chi phí LLM không có chống trùng | `ingest.py:61,142-152`, `rules.py:28-32`, `advisor.py:258` |
| 9 | MEDIUM | SSE `analysis` bị trễ sau webhook Discord | `advisor.py:239-251` |
| 10 | LOW | Tắt server bị treo khi còn client SSE | `system.py:37-43`, `events.py:34-45` |
| 11 | LOW | `last_ingest_at` đặt cả khi mọi feed RSS lỗi | `ingest.py:139` |
| 12 | LOW | Cursor `before` làm mất dòng cùng `event_time` | `routers/news.py:43-45` |
| 13 | LOW | Ngày FF không có offset bị hiểu theo múi giờ máy chủ | `sources/forexfactory.py:43` |
| 14 | LOW | Các điểm nhỏ (embed >6000, max_tokens, sign index, stale price) | xem chi tiết |

---

## HIGH

### 1. Webhook token Discord bị ghi vào log

- **File:** `app/notify/discord.py:82-84`, `app/main.py:15` [P6]
- **Kịch bản:** `logging.basicConfig(level=logging.INFO)` làm logger `httpx` in dòng `HTTP Request: POST https://discord.com/api/webhooks/<id>/<TOKEN> "HTTP/1.1 204 No Content"` mỗi lần gửi thông báo hoặc gọi `/api/notify/test`. Ai có token đều đăng được lên kênh. OpenRouter key thì **không** bị lộ (key nằm ở header, không nằm trong URL).
- **Cách sửa:** trong `main.py`, sau `basicConfig`, đặt `logging.getLogger("httpx").setLevel(logging.WARNING)` và tương tự cho `httpcore`. Nếu log đã từng được chia sẻ thì xoay (tạo lại) webhook.

### 2. `rule_bias` sai hướng giá do gán sai đồng tiền

- **File:** `app/analysis/rules.py:111-125` (dùng `currencies[0]` ở dòng 118 và 125), `app/analysis/mapping.py:93-101` (thứ tự dict regex) và `:128` (đồng tiền lấy từ title), `app/analysis/rules.py:46` (`eas(?:e|ing)`), `app/services/ingest.py:61-76` [P1, P2]
- **Phần đã xác nhận đúng:** logic dấu base/quote (`symbol_sign`, `bias_from_strength`, `fallback_trades`). Kiểm vét cạn 84 tổ hợp (symbol, currency, strength) và 62 ý tưởng fallback, 0 sai lệch. Lỗi nằm ở bước xác định đồng tiền nào đang mạnh hay yếu.
- **Nguyên nhân (a), "đồng tiền chính" theo thứ tự dict:** `currencies[0]` là đồng tiền khớp đầu tiên theo thứ tự dict regex (USD, EUR, GBP…), không theo nội dung câu. "Fed holds rates steady but ECB signals rate cut in December" cho `currencies=['USD','EUR']`, primary USD, `rule_bias=bullish` EURUSD. Đúng ra ECB cắt lãi thì EUR yếu và phải là bearish. Cùng lỗi với "ECB signals rate cut in December as Fed holds steady".
- **Nguyên nhân (b), title và summary không đồng bộ:** đồng tiền lấy từ **title** (`detect_currencies(item.title) or detect_currencies(text)`), nhưng từ khoá mạnh/yếu quét **title+summary** (`rules.py:125`). Dữ liệu thật: "Canadian Dollar declines despite higher oil prices" cho `currencies=['CAD','OIL']`. Summary nhắc "Federal Reserve rate hike" nên bị tính là CAD mạnh, kết quả **bearish USDCAD**. Đúng phải là bullish vì CAD yếu thì USDCAD tăng.
- **Nguyên nhân (c), từ khoá không có ngữ cảnh:** `easing` bị coi là nới lỏng tiền tệ. Dữ liệu thật: "Dow Jones futures surge as **easing** geopolitical risks, AI earnings revive market sentiment" ra **bearish US500**. "Gold rallies as easing yields boost bullion" ra bearish XAUUSD.
- **Mức độ ảnh hưởng:** trong 17 headline thật có bias khác neutral, ít nhất 2 cái sai. `rule_bias` hiện lên UI (`NewsOut.rule_bias`) và được đưa vào prompt làm `rule_prior_primary_symbol` nên lệch cả LLM; nó cũng là đầu vào của `fallback_trades`.
- **Cách sửa:**
  - Chọn đồng tiền chính theo vị trí khớp sớm nhất trong title (`min(match.start())`), không theo thứ tự dict.
  - Tính từ khoá trên đúng đoạn câu chứa đồng tiền đó (tách theo `;`, `,`, "as", "while", "but", "despite"), hoặc dùng cùng một văn bản cho cả phát hiện đồng tiền lẫn từ khoá.
  - Khi phát hiện nhiều đồng tiền mà không khớp được từ khoá vào đoạn nào thì trả `neutral` thay vì đoán.
  - Chỉ coi `easing` là nới lỏng khi đi kèm monetary, policy hoặc rate.
  - Mở rộng `price_action_bias` (surge, rally, jump…) cho các tài sản không có cặp tường minh: SPX, NDX, XAU, OIL, BTC.

### 3. Lỗi OpenRouter thoáng qua làm mất vĩnh viễn pre-event analysis, Discord nhận tin fallback

- **File:** `app/analysis/llm.py:90-102` (không retry), `app/services/ingest.py:173` (`pre_event_alerted = True` đặt trước), `app/analysis/advisor.py:218-224` (`_should_notify`), `:239-244` (gửi Discord), `:298-305` (nhánh fallback), `:213` (`n.analysis_id = analysis.id`) [P5-A]
- **Kịch bản:** 20 phút trước Core CPI (High, USD), OpenRouter trả 503.
  - `chat_json` chỉ lặp 2 lần cho trường hợp 400 `response_format`, không retry 429/5xx/timeout, nên chỉ có 1 lần gọi.
  - Hệ thống lưu analysis `model='rules'`, 0 trade, `error='OpenRouter HTTP 503: upstream overloaded'`.
  - Vì `kind="pre_event"` và `notify_pre_event=True`, Discord nhận embed "LLM không khả dụng (…)" và `notified=True`.
  - `pre_event_alerted=True` đã được đặt từ trước. Khi OpenRouter hồi phục 1 phút sau, chu kỳ kế tiếp tạo 0 analysis mới, tức sự kiện không bao giờ được phân tích lại.
  - `_save` ghi đè `news.analysis_id` bằng bản lỗi, nên bấm analyze lại lúc LLM sập sẽ làm mất liên kết tới bản tốt trước đó.
- Đây là tính năng chủ lực (kịch bản trước tin), và 429/503/timeout của OpenRouter là chuyện thường gặp.
- **Cách sửa:**
  - Retry 2 lần với backoff cho 429, 5xx và timeout trong `chat_json`, tôn trọng `Retry-After`.
  - Chỉ đặt `pre_event_alerted` sau khi có analysis không lỗi, hoặc cho thử lại mỗi phút đến lúc ra tin (giới hạn số lần).
  - Không gửi Discord khi `analysis.error` khác rỗng, hoặc gửi một tin rõ ràng là "chưa có phân tích".
  - Không ghi đè `news.analysis_id` bằng bản lỗi nếu đã có bản tốt.

---

## MEDIUM

### 4. Output LLM sai schema làm sập analysis; `/api/advice` trả 404

- **File:** `app/analysis/advisor.py:157` (confidence), `:160-165` (`TradeIdea(...)`), `app/analysis/llm.py:104` (`res.json()` không bọc), `app/routers/signals.py:45` (`except ValueError` → 404) [P3, P5-D, P8-A]
- **Kịch bản:**
  - Model trả `entry_idea` hoặc `invalidation` là list/dict (JSON hợp lệ nhưng sai schema) thì `TradeIdea(...)` ném `pydantic.ValidationError`. Nó không phải `LLMError` nên thoát khỏi `_run_llm`: **0 analysis được lưu, token đã bị tính tiền**, `news.analysis_id` không đổi.
  - `confidence: Infinity` cho `OverflowError`.
  - OpenRouter trả 200 + HTML cho `JSONDecodeError`, 200 + list cho `AttributeError`; cả hai không phải `LLMError`.
  - `confidence` dạng 0–1 (ví dụ 0.85) bị làm tròn thành **1**, còn `"85%"` thành **0**, nên không bao giờ vượt `notify_min_confidence`.
  - `/api/advice` bắt `ValueError` rồi trả 404. `pydantic.ValidationError` và `json.JSONDecodeError` đều là `ValueError`, nên UI nhận "404: 1 validation error for TradeIdea…" hoặc "404: Expecting value…". Cùng lỗi đó ở `/api/news/{id}/analyze` lại là 500, không nhất quán.
- **Cách sửa:**
  - Ép `entry_idea`, `invalidation`, `targets` về chuỗi (list thì join, dict thì `json.dumps`).
  - Chuẩn hoá confidence: bỏ `%`, nếu `0 < c <= 1` thì nhân 100, bắt cả `OverflowError`.
  - Bọc `res.json()` và kiểm tra payload là dict để mọi lỗi thành `LLMError`; bọc `coerce_llm_output` trong `_run_llm` để lỗi chuyển sang nhánh fallback (lưu `raw_response` để debug).
  - Ở router, kiểm tra symbol bằng `get_symbol` trước (hoặc dùng exception riêng) thay vì bắt `ValueError` chung.

### 5. Endpoint ghi/tốn tiền nhận request cross-site, không xác thực

- **File:** `app/routers/news.py:78-83` (`/news/{id}/analyze`), `app/routers/system.py:22,27` (`/ingest/run`, `/notify/test`), `app/main.py:44-50` (CORS, không có `TrustedHostMiddleware`) [P7-A]
- **Kịch bản:** trang web bất kỳ mà user mở có thể chạy `fetch('http://127.0.0.1:8000/api/news/N/analyze', {method:'POST', mode:'no-cors'})`. Request không body và không content-type nên là "simple request", không cần preflight; xác nhận server vẫn xử lý (200, analysis được tạo). Lặp N=1..∞ sẽ đốt credit OpenRouter và spam Discord. `/ingest/run` và `/notify/test` cũng chạy được theo cách này. CORS chỉ chặn việc *đọc* phản hồi, không chặn việc thực thi. Server không kiểm tra Host nên DNS rebinding cũng qua (`GET /api/health` với `Host: rebind.attacker.example` vẫn trả 200). `/api/advice` thì an toàn nhờ content-type chặt (text/plain trả 422).
- **Cách sửa:**
  - Dependency từ chối request mutating khi có header `Origin` không thuộc `cors_origins` (hoặc `Sec-Fetch-Site: cross-site`), hoặc yêu cầu header tuỳ chỉnh/token.
  - Thêm `TrustedHostMiddleware(allowed_hosts=["localhost","127.0.0.1"])`.
  - Giới hạn tần suất cho các endpoint gọi LLM.

### 6. `/api/ingest/run` bỏ qua guard rate-limit của FF

- **File:** `app/routers/system.py:22-24`, `app/services/ingest.py:104-118` (`run_calendar_cycle`), `:182` (`run_all`), `:95-101` (`seconds_until_calendar_due`) [P7-B]
- **Kịch bản:** 3 lần bấm "ingest" tạo 6 request tới faireconomy (mỗi lần 2 URL), trong khi scheduler chỉ gọi 1 lần mỗi `ff_poll_minutes`. `seconds_until_calendar_due()` vẫn báo ~600s nhưng `run_all` không hỏi nó. Khi feed đang 429, mỗi lần bấm làm tình trạng tệ hơn.
- **Cách sửa:** `run_all` bỏ qua `run_calendar_cycle` khi `seconds_until_calendar_due() > 0`, trừ khi có tham số `force=true`. Trả thời gian còn phải chờ trong `IngestResult.errors`.

### 7. `calendar_window` loại mất sự kiện sắp tới

- **File:** `app/analysis/advisor.py:79-89`; gọi tại `:284` (−24h…+24h, limit 10) và `:337` (−24h…+48h, limit 14) [P4]
- **Kịch bản:** truy vấn `ORDER BY event_time ASC LIMIT 10` trên cửa sổ rộng. USD có 12 tin High/Medium trong 24h vừa qua thì cả 3 tin sắp ra (Core PCE sau 2h, FOMC speaker sau 5h, Powell sau 20h) bị cắt; prompt `calendar_last24h_next24h` chỉ chứa tin đã qua. `advise()` dùng cửa sổ 72h với limit 14 nên càng dễ gặp. Đúng thông tin LLM cần nhất cho kịch bản trước tin lại bị mất.
- **Cách sửa:** query tách đôi, mỗi phía một nửa giới hạn: sắp tới `ORDER BY event_time ASC LIMIT n/2` và vừa qua `ORDER BY event_time DESC LIMIT n/2`, rồi gộp và sắp xếp lại. Hoặc sắp xếp theo `abs(event_time - now)`.

### 8. Chi phí LLM không có chống trùng

- **File:** `app/services/ingest.py:61` (impact chấm trên title+summary), `:142-152` (chọn ứng viên), `app/analysis/rules.py:28-32` (`_HIGH_IMPACT_HEADLINE` rộng), `app/analysis/advisor.py:258-312` (`analyze_news` không idempotent) [P5-C]
- **Số liệu thật (66 headline đang có):**
  - 31 (47%) là "High" và 29 đủ điều kiện gọi LLM (đồng tiền thuộc watch list). 22/29 thuộc USD.
  - Trong 6 giờ có 19 lần gọi, gom thành 12 "câu chuyện" (cặp đồng tiền × cửa sổ 30 phút), tức **7 lần gần như trùng** (cụm USD/Fed lặp lại nhiều lần).
  - 3/29 chỉ là High nhờ từ khoá trong summary chứ không phải title.
  - Tốc độ thực tế mặc định: ~24 lần/ngày, đỉnh ~4 lần/giờ. Hiện chưa tốn tiền vì `OPENROUTER_API_KEY` đang trống.
- **Rủi ro:** guard duy nhất là `max_headline_analyses_per_cycle=5` mỗi chu kỳ 3 phút, tối đa 100 lần/giờ. Burst như NFP (nhiều bài cùng chủ đề trong vài phút) sẽ chạm trần. Nếu bật override model đắt cho một symbol thì chi phí tăng tương ứng.
- **Trùng do gọi đồng thời:** hai lần `analyze_news` đồng thời cho cùng một tin (double-click, hoặc tự động + thủ công) sinh **2 lần gọi OpenRouter, 2 analysis, 2 tin Discord**.
- **Cách sửa:**
  - Cooldown theo đồng tiền chính (ví dụ không tự phân tích headline nếu đã có analysis cùng đồng tiền trong 10 phút gần nhất).
  - Chấm `headline_impact` chỉ trên title.
  - Ngân sách theo giờ/ngày (`max_llm_calls_per_hour`).
  - Khóa in-flight theo `news_id`, hoặc trả lại analysis gần đây nếu chưa `force`.

### 9. SSE `analysis` bị trễ sau webhook Discord

- **File:** `app/analysis/advisor.py:239-251` (`_finish`) [P5-B]
- **Kịch bản:** analysis có trong DB lúc t=0.07s nhưng UI chỉ nhận event lúc t=2.07s khi webhook mất 2.0s, vì `broadcaster.publish("analysis", out)` nằm **sau** `await discord.send_analysis(...)`. Webhook chậm hoặc 429 (`_post` có thể ngủ tới 10s mỗi lần, 3 lần, timeout 15s) có thể trễ tới vài chục giây, và trong thời gian đó giữ luôn slot của `_analysis_slots` (Semaphore 2).
- **Cách sửa:** publish SSE ngay sau khi lưu, rồi mới gửi Discord (nếu `notified` đổi thì publish lại), hoặc đưa việc gửi Discord ra một task nền riêng.

---

## LOW

### 10. Tắt server bị treo khi còn client SSE

- **File:** `app/routers/system.py:37-43`, `app/events.py:34-45` [P9]
- **Kịch bản:** SIGINT khi có một `/api/stream` đang mở, sau 10 giây uvicorn vẫn dừng ở "Waiting for connections to close". Server ở cổng 8000 đang chạy với `--reload`, nên mỗi lần lưu code sẽ bị kẹt khi còn tab trình duyệt kết nối. Lifespan shutdown (`scheduler.stop`, `llm.aclose`, drain) không chạy trong lúc này.
- **Cách sửa:** chạy với `--timeout-graceful-shutdown 3` (ghi vào README/lệnh chạy). Không thể dựa vào lifespan để đóng stream vì uvicorn chạy lifespan shutdown sau khi đã đóng kết nối.

### 11. `last_ingest_at` đặt cả khi mọi feed RSS lỗi

- **File:** `app/services/ingest.py:139` [P7-C]
- **Kịch bản:** 2/2 feed trả 500, kết quả `inserted=0, errors=2`, nhưng `/api/health` vẫn hiện `last_ingest_at` bằng thời điểm vừa xong, che mất việc ingest đang hỏng. Chu kỳ calendar thì đặt đúng (chỉ khi thành công).
- **Cách sửa:** chỉ đặt `state["last_ingest_at"]` khi có ít nhất một nguồn tải thành công.

### 12. Cursor `before` làm mất dòng cùng `event_time`

- **File:** `app/routers/news.py:43-45` (`event_time < before`) [P7-D]
- **Kịch bản:** 5 tin lịch có cùng `event_time`; phân trang `limit=2`, lấy `before` = `event_time` của dòng cuối thì trang sau rỗng. Chỉ nhận về 2 dòng, mất 3. Dòng có `event_time` NULL cũng không bao giờ tới được ở các trang sau. Hiện là lỗi tiềm ẩn vì frontend mới khai báo kiểu `before`, chưa dùng.
- **Cách sửa:** cursor ghép `(event_time, id)` với điều kiện `(event_time, id) < (before_time, before_id)`.

### 13. Ngày FF không có offset bị hiểu theo múi giờ máy chủ

- **File:** `app/sources/forexfactory.py:43` (`datetime.fromisoformat(date_raw).astimezone(UTC)`) [P8-B]
- **Kịch bản:** `"2026-11-11T08:30:00"` (không có offset) thành 01:30 UTC khi máy ở múi giờ VN và 13:30 UTC khi ở New York, vì `.astimezone()` trên datetime naive dùng múi giờ cục bộ. Feed thật có offset (`-05:00`) nên hiện đang an toàn; chỉ lộ ra nếu nguồn đổi định dạng.
- **Cách sửa:** nếu `tzinfo is None` thì gán rõ một múi giờ cố định (hoặc bỏ qua bản ghi và log), không dựa vào múi giờ hệ thống.

### 14. Các điểm nhỏ

- **Embed Discord có thể vượt 6000 ký tự tổng** (`notify/discord.py:65-72`): từng phần được cắt riêng (description 4000, 6 trade × 1280…) nhưng tổng có thể vượt giới hạn của Discord, khi đó webhook trả 400, `_post` ghi warning và trả `False`, tin bị bỏ im lặng. *Suy ra từ giới hạn trong tài liệu Discord, chưa tái hiện.* Sửa: tính tổng độ dài và cắt dần fields.
- **`max_tokens=1800`** (`llm.py:75`) có thể bị token "thinking" của gemini-2.5-flash ăn mất, dẫn tới JSON bị cắt và rơi vào fallback. *Chưa kiểm chứng vì không có key.* Sửa: nâng `max_tokens` hoặc tắt/giới hạn reasoning cho model này; thử với key thật.
- **Bảng dấu coi index như cặp FX** (`mapping.py:77-81`, dùng ở `advisor._sign_table`): `symbol_sign("US500","USD") = -1` đưa vào prompt dòng "USD mạnh lên ⇒ US500 GIẢM" như một quy tắc, tương tự "EUR mạnh lên ⇒ DE40 GIẢM". Đúng với cặp FX, kim loại, crypto; còn với index và USOIL thì là giả định. Sửa: chỉ sinh dòng bảng cho `kind in ("fx","metal","crypto")`.
- **`price_context` không báo dữ liệu nến cũ** (`advisor.py:92-122`): bỏ qua `data.source`. Khi nến là `yahoo-stale` (tối đa ~6 giờ) thì mức giá cũ vẫn được đưa vào prompt như giá hiện tại, chỉ có `last_bar_utc` để model tự suy ra. Sửa: thêm `"stale": true` và `"source"` vào context, hoặc bỏ PRICE_CONTEXT khi stale.

---

## Đã kiểm, không có lỗi

- Dấu base/quote và `fallback_trades` đúng [P1].
- Không có session SQLAlchemy nào bị giữ qua `await`: mọi `session_scope` đều đóng trước khi gọi LLM, Discord hoặc lấy nến. Ghi DB chỉ xảy ra trên thread event loop, nên không có tranh chấp ghi.
- `broadcaster.publish` luôn chạy sau commit; `QueueFull` được xử lý (bỏ event cho client chậm).
- `store_items` đồng bộ chặn event loop 30ms cho 200 bản ghi mới, không đáng kể [P8-C].
- Vòng DB↔UTC (`UTCDateTime`) trả về datetime có timezone; không gặp lỗi so sánh naive/aware.
- `.env` và `data/` đã nằm trong `.gitignore`.
- OpenRouter API key không xuất hiện trong log. Twelve Data `apikey` đã được filter che (trong `market/candles.py`).
- Không có race tạo bản ghi trùng trong `store_items` (chạy đồng bộ trên một thread, có unique `dedupe_key`).

## Script chứng minh

Chạy bằng `.venv/bin/python <đường dẫn>` từ `backend`. Tất cả dùng DB tạm và đặt biến môi trường trước khi import `app`. Riêng P7 phần A cần được sửa (cài mock trước khi gọi `/api/ingest/run`) trước khi chạy lại, nếu không nó sẽ gọi mạng thật.

```
_env (prelude dùng chung)  /private/tmp/claude-501/-Users-bourbon-Code-Trading-NewsTrade/92135206-1f1c-4cff-8f5b-792d8d95e4a3/scratchpad/_env.py
P1  /private/tmp/claude-501/-Users-bourbon-Code-Trading-NewsTrade/92135206-1f1c-4cff-8f5b-792d8d95e4a3/scratchpad/p1_signs.py
P2  /private/tmp/claude-501/-Users-bourbon-Code-Trading-NewsTrade/92135206-1f1c-4cff-8f5b-792d8d95e4a3/scratchpad/p2_attribution.py
P3  /private/tmp/claude-501/-Users-bourbon-Code-Trading-NewsTrade/92135206-1f1c-4cff-8f5b-792d8d95e4a3/scratchpad/p3_llm_robust.py
P4  /private/tmp/claude-501/-Users-bourbon-Code-Trading-NewsTrade/92135206-1f1c-4cff-8f5b-792d8d95e4a3/scratchpad/p4_calendar_window.py
P5  /private/tmp/claude-501/-Users-bourbon-Code-Trading-NewsTrade/92135206-1f1c-4cff-8f5b-792d8d95e4a3/scratchpad/p5_pipeline.py
P6  /private/tmp/claude-501/-Users-bourbon-Code-Trading-NewsTrade/92135206-1f1c-4cff-8f5b-792d8d95e4a3/scratchpad/p6_log_leak.py
P7  /private/tmp/claude-501/-Users-bourbon-Code-Trading-NewsTrade/92135206-1f1c-4cff-8f5b-792d8d95e4a3/scratchpad/p7_http.py
P8  /private/tmp/claude-501/-Users-bourbon-Code-Trading-NewsTrade/92135206-1f1c-4cff-8f5b-792d8d95e4a3/scratchpad/p8_misc.py
P9  /private/tmp/claude-501/-Users-bourbon-Code-Trading-NewsTrade/92135206-1f1c-4cff-8f5b-792d8d95e4a3/scratchpad/p9_sse_shutdown.py
```
