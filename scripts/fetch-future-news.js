#!/usr/bin/env node
require('dotenv').config({ path: require('path').join(__dirname, '..', '.env'), quiet: true });

const fs = require('fs');
const path = require('path');
const { collectFutureNews } = require('../src/news/futureNewsCollector');

function hasFlag(flag) {
  return process.argv.includes(flag);
}

function getArgValue(flag, fallback = null) {
  const index = process.argv.indexOf(flag);
  if (index === -1 || index + 1 >= process.argv.length) return fallback;
  return process.argv[index + 1];
}

function ensureParentDir(filePath) {
  const parent = path.dirname(filePath);
  fs.mkdirSync(parent, { recursive: true });
}

function parseSourcesArg(value) {
  if (!value) return null;
  return value
    .split(',')
    .map(item => String(item || '').trim().toLowerCase())
    .filter(Boolean);
}

async function main() {
  const daysAhead = parseInt(getArgValue('--days-ahead', '14'), 10) || 14;
  const timeout = parseInt(getArgValue('--timeout-ms', '30000'), 10) || 30000;
  const sourceRetries = parseInt(getArgValue('--source-retries', '1'), 10) || 1;
  const outPath = getArgValue('--out', path.join('data', 'news', 'future-news.json'));
  const sources = parseSourcesArg(getArgValue('--sources'));
  const noWrite = hasFlag('--no-write');

  const result = await collectFutureNews({
    daysAhead,
    timeout: Math.max(timeout, 45000),
    sourceRetries,
    sources,
    tradingEconomicsKey: process.env.TRADING_ECONOMICS_KEY,
  });

  if (!noWrite) {
    ensureParentDir(outPath);
    fs.writeFileSync(outPath, JSON.stringify(result, null, 2));
  }

  console.log(
    JSON.stringify(
      {
        success: true,
        generatedAt: result.generatedAt,
        daysAhead: result.daysAhead,
        sourcesRequested: result.sourcesRequested,
        sourcesSucceeded: result.sourcesSucceeded,
        sourcesFailed: result.sourcesFailed,
        countsBySource: result.countsBySource,
        totalEvents: result.totalEvents,
        output: noWrite ? null : outPath,
        errors: result.errors,
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
