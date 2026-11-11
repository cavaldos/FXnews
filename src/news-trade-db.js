const { notion } = require('./index');
const { deriveRecommendedAction, normalizeBiasKey } = require('./analysis/recommendedAction');
const { normalizeSignalSymbols } = require('./analysis/symbolSelector');
const { inferSymbols } = require('./analysis/newsSignalGenerator');

const DEFAULT_DATABASE_ID = 'b64498d9-1fdc-40dd-97ac-d17935103576';
const config = require('./config');
const CONFIGURED_DATABASE_ID = config.notion.databaseId || '';
const DATABASE_ID = CONFIGURED_DATABASE_ID || DEFAULT_DATABASE_ID;
const NOTION_TOKEN = process.env.NOTION_TOKEN;

const PROPERTY_ALIASES = {
  event: ['Event', 'Ten su kien', 'Tên sự kiện', 'Name', 'Title'],
  symbol: ['Symbol', 'Ma co phieu', 'Mã cổ phiếu', 'Symbols'],
  time: ['Time (ET)', 'Thoi gian (US)', 'Thời gian (US)', 'Time'],
  bias: ['Bias'],
  impact: ['Impact', 'Muc do anh huong', 'Mức độ ảnh hưởng'],
  recommendedAction: ['Recommended Action'],
  check: ['Check'],
  url: ['Source URL', 'Link lien quan', 'Link liên quan'],
  result: ['Result'],
  details: ['Details', 'Thong tin chi tiet', 'Thông tin chi tiết', 'Description'],
  status: ['Status', 'Trang thai', 'Trạng thái'],
};

let cachedSchema = null;

function isHoldAction(action = '') {
  return String(action || '').trim().toLowerCase().includes('hold');
}

function isDirectionalSignal(input = {}) {
  const biasKey = normalizeBiasKey(input.bias);
  return biasKey !== 'neutral' && !isHoldAction(input.recommendedAction);
}

function assertDirectionalSignal(input = {}) {
  if (isDirectionalSignal(input)) {
    return;
  }

  throw new Error('Neutral/Hold signals are not allowed. Skip saving until the bias is directional.');
}

function hasNewsTradeFields(map) {
  return Boolean(
    map.symbol ||
    map.bias ||
    map.impact ||
    map.recommendedAction ||
    map.url ||
    map.details ||
    map.status
  );
}

async function fetchDatabaseProperties(databaseId) {
  const response = await fetch(`https://api.notion.com/v1/databases/${databaseId}`, {
    method: 'GET',
    headers: {
      Authorization: `Bearer ${NOTION_TOKEN}`,
      'Content-Type': 'application/json',
      'Notion-Version': '2022-06-28',
    },
  });

  if (!response.ok) {
    const err = await response.json();
    throw new Error(err.message || `Unable to retrieve Notion schema for ${databaseId}`);
  }

  const payload = await response.json();
  return payload.properties || {};
}

function pickProperty(properties, aliases, expectedType) {
  const entries = Object.entries(properties || {});

  for (const alias of aliases) {
    const found = entries.find(([name, meta]) => {
      if (name.toLowerCase() !== alias.toLowerCase()) {
        return false;
      }
      return expectedType ? meta.type === expectedType : true;
    });

    if (found) {
      return found[0];
    }
  }

  return null;
}

function buildSchema(properties, databaseId) {
  const map = {
    event: pickProperty(properties, PROPERTY_ALIASES.event, 'title'),
    symbol: pickProperty(properties, PROPERTY_ALIASES.symbol, 'multi_select'),
    time: pickProperty(properties, PROPERTY_ALIASES.time, 'date'),
    bias: pickProperty(properties, PROPERTY_ALIASES.bias, 'select'),
    impact: pickProperty(properties, PROPERTY_ALIASES.impact, 'select'),
    recommendedAction: pickProperty(properties, PROPERTY_ALIASES.recommendedAction, 'select'),
    check: pickProperty(properties, PROPERTY_ALIASES.check, 'select'),
    url: pickProperty(properties, PROPERTY_ALIASES.url, 'url'),
    result: pickProperty(properties, PROPERTY_ALIASES.result, 'rich_text'),
    details: pickProperty(properties, PROPERTY_ALIASES.details, 'rich_text'),
    status: pickProperty(properties, PROPERTY_ALIASES.status, 'select'),
  };

  return {
    databaseId,
    properties,
    map,
  };
}

