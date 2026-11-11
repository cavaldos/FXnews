const axios = require('axios');
const config = require('../config');

const TAVILY_ENDPOINT = 'https://api.tavily.com/search';

const TRUSTED_DOMAINS = [
  'reuters.com',
  'bloomberg.com',
  'cnbc.com',
  'wsj.com',
  'apnews.com',
  'barrons.com',
  'axios.com',
  'businessinsider.com',
  'cnn.com',
  'nikkei.com',
  'theglobeandmail.com',
  'federalreserve.gov',
  'ecb.europa.eu',
  'bankofengland.co.uk',
  'boj.or.jp',
  'bankofcanada.ca',
  'bis.org',
  'opec.org',
  'eia.gov',
  'bls.gov',
  'bea.gov',
  'treasury.gov',
  'imf.org',
  'oecd.org',
  'worldbank.org',
  'marketwatch.com',
  'ft.com',
  'finance.yahoo.com',
  'investing.com',
];

const DEFAULT_QUERIES = [
  'Federal Reserve ECB BOE BOJ Bank of Canada rates inflation latest market impact Reuters Bloomberg FT AP',
  'US CPI PCE NFP unemployment GDP surprise market impact forex commodities BLS BEA Treasury Nikkei',
  'oil gold natural gas geopolitical risk sanctions conflict market reaction Reuters CNBC Barrons OPEC EIA',
  'Nasdaq S&P 500 earnings outlook AI chip stocks macro news WSJ CNBC Barrons Business Insider',
  'crypto regulation ETF flows bitcoin ethereum market impact Bloomberg Reuters CNBC WSJ',
  'global macro central banks bonds yields FX Reuters Bloomberg FT AP Axios',
];

const SOURCE_PRIORITY = {
  Reuters: 6,
  Bloomberg: 5,
  WSJ: 4,
  CNBC: 3,
  FT: 4,
  AP: 4,
  Barrons: 4,
  Nikkei: 4,
  'Globe and Mail': 3,
  Axios: 3,
  'CNN Business': 2,
  'Business Insider': 2,
  Fed: 6,
  ECB: 6,
  BOE: 6,
  BOJ: 6,
  BoC: 6,
  BIS: 6,
  OPEC: 6,
  EIA: 6,
  BLS: 6,
  BEA: 6,
  Treasury: 6,
  OECD: 5,
  IMF: 5,
  'World Bank': 5,
  MarketWatch: 2,
  'Yahoo Finance': 2,
  Investing: 2,
  Other: 1,
};

const TITLE_STOPWORDS = new Set([
  'the', 'and', 'for', 'with', 'from', 'that', 'this', 'after', 'amid', 'into', 'over', 'under', 'latest',
  'news', 'report', 'reports', 'says', 'say', 'market', 'markets', 'update', 'live', 'analysis', 'global',
  'stocks', 'stock', 'economy', 'economic', 'finance', 'financial', 'latest', 'breaking', 'exclusive',
]);

function uniqueStrings(values) {
  return [...new Set((values || []).map(value => String(value || '').trim()).filter(Boolean))];
}

function toTimestamp(value) {
  const timestamp = value ? new Date(value).getTime() : 0;
  return Number.isFinite(timestamp) ? timestamp : 0;
}

function isFreshNewsItem(item, maxAgeHours, nowMs) {
  if (!Number.isFinite(maxAgeHours) || maxAgeHours <= 0) {
    return true;
  }

  const publishedMs = item?.publishedAt ? new Date(item.publishedAt).getTime() : NaN;
  if (!Number.isFinite(publishedMs)) {
    return false;
  }

  const maxAgeMs = maxAgeHours * 60 * 60 * 1000;
  return publishedMs <= nowMs && (nowMs - publishedMs) <= maxAgeMs;
}

function tokenizeTitle(title = '') {
  return String(title || '')
    .toLowerCase()
    .replace(/https?:\/\/\S+/g, ' ')
    .replace(/[^a-z0-9\s]/g, ' ')
    .split(/\s+/)
    .map(token => token.trim())
    .filter(token => token.length >= 3 && !TITLE_STOPWORDS.has(token));
}

function normalizeTitle(title = '') {
  return tokenizeTitle(title).join(' ');
}

function countSharedTokens(a, b) {
  const set = new Set(a);
  let shared = 0;

  for (const token of b) {
    if (set.has(token)) {
      shared += 1;
    }
  }

  return shared;
}

