I have everything I need. Now let me compose the comprehensive reference document.

# ARIA System Reference

Operator-grade walkthrough of every moving part. Cross-referenced against the tree at `c:\Users\Admin\Documents\ARIA` as of 2026-05-14.

---

## 1. Product

ARIA is a multi-tenant Python/Next.js SaaS that gives a developer founder a virtual marketing team consisting of six Claude-powered agents that produce ready-to-send drafts. The v1 delivery model is intentionally **copy-paste-first**: ad copy, blog posts, social posts, and email sequences land in an in-app inbox (`(dashboard)/inbox`) where the user reviews, approves, and either sends inline (email only) or copy-pastes into the destination platform. The actual sending pipeline only exists for email today (Gmail SMTP and a `replies+<thread_id>@inbound...` plus-addressing scheme described later); Twitter/LinkedIn posting endpoints exist but are gated.

The agent roster lives at [backend/agents/__init__.py:12-19](backend/agents/__init__.py#L12) â€” `ceo`, `content_writer`, `email_marketer`, `social_manager`, `ad_strategist`, `media`. The `media` agent is the v1 newcomer not mentioned in CLAUDE.md's "5 agents" table; the canonical count is now **6**. It also doesn't burn quota in the way the other agents do â€” see Section 13.

What makes it not a CrewAI/AutoGPT clone:

- **Persistent tenant identity.** Every agent run reads the `TenantConfig` (`backend/config/tenant_schema.py`) which contains the GTM playbook, brand voice, ICP, and integration tokens, then attaches a `business_context()` block to every prompt ([backend/agents/base.py:76-108](backend/agents/base.py#L76)). The condensed `agent_brief` (~150 tokens, generated once at onboarding) is preferred over rebuilding the brief from fields each call â€” that saves ~650 tokens per dispatch.
- **Two-layer orchestration with a real outside-the-repo service (Paperclip)**. Paperclip is a third-party Node app on `127.0.0.1:3100` that maintains the issue tracker, scheduling, and the Claude subprocess sandbox. ARIA talks to it over urllib + httpx (see `_urllib_request` at [backend/orchestrator.py:125](backend/orchestrator.py#L125)). Paperclip is the **only** place where agents are configured; ARIA does runtime lookups (`get_paperclip_agent_id`, [backend/orchestrator.py:185](backend/orchestrator.py#L185)) and never registers them anymore â€” the old self-registration in `backend/paperclip_sync.py` was deleted because it kept fighting the operator's manual config.
- **Determinism where it matters.** Onboarding is an 8-question state machine driven by the backend, not the LLM ([backend/onboarding_agent.py:1-17](backend/onboarding_agent.py#L1)). The LLM is used as a YES/NO on-topic classifier (Haiku, `max_tokens=8`, [backend/onboarding_agent.py:468](backend/onboarding_agent.py#L468)) plus a one-shot JSON extractor at the end.
- **Two parallel result-delivery paths** so agent output is durable against Path A or Path B failing (see Section 4).

Pricing tiers + plan gating are wired end-to-end (free/starter/growth/scale) â€” see Section 13. Stripe code exists but is dormant per project memory; plan changes are intentionally free.

---

## 2. Repository layout

Top-level tree (omitting `node_modules`, `venv`, `__pycache__`):

```
ARIA/
â”œâ”€â”€ backend/                       # FastAPI app + agents (Python 3.11)
â”‚   â”œâ”€â”€ server.py                  # 7082-line FastAPI app
â”‚   â”œâ”€â”€ orchestrator.py            # dispatch_agent, Paperclip helpers
â”‚   â”œâ”€â”€ auth.py                    # Supabase JWT verification
â”‚   â”œâ”€â”€ approval.py                # action policy registry
â”‚   â”œâ”€â”€ ceo_actions.py             # synchronous CEO action handlers
â”‚   â”œâ”€â”€ onboarding_agent.py        # 8-question state machine
â”‚   â”œâ”€â”€ paperclip_office_sync.py   # Path B inbox poller + Virtual Office sync
â”‚   â”œâ”€â”€ schemas.py                 # shared Pydantic models
â”‚   â”œâ”€â”€ agents/                    # ceo/content_writer/email_marketer/social_manager/ad_strategist/media + base
â”‚   â”œâ”€â”€ routers/                   # admin, campaigns, ceo, crm, email, inbox, login_rate_limit, plans, reports, security_review, tasks
â”‚   â”œâ”€â”€ services/                  # ~30 modules (chat_state, semantic_cache, rate_limit, paperclip_chat, â€¦)
â”‚   â”œâ”€â”€ tools/                     # claude_cli, gmail_sync, twitter_tool, linkedin_tool, sendgrid_tool, stripe_tool (dormant), â€¦
â”‚   â”œâ”€â”€ config/                    # loader, tenant_schema, brief
â”‚   â”œâ”€â”€ tasks/                     # task_definitions (WORKFLOW_TEMPLATES, CRON_SCHEDULES)
â”‚   â”œâ”€â”€ sql/                       # CREATE TABLE scripts (chat_tables, email_tables, â€¦)
â”‚   â”œâ”€â”€ migrations/                # incremental schema deltas + RLS (all_tables_rls.sql)
â”‚   â”œâ”€â”€ tests/                     # pytest suite (9 files, ~25 cases)
â”‚   â”œâ”€â”€ Dockerfile
â”‚   â”œâ”€â”€ requirements.txt
â”‚   â””â”€â”€ requirements-test.txt
â”œâ”€â”€ frontend/                      # Next.js 14 app (app router)
â”‚   â”œâ”€â”€ app/
â”‚   â”‚   â”œâ”€â”€ (marketing)/           # public landing/about/pricing/terms/blog/use-cases
â”‚   â”‚   â”œâ”€â”€ (auth)/                # login/signup/forgot-password/reset-password/check-email
â”‚   â”‚   â”œâ”€â”€ (onboarding)/          # welcome/describe/select-agents/review/connect/edit-profile/launching
â”‚   â”‚   â”œâ”€â”€ (dashboard)/           # dashboard/inbox/chat/agents/calendar/campaigns/conversations/crm/projects/reports/usage/admin/office/settings/analytics
â”‚   â”‚   â”œâ”€â”€ auth/                  # /auth/callback (Supabase OAuth)
â”‚   â”‚   â”œâ”€â”€ banned/                # /banned dead-end for banned users
â”‚   â”‚   â””â”€â”€ layout.tsx
â”‚   â”œâ”€â”€ components/{ui,shared,virtual-office}/
â”‚   â”œâ”€â”€ lib/                       # api.ts (authFetch), supabase.ts, socket.ts, use-ceo-chat, use-notifications, â€¦
â”‚   â”œâ”€â”€ middleware.ts
â”‚   â””â”€â”€ Dockerfile
â”œâ”€â”€ docs/
â”‚   â”œâ”€â”€ agents/                    # ceo.md + per-agent skill MD files (loaded at run time by base.py)
â”‚   â”œâ”€â”€ paperclip/                 # external service notes
â”‚   â”œâ”€â”€ aria-prd-v1.pdf, ARIA_Pitch_Deck.pptx, ARIA_Promo.mp4
â”‚   â””â”€â”€ aria-landing.jsx.pdf
â”œâ”€â”€ tests/playwright/              # E2E specs (onboarding funnel + virtual office sync)
â”œâ”€â”€ .github/workflows/             # tests.yml, security.yml, security-review.yml
â”œâ”€â”€ .claude/                       # local Claude Code config â€” settings.json, agents/playwright-tester.md
â”œâ”€â”€ scripts/
â”œâ”€â”€ docker-compose.yml             # prod stack
â”œâ”€â”€ docker-compose.staging.yml     # staging stack â€” backend-staging + frontend-staging
â”œâ”€â”€ deploy-staging.sh              # /opt/aria-staging/deploy-staging.sh on the VPS
â”œâ”€â”€ nginx.conf                     # host-based routing for prod + staging
â”œâ”€â”€ railway.toml                   # legacy, only useful for Railway redeploys
â”œâ”€â”€ vercel.json                    # legacy, frontend lives in Docker now
â”œâ”€â”€ start.sh
â”œâ”€â”€ CLAUDE.md, CEO.md, HEARTBEAT.md, ARIA_log.md, README.md, STAGING_SETUP.md
â””â”€â”€ pytest_err.txt, pytest_out.txt # left over from a local run, not used by CI
```

Not checked in: `.env` (project root), `frontend/.env.local`, `frontend/node_modules/`, `backend/venv/`, `*.pyc`. The deploy script bakes the env file separately on the VPS at `/opt/aria/.env`.

---

## 3. Runtime topology

Five containers run side-by-side in `/opt/aria/docker-compose.yml`. The staging stack at `/opt/aria-staging/docker-compose.staging.yml` adds `aria-backend-staging` and `aria-frontend-staging` next to (not replacing) the prod containers, and reuses the prod `aria-nginx`, `aria-redis`, `aria-qdrant`.

- **`aria-backend`** ([docker-compose.yml:4](docker-compose.yml#L4)) â€” FastAPI on uvicorn, with Socket.IO mounted on the same ASGI app via `socketio.ASGIApp` ([backend/server.py:1006](backend/server.py#L1006)). Bound to `172.17.0.1:8000:8000` so Paperclip's container can reach the inbox-create endpoint through the docker bridge without going through nginx. Volume-mounts `/opt/aria/.claude` â†’ `/home/aria/.claude` so the Claude CLI's auth file (`~/.claude.json`) and its rotating backups survive container rebuilds.
- **`aria-frontend`** ([docker-compose.yml:35](docker-compose.yml#L35)) â€” Next.js 14 server. Bound to `127.0.0.1:3000:3000` â€” only nginx reaches it. Build args bake `NEXT_PUBLIC_*` env vars at image build time.
- **`aria-nginx`** ([docker-compose.yml:74](docker-compose.yml#L74)) â€” listens on 8080/8443. The VPS runs Hostinger-managed Traefik on the public 80/443, with labels in compose ([docker-compose.yml:92-107](docker-compose.yml#L92)) telling Traefik to route `aria.hoversight.agency` and `staging.aria.hoversight.agency` to this nginx. The nginx config in [nginx.conf:24](nginx.conf#L24) uses a `resolver 127.0.0.11 valid=10s` block + `proxy_pass http://$variable` so backend/frontend rebuilds don't leave nginx pointing at a stale container IP (a recurring 502 source before this change). Two server blocks distinguish prod (`server_name aria.hoversight.agency`, [nginx.conf:24](nginx.conf#L24)) from staging ([nginx.conf:90](nginx.conf#L90)), each routing `/`, `/api`, `/socket.io`, `/health` to its respective backend.
- **`aria-redis`** ([docker-compose.yml:54](docker-compose.yml#L54)) â€” Redis 7 alpine. Used by `backend/services/rate_limit.py` (Lua-script sliding window) and by `backend/services/chat_state.py` (session locks are in-process asyncio Locks, not Redis â€” see Section 4).
- **`aria-qdrant`** ([docker-compose.yml:62](docker-compose.yml#L62)) â€” vector store on `127.0.0.1:6333`. Used by `backend/services/semantic_cache.py` for cross-request prompt-result caching (`ensure_collection()` is called from `lifespan` at [backend/server.py:677](backend/server.py#L677)). CEO chat **bypasses** the semantic cache because the 0.92 cosine threshold produced false positives.
- **Paperclip** â€” runs on `127.0.0.1:3100` outside this repo. Joined to the docker network `paperclip-tgdk_default` ([docker-compose.yml:113](docker-compose.yml#L113)) so `aria-backend` can hit it as `http://172.17.0.1:3100` via `PAPERCLIP_URL` ([backend/orchestrator.py:49](backend/orchestrator.py#L49)). It runs its own Claude CLI subprocesses (`claude_local` adapter) and posts wake-comment-driven runs.
- **Webhook listener** â€” `adnanh/webhook` 2.8.0 on `0.0.0.0:9000`, systemd unit `webhook.service`. Hook `/hooks/deploy-aria` validates `X-Hub-Signature-256` with the secret `absolutemadness`, requires `ref == refs/heads/main`, then runs `/opt/aria/deploy.sh`. Staging path uses `deploy-staging.sh` invoked manually via plink because no staging webhook is wired yet (`STAGING_SETUP.md`).

Staging vs prod difference summary: same nginx, same Redis/Qdrant/Paperclip, plus a parallel `backend-staging` + `frontend-staging` deployed from the `staging` git branch into `/opt/aria-staging`. CI does not auto-deploy staging â€” only `main` triggers the webhook.

---

## 4. The agent system

### The 6 agents

| Slug | Role | File | Default model | Max tokens | Notes |
|---|---|---|---|---|---|
| `ceo` | Chief Marketing Strategist | [backend/agents/ceo_agent.py](backend/agents/ceo_agent.py) | Sonnet (4-6) by base default | 1500 | Builds GTM playbook, emits ` ```delegate ` and ` ```action ` blocks |
| `content_writer` | Long-form content | [backend/agents/content_writer_agent.py:45](backend/agents/content_writer_agent.py#L45) | Sonnet, switches to Haiku for `landing_page`, `product_hunt`, `show_hn`, `email_copy` ([content_writer_agent.py:52-61](backend/agents/content_writer_agent.py#L52)) | 3000 (Sonnet) / 2000 (Haiku) | Cross-agent hooks: pulls FAQ questions from `email_messages` for FAQ tasks ([content_writer_agent.py:89](backend/agents/content_writer_agent.py#L89)), closed-won deals from `crm_deals` for case studies ([content_writer_agent.py:142](backend/agents/content_writer_agent.py#L142)), and recent Media images via `asset_lookup` ([content_writer_agent.py:235](backend/agents/content_writer_agent.py#L235)). |
| `email_marketer` | Email campaigns | [backend/agents/email_marketer_agent.py:56](backend/agents/email_marketer_agent.py#L56) | Haiku | 2000 | Recognises a "send to X" intent via `_EMAIL_RE` and emits raw HTML w/ inline styles for send-ready output. Detects "newsletter from blog" via `_BLOG_DIGEST_RE` and pulls the latest `content_writer` inbox row. |
| `social_manager` | X/LinkedIn posts | [backend/agents/social_manager_agent.py:37](backend/agents/social_manager_agent.py#L37) | Haiku | 1500 | Strict JSON output for 2 platforms, strips agent-style framing from `text` ([social_manager_agent.py:69-76](backend/agents/social_manager_agent.py#L69)). |
| `ad_strategist` | Paid ads advisor | [backend/agents/ad_strategist_agent.py:26](backend/agents/ad_strategist_agent.py#L26) | Haiku | 2500 (bumped from 1500 on 2026-05-01 to fit a `[GRAPH_DATA]` block) | Reads `campaigns` table for past performance via `list_recent_campaigns_with_metrics` ([ad_strategist_agent.py:62](backend/agents/ad_strategist_agent.py#L62)) |
| `media` | Visual content | [backend/agents/media_agent.py:56](backend/agents/media_agent.py#L56) | Haiku | 500 (for prompt refinement only) | Pipeline: Haiku refines prompt â†’ Pollinations API generates PNG â†’ Supabase Storage upload â†’ row in `content_library_entries` + `inbox_items`. Falls back to Gemini if `GEMINI_API_KEY` is set ([media_agent.py:198](backend/agents/media_agent.py#L198)). |

The shared `BaseAgent.run()` at [backend/agents/base.py:130](backend/agents/base.py#L130) is where every agent (except media, which overrides) bottoms out. It:

1. Loads tenant config + builds `system_prompt` + `user_message`.
2. Appends the agent's MD skill file from `docs/agents/skills/<agent>_skills.md` ([base.py:142-149](backend/agents/base.py#L142)). No cache â€” file is re-read every call so the operator can edit a skill MD and see the change on the next run.
3. Appends three best-effort blocks from `services/asset_lookup`: top-performers, style-memory diffs, and prior-cancellation reasons.
4. Adds related content recall via cheap ILIKE on `content_library_entries` ([base.py:192-226](backend/agents/base.py#L192)).
5. Calls `call_claude(system_prompt, user_message, max_tokens, tenant_id, model, agent_id)` from [backend/tools/claude_cli.py:290](backend/tools/claude_cli.py#L290).

### CEO chat â€” local-only path

CEO chat is not routed through Paperclip. The reasoning (per CLAUDE.md and 2026-04-11 commit `5c4f16d`) was a latency drop from 10â€“30s to 1â€“4s. The router is at [backend/routers/ceo.py:636](backend/routers/ceo.py#L636).

Flow:

1. `POST /api/ceo/chat` body carries `{tenant_id, session_id, message}`. `tenant_id` is verified against the JWT owner ([routers/ceo.py:660-662](backend/routers/ceo.py#L660)).
2. The handler acquires the per-session `asyncio.Lock` via `get_session_lock(session_id)` from [backend/services/chat_state.py:56](backend/services/chat_state.py#L56). The dict is bounded (`MAX_CACHED_SESSIONS = 100`, [chat_state.py:38](backend/services/chat_state.py#L38)) with insertion-order eviction that drops the matching lock too. Cross-process locking is **not** implemented â€” a multi-worker deploy would have a race here.
3. Inside the lock: append user turn â†’ build prompt â†’ `call_claude(system_prompt, conversation, model=MODEL_HAIKU)` directly (no Paperclip).
4. CEO reply is parsed for ` ```delegate ` and ` ```action ` blocks via `_parse_codeblock_json` (recovers from trailing commas, JS comments, prose padding).
5. Action blocks (`create_contact`, `read_deals`, etc.) run synchronously via `execute_action` from [backend/ceo_actions.py](backend/ceo_actions.py); failures are swallowed and surfaced as `error` keys instead of 500ing the chat.
6. Delegate blocks fire `_dispatch_paperclip_and_watch_to_inbox` via `_safe_background` ([backend/services/async_utils.py](backend/services/async_utils.py), re-exported in `server.py:5062`).

### Sub-agent delegation (`_dispatch_paperclip_and_watch_to_inbox`)

The watcher is the **only** path where Paperclip is exercised. Its job is dispatch + placeholder write + active poll + inbox row.

- **CRM enrichment** (`_enrich_task_desc_with_crm`) augments task text with matched CRM contact emails before dispatching.
- **`dispatch_agent`** ([backend/orchestrator.py:261](backend/orchestrator.py#L261)) routes through Paperclip via `_dispatch_via_paperclip` ([orchestrator.py:323](backend/orchestrator.py#L323)) which:
  - Creates an issue with `status: "todo"` (not the default `backlog`, which would never appear in the agent's `inbox-lite`).
  - Posts a verbose wake comment ([orchestrator.py:392-404](backend/orchestrator.py#L392)) â€” `[wake] AUTONOMOUS TASK -- execute immediatelyâ€¦` â€” which triggers `wakeOnDemand=true` on the `issue.comment` event. **The heartbeat endpoint is NOT used** because it returns 200 OK without actually triggering a `claude_local` Automation run.
- The skill MD attached to each Paperclip agent (`aria-backend-api`) instructs the spawned CLI to `POST http://172.17.0.1:8000/api/inbox/{tenant_id}/items` once the work is done â€” this is **Path A**.
- Active polling on the specific issue's comments with adaptive intervals (1s, 1s, 1.5s, 2s, 2s, 3s, 4s, up to 600s timeout). Bails fast on `_is_failed` or `PaperclipUnreachable` (5 consecutive network failures = ~10s).
- When the agent's reply comment lands, `pick_agent_output` ([backend/services/paperclip_chat.py](backend/services/paperclip_chat.py)) filters CEO-authored comments and `[wake]/[tenant_id=` framing, returns the canonical agent output.
- Dedupe: if the agent's skill curl already wrote a canonical row (>200 chars, recent same-tenant+agent, status â‰  `processing`), the watcher's placeholder is deleted. If it didn't, the placeholder is updated with parsed drafts (email/social).

**Path B safety net** â€” `poll_completed_issues` in [backend/paperclip_office_sync.py:60](backend/paperclip_office_sync.py#L60), running on the adaptive 5s/30s loop, scrapes finished-issue comments globally so every failure mode of Path A still gets the agent output into the inbox. Dedupe is by `paperclip_issue_id` ([paperclip_office_sync.py:67](backend/paperclip_office_sync.py#L67)).

### `dispatch_agent` quota gate

Before any Paperclip work, `dispatch_agent` calls `check_quota(tenant_id, agent_name)` ([backend/orchestrator.py:288-303](backend/orchestrator.py#L288)). If `allowed=False`, it raises `PlanQuotaExceeded` with `.as_dict()` carrying `{status: "quota_exceeded", reason, plan, used, limit}`. Callers (chat handler, cron runner, REST `/run` endpoint) catch and surface the reason to the user; ERROR-level logs are deliberately not used (the reason "Monthly content quota reached (10/10 on Starter plan)" is expected user behaviour, not an outage).

Agent log writes always happen â€” `log_agent_action` at [backend/orchestrator.py:209](backend/orchestrator.py#L209) inserts into `agent_logs` with the dispatch status, which feeds the dashboard activity feed AND the quota counter for next-time.

---

## 5. Authentication & authorization (full picture)

JWT verification path lives in [backend/auth.py:61](backend/auth.py#L61). Supports HS256 (legacy `SUPABASE_JWT_SECRET`) **and** ES256 via JWKS â€” algorithm header is required, `none` is explicitly rejected ([auth.py:82-91](backend/auth.py#L82)). Tokens are fetched from `Authorization: Bearer <jwt>` and from `auth.token` in the Socket.IO connect payload.

The 5-layer security model:

1. **Middleware** â€” every request matching `/api/*` hits `auth_and_rate_limit_middleware` at [backend/server.py:817](backend/server.py#L817):
   - 120 req/min/IP via Redis-backed sliding window ([auth.py:315](backend/auth.py#L315)).
   - Skip-list: `_PUBLIC_PATHS` ([server.py:791-795](backend/server.py#L791)) = `/health`, `/api/whatsapp/webhook`, `/api/cron/run-scheduled`; `_PUBLIC_PREFIXES` ([server.py:797-813](backend/server.py#L797)) = `/api/auth/`, `/api/webhooks/`, `/api/inbox/`, `/api/media/`, `/api/email/inbound`, `/api/internal/`; plus any path ending in `/google-tokens` (OAuth callback during login).
   - Production refuses to fall through to dev-mode when `SUPABASE_JWT_SECRET` is unset ([server.py:847-859](backend/server.py#L847)) â€” fail-loud rather than silent auth bypass.
   - **Ban gate** ([server.py:893-910](backend/server.py#L893)) â€” returns 403 `{detail: "BANNED", user_id}` for users whose `profiles.banned_at` is set. Frontend axios interceptor routes to `/banned`.
   - **Pause gate** ([server.py:920-955](backend/server.py#L920)) â€” soft-lock for `profiles.status in (paused, suspended)`, blocks only "expensive" POSTs (`/api/ceo/chat`, `/api/agents/*/run`). 30 expensive calls/user/min hard cap.
   - **RBAC** â€” `/api/admin/*` requires `profiles.role in (admin, super_admin)` via `get_user_role` + `is_admin` from `services/profiles.py` ([server.py:963-975](backend/server.py#L963)).
2. **`Depends(get_verified_tenant)`** ([auth.py:249](backend/auth.py#L249)) â€” per-route gate that hydrates `TenantConfig` and verifies `lower(owner_email) == lower(jwt.email)` or `tenant_id == jwt.sub`. Collapses 404 and 403 into one identical "Access denied" response so an attacker can't enumerate tenant existence ([auth.py:272-279](backend/auth.py#L272)). 45+ routes bulk-applied this dep in commit `0b10918`.
3. **`X-Aria-Agent-Token`** â€” the shared secret guarding `/api/inbox/{tenant_id}/items` ([backend/routers/inbox.py:852-872](backend/routers/inbox.py#L852)). Set as `ARIA_INTERNAL_AGENT_TOKEN` in `/opt/aria/.env`. Production fail-closes when unset (503), dev still allows unauth with a warning. **As of 52294b3, the Paperclip skill MD has not been updated to send this header** â€” Path A will 401 until that change lands; Path B catches everything in the meantime.
4. **`?access_token=` for OAuth init + WebSockets** â€” browser navigation (`window.location.href`) can't send `Authorization` headers, so OAuth init endpoints like [server.py:1354](backend/server.py#L1354) (`twitter_connect`) explicitly require the JWT as a query parameter and verify it against the tenant's owner_email before redirecting to the provider. Socket.IO `connect` reads the token from `auth: {token}` payload OR `Authorization: Bearer â€¦` header ([server.py:1078-1123](backend/server.py#L1078)), and `join_tenant` re-verifies tenant ownership ([server.py:1127-1179](backend/server.py#L1127)).
5. **HMAC for machine-to-machine** â€” the deploy webhook (X-Hub-Signature-256 with `absolutemadness`), the security-review endpoint (`SECURITY_REVIEW_HMAC_SECRET`, [backend/routers/security_review.py:98](backend/routers/security_review.py#L98)), and inbound email webhooks (`INBOUND_WEBHOOK_SECRET`, [backend/routers/email.py:815](backend/routers/email.py#L815) for Postmark HMAC-SHA256-base64, `_verify_resend_signature`, and the **stubbed** SendGrid path at [routers/email.py:843](backend/routers/email.py#L843)). The Whatsapp webhook GET ([server.py:2018](backend/server.py#L2018)) is the only `/api/...` public path that is JWT-bypassed but not under a `_PUBLIC_PREFIXES` entry.

Public endpoints + their justifications:

- `/health` â€” load-balancer probe; intentionally trivial output ([server.py:1213-1225](backend/server.py#L1213)), no version/SHA/timestamp leakage.
- `/api/whatsapp/webhook` (GET + POST) â€” WhatsApp Business platform verification doesn't carry our JWT.
- `/api/cron/run-scheduled` â€” **public, no signing** ([server.py:5027](backend/server.py#L5027)). This is the HIGH gap in Section 14.
- `/api/auth/*` â€” OAuth callbacks from Twitter/LinkedIn/Google; the provider doesn't carry our JWT.
- `/api/webhooks/*` â€” Stripe etc. (Stripe is dormant, code path inert).
- `/api/inbox/*` â€” Paperclip-spawned Claude CLI POSTs results here; gated by `X-Aria-Agent-Token`.
- `/api/media/*` â€” same model as `/api/inbox/`, agent-driven, token-gated.
- `/api/email/inbound` â€” Postmark/Resend/SendGrid inbound parse webhook, HMAC-gated.
- `/api/internal/` â€” security-review endpoint, HMAC-gated.

---

## 6. Data model â€” Supabase tables

Discovered by greping `.table("...")` across `backend/`. Every owner column is `tenant_id` unless noted.

| Table | Purpose | Owner | RLS | Writers / Readers |
|---|---|---|---|---|
| `tenant_configs` | Core tenant record â€” config + integrations + plan + owner_email + GTM playbook | `tenant_id`, `owner_email` | âœ… enabled, predicate `lower(owner_email)=lower(jwt.email)` ([backend/migrations/all_tables_rls.sql:71-95](backend/migrations/all_tables_rls.sql#L71)) | Written by `save_tenant_config` ([config/loader.py:72](backend/config/loader.py#L72)) at the end of onboarding; read everywhere. |
| `agent_logs` | Append-only ledger of every dispatch | `tenant_id` | âœ… enabled via subquery | Written by `log_agent_action` ([orchestrator.py:209](backend/orchestrator.py#L209)); read by dashboard activity feed + `plan_quotas.get_current_usage` ([services/plan_quotas.py:189-217](backend/services/plan_quotas.py#L189)). Migration [migrations/create_agent_logs.sql](backend/migrations/create_agent_logs.sql) â€” missing from prod Supabase until commit `d42712a`. |
| `agent_status` | Live agent state for the Virtual Office | `(tenant_id, agent_id)` upsert key | not explicit | `_emit_agent_status` ([server.py:1057](backend/server.py#L1057)) upserts on every status change. |
| `inbox_items` | The user-facing delivery surface for all agent output | `tenant_id` | âœ… enabled via subquery | Written by `inbox_service.create_item` + Path A skill curl + Path B poller + watcher placeholder; read by `(dashboard)/inbox`. Migration `backend/sql/add_paperclip_issue_id.sql` adds the dedupe column. |
| `onboarding_drafts` | In-progress onboarding state for resume | `user_id` (Supabase auth uid, UNIQUE) | **NOT RLS-protected** (skipped per [all_tables_rls.sql:49-51](backend/migrations/all_tables_rls.sql#L49) â€” JWT-bound at app layer) | UPSERT-keyed on user_id ([sql/create_onboarding_drafts.sql:19](backend/sql/create_onboarding_drafts.sql#L19)). |
| `profiles` | Role, status, banned_at, ban_reason | `user_id` | not enabled (not tenant-scoped) | Written by admin routes + ban/unban; read by middleware on every authed request. Migrations `create_profiles.sql`, `add_profile_status.sql`, `profiles_banned.sql`, `profiles_ban_reason.sql`. |
| `notifications` | Bell-icon notifications | `tenant_id` | âœ… enabled | Written by scheduler + Path B poller; read by notification bell. |
| `scheduled_tasks` | Cron + ad-hoc one-shot tasks | `tenant_id` | âœ… enabled (likely via subquery) | Created by [server.py:2271](backend/server.py#L2271); consumed by `_scheduler_executor_loop`. Migration `backend/sql/create_scheduled_tasks.sql`. |
| `chat_sessions` | CEO chat sessions metadata + auto-titles | `tenant_id` | âœ… enabled | Read/write via `backend/services/chat.py`. Migration `backend/sql/create_chat_tables.sql`. |
| `chat_messages` | CEO chat message history | `tenant_id`, `session_id` | âœ… enabled | Persisted by `_save_chat_message`; the in-memory `chat_sessions` dict at [chat_state.py:46](backend/services/chat_state.py#L46) is just a cache. |
| `tasks` | Kanban projects board | `tenant_id` | âœ… enabled | Migrations: `create_tasks_table.sql`, `add_tasks_deleted_at.sql`, `add_tasks_project_columns.sql`. |
| `email_threads` | Email conversation per contact | `tenant_id`, `contact_email` | âœ… enabled | Written by `gmail_sync.py`, `imap_inbound.py`, `email_inbound.py`. |
| `email_messages` | Individual messages within threads | `tenant_id` | âœ… enabled | Indexed for `_collect_customer_questions` scan ([content_writer_agent.py:99](backend/agents/content_writer_agent.py#L99)). Migration `backend/sql/create_email_tables.sql`. |
| `crm_contacts` | CRM contact records | `tenant_id` | âœ… enabled | Read by `email_marketer` (recipient lookup), `content_writer` (case studies), `ceo_actions.create_contact`. |
| `crm_companies` | CRM company records | `tenant_id` | âœ… enabled | â€” |
| `crm_deals` | CRM deals/pipeline | `tenant_id` | âœ… enabled | â€” |
| `crm_activities` | CRM activity log | `tenant_id` | âœ… enabled | â€” |
| `campaigns` | Ad campaign records â€” copy + status + performance metadata | `tenant_id` | âœ… enabled | Indexed `metadata.performance_review_at` for the 7-day reminder sweep ([server.py:204](backend/server.py#L204)). Migration `backend/sql/create_campaigns.sql` + `migrations/add_campaigns_inbox_link.sql`. |
| `campaign_reports` | Persisted performance snapshots | `tenant_id` | likely âœ… via subquery | â€” |
| `style_adjustments` | User-edit diffs feeding the style-memory loop | `tenant_id` | not in RLS template; check `create_style_memory.sql` | Read by `summarize_style_memory_for_prompt` in `services/asset_lookup.py`. |
| `marketing_reports` | State-of-the-Union + agent productivity snapshots | `tenant_id` | âœ… enabled, dedicated migration [marketing_reports_rls.sql](backend/migrations/marketing_reports_rls.sql) | Written by `services/reports*`. |
| `content_library_entries` | Searchable archive of every generated piece | `tenant_id` | âœ… enabled | Written by every agent's run; read by `BaseAgent.run` for related-content recall ([base.py:192-226](backend/agents/base.py#L192)) and by `content_repurpose_loop` ([server.py:347](backend/server.py#L347)). |
| `api_usage` | Per-hour Claude CLI token + request counters | `tenant_id` | not in RLS template | Written by `claude_cli.py:_load_usage` / `_save_usage`. |
| `content_library` | Legacy alias seen in a few greps; the live table is `content_library_entries` |

Extra tables found via grep that are **test fixtures** (`backend/tests/`), not real: `can_cast`, `datalim`, `flags`, `foo`, `head`, `legacy`, `test`. Ignore.

---

## 7. Background workers / loops

Started inside `lifespan` at [backend/server.py:636](backend/server.py#L636):

- **`_gmail_sync_loop`** ([server.py:111-146](backend/server.py#L111)) â€” was every 2 min Gmail readonly poll; **disabled** by default. Returns immediately unless `GMAIL_READONLY_ENABLED=1` because the `gmail.readonly` scope was dropped to skip CASA Tier-2 audit. Inbound replies now come through plus-addressing â†’ Postmark/Resend webhook.
- **`_scheduler_executor_loop`** ([server.py:149-243](backend/server.py#L149)) â€” every 30s, dispatches due `scheduled_tasks` via `services/scheduler.execute_task`. Every other tick (~60s) sweeps `campaigns` for 7-day performance-review reminders and writes `notifications` rows.
- **`_followup_nudge_loop`** ([server.py:246-344](backend/server.py#L246)) â€” every 6h. For each tenant, finds `email_threads` with `status='awaiting_reply'` older than 7 days, generates a draft reply via the same pipeline as the manual "Generate Reply Draft" button. Caps at 3 per tenant per sweep. **First agent-driven cron in ARIA.**
- **`_content_repurpose_loop`** ([server.py:347-475](backend/server.py#L347)) â€” every 7 days. Scans `content_library_entries` for blog/article/landing_page rows >90 days old, drops a `refresh_suggestion` inbox row (max 3 per tenant per sweep). Dedup via `metadata.library_entry_id` keyed against existing inbox rows within 30 days.
- **`_paperclip_office_sync_loop`** ([server.py:478-531](backend/server.py#L478)) â€” adaptive 5s/30s. `FAST_INTERVAL=5`, `SLOW_INTERVAL=30`, backs off after 6 consecutive empty cycles (~30s of nothing). Resets to fast on: (a) imported a new inbox row, (b) `sync_agent_statuses` saw a state change, (c) Paperclip not connected, (d) `poke_paperclip_poller()` event fires (called from chat handler + inbox routes). 70-80% reduction in idle-hour Paperclip hits.
- **`imap_poll_loop`** ([backend/services/imap_inbound.py](backend/services/imap_inbound.py), started in `lifespan` at [server.py:691](backend/server.py#L691)) â€” polls the shared SMTP mailbox for customer replies, parses the plus-address thread_id, writes `email_threads` + `email_messages`. No-op if `IMAP_HOST`/`SMTP_USER`/`SMTP_PASSWORD` unconfigured.

All six are wrapped via `asyncio.create_task` directly in `lifespan` (not `_safe_background`); they cancel cleanly on shutdown via `task.cancel()` ([server.py:693-698](backend/server.py#L693)).

The shared `httpx.AsyncClient` singleton in `orchestrator._get_httpx_client` is closed in `lifespan` shutdown ([server.py:702-705](backend/server.py#L702)).

---

## 8. External integrations

- **Gmail** â€” OAuth tokens land via `POST /api/integrations/{tenant_id}/google-tokens` ([server.py:3473](backend/server.py#L3473)) during the login flow (middleware special-cases `path.endswith("/google-tokens")` at [server.py:831](backend/server.py#L831)). Refresh tokens stored in `tenant_configs.integrations.gmail_refresh_token`. Outbound send via `services/email_sender.send_with_refresh`; the Reply-To uses plus-addressing on the SMTP mailbox (`replies+<thread_id>@inbound.<INBOUND_EMAIL_DOMAIN>`, commit `c8e4aa7`) so customer replies route to the IMAP poller / inbound webhook. Inbound `gmail_sync.py` poller is dormant (see Section 7).
- **Twitter / X** â€” OAuth 2.0 PKCE flow. `/api/auth/twitter/connect/{tenant_id}` ([server.py:1354](backend/server.py#L1354)) requires `?access_token=` JWT verified against tenant owner. Tokens land in `tenant_configs.integrations.twitter_*` ([server.py:1417-1421](backend/server.py#L1417)). Posting at `/api/twitter/{tenant_id}/tweet` ([server.py:1699](backend/server.py#L1699)).
- **LinkedIn** â€” Same OAuth pattern. `/api/auth/linkedin/connect/{tenant_id}` ([server.py:1461](backend/server.py#L1461)). Posting at `/api/linkedin/{tenant_id}/post` ([server.py:1618](backend/server.py#L1618)).
- **WhatsApp** â€” Business API. Public webhook verify at `/api/whatsapp/webhook` GET ([server.py:2018](backend/server.py#L2018)), inbound POST at [server.py:2033](backend/server.py#L2033), outbound at `/api/whatsapp/{tenant_id}/send` ([server.py:2127](backend/server.py#L2127)).
- **Postmark/Resend/SendGrid (inbound)** â€” `/api/email/inbound` ([routers/email.py:856](backend/routers/email.py#L856)) accepts payloads from whichever provider is configured via `INBOUND_EMAIL_PROVIDER`. HMAC-signed via `INBOUND_WEBHOOK_SECRET`. Postmark uses HMAC-SHA256-base64; Resend uses Svix's signature scheme; **SendGrid is stubbed** at [routers/email.py:843-853](backend/routers/email.py#L843) â€” accepts any signature with a warning log. Routed by host header / Reply-To plus-addressing into `email_threads`.
- **Pollinations** â€” image generation at `https://image.pollinations.ai/prompt/{encoded}` ([backend/agents/media_agent.py:36](backend/agents/media_agent.py#L36)). Free, no auth. Result PNG bytes pass an HTML-page heuristic (`_looks_like_image`, [media_agent.py:351](backend/agents/media_agent.py#L351)) then upload to Supabase Storage bucket `content` at path `media/{tenant_id}/{uuid}.png` ([media_agent.py:368](backend/agents/media_agent.py#L368)). Fallback to Gemini if `GEMINI_API_KEY` is set.
- **Stripe** â€” **DORMANT** per project memory. `backend/tools/stripe_tool.py` carries create_customer/create_subscription/create_invoice helpers, but no route wires them into a request lifecycle. `STRIPE_SECRET_KEY` is read at module load ([stripe_tool.py:6](backend/tools/stripe_tool.py#L6)) but plan changes are intentionally free; the plan picker writes directly to `tenant_configs.plan` via the plans router. The security-review prompt at [routers/security_review.py:55](backend/routers/security_review.py#L55) explicitly tells the reviewer not to flag missing payment gates as bugs.

---

## 9. Frontend architecture

Next.js 14 app router. Route groups (folder name in parens doesn't appear in URL):

- **`(marketing)`** ([frontend/app/(marketing)](frontend/app/(marketing))) â€” public: `/` (landing), `/about`, `/features`, `/pricing`, `/contact`, `/blog`, `/use-cases`, `/privacy`, `/terms`.
- **`(auth)`** â€” `/login`, `/signup`, `/forgot-password`, `/reset-password`, `/check-email`.
- **`(onboarding)`** â€” `/welcome` (entry), `/describe` (conversational intake), `/select-agents` (toggle agent roster), `/review` (LLM-extracted config preview), `/connect` (integrations setup), `/edit-profile` (post-onboarding edits), `/launching` (post-save spinner).
- **`(dashboard)`** â€” every authenticated app surface: `/dashboard`, `/inbox`, `/chat`, `/agents` (org chart), `/calendar`, `/campaigns`, `/conversations` (email threads), `/crm`, `/projects` (kanban), `/reports`, `/usage`, `/admin`, `/office` (Virtual Office), `/settings`, `/analytics`.
- **`/banned`** ([frontend/app/banned](frontend/app/banned)) â€” escape route for banned users; not under any group so the dashboard layout's auth provider tree doesn't apply.
- **`/auth/callback`** ([frontend/app/auth](frontend/app/auth)) â€” Supabase OAuth callback.

The dashboard layout at [frontend/app/(dashboard)/layout.tsx](frontend/app/(dashboard)/layout.tsx) wires four nested providers ([dashboard/layout.tsx:231-234](frontend/app/(dashboard)/layout.tsx#L231)):

- `CeoChatProvider` â€” global chat session state for `FloatingChat`.
- `NotificationProvider` â€” toast + bell state.
- `OfficeAgentsProvider` â€” Virtual Office agent positions/states.
- `ConfirmProvider` â€” modal confirmation helper.

Floating widgets always mounted: `FloatingChat`, `OfficeKanban`, `NotificationBell`, `ToastContainer`, plus mobile-only `MobileBottomNav` ([dashboard/layout.tsx:362-370](frontend/app/(dashboard)/layout.tsx#L362)).

Auth helpers in `frontend/lib/api.ts`:
- `getAuthHeaders()` returns `Authorization: Bearer <token>` from the active Supabase session.
- `authFetch(url, options)` for direct URL calls â€” on no session redirects to `/login` ([api.ts:43-55](frontend/lib/api.ts#L43)) via full page reload so auth state resets.
- `fetchAPI(endpoint, options)` wraps backend `/api/*` calls.

The dashboard layout polls `/api/profile/me` every 60s ([dashboard/layout.tsx:184-217](frontend/app/(dashboard)/layout.tsx#L184)); 403 with `detail: "BANNED"` triggers an immediate redirect to `/banned?user=<uid>`. Account-paused banner shows for `status in (paused, suspended)`.

Virtual Office components live at [frontend/components/virtual-office](frontend/components/virtual-office): `VirtualOffice.tsx` (canvas), `OfficeKanban.tsx` (sidebar widget), `AgentInfoPanel.tsx`. Agent walking sprites sync to `agent_status_change` Socket.IO events emitted by `_emit_agent_status` + `sync_agent_statuses`.

---

## 10. Onboarding pipeline

Deterministic 8-question state machine (see Section 4 and [onboarding_agent.py:1-17](backend/onboarding_agent.py#L1)). Frontend steps: `/welcome` â†’ `/describe` (CEO chat) â†’ `/select-agents` â†’ `/review` â†’ `/launching`.

REST endpoints (all behind JWT auth since 2026-05-07):

- `POST /api/onboarding/start` ([server.py:2961](backend/server.py#L2961)) â€” creates a server-side `OnboardingAgent` and binds it to the JWT `sub` claim. Returns `{session_id, message}` where message is the greeting + first question ([onboarding_agent.py:369-378](backend/onboarding_agent.py#L369)).
- `POST /api/onboarding/message` ([server.py:3025](backend/server.py#L3025)) â€” verifies JWT user matches the session's bound user, then runs `process_message`. The Haiku classifier (`max_tokens=8`) answers YES/NO; on 3rd off-topic attempt the answer is force-accepted ([onboarding_agent.py:84](backend/onboarding_agent.py#L84)).
- `POST /api/onboarding/skip` ([server.py:3039](backend/server.py#L3039)) â€” marks current field as skipped, advances to next.
- `POST /api/onboarding/extract-config` ([server.py:3057](backend/server.py#L3057)) â€” one-shot Claude call using `EXTRACTION_PROMPT` ([onboarding_agent.py:116-180](backend/onboarding_agent.py#L116)) to turn 8 answers into a structured `TenantConfig` payload + a `gtm_profile` mini-block. `_ensure_generated_fields` ([onboarding_agent.py:188-208](backend/onboarding_agent.py#L188)) fills `positioning_summary` and `30_day_gtm_focus` deterministically if Claude left them blank.
- `POST /api/onboarding/save-config` / `save-config-direct` â€” final write. Builds `TenantConfig`, calls `save_tenant_config` ([config/loader.py:72](backend/config/loader.py#L72)) which UPSERTs into `tenant_configs` with a 5-retry loop that strips unknown columns from the payload (handles schema drift between code and Supabase).

Persisted draft (`onboarding_drafts` table â€” keyed on `user_id` UNIQUE):

- `POST /api/onboarding/save-draft` UPSERTs the full agent state (messages, field_state, field_answers, attempts, extracted_config).
- `GET /api/onboarding/draft` fetches; `DELETE /api/onboarding/draft` clears.
- `OnboardingAgent.from_dict` ([onboarding_agent.py:258-325](backend/onboarding_agent.py#L258)) rehydrates with capped inputs: 500 messages max, 4096 char per field answer max. Older snapshots from before code changes load tolerantly â€” missing keys reset to defaults. Recomputes `_complete` from `field_state` after rehydration.

The **restart flag** (re-onboarding): commit `5abb1cd` makes `/api/onboarding/start` accept a flag to wipe the existing draft AND tenant_configs row before starting fresh, fixing the bug where new answers stopped persisting on second-time onboarding.

---

## 11. Deployment pipeline

CI workflow at [.github/workflows/tests.yml](.github/workflows/tests.yml):

- **Concurrency** ([tests.yml:14-16](.github/workflows/tests.yml#L14)) cancels in-progress runs on the same ref so two quick pushes don't deploy in the wrong order.
- **pytest job** ([tests.yml:19](.github/workflows/tests.yml#L19)) â€” Python 3.11, installs `backend/requirements.txt + requirements-test.txt`, sets test env vars (`ARIA_ENV=test`, dummy Supabase keys), runs `pytest backend/tests/ -v -s --tb=short`.
- **deploy job** ([tests.yml:55](.github/workflows/tests.yml#L55)) â€” `needs: pytest`, only on `push` to `main`. Builds a GitHub-style JSON body, HMAC-SHA256-signs with `VPS_WEBHOOK_SECRET`, curls `http://72.61.126.188:9000/hooks/deploy-aria` with `X-Hub-Signature-256` header. `--max-time 240` so a cold rebuild can complete. Curl non-2xx fails the job.

Security workflows:

- [.github/workflows/security.yml](.github/workflows/security.yml) â€” bandit (Python SAST), pip-audit (dependency CVEs), detect-secrets. `continue-on-error: true` (non-blocking until baseline is clean).
- [.github/workflows/security-review.yml](.github/workflows/security-review.yml) â€” Claude Opus diff review via [backend/routers/security_review.py](backend/routers/security_review.py). Posts a sticky PR comment; routes through the VPS Claude CLI (no Anthropic API charge), HMAC-gated by `SECURITY_REVIEW_HMAC_SECRET`. The system prompt encodes ARIA's `safe_or_value()` rule, `_PUBLIC_PREFIXES` model, RLS-via-service-role pattern, and the "Stripe deferred" exemption ([security_review.py:49-69](backend/routers/security_review.py#L49)).

VPS webhook â†’ `/opt/aria/deploy.sh` flow:

1. CI's curl POSTs HMAC-signed payload to `:9000/hooks/deploy-aria`.
2. `adnanh/webhook` validates signature + ref, runs `deploy.sh`.
3. `deploy.sh`: `git pull origin main` â†’ `docker compose up -d --build backend frontend`. Redis/qdrant/nginx untouched. Cycle: ~4s no-op, up to 3min on requirements churn.

Staging path: [deploy-staging.sh](deploy-staging.sh) at `/opt/aria-staging/deploy-staging.sh` â€” pulls `staging` branch, runs `docker compose -f docker-compose.staging.yml --env-file /opt/aria/.env up -d --build backend-staging frontend-staging`. **No webhook wired** â€” triggered manually via plink until a staging hook is configured.

Env vars on the VPS (from `docker-compose.yml` references + auth.py + claude_cli + plan_quotas + emails; values not enumerated):

- `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_ANON_KEY`, `SUPABASE_JWT_SECRET`, `SUPABASE_JWT_ECC_PUBLIC_KEY`
- `PAPERCLIP_API_URL`, `PAPERCLIP_API_TOKEN`, `PAPERCLIP_SESSION_COOKIE`, `PAPERCLIP_COMPANY_ID`, `PAPERCLIP_<AGENT>_AGENT_ID`, `PAPERCLIP_<AGENT>_KEY`
- `ARIA_INTERNAL_AGENT_TOKEN`, `SECURITY_REVIEW_HMAC_SECRET`, `VPS_WEBHOOK_SECRET` (GitHub secret + `/etc/webhook.conf`)
- `ARIA_ENV` (`prod` in production, gates dev-fallthroughs)
- `REDIS_URL`, `QDRANT_URL`
- `CORS_ALLOWED_ORIGINS`
- `BACKEND_URL` (override for OAuth redirect URI derivation)
- `INBOUND_EMAIL_PROVIDER`, `INBOUND_WEBHOOK_SECRET`, `INBOUND_EMAIL_DOMAIN`, `IMAP_HOST`, `SMTP_USER`, `SMTP_PASSWORD`
- `POLLINATIONS_MODEL`, `GEMINI_API_KEY`
- `STRIPE_SECRET_KEY` (read but dormant), `ENABLE_API_DOCS`, `GMAIL_READONLY_ENABLED`
- `ARIA_HOURLY_REQUEST_LIMIT`, `ARIA_HOURLY_TOKEN_LIMIT`

Branch policy: `main` (auto-deploys to prod via CI), `staging` (manual deploy to staging stack), `v1` (mentioned in CLAUDE.md task brief â€” historical/protected). PR builds run pytest but never deploy ([tests.yml:64-65](.github/workflows/tests.yml#L64)).

---

## 12. Observability + error handling

**Logging.** Every module uses Python's stdlib `logging`. The naming convention is `aria.<module>` â€” e.g., `logger = logging.getLogger("aria.orchestrator")`. WARNING for recoverable / expected-but-noteworthy (quota walls, Paperclip outages); ERROR for unexpected failures; INFO for normal lifecycle; DEBUG for verbose path tracing. The global log redaction filter at [backend/services/log_redaction.py](backend/services/log_redaction.py) is installed earliest in `server.py` ([server.py:26-27](backend/server.py#L26)) and scrubs known secret-shaped substrings before stdout flush â€” useful for startup logs that might include connection strings in exceptions during DB warmup.

**Sentry.** `sentry-sdk[fastapi]>=2.14.0` is in `backend/requirements.txt:13` but I couldn't locate any `sentry_sdk.init(...)` call in the codebase â€” the package is installed but not initialised. If you intend it to be live, the init needs adding.

**`_safe_background`** ([backend/services/async_utils.py](backend/services/async_utils.py), re-exported at [server.py:5062](backend/server.py#L5062)) wraps fire-and-forget coroutines with an error callback so silent crashes log instead of disappearing as "Task exception was never retrieved". Used at least at [server.py:5482, 5500, 6108](backend/server.py#L5482) plus everywhere the chat handler spawns delegation watchers. The lifespan background loops themselves use bare `asyncio.create_task` because they're top-level and shouldn't be abandoned â€” different policy.

**`_try_restore_claude_config`** ([backend/tools/claude_cli.py:221](backend/tools/claude_cli.py#L221)) â€” the Claude CLI rotates `~/.claude.json` and sometimes leaves only a backup at `~/.claude/backups/.claude.json.backup.<timestamp>`. The restore helper:

- Runs once at startup in `lifespan` ([server.py:650-657](backend/server.py#L650)) â€” restores if `.claude.json` is missing or zero-bytes.
- Re-runs reactively on any non-zero CLI exit inside `call_claude` and retries the call once if a restore actually happened.
- Uses a process-wide `threading.RLock` to prevent two concurrent calls from racing.
- Atomic rename: copies to `.json.tmp`, then `os.replace()` â€” never leaves a half-written file.

**Hook scripts in `.claude/`** â€” there's an `agents/playwright-tester.md` custom agent definition and `settings.json`/`settings.local.json` with the operator's allow-list of bash commands. No `.claude/hooks/` directory; no on-startup or on-tool-call hooks configured. `scheduled_tasks.lock` is an empty file â€” looks like an old advisory lock that's no longer referenced.

**Audit log.** `agent_logs` is the canonical record of every dispatch with `status` âˆˆ `{completed, completed_with_warning, error}`. `_sanitize_error_message` ([orchestrator.py:437-449](backend/orchestrator.py#L437)) strips known credential-shaped substrings (`eyJ`, `Bearer `, `sk-`, `API_KEY`, postgres URIs) from exception messages before storing.

---

## 13. Pricing & plan gates

Tiers and limits live in a single frozen dict at [backend/services/plan_quotas.py:86-96](backend/services/plan_quotas.py#L86):

```python
"free":    PlanLimits(content_pieces=3,  campaign_plans=0, email_sequences_enabled=False)
"starter": PlanLimits(content_pieces=10, campaign_plans=1, email_sequences_enabled=False)
"growth":  PlanLimits(content_pieces=30, campaign_plans=3, email_sequences_enabled=True)
"scale":   PlanLimits(content_pieces=-1, campaign_plans=-1, email_sequences_enabled=True)
```

Mapping agent slug â†’ quota bucket ([plan_quotas.py:100-104](backend/services/plan_quotas.py#L100)):

- **content** bucket: `content_writer`, `social_manager`, `media`.
- **campaign** bucket: `ad_strategist`.
- **email** bucket (feature flag): `email_marketer`.
- CEO and any unmapped agent: always allowed, no quota.

`check_quota` ([plan_quotas.py:273-356](backend/services/plan_quotas.py#L273)) is called from `dispatch_agent` ([orchestrator.py:288-303](backend/orchestrator.py#L288)). Logic:

1. Resolve plan (defaults to `free` on lookup error â€” fail-closed).
2. Email bucket â€” check the feature flag FIRST. Free/Starter dispatches to `email_marketer` get blocked with "Email sequences require the Growth plan or higher".
3. Numeric bucket â€” `limit == -1` short-circuits to allowed without a count query. `limit == 0` returns a feature-gate-style block message ("Campaign plans aren't included on the Free plan â€” upgrade to unlock").
4. Otherwise count rows in `agent_logs` since `month_start_utc()` ([plan_quotas.py:156-165](backend/services/plan_quotas.py#L156)) filtered to `status in ('completed', 'completed_with_warning')` â€” failed runs don't burn quota.

Block raised as `PlanQuotaExceeded` ([orchestrator.py:93-122](backend/orchestrator.py#L93)) with `as_dict()` carrying `{status, reason, plan, used, limit}`. Caught by chat handler + cron runner + REST `/run` and shown verbatim to the user.

Usage reporting:

- `GET /api/usage/{tenant_id}` ([server.py:2586](backend/server.py#L2586)) â€” wired to real per-tenant usage as of commit `70b48a0`.
- `GET /api/profile/me` ([server.py:1229](backend/server.py#L1229)) returns `{plan, limits: {content_pieces_per_month, campaign_plans_per_month, email_sequences_enabled}}` for the frontend's plan picker + usage badges.
- Plan picker (self-service) lives in `(dashboard)/settings/page.tsx` + `/usage` page; backend routes are split across `plans_profile_router` and `plans_admin_router` at [backend/routers/plans.py](backend/routers/plans.py).

---

## 14. Known gaps / open security items

Carrying over from the prior audit + verified against the current tree:

- **`/api/cron/run-scheduled` public â€” HIGH** ([server.py:5027](backend/server.py#L5027), listed in `_PUBLIC_PATHS` at [server.py:794](backend/server.py#L794)). Anyone on the internet can trigger the global scheduled-task runner including Gmail sync for every connected tenant. Needs HMAC-gating like `/api/internal/security-review`.
- **`/api/email/sync-all` and `/api/email/imap/poll` â€” MEDIUM** ([routers/email.py:992, 1025](backend/routers/email.py#L992)). Live in the inbox/email router. Sync-all is a multi-tenant fan-out; imap-poll triggers a real IMAP fetch. Currently subject only to the global JWT middleware (not `_PUBLIC_PREFIXES`) but with no per-tenant ownership check â€” any authenticated user can trigger sync for all tenants.
- **`/api/settings/email` GET+PATCH IDOR â€” HIGH** ([routers/email.py:737, 758](backend/routers/email.py#L737)). The query-string / body-tenant variants don't apply `get_verified_tenant`; only the path-aliased versions at lines 752 and 771 do. An authenticated user can read or PATCH any tenant's email settings by sending the target's tenant_id in the body or `?tenant_id=`.
- **`/api/ceo/chat/{session_id}/history` IDOR â€” HIGH** ([server.py:6953](backend/server.py#L6953), `routers/ceo.py:9` route signature). Takes a session_id without verifying the caller owns the underlying tenant â€” same pattern as the inbox-item GET below; needs a `_verify_chat_session_owner` helper.
- **`/api/inbox/item/{item_id}` GET â€” MEDIUM** ([routers/inbox.py:66](backend/routers/inbox.py#L66)). The doc comment acknowledges the gap: "If the user forges someone else's item id, they get a row whose tenant_id won't match their own". Other inbox methods use `_verify_inbox_owner` ([routers/inbox.py:32](backend/routers/inbox.py#L32)) which does the lookup-then-verify-tenant dance; the GET still needs the same.
- **`_verify_sendgrid_signature` stub â€” MEDIUM** ([routers/email.py:843-853](backend/routers/email.py#L843)). Function returns True regardless of signature presence (logs a warning). If SendGrid is the configured provider, the endpoint accepts any payload. Postmark + Resend are properly verified.
- **Paperclip skill MD not yet updated for `X-Aria-Agent-Token`** â€” commit `52294b3` added the server-side gate at [routers/inbox.py:852](backend/routers/inbox.py#L852) but the FIXME at line 839 calls out that the operator still needs to update the `aria-backend-api` skill MD inside Paperclip to send `X-Aria-Agent-Token: <ARIA_INTERNAL_AGENT_TOKEN>` on every inbox POST. Until that lands, Path A returns 401 in production; Path B (poll_completed_issues) keeps user-visible behavior alive at ~5s latency instead of near-instant.

---

## 15. Recent git history (last 14 days)

213 commits since 2026-04-30. Grouping by theme:

- **Security tightening (12 commits)** â€” the dominant theme. Bulk-applied `Depends(get_verified_tenant)` to 45+ tenant-scoped endpoints + the WebSocket join_tenant gate (`0b10918`); locked down OAuth init endpoints (`5edb26b`); required JWT + email-match on `/api/tenant/by-email` (`359222a`); locked `/api/inbox/{tenant_id}/items` with `X-Aria-Agent-Token` (`52294b3`); centralized PostgREST `or_()` escape helper with CI lint guard (`dda4e2c`); added Bandit/pip-audit/detect-secrets workflow + Claude PR review via the VPS CLI (`18caa28`, `5bfc81f`); locked down onboarding draft endpoints + dev-mode prod guard (`d0bd06b`); enabled RLS on every tenant-scoped table (`ecf04ab`, `82cb424`).
- **Ban system (9 commits)** â€” admin ban/unban UI + endpoints, duration/until-date/indefinite ban semantics, dedicated `/banned` page with banned-email lookup, OAuth-callback redirects for banned users.
- **Plan picker + quotas (7 commits)** â€” scaffolded `PlanLimits` table + `check_quota` (`f365c44`), self-service plan picker UI in settings (`ed09e01`), `/usage` page wired to real per-tenant data, plan endpoint backend split.
- **Mobile UI refactor (~25 commits)** â€” sticky header + scroll-hide, MobileBottomNav, sidebar drawer swipe-to-close, Virtual Office mobile canvas redesign (canvas â†’ 2Ã—4 grid â†’ stacked room cards depending on viewport), CRM mobile cards, Projects table mobile cards, landing mobile nav, dashboard/campaigns icon-only buttons.
- **CI gating + tests (~15 commits)** â€” gated VPS deploy behind pytest passing (`0ad6615` + `29e419c`), Python path fix for backend imports, pytest-timeout, conftest mock helpers, bootstrapped pytest infra + tenant isolation tests (`9fdedca`), rate-limit + race-condition + malicious-input integration tests (`2dac3a5`), admin ban + agent skill handshake tests (`f49db15`), Playwright E2E specs for onboarding + virtual office sync (`f4bb537`).
- **Reports feature (~10 commits)** â€” campaign_roi funnel chart, daily_pulse 24h activity, channel_spend pie chart, state-of-the-union + agent productivity snapshots persisted to `marketing_reports`, 6 audit-fix follow-ups.
- **CRM + Conversations (~6 commits)** â€” CRM contacts CSV export, "Add to CRM" button on inbound email threads using sender display name, deep-link highlight with DOM polling, plus-addressing on Reply-To.
- **Onboarding hardening (~6 commits)** â€” resume from `onboarding_drafts` table (`3cc1be9`), auth-bind sessions to JWT user, frontend wires resume + auth header, mobile fixes (Save & Exit wrap, stepper hide, persist session), re-onboarding actually persists new answers (`5abb1cd`).
- **Image size optimization (2 commits)** â€” dropped `sentence-transformers` + `torch` for a ~7GB image savings, pinned CPU-only torch.
- **Chat UI polish (4 commits)** â€” full-bleed mobile layout for the chat page, pill-style input, mirror FloatingChat aesthetic, drop divider above input.
- **Virtual Office (~3 commits)** â€” idle agents roam continuously instead of sitting at desks (`192766b`), room decorations get per-room translate so mobile renders correctly.

---

### Surprises during this read

- **The "5 agents" promise has slipped to 6** â€” `media` (Media Designer, Haiku) is treated as a utility available to every tenant ([backend/orchestrator.py:272-274](backend/orchestrator.py#L272)) and bypasses the `active_agents` check, but the CLAUDE.md table at the top of the file still lists 5. The Virtual Office model selector at [server.py:1202-1208](backend/server.py#L1202) explicitly enumerates all 6.
- **A `content_library` table is referenced alongside the canonical `content_library_entries`** â€” the bare `content_library` reference appears in only one place; everything live uses `content_library_entries`. Worth dropping the legacy reference or migrating data if anything actually wrote to it.
- **The Stripe code is fully wired and importable** â€” `stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")` runs on every backend boot ([stripe_tool.py:6](backend/tools/stripe_tool.py#L6)), and the security review prompt's mention of "Stripe deferred" is operator memory, not enforced by anything in the codebase. If `STRIPE_SECRET_KEY` is ever set to a real value, `create_customer`/`create_subscription` will silently start working.
- **`sentry-sdk[fastapi]` is installed but never initialised** â€” see Section 12. Trivial fix; surprising that the dependency ships unused.
- **The lifespan starts SIX background loops, not the three I expected** â€” `_gmail_sync_loop` (no-op by default), `_scheduler_executor_loop`, `_paperclip_office_sync_loop`, `_followup_nudge_loop` (6h, agent-driven), `_content_repurpose_loop` (7-day), and `imap_poll_loop`. The followup nudge and content repurpose loops aren't in CLAUDE.md.
- **The default `claude_cli.py` model constants point to 4-6** â€” `MODEL_OPUS = "claude-opus-4-6"`, `MODEL_SONNET = "claude-sonnet-4-6"`, `MODEL_HAIKU = "claude-haiku-4-5"` ([backend/tools/claude_cli.py:20-22](backend/tools/claude_cli.py#L20)). But the security review endpoint hardcodes `DEFAULT_MODEL = "claude-opus-4-7"` ([routers/security_review.py:43](backend/routers/security_review.py#L43)). The agent fleet hasn't been bumped to 4-7 yet.
- **`profiles.role` cache is referenced as "60s TTL"** in middleware comments at [server.py:962](backend/server.py#L962) but the actual cache implementation lives in `backend/services/profiles.py` â€” worth a look if you're touching admin RBAC.
- **`/api/cron/run-scheduled` ALSO triggers Gmail sync for every tenant** ([server.py:5031-5041](backend/server.py#L5031)) â€” which makes the public-endpoint gap (Section 14) more impactful than just running the scheduler loop on-demand. A drive-by HTTP call kicks off a cross-tenant Gmail fetch.