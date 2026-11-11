#!/usr/bin/env node
/**
 * Directional AI analysis for lean-schema signals.
 *
 * Modes:
 *   --pending          List active signals that are candidates for analysis
 *   --analyze <id>     Output analysis prompt for a specific signal
 *   --update <id>      Apply analysis result back to lean Notion fields
 *   --batch-update     Apply multiple analysis results (stdin JSON array)
 *   --ollama <id>      Auto-analyze with Ollama (requires Ollama running)
 *   --status           Show lean signal inventory summary
 *
 * Examples:
 *   node analyze-signals.js --pending
 *   node analyze-signals.js --analyze 315f312e-ab25-81bd-9383-ec825d7b9470
 *   echo '{"bias":"bullish",...}' | node analyze-signals.js --update 315f312e-ab25-81bd-9383-ec825d7b9470
 *   echo '[{...}]' | node analyze-signals.js --batch-update
 *   node analyze-signals.js --status
 */
require('dotenv').config({ path: require('path').join(__dirname, '..', '..', '..', '..', '.env'), quiet: true });
const { getSignals, updateSignalFields } = require('../../../../src/news-trade-db');

// ── Consensus algorithm ─────────────────────────────────────────────

const ACTION_MAP = { strong_buy: 5, buy: 4, hold: 3, sell: 2, strong_sell: 1 };
const IMPACT_MAP = { high: 3, medium: 2, low: 1 };
const RISK_MAP = { low: 1, medium: 2, high: 3 };

const ALLOWED_SYMBOLS = new Set([
  'EURUSD', 'GBPUSD', 'USDJPY', 'AUDUSD', 'NZDUSD', 'USDCAD', 'USDCHF', 'GBPJPY', 'EURJPY', 'EURGBP', 'AUDJPY', 'EURAUD',
  'US500', 'US30', 'NAS100', 'DE40', 'UK100', 'JP225', 'FR40',
  'XAUUSD', 'XAGUSD', 'USOIL', 'UKOIL', 'NGAS',
  'BTCUSD', 'ETHUSD',
]);

const ACTION_BIAS_HINT = {
  strong_buy: 'bullish',
  buy: 'bullish',
  hold: 'neutral',
  sell: 'bearish',
  strong_sell: 'bearish',
};

const BIAS_EMOJI = { bullish: '\u{1F7E2} Bullish', bearish: '\u{1F534} Bearish', neutral: '\u{1F7E1} Neutral' };
const IMPACT_EMOJI = { high: '\u{1F525} High', medium: '\u26A1 Medium', low: '\u{1F4A7} Low' };
const ACTION_EMOJI = {
  strong_buy: '\u{1F7E2} Strong Buy',
  buy: '\u{1F535} Buy',
  hold: '\u26AA Hold',
  sell: '\u{1F7E0} Sell',
  strong_sell: '\u{1F534} Strong Sell',
};
const RISK_EMOJI = { low: '\u{1F7E2} Low', medium: '\u{1F7E1} Medium', high: '\u{1F534} High' };

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function normalizeEnum(value, allowed, fallback) {
  const v = String(value || '').trim().toLowerCase();
  return allowed.includes(v) ? v : fallback;
}

