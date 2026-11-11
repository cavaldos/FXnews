const axios = require('axios');
const { formatInTimeZone } = require('date-fns-tz');

const ET_TIMEZONE = 'America/New_York';

// Public CDN feed used by the ForexFactory calendar.
// This is significantly easier to consume than scraping the website HTML.
const FOREXFACTORY_THISWEEK_JSON_URL = 'https://nfs.faireconomy.media/ff_calendar_thisweek.json';

function normalizeImpact(input) {
  const value = String(input || '').trim().toLowerCase();
  if (value === 'high') return 'High';
  if (value === 'medium') return 'Medium';
  if (value === 'low') return 'Low';
  return 'Low';
}

function normalizeCountry(input) {
  const value = String(input || '').trim().toUpperCase();
  return value || 'ALL';
}

function normalizeEvent(raw) {
  if (!raw || typeof raw !== 'object') return null;

  const title = String(raw.title || '').trim();
  const country = normalizeCountry(raw.country);
  const date = String(raw.date || '').trim();

  if (!title || !date) {
    return null;
  }

  return {
    title,
    country,
    date,
    impact: normalizeImpact(raw.impact),
    actual: String(raw.actual || '').trim(),
    forecast: String(raw.forecast || '').trim(),
    previous: String(raw.previous || '').trim(),
  };
}

function getEtDayString(dateInput) {
  const parsed = dateInput instanceof Date ? dateInput : new Date(dateInput);
  if (Number.isNaN(parsed.getTime())) {
    return null;
  }
  return formatInTimeZone(parsed, ET_TIMEZONE, 'yyyy-MM-dd');
}

function toEtDateTime(dateInput) {
  const parsed = dateInput instanceof Date ? dateInput : new Date(dateInput);
  if (Number.isNaN(parsed.getTime())) {
    return null;
  }
  return formatInTimeZone(parsed, ET_TIMEZONE, "yyyy-MM-dd'T'HH:mm:ss");
}

function filterEventsForEtDay(events, etDay) {
  const target = String(etDay || '').trim();
  if (!target) return [];

  return (events || []).filter(item => {
    const day = getEtDayString(item?.date);
    return day === target;
  });
}

function filterEventsOnOrAfter(events, startTime) {
  const threshold = startTime instanceof Date ? startTime.getTime() : new Date(startTime).getTime();
  if (!Number.isFinite(threshold)) {
    return events || [];
  }

  return (events || []).filter(item => {
    const eventMs = new Date(item?.date || '').getTime();
    return Number.isFinite(eventMs) && eventMs >= threshold;
  });
}

async function fetchForexFactoryThisWeek(options = {}) {
  const url = options.url || FOREXFACTORY_THISWEEK_JSON_URL;
  const res = await axios.get(url, {
    timeout: options.timeout || 20000,
    headers: {
      // A stable UA helps avoid edge-case blocks.
      'User-Agent': options.userAgent || 'NewsTrade/1.0 (+calendar)'
    },
  });

  const data = res.data;
  if (!Array.isArray(data)) {
    throw new Error('ForexFactory calendar feed returned unexpected payload');
  }

  return data.map(normalizeEvent).filter(Boolean);
}

module.exports = {
  ET_TIMEZONE,
  FOREXFACTORY_THISWEEK_JSON_URL,
  fetchForexFactoryThisWeek,
  filterEventsForEtDay,
  filterEventsOnOrAfter,
  getEtDayString,
  toEtDateTime,
};
