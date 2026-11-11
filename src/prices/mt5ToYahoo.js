const MT5_TO_YAHOO = {
  // Forex
  EURUSD: 'EURUSD=X',
  GBPUSD: 'GBPUSD=X',
  USDJPY: 'USDJPY=X',
  AUDUSD: 'AUDUSD=X',
  NZDUSD: 'NZDUSD=X',
  USDCAD: 'USDCAD=X',
  USDCHF: 'USDCHF=X',
  EURGBP: 'EURGBP=X',
  EURJPY: 'EURJPY=X',
  GBPJPY: 'GBPJPY=X',
  AUDJPY: 'AUDJPY=X',
  EURAUD: 'EURAUD=X',

  // Metals
  // Yahoo does not reliably expose XAUUSD=X spot; use futures.
  XAUUSD: 'GC=F',
  XAGUSD: 'SI=F',

  // Energy
  USOIL: 'CL=F',
  UKOIL: 'BZ=F',
  NGAS: 'NG=F',

  // Indices
  US500: '^GSPC',
  US30: '^DJI',
  NAS100: '^NDX',
  DE40: '^GDAXI',
  UK100: '^FTSE',
  JP225: '^N225',
  FR40: '^FCHI',

  // Crypto
  BTCUSD: 'BTC-USD',
  ETHUSD: 'ETH-USD',
};

function normalizeMt5Symbol(symbol) {
  return String(symbol || '').trim().toUpperCase();
}

function toYahooSymbol(mt5Symbol) {
  const key = normalizeMt5Symbol(mt5Symbol);
  return MT5_TO_YAHOO[key] || null;
}

function getSupportedMt5Symbols() {
  return Object.keys(MT5_TO_YAHOO).sort();
}

module.exports = {
  MT5_TO_YAHOO,
  normalizeMt5Symbol,
  toYahooSymbol,
  getSupportedMt5Symbols,
};
