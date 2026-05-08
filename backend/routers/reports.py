"""Reports Router — endpoints for the Reports tab.

Surface:
  GET    /api/reports/{tenant_id}             list reports (newest first)
  POST   /api/reports/{tenant_id}/generate    generate a new report
  GET    /api/reports/{tenant_id}/{id}        get one report
  DELETE /api/reports/{tenant_id}/{id}        delete one report

The generate endpoint dispatches by `report_type` query/body param —
default is `state_of_union`, which is what the big Generate button on
the Reports tab fires. Cheaper variants (`agent_productivity`) skip the
LLM call.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.auth import get_verified_tenant
from backend.services import reports as reports_service

logger = logging.getLogger("aria.routers.reports")

router = APIRouter(
    prefix="/api/reports/{tenant_id}",
    tags=["Reports"],
    dependencies=[Depends(get_verified_tenant)],
)


class GenerateReportRequest(BaseModel):
    """Body for POST /generate. `report_type` defaults to state_of_union
    when omitted so the most common UI case is a zero-arg button click."""
    report_type: str = "state_of_union"


@router.get("")
async def list_reports(tenant_id: str, limit: int = 50):
    return reports_service.list_reports(tenant_id, limit)


@router.post("/generate")
async def generate_report(tenant_id: str, body: GenerateReportRequest = GenerateReportRequest()):
    rt = (body.report_type or "state_of_union").strip().lower()
    try:
        if rt == "state_of_union":
            row = await reports_service.generate_state_of_union(tenant_id)
        elif rt == "agent_productivity":
            row = await reports_service.generate_agent_productivity(tenant_id)
        elif rt == "campaign_roi":
            from backend.services.reports_campaign_roi import generate_campaign_roi
            row = await generate_campaign_roi(tenant_id)
        elif rt == "channel_spend":
            from backend.services.reports_channel_spend import generate_channel_spend
            row = await generate_channel_spend(tenant_id)
        elif rt == "daily_pulse":
            from backend.services.reports_daily_pulse import generate_daily_pulse
            row = await generate_daily_pulse(tenant_id)
        else:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Unknown report_type '{rt}'. Supported: state_of_union, "
                    "agent_productivity, campaign_roi, channel_spend, daily_pulse."
                ),
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("[reports] generate %s failed: %s", rt, e)
        from backend.services.safe_errors import safe_detail
        raise HTTPException(status_code=500, detail=safe_detail(e, "Report generation failed"))
    return {"report": row}


@router.get("/{report_id}")
async def get_report(tenant_id: str, report_id: str):
    row = reports_service.get_report(tenant_id, report_id)
    if not row:
        raise HTTPException(status_code=404, detail="Report not found")
    return {"report": row}


@router.delete("/{report_id}")
async def delete_report(tenant_id: str, report_id: str):
    # Pre-check so the 404 path is explicit instead of a silent 200 on
    # an already-deleted (or wrong-tenant) row. `get_report` already
    # returns None on miss / tenant mismatch — no need to re-implement.
    if not reports_service.get_report(tenant_id, report_id):
        raise HTTPException(status_code=404, detail="Report not found")
    try:
        result = reports_service.delete_report(tenant_id, report_id)
    except Exception as e:
        logger.exception("[reports] delete %s failed: %s", report_id, e)
        from backend.services.safe_errors import safe_detail
        raise HTTPException(status_code=500, detail=safe_detail(e, "Report delete failed"))
    # Race: row vanished between the get_report check and the delete.
    # Treat as 404 so the client retries cleanly instead of seeing a 200
    # that didn't actually do anything.
    if result.get("found") is False:
        raise HTTPException(status_code=404, detail="Report not found")
    return result
