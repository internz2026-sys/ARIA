"""ARIA FastAPI Server — webhooks, chat, agent management, dashboard API."""
from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import socketio
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional

load_dotenv()

from backend.config.loader import get_tenant_config, save_tenant_config
from backend.onboarding_agent import OnboardingAgent
from backend.orchestrator import (
    dispatch_agent,
    get_agent_status,
    handle_webhook,
    pause_agent_paperclip,
    resume_agent_paperclip,
    run_scheduled_agents,
)
from backend.paperclip_sync import initialize as paperclip_init, is_connected as paperclip_connected

# Socket.IO for real-time events
sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: sync agents with Paperclip AI orchestrator."""
    await paperclip_init()
    yield


app = FastAPI(title="ARIA API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount Socket.IO
socket_app = socketio.ASGIApp(sio, other_asgi_app=app)


# ─── Socket.IO Events ───
@sio.event
async def connect(sid, environ):
    pass


@sio.event
async def join_tenant(sid, data):
    tenant_id = data.get("tenant_id", "")
    if tenant_id:
        sio.enter_room(sid, tenant_id)


# Active onboarding sessions
onboarding_sessions: dict[str, OnboardingAgent] = {}

# ─── Virtual Office Agent Definitions (matches AGENT_REGISTRY) ───
VIRTUAL_OFFICE_AGENTS = [
    {"agent_id": "ceo", "name": "ARIA CEO", "role": "Chief Marketing Strategist", "model": "opus-4-6", "department": "ceo-office"},
    {"agent_id": "content_writer", "name": "Content Writer", "role": "Content Creation Agent", "model": "sonnet-4-6", "department": "content-studio"},
    {"agent_id": "email_marketer", "name": "Email Marketer", "role": "Email Campaign Agent", "model": "sonnet-4-6", "department": "email-room"},
    {"agent_id": "social_manager", "name": "Social Manager", "role": "Social Media Agent", "model": "sonnet-4-6", "department": "social-hub"},
    {"agent_id": "ad_strategist", "name": "Ad Strategist", "role": "Paid Ads Advisor", "model": "sonnet-4-6", "department": "ads-room"},
]


# ─── Health Check ───
@app.get("/health")
async def health():
    return {"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat()}


# ─── Onboarding API ───
class OnboardingMessage(BaseModel):
    session_id: str
    message: str


class OnboardingStart(BaseModel):
    session_id: Optional[str] = None


@app.get("/api/tenant/by-email/{email}")
async def tenant_by_email(email: str):
    """Look up a tenant config by owner email. Returns tenant_id if found."""
    try:
        from backend.config.loader import _get_supabase
        sb = _get_supabase()
        result = sb.table("tenant_configs").select("tenant_id").eq("owner_email", email).limit(1).execute()
        if result.data and len(result.data) > 0:
            return {"tenant_id": result.data[0]["tenant_id"]}
        return {"tenant_id": None}
    except Exception:
        return {"tenant_id": None}


@app.post("/api/onboarding/start")
async def start_onboarding(body: OnboardingStart):
    session_id = body.session_id or str(uuid.uuid4())
    agent = OnboardingAgent()
    greeting = agent.start_conversation()
    onboarding_sessions[session_id] = agent
    return {"session_id": session_id, "message": greeting}


@app.post("/api/onboarding/message")
async def onboarding_message(body: OnboardingMessage):
    agent = onboarding_sessions.get(body.session_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Session not found")
    response = await agent.process_message(body.message)
    return {
        "message": response,
        "is_complete": agent.is_complete(),
        "questions_answered": agent.questions_answered,
    }


@app.post("/api/onboarding/skip")
async def onboarding_skip(body: OnboardingStart):
    agent = onboarding_sessions.get(body.session_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Session not found")
    skipped = agent.skip_current_topic()
    current = agent.get_current_topic()
    return {
        "skipped_topic": skipped,
        "current_topic": current,
        "questions_answered": agent.questions_answered,
        "is_complete": agent.is_complete(),
        "skipped_topics": agent.skipped_topics,
    }


@app.post("/api/onboarding/extract-config")
async def extract_config(body: OnboardingStart):
    agent = onboarding_sessions.get(body.session_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Session not found")
    config_data = await agent.extract_config()
    return {"config": config_data}


class SaveConfig(BaseModel):
    session_id: str
    owner_email: str
    owner_name: str
    active_agents: list[str] | None = None


@app.post("/api/onboarding/save-config")
async def save_config(body: SaveConfig):
    agent = onboarding_sessions.get(body.session_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Session not found")
    tenant_id = str(uuid.uuid4())
    config = await agent.build_tenant_config(tenant_id, body.owner_email, body.owner_name, body.active_agents)
    save_tenant_config(config)
    del onboarding_sessions[body.session_id]
    return {"tenant_id": tenant_id, "config": config.model_dump(mode="json")}


class SaveConfigDirect(BaseModel):
    """Accept the raw extracted config JSON (cached on the frontend) to save
    directly — no backend session needed."""
    config: dict
    owner_email: str
    owner_name: str
    active_agents: list[str] | None = None
    skipped_topics: list[str] | None = None


@app.post("/api/onboarding/save-config-direct")
async def save_config_direct(body: SaveConfigDirect):
    from backend.config.tenant_schema import (
        TenantConfig, ICPConfig, ProductConfig, GTMPlaybook, BrandVoice,
    )
    extracted = body.config
    has_skips = bool(body.skipped_topics)
    tenant_id = str(uuid.uuid4())
    config = TenantConfig(
        tenant_id=tenant_id,
        business_name=extracted.get("business_name", ""),
        industry=extracted.get("industry", "technology"),
        description=extracted.get("description", ""),
        icp=ICPConfig(**extracted.get("icp", {})),
        product=ProductConfig(**extracted.get("product", {})),
        gtm_playbook=GTMPlaybook(**extracted.get("gtm_playbook", {})),
        brand_voice=BrandVoice(**extracted.get("brand_voice", {})),
        active_agents=body.active_agents or extracted.get("recommended_agents", ["ceo", "content_writer"]),
        channels=extracted.get("channels", []),
        owner_email=body.owner_email,
        owner_name=body.owner_name,
        plan="starter",
        onboarding_status="completed" if not has_skips else "in_progress",
        skipped_fields=body.skipped_topics or [],
    )
    save_tenant_config(config)
    return {"tenant_id": tenant_id, "config": config.model_dump(mode="json")}


# ─── Webhook Endpoints ───
@app.post("/api/webhooks/sendgrid")
async def sendgrid_webhook(request: Request):
    payload = await request.json()
    tenant_id = request.headers.get("X-Tenant-Id", "")
    result = await handle_webhook("inbound_email", {"tenant_id": tenant_id, **payload})
    await sio.emit("agent_event", result, room=tenant_id)
    return result


@app.post("/api/webhooks/stripe")
async def stripe_webhook(request: Request):
    payload = await request.json()
    event_type = payload.get("type", "")
    tenant_id = payload.get("data", {}).get("object", {}).get("metadata", {}).get("tenant_id", "")
    if "invoice" in event_type:
        result = await handle_webhook("payment_received", {"tenant_id": tenant_id, **payload})
    else:
        result = {"status": "ignored", "event": event_type}
    return result


@app.post("/api/webhooks/shopify")
async def shopify_webhook(request: Request):
    payload = await request.json()
    tenant_id = request.headers.get("X-Tenant-Id", "")
    topic = request.headers.get("X-Shopify-Topic", "")
    event_map = {"orders/create": "new_order", "checkouts/create": "abandoned_cart"}
    event_type = event_map.get(topic, "unknown")
    result = await handle_webhook(event_type, {"tenant_id": tenant_id, **payload})
    return result


# ─── Agent Management API ───
@app.get("/api/agents/{tenant_id}")
async def list_agents(tenant_id: str):
    statuses = await get_agent_status(tenant_id)
    return {"tenant_id": tenant_id, "agents": statuses}


@app.post("/api/agents/{tenant_id}/{agent_name}/run")
async def run_agent(tenant_id: str, agent_name: str):
    result = await dispatch_agent(tenant_id, agent_name)
    await sio.emit("agent_event", result, room=tenant_id)

    # Save output to inbox
    content = result.get("result", "")
    if content and isinstance(content, str):
        content_type = _infer_content_type(agent_name, content)
        title = _extract_title(agent_name, "", content)
        saved = _save_inbox_item(
            tenant_id=tenant_id,
            agent=agent_name,
            title=title,
            content=content,
            content_type=content_type,
        )
        if saved:
            await sio.emit("inbox_new_item", {
                "id": saved["id"],
                "agent": agent_name,
                "type": content_type,
                "title": title,
                "status": "ready",
                "created_at": saved.get("created_at", ""),
            }, room=tenant_id)

    return result


@app.post("/api/agents/{tenant_id}/{agent_name}/pause")
async def pause_agent(tenant_id: str, agent_name: str):
    config = get_tenant_config(tenant_id)
    if agent_name in config.active_agents:
        config.active_agents.remove(agent_name)
        save_tenant_config(config)
    # Also pause in Paperclip orchestrator
    await pause_agent_paperclip(agent_name)
    return {"status": "paused", "agent": agent_name}


@app.post("/api/agents/{tenant_id}/{agent_name}/resume")
async def resume_agent(tenant_id: str, agent_name: str):
    config = get_tenant_config(tenant_id)
    if agent_name not in config.active_agents:
        config.active_agents.append(agent_name)
        save_tenant_config(config)
    # Also resume in Paperclip orchestrator
    await resume_agent_paperclip(agent_name)
    return {"status": "resumed", "agent": agent_name}


# ─── Virtual Office API ───
@app.get("/api/office/agents/{tenant_id}")
async def virtual_office_agents(tenant_id: str):
    """Return all 18 virtual office agents with their current status."""
    now = datetime.now(timezone.utc).isoformat()
    agents = [
        {
            "agent_id": a["agent_id"],
            "name": a["name"],
            "role": a["role"],
            "model": a["model"],
            "status": "idle",
            "current_task": "",
            "department": a["department"],
            "last_updated": now,
        }
        for a in VIRTUAL_OFFICE_AGENTS
    ]
    return {"agents": agents}


# ─── Dashboard API ───
@app.get("/api/dashboard/{tenant_id}/config")
async def dashboard_config(tenant_id: str):
    """Return tenant business info for the dashboard."""
    try:
        config = get_tenant_config(tenant_id)
        return {
            "tenant_id": tenant_id,
            "business_name": config.business_name,
            "product_name": config.product.name,
            "product_description": config.product.description,
            "positioning": config.gtm_playbook.positioning,
            "channels": config.channels,
            "active_agents": config.active_agents,
            "brand_voice_tone": config.brand_voice.tone,
            "action_plan_30": config.gtm_playbook.action_plan_30,
            "messaging_pillars": config.gtm_playbook.messaging_pillars,
            "onboarding_status": config.onboarding_status,
            "skipped_fields": config.skipped_fields,
        }
    except Exception:
        return {"tenant_id": tenant_id, "business_name": None}


@app.get("/api/dashboard/{tenant_id}/stats")
async def dashboard_stats(tenant_id: str):
    return {
        "tenant_id": tenant_id,
        "kpis": {
            "content_published": {"value": 0, "delta": 0, "delta_pct": 0},
            "emails_sent": {"value": 0, "open_rate": 0, "click_rate": 0},
            "social_engagement": {"value": 0, "delta_pct": 0},
            "ad_spend": {"value": 0, "roas": 0},
        },
    }


@app.get("/api/dashboard/{tenant_id}/activity")
async def dashboard_activity(tenant_id: str):
    from backend.config.loader import _get_supabase
    sb = _get_supabase()
    result = sb.table("agent_logs").select("*").eq("tenant_id", tenant_id).order("timestamp", desc=True).limit(50).execute()
    return {"tenant_id": tenant_id, "activity": result.data}


@app.get("/api/dashboard/{tenant_id}/inbox")
async def dashboard_inbox(tenant_id: str):
    """Return inbox items for the dashboard (latest 5)."""
    try:
        from backend.config.loader import _get_supabase
        sb = _get_supabase()
        result = sb.table("inbox_items").select("*").eq("tenant_id", tenant_id).order("created_at", desc=True).limit(5).execute()
        return {"tenant_id": tenant_id, "items": result.data}
    except Exception:
        return {"tenant_id": tenant_id, "items": []}


@app.get("/api/inbox/{tenant_id}")
async def list_inbox(tenant_id: str, status: str = ""):
    """List all inbox items for a tenant, optionally filtered by status."""
    try:
        from backend.config.loader import _get_supabase
        sb = _get_supabase()
        query = sb.table("inbox_items").select("*").eq("tenant_id", tenant_id)
        if status:
            query = query.eq("status", status)
        result = query.order("created_at", desc=True).execute()
        return {"items": result.data}
    except Exception as e:
        return {"items": [], "error": str(e)}


@app.patch("/api/inbox/{item_id}")
async def update_inbox_item(item_id: str, request: Request):
    """Update an inbox item's status (ready, needs_review, completed, archived)."""
    from backend.config.loader import _get_supabase
    sb = _get_supabase()
    body = await request.json()
    updates = {}
    if "status" in body:
        updates["status"] = body["status"]
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    sb.table("inbox_items").update(updates).eq("id", item_id).execute()
    return {"ok": True}


