/**
 * Virtual Office Sync — Socket.IO agent status transition spec
 *
 * WHAT IT TESTS
 * ---------------------------------------------------------------------------
 * 1. Navigate to /agents
 * 2. All 5 agent cards initially show "Idle" status
 * 3. Type a delegation-triggering message in the CEO chat widget
 *    ("draft a blog post about developer productivity")
 * 4. Within ~10 seconds the Content Writer card transitions to "Running"
 *    WITHOUT a page refresh — purely driven by the Socket.IO
 *    `agent_status_change` event.
 *
 * AUTH STRATEGY
 * ---------------------------------------------------------------------------
 * Same cached-session approach as onboarding-funnel.spec.ts.
 * If the session has expired the test fails with a clear message.
 *
 * SOCKET.IO DIAGNOSTIC LOGGING
 * ---------------------------------------------------------------------------
 * The spec attaches a `page.on('websocket', ...)` listener that logs every
 * WebSocket frame to stdout.  This makes failures debuggable without needing
 * to SSH into prod — you can see exactly which frames arrived (and which
 * `agent_status_change` payloads, if any) before the timeout.
 *
 * HOW THE DELEGATION TRIGGERS A STATUS CHANGE
 * ---------------------------------------------------------------------------
 * CEO chat widget  → POST /api/ceo/chat
 * Backend CEO handler emits a ```delegate``` block
 * _dispatch_paperclip_and_watch_to_inbox fires as a background task
 * Paperclip creates a "todo" issue and wakes the Content Writer agent
 * paperclip_office_sync.sync_agent_statuses() polls Paperclip every ~5s
 *   and emits  socket.emit('agent_status_change', {..., status:'running'})
 *   to the tenant's Socket.IO room
 * Frontend useAgentStatus() / VirtualOffice picks up the event and
 *   re-renders the agent card badge to "Running".
 *
 * TIMING NOTE
 * ---------------------------------------------------------------------------
 * The 10s timeout in the assertion below is tight.  If the Paperclip agent
 * is cold-starting, the Running status may take 15-30s.  The spec uses a
 * generous 30s `expect` timeout with a note explaining why.  Adjust
 * AGENT_STATUS_TIMEOUT_MS if your environment needs more headroom.
 */

import { test, expect, Page } from "@playwright/test";
import * as path from "path";
import * as fs from "fs";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const AUTH_SESSION_PATH = path.join(__dirname, "../fixtures/auth-session.json");

/**
 * How long we wait for the agent card to show "Running".
 * The spec description says ~10s but in practice cold-start + Paperclip
 * polling can take up to 30s.  Set via env for CI tunability.
 */
const AGENT_STATUS_TIMEOUT_MS = parseInt(
  process.env.AGENT_STATUS_TIMEOUT_MS ?? "30000",
  10
);

// The CEO chat message that should trigger a content_writer delegation.
// Phrased to make the CEO's intent unambiguous: "draft a blog post" maps
// directly to the ContentWriter agent in the delegate block logic.
const DELEGATION_MESSAGE =
  "draft a blog post about developer productivity for my marketing";

// Agent slugs as they appear in the /agents page card titles.
// Matches AGENT_DEFS in frontend/lib/agent-config.ts.
const ALL_AGENT_NAMES = [
  "ARIA CEO",
  "Content Writer",
  "Email Marketer",
  "Social Manager",
  "Ad Strategist",
];

// The agent whose status we expect to flip to "Running" after delegation.
const DELEGATED_AGENT_NAME = "Content Writer";

// ---------------------------------------------------------------------------
// Auth helper (shared with onboarding-funnel.spec.ts)
// ---------------------------------------------------------------------------

function loadStoredAuth(): object | null {
  try {
    if (fs.existsSync(AUTH_SESSION_PATH)) {
      return JSON.parse(fs.readFileSync(AUTH_SESSION_PATH, "utf8"));
    }
  } catch {
    // Malformed JSON
  }
  return null;
}