function sanitizeModelResponse(raw, idx = 0) {
  const base = raw && typeof raw === 'object' ? raw : {};
  let bias = normalizeEnum(base.bias, ['bullish', 'bearish', 'neutral'], 'neutral');
  const action = normalizeEnum(base.action, ['strong_buy', 'buy', 'hold', 'sell', 'strong_sell'], 'hold');
  const impact = normalizeEnum(base.impact, ['high', 'medium', 'low'], 'medium');
  const risk = normalizeEnum(base.risk, ['low', 'medium', 'high'], 'medium');
  let confidence = Number(base.confidence);

  if (!Number.isFinite(confidence)) confidence = 50;
  confidence = Math.round(clamp(confidence, 0, 100));

  const hintedBias = ACTION_BIAS_HINT[action] || 'neutral';
  if (bias === 'neutral' && hintedBias !== 'neutral') {
    bias = hintedBias;
    confidence = Math.max(confidence, 55);
  }

  if (bias === 'neutral' && action === 'hold' && confidence > 60) {
    confidence = 60;
  }

  const symbols = Array.isArray(base.symbols)
    ? base.symbols
        .map(s => String(s || '').trim().toUpperCase())
        .filter(s => ALLOWED_SYMBOLS.has(s))
        .slice(0, 3)
    : [];

  const reasoning = String(base.reasoning || '').trim().slice(0, 500);

  let bullishScore = Number(base.bullish_score);
  let bearishScore = Number(base.bearish_score);

  if (!Number.isFinite(bullishScore) || !Number.isFinite(bearishScore)) {
    if (bias === 'bullish') {
      bullishScore = Math.max(confidence, 60);
      bearishScore = 100 - bullishScore;
    } else if (bias === 'bearish') {
      bearishScore = Math.max(confidence, 60);
      bullishScore = 100 - bearishScore;
    } else {
      bullishScore = 50;
      bearishScore = 50;
    }
  }

  bullishScore = clamp(Math.round(bullishScore), 0, 100);
  bearishScore = clamp(Math.round(bearishScore), 0, 100);

  return {
    ...base,
    model: base.model || `model_${idx + 1}`,
    bias,
    confidence,
    impact,
    action,
    risk,
    symbols,
    reasoning,
    bullish_score: bullishScore,
    bearish_score: bearishScore,
  };
}

function computeBiasConsensus(valid) {
  const weighted = { bullish: 0, bearish: 0, neutral: 0 };

  for (const r of valid) {
    const c = Number(r.confidence) || 0;
    const bullScore = clamp(Number(r.bullish_score) || 0, 0, 100);
    const bearScore = clamp(Number(r.bearish_score) || 0, 0, 100);
    const actionHint = ACTION_BIAS_HINT[r.action] || 'neutral';
    const actionWeight = r.action === 'hold' ? c * 0.15 : c * 0.25;

    weighted[r.bias] += c;
    weighted.bullish += bullScore * 0.4;
    weighted.bearish += bearScore * 0.4;
    weighted[actionHint] += actionWeight;
  }

  const directionalTotal = weighted.bullish + weighted.bearish;
  const directionalGapPct = directionalTotal > 0
    ? (Math.abs(weighted.bullish - weighted.bearish) / directionalTotal) * 100
    : 0;

  const neutralVotes = valid.filter(r => r.bias === 'neutral').length;
  const holdVotes = valid.filter(r => r.action === 'hold').length;
  const neutralRatio = neutralVotes / valid.length;
  const holdRatio = holdVotes / valid.length;

  let winnerBias;
  if (directionalTotal === 0) {
    winnerBias = 'neutral';
  } else if (neutralRatio >= 0.67 && holdRatio >= 0.67 && directionalGapPct < 15) {
    winnerBias = 'neutral';
  } else {
    winnerBias = weighted.bullish >= weighted.bearish ? 'bullish' : 'bearish';
  }

  const totalWeight = weighted.bullish + weighted.bearish + weighted.neutral;
  const biasAgreement = totalWeight > 0 ? (weighted[winnerBias] / totalWeight) * 100 : 0;

  return {
    winnerBias,
    biasAgreement,
    weighted,
    neutralRatio,
    holdRatio,
    directionalGapPct,
  };
}