@app.delete("/api/inbox/{item_id}")
async def delete_inbox_item(item_id: str):
    """Delete an inbox item."""
    from backend.config.loader import _get_supabase
    sb = _get_supabase()
    sb.table("inbox_items").delete().eq("id", item_id).execute()
    return {"ok": True}


@app.get("/api/analytics/{tenant_id}")
async def analytics_data(tenant_id: str, date_range: str = "7d"):
    return {
        "tenant_id": tenant_id,
        "date_range": date_range,
        "funnel": {
            "impressions": 0, "clicks": 0, "signups": 0,
            "activated": 0, "converted": 0, "retained": 0,
        },
    }


# ─── Paperclip AI Integration ───
@app.get("/api/paperclip/status")
async def paperclip_status():
    """Check if Paperclip AI orchestrator is connected."""
    from backend.paperclip_sync import get_company_id, _agent_id_cache
    return {
        "connected": paperclip_connected(),
        "company_id": get_company_id(),
        "agents_registered": len(_agent_id_cache),
        "url": os.environ.get("PAPERCLIP_API_URL", "http://127.0.0.1:3100"),
    }


@app.post("/api/paperclip/heartbeat/{agent_name}")
async def paperclip_heartbeat(agent_name: str, request: Request):
    """Callback endpoint for Paperclip heartbeat invocations.

    When Paperclip triggers a heartbeat, it POSTs here. ARIA executes the
    agent logic and returns the result to Paperclip.
    """
    payload = await request.json()
    tenant_id = payload.get("metadata", {}).get("tenant_id")
    context = payload.get("metadata", {}).get("context", {})
    run_id = request.headers.get("X-Paperclip-Run-Id", "")

    if not tenant_id:
        raise HTTPException(status_code=400, detail="tenant_id required in metadata")

    from backend.agents import AGENT_REGISTRY
    agent_module = AGENT_REGISTRY.get(agent_name)
    if not agent_module:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {agent_name}")

    try:
        result = await agent_module.run(
            tenant_id,
            **({"context": context} if context and "context" in agent_module.run.__code__.co_varnames else {}),
        )
        result["paperclip_run_id"] = run_id
        await sio.emit("agent_event", result, room=tenant_id)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── CEO Task Triage ───
