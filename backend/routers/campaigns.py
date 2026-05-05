"""Campaign API Router — campaign CRUD, report upload, AI analysis."""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from backend.auth import get_verified_tenant
from backend.services import campaigns as campaign_service
from backend.tools.fb_ads_parser import parse_csv

logger = logging.getLogger("aria.api.campaigns")

# Every route under /api/campaigns/ takes {tenant_id} as the first path
# segment, so router-level get_verified_tenant covers the whole surface
# in one shot. Closes IDOR class for campaigns CRUD + CSV uploads.
router = APIRouter(
    prefix="/api/campaigns",
    tags=["campaigns"],
    dependencies=[Depends(get_verified_tenant)],
)


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


async def _auto_generate_overview_charts(tenant_id: str, report_id: str):
    """Background task: deterministically generate Overview-tab charts
    from the freshly-uploaded report's parsed metrics. Runs separately
    from the AI report (which is text-only narrative now); this one is
    pure data → matplotlib → Supabase storage, no Haiku call.

    Mutates the report row's raw_metrics_json by adding a `charts`
    array of `{type, title, url}` dicts. Frontend Overview tab reads
    that array and renders each chart as a <figure>. Best-effort:
    DB / matplotlib / storage hiccups silently log and leave the
    `charts` field absent (the Overview falls back to metric tiles
    only).
    """
    try:
        report = campaign_service.get_report(tenant_id, report_id)
        if not report:
            logger.warning("Overview charts skipped — report not found: %s", report_id)
            return
        raw_metrics = report.get("raw_metrics_json") or {}
        if not isinstance(raw_metrics, dict):
            return

        from backend.services.visualizer import generate_overview_charts_from_metrics
        # Run matplotlib synchronously inside this task's event loop —
        # it's CPU-bound but fast (a few hundred ms per chart) and
        # already off the request hot path since the whole helper is
        # a background task fired post-response.
        charts = generate_overview_charts_from_metrics(tenant_id, raw_metrics)
        if not charts:
            return

        # Re-fetch raw_metrics_json in case create_report mutated it
        # in-flight, then merge the new charts list and persist.
        merged = dict(raw_metrics)
        merged["charts"] = charts
        campaign_service.update_report(tenant_id, report_id, {
            "raw_metrics_json": merged,
        })
        logger.info(
            "Overview charts generated for report %s — %d chart(s)",
            report_id, len(charts),
        )
    except Exception as e:
        logger.error("Overview chart generation failed for report %s: %s", report_id, e)


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
    # Free-form JSONB. Used by the Copy-Paste tab to record
    # `pasted_at` / `performance_review_at` so the user's "I have
    # pasted" click survives a refresh without leaning on
    # localStorage. Values are merged shallow at the service layer
    # (see update_campaign).
    metadata: Optional[dict] = None


# Canonical campaign status set. Anything outside this is rejected from PATCH so
# typos / mis-cased strings (e.g. "Active", "live", "running") can't land in the
# DB and leak a campaign past whichever filter ("active"/"completed") the rest
# of the app uses. Same idiom as _VALID_INBOX_STATUSES in routers/inbox.py.
_VALID_CAMPAIGN_STATUSES = frozenset({
    "draft",
    "active",
    "paused",
    "completed",
    "archived",
})


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
    # Status whitelist + normalization. Lowercase + strip so "Active" /
    # "  draft  " all land canonically; reject anything outside the
    # canonical set with a 400 so a buggy caller can't silently poison
    # the row (e.g. "live" / "running" / "paused" mis-spellings break
    # the campaigns-list status filter forever). Same pattern as the
    # inbox PATCH validator in backend/routers/inbox.py.
    if "status" in updates:
        raw = updates["status"]
        if not isinstance(raw, str):
            raise HTTPException(400, "status must be a string")
        normalized = raw.strip().lower()
        if normalized not in _VALID_CAMPAIGN_STATUSES:
            raise HTTPException(
                400,
                f"invalid status {raw!r}; must be one of "
                f"{sorted(_VALID_CAMPAIGN_STATUSES)}",
            )
        updates["status"] = normalized
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

# Hard cap on uploaded CSV size. Anything larger is almost certainly an
# attack or a misconfigured export — Facebook Ads reports for a single
# account at the lifetime level rarely exceed a few MB. The cap is
# enforced via a streaming bounded reader so a multi-GB upload is
# rejected on first chunk-overflow rather than after the full body has
# been buffered in memory (the original `await file.read()` then
# size-check pattern still OOMs the container before the check fires).
_CSV_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
_CSV_READ_CHUNK = 64 * 1024


async def _read_upload_bounded(file: "UploadFile", limit: int = _CSV_MAX_BYTES) -> bytes:
    """Read an UploadFile in chunks, raising 413 the moment we cross
    `limit` bytes. Returns the full content on success."""
    parts: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_CSV_READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise HTTPException(
                status_code=413,
                detail=f"File too large. Maximum {limit // (1024 * 1024)}MB.",
            )
        parts.append(chunk)
    return b"".join(parts)


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

    content = await _read_upload_bounded(file)
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

    # Auto-generate AI report (text narrative) AND Overview charts
    # (deterministic visualizations) in the background, in parallel —
    # neither blocks the upload response.
    report = report_result.get("report")
    if report:
        asyncio.create_task(_auto_generate_ai_report(tenant_id, report["id"], campaign_id))
        asyncio.create_task(_auto_generate_overview_charts(tenant_id, report["id"]))

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

    content = await _read_upload_bounded(file)
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

    # Auto-generate AI report (text narrative) AND Overview charts
    # (deterministic visualizations) in the background, in parallel.
    report = report_result.get("report")
    if report:
        asyncio.create_task(_auto_generate_ai_report(tenant_id, report["id"], campaign["id"]))
        asyncio.create_task(_auto_generate_overview_charts(tenant_id, report["id"]))

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
        from backend.services.safe_errors import safe_detail
        raise HTTPException(500, safe_detail(e, "AI analysis failed"))
