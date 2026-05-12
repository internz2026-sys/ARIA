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

### CEO chat flow (LOCAL — does NOT go through Paperclip anymore)

As of 2026-04-11 (commit `5c4f16d`), CEO chat replies are generated directly via local `call_claude` with Haiku, not routed through Paperclip. Reason: Paperclip subprocess cold-start + polling added 8-25s of latency for nothing — the chat reply itself doesn't need any of Paperclip's orchestration features. Reply latency dropped from ~10-30s to ~1-4s.

1. User types in CEO chat widget → `POST /api/ceo/chat`
2. Per-session `asyncio.Lock` (`_chat_session_locks`) prevents two concurrent requests for the same `session_id` from interleaving and corrupting history
3. Chat handler builds the system prompt + conversation history (last CEO turn kept verbatim if under 2KB) and calls `call_claude(system_prompt, conversation, model=MODEL_HAIKU)` directly
4. Semantic cache is **disabled for CEO chat** (`agent_id == "ceo"`) — the 0.92 cosine threshold caused false positives where similar-but-different messages collapsed to the same cached reply
5. CEO reply parsed for ` ```delegate ` and ` ```action ` blocks via `_parse_codeblock_json` (which recovers from common Haiku JSON mistakes: trailing commas, JS comments, prose padding)
6. Action blocks (`create_contact`, `read_deals`, etc.) execute synchronously via `execute_action`, wrapped in try/except so handler crashes don't 500 the whole chat
7. Delegate blocks fire `_dispatch_paperclip_and_watch_to_inbox` as a background task (see next section)

### Sub-agent delegation: `_dispatch_paperclip_and_watch_to_inbox`

When the CEO emits a delegate block, the chat handler spawns this background task via `_safe_background` (which adds an error callback so silent crashes get logged instead of disappearing as "Task exception was never retrieved"). The watcher does dispatch + placeholder + active polling + inbox write as one unit:

1. **CRM enrichment** — `_enrich_task_desc_with_crm` looks up CRM contacts mentioned by name in the task description and appends their emails to the task. Closes the gap where the CEO's CRM-context heuristic doesn't fire on phrases like "create marketing email for Hanz".
2. **Dispatch via `dispatch_agent`** which calls `_dispatch_via_paperclip` → creates a Paperclip issue with `status: "todo"` (NOT default `backlog`, which is excluded from `inbox-lite` and causes the agent to ask "which task should I work on?")
3. **Wake the agent via comment post** — `_dispatch_via_paperclip` posts a verbose directive comment ("AUTONOMOUS TASK -- execute immediately, do not ask for clarification...") on the issue. The comment fires `issue.comment` event → `wakeOnDemand=true` → Automation run starts. **Do NOT call `/heartbeat/invoke`** — heartbeat returns 200 OK but doesn't actually trigger an Automation run for `claude_local` agents (issues just sit in `backlog` forever). This was a hard-won lesson on 2026-04-11.
4. **Create a placeholder inbox row** with `paperclip_issue_id` baked in so the global poller can't race and create a duplicate
5. **Adaptive polling** of THIS specific issue via `_urllib_request("GET", f"/api/issues/{id}/comments", strict=True)` with intervals (1s, 1s, 1.5s, 2s, 2s, 3s, 4s) up to 600s timeout. Bails fast on `_is_failed` status or `PaperclipUnreachable` outage (5 consecutive failures = ~10s).
6. **When the agent's reply comment arrives** — `pick_agent_output` filters by `expected_agent` and skips CEO-authored comments. If found and substantive (≥50 chars), proceed to step 7.
7. **Skill-curl dedupe** — Check for existing inbox rows from the same tenant + agent in the last 5 min that have substantial content (>200 chars, status not `processing`). If found → the agent's `aria-backend-api` skill curl already wrote the canonical row → **delete the watcher's placeholder** so only one row exists per delegation. Sentinel `skill_row_already_exists` prevents the fresh-row fallback from re-creating it.
8. **If no skill curl row exists** → update the placeholder with the parsed `email_draft` (or `social_draft` for content_writer/social_manager) and content

### Inbox CREATE endpoint: `POST /api/inbox/{tenant_id}/items`