class TriageRequest(BaseModel):
    title: str

@app.post("/api/ceo/triage")
async def ceo_triage(body: TriageRequest):
    """CEO agent analyzes a task and returns column, priority, and assigned agent."""
    from backend.tools.claude_cli import call_claude
    import json as _json

    system = (
        "You are the ARIA CEO, a Chief Marketing Strategist. "
        "Given a marketing task description, classify it by returning ONLY a JSON object with these fields:\n"
        '- "column": one of "backlog", "todo", "in_progress" (use your judgment: vague/aspirational ideas → backlog, concrete actionable tasks → todo, urgent/time-sensitive → in_progress)\n'
        '- "priority": one of "low", "medium", "high" (based on impact and urgency)\n'
        '- "agent": one of "ceo", "content_writer", "email_marketer", "social_manager", "ad_strategist" (the best agent for the job)\n'
        '- "reason": one short sentence explaining your decision\n'
        "Return ONLY valid JSON, no markdown, no explanation outside the JSON."
    )
    try:
        raw = await call_claude(system, f"Triage this task: {body.title}", tenant_id="global")
        # Extract JSON from response
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start >= 0 and end > start:
            result = _json.loads(raw[start:end])
            # Validate values
            if result.get("column") not in ("backlog", "todo", "in_progress"):
                result["column"] = "todo"
            if result.get("priority") not in ("low", "medium", "high"):
                result["priority"] = "medium"
            if result.get("agent") not in ("ceo", "content_writer", "email_marketer", "social_manager", "ad_strategist"):
                result["agent"] = "ceo"
            return result
        return {"column": "todo", "priority": "medium", "agent": "ceo", "reason": "Could not parse CEO response"}
    except Exception:
        return {"column": "todo", "priority": "medium", "agent": "ceo", "reason": "CEO agent unavailable, using defaults"}


