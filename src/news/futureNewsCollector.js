const axios = require('axios');
const { chromium } = require('playwright');
const { formatInTimeZone, fromZonedTime } = require('date-fns-tz');
const { fetchForexFactoryThisWeek, filterEventsOnOrAfter } = require('./forexfactoryCalendar');
const { searchNews, TRUSTED_DOMAINS } = require('./tavily');
const config = require('../config');

const FOREXFACTORY_NEXTWEEK_URL = 'https://nfs.faireconomy.media/ff_calendar_nextweek.json';
const ET_TIMEZONE = 'America/New_York';
const TRADING_ECONOMICS_CALENDAR_URL = 'https://api.tradingeconomics.com/calendar';

// ─── helpers ────────────────────────────────────────────────────────

function dedupeEvents(events) {
  const seen = new Set();
  const result = [];
  for (const event of events || []) {
    const key = [
      event.source,
      String(event.title || '').toLowerCase(),
      event.eventTimeUtc || '',
      event.country || '',
      event.currency || '',
    ].join('|');
    if (seen.has(key)) continue;
    seen.add(key);
    result.push(event);
  }
  return result;
}

function textOf(value) {
  return String(value || '').replace(/\s+/g, ' ').trim();
}

function clampFutureWindow(date, now, daysAhead) {
  const nowMs = now.getTime();
  const upperBoundMs = nowMs + daysAhead * 24 * 60 * 60 * 1000;
  const ms = date.getTime();
  return ms > nowMs && ms <= upperBoundMs;
}

function normalizeImpactLabel(value) {
  const input = String(value || '').trim().toLowerCase();
  if (input === '3' || input.includes('high')) return 'High';
  if (input === '2' || input.includes('medium') || input.includes('med')) return 'Medium';
  return 'Low';
}

function inferCurrencyFromCountryName(country = '') {
  const name = String(country || '').trim().toLowerCase();
  if (!name) return null;
  if (name.includes('united states') || name === 'us') return 'USD';
  if (name.includes('euro') || name.includes('euro area')) return 'EUR';
  if (name.includes('united kingdom') || name.includes('britain')) return 'GBP';
  if (name.includes('japan')) return 'JPY';
  if (name.includes('australia')) return 'AUD';
  if (name.includes('new zealand')) return 'NZD';
  if (name.includes('canada')) return 'CAD';
  if (name.includes('switzerland')) return 'CHF';
  if (name.includes('china')) return 'CNY';
  return null;
}

function addDaysToEtDayString(etDayString, days) {
  const base = new Date(`${etDayString}T00:00:00Z`);
  if (!Number.isFinite(base.getTime())) return null;
  base.setUTCDate(base.getUTCDate() + days);
  return base.toISOString().slice(0, 10);
}

function normalizeTimeTo24h(rawTime = '') {
  const input = String(rawTime || '').trim().toLowerCase();
  if (!input || input === 'all day' || input === 'tentative') return null;

  const m = input.match(/^(\d{1,2}):(\d{2})(?:\s*(am|pm))?$/i);
  if (!m) return null;

  let hour = parseInt(m[1], 10);
  const minute = parseInt(m[2], 10);
  const suffix = (m[3] || '').toLowerCase();

  if (!Number.isFinite(hour) || !Number.isFinite(minute)) return null;

  if (suffix === 'pm' && hour < 12) hour += 12;
  if (suffix === 'am' && hour === 12) hour = 0;
  if (hour < 0 || hour > 23 || minute < 0 || minute > 59) return null;

  return `${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}:00`;
}

function buildInvestingEventTimeUtc(tab, displayTime, now = new Date()) {
  const timePart = normalizeTimeTo24h(displayTime);
  if (!timePart) return null;

  const todayEt = formatInTimeZone(now, ET_TIMEZONE, 'yyyy-MM-dd');
  if (!todayEt) return null;

  let etDate = null;
  const normalizedTab = String(tab || '').trim().toLowerCase();
  if (normalizedTab === 'tomorrow') {
    etDate = addDaysToEtDayString(todayEt, 1);
  } else if (normalizedTab === 'next week') {
    etDate = addDaysToEtDayString(todayEt, 7);
  }

  if (!etDate) return null;

  const zoned = `${etDate} ${timePart}`;
  const utcDate = fromZonedTime(zoned, ET_TIMEZONE);
  return Number.isFinite(utcDate.getTime()) ? utcDate.toISOString() : null;
}

