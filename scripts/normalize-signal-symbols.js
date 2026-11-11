#!/usr/bin/env node
require('dotenv').config({ path: require('path').join(__dirname, '..', '.env'), quiet: true });

const { getSchema, updateSignalFields } = require('../src/news-trade-db');
const { normalizeSignalSymbols } = require('../src/analysis/symbolSelector');
const { inferSymbols } = require('../src/analysis/newsSignalGenerator');

function rawSymbols(page, propertyName) {
  return (page.properties?.[propertyName]?.multi_select || []).map(item => item.name);
}

async function queryDatabase(databaseId, startCursor) {
  const response = await fetch(`https://api.notion.com/v1/databases/${databaseId}/query`, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${process.env.NOTION_TOKEN}`,
      'Content-Type': 'application/json',
      'Notion-Version': '2022-06-28',
    },
    body: JSON.stringify({ page_size: 100, start_cursor: startCursor }),
  });

  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.message || 'Unable to query Notion database');
  }

  return data;
}

async function main() {
  const schema = await getSchema();
  const pages = [];
  let cursor;

  do {
    const response = await queryDatabase(schema.databaseId, cursor);
    pages.push(...response.results.filter(page => !page.archived));
    cursor = response.has_more ? response.next_cursor : undefined;
  } while (cursor);

  const changed = [];
  const failed = [];

  for (const page of pages) {
    const event = page.properties?.[schema.map.event]?.title?.map(item => item.plain_text || '').join('') || '';
    const details = page.properties?.[schema.map.details]?.rich_text?.map(item => item.plain_text || '').join('') || '';
    const original = rawSymbols(page, schema.map.symbol);
    const inferred = inferSymbols(event || details || '');
    const normalized = normalizeSignalSymbols([...original, ...inferred], { text: event || details || '' });

    if (JSON.stringify(original) === JSON.stringify(normalized)) {
      continue;
    }

    try {
      await updateSignalFields(page.id, {
        event,
        details,
        symbols: normalized,
      });
      changed.push({ id: page.id, event, original, normalized });
    } catch (error) {
      failed.push({ id: page.id, event, original, normalized, error: error.message });
    }
  }

  console.log(JSON.stringify({
    success: true,
    checked: pages.length,
    updated: changed.length,
    failed: failed.length,
    examples: changed.slice(0, 15),
    failures: failed,
  }, null, 2));
}

main().catch(error => {
  console.error(JSON.stringify({ success: false, error: error.message }, null, 2));
  process.exit(1);
});
