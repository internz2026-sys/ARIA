# ARIA Change Log

---

## 2026-04-11 — Massive Paperclip integration overhaul (CEO chat speed, sub-agent dispatch, inbox plumbing, audit sweep)

A full-day debugging arc that started with "CEO chat is slow" and ended with a hardened end-to-end Paperclip + watcher + inbox pipeline. ~15 commits, the chat reply path is now ~3x faster, sub-agent delegations actually land in the inbox with structured email_draft fields, and ~26 latent failure modes from a code audit are closed.

### Phase 1 — Speed up CEO chat (commits `5c4f16d`, `bf4fe1c`, `be16e4c`, `04e4b81`)

**Problem:** CEO chat replies were taking 10-30 seconds. Diagnosed: the chat handler was routing through `run_agent_via_paperclip_sync` which (1) created a Paperclip issue, (2) posted a comment, (3) waited for the agent to spin up a `claude` CLI subprocess inside the Paperclip docker container, (4) polled for the reply with adaptive 1-4s intervals.

The Paperclip detour was adding 8-25s per chat for nothing — the chat reply itself doesn't need any of Paperclip's orchestration features.

**Fixes:**
- `backend/server.py:ceo_chat` — Removed the Paperclip routing block entirely. Chat now calls `call_claude(model=MODEL_HAIKU)` directly. Result: chat reply latency dropped from 10-30s → 1-4s.
- `backend/tools/claude_cli.py` — Disabled the semantic cache for CEO chat (`agent_id == "ceo"`). The 0.92 cosine threshold was producing false positives, returning the wrong cached reply for similar-but-different messages (e.g. "create a lead for Hanz" → "create an email for Hanz" had 0.93 similarity and collapsed to the same response).
- `backend/tools/claude_cli.py` — `--max-turns 1` was failing on complex chat ("Reached max turns" error) because Haiku was eager to use tools. Bumped to `--max-turns 5`.
- `backend/tools/claude_cli.py` — Added `_try_restore_claude_config()` helper that detects the CLI's "configuration file not found at /root/.claude.json" race and atomically restores from `~/.claude/backups/`. The CLI rotates its auth file periodically and occasionally leaves only the backup; without auto-recovery the only fix was SSH+manual cp. Wired into `lifespan` startup AND into `call_claude`'s exception path so it self-heals on every container restart and on every mid-runtime failure. Process-wide RLock prevents two concurrent calls from racing to copy the same backup.

### Phase 2 — Sub-agent delegation watcher (commits `788a732`, `40b547b`, `a415094`)

**Problem:** When CEO emitted a delegate block, `dispatch_agent` was fire-and-forget. Sub-agents would run in Paperclip but their results sometimes never reached the ARIA inbox (Path A skill curl failed silently, or Path B 5s global poller missed the comment).

**Fixes:**
- `backend/server.py:_dispatch_paperclip_and_watch_to_inbox` — New helper that wraps dispatch + placeholder + active polling + inbox write in one background task. Spawns immediately when a delegate block fires. Creates a placeholder inbox row right away so the user sees activity, then polls THIS specific issue with adaptive 1-4s intervals (not the global 5s loop) and updates the placeholder when the agent's reply lands. Marks the issue in `_processed_issues` so the global poller doesn't double-import.
- `backend/server.py:ceo_chat` dispatch block now calls this helper instead of `dispatch_agent` directly.
- `backend/paperclip_office_sync.py` — Fixed an infinite recursion regression in `_add_processed` from a `replace_all` rename gone wrong. Documented the trap with a comment so future-me doesn't repeat it.

### Phase 3 — Wake mechanism (commits `a412864`, `13f6fef`)

**Problem:** Even with the watcher, delegated agent issues were sitting in `backlog` forever. The `[paperclip-heartbeat] OK` log line was misleading — heartbeat returns 200 but doesn't actually trigger an Automation run for `claude_local` agents.

**Diagnosis:** Direct API query showed `STATUS: backlog, COMMENT COUNT: 0` for issues the watcher was polling.

