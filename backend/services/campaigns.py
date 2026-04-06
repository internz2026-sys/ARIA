"""Campaign Service — CRUD for campaigns and campaign reports.

Handles campaign management, report association, and metric queries.
Used by API endpoints and CEO actions.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from backend.services.supabase import get_db

logger = logging.getLogger("aria.campaigns")


# ─── Campaign CRUD ──────────────────────────────────────────────────────────────

def list_campaigns(
    tenant_id: str,
    status: str = "",
    platform: str = "",
    page: int = 1,
    page_size: int = 50,
) -> dict:
    sb = get_db()
    query = sb.table("campaigns").select("*", count="exact").eq("tenant_id", tenant_id)
    if status:
        query = query.eq("status", status)
    if platform:
        query = query.eq("platform", platform)
    offset = (max(page, 1) - 1) * page_size
    result = query.order("created_at", desc=True).range(offset, offset + page_size - 1).execute()
    total = result.count if result.count is not None else len(result.data or [])
    return {"campaigns": result.data or [], "total": total, "page": page, "page_size": page_size}


def get_campaign(tenant_id: str, campaign_id: str) -> dict:
    sb = get_db()
    result = sb.table("campaigns").select("*").eq("id", campaign_id).eq("tenant_id", tenant_id).single().execute()
    return result.data or {}


def create_campaign(tenant_id: str, data: dict) -> dict:
    sb = get_db()
    row = {
        "tenant_id": tenant_id,
        "campaign_name": data.get("campaign_name", "Untitled Campaign"),
        "platform": data.get("platform", "facebook"),
        "objective": data.get("objective", ""),
        "status": data.get("status", "active"),
        "budget": data.get("budget"),
        "notes": data.get("notes", ""),
        "tags": data.get("tags", []),
        "date_range_start": data.get("date_range_start"),
        "date_range_end": data.get("date_range_end"),
        "campaign_external_id": data.get("campaign_external_id"),
        "campaign_external_name": data.get("campaign_external_name"),
        "source_type": data.get("source_type", "manual_upload"),
    }
    # Remove None values so Supabase uses defaults
    row = {k: v for k, v in row.items() if v is not None}
    result = sb.table("campaigns").insert(row).execute()
    campaign = result.data[0] if result.data else None
    return {"campaign": campaign}


def update_campaign(tenant_id: str, campaign_id: str, updates: dict) -> dict:
    sb = get_db()
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    sb.table("campaigns").update(updates).eq("id", campaign_id).eq("tenant_id", tenant_id).execute()
    return {"updated": campaign_id, "changes": updates}


def delete_campaign(tenant_id: str, campaign_id: str) -> dict:
    sb = get_db()
    # Reports are cascade-deleted via FK
    sb.table("campaigns").delete().eq("id", campaign_id).eq("tenant_id", tenant_id).execute()
    return {"deleted": campaign_id}


# ─── Campaign Report CRUD ───────────────────────────────────────────────────────

def list_reports(tenant_id: str, campaign_id: str = "", page: int = 1, page_size: int = 20) -> dict:
    sb = get_db()
    query = sb.table("campaign_reports").select("*", count="exact").eq("tenant_id", tenant_id)
    if campaign_id:
        query = query.eq("campaign_id", campaign_id)
    offset = (max(page, 1) - 1) * page_size
    result = query.order("uploaded_at", desc=True).range(offset, offset + page_size - 1).execute()
    total = result.count if result.count is not None else len(result.data or [])
    return {"reports": result.data or [], "total": total, "page": page, "page_size": page_size}


def get_report(tenant_id: str, report_id: str) -> dict:
    sb = get_db()
    result = sb.table("campaign_reports").select("*").eq("id", report_id).eq("tenant_id", tenant_id).single().execute()
    return result.data or {}


def create_report(
    tenant_id: str,
    campaign_id: str,
    source_file_name: str,
    raw_metrics: dict,
    report_start_date: str | None = None,
    report_end_date: str | None = None,
) -> dict:
    sb = get_db()
    row = {
        "tenant_id": tenant_id,
        "campaign_id": campaign_id,
        "source_file_name": source_file_name,
        "source_type": "manual_upload",
        "raw_metrics_json": raw_metrics,
        "parsed_status": "parsed",
        "ai_summary_status": "pending",
    }
    if report_start_date:
        row["report_start_date"] = report_start_date
    if report_end_date:
        row["report_end_date"] = report_end_date

    result = sb.table("campaign_reports").insert(row).execute()
    report = result.data[0] if result.data else None

    # Update campaign's latest report pointer
    if report:
        sb.table("campaigns").update({
            "latest_report_id": report["id"],
            "latest_report_date": report["uploaded_at"],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", campaign_id).eq("tenant_id", tenant_id).execute()

    return {"report": report}


def update_report(tenant_id: str, report_id: str, updates: dict) -> dict:
    sb = get_db()
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    sb.table("campaign_reports").update(updates).eq("id", report_id).eq("tenant_id", tenant_id).execute()
    return {"updated": report_id}


def delete_report(tenant_id: str, report_id: str) -> dict:
    sb = get_db()
    sb.table("campaign_reports").delete().eq("id", report_id).eq("tenant_id", tenant_id).execute()
    return {"deleted": report_id}


# ─── Campaign Matching ──────────────────────────────────────────────────────────

def find_matching_campaigns(tenant_id: str, campaign_name: str) -> list[dict]:
    """Find campaigns that match a name from an uploaded report (single query)."""
    sb = get_db()
    # Partial ilike covers both exact and fuzzy matches in one query
    result = sb.table("campaigns").select("id,campaign_name,platform,status").eq(
        "tenant_id", tenant_id
    ).ilike("campaign_name", f"%{campaign_name[:30]}%").limit(10).execute()
    return result.data or []


def get_latest_report(tenant_id: str, campaign_id: str) -> dict:
    """Get the most recent report for a campaign."""
    sb = get_db()
    result = sb.table("campaign_reports").select("*").eq(
        "campaign_id", campaign_id
    ).eq("tenant_id", tenant_id).order("uploaded_at", desc=True).limit(1).execute()
    return result.data[0] if result.data else {}


def get_campaign_with_latest_report(tenant_id: str, campaign_id: str) -> dict:
    """Get campaign details with its latest report data."""
    campaign = get_campaign(tenant_id, campaign_id)
    if not campaign:
        return {}
    latest = get_latest_report(tenant_id, campaign_id)
    campaign["latest_report"] = latest
    return campaign
