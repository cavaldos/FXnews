const { collectFutureNews } = require('./futureNewsCollector');
const { FOREXFACTORY_THISWEEK_JSON_URL } = require('./forexfactoryCalendar');

const IMPACT_ORDER = {
  low: 1,
  medium: 2,
  high: 3,
};

const CURRENCY_TO_SYMBOL = {
  USD: 'EURUSD',
  EUR: 'EURUSD',
  GBP: 'GBPUSD',
  JPY: 'USDJPY',
  AUD: 'AUDUSD',
  NZD: 'NZDUSD',
  CAD: 'USDCAD',
  CHF: 'USDCHF',
};

function normalizeImpact(value) {
  const key = String(value || '').trim().toLowerCase();
  if (key === 'high') return 'high';
  if (key === 'medium') return 'medium';
  return 'low';
}

function getImpactThreshold(minImpact = 'high') {
  const key = normalizeImpact(minImpact);
  return IMPACT_ORDER[key] || IMPACT_ORDER.high;
}

function parseEventTime(event) {
  if (!event?.eventTimeUtc) return null;
  const parsed = new Date(event.eventTimeUtc);
  if (!Number.isFinite(parsed.getTime())) return null;
  return parsed;
}

function clampFutureWindow(date, now, daysAhead) {
  const nowMs = now.getTime();
  const upperBoundMs = nowMs + daysAhead * 24 * 60 * 60 * 1000;
  const ms = date.getTime();
  return ms > nowMs && ms <= upperBoundMs;
}

function inferCurrency(event) {
  const currency = String(event?.currency || '').trim().toUpperCase();
  if (currency) return currency;

  const country = String(event?.country || '').trim().toUpperCase();
  if (country.length === 3) return country;

  return 'USD';
}

function inferSymbol(currency) {
  return CURRENCY_TO_SYMBOL[currency] || 'US500';
}

function buildEventUrl(event) {
  const source = String(event?.source || '').trim().toLowerCase();
  const title = String(event?.title || '').trim();
  const time = String(event?.eventTimeUtc || event?.displayTime || '').trim();
  const country = String(event?.country || 'ALL').trim().toUpperCase();
  const key = `${source}|${country}|${time}|${title}`;
  const encodedKey = encodeURIComponent(key);

  if (event?.url) {
    return `${event.url}#${encodedKey}`;
  }

  if (source.includes('forexfactory')) {
    return `${FOREXFACTORY_THISWEEK_JSON_URL}#${encodedKey}`;
  }

  if (source.includes('investing')) return `https://www.investing.com/economic-calendar/#${encodedKey}`;
  if (source.includes('babypips')) return `https://www.babypips.com/economic-calendar#${encodedKey}`;
  if (source.includes('forexlive')) return `https://www.forexlive.com/economic-calendar/#${encodedKey}`;

  return null;
}

function buildPlaybook(currency, symbol) {
  const localStronger = `If ${currency} is stronger than forecast: BUY ${symbol}`;
  const localWeaker = `If ${currency} is weaker than forecast: SELL ${symbol}`;

  return {
    localStronger,
    localWeaker,
    note: 'This is a pre-release watch order. Confirm actual vs forecast before entering.',
  };
}

function toOrderCandidate(event, now) {
  const impact = normalizeImpact(event.impact);
  const currency = inferCurrency(event);
  const symbol = inferSymbol(currency);
  const eventTime = parseEventTime(event);

  const sourceUrl = buildEventUrl(event);

  return {
    id: [event.source || 'unknown', event.title || 'event', event.eventTimeUtc || event.displayTime || 'na']
      .join('|')
      .toLowerCase(),
    source: event.source || 'Unknown',
    title: String(event.title || '').trim(),
    status: 'PENDING',
    orderType: 'WATCH_EVENT',
    impact,
    currency,
    symbol,
    eventTimeUtc: eventTime ? eventTime.toISOString() : null,
    displayTime: event.displayTime || null,
    createdAtUtc: now.toISOString(),
    sourceUrl,
    playbook: buildPlaybook(currency, symbol),
    raw: {
      forecast: event.forecast || null,
      previous: event.previous || null,
      actual: event.actual || null,
      url: sourceUrl,
      kind: event.kind || null,
    },
  };
}

async function collectFutureOrders(options = {}) {
  const now = options.now instanceof Date ? options.now : new Date();
  const daysAhead = Number.isFinite(options.daysAhead) ? options.daysAhead : 7;
  const minImpactThreshold = getImpactThreshold(options.minImpact || 'high');
  const requireEventTime = options.requireEventTime !== false;

  const futureNews = await collectFutureNews({
    ...options,
    now,
    daysAhead,
    sources: options.sources || ['forexfactory'],
  });

  const calendarEvents = (futureNews.events || [])
    .filter(event => String(event.kind || '').toLowerCase() === 'calendar')
    .filter(event => IMPACT_ORDER[normalizeImpact(event.impact)] >= minImpactThreshold);

  let skippedUndated = 0;
  let skippedOutOfWindow = 0;

  const timeFilteredEvents = calendarEvents.filter(event => {
    const eventTime = parseEventTime(event);
    if (!eventTime) {
      if (requireEventTime) {
        skippedUndated += 1;
        return false;
      }
      return true;
    }

    if (!clampFutureWindow(eventTime, now, daysAhead)) {
      skippedOutOfWindow += 1;
      return false;
    }

    return true;
  });

  const candidates = timeFilteredEvents
    .map(event => toOrderCandidate(event, now))
    .sort((a, b) => {
      const ta = a.eventTimeUtc ? new Date(a.eventTimeUtc).getTime() : Number.MAX_SAFE_INTEGER;
      const tb = b.eventTimeUtc ? new Date(b.eventTimeUtc).getTime() : Number.MAX_SAFE_INTEGER;
      return ta - tb;
    });

  return {
    generatedAt: new Date().toISOString(),
    nowUtc: now.toISOString(),
    daysAhead,
    minImpact: options.minImpact || 'high',
    sources: futureNews.sourcesRequested,
    totalOrders: candidates.length,
    orders: candidates,
    skippedUndated,
    skippedOutOfWindow,
    upstream: {
      totalEvents: futureNews.totalEvents,
      countsBySource: futureNews.countsBySource,
      sourcesFailed: futureNews.sourcesFailed,
      errors: futureNews.errors,
    },
  };
}

module.exports = {
  collectFutureOrders,
};
