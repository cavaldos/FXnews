const { fetchGlobalFinancialNews } = require('../news/tavily');
const { generateSignals, IMPACT } = require('../analysis/newsSignalGenerator');
const { addSignal, getSignals, getResolvedDatabaseId, isDirectionalSignal } = require('../news-trade-db');

function mapMinImpact(input) {
  const normalized = (input || 'high').toLowerCase();
  if (normalized === 'high') return IMPACT.high;
  if (normalized === 'low') return IMPACT.low;
  return IMPACT.medium;
}

async function getExistingSourceUrls(limit = 100) {
  const existing = await getSignals({ pageSize: limit });
  return new Set(existing.map(item => item.url).filter(Boolean));
}

function getSignalUrlCandidates(signal) {
  return [...new Set([signal.url, ...(signal.sourceUrls || [])].filter(Boolean))];
}

async function runNewsTradePipeline(options = {}) {
  const saveToNotion = options.saveToNotion === true;
  const minImpact = mapMinImpact(options.minImpact);
  const databaseId = saveToNotion ? await getResolvedDatabaseId() : null;

  const fetchedNews = await fetchGlobalFinancialNews({
    limit: options.fetchLimit || 60,
    maxResultsPerQuery: options.maxResultsPerQuery || 12,
    maxAgeHours: Number.isFinite(options.maxAgeHours) ? options.maxAgeHours : 12,
    includeDomains: options.includeDomains,
    searchDepth: options.searchDepth || 'advanced',
    queries: options.queries,
  });

  const signals = generateSignals(fetchedNews, {
    minImpact,
    maxSignals: options.maxSignals || 20,
  });

  const saved = [];
  const skipped = [];
  const knownUrls = saveToNotion ? await getExistingSourceUrls(options.existingLimit || 100) : new Set();

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

    if (saveToNotion) {
      const page = await addSignal(signal);
      saved.push({ id: page.id, event: signal.event, url: signal.url });
      urlCandidates.forEach(url => knownUrls.add(url));
    }
  }

  return {
    databaseId,
    fetchedCount: fetchedNews.length,
    generatedCount: signals.length,
    savedCount: saved.length,
    skippedCount: skipped.length,
    signals,
    saved,
    skipped,
  };
}

module.exports = {
  runNewsTradePipeline,
};
