"""CEO chat prompt-time constants — content + regex patterns.

Senior-dev replacement for the lazy imports in routers/ceo.py. These
are read-only module-level constants computed at import time:

  - CEO_MD          : the CEO agent's identity markdown (truncated to 4000
                      chars so we can fit the rest of the system prompt
                      without blowing the cache window)
  - AGENT_MDS       : sub-agent role markdown, keyed by slug. Loaded once
                      from disk on import; used by the chat handler to
                      build a one-line capabilities cheat sheet on the
                      first chat turn only.
  - DELEGATE_BLOCK_RE / ACTION_BLOCK_RE : the codeblock parsers Haiku
                      emits for delegating to sub-agents and invoking
                      CRUD actions.
  - CRM_TRIGGER_PHRASES / CRM_NOUN_RE / CRM_VERB_RE : the heuristic the
                      chat handler uses to decide whether the user's
                      message references CRM entities (contacts/deals/
                      companies) — when it matches, the system prompt
                      gets a CRM-context block injected.

server.py keeps underscore-prefixed aliases (_CEO_MD, _AGENT_MDS, etc.)
so the dozens of in-file references continue to work.
"""
from __future__ import annotations

import pathlib
import re

_AGENTS_DIR = pathlib.Path(__file__).resolve().parent.parent.parent / "docs" / "agents"

# ── CEO identity + sub-agent role MDs ─────────────────────────────────
CEO_MD_FULL = (_AGENTS_DIR / "ceo.md").read_text(encoding="utf-8")
# Truncated to 4000 chars: enough to include all delegation rules + the
# sub-agent list, while leaving headroom for the rest of the system
# prompt (recent inbox activity, stale items, CRM context, action
# descriptions, current date). Prompt caching keeps cost low on repeat
# calls.
CEO_MD = CEO_MD_FULL[:4000]

# Sub-agent role MDs — used by the chat handler on the FIRST chat turn
# only to inline a one-line capabilities cheat sheet into the system
# prompt. Skill MDs are NOT loaded here; BaseAgent.run() loads those
# per-agent at runtime via backend.agents.base._load_agent_skill().
AGENT_MDS: dict[str, str] = {}
for _f in _AGENTS_DIR.glob("*.md"):
    if _f.stem != "ceo":
        AGENT_MDS[_f.stem] = _f.read_text(encoding="utf-8")


# ── Codeblock parsers for delegate / action blocks the CEO emits ──────
# Compiled once at module load. The chat handler runs these on every
# turn; per-request compilation was cheap but not free (~50us each).
DELEGATE_BLOCK_RE = re.compile(r"```delegate\s*\n(.*?)\n```", re.DOTALL)
ACTION_BLOCK_RE = re.compile(r"```action\s*\n(.*?)\n```", re.DOTALL)


# ── CRM context heuristic ─────────────────────────────────────────────
# Word-boundary regexes so substring false positives don't trigger:
# "ideal" must not match "deal", "leader" must not match "lead",
# "calling" must not match "call".
CRM_TRIGGER_PHRASES = (
    "send email to", "reach out to", "follow up with",
    "the contact", "this contact", "all contacts", "my contacts",
    "the company", "this company", "all companies", "my companies",
    "the deal", "this deal", "all deals", "my deals",
    "the lead", "this lead", "all leads", "my leads",
    "crm",
)
CRM_NOUN_RE = re.compile(
    r"\b(contacts?|compan(?:y|ies)|deals?|leads?|prospects?|pipelines?)\b"
)
CRM_VERB_RE = re.compile(
    r"\b(create|add|update|delete|remove|find|show|list|search|email|call)\b"
)
