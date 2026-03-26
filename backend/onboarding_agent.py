"""Onboarding agent — builds GTM strategy through natural conversation.

The CEO agent conducts a structured intake conversation with developer founders
to understand their product, audience, and goals, then generates a GTM playbook
that all other agents reference.
"""

import asyncio
import json

from backend.config.tenant_schema import (
    TenantConfig, ICPConfig, ProductConfig, GTMPlaybook, BrandVoice,
)
from backend.tools.claude_cli import call_claude

SYSTEM_PROMPT = """You are ARIA, the Chief Marketing Strategist — an AI marketing co-founder for developer founders. Your job is to understand their product through natural conversation so you can build a GTM (go-to-market) strategy and configure AI marketing agents.

You must extract the following through 6-8 targeted questions:
1. What the product does (SaaS, developer tool, API, app) and what problem it solves
2. What makes it different from competitors
3. Who the ideal customer is (developers? CTOs? startup founders?) and their pain points
4. Where the target audience hangs out online (HN, Twitter, Reddit, LinkedIn, etc.)
5. Current traction (any users, revenue, social following?)
6. Marketing goals and timeline (launch on PH? grow to X users? SEO traffic?)
7. Budget for marketing ($50-300/month typical) and time available per week
8. Preferred brand voice (casual/technical/professional/playful)

CONVERSATION RULES:
- Be warm, direct, and concise — talk like a smart co-founder, not a corporate marketer
- Ask one question at a time, building on previous answers
- Acknowledge each answer before asking the next question
- Use developer-friendly language, not marketing jargon
- After collecting enough info, summarize your understanding and present a high-level GTM strategy
- Start by asking what they built

NEVER ask all questions at once. Guide the conversation naturally."""

EXTRACTION_PROMPT = """Based on the conversation below, extract a structured business configuration and GTM strategy as JSON.

Return ONLY valid JSON with this exact structure:
{
  "business_name": "",
  "industry": "technology",
  "description": "",
  "product": {
    "name": "",
    "description": "",
    "value_props": [],
    "pricing_info": "",
    "competitors": [],
    "differentiators": [],
    "product_type": ""
  },
  "icp": {
    "target_titles": [],
    "target_industries": [],
    "company_size": "",
    "pain_points": [],
    "language_patterns": [],
    "online_hangouts": []
  },
  "gtm_playbook": {
    "positioning": "",
    "messaging_pillars": [],
    "content_themes": [],
    "channel_strategy": [],
    "action_plan_30": "",
    "action_plan_60": "",
    "action_plan_90": "",
    "kpis": [],
    "competitor_differentiation": ""
  },
  "brand_voice": {
    "tone": "",
    "example_phrases": [],
    "do_guidelines": [],
    "dont_guidelines": []
  },
  "channels": [],
  "recommended_agents": ["ceo", "content_writer", "email_marketer", "social_manager", "ad_strategist"]
}

For recommended_agents, always include "ceo" and "content_writer". Add others based on the founder's goals:
- "email_marketer" if they want email campaigns, newsletters, or launch sequences
- "social_manager" if they're active on social media or want to grow social presence
- "ad_strategist" if they have budget for paid ads or want to run Facebook campaigns

For channel_strategy, prioritize based on audience:
- Developer tools → content marketing + Twitter/X + Hacker News
- B2B SaaS → LinkedIn + email + content marketing
- Consumer apps → social media + paid ads + content

CONVERSATION:
"""


ONBOARDING_TOPICS = [
    "product_description",
    "target_audience",
    "value_proposition",
    "competitors",
    "marketing_goals",
    "budget_timeline",
    "brand_voice",
    "channels_platforms",
]

# Map topic index to the config fields that stay empty when skipped
TOPIC_SKIPPED_FIELDS = {
    "product_description": ["product"],
    "target_audience": ["icp"],
    "value_proposition": ["product.value_props", "product.differentiators"],
    "competitors": ["product.competitors", "gtm_playbook.competitor_differentiation"],
    "marketing_goals": ["gtm_playbook.kpis", "gtm_playbook.action_plan_30"],
    "budget_timeline": ["gtm_playbook.action_plan_60", "gtm_playbook.action_plan_90"],
    "brand_voice": ["brand_voice"],
    "channels_platforms": ["channels", "gtm_playbook.channel_strategy"],
}


