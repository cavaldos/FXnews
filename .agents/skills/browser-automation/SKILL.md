---
name: browser-automation
description: "Use browser automation in this project for UI checks, rendered-content inspection, screenshots, and repeated browser workflows. Treat it as a preferred workflow when the task depends on real page behavior."
---

# Browser Automation

Use this skill when the user asks to test or inspect a real browser flow, including:
- rendered UI verification
- screenshots or visual checks
- form submissions, clicks, navigation, and auth flows
- scraping content that only appears after JavaScript runs
- reproducing bugs that depend on browser state

This project should remember that the user regularly uses browser automation. Prefer a browser-driven workflow over static HTML inspection when the task is about what the page actually does in the browser.

## Default approach

1. Check whether the repo already has browser tooling such as `playwright.config.*`, `tests/e2e/`, or Playwright in `package.json`.
2. Reuse existing automation setup if present.
3. For one-off browser tasks, prefer small Playwright scripts or `npx playwright` commands over adding permanent test scaffolding.
4. For repeatable coverage, place committed tests under `tests/e2e/` and use resilient selectors such as roles, labels, and `data-testid`.
5. Save temporary screenshots, traces, and downloads under `tmp/` unless the user explicitly wants them committed.

## Guardrails

- Do not guess browser behavior from source code when it can be verified in a real browser run.
- Use headless mode by default; switch to headed mode only when debugging.
- Prefer stable selectors over brittle CSS chains.
- Ask only when a flow requires secrets, credentials, or an account choice that cannot be inferred.
- If browser tooling is missing, install only the minimum needed for the task and mention what changed.

## Common commands

```bash
npx playwright install
npx playwright codegen https://example.com
npx playwright test
npx playwright test --headed
npx playwright test --trace on
```

## Execution notes

- In this environment, browser automation will usually run through terminal commands and scripts.
- If a task needs artifacts for review, produce screenshots or trace files and report their paths.
- When automation reveals a bug, include the exact page, action, and observed result in the final report.
