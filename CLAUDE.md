# ARIA — Claude Code Instructions

ARIA is a cloud-hosted SaaS platform that provides developer founders with an AI-powered marketing team. Instead of traditional marketing software modules, ARIA deploys autonomous AI agents organized in a company-like hierarchy, each responsible for a specific marketing function. The platform provides both strategic guidance (what to do) and tactical execution (doing it).

For v1, ARIA focuses exclusively on digital marketing for developer founders who are building products but lack the knowledge, time, or budget to market them effectively. The platform guides users through GTM strategy creation and then executes against that strategy through content creation, email marketing, social media management, and ad strategy — with a copy-paste delivery model.

---

## Agent Architecture (v1)

ARIA deploys 5 agents in a marketing team hierarchy:

| Agent | Slug | Role | Responsibilities |
|-------|------|------|-----------------|
| ARIA_CEO | `ceo` | Chief Marketing Strategist | Onboards user, builds GTM playbook, coordinates all agents, reviews outputs, adjusts strategy based on performance |
| ContentWriter | `content_writer` | Content Creation Agent | Blog posts, landing page copy, product descriptions, case studies, thought leadership. Maintains brand voice |
| EmailMarketer | `email_marketer` | Email Campaign Agent | Welcome sequences, newsletter drafts, drip campaigns, launch announcements. Copy-paste-ready with subject lines and send timing |
| SocialManager | `social_manager` | Social Media Agent | Platform-specific posts (X/Twitter, LinkedIn, Facebook), content calendar, engagement suggestions, hashtag strategy |
| AdStrategist | `ad_strategist` | Paid Ads Advisor | Facebook/Meta ad copy, audience targeting, budget allocation, A/B test variants. Step-by-step instructions for manual ad setup |

---

## Project Structure

```
ARIA/
├── backend/                    # FastAPI server + all agent logic
│   ├── server.py               # Main FastAPI app (port 8000)
│   ├── orchestrator.py         # CEO brain — dispatches all agents + Paperclip lookup helpers
│   ├── paperclip_office_sync.py # 5s loop: scrape completed Paperclip issues -> inbox; sync agent statuses to Virtual Office
│   ├── onboarding_agent.py     # Conversational GTM strategy builder
│   ├── agents/                 # 5 agent modules
│   │   ├── __init__.py         # AGENT_REGISTRY + DEPARTMENT_MAP
│   │   ├── ceo_agent.py        # Chief Marketing Strategist
│   │   ├── content_writer_agent.py  # Content creation
│   │   ├── email_marketer_agent.py  # Email campaigns
│   │   ├── social_manager_agent.py  # Social media
│   │   └── ad_strategist_agent.py   # Paid ads advisor
│   ├── tools/                  # API wrappers + Claude CLI
│   │   └── claude_cli.py       # Local Claude Code CLI wrapper (no API key needed)
│   ├── config/
│   │   ├── loader.py           # Supabase CRUD for tenant configs
│   │   └── tenant_schema.py    # Pydantic models (TenantConfig, etc.)
│   ├── tasks/
│   │   └── task_definitions.py # WORKFLOW_TEMPLATES + CRON_SCHEDULES
│   └── requirements.txt
├── frontend/                   # Next.js 14 app (port 3000)
│   ├── app/
│   │   ├── (marketing)/        # Public landing pages
│   │   ├── (auth)/             # login/, signup/ (email + GitHub OAuth)
│   │   ├── (onboarding)/       # welcome/, select-agents/, connect/, review/
│   │   └── (dashboard)/        # dashboard/, agents/, analytics/, inbox/, settings/
│   ├── components/
│   │   ├── ui/                 # Shadcn/UI base components
│   │   └── shared/             # kpi-card, agent-status-badge, chat-widget, sidebar
│   ├── lib/
│   │   └── supabase.ts         # Supabase client
│   └── .env.local              # Frontend env vars (NEXT_PUBLIC_*)
├── CEO.md                      # Orchestrator blueprint and org chart
├── HEARTBEAT.md                # CEO agent instructions (read by Paperclip)
├── CLAUDE.md                   # This file
├── README.md                   # Full project documentation
├── railway.toml                # Railway deployment config
└── .env                        # Backend env vars (never commit)
```