class OnboardingAgent:
    def __init__(self):
        self.messages: list[dict] = []
        self.questions_answered = 0
        self.min_questions = 6
        self._complete = False
        self._extracted_config: dict | None = None
        self.skipped_topics: list[str] = []
        self.current_topic_index: int = 0

    def start_conversation(self) -> str:
        greeting = (
            "Hey! I'm ARIA, your AI marketing co-founder. "
            "I'm going to learn about your product and build a marketing strategy for you. "
            "To start — what did you build?"
        )
        self.messages.append({"role": "assistant", "content": greeting})
        return greeting

    async def process_message(self, user_input: str) -> str:
        self.messages.append({"role": "user", "content": user_input})
        self.questions_answered += 1
        if self.current_topic_index < len(ONBOARDING_TOPICS):
            self.current_topic_index += 1

        conversation_text = "\n".join(
            f"{'ARIA' if m['role'] == 'assistant' else 'Founder'}: {m['content']}"
            for m in self.messages
        )

        assistant_text = await call_claude(
            SYSTEM_PROMPT,
            f"Continue this onboarding conversation. Reply as ARIA with your next response only.\n\n{conversation_text}",
        )

        self.messages.append({"role": "assistant", "content": assistant_text})

        if self.questions_answered >= self.min_questions:
            lower = assistant_text.lower()
            if any(phrase in lower for phrase in [
                "here's what i understood",
                "let me summarize",
                "does this look right",
                "here's your strategy",
                "here's the plan",
                "sound about right",
                "gtm strategy",
            ]):
                self._complete = True

        return assistant_text

    def skip_current_topic(self) -> str:
        """Skip the current onboarding topic. Returns the name of the skipped topic."""
        if self.current_topic_index >= len(ONBOARDING_TOPICS):
            return ""
        topic = ONBOARDING_TOPICS[self.current_topic_index]
        self.skipped_topics.append(topic)
        self.questions_answered += 1
        self.current_topic_index += 1
        self.messages.append({"role": "user", "content": f"[Skipped: {topic}]"})
        return topic

    def get_current_topic(self) -> str:
        """Return the current topic being asked about."""
        if self.current_topic_index < len(ONBOARDING_TOPICS):
            return ONBOARDING_TOPICS[self.current_topic_index]
        return ""

    def get_skipped_fields(self) -> list[str]:
        """Return the config fields that are empty due to skipped topics."""
        fields = []
        for topic in self.skipped_topics:
            fields.extend(TOPIC_SKIPPED_FIELDS.get(topic, []))
        return fields

    def is_complete(self) -> bool:
        return self._complete

    async def extract_config(self) -> dict:
        conversation_text = "\n".join(
            f"{'ARIA' if m['role'] == 'assistant' else 'Founder'}: {m['content']}"
            for m in self.messages
        )

        raw = await call_claude(
            "You are a structured data extraction assistant. Return ONLY valid JSON, no other text.",
            EXTRACTION_PROMPT + conversation_text,
        )

        start = raw.find("{")
        end = raw.rfind("}") + 1
        result = json.loads(raw[start:end])
        self._extracted_config = result
        return result

    async def build_tenant_config(self, tenant_id: str, owner_email: str, owner_name: str, active_agents: list[str] | None = None) -> TenantConfig:
        extracted = self._extracted_config or await self.extract_config()
        has_skips = len(self.skipped_topics) > 0
        return TenantConfig(
            tenant_id=tenant_id,
            business_name=extracted.get("business_name", ""),
            industry=extracted.get("industry", "technology"),
            description=extracted.get("description", ""),
            icp=ICPConfig(**extracted.get("icp", {})),
            product=ProductConfig(**extracted.get("product", {})),
            gtm_playbook=GTMPlaybook(**extracted.get("gtm_playbook", {})),
            brand_voice=BrandVoice(**extracted.get("brand_voice", {})),
            active_agents=active_agents or extracted.get("recommended_agents", ["ceo", "content_writer"]),
            channels=extracted.get("channels", []),
            owner_email=owner_email,
            owner_name=owner_name,
            plan="starter",
            onboarding_status="completed" if not has_skips else "in_progress",
            skipped_fields=self.get_skipped_fields(),
        )
