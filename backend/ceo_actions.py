"""CEO Action Registry — allowlisted business actions the CEO Agent can perform.

The CEO Agent is a business operations orchestrator, NOT a developer agent.
It can CRUD business records and trigger approved workflows, but cannot
modify codebase, prompts, backend logic, database schema, or infrastructure.
"""
from __future__ import annotations

import json as _json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from backend.config.loader import get_tenant_config, save_tenant_config
from backend.services.supabase import get_db
from backend.services import crm as crm_service, inbox as inbox_service

logger = logging.getLogger("aria.ceo_actions")


# ─── Shared helpers ───────────────────────────────────────────────────────────

def _find_latest_inbox_item(tenant_id: str, item_type: str = "", statuses: list[str] | None = None) -> dict | None:
    """Find the most recent inbox item matching type and status filters."""
    sb = get_db()
    query = sb.table("inbox_items").select("id,content,type,status").eq("tenant_id", tenant_id)
    if item_type:
        query = query.eq("type", item_type)
    if statuses:
        query = query.in_("status", statuses)
    result = query.order("created_at", desc=True).limit(1).execute()
    return result.data[0] if result.data else None


def _extract_post_text(content: str, platform: str | None = None) -> str:
    """Extract post text from JSON content, optionally for a specific platform."""
    try:
        start = content.find("{")
        end = content.rfind("}") + 1
        if start >= 0 and end > start:
            data = _json.loads(content[start:end])
            for p in data.get("posts", []):
                if platform and p.get("platform", "").lower() != platform:
                    continue
                return p.get("text", "")
    except Exception:
        pass
    return content[:3000]

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
        "description": "Publish a social post to connected platforms (auto-finds latest if no ID given)",
        "required_fields": [],
        "optional_fields": ["inbox_item_id", "platform"],
        "confirm": ConfirmLevel.REQUIRED,
        "risk": "high",
    },

    # ── Email: Send ──
    "send_email_draft": {
        "entity": "email_draft",
        "operation": "send",
        "description": "Send a pending email draft via Gmail (auto-finds latest if no ID given)",
        "required_fields": [],
        "optional_fields": ["inbox_item_id"],
        "confirm": ConfirmLevel.REQUIRED,
        "risk": "high",
    },

    # ── LinkedIn: Publish ──
    "publish_to_linkedin": {
        "entity": "linkedin_post",
        "operation": "publish",
        "description": "Publish a post to LinkedIn (auto-finds latest social post if no ID given)",
        "required_fields": [],
        "optional_fields": ["inbox_item_id", "text"],
        "confirm": ConfirmLevel.REQUIRED,
        "risk": "high",
    },

    # ── WhatsApp: Send ──
    "send_whatsapp": {
        "entity": "whatsapp_message",
        "operation": "send",
        "description": "Send a WhatsApp message to a phone number",
        "required_fields": ["to", "message"],
        "optional_fields": [],
        "confirm": ConfirmLevel.REQUIRED,
        "risk": "high",
    },

    # ── Email: Draft Reply ──
    "draft_email_reply": {
        "entity": "email_reply",
        "operation": "create",
        "description": "Draft a reply to an email thread (goes to inbox for approval)",
        "required_fields": ["thread_id"],
        "optional_fields": ["custom_instructions"],
        "confirm": ConfirmLevel.NONE,
        "risk": "low",
    },

    # ── Email: Cancel Draft ──
    "cancel_draft": {
        "entity": "email_draft",
        "operation": "cancel",
        "description": "Cancel a pending email draft",
        "required_fields": [],
        "optional_fields": ["inbox_item_id"],
        "confirm": ConfirmLevel.REQUIRED,
        "risk": "medium",
    },

    # ── Gmail: Sync ──
    "sync_gmail": {
        "entity": "gmail",
        "operation": "sync",
        "description": "Sync Gmail inbox to check for new replies",
        "required_fields": [],
        "optional_fields": [],
        "confirm": ConfirmLevel.NONE,
        "risk": "none",
    },

    # ── Agents: Run ──
    "run_agent": {
        "entity": "agent",
        "operation": "run",
        "description": "Run a specific agent (content_writer, email_marketer, social_manager, ad_strategist)",
        "required_fields": ["agent_name"],
        "optional_fields": ["task"],
        "confirm": ConfirmLevel.REQUIRED,
        "risk": "medium",
    },

    # ── Inbox: Read ──
    "read_inbox": {
        "entity": "inbox_item",
        "operation": "read",
        "description": "List inbox items, optionally filtered by status",
        "required_fields": [],
        "optional_fields": ["status"],
        "confirm": ConfirmLevel.NONE,
        "risk": "none",
    },

    # ── CRM: Read Companies ──
    "read_companies": {
        "entity": "crm_company",
        "operation": "read",
        "description": "List or search CRM companies",
        "required_fields": [],
        "optional_fields": ["search"],
        "confirm": ConfirmLevel.NONE,
        "risk": "none",
    },

    # ── CRM: Read Deals ──
    "read_deals": {
        "entity": "crm_deal",
        "operation": "read",
        "description": "List CRM deals, optionally filtered by stage",
        "required_fields": [],
        "optional_fields": ["stage"],
        "confirm": ConfirmLevel.NONE,
        "risk": "none",
    },

    # ── Tasks ──
    "create_task": {
        "entity": "task",
        "operation": "create",
        "description": "Create a new task and assign it to an agent",
        "required_fields": ["agent", "task"],
        "optional_fields": ["priority", "status"],
        "confirm": ConfirmLevel.NONE,
        "risk": "low",
    },
    "read_tasks": {
        "entity": "task",
        "operation": "read",
        "description": "List tasks, optionally filtered by agent or status",
        "required_fields": [],
        "optional_fields": ["agent", "status"],
        "confirm": ConfirmLevel.NONE,
        "risk": "none",
    },
    "update_task_status": {
        "entity": "task",
        "operation": "update",
        "description": "Move a task to a different status",
        "required_fields": ["id", "status"],
        "optional_fields": [],
        "confirm": ConfirmLevel.REQUIRED,
        "risk": "medium",
    },
    "delete_task": {
        "entity": "task",
        "operation": "delete",
        "description": "Permanently delete a task",
        "required_fields": ["id"],
        "optional_fields": [],
        "confirm": ConfirmLevel.REQUIRED,
        "risk": "high",
    },

    # ── CRM Activities ──
    "read_activities": {
        "entity": "crm_activity",
        "operation": "read",
        "description": "List CRM activity history, optionally filtered by contact",
        "required_fields": [],
        "optional_fields": ["contact_id"],
        "confirm": ConfirmLevel.NONE,
        "risk": "none",
    },
    "create_activity": {
        "entity": "crm_activity",
        "operation": "create",
        "description": "Log a new CRM activity (call, meeting, note, follow-up)",
        "required_fields": ["type", "description"],
        "optional_fields": ["contact_id"],
        "confirm": ConfirmLevel.NONE,
        "risk": "low",
    },

    # ── Email Threads ──
    "read_email_threads": {
        "entity": "email_thread",
        "operation": "read",
        "description": "List email threads, optionally filtered by status",
        "required_fields": [],
        "optional_fields": ["status"],
        "confirm": ConfirmLevel.NONE,
        "risk": "none",
    },
    "update_email_thread": {
        "entity": "email_thread",
        "operation": "update",
        "description": "Update an email thread's status (open, awaiting_reply, replied, closed)",
        "required_fields": ["id", "status"],
        "optional_fields": [],
        "confirm": ConfirmLevel.REQUIRED,
        "risk": "medium",
    },

    # ── Notifications ──
    "read_notifications": {
        "entity": "notification",
        "operation": "read",
        "description": "List recent notifications",
        "required_fields": [],
        "optional_fields": ["unread_only"],
        "confirm": ConfirmLevel.NONE,
        "risk": "none",
    },
    "mark_notifications_read": {
        "entity": "notification",
        "operation": "update",
        "description": "Mark notifications as read (all, or specific IDs)",
        "required_fields": [],
        "optional_fields": ["ids"],
        "confirm": ConfirmLevel.NONE,
        "risk": "low",
    },

    # ── Agent Logs ──
    "read_agent_logs": {
        "entity": "agent_log",
        "operation": "read",
        "description": "View recent agent run history and results",
        "required_fields": [],
        "optional_fields": ["agent_name", "status"],
        "confirm": ConfirmLevel.NONE,
        "risk": "none",
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
        sb = get_db()
        inbox_item_id = params.get("inbox_item_id", "")

        if not inbox_item_id:
            item = _find_latest_inbox_item(tenant_id, "social_post", ["ready", "needs_review"])
            if not item:
                return {"error": "No social posts found ready to publish"}
            inbox_item_id = item["id"]
            content = item.get("content", "")
        else:
            item_result = sb.table("inbox_items").select("id,content").eq("id", inbox_item_id).single().execute()
            if not item_result.data:
                raise ValueError("Inbox item not found")
            content = item_result.data.get("content", "")

        text = _extract_post_text(content, "twitter")

        access_token = config.integrations.twitter_access_token
        if not access_token:
            return {"error": "Twitter not connected. Go to Settings > Integrations."}

        result = await twitter_tool.post_tweet(access_token, text[:280])
        if result.get("error"):
            return {"error": result["error"]}

        sb.table("inbox_items").update({"status": "sent"}).eq("id", inbox_item_id).execute()
        return {"published": inbox_item_id, "tweet_id": result.get("tweet_id", ""), "status": "sent"}

    # ── Email Send ──
    elif entity == "email_draft" and operation == "send":
        sb = get_db()
        inbox_item_id = params.get("inbox_item_id", "")

        if not inbox_item_id:
            item = _find_latest_inbox_item(tenant_id, statuses=["draft_pending_approval"])
            if not item:
                return {"error": "No email drafts found pending approval"}
            inbox_item_id = item["id"]

        item_result = sb.table("inbox_items").select("*").eq("id", inbox_item_id).single().execute()
        item = item_result.data
        if not item:
            raise ValueError("Inbox item not found")

        email_draft = item.get("email_draft", {})
        if not email_draft:
            return {"error": "This inbox item has no email draft"}

        # Send via Gmail
        config = get_tenant_config(tenant_id)
        google_token = config.integrations.google_access_token
        google_refresh = config.integrations.google_refresh_token

        if not google_token and not google_refresh:
            return {"error": "Gmail not connected. Go to Settings > Integrations."}

        from backend.tools import gmail_tool
        to = email_draft.get("to", "")
        subject = email_draft.get("subject", "")
        html_body = email_draft.get("html_body", email_draft.get("text_body", ""))

        if not to:
            return {"error": "No recipient email in the draft"}

        send_result = await gmail_tool.send_email(
            access_token=google_token,
            refresh_token=google_refresh,
            to=to,
            subject=subject,
            body_html=html_body,
            user_email=config.owner_email,
        )

        if send_result.get("error"):
            sb.table("inbox_items").update({"status": "failed"}).eq("id", inbox_item_id).execute()
            return {"error": send_result["error"]}

        # Update tokens if refreshed
        if send_result.get("new_access_token"):
            config.integrations.google_access_token = send_result["new_access_token"]
            save_tenant_config(config)

        sb.table("inbox_items").update({"status": "sent"}).eq("id", inbox_item_id).execute()
        return {"sent": inbox_item_id, "to": to, "subject": subject, "status": "sent"}

    # ── LinkedIn Publish ──
    elif entity == "linkedin_post" and operation == "publish":
        sb = get_db()
        inbox_item_id = params.get("inbox_item_id", "")
        text = params.get("text", "")

        if not text and not inbox_item_id:
            item = _find_latest_inbox_item(tenant_id, "social_post", ["ready", "needs_review"])
            if not item:
                return {"error": "No social posts found ready to publish"}
            inbox_item_id = item["id"]
            text = _extract_post_text(item.get("content", ""), "linkedin")

        config = get_tenant_config(tenant_id)
        li_token = config.integrations.linkedin_access_token
        li_urn = config.integrations.linkedin_org_urn or config.integrations.linkedin_member_urn
        if not li_token or not li_urn:
            return {"error": "LinkedIn not connected. Go to Settings > Integrations."}

        from backend.tools import linkedin_tool
        result = await linkedin_tool.create_post(li_token, li_urn, text[:3000])
        if result.get("error"):
            return {"error": result["error"]}

        if inbox_item_id:
            sb.table("inbox_items").update({"status": "sent"}).eq("id", inbox_item_id).execute()
        return {"published": "linkedin", "post_id": result.get("post_id", ""), "status": "sent"}

    # ── WhatsApp Send ──
    elif entity == "whatsapp_message" and operation == "send":
        config = get_tenant_config(tenant_id)
        wa_token = config.integrations.whatsapp_access_token
        wa_pid = config.integrations.whatsapp_phone_number_id
        if not wa_token or not wa_pid:
            return {"error": "WhatsApp not connected. Go to Settings > Integrations."}

        from backend.tools import whatsapp_tool
        result = await whatsapp_tool.send_message(
            to=params["to"], text=params["message"],
            access_token=wa_token, phone_number_id=wa_pid,
        )
        if result.get("error"):
            return {"error": result["error"]}
        return {"sent": True, "to": params["to"], "message_id": result.get("message_id", "")}

    # ── Email Draft Reply ──
    elif entity == "email_reply" and operation == "create":
        sb = get_db()
        # Trigger the draft reply via the email marketer
        thread_id = params.get("thread_id", "")
        if not thread_id:
            # Find latest thread that needs a reply
            result = sb.table("email_threads").select("id").eq(
                "tenant_id", tenant_id
            ).eq("status", "needs_review").order("last_message_at", desc=True).limit(1).execute()
            if result.data:
                thread_id = result.data[0]["id"]
            else:
                return {"error": "No email threads needing a reply"}

        return {"drafted": True, "thread_id": thread_id, "status": "delegated_to_email_marketer"}

    # ── Email Cancel Draft ──
    elif entity == "email_draft" and operation == "cancel":
        inbox_item_id = params.get("inbox_item_id", "")
        if not inbox_item_id:
            item = _find_latest_inbox_item(tenant_id, statuses=["draft_pending_approval"])
            if not item:
                return {"error": "No pending email drafts to cancel"}
            inbox_item_id = item["id"]

        return inbox_service.update_status(tenant_id, inbox_item_id, "cancelled")

    # ── Gmail Sync ──
    elif entity == "gmail" and operation == "sync":
        try:
            from backend.tools.gmail_sync import sync_tenant
            result = await sync_tenant(tenant_id)
            return {"synced": True, "imported": result.get("imported", 0)}
        except Exception as e:
            return {"error": f"Gmail sync failed: {e}"}

    # ── Run Agent ──
    elif entity == "agent" and operation == "run":
        agent_name = params.get("agent_name", "")
        if agent_name not in ("content_writer", "email_marketer", "social_manager", "ad_strategist"):
            return {"error": f"Unknown agent: {agent_name}. Valid: content_writer, email_marketer, social_manager, ad_strategist"}

        from backend.orchestrator import dispatch_agent
        result = await dispatch_agent(tenant_id, agent_name)
        return {"ran": agent_name, "status": result.get("status", ""), "result_preview": str(result.get("result", ""))[:200]}

    # ── Inbox Read ──
    elif entity == "inbox_item" and operation == "read":
        status_filter = params.get("status", "")
        items = inbox_service.list_items(tenant_id, status=status_filter, page=1, page_size=10)
        return items

    # ── CRM Company Read ──
    elif entity == "crm_company" and operation == "read":
        return crm_service.list_companies(tenant_id, search=params.get("search", ""))

    # ── CRM Deal Read ──
    elif entity == "crm_deal" and operation == "read":
        return crm_service.list_deals(tenant_id, stage=params.get("stage", ""))

    # ── Tasks ──
    elif entity == "task":
        sb = get_db()
        if operation == "create":
            row = {
                "tenant_id": tenant_id,
                "agent": params["agent"],
                "task": params["task"],
                "priority": params.get("priority", "medium"),
                "status": params.get("status", "to_do"),
            }
            result = sb.table("tasks").insert(row).execute()
            task = result.data[0] if result.data else None
            return {"task": task}
        elif operation == "read":
            query = sb.table("tasks").select("*").eq("tenant_id", tenant_id)
            if params.get("agent"):
                query = query.eq("agent", params["agent"])
            if params.get("status"):
                query = query.eq("status", params["status"])
            result = query.order("created_at", desc=True).limit(30).execute()
            return {"tasks": result.data or []}
        elif operation == "update":
            sb.table("tasks").update({
                "status": params["status"],
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", params["id"]).eq("tenant_id", tenant_id).execute()
            return {"updated": params["id"], "new_status": params["status"]}
        elif operation == "delete":
            sb.table("tasks").delete().eq("id", params["id"]).eq("tenant_id", tenant_id).execute()
            return {"deleted": params["id"]}

    # ── CRM Activities ──
    elif entity == "crm_activity":
        if operation == "read":
            return crm_service.list_activities(tenant_id, contact_id=params.get("contact_id", ""))
        elif operation == "create":
            data = {k: params.get(k) for k in ["type", "description", "contact_id"] if params.get(k) is not None}
            return crm_service.create_activity(tenant_id, data)

    # ── Email Threads ──
    elif entity == "email_thread":
        sb = get_db()
        if operation == "read":
            query = sb.table("email_threads").select("id,subject,contact_email,status,last_message_at").eq("tenant_id", tenant_id)
            if params.get("status"):
                query = query.eq("status", params["status"])
            result = query.order("last_message_at", desc=True).limit(20).execute()
            return {"threads": result.data or []}
        elif operation == "update":
            sb.table("email_threads").update({
                "status": params["status"],
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", params["id"]).eq("tenant_id", tenant_id).execute()
            return {"updated": params["id"], "new_status": params["status"]}

    # ── Notifications ──
    elif entity == "notification":
        sb = get_db()
        if operation == "read":
            query = sb.table("notifications").select("id,title,body,category,is_read,created_at").eq("tenant_id", tenant_id)
            if params.get("unread_only"):
                query = query.eq("is_read", False)
            result = query.order("created_at", desc=True).limit(20).execute()
            return {"notifications": result.data or []}
        elif operation == "update":
            ids = params.get("ids", [])
            if ids:
                sb.table("notifications").update({"is_read": True}).in_("id", ids).eq("tenant_id", tenant_id).execute()
                return {"marked_read": len(ids)}
            else:
                sb.table("notifications").update({"is_read": True}).eq("tenant_id", tenant_id).eq("is_read", False).execute()
                return {"marked_read": "all"}

    # ── Agent Logs ──
    elif entity == "agent_log" and operation == "read":
        sb = get_db()
        query = sb.table("agent_logs").select("agent_name,action,status,timestamp,result").eq("tenant_id", tenant_id)
        if params.get("agent_name"):
            query = query.eq("agent_name", params["agent_name"])
        if params.get("status"):
            query = query.eq("status", params["status"])
        result = query.order("timestamp", desc=True).limit(20).execute()
        return {"logs": result.data or []}

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
