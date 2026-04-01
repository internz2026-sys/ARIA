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

def list_deals(tenant_id: str, stage: str = "") -> dict:
    sb = get_db()
    query = sb.table("crm_deals").select("*").eq("tenant_id", tenant_id)
    if stage:
        query = query.eq("stage", stage)
    result = query.order("created_at", desc=True).execute()
    return {"deals": result.data or []}


def get_deal(tenant_id: str, deal_id: str) -> dict:
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
    sb = get_db()
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    sb.table("crm_deals").update(updates).eq("id", deal_id).eq("tenant_id", tenant_id).execute()
    return {"updated": deal_id, "changes": updates}


def delete_deal(tenant_id: str, deal_id: str) -> dict:
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
    sb = get_db()
    result = sb.table("crm_deals").select("stage,value").eq("tenant_id", tenant_id).execute()
    stages: dict[str, dict] = {}
    for deal in (result.data or []):
        s = deal.get("stage", "lead")
        if s not in stages:
            stages[s] = {"count": 0, "value": 0}
        stages[s]["count"] += 1
        stages[s]["value"] += deal.get("value", 0)
    return {"stages": stages}
