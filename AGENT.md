# NewsTrade - Agent Guide

For AI coding agents (OpenCode, Claude Code, VSCode, etc.)

## Quick Start

```bash
# Install dependencies
npm install

# Run full pipeline (Phase 1 + Phase 3)
npm run signals:collect
npm run signals:verify
```

## Available Commands

| Command | Description | AI Required |
|---------|-------------|-------------|
| `npm run signals:dry-run` | Test without saving | ❌ |
| `npm run signals:collect` | Collect & save signals to Notion | ❌ |
| `npm run signals:verify` | Verify signals with real prices | ❌ |
| `npm run signals:verify-report` | Accuracy report | ❌ |
| `npm run signals:status` | Show signal inventory summary | ❌ |
| `npm run signals:result` | Prefetch OHLC, then write `Check`/`Result` | ❌ |

## Environment Variables

Create `.env`:
```
NOTION_TOKEN=your_token
TAVILY_API_KEY=your_key
```

## What It Does

The project is now being split conceptually into two agent skills:

1. **News Signal Analysis**: Fetches financial news/calendar items, maps them to symbols, and assigns a long/short bias via `Bias`
2. **News Signal Validation**: Checks historical calls by fetching symbol price data and comparing the post-news move to the original bias

Workflow layers currently live under the split skill folders:
- signal collection
- optional AI consensus analysis
- historical validation / intraday result checks

## For Agents

Just run these commands in sequence:
```bash
npm run signals:collect
npm run signals:verify
npm run signals:verify-report
```

No manual setup needed beyond `.env` file.
