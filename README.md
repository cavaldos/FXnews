# NewsTrade

Hệ thống thu thập tin tức tài chính, tạo tín hiệu giao dịch MT5 và lưu vào Notion.

## Cấu trúc

```
NewsTrade/
├── src/
│   ├── config.js                    # Cấu hình (API keys)
│   ├── index.js                     # Notion client
│   ├── news/
│   │   └── tavily.js               # Thu thập tin tức
│   ├── analysis/
│   │   └── signalGenerator.js      # Tạo tín hiệu trading
│   ├── db/
│   │   └── notion.js               # Thao tác Notion database
│   └── pipeline/
│       └── index.js                # Pipeline chính
├── scripts/                        # CLI commands
│   ├── collect-signals.js          # Thu thập & lưu tín hiệu
│   ├── query-signals.js            # Truy vấn tín hiệu
│   ├── analyze-signals.js          # Phân tích AI (Phase 2)
│   ├── verify-signals.js           # Xác minh với giá thực (Phase 3)
│   └── run-scheduler.js            # Chạy tự động
└── package.json
```

## Cài đặt

```bash
npm install
```

## Cấu hình

Tạo file `.env`:

```env
# Required
NOTION_TOKEN=your_notion_token
TAVILY_API_KEY=your_tavily_key

# Optional
NOTION_DATABASE_ID=your_database_id
```

## Sử dụng

## Timezone chuẩn

- Timezone chuẩn của hệ thống là `America/New_York`
- Trong Notion, cột chuẩn là `Time (ET)`
- Trên TradingView, nên chọn `New York` để đồng nhất với Notion và validator
- Không nên dùng `UTC` hoặc `Sở giao dịch` nếu mục tiêu là so trực tiếp với `Time (ET)`

### Phase 1: Thu thập tin tức & tạo tín hiệu

```bash
# Chạy thử (không lưu)
npm run signals:dry-run

# Thu thập và lưu vào Notion
npm run signals:collect
```

### Phase 2: Phân tích AI (multi-model)

```bash
# Xem trạng thái phân tích
npm run signals:status

# Phân tích với Ollama
node scripts/analyze-signals.js --ollama <pageId>
```

### Phase 3: Xác minh tín hiệu

```bash
# Xác minh tất cả tín hiệu
npm run signals:verify

# Xem báo cáo độ chính xác
npm run signals:verify-report

# Thống kê nhanh
npm run signals:verify-stats
```

### Phase 4: Kiểm tra phản ứng giá ngắn hạn

```bash
# Kiểm tra các signal active và ghi `Check` / `Result` vào Notion
npm run signals:result

# Kiểm tra tất cả signals
npm run signals:result-all

# Chuẩn hóa toàn bộ signals cũ về 1 primary symbol / signal
npm run signals:normalize-symbols

# Tạo lại bộ nhớ các lệnh thua để hệ thống học cho lần sau
npm run signals:rebuild-memory

# Chạy thử không ghi Notion
node .agents/skills/news-signal-validation/scripts/check-result.js --pending --dry-run

# Tuỳ chỉnh cửa sổ phản ứng (ví dụ 120 phút)
node .agents/skills/news-signal-validation/scripts/check-result.js --pending --window-minutes 120
```

Flow mới của validation:
- prefetch toàn bộ OHLC cho các symbol liên quan trước
- lưu raw data theo từng run ở `verification-data/validation-runs/`
- xác định hướng thực tế sau thời điểm signal (`up` / `down` / inconclusive)
- sau đó mới đối chiếu với `Bias` trong Notion để ghi `Check` và `Result`

Loss-memory:
- file máy đọc được: `data/validation/loss-memory.json`
- file người đọc được: `docs/validation-lessons.md`
- bộ generate signal dùng các lesson này để phạt hoặc bỏ qua những pattern đã thua lặp lại

### Scripts bổ sung

```bash
# Truy vấn tín hiệu
node scripts/query-signals.js [all|new|bullish|bearish]

# Thêm tín hiệu thủ công
node scripts/add-signal.js '{"event": "...", "symbols": ["EURUSD"]}'

# Cập nhật trạng thái
node scripts/update-status.js <pageId> "👀 Watching"

# Chạy tự động (cron)
npm run signals:schedule

# Lấy các tin/sự kiện trong tương lai từ Investing, Trading Economics, ForexLive, BabyPips
npm run news:future

# Phân tích lệnh tương lai và lưu vào Notion (chỉ lưu bias rõ ràng, bỏ qua neutral)
npm run orders:analyze-save

# Chạy thử không lưu Notion
node scripts/analyze-and-save-future-orders.js --dry-run --days-ahead 7 --min-impact high

# Tùy chọn nguồn và cửa sổ thời gian (ngày)
node scripts/fetch-future-news.js --sources investing,tradingeconomics --days-ahead 7
```

## Tín hiệu & Giá trị

### Bias
- 🟢 Bullish - Giá dự kiến tăng
- 🔴 Bearish - Giá dự kiến giảm
- 🟡 Neutral - Chưa rõ ràng

### Impact
- 🔥 High - Fed, CPI, NFP, GDP, địa chính trị
- ⚡ Medium - Tin ngành, earnings, M&A
- 💧 Low - Tin nhỏ, ít tác động

### Symbols (MT5)
- Forex: EURUSD, GBPUSD, USDJPY, AUDUSD, ...
- Indices: US500, US30, NAS100, DE40, ...
- Commodities: XAUUSD, USOIL, ...
- Crypto: BTCUSD, ETHUSD

## API Keys

| Service | Required | Purpose |
|---------|----------|---------|
| Tavily | ✅ | Search tin tức |
| Notion | ✅ | Lưu trữ tín hiệu |
| Ollama | ❌ | Phân tích AI local |
| Frankfurter | ❌ | Verify giá forex |

## License

MIT
