#!/usr/bin/env node
/**
 * Intraday reaction check for historical signals.
 *
 * Purpose:
 * - Check whether price moved in the signal bias direction shortly after news time
 * - Write `Check` and `Result` back to Notion unless `--dry-run` is used
 *
 * Modes:
 *   --pending          Check active signals only (default: New / Watching / empty status)
 *   --all              Check all signals
 *   --id <pageId>      Check one signal
 *   --dry-run          Preview only; no Notion writes occur
 *
 * Options:
 *   --window-minutes <n>  Reaction window in minutes (default: 90)
 *   --min-move-pct <n>    Minimum move threshold in percent (default: 0.05)
 *   --interval <tf>       Yahoo chart interval (default: 1m)
 *   --page-size <n>       Number of signals to load (default: 100)
 *   --prefetch-dir <dir>  Directory to store prefetched OHLC files
 *   --lookahead-minutes <n> Extra minutes to keep fetching after signal time to catch the next tradable session (default: 4320)
 */
require('dotenv').config({ path: require('path').join(__dirname, '..', '..', '..', '..', '.env'), quiet: true });

const { getSignals, updateSignalFields } = require('../../../../src/news-trade-db');
const { evaluateIntradayResult } = require('../../../../src/verification/intradayResultChecker');
const { prefetchValidationData } = require('../../../../src/verification/prefetchValidationData');

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

function parseNumber(value, fallback) {
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
}

function isActiveSignal(signal) {
  const status = String(signal.status || '').toLowerCase();
  return !status || /new|watching|moi/i.test(status);
}

async function loadTargetSignals() {
  const pageSize = parseInt(getArgValue('--page-size', '100'), 10) || 100;
  const signalId = getArgValue('--id');
  const modeAll = hasFlag('--all');

  const allSignals = await getSignals({ pageSize });

  if (signalId) {
    const signal = allSignals.find(s => s.id === signalId);
    if (!signal) throw new Error(`Signal not found: ${signalId}`);
    return [signal];
  }

  if (modeAll) return allSignals;

  return allSignals.filter(isActiveSignal);
}

async function main() {
  const dryRun = hasFlag('--dry-run');
  const options = {
    windowMinutes: parseInt(getArgValue('--window-minutes', '90'), 10) || 90,
    minMovePct: parseNumber(getArgValue('--min-move-pct', '0.05'), 0.05),
    interval: getArgValue('--interval', '1m') || '1m',
    prefetchOutDir: getArgValue('--prefetch-dir') || null,
    marketLookaheadMinutes: parseInt(getArgValue('--lookahead-minutes', '4320'), 10) || 4320,
  };

  const targets = await loadTargetSignals();
  if (targets.length === 0) {
    console.log(JSON.stringify({ success: true, checked: 0, writesToNotion: false, message: 'No signals to check.' }, null, 2));
    return;
  }

  const prefetched = await prefetchValidationData(targets, {
    windowMinutes: options.windowMinutes,
    minMovePct: options.minMovePct,
    interval: options.interval,
    outDir: options.prefetchOutDir,
    marketLookaheadMinutes: options.marketLookaheadMinutes,
  });

  const details = [];
  let failed = 0;
  let updated = 0;

  for (let i = 0; i < targets.length; i++) {
    const signal = targets[i];
    try {
      const result = await evaluateIntradayResult(signal, {
        ...options,
        prefetchedData: prefetched.dataBySymbol,
      });

      if (!dryRun) {
        await updateSignalFields(signal.id, {
          check: result.check,
          result: result.resultText,
        });
        updated += 1;
      }

      details.push({
        pageId: signal.id,
        event: signal.event,
        status: signal.status || '',
        bias: signal.bias,
        symbols: signal.symbols,
        check: result.check,
        finalResult: result.result,
        resultText: result.resultText,
        windowMinutes: options.windowMinutes,
        minMovePct: options.minMovePct,
        interval: options.interval,
        symbolOutcomes: result.symbolOutcomes.map(item => ({
          symbol: item.mt5Symbol,
          outcome: item.outcome,
          observedDirection: item.observedDirection || null,
          dataSource: item.dataSource || null,
          usedInterval: item.usedInterval || null,
          upPct: item.upPct ?? null,
          downPct: item.downPct ?? null,
          reason: item.reason || null,
        })),
      });
    } catch (error) {
      failed += 1;
      details.push({
        pageId: signal.id,
        event: signal.event,
        error: error.message,
      });
    }

    if (i < targets.length - 1) {
      await sleep(250);
    }
  }

  const summary = {
    success: true,
    dryRun,
    writesToNotion: !dryRun,
    checked: targets.length,
    failed,
    updated,
    prefetch: {
      runId: prefetched.runId,
      outDir: prefetched.outDir,
      manifestPath: prefetched.manifestPath,
      supportedSymbols: prefetched.manifest.requestedSymbols.length,
      unsupportedSymbols: prefetched.manifest.unsupportedSymbols,
      successCount: prefetched.manifest.successCount,
      emptyCount: prefetched.manifest.emptyCount,
      errorCount: prefetched.manifest.errorCount,
    },
    resultBreakdown: {
      Win: details.filter(d => d.finalResult === 'Win').length,
      Loss: details.filter(d => d.finalResult === 'Loss').length,
      Tie: details.filter(d => d.finalResult === 'Tie').length,
      Pass: details.filter(d => d.finalResult === 'Pass').length,
    },
    options,
    details,
  };

  console.log(JSON.stringify(summary, null, 2));
}

main().catch(err => {
  console.error(JSON.stringify({ success: false, error: err.message }, null, 2));
  process.exit(1);
});
