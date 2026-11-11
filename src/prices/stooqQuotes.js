const axios = require('axios');

const STOOQ_QUOTE_ENDPOINT = 'https://stooq.com/q/l/';

function parseStooqCsvLine(line) {
  // Expected columns: Symbol,Date,Time,Open,High,Low,Close,Volume
  // Example: EURUSD,2026-03-02,16:54:08,1.17674,1.1796,1.16873,1.16882,
  const cols = String(line || '').trim().split(',');
  if (cols.length < 7) return null;

  const [symbol, date, time, open, high, low, close, volume] = cols;
  const isND = v => !v || v === 'N/D';

  const price = isND(close) ? null : Number(close);
  const openN = isND(open) ? null : Number(open);
  const highN = isND(high) ? null : Number(high);
  const lowN = isND(low) ? null : Number(low);
  const volumeN = isND(volume) ? null : Number(volume);

  const asOf = !isND(date)
    ? new Date(`${date}${isND(time) ? '' : `T${time}`}Z`).toISOString()
    : null;

  return {
    stooqSymbol: symbol || null,
    date: isND(date) ? null : date,
    time: isND(time) ? null : time,
    asOf,
    open: Number.isFinite(openN) ? openN : null,
    high: Number.isFinite(highN) ? highN : null,
    low: Number.isFinite(lowN) ? lowN : null,
    close: Number.isFinite(price) ? price : null,
    volume: Number.isFinite(volumeN) ? volumeN : null,
  };
}

async function fetchStooqQuote(stooqSymbol, options = {}) {
  const timeoutMs = options.timeoutMs || 15000;
  const sym = String(stooqSymbol || '').trim();
  if (!sym) return null;

  const res = await axios.get(STOOQ_QUOTE_ENDPOINT, {
    params: { s: sym, f: 'sd2t2ohlcv', h: '', e: 'csv' },
    timeout: timeoutMs,
    headers: {
      'User-Agent': 'NewsTrade/1.0',
      Accept: 'text/csv',
    },
  });

  const text = String(res.data || '').trim();
  const lines = text.split(/\r?\n/);
  if (lines.length < 2) return null;

  const parsed = parseStooqCsvLine(lines[1]);
  if (!parsed) return null;

  // If Symbol is N/D, treat as missing
  if (!parsed.stooqSymbol || parsed.date == null) return { ...parsed, close: null };
  return parsed;
}

async function fetchStooqQuotes(stooqSymbols, options = {}) {
  const symbols = [...new Set((stooqSymbols || []).filter(Boolean))];
  const concurrency = options.concurrency || 5;

  const resultsBySymbol = {};
  const errors = [];

  let idx = 0;
  async function worker() {
    while (idx < symbols.length) {
      const current = symbols[idx++];
      try {
        const q = await fetchStooqQuote(current, options);
        resultsBySymbol[current] = q;
      } catch (err) {
        errors.push(`${current}: ${err?.message || 'stooq request failed'}`);
        resultsBySymbol[current] = null;
      }
    }
  }

  const workers = Array.from({ length: Math.min(concurrency, symbols.length) }, () => worker());
  await Promise.all(workers);

  return { resultsBySymbol, errors };
}

module.exports = {
  STOOQ_QUOTE_ENDPOINT,
  fetchStooqQuote,
  fetchStooqQuotes,
};