// ─── Shared Playwright browser ─────────────────────────────────────

async function launchBrowser() {
  const browser = await chromium.launch({
    headless: true,
    args: ['--disable-blink-features=AutomationControlled'],
  });
  const context = await browser.newContext({
    userAgent:
      'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    viewport: { width: 1920, height: 1080 },
    locale: 'en-US',
    timezoneId: 'America/New_York',
  });
  await context.addInitScript(() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => false });
  });
  return { browser, context };
}

function getBrowserTimeout(options = {}, fallback = 45000) {
  const input = Number(options.timeout);
  if (!Number.isFinite(input)) return fallback;
  return Math.max(input, fallback);
}

// ─── Investing.com (Playwright) ─────────────────────────────────────

function parseInvestingRowId(rowId) {
  // Row ID format: "910-543603-NewZealand-5" → extract country
  if (!rowId) return null;
  const parts = rowId.split('-');
  if (parts.length >= 3) {
    return parts.slice(2, -1).join(' ').replace(/([a-z])([A-Z])/g, '$1 $2');
  }
  return null;
}

async function fetchInvestingFutureEvents(options = {}) {
  const { browser, context } = options._browser
    ? { browser: null, context: options._browser }
    : await launchBrowser();
  const ownBrowser = !options._browser;

  try {
    const page = await context.newPage();
    await page.goto('https://www.investing.com/economic-calendar/', {
      waitUntil: 'commit',
      timeout: getBrowserTimeout(options),
    });
    await page.waitForTimeout(10000);

    const allEvents = [];

    // Scrape both "Tomorrow" and "Next Week" tabs
    for (const tabName of ['Tomorrow', 'Next Week']) {
      try {
        const btn = page.locator(`button:has-text("${tabName}")`).first();
        if ((await btn.count()) > 0) {
          await btn.click();
          await page.waitForTimeout(6000);
        }
      } catch {
        continue;
      }

      const events = await page.evaluate((tab) => {
        const rows = document.querySelectorAll('table tbody tr[id]');
        const results = [];

        for (const row of rows) {
          const tds = row.querySelectorAll('td');
          if (tds.length < 5) continue;

          const rowId = row.id || '';
          const time = tds[1]?.textContent?.trim() || '';
          const currency = tds[2]?.textContent?.trim() || '';

          // Cell 3: event name with embedded Act/Cons/Prev
          const eventCell = tds[3]?.textContent?.trim() || '';
          const eventName = eventCell
            .replace(/Act:.*$/, '')
            .replace(/Cons:.*$/, '')
            .replace(/Prev\.?:.*$/, '')
            .trim();

          // Extract inline Act/Cons/Prev (primary data source)
          // Values end before the next key or end of string (no spaces between keys)
          const actMatch = eventCell.match(/Act:\s*([^A-Z\s][^A-Z]*?)(?=Cons:|Prev[.:]|$)/);
          const consMatch = eventCell.match(/Cons:\s*([^A-Z\s][^A-Z]*?)(?=Prev[.:]|Act:|$)/);
          const prevMatch = eventCell.match(/Prev\.?:\s*([^A-Z\s][^A-Z]*?)(?=Act:|Cons:|$)/);

          const actual = actMatch ? actMatch[1] : '';
          const forecast = (tds[6]?.textContent?.trim()) || (consMatch ? consMatch[1] : '');
          const previous = (tds[7]?.textContent?.trim()) || (prevMatch ? prevMatch[1] : '');

          // Impact from cell 4 (icon classes) or cell 8
          const impactCell = tds[4]?.innerHTML || tds[8]?.innerHTML || '';
          const bullIcons = impactCell.querySelectorAll ? 0 : (impactCell.match(/icon/gi) || []).length;
          // Alternative: count colored circles/bars
          const svgCount = (impactCell.match(/<svg|<i |bull|#FFB84D|#F7931A|#FF4500|medium|high|low/gi) || []).length;

          let impact = 'unknown';
          if (svgCount >= 3 || /high/i.test(impactCell)) impact = 'High';
          else if (svgCount === 2 || /medium/i.test(impactCell)) impact = 'Medium';
          else if (svgCount >= 1 || /low/i.test(impactCell)) impact = 'Low';

          if (eventName && eventName.length > 3) {
            results.push({
              rowId,
              tab,
              time,
              currency,
              event: eventName,
              impact,
              actual,
              forecast,
              previous,
            });
          }
        }
        return results;
      }, tabName);

      allEvents.push(...events);
    }

    await page.close();

    // Filter: keep only future events (Act is "-" or empty = not yet reported)
    const now = options.now || new Date();
    return allEvents
      .filter((e) => {
        const act = (e.actual || '').replace(/-/g, '').trim();
        return !act;
      })
      .map((e) => ({
        source: 'Investing.com',
        kind: 'calendar',
        title: e.event,
        eventTimeUtc: buildInvestingEventTimeUtc(e.tab, e.time, now),
        displayTime: e.time,
        tab: e.tab,
        country: parseInvestingRowId(e.rowId),
        currency: e.currency || null,
        impact: e.impact,
        actual: null,
        forecast: e.forecast && e.forecast !== '-' ? e.forecast : null,
        previous: e.previous && e.previous !== '-' ? e.previous : null,
        url: 'https://www.investing.com/economic-calendar/',
      }));
  } finally {
    if (ownBrowser && browser) await browser.close();
  }
}

// ─── BabyPips (Playwright) ──────────────────────────────────────────

async function fetchBabyPipsFutureEvents(options = {}) {
  const { browser, context } = options._browser
    ? { browser: null, context: options._browser }
    : await launchBrowser();
  const ownBrowser = !options._browser;

  try {
    const page = await context.newPage();
    await page.goto('https://www.babypips.com/economic-calendar', {
      waitUntil: 'commit',
      timeout: getBrowserTimeout(options),
    });
    await page.waitForTimeout(12000);

    const events = await page.evaluate(() => {
      const rows = document.querySelectorAll('table tr, [class*=calendar] tr');
      const results = [];
      let currentDate = '';
      const now = new Date();

      for (const row of rows) {
        const text = row.textContent.trim();

        // Date header detection
        const dateMatch = text.match(/((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s*\d+)/i);
        const dayMatch = text.match(/(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)/i);
        if (dateMatch && dayMatch) {
          currentDate = text.replace(/Back to Top.*$/, '').trim();
          continue;
        }
        if (dateMatch && text.length < 50) {
          currentDate = text.replace(/Back to Top.*$/, '').trim();
          continue;
        }

        const tds = row.querySelectorAll('td');
        if (tds.length < 3) continue;

        const cellTexts = Array.from(tds).map((td) => td.textContent.trim());
        let time = '';
        let currency = '';
        let eventName = '';
        let impact = '';

        for (const td of tds) {
          const t = td.textContent.trim();
          const cls = (td.className || '').toLowerCase();
          if (/^\d{1,2}:\d{2}\s*(am|pm)?$/i.test(t) || t === 'All Day' || t === 'Tentative') {
            time = t;
          } else if (/^[A-Z]{3}$/.test(t)) {
            currency = t;
          } else if (t.length > 5 && t.length < 200 && !eventName && !/^(high|medium|low)$/i.test(t)) {
            eventName = t.replace(/View Details.*$/, '').trim();
          }
        }

        // Impact from classes or text
        const impactEl = row.querySelector('[class*=impact]');
        if (impactEl) {
          const impText = impactEl.textContent.trim().toLowerCase();
          if (impText.includes('high')) impact = 'High';
          else if (impText.includes('medium')) impact = 'Medium';
          else if (impText.includes('low')) impact = 'Low';
        }
        if (!impact) {
          const fullText = row.textContent.toLowerCase();
          if (fullText.includes('high')) impact = 'High';
          else if (fullText.includes('medium')) impact = 'Medium';
          else if (fullText.includes('low')) impact = 'Low';
        }

        if (eventName && eventName.length > 3) {
          results.push({
            date: currentDate,
            time,
            currency,
            event: eventName,
            impact: impact || 'unknown',
          });
        }
      }

      return results;
    });

    await page.close();

    // Filter to future dates only
    const now = options.now || new Date();
    const daysAhead = options.daysAhead || 14;
    const months = { Jan: 0, Feb: 1, Mar: 2, Apr: 3, May: 4, Jun: 5, Jul: 6, Aug: 7, Sep: 8, Oct: 9, Nov: 10, Dec: 11 };

    return events
      .filter((e) => {
        if (!e.date) return false;
        const m = e.date.match(/(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s*(\d+)/i);
        if (!m) return false;
        const eventDate = new Date(now.getFullYear(), months[m[1]], parseInt(m[2]));
        return eventDate > now && clampFutureWindow(eventDate, now, daysAhead);
      })
      .map((e) => ({
        source: 'BabyPips',
        kind: 'calendar',
        title: e.event,
        eventTimeUtc: null,
        displayTime: e.time,
        displayDate: e.date,
        country: null,
        currency: e.currency || null,
        impact: e.impact,
        actual: null,
        forecast: null,
        previous: null,
        url: 'https://www.babypips.com/economic-calendar',
      }));
  } finally {
    if (ownBrowser && browser) await browser.close();
  }
}

// ─── ForexLive (Playwright) ─────────────────────────────────────────

async function fetchForexLiveFutureEvents(options = {}) {
  const { browser, context } = options._browser
    ? { browser: null, context: options._browser }
    : await launchBrowser();
  const ownBrowser = !options._browser;

  try {
    const page = await context.newPage();
    await page.goto('https://www.forexlive.com/economic-calendar/', {
      waitUntil: 'commit',
      timeout: getBrowserTimeout(options),
    });
    await page.waitForTimeout(12000);

    const title = await page.title();
    const contentLen = (await page.content()).length;

    // ForexLive embeds investingLive calendar — try extracting data
    const events = await page.evaluate(() => {
      const rows = document.querySelectorAll('table tr, [class*=calendar] tr, [class*=event-row]');
      const results = [];

      for (const row of rows) {
        const tds = row.querySelectorAll('td');
        if (tds.length < 3) continue;

        const cellTexts = Array.from(tds).map((td) => td.textContent.trim());
        let time = '';
        let currency = '';
        let eventName = '';
        let impact = '';

        for (const td of tds) {
          const t = td.textContent.trim();
          if (/^\d{1,2}:\d{2}$/.test(t)) time = t;
          else if (/^[A-Z]{3}$/.test(t)) currency = t;
          else if (t.length > 5 && t.length < 200 && !eventName) {
            eventName = t.replace(/Act:.*$/, '').replace(/Cons:.*$/, '').replace(/Prev\.?:.*$/, '').trim();
          }
        }

        const impactHtml = row.innerHTML || '';
        const bullCount = (impactHtml.match(/bull/gi) || []).length;
        if (bullCount >= 3) impact = 'High';
        else if (bullCount === 2) impact = 'Medium';
        else if (bullCount === 1) impact = 'Low';

        if (eventName && eventName.length > 3) {
          results.push({ time, currency, event: eventName, impact: impact || 'unknown' });
        }
      }

      return results;
    });

    await page.close();

    return events.map((e) => ({
      source: 'ForexLive',
      kind: 'calendar',
      title: e.event,
      eventTimeUtc: null,
      displayTime: e.time,
      country: null,
      currency: e.currency || null,
      impact: e.impact,
      actual: null,
      forecast: null,
      previous: null,
      url: 'https://www.forexlive.com/economic-calendar/',
    }));
  } catch (err) {
    // ForexLive is intermittent — fail gracefully
    return [];
  } finally {
    if (ownBrowser && browser) await browser.close();
  }
}

// ─── ForexFactory (JSON API) ────────────────────────────────────────

function mapFfEvent(raw) {
  const date = raw.date ? new Date(raw.date) : null;
  if (!date || !Number.isFinite(date.getTime())) return null;
  return {
    source: 'ForexFactory',
    kind: 'calendar',
    title: textOf(raw.title),
    eventTimeUtc: date.toISOString(),
    country: textOf(raw.country) || null,
    currency: textOf(raw.country) || null,
    impact: raw.impact || 'unknown',
    actual: textOf(raw.actual) || null,
    forecast: textOf(raw.forecast) || null,
    previous: textOf(raw.previous) || null,
    url: null,
  };
}

async function fetchForexFactoryFutureEvents(options = {}) {
  const now = options.now || new Date();
  const daysAhead = options.daysAhead || 14;
  const allEvents = [];

  try {
    const thisWeek = await fetchForexFactoryThisWeek({ timeout: options.timeout || 20000 });
    const future = filterEventsOnOrAfter(thisWeek, now);
    for (const raw of future) {
      const mapped = mapFfEvent(raw);
      if (mapped && clampFutureWindow(new Date(mapped.eventTimeUtc), now, daysAhead)) {
        allEvents.push(mapped);
      }
    }
  } catch {
    // thisWeek failed
  }

  try {
    const res = await axios.get(FOREXFACTORY_NEXTWEEK_URL, {
      timeout: options.timeout || 20000,
      headers: { 'User-Agent': 'NewsTrade/1.0 (+calendar)' },
    });
    if (Array.isArray(res.data)) {
      for (const raw of res.data) {
        const mapped = mapFfEvent(raw);
        if (mapped && clampFutureWindow(new Date(mapped.eventTimeUtc), now, daysAhead)) {
          allEvents.push(mapped);
        }
      }
    }
  } catch {
    // nextWeek may 404 — OK
  }

  return dedupeEvents(allEvents).sort((a, b) => (a.eventTimeUtc || '').localeCompare(b.eventTimeUtc || ''));
}

// ─── TradingEconomics (REST API) ─────────────────────────────────────

function mapTradingEconomicsEvent(raw) {
  const eventDate = new Date(raw?.Date || raw?.date || raw?.CalendarDate || '');
  if (!Number.isFinite(eventDate.getTime())) return null;

  const country = textOf(raw?.Country || raw?.country) || null;
  const explicitCurrency = textOf(raw?.Currency || raw?.currency).toUpperCase();
  const currency = explicitCurrency || inferCurrencyFromCountryName(country) || null;

  return {
    source: 'TradingEconomics',
    kind: 'calendar',
    title: textOf(raw?.Event || raw?.event || raw?.Category || 'Economic Event'),
    eventTimeUtc: eventDate.toISOString(),
    displayTime: formatInTimeZone(eventDate, ET_TIMEZONE, 'HH:mm'),
    country,
    currency,
    impact: normalizeImpactLabel(raw?.Importance || raw?.importance),
    actual: textOf(raw?.Actual || raw?.actual) || null,
    forecast: textOf(raw?.Forecast || raw?.forecast) || null,
    previous: textOf(raw?.Previous || raw?.previous) || null,
    url: textOf(raw?.URL || raw?.url) || 'https://tradingeconomics.com/calendar',
  };
}

async function fetchTradingEconomicsFutureEvents(options = {}) {
  const now = options.now || new Date();
  const daysAhead = options.daysAhead || 14;
  const d1 = formatInTimeZone(now, ET_TIMEZONE, 'yyyy-MM-dd');
  const d2 = formatInTimeZone(new Date(now.getTime() + daysAhead * 24 * 60 * 60 * 1000), ET_TIMEZONE, 'yyyy-MM-dd');
  const apiKey = options.tradingEconomicsKey || process.env.TRADING_ECONOMICS_KEY;

  if (!apiKey) {
    throw new Error('TRADING_ECONOMICS_KEY is not configured');
  }

  const res = await axios.get(TRADING_ECONOMICS_CALENDAR_URL, {
    timeout: options.timeout || 30000,
    params: {
      c: apiKey,
      f: 'json',
      d1,
      d2,
    },
  });

  const rows = Array.isArray(res.data) ? res.data : [];
  return rows
    .map(mapTradingEconomicsEvent)
    .filter(Boolean)
    .filter((event) => {
      const when = new Date(event.eventTimeUtc);
      return Number.isFinite(when.getTime()) && clampFutureWindow(when, now, daysAhead);
    })
    .sort((a, b) => (a.eventTimeUtc || '').localeCompare(b.eventTimeUtc || ''));
}

// ─── Tavily (news search) ───────────────────────────────────────────

const FUTURE_NEWS_QUERIES = [
  'upcoming economic calendar events this week next week CPI NFP GDP FOMC ECB BOE BOJ rate decision',
  'scheduled central bank meetings interest rate decision upcoming policy announcement',
  'upcoming earnings reports major companies this week next week',
  'economic data releases scheduled this week GDP inflation employment',
];

const CALENDAR_DOMAINS = [
  ...TRUSTED_DOMAINS,
  'forexlive.com',
  'babypips.com',
  'forexfactory.com',
  'tradingeconomics.com',
  'dailyfx.com',
  'fxstreet.com',
];

async function fetchTavilyFutureNews(options = {}) {
  const queries = options.queries || FUTURE_NEWS_QUERIES;
  const maxPerQuery = options.maxPerQuery || 8;
  const allEvents = [];

  const settled = await Promise.allSettled(
    queries.map((q) =>
      searchNews(q, {
        apiKey: options.tavilyApiKey || config.news.tavily.apiKey,
        maxResults: maxPerQuery,
        searchDepth: 'basic',
        includeDomains: CALENDAR_DOMAINS,
        timeout: options.timeout || 20000,
      })
    )
  );

  for (const result of settled) {
    if (result.status !== 'fulfilled') continue;
    for (const item of result.value) {
      if (!item.title) continue;
      allEvents.push({
        source: textOf(item.source) || 'Tavily',
        kind: 'news',
        title: textOf(item.title),
        eventTimeUtc: item.publishedAt || null,
        country: null,
        currency: null,
        impact: 'unknown',
        actual: null,
        forecast: null,
        previous: null,
        url: item.sourceUrl || null,
        description: textOf(item.description) || null,
        score: item.score || 0,
      });
    }
  }

  const seen = new Set();
  return allEvents.filter((ev) => {
    const key = ev.url || ev.title;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

// ─── Main collector ─────────────────────────────────────────────────

const SOURCE_FETCHERS = {
  investing: fetchInvestingFutureEvents,
  babypips: fetchBabyPipsFutureEvents,
  forexlive: fetchForexLiveFutureEvents,
  forexfactory: fetchForexFactoryFutureEvents,
  tradingeconomics: fetchTradingEconomicsFutureEvents,
  tavily: fetchTavilyFutureNews,
};

async function collectFutureNews(options = {}) {
  const now = options.now || new Date();
  const daysAhead = Number.isFinite(options.daysAhead) ? options.daysAhead : 14;
  const requested = Array.isArray(options.sources) && options.sources.length
    ? options.sources
    : Object.keys(SOURCE_FETCHERS);

  const normalizedSources = requested.map((s) => String(s || '').trim().toLowerCase()).filter(Boolean);
  const uniqueSources = [...new Set(normalizedSources)];

  // Playwright sources share a single browser instance
  const playwrightSources = ['investing', 'babypips', 'forexlive'];
  const needsBrowser = uniqueSources.some((s) => playwrightSources.includes(s));

  let browserObj = null;
  if (needsBrowser) {
    try {
      browserObj = await launchBrowser();
    } catch (err) {
      console.error('Failed to launch browser:', err.message);
    }
  }

  const events = [];
  const errors = [];
  const countsBySource = {};

  for (const source of uniqueSources) {
    const fetcher = SOURCE_FETCHERS[source];
    if (!fetcher) {
      errors.push({ source, error: `Unsupported source: ${source}` });
      continue;
    }

    try {
      const fetchOptions = {
        ...options,
        now,
        daysAhead,
        _browser: browserObj && playwrightSources.includes(source) ? browserObj.context : null,
      };
      const sourceEvents = await fetcher(fetchOptions);
      countsBySource[source] = sourceEvents.length;
      events.push(...sourceEvents);
    } catch (err) {
      countsBySource[source] = 0;
      errors.push({ source, error: err.message });
    }
  }

  if (browserObj?.browser) {
    await browserObj.browser.close().catch(() => {});
  }

  const merged = dedupeEvents(events).sort((a, b) => {
    const ta = a.eventTimeUtc || a.displayTime || '';
    const tb = b.eventTimeUtc || b.displayTime || '';
    return ta.localeCompare(tb);
  });

  return {
    generatedAt: new Date().toISOString(),
    nowUtc: now.toISOString(),
    daysAhead,
    sourcesRequested: uniqueSources,
    sourcesSucceeded: uniqueSources.filter((s) => !errors.some((e) => e.source === s)),
    sourcesFailed: errors.map((e) => e.source),
    countsBySource,
    totalEvents: merged.length,
    events: merged,
    errors,
  };
}

module.exports = {
  collectFutureNews,
  fetchInvestingFutureEvents,
  fetchBabyPipsFutureEvents,
  fetchForexLiveFutureEvents,
  fetchForexFactoryFutureEvents,
  fetchTradingEconomicsFutureEvents,
  fetchTavilyFutureNews,
};
