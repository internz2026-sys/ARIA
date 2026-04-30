"""CRM Service — shared CRUD operations for contacts, companies, and deals.

Used by both server.py API endpoints and ceo_actions.py CEO dispatcher.
"""
from __future__ import annotations

from datetime import datetime, timezone

from backend.services.supabase import get_db


# ── Contacts ──────────────────────────────────────────────────────────────────

def list_contacts(tenant_id: str, search: str = "", status: str = "", page: int = 1, page_size: int = 50) -> dict:
    sb = get_db()
    query = sb.table("crm_contacts").select("*").eq("tenant_id", tenant_id)
    if search:
        query = query.ilike("name", f"%{search}%")
    if status:
        query = query.eq("status", status)
    count_q = sb.table("crm_contacts").select("id", count="exact").eq("tenant_id", tenant_id)
    if search:
        count_q = count_q.ilike("name", f"%{search}%")
    if status:
        count_q = count_q.eq("status", status)
    count_result = count_q.execute()
    total = count_result.count if count_result.count is not None else len(count_result.data)
    offset = (max(page, 1) - 1) * page_size
    result = query.order("created_at", desc=True).range(offset, offset + page_size - 1).execute()
    return {"contacts": result.data or [], "total": total}


def get_contact(tenant_id: str, contact_id: str) -> dict:
    sb = get_db()
    result = sb.table("crm_contacts").select("*").eq("id", contact_id).eq("tenant_id", tenant_id).single().execute()
    return result.data or {}


def create_contact(tenant_id: str, data: dict) -> dict:
    sb = get_db()
    row = {"tenant_id": tenant_id, **data}
    row.setdefault("source", "manual")
    row.setdefault("status", "lead")
    result = sb.table("crm_contacts").insert(row).execute()
    contact = result.data[0] if result.data else None
    if contact:
        sb.table("crm_activities").insert({
            "tenant_id": tenant_id,
            "contact_id": contact["id"],
            "type": "contact_created",
            "description": f"Contact {data.get('name', '')} created",
        }).execute()
    return {"contact": contact}


def update_contact(tenant_id: str, contact_id: str, updates: dict) -> dict:
    sb = get_db()
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    sb.table("crm_contacts").update(updates).eq("id", contact_id).eq("tenant_id", tenant_id).execute()
    return {"updated": contact_id, "changes": updates}


def delete_contact(tenant_id: str, contact_id: str) -> dict:
    sb = get_db()
    sb.table("crm_contacts").delete().eq("id", contact_id).eq("tenant_id", tenant_id).execute()
    return {"deleted": contact_id}


# ── Companies ─────────────────────────────────────────────────────────────────

def list_companies(tenant_id: str, search: str = "") -> dict:
    sb = get_db()
    query = sb.table("crm_companies").select("*").eq("tenant_id", tenant_id)
    if search:
        query = query.ilike("name", f"%{search}%")
    result = query.order("created_at", desc=True).execute()
    return {"companies": result.data or []}


def get_company(tenant_id: str, company_id: str) -> dict:
    sb = get_db()
    result = sb.table("crm_companies").select("*").eq("id", company_id).eq("tenant_id", tenant_id).single().execute()
    company = result.data or {}
    contacts = sb.table("crm_contacts").select("*").eq("company_id", company_id).eq("tenant_id", tenant_id).execute()
    deals = sb.table("crm_deals").select("*").eq("company_id", company_id).eq("tenant_id", tenant_id).execute()
    return {"company": company, "contacts": contacts.data or [], "deals": deals.data or []}


def create_company(tenant_id: str, data: dict) -> dict:
    sb = get_db()
    row = {"tenant_id": tenant_id, **data}
    result = sb.table("crm_companies").insert(row).execute()
    return {"company": result.data[0] if result.data else None}


def update_company(tenant_id: str, company_id: str, updates: dict) -> dict:
    sb = get_db()
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    sb.table("crm_companies").update(updates).eq("id", company_id).eq("tenant_id", tenant_id).execute()
    return {"updated": company_id, "changes": updates}


