#!/usr/bin/env node
/**
 * Historical signal verification against forex prices.
 *
 * Verifies directional calls by comparing signal bias against realized FX moves.
 * This script no longer writes verification output back to Notion.
 *
 * Modes:
 *   --all              Verify all forex-eligible signals in the loaded page set
 *   --dry-run          Alias of --all kept for backward compatibility
 *   --id <pageId>      Verify a specific signal by Notion page ID
 *   --report           Show full accuracy report from live verification
 *   --stats            Quick inventory summary without running verification
 *
 * Examples:
 *   node verify-signals.js --all
 *   node verify-signals.js --dry-run
 *   node verify-signals.js --id 315f312e-ab25-81bd-9383-ec825d7b9470
 *   node verify-signals.js --report
 *   node verify-signals.js --stats
 */
require('dotenv').config({ path: require('path').join(__dirname, '..', '..', '..', '..', '.env'), quiet: true });

const { getSignals } = require('../../../../src/news-trade-db');
const { verifySignal, verifySignals, generateReport } = require('../../../../src/verification/signalVerifier');
const { isForexSymbol } = require('../../../../src/verification/priceChecker');

function hasFlag(flag) {
  return process.argv.includes(flag);
}

function formatPct(pct) {
  if (pct == null) return 'N/A';
  const sign = pct >= 0 ? '+' : '';
  return `${sign}${pct.toFixed(4)}%`;
}

function printSignalSummary(signal) {
  console.log(`  Event: ${signal.event}`);
  console.log(`  Symbols: ${(signal.symbols || []).join(', ')}`);
  console.log(`  Bias: ${signal.bias} | Impact: ${signal.impact}`);
  console.log(`  Time: ${signal.time || 'N/A'}`);
}

function printVerificationResult(result) {
  console.log(`\n--- ${result.event} ---`);
  console.log(`  Signal ID: ${result.signalId}`);
  console.log(`  Original Bias: ${result.originalBias}`);
  console.log(`  Forex Symbols: ${result.forexSymbols.join(', ') || 'none'}`);

  if (result.nonForexSymbols.length > 0) {
    console.log(`  Non-Forex (skipped): ${result.nonForexSymbols.join(', ')}`);
  }

  console.log(`  Status: ${result.verificationStatus}`);

  if (result.verifiable && result.priceData) {
    const p = result.priceData;
    console.log(`  Price at T+0 (${p.t0?.date}): ${p.t0?.rate}`);
    if (p.t1) console.log(`  Price at T+1 (${p.t1.date}): ${p.t1.rate} (${formatPct(result.changes.d1)})`);
    if (p.t3) console.log(`  Price at T+3 (${p.t3.date}): ${p.t3.rate} (${formatPct(result.changes.d3)})`);
    if (p.t7) console.log(`  Price at T+7 (${p.t7.date}): ${p.t7.rate} (${formatPct(result.changes.d7)})`);
    console.log(`  Verification Score: ${result.verificationScore}/100`);
  }

  if (result.verificationNotes) {
    console.log(`  Notes: ${result.verificationNotes.split('\n')[0]}`);
  }
}

async function cmdVerifyAll(dryRun = false) {
  console.log('Fetching signals from Notion...');
  const signals = await getSignals({ pageSize: 100 });
  console.log(`Found ${signals.length} signals total.`);

  const toVerify = signals.filter(s => (s.symbols || []).some(sym => isForexSymbol(sym)));
  console.log(`${toVerify.length} signals eligible for forex verification.`);

  if (toVerify.length === 0) {
    console.log('Nothing to verify.');
    return;
  }

  console.log(`\nStarting verification${dryRun ? ' (legacy dry-run mode; no Notion writes)' : ''}...\n`);

  const results = await verifySignals(toVerify, { delayMs: 600 });

  for (const result of results) {
    printVerificationResult(result);
  }

  const report = generateReport(results);
  console.log('\n========== VERIFICATION SUMMARY ==========');
  console.log(`Total processed: ${results.length}`);
  console.log(`Verifiable (forex): ${report.summary.verifiable}`);
  console.log(`Scored: ${report.accuracy.correct + report.accuracy.incorrect + report.accuracy.inconclusive}`);
  console.log(`Correct: ${report.accuracy.correct}`);
  console.log(`Incorrect: ${report.accuracy.incorrect}`);
  console.log(`Inconclusive: ${report.accuracy.inconclusive}`);
  if (report.accuracy.accuracyRate != null) {
    console.log(`Accuracy Rate: ${report.accuracy.accuracyRate}%`);
    console.log(`Average Score: ${report.accuracy.averageScore}/100`);
  }
  console.log('Writes to Notion: false');
  console.log('==========================================');
}

async function cmdVerifyOne(pageId) {
  console.log(`Fetching signal ${pageId}...`);
  const signals = await getSignals({ pageSize: 100 });
  const signal = signals.find(s => s.id === pageId);

  if (!signal) {
    console.error(`Signal not found: ${pageId}`);
    process.exit(1);
  }

  printSignalSummary(signal);
  console.log('\nVerifying...');

  const result = await verifySignal(signal);
  printVerificationResult(result);

  console.log('\n--- JSON Output ---');
  console.log(JSON.stringify(result, null, 2));
}