function computeConsensus(modelResponses) {
  const normalized = (modelResponses || []).map((r, i) => sanitizeModelResponse(r, i));
  const valid = normalized.filter(r =>
    r && r.bias && r.confidence != null && r.action && r.risk
  );

  if (valid.length === 0) {
    return { error: 'No valid model responses' };
  }

  // Single model: skip consensus, use directly
  if (valid.length === 1) {
    const r = valid[0];
    const score = r.confidence;
    return {
      ai_consensus_score: score,
      analysis_status: score >= 60 ? '\u2705 Analyzed' : '\u26A0\uFE0F Conflict',
      consensus_bias: BIAS_EMOJI[r.bias] || r.bias,
      consensus_impact: IMPACT_EMOJI[r.impact] || r.impact,
      recommended_action: ACTION_EMOJI[r.action] || r.action,
      risk_level: RISK_EMOJI[r.risk] || r.risk,
      consensus_symbols: r.symbols || [],
      consensus_confidence: r.confidence,
      bias_agreement: 100,
      model_count: 1,
      per_model_results: valid,
      consensus_reasoning: `Single model (${r.model || 'unknown'}): ${r.reasoning || ''}`,
    };
  }

  // Step 1: Bias consensus
  const biasConsensus = computeBiasConsensus(valid);
  const winnerBias = biasConsensus.winnerBias;
  const biasAgreement = biasConsensus.biasAgreement;

  // Step 2: Confidence
  const avgConfidence = valid.reduce((s, r) => s + r.confidence, 0) / valid.length;
  const finalConfidence = Math.round(avgConfidence * (biasAgreement / 100));

  // Step 3: Impact consensus
  const avgImpact = valid.reduce((s, r) => s + (IMPACT_MAP[r.impact] || 2), 0) / valid.length;
  const consensusImpact = avgImpact >= 2.5 ? 'high' : avgImpact >= 1.5 ? 'medium' : 'low';

  // Step 4: Action consensus (confidence-weighted)
  const totalConf = valid.reduce((s, r) => s + r.confidence, 0);
  const weightedAction = valid.reduce((s, r) => s + (ACTION_MAP[r.action] || 3) * r.confidence, 0) / totalConf;
  let consensusAction;
  if (weightedAction >= 4.5) consensusAction = 'strong_buy';
  else if (weightedAction >= 3.5) consensusAction = 'buy';
  else if (weightedAction >= 2.5) consensusAction = 'hold';
  else if (weightedAction >= 1.5) consensusAction = 'sell';
  else consensusAction = 'strong_sell';

  // Step 5: Risk (conservative max)
  const riskValues = valid.map(r => RISK_MAP[r.risk] || 2);
  const maxRisk = Math.max(...riskValues);
  const consensusRisk = maxRisk >= 3 ? 'high' : maxRisk >= 2 ? 'medium' : 'low';

  // Step 6: Symbol consensus
  const symbolFreq = {};
  valid.forEach(r => (r.symbols || []).forEach(s => { symbolFreq[s] = (symbolFreq[s] || 0) + 1; }));
  const threshold = valid.length * 0.5;
  let consensusSymbols = Object.entries(symbolFreq)
    .filter(([, count]) => count >= threshold)
    .map(([sym]) => sym);
  if (consensusSymbols.length === 0) {
    consensusSymbols = Object.entries(symbolFreq)
      .sort((a, b) => b[1] - a[1])
      .slice(0, 3)
      .map(([sym]) => sym);
  }
  consensusSymbols = consensusSymbols.slice(0, 3);

  // Step 7: AI Consensus Score
  const actionAgreement = (valid.filter(r => r.action === consensusAction).length / valid.length) * 100;
  const aiConsensusScore = Math.round(biasAgreement * 0.4 + finalConfidence * 0.4 + actionAgreement * 0.2);

  // Step 8: Analysis Status
  let analysisStatus;
  if (aiConsensusScore >= 60) analysisStatus = '\u2705 Analyzed';
  else if (aiConsensusScore >= 40) analysisStatus = '\u26A0\uFE0F Conflict';
  else analysisStatus = '\u26A0\uFE0F Conflict';

  return {
    ai_consensus_score: aiConsensusScore,
    analysis_status: analysisStatus,
    consensus_bias: BIAS_EMOJI[winnerBias] || winnerBias,
    consensus_impact: IMPACT_EMOJI[consensusImpact] || consensusImpact,
    recommended_action: ACTION_EMOJI[consensusAction] || consensusAction,
    risk_level: RISK_EMOJI[consensusRisk] || consensusRisk,
    consensus_symbols: consensusSymbols,
    consensus_confidence: finalConfidence,
    bias_agreement: Math.round(biasAgreement),
    model_count: valid.length,
    per_model_results: valid,
    consensus_reasoning: `${valid.length} models. Bias winner=${winnerBias} (agreement ${Math.round(biasAgreement)}%, directional gap ${biasConsensus.directionalGapPct.toFixed(1)}%, neutral ratio ${(biasConsensus.neutralRatio * 100).toFixed(0)}%, weighted bull ${biasConsensus.weighted.bullish.toFixed(1)}, weighted bear ${biasConsensus.weighted.bearish.toFixed(1)}). Action=${consensusAction} (weighted ${weightedAction.toFixed(1)}). Risk=${consensusRisk}.`,
  };
}