def delete_company(tenant_id: str, company_id: str) -> dict:
    sb = get_db()
    sb.table("crm_companies").delete().eq("id", company_id).eq("tenant_id", tenant_id).execute()
    return {"deleted": company_id}


# ── Deals ─────────────────────────────────────────────────────────────────────
#
# The Deals board (Kanban view) shows a UNION of two sources so the
# pipeline visualization is never empty just because the user hasn't
# manually opened a "Deal" record yet:
#
#   1. Real deal rows from crm_deals (have title, value, stage, notes,
#      expected_close, etc.)
#   2. Contact-derived synthetic deals from crm_contacts — any contact
#      whose id is NOT already referenced by a real deal appears as a
#      synthetic deal with stage = contact.status, title = contact.name.
#      These have id "contact:<contact_uuid>" so the frontend / update
#      handler can tell them apart.
#
# When the user drags a synthetic deal between columns, update_deal
# detects the "contact:" prefix and writes to crm_contacts.status
# instead of crm_deals.stage. Deleting a synthetic deal is a no-op
# (the user should remove the contact via the Contacts tab — deleting
# the pipeline card without deleting the contact would just put it
# right back next refetch).
#
# Stage values are normalized to crm_deals' vocabulary. The two tables
# happen to share the same column values (lead/qualified/proposal/
# negotiation/won/lost) but if they diverge in the future, the mapping
# happens here in _stage_from_contact_status.

_SYNTHETIC_PREFIX = "contact:"


def _stage_from_contact_status(status: str | None) -> str:
    """Map crm_contacts.status to crm_deals.stage. Currently 1:1 since
    the vocabularies match, but the mapping is centralized here so a
    future schema split is a single edit."""
    return (status or "lead").lower().strip() or "lead"


def _contact_to_synthetic_deal(c: dict) -> dict:
    """Project a crm_contacts row into the deal-card shape the Kanban
    expects. The id prefix lets update_deal route stage changes back
    to the underlying contact row."""
    return {
        "id": f"{_SYNTHETIC_PREFIX}{c['id']}",
        "tenant_id": c.get("tenant_id"),
        "title": c.get("name") or c.get("email") or "Unnamed contact",
        "value": 0,
        "stage": _stage_from_contact_status(c.get("status")),
        "contact_id": c["id"],
        "company_id": c.get("company_id"),
        "notes": c.get("notes") or "",
        "created_at": c.get("created_at"),
        "updated_at": c.get("updated_at"),
        "_synthetic": True,
    }


def _list_deals_union(tenant_id: str) -> list[dict]:
    """Build the UNION of real + synthetic deals.

    Skips synthetic generation for any contact already referenced by a
    real deal (so we don't show the same person twice).
    """
    sb = get_db()
    real_deals = (
        sb.table("crm_deals")
        .select("*")
        .eq("tenant_id", tenant_id)
        .order("created_at", desc=True)
        .execute()
    ).data or []

    contacts = (
        sb.table("crm_contacts")
        .select("id, name, email, status, company_id, notes, tenant_id, created_at, updated_at")
        .eq("tenant_id", tenant_id)
        .execute()
    ).data or []

    referenced_contacts = {d.get("contact_id") for d in real_deals if d.get("contact_id")}
    synthetic = [_contact_to_synthetic_deal(c) for c in contacts if c["id"] not in referenced_contacts]

    return real_deals + synthetic


def list_deals(tenant_id: str, stage: str = "") -> dict:
    deals = _list_deals_union(tenant_id)
    if stage:
        deals = [d for d in deals if d.get("stage") == stage]
    return {"deals": deals}