function isLikelySameStory(item, cluster) {
  const normalizedTitle = normalizeTitle(item.title);
  if (!normalizedTitle) {
    return false;
  }

  if (cluster.normalizedTitles.has(normalizedTitle)) {
    return true;
  }

  const itemTokens = tokenizeTitle(item.title);
  const itemTime = toTimestamp(item.publishedAt);

  for (const existingItem of cluster.items) {
    const existingNormalizedTitle = normalizeTitle(existingItem.title);
    const existingTokens = tokenizeTitle(existingItem.title);
    const existingTime = toTimestamp(existingItem.publishedAt);
    const withinTimeWindow = !itemTime || !existingTime || Math.abs(itemTime - existingTime) <= 48 * 60 * 60 * 1000;

    if (!withinTimeWindow) {
      continue;
    }

    if (
      existingNormalizedTitle &&
      Math.min(existingNormalizedTitle.length, normalizedTitle.length) >= 20 &&
      (existingNormalizedTitle.includes(normalizedTitle) || normalizedTitle.includes(existingNormalizedTitle))
    ) {
      return true;
    }

    const shared = countSharedTokens(itemTokens, existingTokens);
    const unionSize = new Set([...itemTokens, ...existingTokens]).size || 1;
    const jaccard = shared / unionSize;

    if ((shared >= 5 && jaccard >= 0.42) || (shared >= 4 && jaccard >= 0.58)) {
      return true;
    }
  }

  return false;
}

function getSourcePriority(source = '') {
  return SOURCE_PRIORITY[String(source || '').trim()] || SOURCE_PRIORITY.Other;
}

function pickPrimaryStoryItem(items) {
  return [...(items || [])].sort((a, b) => {
    const priorityGap = getSourcePriority(b.source) - getSourcePriority(a.source);
    if (priorityGap !== 0) return priorityGap;

    const timeGap = toTimestamp(b.publishedAt) - toTimestamp(a.publishedAt);
    if (timeGap !== 0) return timeGap;

    const scoreGap = (b.score || 0) - (a.score || 0);
    if (scoreGap !== 0) return scoreGap;

    return String(b.description || '').length - String(a.description || '').length;
  })[0];
}

function mergeStoryCluster(cluster) {
  const primary = pickPrimaryStoryItem(cluster.items);
  const latestItem = [...cluster.items].sort((a, b) => toTimestamp(b.publishedAt) - toTimestamp(a.publishedAt))[0] || primary;
  const sources = uniqueStrings(cluster.items.map(item => item.source)).sort((a, b) => {
    const priorityGap = getSourcePriority(b) - getSourcePriority(a);
    if (priorityGap !== 0) return priorityGap;
    return a.localeCompare(b);
  });
  const sourceUrls = uniqueStrings(cluster.items.map(item => item.sourceUrl));
  const titleVariants = uniqueStrings(cluster.items.map(item => item.title));
  const descriptions = uniqueStrings(cluster.items.map(item => item.description)).slice(0, 3);

  return {
    ...primary,
    source: primary.source,
    sourceUrl: primary.sourceUrl,
    title: primary.title,
    description: descriptions.join(' '),
    publishedAt: latestItem.publishedAt || primary.publishedAt,
    score: Math.max(...cluster.items.map(item => Number(item.score) || 0)),
    sources,
    sourceUrls,
    titleVariants,
    descriptions,
    corroborationCount: sourceUrls.length,
  };
}

function getSourceFromUrl(url = '') {
  try {
    const hostname = new URL(url).hostname.toLowerCase();

    if (hostname.includes('reuters.com')) return 'Reuters';
    if (hostname.includes('bloomberg.com')) return 'Bloomberg';
    if (hostname.includes('cnbc.com')) return 'CNBC';
    if (hostname.includes('wsj.com')) return 'WSJ';
    if (hostname.includes('apnews.com')) return 'AP';
    if (hostname.includes('barrons.com')) return 'Barrons';
    if (hostname.includes('axios.com')) return 'Axios';
    if (hostname.includes('businessinsider.com')) return 'Business Insider';
    if (hostname.includes('cnn.com')) return 'CNN Business';
    if (hostname.includes('nikkei.com')) return 'Nikkei';
    if (hostname.includes('theglobeandmail.com')) return 'Globe and Mail';
    if (hostname.includes('federalreserve.gov')) return 'Fed';
    if (hostname.includes('ecb.europa.eu')) return 'ECB';
    if (hostname.includes('bankofengland.co.uk')) return 'BOE';
    if (hostname.includes('boj.or.jp')) return 'BOJ';
    if (hostname.includes('bankofcanada.ca')) return 'BoC';
    if (hostname.includes('bis.org')) return 'BIS';
    if (hostname.includes('opec.org')) return 'OPEC';
    if (hostname.includes('eia.gov')) return 'EIA';
    if (hostname.includes('bls.gov')) return 'BLS';
    if (hostname.includes('bea.gov')) return 'BEA';
    if (hostname.includes('treasury.gov')) return 'Treasury';
    if (hostname.includes('oecd.org')) return 'OECD';
    if (hostname.includes('imf.org')) return 'IMF';
    if (hostname.includes('worldbank.org')) return 'World Bank';
    if (hostname.includes('ft.com')) return 'FT';
    if (hostname.includes('marketwatch.com')) return 'MarketWatch';
    if (hostname.includes('finance.yahoo.com')) return 'Yahoo Finance';
    if (hostname.includes('investing.com')) return 'Investing';

    return 'Other';
  } catch (_error) {
    return 'Other';
  }
}

