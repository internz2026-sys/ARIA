"""Helpers for parsing Paperclip /comments responses.

Used by orchestrator.run_agent_via_paperclip_sync() to find the agent's
reply among the comments on an issue. Originally also shared with
paperclip_poller.poll_completed_issues(), but the inbox importer was
deleted in favor of Path A (agent uses aria-backend-api skill to write
inbox items directly), so only the chat-side helpers remain.
"""
from __future__ import annotations


def normalize_comments(payload: object) -> list[dict]:
    """Coerce Paperclip's /comments response (list or wrapped dict) into a flat list."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        return payload.get("data") or payload.get("comments") or []
    return []


def pick_agent_output(comments: list[dict], exclude_text: str = "") -> str | None:
    """Return the longest non-empty comment that isn't `exclude_text`.

    The chat sync route posts the user's prompt as a comment so the agent
    has full context beyond the truncated title. We don't want to return
    that as the agent's reply, so callers pass `exclude_text=user_message`.

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
        if len(body) > len(best):
            best = body
    return best or None
