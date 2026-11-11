const { formatInTimeZone } = require('date-fns-tz');
const { deriveRecommendedAction } = require('./recommendedAction');
const { normalizeSignalSymbols } = require('./symbolSelector');
const { getLossMemoryAdjustment } = require('./lossMemory');

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

const MT5_SYMBOL_RULES = [
  { regex: /\b(oil|wti|brent|opec|crude|hormuz|refiner|shipping disruption|supply disruption)\b/i, symbols: ['USOIL', 'UKOIL'], weight: 3.6, theme: 'oil' },
  { regex: /\b(gold|bullion|safe haven|precious metals?)\b/i, symbols: ['XAUUSD'], weight: 3.2, theme: 'gold' },
  { regex: /\b(silver)\b/i, symbols: ['XAGUSD'], weight: 3.0, theme: 'silver' },
  { regex: /\b(bitcoin|btc|crypto etf|crypto regulation|stablecoin|digital asset)\b/i, symbols: ['BTCUSD'], weight: 3.0, theme: 'btc' },
  { regex: /\b(ethereum|eth)\b/i, symbols: ['ETHUSD'], weight: 3.0, theme: 'eth' },
  { regex: /\b(nasdaq|nvidia|semiconductor|chip|ai|ai stocks|tech stocks|software stocks|earnings|guidance)\b/i, symbols: ['NAS100', 'US500'], weight: 2.9, theme: 'tech' },
  { regex: /\b(s&p 500|dow jones|wall street|stocks|equity funds|risk sentiment|markets)\b/i, symbols: ['US500', 'US30'], weight: 2.6, theme: 'equity' },
  { regex: /\b(fed|federal reserve|us cpi|nfp|nonfarm payroll|jobs report|payroll|us gdp|pce|ism|job openings|jobless claims|initial claims|continuing claims|unemployment rate)\b/i, symbols: ['EURUSD', 'XAUUSD'], weight: 2.8, theme: 'usd-macro' },
  { regex: /\b(ecb|euro area|eu inflation|germany|lagarde|eurozone)\b/i, symbols: ['EURUSD'], weight: 2.7, theme: 'eur' },
  { regex: /\b(boe|uk cpi|uk gdp|britain|british|bank of england)\b/i, symbols: ['GBPUSD'], weight: 2.7, theme: 'gbp' },
  { regex: /\b(boj|japan|japanese|yen|bank of japan)\b/i, symbols: ['USDJPY'], weight: 2.7, theme: 'jpy' },
  { regex: /\b(australia|australian|rba)\b/i, symbols: ['AUDUSD'], weight: 2.5, theme: 'aud' },
  { regex: /\b(new zealand|new zealander|rbnz)\b/i, symbols: ['NZDUSD'], weight: 2.5, theme: 'nzd' },
  { regex: /\b(canada|canadian|boc|bank of canada)\b/i, symbols: ['USDCAD'], weight: 2.5, theme: 'cad' },
  { regex: /\b(switzerland|swiss|snb)\b/i, symbols: ['USDCHF'], weight: 2.5, theme: 'chf' },
];

const BULLISH_KEYWORDS = [
  'beats',
  'beat',
  'stronger',
  'cooling inflation',
  'rate cuts',
  'dovish',
  'deal reached',
  'ceasefire',
  'surge in demand',
  'upgrades outlook',
  'stimulus',
  'soft landing',
  'spike',
  'surge',
  'jump',
  'rally',
  'rebound',
  'higher',
  'upside',
  'tight supply',
  'supply disruption',
  'shipment halt',
];

const BEARISH_KEYWORDS = [
  'misses',
  'miss',
  'hotter inflation',
  'sticky inflation',
  'rate hike',
  'hawkish',
  'higher for longer',
  'better jobs data',
  'strong jobs',
  'sanctions',
  'escalation',
  'war risk',
  'downgrade',
  'recession risk',
  'capital outflow',
  'default risk',
  'plunge',
  'slump',
  'selloff',
  'drop',
  'falls',
  'lower',
  'oversupply',
  'output hike',
  'production increase',
  'inventory build',
];

