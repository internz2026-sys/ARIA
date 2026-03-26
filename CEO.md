# ARIA — CEO Orchestrator Blueprint

> The central command document for ARIA's AI marketing team.
> This file defines how 5 AI agents are coordinated, dispatched, and governed
> using Paperclip AI as the orchestration backbone.

---

## 1. Mission

ARIA is the marketing co-founder that developer founders never had — it builds
your GTM strategy and then executes it for you.

Developer founders are exceptional at building products but consistently fail
at marketing them. ARIA deploys 5 autonomous AI agents organized as a marketing
team, providing both strategic guidance (what to do) and tactical execution
(doing it) — with a copy-paste delivery model for v1.

The orchestrator is the CEO (Chief Marketing Strategist) of this agent team.
It onboards the user, builds the GTM playbook, coordinates all other agents,
reviews outputs, and adjusts strategy based on performance.

---

## 2. Architecture Overview

```
                         ┌──────────────────────┐
                         │    Paperclip AI       │
                         │   localhost:3100      │
                         │                      │
                         │  - Org chart          │
                         │  - Heartbeats         │
                         │  - Budget control     │
                         │  - Task checkout      │
                         │  - Run tracking       │
                         └──────────┬───────────┘
                                    │ REST API
                                    ▼
┌────────────┐          ┌──────────────────────┐          ┌──────────────┐
│  Next.js   │◄────────►│   FastAPI Backend     │◄────────►│   Supabase   │
│  Frontend  │  HTTP    │   localhost:8000      │  DB/Auth │   Cloud      │
│  :3000     │          │                      │          │              │
└────────────┘          │  ┌──────────────────┐│          └──────────────┘
                        │  │   Orchestrator    ││
                        │  │                  ││
                        │  │  dispatch_agent()││
                        │  │  run_workflow()  ││
                        │  └────────┬─────────┘│
                        │           │          │
                        │  ┌────────▼─────────┐│
                        │  │  5 Agent Modules  ││
                        │  │  (Claude CLI)     ││
                        │  └──────────────────┘│
                        └──────────────────────┘
```

**Dispatch flow:**
1. Trigger arrives (cron, manual, or workflow step)
2. `orchestrator.dispatch_agent()` checks tenant config (is agent active?)
3. If Paperclip is connected → routes through Paperclip heartbeat API
4. Paperclip enforces rate limits, budgets, and atomic task checkout
5. Agent module executes via local Claude Code CLI
6. Result logged to Supabase `agent_logs` table
7. Real-time event emitted via Socket.IO to frontend dashboard

**Fallback:** If Paperclip is unreachable, agents run locally with direct dispatch.

---

## 3. Org Chart — The Marketing Team

```
                              ┌──────────────┐
                              │  ARIA_CEO    │
                              │  Chief Mktg  │
                              │  Strategist  │
                              └──────┬───────┘
              ┌──────────┬──────────┼──────────┬──────────┐
              ▼          ▼          ▼          ▼          ▼
        ┌──────────┐┌──────────┐┌──────────┐┌──────────┐
        │ Content  ││  Email   ││  Social  ││   Ad     │
        │ Writer   ││ Marketer ││ Manager  ││Strategist│
        └──────────┘└──────────┘└──────────┘└──────────┘
```

### Agent Details

| Agent | Slug | Role | Key Outputs |
|-------|------|------|-------------|
| **ARIA_CEO** | `ceo` | Chief Marketing Strategist | GTM playbook, strategy reviews, agent coordination, performance adjustments |
| **ContentWriter** | `content_writer` | Content Creation Agent | Blog posts, landing page copy, Product Hunt copy, Show HN posts, case studies |
| **EmailMarketer** | `email_marketer` | Email Campaign Agent | Welcome sequences, newsletters, launch sequences, re-engagement emails |
| **SocialManager** | `social_manager` | Social Media Agent | X/Twitter threads, LinkedIn posts, Facebook posts, content calendar, hashtags |
| **AdStrategist** | `ad_strategist` | Paid Ads Advisor | Campaign structure, audience targeting, ad creative, step-by-step Ads Manager guides |