async function cmdReport() {
  console.log('Fetching all signals from Notion...');
  const signals = await getSignals({ pageSize: 100 });

  const forexSignals = signals.filter(s =>
    (s.symbols || []).some(sym => isForexSymbol(sym))
  );

  console.log(`Found ${signals.length} signals total, ${forexSignals.length} with forex symbols.`);
  console.log('Verifying forex signals against historical prices...\n');

  const results = await verifySignals(forexSignals, { delayMs: 600 });
  const report = generateReport(results);

  console.log('========== ACCURACY REPORT ==========\n');

  console.log('--- Overview ---');
  console.log(`Total signals: ${report.summary.totalSignals}`);
  console.log(`Verifiable (forex): ${report.summary.verifiable}`);
  console.log(`Non-forex (skipped): ${report.summary.nonForex}`);
  console.log(`Too recent: ${report.summary.tooRecent}`);
  console.log(`Scored: ${report.accuracy.correct + report.accuracy.incorrect + report.accuracy.inconclusive}`);

  console.log('\n--- Accuracy ---');
  console.log(`Correct: ${report.accuracy.correct}`);
  console.log(`Incorrect: ${report.accuracy.incorrect}`);
  console.log(`Inconclusive: ${report.accuracy.inconclusive}`);
  if (report.accuracy.accuracyRate != null) {
    console.log(`Accuracy Rate: ${report.accuracy.accuracyRate}%`);
    console.log(`Average Score: ${report.accuracy.averageScore}/100`);
  } else {
    console.log('Accuracy Rate: N/A (no scored signals)');
  }

  if (Object.keys(report.byBias).length > 0) {
    console.log('\n--- By Bias ---');
    for (const [bias, stats] of Object.entries(report.byBias)) {
      console.log(`  ${bias}: ${stats.correct}/${stats.total} correct (${stats.accuracy}%) | avg score: ${stats.avgScore}`);
    }
  }

  if (Object.keys(report.byImpact).length > 0) {
    console.log('\n--- By Impact ---');
    for (const [impact, stats] of Object.entries(report.byImpact)) {
      console.log(`  ${impact}: ${stats.correct}/${stats.total} correct (${stats.accuracy}%) | avg score: ${stats.avgScore}`);
    }
  }

  if (Object.keys(report.bySymbol).length > 0) {
    console.log('\n--- By Symbol ---');
    const sorted = Object.entries(report.bySymbol).sort((a, b) => b[1].avgScore - a[1].avgScore);
    for (const [sym, stats] of sorted) {
      console.log(`  ${sym}: ${stats.correct}/${stats.total} correct (${stats.accuracy}%) | avg score: ${stats.avgScore}`);
    }
  }

  console.log('\n=====================================');
  console.log('\n--- JSON Report ---');
  console.log(JSON.stringify(report, null, 2));
}

async function cmdStats() {
  const signals = await getSignals({ pageSize: 100 });

  const stats = {
    total: signals.length,
    withForex: signals.filter(s => (s.symbols || []).some(sym => isForexSymbol(sym))).length,
    withoutForex: signals.filter(s => !(s.symbols || []).some(sym => isForexSymbol(sym))).length,
    active: signals.filter(s => !s.status || /new|watching/i.test(s.status)).length,
    byStatus: {},
    byBias: {},
    byImpact: {},
    writesToNotion: false,
  };

  signals.forEach(signal => {
    const statusKey = signal.status || '(empty)';
    const biasKey = signal.bias || '(empty)';
    const impactKey = signal.impact || '(empty)';
    stats.byStatus[statusKey] = (stats.byStatus[statusKey] || 0) + 1;
    stats.byBias[biasKey] = (stats.byBias[biasKey] || 0) + 1;
    stats.byImpact[impactKey] = (stats.byImpact[impactKey] || 0) + 1;
  });

  console.log(JSON.stringify(stats, null, 2));
}

const args = process.argv.slice(2);
const command = args[0];

(async () => {
  switch (command) {
    case '--all':
      await cmdVerifyAll(false);
      break;

    case '--dry-run':
      await cmdVerifyAll(true);
      break;

    case '--id':
      if (!args[1]) { console.error('Usage: --id <pageId>'); process.exit(1); }
      await cmdVerifyOne(args[1]);
      break;

    case '--report':
      await cmdReport();
      break;

    case '--stats':
      await cmdStats();
      break;

    default:
      console.log(`Historical signal verification against forex prices

Usage:
  node verify-signals.js --all          Verify all eligible signals
  node verify-signals.js --dry-run      Backward-compatible alias of --all
  node verify-signals.js --id <id>      Verify a specific signal by page ID
  node verify-signals.js --report       Full live accuracy report with breakdowns
  node verify-signals.js --stats        Quick signal inventory summary

Only forex pairs (EURUSD, GBPUSD, USDJPY, etc.) can be verified.
Writes to Notion: false.`);
  }
})().catch(err => {
  console.error(JSON.stringify({ error: err.message }));
  process.exit(1);
});

module.exports = { cmdVerifyAll, cmdVerifyOne, cmdReport, cmdStats };
