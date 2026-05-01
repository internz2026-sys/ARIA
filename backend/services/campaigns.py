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
    # Shallow-merge `metadata` — callers (Copy-Paste tab) PATCH only
    # a subset of keys (e.g. {pasted_at, performance_review_at}), and
    # we don't want to clobber unrelated keys the agent may have
    # written (e.g. campaign_objective, projected_budget).
    #
    # Special case: a nested `metadata.performance` dict is itself
    # shallow-merged with the existing performance sub-object so the
    # frontend can PATCH partial metric updates (e.g. {clicks: 100})
    # without wiping previously saved keys (spend, leads, …).
    if "metadata" in updates and isinstance(updates["metadata"], dict):
        try:
            existing = (
                sb.table("campaigns")
                .select("metadata")
                .eq("id", campaign_id)
                .eq("tenant_id", tenant_id)
                .single()
                .execute()
            )
            current = (existing.data or {}).get("metadata") or {}
            if isinstance(current, dict):
                incoming = updates["metadata"]
                # Two-level merge for `performance` only — every other
                # key is treated as an opaque scalar replacement.
                if (
                    isinstance(incoming.get("performance"), dict)
                    and isinstance(current.get("performance"), dict)
                ):
                    merged_perf = {**current["performance"], **incoming["performance"]}
                    incoming = {**incoming, "performance": merged_perf}
                merged = {**current, **incoming}
                updates["metadata"] = merged
        except Exception as e:
            # Lookup failure shouldn't block the PATCH; we just lose
            # the merge and overwrite. Logged for visibility.
            logger.debug("[campaigns] metadata merge fallback to overwrite: %s", e)
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    sb.table("campaigns").update(updates).eq("id", campaign_id).eq("tenant_id", tenant_id).execute()
    return {"updated": campaign_id, "changes": updates}


def update_campaign_metrics(
    tenant_id: str,
    campaign_id: str,
    metrics: dict,
) -> dict | None:
    """Merge user-reported performance metrics into `metadata.performance`.

    `metrics` is an open-ended dict — `{clicks, leads, spend, cpl, ctr,
    cpc, impressions, ...}` — no whitelist, so the frontend can add new
    KPI fields without a backend change. Re-running with the same values
    is a no-op (Postgres treats the JSONB write as identical), so this
    is safe to call from a debounced "Save metrics" button.

    Stores into `metadata.performance` (a sub-object) so we don't pollute
    the top-level metadata namespace where Ad Strategist outputs already
    live (campaign_objective, projected_budget, etc).

    Returns the full updated row, or `None` on DB error / empty input.
    Mirrors the shape used by Coder 1's `UpdateCampaignBody` — callers
    that already go through `PATCH /api/campaigns/{id}` with
    `{metadata: {performance: {...}}}` get the same merge semantics from
    `update_campaign` above. This helper exists for in-process callers
    (CEO actions, scheduler hooks, future analytics ingestion) that
    want a single dedicated entry point.
    """
    if not tenant_id or not campaign_id or not isinstance(metrics, dict):
        return None
    # Drop None values so the frontend can clear a metric by sending
    # `{clicks: null}` if it ever wants to. Empty dict short-circuits.
    cleaned = {k: v for k, v in metrics.items() if v is not None}
    if not cleaned:
        # Idempotent no-op: nothing to merge.
        return get_campaign(tenant_id, campaign_id) or None
    try:
        # Reuse update_campaign so the two-level merge logic stays in
        # one place — this is the helper for in-process callers, the
        # PATCH route lands here too (via update_campaign).
        update_campaign(
            tenant_id,
            campaign_id,
            {"metadata": {"performance": cleaned}},
        )
        row = get_campaign(tenant_id, campaign_id)
        return row or None
    except Exception as e:
        logger.error(
            "[campaigns] update_campaign_metrics failed (tenant=%s campaign=%s): %s",
            tenant_id, campaign_id, e,
        )
        return None


