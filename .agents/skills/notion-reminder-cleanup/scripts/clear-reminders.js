#!/usr/bin/env node

const { spawnSync } = require('child_process');
const path = require('path');

const scriptPath = path.resolve(__dirname, '../../../../scripts/clear-past-reminders.js');
const result = spawnSync(process.execPath, [scriptPath, ...process.argv.slice(2)], {
  stdio: 'inherit',
  cwd: process.cwd(),
});

if (result.error) {
  throw result.error;
}

process.exit(result.status ?? 1);
