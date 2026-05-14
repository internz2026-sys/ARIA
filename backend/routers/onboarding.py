"""Onboarding session lifecycle: start/message/skip/extract-config/save-config,
draft persistence (save-draft/draft GET/DELETE), re-onboarding edit + brief
regeneration, and the JWT-bound tenant-by-email lookup used by the login flow.
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from backend.auth import get_current_user, get_verified_tenant, get_user_id_from_jwt
from backend.config.loader import get_tenant_config, save_tenant_config
from backend.agents.onboarding_agent import OnboardingAgent, FIELD_QUESTIONS
from backend.services.supabase import get_db as _get_supabase

logger = logging.getLogger("aria.server")

router = APIRouter()


# Maps session_id -> (user_id, OnboardingAgent). user_id is the Supabase auth
# `sub` claim from the JWT bound at /start time. Every subsequent endpoint
# (message, skip, extract-config, save-config) verifies the caller's JWT
# user_id matches the bound user_id before touching the agent — anti-replay
# defense per security audit (2026-05-07).
onboarding_sessions: dict[str, tuple[str, OnboardingAgent]] = {}


# ── Pydantic models ──────────────────────────────────────────────────────
class OnboardingMessage(BaseModel):
    session_id: str
    message: str


class OnboardingStart(BaseModel):
    session_id: Optional[str] = None
    # When True, /api/onboarding/start ignores any persisted draft for
    # this user and force-starts a fresh 8-question agent (also wiping
    # the prior onboarding_drafts row). Wired to the welcome page's
    # "Start from scratch" button — without it, /start auto-resumes the
    # completed prior onboarding and the agent reports "complete" on Q1
    # because every field is already validated.
    restart: bool = False


class SaveConfig(BaseModel):
    session_id: str
    owner_email: str
    owner_name: str
    active_agents: list[str] | None = None
    existing_tenant_id: str | None = None  # If set, overwrite this tenant


class SaveConfigDirect(BaseModel):
    """Accept the raw extracted config JSON (cached on the frontend) to save
    directly — no backend session needed.

    NOTE: any client-supplied owner_email is IGNORED — owner_email is derived
    from the JWT email claim. Field kept for backwards-compat with existing
    frontend code that still sends it (and so 4xx-on-extra isn't tripped).
    """
    config: dict
    owner_email: str | None = None  # ignored — derived from JWT
    owner_name: str
    active_agents: list[str] | None = None
    skipped_topics: list[str] | None = None
    existing_tenant_id: str | None = None  # If set, overwrite this tenant


class OnboardingDraftPayload(BaseModel):
    # NOTE: any client-supplied user_id is IGNORED — we always derive
    # user_id from the JWT. Field kept here only for backwards-compat
    # with existing frontend code that still sends it.
    user_id: str | None = None
    session_id: str | None = None
    extracted_config: dict
    skipped_topics: list | None = None
    conversation_history: list | None = None


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


# ── Helpers ──────────────────────────────────────────────────────────────
def _get_session_for_user(session_id: str, user_id: str) -> OnboardingAgent:
    """Look up the agent by session_id and verify the JWT user owns it.

    Raises 404 if the session_id is unknown (in-memory store wiped on
    restart — frontend should call /start to rehydrate from the DB row),
    403 if the JWT user_id doesn't match the bound user.
    """
    entry = onboarding_sessions.get(session_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Session not found")
    bound_user_id, agent = entry
    # Allow dev-mode fallthrough — get_current_user returns "dev-user"
    # when SUPABASE_JWT_SECRET isn't set; the bound user_id will also be
    # "dev-user" in that case, so the equality check still holds.
    if bound_user_id != user_id:
        logger.warning(
            "Onboarding session replay rejected: session=%s bound_to=%s but caller=%s",
            session_id, bound_user_id, user_id,
        )
        raise HTTPException(status_code=403, detail="Session does not belong to this user")
    return agent


def _persist_onboarding_draft(user_id: str, session_id: str, agent: OnboardingAgent) -> None:
    """Best-effort persistence of the agent's state to onboarding_drafts.

    Called on every /message turn. Failures are logged but don't break the
    chat flow — losing a snapshot is preferable to 500ing the user mid-
    conversation. The DB row is keyed by user_id (UNIQUE), so this is an
    UPSERT that always points at the same row per user.
    """
    try:
        sb = _get_supabase()
        row = {
            "user_id": user_id,
            "session_id": session_id,
            "extracted_config": agent._extracted_config or {},
            "skipped_topics": agent.skipped_topics,
            "conversation_history": agent.to_dict(),
        }
        sb.table("onboarding_drafts").upsert(row, on_conflict="user_id").execute()
    except Exception as e:
        logger.warning("Failed to persist onboarding draft for user=%s: %s", user_id, e)


def _load_onboarding_draft(user_id: str) -> dict | None:
    """Fetch the user's persisted onboarding draft row, or None if absent.

    Returns the raw row dict (with conversation_history, session_id,
    extracted_config, skipped_topics). Any error is logged and treated
    as "no draft" so a transient DB blip falls through to fresh-start
    instead of breaking onboarding entirely.
    """
    try:
        sb = _get_supabase()
        result = (
            sb.table("onboarding_drafts")
            .select("user_id,session_id,extracted_config,skipped_topics,conversation_history")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        if result.data:
            row = result.data[0]
            # Defense-in-depth (2026-05-07, HIGH audit fix): the .eq()
            # filter above already restricts the query, but we still
            # explicitly assert the returned row's user_id matches the
            # caller's user_id before handing it back. Catches any
            # future regression where the filter is removed/refactored
            # without updating callers, plus defends against a hostile
            # row injected via a different code path.
            row_user_id = row.get("user_id")
            if row_user_id and str(row_user_id) != str(user_id):
                logger.error(
                    "Onboarding draft load rejected: row user_id=%s != caller=%s",
                    row_user_id, user_id,
                )
                raise HTTPException(
                    status_code=403,
                    detail="Draft does not belong to this user",
                )
            return row
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("Failed to load onboarding draft for user=%s: %s", user_id, e)
    return None


def _delete_onboarding_draft(user_id: str) -> None:
    """Best-effort delete of the user's draft row after save-config."""
    try:
        sb = _get_supabase()
        sb.table("onboarding_drafts").delete().eq("user_id", user_id).execute()
    except Exception as e:
        logger.warning("Failed to delete onboarding draft for user=%s: %s", user_id, e)


def _last_assistant_message(agent: OnboardingAgent) -> str:
    """Return the last 'assistant' role message from the agent's history,
    or fall back to the next-question prompt if the history is empty."""
    for msg in reversed(agent.messages):
        if msg.get("role") == "assistant":
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                return content
    # Fallback: ask the current pending question.
    cur = agent.current_field
    if cur:
        return FIELD_QUESTIONS[cur]
    return agent._build_final_summary()


def _apply_onboarding_edit(config, field: str, value) -> None:
    """Apply a single onboarding field edit to BOTH the legacy nested config
    shape AND the flat gtm_profile mirror in one place.

    Why two writes per field: several sub-agents read from the legacy nested
    fields (config.icp.target_titles, config.product.differentiators, etc.)
    while CEO chat context and the agent brief read from config.gtm_profile.
    Keeping the two in sync here means edits always propagate to every agent.
    """
    gp = config.gtm_profile
    if field == "business_name":
        config.business_name = value
        gp.business_name = value
    elif field == "offer":
        config.product.description = value
        config.description = value
        gp.offer = value
    elif field == "target_audience":
        config.icp.target_titles = [t.strip() for t in value.split(",") if t.strip()]
        gp.audience = value
    elif field == "problem_solved":
        config.icp.pain_points = [p.strip() for p in value.split(",") if p.strip()]
        gp.problem = value
    elif field == "differentiator":
        config.product.differentiators = [d.strip() for d in value.split(",") if d.strip()]
        gp.differentiator = value
    elif field == "channels":
        config.channels = value
        gp.primary_channels = value
    elif field == "brand_voice":
        config.brand_voice.tone = value
        gp.brand_voice = value
    elif field == "thirty_day_goal":
        config.gtm_playbook.action_plan_30 = value
        gp.goal_30_days = value


_ONBOARDING_EDITABLE_FIELDS = (
    "business_name", "offer", "target_audience", "problem_solved",
    "differentiator", "channels", "brand_voice", "thirty_day_goal",
)


# ── Routes ───────────────────────────────────────────────────────────────
@router.get("/api/tenant/by-email/{email}")
async def tenant_by_email(
    email: str,
    user: dict = Depends(get_current_user),
):
    """Look up a tenant config by owner email. Returns only the tenant_id.

    Caller must be authenticated AND the requested email must match the
    JWT's email claim (case-insensitive). This used to be public, which
    made tenant_id enumeration trivial — an attacker could submit any
    address and learn whether it was registered + its tenant UUID.

    Use case is the login flow: the frontend has just resolved a Supabase
    session for email X, then calls this endpoint to fetch the tenant_id
    for that same email. The JWT-email match is exactly the invariant we
    want; nobody legitimately needs to query for somebody else's tenant.
    """
    user_email = (
        user.get("email")
        or user.get("user_metadata", {}).get("email")
        or ""
    ).lower().strip()
    requested = (email or "").lower().strip()

    # Dev-mode fallthrough: get_current_user returns email="dev@localhost"
    # when JWT secret isn't configured. Allow the dev user to look up any
    # email so local testing stays usable; production always has a secret
    # set so this branch can't fire there.
    if user.get("sub") != "dev-user" and user_email != requested:
        logger.warning(
            "tenant_by_email rejected: caller=%s requested=%s (mismatch)",
            user_email or "<no-email>", requested,
        )
        raise HTTPException(status_code=403, detail="Access denied")

    try:
        sb = _get_supabase()
        result = sb.table("tenant_configs").select("tenant_id").eq("owner_email", email).limit(1).execute()
        if result.data and len(result.data) > 0:
            return {"tenant_id": result.data[0]["tenant_id"]}
        return {"tenant_id": None}
    except Exception:
        return {"tenant_id": None}


@router.post("/api/onboarding/start")
async def start_onboarding(body: OnboardingStart, user_id: str = Depends(get_user_id_from_jwt)):
    # Restart path: the user explicitly asked to redo onboarding from
    # scratch. Wipe the prior draft row so the resume block below
    # finds nothing and falls through to a fresh OnboardingAgent. The
    # localStorage clear on the frontend isn't enough on its own
    # because /start re-reads the Postgres draft keyed by user_id, not
    # by the client's session_id.
    if body.restart:
        try:
            sb = _get_supabase()
            sb.table("onboarding_drafts").delete().eq("user_id", user_id).execute()
            logger.info("Onboarding restart for user=%s: cleared prior draft", user_id)
        except Exception as e:
            logger.warning("Onboarding restart: failed to clear prior draft for user=%s: %s", user_id, e)

    # Try to resume from a persisted draft first.
    # Note: only the new from_dict snapshot shape (a dict with messages/
    # field_state keys, written by this server's _persist_onboarding_draft)
    # supports resume. Legacy drafts written by /api/onboarding/save-draft
    # store conversation_history as a list of chat messages — those don't
    # carry enough state to rehydrate, so we let those users start fresh.
    draft = None if body.restart else _load_onboarding_draft(user_id)
    history = (draft or {}).get("conversation_history")
    if isinstance(history, dict) and history.get("messages"):
        try:
            agent = OnboardingAgent.from_dict(history)
            # Reuse stored session_id if present, else mint a new one.
            session_id = draft.get("session_id") or str(uuid.uuid4())
            onboarding_sessions[session_id] = (user_id, agent)
            resumed_message = _last_assistant_message(agent)
            return {
                "session_id": session_id,
                "message": resumed_message,
                "is_resumed": True,
                "is_complete": agent.is_complete(),
                "questions_answered": agent.questions_answered,
                "validated_fields": sorted(agent.validated_fields),
                "skipped_topics": agent.skipped_topics,
            }
        except Exception as e:
            logger.warning(
                "Failed to rehydrate onboarding session for user=%s: %s — starting fresh",
                user_id, e,
            )

    # Fresh start.
    session_id = body.session_id or str(uuid.uuid4())
    agent = OnboardingAgent()
    greeting = agent.start_conversation()
    onboarding_sessions[session_id] = (user_id, agent)
    # Persist the empty draft so /draft GET / future /start calls see it.
    _persist_onboarding_draft(user_id, session_id, agent)
    return {
        "session_id": session_id,
        "message": greeting,
        "is_resumed": False,
        "is_complete": False,
        "questions_answered": 0,
        "validated_fields": [],
        "skipped_topics": [],
    }


@router.post("/api/onboarding/message")
async def onboarding_message(body: OnboardingMessage, user_id: str = Depends(get_user_id_from_jwt)):
    agent = _get_session_for_user(body.session_id, user_id)
    response = await agent.process_message(body.message)
    # Persist after every turn so progress survives restarts/tab closes.
    _persist_onboarding_draft(user_id, body.session_id, agent)
    return {
        "message": response,
        "is_complete": agent.is_complete(),
        "questions_answered": agent.questions_answered,
        "validated_fields": sorted(agent.validated_fields),
    }


@router.post("/api/onboarding/skip")
async def onboarding_skip(body: OnboardingStart, user_id: str = Depends(get_user_id_from_jwt)):
    if not body.session_id:
        raise HTTPException(status_code=400, detail="session_id required")
    agent = _get_session_for_user(body.session_id, user_id)
    skipped = agent.skip_current_topic()
    current = agent.get_current_topic()
    # Persist skip immediately.
    _persist_onboarding_draft(user_id, body.session_id, agent)
    return {
        "skipped_topic": skipped,
        "current_topic": current,
        "questions_answered": agent.questions_answered,
        "is_complete": agent.is_complete(),
        "skipped_topics": agent.skipped_topics,
    }


@router.post("/api/onboarding/extract-config")
async def extract_config(body: OnboardingStart, user_id: str = Depends(get_user_id_from_jwt)):
    if not body.session_id:
        raise HTTPException(status_code=400, detail="session_id required")
    agent = _get_session_for_user(body.session_id, user_id)
    try:
        config_data = await agent.extract_config()
    except Exception as e:
        logger.error("extract_config failed: %s", e)
        # Return the fallback config so the frontend still works
        config_data = agent._fallback_config_from_messages()
    # Persist the fresh extracted_config snapshot too.
    _persist_onboarding_draft(user_id, body.session_id, agent)
    return {"config": config_data}


@router.post("/api/onboarding/save-config")
async def save_config(body: SaveConfig, user_id: str = Depends(get_user_id_from_jwt)):
    from backend.config.brief import generate_agent_brief

    agent = _get_session_for_user(body.session_id, user_id)
    tenant_id = body.existing_tenant_id or str(uuid.uuid4())
    config = await agent.build_tenant_config(tenant_id, body.owner_email, body.owner_name, body.active_agents)

    # Generate condensed brief — all agents use this instead of full context
    try:
        config.agent_brief = await generate_agent_brief(config)
    except Exception as e:
        logger.warning("Brief generation failed (will use full context): %s", e)

    save_tenant_config(config)
    # Drop the in-memory session and the persisted draft row so future
    # /start calls for this user begin from scratch.
    onboarding_sessions.pop(body.session_id, None)
    _delete_onboarding_draft(user_id)
    return {"tenant_id": tenant_id, "config": config.model_dump(mode="json")}


@router.post("/api/onboarding/save-config-direct")
async def save_config_direct(
    body: SaveConfigDirect,
    request: Request,
):
    """Save a tenant config directly from the cached frontend extraction.

    JWT-bound (2026-05-07): owner_email is derived from the JWT email claim,
    NOT trusted from the request body. Previously this endpoint was public
    and accepted whatever owner_email the client sent, which let any caller
    overwrite any user's tenant config (or impersonate a brand-new tenant
    under someone else's email). Now:

      - owner_email comes from the JWT (`email` claim)
      - if existing_tenant_id is set, the JWT user must own that tenant
        (ownership check via `get_verified_tenant`)
    """
    from backend.config.tenant_schema import (
        TenantConfig, ICPConfig, ProductConfig, GTMPlaybook, BrandVoice, GTMProfile,
    )
    from backend.config.brief import generate_agent_brief

    # Derive owner identity from the JWT — ignore body fields entirely.
    user = await get_current_user(request)
    jwt_email = (user.get("email") or user.get("user_metadata", {}).get("email") or "").lower().strip()
    if not jwt_email:
        # Dev-mode fallthrough has email="dev@localhost"; real prod tokens
        # always carry an email claim. If we got here without one, fail.
        raise HTTPException(status_code=401, detail="Invalid token: no email claim")

    # If the caller is overwriting an existing tenant, verify ownership
    # before letting them clobber it. Reuses the same predicate everything
    # else in the system uses (`tenant_configs.owner_email` match).
    if body.existing_tenant_id:
        await get_verified_tenant(request, body.existing_tenant_id)

    extracted = body.config
    has_skips = bool(body.skipped_topics)
    tenant_id = body.existing_tenant_id or str(uuid.uuid4())

    # Build GTMProfile from the flat gtm_profile extraction.
    gp_raw = extracted.get("gtm_profile", {})
    # Ensure generated fields are always populated
    from backend.agents.onboarding_agent import _ensure_generated_fields
    gp_raw = _ensure_generated_fields(gp_raw)
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
        owner_email=jwt_email,
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


# ─── Onboarding Draft Persistence ───
# Server-side persistence of in-progress onboarding state, keyed on the
# Supabase auth user_id. Solves the bug where users who clear localStorage,
# open onboarding in a second tab, hard-refresh after a long idle, or
# switch browsers would lose their 10-min CEO conversation and have to
# restart from scratch.
#
# Frontend flow:
#   1. /review extracts the config -> POST /api/onboarding/save-draft
#      to mirror it to the DB
#   2. /select-agents tries GET /api/onboarding/draft on mount BEFORE
#      reading localStorage; falls back to localStorage only if the API
#      returns empty
#   3. After successful /save-config, frontend calls DELETE to clean up

@router.post("/api/onboarding/save-draft")
async def save_onboarding_draft(
    body: OnboardingDraftPayload,
    user_id: str = Depends(get_user_id_from_jwt),
):
    """Upsert the authenticated user's in-progress onboarding draft.

    JWT-bound (2026-05-07, CRITICAL audit fix): user_id always comes from
    the JWT. Any user_id field in the request body is ignored. Previously
    this endpoint was public and trusted whatever user_id the client sent,
    which defeated the /start auth-binding (an attacker could overwrite
    any user's draft by knowing their UUID).
    """
    try:
        sb = _get_supabase()
        row = {
            "user_id": user_id,
            "session_id": body.session_id,
            "extracted_config": body.extracted_config,
            "skipped_topics": body.skipped_topics,
            "conversation_history": body.conversation_history,
        }
        # Upsert on user_id so we always have at most one in-progress
        # draft per user. The trigger updates updated_at automatically.
        sb.table("onboarding_drafts").upsert(row, on_conflict="user_id").execute()
        return {"saved": True}
    except Exception as e:
        logger.warning("Failed to save onboarding draft for user=%s: %s", user_id, e)
        return {"saved": False, "error": str(e)[:200]}


@router.get("/api/onboarding/draft")
async def get_onboarding_draft(user_id: str = Depends(get_user_id_from_jwt)):
    """Return the authenticated user's most recent in-progress onboarding
    draft, or 404 if none exists. Used by /select-agents on mount before
    falling back to localStorage.

    JWT-bound (2026-05-07, CRITICAL audit fix): user_id always comes from
    the JWT. Any ?user_id= query param is ignored. Previously this was
    public and let any caller read any user's draft by guessing UUIDs.
    """
    try:
        sb = _get_supabase()
        result = (
            sb.table("onboarding_drafts")
            .select("session_id,extracted_config,skipped_topics,conversation_history,updated_at")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        if not result.data:
            raise HTTPException(status_code=404, detail="No draft found")
        return result.data[0]
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("Failed to load onboarding draft for user=%s: %s", user_id, e)
        raise HTTPException(status_code=500, detail="Could not load draft")


@router.delete("/api/onboarding/draft")
async def delete_onboarding_draft(user_id: str = Depends(get_user_id_from_jwt)):
    """Clean up the authenticated user's draft after successful save-config.
    Best-effort: if the delete fails the row will just expire naturally
    over time.

    JWT-bound (2026-05-07, CRITICAL audit fix): user_id always comes from
    the JWT. Any ?user_id= query param is ignored. Previously this was
    public and let any caller delete any user's draft.
    """
    try:
        sb = _get_supabase()
        sb.table("onboarding_drafts").delete().eq("user_id", user_id).execute()
        return {"deleted": True}
    except Exception as e:
        logger.warning("Failed to delete onboarding draft for user=%s: %s", user_id, e)
        return {"deleted": False}


# ─── Re-onboarding / Edit Mode ───

@router.get("/api/tenant/{tenant_id}/onboarding-data")
async def get_onboarding_data(
    tenant_id: str,
    _verified: dict = Depends(get_verified_tenant),
):
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


@router.post("/api/tenant/{tenant_id}/update-onboarding")
async def update_onboarding(
    tenant_id: str,
    body: UpdateOnboarding,
    _verified: dict = Depends(get_verified_tenant),
):
    """Update specific onboarding fields on an existing tenant, regenerate
    derived gtm_profile fields and the agent brief so edits propagate to
    every downstream consumer.
    """
    from backend.config.brief import generate_agent_brief
    from backend.agents.onboarding_agent import _ensure_generated_fields

    try:
        config = get_tenant_config(tenant_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Tenant not found")

    for field in _ONBOARDING_EDITABLE_FIELDS:
        value = getattr(body, field)
        if value is not None:
            _apply_onboarding_edit(config, field, value)

    # Regenerate derived fields (positioning_summary, 30_day_gtm_focus) from
    # the refreshed answers. _ensure_generated_fields is deterministic — just
    # string interpolation over the gtm_profile dict, no LLM call.
    gp = config.gtm_profile
    gp_dict = gp.model_dump()
    gp_dict["positioning_summary"] = ""
    gp_dict["30_day_gtm_focus"] = ""
    regen = _ensure_generated_fields(gp_dict)
    gp.positioning_summary = regen.get("positioning_summary", "")
    gp.thirty_day_gtm_focus = regen.get("30_day_gtm_focus", "")

    config.onboarding_status = "completed"
    config.skipped_fields = []

    # Regenerate the condensed agent brief so CEO chat picks up the edits.
    try:
        config.agent_brief = await generate_agent_brief(config)
    except Exception as e:
        logger.warning("Brief regeneration failed: %s", e)

    save_tenant_config(config)
    return {"ok": True, "tenant_id": str(config.tenant_id)}


# ─── Agent Brief (re)generation ───

@router.post("/api/tenants/{tenant_id}/regenerate-brief")
async def regenerate_brief(
    tenant_id: str,
    _verified: dict = Depends(get_verified_tenant),
):
    """Regenerate the condensed agent brief for an existing tenant.

    Call this after the user updates their business info in settings,
    or to backfill briefs for tenants who onboarded before this feature.
    """
    from backend.config.brief import generate_agent_brief

    config = get_tenant_config(tenant_id)
    config.agent_brief = await generate_agent_brief(config)
    save_tenant_config(config)
    return {"agent_brief": config.agent_brief}