The agent's `aria-backend-api` skill curls this endpoint directly from inside Paperclip. The endpoint is in `_PUBLIC_PREFIXES` so no JWT needed. Critical behaviors:

- **Confirmation rejection** — content matching `✅` / `Saved to ARIA Inbox` / `Successfully saved` / `draft created and saved` / `Draft ID:` / `## Task Complete` is short-circuited with `{item: null, skipped: "confirmation_message"}`. Catches the agent's "I'm done!" follow-up POSTs that would otherwise create duplicate rows.
- **Always run the parser** — even when the agent provides `email_draft` itself, `_parse_email_draft_from_text` (or `_parse_html_email_draft` for raw HTML content) runs and merges. Agent's fields win where set; parser fills gaps. Subject/recipient that look like raw HTML (`<html><body style=`) get overridden by the parser's clean values.
- **Type normalization** — any `email_marketer` content with parsed `email_draft` is forced to `type='email_sequence'` regardless of what the agent sent. This is the canonical type the frontend's `EmailEditor` component renders the editable form for.
- **Recent-row dedupe** — when no `paperclip_issue_id` is provided, look back 5 minutes for inbox rows with the same tenant + agent + first 100 chars of content. Update existing instead of inserting a duplicate.
- **Email template wrapper** — `_wrap_email_in_designed_template` is applied as a fallback only when `_agent_html_already_designed(html_body)` returns False (no inline styles, no tables, no gradients). Plain unstyled HTML gets wrapped in a light-themed branded template (gradient header, card sections, CTA button, footer) so the inbox UI shows a beautiful email instead of naked `<p>` tags. Designed HTML the agent already styled passes through unchanged.

### email_draft schema (must match the frontend `EmailDraft` interface)

The frontend's `EmailDraft` interface at [frontend/app/(dashboard)/inbox/page.tsx](frontend/app/(dashboard)/inbox/page.tsx) uses these field names:

```typescript
{ to, subject, html_body, text_body, preview_snippet, status }
```

**Critical:** use `html_body` and `text_body`, NOT `body_html` / `body`. The contenteditable iframe loads `srcDoc={draft.html_body}` so wrong field names render an empty editor. The `EmailEditor` component buttons (Approve & Send / Schedule / Save changes / Cancel draft) only render when `email_draft != null` AND `type == 'email_sequence'` AND `status == 'draft_pending_approval'`.

### `~/.claude.json` auto-restore

The Claude CLI rotates its auth file periodically and occasionally leaves only the backup at `~/.claude/backups/.claude.json.backup.<timestamp>`. Without auto-recovery the only fix was SSH+manual `cp`. Now handled by `_try_restore_claude_config` in `backend/tools/claude_cli.py`:

- **Startup check** in `lifespan` — runs once at backend boot, restores from latest backup if `~/.claude.json` is missing or zero-bytes
- **Reactive heal** in `call_claude` — on any non-zero CLI exit, calls `_try_restore_claude_config()` (no-op if file exists) and retries the CLI call once if a restore actually happened
- **Process-wide RLock** prevents two concurrent calls from racing to copy the same backup
- **Atomic rename** — copies to `.json.tmp` first, then `os.replace()` so a process death mid-write never leaves a half-written file

The fix self-heals on every container restart and on every mid-runtime failure. If you ever see `Auto-restored ~/.claude.json from backup` warnings firing more than a few times a day, the underlying CLI rotation race is happening too often — investigate via `docker exec aria-backend ls -la /root/.claude/backups/`.

### Background loops in `server.py:lifespan`
- `_gmail_sync_loop` — every 2 min, Gmail inbound reply sync
- `_scheduler_executor_loop` — runs scheduled tasks
- `_paperclip_office_sync_loop` — adaptive 5s (active) → 30s (idle) backoff. Calls `poll_completed_issues()` (Path B inbox importer) then `sync_agent_statuses(sio)` (Virtual Office walking sprites). Resets to fast interval when `poke_paperclip_poller()` event fires (chat handler + inbox routes wake it when the user does something). 70–80% reduction in idle-period Paperclip hits.

---

## Production deployment (Hostinger VPS)

The frontend lives in Docker on the VPS (not Vercel — `aria-frontend` is a real container in the stack). Backend auto-deploys after a `git push origin main`, but **only if the `pytest` job in CI passes** — the deploy is gated on a green test run as of 2026-05-08.

