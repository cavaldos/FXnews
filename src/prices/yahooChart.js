const axios = require('axios');

const YAHOO_CHART_ENDPOINT = 'https://query2.finance.yahoo.com/v8/finance/chart';

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function isRetryableStatus(status) {
  return status === 429 || (status >= 500 && status <= 599);
}

async function fetchYahooChart(yahooSymbol, options = {}) {
  const symbol = String(yahooSymbol || '').trim();
  if (!symbol) throw new Error('Missing yahooSymbol');

  const interval = options.interval || '1m';
  const range = options.range || '1d';
  const timeoutMs = options.timeoutMs || 20000;
  const maxRetries = options.maxRetries ?? 3;

  const params = { interval };
  if (options.period1 != null && options.period2 != null) {
    params.period1 = options.period1;
    params.period2 = options.period2;
  } else {
    params.range = range;
  }

  let attempt = 0;
  while (true) {
    try {
      const res = await axios.get(`${YAHOO_CHART_ENDPOINT}/${encodeURIComponent(symbol)}`, {
        params,
        timeout: timeoutMs,
        headers: {
          'User-Agent': options.userAgent || 'Mozilla/5.0 (NewsTrade/1.0)',
          Accept: 'application/json',
        },
      });
      return res.data;
    } catch (err) {
      const status = err?.response?.status;
      attempt += 1;

      if (attempt > maxRetries || !status || !isRetryableStatus(status)) {
        throw new Error(`${symbol}: Yahoo chart request failed${status ? ` (${status})` : ''}: ${err.message}`);
      }

      const retryAfterHeader = err?.response?.headers?.['retry-after'];
      const retryAfterMs = retryAfterHeader ? Number(retryAfterHeader) * 1000 : null;
      const backoffMs = retryAfterMs || Math.min(30000, 800 * Math.pow(2, attempt));
      await sleep(backoffMs);
    }
  }
}

function parseYahooOhlcv(chartJson) {
  const result = chartJson?.chart?.result?.[0];
  if (!result) {
    const msg = chartJson?.chart?.error?.description || 'Missing chart result';
    throw new Error(`Yahoo chart parse error: ${msg}`);
  }

  const timestamps = result.timestamp || [];
  const quote = result.indicators?.quote?.[0] || {};
  const open = quote.open || [];
  const high = quote.high || [];
  const low = quote.low || [];
  const close = quote.close || [];
  const volume = quote.volume || [];

  const rows = [];
  for (let i = 0; i < timestamps.length; i++) {
    const ts = timestamps[i];
    const o = open[i];
    const h = high[i];
    const l = low[i];
    const c = close[i];

    if (o == null || h == null || l == null || c == null) {
      continue;
    }

    rows.push({
      time: ts,
      open: o,
      high: h,
      low: l,
      close: c,
      volume: volume[i] ?? null,
    });
  }

  return {
    meta: result.meta || {},
    rows,
  };
}

module.exports = {
  YAHOO_CHART_ENDPOINT,
  fetchYahooChart,
  parseYahooOhlcv,
};
