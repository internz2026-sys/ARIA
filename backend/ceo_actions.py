"""CEO Action Registry — allowlisted business actions the CEO Agent can perform.

The CEO Agent is a business operations orchestrator, NOT a developer agent.
It can CRUD business records and trigger approved workflows, but cannot
modify codebase, prompts, backend logic, database schema, or infrastructure.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("aria.ceo_actions")

# ─── Forbidden patterns — CEO must refuse these ──────────────────────────────

FORBIDDEN_PATTERNS = [
    "change the code", "modify the code", "edit the code", "update the code",
    "change the backend", "modify the backend", "edit the backend",
    "change the prompt", "modify the prompt", "edit the prompt", "update the prompt",
    "change the api", "modify the api", "patch the api",
    "change the database schema", "modify the schema", "alter the table",
    "change the config", "modify deployment", "edit infrastructure",
    "change the server", "patch the server", "update the codebase",
    "edit source", "modify source file", "change source code",
    "remove approvals", "bypass approval", "skip confirmation",
    "change environment variable", "edit .env", "modify secrets",
    "run migration", "alter column", "drop table", "raw sql",
    "change ci/cd", "modify pipeline", "edit dockerfile",
    "rewrite the agent", "change agent logic", "modify agent behavior",
]

REFUSAL_MESSAGE = (
    "I can help you operate the business — create leads, update records, publish posts, "
    "send emails, and manage workflows. However, I don't have access to modify the codebase, "
    "backend logic, database schema, prompts, or infrastructure. Those changes need to be made "
    "by a developer. Is there a business action I can help you with instead?"
)


def is_forbidden_request(message: str) -> bool:
    """Check if a user message is asking the CEO to do something forbidden."""
    msg_lower = message.lower()
    return any(pattern in msg_lower for pattern in FORBIDDEN_PATTERNS)


# ─── Confirmation levels ──────────────────────────────────────────────────────

class ConfirmLevel:
    NONE = "none"           # No confirmation needed (READ, simple CREATE)
    RECOMMENDED = "recommended"  # Show confirmation but allow skip
    REQUIRED = "required"   # Must confirm before execution


# ─── Action Registry ──────────────────────────────────────────────────────────

ACTION_REGISTRY: dict[str, dict] = {
    # ── CRM: Contacts ──
    "create_contact": {
        "entity": "crm_contact",
        "operation": "create",
        "description": "Create a new CRM contact/lead",
        "required_fields": ["name"],
        "optional_fields": ["email", "phone", "company_id", "source", "status", "tags", "notes"],
        "confirm": ConfirmLevel.NONE,
        "risk": "low",
    },
    "read_contacts": {
        "entity": "crm_contact",
        "operation": "read",
        "description": "List or search CRM contacts",
        "required_fields": [],
        "optional_fields": ["search", "status"],
        "confirm": ConfirmLevel.NONE,
        "risk": "none",
    },
    "update_contact": {
        "entity": "crm_contact",
        "operation": "update",
        "description": "Update a CRM contact's information",
        "required_fields": ["id"],
        "optional_fields": ["name", "email", "phone", "status", "tags", "notes"],
        "confirm": ConfirmLevel.REQUIRED,
        "risk": "medium",
    },
    "delete_contact": {
        "entity": "crm_contact",
        "operation": "delete",
        "description": "Permanently delete a CRM contact",
        "required_fields": ["id"],
        "optional_fields": [],
        "confirm": ConfirmLevel.REQUIRED,
        "risk": "high",
    },

    # ── CRM: Companies ──
    "create_company": {
        "entity": "crm_company",
        "operation": "create",
        "description": "Create a new company record",
        "required_fields": ["name"],
        "optional_fields": ["domain", "industry", "size", "notes"],
        "confirm": ConfirmLevel.NONE,
        "risk": "low",
    },
    "update_company": {
        "entity": "crm_company",
        "operation": "update",
        "description": "Update a company record",
        "required_fields": ["id"],
        "optional_fields": ["name", "domain", "industry", "size", "notes"],
        "confirm": ConfirmLevel.REQUIRED,
        "risk": "medium",
    },
    "delete_company": {
        "entity": "crm_company",
        "operation": "delete",
        "description": "Permanently delete a company record",
        "required_fields": ["id"],
        "optional_fields": [],
        "confirm": ConfirmLevel.REQUIRED,
        "risk": "high",
    },

    # ── CRM: Deals ──
    "create_deal": {
        "entity": "crm_deal",
        "operation": "create",
        "description": "Create a new deal in the pipeline",
        "required_fields": ["title"],
        "optional_fields": ["value", "stage", "contact_id", "company_id", "notes", "expected_close"],
        "confirm": ConfirmLevel.NONE,
        "risk": "low",
    },
    "update_deal": {
        "entity": "crm_deal",
        "operation": "update",
        "description": "Update a deal's information or stage",
        "required_fields": ["id"],
        "optional_fields": ["title", "value", "stage", "contact_id", "notes", "expected_close"],
        "confirm": ConfirmLevel.REQUIRED,
        "risk": "medium",
    },
    "delete_deal": {
        "entity": "crm_deal",
        "operation": "delete",
        "description": "Permanently delete a deal",
        "required_fields": ["id"],
        "optional_fields": [],
        "confirm": ConfirmLevel.REQUIRED,
        "risk": "high",
    },

    # ── Inbox ──
    "update_inbox_status": {
        "entity": "inbox_item",
        "operation": "update",
        "description": "Update an inbox item's status (e.g., mark complete, reopen)",
        "required_fields": ["id", "status"],
        "optional_fields": [],
        "confirm": ConfirmLevel.REQUIRED,
        "risk": "medium",
    },
    "delete_inbox_item": {
        "entity": "inbox_item",
        "operation": "delete",
        "description": "Delete an inbox item",
        "required_fields": ["id"],
        "optional_fields": [],
        "confirm": ConfirmLevel.REQUIRED,
        "risk": "high",
    },

    # ── Social: Publish ──
    "publish_social_post": {
        "entity": "social_post",
        "operation": "publish",
        "description": "Publish a social post to connected platforms",
        "required_fields": ["inbox_item_id"],
        "optional_fields": ["platform"],
        "confirm": ConfirmLevel.REQUIRED,
        "risk": "high",
    },

    # ── Email: Send ──
    "send_email_draft": {
        "entity": "email_draft",
        "operation": "send",
        "description": "Send an approved email draft",
        "required_fields": ["inbox_item_id"],
        "optional_fields": [],
        "confirm": ConfirmLevel.REQUIRED,
        "risk": "high",
    },

    # ── Tasks ──
    "update_task_status": {
        "entity": "task",
        "operation": "update",
        "description": "Move a task to a different status",
        "required_fields": ["id", "status"],
        "optional_fields": [],
        "confirm": ConfirmLevel.REQUIRED,
        "risk": "medium",
    },
}


# ─── Action Executor ──────────────────────────────────────────────────────────

async def execute_action(
    tenant_id: str,
    action_name: str,
    params: dict[str, Any],
    confirmed: bool = False,
) -> dict:
    """Execute a CEO business action.

    Returns:
        {
            "status": "executed" | "needs_confirmation" | "error" | "forbidden",
            "action": action_name,
            "result": ...,
            "confirmation": { ... }  # only if needs_confirmation
        }
    """
    action_def = ACTION_REGISTRY.get(action_name)
    if not action_def:
        return {"status": "error", "action": action_name, "message": f"Unknown action: {action_name}"}

    # Check required fields
    missing = [f for f in action_def["required_fields"] if not params.get(f)]
    if missing:
        return {
            "status": "missing_fields",
            "action": action_name,
            "missing_fields": missing,
            "message": f"Missing required fields: {', '.join(missing)}",
        }

    # Check confirmation requirement
    if action_def["confirm"] == ConfirmLevel.REQUIRED and not confirmed:
        return {
            "status": "needs_confirmation",
            "action": action_name,
            "params": params,
            "confirmation": _build_confirmation(action_name, action_def, params),
        }

    # Execute the action
    try:
        result = await _dispatch_action(tenant_id, action_name, action_def, params)

        # Audit log
        await _audit_log(tenant_id, action_name, params, result, confirmed)

        return {
            "status": "executed",
            "action": action_name,
            "result": result,
        }
    except Exception as e:
        logger.error("CEO action %s failed: %s", action_name, e)
        return {"status": "error", "action": action_name, "message": str(e)}


def _build_confirmation(action_name: str, action_def: dict, params: dict) -> dict:
    """Build a confirmation dialog payload."""
    operation = action_def["operation"]
    entity = action_def["entity"].replace("_", " ")

    if operation == "delete":
        title = "Confirm Delete"
        message = f"Permanently delete this {entity}?"
        confirm_label = "Delete"
        destructive = True
    elif operation == "update":
        title = "Confirm Update"
        changes = {k: v for k, v in params.items() if k != "id" and v is not None}
        message = f"Update {entity} with: {changes}" if changes else f"Update this {entity}?"
        confirm_label = "Confirm"
        destructive = False
    elif operation in ("publish", "send"):
        title = f"Confirm {operation.title()}"
        message = f"{operation.title()} this {entity}? This action cannot be undone."
        confirm_label = operation.title()
        destructive = True
    else:
        title = "Confirm Action"
        message = f"Execute {action_name}?"
        confirm_label = "Confirm"
        destructive = False

    return {
        "title": title,
        "message": message,
        "action": action_name,
        "params": params,
        "confirm_label": confirm_label,
        "cancel_label": "Cancel",
        "destructive": destructive,
    }


async def _dispatch_action(tenant_id: str, action_name: str, action_def: dict, params: dict) -> dict:
    """Route to the appropriate shared service handler."""
    from backend.services import crm as crm_service, inbox as inbox_service

    entity = action_def["entity"]
    operation = action_def["operation"]

    # ── CRM Contacts ──
    if entity == "crm_contact":
        if operation == "create":
            data = {k: params.get(k) for k in ["name", "email", "phone", "company_id", "tags", "notes"] if params.get(k) is not None}
            data.setdefault("source", "ceo_chat")
            data.setdefault("status", params.get("status", "lead"))
            return crm_service.create_contact(tenant_id, data)
        elif operation == "read":
            return crm_service.list_contacts(tenant_id, search=params.get("search", ""), status=params.get("status", ""))
        elif operation == "update":
            updates = {k: v for k, v in params.items() if k != "id" and v is not None}
            return crm_service.update_contact(tenant_id, params["id"], updates)
        elif operation == "delete":
            return crm_service.delete_contact(tenant_id, params["id"])

    # ── CRM Companies ──
    elif entity == "crm_company":
        if operation == "create":
            data = {k: params.get(k) for k in ["name", "domain", "industry", "size", "notes"] if params.get(k) is not None}
            return crm_service.create_company(tenant_id, data)
        elif operation == "update":
            updates = {k: v for k, v in params.items() if k != "id" and v is not None}
            return crm_service.update_company(tenant_id, params["id"], updates)
        elif operation == "delete":
            return crm_service.delete_company(tenant_id, params["id"])

    # ── CRM Deals ──
    elif entity == "crm_deal":
        if operation == "create":
            data = {k: params.get(k) for k in ["title", "value", "stage", "contact_id", "company_id", "notes", "expected_close"] if params.get(k) is not None}
            return crm_service.create_deal(tenant_id, data)
        elif operation == "update":
            updates = {k: v for k, v in params.items() if k != "id" and v is not None}
            return crm_service.update_deal(tenant_id, params["id"], updates)
        elif operation == "delete":
            return crm_service.delete_deal(tenant_id, params["id"])

    # ── Inbox ──
    elif entity == "inbox_item":
        if operation == "update":
            return inbox_service.update_status(tenant_id, params["id"], params["status"])
        elif operation == "delete":
            return inbox_service.delete_item(tenant_id, params["id"])

    # ── Social Publish ──
    elif entity == "social_post" and operation == "publish":
        return {"published": params["inbox_item_id"], "status": "delegated_to_publish_flow"}

    # ── Email Send ──
    elif entity == "email_draft" and operation == "send":
        return {"sent": params["inbox_item_id"], "status": "delegated_to_email_flow"}

    # ── Tasks ──
    elif entity == "task" and operation == "update":
        sb.table("tasks").update({
            "status": params["status"],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", params["id"]).eq("tenant_id", tenant_id).execute()
        return {"updated": params["id"], "new_status": params["status"]}

    return {"status": "unknown_action"}


async def _audit_log(tenant_id: str, action_name: str, params: dict, result: dict, confirmed: bool):
    """Log every CEO-triggered business action for traceability."""
    try:
        from backend.config.loader import _get_supabase
        sb = _get_supabase()
        sb.table("agent_logs").insert({
            "tenant_id": tenant_id,
            "agent_name": "ceo",
            "action": f"ceo_action:{action_name}",
            "result": {
                "action": action_name,
                "params": _sanitize_params(params),
                "result_summary": str(result)[:500],
                "confirmed": confirmed,
                "source": "ceo_chat",
            },
            "status": "completed",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        logger.warning("Failed to audit log CEO action: %s", e)


def _sanitize_params(params: dict) -> dict:
    """Remove sensitive data from params before logging."""
    sanitized = {}
    for k, v in params.items():
        if k in ("password", "secret", "token", "access_token"):
            sanitized[k] = "***"
        else:
            sanitized[k] = v
    return sanitized


def get_action_names() -> list[str]:
    """Return all registered action names for prompt injection."""
    return list(ACTION_REGISTRY.keys())


def get_action_descriptions() -> str:
    """Return a compact description of all available actions for the CEO system prompt."""
    lines = []
    for name, defn in ACTION_REGISTRY.items():
        confirm = " [REQUIRES CONFIRMATION]" if defn["confirm"] == ConfirmLevel.REQUIRED else ""
        lines.append(f"- {name}: {defn['description']}{confirm}")
    return "\n".join(lines)
