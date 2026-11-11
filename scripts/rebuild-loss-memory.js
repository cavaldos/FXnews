#!/usr/bin/env node
require('dotenv').config({ path: require('path').join(__dirname, '..', '.env'), quiet: true });

const fs = require('fs');
const path = require('path');

const { getSignals } = require('../src/news-trade-db');
const { LOSS_MEMORY_PATTERNS, buildLossMemorySearchText, patternMatchesContext } = require('../src/analysis/lossMemoryPatterns');
const { LOSS_MEMORY_PATH, resetLossMemoryCache } = require('../src/analysis/lossMemory');
const { SYMBOL_FAMILY_RANKINGS } = require('../src/analysis/symbolFamilies');
const { prefetchValidationData } = require('../src/verification/prefetchValidationData');
const { evaluateIntradayResult } = require('../src/verification/intradayResultChecker');

const LESSONS_DOC_PATH = path.join(process.cwd(), 'docs', 'validation-lessons.md');

function ensureDir(filePath) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
}

function extractSignalVerdict(signal) {
  if (signal.check === 'Win' || signal.check === 'Loss' || signal.check === 'Tie') {
    return signal.check;
  }

  if (String(signal.result || '').startsWith('Win')) return 'Win';
  if (String(signal.result || '').startsWith('Loss')) return 'Loss';
  if (String(signal.result || '').startsWith('Tie')) return 'Tie';
  return 'Pass';
}

function pct(numerator, denominator) {
  if (!denominator) return 0;
  return Number(((numerator / denominator) * 100).toFixed(1));
}

function summarizeMinutes(values) {
  const clean = (values || [])
    .map(value => Number(value))
    .filter(value => Number.isFinite(value))
    .sort((a, b) => a - b);

  if (clean.length === 0) {
    return {
      count: 0,
      average: null,
      median: null,
      min: null,
      max: null,
    };
  }

  const mid = Math.floor(clean.length / 2);
  const median = clean.length % 2 === 0
    ? Number((((clean[mid - 1] + clean[mid]) / 2)).toFixed(1))
    : clean[mid];
  const average = Number((clean.reduce((sum, value) => sum + value, 0) / clean.length).toFixed(1));

  return {
    count: clean.length,
    average,
    median,
    min: clean[0],
    max: clean[clean.length - 1],
  };
}

async function buildValidationRows(signals) {
  const options = {
    windowMinutes: 90,
    minMovePct: 0.05,
    interval: '1m',
    marketLookaheadMinutes: 4320,
  };

  const prefetched = await prefetchValidationData(signals, options);
  const rows = [];

  for (const signal of signals) {
    const evaluation = await evaluateIntradayResult(signal, {
      ...options,
      prefetchedData: prefetched.dataBySymbol,
    });

    rows.push({
      signal,
      evaluation,
      verdict: evaluation.result,
    });
  }

  return {
    rows,
    prefetch: {
      runId: prefetched.runId,
      outDir: prefetched.outDir,
      manifestPath: prefetched.manifestPath,
      supportedSymbols: prefetched.manifest.requestedSymbols.length,
      successCount: prefetched.manifest.successCount,
      emptyCount: prefetched.manifest.emptyCount,
      errorCount: prefetched.manifest.errorCount,
    },
  };
}

function buildSymbolPerformance(validationRows) {
  const symbolStats = new Map();

  for (const row of validationRows) {
    for (const outcomeRow of row.evaluation.symbolOutcomes || []) {
      const symbol = outcomeRow.mt5Symbol;
      const outcome = String(outcomeRow.outcome || '').toLowerCase();
      if (!symbol || !['win', 'loss', 'pass'].includes(outcome)) {
        continue;
      }

      if (!symbolStats.has(symbol)) {
        symbolStats.set(symbol, {
          win: 0,
          loss: 0,
          pass: 0,
          total: 0,
          minutesToWin: [],
          minutesToLoss: [],
        });
      }

      const row = symbolStats.get(symbol);
      row.total += 1;
      row[outcome] += 1;

      if (outcome === 'win' && outcomeRow.minutesToOutcome != null) {
        row.minutesToWin.push(outcomeRow.minutesFromSignalToOutcome ?? outcomeRow.minutesToOutcome);
      }

      if (outcome === 'loss' && outcomeRow.minutesToOutcome != null) {
        row.minutesToLoss.push(outcomeRow.minutesFromSignalToOutcome ?? outcomeRow.minutesToOutcome);
      }
    }
  }

  return Object.fromEntries(
    [...symbolStats.entries()].map(([symbol, stats]) => [symbol, {
      win: stats.win,
      loss: stats.loss,
      pass: stats.pass,
      total: stats.total,
      decisive: stats.win + stats.loss,
      winRate: pct(stats.win, stats.win + stats.loss),
      lossRate: pct(stats.loss, stats.win + stats.loss),
      timing: {
        toWinMinutes: summarizeMinutes(stats.minutesToWin),
        toLossMinutes: summarizeMinutes(stats.minutesToLoss),
      },
    }])
  );
}