---

## Running Locally

### Backend (FastAPI — port 8000)
```bash
cd C:\Users\Admin\Documents\ARIA
pip install -r backend/requirements.txt
uvicorn backend.server:socket_app --reload --port 8000
```

### Frontend (Next.js — port 3000)
```bash
cd C:\Users\Admin\Documents\ARIA\frontend
npm install
npm run dev
```

### Paperclip AI (orchestration — port 3100)
```bash
npx paperclipai onboard --yes
# Dashboard: http://127.0.0.1:3100
```

All three must be running for full functionality. The backend syncs with Paperclip automatically on startup.

---

## Architecture

- **Next.js frontend** (`frontend/`) talks to FastAPI backend via `NEXT_PUBLIC_API_URL=http://localhost:8000`
- **FastAPI backend** (`backend/server.py`) handles webhooks, agent dispatch, dashboard data
- **Orchestrator** (`backend/orchestrator.py`) routes all agent calls — through Paperclip if connected, local fallback otherwise
- **Paperclip AI** (`localhost:3100`) manages scheduling, org chart, budgets, run tracking
- **Supabase** stores tenant configs (`tenant_configs`), agent logs (`agent_logs`), content library, and GTM playbooks
- **5 agents** each have a `run(tenant_id, **context)` async function using local Claude Code CLI (no API key needed)

---

## Agent Development

### Agent pattern (v1 — local Claude Code CLI)

Every agent in `backend/agents/` follows this pattern:

```python
from backend.config.loader import get_tenant_config
from backend.tools.claude_cli import call_claude

async def run(tenant_id: str, **context) -> dict:
    config = get_tenant_config(tenant_id)

    system_prompt = f"You are the [Agent Role] for {config.business_name}..."
    result = await call_claude(system_prompt, "Your task instructions here")

    return {"status": "completed", "result": result}
```

Agents use local Claude Code CLI via `backend/tools/claude_cli.py` — no `ANTHROPIC_API_KEY` required.

### Registering a new agent
1. Create `backend/agents/your_agent.py` with `async def run(tenant_id, **context) -> dict`
2. Import and add to `AGENT_REGISTRY` in `backend/agents/__init__.py`
3. Add to the correct department in `DEPARTMENT_MAP`
4. Add a cron schedule in `backend/tasks/task_definitions.py` if needed
5. Add metadata to `AGENT_METADATA` in `backend/paperclip_sync.py`

---

## Target User

**The Technical Founder** — a software developer or engineer who has built a product (SaaS, developer tool, API, or app) and needs to acquire users/customers.

- Marketing experience: Minimal to none
- Budget: $50–$300/month for marketing tools
- Time: 2–5 hours/week for marketing
- Pain: Knows marketing matters, doesn't know where to start

---

## Core Product Principles

1. **Guidance before execution** — understand product, audience, goals → build strategy → then execute
2. **Context and continuity** — remembers product, brand voice, audience, campaign history
3. **Agents as employees** — each function handled by a specialized agent in a hierarchy
4. **Progressive disclosure** — simple outputs for non-technical; dig into configs for technical users
5. **Copy-paste first, automation later** — v1 produces ready-to-use outputs with manual execution instructions

---

## Key Features (v1)

### 1. Onboarding & GTM Strategy Builder (P0)
- Conversational intake (10–15 min) by ARIA_CEO agent
- Product discovery → audience definition → goals & constraints → channel prioritization
- Outputs a GTM Playbook: positioning, messaging pillars, content themes, channel strategy, 30/60/90 day plan, KPIs
- All other agents reference this playbook

### 2. Content Creation Engine (P0)
- Blog posts, landing page copy, Product Hunt copy, Show HN posts, email copy
- Brand voice consistency learned from onboarding
- Content calendar with proactive suggestions

### 3. Email Marketing (P1)
- Welcome sequences, launch sequences, newsletters, re-engagement
- Copy-paste-ready with subject line A/B variants, send timing, segmentation notes
- No direct sending in v1

### 4. Social Media Management (P1)
- X/Twitter, LinkedIn, Facebook
- Platform-specific posts with character counts, hashtags, posting times
- Content adapted from ContentWriter output

