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
│   ├── orchestrator.py         # CEO brain — dispatches all agents
│   ├── paperclip_sync.py       # Syncs agents with Paperclip AI on startup
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

ARIA uses Paperclip AI (`localhost:3100`) as its orchestration layer. On startup, `backend/paperclip_sync.py` automatically:
1. Finds or creates the "ARIA" company in Paperclip
2. Registers all 5 agents with their slugs, roles, and cron schedules
3. Sets up the org chart hierarchy (CEO → ContentWriter, EmailMarketer, SocialManager, AdStrategist)

The CEO agent in Paperclip uses `CLAUDE.md` and `HEARTBEAT.md` as its context.

---

## Common Issues

| Problem | Fix |
|---------|-----|
| `supabaseUrl is required` | Check `frontend/.env.local` exists with `NEXT_PUBLIC_SUPABASE_URL` |
| `NEXT_PUBLIC_API_URL` wrong | Must be `http://localhost:8000` (backend), not `3000` (frontend) |
| Paperclip not connecting | Run `npx paperclipai onboard --yes` first, then restart backend |
| Agent not dispatching | Check agent is in tenant's `active_agents` list |
| Next.js route conflict | Route group pages can't resolve to the same URL path — rename the folder |
| `asChild` prop warning | Button component must import `Slot` from `@radix-ui/react-slot` |
