const { canonicalizeSymbols, normalizeSymbol } = require('./symbolFamilies');
const { getSymbolWinRate } = require('./lossMemory');

const SYMBOL_BOOST_RULES = [
  { symbol: 'USOIL', regex: /\b(oil|wti|brent|opec|crude|hormuz|refiner|shipment|shipping disruption|supply disruption|output cut)\b/i, score: 2.4 },
  { symbol: 'XAUUSD', regex: /\b(gold|bullion|safe haven|haven demand|precious metals?|real yields|geopolitical outflows)\b/i, score: 2.4 },
  { symbol: 'XAGUSD', regex: /\b(silver)\b/i, score: 2.2 },
  { symbol: 'US500', regex: /\b(nasdaq|dow jones|s&p 500|wall street|equity|equities|stocks|stock market|risk sentiment|earnings|guidance|nvidia|ai stocks|chip stocks)\b/i, score: 2.1 },
  { symbol: 'EURUSD', regex: /\b(fed|federal reserve|us cpi|nfp|nonfarm payroll|payroll|jobless claims|unemployment|pce|gdp|ecb|eurozone|lagarde|germany|euro area)\b/i, score: 2.0 },
  { symbol: 'GBPUSD', regex: /\b(boe|bank of england|uk cpi|uk gdp|britain|british|sterling)\b/i, score: 2.0 },
  { symbol: 'USDJPY', regex: /\b(boj|bank of japan|japan|japanese|yen)\b/i, score: 2.0 },
  { symbol: 'AUDUSD', regex: /\b(rba|australia|australian|aussie)\b/i, score: 2.0 },
  { symbol: 'NZDUSD', regex: /\b(rbnz|new zealand|kiwi)\b/i, score: 2.0 },
  { symbol: 'USDCAD', regex: /\b(boc|bank of canada|canada|canadian)\b/i, score: 2.0 },
  { symbol: 'USDCHF', regex: /\b(snb|switzerland|swiss|franc)\b/i, score: 2.0 },
  { symbol: 'DE40', regex: /\b(dax|german equities|germany stocks|eurozone stocks)\b/i, score: 1.8 },
  { symbol: 'UK100', regex: /\b(ftse|uk equities|london stocks)\b/i, score: 1.8 },
  { symbol: 'JP225', regex: /\b(nikkei|japan equities|tokyo stocks)\b/i, score: 1.8 },
  { symbol: 'BTCUSD', regex: /\b(bitcoin|btc|crypto etf|stablecoin|digital asset|treasury buying)\b/i, score: 2.2 },
  { symbol: 'ETHUSD', regex: /\b(ethereum|eth)\b/i, score: 2.2 },
  { symbol: 'NGAS', regex: /\b(natural gas|lng|gas supply)\b/i, score: 2.0 },
];

function scoreCandidate(symbol, text = '', matchedRules = []) {
  const normalized = normalizeSymbol(symbol);
  let score = 0;

  for (const rule of matchedRules || []) {
    const matchesSymbol = (rule.symbols || []).some(candidate => normalizeSymbol(candidate) === normalized);
    if (matchesSymbol) {
      score += Number(rule.weight) || 1;
    }
  }

  for (const boost of SYMBOL_BOOST_RULES) {
    if (boost.symbol === normalized && boost.regex.test(text)) {
      score += boost.score;
    }
  }

  score += getSymbolWinRate(normalized) / 100;

  return Number(score.toFixed(3));
}

function selectPrimarySymbol(symbols = [], text = '', options = {}) {
  const candidates = canonicalizeSymbols(symbols, { maxSymbols: symbols.length || 10 });
  if (candidates.length === 0) {
    return null;
  }

  const matchedRules = Array.isArray(options.matchedRules) ? options.matchedRules : [];
  const ranked = candidates
    .map(symbol => ({
      symbol,
      score: scoreCandidate(symbol, text, matchedRules),
      winRate: getSymbolWinRate(symbol),
      originalIndex: candidates.indexOf(symbol),
    }))
    .sort((a, b) => b.score - a.score || b.winRate - a.winRate || a.originalIndex - b.originalIndex);

  return ranked[0]?.symbol || candidates[0];
}

function normalizeSignalSymbols(symbols = [], context = {}) {
  const primary = selectPrimarySymbol(symbols, context.text || '', { matchedRules: context.matchedRules || [] });
  return primary ? [primary] : [];
}

module.exports = {
  SYMBOL_BOOST_RULES,
  scoreCandidate,
  selectPrimarySymbol,
  normalizeSignalSymbols,
};
