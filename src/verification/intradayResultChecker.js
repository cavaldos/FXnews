const { toYahooSymbol, normalizeMt5Symbol } = require('../prices/mt5ToYahoo');
const { fetchYahooChart, parseYahooOhlcv } = require('../prices/yahooChart');
const { normalizeBias } = require('./signalVerifier');

const RESULT = {
  win: 'Win',
  loss: 'Loss',
  tie: 'Tie',
  pass: 'Pass',
};

const CHECK = {
  win: 'Win',
  loss: 'Loss',
  tie: 'Tie',
};

const OBSERVED_DIRECTION = {
  up: 'up',
  down: 'down',
  flat: 'flat',
  mixed: 'mixed',
};

function pctChange(from, to) {
  if (from == null || to == null || from === 0) return null;
  return ((to - from) / from) * 100;
}

function toIsoEpoch(epochSeconds) {
  return new Date(epochSeconds * 1000).toISOString();
}

function toMinutes(startEpochSeconds, endEpochSeconds) {
  if (!Number.isFinite(startEpochSeconds) || !Number.isFinite(endEpochSeconds)) {
    return null;
  }

  return Number((((endEpochSeconds - startEpochSeconds) / 60)).toFixed(1));
}

function formatPct(value) {
  if (value == null) return 'N/A';
  const sign = value >= 0 ? '+' : '';
  return `${sign}${value.toFixed(3)}%`;
}

function formatMinutes(value) {
  if (value == null) return 'N/A';
  return `${value}m`;
}

function summarizeMajorityCheck(symbolOutcomes) {
  const decisive = (symbolOutcomes || []).filter(item => item.outcome === 'win' || item.outcome === 'loss');
  const wins = decisive.filter(item => item.outcome === 'win').length;
  const losses = decisive.filter(item => item.outcome === 'loss').length;

  let finalResult = RESULT.pass;
  let check = null;

  if (wins > losses) {
    finalResult = RESULT.win;
    check = CHECK.win;
  } else if (losses > wins) {
    finalResult = RESULT.loss;
    check = CHECK.loss;
  } else if (wins > 0 && losses > 0) {
    finalResult = RESULT.tie;
    check = CHECK.tie;
  }

  return {
    wins,
    losses,
    finalResult,
    check,
  };
}

function buildResultText(finalResult, symbolOutcomes, summary = null) {
  const parts = [finalResult];

  if (summary) {
    parts.push(`wins:${summary.wins}`);
    parts.push(`losses:${summary.losses}`);
  }

  for (const item of symbolOutcomes) {
    if (item.reason) {
      parts.push(`${item.mt5Symbol}:${item.outcome.toUpperCase()}(${item.reason})`);
      continue;
    }

    const timingText = item.minutesToOutcome != null ? `, hit ${formatMinutes(item.minutesToOutcome)}` : '';
    parts.push(
      `${item.mt5Symbol}:${item.outcome.toUpperCase()}(dir ${(item.observedDirection || 'na').toUpperCase()}${timingText}, up ${formatPct(item.upPct)}, down ${formatPct(item.downPct)})`
    );
  }

  return parts.join(' | ').slice(0, 2000);
}

function getObservedDirectionFromWindow(baseline, maxHigh, minLow, minMovePct) {
  const upPct = pctChange(baseline, maxHigh);
  const downPct = pctChange(baseline, minLow);
  const hasUpMove = upPct != null && upPct >= minMovePct;
  const hasDownMove = downPct != null && downPct <= -minMovePct;

  if (hasUpMove && !hasDownMove) return OBSERVED_DIRECTION.up;
  if (hasDownMove && !hasUpMove) return OBSERVED_DIRECTION.down;

  if (hasUpMove && hasDownMove) {
    if (upPct > Math.abs(downPct)) return OBSERVED_DIRECTION.up;
    if (Math.abs(downPct) > upPct) return OBSERVED_DIRECTION.down;
    return OBSERVED_DIRECTION.mixed;
  }

  return OBSERVED_DIRECTION.flat;
}