function buildPatternStats(validationRows) {
  return LOSS_MEMORY_PATTERNS.map(pattern => {
    const stats = {
      id: pattern.id,
      title: pattern.title,
      matches: 0,
      win: 0,
      loss: 0,
      tie: 0,
      pass: 0,
      minutesToWin: [],
      minutesToLoss: [],
      lossExamples: [],
    };

    for (const row of validationRows) {
      const signal = row.signal;
      const context = {
        event: signal.event,
        details: signal.details,
        text: buildLossMemorySearchText(signal),
        primarySymbol: (signal.symbols || [])[0] || '',
        bias: signal.bias,
        impact: signal.impact,
      };

      if (!patternMatchesContext(pattern, context)) {
        continue;
      }

      const verdict = row.verdict;
      stats.matches += 1;
      if (verdict === 'Win') stats.win += 1;
      else if (verdict === 'Loss') stats.loss += 1;
      else if (verdict === 'Tie') stats.tie += 1;
      else stats.pass += 1;

      for (const symbolOutcome of row.evaluation.symbolOutcomes || []) {
        if (symbolOutcome.outcome === 'win' && symbolOutcome.minutesToOutcome != null) {
          stats.minutesToWin.push(symbolOutcome.minutesFromSignalToOutcome ?? symbolOutcome.minutesToOutcome);
        }

        if (symbolOutcome.outcome === 'loss' && symbolOutcome.minutesToOutcome != null) {
          stats.minutesToLoss.push(symbolOutcome.minutesFromSignalToOutcome ?? symbolOutcome.minutesToOutcome);
          stats.lossExamples.push({
            event: signal.event,
            time: signal.time,
            symbol: symbolOutcome.mt5Symbol,
            minutesToLoss: symbolOutcome.minutesFromSignalToOutcome ?? symbolOutcome.minutesToOutcome,
            outcomeReachedAt: symbolOutcome.outcomeReachedAt,
          });
        }
      }
    }

    const decisive = stats.win + stats.loss;
    return {
      ...pattern,
      ...stats,
      decisive,
      lossRate: pct(stats.loss, decisive),
      winRate: pct(stats.win, decisive),
      timing: {
        toWinMinutes: summarizeMinutes(stats.minutesToWin),
        toLossMinutes: summarizeMinutes(stats.minutesToLoss),
      },
      lossExamples: stats.lossExamples
        .sort((a, b) => (a.minutesToLoss || 0) - (b.minutesToLoss || 0))
        .slice(0, 5),
    };
  });
}

function buildLessons(patternStats) {
  return patternStats
    .filter(pattern => pattern.matches >= (pattern.minSamples || 1))
    .filter(pattern => pattern.loss > pattern.win)
    .filter(pattern => pattern.lossRate >= (pattern.minLossRate || 0))
    .map(pattern => ({
      id: pattern.id,
      title: pattern.title,
      action: pattern.action,
      penalty: pattern.penalty,
      note: pattern.note,
      symbol: pattern.symbol,
      bias: pattern.bias,
      impacts: pattern.impacts || [],
      keywordsAny: pattern.keywordsAny || [],
      matches: pattern.matches,
      win: pattern.win,
      loss: pattern.loss,
      tie: pattern.tie,
      pass: pattern.pass,
      lossRate: pattern.lossRate,
      winRate: pattern.winRate,
      timing: pattern.timing,
      lossExamples: pattern.lossExamples,
    }));
}