def list_due_performance_reviews(limit: int = 100) -> list[dict]:
    """Return campaigns whose 7-day Copy-Paste reminder is due.

    A row is "due" when:
      - metadata.performance_review_at is set and is in the past, AND
      - metadata.performance_review_fired is NOT truthy

    The supabase-py client doesn't have first-class JSONB-key filters,
    so we pull a small page of candidate rows (those whose metadata is
    non-null) and filter in Python. Campaigns table is small per
    tenant; one query per scheduler tick is fine.

    Used by the scheduler executor loop in server.py to fan reminder
    notifications out to the user's bell. Set
    `metadata.performance_review_fired = true` after firing so we
    don't re-notify.
    """
    sb = get_db()
    try:
        # Pull candidate rows that have at least *some* metadata.
        # We can't `not_.is_("metadata->>performance_review_at", "null")`
        # via supabase-py, so the broad pull + Python filter is the
        # pragmatic shape. `limit` is a guardrail; in practice a tenant
        # rarely has more than a handful of pending reviews at once.
        result = (
            sb.table("campaigns")
            .select("id,tenant_id,campaign_name,metadata")
            .not_.is_("metadata", "null")
            .limit(limit)
            .execute()
        )
    except Exception as e:
        logger.warning("[campaigns] list_due_performance_reviews query failed: %s", e)
        return []
    now_iso = datetime.now(timezone.utc).isoformat()
    due: list[dict] = []
    for row in result.data or []:
        meta = row.get("metadata") or {}
        if not isinstance(meta, dict):
            continue
        review_at = meta.get("performance_review_at")
        if not review_at or not isinstance(review_at, str):
            continue
        if meta.get("performance_review_fired"):
            continue
        # ISO-8601 lex-compares correctly for UTC timestamps the Copy-
        # Paste tab writes, so a string compare is safe and avoids
        # the parsing cost on every scan.
        if review_at > now_iso:
            continue
        due.append(row)
    return due


def mark_performance_review_fired(tenant_id: str, campaign_id: str) -> bool:
    """Flag a campaign as 'reminder already sent' so we don't re-notify.

    Sets `metadata.performance_review_fired = true` (merged into
    existing metadata via update_campaign's shallow-merge). Returns
    True on success, False on DB error — callers should log but not
    raise, since a missed flag at worst causes a duplicate
    notification on the next scheduler tick.
    """
    try:
        update_campaign(
            tenant_id,
            campaign_id,
            {"metadata": {"performance_review_fired": True}},
        )
        return True
    except Exception as e:
        logger.warning(
            "[campaigns] mark_performance_review_fired failed (tenant=%s id=%s): %s",
            tenant_id, campaign_id, e,
        )
        return False


def list_recent_campaigns_with_metrics(
    tenant_id: str,
    *,
    source_type: str = "ad_strategist",
    limit: int = 3,
) -> list[dict]:
    """Return the most recent campaigns for `tenant_id` that have
    `metadata.performance` populated. Used by the Ad Strategist's
    Past Performance Context prompt block to close the feedback loop.

    Filter is done in Python because supabase-py doesn't expose a
    first-class JSONB-key-presence filter. We pull a slightly larger
    page than `limit` and stop once we've collected `limit` rows that
    actually have metrics.
    """
    if not tenant_id:
        return []
    sb = get_db()
    try:
        result = (
            sb.table("campaigns")
            .select("id,campaign_name,metadata,created_at")
            .eq("tenant_id", tenant_id)
            .eq("source_type", source_type)
            .order("created_at", desc=True)
            .limit(max(limit * 4, 12))
            .execute()
        )
    except Exception as e:
        logger.warning(
            "[campaigns] list_recent_campaigns_with_metrics query failed: %s", e,
        )
        return []
    out: list[dict] = []
    for row in result.data or []:
        meta = row.get("metadata") or {}
        if not isinstance(meta, dict):
            continue
        perf = meta.get("performance")
        if not isinstance(perf, dict) or not perf:
            continue
        out.append(row)
        if len(out) >= limit:
            break
    return out


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


