#!/usr/bin/env node
require('dotenv').config({ path: require('path').join(__dirname, '..', '.env'), quiet: true });

const fs = require('fs');
const path = require('path');
const { collectFutureOrders } = require('../src/news/futureOrderCollector');

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
  const daysAhead = parseInt(getArgValue('--days-ahead', '7'), 10) || 7;
  const minImpact = String(getArgValue('--min-impact', 'high') || 'high').toLowerCase();
  const timeout = parseInt(getArgValue('--timeout-ms', '30000'), 10) || 30000;
  const outPath = getArgValue('--out', path.join('data', 'news', 'future-orders.json'));
  const sources = parseSourcesArg(getArgValue('--sources'));
  const noWrite = hasFlag('--no-write');

  const result = await collectFutureOrders({
    daysAhead,
    minImpact,
    timeout: Math.max(timeout, 30000),
    sources,
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
        minImpact: result.minImpact,
        sources: result.sources,
        totalOrders: result.totalOrders,
        output: noWrite ? null : outPath,
        upstream: result.upstream,
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
