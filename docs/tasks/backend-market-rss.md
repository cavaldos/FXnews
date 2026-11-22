# Task: backend — RSS source + market data (candles/quotes)

Bối cảnh: đọc `docs/README.vi.md` (kiến trúc + API contract) và `backend/app/schemas.py` trước.
Backend Python ở `backend` (venv đã cài sẵn: `backend/.venv`, chạy bằng `.venv/bin/python`,
`.venv/bin/pytest`). Lead (agent khác) đang viết song song: `config.py, db.py, models.py, main.py, events.py,
sources/forexfactory.py, analysis/*, notify/*, services/*, routers/` (trừ `routers/market.py`).

**Chỉ tạo/sửa các file dưới đây.** Không sửa file khác, không sửa code Node cũ ở thư mục gốc, không git commit.
Nếu cần thay đổi contract (`schemas.py`, `sources/base.py`, `market/symbols.py`, `config.py`) thì ghi đề xuất vào
báo cáo cuối, đừng tự sửa.

## 1. `app/sources/rss.py`

- `class RssSource` có `name: str`, `url: str`, `async def fetch(self) -> list[RawNews]` (RawNews ở `app/sources/base.py`).
- Tải bằng `httpx.AsyncClient` (timeout 15s, User-Agent kiểu trình duyệt), parse bằng `feedparser`.
- Mỗi item → `RawNews(kind="headline", source=self.name, title, url=link, summary=<description bỏ HTML, gộp khoảng trắng, cắt 600 ký tự>, event_time=<pubDate → datetime aware UTC>, raw={guid,...})`.
- Item thiếu title → bỏ. Thiếu pubDate → `event_time=None`.
- Lỗi mạng/HTTP: `raise` một exception rõ ràng (ingest của lead sẽ bắt và log), không nuốt lỗi im lặng.
- `def build_rss_sources(settings) -> list[RssSource]` tạo từ `settings.rss_feeds` (dict name→url, xem `app/config.py`).

## 2. `app/market/candles.py`

Port từ `src/prices/yahooChart.js` (dự án cũ). Nguồn: Yahoo `https://query2.finance.yahoo.com/v8/finance/chart/{ticker}`,
ticker lấy từ `app/market/symbols.py` (`get_symbol(sym).yahoo`).

- `async def get_candles(symbol: str, interval: str = "15m", range_: str | None = None) -> CandlesOut`
  - interval hỗ trợ: `1m,5m,15m,30m,1h,4h,1d`. Yahoo không có 4h → lấy `1h` rồi resample 4h (mốc theo UTC 00/04/08...).
    `1h` gọi Yahoo là `60m`.
  - range mặc định hợp lý theo interval (vd 1m→1d, 5m→5d, 15m→5d, 30m→1mo, 1h→1mo, 4h→3mo, 1d→1y) và kẹp theo giới hạn Yahoo
    (1m tối đa ~7d, intraday <1h tối đa 60d, 1h tối đa 730d).
  - Bỏ bar có giá trị None; `time` = unix seconds int; sort tăng dần, không trùng `time` (lightweight-charts yêu cầu).
  - Retry 429/5xx với backoff (tối đa 3 lần). Symbol không hỗ trợ → `ValueError`.
  - Cache in-memory TTL (vd 1m/5m: 20s, 15m/30m: 60s, 1h+: 300s) theo key (symbol, interval, range), có lock tránh gọi trùng.
- `async def get_quotes(symbols: list[str]) -> list[QuoteOut]`: giá cuối + change so với previous close
  (từ `meta.regularMarketPrice`, `meta.chartPreviousClose`/`previousClose`). Gọi song song, symbol lỗi thì bỏ qua (log warning).
- Dùng chung 1 `httpx.AsyncClient` module-level (lazy) + hàm `async def aclose()` để main.py đóng khi shutdown.

## 3. `app/routers/market.py`

`router = APIRouter(prefix="/api", tags=["market"])` — lead sẽ `include_router` trong `main.py`.

- `GET /api/symbols` → `list[SymbolOut]` từ `all_symbols()`.
- `GET /api/candles?symbol=EURUSD&interval=15m&range=5d` → `CandlesOut`. Symbol sai → 404; Yahoo lỗi → 502 kèm message.
- `GET /api/quotes?symbols=EURUSD,XAUUSD` → `list[QuoteOut]` (mặc định: các major + XAUUSD nếu không truyền).

## 4. Test

- `tests/test_rss.py`: dùng `respx` mock HTTP với 1 RSS mẫu (có CDATA, HTML trong description) → kiểm tra parse.
- `tests/test_candles.py`: mock Yahoo JSON → kiểm tra bỏ None, sort, resample 4h, quotes.
- Thêm 1 smoke test chạy thật (đánh dấu `@pytest.mark.network`, skip mặc định) gọi FXStreet + Yahoo EURUSD.
- Tự chạy thật 1 lần bằng script nhỏ để chắc chắn feed FXStreet và Yahoo EURUSD/XAUUSD trả dữ liệu.
- Nếu cần cấu hình pytest (marker, asyncio_mode) thì tạo `backend/pytest.ini`.

## Báo cáo khi xong

Trả lời ngắn: danh sách file đã tạo, kết quả pytest, kết quả gọi thật, đề xuất sửa contract (nếu có).
