const SYMBOL_FAMILY_RANKINGS = [
  {
    family: 'oil-benchmarks',
    rankedSymbols: ['USOIL', 'UKOIL'],
    winRates: {
      USOIL: 40.0,
      UKOIL: 33.3,
    },
  },
  {
    family: 'us-equity-indices',
    rankedSymbols: ['US500', 'NAS100', 'US30'],
    winRates: {
      US500: 43.5,
      NAS100: 0.0,
      US30: 0.0,
    },
  },
];

const SYMBOL_TO_FAMILY = new Map();
for (const family of SYMBOL_FAMILY_RANKINGS) {
  for (const symbol of family.rankedSymbols) {
    SYMBOL_TO_FAMILY.set(symbol, family);
  }
}

function normalizeSymbol(symbol = '') {
  return String(symbol || '').trim().toUpperCase();
}

function getFamily(symbol) {
  return SYMBOL_TO_FAMILY.get(normalizeSymbol(symbol)) || null;
}

function getPreferredSymbol(symbol) {
  const normalized = normalizeSymbol(symbol);
  const family = getFamily(normalized);
  return family ? family.rankedSymbols[0] : normalized;
}

function canonicalizeSymbols(symbols = [], options = {}) {
  const maxSymbols = options.maxSymbols || 3;
  const seen = new Set();
  const output = [];

  for (const rawSymbol of symbols || []) {
    const preferred = getPreferredSymbol(rawSymbol);
    if (!preferred || seen.has(preferred)) {
      continue;
    }

    seen.add(preferred);
    output.push(preferred);

    if (output.length >= maxSymbols) {
      break;
    }
  }

  return output;
}

function getDeprecatedSymbols() {
  return SYMBOL_FAMILY_RANKINGS.flatMap(family => family.rankedSymbols.slice(1));
}

module.exports = {
  SYMBOL_FAMILY_RANKINGS,
  canonicalizeSymbols,
  getDeprecatedSymbols,
  getFamily,
  getPreferredSymbol,
  normalizeSymbol,
};
