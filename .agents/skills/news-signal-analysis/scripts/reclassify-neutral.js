#!/usr/bin/env node
require('dotenv').config({ path: require('path').join(__dirname, '..', '..', '..', '..', '.env'), quiet: true });

const { getSignals, updateSignalFields } = require('../../../../src/news-trade-db');
const { BIAS } = require('../../../../src/analysis/newsSignalGenerator');

const BULLISH_KEYWORDS = [
  'beats', 'beat', 'stronger', 'cooling inflation', 'rate cuts', 'dovish', 'deal reached', 'ceasefire', 'surge in demand', 'upgrades outlook', 'stimulus', 'soft landing',
  'spike', 'surge', 'jump', 'rally', 'rebound', 'higher', 'upside', 'tight supply', 'supply disruption', 'shipment halt',
];

const BEARISH_KEYWORDS = [
  'misses', 'miss', 'hotter inflation', 'rate hike', 'hawkish', 'sanctions', 'escalation', 'war risk', 'downgrade', 'recession risk', 'capital outflow', 'default risk',
  'plunge', 'slump', 'selloff', 'drop', 'falls', 'lower', 'oversupply', 'output hike', 'production increase', 'inventory build',
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

function hasFlag(flag) {
  return process.argv.includes(flag);
}

function getArgValue(flag, fallback = null) {
  const idx = process.argv.indexOf(flag);
  if (idx === -1 || idx + 1 >= process.argv.length) return fallback;
  return process.argv[idx + 1];
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function hasAnySymbol(symbols, targets) {
  const set = new Set(symbols || []);
  return targets.some(sym => set.has(sym));
}

function inferBias(text, symbols = []) {
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

  const totalScore = bullishScore + bearishScore;
  const gap = Math.abs(bullishScore - bearishScore);

  if (totalScore < 1 || gap < 0.8) return BIAS.neutral;
  if (bullishScore > bearishScore) return BIAS.bullish;
  if (bearishScore > bullishScore) return BIAS.bearish;
  return BIAS.neutral;
}

async function main() {
  const pageSize = parseInt(getArgValue('--page-size', '200'), 10) || 200;
  const dryRun = hasFlag('--dry-run');

  const signals = await getSignals({ pageSize });
  const neutralSignals = signals.filter(s => /neutral/i.test(String(s.bias || '')));

  const proposed = neutralSignals.map(signal => {
    const text = `${signal.event || ''} ${signal.details || ''}`.toLowerCase();
    const newBias = inferBias(text, signal.symbols || []);
    return {
      id: signal.id,
      event: signal.event,
      oldBias: signal.bias,
      newBias,
      symbols: signal.symbols || [],
    };
  });

  const toUpdate = proposed.filter(item => !/neutral/i.test(item.newBias));
  const unchanged = proposed.filter(item => /neutral/i.test(item.newBias));

  const updated = [];
  const failed = [];

  if (!dryRun) {
    for (const item of toUpdate) {
      try {
        await updateSignalFields(item.id, { bias: item.newBias });
        updated.push(item);
      } catch (error) {
        failed.push({ ...item, error: error.message });
      }
      await sleep(150);
    }
  }

  console.log(JSON.stringify({
    success: true,
    dryRun,
    totalSignals: signals.length,
    neutralBefore: neutralSignals.length,
    toDirectional: toUpdate.length,
    keptNeutral: unchanged.length,
    updated: dryRun ? 0 : updated.length,
    failed: dryRun ? 0 : failed.length,
    directionalBreakdown: {
      bullish: toUpdate.filter(item => /bullish/i.test(item.newBias)).length,
      bearish: toUpdate.filter(item => /bearish/i.test(item.newBias)).length,
    },
    examples: {
      toBullish: toUpdate.filter(item => /bullish/i.test(item.newBias)).slice(0, 5),
      toBearish: toUpdate.filter(item => /bearish/i.test(item.newBias)).slice(0, 5),
    },
    failedItems: failed.slice(0, 10),
  }, null, 2));
}

main().catch(error => {
  console.error(JSON.stringify({ success: false, error: error.message }, null, 2));
  process.exit(1);
});
