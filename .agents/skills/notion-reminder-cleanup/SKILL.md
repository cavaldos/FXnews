---
name: notion-reminder-cleanup
description: "Clear expired native Notion date reminders by exporting Brave cookies and using Playwright to set Remind -> None."
---

# Notion Reminder Cleanup

Use this skill when the goal is to remove past-due native Notion reminders from the `Time (ET)` date property.

This skill exists because the official Notion API does not expose a reminder field on date properties. The workflow must:
- query expired items from Notion
- open the editable page in a browser session
- change `Remind` to `None`
- verify the alarm icon disappears

## When to use it

- a page still shows the red alarm icon after the event time has passed
- you want a one-shot cleanup of old reminders
- you want a dry run list before changing anything
- you need a report of how many reminders were cleared vs already absent

## Prerequisites

- Brave is installed on this machine
- Brave is already logged into the target Notion workspace
- the project has access to the Notion database through `NOTION_TOKEN`

## Commands

Primary one-shot command:

```bash
npm run signals:clear-reminders
```

Useful variants:

```bash
node .agents/skills/notion-reminder-cleanup/scripts/clear-reminders.js --dry-run
node .agents/skills/notion-reminder-cleanup/scripts/clear-reminders.js --limit 5
node .agents/skills/notion-reminder-cleanup/scripts/clear-reminders.js --id <pageId>
node .agents/skills/notion-reminder-cleanup/scripts/clear-reminders.js --skip-cookie-refresh
npm run signals:clear-reminders-report
```

## Workflow

### 1. Load expired signals

Use the existing Notion integration to fetch pages and filter where `Time (ET)` is already in the past.

### 2. Refresh browser auth

Export current Notion cookies from Brave into `tmp/notion-brave-cookies.json`.

This happens automatically inside the main cleanup script unless `--skip-cookie-refresh` is passed.

### 3. Open each page in an editable browser session

Use Playwright with the imported cookies to open the real Notion page, not the public read-only site.

### 4. Remove the reminder

For each page:
- dismiss the `Open in Notion's desktop app` prompt if it appears
- open the `Time (ET)` date editor
- open the `Remind` menu
- choose `None`

### 5. Verify the result

Reload the page and confirm the alarm icon is gone.

If a step fails, save a screenshot under `tmp/notion-reminder-cleaner/`.

### 6. Report the run

Each run emits JSON with:
- `checked`
- `cleared`
- `skippedNoAlarm`
- `failed`
- per-page statuses

The latest log can be summarized with:

```bash
npm run signals:clear-reminders-report
```

## Failure handling

- if cookie export fails, refresh the Brave login and rerun
- if a page is already clean, mark it as `skipped_no_alarm`
- if Notion changes the reminder UI, inspect screenshots in `tmp/notion-reminder-cleaner/`
- prefer rerunning one `--id <pageId>` first when debugging a single failure

## File Map

- Main cleanup script: `scripts/clear-past-reminders.js`
- Brave cookie export: `scripts/export-brave-notion-cookies.py`
- Skill wrapper: `.agents/skills/notion-reminder-cleanup/scripts/clear-reminders.js`
- Skill report helper: `.agents/skills/notion-reminder-cleanup/scripts/report-last-run.js`
- Run log: `tmp/clear-reminders.log`
- Failure screenshots: `tmp/notion-reminder-cleaner/`

Base directory for this skill: file:///Users/bourbon/Code/Workspace/NewsTrade/.agents/skills/notion-reminder-cleanup
