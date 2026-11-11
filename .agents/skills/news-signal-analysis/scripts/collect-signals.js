#!/usr/bin/env node
require('dotenv').config({ path: require('path').join(__dirname, '..', '..', '..', '..', '.env'), quiet: true });

const { runTomorrowBriefPipeline } = require('../../../../src/pipeline/tomorrowBriefPipeline');

function getArgValue(flag, fallback = null) {
  const idx = process.argv.indexOf(flag);
  if (idx === -1 || idx + 1 >= process.argv.length) {
    return fallback;
  }
  return process.argv[idx + 1];
}

function hasFlag(flag) {
  return process.argv.includes(flag);
}

function toInt(value, fallback) {
  const parsed = parseInt(value, 10);
  return Number.isNaN(parsed) ? fallback : parsed;
}

async function main() {
  const dryRun = hasFlag('--dry-run');
  const save = hasFlag('--save') || !dryRun;
  const fromNow = hasFlag('--from-now');
  const includeCalendar = !hasFlag('--no-calendar');
  const includeNews = hasFlag('--include-news') && !hasFlag('--no-news');

  const result = await runTomorrowBriefPipeline({
    fromNow,
    dateEt: getArgValue('--date', null),
    includeCalendar,
    includeNews,
    saveCalendar: save && includeCalendar,
    saveNews: save && includeNews,
    calendarMinImpact: getArgValue('--min-impact', 'medium'),
    newsMinImpact: getArgValue('--news-min-impact', 'high'),
    newsMaxAgeHours: toInt(getArgValue('--news-max-age-hours', '12'), 12),
    fetchLimit: toInt(getArgValue('--fetch-limit', '60'), 60),
    maxSignals: toInt(getArgValue('--max-signals', '20'), 20),
    maxResultsPerQuery: toInt(getArgValue('--max-results-per-query', '12'), 12),
    existingLimit: toInt(getArgValue('--existing-limit', '400'), 400),
    searchDepth: getArgValue('--search-depth', 'advanced'),
  });

  const primary = includeCalendar ? result.calendar : result.news;
  if (primary?.success === false) {
    throw new Error(primary.error || 'Primary signal source failed');
  }

  const topSignals = (primary?.signals || []).slice(0, 8).map(signal => ({
    event: signal.event,
    time: signal.time,
    symbols: signal.symbols,
    bias: signal.bias,
    recommendedAction: signal.recommendedAction,
    impact: signal.impact,
    url: signal.url,
  }));

  console.log(JSON.stringify({
    success: true,
    mode: dryRun ? 'dry-run' : 'save',
    fromNow,
    source: primary?.source || null,
    dateEt: result.dateEt,
    databaseId: result.databaseId,
    fetchedCount: primary?.fetchedCount || 0,
    upcomingCount: primary?.dayCount || 0,
    generatedCount: primary?.generatedCount || 0,
    savedCount: primary?.savedCount || 0,
    skippedCount: primary?.skippedCount || 0,
    topSignals,
    newsSummary: includeNews ? {
      fetchedCount: result.news?.fetchedCount || 0,
      generatedCount: result.news?.generatedCount || 0,
      savedCount: result.news?.savedCount || 0,
      skippedCount: result.news?.skippedCount || 0,
    } : undefined,
  }, null, 2));
}

main().catch(error => {
  console.error(JSON.stringify({ success: false, error: error.message }));
  process.exit(1);
});
