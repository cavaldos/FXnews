#!/usr/bin/env node
require('dotenv').config({ path: require('path').join(__dirname, '..', '.env'), quiet: true });

const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');
const { chromium } = require('playwright');

const { getSignals, getResolvedDatabaseId } = require('../src/news-trade-db');

const DEFAULT_PROFILE_DIR = path.join(process.cwd(), 'tmp', 'notion-playwright-profile');
const DEFAULT_SCREENSHOT_DIR = path.join(process.cwd(), 'tmp', 'notion-reminder-cleaner');
const DEFAULT_COOKIE_FILE = path.join(process.cwd(), 'tmp', 'notion-brave-cookies.json');
const DEFAULT_PYTHON_VENV = path.join(process.cwd(), 'tmp', 'pyvenv');
const COOKIE_EXPORT_SCRIPT = path.join(process.cwd(), 'scripts', 'export-brave-notion-cookies.py');

function hasFlag(flag) {
  return process.argv.includes(flag);
}

function getArgValue(flag, fallback = null) {
  const idx = process.argv.indexOf(flag);
  if (idx === -1 || idx + 1 >= process.argv.length) return fallback;
  return process.argv[idx + 1];
}

function pathExists(targetPath) {
  return Boolean(targetPath) && fs.existsSync(targetPath);
}

function toInt(value, fallback) {
  const parsed = parseInt(value, 10);
  return Number.isNaN(parsed) ? fallback : parsed;
}

function stripDashes(id) {
  return String(id || '').replace(/-/g, '');
}

function toNotionUrl(id) {
  return `https://www.notion.so/${stripDashes(id)}`;
}

function getWorkspacePrefix(databaseUrl) {
  try {
    const parsed = new URL(databaseUrl);
    const parts = parsed.pathname.split('/').filter(Boolean);
    const first = parts[0] || '';

    if (first && !/^[a-f0-9]{32}$/i.test(first)) {
      return `${parsed.origin}/${first}`;
    }
  } catch (_error) {
    return null;
  }

  return null;
}

async function getDatabaseUrl() {
  if (process.env.NOTION_DATABASE_URL) {
    return process.env.NOTION_DATABASE_URL.trim();
  }

  const databaseId = await getResolvedDatabaseId();
  return toNotionUrl(databaseId);
}

function getSignalPageUrl(signal, databaseUrl = null) {
  const workspacePrefix = databaseUrl ? getWorkspacePrefix(databaseUrl) : null;
  if (workspacePrefix) {
    return `${workspacePrefix}/${stripDashes(signal.id)}`;
  }

  return toNotionUrl(signal.id);
}

function getSignalTimeMs(signal) {
  const ms = new Date(signal.time || '').getTime();
  return Number.isFinite(ms) ? ms : NaN;
}

function getExpiredSignals(signals, nowMs, targetPageId = null) {
  return (signals || [])
    .filter(signal => !targetPageId || signal.id === targetPageId)
    .filter(signal => {
      const timeMs = getSignalTimeMs(signal);
      return Number.isFinite(timeMs) && timeMs < nowMs;
    })
    .sort((a, b) => getSignalTimeMs(a) - getSignalTimeMs(b));
}

function isLikelyLoginUrl(url) {
  return /\/login|oauth|auth|signin/i.test(String(url || ''));
}

async function getPrimaryPage(context) {
  return context.pages()[0] || context.newPage();
}

async function launchContext(options = {}) {
  fs.mkdirSync(options.profileDir, { recursive: true });

  return chromium.launchPersistentContext(options.profileDir, {
    headless: options.headless,
    viewport: { width: 1440, height: 960 },
    colorScheme: 'dark',
  });
}

async function waitForNotionReady(page) {
  await page.waitForLoadState('domcontentloaded', { timeout: 60000 });
  await page.waitForTimeout(2500);
  await page.waitForLoadState('networkidle', { timeout: 10000 }).catch(() => {});
}

async function pageLooksLoggedOut(page) {
  if (isLikelyLoginUrl(page.url())) {
    return true;
  }

  const loginLocators = [
    page.getByRole('button', { name: /Continue with Google/i }),
    page.getByRole('button', { name: /Continue with email/i }),
    page.getByRole('button', { name: /Continue with Apple/i }),
    page.getByPlaceholder(/Enter your email address/i),
    page.locator('input[type="email"]'),
  ];

  for (const locator of loginLocators) {
    if (await locator.first().isVisible().catch(() => false)) {
      return true;
    }
  }

  return false;
}

