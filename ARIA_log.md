# ARIA Change Log

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