def get_deal(tenant_id: str, deal_id: str) -> dict:
    if deal_id.startswith(_SYNTHETIC_PREFIX):
        contact_id = deal_id[len(_SYNTHETIC_PREFIX):]
        contact = get_contact(tenant_id, contact_id)
        if not contact:
            return {}
        return _contact_to_synthetic_deal(contact)
    sb = get_db()
    result = sb.table("crm_deals").select("*").eq("id", deal_id).eq("tenant_id", tenant_id).single().execute()
    return result.data or {}


def create_deal(tenant_id: str, data: dict) -> dict:
    sb = get_db()
    row = {"tenant_id": tenant_id, **data}
    row.setdefault("stage", "lead")
    row.setdefault("value", 0)
    result = sb.table("crm_deals").insert(row).execute()
    return {"deal": result.data[0] if result.data else None}


def update_deal(tenant_id: str, deal_id: str, updates: dict) -> dict:
    """Update a deal. For synthetic deals (id starts with "contact:"),
    the only field we can meaningfully sync back is stage → contact
    status. Other fields (title/value/notes) are silently dropped
    because there's no underlying deal row to store them on — the
    user would need to "promote" the contact to a real deal first."""
    if deal_id.startswith(_SYNTHETIC_PREFIX):
        contact_id = deal_id[len(_SYNTHETIC_PREFIX):]
        contact_updates: dict = {}
        if "stage" in updates and updates["stage"] is not None:
            contact_updates["status"] = _stage_from_contact_status(updates["stage"])
        if not contact_updates:
            # Caller sent fields that don't apply to a synthetic deal —
            # noop response so the frontend doesn't think the call
            # failed.
            return {"updated": deal_id, "changes": {}, "synthetic": True}
        contact_updates["updated_at"] = datetime.now(timezone.utc).isoformat()
        sb = get_db()
        sb.table("crm_contacts").update(contact_updates).eq("id", contact_id).eq("tenant_id", tenant_id).execute()
        return {"updated": deal_id, "changes": contact_updates, "synthetic": True}

    sb = get_db()
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    sb.table("crm_deals").update(updates).eq("id", deal_id).eq("tenant_id", tenant_id).execute()
    return {"updated": deal_id, "changes": updates}


def delete_deal(tenant_id: str, deal_id: str) -> dict:
    """Delete a real deal. Synthetic deals can't be deleted from the
    Deals tab — deleting a pipeline card without removing the contact
    would just put it right back on the next refetch. The frontend
    surfaces this as a tooltip on the X button."""
    if deal_id.startswith(_SYNTHETIC_PREFIX):
        return {
            "deleted": None,
            "skipped": "synthetic_deal",
            "message": "Remove the underlying contact to drop this from the pipeline.",
        }
    sb = get_db()
    sb.table("crm_deals").delete().eq("id", deal_id).eq("tenant_id", tenant_id).execute()
    return {"deleted": deal_id}


# ── Activities ────────────────────────────────────────────────────────────────

def list_activities(tenant_id: str, contact_id: str = "", limit: int = 30) -> dict:
    sb = get_db()
    query = sb.table("crm_activities").select("*").eq("tenant_id", tenant_id)
    if contact_id:
        query = query.eq("contact_id", contact_id)
    result = query.order("created_at", desc=True).limit(limit).execute()
    return {"activities": result.data or []}


def create_activity(tenant_id: str, data: dict) -> dict:
    sb = get_db()
    row = {"tenant_id": tenant_id, **data}
    result = sb.table("crm_activities").insert(row).execute()
    return {"activity": result.data[0] if result.data else None}


def pipeline_summary(tenant_id: str) -> dict:
    """Stage-keyed counts + total $ value across the unioned deal list.
    Contacts contribute to count but not to $ value (synthetic deals
    have value=0)."""
    deals = _list_deals_union(tenant_id)
    stages: dict[str, dict] = {}
    for deal in deals:
        s = deal.get("stage", "lead")
        if s not in stages:
            stages[s] = {"count": 0, "value": 0}
        stages[s]["count"] += 1
        stages[s]["value"] += deal.get("value", 0) or 0
    return {"stages": stages}
