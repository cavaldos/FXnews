#!/usr/bin/env node
/**
 * Cloud directional analysis via Groq for lean-schema signals.
 *
 * This script analyzes active signals stored in Notion using Groq-hosted
 * LLMs, then writes back only lean fields like Bias / Impact / Symbol.
 *
 * Usage:
 *   node analyze-signals-groq.js --pending
 *   node analyze-signals-groq.js --new --limit 10
 *   node analyze-signals-groq.js --all --limit 20
 *   node analyze-signals-groq.js --id <pageId>
 *
 * Env:
 *   GROQ_API_KEY (required)
 *   GROQ_MODELS (optional, comma-separated)
 */

require('dotenv').config({
  path: require('path').join(__dirname, '..', '..', '..', '..', '.env'),
  quiet: true,
});

const Groq = require('groq-sdk');
const {
  getSignals,
  updateSignalFields,
} = require('../../../../src/news-trade-db');

const {
  buildPrompt,
  computeConsensus,
  pickAnalysisCandidates,
  buildLeanFieldUpdate,
} = require('./analyze-signals');

const DEFAULT_MODELS = [
  'llama-3.3-70b-versatile',
  'qwen/qwen3-32b',
  'meta-llama/llama-4-maverick-17b-128e-instruct',
];

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function extractJsonObject(text) {
  if (!text) throw new Error('Empty model response');
  const trimmed = String(text).trim();

  // Fast path: pure JSON
  try {
    return JSON.parse(trimmed);
  } catch (_err) {
    // Continue
  }

  // Fallback: attempt to extract first {...} block
  const start = trimmed.indexOf('{');
  const end = trimmed.lastIndexOf('}');
  if (start === -1 || end === -1 || end <= start) {
    throw new Error('No JSON object found in response');
  }
  const slice = trimmed.slice(start, end + 1);
  return JSON.parse(slice);
}

function getModels() {
  const raw = process.env.GROQ_MODELS;
  if (!raw) return DEFAULT_MODELS;
  const parsed = raw
    .split(',')
    .map(s => s.trim())
    .filter(Boolean);
  return parsed.length ? parsed : DEFAULT_MODELS;
}

async function groqAnalyze({ prompt, model }) {
  const apiKey = process.env.GROQ_API_KEY;
  if (!apiKey) {
    throw new Error('Missing GROQ_API_KEY in environment');
  }

  const groq = new Groq({ apiKey });

  const response = await groq.chat.completions.create({
    model,
    messages: [
      {
        role: 'user',
        content: prompt,
      },
    ],
    temperature: 0.2,
  });

  const content = response?.choices?.[0]?.message?.content;
  const parsed = extractJsonObject(content);
  parsed.model = `${model} (groq)`;
  return parsed;
}

async function analyzeAndUpdate(signal, models) {
  const prompt = buildPrompt(signal);
  const modelResponses = [];
  const failures = [];

  for (const model of models) {
    try {
      // Stagger slightly to reduce rate limit bursts
      await sleep(250);
      const res = await groqAnalyze({ prompt, model });
      modelResponses.push(res);
    } catch (err) {
      failures.push({ model, error: err.message });
    }
  }

  if (modelResponses.length === 0) {
    throw new Error(`All Groq models failed (${failures.map(f => f.model).join(', ')})`);
  }

  const consensus = computeConsensus(modelResponses);
  if (consensus.error) {
    throw new Error(consensus.error);
  }

  const appliedUpdate = buildLeanFieldUpdate(consensus);
  await updateSignalFields(signal.id, appliedUpdate);

  return {
    pageId: signal.id,
    event: signal.event,
    modelCount: consensus.model_count,
    aiConsensusScore: consensus.ai_consensus_score,
    analysisStatus: consensus.analysis_status,
    recommendedAction: consensus.recommended_action,
    riskLevel: consensus.risk_level,
    consensusSymbols: consensus.consensus_symbols,
    appliedUpdate,
    failures,
  };
}

function hasFlag(flag) {
  return process.argv.includes(flag);
}

function getArg(flag) {
  const idx = process.argv.indexOf(flag);
  if (idx === -1) return null;
  return process.argv[idx + 1] || null;
}

async function main() {
  const id = getArg('--id');
  const limitRaw = getArg('--limit');
  const limit = limitRaw ? Math.max(1, Math.min(50, Number(limitRaw) || 0)) : null;

  const models = getModels();

  if (hasFlag('--pending')) {
    const signals = await getSignals({ pageSize: 100 });
    const pending = pickAnalysisCandidates(signals);
    console.log(JSON.stringify({ pending: pending.length, models }, null, 2));
    return;
  }

  let targets = [];

  if (id) {
    const signals = await getSignals({ pageSize: 100 });
    const found = signals.find(s => s.id === id);
    if (!found) {
      throw new Error(`Signal not found: ${id}`);
    }
    targets = [found];
  } else if (hasFlag('--all')) {
    targets = await getSignals({ pageSize: 100 });
  } else {
    targets = pickAnalysisCandidates(await getSignals({ pageSize: 100 }));
  }

  if (limit) {
    targets = targets.slice(0, limit);
  }

  if (targets.length === 0) {
    console.log(JSON.stringify({ ok: true, analyzed: 0, models }, null, 2));
    return;
  }

  const results = [];
  for (const signal of targets) {
    try {
      // Slight delay between signals
      await sleep(400);
      const r = await analyzeAndUpdate(signal, models);
      results.push({ ...r, updated: true });
    } catch (err) {
      results.push({ pageId: signal.id, event: signal.event, error: err.message, updated: false });
    }
  }

  console.log(JSON.stringify({
    ok: true,
    analyzed: results.filter(r => r.updated).length,
    failed: results.filter(r => !r.updated).length,
    models,
    results,
  }, null, 2));
}

main().catch(err => {
  console.error(JSON.stringify({ error: err.message }, null, 2));
  process.exit(1);
});
