const { FOREXFACTORY_THISWEEK_JSON_URL, toEtDateTime } = require('../news/forexfactoryCalendar');
const { deriveRecommendedAction } = require('./recommendedAction');
const { normalizeSignalSymbols } = require('./symbolSelector');

const BIAS = {
  bullish: '🟢 Bullish',
  bearish: '🔴 Bearish',
  neutral: '🟡 Neutral',
};

const IMPACT = {
  high: '🔥 High',
  medium: '⚡ Medium',
  low: '💧 Low',
};

const IMPACT_ORDER = {
  high: 3,
  medium: 2,
  low: 1,
};

const COUNTRY_TO_SYMBOLS = {
  USD: ['EURUSD'],
  EUR: ['EURUSD'],
  GBP: ['GBPUSD'],
  JPY: ['USDJPY'],
  AUD: ['AUDUSD'],
  NZD: ['NZDUSD'],
  CAD: ['USDCAD'],
  CHF: ['USDCHF'],
};

const HIGHER_IS_BETTER_PATTERNS = [
  /gdp/i,
  /employment change/i,
  /non-farm/i,
  /payroll/i,
  /retail sales/i,
  /pmi/i,
  /business confidence/i,
  /job openings/i,
  /average hourly earnings/i,
  /consumer confidence/i,
  /trade balance/i,
  /industrial production/i,
  /manufacturing production/i,
  /cpi/i,
  /ppi/i,
  /inflation/i,
];

const LOWER_IS_BETTER_PATTERNS = [
  /unemployment rate/i,
  /jobless claims/i,
  /initial claims/i,
  /continuing claims/i,
];

function mapForexFactoryImpactToNotion(input) {
  const value = String(input || '').trim().toLowerCase();
  if (value === 'high') return IMPACT.high;
  if (value === 'medium') return IMPACT.medium;
  return IMPACT.low;
}

function inferSymbolsFromCountry(country) {
  const key = String(country || '').trim().toUpperCase();
  if (COUNTRY_TO_SYMBOLS[key]) {
    return normalizeSignalSymbols(COUNTRY_TO_SYMBOLS[key], { text: key });
  }
  return ['US500'];
}

function buildForexFactoryEventUrl(event) {
  const key = `${event.country || 'ALL'}|${event.date || ''}|${event.title || ''}`;
  return `${FOREXFACTORY_THISWEEK_JSON_URL}#${encodeURIComponent(key)}`;
}

function buildDetails(event, recommendedAction) {
  const isReleased = Boolean(String(event.actual || '').trim());
  const parts = [
    'Calendar: ForexFactory (weekly feed).',
    `Impact: ${event.impact || 'Low'}.`,
    recommendedAction ? `Recommended action: ${recommendedAction}.` : null,
    isReleased ? `Actual: ${event.actual}.` : 'Status: upcoming release, actual value not available yet.',
    event.forecast ? `Forecast: ${event.forecast}.` : null,
    event.previous ? `Previous: ${event.previous}.` : null,
    isReleased
      ? 'Bias note: released events use the actual-versus-forecast surprise when direction is clear.'
      : 'Bias note: future calendar events are skipped until the release confirms direction.'
  ].filter(Boolean);

  return parts.join(' ');
}

function parseEconomicValue(raw) {
  const input = String(raw || '').trim();
  if (!input) return null;

  const normalized = input.replace(/,/g, '').replace(/\s+/g, '');
  const match = normalized.match(/^(-?\d+(?:\.\d+)?)([KMBT%]?)$/i);
  if (!match) return null;

  const value = Number(match[1]);
  if (!Number.isFinite(value)) return null;

  const suffix = match[2].toUpperCase();
  if (suffix === 'K') return value * 1e3;
  if (suffix === 'M') return value * 1e6;
  if (suffix === 'B') return value * 1e9;
  if (suffix === 'T') return value * 1e12;
  return value;
}

function getCalendarSurpriseDirection(event) {
  const title = String(event.title || '');
  const actual = parseEconomicValue(event.actual);
  const forecast = parseEconomicValue(event.forecast);

  if (actual == null || forecast == null || actual === forecast) {
    return null;
  }

  const higherIsBetter = HIGHER_IS_BETTER_PATTERNS.some(pattern => pattern.test(title));
  const lowerIsBetter = LOWER_IS_BETTER_PATTERNS.some(pattern => pattern.test(title));

  if (!higherIsBetter && !lowerIsBetter) {
    return null;
  }

  const actualHigher = actual > forecast;
  if (higherIsBetter) {
    return actualHigher ? 'stronger_local_currency' : 'weaker_local_currency';
  }

  return actualHigher ? 'weaker_local_currency' : 'stronger_local_currency';
}

function inferCalendarBias(event, country, symbols) {
  const direction = getCalendarSurpriseDirection(event);
  if (!direction) return BIAS.neutral;

  const primarySymbol = (symbols || [])[0] || '';
  const localStrengthBullishSymbols = new Set(['EURUSD', 'GBPUSD', 'AUDUSD', 'NZDUSD']);
  const localStrengthBearishSymbols = new Set(['EURUSD', 'XAUUSD', 'USDJPY', 'USDCAD', 'USDCHF']);

  if (country === 'USD') {
    return direction === 'stronger_local_currency' ? BIAS.bearish : BIAS.bullish;
  }

  if (localStrengthBullishSymbols.has(primarySymbol)) {
    return direction === 'stronger_local_currency' ? BIAS.bullish : BIAS.bearish;
  }

  if (localStrengthBearishSymbols.has(primarySymbol)) {
    return direction === 'stronger_local_currency' ? BIAS.bearish : BIAS.bullish;
  }

  return BIAS.neutral;
}

function toCalendarSignal(event) {
  const title = String(event.title || '').trim();
  const country = String(event.country || '').trim().toUpperCase();
  const time = toEtDateTime(event.date);
  const impact = mapForexFactoryImpactToNotion(event.impact);
  const symbols = inferSymbolsFromCountry(country).slice(0, 1);
  const bias = inferCalendarBias(event, country, symbols);
  const recommendedAction = deriveRecommendedAction({ bias, impact, corroborationCount: 1 });

  return {
    event: `[${country || 'ALL'}] ${title}`.slice(0, 120),
    symbols,
    time,
    bias,
    impact,
    recommendedAction,
    url: buildForexFactoryEventUrl(event),
    details: buildDetails(event, recommendedAction).slice(0, 2000),
    status: '👀 Watching',
  };
}

function generateCalendarSignals(events, options = {}) {
  const minImpact = String(options.minImpact || 'high').toLowerCase();
  const threshold = IMPACT_ORDER[minImpact] || IMPACT_ORDER.medium;

  const signals = (events || [])
    .map(item => {
      const signal = toCalendarSignal(item);
      return { ...signal, __rawImpact: String(item.impact || '').toLowerCase() };
    })
    .filter(signal => {
      const impactKey = signal.impact === IMPACT.high ? 'high' : signal.impact === IMPACT.medium ? 'medium' : 'low';
      return (IMPACT_ORDER[impactKey] || 1) >= threshold;
    })
    .filter(signal => signal.bias !== BIAS.neutral)
    .map(({ __rawImpact, ...rest }) => rest);

  return signals;
}

module.exports = {
  BIAS,
  IMPACT,
  COUNTRY_TO_SYMBOLS,
  mapForexFactoryImpactToNotion,
  inferSymbolsFromCountry,
  toCalendarSignal,
  generateCalendarSignals,
};
