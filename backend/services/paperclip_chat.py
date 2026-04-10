"""Helpers for parsing Paperclip /comments responses.

Used by orchestrator.run_agent_via_paperclip_sync() to find the agent's
reply among the comments on an issue. Originally also shared with the
inbox-importer poller, but that was deleted in favor of Path A (agent
uses aria-backend-api skill to write inbox items directly), so only
the chat-side helpers remain.
"""
from __future__ import annotations


def normalize_comments(payload: object) -> list[dict]:
    """Coerce Paperclip's /comments response (list or wrapped dict) into a flat list."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        return payload.get("data") or payload.get("comments") or []
    return []


# Comments that match these prefixes are ARIA's own framing wrappers
# (not real agent replies). Used by pick_agent_output as a safety net
# in case the orchestrator's exclude_text doesn't catch a near-duplicate.
_ARIA_FRAMING_PREFIXES = ("[tenant_id=", "TENANT_ID:", "USER MESSAGE:")


def pick_agent_output(comments: list[dict], exclude_text: str = "") -> str | None:
    """Return the longest comment that's a real agent reply.

    Skips:
    - Empty/whitespace-only comments
    - The exact `exclude_text` (the user's original message, prefixed)
    - Anything starting with an ARIA framing prefix like `[tenant_id=`
      or `TENANT_ID:` — those are our own wrappers, never the agent

    Returns None if no usable comment was found.
    """
    needle = exclude_text.strip() if exclude_text else ""
    best = ""
    for c in comments:
        body = (c.get("body") or c.get("content") or "").strip()
        if not body:
            continue
        if needle and body == needle:
            continue
        if any(body.startswith(prefix) for prefix in _ARIA_FRAMING_PREFIXES):
            continue
        if len(body) > len(best):
            best = body
    return best or None