---

## 4. Onboarding Flow (CEO Agent)

The onboarding is the most critical feature. It transforms ARIA from a generic
content tool into a strategic marketing partner.

### Intake Conversation (10–15 minutes)
1. **Product Discovery** — What is the product? What problem does it solve? What makes it different? Competitors?
2. **Audience Definition** — Who is the ideal customer? Pain points? Where do they hang out online?
3. **Goals & Constraints** — Success metrics, timeline, marketing budget, weekly time commitment
4. **Channel Prioritization** — Based on audience + product type, recommend focus channels

### GTM Playbook Output
- Positioning statement
- Messaging pillars (3–5 key themes)
- Content themes aligned to audience pain points
- Channel strategy (prioritized by impact)
- 30/60/90 day action plan
- KPIs to track

This playbook becomes the system of record that all other agents reference.

---

## 5. Agent Responsibilities

### 5.1 ContentWriter — Content Creation Engine

**Content Types (v1):**
| Type | Description | Output |
|------|-------------|--------|
| Blog Posts | SEO-optimized articles for the product's audience | 1,000–2,000 word article |
| Landing Page Copy | Headline, subheadline, features, social proof, CTA | Structured copy blocks |
| Product Hunt Copy | Title, tagline, description, first comment | Complete PH listing |
| Show HN / Indie Hackers | Community-appropriate product story posts | Draft with norms guidance |
| Email Copy | Blog-to-email adaptations, announcements | Email body + subject lines |

**Key differentiators:**
- Brand voice consistency learned from onboarding
- Content topics suggested from GTM playbook, not random
- Maintains a content calendar with proactive suggestions

### 5.2 EmailMarketer — Email Campaigns

**Campaign Types:**
- **Welcome Sequence** — 3–5 emails for new signups (intro, value, activation)
- **Launch Sequence** — pre-launch teaser, launch day, social proof follow-up, final reminder
- **Newsletter Template** — weekly/biweekly with product updates, content roundup
- **Re-engagement** — win-back emails for inactive users

**Output:** Subject line (2–3 A/B variants), preview text, body (plain text + HTML-ready), recommended send time, segmentation notes. Copy-paste into Mailchimp/ConvertKit/Resend.

### 5.3 SocialManager — Social Media

**Platforms (v1):**
- **X/Twitter** — threads, standalone posts, engagement replies (developer audiences)
- **LinkedIn** — professional posts, article summaries (B2B)
- **Facebook** — page posts, group content (ad copy support)

**Output:** Copy-paste-ready posts with character counts, hashtags, posting times, image/visual descriptions (text only in v1).

### 5.4 AdStrategist — Facebook Ads Advisor (Copy-Paste Model)

This is a critical v1 differentiator. No API integration — instead produces detailed step-by-step instructions for someone who has never used Ads Manager.

**Outputs:**
- Campaign structure (objective, ad sets, budget allocation)
- Audience targeting specs (exact values to enter in Ads Manager)
- Ad creative (primary text variants, headline, description, CTA button)
- Step-by-step setup guide (numbered instructions for Ads Manager)
- Budget recommendations (daily/lifetime based on stated budget)
- A/B testing plan (what to test, how long, what metrics to evaluate)
- Optimization checkpoints (scheduled cron reminders to review performance)

---

## 6. Workflows

### Content Pipeline
```
CEO (strategy) → ContentWriter (draft) → CEO (review) → User (approve)
                                       ↘ EmailMarketer (adapt for email)
                                       ↘ SocialManager (adapt for social)
```

### Campaign Launch
```
CEO (campaign brief) → AdStrategist (campaign plan)
                     → ContentWriter (landing page copy)
                     → EmailMarketer (launch sequence)
                     → SocialManager (social posts)
```

### Weekly Cadence
```
Monday:    ContentWriter drafts weekly blog post
Tuesday:   SocialManager creates week's social calendar
Wednesday: EmailMarketer prepares newsletter
Thursday:  AdStrategist reviews ad performance (if running)
Friday:    CEO weekly strategy review + next week planning
```

