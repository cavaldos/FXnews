const { addDays } = require('date-fns');
const { formatInTimeZone } = require('date-fns-tz');

const { runNewsTradePipeline } = require('./newsTradePipeline');
const { addSignal, getSignals, getResolvedDatabaseId, isDirectionalSignal } = require('../news-trade-db');
const {
  ET_TIMEZONE,
  fetchForexFactoryThisWeek,
  filterEventsForEtDay,
  filterEventsOnOrAfter,
} = require('../news/forexfactoryCalendar');
const { generateCalendarSignals } = require('../analysis/calendarSignalGenerator');

function getTomorrowEtDayString(now = new Date()) {
  const todayEt = formatInTimeZone(now, ET_TIMEZONE, 'yyyy-MM-dd');

  // Do day math in UTC to avoid host timezone quirks.
  const baseUtc = new Date(`${todayEt}T00:00:00Z`);
  const tomorrowUtc = addDays(baseUtc, 1);
  return tomorrowUtc.toISOString().slice(0, 10);
}

function getTodayEtDayString(now = new Date()) {
  return formatInTimeZone(now, ET_TIMEZONE, 'yyyy-MM-dd');
}

async function getExistingSourceUrls(limit = 200) {
  const existing = await getSignals({ pageSize: limit });
  return new Set(existing.map(item => item.url).filter(Boolean));
}

function getSignalUrlCandidates(signal) {
  return [...new Set([signal.url, ...(signal.sourceUrls || [])].filter(Boolean))];
}

async function saveSignalsToNotion(signals, options = {}) {
  const pageSize = options.existingLimit || 400;
  const knownUrls = await getExistingSourceUrls(pageSize);

  const saved = [];
  const skipped = [];

  for (const signal of signals) {
    if (!isDirectionalSignal(signal)) {
      skipped.push({ reason: 'neutral_signal', event: signal.event, url: signal.url || null });
      continue;
    }

    const urlCandidates = getSignalUrlCandidates(signal);

    if (urlCandidates.length === 0) {
      skipped.push({ reason: 'missing_url', event: signal.event });
      continue;
    }

    const duplicateUrl = urlCandidates.find(url => knownUrls.has(url));
    if (duplicateUrl) {
      skipped.push({ reason: 'duplicate_url', event: signal.event, url: duplicateUrl });
      continue;
    }

    const page = await addSignal(signal);
    saved.push({ id: page.id, event: signal.event, url: signal.url });
    urlCandidates.forEach(url => knownUrls.add(url));
  }

  return { saved, skipped };
}

async function runTomorrowBriefPipeline(options = {}) {
  const now = options.now instanceof Date ? options.now : new Date();
  const targetDateEt = String(
    options.dateEt || (options.fromNow ? getTodayEtDayString(now) : getTomorrowEtDayString(now))
  ).trim();
  const includeNews = options.includeNews !== false;
  const includeCalendar = options.includeCalendar !== false;
  const saveCalendar = options.saveCalendar === true;
  const saveNews = options.saveNews === true;

  const result = {
    success: true,
    dateEt: targetDateEt,
    databaseId: null,
    calendar: null,
    news: null,
  };

  if (saveCalendar || saveNews) {
    result.databaseId = await getResolvedDatabaseId();
  }

  if (includeCalendar) {
    try {
      const allEvents = await fetchForexFactoryThisWeek({ timeout: options.calendarTimeout });
      const dayEvents = options.fromNow
        ? filterEventsOnOrAfter(filterEventsForEtDay(allEvents, targetDateEt), now)
        : filterEventsForEtDay(allEvents, targetDateEt);
      const calendarSignals = generateCalendarSignals(dayEvents, {
        minImpact: options.calendarMinImpact || 'high',
      });

      let saveInfo = { saved: [], skipped: [] };
      if (saveCalendar) {
        saveInfo = await saveSignalsToNotion(calendarSignals, { existingLimit: options.existingLimit || 400 });
      }

      result.calendar = {
        success: true,
        source: 'forexfactory',
        fetchedCount: allEvents.length,
        dayCount: dayEvents.length,
        generatedCount: calendarSignals.length,
        savedCount: saveInfo.saved.length,
        skippedCount: saveInfo.skipped.length,
        signals: calendarSignals,
        saved: saveInfo.saved,
        skipped: saveInfo.skipped,
      };
    } catch (error) {
      result.calendar = {
        success: false,
        source: 'forexfactory',
        error: error.message,
      };
    }
  }

  if (includeNews) {
    try {
      const news = await runNewsTradePipeline({
        saveToNotion: saveNews,
        minImpact: options.newsMinImpact || 'high',
        maxAgeHours: options.newsMaxAgeHours,
        fetchLimit: options.fetchLimit,
        maxSignals: options.maxSignals,
        maxResultsPerQuery: options.maxResultsPerQuery,
        existingLimit: options.existingLimit,
        searchDepth: options.searchDepth,
        queries: options.queries,
      });
      result.news = {
        success: true,
        source: 'tavily',
        ...news,
      };
    } catch (error) {
      result.news = {
        success: false,
        source: 'tavily',
        error: error.message,
      };
    }
  }

  return result;
}

module.exports = {
  getTodayEtDayString,
  getTomorrowEtDayString,
  runTomorrowBriefPipeline,
};
