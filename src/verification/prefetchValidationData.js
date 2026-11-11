const fs = require('fs');
const path = require('path');

const { toYahooSymbol, normalizeMt5Symbol } = require('../prices/mt5ToYahoo');
const { fetchYahooChart, parseYahooOhlcv } = require('../prices/yahooChart');

function ensureDir(dirPath) {
  fs.mkdirSync(dirPath, { recursive: true });
}

function formatTimeUtc(epochSeconds) {
  const iso = new Date(epochSeconds * 1000).toISOString();
  return iso.slice(0, 19).replace('T', ' ');
}

function rowsToCsv(rows = []) {
  const header = 'Time,Open,High,Low,Close,Volume';
  const lines = [header];

  for (const row of rows) {
    lines.push([
      formatTimeUtc(row.time),
      row.open,
      row.high,
      row.low,
      row.close,
      row.volume,
    ].map(value => (value == null ? '' : String(value))).join(', '));
  }

  return lines.join('\n') + '\n';
}

function safeFileName(value = '') {
  return String(value || '').trim().replace(/[^a-z0-9_-]+/gi, '_') || 'unknown';
}

function getIntervalCandidates(interval = '1m') {
  return [...new Set([interval || '1m', '5m', '15m', '30m', '60m'])];
}

async function runWithConcurrency(items, concurrency, workerFn) {
  if (!Array.isArray(items) || items.length === 0) {
    return [];
  }

  const results = [];
  let index = 0;

  async function worker() {
    while (index < items.length) {
      const current = items[index++];
      const output = await workerFn(current);
      results.push(output);
    }
  }

  const workers = Array.from({ length: Math.min(concurrency, items.length) }, () => worker());
  await Promise.all(workers);
  return results;
}

function buildSymbolRequests(signals, options = {}) {
  const warmupMinutes = options.warmupMinutes || 15;
  const windowMinutes = options.windowMinutes || 90;
  const marketLookaheadMinutes = options.marketLookaheadMinutes || 72 * 60;
  const grouped = new Map();
  const unsupportedSymbols = new Set();
  const invalidSignals = [];

  for (const signal of signals || []) {
    const signalEpochMs = new Date(signal.time || '').getTime();
    if (Number.isNaN(signalEpochMs)) {
      invalidSignals.push({ id: signal.id, event: signal.event, time: signal.time || null });
      continue;
    }

    const signalEpochSeconds = Math.floor(signalEpochMs / 1000);
    for (const rawSymbol of Array.isArray(signal.symbols) ? signal.symbols : []) {
      const mt5Symbol = normalizeMt5Symbol(rawSymbol);
      if (!mt5Symbol) continue;

      const yahooSymbol = toYahooSymbol(mt5Symbol);
      if (!yahooSymbol) {
        unsupportedSymbols.add(mt5Symbol);
        continue;
      }

      const period1 = signalEpochSeconds - warmupMinutes * 60;
      const period2 = signalEpochSeconds + (windowMinutes + warmupMinutes + marketLookaheadMinutes) * 60;
      const existing = grouped.get(mt5Symbol);

      if (existing) {
        existing.period1 = Math.min(existing.period1, period1);
        existing.period2 = Math.max(existing.period2, period2);
        existing.signalIds.add(signal.id);
        existing.events.add(signal.event || 'Untitled event');
      } else {
        grouped.set(mt5Symbol, {
          mt5Symbol,
          yahooSymbol,
          period1,
          period2,
          signalIds: new Set([signal.id]),
          events: new Set([signal.event || 'Untitled event']),
        });
      }
    }
  }

  return {
    requests: [...grouped.values()].map(item => ({
      ...item,
      signalIds: [...item.signalIds],
      events: [...item.events],
    })),
    unsupportedSymbols: [...unsupportedSymbols].sort(),
    invalidSignals,
  };
}