async function pageLooksPublicReadOnly(page) {
  const publicMarkers = [
    page.getByRole('button', { name: /Get Notion free/i }),
    page.getByText(/Get Notion free/i),
  ];

  for (const locator of publicMarkers) {
    if (await locator.first().isVisible().catch(() => false)) {
      return true;
    }
  }

  return false;
}

function normalizeCookie(cookie) {
  return {
    name: cookie.name,
    value: cookie.value,
    domain: cookie.domain,
    path: cookie.path || '/',
    secure: Boolean(cookie.secure),
    httpOnly: Boolean(cookie.httpOnly),
    expires: typeof cookie.expires === 'number' ? cookie.expires : -1,
    sameSite: cookie.sameSite || 'Lax',
  };
}

function loadCookiesFromFile(cookieFile) {
  if (!cookieFile || !fs.existsSync(cookieFile)) {
    return [];
  }

  return JSON.parse(fs.readFileSync(cookieFile, 'utf8')).map(normalizeCookie);
}

function runCommand(command, args, errorPrefix) {
  try {
    return execFileSync(command, args, {
      cwd: process.cwd(),
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'pipe'],
    }).trim();
  } catch (error) {
    const stderr = error.stderr ? String(error.stderr).trim() : '';
    const stdout = error.stdout ? String(error.stdout).trim() : '';
    const details = stderr || stdout || error.message;
    throw new Error(`${errorPrefix}: ${details}`);
  }
}

function getVenvPythonPath() {
  return path.join(DEFAULT_PYTHON_VENV, 'bin', 'python');
}

function getVenvPipPath() {
  return path.join(DEFAULT_PYTHON_VENV, 'bin', 'pip');
}

function canImportBrowserCookie3(pythonPath) {
  const isCommandName = !String(pythonPath || '').includes(path.sep);
  if (!isCommandName && !pathExists(pythonPath)) {
    return false;
  }

  try {
    runCommand(pythonPath, ['-c', 'import browser_cookie3'], 'Python check failed');
    return true;
  } catch (_error) {
    return false;
  }
}

function ensureCookieExportPython() {
  const venvPython = getVenvPythonPath();
  if (canImportBrowserCookie3(venvPython)) {
    return venvPython;
  }

  if (canImportBrowserCookie3('python3')) {
    return 'python3';
  }

  runCommand('python3', ['-m', 'venv', DEFAULT_PYTHON_VENV], 'Failed to create Python virtualenv');
  runCommand(getVenvPipPath(), ['install', 'browser_cookie3'], 'Failed to install browser_cookie3');

  if (!canImportBrowserCookie3(venvPython)) {
    throw new Error('Cookie export environment is still unavailable after setup.');
  }

  return venvPython;
}

function exportBraveCookies(cookieFile) {
  if (!pathExists(COOKIE_EXPORT_SCRIPT)) {
    throw new Error(`Cookie export script is missing: ${COOKIE_EXPORT_SCRIPT}`);
  }

  const pythonPath = ensureCookieExportPython();
  runCommand(pythonPath, [COOKIE_EXPORT_SCRIPT, cookieFile], 'Failed to export Brave Notion cookies');
  return cookieFile;
}

async function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function getAnyUsefulPage(context) {
  const pages = context.pages();
  return pages[pages.length - 1] || context.newPage();
}

async function runSetupLogin(options) {
  const context = await launchContext({
    profileDir: options.profileDir,
    headless: false,
  });

  try {
    const page = await getPrimaryPage(context);
    await page.goto(options.databaseUrl, { waitUntil: 'domcontentloaded', timeout: 60000 });

    console.log('Opened Notion in a persistent Playwright profile.');
    console.log(`Login profile: ${options.profileDir}`);
    console.log(`Waiting up to ${options.setupTimeoutMinutes} minute(s) for you to finish login in the browser window...`);

    const deadline = Date.now() + options.setupTimeoutMinutes * 60 * 1000;

    while (Date.now() < deadline) {
      const activePage = await getAnyUsefulPage(context);
      await waitForNotionReady(activePage).catch(() => {});

      if (!(await pageLooksLoggedOut(activePage))) {
        await activePage.goto(options.databaseUrl, { waitUntil: 'domcontentloaded', timeout: 60000 }).catch(() => {});
        await waitForNotionReady(activePage).catch(() => {});

        const editable = await canOpenTimeEditorForSignal(activePage, options.sampleSignal).catch(() => false);
        if (!(await pageLooksLoggedOut(activePage)) && !await pageLooksPublicReadOnly(activePage) && editable) {
          console.log(JSON.stringify({ success: true, setupLogin: true, profileDir: options.profileDir }, null, 2));
          return;
        }
      }

      await sleep(5000);
    }

    throw new Error(`Login was not detected within ${options.setupTimeoutMinutes} minute(s). Run again with --setup-login after opening the browser window.`);
  } finally {
    await context.close();
  }
}

