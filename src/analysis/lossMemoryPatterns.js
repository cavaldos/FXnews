const LOSS_MEMORY_PATTERNS = [
  {
    id: 'generic-equity-earnings-bullish',
    title: 'Avoid generic earnings recap longs on US500',
    symbol: 'US500',
    bias: '🟢 Bullish',
    keywordsAny: ['earnings', 'nvidia', 'guidance', 'daily open', 'live updates', 'ai developments', 'earnings beat'],
    action: 'avoid',
    penalty: 2.4,
    minSamples: 3,
    minLossRate: 65,
    note: 'Generic earnings or recap headlines often fail to create bullish US500 follow-through.',
  },
  {
    id: 'commentary-oil-bullish',
    title: 'Avoid commentary-only oil bullish calls',
    symbol: 'USOIL',
    bias: '🟢 Bullish',
    keywordsAny: ['analysts', 'commentary', 'reacting', 'impact', 'daily chart', 'set to', 'outlook', 'would', 'could'],
    action: 'avoid',
    penalty: 2.0,
    minSamples: 3,
    minLossRate: 60,
    note: 'Commentary-heavy oil headlines often arrive after the move or fade quickly.',
  },
  {
    id: 'geopolitical-oil-bullish-followthrough',
    title: 'Penalize late geopolitical oil spikes',
    symbol: 'USOIL',
    bias: '🟢 Bullish',
    keywordsAny: ['iran', 'hormuz', 'shipment', 'shipments', 'strikes', 'attack', 'war', 'conflict', 'supply disruption'],
    action: 'penalize',
    penalty: 1.2,
    minSamples: 5,
    minLossRate: 55,
    note: 'Geopolitical oil-spike headlines are often late and vulnerable to reversal after the reopen.',
  },
  {
    id: 'low-conviction-equity-bearish',
    title: 'Penalize low-conviction bearish US500 headlines',
    symbol: 'US500',
    bias: '🔴 Bearish',
    impacts: ['💧 Low', '⚡ Medium'],
    keywordsAny: ['downgrades', 'worried', 'afraid', 'weekly gain', 'dismisses', 'employment', 'loan'],
    action: 'penalize',
    penalty: 1.0,
    minSamples: 3,
    minLossRate: 55,
    note: 'Soft bearish narrative headlines often fail to push US500 lower on follow-through.',
  },
];

function buildLossMemorySearchText(context = {}) {
  return [context.event, context.details, context.text]
    .filter(Boolean)
    .join(' ')
    .toLowerCase();
}

function patternMatchesContext(pattern, context = {}) {
  const searchText = buildLossMemorySearchText(context);
  const primarySymbol = String(context.primarySymbol || '').trim().toUpperCase();
  const bias = String(context.bias || '').trim();
  const impact = String(context.impact || '').trim();

  if (pattern.symbol && primarySymbol !== pattern.symbol) {
    return false;
  }

  if (pattern.bias && bias !== pattern.bias) {
    return false;
  }

  if (Array.isArray(pattern.impacts) && pattern.impacts.length > 0 && !pattern.impacts.includes(impact)) {
    return false;
  }

  if (Array.isArray(pattern.keywordsAny) && pattern.keywordsAny.length > 0) {
    const hasKeyword = pattern.keywordsAny.some(keyword => searchText.includes(String(keyword).toLowerCase()));
    if (!hasKeyword) {
      return false;
    }
  }

  return true;
}

module.exports = {
  LOSS_MEMORY_PATTERNS,
  buildLossMemorySearchText,
  patternMatchesContext,
};