# ─── Cron trigger endpoint ───
@app.post("/api/cron/run-scheduled")
async def cron_trigger():
    results = await run_scheduled_agents()
    return {"status": "completed", "tasks_run": len(results) if results else 0}


# ─── Inbox helpers ───

def _infer_content_type(agent: str, content: str) -> str:
    """Infer the content type from the agent slug and output."""
    type_map = {
        "content_writer": "blog_post",
        "email_marketer": "email_sequence",
        "social_manager": "social_post",
        "ad_strategist": "ad_campaign",
        "ceo": "strategy_update",
    }
    return type_map.get(agent, "general")


def _extract_title(agent: str, task_desc: str, content: str) -> str:
    """Extract a short title from the task description or content."""
    if task_desc and len(task_desc) > 5:
        title = task_desc[:120].split("\n")[0]
        if len(task_desc) > 120:
            title = title.rsplit(" ", 1)[0] + "..."
        return title
    # Fallback: first non-empty line of content
    for line in content.split("\n"):
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped[:120]
    return f"{agent} output"


def _save_inbox_item(
    tenant_id: str,
    agent: str,
    title: str,
    content: str,
    content_type: str = "general",
    priority: str = "medium",
    task_id: str | None = None,
    chat_session_id: str | None = None,
) -> dict | None:
    """Save an agent output to the inbox_items table. Returns the saved row."""
    try:
        from backend.config.loader import _get_supabase
        sb = _get_supabase()
        row = {
            "tenant_id": tenant_id,
            "agent": agent,
            "type": content_type,
            "title": title,
            "content": content,
            "status": "ready",
            "priority": priority,
        }
        if task_id:
            row["task_id"] = task_id
        if chat_session_id:
            row["chat_session_id"] = chat_session_id
        result = sb.table("inbox_items").insert(row).execute()
        return result.data[0] if result.data else None
    except Exception as e:
        logging.getLogger("aria.inbox").error("Failed to save inbox item: %s", e)
        return None


