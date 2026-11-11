#!/usr/bin/env node

const fs = require('fs');
const path = require('path');

const defaultLogPath = path.resolve(process.cwd(), 'tmp/clear-reminders.log');
const inputPath = process.argv[2] ? path.resolve(process.argv[2]) : defaultLogPath;

function parseLastJsonBlock(text) {
  const start = text.lastIndexOf('\n{');
  const jsonText = (start >= 0 ? text.slice(start + 1) : text.trim()).trim();
  return JSON.parse(jsonText);
}

if (!fs.existsSync(inputPath)) {
  console.error(JSON.stringify({ success: false, error: `Log file not found: ${inputPath}` }, null, 2));
  process.exit(1);
}

const raw = fs.readFileSync(inputPath, 'utf8');
const result = parseLastJsonBlock(raw);
const failedItems = (result.results || []).filter(item => item.status === 'failed');
const clearedItems = (result.results || []).filter(item => item.status === 'cleared');

console.log(JSON.stringify({
  logPath: inputPath,
  success: Boolean(result.success),
  checked: result.checked || 0,
  cleared: result.cleared || 0,
  skippedNoAlarm: result.skippedNoAlarm || 0,
  failed: result.failed || 0,
  failedItems,
  recentCleared: clearedItems.slice(-5),
}, null, 2));
