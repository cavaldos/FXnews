#!/usr/bin/env node
require('dotenv').config({ path: require('path').join(__dirname, '..', '.env'), quiet: true });

const fs = require('fs');
const path = require('path');

const { getSignals, getResolvedDatabaseId } = require('../src/news-trade-db');
const { toYahooSymbol, normalizeMt5Symbol } = require('../src/prices/mt5ToYahoo');
const { fetchYahooChart, parseYahooOhlcv } = require('../src/prices/yahooChart');

function hasFlag(flag) {
  return process.argv.includes(flag);
}

function getArgValue(flag, fallback = null) {
  const idx = process.argv.indexOf(flag);
  if (idx === -1 || idx + 1 >= process.argv.length) return fallback;
  return process.argv[idx + 1];
}

function ensureDir(dirPath) {
  fs.mkdirSync(dirPath, { recursive: true });
}

function formatTimeUtc(epochSeconds) {
  const iso = new Date(epochSeconds * 1000).toISOString();
  return iso.slice(0, 19).replace('T', ' ');
}

function rowsToCsv(rows) {
  const header = 'Time,Open,High,Low,Close,Volume';
  const lines = [header];
  for (const r of rows) {
    const parts = [
      formatTimeUtc(r.time),
      r.open,
      r.high,
      r.low,
      r.close,
      r.volume,
    ].map(v => (v == null ? '' : String(v)));
    lines.push(parts.join(', '));
  }
  return lines.join('\n') + '\n';
}

async function getSymbolsFromNotion(pageSize) {
  const signals = await getSignals({ pageSize });
  const symbols = new Set();
  for (const s of signals) {
    for (const sym of s.symbols || []) {
      const k = normalizeMt5Symbol(sym);
      if (k) symbols.add(k);
    }
  }
  return { symbols: [...symbols].sort(), signalsCount: signals.length };
}

async function runWithConcurrency(items, concurrency, workerFn) {
  const results = [];
  let idx = 0;

  async function worker() {
    while (idx < items.length) {
      const current = items[idx++];
      const out = await workerFn(current);
      results.push(out);
    }
  }

  const workers = Array.from({ length: Math.min(concurrency, items.length) }, () => worker());
  await Promise.all(workers);
  return results;
}

async function main() {
  const pageSize = parseInt(getArgValue('--page-size', '100'), 10) || 100;
  const interval = getArgValue('--interval', '1m');
  const range = getArgValue('--range', '1d');
  const outDir = getArgValue('--out-dir', path.join('verification-data', 'ohlc'));
  const concurrency = parseInt(getArgValue('--concurrency', '2'), 10) || 2;
  const dryRun = hasFlag('--dry-run');

  let mt5Symbols;
  let signalsCount = null;
  let databaseId = null;

  const symbolsArg = getArgValue('--symbols');
  if (symbolsArg) {
    mt5Symbols = symbolsArg.split(',').map(s => normalizeMt5Symbol(s)).filter(Boolean);
  } else {
    databaseId = await getResolvedDatabaseId();
    const notion = await getSymbolsFromNotion(pageSize);
    mt5Symbols = notion.symbols;
    signalsCount = notion.signalsCount;
  }

  const mapped = [];
  const unsupported = [];
  for (const s of mt5Symbols) {
    const y = toYahooSymbol(s);
    if (!y) unsupported.push(s);
    else mapped.push({ mt5Symbol: s, yahooSymbol: y });
  }

  ensureDir(outDir);

  const results = await runWithConcurrency(mapped, concurrency, async item => {
    const outPath = path.join(outDir, `${item.mt5Symbol}_${interval}_${range}.csv`);

    try {
      const chart = await fetchYahooChart(item.yahooSymbol, { interval, range, maxRetries: 4 });
      const parsed = parseYahooOhlcv(chart);

      if (!dryRun) {
        fs.writeFileSync(outPath, rowsToCsv(parsed.rows));
      }

      return {
        mt5Symbol: item.mt5Symbol,
        yahooSymbol: item.yahooSymbol,
        outPath,
        rows: parsed.rows.length,
        meta: {
          exchangeName: parsed.meta?.exchangeName || null,
          instrumentType: parsed.meta?.instrumentType || null,
          currency: parsed.meta?.currency || null,
          gmtoffset: parsed.meta?.gmtoffset ?? null,
        },
      };
    } catch (err) {
      return {
        mt5Symbol: item.mt5Symbol,
        yahooSymbol: item.yahooSymbol,
        outPath,
        error: err.message,
      };
    }
  });

  const ok = results.filter(r => !r.error);
  const failed = results.filter(r => r.error);

  const manifest = {
    generatedAt: new Date().toISOString(),
    provider: 'yahoo_chart',
    databaseId,
    signalsCount,
    interval,
    range,
    symbolsRequested: mt5Symbols,
    symbolsSupported: mapped.map(m => m.mt5Symbol),
    symbolsUnsupported: unsupported,
    dryRun,
    successCount: ok.length,
    errorCount: failed.length,
    results,
  };

  const manifestPath = path.join(outDir, `manifest_${interval}_${range}.json`);
  if (!dryRun) {
    fs.writeFileSync(manifestPath, JSON.stringify(manifest, null, 2));
  }

  console.log(
    JSON.stringify(
      {
        success: true,
        interval,
        range,
        uniqueSymbols: mt5Symbols.length,
        supportedSymbols: mapped.length,
        unsupportedSymbols: unsupported.length,
        wrote: dryRun ? null : { outDir, manifest: manifestPath },
        ok: ok.map(r => ({ mt5Symbol: r.mt5Symbol, rows: r.rows, outPath: r.outPath })),
        failed: failed.map(r => ({ mt5Symbol: r.mt5Symbol, error: r.error })),
      },
      null,
      2
    )
  );
}

main().catch(err => {
  console.error(JSON.stringify({ success: false, error: err.message }, null, 2));
  process.exit(1);
});