async function countVisible(locator, limit = 5) {
  const count = await locator.count().catch(() => 0);
  let visible = 0;

  for (let i = 0; i < Math.min(count, limit); i += 1) {
    const item = locator.nth(i);
    if (await item.isVisible().catch(() => false)) {
      visible += 1;
    }
  }

  return visible;
}

async function clickFirstVisible(locators, options = {}) {
  for (const locator of locators) {
    const count = await locator.count().catch(() => 0);
    for (let i = 0; i < Math.min(count, options.limit || 6); i += 1) {
      const item = locator.nth(i);
      if (await item.isVisible().catch(() => false)) {
        await item.click({ force: true, timeout: options.timeout || 5000 }).catch(() => null);
        return true;
      }
    }
  }

  return false;
}

async function getTimeRow(page) {
  const row = page.getByRole('row').filter({ hasText: 'Time (ET)' }).first();
  if (await row.count().catch(() => 0)) {
    return row;
  }

  return page.locator('[role="row"]').filter({ hasText: 'Time (ET)' }).first();
}

async function hasAlarmIcon(row) {
  const alarmLocator = row.locator('svg.alarmSmall, .alarmSmall');
  return (await countVisible(alarmLocator, 3)) > 0;
}

function getDateEditorDialog(page) {
  return page.locator('[role="dialog"]').filter({ hasText: 'Remind' }).filter({ hasText: 'Time zone' }).last();
}

