const { collectFutureOrders } = require('../news/futureOrderCollector');
const { generateFutureOrderSignals } = require('../analysis/futureOrderSignalGenerator');
const { addSignal, getSignals, getResolvedDatabaseId, isDirectionalSignal } = require('../news-trade-db');

async function getExistingSourceUrls(limit = 400) {
  const existing = await getSignals({ pageSize: limit });
  return new Set(existing.map(item => item.url).filter(Boolean));
}

function getSignalUrlCandidates(signal) {
  return [...new Set([signal.url, ...(signal.sourceUrls || [])].filter(Boolean))];
}

async function runFutureOrderPipeline(options = {}) {
  const saveToNotion = options.saveToNotion !== false;
  const existingLimit = Number.isFinite(options.existingLimit) ? options.existingLimit : 400;
  const databaseId = saveToNotion ? await getResolvedDatabaseId() : null;

  const collected = await collectFutureOrders({
    daysAhead: Number.isFinite(options.daysAhead) ? options.daysAhead : 7,
    minImpact: options.minImpact || 'high',
    timeout: options.timeout,
    sources: options.sources,
  });

  const allSignals = generateFutureOrderSignals(collected.orders || []);

  const saved = [];
  const skipped = [];
  const knownUrls = saveToNotion ? await getExistingSourceUrls(existingLimit) : new Set();

  for (const signal of allSignals) {
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
      saved.push({ id: page.id, event: signal.event, url: signal.url || null, bias: signal.bias });
      urlCandidates.forEach(url => knownUrls.add(url));
    }
  }

  return {
    databaseId,
    collectedAt: collected.generatedAt,
    daysAhead: collected.daysAhead,
    minImpact: collected.minImpact,
    fetchedOrderCount: collected.totalOrders,
    analyzedCount: allSignals.length,
    directionalCount: allSignals.filter(item => isDirectionalSignal(item)).length,
    savedCount: saved.length,
    skippedCount: skipped.length,
    saved,
    skipped,
    upstream: collected.upstream,
  };
}

module.exports = {
  runFutureOrderPipeline,
};
