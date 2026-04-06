"""Campaign API Router — campaign CRUD, report upload, AI analysis."""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from backend.services import campaigns as campaign_service
from backend.tools.fb_ads_parser import parse_csv

logger = logging.getLogger("aria.api.campaigns")

router = APIRouter(prefix="/api/campaigns", tags=["campaigns"])


def _extract_report_context(parsed: dict) -> tuple[dict, str | None, str | None]:
    """Extract metrics, date_start, date_end from parsed CSV data. Shared by upload endpoints."""
    metrics = parsed["totals"] if len(parsed["campaigns"]) > 1 else (parsed["campaigns"][0]["metrics"] if parsed["campaigns"] else {})
    date_start = parsed["campaigns"][0].get("date_start") if parsed["campaigns"] else None
    date_end = parsed["campaigns"][0].get("date_end") if parsed["campaigns"] else None
    return metrics, date_start, date_end


async def _auto_generate_ai_report(tenant_id: str, report_id: str, campaign_id: str):
    """Background task: automatically generate an AI report after upload.

    Runs after the upload response is returned so the user doesn't wait.
    The report is saved to the DB and visible on the campaign detail page.
    """
    try:
        report = campaign_service.get_report(tenant_id, report_id)
        campaign = campaign_service.get_campaign(tenant_id, campaign_id)
        if not report or not campaign:
            logger.warning("Auto AI report skipped — report or campaign not found: %s", report_id)
            return

        campaign_service.update_report(tenant_id, report_id, {"ai_summary_status": "generating"})

        from backend.tools.campaign_analyzer import analyze_report
        ai_result = await analyze_report(tenant_id, campaign, report)

        campaign_service.update_report(tenant_id, report_id, {
            "ai_summary_status": "completed",
            "ai_report_text": ai_result.get("report_text", ""),
            "ai_recommendations": ai_result.get("recommendations", ""),
        })
        logger.info("Auto AI report generated for report %s (campaign %s)", report_id, campaign.get("campaign_name", ""))

    except Exception as e:
        logger.error("Auto AI report generation failed for report %s: %s", report_id, e)
        try:
            campaign_service.update_report(tenant_id, report_id, {"ai_summary_status": "failed"})
        except Exception:
            pass


# ── Request models ──────────────────────────────────────────────────────────────

class CreateCampaignBody(BaseModel):
    campaign_name: str
    platform: str = "facebook"
    objective: str = ""
    status: str = "active"
    budget: Optional[float] = None
    notes: str = ""
    tags: list[str] = []
    date_range_start: Optional[str] = None
    date_range_end: Optional[str] = None


class UpdateCampaignBody(BaseModel):
    campaign_name: Optional[str] = None
    platform: Optional[str] = None
    objective: Optional[str] = None
    status: Optional[str] = None
    budget: Optional[float] = None
    notes: Optional[str] = None
    tags: Optional[list[str]] = None
    date_range_start: Optional[str] = None
    date_range_end: Optional[str] = None


# ── Campaign CRUD ───────────────────────────────────────────────────────────────

@router.get("/{tenant_id}")
async def list_campaigns(
    tenant_id: str,
    status: str = "",
    platform: str = "",
    page: int = 1,
    page_size: int = 50,
):
    return campaign_service.list_campaigns(tenant_id, status=status, platform=platform, page=page, page_size=page_size)


@router.get("/{tenant_id}/{campaign_id}")
async def get_campaign(tenant_id: str, campaign_id: str):
    result = campaign_service.get_campaign_with_latest_report(tenant_id, campaign_id)
    if not result:
        raise HTTPException(404, "Campaign not found")
    return result


@router.post("/{tenant_id}")
async def create_campaign(tenant_id: str, body: CreateCampaignBody):
    return campaign_service.create_campaign(tenant_id, body.model_dump(exclude_none=True))


@router.patch("/{tenant_id}/{campaign_id}")
async def update_campaign(tenant_id: str, campaign_id: str, body: UpdateCampaignBody):
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(400, "No fields to update")
    return campaign_service.update_campaign(tenant_id, campaign_id, updates)


@router.delete("/{tenant_id}/{campaign_id}")
async def delete_campaign(tenant_id: str, campaign_id: str):
    return campaign_service.delete_campaign(tenant_id, campaign_id)


# ── Campaign Reports ────────────────────────────────────────────────────────────

@router.get("/{tenant_id}/{campaign_id}/reports")
async def list_reports(tenant_id: str, campaign_id: str, page: int = 1, page_size: int = 20):
    return campaign_service.list_reports(tenant_id, campaign_id=campaign_id, page=page, page_size=page_size)


@router.get("/{tenant_id}/reports/{report_id}")
async def get_report(tenant_id: str, report_id: str):
    result = campaign_service.get_report(tenant_id, report_id)
    if not result:
        raise HTTPException(404, "Report not found")
    return result


# ── CSV Upload + Parse ──────────────────────────────────────────────────────────

