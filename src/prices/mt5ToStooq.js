const MT5_TO_STOOQ = {
  // Forex
  EURUSD: 'eurusd',
  GBPUSD: 'gbpusd',
  USDJPY: 'usdjpy',
  EURGBP: 'eurgbp',
  EURJPY: 'eurjpy',
  GBPJPY: 'gbpjpy',
  AUDUSD: 'audusd',
  NZDUSD: 'nzdusd',
  USDCAD: 'usdcad',
  USDCHF: 'usdchf',
  AUDJPY: 'audjpy',
  EURAUD: 'euraud',

  // Metals
  XAUUSD: 'xauusd',
  XAGUSD: 'xagusd',

  // Energy
  USOIL: 'cl.f',
  NGAS: 'ng.f',

  // Indices (via futures)
  US500: 'es.f',
  US30: 'ym.f',
  NAS100: 'nq.f',
  JP225: 'nk.f',

  // Crypto
  BTCUSD: 'btcusd',
};

function normalizeMt5Symbol(symbol) {
  return String(symbol || '').trim().toUpperCase();
}

function toStooqSymbol(mt5Symbol) {
  const key = normalizeMt5Symbol(mt5Symbol);
  return MT5_TO_STOOQ[key] || null;
}

function getSupportedMt5Symbols() {
  return Object.keys(MT5_TO_STOOQ).sort();
}

module.exports = {
  MT5_TO_STOOQ,
  normalizeMt5Symbol,
  toStooqSymbol,
  getSupportedMt5Symbols,
};
