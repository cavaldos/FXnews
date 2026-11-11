const axios = require('axios');

const YAHOO_QUOTE_ENDPOINT = 'https://query1.finance.yahoo.com/v7/finance/quote';

function chunk(arr, size) {
  const out = [];
  for (let i = 0; i < (arr || []).length; i += size) {
    out.push(arr.slice(i, i + size));
  }
  return out;
}

function toIsoFromEpochSeconds(epochSeconds) {
  if (!epochSeconds || typeof epochSeconds !== 'number') return null;
  return new Date(epochSeconds * 1000).toISOString();
}

async function fetchYahooQuotes(yahooSymbols, options = {}) {
  const symbols = [...new Set((yahooSymbols || []).filter(Boolean))];
  const timeoutMs = options.timeoutMs || 15000;
  const batchSize = options.batchSize || 50;

  const resultsBySymbol = {};
  const errors = [];

  for (const batch of chunk(symbols, batchSize)) {
    try {
      const res = await axios.get(YAHOO_QUOTE_ENDPOINT, {
        params: { symbols: batch.join(',') },
        timeout: timeoutMs,
        headers: {
          'User-Agent': 'NewsTrade/1.0',
          Accept: 'application/json',
        },
      });

      const items = res.data?.quoteResponse?.result || [];
      for (const item of items) {
        if (!item?.symbol) continue;
        resultsBySymbol[item.symbol] = {
          yahooSymbol: item.symbol,
          price: item.regularMarketPrice ?? null,
          currency: item.currency ?? null,
          marketTime: toIsoFromEpochSeconds(item.regularMarketTime),
          marketState: item.marketState ?? null,
          exchange: item.fullExchangeName ?? item.exchange ?? null,
          shortName: item.shortName ?? null,
          longName: item.longName ?? null,
        };
      }
    } catch (err) {
      errors.push(err?.message || 'Yahoo quote request failed');
    }
  }

  return { resultsBySymbol, errors };
}

module.exports = {
  YAHOO_QUOTE_ENDPOINT,
  fetchYahooQuotes,
};
