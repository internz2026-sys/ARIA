"""Onboarding agent — builds GTM strategy through natural conversation.

The CEO agent conducts a structured intake conversation with developer founders
to understand their product, audience, and goals, then generates a GTM playbook
that all other agents reference.
"""

import asyncio
import json
import logging
import re

from backend.config.tenant_schema import (
    TenantConfig, ICPConfig, ProductConfig, GTMPlaybook, BrandVoice, GTMProfile,
)
from backend.tools.claude_cli import call_claude, MODEL_HAIKU, MODEL_SONNET

logger = logging.getLogger("aria.onboarding")

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
3. Who or what will you sell this to?
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
- audience = who or what the user sells to (person persona, business, org, etc.)
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

CHECKLIST VALIDATION — ACCEPT ANY ON-TOPIC ANSWER

Your default is to ACCEPT the user's answer and move on. Only re-ask when the answer is clearly NOT about the current topic.

CORE RULE: If the user's reply mentions anything related to the current field's topic, accept it and move on. Do NOT demand more specificity, do NOT demand demographics, do NOT demand titles or company sizes — any clear on-topic answer is enough.

EXAMPLES OF ACCEPTABLE ANSWERS (DO NOT RE-ASK THESE):
- For audience "Who or what will you sell this to?":
  - "Parochial schools in the Philippines" — ACCEPT (B2B answer, clear customer)
  - "Small business owners" — ACCEPT
  - "Developers" — ACCEPT
  - "Catholic schools" — ACCEPT
  - "K-12 administrators" — ACCEPT
- For channels: "email", "social", "all of them" — all ACCEPT
- For brand voice: "professional", "casual", "friendly" — all ACCEPT
- For goal: "grow revenue", "50 signups", "get users" — all ACCEPT

RE-ASK THE SAME QUESTION ONLY WHEN:
- The reply is completely empty or random characters
- The reply is clearly a question back to ARIA ("what do you mean?", "huh?")
- The reply is about a completely different topic (e.g. user answers with a pricing question when asked about audience)

When re-asking, be polite and brief.

NORMALIZATION
When accepting an on-topic answer, normalize casual wording into clean stored values:
- "grow" -> "increase growth"
- "online" -> ["content", "social"]
- "good" (for brand voice) -> "professional"
- "idk maybe email" -> ["email"]
- "all of them" for channels -> ["email", "social", "ads", "content"]

B2B vs B2C: both are valid customer answers. Schools, businesses,
organizations, or individuals — all count as legitimate "who will you sell to"
answers. Never reject a B2B answer just because it's not a person persona.

NORMALIZATION
Always normalize casual answers into clean stored values rather than asking again:
- "grow" -> "increase growth"
- "people" -> "general consumers"
- "online" -> ["content", "social"]
- "good" (for brand voice) -> "professional"
- "better service" -> "superior service quality"
- "idk maybe email" -> ["email"]

FIELD MATCHING
Only accept answers that match the CURRENT question's field. If the user's reply clearly belongs to a different topic than the one you just asked, do NOT store it — re-ask the current question instead.

INTELLIGENT EXTRACTION
- If the answer is on-topic for the current field, accept it.
- If the answer is off-topic or unrelated, re-ask the same question and do not advance.
- Never guess or infer values for fields that have not been explicitly asked and answered.

RE-ONBOARDING / EDIT MODE
- If previous onboarding exists, do not block access.
- Offer:
  - Restart onboarding
  - Edit specific answers
- In edit mode, ask only for the selected fields to update.
- Keep unchanged fields unchanged.
- Regenerate the full summary and GTM profile after edits.

FINAL OUTPUT FORMAT
After the 8th prompt is completed, output ONLY a human-readable summary. Do NOT output any JSON, code blocks, config objects, or technical markup.

Output exactly this and nothing else:

**Onboarding Complete**

**Business Name:** <value>
**Offer:** <value>
**Target Audience:** <value>
**Problem Solved:** <value>
**Differentiator:** <value>
**Channels:** <value>
**Brand Voice:** <value>
**30-Day Goal:** <value>

ARIA is ready for review.

CRITICAL FORMATTING RULES:
- Do NOT include any JSON blocks.
- Do NOT include any code blocks (no triple backticks).
- Do NOT include "Extracted Config:" or "GTM Profile:" sections.
- Do NOT include curly braces, square brackets, or key-value pairs.
- The summary must be plain text only — readable by a non-technical user.
- The structured config extraction happens separately via the extract-config endpoint.

