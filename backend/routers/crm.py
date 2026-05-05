"""CRM Router — contacts, companies, deals, activities, pipeline."""
from __future__ import annotations

import logging
import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.schemas import (
    CrmContactCreate, CrmContactUpdate, CrmCompanyCreate, CrmCompanyUpdate,
    CrmDealCreate, CrmDealUpdate, CrmActivityCreate,
)
from backend.services import crm as crm_service
from backend.services.email_provider import send_email as send_email_via_provider
from backend.services.realtime import sio

# RFC 5322 is huge — this matches the practical 99% subset most CRMs
# accept. Frontend uses a similar regex, but server-side is authoritative.
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")

logger = logging.getLogger("aria.routers.crm")

router = APIRouter(prefix="/api/crm/{tenant_id}", tags=["CRM"])


async def _emit_crm_update(tenant_id: str, entity: str) -> None:
    """Notify the dashboard's CRM page that a record has changed so it
    refetches the affected list. The frontend already listens for
    `crm_update` and refreshes the relevant tab when the entity matches.
    Synthetic deals (contacts viewed as pipeline cards) need this too —
    when a contact's status changes, both the Contacts table and the
    Deals board need to re-render.
    """
    try:
        await sio.emit("crm_update", {"entity": entity}, room=tenant_id)
    except Exception as e:
        logger.debug("[crm] socket emit failed for entity=%s: %s", entity, e)


# ── Contacts ──────────────────────────────────────────────────────────────────

@router.get("/contacts")
async def list_contacts(tenant_id: str, search: str = "", status: str = "", page: int = 1, page_size: int = 50):
    return crm_service.list_contacts(tenant_id, search, status, page, page_size)


@router.get("/contacts/{contact_id}")
async def get_contact(tenant_id: str, contact_id: str):
    return crm_service.get_contact(tenant_id, contact_id)


@router.post("/contacts")
async def create_contact(tenant_id: str, body: CrmContactCreate):
    result = crm_service.create_contact(tenant_id, body.model_dump())
    # Emit BOTH entities — a new contact also appears as a synthetic
    # deal in the pipeline view, so the Deals tab needs to refetch too.
    await _emit_crm_update(tenant_id, "crm_contact")
    await _emit_crm_update(tenant_id, "crm_deal")
    return result


@router.patch("/contacts/{contact_id}")
async def update_contact(tenant_id: str, contact_id: str, body: CrmContactUpdate):
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        return {"updated": contact_id, "changes": {}}
    result = crm_service.update_contact(tenant_id, contact_id, updates)
    await _emit_crm_update(tenant_id, "crm_contact")
    # Status change => the contact's synthetic deal moves between Kanban
    # columns. Always emit the deal entity too on contact updates so the
    # Deals board never lags behind the Contacts table.
    if "status" in updates:
        await _emit_crm_update(tenant_id, "crm_deal")
    return result


@router.delete("/contacts/{contact_id}")
async def delete_contact(tenant_id: str, contact_id: str):
    result = crm_service.delete_contact(tenant_id, contact_id)
    await _emit_crm_update(tenant_id, "crm_contact")
    await _emit_crm_update(tenant_id, "crm_deal")
    return result


# ── Contact email send ────────────────────────────────────────────────────────


class CrmContactSendEmailRequest(BaseModel):
    subject: str
    body: str  # plain-text body; we also wrap into a minimal HTML version