async function getSchema() {
  if (cachedSchema) {
    return cachedSchema;
  }

  const candidates = [...new Set([CONFIGURED_DATABASE_ID, DEFAULT_DATABASE_ID].filter(Boolean))];
  let lastError = null;

  for (const databaseId of candidates) {
    try {
      const properties = await fetchDatabaseProperties(databaseId);
      const schema = buildSchema(properties, databaseId);

      if (schema.map.event) {
        cachedSchema = schema;
        return cachedSchema;
      }

      lastError = new Error(`Database ${databaseId} does not contain a title column for events`);
    } catch (error) {
      lastError = error;
    }
  }

  throw new Error(lastError?.message || 'Unable to resolve a valid News Trade Signals database');
}

async function queryDatabase(databaseId, body = {}) {
  const res = await fetch(`https://api.notion.com/v1/databases/${databaseId}/query`, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${NOTION_TOKEN}`,
      'Content-Type': 'application/json',
      'Notion-Version': '2022-06-28',
    },
    body: JSON.stringify(body),
  });

  if (!res.ok) {
    const err = await res.json();
    throw new Error(err.message || 'Unable to query Notion database');
  }

  return res.json();
}

async function addSignal({ event, symbols, time, bias, impact, recommendedAction, url, details, status, sourceUrls, sources }) {
  const schema = await getSchema();

  if (!schema.map.event) {
    throw new Error('Target Notion database must have an Event/Name title property.');
  }

  const properties = {};
  const inferredSymbols = inferSymbols(event || details || '');
  const explicitSymbols = Array.isArray(symbols) ? symbols.filter(Boolean) : [];
  const canonicalSymbols = normalizeSignalSymbols(
    explicitSymbols.length ? explicitSymbols : inferredSymbols,
    { text: event || details || '' }
  );

  properties[schema.map.event] = {
    title: [{ text: { content: event || 'Trading Event' } }],
  };

  if (schema.map.symbol && canonicalSymbols.length) {
    properties[schema.map.symbol] = {
      multi_select: canonicalSymbols.map(symbol => ({ name: symbol.toUpperCase() })),
    };
  }

  if (schema.map.bias && bias) {
    properties[schema.map.bias] = { select: { name: bias } };
  }

  if (schema.map.time && time) {
    properties[schema.map.time] = {
      date: { start: time, time_zone: 'America/New_York' },
    };
  }

  if (schema.map.impact && impact) {
    properties[schema.map.impact] = { select: { name: impact } };
  }

  const derivedRecommendedAction = recommendedAction || deriveRecommendedAction({
    bias,
    impact,
    corroborationCount: Math.max(1, (sourceUrls || []).length || (sources || []).length || 1),
  });

  assertDirectionalSignal({ bias, recommendedAction: derivedRecommendedAction });

  if (schema.map.recommendedAction && derivedRecommendedAction) {
    properties[schema.map.recommendedAction] = { select: { name: derivedRecommendedAction } };
  }

  if (schema.map.url && url) {
    properties[schema.map.url] = { url };
  }

  if (schema.map.details && details) {
    properties[schema.map.details] = {
      rich_text: [{ text: { content: details.slice(0, 2000) } }],
    };
  }

  if (schema.map.status) {
    properties[schema.map.status] = { select: { name: status || '📋 New' } };
  }

  return notion.pages.create({
    parent: { type: 'database_id', database_id: schema.databaseId },
    properties,
  });
}

function getPropertyValue(page, propName) {
  if (!propName) return null;

  const prop = page.properties[propName];
  if (!prop) return null;

  if (prop.type === 'title') {
    return prop.title?.[0]?.plain_text || '';
  }

  if (prop.type === 'multi_select') {
    return (prop.multi_select || []).map(item => item.name);
  }

  if (prop.type === 'date') {
    return prop.date?.start || null;
  }

  if (prop.type === 'select') {
    return prop.select?.name || '';
  }

  if (prop.type === 'url') {
    return prop.url || '';
  }

  if (prop.type === 'rich_text') {
    return prop.rich_text?.[0]?.plain_text || '';
  }

  if (prop.type === 'number') {
    return prop.number;
  }

  return null;
}

function parseSignal(page, schema) {
  const event = getPropertyValue(page, schema.map.event) || '';
  const details = getPropertyValue(page, schema.map.details) || '';
  const inferredSymbols = inferSymbols(event || details || '');
  const storedSymbols = getPropertyValue(page, schema.map.symbol) || [];
  const canonicalSymbols = normalizeSignalSymbols(
    storedSymbols.length ? storedSymbols : inferredSymbols,
    { text: event || details || '' }
  );

  return {
    id: page.id,
    event,
    symbols: canonicalSymbols,
    time: getPropertyValue(page, schema.map.time),
    bias: getPropertyValue(page, schema.map.bias) || '',
    impact: getPropertyValue(page, schema.map.impact) || '',
    recommendedAction: getPropertyValue(page, schema.map.recommendedAction) || '',
    check: getPropertyValue(page, schema.map.check) || '',
    url: getPropertyValue(page, schema.map.url) || '',
    result: getPropertyValue(page, schema.map.result) || '',
    details,
    status: getPropertyValue(page, schema.map.status) || '',
  };
}

async function getSignals({ filter, sorts, pageSize = 20 } = {}) {
  const schema = await getSchema();
  const body = { page_size: pageSize };

  if (filter) {
    body.filter = filter;
  }

  if (sorts) {
    body.sorts = sorts;
  } else if (schema.map.time) {
    body.sorts = [{ property: schema.map.time, direction: 'descending' }];
  }

  let response;
  try {
    response = await queryDatabase(schema.databaseId, body);
  } catch (error) {
    const sortError = (error.message || '').toLowerCase().includes('sort property');
    if (sortError && body.sorts) {
      delete body.sorts;
      response = await queryDatabase(schema.databaseId, body);
    } else {
      throw error;
    }
  }

  return response.results.map(page => parseSignal(page, schema));
}

async function updateSignalStatus(pageId, status) {
  const schema = await getSchema();
  if (!schema.map.status) {
    throw new Error('Status property not found in target Notion database');
  }

  return notion.pages.update({
    page_id: pageId,
    properties: {
      [schema.map.status]: { select: { name: status } },
    },
  });
}

async function updateSignalFields(pageId, updates) {
  const schema = await getSchema();
  const properties = {};
  let currentPage = null;

  if (Object.prototype.hasOwnProperty.call(updates, 'bias') && normalizeBiasKey(updates.bias) === 'neutral') {
    throw new Error('Neutral/Hold signals are not allowed. Skip saving until the bias is directional.');
  }

  if (Object.prototype.hasOwnProperty.call(updates, 'recommendedAction') && isHoldAction(updates.recommendedAction)) {
    throw new Error('Neutral/Hold signals are not allowed. Skip saving until the bias is directional.');
  }

  if (
    schema.map.recommendedAction &&
    !updates.recommendedAction &&
    (updates.bias || updates.impact) &&
    (!updates.bias || !updates.impact)
  ) {
    currentPage = await notion.pages.retrieve({ page_id: pageId });
  }

  if (schema.map.event && updates.event != null) {
    properties[schema.map.event] = {
      title: [{ text: { content: String(updates.event || 'Trading Event').slice(0, 2000) } }],
    };
  }

  if (schema.map.symbol && Array.isArray(updates.symbols)) {
    const inferredSymbols = inferSymbols(updates.event || updates.details || '');
    const explicitSymbols = Array.isArray(updates.symbols) ? updates.symbols.filter(Boolean) : [];
    const canonicalSymbols = normalizeSignalSymbols(
      explicitSymbols.length ? explicitSymbols : inferredSymbols,
      {
        text: updates.event || updates.details || '',
      }
    );
    properties[schema.map.symbol] = {
      multi_select: canonicalSymbols
        .map(symbol => String(symbol || '').trim().toUpperCase())
        .filter(Boolean)
        .slice(0, 3)
        .map(name => ({ name })),
    };
  }

  if (schema.map.time && updates.time) {
    properties[schema.map.time] = {
      date: { start: updates.time, time_zone: 'America/New_York' },
    };
  }

  if (schema.map.bias && updates.bias) {
    properties[schema.map.bias] = {
      select: { name: updates.bias },
    };
  }

  if (schema.map.impact && updates.impact) {
    properties[schema.map.impact] = {
      select: { name: updates.impact },
    };
  }

  const effectiveBias = updates.bias || (currentPage ? getPropertyValue(currentPage, schema.map.bias) : null);
  const effectiveImpact = updates.impact || (currentPage ? getPropertyValue(currentPage, schema.map.impact) : null);
  const nextRecommendedAction = updates.recommendedAction || (
    schema.map.recommendedAction && effectiveBias && effectiveImpact
      ? deriveRecommendedAction({ bias: effectiveBias, impact: effectiveImpact })
      : null
  );

  if (schema.map.recommendedAction && nextRecommendedAction) {
    properties[schema.map.recommendedAction] = {
      select: { name: nextRecommendedAction },
    };
  }

  if (schema.map.check && Object.prototype.hasOwnProperty.call(updates, 'check')) {
    properties[schema.map.check] = {
      select: updates.check ? { name: updates.check } : null,
    };
  }

  if (schema.map.url && updates.url) {
    properties[schema.map.url] = { url: updates.url };
  }

  if (schema.map.result && Object.prototype.hasOwnProperty.call(updates, 'result')) {
    const resultText = String(updates.result || '').slice(0, 2000);
    properties[schema.map.result] = {
      rich_text: resultText ? [{ text: { content: resultText } }] : [],
    };
  }

  if (schema.map.details && updates.details != null) {
    const detailText = String(updates.details || '').slice(0, 2000);
    properties[schema.map.details] = {
      rich_text: detailText ? [{ text: { content: detailText } }] : [],
    };
  }

  if (schema.map.status && updates.status) {
    properties[schema.map.status] = {
      select: { name: updates.status },
    };
  }

  if (Object.keys(properties).length === 0) {
    throw new Error('No valid lean-schema properties to update');
  }

  return notion.pages.update({
    page_id: pageId,
    properties,
  });
}

async function getNewSignals() {
  const signals = await getSignals({ pageSize: 100 });
  return signals.filter(item => /new|moi/i.test(item.status || ''));
}

async function getBullishSignals() {
  const signals = await getSignals({ pageSize: 100 });
  return signals.filter(item => /bullish|tang/i.test(item.bias || ''));
}

async function getBearishSignals() {
  const signals = await getSignals({ pageSize: 100 });
  return signals.filter(item => /bearish|giam/i.test(item.bias || ''));
}

async function getResolvedDatabaseId() {
  const schema = await getSchema();
  return schema.databaseId;
}

module.exports = {
  DATABASE_ID,
  DEFAULT_DATABASE_ID,
  addSignal,
  getSignals,
  isDirectionalSignal,
  updateSignalStatus,
  updateSignalFields,
  getNewSignals,
  getBullishSignals,
  getBearishSignals,
  getResolvedDatabaseId,
  getSchema,
};

if (require.main === module) {
  (async () => {
    console.log('Querying signals...\n');
    const signals = await getSignals();
    console.log(`Found ${signals.length} signals:`);
    signals.forEach(signal => {
      console.log(`  - ${signal.event} | ${signal.symbols.join(', ')} | ${signal.bias} | ${signal.impact}`);
    });
  })();
}