function findFirstThresholdCrossings(rows, baseline, minMovePct) {
  const upThreshold = baseline * (1 + (minMovePct / 100));
  const downThreshold = baseline * (1 - (minMovePct / 100));
  let firstUpRow = null;
  let firstDownRow = null;

  for (const row of rows || []) {
    const high = row.high == null ? row.close : row.high;
    const low = row.low == null ? row.close : row.low;

    if (!firstUpRow && high != null && high >= upThreshold) {
      firstUpRow = row;
    }

    if (!firstDownRow && low != null && low <= downThreshold) {
      firstDownRow = row;
    }

    if (firstUpRow && firstDownRow) {
      break;
    }
  }

  return {
    upThreshold,
    downThreshold,
    firstUpRow,
    firstDownRow,
  };
}

function getOutcomeFromWindow(bias, baseline, maxHigh, minLow, minMovePct) {
  if (bias === 'neutral') return 'pass';

  const observedDirection = getObservedDirectionFromWindow(baseline, maxHigh, minLow, minMovePct);

  if (observedDirection === OBSERVED_DIRECTION.flat || observedDirection === OBSERVED_DIRECTION.mixed) {
    return 'pass';
  }

  if (bias === 'bullish') {
    return observedDirection === OBSERVED_DIRECTION.up ? 'win' : 'loss';
  }

  if (bias === 'bearish') {
    return observedDirection === OBSERVED_DIRECTION.down ? 'win' : 'loss';
  }

  return 'pass';
}

function getPrefetchedSymbolData(mt5Symbol, options = {}) {
  const key = normalizeMt5Symbol(mt5Symbol);
  return options.prefetchedData?.[key] || null;
}

async function evaluateSymbolWindow(mt5Symbol, signalEpochSeconds, options) {
  const yahooSymbol = toYahooSymbol(mt5Symbol);
  if (!yahooSymbol) {
    return {
      mt5Symbol,
      yahooSymbol: null,
      outcome: 'pass',
      reason: 'unsupported_symbol',
    };
  }

  const warmupMinutes = options.warmupMinutes || 15;
  const windowMinutes = options.windowMinutes || 90;
  const period1 = signalEpochSeconds - warmupMinutes * 60;
  const period2 = signalEpochSeconds + (windowMinutes + warmupMinutes) * 60;

  const intervalCandidates = [
    options.interval || '1m',
    '5m',
    '15m',
    '30m',
    '60m',
  ].filter((value, idx, arr) => arr.indexOf(value) === idx);

  try {
    let parsed = null;
    let usedInterval = null;
    let dataSource = 'live';
    const prefetched = getPrefetchedSymbolData(mt5Symbol, options);

    if (prefetched) {
      parsed = {
        rows: prefetched.rows || [],
        meta: prefetched.meta || {},
      };
      usedInterval = prefetched.usedInterval || options.interval || '1m';
      dataSource = 'prefetched';
    } else {
      let lastError = null;

      for (const interval of intervalCandidates) {
        try {
          const chart = await fetchYahooChart(yahooSymbol, {
            interval,
            period1,
            period2,
            maxRetries: options.maxRetries ?? 4,
          });
          parsed = parseYahooOhlcv(chart);
          usedInterval = interval;
          break;
        } catch (error) {
          lastError = error;
        }
      }

      if (!parsed) {
        throw lastError || new Error('Unable to fetch OHLC data from Yahoo');
      }
    }

    const allRows = parsed.rows || [];
    const startBar = allRows.find(row => row.time >= signalEpochSeconds) || null;
    if (!startBar) {
      return {
        mt5Symbol,
        yahooSymbol,
        outcome: 'pass',
        reason: 'no_bar_after_signal_time',
      };
    }

    const windowEnd = startBar.time + windowMinutes * 60;
    const rows = allRows.filter(row => row.time >= startBar.time && row.time <= windowEnd);

    if (rows.length === 0) {
      return {
        mt5Symbol,
        yahooSymbol,
        outcome: 'pass',
        reason: 'no_data_in_window',
      };
    }

    const baseline = startBar.close;
    const highs = rows.map(row => (row.high == null ? row.close : row.high)).filter(v => v != null);
    const lows = rows.map(row => (row.low == null ? row.close : row.low)).filter(v => v != null);

    if (baseline == null || highs.length === 0 || lows.length === 0) {
      return {
        mt5Symbol,
        yahooSymbol,
        outcome: 'pass',
        reason: 'insufficient_price_points',
      };
    }

    const maxHigh = Math.max(...highs);
    const minLow = Math.min(...lows);
    const crossings = findFirstThresholdCrossings(rows, baseline, options.minMovePct);
    const observedDirection = getObservedDirectionFromWindow(baseline, maxHigh, minLow, options.minMovePct);

    return {
      mt5Symbol,
      yahooSymbol,
      baseline,
      usedInterval,
      dataSource,
      startTime: toIsoEpoch(startBar.time),
      endTime: toIsoEpoch(rows[rows.length - 1].time),
      startDelayMinutes: toMinutes(signalEpochSeconds, startBar.time),
      maxHigh,
      minLow,
      upPct: pctChange(baseline, maxHigh),
      downPct: pctChange(baseline, minLow),
      firstUpMoveAt: crossings.firstUpRow ? toIsoEpoch(crossings.firstUpRow.time) : null,
      firstDownMoveAt: crossings.firstDownRow ? toIsoEpoch(crossings.firstDownRow.time) : null,
      minutesToUpMove: crossings.firstUpRow ? toMinutes(startBar.time, crossings.firstUpRow.time) : null,
      minutesToDownMove: crossings.firstDownRow ? toMinutes(startBar.time, crossings.firstDownRow.time) : null,
      minutesFromSignalToUpMove: crossings.firstUpRow ? toMinutes(signalEpochSeconds, crossings.firstUpRow.time) : null,
      minutesFromSignalToDownMove: crossings.firstDownRow ? toMinutes(signalEpochSeconds, crossings.firstDownRow.time) : null,
      observedDirection,
      bars: rows.length,
      outcome: null,
      reason: null,
    };
  } catch (error) {
    return {
      mt5Symbol,
      yahooSymbol,
      outcome: 'pass',
      reason: `fetch_error: ${error.message}`,
    };
  }
}

