const { toEtDateTime } = require('../news/forexfactoryCalendar');
const { BIAS, mapForexFactoryImpactToNotion } = require('./calendarSignalGenerator');
const { deriveRecommendedAction } = require('./recommendedAction');

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

function parseEconomicValue(raw) {
  const input = String(raw || '').trim();
  if (!input || input === '-') return null;

  const cleaned = input
    .replace(/,/g, '')
    .replace(/\s+/g, '')
    .replace(/^[<>~=]+/, '')
    .replace(/\(.*?\)/g, '');

  const match = cleaned.match(/^(-?\d+(?:\.\d+)?)([KMBT%]?)$/i);
  if (!match) return null;

  const value = Number(match[1]);
  if (!Number.isFinite(value)) return null;

  const suffix = String(match[2] || '').toUpperCase();
  if (suffix === 'K') return value * 1e3;
  if (suffix === 'M') return value * 1e6;
  if (suffix === 'B') return value * 1e9;
  if (suffix === 'T') return value * 1e12;
  return value;
}

function inferDirectionalMove(title, forecastRaw, previousRaw) {
  const titleText = String(title || '');
  const forecast = parseEconomicValue(forecastRaw);
  const previous = parseEconomicValue(previousRaw);

  if (forecast == null || previous == null || forecast === previous) {
    return null;
  }

  const higherIsBetter = HIGHER_IS_BETTER_PATTERNS.some(pattern => pattern.test(titleText));
  const lowerIsBetter = LOWER_IS_BETTER_PATTERNS.some(pattern => pattern.test(titleText));

  if (!higherIsBetter && !lowerIsBetter) {
    return null;
  }

  if (higherIsBetter) {
    return forecast > previous ? 'stronger_local_currency' : 'weaker_local_currency';
  }

  return forecast < previous ? 'stronger_local_currency' : 'weaker_local_currency';
}

function biasFromCurrencySymbol(move, currency, symbol) {
  if (!move) return BIAS.neutral;

  const ccy = String(currency || '').trim().toUpperCase();
  const pair = String(symbol || '').trim().toUpperCase();

  if (!ccy || pair.length < 6) {
    return BIAS.neutral;
  }

  const base = pair.slice(0, 3);
  const quote = pair.slice(3, 6);
  const localStrong = move === 'stronger_local_currency';

  if (ccy === base) {
    return localStrong ? BIAS.bullish : BIAS.bearish;
  }

  if (ccy === quote) {
    return localStrong ? BIAS.bearish : BIAS.bullish;
  }

  return BIAS.neutral;
}

function buildDetails(order, bias, recommendedAction) {
  const parts = [
    `Pre-event analysis from ${order.source}.`,
    `Currency: ${order.currency}.`,
    `Symbol: ${order.symbol}.`,
    `Impact: ${order.impact}.`,
    order.raw?.forecast ? `Forecast: ${order.raw.forecast}.` : null,
    order.raw?.previous ? `Previous: ${order.raw.previous}.` : null,
    `Bias: ${bias}.`,
    `Recommended action: ${recommendedAction}.`,
    'Rule: compare forecast vs previous before release. Re-check after actual print.',
  ].filter(Boolean);

  return parts.join(' ').slice(0, 2000);
}

function toFutureOrderSignal(order) {
  const move = inferDirectionalMove(order.title, order.raw?.forecast, order.raw?.previous);
  const bias = biasFromCurrencySymbol(move, order.currency, order.symbol);
  const impact = mapForexFactoryImpactToNotion(order.impact);
  const recommendedAction = deriveRecommendedAction({ bias, impact, corroborationCount: 1 });
  const eventTime = order.eventTimeUtc ? toEtDateTime(order.eventTimeUtc) : null;

  return {
    event: `[PRE] ${order.title}`.slice(0, 120),
    symbols: order.symbol ? [order.symbol] : [],
    time: eventTime,
    bias,
    impact,
    recommendedAction,
    url: order.sourceUrl || order.raw?.url || null,
    details: buildDetails(order, bias, recommendedAction),
    status: '👀 Watching',
    sourceUrls: [order.sourceUrl || order.raw?.url].filter(Boolean),
  };
}

function generateFutureOrderSignals(orders = []) {
  return (orders || []).map(order => toFutureOrderSignal(order));
}

module.exports = {
  parseEconomicValue,
  inferDirectionalMove,
  biasFromCurrencySymbol,
  toFutureOrderSignal,
  generateFutureOrderSignals,
};
