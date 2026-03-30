"""Content Writer Agent — creates marketing content for developer founders."""
from __future__ import annotations

from backend.agents.base import BaseAgent, MODEL_SONNET, MODEL_HAIKU

_agent = None


class ContentWriterAgent(BaseAgent):
    AGENT_NAME = "content_writer"
    CONTEXT_KEY = "type"
    DEFAULT_CONTEXT = "blog_post"
    CONTEXT_FIELDS = {"business", "product", "audience", "pain_points", "voice"}

    # Use Haiku for short-form, Sonnet for long-form
    _HAIKU_TYPES = {"landing_page", "product_hunt", "show_hn", "email_copy"}

    def build_system_prompt(self, config, content_type: str) -> str:
        # Dynamic model selection based on content type
        if content_type in self._HAIKU_TYPES:
            self.MODEL = MODEL_HAIKU
            self.MAX_TOKENS = 2000
        else:
            self.MODEL = MODEL_SONNET
            self.MAX_TOKENS = 3000

        return f"""You are the Content Writer for {config.business_name}.

{self.business_context(config, self.CONTEXT_FIELDS)}
Positioning: {config.gtm_playbook.positioning}

Create {content_type} content. Match brand voice, include one clear CTA, ready to copy-paste.
Return JSON: content_type, title, body, cta_text, word_count"""

    def build_user_message(self, content_type: str, context: dict | None) -> str:
        return f"Create {content_type} content. Context: {context}"


def _get():
    global _agent
    if _agent is None:
        _agent = ContentWriterAgent()
    return _agent


AGENT_NAME = ContentWriterAgent.AGENT_NAME


async def run(tenant_id: str, context: dict | None = None) -> dict:
    return await _get().run(tenant_id, context)
