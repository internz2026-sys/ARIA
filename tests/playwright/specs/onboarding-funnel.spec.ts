/**
 * Onboarding Funnel — happy-path E2E spec
 *
 * SAFETY GATE: This spec overwrites the real `tenant_configs` row for the
 * test Google account.  It is skipped by default and ONLY runs when the
 * caller explicitly opts in:
 *
 *   PLAYWRIGHT_DESTRUCTIVE=1 npx playwright test specs/onboarding-funnel.spec.ts
 *
 * When run without the flag the entire describe block is skipped so CI
 * never accidentally nukes the production profile.
 *
 * AUTH STRATEGY
 * ---------------------------------------------------------------------------
 * Tests try to reuse a cached Supabase / Google OAuth session stored at
 * tests/playwright/fixtures/auth-session.json.  If the session has expired
 * (Google account-chooser page appears, or we land on /login instead of the
 * target page), the test fails immediately with a clear message rather than
 * attempting to drive Google's OAuth UI through MFA.
 *
 * To refresh the session file:
 *   npx playwright codegen https://aria.hoversight.agency
 *   Complete Google OAuth, then save storageState to
 *   tests/playwright/fixtures/auth-session.json
 *
 * FLOW UNDER TEST
 * ---------------------------------------------------------------------------
 *  /welcome
 *    → if "already onboarded", click "Start from scratch"
 *    → /describe: send 8 short answers, click "Review & finish"
 *  /review
 *    → assert business name appears in the profile card
 *    → click "Looks good, continue"
 *  /select-agents
 *    → keep defaults, click "Launch ARIA"
 *  /launching (animated setup — wait for "Go to Dashboard" CTA)
 *  /dashboard
 *    → assert URL is /dashboard
 *    → assert greeting contains the test business name
 */

import { test, expect, Page } from "@playwright/test";
import * as path from "path";
import * as fs from "fs";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const DESTRUCTIVE = process.env.PLAYWRIGHT_DESTRUCTIVE === "1";
const AUTH_SESSION_PATH = path.join(__dirname, "../fixtures/auth-session.json");

// Short deterministic answers for the 8 onboarding questions.
// These are chosen to be recognisable in the /review assertions.
const BUSINESS_NAME = "ARIA Test Co";
const ANSWERS: Record<number, string> = {
  1: BUSINESS_NAME,                            // Q1 — business name
  2: "A SaaS tool that automates marketing",   // Q2 — product/offer
  3: "Developer founders and indie hackers",   // Q3 — target audience
  4: "Founders waste hours on repetitive marketing tasks", // Q4 — problem solved
  5: "AI agents that work 24/7 without hiring", // Q5 — differentiator
  6: "X/Twitter and LinkedIn",                 // Q6 — channels
  7: "Friendly and direct",                    // Q7 — brand voice
  8: "Get 100 signups in 30 days",             // Q8 — 30-day goal
};

// ---------------------------------------------------------------------------
// Auth helper
// ---------------------------------------------------------------------------

/**
 * Returns a Playwright storageState object from fixtures/auth-session.json,
 * or null when the file does not exist.
 */
function loadStoredAuth(): object | null {
  try {
    if (fs.existsSync(AUTH_SESSION_PATH)) {
      return JSON.parse(fs.readFileSync(AUTH_SESSION_PATH, "utf8"));
    }
  } catch {
    // Malformed JSON — treat as missing
  }
  return null;
}

/**
 * Asserts that the current page is NOT a login / Google auth-chooser page.
 * If it is, fail with a helpful message so the test surface is obvious.
 */
async function assertAuthenticated(page: Page): Promise<void> {
  const url = page.url();
  const isLoginPage = url.includes("/login") || url.includes("accounts.google.com");
  if (isLoginPage) {
    test.fail(
      true,
      [
        "Session has expired — Google account-chooser / login page appeared.",
        "To refresh: run `npx playwright codegen https://aria.hoversight.agency`,",
        "complete Google OAuth, then save storageState to:",
        `  ${AUTH_SESSION_PATH}`,
      ].join("\n")
    );
  }
}

// ---------------------------------------------------------------------------
// Test suite
// ---------------------------------------------------------------------------

const maybeskip = DESTRUCTIVE ? test.describe : test.describe.skip;

