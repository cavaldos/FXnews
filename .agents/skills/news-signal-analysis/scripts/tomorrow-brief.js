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
  const dryRun = hasFlag('--dry-run') || (!hasFlag('--save') && !hasFlag('--save-calendar') && !hasFlag('--save-news'));

  const saveAll = hasFlag('--save') && !dryRun;
  const saveCalendar = hasFlag('--save-calendar') || saveAll;
  const saveNews = hasFlag('--save-news') || saveAll;
  const fromNow = hasFlag('--from-now');

  const includeCalendar = !hasFlag('--no-calendar');
  const includeNews = hasFlag('--include-news') && !hasFlag('--no-news');

  const result = await runTomorrowBriefPipeline({
    fromNow,
    dateEt: getArgValue('--date', null),
    includeCalendar,
    includeNews,
    saveCalendar,
    saveNews,
    calendarMinImpact: getArgValue('--calendar-min-impact', 'medium'),
    newsMinImpact: getArgValue('--min-impact', 'high'),
    newsMaxAgeHours: toInt(getArgValue('--news-max-age-hours', '48'), 48),
    fetchLimit: toInt(getArgValue('--fetch-limit', '60'), 60),
    maxSignals: toInt(getArgValue('--max-signals', '20'), 20),
    maxResultsPerQuery: toInt(getArgValue('--max-results-per-query', '12'), 12),
    existingLimit: toInt(getArgValue('--existing-limit', '400'), 400),
    searchDepth: getArgValue('--search-depth', 'advanced'),
  });

  const topCalendar = Array.isArray(result.calendar?.signals)
    ? result.calendar.signals.slice(0, 12).map(s => ({
      event: s.event,
      time: s.time,
      recommendedAction: s.recommendedAction,
      impact: s.impact,
      symbols: s.symbols,
      url: s.url,
    }))
    : [];

  const topNews = Array.isArray(result.news?.signals)
    ? result.news.signals.slice(0, 8).map(s => ({
      event: s.event,
      time: s.time,
      bias: s.bias,
      recommendedAction: s.recommendedAction,
      impact: s.impact,
      symbols: s.symbols,
      url: s.url,
    }))
    : [];

  console.log(JSON.stringify({
    success: true,
    mode: (saveCalendar || saveNews) ? 'save' : 'dry-run',
    fromNow,
    dateEt: result.dateEt,
    databaseId: result.databaseId,
    calendar: result.calendar ? {
      ...result.calendar,
      topSignals: topCalendar,
      // keep full list in signals
    } : null,
    news: result.news ? {
      ...result.news,
      topSignals: topNews,
      // keep full list in signals
    } : null,
  }, null, 2));
}

main().catch(error => {
  console.error(JSON.stringify({ success: false, error: error.message }));
  process.exit(1);
});
