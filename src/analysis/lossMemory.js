const fs = require('fs');
const path = require('path');

const { LOSS_MEMORY_PATTERNS, patternMatchesContext } = require('./lossMemoryPatterns');

const LOSS_MEMORY_PATH = path.join(process.cwd(), 'data', 'validation', 'loss-memory.json');

const FALLBACK_SYMBOL_PERFORMANCE = {
  EURUSD: { winRate: 53.8, decisive: 13, win: 7, loss: 6, pass: 1 },
  XAUUSD: { winRate: 46.9, decisive: 32, win: 15, loss: 17, pass: 1 },
  US500: { winRate: 43.5, decisive: 23, win: 10, loss: 13, pass: 0 },
  USOIL: { winRate: 40.0, decisive: 30, win: 12, loss: 18, pass: 1 },
  DE40: { winRate: 40.0, decisive: 5, win: 2, loss: 3, pass: 0 },
  USDJPY: { winRate: 37.5, decisive: 8, win: 3, loss: 5, pass: 0 },
  JP225: { winRate: 25.0, decisive: 4, win: 1, loss: 3, pass: 0 },
  GBPUSD: { winRate: 0.0, decisive: 2, win: 0, loss: 2, pass: 0 },
  EURGBP: { winRate: 0.0, decisive: 1, win: 0, loss: 1, pass: 0 },
  BTCUSD: { winRate: 100.0, decisive: 1, win: 1, loss: 0, pass: 0 },
  ETHUSD: { winRate: 100.0, decisive: 1, win: 1, loss: 0, pass: 0 },
  NGAS: { winRate: 100.0, decisive: 1, win: 1, loss: 0, pass: 0 },
  UK100: { winRate: 100.0, decisive: 2, win: 2, loss: 0, pass: 0 },
};

let cachedLossMemory = null;

function readLossMemoryFile() {
  try {
    if (!fs.existsSync(LOSS_MEMORY_PATH)) {
      return null;
    }

    return JSON.parse(fs.readFileSync(LOSS_MEMORY_PATH, 'utf8'));
  } catch (_error) {
    return null;
  }
}

function getFallbackMemory() {
  return {
    generatedAt: null,
    symbolPerformance: FALLBACK_SYMBOL_PERFORMANCE,
    lessons: [],
    patterns: [],
  };
}

function loadLossMemory() {
  if (cachedLossMemory) {
    return cachedLossMemory;
  }

  const fromDisk = readLossMemoryFile();
  cachedLossMemory = {
    ...getFallbackMemory(),
    ...(fromDisk || {}),
    symbolPerformance: {
      ...FALLBACK_SYMBOL_PERFORMANCE,
      ...((fromDisk && fromDisk.symbolPerformance) || {}),
    },
    lessons: Array.isArray(fromDisk?.lessons) ? fromDisk.lessons : [],
    patterns: Array.isArray(fromDisk?.patterns) ? fromDisk.patterns : [],
  };

  return cachedLossMemory;
}

function resetLossMemoryCache() {
  cachedLossMemory = null;
}

function normalizeSymbol(symbol = '') {
  return String(symbol || '').trim().toUpperCase();
}

function getSymbolPerformance(symbol = '') {
  const memory = loadLossMemory();
  return memory.symbolPerformance[normalizeSymbol(symbol)] || null;
}

function getSymbolWinRate(symbol = '') {
  return Number(getSymbolPerformance(symbol)?.winRate) || 0;
}

function getMatchingLessons(context = {}) {
  const memory = loadLossMemory();

  return (memory.lessons || []).filter(lesson => {
    const basePattern = LOSS_MEMORY_PATTERNS.find(pattern => pattern.id === lesson.id) || lesson;
    return patternMatchesContext(basePattern, context);
  });
}

function getLossMemoryAdjustment(context = {}) {
  const lessons = getMatchingLessons(context);
  const totalPenalty = lessons.reduce((sum, lesson) => sum + (Number(lesson.penalty) || 0), 0);
  const shouldAvoid = lessons.some(lesson => lesson.action === 'avoid');

  return {
    lessons,
    penalty: Number(totalPenalty.toFixed(2)),
    shouldAvoid,
    notes: lessons.map(lesson => lesson.note || lesson.title).filter(Boolean),
  };
}

module.exports = {
  LOSS_MEMORY_PATH,
  FALLBACK_SYMBOL_PERFORMANCE,
  loadLossMemory,
  resetLossMemoryCache,
  getSymbolPerformance,
  getSymbolWinRate,
  getMatchingLessons,
  getLossMemoryAdjustment,
};
