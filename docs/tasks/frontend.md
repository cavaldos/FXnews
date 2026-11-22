# Task: frontend — dashboard tin tức + chart + AI panel

Bối cảnh: đọc `docs/README.vi.md` (kiến trúc + API contract) và `backend/app/schemas.py` (nguồn chuẩn của kiểu dữ liệu).
Backend FastAPI chạy ở `http://127.0.0.1:8000` (lead đang viết song song; có thể chưa chạy được lúc bạn bắt đầu —
cứ code theo contract, khi backend lên thì test thật).

**Chỉ làm trong `frontend/`.** Không sửa `frontend/` cũ ở gốc (chỉ được đọc/copy), không sửa backend, không git commit.

## Stack

- Vite + React 19 + TypeScript (scaffold mới trong `frontend`). CSS thuần hoặc Tailwind v4 đều được — ưu tiên đơn giản.
- Chart: **TradingView lightweight-charts** (npm `lightweight-charts`, bản 5.x, pin version cụ thể). API v5:
  `chart.addSeries(CandlestickSeries, ...)`, `createSeriesMarkers(series, markers)`.
- Vite dev server port 5173, proxy `/api` → `http://127.0.0.1:8000` (kể cả SSE `/api/stream`).
- Tận dụng theme tối của dự án cũ: copy token màu từ `frontend/src/style/global.css` (`--bg-base`, `--bg-card`, `--border`,
  `--accent`, `--green`, `--red`, `--yellow`, font mono...). Có thể tham khảo component cũ trong `frontend/src/components/UI`.

## Màn hình chính (1 trang, 3 cột, responsive xuống 1 cột trên mobile)

1. **Header**: chọn symbol (từ `GET /api/symbols`), dải quote (`GET /api/quotes`, refresh 15s), trạng thái
   `GET /api/health` (LLM bật/tắt, Discord bật/tắt, model mặc định), nút "Thu thập ngay" (`POST /api/ingest/run`).
2. **Cột trái — Tin tức**: 2 tab
   - *Headlines*: `GET /api/news?kind=headline` — tiêu đề, nguồn, thời gian tương đối, chip currencies/symbols, rule_bias.
   - *Lịch kinh tế*: `GET /api/calendar?days=7` — nhóm theo ngày, giờ địa phương, cờ/currency, impact màu
     (High đỏ, Medium cam, Low vàng), forecast/previous/actual, countdown cho sự kiện sắp tới.
   - Lọc theo currency / impact; mặc định lọc theo 2 đồng tiền của symbol đang chọn (có toggle "tất cả").
   - Click 1 tin → hiện chi tiết + nút "Phân tích AI" (`POST /api/news/{id}/analyze`), nếu tin có `analysis_id` thì hiện luôn.
3. **Cột giữa — Chart**: nến từ `GET /api/candles?symbol&interval`, chọn interval (1m 5m 15m 30m 1h 4h 1d), auto refresh
   (1m/5m: 15s, còn lại 60s) bằng `series.update` / `setData`.
   - **Marker tin tức** trên chart: các tin liên quan symbol (theo `symbols` hoặc currency của symbol) đặt marker tại
     `event_time` (snap về bar gần nhất), màu theo impact, text ngắn (vd "USD CPI").
   - **Marker tín hiệu**: `GET /api/signals?symbol=` → arrowUp xanh (bullish) / arrowDown đỏ (bearish).
   - Đường dọc/marker cho sự kiện lịch sắp tới nằm trong tương lai thì bỏ qua hoặc hiện ở panel, không crash.
4. **Cột phải — AI tư vấn**:
   - Form "Hỏi AI": câu hỏi (tuỳ chọn) + dropdown model (`GET /api/models`, chọn default) → `POST /api/advice`
     `{symbol, question, model, interval}`. Hiện loading (LLM có thể mất 10–60s).
   - Danh sách phân tích gần nhất cho symbol (từ signals + `GET /api/analyses/{id}`): summary, thẻ TradeIdea
     (bias màu, confidence %, horizon, entry_idea, invalidation, targets), scenarios (pre_event), risk_notes, model, thời gian.
   - Nếu `error` khác null → hiện cảnh báo "LLM lỗi, đang dùng kết quả rule-based".

## Realtime

`EventSource('/api/stream')`: event `news` (payload NewsOut) → prepend vào feed (+ toast nhỏ nếu impact High);
event `analysis` (AnalysisOut) → prepend vào panel AI nếu liên quan symbol đang chọn. Tự reconnect.

## Cấu trúc gợi ý

```
src/api/types.ts      # mirror schemas.py
src/api/client.ts     # fetch wrapper + hooks đơn giản
src/components/{Header,NewsFeed,CalendarList,PriceChart,AdvicePanel,TradeIdeaCard}.tsx
src/App.tsx, src/main.tsx, src/styles.css
```

## Kiểm tra

- `npm run build` phải pass (tsc + vite). `npm run lint` nếu có cấu hình.
- Khi backend chạy được (`curl http://127.0.0.1:8000/api/health`), mở dev server và kiểm tra thật: chart có nến, feed có tin.
  Nếu có công cụ trình duyệt/screenshot thì chụp lại để tự kiểm tra layout.

## Báo cáo khi xong

Trả lời ngắn: file chính, cách chạy, đã verify gì, chỗ nào contract chưa khớp backend (nếu có).