### 5. Facebook Ads Advisor (P1)
- Campaign structure, audience targeting, ad creative, budget recommendations
- Step-by-step setup guide for Ads Manager (written for first-timers)
- A/B testing plan and optimization checkpoints via cron

---

## Data Model

| Entity | Description | Persistence |
|--------|-------------|-------------|
| Product Profile | Name, description, value prop, competitors | Created at onboarding, updated by user |
| Audience Definition | ICP, pain points, channels, language | Created at onboarding, refined over time |
| GTM Playbook | Positioning, messaging, channel strategy, 30/60/90 plan | Generated by CEO agent, versioned |
| Brand Voice | Tone, example phrases, do/don't guidelines | Learned during onboarding, refined via feedback |
| Content Calendar | Scheduled content with status, type, channel, date | Maintained by agents, editable by user |
| Content Library | All generated content with metadata and versions | Append-only, searchable |
| Campaign History | Ad and email campaigns with structure and copy | Append-only, referenced for optimization |
| Performance Log | User-reported metrics (clicks, signups, open rates) | Manual input, used for strategy adjustment |

---

## Pricing (v1)

| Tier | Price | Includes |
|------|-------|----------|
| Starter | $49/month | GTM playbook, 10 content pieces/month, content calendar, 1 campaign plan/month |
| Growth | $149/month | + 30 content pieces/month, email sequences, social calendar, 3 campaign plans/month, optimization reviews |
| Scale | $299/month | + unlimited content, priority generation, custom agent configs, dedicated support |

---

## Frontend Development

### Route groups (do not add to URL path)
- `(marketing)` — public pages
- `(auth)` — `/login`, `/signup`
- `(onboarding)` — `/welcome`, `/select-agents`, `/connect`, `/review`
- `(dashboard)` — `/dashboard`, `/agents`, `/analytics`, `/inbox`, `/settings`

**Important:** Route group folder names must not conflict across groups. The onboarding agents page is at `/select-agents` (not `/agents`) to avoid collision with `(dashboard)/agents`.

### Environment variables
Next.js reads env from `frontend/.env.local` — **not** the project root `.env`.
All browser-accessible vars must be prefixed `NEXT_PUBLIC_`.

### Component conventions
- UI primitives: `frontend/components/ui/` (Shadcn/UI + Radix)
- Shared business components: `frontend/components/shared/`
- The `Button` component supports `asChild` prop via `@radix-ui/react-slot`
- Design tokens: use ARIA's color variables (`--color-primary`, `--color-surface`, etc.)

### Key UI elements
- **Chat pane** — primary interaction surface, messages routed by CEO agent
- **Org chart** — visual agent hierarchy, click to see recent work and status
- **Content library** — searchable archive of all generated content
- **Content calendar** — visual timeline of planned/published content
- **Playbook view** — dedicated GTM strategy document, always accessible

---

## Environment Variables

### Backend (`.env` in project root)
| Variable | Purpose |
|----------|---------|
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | Server-side DB access (never expose to frontend) |
| `SUPABASE_ANON_KEY` | Public Supabase key |
| `PAPERCLIP_API_URL` | Paperclip server (default: `http://127.0.0.1:3100`) |
| `PAPERCLIP_API_TOKEN` | Paperclip auth token |
| `STRIPE_SECRET_KEY` | Payment processing |

### Frontend (`frontend/.env.local`)
| Variable | Purpose |
|----------|---------|
| `NEXT_PUBLIC_SUPABASE_URL` | Supabase URL (browser) |
| `NEXT_PUBLIC_SUPABASE_ANON_KEY` | Supabase anon key (browser) |
| `NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY` | Stripe (browser) |
| `NEXT_PUBLIC_API_URL` | Backend URL — `http://localhost:8000` |

Note: `ANTHROPIC_API_KEY` is **not required** — agents use local Claude Code CLI.

---

## Integration Roadmap

