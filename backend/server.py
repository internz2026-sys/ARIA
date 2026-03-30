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

logger = logging.getLogger("aria.server")

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

# In-memory live status store: tenant_id → agent_id → status payload
_live_agent_status: dict[str, dict[str, dict]] = {}


async def _emit_agent_status(tenant_id: str, agent_id: str, status: str,
                              current_task: str = "", **extra):
    """Update in-memory status store AND emit Socket.IO event."""
    now_ts = datetime.now(timezone.utc).isoformat()
    payload = {
        "agent_id": agent_id,
        "status": status,
        "current_task": current_task,
        "last_updated": now_ts,
        **extra,
    }
    _live_agent_status.setdefault(tenant_id, {})[agent_id] = payload
    await sio.emit("agent_status_change", payload, room=tenant_id)


# ─── Socket.IO Events ───
@sio.event
async def connect(sid, environ):
    pass


@sio.event
async def join_tenant(sid, data):
    tenant_id = data.get("tenant_id", "")
    if tenant_id:
        await sio.enter_room(sid, tenant_id)


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
        "validated_fields": sorted(agent.validated_fields),
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
    try:
        config_data = await agent.extract_config()
    except Exception as e:
        logger.error("extract_config failed: %s", e)
        # Return the fallback config so the frontend still works
        config_data = agent._fallback_config_from_messages()
    return {"config": config_data}


class SaveConfig(BaseModel):
    session_id: str
    owner_email: str
    owner_name: str
    active_agents: list[str] | None = None
    existing_tenant_id: str | None = None  # If set, overwrite this tenant


@app.post("/api/onboarding/save-config")
async def save_config(body: SaveConfig):
    from backend.config.brief import generate_agent_brief

    agent = onboarding_sessions.get(body.session_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Session not found")
    tenant_id = body.existing_tenant_id or str(uuid.uuid4())
    config = await agent.build_tenant_config(tenant_id, body.owner_email, body.owner_name, body.active_agents)

    # Generate condensed brief — all agents use this instead of full context
    try:
        config.agent_brief = await generate_agent_brief(config)
    except Exception as e:
        logger.warning("Brief generation failed (will use full context): %s", e)

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
    existing_tenant_id: str | None = None  # If set, overwrite this tenant


@app.post("/api/onboarding/save-config-direct")
async def save_config_direct(body: SaveConfigDirect):
    from backend.config.tenant_schema import (
        TenantConfig, ICPConfig, ProductConfig, GTMPlaybook, BrandVoice, GTMProfile,
    )
    from backend.config.brief import generate_agent_brief

    extracted = body.config
    has_skips = bool(body.skipped_topics)
    tenant_id = body.existing_tenant_id or str(uuid.uuid4())

    # Build GTMProfile from the flat gtm_profile extraction.
    gp_raw = extracted.get("gtm_profile", {})
    gtm_profile = GTMProfile(
        business_name=gp_raw.get("business_name", extracted.get("business_name", "")),
        offer=gp_raw.get("offer", extracted.get("description", "")),
        audience=gp_raw.get("audience", ""),
        problem=gp_raw.get("problem", ""),
        differentiator=gp_raw.get("differentiator", ""),
        positioning_summary=gp_raw.get("positioning_summary", ""),
        primary_channels=gp_raw.get("primary_channels", extracted.get("channels", [])),
        brand_voice=gp_raw.get("brand_voice", extracted.get("brand_voice", {}).get("tone", "")),
        goal_30_days=gp_raw.get("goal_30_days", ""),
        thirty_day_gtm_focus=gp_raw.get("30_day_gtm_focus", ""),
    )

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
        gtm_profile=gtm_profile,
        owner_email=body.owner_email,
        owner_name=body.owner_name,
        plan="starter",
        onboarding_status="completed" if not has_skips else "in_progress",
        skipped_fields=body.skipped_topics or [],
    )

    # Generate condensed brief — all agents use this instead of full context
    try:
        config.agent_brief = await generate_agent_brief(config)
    except Exception as e:
        logger.warning("Brief generation failed (will use full context): %s", e)

    save_tenant_config(config)
    return {"tenant_id": tenant_id, "config": config.model_dump(mode="json")}


# ─── Re-onboarding / Edit Mode ───