// ── Format per-model results for Notion ─────────────────────────────

function formatPerModelText(consensus) {
  const date = new Date().toISOString().split('T')[0];
  let text = `=== AI Consensus Analysis ===\nScore: ${consensus.ai_consensus_score}/100 | Models: ${consensus.model_count} | Date: ${date}\n\n`;

  for (const r of consensus.per_model_results) {
    const model = r.model || 'Unknown';
    const bullScore = Number.isFinite(Number(r.bullish_score)) ? Number(r.bullish_score) : null;
    const bearScore = Number.isFinite(Number(r.bearish_score)) ? Number(r.bearish_score) : null;
    const scorePart = bullScore != null && bearScore != null
      ? ` | Bull:${Math.round(bullScore)} Bear:${Math.round(bearScore)}`
      : '';
    text += `[${model}] Bias: ${r.bias} | Conf: ${r.confidence}% | Action: ${r.action} | Risk: ${r.risk}${scorePart}\n`;
    if (r.reasoning) {
      text += `-> ${r.reasoning.slice(0, 200)}\n`;
    }
    text += '\n';
  }

  text += `Consensus: ${consensus.consensus_bias} | Action: ${consensus.recommended_action} | Risk: ${consensus.risk_level}\n`;
  if (consensus.consensus_symbols?.length) {
    text += `Symbols: ${consensus.consensus_symbols.join(', ')}\n`;
  }

  return text.slice(0, 2000);
}

function pickAnalysisCandidates(signals) {
  return (signals || []).filter(signal => {
    const status = String(signal.status || '').toLowerCase();
    return !status || /new|watching|moi/i.test(status);
  });
}

function buildLeanFieldUpdate(consensus) {
  const updates = {
    bias: consensus.consensus_bias,
    impact: consensus.consensus_impact,
    recommendedAction: consensus.recommended_action,
  };

  if (Array.isArray(consensus.consensus_symbols) && consensus.consensus_symbols.length > 0) {
    updates.symbols = consensus.consensus_symbols;
  }

  return updates;
}

// ── Build analysis prompt for a signal ──────────────────────────────

function buildPrompt(signal) {
  return `You are a professional financial analyst specializing in forex, commodities, and indices trading. Analyze the following news signal and provide a structured trading assessment.

Your goal is objective directional assessment for short-term trading (roughly 30-180 minutes after news), not generic commentary.

## News Signal
- **Event**: ${signal.event}
- **Current Bias**: ${signal.bias}
- **Current Impact**: ${signal.impact}
- **Symbols**: ${(signal.symbols || []).join(', ')}
- **Time (ET)**: ${signal.time || 'N/A'}
- **Source URL**: ${signal.url || 'N/A'}
- **Details**: ${signal.details || 'N/A'}

## Analysis Requirements
1. Assess whether the current bias (bullish/bearish/neutral) is correct
2. Evaluate the true market impact level
3. Determine the optimal trading action
4. Identify the risk level for trading this signal
5. Confirm or suggest better symbol assignments (max 3, ONLY from the allowed MT5 symbols list)
6. Provide clear reasoning for your assessment
7. Score BOTH directional cases (bullish_score and bearish_score from 0-100)

## Neutral Guardrail
- Avoid neutral by default.
- Use "neutral" only if BOTH conditions are true:
  1) Directional score gap is small (|bullish_score - bearish_score| <= 10)
  2) Action is "hold"
- If one directional score clearly exceeds the other, choose that direction.
- For headlines with clear directional verbs (spike/surge/jump/rally or plunge/slump/drop/selloff), do not output neutral unless evidence is truly mixed.
- Confidence should reflect evidence quality, not certainty style:
  - strong evidence: 70-90
  - mixed evidence: 50-69
  - weak evidence: 30-49

## Allowed MT5 Symbols
Forex: EURUSD, GBPUSD, USDJPY, AUDUSD, NZDUSD, USDCAD, USDCHF, GBPJPY, EURJPY, EURGBP, AUDJPY, EURAUD
Indices: US500, US30, NAS100, DE40, UK100, JP225, FR40
Commodities: XAUUSD, XAGUSD, USOIL, UKOIL, NGAS
Crypto: BTCUSD, ETHUSD

## Response Format
Respond with ONLY a valid JSON object, no markdown, no explanation outside JSON:

{
  "bias": "bullish|bearish|neutral",
  "confidence": 0-100,
  "bullish_score": 0-100,
  "bearish_score": 0-100,
  "impact": "high|medium|low",
  "action": "strong_buy|buy|hold|sell|strong_sell",
  "risk": "low|medium|high",
  "symbols": ["SYMBOL1", "SYMBOL2"],
  "reasoning": "Your detailed analysis here (max 500 chars)",
  "neutral_reason": "Required only when bias is neutral, otherwise N/A",
  "key_levels": {
    "entry_zone": "description or N/A",
    "stop_loss": "description or N/A",
    "take_profit": "description or N/A"
  },
  "time_horizon": "intraday|swing|position",
  "catalysts": ["catalyst1", "catalyst2"]
}`;
}