---

## 7. Data Flow

### Tenant Config (Input)

Every agent receives the tenant's GTM playbook and profile:

```
TenantConfig
  ├── business_name, industry, description
  ├── product (name, value_props, competitors, differentiators)
  ├── audience (icp, pain_points, channels, language_patterns)
  ├── gtm_playbook (positioning, messaging_pillars, channel_strategy, action_plan)
  ├── brand_voice (tone, examples, guidelines)
  ├── active_agents (list of enabled agent slugs)
  ├── plan (starter/growth/scale)
  └── owner_email, timezone
```

### Agent Logs (Output)

Every agent run is logged:

```json
{
  "tenant_id": "uuid",
  "agent_name": "content_writer",
  "action": "blog_post",
  "result": { "status": "completed", "content_type": "blog", "..." },
  "status": "completed",
  "timestamp": "2026-03-24T10:00:00Z"
}
```

---

## 8. Pricing Tiers & Agent Limits

| Tier | Price | Content/mo | Campaigns/mo | Agents |
|------|-------|-----------|-------------|--------|
| Starter | $49 | 10 pieces | 1 | CEO + ContentWriter |
| Growth | $149 | 30 pieces | 3 | All 5 agents |
| Scale | $299 | Unlimited | Unlimited | All 5 + custom configs |

---

## 9. Integration Roadmap

| Phase | Timeline | Focus |
|-------|----------|-------|
| v1 | Launch | Copy-paste model, step-by-step instructions |
| v1.5 | +60 days | Email providers (ConvertKit, Mailchimp) via MCP |
| v2 | +120 days | Social publishing (X/Twitter, LinkedIn) via MCP |
| v2.5 | +180 days | Meta Ads API for automated campaign management |
| v3 | +270 days | Analytics ingestion (Google Analytics, Plausible) |
| v3+ | +12 months | Expand beyond marketing: CRM/sales agent, support agent |

---

## 10. Success Metrics

| Metric | Target (90 days) | Why |
|--------|-----------------|-----|
| Onboarding completion | >70% | If users don't finish, product can't deliver value |
| Content adoption | >50% used/published | Measures whether outputs are useful |
| Weekly active users | >40% of paying users | Ongoing value, not one-time curiosity |
| Month 2 retention | >60% | Core product-market fit signal |
| NPS | >40 | Satisfaction and word-of-mouth |
| Paying users | 200+ | Willingness to pay |
| MRR | $15,000+ | Sustainable business |

---

## 11. Key Files

| File | Role |
|------|------|
| `backend/orchestrator.py` | CEO brain — dispatch, workflows, scheduling |
| `backend/paperclip_sync.py` | Syncs agents with Paperclip on startup |
| `backend/server.py` | FastAPI app — all HTTP/WS endpoints |
| `backend/agents/__init__.py` | Agent registry + department map |
| `backend/agents/ceo_agent.py` | Chief Marketing Strategist |
| `backend/agents/content_writer_agent.py` | Content creation |
| `backend/agents/email_marketer_agent.py` | Email campaigns |
| `backend/agents/social_manager_agent.py` | Social media |
| `backend/agents/ad_strategist_agent.py` | Paid ads advisor |
| `backend/tools/claude_cli.py` | Local Claude Code CLI wrapper |
| `backend/config/tenant_schema.py` | Pydantic models for tenant config |
| `backend/onboarding_agent.py` | Conversational GTM strategy builder |

---

## 12. Running the Orchestrator

### Start Paperclip (orchestration layer)
```bash
npx paperclipai onboard --yes
# Runs at http://127.0.0.1:3100
```

### Start ARIA Backend (agent execution)
```bash
cd C:\Users\Admin\Documents\ARIA
pip install -r backend/requirements.txt
uvicorn backend.server:socket_app --reload --port 8000
```

### Verify
```bash
curl http://localhost:8000/health
curl http://localhost:8000/api/paperclip/status
curl -X POST http://localhost:8000/api/agents/{tenant_id}/content_writer/run
```