async def _run_agent_to_inbox(
    agent_module, agent_id: str, tenant_id: str, task_desc: str,
    session_id: str | None = None, task_id: str | None = None,
    priority: str = "medium",
):
    """Run an agent in background and save the result to the inbox."""
    try:
        result = await agent_module.run(tenant_id, context={"action": task_desc})
        content = result.get("result", "")
        if not content:
            return

        content_type = _infer_content_type(agent_id, content)
        title = _extract_title(agent_id, task_desc, content)

        saved = _save_inbox_item(
            tenant_id=tenant_id,
            agent=agent_id,
            title=title,
            content=content,
            content_type=content_type,
            priority=priority,
            task_id=task_id,
            chat_session_id=session_id,
        )

        # Emit real-time notification to frontend
        if saved and tenant_id:
            await sio.emit("inbox_new_item", {
                "id": saved["id"],
                "agent": agent_id,
                "type": content_type,
                "title": title,
                "status": "ready",
                "priority": priority,
                "created_at": saved.get("created_at", ""),
            }, room=tenant_id)

        # Update the task status to done
        if task_id:
            try:
                from backend.config.loader import _get_supabase
                sb = _get_supabase()
                sb.table("tasks").update({
                    "status": "done",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }).eq("id", task_id).execute()
            except Exception:
                pass

    except Exception as e:
        logging.getLogger("aria.inbox").error("Agent %s failed for tenant %s: %s", agent_id, tenant_id, e)


# ─── CEO Chat ───
import pathlib as _pathlib

_AGENTS_DIR = _pathlib.Path(__file__).resolve().parent.parent / "docs" / "agents"
_CEO_MD = (_AGENTS_DIR / "ceo.md").read_text(encoding="utf-8")
_AGENT_MDS = {}
for _f in _AGENTS_DIR.glob("*.md"):
    _AGENT_MDS[_f.stem] = _f.read_text(encoding="utf-8")
# Load skill files
_SKILLS_DIR = _AGENTS_DIR / "skills"
if _SKILLS_DIR.exists():
    for _f in _SKILLS_DIR.glob("*.md"):
        _AGENT_MDS[f"skill_{_f.stem}"] = _f.read_text(encoding="utf-8")

# In-memory chat cache (loaded from DB on first access)
_chat_sessions: dict[str, list[dict]] = {}