function buildNotes(signal, finalResult, check, symbolOutcomes, options, summary = null) {
  const signalTime = signal.time || 'N/A';
  const lines = [];
  lines.push(
    `Intraday result check | Window: ${options.windowMinutes}m | Threshold: ${options.minMovePct}% | Bias: ${signal.bias || 'N/A'} | Time: ${signalTime}`
  );
  if (summary) {
    lines.push(`Majority rule: wins ${summary.wins} vs losses ${summary.losses}${check ? ` | Check: ${check}` : ' | Check: undecided'}`);
  }

  for (const item of symbolOutcomes) {
    if (item.reason) {
      lines.push(`${item.mt5Symbol}: ${item.outcome.toUpperCase()} (${item.reason})`);
      continue;
    }

    lines.push(
      `${item.mt5Symbol}: ${item.outcome.toUpperCase()} | dir ${item.observedDirection || 'na'} | hit ${formatMinutes(item.minutesToOutcome)} | base ${item.baseline} | up ${formatPct(item.upPct)} | down ${formatPct(item.downPct)} | bars ${item.bars} | source ${item.dataSource || 'live'}`
    );
  }

  lines.push(`Final Result: ${finalResult}`);
  return lines.join('\n').slice(0, 2000);
}

function addOutcomeTiming(bias, outcome, base) {
  const isBullish = bias === 'bullish';
  const minutesToWin = isBullish ? base.minutesToUpMove : base.minutesToDownMove;
  const minutesToLoss = isBullish ? base.minutesToDownMove : base.minutesToUpMove;
  const minutesFromSignalToWin = isBullish ? base.minutesFromSignalToUpMove : base.minutesFromSignalToDownMove;
  const minutesFromSignalToLoss = isBullish ? base.minutesFromSignalToDownMove : base.minutesFromSignalToUpMove;
  const outcomeReachedAt = outcome === 'win'
    ? (isBullish ? base.firstUpMoveAt : base.firstDownMoveAt)
    : outcome === 'loss'
      ? (isBullish ? base.firstDownMoveAt : base.firstUpMoveAt)
      : null;

  return {
    minutesToWin,
    minutesToLoss,
    minutesFromSignalToWin,
    minutesFromSignalToLoss,
    minutesToOutcome: outcome === 'win' ? minutesToWin : outcome === 'loss' ? minutesToLoss : null,
    minutesFromSignalToOutcome: outcome === 'win' ? minutesFromSignalToWin : outcome === 'loss' ? minutesFromSignalToLoss : null,
    outcomeReachedAt,
  };
}