@router.post("/contacts/{contact_id}/send-email")
async def send_email_to_contact(
    tenant_id: str, contact_id: str, body: CrmContactSendEmailRequest
):
    """Send an email to a CRM contact via the tenant's configured provider.

    Validation order — fail fast, surface a useful 4xx instead of leaking
    a 5xx from the provider:
      1. Contact exists and belongs to this tenant
      2. Contact has a non-empty email field
      3. Email matches the practical RFC 5322 subset
      4. Subject + body are non-empty
    Logs the send as a `crm_activities` row so the contact's timeline
    shows the touch even when the provider is async (Resend webhooks).
    """
    contact = crm_service.get_contact(tenant_id, contact_id)
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    to = (contact.get("email") or "").strip()
    if not to:
        raise HTTPException(
            status_code=400,
            detail="Contact has no email on file. Add an email address before sending.",
        )
    if not _EMAIL_RE.match(to):
        raise HTTPException(
            status_code=400,
            detail=f"Contact email '{to}' is not a valid address.",
        )

    subject = (body.subject or "").strip()
    text_body = (body.body or "").strip()
    if not subject:
        raise HTTPException(status_code=400, detail="Subject is required.")
    if not text_body:
        raise HTTPException(status_code=400, detail="Message body is required.")

    # Minimal HTML wrapper — preserves user line breaks, no styling so the
    # tenant's email template (if any) takes over downstream. Most tenants
    # use Resend's default rendering which is fine for plain text.
    html_body = "<p>" + "<br>".join(text_body.splitlines()) + "</p>"

    try:
        result = await send_email_via_provider(
            tenant_id,
            to=to,
            subject=subject,
            html_body=html_body,
            text_body=text_body,
        )
    except Exception as e:
        logger.exception("[crm] send-email provider error: %s", e)
        raise HTTPException(status_code=502, detail=f"Email provider error: {e}")

    if not result or not result.get("success"):
        detail = (result or {}).get("error") or "Unknown send error"
        raise HTTPException(status_code=502, detail=str(detail))

    # Best-effort activity log — never block the send response on this
    try:
        crm_service.create_activity(tenant_id, {
            "contact_id": contact_id,
            "type": "email_sent",
            "title": subject,
            "body": text_body[:500],
            "metadata": {"to": to, "provider": result.get("provider", "")},
        })
    except Exception as e:
        logger.debug("[crm] activity log failed (non-fatal): %s", e)

    return {
        "status": "sent",
        "to": to,
        "message_id": result.get("message_id", ""),
        "provider": result.get("provider", ""),
    }


# ── Companies ─────────────────────────────────────────────────────────────────

@router.get("/companies")
async def list_companies(tenant_id: str, search: str = ""):
    return crm_service.list_companies(tenant_id, search)


@router.get("/companies/{company_id}")
async def get_company(tenant_id: str, company_id: str):
    return crm_service.get_company(tenant_id, company_id)


@router.post("/companies")
async def create_company(tenant_id: str, body: CrmCompanyCreate):
    return crm_service.create_company(tenant_id, body.model_dump())


@router.patch("/companies/{company_id}")
async def update_company(tenant_id: str, company_id: str, body: CrmCompanyUpdate):
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        return {"updated": company_id, "changes": {}}
    return crm_service.update_company(tenant_id, company_id, updates)


@router.delete("/companies/{company_id}")
async def delete_company(tenant_id: str, company_id: str):
    return crm_service.delete_company(tenant_id, company_id)


# ── Deals ─────────────────────────────────────────────────────────────────────

@router.get("/deals")
async def list_deals(tenant_id: str, stage: str = ""):
    return crm_service.list_deals(tenant_id, stage)


@router.get("/deals/{deal_id}")
async def get_deal(tenant_id: str, deal_id: str):
    return crm_service.get_deal(tenant_id, deal_id)


@router.post("/deals")
async def create_deal(tenant_id: str, body: CrmDealCreate):
    result = crm_service.create_deal(tenant_id, body.model_dump())
    await _emit_crm_update(tenant_id, "crm_deal")
    return result


@router.patch("/deals/{deal_id}")
async def update_deal(tenant_id: str, deal_id: str, body: CrmDealUpdate):
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        return {"updated": deal_id, "changes": {}}
    result = crm_service.update_deal(tenant_id, deal_id, updates)
    await _emit_crm_update(tenant_id, "crm_deal")
    # Synthetic deals route through crm_contacts.status under the hood,
    # so a successful update there ALSO needs the contacts list to
    # refresh (status badge in the Contacts table changes color).
    if result.get("synthetic"):
        await _emit_crm_update(tenant_id, "crm_contact")
    return result


@router.delete("/deals/{deal_id}")
async def delete_deal(tenant_id: str, deal_id: str):
    result = crm_service.delete_deal(tenant_id, deal_id)
    if not result.get("skipped"):
        await _emit_crm_update(tenant_id, "crm_deal")
    return result


# ── Activities ────────────────────────────────────────────────────────────────

@router.get("/activities")
async def list_activities(tenant_id: str, contact_id: str = "", limit: int = 30):
    return crm_service.list_activities(tenant_id, contact_id, limit)


@router.post("/activities")
async def create_activity(tenant_id: str, body: CrmActivityCreate):
    return crm_service.create_activity(tenant_id, body.model_dump())


# ── Pipeline ──────────────────────────────────────────────────────────────────

@router.get("/pipeline-summary")
async def get_pipeline_summary(tenant_id: str):
    return crm_service.pipeline_summary(tenant_id)
