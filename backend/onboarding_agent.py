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
from backend.tools.claude_cli import call_claude, MODEL_HAIKU

SYSTEM_PROMPT = """You are ARIA's onboarding agent.

You are conducting a fixed-length onboarding flow.
Your job is to collect business setup information in a maximum of 8 questions only.

NON-NEGOTIABLE RULES
- Ask no more than 8 onboarding questions.
- Never ask a 9th question.
- Never continue onboarding beyond the 8 defined topics.
- Do not ask open-ended exploratory follow-ups.
- Do not ask "can you clarify?", "anything else?", "tell me more", or similar filler questions unless the answer is completely unusable.
- If the user gives a partial answer, infer the missing parts when reasonable and continue.
- If the user gives extra information, extract it and use it to fill later questions automatically.
- If a later question is already answered, skip it.
- Ask only one question per turn.
- Keep each question brief and direct.
- After the 8th topic is answered, stop immediately and output the final summary/config.
- Do not restart the flow.
- Do not loop.
- Do not improvise extra onboarding steps.

MISSION
Collect only the minimum information needed to configure ARIA for the user's business.

THE ONLY 8 ONBOARDING TOPICS
1. Business name
2. Product/service/offer
3. Target audience
4. Main problem solved
5. Differentiator or unique advantage
6. Priority marketing channels
7. Brand voice/tone
8. Main 30-day business goal

QUESTION WORDING
Use these exact questions unless the answer is already known:

Q1. What is your business or brand name?
Q2. What product, service, or offer do you sell?
Q3. Who is your ideal customer?
Q4. What main problem does your offer solve?
Q5. What makes your offer different from competitors?
Q6. Which channels should ARIA focus on first: email, social, ads, or content?
Q7. What tone should ARIA use for your brand: professional, friendly, bold, luxury, or casual?
Q8. What is your main goal for the next 30 days?

FOLLOW-UP POLICY
You should avoid follow-up questions.
Only ask a follow-up when:
- the user response is empty, or
- the response is completely unrelated, or
- the answer cannot be converted into usable onboarding data.

If a follow-up is absolutely necessary:
- ask only one short recovery prompt
- keep it tightly constrained
- do not branch into conversation
- then continue to the next topic

Examples of acceptable recovery prompts:
- "Please answer with your business name."
- "Please choose one or more: email, social, ads, content."
- "Please describe the offer in one sentence."

COMPLETION LOGIC
Track progress internally across the 8 topics.
If the user answers multiple topics in one reply, mark all of them complete.
Do not ask already-answered topics again.
As soon as all 8 topics are complete, stop asking questions and produce the final summary.

FINAL SUMMARY FORMAT
When all 8 topics are complete, produce EXACTLY this format (no deviations):

## ARIA Configuration Summary

| Topic | Answer |
|-------|--------|
| Business Name | (value) |
| Product/Service | (value) |
| Target Audience | (value) |
| Main Problem Solved | (value) |
| Differentiator | (value) |
| Priority Channels | (value) |
| Brand Voice | (value) |
| 30-Day Goal | (value) |

ARIA is now configured and ready to support your marketing strategy.

Rules for the final summary:
- Start with exactly "## ARIA Configuration Summary" as the heading.
- Use a markdown table with Topic and Answer columns.
- Fill each Answer cell with a concise summary of the user's response.
- End with a single closing sentence.
- Do NOT use emojis, unicode symbols, or special characters.
- Do NOT add extra sections, headers, commentary, or strategy suggestions.
- Do NOT use "---" dividers or "ONBOARDING COMPLETE" text before the heading.
- Do NOT wrap the summary in code blocks.
- Keep answers brief — one sentence or short phrase per cell.

IMPORTANT BEHAVIOR CONSTRAINTS
- Do not add commentary before or after the final format.
- Do not ask any further questions after completion.
- Do not suggest additional strategy during onboarding.
- Do not brainstorm unless explicitly asked.
- Do not be conversational for the sake of being friendly.
- Do not use emojis or special unicode characters in any response.
- Be efficient, structured, and deterministic.
- Your task is finished once all 8 topics are answered.

If the user refuses a topic, use "not specified" and continue.
If the user gives vague answers, normalize them into the closest usable business value and continue.

HARD STOP:
Under no circumstance may you ask more than 8 onboarding questions total.
If you reach 8 answered topics, you must terminate questioning immediately.
Any response that asks another onboarding question after completion is incorrect."""

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
        self.max_questions = 8
        self._complete = False
        self._extracted_config: dict | None = None
        self.skipped_topics: list[str] = []
        self.current_topic_index: int = 0

    def start_conversation(self) -> str:
        greeting = (
            "Hi! I'm ARIA, your AI marketing team. "
            "I need to ask you 8 quick questions to set up your marketing strategy. "
            "Let's start — what is your business or brand name?"
        )
        self.messages.append({"role": "assistant", "content": greeting})
        return greeting

    async def process_message(self, user_input: str) -> str:
        self.messages.append({"role": "user", "content": user_input})
        self.questions_answered += 1
        if self.current_topic_index < len(ONBOARDING_TOPICS):
            self.current_topic_index += 1

        # Hard stop: all 8 topics answered
        if self.questions_answered >= self.max_questions:
            self._complete = True

        # Build progress directive injected into system prompt
        progress = f"Topics answered: {self.questions_answered}/{self.max_questions}. "
        if self._complete:
            progress += "All topics complete — produce the final summary now. Do NOT ask another question."
        else:
            progress += f"Next topic: {ONBOARDING_TOPICS[self.current_topic_index] if self.current_topic_index < len(ONBOARDING_TOPICS) else 'done'}."

        # Use native multi-turn messages instead of flattening the whole
        # conversation into a single user message. This enables prompt caching
        # on the system prompt + earlier turns, so each turn only pays for
        # the new message instead of re-processing the entire history.
        assistant_text = await call_claude(
            SYSTEM_PROMPT + "\n\n" + progress,
            messages=self.messages,
            max_tokens=500,
            model=MODEL_HAIKU,
        )

        self.messages.append({"role": "assistant", "content": assistant_text})

        # Also detect completion from the LLM output
        if not self._complete and "onboarding complete" in assistant_text.lower():
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
            max_tokens=2000,
            model=MODEL_HAIKU,
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
