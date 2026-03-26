# ARIA — AI-Powered Business Automation Platform

ARIA is a multi-tenant SaaS platform that deploys AI agents across every department of a small business — from lead generation and sales to customer support, finance, and operations. Business owners describe their company once, and ARIA auto-configures a team of 18 intelligent agents tailored to their industry.

## Architecture

```
ARIA/
├── backend/                 # Python FastAPI server
│   ├── config/              # Tenant schema & Supabase loader
│   ├── agents/              # 18 AI agents across 6 departments
│   ├── tools/               # 12 third-party API wrappers
│   ├── tasks/               # Workflow templates & cron schedules
│   ├── onboarding_agent.py  # Conversational business profiler
│   ├── orchestrator.py      # Paperclip AI multi-agent orchestration
│   └── server.py            # FastAPI + WebSocket + webhooks
├── frontend/                # Next.js 14 App Router
│   ├── app/(marketing)/     # Landing, pricing, features, blog, etc.
│   ├── app/(auth)/          # Login, signup, forgot password
│   ├── app/(onboarding)/    # 6-step onboarding wizard
│   ├── app/(dashboard)/     # Dashboard, inbox, agents, analytics, settings
│   └── components/          # UI primitives, shared components, chat widget
├── .env.example             # Required environment variables
└── railway.toml             # Railway deployment config
```

## Tech Stack

| Layer | Technology |
|-------|-----------|
| **AI Models** | Anthropic Claude (Opus 4.6, Sonnet 4.6, Haiku 4.5) |
| **Agent Orchestration** | Paperclip AI |
| **Backend** | Python, FastAPI, Socket.IO |
| **Frontend** | Next.js 14, React 18, Tailwind CSS |
| **Database & Auth** | Supabase (PostgreSQL + Auth) |
| **Payments** | Stripe |
| **Email** | SendGrid |
| **CRM** | HubSpot |
| **Messaging** | Twilio (WhatsApp/SMS) |
| **Scheduling** | Calendly |
| **Data Enrichment** | Apollo, Hunter.io |
| **Deployment** | Railway |

## Agents (18 total)

### Sales & Lead Gen
- **Lead Gen Agent** (Sonnet) — Prospect discovery, ICP scoring
- **Outreach Agent** (Sonnet) — Multi-step email sequences with A/B testing
- **Closer Agent** (Opus) — Adaptive sales conversations, objection handling
- **Follow-up Agent** (Sonnet) — Cold lead re-engagement

### Finance
- **Accounting Summary Agent** (Haiku) — Daily/weekly financial reports
- **Invoice Agent** (Haiku) — Invoicing with escalating reminders
- **Expense Alert Agent** (Haiku) — Transaction categorization & alerts

### Customer Service
- **Support Agent** (Sonnet) — FAQ, complaints, smart escalation
- **Review Agent** (Haiku) — Review requests & response generation
- **Feedback Agent** (Haiku) — NPS surveys & testimonial collection

### Marketing
- **Social Media Agent** (Sonnet) — DM responses & content scheduling
- **Content Agent** (Sonnet) — Newsletters & promotional copy
- **Ad Monitor Agent** (Haiku) — Ad spend tracking & alerts

### Operations
- **CRM Agent** (Haiku) — Interaction logging & deal stage management
- **Scheduling Agent** (Haiku) — Calendar management via Calendly
- **Customer Onboarding Agent** (Sonnet) — Day 0/3/7 welcome sequences

### Internal Ops
- **HR & Payroll Agent** (Haiku) — Attendance & payroll summaries
- **Analytics Agent** (Haiku) — Weekly business health report

## Getting Started

### Prerequisites
- Python 3.11+
- Node.js 18+
- Supabase project
- API keys for integrations (see `.env.example`)

### Backend Setup

```bash
cd backend
python -m venv venv
source venv/bin/activate  # or venv\Scripts\activate on Windows
pip install -r requirements.txt
cp ../.env.example ../.env  # Fill in your API keys
uvicorn server:app --reload --port 8000
```

### Frontend Setup

```bash
cd frontend
npm install
npm run dev
```

The frontend runs on `http://localhost:3000` and the backend on `http://localhost:8000`.

## Environment Variables

Copy `.env.example` to `.env` and fill in all required keys:

- `ANTHROPIC_API_KEY` — Claude API access
- `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` — Database & auth
- `STRIPE_SECRET_KEY` — Payment processing
- `SENDGRID_API_KEY` — Transactional email
- `APOLLO_API_KEY` / `HUNTER_API_KEY` — Lead enrichment
- `TWILIO_*` — WhatsApp & SMS
- See `.env.example` for the full list

## Deployment

ARIA is configured for Railway deployment via `railway.toml`. Deploy both services:

```bash
railway up
```

## Design System

| Token | Value |
|-------|-------|
| Primary | `#534AB7` |
| Success | `#1D9E75` |
| Warning | `#BA7517` |
| Danger | `#D85A30` |
| Text Primary | `#2C2C2A` |
| Text Secondary | `#5F5E5A` |
| Background | `#F8F8F6` |
| Border | `#E0DED8` |
| Card Radius | `12px` |
| Input Radius | `8px` |
| CTA Radius | `24px` |

## License

Proprietary — All rights reserved.
