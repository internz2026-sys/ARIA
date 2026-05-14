# Ad Strategist Graph Validation — 672fac0
**Run at:** 2026-05-01T11:34:50Z

## Result
- ✅ Task fired successfully — CEO received prompt, delegated to Ad Strategist within ~30s
- ✅ Ad Strategist run completed within 420s (~7 minutes; agent was actively running)
- ❌ Inbox row contains NO rendered chart image
- ❌ Chart is absent — no `[GRAPH_DATA]` text and no PNG image in the detail pane

## Screenshots
- `01-dashboard-load.png` — Dashboard loaded, session active (SMAPS-SIS tenant)
- `02-chat-page.png` — CEO Chat page at /chat, new conversation started
- `03-message-sent.png` — Message sent: "Create a Facebook ad campaign for SMAPS-SIS targeting school administrators"; CEO thinking indicator visible
- `04-wait-30s.png` — CEO replied at ~30s, delegation block to Ad Strategist (high priority) visible
- `05-wait-60s.png` through `14-inbox-refresh-7m.png` — Inbox refresh snapshots showing "In progress..." placeholder at 1m–7m mark
- `15-inbox-detail-pane.png` — Ad Strategist row clicked open: "SMAPS-SIS Facebook Ad Campaign - School Administrator Targeting (Philippines)"
- `16-inbox-detail-fullpage.png` — Full-page screenshot of detail pane: 17,587 chars of text, no image visible
- `17-inbox-detail-bottom.png` — Bottom of document: ends with "Good luck! 🚀", no chart
- `18-final-no-chart.png` — Final confirmation: document end, no chart present

## Console / Network
Clean — no JS errors or failed network requests observed. DOM evaluation confirmed:
- `hasGraphData: false` (no literal `[GRAPH_DATA]` text in the rendered page)
- `.prose` content area: `imgs: []` (zero image elements)
- The single PNG URL found in `main img` was the row thumbnail in the list, NOT a chart in the content pane

## Diagnosis — ROOT CAUSE IDENTIFIED

The fix in commit `672fac0` updated TWO things in `backend/agents/ad_strategist_agent.py`:
1. `MAX_TOKENS = 2500` (bumped from 1500)
2. System prompt mandating at least one `[GRAPH_DATA]` block per brief

**However, this only applies to the backend's `run()` function call path** (e.g., direct API call to `/api/agents/{tenant_id}/ad_strategist/run` or the cron scheduler).

When the task is delegated via CEO chat → Paperclip issue → `claude_local` agent subprocess, **Paperclip runs the agent against its own MD file from the Skills tab in Paperclip's UI**, not the system prompt in `ad_strategist_agent.py`. The Paperclip agent's skill MD does not contain the `[GRAPH_DATA]` mandate or MAX_TOKENS configuration.

The backend IS ready to process `[GRAPH_DATA]` blocks when they arrive — both paths are hooked:
- `backend/routers/inbox.py` line 749: direct-curl path checks and renders
- `backend/server.py` line 5116: watcher path checks and renders

The gap is **upstream**: the Paperclip-hosted agent never emitted the block in the first place because its instructions (Paperclip Skills tab → `aria-backend-api` skill MD) predate the `[GRAPH_DATA]` mandate.

## Fix Required
Update the Ad Strategist's skill/instruction content inside Paperclip's UI (Skills tab → the agent's system prompt or appended system prompt file, e.g. `docs/agents/ad_strategist.md` if one exists) to include the same `[GRAPH_DATA]` mandate that was added to `build_system_prompt()` in commit `672fac0`. The backend rendering pipeline is correctly wired — only the Paperclip agent instructions need to be updated.
