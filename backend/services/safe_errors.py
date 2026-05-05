"""Client-safe error detail rendering.

Replaces the pattern `raise HTTPException(detail=str(e))` and
`detail=f"... {e}"` that audit item #11 flagged. Those literal
exception interpolations leak internals to clients:
  - DB errors with column names + foreign key constraints
  - API key prefixes inside provider error strings
  - file paths from tracebacks
  - library version info from stack frames

In production, `safe_detail()` returns a generic message plus a short
correlation ID. The full exception (with traceback when applicable) is
logged server-side at ERROR level under the same ID, so an operator can
grep for "ref: <id>" in the logs to recover the original detail without
the client ever seeing it.

In development (`ARIA_ENV` / `ENV` not set to prod/production),
returns the verbatim exception so local debugging stays fast.
"""
from __future__ import annotations

import logging
import os
import uuid
from typing import Union

logger = logging.getLogger("aria.services.safe_errors")


def _is_production() -> bool:
    return (os.environ.get("ARIA_ENV") or os.environ.get("ENV") or "").lower() in ("prod", "production")


def safe_detail(exc: Union[BaseException, str, None], public_msg: str = "Internal error") -> str:
    """Build an HTTPException-safe `detail` string.

    Args:
        exc: the underlying exception (or pre-stringified error). May be None.
        public_msg: the generic message clients see in production. Defaults
            to "Internal error" — pass a more specific (but still
            non-sensitive) phrase like "Email send failed" or "AI report
            generation failed" when context helps the user retry.

    Returns:
        - In dev: f"{public_msg}: {exc}" (or just public_msg if exc is None/empty)
        - In prod: f"{public_msg} (ref: <12-char correlation id>)" — the
          full exc is logged at ERROR level with the same correlation id
          so an operator can grep journalctl for "ref: <id>" to recover
          the original detail.
    """
    if not _is_production():
        if exc is None or exc == "":
            return public_msg
        return f"{public_msg}: {exc}"

    correlation_id = uuid.uuid4().hex[:12]
    # exc_info=True preserves the traceback when called from inside an
    # `except` block where exc is the live exception. For string-only
    # callers it's just a regular ERROR log.
    logger.error(
        "[safe_errors %s] %s: %s",
        correlation_id,
        public_msg,
        exc,
        exc_info=isinstance(exc, BaseException),
    )
    return f"{public_msg} (ref: {correlation_id})"
