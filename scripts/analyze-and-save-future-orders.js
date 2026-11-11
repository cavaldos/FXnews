#!/usr/bin/env node
require('dotenv').config({ path: require('path').join(__dirname, '..', '.env'), quiet: true });

const { runFutureOrderPipeline } = require('../src/pipeline/futureOrderPipeline');

function hasFlag(flag) {
  return process.argv.includes(flag);
}

function getArgValue(flag, fallback = null) {
  const index = process.argv.indexOf(flag);
  if (index === -1 || index + 1 >= process.argv.length) return fallback;
  return process.argv[index + 1];
}

function parseSourcesArg(value) {
  if (!value) return null;
  return value
    .split(',')
    .map(item => String(item || '').trim().toLowerCase())
    .filter(Boolean);
}

async function main() {
  const dryRun = hasFlag('--dry-run');
  const daysAhead = parseInt(getArgValue('--days-ahead', '7'), 10) || 7;
  const minImpact = String(getArgValue('--min-impact', 'high') || 'high').toLowerCase();
  const timeout = parseInt(getArgValue('--timeout-ms', '30000'), 10) || 30000;
  const existingLimit = parseInt(getArgValue('--existing-limit', '400'), 10) || 400;
  const sources = parseSourcesArg(getArgValue('--sources'));

  const result = await runFutureOrderPipeline({
    saveToNotion: !dryRun,
    daysAhead,
    minImpact,
    timeout,
    existingLimit,
    sources,
  });

  console.log(
    JSON.stringify(
      {
        success: true,
        mode: dryRun ? 'dry-run' : 'save',
        databaseId: result.databaseId,
        daysAhead: result.daysAhead,
        minImpact: result.minImpact,
        fetchedOrderCount: result.fetchedOrderCount,
        analyzedCount: result.analyzedCount,
        directionalCount: result.directionalCount,
        savedCount: result.savedCount,
        skippedCount: result.skippedCount,
        skippedByReason: result.skipped.reduce((acc, item) => {
          const reason = item.reason || 'other';
          acc[reason] = (acc[reason] || 0) + 1;
          return acc;
        }, {}),
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
