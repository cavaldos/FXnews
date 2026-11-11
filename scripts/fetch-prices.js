#!/usr/bin/env node
require('dotenv').config({ path: require('path').join(__dirname, '..', '.env'), quiet: true });

const fs = require('fs');
const path = require('path');

const { getSignals, getResolvedDatabaseId } = require('../src/news-trade-db');
const { toStooqSymbol, normalizeMt5Symbol } = require('../src/prices/mt5ToStooq');
const { fetchStooqQuotes } = require('../src/prices/stooqQuotes');

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

function toCsvRow(values) {
  return values
    .map(v => {
      const s = v == null ? '' : String(v);
      const escaped = s.replace(/"/g, '""');
      return `"${escaped}"`;
    })
    .join(',');
}

function toCsv(quotes) {
  const header = [
    'mt5Symbol',
    'provider',
    'providerSymbol',
    'price',
    'asOf',
    'open',
    'high',
    'low',
    'volume',
  ];

  const rows = [toCsvRow(header)];
  for (const q of quotes) {
    rows.push(
      toCsvRow([
        q.mt5Symbol,
        q.provider,
        q.providerSymbol,
        q.price,
        q.asOf,
        q.open,
        q.high,
        q.low,
        q.volume,
      ])
    );
  }
  return rows.join('\n') + '\n';
}

async function getSymbolsFromNotion(pageSize) {
  const signals = await getSignals({ pageSize });
  const freq = new Map();

  for (const s of signals) {
    for (const sym of s.symbols || []) {
      const k = normalizeMt5Symbol(sym);
      if (!k) continue;
      freq.set(k, (freq.get(k) || 0) + 1);
    }
  }

  const symbols = [...freq.keys()].sort();
  return { symbols, freq, signalsCount: signals.length };
}

async function main() {
  const pageSize = parseInt(getArgValue('--page-size', '100'), 10) || 100;
  const outDir = getArgValue('--out-dir', path.join('verification-data', 'market-prices'));
  const outJson = getArgValue('--out-json', path.join(outDir, 'latest.json'));
  const outCsv = getArgValue('--out-csv', path.join(outDir, 'latest.csv'));
  const format = (getArgValue('--format', 'both') || 'both').toLowerCase();
  const provider = (getArgValue('--provider', 'stooq') || 'stooq').toLowerCase();

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
    const p = provider === 'stooq' ? toStooqSymbol(s) : null;
    if (!p) unsupported.push(s);
    else mapped.push({ mt5Symbol: s, providerSymbol: p });
  }

  let resultsByProviderSymbol = {};
  let errors = [];
  if (provider === 'stooq') {
    const stooqSymbols = mapped.map(m => m.providerSymbol);
    const res = await fetchStooqQuotes(stooqSymbols, { concurrency: 5 });
    resultsByProviderSymbol = res.resultsBySymbol;
    errors = res.errors;
  } else {
    throw new Error(`Unsupported provider: ${provider}. Supported: stooq`);
  }

  const quotes = mapped
    .map(m => {
      const q = resultsByProviderSymbol[m.providerSymbol] || null;
      return {
        mt5Symbol: m.mt5Symbol,
        provider,
        providerSymbol: m.providerSymbol,
        price: q?.close ?? null,
        asOf: q?.asOf ?? null,
        open: q?.open ?? null,
        high: q?.high ?? null,
        low: q?.low ?? null,
        volume: q?.volume ?? null,
      };
    })
    .sort((a, b) => a.mt5Symbol.localeCompare(b.mt5Symbol));

  const payload = {
    generatedAt: new Date().toISOString(),
    provider,
    databaseId,
    signalsCount,
    symbolsRequested: mt5Symbols,
    symbolsSupported: mapped.map(m => m.mt5Symbol),
    symbolsUnsupported: unsupported,
    quoteErrors: errors,
    quotes,
  };

  ensureDir(path.dirname(outJson));

  if (format === 'json' || format === 'both') {
    fs.writeFileSync(outJson, JSON.stringify(payload, null, 2));
  }
  if (format === 'csv' || format === 'both') {
    fs.writeFileSync(outCsv, toCsv(quotes));
  }

  console.log(
    JSON.stringify(
      {
        success: true,
        databaseId,
        signalsCount,
        uniqueSymbols: mt5Symbols.length,
        supportedSymbols: mapped.length,
        unsupportedSymbols: unsupported.length,
        wrote: {
          json: format === 'csv' ? null : outJson,
          csv: format === 'json' ? null : outCsv,
        },
        quoteErrors: errors,
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