# ─── Inbox -> Campaigns Mirror ──────────────────────────────────────────────────
#
# When the Ad Strategist finalizes a deliverable, the inbox CREATE
# handler mirrors the row into the campaigns table so the Campaigns
# page Copy-Paste tab can surface it as a queued draft. Default status
# is "draft" — the tab flips it to "active" once the user clicks
# "I've pasted this into Ads Manager".


def create_campaign_from_inbox(
    tenant_id: str,
    *,
    inbox_item_id: str,
    task_id: str | None = None,
    title: str,
    status: str = "draft",
    metadata: dict | None = None,
) -> dict | None:
    """Insert a campaigns row mirroring a finalized inbox deliverable.

    Idempotent: if a campaigns row already exists for `inbox_item_id`
    we return the existing row instead of inserting a duplicate. The
    partial unique index on (inbox_item_id) is the backstop, but we
    check first to avoid noisy 23505 conflicts when retries / dual
    paths (skill-curl + watcher placeholder) fire.

    Returns the row dict, or None on DB error (logged, never raised —
    the agent's deliverable is already in the inbox + Projects, so a
    Campaigns mirror failure must not break the user-visible flow).
    """
    if not tenant_id or not inbox_item_id:
        logger.warning("[campaigns] create_campaign_from_inbox: missing tenant_id or inbox_item_id")
        return None
    try:
        sb = get_db()
        # Idempotency: skip if a campaigns row already references this inbox item
        try:
            existing = (
                sb.table("campaigns")
                .select("*")
                .eq("tenant_id", tenant_id)
                .eq("inbox_item_id", inbox_item_id)
                .limit(1)
                .execute()
            )
            if existing.data:
                logger.info(
                    "[campaigns] row already exists for inbox %s — skipping insert",
                    inbox_item_id,
                )
                return existing.data[0]
        except Exception as e:
            # Column missing on unmigrated DB — fall through to insert;
            # the unique index will reject duplicates if it exists,
            # otherwise we accept the small dup risk.
            logger.debug("[campaigns] idempotency lookup skipped: %s", e)

        meta = metadata or {}
        row: dict[str, Any] = {
            "tenant_id": tenant_id,
            "campaign_name": (title or "Untitled Campaign")[:200],
            "platform": "facebook",
            "objective": (meta.get("campaign_objective") or "")[:120],
            "status": status,
            "source_type": "ad_strategist",
            "inbox_item_id": inbox_item_id,
            "metadata": meta,
        }
        if task_id:
            row["task_id"] = task_id
        # Strip empty strings so column defaults can kick in
        row = {k: v for k, v in row.items() if v not in (None, "")}

        result = sb.table("campaigns").insert(row).execute()
        created = result.data[0] if result.data else None
        if created:
            logger.info(
                "[campaigns] mirrored inbox %s -> campaign %s (task=%s)",
                inbox_item_id, created.get("id"), task_id,
            )
        return created
    except Exception as e:
        # 23505 = duplicate key on the partial unique index — race
        # with a concurrent path. Re-fetch and return the winner so
        # the caller still gets a row back.
        msg = str(e)
        if "23505" in msg or "duplicate key" in msg.lower():
            try:
                sb = get_db()
                row_lookup = (
                    sb.table("campaigns")
                    .select("*")
                    .eq("tenant_id", tenant_id)
                    .eq("inbox_item_id", inbox_item_id)
                    .limit(1)
                    .execute()
                )
                if row_lookup.data:
                    logger.info("[campaigns] insert race resolved — returning existing row")
                    return row_lookup.data[0]
            except Exception:
                pass
        logger.error("[campaigns] create_campaign_from_inbox failed: %s", e)
        return None