**Fixes:**
- `backend/orchestrator.py:_dispatch_via_paperclip` — Removed the `heartbeat/invoke` call entirely. Replaced with a comment post (the agent's `wakeOnDemand=true` fires on `issue.comment` events, which is the canonical wake mechanism the CEO chat path was already using). Memory file warned about this explicitly: *"Do NOT call /heartbeat/invoke -- that creates a second On-demand run racing the Automation one."* I had misread that originally as "don't call BOTH."
- `backend/orchestrator.py:_dispatch_via_paperclip` — Added `"status": "todo"` to the issue create payload. Default Paperclip status is `backlog`, but the agent's `inbox-lite` endpoint (which the heartbeat skill queries first) only returns `todo`/`in_progress`/`blocked`. Without this the agent saw 0 assignments and asked the user "which task should I work on?" instead of executing.
- Wake comment is now verbose and directive: *"AUTONOMOUS TASK -- execute immediately, do not ask for clarification. ... Do NOT list other assignments. Generate the requested content. POST result to ARIA inbox via skill."* The previous short body let the agent's autonomy mode interpret it as a clarification request.

### Phase 4 — Audit sweep (commit `40aeb70`)

Ran an audit subagent to find latent failure modes. 26 issues found across HIGH/MEDIUM/LOW severity. All fixed in one bundled commit:

**`backend/tools/claude_cli.py`:**
- Subprocess kill now followed by `await proc.wait()` so timeouts don't leak zombie `claude` processes
- `_safe_decode` wrapper around bytes→str so corrupted CLI output doesn't crash the handler with `UnicodeDecodeError`
- Combined stdout+stderr for both error display and config-restore trigger
- `_try_restore_claude_config` uses atomic-rename + RLock to prevent two concurrent calls from both truncating the same backup mid-copy

**`backend/orchestrator.py`:**
- New `PaperclipUnreachable` exception + `strict=True` flag on `_urllib_request` so the watcher can fail-fast on outages instead of treating "no data" the same as "Paperclip is down"
- New `_sanitize_error_message` helper redacts secrets (JWT, supabase URLs, API keys) from raw exception messages before they hit `agent_logs` or API responses
- Separated `HTTPError`, `URLError`, `ConnectionError`, `TimeoutError`, `SSLError` from generic Exception

**`backend/services/paperclip_chat.py`:**
- `pick_agent_output` now takes optional `expected_agent` and skips comments authored by CEO when looking for a delegated agent's reply
- Added `[wake]` to the framing-prefix filter list
- Three-tier fallback: agent-authored → non-CEO authored → longest comment overall (later relaxed because too aggressive)

**`backend/paperclip_office_sync.py`:**
- `_processed_issues` bounded at 5000 entries with eviction
- New `_is_failed` helper for failed/cancelled status detection
- `tenant_id` validated against active tenants before insert (prevents orphan rows)
- **Removed** the dangerous "fall back to issue.body as content" path that was making failed runs look successful by importing the user's own prompt as the agent's reply
- Socket.IO emit failures now log at debug level instead of bare pass

**`backend/server.py` (ceo_chat + watcher):**
- Per-session `asyncio.Lock` so two concurrent requests for the same `session_id` don't interleave their `session.append()` calls and corrupt history (covers double-click send, two open tabs)
- New `_safe_background` wrapper around `create_task` adds an error callback so silent crashes show up in logs instead of "Task exception was never retrieved" at GC time. **This caught the recursion bug in production.**
- `execute_action` calls wrapped in try/except so action handler crashes don't 500 the whole chat after the CEO already replied
- Generic error message to user, sanitized errors in logs
- Forbidden-request override only fires when CEO didn't already refuse AND user message clearly asks for forbidden action
- `_parse_codeblock_json` helper recovers from common Haiku JSON mistakes (trailing commas, JS comments, prose padding)

**`backend/server.py` (`_run_agent_to_inbox`):**
- Outer except now UPDATES the placeholder with error content instead of creating a duplicate "Failed:" row
- Sanitizes error message before storing/displaying

### Phase 5 — Email parser + template wrapper (commits `19c8611`, `b80b749`, `aba24a5`, `a2ad072`, `8d639ba`, `44f66bd`, `472d6e2`, `a7e466e`, `d42236e`)

The watcher was creating placeholder rows that had no `email_draft` field, so the inbox UI didn't render the Approve & Send / Schedule / Cancel draft buttons. The agent's skill curl POST was creating a SECOND row with the actual content but ALSO without `email_draft`. Two rows per delegation, neither correct.

**Fixes (iterated through several false starts):**
- `backend/server.py:_parse_email_draft_from_text` — New helper that extracts `subject`, `to`, `text_body`, `html_body`, `preview_snippet` from the agent's plain markdown OR HTML reply. Three subject formats supported (`**Subject:**`, A/B test variants, plain `Subject:`). Three fallbacks (Preview Text, first non-greeting sentence, "Untitled email"). The recipient regex strips HTML attributes first so `style="font-family: ...@..."` doesn't false-match.
- `backend/server.py:_parse_html_email_draft` — Separate branch for raw HTML content. Extracts subject from `<title>` → `<h1>` → `<h2>` → "Subject Line:" markers in stripped text → first non-greeting `<p>`. The first version of the watcher was grabbing `<html><body style="font-family: -apple-system, ...` as the subject because the markdown parser was treating HTML as plain text.
- `backend/server.py:_parse_social_drafts_from_text` — Detects Twitter/LinkedIn sections so content_writer/social_manager output gets `type=social_post` (which renders the Publish to X / Publish to LinkedIn buttons).
- `backend/server.py:_markdown_to_basic_html` — Quick markdown→HTML converter (bold, italic, headers, lists, links, paragraph wrapping). Used as a fallback for `html_body` when the agent didn't include a fenced ```html``` block. Without this the editor's contenteditable iframe was loading an empty `srcDoc` and looking broken.
- `backend/server.py:_enrich_task_desc_with_crm` — Looks up CRM contacts mentioned in the task description by name token, appends matched contacts (with emails) so the agent has the right recipient address. The CEO's CRM-context heuristic doesn't fire on phrases like "create marketing email for Hanz" (no CRM noun), so this closes that gap by doing a cheap CRM lookup right before dispatch.
- `backend/server.py:_wrap_email_in_designed_template` — Light-themed branded email template (600px max-width, blue gradient header, white body card, blue section headers, callout cards with colored left borders, styled CTA button, footer with copyright). Auto-styles plain `<p>`/`<ul>`/`<strong>` tags with inline styles. Used as a **fallback** only when the agent's HTML is plain unstyled — gated by `_agent_html_already_designed` so emails the agent designed itself stay untouched.
- `backend/server.py:_business_name_for_template` — Reads the tenant config to get `business_name` for the template header.

**Field name fix (commit `44f66bd`):** I was writing `body_html` and `body` to the email_draft dict, but the frontend's `EmailDraft` interface (`frontend/app/(dashboard)/inbox/page.tsx:13`) expects `html_body` and `text_body`. The contenteditable iframe loaded `draft.html_body` which was undefined. Also added `status: "draft_pending_approval"` to the dict so the badge renders correctly.

### Phase 6 — Inbox CREATE endpoint hardening (commits `b80b749`, `d140762`)

**Problem:** The agent posts via `/api/inbox/{tenant_id}/items` AND the watcher writes to inbox separately. Two paths, two rows per delegation. Plus the agent often sends a "Saved successfully!" confirmation message as a SECOND POST, creating a third row.

**Fixes in `create_inbox_item`:**
- **Reject confirmation messages** — short content with markers like `✅`, `Saved to ARIA Inbox`, `Successfully saved`, `draft created and saved`, `Draft ID:` gets short-circuited with `{item: null, skipped: "confirmation_message"}`. Removed the 600-char length filter (was letting through long status messages that echoed the email).
- **Always run the parser, even when agent provides email_draft** — agent's fields win where set, parser fills gaps. Subject/recipient that look like raw HTML (`<html><body style=`) get overridden by the parser's clean values.
- **Type normalization** — any email_marketer content with parsed `email_draft` is forced to `type='email_sequence'` regardless of what the agent sent (`email`, `email_draft`, etc.). This is the canonical type the frontend renders the editable form for.
- **Recent-row dedupe** — when no `paperclip_issue_id` is provided, look back 5 minutes for inbox rows from the same tenant + agent with the same first 100 chars of content. If found, UPDATE that row instead of inserting a duplicate. Catches the watcher placeholder + agent skill curl race.

### Phase 7 — Watcher placeholder deletion (commit `472d6e2`)

User explicitly asked: *"I want only one inbox message from the subagent not the draft, and then the final output is that possible?"*

**Fix in `_dispatch_paperclip_and_watch_to_inbox`:**
- After detecting the agent's reply comment, the watcher looks for any existing row from the same tenant + agent in the last 5 minutes that ISN'T the placeholder AND has substantial content (>200 chars, status not `processing`)
- If found → the agent's skill curl already wrote the canonical row → **deletes the watcher's placeholder**
- Sentinel `skill_row_already_exists` prevents the fallback branch from re-creating a fresh row after the delete
- Result: one row per delegation when the agent uses the skill curl path

### Lessons / Things to Watch

1. **Heartbeat does NOT wake claude_local agents.** Use comment posts. Memory file warned about this explicitly; I misread it the first time.
2. **Default Paperclip issue status is `backlog`**, which is excluded from `inbox-lite`. Always set `status: "todo"` when creating issues you want the agent to work on.
3. **`_safe_background` is essential.** Bare `_aio.create_task(...)` swallows exceptions until GC; the recursion bug in `_add_processed` would have been invisible without the error callback.
4. **Frontend field names matter.** `EmailDraft.html_body` not `body_html`, `text_body` not `body`. Match the frontend interface or the editor renders empty.
5. **Docker layer caching can lie.** `git pull` showing "Already up to date" combined with `=> CACHED [6/7] COPY backend/ backend/` means the new code is NOT in the running image. Verify with `docker exec aria-backend grep -c <new_helper_name> /app/backend/server.py` and force `--no-cache` rebuild if needed.
6. **The CLI's config-rotation race fires on every container restart.** Auto-restore is now in lifespan + reactive in call_claude, so it self-heals; never need to manually `cp` again.
7. **Two write paths to the same inbox row need explicit dedupe.** Watcher placeholder + agent skill curl will always race; the watcher now deletes its own placeholder when the skill curl row exists.

---

## 2026-03-26 — Onboarding-to-Dashboard Data Flow

### Problem
- Onboarding collected business data but never saved it (the "Launch ARIA" button was a plain `<a href>` with no API call)
- Dashboard used hardcoded `"demo"` as tenant_id and showed static/generic data
- `build_tenant_config` had an async bug — called `extract_config()` without `await`, returning a coroutine instead of a dict

### Changes

**Backend:**
- `backend/onboarding_agent.py` — Fixed async bug in `build_tenant_config` (now `async def` with proper `await`), added `_extracted_config` cache, added `active_agents` parameter override
- `backend/server.py` — Added `GET /api/dashboard/{tenant_id}/config` endpoint returning business name, product, positioning, channels, active agents. Updated `SaveConfig` model to accept `active_agents` list

**Frontend:**
- `frontend/app/(onboarding)/review/page.tsx` — Caches extracted config to `localStorage("aria_onboarding_config")` after extraction
- `frontend/app/(onboarding)/select-agents/page.tsx` — Rewrote "Launch ARIA" from plain link to button that calls `/api/onboarding/save-config` with selected agents, stores `tenant_id` in localStorage, then navigates to dashboard
- `frontend/app/(dashboard)/dashboard/page.tsx` — Reads `tenant_id` from localStorage, fetches `/api/dashboard/{tid}/config`, displays: product name in greeting, GTM positioning card with 30-day plan and channels, correct active agent count/status, completed onboarding checkmarks in Getting Started

---

## 2026-03-26 — Chat Markdown Rendering

### Problem
- Chat messages displayed raw markdown (`**bold**` shown as literal asterisks)
- Em dashes and emoji rendered as mojibake (`â€"`, `ðŸ'‹`) due to Windows cp1252/UTF-8 mismatch

### Changes

**Frontend:**
- `frontend/app/(dashboard)/chat/page.tsx` — Added `renderMarkdown()` function that converts `**bold**`, `*italic*`, inline code, and `\n` to proper HTML. Applied to assistant message rendering via `dangerouslySetInnerHTML`

**Backend:**
- `backend/tools/claude_cli.py` — Added `encoding="utf-8"` and `errors="replace"` to `subprocess.run()`. Set `PYTHONIOENCODING=utf-8` and `PYTHONUTF8=1` env vars. Added cp1252→UTF-8 mojibake reversal fallback

---

## 2026-03-26 — Python 3.14 Compatibility

### Problem
- `pydantic==2.9.0` required `pydantic-core` which had no wheels for Python 3.14, failing pip install with Rust compilation errors

### Changes
- `backend/requirements.txt` — Changed all pinned versions (`==`) to minimum versions (`>=`), bumped pydantic to `>=2.10.0`

---

## 2026-03-26 — Virtual Office: Agent Movement, Idle Behaviors & Live Status

### Features Added

**Virtual Office canvas (`frontend/components/virtual-office/VirtualOffice.tsx`):**
- Waypoint-based animation system: each agent has an `AnimPos` struct tracking animated x/y separately from desk positions
- Agents walk to the Meeting Room (offset per agent to avoid stacking) when status → `running`
- Agents walk back to their desk when status → `idle` or `busy`
- **Idle life behaviors**: agents randomly wander to spots in their department room every 4–10 seconds, pause 1.5–2.5s, then return to desk. Wander destinations defined per room in `IDLE_SPOTS`
- Status-change detection uses `prevStatusRef` — wandering is not interrupted by re-renders, only by actual status transitions
- Walking animation: leg alternation, arm swing, facing direction
- Always-visible name tags above heads; crown for Opus 4.6 agents
- Room decorations: rugs, bookshelves, whiteboards, filing cabinets, clocks, lamps, coffee machine, water cooler, conference table with chairs

**Office page (`frontend/app/(dashboard)/office/page.tsx`):**
- Integrated `useAgentStatus(tenantId)` Socket.IO hook — live `agent_status_change` events from backend are merged into agent list in real-time
- `tenantId` stored in state so it can be passed to the socket hook
- `agentsWithLive` memoized merge of REST-fetched agents + live socket statuses
- Info panel auto-syncs when selected agent's live status changes

**Backend (`backend/server.py`):**
- Socket.IO `agent_status_change` events emitted when CEO delegates tasks: CEO → `busy`, assigned agent → `running`, after 8s both return, agent works 20s, then → `idle`
- `GET /api/office/agents/{tenant_id}` endpoint returns agent statuses
- `GET /api/tenant/by-email/{email}` for persistent login across sessions

**Auth persistence (`frontend/app/auth/callback/page.tsx`, `frontend/app/(dashboard)/layout.tsx`):**
- On login/reload, checks `/api/tenant/by-email/{email}` server-side to restore `tenant_id` to localStorage, preventing re-login from sending users back to onboarding

**Socket client (`frontend/lib/socket.ts`):**
- Singleton Socket.IO connection with `useAgentStatus` and `useActivityFeed` hooks
- Joins tenant room on connect, leaves on unmount

**Config (`frontend/lib/office-config.ts`):**
- 5 real ARIA agents mapped to Paperclip slugs
- 6 rooms in 3×2 grid with desk positions, colors, department IDs
- `MEETING_CENTER` export for rally point coordination

---

## 2026-03-26 — Virtual Office Canvas Alignment Fix

### Problem
- Virtual Office canvas was pushed too far right — white container visible on right side
- Root cause: `<main className="p-6 lg:p-8">` padding in dashboard layout could not be reliably countered with negative margins

### Fix
- `frontend/app/(dashboard)/office/page.tsx` — Changed from flex/padding approach to `position: fixed top-14 lg:top-0 left-0 lg:left-[240px] right-0 bottom-0`, fully bypassing `<main>` padding
- `VirtualOffice` wrapper changed to `absolute inset-0` so it fills parent regardless of height chain
- Canvas set to `position: absolute` to prevent wrapper inflation from canvas size

---

## 2026-03-26 — Floating Kanban Board Widget

### Features Added
- `frontend/components/virtual-office/OfficeKanban.tsx` — New draggable floating Task Board button (orange-pink gradient `#FF6B35 → #F7418F`)
- Button is freely draggable anywhere on screen (Photoshop-style floating panel)
- Opens a Kanban panel toward screen center based on button position
- Shows task count badge and animated pulse dot for in-progress tasks
- Panel renders `<KanbanBoard>` in compact mode with loading/empty states
- Integrated into `frontend/app/(dashboard)/office/page.tsx`

### Shared Components Created
- `frontend/components/shared/KanbanBoard.tsx` — Reusable Kanban board component with drag-to-reorder columns
- `frontend/components/shared/EmptyState.tsx` — Reusable empty state component
- `frontend/lib/task-config.ts` — Shared `Task` type, `fetchTasks`, `patchTaskStatus`, `deleteTaskApi`, `PRIORITY_STYLES`

---

## 2026-03-26 — Floating CEO Chat Widget

### Features Added
- `frontend/components/shared/FloatingChat.tsx` — New draggable floating CEO Chat button (purple gradient `#534AB7 → #7C3AED`)
- Available on every dashboard page via `frontend/app/(dashboard)/layout.tsx`
- Opens a chat panel with full message history, agent delegation cards, and session history sidebar
- Session history toggled with clock icon button; new chat button in header
- Message count badge on button
- Integrated into dashboard layout so it persists across all dashboard page navigations

---

## 2026-03-26 — Drag Performance Optimization

### Problem
- Both floating widgets (CEO Chat, Task Board) had noticeable lag during dragging
- Two root causes: (1) `transition-all` CSS was animating position changes during drag, (2) `left`/`top` properties trigger full layout recalculation on every frame

### Fix
- `frontend/lib/use-draggable.ts` — New shared draggable hook replacing per-component drag logic
  - Uses `transform: translate3d()` instead of `left`/`top` — GPU compositor path, zero layout recalculation
  - Direct DOM style updates during drag (`mousemove`), React state sync only on `mouseup`
  - `will-change: transform` hint for GPU layer promotion
  - `onDragStart` callback so panels auto-close when drag begins
- Both `FloatingChat.tsx` and `OfficeKanban.tsx` updated to use `useDraggable()` hook
- Removed `transition-all` from button styles on both widgets

---

## 2026-03-26 — Shared CEO Chat Session

### Problem
- Floating CEO Chat widget and `/chat` page used different localStorage keys for session ID
- Loading a previous chat in `/chat` page had no effect on the floating widget (and vice versa)

### Fix
- `frontend/lib/use-ceo-chat.ts` — Converted from standalone hook to React Context (`CeoChatProvider` + `useCeoChat()`)
  - Single `SESSION_KEY = "aria_ceo_chat_active"` shared across all consumers
  - State: `messages`, `sessions`, `sessionId`, `sending`
  - Actions: `send()`, `switchSession()`, `startNewChat()`, `refreshSessions()`
- `frontend/app/(dashboard)/layout.tsx` — Wraps entire layout in `<CeoChatProvider>` so all pages share one chat instance
- `frontend/app/(dashboard)/chat/page.tsx` — Refactored to use `useCeoChat()` context hook; removed all local session state
- `frontend/components/shared/FloatingChat.tsx` — Refactored to use `useCeoChat()` context hook
- Switching sessions in `/chat` page now immediately reflects in the floating widget and vice versa

---

## 2026-03-26 — Codebase Architecture Refactor

### Backend
- `backend/agents/base.py` — New `BaseAgent` class
  - `business_context(config)` static method: shared product/audience/brand context block
  - `gtm_context(config)` static method: shared GTM playbook block
  - `run(tenant_id, context)` shared async runner with lazy `call_claude` import (avoids circular import via `tools/__init__.py`)
- All 5 agent files (`ceo_agent.py`, `content_writer_agent.py`, `email_marketer_agent.py`, `social_manager_agent.py`, `ad_strategist_agent.py`) — Refactored to extend `BaseAgent`, each only overrides `build_system_prompt()` and optionally `build_user_message()`; `AGENT_REGISTRY`-compatible module-level `run()` preserved

### Frontend
- `frontend/lib/agent-config.ts` — New single source of truth for agent metadata
  - `AGENT_DEFS[]` with slug, name, role, description, color, model, schedule
  - Derived exports: `AGENT_MAP`, `AGENT_NAMES`, `AGENT_COLORS`, `AGENT_LABELS`
  - Eliminates 3+ duplicate agent arrays across dashboard, agents page, chat page
- `frontend/lib/api.ts` — `API_URL` now exported (was private); removes 5+ duplicate `const API_URL = process.env...` definitions
- `frontend/lib/utils.ts` — Added `formatDateAgo()`, `getGreeting()`, `getInitials()` utilities
- `frontend/lib/office-config.ts` — Refactored to import `AGENT_DEFS` from `agent-config.ts`; `AGENTS` array derived from `AGENT_DEFS.map()`
- `frontend/lib/task-config.ts` — Re-exports `AGENT_LABELS` from `agent-config.ts`; imports `API_URL` from `api.ts`
- `frontend/app/(dashboard)/dashboard/page.tsx` — Imports `AGENT_DEFS`, `API_URL`, `PRIORITY_STYLES` from shared modules
- `frontend/app/(dashboard)/agents/page.tsx` — Imports `AGENT_DEFS` from `agent-config.ts`
- `frontend/app/(dashboard)/chat/page.tsx` — Uses `formatDateAgo` from `utils.ts`, `AGENT_COLORS`/`AGENT_NAMES` from `agent-config.ts`
- `frontend/components/shared/TaskCard.tsx` — Compact mode now shows single-line truncated text with full-text `title` tooltip

### Folder Cleanup
- `docs/` — Created; moved `aria-landing.jsx.pdf`, `aria-prd-v1.pdf`, root `agents/` folder → `docs/agents/`
- Deleted: stale `/.next/` at project root, orphaned `frontend/components/aria-chat/`

---

## 2026-03-26 — Cloud Backend: Anthropic API + Railway Deployment

### Problem
- Backend used local Claude Code CLI (subprocess) — required user's PC to be on for agents to function
- No API key meant the system could not run on any cloud host
- No rate limiting — uncapped API usage risked runaway costs
- Usage tracking was in-memory only — limits reset on every server restart

### Changes

**`backend/tools/claude_cli.py`** (fully rewritten):
- Replaced subprocess/CLI with `anthropic.AsyncAnthropic` SDK — uses `ANTHROPIC_API_KEY`
- Per-tenant hourly rate limiting: `ARIA_HOURLY_REQUEST_LIMIT` (default 60 req/hr), `ARIA_HOURLY_TOKEN_LIMIT` (default 200k tokens/hr)
- Usage persisted to Supabase `api_usage` table (upsert on `tenant_id, hour`) — survives server restarts
- Local cache avoids hitting Supabase on every request; syncs global totals alongside per-tenant totals
- Model configurable via `ARIA_MODEL` env var (default `claude-sonnet-4-20250514`)

**`backend/server.py`**:
- Added `tenant_id` param to all `call_claude()` calls in triage and CEO chat endpoints
- Added `GET /api/usage?tenant_id=` endpoint to expose current token/request counts
- Fixed `_AGENTS_DIR` path: `parent.parent / "agents"` → `parent.parent / "docs" / "agents"`

**`backend/Dockerfile`**:
- Removed Node.js and Claude CLI install (no longer needed)
- Added `COPY docs/ docs/` so agent `.md` files are available in container
- CMD uses `sh -c` for `$PORT` shell expansion: `uvicorn backend.server:socket_app --host 0.0.0.0 --port ${PORT:-8000}`

**`railway.toml`**:
- Switched builder from Nixpacks to `DOCKERFILE` with `dockerfilePath = "backend/Dockerfile"`
- Removed `startCommand` (was passing `$PORT` as a literal string, not expanding it)
- Health check: `GET /health`, timeout 300s

**`.env.example`**:
- Removed ngrok vars
- Added `ARIA_MODEL`, `ARIA_HOURLY_REQUEST_LIMIT`, `ARIA_HOURLY_TOKEN_LIMIT`

**`start.sh`**:
- Removed ngrok check and startup; simplified to backend + frontend only

**Supabase** — new `api_usage` table:
```sql
CREATE TABLE api_usage (
  tenant_id TEXT NOT NULL,
  hour TEXT NOT NULL,        -- 'YYYY-MM-DD-HH' UTC
  input_tokens BIGINT NOT NULL DEFAULT 0,
  output_tokens BIGINT NOT NULL DEFAULT 0,
  requests INT NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ DEFAULT now(),
  PRIMARY KEY (tenant_id, hour)
);
```

### Deployment Fixes (Railway — 3 iterations)
1. **Nixpacks failed** — Railway couldn't detect app type from repo root; fixed by switching to Dockerfile builder
2. **Health check failed (missing docs/)** — `_AGENTS_DIR` pointed to `/app/agents/` which didn't exist in container; fixed path + added `COPY docs/ docs/`
3. **Health check failed (`$PORT` literal)** — `startCommand` in railway.toml passed `$PORT` as a string to uvicorn; fixed by removing `startCommand` so Dockerfile CMD (using `sh -c`) handles expansion

### Result
- Backend is live on Railway with health check passing
- Agents run without user's PC being on
- Rate limits active and persisted to Supabase

---

## 2026-03-26 — Inbox Pipeline: Agent Outputs Saved & Displayed

### Problem
- Agents generated content in chat or via direct run, but outputs were fire-and-forget
- The Inbox page was hardcoded empty with no backend data
- CEO chat delegations ran sub-agents in background but never captured or stored their results
- No way for users to review, copy, or manage generated content after it left the chat

### Changes

**Supabase** — new `inbox_items` table:
```sql
CREATE TABLE inbox_items (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id TEXT NOT NULL,
  agent TEXT NOT NULL,
  type TEXT NOT NULL DEFAULT 'general',
  title TEXT NOT NULL,
  content TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'ready',
  priority TEXT NOT NULL DEFAULT 'medium',
  task_id UUID,
  chat_session_id TEXT,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);
```

**`backend/server.py`**:
- Added `_save_inbox_item()` — inserts agent output into `inbox_items` table
- Added `_run_agent_to_inbox()` — wraps background agent execution, captures result, saves to inbox, emits Socket.IO `inbox_new_item` event, and marks the originating task as `done`
- Added `_infer_content_type()` — maps agent slug to content type (blog_post, email_sequence, social_post, ad_campaign, strategy_update)
- CEO chat delegation now uses `_run_agent_to_inbox()` instead of fire-and-forget `create_task`
- Direct agent run endpoint (`POST /api/agents/{tenant_id}/{agent_name}/run`) also saves output to inbox
- Added `GET /api/inbox/{tenant_id}` — list inbox items with optional `?status=` filter
- Added `PATCH /api/inbox/{item_id}` — update item status (ready, completed, archived)
- Added `DELETE /api/inbox/{item_id}` — remove item
- Updated `GET /api/dashboard/{tenant_id}/inbox` — returns latest 5 inbox items

**`frontend/lib/api.ts`**:
- Added `inbox.list()`, `inbox.update()`, `inbox.remove()` API methods

**`frontend/app/(dashboard)/inbox/page.tsx`** (fully rewritten):
- Fetches real data from `/api/inbox/{tenant_id}`
- Tab filters: All, Content ready, Needs review, Completed — with live counts
- List/detail split view: item list on left, full content preview on right
- Copy button to clipboard for any deliverable
- Mark complete / Reopen / Delete actions
- Agent name + color badges, content type labels, priority dots, relative timestamps
- Socket.IO listener for `inbox_new_item` — auto-refreshes when new content arrives
- Empty state links to CEO chat

### Data Flow (now complete)
1. User asks CEO in chat → "Write me a blog post"
2. CEO delegates to Content Writer → task saved to `tasks` table
3. Content Writer runs in background → generates blog post
4. Output saved to `inbox_items` → Socket.IO notifies frontend
5. Inbox page shows the deliverable → user can copy, review, or mark complete

---

## 2026-03-26 — Task-Synced Virtual Office Movement

### Problem
- Agent movement was driven by hardcoded timers (8s meeting, 20s fake "working") completely disconnected from real task execution
- Agents wandered randomly around their rooms even when no tasks existed — made the office feel dishonest
- Activity ticker used hardcoded `MOCK_ACTIVITY` strings instead of real data
- No "working" state — agents were either "running" (walking to meeting) or "idle" (at desk), nothing in between

### Changes

**New status: `working`** — agent is at their desk executing a real task (typing animation + blue glow)

**Status lifecycle (now reflects real execution):**
1. CEO delegates task → CEO "busy" + agent "running" (both walk to meeting room)
2. After 4s meeting → CEO "idle" (returns to desk), agent "working" (returns to desk, starts typing)
3. Agent actually executes via Claude API → stays in "working" for the real duration
4. Agent finishes → "idle" (stops typing), output saved to inbox
5. On failure → agent returns to "idle" so it doesn't get stuck

**`backend/server.py`**:
- Removed `_return_to_desk()` function (was hardcoded 8s+20s timers)
- `_run_agent_to_inbox()` now drives the full lifecycle: meeting delay → CEO returns → agent works → agent done
- Direct `run_agent` endpoint also emits "working" → "idle" status events
- `GET /api/dashboard/{tenant_id}/activity` now returns real data from `inbox_items` + `tasks` tables (no more hardcoded mock)

**`frontend/components/virtual-office/VirtualOffice.tsx`**:
- Removed all idle wandering: `IDLE_SPOTS`, `idleTimer`, `waitTimer`, `wandering`, `waiting` — agents only move when a real task triggers them
- Added "working" status handler: agent walks back to desk, shows typing animation (fast arm movement) with blue pulsing glow
- Simplified `AnimPos` interface (removed 5 wandering-related fields)
- No tasks = no movement (office is "quiet")

**`frontend/app/(dashboard)/office/page.tsx`**:
- Replaced `MOCK_ACTIVITY` with real data fetched from `/api/dashboard/{tenant_id}/activity`
- Falls back to "No recent activity" message when empty
- Updated status legend: "Running" → "Working", "Busy" → "In Meeting"

**`frontend/lib/office-config.ts`** + **`frontend/lib/socket.ts`**:
- Added "working" to `AgentStatus` type union

---

## 2026-03-26 — Virtual Office: Persistent Agent Status + CEO Meeting Walk

### Problem
- Agents never visibly moved in the Virtual Office when chatting with the CEO, caused by three issues:
  1. `/api/office/agents/` always returned hardcoded `"idle"` for all agents — no status persistence
  2. CEO received `"busy"` status when delegating, but only `"running"` triggers the walk-to-meeting animation
  3. Socket.IO events are ephemeral — if the user is on `/chat` page, the `/office` page component isn't mounted and misses all status change events. By the time the user navigates to `/office`, the lifecycle has completed

### Changes

**`backend/server.py`**:
- Added `_live_agent_status` in-memory dict: `tenant_id → agent_id → status payload`
- Added `_emit_agent_status()` helper that atomically updates the in-memory store AND emits the Socket.IO event
- Replaced all 7 direct `sio.emit("agent_status_change", ...)` calls with `_emit_agent_status()`
- `/api/office/agents/{tenant_id}` now merges live statuses from `_live_agent_status` — no more hardcoded `"idle"`
- CEO delegation status changed from `"busy"` to `"running"` so the CEO visually walks to the meeting room alongside the delegated agent

### Result
- Navigate to `/office` during or after a CEO chat delegation → agents show correct status (running/working/idle)
- Both CEO and delegated agent walk to meeting room when a task is delegated
- Status persists across page navigations (in-memory, resets on server restart)

---

## 2026-03-26 — Draggable Widgets: Panel Follows During Drag

### Problem
- CEO Chat and Task Board floating widgets closed their dropdown panel as soon as dragging started (`onDragStart(() => setOpen(false))`)
- Panel position only updated on mouse release, so even if the panel stayed open it wouldn't follow the button

### Changes

**`frontend/lib/use-draggable.ts`**:
- Added RAF-throttled `setPos()` sync during drag — `pos` state now updates at ~60fps while dragging, allowing open panels to follow the button in real-time
- Button still uses direct DOM `translate3d()` for zero-lag movement; panel repositions via React state
- Cleanup: cancel pending RAF on mouseup

**`frontend/components/shared/FloatingChat.tsx`**:
- Removed `onDragStart(() => setOpen(false))` — panel stays open during drag and follows the button

**`frontend/components/virtual-office/OfficeKanban.tsx`**:
- Removed `onDragStart(() => setOpen(false))` — panel stays open during drag and follows the button

### Result
- Open either widget's dropdown → drag the button → panel follows smoothly
- Click behavior unchanged (4px threshold distinguishes click from drag)

---

## 2026-03-26 — Task-to-Office Sync: Agent Status Reflects Task Board

### Problem
- Agents in the Virtual Office never reflected tasks from the Task Board — movement only happened during LIVE Socket.IO events from CEO chat delegation
- `/api/office/agents/` returned hardcoded "idle" from in-memory store (resets on every Railway deploy)
- Moving a task to "In Progress" on the Kanban board updated the database but emitted no Socket.IO event — Virtual Office didn't react
- Deleting an in-progress task left the agent stuck in "working" visual state

### Changes

**`backend/server.py`**:
- `GET /api/office/agents/{tenant_id}` — now queries `tasks` table for "in_progress" tasks and merges into agent statuses. Priority: active live status (running/working from delegation) > task-based status > idle
- `PATCH /api/tasks/{task_id}` — emits `agent_status_change` via Socket.IO when task status changes:
  - Task → "in_progress": agent emits "working" (starts typing animation in office)
  - Task → "done"/"to_do"/"backlog": agent emits "idle" (only if no OTHER in_progress tasks remain for that agent)
- `DELETE /api/tasks/{task_id}` — if deleted task was "in_progress", checks for remaining active tasks before emitting "idle"

### Result
- Navigate to Virtual Office → agents with in_progress tasks show "working" (typing at desk with blue glow)
- Drag task to "In Progress" on Kanban → assigned agent starts working animation in real-time
- Drag task to "Done" → agent returns to idle
- Delete in-progress task → agent returns to idle
- Multiple in_progress tasks for same agent: stays "working" until ALL are moved out of in_progress
- Survives Railway redeploys (reads from Supabase tasks table, not just in-memory store)

---

## 2026-03-26 — CEO Active While Chatting + Kanban-Driven Agent Lifecycle

### Problem
- CEO agent showed no visual activity when the user was chatting with it — sprite stayed idle the whole time
- Delegated agents went "idle" after the Claude API call finished (~30s), even though the task still existed in the Kanban board
- Tasks were created with status "to_do" during delegation, but the task-to-office sync only activated for "in_progress" — agents never appeared to be working
- If an agent produced no content, the task was left as "in_progress" forever (agent stuck working)

### Changes

**`backend/server.py`**:

*CEO activity:*
- `POST /api/ceo/chat` now emits CEO status "running" (walks to meeting room) when a message is received — the CEO is "in a meeting with the user"
- If CEO responds without delegating → emits "idle" (meeting over, returns to desk)
- If CEO delegates → CEO stays in meeting room, then the delegation flow takes over (meeting with agent, then CEO idle)

*Task lifecycle — Kanban is the source of truth:*
- Delegated tasks are now created with status `"in_progress"` (was `"to_do"`) — agents immediately show as working
- `_run_agent_to_inbox()` no longer emits "idle" when the Claude API call finishes
- Instead: task is marked "done" in DB → checks if agent has OTHER in_progress tasks → only goes idle if none remain
- Empty content edge case handled: task still marked done and agent goes idle if no other work

### New lifecycle
1. User sends chat message → CEO walks to meeting room ("running")
2. CEO responds without delegating → CEO returns to desk ("idle")
3. CEO delegates → CEO stays at meeting room, agent walks to meeting room ("running")
4. After 4s briefing → CEO returns to desk ("idle"), agent returns to desk ("working")
5. Agent executes Claude API call → task marked "done" in DB → result saved to Inbox
6. Agent checks for remaining in_progress tasks → goes "idle" only if none remain
7. If user creates new tasks on Kanban → "In Progress" tasks keep agents working

### Source of truth
- **Kanban Board** controls when agents are working vs idle
- **Socket.IO events** provide real-time visual transitions (walking, typing)
- **Tasks table in Supabase** survives server restarts

---

## 2026-03-26 — Virtual Office Polish: NPCs, Decorations, Clock, Idle Wander, Widget UX

### Features Added

**NPC Office Staff (`frontend/lib/office-config.ts`):**
- Added `isNpc?: boolean` flag to `OfficeAgent` interface
- 10 NPC staff added (Receptionist, Office Manager, IT Support, HR Coordinator, Finance Analyst, Legal Counsel, Operations Lead, Product Manager, Data Analyst, Customer Success) — visual only, no AI model assigned, no tokens used
- NPCs do not attend CEO chat meetings (filtered by `!a.isNpc`)

**Virtual Office Canvas (`frontend/components/virtual-office/VirtualOffice.tsx`):**
- Re-added idle wandering for all agents (removed earlier): agents stroll around their department rooms using `STROLL_SPEED = 0.18` (separate from `WALK_SPEED = 0.35` for meetings)
- Thought bubbles appear when agents pause at idle spots (icons: `?`, `!`, `~`, `*`, `#`)
- Wave animation on agent hover (`waveTimer`)
- Name labels moved below feet; main agents get colored badge with white border
- Office decorations added: printer, sofa, trash bin, picture frames, sticky notes, cactus, wall screen with animated chart, bean bags — moved away from room label zones
- Real-time wall clock in top-left corner using browser timezone (analog face + digital readout)
- `MEETING_CHAIRS` array: 6 specific pixel positions around the conference table so agents sit in chairs rather than stacking

**Activity Ticker (`frontend/app/(dashboard)/office/page.tsx`):**
- Slowed from 30s → 120s → 600s animation duration (comfortable reading speed ~250 WPM)

**Floating Widget UX:**
- `frontend/lib/use-draggable.ts` — Added `storageKey` param; widget positions persisted to `localStorage` under `aria_widget_pos_{key}` and restored on page load. Validates saved position is still within viewport before restoring
- `frontend/components/shared/FloatingChat.tsx` — Passes `"ceo-chat"` storage key
- `frontend/components/virtual-office/OfficeKanban.tsx` — Passes `"task-board"` storage key; moved to dashboard layout so it persists across all page navigations
- Both widgets can be open simultaneously; closing one no longer closes the other (`data-floating-widget` attribute prevents mutual close-on-click-outside)
- CEO Chat widget z-index raised to `z-[60]` to stay above navbar

---

## 2026-03-26 — Gmail Integration: Send Emails from Authenticated Google Account

### Features Added

**Google OAuth scope expansion:**
- `frontend/app/(auth)/signup/page.tsx` and `login/page.tsx` — Added `scopes: "https://www.googleapis.com/auth/gmail.send"` and `queryParams: { access_type: "offline", prompt: "consent" }` to Google OAuth calls
- Users see a one-time consent screen: "ARIA wants to send emails on your behalf"

**Token capture and storage:**
- `frontend/app/auth/callback/page.tsx` — Captures `provider_token` and `provider_refresh_token` from Supabase session after OAuth, sends to backend via `POST /api/integrations/{tenant_id}/google-tokens`
- Tokens temporarily stored in `localStorage` for users who haven't completed onboarding yet

**Backend (`backend/server.py`):**
- `POST /api/integrations/{tenant_id}/google-tokens` — Stores Google OAuth tokens in `tenant_configs.integrations`
- `POST /api/email/{tenant_id}/send` — Send email via user's Gmail; auto-refreshes expired tokens using `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`

**`backend/tools/gmail_tool.py`** (new file):
- `send_email(access_token, to, subject, html_body, from_email)` — Sends via Gmail API using RFC 2822 MIME encoding + base64url
- `refresh_access_token(refresh_token)` — Exchanges refresh token for new access token
- Uses Python stdlib `email.mime` + `httpx` (no new dependencies)

**`backend/config/tenant_schema.py`:**
- Added `google_access_token` and `google_refresh_token` optional fields to `IntegrationsConfig`

**`backend/tools/__init__.py`:** — `gmail_tool` registered in `communication` category

**`.env.example`:** — Added `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` (same credentials as Supabase Google provider)

---

## 2026-03-26 — Email Marketer: Auto-Send via Gmail on Delegation

### Problem
- Email Marketer generated email copy but never actually sent it
- Relied on LLM outputting structured `send_email` blocks — fragile and unreliable
- `CONTEXT_KEY = "type"` but tasks arrived as `context={"action": "..."}` — task description never reached the agent properly

### Changes

**`backend/agents/email_marketer_agent.py`** (rewritten):
- Changed `CONTEXT_KEY` from `"type"` to `"action"` — task description now correctly read from context
- `_extract_recipient()` — regex scans task description for email addresses
- `_extract_subject_and_body()` — parses agent output in multiple formats: `SUBJECT: ... --- <body>`, `Subject: ...`, or first-line-as-subject fallback
- `_wrap_html()` — wraps plain text output in responsive HTML email template
- `run()` — after generating content, if task contains a recipient email address, automatically extracts subject + body and sends via Gmail. No LLM-structured blocks required
- System prompt explicitly tells agent to format output as `SUBJECT: ... --- <body>` when sending

**`backend/server.py`:**
- CEO system prompt detects if Gmail is connected and adds instruction: when user asks to send, delegate with "SEND:" prefix and recipient email in task description

### Result
- Tell CEO: "Send an email marketing strategy to user@example.com" → Email Marketer drafts it and sends automatically
- No send blocks, no manual steps — recipient email in task description is the trigger

---

## 2026-03-26 — Fully Automated Agent Lifecycle: Real-Time Kanban + Office Sync

### Problem
- Kanban board only refreshed when the panel was manually opened — no real-time updates
- New tasks from CEO chat delegation didn't appear on Kanban until user reopened the panel
- Completed tasks didn't auto-move to Done column
- Agent office status (idle/working) was not always in sync with actual task state

### Changes

**`backend/server.py`:**
- Emits `task_updated` Socket.IO event when a task is **created** (status: `in_progress`) during CEO chat delegation
- Emits `task_updated` Socket.IO event when a task is **completed** (status: `done`) in `_run_agent_to_inbox()` — both the content-produced and no-content branches
- Agent emits `idle` after task completion if no other in_progress tasks remain

**`frontend/lib/socket.ts`:**
- New `useTaskUpdates(tenantId)` hook — subscribes to `task_updated` Socket.IO events, returns latest `TaskUpdatePayload`

**`frontend/components/virtual-office/OfficeKanban.tsx`:**
- Uses `useTaskUpdates()` hook — applies real-time task updates to local state:
  - New task → added to Kanban immediately (appears in In Progress column)
  - Task status change → card moves to correct column automatically
- No longer requires panel re-open to see delegated tasks or completions

### Full automated flow
1. Tell CEO: "Send an email to user@example.com" → all agents walk to meeting room
2. CEO delegates → task appears **instantly** in Kanban as "In Progress" (Socket.IO)
3. Email Marketer walks to desk → status "Working" in virtual office
4. Email Marketer drafts email + sends via Gmail
5. Task moves to **"Done"** on Kanban automatically (Socket.IO)
6. Agent returns to **"Idle"** in virtual office
7. Content saved to Inbox

---

## 2026-03-27 — Gmail Integration Bug Fixes: Token Storage, Error Handling, Settings UI

### Problems
- New users who signed up via Google OAuth never had their Gmail tokens stored in the backend — tokens were stashed in localStorage during the callback (no `tenant_id` yet) but the onboarding completion flow never flushed them to the backend
- `gmail_tool.send_email()` crashed on any non-401 HTTP error (`raise_for_status()`) — errors like 400, 403, 429 propagated and crashed the entire agent background task
- When `_run_agent_to_inbox()` caught an agent crash, it only logged the error — no inbox item was created, task stayed stuck as "in_progress", user got no feedback
- When the Google refresh token expired (Testing mode: 7-day limit), the error message was a raw HTTP 400 with no guidance
- Settings > Integrations had no Gmail status — users couldn't see if Gmail was connected or reconnect it

### Changes

**`frontend/app/(onboarding)/select-agents/page.tsx`:**
- Added `flushGoogleTokens(tenantId)` — after onboarding creates `tenant_id`, reads `aria_google_token` / `aria_google_refresh_token` from localStorage and POSTs them to `/api/integrations/{tenant_id}/google-tokens`, then clears localStorage

**`backend/server.py`:**
- Added `GET /api/integrations/{tenant_id}/gmail-status` endpoint — returns `{connected, email}` for Settings UI
- In `_run_agent_to_inbox()`: email send status (success count or error) is now appended to inbox item content so users see ✅ or ⚠️
- In `_run_agent_to_inbox()` exception handler: now creates an inbox error item, marks the task as done, and emits `task_updated` so the Kanban doesn't get stuck

**`backend/tools/gmail_tool.py`:**
- `send_email()`: replaced `raise_for_status()` with structured error return for all 4xx/5xx — no more crashes
- `refresh_access_token()`: validates `GOOGLE_CLIENT_ID`/`SECRET` are set, returns Google's actual error description on failure instead of raw HTTP exception

**`backend/agents/email_marketer_agent.py`:**
- Wrapped send calls in try/except — network errors no longer crash the agent
- When refresh token is missing: clears stale access token and returns actionable error
- When refresh fails: clears both tokens from `tenant_configs` so `gmail-status` returns `connected: false`, error message says "reconnect Gmail in Settings > Integrations"

**`frontend/app/(dashboard)/settings/page.tsx`:**
- Integrations tab now shows Gmail as an active integration with live connection status
- "Connect Gmail" button triggers Google OAuth re-authentication when disconnected
- Future integrations moved to "Coming Soon" section

### Token lifecycle (post-fix)
- Google access tokens expire after 1 hour — backend auto-refreshes using stored refresh token
- For published OAuth apps: refresh token never expires — email sending works indefinitely
- If refresh fails: tokens are cleared, Settings shows "Not connected", error in inbox tells user to reconnect

---

## 2026-03-30 — Onboarding Error Fixes: JSON Parsing, Socket.IO, Column Safety

### Problems
- Deployed `select-agents` page returned error "Cannot connect to the backend server" because multiple backend crashes on `/api/onboarding/save-config` and `/api/onboarding/extract-config`
- Root causes:
  1. `server.py` had no module-level `logger` — `except` blocks that tried `logger.warning()` crashed with `NameError`, turning recoverable errors into 500s
  2. `onboarding_agent.py` `extract_config()` called `json.loads()` on LLM output without any error handling — when Haiku returned JSON with unescaped quotes/newlines, it crashed with `JSONDecodeError`
  3. `sio.enter_room()` in `server.py` was missing `await` — caused `RuntimeWarning: coroutine never awaited`
  4. Supabase `tenant_configs` table didn't have `gtm_profile` column — save crashed with `PGRST204: Could not find 'gtm_profile' column`

### Changes

**`backend/server.py`:**
- Added `logger = logging.getLogger("aria.server")` at module level (after `load_dotenv()`)
- Changed `sio.enter_room(sid, tenant_id)` → `await sio.enter_room(sid, tenant_id)`
- Wrapped `extract_config()` endpoint in try/except — on any exception, returns fallback config from conversation messages instead of 500

**`backend/onboarding_agent.py`:**
- Added `logger = logging.getLogger("aria.onboarding")` and `import logging`
- Replaced brittle JSON extraction with:
  - `_extract_json()`: properly tracks brace depth + string boundaries (not naive `find/rfind`)
  - `_repair_json()`: handles markdown fences, JS comments, trailing commas, control chars, unescaped newlines in strings
  - `_try_parse_json()`: attempts parse with original, then repaired JSON
  - `_fallback_config_from_messages()`: if both LLM attempts fail, builds minimal config directly from user messages (never crashes)
- `extract_config()` now has two LLM attempts with stricter second prompt, then falls back to conversation-based config

**`backend/config/loader.py`:**
- Added `save_tenant_config()` retry loop: on PGRST204 (missing column), extracts the column name from error message, strips it from data, and retries (up to 5 times)
- Gracefully handles new schema fields that don't exist in Supabase yet

### Result
- Onboarding flow now works end-to-end without crashing
- When LLM produces invalid JSON, the fallback ensures users still get a usable config (built from their answers)
- Missing database columns don't block onboarding — data is safely filtered before insert
- Frontend "Launch ARIA" button succeeds after completing the flow

---

## 2026-03-30 — Real-time Email Sync + Gmail OAuth Fixes

### Problems
- Inbound email replies from recipients were not automatically captured — only visible if user manually clicked "Sync Gmail" or cron ran
- Conversations page had no way to see replies in real-time
- Inbox page didn't refresh when replies arrived
- 403 Gmail API errors when sync tried to read threads — insufficient OAuth scopes
- Background Gmail sync crashed with error "column tenant_configs.is_active does not exist"
- Email sending didn't notify the conversations page to update thread status

### Changes

**`backend/tools/gmail_sync.py`:**
- Modified `sync_tenant_replies()` to collect `new_replies` list with `{thread_id, sender, subject, snippet, inbox_item}` data for Socket.IO emission
- Modified `_create_inbox_item_for_reply()` to log created items for debugging
- Modified `_ensure_access_token()` to auto-refresh when only `refresh_token` exists (no access token yet)
- Fixed `sync_all_tenants()` to check both `google_access_token` AND `google_refresh_token` (was only checking access token)

**`backend/server.py`:**
- Added `import asyncio` and `_gmail_sync_loop()` background task that runs every 2 minutes, syncing all tenants and emitting socket events
- Added `_emit_sync_events(tenant_id, sync_result)` helper — reusable function that emits `inbox_new_item` + `email_reply_received` socket events for all new replies (eliminates duplicate code across 3 sync endpoints)
- Modified `/api/email/{tenant_id}/sync` to call `_emit_sync_events()` after sync
- Modified `/api/email/sync-all` to call `_emit_sync_events()` for each tenant
- Modified `/api/cron/run-scheduled` to call `_emit_sync_events()` for each tenant after sync
- Modified `approve_and_send_email()` to emit `email_thread_updated` socket event after sending so conversations page refreshes
- Fixed `get_active_tenants()` — removed filter on non-existent `is_active` column, now returns all tenants

**`frontend/app/(dashboard)/conversations/page.tsx`:**
- Added Socket.IO listeners for `email_reply_received`, `email_thread_updated`, `inbox_item_updated` — triggers real-time refresh of threads and current thread messages
- Added `selectedRef` useRef to track selected thread across render cycles (needed for auto-poll)
- Added 30-second auto-poll interval on conversations page — calls `emailThreads.sync()` + refreshes thread list and selected thread messages

**`frontend/app/(auth)/{login,signup}/page.tsx` + `frontend/app/(dashboard)/settings/page.tsx`:**
- Updated OAuth scopes to include `https://www.googleapis.com/auth/gmail.readonly` alongside `gmail.send` (required for sync to read threads and messages)

**`backend/config/loader.py`:**
- Fixed `get_active_tenants()` to not filter by `is_active` column (doesn't exist in schema)

### Result
- Replies are now caught automatically **every 2 minutes** via background sync loop
- When using conversations page, replies appear **in real-time** via socket events + 30s auto-poll
- Inbox auto-refreshes when new reply inbox items are created
- Email thread status updates immediately after sending
- No manual "Sync Gmail" button needed (though it's still available)
- Users must reconnect Gmail once to grant the new `gmail.readonly` scope

### Real-time flow
1. User sends email via ARIA → Thread tracked in `email_threads` table
2. Recipient replies → Gmail stores it in same thread
3. Background sync (every 2min) or conversations page auto-poll (every 30s) detects reply
4. Socket.IO emits `email_reply_received` + `inbox_new_item` events
5. Frontend listens and instantly refreshes:
   - Inbox: shows new reply as high-priority item
   - Conversations: thread moves to "needs_review", messages list updates
6. User clicks thread → sees full conversation with reply visible

---

## 2026-03-31 — Notification System, Email Editor, Voice, CRM, Token Optimization, Usage Dashboard

### Notification Badge System + Real-Time Toasts + Bell Panel
- Backend: `_notify()` helper persists to `notifications` table + emits `notification` socket event
- API: `/api/notifications/{tenant_id}/counts`, list, mark-read, mark-seen endpoints
- Wired into inbox creation, email send, and inbound reply sync
- Frontend: `NotificationProvider` context with socket listener, badge counts, toast queue
- Sidebar badges: dynamic unread counts on Inbox and Conversations tabs
- `NotificationBell` component: dropdown panel with notification history, mark-all-read
- `ToastContainer`: animated slide-in toasts for high-priority events
- Browser notifications for granted permissions
- Desktop header bar with bell icon (increased to w-7 h-7)

### Email Editor — HTML Design Preservation
- Replaced Tiptap with contentEditable iframe approach
- 3-tab editor: Edit (click text to edit, styling preserved), Preview (read-only), Source (dark monospace HTML editor)
- Backend: `POST /api/email/{tenant_id}/update-draft` saves edited to/subject/html_body
- `_wrap_html()` now detects HTML fragments (tables, styled divs) and wraps without mangling
- Agent system prompt updated to require complete inline-styled HTML with table-based layout
- Action buttons (Approve & Send, Save, Cancel) moved above content for quick access
- Sent email now matches preview — no styling loss

### Voice Input + Text-to-Speech
- `use-voice.ts` hook: `useSpeechToText` (mic input) + `useTTS` (read aloud)
- Added to CEO Chat page, FloatingChat widget, and Onboarding /describe page
- Voice auto-sends on speech end (no need to click Send)
- TTS auto-reads new AI responses aloud
- Mute/unmute toggle button persisted in localStorage (`aria_tts_enabled`)
- Per-message speaker buttons still work when auto-read is off

### Agent Token Optimization
**All agents compressed for minimal token usage:**
- CEO: filtered CONTEXT_FIELDS from 9 to 4, prompt compressed 50%, MAX_TOKENS 2000→1500
- Content Writer: dynamic model (Haiku for short-form, Sonnet for long-form), MAX_TOKENS 4000→2000/3000
- Email Marketer: switched to Haiku (60% cost reduction), HTML instructions compressed from 200→40 tokens
- Social Manager: prompt compressed 40%, already on Haiku
- Ad Strategist: MAX_TOKENS 2000→1500, prompt compressed 50%

**CEO Chat context compaction:**
- Business context uses `agent_brief` (~150 tokens) instead of 25-line field dump (~600 tokens)
- Conversation summarization: last 6 messages in full, older messages truncated to 100-char summaries
- Sub-agent docs truncated to 200 chars each (was full .md files ~1500 tokens)
- CEO .md capped at 800 chars

### Per-Agent Token Limits + Usage Dashboard
**Backend:**
- `AGENT_HOURLY_LIMITS`: CEO 30req/80k tokens, Content 10/50k, Email 15/40k, Social 10/30k, Ads 10/30k
- `call_claude()` accepts `agent_id`, enforces per-agent limits
- `GET /api/usage/{tenant_id}` returns tenant totals + per-agent breakdown

**Frontend `/usage` page:**
- 4 stat cards: requests, total tokens, input tokens, output tokens
- Overall hourly limit progress bars
- Per-agent breakdown grid with color-coded cards
- Progress bars turn red at 80%+ usage
- Auto-refreshes every 15 seconds

### Lightweight CRM
**Backend (19 API endpoints):**
- CRUD for `crm_contacts`, `crm_companies`, `crm_deals`, `crm_activities`
- Pipeline summary endpoint with stage counts and values
- Auto-logs activities on contact/deal creation and stage changes

**Frontend `/crm` page:**
- 3 tabs: Contacts, Companies, Deals
- Contacts: searchable table, status filter (Lead/Prospect/Customer/Churned), inline status change
- Companies: searchable table with domain/industry/size
- Deals: kanban pipeline board with 6 stages (Lead→Qualified→Proposal→Negotiation→Won/Lost)
- Pipeline summary bar with counts and values per stage
- Add modals for each entity with form validation

**CEO CRM Awareness:**
- Keyword-triggered CRM context injection (scans for "contact", "deal", "lead", "follow up with", etc.)
- Only when triggered: fetches top 20 contacts + 10 deals in compact 1-line format (~5 tokens/record)
- Zero CRM tokens on non-CRM conversations

### Agents Page Cleanup
- Removed Paperclip status badge, footer, and all Paperclip references
- Removed non-functional "Run Now" button (used hardcoded demo tenant)
- Fixed toggle to use actual tenant_id

### Supabase Tables Added
- `notifications`: id, tenant_id, type, category, title, body, href, priority, is_read, is_seen
- `email_draft` column on `inbox_items` (JSONB)
- `crm_contacts`: name, email, phone, status, source, tags, company_id
- `crm_companies`: name, domain, industry, size
- `crm_deals`: title, value, stage, contact_id, company_id, expected_close
- `crm_activities`: type, description, metadata, contact_id, deal_id

---

## 2026-04-01 — WhatsApp Cloud API Integration

### Changes

**Backend:**
- `backend/tools/whatsapp_tool.py` — New WhatsApp Cloud API tool: `send_message()`, `send_template()`, `get_business_profile()` via Meta's Graph API v21.0
- `backend/server.py` — Added 6 WhatsApp endpoints:
  - `GET /api/whatsapp/webhook` — Meta webhook verification (hub.challenge)
  - `POST /api/whatsapp/webhook` — Receives incoming WhatsApp messages, stores in inbox
  - `POST /api/whatsapp/{tenant_id}/send` — Send message from tenant's WhatsApp
  - `POST /api/whatsapp/{tenant_id}/connect` — Save & verify credentials
  - `POST /api/whatsapp/{tenant_id}/disconnect` — Remove credentials
  - `GET /api/integrations/{tenant_id}/whatsapp-status` — Connection status
- `backend/config/tenant_schema.py` — Added `whatsapp_access_token`, `whatsapp_phone_number_id`, `whatsapp_business_account_id` to IntegrationsConfig

**Frontend:**
- `frontend/app/(dashboard)/settings/page.tsx` — WhatsApp connect card with credentials form (Phone Number ID, Business Account ID, Access Token), verify & save, disconnect
- `frontend/app/(dashboard)/inbox/page.tsx` — WhatsApp message rendering with green chat bubble + reply box
- `frontend/lib/api.ts` — `whatsapp.connect()`, `whatsapp.disconnect()`, `whatsapp.send()`

---

## 2026-04-01 — LinkedIn OAuth Integration + Publishing

### Changes

**Backend:**
- `backend/tools/linkedin_tool.py` — LinkedIn API tool: OAuth 2.0 flow, `get_profile()`, `get_admin_organizations()`, `create_post()` via v2 UGC Posts API
- `backend/server.py` — Added LinkedIn endpoints:
  - `GET /api/auth/linkedin/connect/{tenant_id}` — Start OAuth flow
  - `GET /api/auth/linkedin/callback` — Handle OAuth callback
  - `GET /api/integrations/{tenant_id}/linkedin-status` — Connection status with posting target
  - `GET /api/linkedin/{tenant_id}/organizations` — List admin company pages
  - `POST /api/linkedin/{tenant_id}/set-target` — Switch between personal/company posting
  - `POST /api/linkedin/{tenant_id}/post` — Publish post
- `backend/config/tenant_schema.py` — Added `linkedin_member_urn`, `linkedin_name`, `linkedin_org_urn`, `linkedin_org_name`

**Frontend:**
- Settings page — Connect LinkedIn button (blue, OAuth popup), company page selector
- Inbox page — "Publish to LinkedIn" button alongside "Publish to X"
- Social Manager agent updated to generate 2 posts per run (tweet + LinkedIn post)

---

## 2026-04-01 — Reconnect Buttons for All Integrations

### Changes
- Gmail, Twitter/X, LinkedIn, WhatsApp all show "Reconnect" link when connected in Settings
- Allows re-authentication without disconnecting first

---

## 2026-04-01 — Agent-to-Inbox Race Condition Fix

### Problem
When CEO chat delegated a task, the task appeared on the board but the inbox item didn't exist yet because:
1. 4-second meeting animation delay before agent started
2. Agent took 5-15 seconds to call Claude API
3. Frontend navigated to inbox and found nothing

### Changes
- Create **placeholder inbox item** immediately with status "processing" and text "Agent is working on..."
- Reduced meeting delay from 4s to 1s
- Placeholder updated in-place with real content when agent finishes
- Added "processing" / "In progress" status tab and purple badge to Inbox
- `inbox_item_updated` socket event triggers auto-refresh

---

## 2026-04-01 — Security Implementation

### Changes

**Authentication:**
- `backend/auth.py` — New auth module: Supabase JWT verification, tenant ownership checks, rate limiting
- Middleware verifies Bearer token on all `/api/` routes
- Tenant ownership check: authenticated user must own the tenant_id in the URL
- Dev mode: auth bypassed when `SUPABASE_JWT_SECRET` not set

**CORS Lockdown:**
- Restricted from `allow_origins=["*"]` to specific domains (localhost:3000, Vercel URL)
- Configurable via `CORS_ALLOWED_ORIGINS` env var
- Socket.IO CORS also restricted

**Rate Limiting:**
- 120 requests/minute per IP on all API endpoints
- In-memory sliding window implementation

**XSS Fix:**
- All OAuth callback error pages now use HTML-escaped `_safe_oauth_error()` helper
- Replaced 5 vulnerable `alert()` injections with safe HTML rendering

**Frontend Auth Headers:**
- `frontend/lib/api.ts` — `fetchAPI()` now includes `Authorization: Bearer` from Supabase session
- Settings, Inbox, Dashboard layout all pass auth headers on direct fetch calls
- CEO chat hook passes auth headers

**Public Paths (exempt from auth):**
- `/health`, `/api/onboarding/*`, `/api/auth/*` (OAuth callbacks), `/api/whatsapp/webhook`, `/api/webhooks/*`

---

## 2026-04-01 — CEO Agent CRUD Powers with Confirmation Rules

### Changes

**Backend:**
- `backend/ceo_actions.py` — Action registry with allowlisted business operations:
  - CRM CRUD (contacts, companies, deals)
  - Inbox management (update status, delete)
  - Social publish, email send
  - Task status updates
- Forbidden scope enforcement: CEO cannot modify code, prompts, backend, schema, infra
- `is_forbidden_request()` checks against 30+ forbidden patterns
- Confirmation matrix: DELETE/UPDATE always require confirmation, CREATE is direct, PUBLISH/SEND require confirmation
- Audit logging: all CEO actions logged to `agent_logs` with params, confirmation status, timestamp
- `POST /api/ceo/{tenant_id}/action` — Execute business actions with confirmation enforcement

**Frontend:**
- `frontend/components/shared/ConfirmationDialog.tsx` — Modal with destructive (red) and safe (purple) variants
- `frontend/lib/use-ceo-chat.ts` — Added `pendingConfirmation`, `confirmAction()`, `cancelAction()` to chat hook
- `frontend/components/shared/FloatingChat.tsx` — Confirmation dialog wired into CEO chat

**Agent Docs Updated:**
- `docs/agents/ceo.md` — Added CRUD powers table, confirmation matrix, forbidden scope constraints, example allowed/refused requests
- `docs/agents/social_manager.md` — Added Twitter/LinkedIn publishing, content adaptation pipeline, inbox approval flow
- `docs/agents/email_marketer.md` — Fixed "does NOT send" to document Gmail sending, draft approval flow, recipient extraction
- `docs/agents/content_writer.md` — Added dynamic model selection (Sonnet/Haiku), content-to-social pipeline

---

## 2026-04-01 — Privacy Policy & Terms of Service

### Changes
- `frontend/app/(marketing)/privacy/page.tsx` — Full privacy policy covering data collection, connected services, security measures, data retention, user rights, third-party links
- `frontend/app/(marketing)/terms/page.tsx` — Terms of service covering acceptable use, AI content ownership, approval system, subscriptions, liability
- Both pages redesigned with dark hero header, card-based sections with lucide icons
- URLs: `/privacy` and `/terms` — used for Google OAuth verification and X Developer Portal

---

## 2026-04-10 — Paperclip integration: full debugging arc + working architecture

This was a long single-day debugging session that touched almost every part of the Paperclip integration. Recording here so the trail is preserved.

### Symptoms at session start
- CEO Timer runs ending with `fetch failed (adapter_failed)`
- Skills tab grayed out with "Paperclip cannot manage skills for this adapter yet"
- Chat messages either echoed framing back as the agent's reply, or never produced an inbox item
- One chat creating two Paperclip runs (one always cancelled by control plane)
- Agent runs hanging for 9-10 minutes then exiting with code 143

### Root causes (each took multiple commits to find)

**1. HTTP adapter experiment was leaking state.** Earlier in the week we'd flipped agents to Paperclip's `http` adapter to try a webhook receiver in ARIA (`backend/routers/paperclip.py`). It bypassed Paperclip's skill system entirely (Skills tab grayed out, no `aria-backend-api` skill access) and required ARIA to expose `/api/paperclip/heartbeat/{agent_name}`. We reverted to claude_local, but the live agents in Paperclip stayed on `http` and `paperclip_sync.py` wasn't flipping them back. Manual fix in Paperclip's UI per agent: Configuration → Adapter type → Claude (local).

**2. The "Skip permissions" toggle in Paperclip's Configuration tab is BROKEN on our version.** It shows green/ON but doesn't inject `--dangerously-skip-permissions` into the spawned `claude` command. Without the flag, every Bash/Write/curl tool call inside the agent's CLI hits a permission prompt with no human to approve, so the run hangs for the full 10-minute timeout and SIGTERMs (exit code 143). Smoking-gun transcript: an agent run literally said "The sandbox keeps blocking me. Let me try a completely different approach" and looped on Write tool calls all returning `Claude requested permissions to write to ..., but you haven't granted it yet`.

   **Fix:** in Paperclip → each agent → Configuration → **Extra args (comma-separated)** field, set `--dangerously-skip-permissions`. The Command field stays as just `claude` (it's a binary path, not a command line). After saving, the agent's Command in subsequent runs reads `claude --print - --output-format stream-json --verbose --model claude-opus-4-6 --append-system-prompt-file ... --dangerously-skip-permissions`. Done for all 6 agents.

**3. Skill MD told the agent to fetch dashboard config first.** The `aria-backend-api` skill MD attached to every agent had a `## Get Business Context First` section that did `curl http://72.61.126.188:8000/api/dashboard/{tenant_id}/config`. That endpoint requires JWT auth, the agent has no JWT, the call returns 401, the agent gives up and never reaches the inbox write. Updated the skill MD in Paperclip's Skills UI to: (a) remove all references to auth-protected endpoints, (b) keep only the `POST /api/inbox/{tenant_id}/items` write, (c) use `http://172.17.0.1:8000` (docker host IP) instead of the public IP so requests bypass nginx and go straight to FastAPI where `/api/inbox/` is in `_PUBLIC_PREFIXES`.

**4. CEO chat sync route was creating double runs.** `run_agent_via_paperclip_sync` was both posting a comment AND calling `/heartbeat/invoke`. Both wake the agent — the comment via `wakeOnDemand: true` (Automation run) and the heartbeat directly (On-demand run). `maxConcurrentRuns: 1` then races them and cancels one. Fix: removed the manual `/heartbeat/invoke` call. Posting the comment alone is enough.

**5. Chat handler was echoing the framing back as the agent's reply.** `_build_chat_comment_body` was packing curl examples and instructions into the same comment as the user message. The Paperclip CEO interpreted that as "content to reformat" and posted the framing wrapper back as its reply. Two-part fix: (a) trimmed `_build_chat_comment_body` to a minimal `[tenant_id=<uuid>]\n\n<message>` shape, (b) added a defensive prefix-filter in `pick_agent_output()` that skips any comment starting with `[tenant_id=`, `TENANT_ID:`, or `USER MESSAGE:`.

**6. Almost deleted the poller for the wrong reason.** Mid-session we deleted `poll_completed_issues()` (commit `4c7b94d`) thinking the agent's skill curl (Path A) was the active write path and the poller (Path B) was redundant duplication. The user's "yes, 3 days ago and before I added the media agent" the inbox flow worked — and that was deep in the poller era. Restored `poll_completed_issues` from git history into `paperclip_office_sync.py` (commit `530cba0`) along with all its helpers. Both paths now coexist: Path A is primary, Path B is the safety net. Dedupe is via the `paperclip_issue_id` column.

### Architecture refactor (kept after the debugging dust settled)

- **Deleted** `backend/paperclip_sync.py` (320 lines) and `backend/paperclip_skill.py`. Their startup automation kept fighting manual Paperclip configuration. The 4 useful exports (`PAPERCLIP_URL`, `_urllib_request`, `get_company_id`, `get_paperclip_agent_id`, `paperclip_connected`) are now inlined into `backend/orchestrator.py`. ARIA no longer touches Paperclip's API at startup.
- **Renamed** `backend/paperclip_poller.py` → `backend/paperclip_office_sync.py` to reflect that it does both inbox import (`poll_completed_issues`) and Virtual Office sync (`sync_agent_statuses`) in the same 5s background loop.
- **New** `backend/services/paperclip_chat.py` with `pick_agent_output()` and `normalize_comments()` — pure functions shared by both the chat sync route and the poller. Includes the framing-prefix filter (`[tenant_id=`, `TENANT_ID:`, `USER MESSAGE:`) so ARIA's own chat wrapper never gets re-imported as the agent's reply.
- **MediaAgent refactor** — uses `services/inbox.create_item()` and the new `services/content_library.create_entry()` shared services instead of bespoke `_save_to_inbox` / `_log_to_content_library` helpers. Failure paths consolidated via a `_fail()` method.
- **CEO chat history summarization** — `_summarize_ceo_assistant_message()` replaces verbatim prior CEO turns with one-line tags like `[CEO previously delegated to media]` so the model can't plagiarize its own prior outputs in the next turn.
- **CEO agent f-string crash fix** — `{agent: "media", ...}` inside an f-string was throwing NameError on every `build_system_prompt` call. Escaped to `{{"agent": "media", ...}}`.
- **Semantic cache key bug** — `_prompt_key()` was truncating the system prompt to 200 chars before embedding. Every CEO chat call shared the same 200-char prefix so unrelated user messages collided on the cache and returned identical replies. Now hashes the FULL system prompt with MD5.
- **CEO chat session rotation** — frontend localStorage key bumped to `_v2` and got a 30-min idle timeout, so old test sessions don't bleed historical context into new conversations.
- **GitHub auto-deploy webhook** — re-enabled by replacing the placeholder `REPLACE_WITH_A_LONG_RANDOM_STRING` in `/etc/webhook.conf` with a real HMAC secret + adding the same secret to GitHub's webhook config. The deploy.sh on the VPS was also rewritten (the original ran every command twice).

### Known good architecture (as of end of session)

- **Adapter:** all 6 agents on `claude_local` in Paperclip's UI
- **Permissions:** `--dangerously-skip-permissions` set in each agent's Extra args field (NOT the broken Skip permissions toggle)
- **Skills:** `aria-backend-api` checked under each agent's Skills tab; the skill MD references ONLY the `/api/inbox/` write endpoint, no other ARIA endpoints; uses `172.17.0.1:8000` not the public IP
- **Inbox path A (primary):** agent's spawned CLI runs `curl POST http://172.17.0.1:8000/api/inbox/{tenant}/items` — `/api/inbox/` is in `_PUBLIC_PREFIXES` so no JWT required
- **Inbox path B (safety net):** `paperclip_office_sync.poll_completed_issues` runs every 5s, scrapes finished issue comments, dedupes via `paperclip_issue_id` column
- **CEO chat:** posts user message as comment on a fresh CEO issue, polls comments every 1-4s (adaptive backoff), 60s timeout, falls back to local `call_claude` if Paperclip is unreachable
- **Verification end-to-end:** "create a social media post based on my GTM" → Paperclip CEO run completes in ~90s → Social Manager subagent → inbox row appears with title "Twitter/X Post: Build in Public — Developer Founder GTM" + Publish to X/LinkedIn buttons wired up.

---

## 2026-04-11 — Backend efficiency audit (27 items, 4 commits) + VPS auto-deploy webhook

Two parallel tracks in one session: shipped a full backend performance audit then wired up the GitHub→Hostinger auto-deploy webhook that had been sitting broken.

### Backend perf audit — 27 items across 4 commits

Grouped 27 audit items into 4 thematic commits on `main`:

**Batch 1 — concurrency unlock + poller backoff** (`d46355a`)
- `dashboard_stats` was 4 sequential sync Supabase queries (200-800ms total). Rewrote to run in parallel via `asyncio.gather` + `asyncio.to_thread` — 4x faster.
- `_paperclip_office_sync_loop` was firing every 5s 24/7 (~17,000 calls/day) even when idle. Added adaptive backoff: 5s active → 30s idle after 6 empty cycles. Added `poke_paperclip_poller()` asyncio.Event so chat handler / inbox routes can reset the interval when the user does something. 70-80% fewer idle hits.
- Watcher hot path (`_dispatch_paperclip_and_watch_to_inbox`) wrapped every sync DB call in `asyncio.to_thread` so it stops blocking the event loop on each tick.
- Inbox poller (`poll_completed_issues`) — existence-check SELECT, insert, and notifications INSERT all wrapped in `to_thread`. Returns count so the adaptive loop knows work happened.
- `_gmail_sync_loop` audit item was a false alarm — the pre-check for tenants without Gmail already existed in `gmail_sync.py:282`.

**Batch 2 — CEO chat token diet + dead code removal** (`d789cdb`)
- CEO chat system prompt trimmed ~30%: cached `_CEO_ACTION_DESCRIPTIONS` at module load (was rebuilt from `ACTION_REGISTRY` on every chat request), deduped the "do not auto-act" rule (was repeated 4x in 80 lines), dropped verbose EXAMPLES blocks, consolidated delegation/action rules into single sections.
- CRM context heuristic tightened with word-boundary regexes (`_CRM_NOUN_RE` / `_CRM_VERB_RE`). Previous substring matching was firing on "ideal"→"deal", "leader"→"lead", "calling"→"call", "client product"→"client" — inflating CRM context injection (~1.5k tokens) on totally unrelated questions. Also dropped the too-loose "customer" and "client" nouns from the list.
- Deleted ~140 lines of dead `run_agent_via_paperclip_sync` chain — `run_agent_via_paperclip_sync`, `_create_chat_issue`, `_post_chat_comment`, `_build_chat_comment_body`, `_poll_for_agent_reply`, `_POLL_INTERVALS`. CEO chat calls `call_claude` with Haiku directly now; the Paperclip sync path was the original 10-30s slow path replaced in an earlier session. Updated CLAUDE.md and `paperclip_chat.py` docstring to match.

**Batch 3 — N+1 fix + partial writes + cached helpers** (`07c626b`)
- `get_agent_status` N+1 unwound: was 12 sequential round-trips for 6 active agents (1 `agent_logs` SELECT + 1 Paperclip GET per agent), with SELECTs blocking the event loop via sync Supabase. Rewrote as 1 batched SELECT (`.in_("agent_name", active)`) grouped in Python + parallel Paperclip GETs via `asyncio.gather`.
- Added `update_tenant_integrations(config)` helper in `config/loader.py` that writes ONLY the `integrations` JSONB column instead of upserting the entire 30+ field row through `save_tenant_config`'s column-strip retry loop. `gmail_sync._ensure_access_token` now uses the targeted helper for all 5 token-refresh paths — meaningful DB write reduction across tenants during the 2-min Gmail sync loop. Helper also primes the in-memory cache instead of invalidating.
- httpx.AsyncClient singleton + module-level SSL context in `orchestrator.py`: `_paperclip_api` was opening a fresh `httpx.AsyncClient` on every call (TCP+TLS handshake each time) — now uses `_get_httpx_client()` singleton closed on shutdown via `close_httpx_client()` wired into `server.py` lifespan. `_urllib_request` was calling `ssl.create_default_context()` per request (~1-3ms on Windows) — now uses module-level `_SSL_CTX` cached at import.
- Bounded `_config_cache` (max 500 entries) with insertion-order eviction — was unbounded.
- Lazy log formatting (`logger.X(f"...")` → `%s`) in orchestrator.py hot paths.
- Hoisted `_DELEGATE_BLOCK_RE` / `_ACTION_BLOCK_RE` regexes to module scope + removed inline `import re` calls in the chat handler.

**Batch 4 — regex hoist + lazy import cleanup + cache caps** (`d34ba61`)
- Hoisted 20 email-template regex patterns in `_wrap_email_in_designed_template` and `_strip_html_to_text` to module-level `_EMAIL_*_RE` / `_STRIP_*_RE` constants. Was recompiling 12 patterns on every inbox row build.
- Moved lazy imports inside hot paths up to module-level: `dispatch_agent`, `get_paperclip_agent_id`, `_sanitize_error_message`, `_urllib_request`, `PaperclipUnreachable`, `AGENT_REGISTRY`, `normalize_comments`, `pick_agent_output`, `_is_finished`, `_is_failed`, `_add_processed`, `poll_completed_issues`, `sync_agent_statuses`. Removed inline imports inside `_dispatch_paperclip_and_watch_to_inbox`, `_run_agent_to_inbox` error path, CEO chat dispatch loop, and paperclip office sync loop.
- Bounded remaining caches: `_live_agent_status` (server.py) capped at 1000 tenants; `_usage_cache` + `_agent_usage` (claude_cli.py) both capped at 1000 via `_usage_cache_set` helper.
- `_CLAUDE_BIN = shutil.which("claude")` cached at module load in `claude_cli.py` — was letting `create_subprocess_exec` do PATH lookup on every CLI call.

### `_re_crm` import-order hotfix (`73b8618`)

Batch 4 shipped with a bug that crash-looped the backend container in production. The error:
```
File "/app/backend/server.py", line 5388, in <module>
    _DELEGATE_BLOCK_RE = _re_crm.compile(r"```delegate\s*\n(.*?)\n```", _re_crm.DOTALL)
NameError: name '_re_crm' is not defined
```

Root cause: I added the pre-compiled `_DELEGATE_BLOCK_RE` / `_ACTION_BLOCK_RE` block BEFORE the `import re as _re_crm` line (which lived inside the CRM-heuristic block further down). At module-execution time `_re_crm` didn't exist yet. `py_compile` passed because the name is a runtime lookup, not a parse-time check.

Fix: hoisted `import re as _re_crm` above both regex blocks so every reference sees a defined alias.

**Lesson saved to CLAUDE.md common issues table:** `py_compile` cannot catch forward-reference `NameError` in module-level code. Always visually verify that import aliases are defined BEFORE the first use when hoisting blocks around.

### VPS auto-deploy webhook — wired up end-to-end

Background: a GitHub webhook was half-configured on the repo (`http://72.61.126.188:9000/hooks/deploy-aria`) but the HMAC secret was empty and systemd verbose logging was off, so pushes weren't actually deploying and there was no visibility into why.

**Fixes applied this session:**

1. **Secret sync.** Generated shared secret `absolutemadness` (user picked it), replaced `REPLACE_WITH_A_LONG_RANDOM_STRING` in `/etc/webhook.conf` via `sed -i`, pasted the same string into the GitHub webhook Secret field. Restarted `webhook.service`.
2. **Verbose logging.** Added a systemd drop-in at `/etc/systemd/system/webhook.service.d/override.conf` with `ExecStart=/usr/bin/webhook -nopanic -hooks /etc/webhook.conf -port 9000 -verbose`. The interactive `systemctl edit webhook` flow silently failed to save the first time — wrote the file directly via heredoc instead. `journalctl -u webhook -f` now shows every incoming POST.
3. **`deploy.sh` rewrite.** The existing `/opt/aria/deploy.sh` had TWO stacked command blocks (pre-existing bug from a prior session that got re-introduced — see 2025-era log entry about "the original ran every command twice"). Rewrote cleanly with `set -euo pipefail`, single-pass, rebuilds backend + frontend (user confirmed both are real containers on the VPS — frontend is NOT on Vercel as CLAUDE.md used to suggest):
   ```bash
   #!/bin/bash
   set -euo pipefail
   cd /opt/aria
   git pull origin main
   docker compose up -d --build backend frontend
   ```

**Verification end-to-end:** `git commit --allow-empty -m "test: webhook deploy" && git push origin main` → webhook fires in ~2s → `deploy-aria got matched` → HMAC validates → `200 OK` in 280µs → `deploy.sh` runs → `git pull` → all Docker layers CACHED (empty commit) → containers recreated → `[deploy] done at 2026-04-11T21:32:59Z`, **4 seconds total**.

### Stack confirmed on the VPS
- **aria-backend** — python:3.11-slim, uvicorn serving FastAPI on port 8000
- **aria-frontend** — node:20-alpine, Next.js standalone build on port 3000 (NOT Vercel)
- **aria-nginx** — nginx:alpine, reverse proxy on 8080/8443
- **aria-qdrant** — qdrant:latest on 6333 (semantic cache)
- **aria-redis** — redis:7-alpine on 6379

The frontend running as a container on the VPS is the biggest correction from this session's discovery — previous CLAUDE.md text said "frontend is on Vercel" which is no longer accurate. Updated.

### Known-good end state
- All 4 audit commits deployed and the backend container is `Up` after the hotfix
- Auto-deploy is live: every `git push origin main` triggers a backend + frontend rebuild via the webhook
- Typical cycle: ~4s (no-op), ~20-40s (backend code), ~1-3min (deps change)
- `journalctl -u webhook -f` is the single source of truth for what the deploy is doing
