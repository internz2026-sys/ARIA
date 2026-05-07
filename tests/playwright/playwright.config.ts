import { defineConfig, devices } from "@playwright/test";

/**
 * ARIA Playwright E2E config.
 *
 * Base URL: PLAYWRIGHT_BASE_URL env var (default: prod URL).
 * Run locally:  cd tests/playwright && npx playwright test
 * Run against local dev: PLAYWRIGHT_BASE_URL=http://localhost:3000 npx playwright test
 */

const BASE_URL =
  process.env.PLAYWRIGHT_BASE_URL ?? "https://aria.hoversight.agency";

export default defineConfig({
  testDir: "./specs",
  fullyParallel: false,            // onboarding tests mutate DB state — keep serial
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,

  reporter: [
    ["list"],
    ["html", { outputFolder: "playwright-report", open: "never" }],
  ],

  use: {
    baseURL: BASE_URL,
    headless: true,
    viewport: { width: 1280, height: 800 },
    screenshot: "only-on-failure",
    video: "retain-on-failure",
    trace: "on-first-retry",
    // Give generous timeouts — CEO chat + agent delegation can be slow
    actionTimeout: 15_000,
    navigationTimeout: 30_000,
  },

  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],

  // Output dirs
  outputDir: "test-results",
});
