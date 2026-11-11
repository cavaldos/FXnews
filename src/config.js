require('dotenv').config({ path: require('path').join(__dirname, '..', '.env'), quiet: true });

function parseNotionDatabaseId(input) {
  if (!input) return undefined;
  input = input.trim();

  // If it's a Notion URL, extract the 32-char hex ID from the path
  if (input.startsWith('http')) {
    const match = input.match(/([a-f0-9]{32})/i);
    if (match) {
      const raw = match[1];
      return `${raw.slice(0,8)}-${raw.slice(8,12)}-${raw.slice(12,16)}-${raw.slice(16,20)}-${raw.slice(20)}`;
    }
    return undefined;
  }

  // Raw 32-char hex ID without dashes → format as UUID
  if (/^[a-f0-9]{32}$/i.test(input)) {
    return `${input.slice(0,8)}-${input.slice(8,12)}-${input.slice(12,16)}-${input.slice(16,20)}-${input.slice(20)}`;
  }

  // Already a UUID with dashes
  return input;
}

const notionDbInput = process.env.NOTION_DATABASE_URL || process.env.NOTION_DATABASE_ID;

module.exports = {
  notion: {
    token: process.env.NOTION_TOKEN,
    databaseId: parseNotionDatabaseId(notionDbInput),
  },
  news: {
    tavily: {
      apiKey: process.env.TAVILY_API_KEY || process.env.TAVILY_API || process.env.TAVILY_KEY || process.env.TAVILY,
    },
    forexfactory: {
      calendarUrl: 'https://www.forexfactory.com/calendar',
    },
    bloomberg: {
      apiKey: process.env.BLOOMBERG_API_KEY,
    },
    reuters: {
      apiKey: process.env.REUTERS_API_KEY,
    },
    investing: {
      apiKey: process.env.INVESTING_API_KEY,
    },
  },
  analysis: {
    impactThreshold: 3,
    defaultTimezone: 'America/New_York',
  },
};
