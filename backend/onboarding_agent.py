"""Onboarding agent — builds GTM strategy through natural conversation.

The CEO agent conducts a structured intake conversation with developer founders
to understand their product, audience, and goals, then generates a GTM playbook
that all other agents reference.
"""

import asyncio
import json
import re

from backend.config.tenant_schema import (
    TenantConfig, ICPConfig, ProductConfig, GTMPlaybook, BrandVoice,
)
from backend.tools.claude_cli import call_claude, MODEL_HAIKU

SYSTEM_PROMPT = """You are ARIA's onboarding agent.

Your task is to run a controlled onboarding flow and then STOP.
You are not a general chat assistant during onboarding.

PRIMARY RULE
- Ask a maximum of 8 onboarding prompts only.
- After the 8th prompt is answered, do not say anything else except the final structured output.
- Do not add extra commentary, suggestions, acknowledgments, or follow-up messages after the 8th prompt.
- Do not respond conversationally after onboarding is complete.
- Your final output after the 8th prompt must be the summary/config only.

HARD STOP RULE
- Never ask a 9th onboarding question.
- Never send a "thanks", "great", "you're all set", "anything else?", or similar follow-up after the final output.
- Once onboarding is complete, terminate with the final structured summary only.

QUESTION RULES
- Ask only one prompt at a time.
- Keep prompts short and direct.
- Do not ask unnecessary follow-ups.
- If the user already provided information for future prompts, extract it and skip those prompts.
- If the answer is vague but usable, normalize it and continue.
- If the user refuses to answer, store "not specified" and continue.
- If onboarding data already exists, allow:
  1. restart full onboarding
  2. edit specific answers only

THE 8 REQUIRED PROMPTS
1. What is your business or brand name?
2. What product, service, or offer do you sell?
3. Who is your ideal customer?
4. What main problem does your offer solve?
5. What makes your offer different from competitors?
6. Which channels should ARIA focus on first: email, social, ads, or content?
7. What tone should ARIA use for your brand: professional, friendly, bold, luxury, or casual?
8. What is your main goal for the next 30 days?

CRITICAL EXTRACTION RULE
You must always extract BOTH:
1. onboarding config fields
2. GTM profile summary fields

Do not leave GTM summary fields blank if the answer can be inferred from the user's responses.

GTM PROFILE REQUIREMENTS
After onboarding, generate a GTM profile summary using the collected answers.

The GTM profile must always include:
- business_name
- offer
- audience
- problem
- differentiator
- positioning_summary
- primary_channels
- brand_voice
- goal_30_days
- 30_day_gtm_focus

FIELD MAPPING RULES
- business_name = business/brand name
- offer = product/service/offer
- audience = ideal customer
- problem = main problem solved
- differentiator = unique advantage
- primary_channels = selected channels
- brand_voice = selected tone
- goal_30_days = main 30-day goal

GTM INFERENCE RULES
- positioning_summary must be a 1-2 sentence summary of who the business helps, what it offers, and why it is different.
- 30_day_gtm_focus must be a 1-2 sentence practical GTM direction using the user's goal + channels.
- Never leave positioning_summary empty.
- Never leave 30_day_gtm_focus empty.
- If the user provides limited detail, create the best valid summary from available answers.

EXAMPLES OF VALID INFERENCE
- positioning_summary:
  "<Business> helps <audience> solve <problem> through <offer>, differentiated by <differentiator>."
- 30_day_gtm_focus:
  "Over the next 30 days, ARIA should prioritize <channels> to support the goal of <goal_30_days>, using a <brand_voice> tone."

SEMANTIC ANSWER UNDERSTANDING RULE
- Do not require the user's answer to exactly match the wording of the question.
- Consider an answer valid if it clearly conveys the meaning needed for that onboarding field.
- Use intent and semantic meaning, not exact phrasing.
- Accept natural language, short phrases, partial sentences, and conversational answers as long as the meaning is usable.
- The checklist should be checked when the answer is reasonably interpretable as a valid response to the field.

VALIDATION PRINCIPLE
- Validate based on "Does this answer provide usable business information for this field?"
- Do not validate based on "Does this answer use the expected keywords?"
- If the answer is understandable and can be saved meaningfully, mark it complete.

ANSWER VALIDATION RULES
- A checklist item must only be marked complete if the user's response meaningfully answers that specific onboarding question.
- Do not mark a question complete just because the user replied with any text.
- Do not mark a question complete if the response is truly unrelated or provides zero usable information.
- You must classify each user reply against the 8 onboarding fields before updating progress.
- Only check the checklist item if the answer matches the current question or clearly answers one of the 8 onboarding fields.

FUZZY FIELD MATCHING
Classify each answer by meaning into one or more of these fields:
1. business_name
2. product_or_offer
3. target_audience
4. problem_solved
5. differentiator
6. channels
7. brand_voice
8. goal_30_days

If the answer strongly or reasonably maps to the intended field, accept it.

CHECKLIST UPDATE RULE
- If the current question is answered properly, mark only that question complete.
- If the user also included valid answers for later questions, mark those later questions complete too.
- If the current question is not answered, do not check it.
- If the answer is for a different onboarding field, store that field but still ask the current unanswered question.

EXAMPLES OF ACCEPTABLE ANSWERS

Business name:
- "We're called Northstar Media" -> valid
- "Northstar" -> valid
- "My brand is GlowSkin Co." -> valid

Product / offer:
- "We do social media management for dentists" -> valid
- "Organic skincare for teens" -> valid
- "We help businesses automate lead follow-up" -> valid

Target audience:
- "Mostly small business owners" -> valid
- "College students and fresh grads" -> valid
- "Local restaurants that want more customers" -> valid

Problem solved:
- "We help them get more leads" -> valid
- "It makes scheduling easier for clinics" -> valid
- "We reduce manual admin work" -> valid

Differentiator:
- "We do everything done-for-you" -> valid
- "We specialize only in dental clinics" -> valid
- "We deliver faster than most agencies" -> valid

Channels:
- "Instagram and email" -> valid
- "Mostly content and paid ads" -> valid
- "Social first, then email later" -> valid
- "probably instagram and fb" -> valid, normalize to ["social"]

Brand voice:
- "Clean and professional" -> valid
- "Casual but still credible" -> valid
- "Friendly, modern, and confident" -> valid
- "kind of professional but not too stiff" -> valid, normalize to "professional and approachable"

30-day goal:
- "Get more booked calls" -> valid
- "Increase sales this month" -> valid
- "Generate 20 quality leads" -> valid
- "get more customers" -> valid, normalize to "increase customer acquisition"

DO NOT BE OVERLY STRICT
- Do not reject an answer just because it is short.
- Do not reject an answer just because it does not repeat the field name.
- Do not reject an answer just because it is phrased differently from the prompt.
- Do not require perfect grammar or complete sentences.

WHEN TO ACCEPT
Accept the answer if:
- its meaning is clear enough to store
- it reasonably answers the current field
- it can be normalized into a usable config value

WHEN TO ASK AGAIN
Ask again only if:
- the reply is truly unrelated to any onboarding field
- the reply is too vague to store meaningfully (e.g. just "yes", "ok", "idk")
- the reply could belong to multiple fields and cannot be resolved
- the reply does not provide any usable business information

NORMALIZATION RULE
If the user gives a semantically valid answer in casual wording, normalize it into a clean stored value.
Examples:
- "probably instagram and fb" -> ["social"]
- "kind of professional but not too stiff" -> "professional and approachable"
- "get more customers" -> "increase customer acquisition"

INVALID ANSWER HANDLING
- If the user response is truly unrelated or provides no usable information, ask a short corrective prompt for that same question.
- Do not move to the next question.
- Do not check the checklist.

Recovery prompt examples:
- "Please provide your business name."
- "What exactly do you sell?"
- "Who is your ideal customer specifically?"
- "Please choose one or more: email, social, ads, content."
- "What is your main goal for the next 30 days in one sentence?"

INTELLIGENT EXTRACTION RULE
- Identify whether the answer belongs to the current question, a future question, multiple questions, or none.
- Do not confuse brand voice with differentiator, or audience with problem solved, etc.
- If a message contains multiple valid answers, extract them into the correct fields.
- If a message contains no valid onboarding answer, keep the same question active.
- Prefer accepting usable answers over unnecessarily asking again.

PROGRESS METADATA TAG (REQUIRED)
After EVERY response, you MUST append a metadata tag on its own line at the very end:

[VALIDATED: field1, field2, field3]

Where field1, field2, etc. are the field names from this list that NOW have valid answers:
business_name, product_or_offer, target_audience, problem_solved, differentiator, channels, brand_voice, goal_30_days

Rules for the metadata tag:
- Include ALL fields that have been validly answered so far (cumulative).
- If no new field was validated this turn, still include all previously validated fields.
- If no fields have valid answers yet, use: [VALIDATED: none]
- This tag must appear on the LAST line of every response, including the final summary.
- The tag is machine-parsed and will be stripped before showing to the user.

Examples:
- User answers business name validly: [VALIDATED: business_name]
- User answers business name and product in one message: [VALIDATED: business_name, product_or_offer]
- User gives weak answer, nothing new validated: [VALIDATED: business_name] (keep previous)
- First message, no valid answer yet: [VALIDATED: none]

RE-ONBOARDING / EDIT MODE
- If previous onboarding exists, do not block access.
- Offer:
  - Restart onboarding
  - Edit specific answers
- In edit mode, ask only for the selected fields to update.
- Keep unchanged fields unchanged.
- Regenerate the full summary and GTM profile after edits.

FINAL OUTPUT FORMAT
After the 8th prompt is completed, output exactly this and nothing else:

**Onboarding Complete**

**Summary:**
- **Business name:** <value>
- **Offer:** <value>
- **Target audience:** <value>
- **Problem solved:** <value>
- **Differentiator:** <value>
- **Channels:** <value>
- **Brand voice:** <value>
- **30-day goal:** <value>

**Extracted Config:**
```json
{
  "business_name": "<value>",
  "product_or_offer": "<value>",
  "target_audience": "<value>",
  "problem_solved": "<value>",
  "differentiator": "<value>",
  "channels": ["<value>"],
  "brand_voice": "<value>",
  "goal_30_days": "<value>"
}
```

**GTM Profile:**
```json
{
  "business_name": "<value>",
  "offer": "<value>",
  "audience": "<value>",
  "problem": "<value>",
  "differentiator": "<value>",
  "positioning_summary": "<generated summary>",
  "primary_channels": ["<value>"],
  "brand_voice": "<value>",
  "goal_30_days": "<value>",
  "30_day_gtm_focus": "<generated GTM direction>"
}
```

TERMINATION RULE
After producing the final Summary + Extracted Config + GTM Profile, output must end immediately.
No extra sentence may follow.
No additional assistant turn should be generated from the onboarding agent.

ABSOLUTE FINAL CONSTRAINT
If the final structured output has been produced, your task is over.
Do not send any additional message after it."""

