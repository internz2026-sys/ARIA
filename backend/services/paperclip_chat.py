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
_ARIA_FRAMING_PREFIXES = (
    "[tenant_id=",
    "TENANT_ID:",
    "USER MESSAGE:",
    "[wake]",  # fallback wake comments posted by orchestrator on heartbeat failure
)

# Author names / slugs that mean "this comment is from the CEO, not the
# delegated agent" -- used to filter the CEO's own staging dumps from
# pick_agent_output's longest-comment selection.
_CEO_AUTHOR_MARKERS = {"ceo", "ARIA CEO", "CEO", "ARIA_CEO"}


def _comment_author_id(comment: dict) -> str:
    """Extract the comment author identifier across Paperclip's payload shapes."""
    author = comment.get("author") or comment.get("agent") or {}
    if isinstance(author, dict):
        return (
            author.get("name")
            or author.get("displayName")
            or author.get("slug")
            or author.get("urlKey")
            or ""
        )
    if isinstance(author, str):
        return author
    return comment.get("authorName") or comment.get("agentName") or ""


def pick_agent_output(
    comments: list[dict],
    exclude_text: str = "",
    *,
    expected_agent: str | None = None,
) -> str | None:
    """Return the longest comment that's a real agent reply.

    Skips (always):
    - Empty/whitespace-only comments
    - The exact `exclude_text` (the user's original message, prefixed)
    - Anything starting with an ARIA framing prefix like `[tenant_id=`,
      `[wake]`, or `TENANT_ID:` -- those are our own wrappers, never the agent

    Selection priority:
    1. If a comment is explicitly authored by the expected agent slug,
       use the longest such comment (highest-signal match).
    2. Otherwise, fall back to the longest comment that is NOT authored
       by the CEO (when expected_agent is set to a non-CEO slug). This
       prevents the CEO's own non-framing-prefix posts from being
       imported as a sub-agent's reply.
    3. If even that yields nothing (every candidate is CEO-tagged or
       has no author info), fall back to the longest comment overall.
       Better to occasionally import a CEO comment than to drop the
       agent's actual reply because of an author-tag mismatch -- we
       saw this happen in production when claude_local agents shared
       the CEO's auth context and Paperclip tagged their replies as
       CEO-authored.

    Returns None if no usable comment was found at all.
    """
    needle = exclude_text.strip() if exclude_text else ""
    best_overall = ""           # any usable comment, regardless of author
    best_non_ceo = ""           # longest non-CEO-authored comment
    best_authored = ""          # longest comment authored by expected agent

    expected_norm = (expected_agent or "").lower()

    for c in comments:
        body = (c.get("body") or c.get("content") or "").strip()
        if not body:
            continue
        if needle and body == needle:
            continue
        if any(body.startswith(prefix) for prefix in _ARIA_FRAMING_PREFIXES):
            continue

        author = _comment_author_id(c)
        author_norm = author.lower() if isinstance(author, str) else ""

        # Track the overall longest as the absolute fallback
        if len(body) > len(best_overall):
            best_overall = body

        is_ceo_authored = (
            author in _CEO_AUTHOR_MARKERS
            or author_norm in {"ceo", "aria_ceo", "aria ceo"}
        )
        if not is_ceo_authored and len(body) > len(best_non_ceo):
            best_non_ceo = body

        # Preferred: comment authored by the expected agent (highest signal)
        if expected_norm and author_norm and (
            author_norm == expected_norm
            or author_norm.replace(" ", "_") == expected_norm
            or expected_norm in author_norm
        ):
            if len(body) > len(best_authored):
                best_authored = body

    # Three-tier fallback: agent-authored -> non-CEO -> anything usable
    return best_authored or best_non_ceo or best_overall or None
