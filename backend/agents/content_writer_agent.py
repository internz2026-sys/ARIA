"""Content Writer Agent — creates marketing content for developer founders."""
from __future__ import annotations

from backend.agents.base import BaseAgent

_agent = None


class ContentWriterAgent(BaseAgent):
    AGENT_NAME = "content_writer"
    CONTEXT_KEY = "type"
    DEFAULT_CONTEXT = "blog_post"

    def build_system_prompt(self, config, content_type: str) -> str:
        return f"""You are the Content Writer for {config.business_name}, an AI marketing agent
specializing in content for developer-focused products.

{self.business_context(config)}

GTM context:
{self.gtm_context(config)}

Content types you produce:
1. blog_post — SEO-optimized article (1,000-2,000 words) with meta description, headers, CTA
2. landing_page — headline, subheadline, feature bullets, social proof, CTA copy
3. product_hunt — title, tagline, description, first comment, maker comment
4. show_hn — community-appropriate post telling the product story (not salesy)
5. case_study — customer success story with problem, solution, results
6. email_copy — content adapted for email (feeds into EmailMarketer)

Every piece must:
- Match the brand voice exactly
- Tie back to the GTM playbook positioning
- Have one clear CTA
- Be ready to copy-paste

Return JSON: content_type, title, meta_description (if blog), body, cta_text, cta_url, word_count"""

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