| Phase | Integrations | Timeline |
|-------|-------------|----------|
| v1 (Launch) | None — copy-paste model with step-by-step instructions | Launch |
| v1.5 | Email service providers (ConvertKit, Mailchimp) via MCP | Launch + 60 days |
| v2 | Social media publishing (X/Twitter, LinkedIn) via MCP | Launch + 120 days |
| v2.5 | Meta Ads API for automated campaign management | Launch + 180 days |
| v3 | Analytics ingestion (Google Analytics, Plausible) | Launch + 270 days |

---

## Key APIs

### Onboarding flow
```
POST /api/onboarding/start        → { session_id, message }
POST /api/onboarding/message      → { message, is_complete, questions_answered }
POST /api/onboarding/extract-config → { config, gtm_playbook }
POST /api/onboarding/save-config  → { tenant_id, config }
```

### Agent dispatch
```bash
curl -X POST http://localhost:8000/api/agents/{tenant_id}/content_writer/run
curl -X POST http://localhost:8000/api/cron/run-scheduled
```

### System health
```bash
curl http://localhost:8000/health
curl http://localhost:8000/api/paperclip/status
```

---

## Paperclip AI Integration

ARIA uses Paperclip AI (`localhost:3100`) as its orchestration layer. Agents live in Paperclip; ARIA dispatches work to them and pulls the results back.

### Adapter — must stay `claude_local`

All 6 agents (CEO, Content Writer, Email Marketer, Social Manager, Ad Strategist, Media Designer) MUST be configured with the `claude_local` adapter in Paperclip's UI (Configuration → Adapter type). The HTTP adapter was an experiment that bypassed the skill MD system and is gone — never re-enable it. `claude_local` spawns the real `claude` CLI binary as a subprocess; it is NOT sandboxed in the network sense (curl works fine).

### Required: `--dangerously-skip-permissions` in Extra args

The "Skip permissions" toggle in Paperclip's Configuration tab is **broken on our version** — it shows ON but doesn't actually inject the flag. Without the flag, every Bash/Write/curl tool call from inside the agent's CLI gets stuck on a permission prompt with no human to approve it, and the run hangs for 9-10 minutes before SIGTERM-ing with exit code 143.

**Fix:** in Paperclip → each agent → Configuration → **Extra args (comma-separated)** field, set:

```
--dangerously-skip-permissions
```

After saving, the agent's `Command` line in subsequent runs should read:
```
claude ... --dangerously-skip-permissions
```

If you spin up a new agent in Paperclip, this is the first thing to do.

### How agent output reaches ARIA's inbox (two paths, both work)

**Path A — `aria-backend-api` skill (primary):** The skill MD is attached to every agent in Paperclip's Skills tab. When the agent finishes its work, the skill instructs it to `curl POST http://172.17.0.1:8000/api/inbox/{tenant_id}/items` directly from inside the spawned CLI. The agent extracts `tenant_id` from the issue title prefix `[uuid] ...`. `/api/inbox/` is in `_PUBLIC_PREFIXES` so no auth header is needed. **Use the docker host IP `172.17.0.1`, not the public IP** — public IP routes through nginx which adds its own auth checks and breaks Path A.

**Path B — `paperclip_office_sync.poll_completed_issues` (safety net):** A 5s background loop in ARIA's lifespan polls Paperclip for finished issues and scrapes the agent's reply from the comments. Catches every failure mode of Path A (agent forgot to curl, curl returned a 5xx, JSON malformed, model ran out of context). Both paths dedupe via the `paperclip_issue_id` column, so they coexist without producing duplicate inbox items.

### Skill MD content rules

The `aria-backend-api` skill MD lives **inside Paperclip's instance** (Skills → aria-backend-api → Edit), not in this repo. Two rules for what goes in it:

1. **Do NOT include any other auth-protected ARIA endpoint** (`/api/dashboard/...`, `/api/crm/...`, etc.) The agent doesn't have JWT credentials and will fail on the first auth-protected call, then give up before reaching the inbox write. The only HTTP call the skill should reference is the `POST /api/inbox/{tenant_id}/items` write.
2. **Use `http://172.17.0.1:8000`, not the public IP.** Docker host IP from inside Paperclip's container goes straight to FastAPI on port 8000 and bypasses nginx.

### CEO chat → Paperclip → inbox flow