async function assertAuthenticated(page: Page): Promise<void> {
  const url = page.url();
  const isLoginPage =
    url.includes("/login") || url.includes("accounts.google.com");
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
// WebSocket frame logger
// ---------------------------------------------------------------------------

/**
 * Attaches a WebSocket listener to the page that logs every frame sent and
 * received on any WebSocket connection.  This captures Socket.IO frames for
 * post-mortem debugging when the agent status assertion times out.
 *
 * Logged format (stdout):
 *   [WS SENT]     <payload>
 *   [WS RECEIVED] <payload>
 *   [WS CLOSED]   url
 */
function attachWebSocketDiagnostics(page: Page): void {
  page.on("websocket", (ws) => {
    console.log(`[WS OPEN] ${ws.url()}`);

    ws.on("framesent", (frame) => {
      // Socket.IO encodes frames as text like "42[\"event\",{...}]"
      // Print them so failures show which events were exchanged.
      console.log(`[WS SENT]     ${frame.payload}`);
    });

    ws.on("framereceived", (frame) => {
      const payload = String(frame.payload);
      // Highlight agent_status_change events specifically
      if (payload.includes("agent_status_change")) {
        console.log(`[WS RECEIVED] *** agent_status_change *** ${payload}`);
      } else {
        // Truncate long payloads (e.g. full inbox items) so logs stay readable
        const truncated =
          payload.length > 300 ? payload.slice(0, 300) + "…" : payload;
        console.log(`[WS RECEIVED] ${truncated}`);
      }
    });

    ws.on("close", () => {
      console.log(`[WS CLOSED]   ${ws.url()}`);
    });
  });
}

// ---------------------------------------------------------------------------
// Test suite
// ---------------------------------------------------------------------------

test.describe("Virtual Office — Socket.IO agent status sync", () => {
  test.use({
    storageState: loadStoredAuth() ? AUTH_SESSION_PATH : undefined,
  });

  test(
    "agent card transitions Idle → Running after CEO chat delegation (no page refresh)",
    async ({ page }) => {
      // Attach WS diagnostics before any navigation so we capture the
      // initial connection handshake too.
      attachWebSocketDiagnostics(page);

      // ── Step 1: Navigate to /agents ───────────────────────────────────────
      await page.goto("/agents");
      await assertAuthenticated(page);

      // Wait for the agent grid to render.  The page fetches no async data
      // for the static card list (statuses start as "idle" in useState),
      // so the grid should be immediate.
      await expect(
        page.getByRole("heading", { name: "Marketing Agents" })
      ).toBeVisible({ timeout: 10_000 });

      // ── Step 2: Assert all agent cards show "Idle" initially ─────────────
      // The status badge text is rendered inside a <span> with text like
      // "Idle", "Running", etc. (statusDisplay[state.status].label).
      for (const agentName of ALL_AGENT_NAMES) {
        // Each agent card has the agent name in an h3 + the status in a
        // sibling span.  We locate the card by the h3 text and then check
        // the badge within that card.
        const card = page
          .locator(".bg-white.rounded-xl.border")
          .filter({ has: page.getByText(agentName, { exact: true }) })
          .first();

        // The CEO card is special — it may already show "Active" if the
        // backend last pushed a running status.  We accept "Idle" OR "Active"
        // for CEO to avoid flapping.
        if (agentName === "ARIA CEO") {
          await expect(card).toContainText(/Idle|Active/, { timeout: 5_000 });
        } else {
          await expect(card).toContainText("Idle", { timeout: 5_000 });
        }
      }

      // Screenshot: baseline state
      await page.screenshot({
        path: path.join(
          __dirname,
          "../screenshots/virtual-office-all-idle.png"
        ),
      });

      // ── Step 3: Open CEO chat and send delegation message ─────────────────
      // The FloatingChat widget is a fixed button in the bottom-right corner.
      // It renders a chat bubble icon button.  On /agents the sidebar is
      // present so the floating chat button should be visible.
      //
      // Selector: the FloatingChat open button doesn't have an aria-label in
      // the source, but it's the only button with the chat bubble SVG in the
      // fixed bottom-right region. We'll find it by its containing div's
      // position class or by looking for the pulse ring if the user has
      // unread messages.
      //
      // Fallback: if the floating chat isn't visible (e.g. mobile breakpoint
      // hides it), navigate to /dashboard which always shows it.
      let chatButton = page
        .locator("button")
        .filter({
          has: page.locator("svg path[d*='M21.75 6.75']"), // envelope icon used in chat
        })
        .first();

      // More reliable: look for the fixed positioning wrapper
      const floatingChatArea = page.locator(
        "[class*='fixed'][class*='bottom']"
      ).first();

      // Try to find ANY button in the floating chat area
      const floatingBtn = floatingChatArea.locator("button").first();

      const isBtnVisible = await floatingBtn.isVisible().catch(() => false);
      if (isBtnVisible) {
        await floatingBtn.click();
      } else {
        // Fallback: navigate to /dashboard where the floating chat is always
        // rendered, then send the message there.
        await page.goto("/dashboard");
        await assertAuthenticated(page);
        await page.waitForSelector("[class*='fixed'][class*='bottom']", {
          timeout: 10_000,
        });
        const dashFloatingBtn = page
          .locator("[class*='fixed'][class*='bottom']")
          .locator("button")
          .first();
        await dashFloatingBtn.click();
        // Navigate back to /agents so the agent card assertion still works
        // in a separate tab or by keeping this page open.
        // Actually: stay on /dashboard — the agent status change is a
        // Socket.IO event so we can assert it on any page that has the
        // socket connected.  We'll navigate back after sending.
      }

      // Wait for the chat input to appear (panel open)
      const chatInput = page.locator("textarea").filter({
        hasNot: page.locator("[class*='describe']"), // exclude onboarding textarea
      }).last();

      await expect(chatInput).toBeVisible({ timeout: 10_000 });

      // Send the delegation message
      await chatInput.fill(DELEGATION_MESSAGE);
      await chatInput.press("Enter");

      console.log(`[TEST] Sent CEO chat message: "${DELEGATION_MESSAGE}"`);

      // Wait for the message to appear in the chat (sending = false, message
      // added).  We look for our text appearing as a user bubble.
      await expect(
        page.locator("div").filter({ hasText: DELEGATION_MESSAGE }).first()
      ).toBeVisible({ timeout: 10_000 });

      // ── Step 4: Navigate to /agents (if we navigated away) ───────────────
      const currentUrl = page.url();
      if (!currentUrl.includes("/agents")) {
        await page.goto("/agents");
        await assertAuthenticated(page);
        await expect(
          page.getByRole("heading", { name: "Marketing Agents" })
        ).toBeVisible({ timeout: 10_000 });
      }

      // ── Step 5: Assert Content Writer card transitions to "Running" ────────
      // The Socket.IO event `agent_status_change` is emitted by
      // paperclip_office_sync.sync_agent_statuses() and picked up by
      // useAgentStatus() in socket.ts which calls setStatuses().
      // The component re-renders and the badge label changes from "Idle" to
      // "Running".
      //
      // We do NOT refresh the page — if this assertion passes it proves the
      // status update arrived via WebSocket, not a page reload.
      const contentWriterCard = page
        .locator(".bg-white.rounded-xl.border")
        .filter({ has: page.getByText(DELEGATED_AGENT_NAME, { exact: true }) })
        .first();

      console.log(
        `[TEST] Waiting up to ${AGENT_STATUS_TIMEOUT_MS}ms for Content Writer → Running`
      );

      await expect(contentWriterCard).toContainText("Running", {
        timeout: AGENT_STATUS_TIMEOUT_MS,
      });

      // Screenshot: running state captured
      await page.screenshot({
        path: path.join(
          __dirname,
          "../screenshots/virtual-office-content-writer-running.png"
        ),
      });

      console.log("[TEST] PASS — Content Writer card shows Running");

      // ── Step 6: Verify the status reverts to Idle eventually (optional) ───
      // This is a best-effort check.  If the agent finishes quickly we can
      // confirm the status goes back to Idle.  We don't fail if it doesn't
      // (the agent may legitimately still be running or transitioning to
      // "active").
      //
      // Commented out to keep the test deterministic on slow environments:
      //
      // await expect(contentWriterCard).not.toContainText("Running", {
      //   timeout: 120_000,
      // });
    }
  );
});