async function fetchRangeWithFallback(request, options = {}) {
  const intervalCandidates = getIntervalCandidates(options.interval);
  let lastError = null;
  let lastParsed = null;
  let lastInterval = intervalCandidates[intervalCandidates.length - 1];

  for (const interval of intervalCandidates) {
    lastInterval = interval;

    try {
      const chart = await fetchYahooChart(request.yahooSymbol, {
        interval,
        period1: request.period1,
        period2: request.period2,
        maxRetries: options.maxRetries ?? 4,
      });
      const parsed = parseYahooOhlcv(chart);
      lastParsed = parsed;

      if ((parsed.rows || []).length > 0) {
        return {
          usedInterval: interval,
          rows: parsed.rows || [],
          meta: parsed.meta || {},
          fetchError: null,
        };
      }
    } catch (error) {
      lastError = error;
    }
  }

  return {
    usedInterval: lastInterval,
    rows: lastParsed?.rows || [],
    meta: lastParsed?.meta || {},
    fetchError: lastError ? lastError.message : null,
  };
}

async function prefetchValidationData(signals, options = {}) {
  const runId = options.runId || new Date().toISOString().replace(/[:.]/g, '-');
  const outDir = path.resolve(options.outDir || path.join(process.cwd(), 'verification-data', 'validation-runs', runId));
  const rawDir = path.join(outDir, 'raw');
  const concurrency = options.prefetchConcurrency || 2;

  ensureDir(rawDir);

  const { requests, unsupportedSymbols, invalidSignals } = buildSymbolRequests(signals, options);
  const results = await runWithConcurrency(requests, concurrency, async request => {
    const fetched = await fetchRangeWithFallback(request, options);
    const outPath = path.join(rawDir, `${safeFileName(request.mt5Symbol)}_${safeFileName(fetched.usedInterval || options.interval || '1m')}.csv`);
    fs.writeFileSync(outPath, rowsToCsv(fetched.rows));

    return {
      mt5Symbol: request.mt5Symbol,
      yahooSymbol: request.yahooSymbol,
      period1: request.period1,
      period2: request.period2,
      signalIds: request.signalIds,
      events: request.events,
      usedInterval: fetched.usedInterval,
      rows: fetched.rows,
      meta: fetched.meta,
      fetchError: fetched.fetchError,
      outPath,
    };
  });

  const dataBySymbol = Object.fromEntries(results.map(item => [normalizeMt5Symbol(item.mt5Symbol), item]));
  const manifest = {
    generatedAt: new Date().toISOString(),
    runId,
    intervalCandidates: getIntervalCandidates(options.interval),
    windowMinutes: options.windowMinutes || 90,
    warmupMinutes: options.warmupMinutes || 15,
    marketLookaheadMinutes: options.marketLookaheadMinutes || 72 * 60,
    signalsCount: Array.isArray(signals) ? signals.length : 0,
    requestedSymbols: requests.map(item => item.mt5Symbol),
    unsupportedSymbols,
    invalidSignals,
    successCount: results.filter(item => item.rows.length > 0).length,
    emptyCount: results.filter(item => item.rows.length === 0 && !item.fetchError).length,
    errorCount: results.filter(item => item.fetchError).length,
    results: results.map(item => ({
      mt5Symbol: item.mt5Symbol,
      yahooSymbol: item.yahooSymbol,
      period1: new Date(item.period1 * 1000).toISOString(),
      period2: new Date(item.period2 * 1000).toISOString(),
      signalCount: item.signalIds.length,
      usedInterval: item.usedInterval,
      rows: item.rows.length,
      outPath: item.outPath,
      fetchError: item.fetchError,
    })),
  };

  const manifestPath = path.join(outDir, 'manifest.json');
  fs.writeFileSync(manifestPath, JSON.stringify(manifest, null, 2));

  return {
    runId,
    outDir,
    rawDir,
    manifestPath,
    dataBySymbol,
    manifest,
  };
}

module.exports = {
  prefetchValidationData,
  buildSymbolRequests,
};