TERMINATION RULE
After producing the final summary, output must end immediately.
No extra sentence may follow.
No additional assistant turn should be generated from the onboarding agent.

ABSOLUTE FINAL CONSTRAINT
If the final summary has been produced, your task is over.
Do not send any additional message after it.
Do not output JSON under any circumstances in the chat."""

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
  "recommended_agents": ["ceo", "content_writer", "email_marketer", "social_manager", "ad_strategist", "media"],
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
- "media" if they need marketing images, social media visuals, or ad creatives

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

# Canned fallback questions used when the LLM goes off-script and tries to
# produce a final summary before all 8 fields are answered. Keyed by field.
FIELD_QUESTIONS = {
    "business_name": "What is your business or brand name?",
    "product_or_offer": "What product, service, or offer do you sell?",
    "target_audience": "Who or what will you sell this to?",
    "problem_solved": "What main problem does your offer solve?",
    "differentiator": "What makes your offer different from competitors?",
    "channels": "Which channels should ARIA focus on first: email, social, ads, or content?",
    "brand_voice": "What tone should ARIA use for your brand: professional, friendly, bold, luxury, or casual?",
    "goal_30_days": "What is your main goal for the next 30 days?",
}

# Keyword patterns used to detect which onboarding field the LLM is CURRENTLY
# asking about. The source of truth for "where we are" is the LLM's latest
# question, NOT a blind counter. This stops the checklist from advancing
# when the LLM re-asks the same question (e.g. "Who is your ideal customer?"
# three times because the first answer was too vague).
_FIELD_QUESTION_KEYWORDS = [
    ("business_name", ["business or brand name", "business name", "brand name"]),
    ("product_or_offer", ["product, service, or offer", "do you sell", "what do you sell"]),
    ("target_audience", [
        "who or what will you sell",
        "sell this to",
        "ideal customer",
        "target audience",
        "who is your customer",
    ]),
    ("problem_solved", ["problem does your offer solve", "what problem", "pain point"]),
    ("differentiator", ["different from competitors", "makes your offer different", "differentiate"]),
    ("channels", ["which channels", "email, social, ads", "channels should aria"]),
    ("brand_voice", ["what tone", "brand voice", "professional, friendly"]),
    ("goal_30_days", ["next 30 days", "30-day goal", "main goal for the next"]),
]


def _detect_asked_field(text: str) -> str | None:
    """Detect which onboarding field the LLM's response is asking about.

    Only matches text containing a '?' to avoid false positives on the final
    summary (which has labels like "**Differentiator:**" but no question mark).
    """
    if "?" not in text:
        return None
    t = text.lower()
    for field, keywords in _FIELD_QUESTION_KEYWORDS:
        if any(kw in t for kw in keywords):
            return field
    return None


def _count_asks_for_field(messages: list[dict], field: str) -> int:
    """How many times has the LLM already asked about `field` in this session?

    Used to escape a re-ask loop — if the LLM has asked the same field 2+
    times, we force-accept on the next turn instead of letting it re-ask a
    third time.
    """
    keywords = dict(_FIELD_QUESTION_KEYWORDS).get(field, [])
    if not keywords:
        return 0
    count = 0
    for m in messages:
        if m.get("role") != "assistant":
            continue
        text = (m.get("content") or "").lower()
        if "?" not in text:
            continue
        if any(kw in text for kw in keywords):
            count += 1
    return count

def _ensure_generated_fields(gp: dict) -> dict:
    """Fill in positioning_summary and 30_day_gtm_focus if the model left them empty."""
    if not gp.get("positioning_summary"):
        biz = gp.get("business_name", "This business")
        audience = gp.get("audience", "its target customers")
        problem = gp.get("problem", "key challenges")
        offer = gp.get("offer", "its product")
        diff = gp.get("differentiator", "")
        diff_part = f", differentiated by {diff}" if diff else ""
        gp["positioning_summary"] = (
            f"{biz} helps {audience} solve {problem} through {offer}{diff_part}."
        )
    if not gp.get("30_day_gtm_focus"):
        channels = gp.get("primary_channels", [])
        ch_str = ", ".join(channels) if isinstance(channels, list) and channels else "key marketing channels"
        goal = gp.get("goal_30_days", "grow awareness and acquire users")
        voice = gp.get("brand_voice", "professional")
        gp["30_day_gtm_focus"] = (
            f"Over the next 30 days, prioritize {ch_str} to support the goal of {goal}, using a {voice} tone."
        )
    return gp


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

    @staticmethod
    def _strip_json_from_chat(text: str) -> str:
        """Remove JSON blocks, code fences, and raw config from visible chat text.

        This is a safety net — the LLM is instructed not to output JSON, but
        if it slips through, we strip it before showing to the user.
        """
        # Remove fenced code blocks (```json ... ``` or ``` ... ```)
        cleaned = re.sub(r'```[\s\S]*?```', '', text)
        # Remove standalone JSON objects (lines starting with { and ending with })
        cleaned = re.sub(r'^\s*\{[\s\S]*?\}\s*$', '', cleaned, flags=re.MULTILINE)
        # Remove section headers that precede JSON: "Extracted Config:", "GTM Profile:"
        cleaned = re.sub(r'\*{0,2}Extracted Config:?\*{0,2}\s*', '', cleaned)
        cleaned = re.sub(r'\*{0,2}GTM Profile:?\*{0,2}\s*', '', cleaned)
        # Collapse excessive blank lines
        cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
        return cleaned.strip()

    async def process_message(self, user_input: str) -> str:
        self.messages.append({"role": "user", "content": user_input})

        # Snapshot state BEFORE LLM call so we can compute progress hints.
        pre_validated = set(self.validated_fields)
        pre_current_idx = self.current_topic_index
        pre_current_field = (
            ONBOARDING_FIELDS[pre_current_idx]
            if pre_current_idx < len(ONBOARDING_FIELDS)
            else None
        )

        # Build progress directive injected into system prompt.
        answered = len(pre_validated) + len(self.skipped_topics)
        validated_list = ", ".join(sorted(pre_validated)) or "none"
        progress = (
            f"Fields validated so far: [{validated_list}] ({answered}/{self.max_questions}). "
        )
        if answered >= self.max_questions:
            progress += (
                "All topics complete — produce the final human-readable summary now. "
                "Do NOT ask another question. "
                "Do NOT output any JSON, code blocks, or config objects. "
                "Output ONLY the plain-text summary."
            )
        else:
            remaining = self.max_questions - answered
            prior_asks = (
                _count_asks_for_field(self.messages, pre_current_field)
                if pre_current_field else 0
            )
            progress += (
                f"Current question field: {pre_current_field}. "
                f"Only {answered} of {self.max_questions} questions answered "
                f"({remaining} remaining). "
                f"DO NOT produce the final summary yet. "
                f"DO NOT output 'Onboarding Complete' or any summary block. "
                f"You MUST use this EXACT wording for the current question: "
                f"\"{FIELD_QUESTIONS.get(pre_current_field or '', '')}\". "
                f"Do NOT paraphrase or rephrase this question — use it verbatim."
            )
            if prior_asks >= 2:
                # Escape hatch — we've already asked this field twice. Force
                # acceptance to break the re-ask loop regardless of how vague
                # the user's reply seems.
                progress += (
                    f" CRITICAL: You have already asked about '{pre_current_field}' "
                    f"{prior_asks} times. The user's last reply MUST be accepted "
                    f"as the answer — no matter how brief or imperfect it is. "
                    f"Do NOT re-ask again. Move on to the NEXT question immediately."
                )

        # Use higher max_tokens for the final summary. Use Sonnet instead of
        # Haiku for onboarding — Sonnet follows the strict validation rules
        # much better (Haiku tended to re-ask the same question 3-4 times or
        # hallucinate answers for un-asked fields).
        tokens = 1000 if answered >= self.max_questions else 500
        assistant_text = await call_claude(
            SYSTEM_PROMPT + "\n\n" + progress,
            messages=self.messages,
            max_tokens=tokens,
            model=MODEL_SONNET,
        )

        # Strip the [VALIDATED: ...] tag if present.
        cleaned_text, _tag_fields = _parse_validated_tag(assistant_text)

        # Safety net: strip any JSON that leaked into the visible response.
        cleaned_text = self._strip_json_from_chat(cleaned_text)

        # Safety net: if the LLM produced a premature "Onboarding Complete"
        # summary while fewer than max_questions fields are validated, replace
        # it with a canned next-question prompt.
        if (
            answered < self.max_questions
            and "onboarding complete" in cleaned_text.lower()
        ):
            cleaned_text = FIELD_QUESTIONS.get(
                pre_current_field or "",
                f"Tell me about: {(pre_current_field or 'the next topic').replace('_', ' ')}.",
            )
            logger.warning(
                "Onboarding: LLM produced premature summary at %d/%d; "
                "replaced with canned question for field=%s",
                answered, self.max_questions, pre_current_field,
            )

        # Advance validation based on the LLM's LATEST question. This is the
        # source of truth — if the LLM re-asks the same field, we do NOT
        # advance the checklist. If the LLM moves on to field X, every field
        # before X is considered answered.
        asked_field = _detect_asked_field(cleaned_text)
        is_final_summary = (
            answered >= self.max_questions - 1
            and "onboarding complete" in cleaned_text.lower()
        )

        if is_final_summary:
            # Final turn — validate every field. The summary confirms the flow
            # is done, including the last answer the user just provided.
            for f in ONBOARDING_FIELDS:
                self.validated_fields.add(f)
        elif asked_field and asked_field in ONBOARDING_FIELDS:
            asked_idx = ONBOARDING_FIELDS.index(asked_field)
            if asked_idx > pre_current_idx:
                # LLM moved on — user's last answer was accepted for
                # pre_current_field (and any fields between them).
                for f in ONBOARDING_FIELDS[pre_current_idx:asked_idx]:
                    self.validated_fields.add(f)
            else:
                # LLM is re-asking the same (or earlier) field. If this is
                # the 3rd+ ask of the same field, OVERRIDE the LLM — force
                # validation and ask the next field instead.
                prior_asks = _count_asks_for_field(self.messages, pre_current_field or "")
                # messages already has this turn's assistant reply appended
                # below, so prior_asks counts the current ask too after append.
                # We check before append: count assistant messages in self.messages
                # (does not yet include current cleaned_text).
                if pre_current_field and prior_asks >= 2:
                    logger.warning(
                        "Onboarding: LLM re-asked field=%s for the %dth time; "
                        "overriding — force-accepting and moving on",
                        pre_current_field, prior_asks + 1,
                    )
                    self.validated_fields.add(pre_current_field)
                    # Replace the LLM's repeated question with the next field's
                    # canned question.
                    next_idx = pre_current_idx + 1
                    if next_idx < len(ONBOARDING_FIELDS):
                        next_field = ONBOARDING_FIELDS[next_idx]
                        cleaned_text = FIELD_QUESTIONS.get(
                            next_field,
                            f"Tell me about: {next_field.replace('_', ' ')}.",
                        )
                    else:
                        # All fields answered — let completion detection handle.
                        cleaned_text = cleaned_text
                else:
                    logger.info(
                        "Onboarding: LLM re-asked field=%s (current=%s) — "
                        "user's answer was rejected, checklist not advanced",
                        asked_field, pre_current_field,
                    )
        # If no field detected and not a summary: ambiguous LLM output, keep
        # validation state unchanged. User's next reply will resolve it.

        # Store the cleaned text in conversation history.
        self.messages.append({"role": "assistant", "content": cleaned_text})

        # Completion — count-based only.
        if not self._complete and self.questions_answered >= self.max_questions:
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

    @staticmethod
    def _repair_json(s: str) -> str:
        """Best-effort fix for common LLM JSON mistakes."""
        # Strip markdown code fences
        s = re.sub(r'^```(?:json)?\s*', '', s, flags=re.MULTILINE)
        s = re.sub(r'```\s*$', '', s, flags=re.MULTILINE)
        # Remove JS-style comments
        s = re.sub(r'//[^\n]*', '', s)
        # Remove trailing commas before } or ]
        s = re.sub(r',\s*([}\]])', r'\1', s)
        # Remove control characters (except \n \r \t)
        s = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', s)
        # Fix unescaped newlines inside JSON strings: replace actual newlines
        # within string values with \\n
        s = re.sub(r'(?<=": ")(.*?)(?=")', lambda m: m.group(0).replace('\n', '\\n'), s, flags=re.DOTALL)
        return s

    @staticmethod
    def _extract_json(raw: str) -> str:
        """Pull the outermost JSON object from LLM output."""
        start = raw.find("{")
        if start == -1:
            return raw
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(raw)):
            c = raw[i]
            if escape:
                escape = False
                continue
            if c == '\\':
                escape = True
                continue
            if c == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    return raw[start:i + 1]
        # Unbalanced — return best guess
        end = raw.rfind("}") + 1
        return raw[start:end] if end > start else raw

    def _try_parse_json(self, raw: str) -> dict | None:
        """Try parsing raw LLM output as JSON with progressive repair."""
        json_str = self._extract_json(raw)
        for text in [json_str, self._repair_json(json_str)]:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                continue
        return None

    def _fallback_config_from_messages(self) -> dict:
        """Build a minimal config from conversation messages when JSON extraction fails."""
        user_msgs = [m['content'] for m in self.messages if m['role'] == 'user']
        fields = ONBOARDING_FIELDS[:]
        config: dict = {"business_name": "", "description": "", "channels": [], "gtm_profile": {}}
        gp: dict = {}

        for i, msg in enumerate(user_msgs):
            if i >= len(fields):
                break
            field = fields[i]
            val = msg.strip()
            if field == "business_name":
                config["business_name"] = val
                gp["business_name"] = val
            elif field == "product_or_offer":
                config["description"] = val
                gp["offer"] = val
            elif field == "target_audience":
                gp["audience"] = val
            elif field == "problem_solved":
                gp["problem"] = val
            elif field == "differentiator":
                gp["differentiator"] = val
            elif field == "channels":
                channels = [c.strip() for c in re.split(r'[,\n]+', val) if c.strip()]
                config["channels"] = channels
                gp["primary_channels"] = channels
            elif field == "brand_voice":
                gp["brand_voice"] = val
            elif field == "goal_30_days":
                gp["goal_30_days"] = val

        # Generate positioning_summary and 30_day_gtm_focus from collected fields
        gp = _ensure_generated_fields(gp)
        config["gtm_profile"] = gp
        return config

    async def extract_config(self) -> dict:
        conversation_text = "\n".join(
            f"{'ARIA' if m['role'] == 'assistant' else 'Founder'}: {m['content']}"
            for m in self.messages
        )

        # Attempt 1: standard extraction
        raw = await call_claude(
            "You are a structured data extraction assistant. Return ONLY valid JSON, no other text.",
            EXTRACTION_PROMPT + conversation_text,
            max_tokens=2000,
            model=MODEL_HAIKU,
        )
        result = self._try_parse_json(raw)
        if result:
            # Ensure generated fields are always populated
            if "gtm_profile" in result:
                result["gtm_profile"] = _ensure_generated_fields(result["gtm_profile"])
            self._extracted_config = result
            return result

        logger.warning("JSON parse failed on first attempt, retrying with stricter prompt")

        # Attempt 2: stricter prompt
        raw2 = await call_claude(
            "You are a JSON generator. Output ONLY a single valid JSON object.\n"
            "RULES:\n"
            "- No trailing commas\n"
            "- No comments\n"
            "- All string values on a single line (use \\n for newlines)\n"
            "- Escape all quotes inside strings with \\\"\n"
            "- No text before or after the JSON object",
            EXTRACTION_PROMPT + conversation_text,
            max_tokens=2000,
            model=MODEL_HAIKU,
        )
        result = self._try_parse_json(raw2)
        if result:
            if "gtm_profile" in result:
                result["gtm_profile"] = _ensure_generated_fields(result["gtm_profile"])
            self._extracted_config = result
            return result

        logger.warning("JSON parse failed on retry — using fallback from conversation messages")

        # Attempt 3: build config directly from user messages
        result = self._fallback_config_from_messages()
        self._extracted_config = result
        return result

    async def build_tenant_config(self, tenant_id: str, owner_email: str, owner_name: str, active_agents: list[str] | None = None) -> TenantConfig:
        extracted = self._extracted_config or await self.extract_config()
        has_skips = len(self.skipped_topics) > 0

        # Build GTMProfile from the flat gtm_profile extraction.
        gp_raw = extracted.get("gtm_profile", {})
        gtm_profile = GTMProfile(
            business_name=gp_raw.get("business_name", extracted.get("business_name", "")),
            offer=gp_raw.get("offer", extracted.get("description", "")),
            audience=gp_raw.get("audience", ""),
            problem=gp_raw.get("problem", ""),
            differentiator=gp_raw.get("differentiator", ""),
            positioning_summary=gp_raw.get("positioning_summary", ""),
            primary_channels=gp_raw.get("primary_channels", extracted.get("channels", [])),
            brand_voice=gp_raw.get("brand_voice", extracted.get("brand_voice", {}).get("tone", "")),
            goal_30_days=gp_raw.get("goal_30_days", ""),
            thirty_day_gtm_focus=gp_raw.get("30_day_gtm_focus", ""),
        )

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
            gtm_profile=gtm_profile,
            owner_email=owner_email,
            owner_name=owner_name,
            plan="starter",
            onboarding_status="completed" if not has_skips else "in_progress",
            skipped_fields=self.get_skipped_fields(),
        )