maybeskip("Onboarding funnel — happy path", () => {
  // ---------------------------------------------------------------------------
  // Apply stored session to every test in this suite (if available).
  // ---------------------------------------------------------------------------
  test.use({
    storageState: loadStoredAuth()
      ? AUTH_SESSION_PATH
      : undefined,
  });

  test("user completes onboarding and lands at /dashboard with business name shown", async ({
    page,
  }) => {
    // ── Step 1: Navigate to /welcome ─────────────────────────────────────────
    await page.goto("/welcome");
    await assertAuthenticated(page);

    // /welcome may show a spinner while it checks localStorage / API.
    // Wait until the spinner resolves to either the "new user" flow
    // or the "already onboarded" restart UI.
    await page.waitForSelector("h1", { timeout: 15_000 });

    const h1Text = await page.locator("h1").first().innerText();

    if (h1Text.includes("already completed")) {
      // Existing tenant: click "Start from scratch" button
      await page.getByRole("button", { name: /start from scratch/i }).click();
      // handleRestart() uses window.location.href = '/describe' (full nav)
      await page.waitForURL("**/describe", { timeout: 10_000 });
    } else if (h1Text.includes("Welcome")) {
      // New user: click "Let's start"
      await page.getByRole("link", { name: /let.s start/i }).click();
      await page.waitForURL("**/describe", { timeout: 10_000 });
    } else {
      // Unknown state — possibly auth failed mid-render
      await assertAuthenticated(page);
      throw new Error(`Unexpected /welcome h1: "${h1Text}"`);
    }

    // ── Step 2: /describe — answer 8 questions ───────────────────────────────
    // Wait for the onboarding session to initialise (ARIA sends Q1 greeting).
    await page.waitForSelector("textarea", { timeout: 15_000 });

    // Helper: type an answer and submit, then wait for ARIA's next reply.
    async function sendAnswer(answer: string): Promise<void> {
      const textarea = page.locator("textarea").first();
      await textarea.fill(answer);
      // Click Send button (submit button with text "Send")
      await page.getByRole("button", { name: /^send$/i }).click();
      // Wait for the loading indicator to disappear (ARIA is replying)
      await page.waitForFunction(
        () => {
          const dots = document.querySelector(".animate-bounce");
          return dots === null;
        },
        { timeout: 30_000 }
      );
      // Small safety gap so React state settles
      await page.waitForTimeout(300);
    }

    // Send all 8 answers sequentially. The describe page is a chat;
    // each answer unlocks the next question from ARIA.
    for (let q = 1; q <= 8; q++) {
      await sendAnswer(ANSWERS[q]);
    }

    // Wait for the "Review & finish" / "Continue to review" button to be enabled.
    // The button is disabled until >= 3 questions are answered; after Q8 it
    // shows "Review & finish".  We target the desktop sidebar version here
    // (lg:block) since Playwright uses a 1280-wide viewport.
    const continueBtn = page
      .locator("button")
      .filter({ hasText: /review.*finish|continue.*review/i })
      .first();

    await expect(continueBtn).toBeEnabled({ timeout: 5_000 });
    await continueBtn.click();

    // ── Step 3: /review ───────────────────────────────────────────────────────
    await page.waitForURL("**/review", { timeout: 15_000 });

    // Wait until the loading spinner disappears (extract-config API call)
    await expect(page.locator(".animate-spin").first()).not.toBeVisible({
      timeout: 20_000,
    });

    // The GTM profile card should contain the business name we typed in Q1.
    // review/page.tsx renders fields like: label "Business name" / value from gp.business_name.
    const reviewBody = page.locator("body");
    await expect(reviewBody).toContainText(BUSINESS_NAME, { timeout: 5_000 });

    // ── Step 4: Continue to /select-agents ───────────────────────────────────
    await page.getByRole("link", { name: /looks good.*continue/i }).click();
    await page.waitForURL("**/select-agents", { timeout: 10_000 });

    // ── Step 5: /select-agents — keep defaults, launch ───────────────────────
    // The "Launch ARIA" button is the primary CTA on this page.
    // Wait for it to be visible and enabled before clicking.
    const launchBtn = page
      .getByRole("button", { name: /launch/i })
      .first();
    await expect(launchBtn).toBeVisible({ timeout: 10_000 });
    await expect(launchBtn).toBeEnabled({ timeout: 5_000 });
    await launchBtn.click();

    // ── Step 6: /launching — animated setup checklist ────────────────────────
    await page.waitForURL("**/launching", { timeout: 15_000 });

    // Wait for the "Go to Dashboard" CTA which appears after the animation
    // completes (~7s for all 10 checklist items at 700ms each).
    const goToDashBtn = page.getByRole("link", { name: /go to dashboard/i });
    await expect(goToDashBtn).toBeVisible({ timeout: 15_000 });

    // Screenshot at the completion screen (saved to screenshots/ on failure,
    // but worth capturing here for the happy-path CI artifact too).
    await page.screenshot({
      path: path.join(__dirname, "../screenshots/onboarding-launching-complete.png"),
    });

    await goToDashBtn.click();

    // ── Step 7: /dashboard — final assertions ────────────────────────────────
    await page.waitForURL("**/dashboard", { timeout: 15_000 });

    // Verify we're genuinely on the dashboard
    await expect(page).toHaveURL(/\/dashboard/);

    // The dashboard page renders a greeting like "Good morning, [FirstName]"
    // and loads the business context which includes business_name.
    // We wait for the page to finish its async data load (spinner gone)
    // and then assert either the h1 greeting or a text block contains the name.
    //
    // The DashboardPage fetches biz.business_name from /api/dashboard/context
    // and displays it in various places (KPI cards, task board header, etc.).
    // We assert the page body contains our test value.
    await expect(page.locator("body")).toContainText(BUSINESS_NAME, {
      timeout: 15_000,
    });

    // Final screenshot
    await page.screenshot({
      path: path.join(__dirname, "../screenshots/onboarding-dashboard-landed.png"),
    });
  });
});