@app.get("/api/tenant/{tenant_id}/onboarding-data")
async def get_onboarding_data(tenant_id: str):
    """Return existing onboarding answers mapped to the 8 onboarding fields."""
    try:
        config = get_tenant_config(tenant_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Tenant not found")
    return {
        "business_name": config.business_name,
        "offer": config.product.description or config.description or "",
        "target_audience": ", ".join(config.icp.target_titles) if config.icp.target_titles else "",
        "problem_solved": ", ".join(config.icp.pain_points) if config.icp.pain_points else "",
        "differentiator": ", ".join(config.product.differentiators) if config.product.differentiators else "",
        "channels": config.channels or [],
        "brand_voice": config.brand_voice.tone or "",
        "thirty_day_goal": config.gtm_playbook.action_plan_30 or "",
        "product_name": config.product.name or "",
        "industry": config.industry or "technology",
        "active_agents": config.active_agents or [],
        "onboarding_status": config.onboarding_status,
    }


class UpdateOnboarding(BaseModel):
    """Partial update of onboarding fields."""
    business_name: str | None = None
    offer: str | None = None
    target_audience: str | None = None
    problem_solved: str | None = None
    differentiator: str | None = None
    channels: list[str] | None = None
    brand_voice: str | None = None
    thirty_day_goal: str | None = None


@app.post("/api/tenant/{tenant_id}/update-onboarding")
async def update_onboarding(tenant_id: str, body: UpdateOnboarding):
    """Update specific onboarding fields on an existing tenant, then regenerate brief."""
    from backend.config.brief import generate_agent_brief

    try:
        config = get_tenant_config(tenant_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Tenant not found")

    # Apply updates only for provided fields
    if body.business_name is not None:
        config.business_name = body.business_name
    if body.offer is not None:
        config.product.description = body.offer
        config.description = body.offer
    if body.target_audience is not None:
        config.icp.target_titles = [t.strip() for t in body.target_audience.split(",") if t.strip()]
    if body.problem_solved is not None:
        config.icp.pain_points = [p.strip() for p in body.problem_solved.split(",") if p.strip()]
    if body.differentiator is not None:
        config.product.differentiators = [d.strip() for d in body.differentiator.split(",") if d.strip()]
    if body.channels is not None:
        config.channels = body.channels
    if body.brand_voice is not None:
        config.brand_voice.tone = body.brand_voice
    if body.thirty_day_goal is not None:
        config.gtm_playbook.action_plan_30 = body.thirty_day_goal

    config.onboarding_status = "completed"
    config.skipped_fields = []

    # Regenerate brief with updated data
    try:
        config.agent_brief = await generate_agent_brief(config)
    except Exception as e:
        logger.warning("Brief regeneration failed: %s", e)

    save_tenant_config(config)
    return {"ok": True, "tenant_id": str(config.tenant_id)}


# ─── Agent Brief (re)generation ───

@app.post("/api/tenants/{tenant_id}/regenerate-brief")
async def regenerate_brief(tenant_id: str):
    """Regenerate the condensed agent brief for an existing tenant.

    Call this after the user updates their business info in settings,
    or to backfill briefs for tenants who onboarded before this feature.
    """
    from backend.config.brief import generate_agent_brief

    config = get_tenant_config(tenant_id)
    config.agent_brief = await generate_agent_brief(config)
    save_tenant_config(config)
    return {"agent_brief": config.agent_brief}


# ─── Google OAuth Token Storage ───
class GoogleTokens(BaseModel):
    google_access_token: str
    google_refresh_token: str | None = None


@app.post("/api/integrations/{tenant_id}/google-tokens")
async def save_google_tokens(tenant_id: str, body: GoogleTokens):
    """Store Google OAuth tokens for Gmail sending."""
    try:
        config = get_tenant_config(tenant_id)
        config.integrations.google_access_token = body.google_access_token
        if body.google_refresh_token:
            config.integrations.google_refresh_token = body.google_refresh_token
        save_tenant_config(config)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/integrations/{tenant_id}/gmail-status")
async def gmail_status(tenant_id: str):
    """Check if Gmail is connected for a tenant.

    Connected = has a valid access_token OR a refresh_token that can mint one.
    """
    try:
        config = get_tenant_config(tenant_id)
        has_access = bool(config.integrations.google_access_token)
        has_refresh = bool(config.integrations.google_refresh_token)

        # If we have a refresh token but no access token, try to refresh now
        if not has_access and has_refresh:
            try:
                from backend.tools import gmail_tool
                new_token = await gmail_tool.refresh_access_token(config.integrations.google_refresh_token)
                config.integrations.google_access_token = new_token
                save_tenant_config(config)
                has_access = True
            except Exception:
                pass  # Refresh failed — still report based on what we have

        connected = has_access or has_refresh
        return {"connected": connected, "email": config.owner_email if connected else None}
    except Exception:
        return {"connected": False, "email": None}


# ─── Gmail Send API ───
class GmailSendRequest(BaseModel):
    to: str
    subject: str
    html_body: str


@app.post("/api/email/{tenant_id}/send")
async def send_gmail_email(tenant_id: str, body: GmailSendRequest):
    """Send an email via the user's authenticated Gmail account."""
    from backend.tools import gmail_tool

    config = get_tenant_config(tenant_id)
    access_token = config.integrations.google_access_token
    refresh_token = config.integrations.google_refresh_token

    # Proactively refresh if we have a refresh token but no access token
    if not access_token and refresh_token:
        try:
            access_token = await gmail_tool.refresh_access_token(refresh_token)
            config.integrations.google_access_token = access_token
            save_tenant_config(config)
        except Exception:
            raise HTTPException(status_code=400, detail="Gmail not connected. Please log in with Google to grant email access.")

    if not access_token:
        raise HTTPException(status_code=400, detail="Gmail not connected. Please log in with Google to grant email access.")

    result = await gmail_tool.send_email(
        access_token=access_token,
        to=body.to,
        subject=body.subject,
        html_body=body.html_body,
        from_email=config.owner_email,
    )

    # Token expired — refresh and retry
    if result.get("error") == "token_expired" and refresh_token:
        try:
            new_token = await gmail_tool.refresh_access_token(refresh_token)
            config.integrations.google_access_token = new_token
            save_tenant_config(config)
            result = await gmail_tool.send_email(
                access_token=new_token,
                to=body.to,
                subject=body.subject,
                html_body=body.html_body,
                from_email=config.owner_email,
            )
        except Exception as e:
            config.integrations.google_access_token = None
            if getattr(e, "is_revoked", False):
                config.integrations.google_refresh_token = None
            save_tenant_config(config)
            raise HTTPException(status_code=401, detail="Gmail token expired. Please log in again to reconnect.")

    if result.get("error"):
        detail = result.get("detail", "Gmail API error")
        raise HTTPException(status_code=result.get("status_code", 401), detail=detail)

    return {"status": "sent", "message_id": result.get("message_id", "")}


# ─── Email Draft Approval ───
class EmailApproveRequest(BaseModel):
    inbox_item_id: str


@app.post("/api/email/{tenant_id}/approve-send")
async def approve_and_send_email(tenant_id: str, body: EmailApproveRequest):
    """Approve a pending email draft and send it via Gmail.

    Only sends drafts in 'draft_pending_approval' status.
    Updates the inbox item status through the lifecycle:
    draft_pending_approval → sending → sent / failed.
    """
    from backend.config.loader import _get_supabase
    from backend.tools import gmail_tool

    sb = _get_supabase()

    # Fetch the inbox item
    item_result = sb.table("inbox_items").select("*").eq("id", body.inbox_item_id).single().execute()
    item = item_result.data
    if not item:
        raise HTTPException(status_code=404, detail="Inbox item not found")
    if item.get("status") != "draft_pending_approval":
        raise HTTPException(status_code=400, detail=f"Item is not a pending draft (status: {item.get('status')})")
    if item.get("tenant_id") != tenant_id:
        raise HTTPException(status_code=403, detail="Tenant mismatch")

    # Extract email draft metadata from the item
    meta = item.get("email_draft") or {}
    to = meta.get("to", "")
    subject = meta.get("subject", "")
    html_body = meta.get("html_body", "")

    if not to or not subject or not html_body:
        raise HTTPException(status_code=400, detail="Email draft is missing required fields (to, subject, or body)")

    # Mark as sending
    sb.table("inbox_items").update({
        "status": "sending",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", body.inbox_item_id).execute()

    # Send via Gmail
    config = get_tenant_config(tenant_id)
    access_token = config.integrations.google_access_token
    refresh_token = config.integrations.google_refresh_token

    # Proactively refresh if we have a refresh token but no access token
    if not access_token and refresh_token:
        try:
            from backend.tools import gmail_tool as _gt
            access_token = await _gt.refresh_access_token(refresh_token)
            config.integrations.google_access_token = access_token
            save_tenant_config(config)
        except Exception:
            pass  # Fall through to the not-connected error

    if not access_token:
        sb.table("inbox_items").update({
            "status": "failed",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", body.inbox_item_id).execute()
        raise HTTPException(status_code=400, detail="Gmail not connected. Please log in with Google to grant email access.")

    result = await gmail_tool.send_email(
        access_token=access_token,
        to=to,
        subject=subject,
        html_body=html_body,
        from_email=config.owner_email,
    )

    # Token expired — try refresh
    if result.get("error") == "token_expired" and refresh_token:
        try:
            new_token = await gmail_tool.refresh_access_token(refresh_token)
            config.integrations.google_access_token = new_token
            save_tenant_config(config)
            result = await gmail_tool.send_email(
                access_token=new_token,
                to=to,
                subject=subject,
                html_body=html_body,
                from_email=config.owner_email,
            )
        except Exception as e:
            config.integrations.google_access_token = None
            if getattr(e, "is_revoked", False):
                config.integrations.google_refresh_token = None
            save_tenant_config(config)
            sb.table("inbox_items").update({
                "status": "failed",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", body.inbox_item_id).execute()
            raise HTTPException(status_code=401, detail="Gmail token expired. Please reconnect Gmail in Settings.")

    if result.get("error"):
        sb.table("inbox_items").update({
            "status": "failed",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", body.inbox_item_id).execute()
        raise HTTPException(status_code=500, detail=f"Email send failed: {result['error']}")

    # Mark as sent
    sb.table("inbox_items").update({
        "status": "sent",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", body.inbox_item_id).execute()

    # ── Thread tracking: persist outbound message for future reply matching ──
    gmail_message_id = result.get("message_id", "")
    gmail_thread_id = result.get("thread_id", "")
    thread_db_id = None
    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        # Find or create thread
        if gmail_thread_id:
            existing = sb.table("email_threads").select("id").eq(
                "tenant_id", tenant_id
            ).eq("gmail_thread_id", gmail_thread_id).limit(1).execute()
            if existing.data:
                thread_db_id = existing.data[0]["id"]
                sb.table("email_threads").update({
                    "last_message_at": now_iso,
                    "status": "awaiting_reply",
                    "updated_at": now_iso,
                }).eq("id", thread_db_id).execute()

        if not thread_db_id:
            thread_row = {
                "tenant_id": tenant_id,
                "gmail_thread_id": gmail_thread_id or None,
                "contact_email": to,
                "subject": subject,
                "status": "awaiting_reply",
                "last_message_at": now_iso,
                "inbox_item_id": body.inbox_item_id,
            }
            t_result = sb.table("email_threads").insert(thread_row).execute()
            if t_result.data:
                thread_db_id = t_result.data[0]["id"]

        # Save the outbound message record
        if thread_db_id:
            text_body = meta.get("text_body", "")
            preview = meta.get("preview_snippet", "")
            sb.table("email_messages").insert({
                "thread_id": thread_db_id,
                "tenant_id": tenant_id,
                "gmail_message_id": gmail_message_id or None,
                "direction": "outbound",
                "sender": config.owner_email,
                "recipients": to,
                "subject": subject,
                "text_body": text_body,
                "html_body": html_body,
                "preview_snippet": preview,
                "message_timestamp": now_iso,
                "approval_status": "sent",
            }).execute()
    except Exception as e:
        logger.warning("Thread tracking failed (email still sent): %s", e)

    await sio.emit("inbox_item_updated", {
        "id": body.inbox_item_id,
        "status": "sent",
    }, room=tenant_id)

    return {"status": "sent", "message_id": gmail_message_id, "thread_id": gmail_thread_id}


@app.post("/api/email/{tenant_id}/cancel-draft")
async def cancel_email_draft(tenant_id: str, body: EmailApproveRequest):
    """Cancel a pending email draft."""
    from backend.config.loader import _get_supabase
    sb = _get_supabase()
    sb.table("inbox_items").update({
        "status": "cancelled",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", body.inbox_item_id).eq("tenant_id", tenant_id).execute()
    return {"ok": True}


# ─── Email Threads & Sync ───

@app.get("/api/email/{tenant_id}/threads")
async def list_email_threads(tenant_id: str, status: str = ""):
    """List email conversation threads for a tenant."""
    from backend.config.loader import _get_supabase
    sb = _get_supabase()
    query = sb.table("email_threads").select("*").eq("tenant_id", tenant_id)
    if status:
        query = query.eq("status", status)
    result = query.order("last_message_at", desc=True).execute()
    return {"threads": result.data or []}


@app.get("/api/email/{tenant_id}/threads/{thread_id}")
async def get_email_thread(tenant_id: str, thread_id: str):
    """Get a single thread with all its messages."""
    from backend.config.loader import _get_supabase
    sb = _get_supabase()
    thread_result = sb.table("email_threads").select("*").eq(
        "id", thread_id
    ).eq("tenant_id", tenant_id).single().execute()
    if not thread_result.data:
        raise HTTPException(status_code=404, detail="Thread not found")

    messages_result = sb.table("email_messages").select("*").eq(
        "thread_id", thread_id
    ).order("message_timestamp", desc=False).execute()

    return {
        "thread": thread_result.data,
        "messages": messages_result.data or [],
    }


@app.post("/api/email/{tenant_id}/threads/{thread_id}/mark-read")
async def mark_thread_read(tenant_id: str, thread_id: str):
    """Mark a thread as read (status → open)."""
    from backend.config.loader import _get_supabase
    sb = _get_supabase()
    sb.table("email_threads").update({
        "status": "open",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", thread_id).eq("tenant_id", tenant_id).execute()
    return {"ok": True}


class DraftReplyRequest(BaseModel):
    thread_id: str
    custom_instructions: str = ""


@app.post("/api/email/{tenant_id}/draft-reply")
async def generate_draft_reply(tenant_id: str, body: DraftReplyRequest):
    """Generate a suggested reply draft for an email thread.

    Uses the email marketer agent to draft a contextual reply based on the
    thread history. The draft is saved as draft_pending_approval — never sent.
    """
    from backend.config.loader import _get_supabase
    from backend.tools.claude_cli import call_claude, MODEL_HAIKU

    sb = _get_supabase()

    # Fetch thread and messages
    thread_result = sb.table("email_threads").select("*").eq(
        "id", body.thread_id
    ).eq("tenant_id", tenant_id).single().execute()
    if not thread_result.data:
        raise HTTPException(status_code=404, detail="Thread not found")
    thread = thread_result.data

    messages_result = sb.table("email_messages").select("*").eq(
        "thread_id", body.thread_id
    ).order("message_timestamp", desc=False).execute()
    messages = messages_result.data or []

    if not messages:
        raise HTTPException(status_code=400, detail="No messages in this thread to reply to")

    # Build conversation context
    config = get_tenant_config(tenant_id)
    conversation = ""
    for msg in messages:
        direction = "SENT" if msg["direction"] == "outbound" else "RECEIVED"
        sender = msg.get("sender", "")
        body_text = msg.get("text_body", "") or msg.get("preview_snippet", "")
        conversation += f"\n[{direction}] From: {sender}\nSubject: {msg.get('subject', '')}\n{body_text}\n---\n"

    # Find the latest inbound message to reply to
    latest_inbound = None
    for msg in reversed(messages):
        if msg["direction"] == "inbound":
            latest_inbound = msg
            break
    if not latest_inbound:
        raise HTTPException(status_code=400, detail="No inbound message to reply to")

    instructions = body.custom_instructions or "Write a helpful, professional reply."

    system_prompt = f"""You are the Email Marketer for {config.business_name}.
Brand voice: {config.brand_voice.tone}
Business: {config.description}

Write a reply email based on the conversation thread below.
{instructions}

Output format:
SUBJECT: Re: <original subject>
---
<email body in HTML>

Keep it professional, concise, and on-brand. Do not include placeholder text."""

    user_prompt = f"Thread conversation:\n{conversation}\n\nDraft a reply to the latest inbound message."

    raw = await call_claude(system_prompt, user_prompt, max_tokens=1500, model=MODEL_HAIKU)

    # Parse the draft
    import re as _re
    subject_match = _re.match(r"(?:SUBJECT:\s*)(.+?)(?:\n---\n|\n\n)(.*)", raw, _re.DOTALL | _re.IGNORECASE)
    if subject_match:
        reply_subject = subject_match.group(1).strip()
        reply_body = subject_match.group(2).strip()
    else:
        reply_subject = f"Re: {thread.get('subject', '')}"
        reply_body = raw.strip()

    # Ensure HTML wrapping
    from backend.agents.email_marketer_agent import _wrap_html
    html_body = _wrap_html(reply_body)
    text_body = _re.sub(r'<[^>]+>', '', reply_body).strip()
    preview_snippet = text_body[:200]

    # Save draft message in the thread
    now_iso = datetime.now(timezone.utc).isoformat()
    draft_row = {
        "thread_id": body.thread_id,
        "tenant_id": tenant_id,
        "direction": "outbound",
        "sender": config.owner_email,
        "recipients": thread.get("contact_email", ""),
        "subject": reply_subject,
        "text_body": text_body,
        "html_body": html_body,
        "preview_snippet": preview_snippet,
        "message_timestamp": now_iso,
        "approval_status": "draft_pending_approval",
    }
    msg_result = sb.table("email_messages").insert(draft_row).execute()
    draft_msg = msg_result.data[0] if msg_result.data else {}

    # Also create an inbox item for visibility
    inbox_row = {
        "tenant_id": tenant_id,
        "agent": "email_marketer",
        "type": "email_sequence",
        "title": f"Draft Reply: {reply_subject}",
        "content": preview_snippet,
        "status": "draft_pending_approval",
        "priority": "high",
        "email_draft": {
            "to": thread.get("contact_email", ""),
            "subject": reply_subject,
            "html_body": html_body,
            "text_body": text_body,
            "preview_snippet": preview_snippet,
            "status": "draft_pending_approval",
            "reply_to_thread_id": body.thread_id,
            "reply_to_message_id": draft_msg.get("id", ""),
        },
    }
    inbox_result = sb.table("inbox_items").insert(inbox_row).execute()
    inbox_item = inbox_result.data[0] if inbox_result.data else {}

    # Update thread status
    sb.table("email_threads").update({
        "status": "replied",
        "updated_at": now_iso,
    }).eq("id", body.thread_id).execute()

    return {
        "draft": {
            "message_id": draft_msg.get("id", ""),
            "inbox_item_id": inbox_item.get("id", ""),
            "to": thread.get("contact_email", ""),
            "subject": reply_subject,
            "preview_snippet": preview_snippet,
            "status": "draft_pending_approval",
        },
    }


@app.post("/api/email/{tenant_id}/sync")
async def trigger_email_sync(tenant_id: str):
    """Manually trigger Gmail inbound reply sync for a tenant."""
    from backend.tools.gmail_sync import sync_tenant_replies
    result = await sync_tenant_replies(tenant_id)
    return result


@app.post("/api/email/sync-all")
async def trigger_sync_all():
    """Trigger Gmail sync for all active tenants. Called by cron."""
    from backend.tools.gmail_sync import sync_all_tenants
    results = await sync_all_tenants()
    return {"tenants_synced": len(results), "results": results}


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
    # Agent starts working at desk
    await _emit_agent_status(tenant_id, agent_name, "working",
                             current_task=f"Running {agent_name} task",
                             action="start_work")

    result = await dispatch_agent(tenant_id, agent_name)
    await sio.emit("agent_event", result, room=tenant_id)

    # Agent done — return to idle
    await _emit_agent_status(tenant_id, agent_name, "idle",
                             action="task_complete")

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
    live = _live_agent_status.get(tenant_id, {})

    # Check tasks table for agents with in_progress tasks
    task_statuses: dict[str, str] = {}
    try:
        from backend.config.loader import _get_supabase
        sb = _get_supabase()
        result = sb.table("tasks").select("agent,task").eq(
            "tenant_id", tenant_id
        ).eq("status", "in_progress").execute()
        for t in (result.data or []):
            task_statuses[t["agent"]] = t["task"]
    except Exception:
        pass

    agents = []
    for a in VIRTUAL_OFFICE_AGENTS:
        aid = a["agent_id"]
        live_entry = live.get(aid, {})
        live_status = live_entry.get("status")

        # Priority: active live status (running/working) > task-based > idle
        if live_status and live_status not in ("idle",):
            status = live_status
            current_task = live_entry.get("current_task", "")
            last_updated = live_entry.get("last_updated", now)
        elif aid in task_statuses:
            status = "working"
            current_task = task_statuses[aid]
            last_updated = now
        else:
            status = "idle"
            current_task = ""
            last_updated = now

        agents.append({
            "agent_id": aid,
            "name": a["name"],
            "role": a["role"],
            "model": a["model"],
            "status": status,
            "current_task": current_task,
            "department": a["department"],
            "last_updated": last_updated,
        })
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
    """Return recent activity from inbox items and tasks."""
    from backend.config.loader import _get_supabase
    sb = _get_supabase()
    activity = []
    try:
        # Recent inbox deliverables
        inbox_result = sb.table("inbox_items").select("agent,type,title,created_at").eq(
            "tenant_id", tenant_id
        ).order("created_at", desc=True).limit(20).execute()
        for item in (inbox_result.data or []):
            activity.append({
                "agent": item["agent"],
                "action": f"Delivered: {item['title'][:60]}",
                "type": item["type"],
                "timestamp": item["created_at"],
            })
    except Exception:
        pass
    try:
        # Recent completed tasks
        task_result = sb.table("tasks").select("agent,task,status,created_at").eq(
            "tenant_id", tenant_id
        ).order("created_at", desc=True).limit(20).execute()
        for task in (task_result.data or []):
            status_verb = "Completed" if task["status"] == "done" else "Working on"
            activity.append({
                "agent": task["agent"],
                "action": f"{status_verb}: {task['task'][:60]}",
                "type": "task",
                "timestamp": task["created_at"],
            })
    except Exception:
        pass
    # Sort by timestamp, newest first
    activity.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return {"tenant_id": tenant_id, "activity": activity[:30]}


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
async def list_inbox(tenant_id: str, status: str = "", page: int = 1, page_size: int = 20):
    """List inbox items for a tenant with pagination."""
    try:
        from backend.config.loader import _get_supabase
        sb = _get_supabase()

        # Count query
        count_query = sb.table("inbox_items").select("id", count="exact").eq("tenant_id", tenant_id)
        if status:
            count_query = count_query.eq("status", status)
        count_result = count_query.execute()
        total = count_result.count if count_result.count is not None else len(count_result.data)

        # Paginated data query
        offset = (max(page, 1) - 1) * page_size
        query = sb.table("inbox_items").select("*").eq("tenant_id", tenant_id)
        if status:
            query = query.eq("status", status)
        result = query.order("created_at", desc=True).range(offset, offset + page_size - 1).execute()

        return {
            "items": result.data,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": max(1, -(-total // page_size)),  # ceil division
        }
    except Exception as e:
        return {"items": [], "total": 0, "page": 1, "page_size": page_size, "total_pages": 1, "error": str(e)}


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

    # Also run Gmail inbound reply sync for all connected tenants
    sync_results = []
    try:
        from backend.tools.gmail_sync import sync_all_tenants
        sync_results = await sync_all_tenants()
    except Exception as e:
        logger.warning("Gmail sync during cron failed: %s", e)

    total_imported = sum(r.get("imported", 0) for r in sync_results)
    return {
        "status": "completed",
        "tasks_run": len(results) if results else 0,
        "email_sync": {
            "tenants_synced": len(sync_results),
            "total_imported": total_imported,
        },
    }


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
    status: str = "ready",
    email_draft: dict | None = None,
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
            "status": status,
            "priority": priority,
        }
        if task_id:
            row["task_id"] = task_id
        if chat_session_id:
            row["chat_session_id"] = chat_session_id
        if email_draft:
            row["email_draft"] = email_draft
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
    """Run an agent in background, drive office movement from real execution.

    Lifecycle:
      1. Brief meeting phase (4s) — CEO + agent walk to meeting room
      2. CEO returns to desk (idle), agent returns to desk (working)
      3. Agent executes for real — stays in "working"
      4. Agent stays "working" until task is moved to "done" on Kanban board
         (no auto-idle — task board is the source of truth)
    """
    import asyncio

    try:
        # Phase 1: Meeting (CEO + agent already walking to meeting room via caller)
        await asyncio.sleep(4)

        # Phase 2: CEO returns to desk
        if tenant_id:
            await _emit_agent_status(tenant_id, "ceo", "idle",
                                     action="return_to_desk")
            # Agent returns to desk and starts working
            await _emit_agent_status(tenant_id, agent_id, "working",
                                     current_task=task_desc,
                                     action="return_and_work")

        # Phase 3: Actually run the agent (this is where real time is spent)
        result = await agent_module.run(tenant_id, context={"action": task_desc})
        content = result.get("result", "")

        if not content:
            # No content but task should still be marked done
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
                if tenant_id:
                    await sio.emit("task_updated", {
                        "id": task_id,
                        "agent": agent_id,
                        "status": "done",
                        "task": task_desc,
                    }, room=tenant_id)
                    try:
                        sb2 = _get_supabase()
                        other = sb2.table("tasks").select("id").eq(
                            "tenant_id", tenant_id
                        ).eq("agent", agent_id).eq("status", "in_progress").limit(1).execute()
                        if not other.data:
                            await _emit_agent_status(tenant_id, agent_id, "idle",
                                                     action="all_tasks_complete")
                    except Exception:
                        pass
            return

        content_type = _infer_content_type(agent_id, content)
        title = _extract_title(agent_id, task_desc, content)

        # If the agent returned an email draft, save as pending approval
        email_draft = result.get("email_draft")
        if email_draft:
            item_status = "draft_pending_approval"
            # Use the draft subject as title if available
            if email_draft.get("subject"):
                title = f"Email: {email_draft['subject']}"
            # Use preview snippet for the display content
            content = email_draft.get("preview_snippet", content)
        else:
            item_status = "ready"

        saved = _save_inbox_item(
            tenant_id=tenant_id,
            agent=agent_id,
            title=title,
            content=content,
            content_type=content_type,
            priority=priority,
            task_id=task_id,
            chat_session_id=session_id,
            status=item_status,
            email_draft=email_draft,
        )

        # Emit real-time notification to frontend
        if saved and tenant_id:
            await sio.emit("inbox_new_item", {
                "id": saved["id"],
                "agent": agent_id,
                "type": content_type,
                "title": title,
                "status": item_status,
                "priority": priority,
                "created_at": saved.get("created_at", ""),
            }, room=tenant_id)

        # Mark task as done and notify frontend in real-time
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

            # Emit task_updated so Kanban board auto-refreshes
            if tenant_id:
                await sio.emit("task_updated", {
                    "id": task_id,
                    "agent": agent_id,
                    "status": "done",
                    "task": task_desc,
                }, room=tenant_id)

            # Agent done — return to idle
            if tenant_id:
                try:
                    sb2 = _get_supabase()
                    other = sb2.table("tasks").select("id").eq(
                        "tenant_id", tenant_id
                    ).eq("agent", agent_id).eq("status", "in_progress").limit(1).execute()
                    if not other.data:
                        await _emit_agent_status(tenant_id, agent_id, "idle",
                                                 action="all_tasks_complete")
                except Exception:
                    pass

    except Exception as e:
        logging.getLogger("aria.inbox").error("Agent %s failed for tenant %s: %s", agent_id, tenant_id, e)
        # Save error to inbox so user can see what went wrong
        _save_inbox_item(
            tenant_id=tenant_id,
            agent=agent_id,
            title=f"Failed: {task_desc[:60]}",
            content=f"The {agent_id} agent encountered an error while processing this task:\n\n"
                    f"**Task:** {task_desc}\n\n"
                    f"**Error:** {e}\n\n"
                    "Please try again. If this persists, check Settings > Integrations to ensure Gmail is connected.",
            content_type="error",
            priority=priority,
            task_id=task_id,
            chat_session_id=session_id,
        )
        # Mark task as done so it doesn't stay stuck in_progress
        if task_id:
            try:
                from backend.config.loader import _get_supabase
                sb = _get_supabase()
                sb.table("tasks").update({
                    "status": "done",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }).eq("id", task_id).execute()
                if tenant_id:
                    await sio.emit("task_updated", {
                        "id": task_id, "agent": agent_id,
                        "status": "done", "task": task_desc,
                    }, room=tenant_id)
            except Exception:
                pass
        # Return agent to idle so it doesn't get stuck
        if tenant_id:
            try:
                await _emit_agent_status(tenant_id, agent_id, "idle",
                                         action="task_failed")
            except Exception:
                pass


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

    # CEO is now in a meeting (processing the user's message)
    if tenant_id:
        await _emit_agent_status(tenant_id, "ceo", "running",
                                 current_task="In meeting with user",
                                 action="meeting_with_user")

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

    # Check if Gmail is connected for this tenant
    gmail_note = ""
    if tenant_id:
        try:
            _tc = get_tenant_config(tenant_id)
            _gmail_connected = bool(
                _tc.integrations.google_access_token or _tc.integrations.google_refresh_token
            )
            if _gmail_connected:
                gmail_note = f"""
5. **Gmail is connected** ({_tc.owner_email}). When the user asks you to SEND an email (not just draft),
   delegate to email_marketer with a task that starts with "SEND:" followed by the details including the
   recipient email address. Example: "SEND: Send a welcome email to user@example.com introducing our product"
   The Email Marketer will compose the draft and the user can approve it to send from {_tc.owner_email}.
   IMPORTANT: Always include the recipient's full email address in the task description."""
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
{gmail_note}

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

    # No delegations — CEO meeting is over, return to idle
    if not delegations and tenant_id:
        await _emit_agent_status(tenant_id, "ceo", "idle",
                                 action="chat_response_sent")

    # Save delegations as tasks, emit status events, and execute in background
    saved_tasks = []
    for d in delegations:
        agent_id = d["agent"]
        task_desc = d.get("task", "")

        # Save to Supabase tasks table — always start as in_progress
        if tenant_id:
            try:
                from backend.config.loader import _get_supabase
                sb = _get_supabase()
                task_row = {
                    "tenant_id": tenant_id,
                    "agent": agent_id,
                    "task": task_desc,
                    "priority": d.get("priority", "medium"),
                    "status": "in_progress",
                }
                result = sb.table("tasks").insert(task_row).execute()
                if result.data:
                    saved_tasks.append(result.data[0])
                    # Notify Kanban board of new task
                    await sio.emit("task_updated", {
                        "id": result.data[0]["id"],
                        "agent": agent_id,
                        "status": "in_progress",
                        "task": task_desc,
                    }, room=tenant_id)
            except Exception:
                pass

        # Emit agent_status_change: CEO walks to meeting room, then agent does
        if tenant_id:
            # CEO starts moving to meeting room
            await _emit_agent_status(tenant_id, "ceo", "running",
                                     current_task=f"Briefing {agent_id} on: {task_desc[:60]}",
                                     action="walk_to_meeting")
            # Target agent starts moving to meeting room
            await _emit_agent_status(tenant_id, agent_id, "running",
                                     current_task=task_desc,
                                     action="walk_to_meeting")
        # Execute agent in background — _run_agent_to_inbox handles the full
        # lifecycle: meeting delay → CEO returns → agent works → agent done
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
    """Update a task's status or priority. Syncs agent visual status in Virtual Office."""
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

    # Fetch task details before updating (for status sync)
    task_result = sb.table("tasks").select("agent,tenant_id,task").eq("id", task_id).execute()

    sb.table("tasks").update(updates).eq("id", task_id).execute()

    # Sync agent visual status with task status change
    if body.status and task_result.data:
        task = task_result.data[0]
        agent_id = task["agent"]
        tid = task["tenant_id"]
        if body.status == "in_progress":
            await _emit_agent_status(tid, agent_id, "working",
                                     current_task=task.get("task", ""),
                                     action="task_started")
        elif body.status in ("done", "to_do", "backlog"):
            # Only go idle if agent has no OTHER in_progress tasks
            other = sb.table("tasks").select("id").eq(
                "tenant_id", tid
            ).eq("agent", agent_id).eq("status", "in_progress").neq(
                "id", task_id
            ).limit(1).execute()
            if not other.data:
                await _emit_agent_status(tid, agent_id, "idle",
                                         action="task_status_changed")

    return {"ok": True}


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str):
    """Delete a task. If it was in_progress, sync agent back to idle."""
    from backend.config.loader import _get_supabase
    sb = _get_supabase()

    # Fetch before deleting for status sync
    task_result = sb.table("tasks").select("agent,tenant_id,status").eq("id", task_id).execute()

    sb.table("tasks").delete().eq("id", task_id).execute()

    # If deleted task was in_progress, check if agent has other active tasks
    if task_result.data and task_result.data[0].get("status") == "in_progress":
        task = task_result.data[0]
        agent_id = task["agent"]
        tid = task["tenant_id"]
        other = sb.table("tasks").select("id").eq(
            "tenant_id", tid
        ).eq("agent", agent_id).eq("status", "in_progress").limit(1).execute()
        if not other.data:
            await _emit_agent_status(tid, agent_id, "idle",
                                     action="task_deleted")

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
