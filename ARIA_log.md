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