function buildLessonsMarkdown(memory) {
  const lines = [];
  lines.push('# Validation Lessons');
  lines.push('');
  lines.push(`Generated: ${memory.generatedAt}`);
  lines.push('');
  lines.push('## Summary');
  lines.push('');
  lines.push(`- Signals analyzed: ${memory.summary.totalSignals}`);
  lines.push(`- Loss signals: ${memory.summary.lossSignals}`);
  lines.push(`- Active lessons: ${memory.lessons.length}`);
  lines.push('');
  lines.push('## Active Lessons');
  lines.push('');

  if (memory.lessons.length === 0) {
    lines.push('- No active lessons met the minimum thresholds.');
  } else {
    for (const lesson of memory.lessons) {
      lines.push(`- ${lesson.title}`);
      lines.push(`  - Symbol: ${lesson.symbol}`);
      lines.push(`  - Bias: ${lesson.bias}`);
      lines.push(`  - Action: ${lesson.action}`);
      lines.push(`  - Loss rate: ${lesson.lossRate}% (${lesson.loss} loss / ${lesson.win} win)`);
      if (lesson.timing?.toLossMinutes?.count) {
        lines.push(`  - Time from signal to loss: avg ${lesson.timing.toLossMinutes.average}m | median ${lesson.timing.toLossMinutes.median}m | range ${lesson.timing.toLossMinutes.min}-${lesson.timing.toLossMinutes.max}m`);
      }
      lines.push(`  - Note: ${lesson.note}`);
      if (Array.isArray(lesson.lossExamples) && lesson.lossExamples.length > 0) {
        lines.push(`  - Examples:`);
        for (const example of lesson.lossExamples.slice(0, 3)) {
          lines.push(`    - ${example.event} | ${example.symbol} | hit loss in ${example.minutesToLoss}m`);
        }
      }
    }
  }

  lines.push('');
  lines.push('## Family Winners');
  lines.push('');
  for (const family of memory.familyWinners) {
    lines.push(`- ${family.family}: ${family.winner} (${family.winnerWinRate}% win rate)`);
  }

  return lines.join('\n') + '\n';
}

async function main() {
  const signals = await getSignals({ pageSize: 200 });
  const validation = await buildValidationRows(signals);
  const symbolPerformance = buildSymbolPerformance(validation.rows);
  const patternStats = buildPatternStats(validation.rows);
  const lessons = buildLessons(patternStats);

  const memory = {
    generatedAt: new Date().toISOString(),
    summary: {
      totalSignals: signals.length,
      lossSignals: validation.rows.filter(row => row.verdict === 'Loss').length,
      winSignals: validation.rows.filter(row => row.verdict === 'Win').length,
      tieSignals: validation.rows.filter(row => row.verdict === 'Tie').length,
      passSignals: validation.rows.filter(row => row.verdict === 'Pass').length,
    },
    validation: validation.prefetch,
    familyWinners: SYMBOL_FAMILY_RANKINGS.map(family => ({
      family: family.family,
      winner: family.rankedSymbols[0],
      winnerWinRate: symbolPerformance[family.rankedSymbols[0]]?.winRate || family.winRates?.[family.rankedSymbols[0]] || 0,
      retiredSymbols: family.rankedSymbols.slice(1),
    })),
    symbolPerformance,
    patterns: patternStats,
    lessons,
  };

  ensureDir(LOSS_MEMORY_PATH);
  fs.writeFileSync(LOSS_MEMORY_PATH, JSON.stringify(memory, null, 2));

  ensureDir(LESSONS_DOC_PATH);
  fs.writeFileSync(LESSONS_DOC_PATH, buildLessonsMarkdown(memory));

  resetLossMemoryCache();

  console.log(JSON.stringify({
    success: true,
    lossMemoryPath: LOSS_MEMORY_PATH,
    lessonsDocPath: LESSONS_DOC_PATH,
    validation: validation.prefetch,
    lessons: memory.lessons,
    summary: memory.summary,
  }, null, 2));
}

main().catch(error => {
  console.error(JSON.stringify({ success: false, error: error.message }, null, 2));
  process.exit(1);
});
