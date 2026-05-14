"""Centralized Approval Policy — human-in-the-loop enforcement for all critical actions.

Core principle: AI prepares, human verifies, system executes only after approval.

This module provides a single source of truth for which actions require human
approval before execution. Both UI-triggered and agent-triggered flows must
route through these checks.
"""
from __future__ import annotations

import logging
from enum import Enum

logger = logging.getLogger("aria.approval")


class RiskLevel(str, Enum):
    NONE = "none"           # Read-only, internal — no approval needed
    LOW = "low"             # Internal drafts, internal analysis — no approval needed
    MEDIUM = "medium"       # CRM mutations, status changes — confirmation required
    HIGH = "high"           # External sends, publishes, deletes — strict approval required
    CRITICAL = "critical"   # Bulk actions, irreversible mutations — strict approval + preview


class ApprovalStatus(str, Enum):
    DRAFT = "draft"
    GENERATED_FOR_REVIEW = "generated_for_review"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    SCHEDULED = "scheduled"
    EXECUTING = "executing"
    SENT = "sent"
    PUBLISHED = "published"
    COMPLETED = "completed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    FAILED = "failed"


# Statuses that allow execution
EXECUTABLE_STATUSES = {
    ApprovalStatus.APPROVED,
    ApprovalStatus.SCHEDULED,
}

# Statuses that mean "waiting for human"
REVIEW_STATUSES = {
    ApprovalStatus.DRAFT,
    ApprovalStatus.GENERATED_FOR_REVIEW,
    ApprovalStatus.PENDING_APPROVAL,
}


# ── Action Policy Registry ──────────────────────────────────────────────────────
# Maps action categories to their risk level and approval requirements.

ACTION_POLICIES: dict[str, dict] = {
    # ── External Communication (HIGH) ──
    "send_email": {"risk": RiskLevel.HIGH, "approval_required": True, "description": "Send email to external recipient"},
    "send_reply": {"risk": RiskLevel.HIGH, "approval_required": True, "description": "Send email reply"},
    "send_whatsapp": {"risk": RiskLevel.HIGH, "approval_required": True, "description": "Send WhatsApp message"},
    "publish_twitter": {"risk": RiskLevel.HIGH, "approval_required": True, "description": "Publish tweet/thread to X"},
    "publish_linkedin": {"risk": RiskLevel.HIGH, "approval_required": True, "description": "Publish post to LinkedIn"},
    "publish_social": {"risk": RiskLevel.HIGH, "approval_required": True, "description": "Publish to any social platform"},

    # ── Content Finalization (HIGH) ──
    "finalize_report": {"risk": RiskLevel.HIGH, "approval_required": True, "description": "Finalize AI report for external use"},
    "finalize_deliverable": {"risk": RiskLevel.HIGH, "approval_required": True, "description": "Finalize client-facing deliverable"},

    # ── Business Data Mutation (MEDIUM) ──
    "update_record": {"risk": RiskLevel.MEDIUM, "approval_required": True, "description": "Update business record"},
    "delete_record": {"risk": RiskLevel.HIGH, "approval_required": True, "description": "Delete business record"},
    "bulk_change": {"risk": RiskLevel.CRITICAL, "approval_required": True, "description": "Bulk data modification"},

    # ── Scheduling (MEDIUM) ──
    "schedule_send": {"risk": RiskLevel.MEDIUM, "approval_required": True, "description": "Schedule external send/publish"},
    "execute_scheduled": {"risk": RiskLevel.HIGH, "approval_required": True, "description": "Execute scheduled action now"},

    # ── Internal/Safe (NONE/LOW) ──
    "read_data": {"risk": RiskLevel.NONE, "approval_required": False, "description": "Read/list records"},
    "create_draft": {"risk": RiskLevel.LOW, "approval_required": False, "description": "Create internal draft"},
    "analyze": {"risk": RiskLevel.LOW, "approval_required": False, "description": "Internal AI analysis"},
    "sync_data": {"risk": RiskLevel.NONE, "approval_required": False, "description": "Sync/refresh data"},
}


def requires_approval(action: str) -> bool:
    """Check if an action requires human approval before execution."""
    policy = ACTION_POLICIES.get(action)
    if policy:
        return policy["approval_required"]
    # Default: require approval for unknown actions (safe default)
    return True


def check_approval_status(status: str) -> bool:
    """Check if a status allows execution. Returns True if approved/ready."""
    return status in {s.value for s in EXECUTABLE_STATUSES}


def is_review_state(status: str) -> bool:
    """Check if a status is in a human-review state."""
    return status in {s.value for s in REVIEW_STATUSES}


def get_initial_status(risk: RiskLevel) -> str:
    """Get the initial status for a new AI-generated item based on risk level."""
    if risk in (RiskLevel.HIGH, RiskLevel.CRITICAL):
        return ApprovalStatus.PENDING_APPROVAL.value
    if risk == RiskLevel.MEDIUM:
        return ApprovalStatus.GENERATED_FOR_REVIEW.value
    return ApprovalStatus.DRAFT.value


def validate_execution(action: str, current_status: str) -> tuple[bool, str]:
    """Validate whether an action can execute given its current status.

    Returns:
        (allowed, reason) — allowed=True if execution is permitted.
    """
    policy = ACTION_POLICIES.get(action)
    if not policy or not policy["approval_required"]:
        return True, "No approval required"

    if current_status in {s.value for s in EXECUTABLE_STATUSES}:
        return True, "Approved for execution"

    if current_status in {s.value for s in REVIEW_STATUSES}:
        return False, f"Requires human approval. Current status: {current_status}"

    if current_status in (ApprovalStatus.SENT.value, ApprovalStatus.PUBLISHED.value, ApprovalStatus.COMPLETED.value):
        return False, f"Already executed ({current_status})"

    if current_status in (ApprovalStatus.CANCELLED.value, ApprovalStatus.REJECTED.value):
        return False, f"Cannot execute — {current_status}"

    return False, f"Unknown status: {current_status}"