const BULLISH_PATTERNS = [
  { regex: /\b(spike|surge|jump|rally|rebound|soar|record high|upside)\b/i, weight: 1.2 },
  { regex: /\b(ceasefire|truce|de-escalation|rate cuts|dovish pivot|stimulus)\b/i, weight: 1.3 },
  { regex: /\b(output cut|supply disruption|shipment halt|tight supply)\b/i, weight: 1.4 },
];

const BEARISH_PATTERNS = [
  { regex: /\b(plunge|slump|selloff|drop|falls|lower|slides|sinks)\b/i, weight: 1.2 },
  { regex: /\b(hawkish|hotter inflation|rate hike|recession|risk-off)\b/i, weight: 1.3 },
  { regex: /\b(oversupply|output hike|production increase|inventory build)\b/i, weight: 1.4 },
];

const DIRECTIONAL_OVERRIDE_RULES = [
  {
    symbols: ['USOIL', 'UKOIL'],
    bullish: /\b(spike|surge|jump|higher|tight supply|supply disruption|shipping disruption|output cut|sanctions?|strikes?|attack|war|conflict|hormuz)\b/i,
    bearish: /\b(slump|drop|lower|oversupply|output hike|production increase|inventory build|demand destruction)\b/i,
  },
  {
    symbols: ['XAUUSD', 'XAGUSD'],
    bullish: /\b(safe haven|conflict|war|geopolitical|dovish|rate cuts|cooling inflation|weak jobs|recession risk)\b/i,
    bearish: /\b(hawkish|hotter inflation|higher yields|rate hike|strong dollar)\b/i,
  },
  {
    symbols: ['NAS100', 'US500', 'US30'],
    bullish: /\b(rally|rebound|beat|beats|stimulus|rate cuts|soft landing|risk-on)\b/i,
    bearish: /\b(sink|sinks|selloff|slump|plunge|downgrade|outflows|shockwaves|tariffs?|risk-off|geopolitical worries)\b/i,
  },
  {
    symbols: ['BTCUSD', 'ETHUSD'],
    bullish: /\b(approval|adoption|reserve|inflows|surge|rally|institutional demand)\b/i,
    bearish: /\b(crackdown|ban|hack|liquidation|outflows|lawsuit)\b/i,
  },
  {
    symbols: ['EURUSD', 'GBPUSD', 'AUDUSD', 'NZDUSD'],
    bullish: /\b(weak jobs|unemployment rose|missed payroll|dovish fed|rate cuts|cooling inflation|soft data)\b/i,
    bearish: /\b(strong jobs|better jobs data|beat payroll|sticky inflation|hotter inflation|hawkish fed|higher for longer|yields rise)\b/i,
  },
  {
    symbols: ['USDJPY', 'USDCAD', 'USDCHF'],
    bullish: /\b(strong jobs|better jobs data|sticky inflation|hotter inflation|hawkish fed|higher for longer|yields rise)\b/i,
    bearish: /\b(weak jobs|unemployment rose|missed payroll|dovish fed|rate cuts|cooling inflation)\b/i,
  },
];

const HIGH_IMPACT_KEYWORDS = [
  'federal reserve',
  'fed',
  'ecb',
  'boe',
  'boj',
  'cpi',
  'nfp',
  'nonfarm payroll',
  'gdp',
  'pce',
  'interest rate',
  'opec',
  'war',
  'sanction',
];

const MEDIUM_IMPACT_KEYWORDS = [
  'earnings',
  'guidance',
  'manufacturing pmi',
  'services pmi',
  'retail sales',
  'bond yields',
  'treasury auction',
  'etf flows',
];

function toEtDateTime(dateInput) {
  const parsed = dateInput ? new Date(dateInput) : new Date();
  const validDate = Number.isNaN(parsed.getTime()) ? new Date() : parsed;
  return formatInTimeZone(validDate, 'America/New_York', "yyyy-MM-dd'T'HH:mm:ss");
}

function sanitizeDescription(description = '') {
  let text = String(description || '').replace(/\s+/g, ' ').trim();
  if (!text) return '';

  const stopMarkers = [
    'Read Next',
    'What to Read Next',
    'Continue To Article',
    'Advertisement',
    'Skip Navigation',
    'Editor\'s Picks',
    'Latest Video',
    'watch now',
  ];

  for (const marker of stopMarkers) {
    const index = text.indexOf(marker);
    if (index > 120) {
      text = text.slice(0, index).trim();
    }
  }

  text = text.replace(/Image\s+\d+:/gi, '');
  return text.slice(0, 700);
}