EXTRACTION_PROMPT = """Based on the conversation below, extract a structured business configuration, GTM strategy, and GTM profile as JSON.

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
  "recommended_agents": ["ceo", "content_writer", "email_marketer", "social_manager", "ad_strategist"],
  "gtm_profile": {
    "business_name": "",
    "offer": "",
    "audience": "",
    "problem": "",
    "differentiator": "",
    "positioning_summary": "",
    "primary_channels": [],
    "brand_voice": "",
    "goal_30_days": "",
    "30_day_gtm_focus": ""
  }
}

CRITICAL: The gtm_profile section must ALWAYS be populated:
- positioning_summary: 1-2 sentence summary of who the business helps, what it offers, and why it is different.
  Example: "<Business> helps <audience> solve <problem> through <offer>, differentiated by <differentiator>."
- 30_day_gtm_focus: 1-2 sentence practical GTM direction using the user's goal + channels.
  Example: "Over the next 30 days, ARIA should prioritize <channels> to support the goal of <goal_30_days>, using a <brand_voice> tone."
- Never leave positioning_summary or 30_day_gtm_focus empty. Infer from available answers.

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


# The 8 onboarding fields in order, matching the system prompt.
ONBOARDING_FIELDS = [
    "business_name",
    "product_or_offer",
    "target_audience",
    "problem_solved",
    "differentiator",
    "channels",
    "brand_voice",
    "goal_30_days",
]

# Legacy topic names used by the frontend skip UI — maps to field names.
ONBOARDING_TOPICS = ONBOARDING_FIELDS

# Map field name to the config paths that stay empty when skipped.
TOPIC_SKIPPED_FIELDS = {
    "business_name": ["business_name"],
    "product_or_offer": ["product"],
    "target_audience": ["icp"],
    "problem_solved": ["icp.pain_points"],
    "differentiator": ["product.differentiators", "gtm_playbook.competitor_differentiation"],
    "channels": ["channels", "gtm_playbook.channel_strategy"],
    "brand_voice": ["brand_voice"],
    "goal_30_days": ["gtm_playbook.action_plan_30"],
}

# Regex to parse the [VALIDATED: ...] metadata tag from LLM responses.
_VALIDATED_RE = re.compile(
    r"\n?\[VALIDATED:\s*(none|[a-z0-9_,\s]+)\]\s*$", re.IGNORECASE
)


def _parse_validated_tag(text: str) -> tuple[str, set[str]]:
    """Parse and strip the [VALIDATED: ...] metadata tag from LLM output.

    Returns (cleaned_text, set_of_validated_fields).
    """
    m = _VALIDATED_RE.search(text)
    if not m:
        return text, set()
    tag_content = m.group(1).strip().lower()
    cleaned = text[: m.start()].rstrip()
    if tag_content == "none":
        return cleaned, set()
    fields = {f.strip() for f in tag_content.split(",") if f.strip()}
    # Only keep recognized field names.
    valid = fields & set(ONBOARDING_FIELDS)
    return cleaned, valid


class OnboardingAgent:
    def __init__(self):
        self.messages: list[dict] = []
        self.max_questions = 8
        self._complete = False
        self._extracted_config: dict | None = None
        self.skipped_topics: list[str] = []
        self.validated_fields: set[str] = set()

    @property
    def questions_answered(self) -> int:
        return len(self.validated_fields) + len(self.skipped_topics)

    @property
    def current_topic_index(self) -> int:
        """Index of the first field that is neither validated nor skipped."""
        for i, field in enumerate(ONBOARDING_FIELDS):
            if field not in self.validated_fields and field not in self.skipped_topics:
                return i
        return len(ONBOARDING_FIELDS)

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

        # Build progress directive injected into system prompt.
        answered = self.questions_answered
        validated_list = ", ".join(sorted(self.validated_fields)) or "none"
        progress = (
            f"Fields validated so far: [{validated_list}] ({answered}/{self.max_questions}). "
        )
        if answered >= self.max_questions:
            progress += (
                "All topics complete — produce the final structured output now. "
                "Do NOT ask another question. "
                "Do NOT add any text after the GTM Profile JSON block. "
                "Still include the [VALIDATED: ...] tag on the last line."
            )
        else:
            next_field = ONBOARDING_FIELDS[self.current_topic_index]
            progress += f"Current question field: {next_field}."

        # Use higher max_tokens for the final summary which includes JSON blocks.
        tokens = 1500 if answered >= self.max_questions else 500
        assistant_text = await call_claude(
            SYSTEM_PROMPT + "\n\n" + progress,
            messages=self.messages,
            max_tokens=tokens,
            model=MODEL_HAIKU,
        )

        # Parse the [VALIDATED: ...] metadata tag and strip it from the response.
        cleaned_text, new_validated = _parse_validated_tag(assistant_text)

        # Update validated fields (cumulative — LLM reports all valid fields).
        if new_validated:
            self.validated_fields = new_validated

        # Store the cleaned text (without metadata) in conversation history.
        self.messages.append({"role": "assistant", "content": cleaned_text})

        # Detect completion.
        if not self._complete and self.questions_answered >= self.max_questions:
            self._complete = True
        if not self._complete and "onboarding complete" in cleaned_text.lower():
            self._complete = True

        return cleaned_text

    def skip_current_topic(self) -> str:
        """Skip the current onboarding topic. Returns the name of the skipped topic."""
        idx = self.current_topic_index
        if idx >= len(ONBOARDING_FIELDS):
            return ""
        topic = ONBOARDING_FIELDS[idx]
        self.skipped_topics.append(topic)
        self.messages.append({"role": "user", "content": f"[Skipped: {topic}]"})
        # Check for completion after skip.
        if self.questions_answered >= self.max_questions:
            self._complete = True
        return topic

    def get_current_topic(self) -> str:
        """Return the current topic being asked about."""
        idx = self.current_topic_index
        if idx < len(ONBOARDING_FIELDS):
            return ONBOARDING_FIELDS[idx]
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