async function loadSignalPage(page, signal, options = {}) {
  const pageUrl = getSignalPageUrl(signal, options.databaseUrl || null);
  await page.goto(pageUrl, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await waitForNotionReady(page);
  return pageUrl;
}

async function openTimeEditor(page, row) {
  const valueCell = row.locator('[role="cell"]').nth(1);
  await valueCell.click({ force: true, timeout: 5000 }).catch(() => null);
  await page.waitForTimeout(400);

  const dialogs = page.locator('[role="dialog"]');
  if (await countVisible(dialogs, 3)) {
    return true;
  }

  await valueCell.dblclick({ force: true, timeout: 5000 }).catch(() => null);
  await page.waitForTimeout(400);

  if (await countVisible(dialogs, 3)) {
    return true;
  }

  await valueCell.focus().catch(() => null);
  await page.keyboard.press('Space').catch(() => null);
  await page.waitForTimeout(400);

  if (await countVisible(dialogs, 3)) {
    return true;
  }

  await page.keyboard.press('Enter').catch(() => null);
  await page.waitForTimeout(400);

  return (await countVisible(dialogs, 3)) > 0;
}

async function canOpenTimeEditorForSignal(page, signal, options = {}) {
  if (!signal) {
    return false;
  }

  await loadSignalPage(page, signal, options);

  const row = await getTimeRow(page);
  const visible = await row.isVisible().catch(() => false);
  if (!visible) {
    return false;
  }

  const opened = await openTimeEditor(page, row);
  if (opened) {
    await closeOpenPopups(page);
  }

  return opened;
}

function getReminderValueLocators(scope) {
  return [
    scope.getByRole('button', { name: /At time of event|minute before|minutes before|hour before|hours before|day before|days before|week before|None/i }),
    scope.getByText(/At time of event|minute before|minutes before|hour before|hours before|day before|days before|week before|None/i),
    scope.locator('[aria-label*="remind" i]'),
  ];
}

async function setReminderToNone(page) {
  const dateDialog = getDateEditorDialog(page);
  const scope = (await dateDialog.isVisible().catch(() => false)) ? dateDialog : page;

  const noneLocators = [
    scope.getByRole('option', { name: /^None$/ }),
    scope.getByRole('menuitem', { name: /^None$/ }),
    scope.getByRole('button', { name: /^None$/ }),
    scope.getByText(/^None$/),
  ];

  if (await clickFirstVisible(noneLocators)) {
    return true;
  }

  const remindLocators = [
    scope.getByRole('button', { name: /^Remind$/i }),
    scope.getByText(/^Remind$/i),
    scope.getByText(/^At time of event$/),
    ...getReminderValueLocators(scope),
  ];

  if (!(await clickFirstVisible(remindLocators))) {
    return false;
  }

  await page.waitForTimeout(300);

  const menuScope = page;
  return clickFirstVisible([
    menuScope.getByRole('option', { name: /^None$/ }),
    menuScope.getByRole('menuitem', { name: /^None$/ }),
    menuScope.getByRole('button', { name: /^None$/ }),
    menuScope.getByText(/^None$/),
  ]);
}

async function closeOpenPopups(page) {
  await page.keyboard.press('Escape').catch(() => null);
  await page.waitForTimeout(150);
  await page.keyboard.press('Escape').catch(() => null);
  await page.waitForTimeout(150);
}

async function dismissDesktopAppPrompt(page) {
  const prompt = page.locator('[role="dialog"]').filter({ hasText: 'Open in Notion’s desktop app?' }).first();
  if (!(await prompt.isVisible().catch(() => false))) {
    return;
  }

  const closeTargets = [
    prompt.getByLabel(/Close/i),
    prompt.locator('[aria-label="Close"]'),
    prompt.locator('svg').locator('xpath=ancestor::*[@role="button"][1]'),
  ];

  if (await clickFirstVisible(closeTargets)) {
    await page.waitForTimeout(200);
    return;
  }

  await page.keyboard.press('Escape').catch(() => null);
  await page.waitForTimeout(200);
}

async function saveFailureScreenshot(page, screenshotDir, signal, suffix) {
  fs.mkdirSync(screenshotDir, { recursive: true });
  const safeName = `${stripDashes(signal.id)}-${suffix}.png`;
  const screenshotPath = path.join(screenshotDir, safeName);
  await page.screenshot({ path: screenshotPath, fullPage: true }).catch(() => null);
  return screenshotPath;
}

async function clearReminderForSignal(page, signal, options) {
  const pageUrl = await loadSignalPage(page, signal, options);
  await dismissDesktopAppPrompt(page);

  if (await pageLooksLoggedOut(page)) {
    throw new Error('Notion session is not logged in. Run with --setup-login first.');
  }

  if (await pageLooksPublicReadOnly(page)) {
    throw new Error('Notion page is still opening in public read-only mode. Finish login with --setup-login first.');
  }

  let row = await getTimeRow(page);
  await row.waitFor({ state: 'visible', timeout: 20000 }).catch(async () => {
    const screenshot = await saveFailureScreenshot(page, options.screenshotDir, signal, 'time-row-missing');
    throw new Error(`Could not find the Time (ET) row on the page. Screenshot: ${screenshot}`);
  });

  if (!(await hasAlarmIcon(row))) {
    return { status: 'skipped_no_alarm' };
  }

  const opened = await openTimeEditor(page, row);
  if (!opened) {
    const screenshot = await saveFailureScreenshot(page, options.screenshotDir, signal, 'open-failed');
    throw new Error(`Could not open the Time (ET) editor. Screenshot: ${screenshot}`);
  }

  const changed = await setReminderToNone(page);
  if (!changed) {
    await closeOpenPopups(page);
    const screenshot = await saveFailureScreenshot(page, options.screenshotDir, signal, 'none-not-found');
    throw new Error(`Could not find the reminder control or the None option. Screenshot: ${screenshot}`);
  }

  await closeOpenPopups(page);
  await page.reload({ waitUntil: 'domcontentloaded', timeout: 60000 });
  await waitForNotionReady(page);

  row = await getTimeRow(page);
  await row.waitFor({ state: 'visible', timeout: 20000 });

  if (await hasAlarmIcon(row)) {
    const screenshot = await saveFailureScreenshot(page, options.screenshotDir, signal, 'alarm-still-visible');
    throw new Error(`Reminder icon is still visible after update. Screenshot: ${screenshot}`);
  }

  return { status: 'cleared' };
}

async function main() {
  if (hasFlag('--help')) {
    console.log(`Clear reminder icons from expired Notion Time (ET) values

Usage:
  node scripts/clear-past-reminders.js --setup-login
  node scripts/clear-past-reminders.js [--headed] [--limit 10] [--page-size 100]
  node scripts/clear-past-reminders.js --dry-run [--limit 10]
  node scripts/clear-past-reminders.js --id <pageId>

Options:
  --setup-login           Open a persistent browser profile so you can log into Notion once
  --headed                Show the browser while the cleanup runs
  --dry-run               List expired pages without changing reminders
  --id <pageId>           Target a single Notion page
  --limit <n>             Only process the first n expired pages
  --page-size <n>         Number of pages to load from Notion (max 100)
  --cookie-file <path>    Import Notion cookies exported from Brave
  --skip-cookie-refresh   Reuse the existing cookie file without exporting again
  --setup-timeout-minutes Minutes to wait for manual Notion login during setup
  --profile-dir <path>    Persistent Playwright profile directory
  --screenshot-dir <path> Store failure screenshots here
`);
    return;
  }

  const pageSize = Math.min(toInt(getArgValue('--page-size', '100'), 100), 100);
  const limit = Math.max(toInt(getArgValue('--limit', '0'), 0), 0);
  const profileDir = path.resolve(getArgValue('--profile-dir', DEFAULT_PROFILE_DIR));
  const screenshotDir = path.resolve(getArgValue('--screenshot-dir', DEFAULT_SCREENSHOT_DIR));
  const cookieFileArg = getArgValue('--cookie-file', null);
  const cookieFile = path.resolve(cookieFileArg || DEFAULT_COOKIE_FILE);
  const targetPageId = getArgValue('--id', null);
  const dryRun = hasFlag('--dry-run');
  const skipCookieRefresh = hasFlag('--skip-cookie-refresh');
  const headed = hasFlag('--headed') || hasFlag('--setup-login');
  const setupTimeoutMinutes = Math.max(toInt(getArgValue('--setup-timeout-minutes', '10'), 10), 1);
  const databaseUrl = await getDatabaseUrl();
  const signals = await getSignals({ pageSize });
  const expiredSignals = getExpiredSignals(signals, Date.now(), targetPageId);
  const sampleSignal = expiredSignals[0] || signals.find(signal => signal.time) || null;

  if (hasFlag('--setup-login')) {
    await runSetupLogin({ profileDir, databaseUrl, setupTimeoutMinutes, sampleSignal });
    return;
  }

  let filteredExpiredSignals = expiredSignals;

  if (limit > 0) {
    filteredExpiredSignals = filteredExpiredSignals.slice(0, limit);
  }

  if (filteredExpiredSignals.length === 0) {
    console.log(JSON.stringify({ success: true, dryRun, checked: 0, message: 'No expired signals found in the loaded page set.' }, null, 2));
    return;
  }

  if (!dryRun && !skipCookieRefresh) {
    exportBraveCookies(cookieFile);
  }

  if (dryRun) {
    console.log(JSON.stringify({
      success: true,
      dryRun: true,
      databaseUrl,
      profileDir,
      cookieFile: fs.existsSync(cookieFile) ? cookieFile : null,
      checked: filteredExpiredSignals.length,
      items: filteredExpiredSignals.map(signal => ({
        id: signal.id,
        event: signal.event,
        time: signal.time,
        status: signal.status,
        pageUrl: getSignalPageUrl(signal, databaseUrl),
        })),
    }, null, 2));
    return;
  }

  const context = await launchContext({ profileDir, headless: !headed });

  try {
    const page = await getPrimaryPage(context);
    const cookies = loadCookiesFromFile(cookieFile);
    if (cookies.length > 0) {
      await context.addCookies(cookies);
    }
    await page.goto(databaseUrl, { waitUntil: 'domcontentloaded', timeout: 60000 });
    await waitForNotionReady(page);

    if (await pageLooksLoggedOut(page)) {
      throw new Error(`Notion session is not ready. Export Brave cookies first or run --setup-login. Profile dir: ${profileDir}`);
    }

    const results = [];
    for (const signal of filteredExpiredSignals) {
      try {
        const result = await clearReminderForSignal(page, signal, { screenshotDir, databaseUrl });
        results.push({
          id: signal.id,
          event: signal.event,
          time: signal.time,
          pageUrl: getSignalPageUrl(signal, databaseUrl),
          ...result,
        });
      } catch (error) {
        results.push({
          id: signal.id,
          event: signal.event,
          time: signal.time,
          pageUrl: getSignalPageUrl(signal, databaseUrl),
          status: 'failed',
          error: error.message,
        });
      }
    }

    console.log(JSON.stringify({
      success: results.every(item => item.status !== 'failed'),
      checked: results.length,
      cleared: results.filter(item => item.status === 'cleared').length,
      skippedNoAlarm: results.filter(item => item.status === 'skipped_no_alarm').length,
      failed: results.filter(item => item.status === 'failed').length,
      profileDir,
      screenshotDir,
      results,
    }, null, 2));
  } finally {
    await context.close();
  }
}

main().catch(error => {
  console.error(JSON.stringify({ success: false, error: error.message }, null, 2));
  process.exit(1);
});