function uniqueList(values = []) {
  return [...new Set((values || []).map(value => String(value || '').trim()).filter(Boolean))];
}

function getSourceNames(newsItem) {
  return uniqueList([newsItem.source, ...(newsItem.sources || [])]);
}

function getSourceUrls(newsItem) {
  return uniqueList([newsItem.sourceUrl, ...(newsItem.sourceUrls || [])]);
}

function getTitleVariants(newsItem) {
  return uniqueList([newsItem.title, ...(newsItem.titleVariants || [])]);
}

function getDescriptions(newsItem) {
  return uniqueList([newsItem.description, ...(newsItem.descriptions || [])]
    .map(description => sanitizeDescription(description))
    .filter(Boolean));
}

function buildAnalysisText(newsItem) {
  const titleParts = getTitleVariants(newsItem).slice(0, 3);
  const descriptionParts = getDescriptions(newsItem).slice(0, 3);

  return [...titleParts, ...descriptionParts].join('. ').trim().toLowerCase();
}

function inferSymbols(text) {
  const matches = MT5_SYMBOL_RULES
    .filter(rule => rule.regex.test(text))
    .sort((a, b) => (b.weight || 0) - (a.weight || 0));

  if (matches.length === 0) {
    return ['XAUUSD'];
  }

  const candidates = matches.flatMap(rule => rule.symbols || []);
  return normalizeSignalSymbols(candidates, { text, matchedRules: matches });
}

function hasAnySymbol(symbols, targets) {
  const set = new Set(symbols || []);
  return targets.some(sym => set.has(sym));
}

function computeDirectionalScores(text, symbols = []) {
  let bullishScore = 0;
  let bearishScore = 0;

  for (const kw of BULLISH_KEYWORDS) {
    if (text.includes(kw)) bullishScore += 1;
  }

  for (const kw of BEARISH_KEYWORDS) {
    if (text.includes(kw)) bearishScore += 1;
  }

  for (const rule of BULLISH_PATTERNS) {
    if (rule.regex.test(text)) bullishScore += rule.weight;
  }

  for (const rule of BEARISH_PATTERNS) {
    if (rule.regex.test(text)) bearishScore += rule.weight;
  }

  const hasOil = hasAnySymbol(symbols, ['USOIL', 'UKOIL']);
  const hasGold = hasAnySymbol(symbols, ['XAUUSD', 'XAGUSD']);
  const hasEquity = hasAnySymbol(symbols, ['US500', 'US30', 'NAS100', 'DE40', 'UK100', 'JP225', 'FR40']);

  if (hasOil && /\b(iran|hormuz|sanction|attack|strikes?|geopolitical|supply disruption)\b/i.test(text)) {
    bullishScore += 1.5;
  }

  if (hasOil && /\b(oversupply|output hike|production increase|inventory build)\b/i.test(text)) {
    bearishScore += 1.5;
  }

  if (hasGold && /\b(conflict|war|safe haven|geopolitical)\b/i.test(text)) {
    bullishScore += 1.1;
  }

  if (hasEquity && /\b(plunge|selloff|war|sanction|risk-off|hotter inflation|hawkish)\b/i.test(text)) {
    bearishScore += 1.1;
  }

  if (hasEquity && /\b(rally|rebound|record high|risk-on|soft landing|rate cuts|stimulus)\b/i.test(text)) {
    bullishScore += 1.1;
  }

  if (hasAnySymbol(symbols, ['BTCUSD', 'ETHUSD']) && /\b(approval|adoption|reserve|inflows|institutional demand|treasury buying)\b/i.test(text)) {
    bullishScore += 1.4;
  }

  if (hasAnySymbol(symbols, ['BTCUSD', 'ETHUSD']) && /\b(crackdown|ban|hack|liquidation|lawsuit|outflows)\b/i.test(text)) {
    bearishScore += 1.4;
  }

  if (hasAnySymbol(symbols, ['EURUSD', 'GBPUSD', 'AUDUSD', 'NZDUSD']) && /\b(weak jobs|unemployment rate also rose|missed expectations|missed payroll|dovish fed|rate cuts|cooling inflation)\b/i.test(text)) {
    bullishScore += 1.4;
  }

  if (hasAnySymbol(symbols, ['EURUSD', 'GBPUSD', 'AUDUSD', 'NZDUSD']) && /\b(strong jobs|better jobs data|sticky inflation|hotter inflation|hawkish fed|higher for longer|yields rise)\b/i.test(text)) {
    bearishScore += 1.4;
  }

  if (hasAnySymbol(symbols, ['USDJPY', 'USDCAD', 'USDCHF']) && /\b(weak jobs|unemployment rate also rose|missed expectations|missed payroll|dovish fed|rate cuts|cooling inflation)\b/i.test(text)) {
    bearishScore += 1.4;
  }

  if (hasAnySymbol(symbols, ['USDJPY', 'USDCAD', 'USDCHF']) && /\b(strong jobs|better jobs data|sticky inflation|hotter inflation|hawkish fed|higher for longer|yields rise)\b/i.test(text)) {
    bullishScore += 1.4;
  }

  return { bullishScore, bearishScore };
}

