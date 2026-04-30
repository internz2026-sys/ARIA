"""Async utility helpers — currently just safe_background.

Extracted from server.py so any router can import directly without
the lazy-import dance that made slice 4c2's chat handler so fragile.
No state, no shared mutables — pure utility module.
"""
from __future__ import annotations

import asyncio
import logging
import traceback
from typing import Any, Coroutine


def safe_background(coro: Coroutine[Any, Any, Any], *, label: str = "background") -> asyncio.Task:
    """Spawn an asyncio task with an error callback so silent crashes
    show up in logs.

    Without this wrapper, exceptions raised inside `asyncio.create_task(...)`
    coroutines only surface at GC time as the (largely useless)
    "Task exception was never retrieved" warning — the user sees the
    chat reply but the inbox row never arrives, and there's no error
    in any log you'd think to check.

    `label` flows into the error log so you can grep by call site
    when something does crash. Convention: short snake_case identifier
    like "paperclip_dispatch" or "schedule_pending_draft".
    """
    task = asyncio.create_task(coro)

    def _on_done(t: asyncio.Task) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            logging.getLogger("aria.background").error(
                "[%s] task crashed: %s\n%s",
                label, exc,
                "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            )

    task.add_done_callback(_on_done)
    return task
