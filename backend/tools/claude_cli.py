"""Claude CLI wrapper — calls local Claude Code instead of the Anthropic API.

All ARIA agents use this instead of the Anthropic SDK, so no API key is needed.
Claude Code must be installed and authenticated locally.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger("aria.claude_cli")

_executor = ThreadPoolExecutor(max_workers=4)


def _run_claude_sync(full_prompt: str) -> str:
    """Run claude CLI synchronously (called from thread pool)."""
    # Force UTF-8 output on Windows to prevent em-dash / emoji corruption
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["CHCP"] = "65001"

    result = subprocess.run(
        ["claude", "-p", full_prompt, "--output-format", "text"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        env=env,
    )
    if result.returncode != 0:
        error_msg = result.stderr.strip() if result.stderr else "Unknown error"
        raise RuntimeError(f"Claude CLI error: {error_msg}")
    output = result.stdout.strip()
    if not output:
        raise RuntimeError("Claude CLI returned empty response")

    # Fix Windows mojibake: if UTF-8 bytes were decoded as cp1252, reverse it
    try:
        output = output.encode("cp1252").decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        pass  # already valid UTF-8

    return output


async def call_claude(system_prompt: str, user_message: str, max_tokens: int = 4000) -> str:
    """Call local Claude Code CLI with a system prompt and user message.

    Uses subprocess.run in a thread pool to avoid Windows asyncio subprocess issues.
    """
    full_prompt = f"<system>\n{system_prompt}\n</system>\n\n{user_message}"

    loop = asyncio.get_event_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(_executor, _run_claude_sync, full_prompt),
            timeout=120,
        )
    except asyncio.TimeoutError:
        logger.error("Claude CLI timed out after 120s")
        raise RuntimeError("Claude CLI timed out")
    except FileNotFoundError:
        raise RuntimeError(
            "Claude CLI not found. Install it with: npm install -g @anthropic-ai/claude-code"
        )
