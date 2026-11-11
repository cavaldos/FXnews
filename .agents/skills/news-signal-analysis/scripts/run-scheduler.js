#!/usr/bin/env node
require('dotenv').config({ path: require('path').join(__dirname, '..', '..', '..', '..', '.env'), quiet: true });

const schedule = require('node-schedule');
const { runTomorrowBriefPipeline } = require('../../../../src/pipeline/tomorrowBriefPipeline');

const cron = process.argv[2] || '*/30 * * * *';

async function execute() {
  const startedAt = new Date().toISOString();

  try {
    const result = await runTomorrowBriefPipeline({
      includeCalendar: true,
      includeNews: false,
      saveCalendar: true,
      saveNews: false,
      calendarMinImpact: process.argv[3] || 'medium',
      fetchLimit: 60,
      maxSignals: 20,
      maxResultsPerQuery: 12,
      existingLimit: 400,
      searchDepth: 'advanced',
    });

    if (result.calendar?.success === false) {
      throw new Error(result.calendar.error || 'Calendar collection failed');
    }

    console.log(JSON.stringify({
      success: true,
      startedAt,
      finishedAt: new Date().toISOString(),
      dateEt: result.dateEt,
      source: result.calendar?.source || null,
      fetchedCount: result.calendar?.fetchedCount || 0,
      upcomingCount: result.calendar?.dayCount || 0,
      generatedCount: result.calendar?.generatedCount || 0,
      savedCount: result.calendar?.savedCount || 0,
      skippedCount: result.calendar?.skippedCount || 0,
    }));
  } catch (error) {
    console.error(JSON.stringify({
      success: false,
      startedAt,
      finishedAt: new Date().toISOString(),
      error: error.message,
    }));
  }
}

console.log(`News trade scheduler started. Cron: ${cron}`);
execute();
schedule.scheduleJob(cron, execute);