function hasStrongDirectionalLanguage(text) {
  return /\b(spike|surge|jump|rally|rebound|soar|plunge|slump|selloff|drop|hawkish|dovish|rate cuts|rate hike|output cut|output hike|approval|ban|ceasefire|sanctions?|supply disruption|inventory build)\b/i.test(text);
}

function hasClearDirectionalBias(text, scores) {
  const bullishScore = Number(scores?.bullishScore) || 0;
  const bearishScore = Number(scores?.bearishScore) || 0;
  const totalScore = bullishScore + bearishScore;
  const gap = Math.abs(bullishScore - bearishScore);
  const dominantScore = Math.max(bullishScore, bearishScore);
  const directionalShare = totalScore > 0 ? dominantScore / totalScore : 0;
  const dominanceThreshold = hasStrongDirectionalLanguage(text) ? 1.1 : 1.4;

  if (dominantScore < dominanceThreshold) return false;
  if (gap < 1.0) return false;
  if (totalScore > 0 && directionalShare < 0.65) return false;

  return true;
}

function resolveNeutralBias(text, symbols = []) {
  for (const rule of DIRECTIONAL_OVERRIDE_RULES) {
    if (!hasAnySymbol(symbols, rule.symbols)) continue;
    const bullish = rule.bullish.test(text);
    const bearish = rule.bearish.test(text);
    if (bullish && !bearish) return BIAS.bullish;
    if (bearish && !bullish) return BIAS.bearish;
  }

  return BIAS.neutral;
}

function inferBias(text, symbols = []) {
  const scores = computeDirectionalScores(text, symbols);
  const { bullishScore, bearishScore } = scores;

  if (!hasClearDirectionalBias(text, scores)) {
    return resolveNeutralBias(text, symbols);
  }

  if (bullishScore > bearishScore) return BIAS.bullish;
  if (bearishScore > bullishScore) return BIAS.bearish;
  return resolveNeutralBias(text, symbols);
}

function inferImpact(text) {
  for (const kw of HIGH_IMPACT_KEYWORDS) {
    if (text.includes(kw)) return IMPACT.high;
  }

  for (const kw of MEDIUM_IMPACT_KEYWORDS) {
    if (text.includes(kw)) return IMPACT.medium;
  }

  return IMPACT.low;
}

function getConfidenceLabel(bias, impact) {
  if (impact === IMPACT.high && bias !== BIAS.neutral) return 'High';
  if (impact === IMPACT.medium) return 'Medium';
  return 'Low';
}

function getTradeValueScore(bias, impact, symbols, text, sourceCount = 1, lossPenalty = 0) {
  const impactScore = impact === IMPACT.high ? 3 : impact === IMPACT.medium ? 2 : 1;
  const biasScore = bias === BIAS.neutral ? 0 : 2;
  const themeBonus = hasAnySymbol(symbols, ['USOIL', 'UKOIL', 'XAUUSD', 'NAS100', 'US500', 'BTCUSD', 'ETHUSD']) ? 1 : 0;
  const verbBonus = /\b(spike|surge|jump|rally|plunge|slump|selloff|sinks|downgrade|outflows|approval|crackdown|rate cuts|hawkish|dovish)\b/i.test(text) ? 1 : 0;
  const corroborationBonus = sourceCount > 1 ? Math.min(1.2, (sourceCount - 1) * 0.4) : 0;
  return impactScore + biasScore + themeBonus + verbBonus + corroborationBonus - (lossPenalty || 0);
}