@router.post("/{tenant_id}/upload")
async def upload_report(
    tenant_id: str,
    file: UploadFile = File(...),
    campaign_id: Optional[str] = Form(None),
):
    """Upload a Facebook Ads CSV report.

    If campaign_id is provided, links to that campaign.
    If not, returns parsed data with suggested campaign matches so the user can choose.
    """
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(400, "Only CSV files are supported. Please export your Facebook Ads report as CSV.")

    content = await file.read()
    if len(content) > 10 * 1024 * 1024:  # 10MB limit
        raise HTTPException(400, "File too large. Maximum 10MB.")

    parsed = parse_csv(content)
    if not parsed["success"]:
        raise HTTPException(400, parsed.get("error", "Failed to parse CSV"))

    # If no campaign_id, return parsed data with suggestions
    if not campaign_id:
        suggestions = []
        for camp_data in parsed["campaigns"]:
            matches = campaign_service.find_matching_campaigns(tenant_id, camp_data["campaign_name"])
            suggestions.append({
                "parsed_campaign_name": camp_data["campaign_name"],
                "metrics": camp_data["metrics"],
                "date_start": camp_data.get("date_start"),
                "date_end": camp_data.get("date_end"),
                "rows": camp_data["rows"],
                "matching_campaigns": matches,
            })

        return {
            "status": "needs_association",
            "file_name": file.filename,
            "parsed": parsed,
            "suggestions": suggestions,
        }

    # Campaign ID provided — create the report
    metrics, date_start, date_end = _extract_report_context(parsed)

    report_result = campaign_service.create_report(
        tenant_id=tenant_id,
        campaign_id=campaign_id,
        source_file_name=file.filename or "report.csv",
        raw_metrics={"campaigns": parsed["campaigns"], "totals": parsed["totals"]},
        report_start_date=date_start,
        report_end_date=date_end,
    )

    # Auto-generate AI report in the background
    report = report_result.get("report")
    if report:
        asyncio.create_task(_auto_generate_ai_report(tenant_id, report["id"], campaign_id))

    return {
        "status": "uploaded",
        "report": report,
        "parsed_summary": parsed["totals"],
    }


@router.post("/{tenant_id}/upload-and-create")
async def upload_and_create_campaign(
    tenant_id: str,
    file: UploadFile = File(...),
    campaign_name: str = Form(""),
    platform: str = Form("facebook"),
    objective: str = Form(""),
):
    """Upload a report AND create a new campaign from it in one step."""
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(400, "Only CSV files are supported.")

    content = await file.read()
    parsed = parse_csv(content)
    if not parsed["success"]:
        raise HTTPException(400, parsed.get("error", "Failed to parse CSV"))

    # Use parsed campaign name if not provided
    if not campaign_name and parsed["campaigns"]:
        campaign_name = parsed["campaigns"][0]["campaign_name"]
    if not campaign_name:
        campaign_name = file.filename.replace(".csv", "")

    metrics, date_start, date_end = _extract_report_context(parsed)

    # Create campaign
    camp_result = campaign_service.create_campaign(tenant_id, {
        "campaign_name": campaign_name,
        "platform": platform,
        "objective": objective,
        "date_range_start": date_start,
        "date_range_end": date_end,
        "source_type": "manual_upload",
    })
    campaign = camp_result.get("campaign")
    if not campaign:
        raise HTTPException(500, "Failed to create campaign")

    # Create report
    report_result = campaign_service.create_report(
        tenant_id=tenant_id,
        campaign_id=campaign["id"],
        source_file_name=file.filename or "report.csv",
        raw_metrics={"campaigns": parsed["campaigns"], "totals": parsed["totals"]},
        report_start_date=date_start,
        report_end_date=date_end,
    )

    # Auto-generate AI report in the background
    report = report_result.get("report")
    if report:
        asyncio.create_task(_auto_generate_ai_report(tenant_id, report["id"], campaign["id"]))

    return {
        "status": "created",
        "campaign": campaign,
        "report": report,
        "parsed_summary": parsed["totals"],
    }


# ── AI Report Generation ───────────────────────────────────────────────────────

@router.post("/{tenant_id}/reports/{report_id}/generate-ai-report")
async def generate_ai_report(tenant_id: str, report_id: str):
    """Trigger AI analysis of a campaign report via the Ad Strategist agent."""
    report = campaign_service.get_report(tenant_id, report_id)
    if not report:
        raise HTTPException(404, "Report not found")

    campaign = campaign_service.get_campaign(tenant_id, report["campaign_id"])
    if not campaign:
        raise HTTPException(404, "Campaign not found")

    # Mark as generating
    campaign_service.update_report(tenant_id, report_id, {"ai_summary_status": "generating"})

    try:
        from backend.tools.campaign_analyzer import analyze_report
        ai_result = await analyze_report(tenant_id, campaign, report)

        campaign_service.update_report(tenant_id, report_id, {
            "ai_summary_status": "completed",
            "ai_report_text": ai_result.get("report_text", ""),
            "ai_recommendations": ai_result.get("recommendations", ""),
        })

        return {
            "status": "completed",
            "report_id": report_id,
            "ai_report_text": ai_result.get("report_text", ""),
            "ai_recommendations": ai_result.get("recommendations", ""),
        }

    except Exception as e:
        logger.error("AI report generation failed for report %s: %s", report_id, e)
        campaign_service.update_report(tenant_id, report_id, {"ai_summary_status": "failed"})
        raise HTTPException(500, f"AI analysis failed: {str(e)}")