async function evaluateIntradayResult(signal, options = {}) {
  const normalizedBias = normalizeBias(signal.bias);
  const signalEpochMs = new Date(signal.time || '').getTime();

  const config = {
    interval: options.interval || '1m',
    windowMinutes: options.windowMinutes || 90,
    minMovePct: options.minMovePct ?? 0.05,
    warmupMinutes: options.warmupMinutes || 15,
    maxRetries: options.maxRetries ?? 4,
    prefetchedData: options.prefetchedData || null,
  };

  if (normalizedBias === 'neutral') {
    const symbolOutcomes = (Array.isArray(signal.symbols) ? signal.symbols : []).map(symbol => ({
      mt5Symbol: symbol,
      yahooSymbol: toYahooSymbol(symbol),
      outcome: 'pass',
      reason: 'neutral_bias',
    }));

    const summary = summarizeMajorityCheck(symbolOutcomes);

    return {
      signalId: signal.id,
      result: summary.finalResult,
      check: summary.check,
      resultText: buildResultText(summary.finalResult, symbolOutcomes, summary),
      notes: buildNotes(signal, summary.finalResult, summary.check, symbolOutcomes, config, summary),
      biasNormalized: normalizedBias,
      symbolOutcomes,
      summary,
      config,
    };
  }

  if (Number.isNaN(signalEpochMs)) {
    return {
      result: RESULT.pass,
      check: null,
      resultText: RESULT.pass,
      notes: `Intraday result check skipped: invalid signal time (${signal.time || 'N/A'})`,
      biasNormalized: normalizedBias,
      signalId: signal.id,
      symbolOutcomes: [],
      summary: summarizeMajorityCheck([]),
      config,
    };
  }

  if (signalEpochMs > Date.now()) {
    const symbolOutcomes = (Array.isArray(signal.symbols) ? signal.symbols : []).map(symbol => ({
      mt5Symbol: symbol,
      yahooSymbol: toYahooSymbol(symbol),
      outcome: 'pass',
      reason: 'future_signal_time',
    }));

    const summary = summarizeMajorityCheck(symbolOutcomes);

    return {
      signalId: signal.id,
      result: summary.finalResult,
      check: summary.check,
      resultText: buildResultText(summary.finalResult, symbolOutcomes, summary),
      notes: buildNotes(signal, summary.finalResult, summary.check, symbolOutcomes, config, summary),
      biasNormalized: normalizedBias,
      symbolOutcomes,
      summary,
      config,
    };
  }

  const signalEpochSeconds = Math.floor(signalEpochMs / 1000);
  const symbols = Array.isArray(signal.symbols) ? signal.symbols : [];

  const symbolOutcomes = [];
  for (const symbol of symbols) {
    const base = await evaluateSymbolWindow(symbol, signalEpochSeconds, config);

    const outcome =
      base.outcome ||
      getOutcomeFromWindow(normalizedBias, base.baseline, base.maxHigh, base.minLow, config.minMovePct);

    symbolOutcomes.push({
      ...base,
      ...addOutcomeTiming(normalizedBias, outcome, base),
      outcome,
    });
  }

  const summary = summarizeMajorityCheck(symbolOutcomes);
  const finalResult = summary.finalResult;
  const notes = buildNotes(signal, finalResult, summary.check, symbolOutcomes, config, summary);

  return {
    signalId: signal.id,
    result: finalResult,
    check: summary.check,
    resultText: buildResultText(finalResult, symbolOutcomes, summary),
    notes,
    biasNormalized: normalizedBias,
    symbolOutcomes,
    summary,
    config,
  };
}

module.exports = {
  CHECK,
  RESULT,
  evaluateIntradayResult,
  summarizeMajorityCheck,
};