function buildSourceDetails(newsItem) {
  const sources = getSourceNames(newsItem);
  const sourceUrls = getSourceUrls(newsItem);
  const extraSources = sources.slice(1);
  const extraUrls = sourceUrls.filter(url => url && url !== newsItem.sourceUrl).slice(0, 2);
  const parts = [];

  if (sources[0]) {
    parts.push(`Primary source: ${sources[0]}.`);
  }

  if (extraSources.length > 0) {
    parts.push(`Cross-check: ${extraSources.join(', ')}.`);
  }

  if (extraUrls.length > 0) {
    parts.push(`Other coverage: ${extraUrls.join(' | ')}.`);
  }

  return parts.join(' ');
}

function buildDetails(newsItem, signalParts) {
  const cleanDescription = getDescriptions(newsItem).slice(0, 2).join(' ');
  const sourceDetails = buildSourceDetails(newsItem);

  return [
    `Prediction: ${signalParts.bias} with ${signalParts.impact} impact (${signalParts.confidence} confidence).`,
    `Recommended action: ${signalParts.recommendedAction}.`,
    `Why: Signal inferred from macro keywords and source context in the headline/content.`,
    `Focus symbols: ${signalParts.symbols.join(', ')}.`,
    signalParts.lossMemoryNotes?.length ? `Historical caution: ${signalParts.lossMemoryNotes.join(' ')}` : null,
    sourceDetails || null,
    cleanDescription ? `Context: ${cleanDescription}` : null,
    'Risk note: Reversal risk if follow-up data or official statements contradict initial headlines.',
  ].filter(Boolean).join(' ');
}

function toSignal(newsItem) {
  const text = buildAnalysisText(newsItem);
  const symbols = inferSymbols(text);
  const bias = inferBias(text, symbols);
  const impact = inferImpact(text);
  const confidence = getConfidenceLabel(bias, impact);
  const sources = getSourceNames(newsItem);
  const sourceUrls = getSourceUrls(newsItem);
  const corroborationCount = sources.length || sourceUrls.length || 1;
  const recommendedAction = deriveRecommendedAction({ bias, impact, corroborationCount });
  const lossMemory = getLossMemoryAdjustment({
    text,
    event: newsItem.title,
    details: getDescriptions(newsItem).join(' '),
    primarySymbol: symbols[0],
    bias,
    impact,
  });
  const tradeValueScore = getTradeValueScore(bias, impact, symbols, text, corroborationCount, lossMemory.penalty);

  const signal = {
    event: (newsItem.title || 'Untitled market event').slice(0, 120),
    symbols,
    time: toEtDateTime(newsItem.publishedAt),
    bias,
    impact,
    recommendedAction,
    url: newsItem.sourceUrl || sourceUrls[0] || '',
    sources,
    sourceUrls,
    status: '📋 New',
    tradeValueScore,
    lossMemoryNotes: lossMemory.notes,
    skipByLossMemory: lossMemory.shouldAvoid,
  };

  signal.details = buildDetails(newsItem, {
    symbols,
    bias,
    impact,
    confidence,
    recommendedAction,
    lossMemoryNotes: lossMemory.notes,
  });

  return signal;
}

function generateSignals(newsItems, options = {}) {
  const minImpact = options.minImpact || IMPACT.high;
  const maxSignals = options.maxSignals || 20;
  const impactOrder = { [IMPACT.high]: 3, [IMPACT.medium]: 2, [IMPACT.low]: 1 };
  const threshold = impactOrder[minImpact] || 2;

  return (newsItems || [])
    .map(item => toSignal(item))
    .filter(signal => impactOrder[signal.impact] >= threshold)
    .filter(signal => signal.bias !== BIAS.neutral)
    .filter(signal => !signal.skipByLossMemory)
    .sort((a, b) => (b.tradeValueScore || 0) - (a.tradeValueScore || 0))
    .map(({ tradeValueScore, skipByLossMemory, lossMemoryNotes, ...signal }) => signal)
    .slice(0, maxSignals);
}

module.exports = {
  BIAS,
  IMPACT,
  MT5_SYMBOL_RULES,
  inferSymbols,
  toSignal,
  generateSignals,
};