### Stack
- **VPS:** `72.61.126.188` (hostname `srv1551345`), `/opt/aria` is the checkout
- **Containers (docker compose):** `aria-backend`, `aria-frontend`, `aria-nginx`, `aria-redis`, `aria-qdrant`
- **Webhook listener:** [adnanh/webhook](https://github.com/adnanh/webhook) 2.8.0 running on `0.0.0.0:9000`, systemd unit `webhook.service`
- **Hook config:** `/etc/webhook.conf` — one hook `deploy-aria` that executes `/opt/aria/deploy.sh`, validates `X-Hub-Signature-256` HMAC against secret `absolutemadness`, and requires `ref == refs/heads/main`
- **Systemd override:** `/etc/systemd/system/webhook.service.d/override.conf` runs webhook with `-hooks /etc/webhook.conf -port 9000 -verbose` so `journalctl -u webhook -f` shows every incoming request
- **CI as gatekeeper:** [.github/workflows/tests.yml](.github/workflows/tests.yml) runs `pytest` first; only on green does its `Deploy to VPS` job curl the webhook URL above. Repo secret `VPS_WEBHOOK_SECRET` (= `absolutemadness`) signs the body. The original GitHub repo-level push webhook was deleted on 2026-05-08 as part of the cutover, so there is no longer a direct push-to-deploy path.

### Deploy flow (CI-gated)
1. `git push origin main` from your laptop
2. GitHub Actions runs the `pytest` job (~30s incl. setup, ~1.25s for the 25 test cases)
3. **If pytest fails, the deploy job is skipped — the push does NOT ship.** Fix the test, push again.
4. On green, the `Deploy to VPS` job builds a GitHub-style HMAC-signed payload (`X-Hub-Signature-256: sha256=<hmac>`, `ref: refs/heads/main`) and curls `http://72.61.126.188:9000/hooks/deploy-aria`
5. The VPS `webhook` binary validates the signature + ref, runs `/opt/aria/deploy.sh`
6. Deploy script does: `git pull origin main` → `docker compose up -d --build backend frontend`
7. Redis, qdrant, nginx are left alone (only backend + frontend rebuild)
8. Typical cycle time: pytest ~30s + deploy 4s (no-op) to ~3min (cold rebuild after `requirements.txt`/`package.json` churn)

### Why CI is the gatekeeper
Before the gate, a push with a broken backend (e.g. the TS strict mode build error episode) auto-deployed via the webhook and broke prod within ~30s. Now any pre-existing bug in the test suite catches the regression before the curl-to-webhook ever fires. **Never propose adding a "skip CI" toggle or a side-channel deploy** — if a hotfix needs to ship and a test is broken, fix or skip the test in the same commit. The whole point of the gate is that there is no second path.

### `/opt/aria/deploy.sh` (canonical version — do not let it drift)
```bash
#!/bin/bash
set -euo pipefail
cd /opt/aria
echo "[deploy] git pull origin main"
git pull origin main
echo "[deploy] rebuilding backend + frontend"
docker compose up -d --build backend frontend
echo "[deploy] done at $(date -u +%FT%TZ)"
```

Watch out for the "runs everything twice" bug — the original had two stacked command blocks that duplicated every action. If you ever see two `Image aria-backend Built` lines back-to-back in the webhook logs, `cat /opt/aria/deploy.sh` and look for duplication.

### Watching a deploy live
```bash
ssh root@72.61.126.188
journalctl -u webhook -f
```
Expected sequence: `incoming HTTP POST` → `deploy-aria got matched` → `200 OK` → `executing /opt/aria/deploy.sh` → build output → `Container aria-backend Started` → `[deploy] done`.

### Manual force-rebuild (bypassing webhook)
```bash
ssh root@72.61.126.188
cd /opt/aria && docker compose build --no-cache backend && docker compose up -d backend
```
Only use `--no-cache` when the layer cache is stale (new code isn't actually running after a deploy). Otherwise `--build` is ~10x faster.

### Testing the deploy loop end-to-end
```bash
git commit --allow-empty -m "test: webhook deploy" && git push origin main
```
Then tail `journalctl -u webhook -f` on the VPS. Should complete in ~4s with all layers CACHED.

### Where things live
- `backend/orchestrator.py` — `dispatch_agent`, `_dispatch_via_paperclip` (creates issue + posts wake comment), `PaperclipUnreachable` exception, `_sanitize_error_message`, plus the Paperclip lookup helpers (`_urllib_request`, `get_company_id`, `get_paperclip_agent_id`, `paperclip_connected`). The CEO chat handler in `server.py` calls `call_claude` directly with Haiku — the legacy `run_agent_via_paperclip_sync` blocking path was removed in favor of fire-and-forget delegation via `dispatch_agent`.
- `backend/paperclip_office_sync.py` — `poll_completed_issues` (5s global poller, safety net for direct Paperclip Timer runs) + `sync_agent_statuses` (Virtual Office), `_add_processed`, `_is_finished`/`_is_failed`
- `backend/services/paperclip_chat.py` — `pick_agent_output` (with `expected_agent` filter + 3-tier fallback), `normalize_comments`
- `backend/server.py` — `_dispatch_paperclip_and_watch_to_inbox`, `_parse_email_draft_from_text`, `_parse_html_email_draft`, `_parse_social_drafts_from_text`, `_markdown_to_basic_html`, `_wrap_email_in_designed_template`, `_agent_html_already_designed`, `_business_name_for_template`, `_enrich_task_desc_with_crm`, `_safe_background`, `_parse_codeblock_json`
- `backend/tools/claude_cli.py` — `call_claude`, `_try_restore_claude_config`, `_safe_decode`
- `docs/agents/ceo.md` — CEO agent identity (role, sub-agents, delegation rules, CRUD action set, refusal rules). The Paperclip CEO reads this via `--append-system-prompt-file`.

---

## Security CI

Two workflows in `.github/workflows/` complement the pytest gate:

- **[security.yml](.github/workflows/security.yml)** — bandit (Python SAST), pip-audit (dependency CVEs), detect-secrets (committed credentials). Runs on every push + PR. `continue-on-error: true` so findings surface as red status checks but don't block deploys yet — tighten once the baseline is clean.
- **[security-review.yml](.github/workflows/security-review.yml)** — Claude Opus reads the PR diff and posts a sticky security review comment via [.github/scripts/security_review.py](.github/scripts/security_review.py). PR-only (no comment surface on direct pushes). The system prompt knows ARIA's supabase-py + PostgREST + RLS + `_PUBLIC_PREFIXES` patterns so it flags real deviations rather than generic OWASP. **Routes through ARIA's local Claude CLI on the VPS** (via the `/api/internal/security-review` HMAC-gated endpoint in [backend/routers/security_review.py](backend/routers/security_review.py)) — no Anthropic API tokens consumed, just uses the existing Claude subscription.

Required setup:
1. Generate a long random string for HMAC.
2. Add it to `/opt/aria/.env` on the VPS as `SECURITY_REVIEW_HMAC_SECRET=<value>`, then `docker compose restart backend` for the env to load.
3. Add the same value as a GitHub repo secret named `SECURITY_REVIEW_HMAC_SECRET` (Settings → Secrets and variables → Actions). Without it the workflow exits with a "skipped" note — non-blocking.

The endpoint is in `_PUBLIC_PREFIXES` (JWT bypass) but HMAC-locked. If you ever expose new internal endpoints, add them under `/api/internal/` and HMAC-gate them the same way — never JWT-protect a machine-to-machine surface.

`.secrets.baseline` is created on the first detect-secrets run and committed afterwards. To accept a finding as a false positive: run `detect-secrets audit .secrets.baseline` locally and commit the updated baseline.

---

## PostgREST raw filters must use `safe_or_value()`

supabase-py parameterizes `.eq()`, `.ilike("col", value)`, `.in_()` — those are always safe to interpolate user input into. But `.or_(...)` and `.filter(...)` pass **raw PostgREST grammar**: `column.operator.value,column.operator.value`. A user-controlled value containing `,` `(` `)` `"` `\` without quoting could chain a new condition and exfiltrate adjacent rows.

**Rule:** any `f"...{user_input}..."` going into `.or_()` or `.filter()` must wrap the interpolated value with `safe_or_value()` from [backend/services/_postgrest_util.py](backend/services/_postgrest_util.py). The helper double-quotes + backslash-escapes properly.

```python
# WRONG — brittle inline blacklist, easy to forget a char
esc = search.replace(",", " ").replace("(", "").replace(")", "")
q = q.or_(f"email.ilike.%{esc}%,full_name.ilike.%{esc}%")

# RIGHT — single source of truth, every PostgREST-grammar char escaped
needle = safe_or_value(f"%{search}%")
q = q.or_(f"email.ilike.{needle},full_name.ilike.{needle}")
```

The CI lint [backend/tests/test_lint_postgrest_or_safety.py](backend/tests/test_lint_postgrest_or_safety.py) fails the build if any non-test file uses `.or_(f"...")` / `.filter(f"...")` without importing `safe_or_value` — so this rule is mechanically enforced going forward.

---

## Common Issues

| Problem | Fix |
|---------|-----|
| `supabaseUrl is required` | Check `frontend/.env.local` exists with `NEXT_PUBLIC_SUPABASE_URL` |
| `NEXT_PUBLIC_API_URL` wrong | Must be `http://localhost:8000` (backend), not `3000` (frontend) |
| Paperclip not connecting | Run `npx paperclipai onboard --yes` first, then restart backend |
| Agent not dispatching | Check agent is in tenant's `active_agents` list |
| **Agent runs hang for 9-10 min then exit 143** | Missing `--dangerously-skip-permissions` in Extra args. The "Skip permissions" toggle is broken; set the flag in Extra args manually for every agent. |
| **Inbox row never appears** | Agent's first call was probably `/api/dashboard/...` which 401's. Update the `aria-backend-api` skill MD in Paperclip to remove all curls except the inbox write. The watcher's active polling will catch it within ~1-2s of the agent finishing. |
| **`fetch failed (adapter_failed)` on Timer runs** | Agent is on the HTTP adapter. Flip Adapter type back to `Claude (local)` in Paperclip Configuration. |
| **`Paperclip cannot manage skills for this adapter yet`** | Same as above — agent is on HTTP. Flip back to claude_local. |
| **Delegated agent issues sit in `backlog` forever, watcher times out** | Two causes: (a) `_dispatch_via_paperclip` is calling `/heartbeat/invoke` instead of posting a wake comment (heartbeat doesn't trigger Automation runs for `claude_local` — only comments do via `wakeOnDemand`), or (b) issue was created with default `status: backlog` instead of `status: todo`. The agent's `inbox-lite` endpoint excludes backlog tasks, so the agent sees 0 assignments and asks "which task should I work on?" instead of executing. Both fixed in `_dispatch_via_paperclip` since 2026-04-11. |
| **Agent asks "Which task would you prefer?" instead of executing** | Wake comment is too vague. Use a verbose directive comment ("AUTONOMOUS TASK -- execute immediately, do not ask for clarification. Do NOT list other assignments..."). The directive style is in `_dispatch_via_paperclip:wake_body`. |
| **Two inbox rows per delegation (placeholder + agent skill curl)** | Watcher's `_dispatch_paperclip_and_watch_to_inbox` is supposed to delete its placeholder when the agent's skill curl row exists. Verify the dedupe block (lookup recent rows from same tenant + agent within 5 min, content > 200 chars, status != processing → delete placeholder) is intact. |
| **Inbox row has empty body editor / Source tab shows `<body contenteditable="true"></body>`** | Field name mismatch: backend wrote `body_html` / `body` but frontend reads `email_draft.html_body` / `text_body`. Fix in `_parse_email_draft_from_text` return dict — must match the `EmailDraft` interface in `frontend/app/(dashboard)/inbox/page.tsx`. |
| **Email subject shows raw HTML like `<html><body style="font-family: -apple-system,...`** | Agent posted raw HTML as content; the markdown parser ran instead of `_parse_html_email_draft` and grabbed the opening `<html>` tag as the first sentence. Fix: ensure `_looks_like_html` detection in `_parse_email_draft_from_text` routes HTML to the HTML parser. |
| **Subject is "Untitled email"** | Agent's reply has no `**Subject:**` line. Parser falls back to Preview Text → first non-greeting sentence → "Untitled". Sometimes this is the agent's prompt at fault — the agent should always emit a subject. As a backend-side band-aid, the parser's three-tier fallback usually finds something usable. |
| **Confirmation message rows like "✅ Email draft saved to ARIA inbox" appearing in inbox** | The agent is POSTing a status confirmation as a second inbox item. The `create_inbox_item` rejection patterns (`✅`, `Saved to ARIA Inbox`, `draft created and saved`, `Draft ID:`, etc.) should catch it. If a new pattern slips through, add it to the `_is_confirmation` block in `create_inbox_item`. |
| **`Claude CLI error: configuration file not found at /root/.claude.json`** | The CLI rotates its auth file and sometimes leaves only the backup. `_try_restore_claude_config` should auto-recover from `~/.claude/backups/`. If it isn't firing, check the lifespan startup log for `Startup: auto-restored ~/.claude.json from backup` and verify the backup directory exists. |
| **CEO chat returns "Reached max turns (1)"** | `--max-turns 1` was too restrictive after switching to Haiku (which is more eager to use tools). Bumped to `--max-turns 5` in `claude_cli.py`. Don't lower it again. |
| **"Task exception was never retrieved" warnings or silent watcher crashes** | A background coroutine is being spawned via bare `_aio.create_task(...)` instead of `_safe_background(...)`. Wrap it so the error callback logs the crash. |
| **Docker rebuild "succeeded" but new code isn't running (`grep -c <new_helper>` returns 0)** | Layer cache is stale — `=> CACHED [6/7] COPY backend/ backend/` reused an old snapshot. Force rebuild with `docker compose build --no-cache backend && docker compose up -d backend`. |
| **Backend container crash-loops with `NameError: name '_re_crm' is not defined`** | Module-level code is referencing `_re_crm` (or any shared import alias) BEFORE the `import re as _re_crm` line. `py_compile` won't catch it because names only fail at module-execution time, not parse time. Fix: move `import re as _re_crm` above every block that uses it. Happened in batch 4 perf work on 2026-04-11 — the pre-compiled `_DELEGATE_BLOCK_RE` / `_ACTION_BLOCK_RE` block ended up above the import. |
| **Frontend sidebar renders but pages spin forever** | Backend is down / crash-looping. Check `docker compose ps` for `Restarting (1)` next to `aria-backend`, then `docker compose logs backend --tail 80` to find the traceback. Never assume it's a frontend bug until backend health is confirmed. |
| **`docker compose ps` says "no configuration file provided: not found"** | You're not in `/opt/aria` on the VPS. `cd /opt/aria` first — the compose file lives there. |
| **Webhook `curl http://IP:9000/hooks/deploy-aria` returns `Hook rules were not satisfied`** | That's the CORRECT response to a bare GET (the hook requires HMAC + push event). Means the listener is up and serving. A real deploy from GitHub will pass rules because it carries `X-Hub-Signature-256`. |
| **`systemctl edit webhook` seems to save but `systemctl cat webhook` doesn't show the override** | The interactive editor can silently drop the override if you exit without triggering a save. Skip the interactive flow: write `/etc/systemd/system/webhook.service.d/override.conf` directly via a heredoc, then `systemctl daemon-reload && systemctl restart webhook`. |
| **`git push` rejected because remote is ahead** | Someone (you, a webhook, or an SSH session) committed on `main` since your last pull — commonly an empty `test: webhook deploy` commit from the VPS. Fix: `git pull --rebase origin main && git push origin main`. Never `--force` unless you actually want to discard the remote work. |
| **Recursion errors in poller / `RecursionError: maximum recursion depth exceeded`** | Don't `replace_all` rename `_processed_issues.add` → `_add_processed` — it'll catch the line inside the helper itself and turn it into infinite recursion. Add a comment warning future-you. |
| **One chat creates two Paperclip runs (one cancelled)** | The chat dispatcher is calling both `/heartbeat/invoke` and posting a comment. Only post the comment — the comment alone wakes the agent via `wakeOnDemand`. Already fixed in `orchestrator._dispatch_via_paperclip`. |
| **CEO chat returns the framing block as its reply** | `pick_agent_output` is supposed to filter out comments starting with `[tenant_id=` / `[wake]`. Verify the filter is intact in `services/paperclip_chat.py`. |
| Next.js route conflict | Route group pages can't resolve to the same URL path — rename the folder |
| `asChild` prop warning | Button component must import `Slot` from `@radix-ui/react-slot` |
