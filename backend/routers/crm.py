"""CRM Router — contacts, companies, deals, activities, pipeline."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from backend.auth import get_verified_tenant
from backend.config.loader import get_tenant_config
from backend.schemas import (
    CrmContactCreate, CrmContactUpdate, CrmCompanyCreate, CrmCompanyUpdate,
    CrmDealCreate, CrmDealUpdate, CrmActivityCreate,
)
from backend.services import crm as crm_service
from backend.services.email_provider import send_email as send_email_via_provider
from backend.services.realtime import sio
from backend.services.supabase import get_db

# RFC 5322 is huge — this matches the practical 99% subset most CRMs
# accept. Frontend uses a similar regex, but server-side is authoritative.
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")

logger = logging.getLogger("aria.routers.crm")

# Router-level dep: every route under /api/crm/{tenant_id}/ runs
# get_verified_tenant first. Returns 403 unless the JWT user owns the
# tenant_id in the path. Closes the IDOR class for the entire CRM
# surface in one line. Dev-mode (SUPABASE_JWT_SECRET unset) still
# bypasses via the "dev-user" early-return inside get_verified_tenant.
router = APIRouter(
    prefix="/api/crm/{tenant_id}",
    tags=["CRM"],
    dependencies=[Depends(get_verified_tenant)],
)


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


@router.get("/contacts/by-email")
async def find_contact_by_email(tenant_id: str, email: str = ""):
    """Look up a CRM contact by email (case-insensitive). Returns
    {contact: <row>} when found, {contact: null} when not — never
    raises 404 so the caller can branch in one shot without a try/catch.

    Used by the Conversations page to switch between 'Add to CRM' and
    'View in CRM' affordances per thread. Path is BEFORE
    /contacts/{contact_id} so FastAPI doesn't route 'by-email' as an
    {contact_id} param.
    """
    contact = crm_service.find_contact_by_email(tenant_id, email)
    return {"contact": contact}


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


# ── Bulk import (CSV / XLSX) ──────────────────────────────────────────────────
#
# Two-phase: /contacts/import/preview returns headers + a sample so the
# frontend can render the column-mapping UI; /contacts/import re-receives
# the file plus the mapping and writes the rows. Both endpoints accept
# the file via multipart/form-data so the browser sets Content-Type
# automatically and we don't have to base64 the file in JSON.


@router.post("/contacts/import/preview")
async def preview_contact_import(tenant_id: str, file: UploadFile = File(...)):
    """Parse the uploaded file and return its header row + a sample of
    body rows. Frontend uses this to render the column-mapping form.
    Nothing is written to the database.
    """
    from backend.services import crm_import

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="uploaded file is empty")
    if len(raw) > 25 * 1024 * 1024:
        raise HTTPException(
            status_code=413,
            detail="file too large (max 25MB) — split it and re-upload",
        )

    try:
        rows = crm_import.parse_file(raw, file.filename or "", file.content_type or "")
    except Exception as e:
        logger.warning("[crm-import] parse failed: %s", e)
        raise HTTPException(
            status_code=400,
            detail=f"could not parse file — make sure it's a valid CSV or XLSX",
        )

    return crm_import.build_preview(rows)


class ContactImportRequest(BaseModel):
    """Body for the second-phase import call. The file rides on the
    same multipart request as a separate 'file' field; this Pydantic
    shape only documents the JSON 'mapping' field for the OpenAPI
    schema. In practice we read everything off the form.
    """
    mapping: dict[str, str]
    extra_notes_columns: list[str] = []


@router.post("/contacts/import")
async def import_contacts(
    tenant_id: str,
    file: UploadFile = File(...),
    mapping: str = Form(...),
    extra_notes_columns: str = Form(default=""),
):
    """Apply the operator-supplied column mapping and insert contacts.

    Form fields:
      file                  - the .csv / .xlsx file
      mapping               - JSON: {aria_field: source_column_name}
      extra_notes_columns   - JSON list of source columns to roll up
                              into the notes field
    """
    import json as _json
    from backend.services import crm_import

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="uploaded file is empty")

    try:
        mapping_dict = _json.loads(mapping) if mapping else {}
    except _json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="mapping must be valid JSON")
    if not isinstance(mapping_dict, dict):
        raise HTTPException(status_code=400, detail="mapping must be a JSON object")
    if not mapping_dict.get("email") and not mapping_dict.get("name"):
        raise HTTPException(
            status_code=400,
            detail="map at least one of: email, name (otherwise rows have no identity)",
        )

    try:
        extra_cols = _json.loads(extra_notes_columns) if extra_notes_columns else []
    except _json.JSONDecodeError:
        extra_cols = []
    if not isinstance(extra_cols, list):
        extra_cols = []

    try:
        rows = crm_import.parse_file(raw, file.filename or "", file.content_type or "")
    except Exception as e:
        logger.warning("[crm-import] parse failed during import: %s", e)
        raise HTTPException(status_code=400, detail="could not parse file")

    result = crm_import.import_contacts(
        tenant_id, rows, mapping_dict, extra_cols,
    )

    if result.get("imported"):
        await _emit_crm_update(tenant_id, "crm_contact")
        # Auto-created companies surface on the Companies tab too
        await _emit_crm_update(tenant_id, "crm_company")

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

    sb = get_db()
    now_iso = datetime.now(timezone.utc).isoformat()
    cfg = get_tenant_config(tenant_id)
    sender_email = (cfg.owner_email or "").strip()
    contact_name = (contact.get("name") or "").strip()
    preview_snippet = text_body[:280]

    # ── 1. Find or create the email_thread for this contact ──
    # Re-using the most recent thread per (tenant, contact_email) means
    # repeated CRM sends to the same person all live in one Conversations
    # thread instead of fragmenting into one-off threads.
    thread_id = ""
    try:
        existing = (
            sb.table("email_threads").select("id")
            .eq("tenant_id", tenant_id).eq("contact_email", to)
            .order("last_message_at", desc=True).limit(1).execute()
        )
        if existing.data:
            thread_id = existing.data[0]["id"]
    except Exception as e:
        logger.debug("[crm] thread lookup failed (will create new): %s", e)

    if not thread_id:
        try:
            tr = sb.table("email_threads").insert({
                "tenant_id": tenant_id,
                "contact_email": to,
                "subject": subject,
                "status": "awaiting_reply",
                "last_message_at": now_iso,
            }).execute()
            if tr.data:
                thread_id = tr.data[0]["id"]
        except Exception as e:
            logger.warning("[crm] email_threads insert failed: %s", e)

    # ── 2. Insert inbox_items row (status=sending, will flip to sent) ──
    # Type=email_sequence + agent=email_marketer matches the existing
    # EmailEditor renderer in the inbox UI so the row displays as a
    # proper email card with the draft body visible.
    inbox_title = f"To {contact_name or to}: {subject}"[:200]
    inbox_item_id = ""
    email_draft_payload = {
        "to": to,
        "subject": subject,
        "html_body": html_body,
        "text_body": text_body,
        "preview_snippet": preview_snippet,
        "status": "sending",
        "reply_to_thread_id": thread_id,
    }
    try:
        ir = sb.table("inbox_items").insert({
            "tenant_id": tenant_id,
            "agent": "email_marketer",
            "type": "email_sequence",
            "title": inbox_title,
            "content": text_body[:500],
            "status": "sending",
            "priority": "normal",
            "email_draft": email_draft_payload,
        }).execute()
        if ir.data:
            inbox_item_id = ir.data[0]["id"]
    except Exception as e:
        logger.warning("[crm] inbox_items insert failed: %s", e)

    # ── 3. Insert outbound email_messages row (the Conversations entry) ──
    email_msg_id = ""
    if thread_id:
        try:
            mr = sb.table("email_messages").insert({
                "thread_id": thread_id,
                "tenant_id": tenant_id,
                "direction": "outbound",
                "sender": sender_email or "ARIA",
                "recipients": to,
                "subject": subject,
                "text_body": text_body,
                "html_body": html_body,
                "preview_snippet": preview_snippet,
                "message_timestamp": now_iso,
                "approval_status": "sending",
            }).execute()
            if mr.data:
                email_msg_id = mr.data[0]["id"]
        except Exception as e:
            logger.warning("[crm] email_messages insert failed: %s", e)

    # ── 4. Send via the provider ──
    # inbound_thread_id is what feeds the Reply-To token builder so when
    # the recipient hits Reply, their message routes back to ARIA's
    # /api/email/inbound webhook and matches THIS thread by id.
    try:
        result = await send_email_via_provider(
            tenant_id,
            to=to,
            subject=subject,
            html_body=html_body,
            text_body=text_body,
            inbound_thread_id=thread_id,
            inbox_item_id=inbox_item_id,
        )
    except Exception as e:
        logger.exception("[crm] send-email provider error: %s", e)
        _mark_send_failed(sb, inbox_item_id, email_msg_id)
        from backend.services.safe_errors import safe_detail
        raise HTTPException(status_code=502, detail=safe_detail(e, "Email provider error"))

    if not result or not result.get("success"):
        _mark_send_failed(sb, inbox_item_id, email_msg_id)
        detail = (result or {}).get("error") or "Unknown send error"
        from backend.services.safe_errors import safe_detail
        raise HTTPException(status_code=502, detail=safe_detail(detail, "Email send failed"))

    provider_msg_id = (result.get("message_id") or "").strip()

    # ── 5. Flip statuses to sent + bump the thread ──
    if inbox_item_id:
        try:
            sb.table("inbox_items").update({
                "status": "sent",
                "email_draft": {
                    **email_draft_payload,
                    "status": "sent",
                    "provider_message_id": provider_msg_id,
                },
                "updated_at": now_iso,
            }).eq("id", inbox_item_id).execute()
        except Exception as e:
            logger.debug("[crm] inbox sent-flip failed: %s", e)

    if email_msg_id:
        try:
            update_msg: dict = {"approval_status": "sent"}
            if provider_msg_id:
                update_msg["gmail_message_id"] = provider_msg_id
            sb.table("email_messages").update(update_msg).eq("id", email_msg_id).execute()
        except Exception as e:
            logger.debug("[crm] email_messages sent-flip failed: %s", e)

    if thread_id:
        try:
            sb.table("email_threads").update({
                "last_message_at": now_iso,
                "status": "awaiting_reply",
                "updated_at": now_iso,
            }).eq("id", thread_id).execute()
        except Exception as e:
            logger.debug("[crm] thread bump failed: %s", e)

    # ── 6. CRM activity log (timeline entry) ──
    try:
        crm_service.create_activity(tenant_id, {
            "contact_id": contact_id,
            "type": "email_sent",
            "title": subject,
            "body": text_body[:500],
            "metadata": {
                "to": to,
                "provider": result.get("provider", ""),
                "thread_id": thread_id,
                "inbox_item_id": inbox_item_id,
            },
        })
    except Exception as e:
        logger.debug("[crm] activity log failed (non-fatal): %s", e)

    # ── 7. Live UI refresh — Inbox + Conversations + CRM ──
    try:
        await sio.emit("inbox_updated", {
            "action": "new_sent",
            "inbox_item_id": inbox_item_id,
        }, room=tenant_id)
        if thread_id:
            await sio.emit("email_thread_updated", {
                "thread_id": thread_id,
                "status": "awaiting_reply",
                "last_message_at": now_iso,
            }, room=tenant_id)
        await _emit_crm_update(tenant_id, "crm_contact")
    except Exception as e:
        logger.debug("[crm] socket emits failed (non-fatal): %s", e)

    return {
        "status": "sent",
        "to": to,
        "thread_id": thread_id,
        "inbox_item_id": inbox_item_id,
        "message_id": provider_msg_id,
        "provider": result.get("provider", ""),
    }


def _mark_send_failed(sb, inbox_item_id: str, email_msg_id: str) -> None:
    """Flip inbox + email_messages to failed when the provider rejects.
    Best-effort: never raises since the caller is already on an error
    path returning a 502 to the user."""
    if inbox_item_id:
        try:
            sb.table("inbox_items").update({"status": "failed"}).eq("id", inbox_item_id).execute()
        except Exception as e:
            logger.debug("[crm] inbox failed-flip failed: %s", e)
    if email_msg_id:
        try:
            sb.table("email_messages").update({"approval_status": "failed"}).eq("id", email_msg_id).execute()
        except Exception as e:
            logger.debug("[crm] email_messages failed-flip failed: %s", e)


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