function normalizeResult(item, fallbackQuery) {
  return {
    source: getSourceFromUrl(item.url),
    sourceUrl: item.url,
    title: (item.title || '').trim(),
    description: (item.content || item.raw_content || '').trim(),
    publishedAt: item.published_date || item.publishedAt || null,
    score: item.score || 0,
    query: fallbackQuery,
  };
}

async function searchNews(query, options = {}) {
  const apiKey = options.apiKey || config.news.tavily.apiKey;

  if (!apiKey) {
    throw new Error('TAVILY_API_KEY is not configured');
  }

  const payload = {
    api_key: apiKey,
    query,
    topic: 'news',
    search_depth: options.searchDepth || 'advanced',
    max_results: options.maxResults || 10,
    include_answer: false,
    include_raw_content: false,
    include_domains: options.includeDomains || TRUSTED_DOMAINS,
  };

  const response = await axios.post(TAVILY_ENDPOINT, payload, {
    timeout: options.timeout || 20000,
  });

  const results = Array.isArray(response.data?.results) ? response.data.results : [];
  return results.map(item => normalizeResult(item, query));
}

async function fetchGlobalFinancialNews(options = {}) {
  const queries = options.queries || DEFAULT_QUERIES;
  const maxResultsPerQuery = options.maxResultsPerQuery || 10;
  const maxItems = options.limit || 50;
  const parsedMaxAgeHours = Number(options.maxAgeHours);
  const maxAgeHours = Number.isFinite(parsedMaxAgeHours) ? parsedMaxAgeHours : 48;
  const nowMs = options.now instanceof Date && Number.isFinite(options.now.getTime())
    ? options.now.getTime()
    : Date.now();

  const settled = await Promise.allSettled(
    queries.map(query =>
      searchNews(query, {
        maxResults: maxResultsPerQuery,
        searchDepth: options.searchDepth,
        includeDomains: options.includeDomains,
      })
    )
  );

  const batches = [];
  const errors = [];
  for (const item of settled) {
    if (item.status === 'fulfilled') {
      batches.push(item.value);
    } else {
      errors.push(item.reason?.message || 'Tavily search failed');
      batches.push([]);
    }
  }

  const deduped = new Map();
  for (const batch of batches) {
    for (const item of batch) {
      if (!item.title || !item.sourceUrl) {
        continue;
      }

      const existing = deduped.get(item.sourceUrl);
      if (!existing || (Number(item.score) || 0) > (Number(existing.score) || 0)) {
        deduped.set(item.sourceUrl, item);
      }
    }
  }

  const storyClusters = [];
  for (const item of deduped.values()) {
    const cluster = storyClusters.find(candidate => isLikelySameStory(item, candidate));
    if (cluster) {
      cluster.items.push(item);
      const normalizedTitle = normalizeTitle(item.title);
      if (normalizedTitle) {
        cluster.normalizedTitles.add(normalizedTitle);
      }
    } else {
      storyClusters.push({
        items: [item],
        normalizedTitles: new Set([normalizeTitle(item.title)].filter(Boolean)),
      });
    }
  }

  const allItems = storyClusters.map(cluster => mergeStoryCluster(cluster));
  allItems.sort((a, b) => {
    const aTime = a.publishedAt ? new Date(a.publishedAt).getTime() : 0;
    const bTime = b.publishedAt ? new Date(b.publishedAt).getTime() : 0;

    if (aTime === bTime) {
      const corroborationGap = (b.corroborationCount || 1) - (a.corroborationCount || 1);
      if (corroborationGap !== 0) {
        return corroborationGap;
      }

      return (b.score || 0) - (a.score || 0);
    }

    return bTime - aTime;
  });

  if (allItems.length === 0 && errors.length > 0) {
    throw new Error(`Unable to fetch news from Tavily: ${errors[0]}`);
  }

  return allItems
    .filter(item => isFreshNewsItem(item, maxAgeHours, nowMs))
    .slice(0, maxItems);
}

module.exports = {
  TAVILY_ENDPOINT,
  TRUSTED_DOMAINS,
  DEFAULT_QUERIES,
  searchNews,
  fetchGlobalFinancialNews,
};
