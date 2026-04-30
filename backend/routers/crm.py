"""CRM Router — contacts, companies, deals, activities, pipeline."""
from __future__ import annotations

import logging

from fastapi import APIRouter

from backend.schemas import (
    CrmContactCreate, CrmContactUpdate, CrmCompanyCreate, CrmCompanyUpdate,
    CrmDealCreate, CrmDealUpdate, CrmActivityCreate,
)
from backend.services import crm as crm_service
from backend.services.realtime import sio

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