def _save_chat_message(session_id: str, tenant_id: str, role: str, content: str, delegations: list | None = None):
    """Persist a single chat message to Supabase."""
    try:
        from backend.config.loader import _get_supabase
        sb = _get_supabase()
        # Ensure session row exists
        sb.table("chat_sessions").upsert({
            "id": session_id,
            "tenant_id": tenant_id or None,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, on_conflict="id").execute()
        # Insert message
        sb.table("chat_messages").insert({
            "session_id": session_id,
            "role": role,
            "content": content,
            "delegations": delegations or [],
        }).execute()
    except Exception:
        pass


def _auto_title(session_id: str, first_message: str):
    """Set the chat title from the user's first message."""
    title = first_message[:80].split("\n")[0]
    if len(first_message) > 80:
        title = title.rsplit(" ", 1)[0] + "..."
    try:
        from backend.config.loader import _get_supabase
        sb = _get_supabase()
        sb.table("chat_sessions").update({"title": title}).eq("id", session_id).execute()
    except Exception:
        pass


class CEOChatMessage(BaseModel):
    session_id: str
    message: str
    tenant_id: str = ""


@app.post("/api/ceo/chat")
async def ceo_chat(body: CEOChatMessage):
    """Send a message to the CEO agent. The CEO reads its own .md file and all sub-agent .md files,
    then responds and may delegate tasks to sub-agents."""
    from backend.tools.claude_cli import call_claude
    import json as _json

    session = _chat_sessions.setdefault(body.session_id, [])
    is_first_message = len(session) == 0
    session.append({"role": "user", "content": body.message})

    # Persist user message to DB
    tenant_id = body.tenant_id
    _save_chat_message(body.session_id, tenant_id, "user", body.message)
    if is_first_message:
        _auto_title(body.session_id, body.message)

    # Build system prompt from agent .md files only (not skill files — too large for chat context)
    sub_agent_context = "\n\n".join(
        f"--- {name}.md ---\n{content}"
        for name, content in _AGENT_MDS.items()
        if name != "ceo" and not name.startswith("skill_")
    )

    # Load tenant config (onboarding data) if available
    business_context = ""
    tenant_id = body.tenant_id
    if tenant_id:
        try:
            tc = get_tenant_config(tenant_id)
            business_context = f"""
## Business Context (from onboarding)
- **Business:** {tc.business_name}
- **Industry:** {tc.industry}
- **Description:** {tc.description}
- **Product:** {tc.product.name} — {tc.product.description}
- **Value Props:** {', '.join(tc.product.value_props) if tc.product.value_props else 'N/A'}
- **Competitors:** {', '.join(tc.product.competitors) if tc.product.competitors else 'N/A'}
- **Differentiators:** {', '.join(tc.product.differentiators) if tc.product.differentiators else 'N/A'}
- **Target Audience:** {', '.join(tc.icp.target_titles) if tc.icp.target_titles else 'N/A'}
- **Target Industries:** {', '.join(tc.icp.target_industries) if tc.icp.target_industries else 'N/A'}
- **Pain Points:** {', '.join(tc.icp.pain_points) if tc.icp.pain_points else 'N/A'}
- **Online Hangouts:** {', '.join(tc.icp.online_hangouts) if tc.icp.online_hangouts else 'N/A'}
- **Positioning:** {tc.gtm_playbook.positioning}
- **Messaging Pillars:** {', '.join(tc.gtm_playbook.messaging_pillars) if tc.gtm_playbook.messaging_pillars else 'N/A'}
- **Channel Strategy:** {', '.join(tc.gtm_playbook.channel_strategy) if tc.gtm_playbook.channel_strategy else 'N/A'}
- **30-Day Plan:** {tc.gtm_playbook.action_plan_30}
- **60-Day Plan:** {tc.gtm_playbook.action_plan_60}
- **90-Day Plan:** {tc.gtm_playbook.action_plan_90}
- **KPIs:** {', '.join(tc.gtm_playbook.kpis) if tc.gtm_playbook.kpis else 'N/A'}
- **Brand Voice:** {tc.brand_voice.tone}
- **Active Channels:** {', '.join(tc.channels) if tc.channels else 'N/A'}
- **Active Agents:** {', '.join(tc.active_agents) if tc.active_agents else 'N/A'}
- **Plan:** {tc.plan}
"""
        except Exception:
            pass

    system_prompt = f"""{_CEO_MD}
{business_context}
## Sub-Agent Documentation
{sub_agent_context}

## Instructions
You are chatting with a developer founder who needs marketing help.
You already know their business from the onboarding data above — use it to give specific, personalized advice.
Based on the conversation, you should:
1. Answer their question or provide strategic guidance tailored to their product and audience
2. If the task should be delegated, include a JSON block at the END of your response:
   ```delegate
   {{"agent": "content_writer|email_marketer|social_manager|ad_strategist", "task": "description of what to do", "priority": "low|medium|high", "status": "backlog|to_do|in_progress|done"}}
   ```
   Choose the status based on urgency and context:
   - "backlog" — nice-to-have, no immediate action needed
   - "to_do" — should be done soon, queued for the agent
   - "in_progress" — starting immediately
   - "done" — already completed in this response
3. You can delegate to multiple agents by including multiple delegate blocks
4. If no delegation is needed, just respond normally

Keep responses concise and actionable. You are their Chief Marketing Strategist."""

    # Build conversation for Claude
    conversation = "\n".join(
        f"{'User' if m['role'] == 'user' else 'CEO Agent'}: {m['content']}"
        for m in session[-20:]  # last 20 messages for context
    )

    try:
        raw = await call_claude(system_prompt, conversation, tenant_id=tenant_id or "global")
    except Exception as exc:
        import traceback
        logger = logging.getLogger("aria.ceo_chat")
        logger.error(f"CEO chat error: {exc}\n{traceback.format_exc()}")
        raw = f"I encountered an error: {str(exc)[:200]}. Please try again."

    # Parse delegation blocks
    delegations = []
    clean_response = raw
    if "```delegate" in raw:
        import re
        blocks = re.findall(r"```delegate\s*\n(.*?)\n```", raw, re.DOTALL)
        for block in blocks:
            try:
                d = _json.loads(block.strip())
                if d.get("agent") in ("content_writer", "email_marketer", "social_manager", "ad_strategist"):
                    delegations.append(d)
            except _json.JSONDecodeError:
                pass
        clean_response = re.sub(r"```delegate\s*\n.*?\n```", "", raw, flags=re.DOTALL).strip()

    session.append({"role": "assistant", "content": clean_response})

    # Persist assistant message to DB
    _save_chat_message(body.session_id, tenant_id, "assistant", clean_response, delegations)

    # Save delegations as tasks, emit status events, and execute in background
    saved_tasks = []
    for d in delegations:
        agent_id = d["agent"]
        task_desc = d.get("task", "")

        # Save to Supabase tasks table
        if tenant_id:
            try:
                from backend.config.loader import _get_supabase
                sb = _get_supabase()
                task_row = {
                    "tenant_id": tenant_id,
                    "agent": agent_id,
                    "task": task_desc,
                    "priority": d.get("priority", "medium"),
                    "status": d.get("status", "to_do"),
                }
                result = sb.table("tasks").insert(task_row).execute()
                if result.data:
                    saved_tasks.append(result.data[0])
            except Exception:
                pass

        # Emit agent_status_change: CEO walks to meeting room, then agent does
        if tenant_id:
            now_ts = datetime.now(timezone.utc).isoformat()
            # CEO starts moving to meeting room
            await sio.emit("agent_status_change", {
                "agent_id": "ceo",
                "status": "busy",
                "current_task": f"Briefing {agent_id} on: {task_desc[:60]}",
                "action": "walk_to_meeting",
                "last_updated": now_ts,
            }, room=tenant_id)
            # Target agent starts moving to meeting room
            await sio.emit("agent_status_change", {
                "agent_id": agent_id,
                "status": "running",
                "current_task": task_desc,
                "action": "walk_to_meeting",
                "last_updated": now_ts,
            }, room=tenant_id)
            # After a delay, agents return to desks and start working
            async def _return_to_desk(aid: str, tid: str, task: str):
                import asyncio
                await asyncio.sleep(8)  # 8 seconds in meeting
                await sio.emit("agent_status_change", {
                    "agent_id": "ceo",
                    "status": "idle",
                    "current_task": "",
                    "action": "return_to_desk",
                    "last_updated": datetime.now(timezone.utc).isoformat(),
                }, room=tid)
                await sio.emit("agent_status_change", {
                    "agent_id": aid,
                    "status": "running",
                    "current_task": task,
                    "action": "return_and_work",
                    "last_updated": datetime.now(timezone.utc).isoformat(),
                }, room=tid)
                # After working for 20 seconds, go idle
                await asyncio.sleep(20)
                await sio.emit("agent_status_change", {
                    "agent_id": aid,
                    "status": "idle",
                    "current_task": "",
                    "action": "task_complete",
                    "last_updated": datetime.now(timezone.utc).isoformat(),
                }, room=tid)
            import asyncio
            asyncio.create_task(_return_to_desk(agent_id, tenant_id, task_desc))

        # Execute agent in background and save output to inbox
        try:
            from backend.agents import AGENT_REGISTRY
            agent_module = AGENT_REGISTRY.get(agent_id)
            if agent_module:
                import asyncio as _aio
                _aio.create_task(_run_agent_to_inbox(
                    agent_module, agent_id, tenant_id or "demo", task_desc,
                    body.session_id,
                    saved_tasks[-1]["id"] if saved_tasks else None,
                    d.get("priority", "medium"),
                ))
        except Exception:
            pass

    return {
        "response": clean_response,
        "delegations": delegations,
        "tasks": saved_tasks,
        "session_id": body.session_id,
    }


@app.get("/api/ceo/chat/{session_id}/history")
async def ceo_chat_history(session_id: str):
    """Get chat history for a session — loads from DB."""
    # Check in-memory cache first
    if session_id in _chat_sessions and _chat_sessions[session_id]:
        return {"session_id": session_id, "messages": _chat_sessions[session_id]}
    # Load from DB
    try:
        from backend.config.loader import _get_supabase
        sb = _get_supabase()
        result = sb.table("chat_messages").select("role,content,delegations").eq("session_id", session_id).order("created_at").execute()
        messages = [{"role": r["role"], "content": r["content"], "delegations": r.get("delegations", [])} for r in result.data]
        if messages:
            _chat_sessions[session_id] = messages
        return {"session_id": session_id, "messages": messages}
    except Exception:
        return {"session_id": session_id, "messages": []}


@app.get("/api/ceo/chat/sessions/{tenant_id}")
async def list_chat_sessions(tenant_id: str):
    """List all chat sessions for a tenant, newest first."""
    try:
        from backend.config.loader import _get_supabase
        sb = _get_supabase()
        result = sb.table("chat_sessions").select("id,title,created_at,updated_at").eq("tenant_id", tenant_id).order("updated_at", desc=True).execute()
        return {"sessions": result.data}
    except Exception:
        return {"sessions": []}


# ─── Project Tasks API ───
@app.get("/api/tasks/{tenant_id}")
async def list_tasks(tenant_id: str):
    """List all tasks for a tenant, ordered by creation date."""
    try:
        from backend.config.loader import _get_supabase
        sb = _get_supabase()
        result = sb.table("tasks").select("*").eq("tenant_id", tenant_id).order("created_at", desc=True).execute()
        return {"tasks": result.data}
    except Exception as e:
        return {"tasks": [], "error": str(e)}


class TaskUpdate(BaseModel):
    status: str | None = None
    priority: str | None = None


@app.patch("/api/tasks/{task_id}")
async def update_task(task_id: str, body: TaskUpdate):
    """Update a task's status or priority."""
    from backend.config.loader import _get_supabase
    sb = _get_supabase()
    updates = {}
    if body.status:
        updates["status"] = body.status
    if body.priority:
        updates["priority"] = body.priority
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    sb.table("tasks").update(updates).eq("id", task_id).execute()
    return {"ok": True}


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str):
    """Delete a task."""
    from backend.config.loader import _get_supabase
    sb = _get_supabase()
    sb.table("tasks").delete().eq("id", task_id).execute()
    return {"ok": True}


# ─── WebSocket for real-time chat ───
@app.websocket("/ws/chat/{tenant_id}")
async def websocket_chat(websocket: WebSocket, tenant_id: str):
    await websocket.accept()
    await sio.enter_room(websocket.client, tenant_id)
    try:
        while True:
            data = await websocket.receive_text()
            await websocket.send_json({"type": "message", "content": f"Received: {data}"})
    except WebSocketDisconnect:
        pass


# ─── API Usage tracking endpoint ───
@app.get("/api/usage")
async def api_usage(tenant_id: str = "global"):
    """Return current API usage stats (tokens, requests) for a tenant."""
    from backend.tools.claude_cli import get_usage
    return get_usage(tenant_id)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.server:socket_app", host="0.0.0.0", port=8000, reload=True)
