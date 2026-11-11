const RECOMMENDED_ACTION = {
  strongBuy: '\u{1F7E2} Strong Buy',
  buy: '\u{1F535} Buy',
  hold: '\u26AA Hold',
  sell: '\u{1F7E0} Sell',
  strongSell: '\u{1F534} Strong Sell',
};

function normalizeBiasKey(bias = '') {
  const value = String(bias || '').trim().toLowerCase();
  if (value.includes('bullish')) return 'bullish';
  if (value.includes('bearish')) return 'bearish';
  return 'neutral';
}

function normalizeImpactKey(impact = '') {
  const value = String(impact || '').trim().toLowerCase();
  if (value.includes('high')) return 'high';
  if (value.includes('medium')) return 'medium';
  return 'low';
}

function deriveRecommendedAction(input = {}) {
  const biasKey = normalizeBiasKey(input.bias);
  const impactKey = normalizeImpactKey(input.impact);
  const corroborationCount = Math.max(1, Number(input.corroborationCount) || 1);
  const isStrongSetup = impactKey === 'high' && corroborationCount >= 2;

  if (biasKey === 'bullish') {
    return isStrongSetup ? RECOMMENDED_ACTION.strongBuy : RECOMMENDED_ACTION.buy;
  }

  if (biasKey === 'bearish') {
    return isStrongSetup ? RECOMMENDED_ACTION.strongSell : RECOMMENDED_ACTION.sell;
  }

  return RECOMMENDED_ACTION.hold;
}

module.exports = {
  RECOMMENDED_ACTION,
  deriveRecommendedAction,
  normalizeBiasKey,
  normalizeImpactKey,
};
