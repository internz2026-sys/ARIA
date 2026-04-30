"""CEO chat helpers — DB persistence + LLM output parsing.

Slice 4b of the multi-batch refactor. Lifts `_save_chat_message` and
`_parse_codeblock_json` out of server.py into their own module. Both
helpers are pure (no shared state, no Socket.IO, no app-level deps)
and were previously sandwiched between unrelated handlers.

server.py imports the public names + re-aliases them under their
original underscore-prefixed forms so call sites keep working.

What lives here:
  - save_message(): persist a single chat turn to chat_messages,
    upserting the chat_sessions row to keep updated_at fresh
  - parse_codeblock_json(): tolerant JSON parser for ```delegate /
    ```action blocks the CEO emits in markdown. Recovers from the
    three most common LLM mistakes (JS-style comments, trailing
    commas, prose padding around the JSON object).

What does NOT live here:
  - The chat HANDLER itself (_ceo_chat_impl) — slice 4c
  - The history endpoints (/api/ceo/chat/{id}/history etc) — slice 4d
  - In-memory session state — slice 4a, services/chat_state.py
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

from backend.services.supabase import get_db

logger = logging.getLogger("aria.services.chat")


def save_message(
    session_id: str,
    tenant_id: str,
    role: str,
    content: str,
    delegations: list | None = None,
) -> None:
    """Persist a single chat message to Supabase.

    Two writes per call:
      1. UPSERT chat_sessions row (creates the session on first turn,
         bumps updated_at on every subsequent turn so the history
         sidebar can sort by recency)
      2. INSERT chat_messages row (the actual turn)

    Failures are swallowed deliberately — the chat reply is the
    primary user-facing artifact. If Supabase hiccups, we don't want
    a 500 from the chat handler that hides the model's response.
    The in-memory chat_sessions cache (services/chat_state.py) means
    the conversation continues even when persistence fails.
    """
    try:
        sb = get_db()
        # Ensure session row exists (upsert keeps the call idempotent
        # so we don't have to track first-turn-or-not at the call site).
        sb.table("chat_sessions").upsert(
            {
                "id": session_id,
                "tenant_id": tenant_id or None,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            on_conflict="id",
        ).execute()
        sb.table("chat_messages").insert(
            {
                "session_id": session_id,
                "role": role,
                "content": content,
                "delegations": delegations or [],
            }
        ).execute()
    except Exception as e:
        logger.debug("[services.chat] save_message failed (non-fatal): %s", e)


def parse_codeblock_json(block: str, kind: str) -> dict | None:
    """Parse a ```delegate / ```action JSON block emitted by the CEO,
    recovering from the most common LLM-side mistakes.

    Returns the parsed dict, or None if every recovery attempt fails.
    Logs the failure with the original block prefix for debugging.

    Why a tolerant parser instead of a strict json.loads():
      Haiku occasionally hallucinates JS-style comments, trailing
      commas, or wraps the JSON in extra prose ("Here's my plan:
      {...} Let me know if..."). A bare json.loads silently failed
      and the CEO promised delegation in prose but nothing fired —
      the user saw the text reply but no inbox row.

    Three recovery passes (in order):
      1. Literal json.loads
      2. Strip // comments, /* */ comments, trailing commas
      3. Extract just the {...} substring via regex
    """
    raw = block.strip()
    if not raw:
        return None

    # 1. Literal parse
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # 2. Strip JS-style comments + trailing commas
    cleaned = re.sub(r"//[^\n]*", "", raw)
    cleaned = re.sub(r"/\*.*?\*/", "", cleaned, flags=re.DOTALL)
    cleaned = re.sub(r",(\s*[}\]])", r"\1", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # 3. Try extracting just the {...} substring (prose padding case)
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    logging.getLogger("aria.ceo_chat").warning(
        "[%s-parse] all recovery attempts failed for block: %s",
        kind, raw[:300],
    )
    return None