1. User types in CEO chat widget → `POST /api/ceo/chat`
2. Chat handler tries Paperclip first via `orchestrator.run_agent_via_paperclip_sync` (creates an issue assigned to CEO, posts the user message as a comment, polls for the reply with adaptive intervals 1s→4s, 60s timeout)
3. Posting a comment on the issue auto-wakes the CEO via Paperclip's `wakeOnDemand` mechanism. **Do NOT also call `/heartbeat/invoke`** — that creates a second On-demand run racing the Automation one, and `maxConcurrentRuns: 1` cancels one of them.
4. CEO reads the comment, may delegate to a sub-agent by creating its own Paperclip issue + comment
5. Sub-agent runs, writes its output as a comment on the issue
6. The comment fires Path A (skill curl) AND/OR is scraped by Path B (poller) on the next 5s tick → inbox row appears
7. Chat handler returns the CEO's reply text to the frontend
8. **Local fallback:** if Paperclip is unreachable or times out, the chat handler falls back to `call_claude` directly so chat keeps working when Paperclip is down

### Background loops in `server.py:lifespan`
- `_gmail_sync_loop` — every 2 min, Gmail inbound reply sync
- `_scheduler_executor_loop` — runs scheduled tasks
- `_paperclip_office_sync_loop` — every 5s, calls `poll_completed_issues()` (Path B inbox importer) then `sync_agent_statuses(sio)` (Virtual Office walking sprites)

### Where things live
- `backend/orchestrator.py` — `dispatch_agent`, `run_agent_via_paperclip_sync`, plus the Paperclip lookup helpers (`_urllib_request`, `get_company_id`, `get_paperclip_agent_id`, `paperclip_connected`)
- `backend/paperclip_office_sync.py` — `poll_completed_issues` (Path B) + `sync_agent_statuses` (Virtual Office)
- `backend/services/paperclip_chat.py` — `pick_agent_output`, `normalize_comments` (shared comment-parsing helpers used by both the chat sync route and the poller; filters out ARIA's own framing wrappers like `[tenant_id=...`)
- `docs/agents/ceo.md` — CEO agent identity (role, sub-agents, delegation rules, CRUD action set, refusal rules). The Paperclip CEO reads this via `--append-system-prompt-file`.

---

## Common Issues

| Problem | Fix |
|---------|-----|
| `supabaseUrl is required` | Check `frontend/.env.local` exists with `NEXT_PUBLIC_SUPABASE_URL` |
| `NEXT_PUBLIC_API_URL` wrong | Must be `http://localhost:8000` (backend), not `3000` (frontend) |
| Paperclip not connecting | Run `npx paperclipai onboard --yes` first, then restart backend |
| Agent not dispatching | Check agent is in tenant's `active_agents` list |
| **Agent runs hang for 9-10 min then exit 143** | Missing `--dangerously-skip-permissions` in Extra args. The "Skip permissions" toggle is broken; set the flag in Extra args manually for every agent. |
| **Inbox row never appears** | Agent's first call was probably `/api/dashboard/...` which 401's. Update the `aria-backend-api` skill MD in Paperclip to remove all curls except the inbox write. Path B (poller) will catch it as a fallback within 5s. |
| **`fetch failed (adapter_failed)` on Timer runs** | Agent is on the HTTP adapter. Flip Adapter type back to `Claude (local)` in Paperclip Configuration. |
| **`Paperclip cannot manage skills for this adapter yet`** | Same as above — agent is on HTTP. Flip back to claude_local. |
| **One chat creates two Paperclip runs (one cancelled)** | The chat dispatcher is calling both `/heartbeat/invoke` and posting a comment. Only post the comment — the comment alone wakes the agent via `wakeOnDemand`. Already fixed in `orchestrator.run_agent_via_paperclip_sync`. |
| **CEO chat returns the framing block as its reply** | `pick_agent_output` is supposed to filter out comments starting with `[tenant_id=`. Verify the filter is intact in `services/paperclip_chat.py`. |
| Next.js route conflict | Route group pages can't resolve to the same URL path — rename the folder |
| `asChild` prop warning | Button component must import `Slot` from `@radix-ui/react-slot` |
