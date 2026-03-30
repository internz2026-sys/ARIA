"""Agent brief generator — condenses tenant config into a compact context block.

After onboarding, a single Haiku call distills the full tenant config (~800 tokens
of structured fields) into a ~150 token natural-language brief. Every agent loads
this brief instead of reconstructing context from individual fields, saving
~600+ input tokens per agent call.
"""
from __future__ import annotations

import logging

from backend.config.tenant_schema import TenantConfig

logger = logging.getLogger("aria.brief")

BRIEF_PROMPT = """Condense this business profile into a single concise paragraph (max 150 words).
Include: business name, what they sell, who they sell to, main differentiator,
brand tone, and priority channels. No bullet points. No headers. Just one dense paragraph.

Business: {business_name}
Product: {product_name} — {product_desc}
Value props: {value_props}
Differentiators: {differentiators}
Target audience: {audience}
Pain points: {pain_points}
Where they hang out: {hangouts}
Brand voice: {tone}
Positioning: {positioning}
Channels: {channels}"""


async def generate_agent_brief(config: TenantConfig) -> str:
    """Generate a condensed agent brief from a TenantConfig.

    Makes one Haiku call (~50 input tokens for prompt + ~100 for config data).
    Returns a ~150 token natural-language brief.
    """
    from backend.tools.claude_cli import call_claude, MODEL_HAIKU

    user_message = BRIEF_PROMPT.format(
        business_name=config.business_name,
        product_name=config.product.name,
        product_desc=config.product.description,
        value_props=", ".join(config.product.value_props) or "not specified",
        differentiators=", ".join(config.product.differentiators) or "not specified",
        audience=", ".join(config.icp.target_titles) or "not specified",
        pain_points=", ".join(config.icp.pain_points) or "not specified",
        hangouts=", ".join(config.icp.online_hangouts) or "not specified",
        tone=config.brand_voice.tone,
        positioning=config.gtm_playbook.positioning or "not specified",
        channels=", ".join(config.channels) or "not specified",
    )

    brief = await call_claude(
        "You are a concise business analyst. Output ONLY the condensed paragraph, nothing else.",
        user_message,
        max_tokens=250,
        model=MODEL_HAIKU,
    )

    logger.info("Generated agent brief for %s (%d chars)", config.business_name, len(brief))
    return brief.strip()