// ── Ollama integration ──────────────────────────────────────────────

async function analyzeWithOllama(signal, model = 'llama3.2') {
  const baseUrl = process.env.OLLAMA_BASE_URL || 'http://localhost:11434';
  const prompt = buildPrompt(signal);

  const res = await fetch(`${baseUrl}/api/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      model,
      messages: [{ role: 'user', content: prompt }],
      stream: false,
      format: 'json',
    }),
  });

  if (!res.ok) {
    throw new Error(`Ollama error: ${res.status} ${res.statusText}`);
  }

  const data = await res.json();
  const content = data.message?.content;
  if (!content) throw new Error('Empty response from Ollama');

  const parsed = JSON.parse(content);
  parsed.model = `${model} (ollama)`;
  return parsed;
}

// ── Commands ────────────────────────────────────────────────────────

async function cmdPending() {
  const signals = await getSignals({ pageSize: 200 });
  const pending = pickAnalysisCandidates(signals);
  console.log(JSON.stringify(pending, null, 2));
}

async function cmdAnalyze(pageId) {
  const signals = await getSignals({ pageSize: 100 });
  const signal = signals.find(s => s.id === pageId);
  if (!signal) {
    console.error(`Signal not found: ${pageId}`);
    process.exit(1);
  }
  console.log('=== Signal ===');
  console.log(JSON.stringify(signal, null, 2));
  console.log('\n=== Analysis Prompt ===');
  console.log(buildPrompt(signal));
}

async function cmdUpdate(pageId, analysisJson) {
  const modelResponses = Array.isArray(analysisJson) ? analysisJson : [analysisJson];
  const consensus = computeConsensus(modelResponses);

  if (consensus.error) {
    console.error('Consensus error:', consensus.error);
    process.exit(1);
  }

  const appliedUpdate = buildLeanFieldUpdate(consensus);
  await updateSignalFields(pageId, appliedUpdate);

  console.log(JSON.stringify({
    pageId,
    ...consensus,
    applied_update: appliedUpdate,
    updated: true,
  }, null, 2));
}

async function cmdBatchUpdate(batchJson) {
  // Expected: [{ pageId, modelResponses: [...] }, ...]
  const results = [];
  for (const item of batchJson) {
    try {
      const modelResponses = item.modelResponses || [item];
      const consensus = computeConsensus(modelResponses);

      if (consensus.error) {
        results.push({ pageId: item.pageId, error: consensus.error });
        continue;
      }

      const appliedUpdate = buildLeanFieldUpdate(consensus);
      await updateSignalFields(item.pageId, appliedUpdate);

      results.push({
        pageId: item.pageId,
        ai_consensus_score: consensus.ai_consensus_score,
        analysis_status: consensus.analysis_status,
        recommended_action: consensus.recommended_action,
        risk_level: consensus.risk_level,
        applied_update: appliedUpdate,
        updated: true,
      });
    } catch (err) {
      results.push({ pageId: item.pageId, error: err.message });
    }
  }
  console.log(JSON.stringify(results, null, 2));
}

async function cmdOllama(pageId) {
  const models = (process.env.OLLAMA_MODELS || 'llama3.2').split(',').map(m => m.trim());
  const signals = await getSignals({ pageSize: 100 });
  const signal = signals.find(s => s.id === pageId);

  if (!signal) {
    console.error(`Signal not found: ${pageId}`);
    process.exit(1);
  }

  const responses = [];
  for (const model of models) {
    try {
      console.error(`Analyzing with ${model}...`);
      const result = await analyzeWithOllama(signal, model);
      responses.push(result);
    } catch (err) {
      console.error(`${model} failed: ${err.message}`);
    }
  }

  if (responses.length === 0) {
    console.error('All models failed');
    process.exit(1);
  }

  await cmdUpdate(pageId, responses);
}

async function cmdStatus() {
  const signals = await getSignals({ pageSize: 100 });
  const stats = {
    total: signals.length,
    new: signals.filter(s => /new/i.test(s.status)).length,
    watching: signals.filter(s => /watching/i.test(s.status)).length,
    traded: signals.filter(s => /traded/i.test(s.status)).length,
    skipped: signals.filter(s => /skipped/i.test(s.status)).length,
    candidates: pickAnalysisCandidates(signals).length,
    byBias: {},
    byImpact: {},
  };

  signals.forEach(s => {
    if (s.bias) stats.byBias[s.bias] = (stats.byBias[s.bias] || 0) + 1;
    if (s.impact) stats.byImpact[s.impact] = (stats.byImpact[s.impact] || 0) + 1;
  });

  console.log(JSON.stringify(stats, null, 2));
}

// ── Read stdin helper ───────────────────────────────────────────────

function readStdin() {
  return new Promise((resolve, reject) => {
    let data = '';
    process.stdin.setEncoding('utf8');
    process.stdin.on('data', chunk => { data += chunk; });
    process.stdin.on('end', () => {
      try {
        resolve(JSON.parse(data));
      } catch (e) {
        reject(new Error(`Invalid JSON on stdin: ${e.message}`));
      }
    });
    process.stdin.on('error', reject);
    // Timeout after 5s if no data
    setTimeout(() => {
      if (!data) reject(new Error('No data on stdin (timeout)'));
    }, 5000);
  });
}

// ── Main ────────────────────────────────────────────────────────────

const args = process.argv.slice(2);
const command = args[0];

(async () => {
  switch (command) {
    case '--pending':
      await cmdPending();
      break;

    case '--analyze':
      if (!args[1]) { console.error('Usage: --analyze <pageId>'); process.exit(1); }
      await cmdAnalyze(args[1]);
      break;

    case '--update':
      if (!args[1]) { console.error('Usage: --update <pageId> (pipe JSON via stdin)'); process.exit(1); }
      const updateData = await readStdin();
      await cmdUpdate(args[1], updateData);
      break;

    case '--batch-update':
      const batchData = await readStdin();
      await cmdBatchUpdate(batchData);
      break;

    case '--ollama':
      if (!args[1]) { console.error('Usage: --ollama <pageId>'); process.exit(1); }
      await cmdOllama(args[1]);
      break;

    case '--status':
      await cmdStatus();
      break;

    default:
      console.log(`Directional AI analysis for lean-schema signals

Usage:
  node analyze-signals.js --pending          List active signals for analysis
  node analyze-signals.js --analyze <id>     Output analysis prompt for a signal
  node analyze-signals.js --update <id>      Apply lean-field update from stdin JSON
  node analyze-signals.js --batch-update     Apply multiple lean-field updates
  node analyze-signals.js --ollama <id>      Auto-analyze with Ollama models
  node analyze-signals.js --status           Show signal inventory summary`);
  }
})().catch(err => {
  console.error(JSON.stringify({ error: err.message }));
  process.exit(1);
});

// Export for programmatic use
module.exports = {
  computeConsensus,
  formatPerModelText,
  buildPrompt,
  analyzeWithOllama,
  pickAnalysisCandidates,
  buildLeanFieldUpdate,
};
