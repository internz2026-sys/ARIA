"""ARIA Agent Registry — 6 marketing-focused agents for developer founders."""

from . import (
    ceo_agent,
    content_writer_agent,
    email_marketer_agent,
    social_manager_agent,
    ad_strategist_agent,
    media_agent,
)

AGENT_REGISTRY: dict[str, object] = {
    "ceo": ceo_agent,
    "content_writer": content_writer_agent,
    "email_marketer": email_marketer_agent,
    "social_manager": social_manager_agent,
    "ad_strategist": ad_strategist_agent,
    "media": media_agent,
}

DEPARTMENT_MAP = {
    "marketing": ["ceo", "content_writer", "email_marketer", "social_manager", "ad_strategist", "media"],
}
