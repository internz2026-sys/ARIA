"""Onboarding agent — deterministic 8-question GTM intake.

Design: the BACKEND drives the flow. The LLM is used only to classify whether
a user's reply is on-topic for the current field and (at the end) to extract
a richer config. Questions are canned. This design prevents the failure modes
the old LLM-driven version hit: re-asking the same question, regressing to
already-validated fields, hallucinating answers, and producing premature
summaries.

State machine (per field): pending -> validated OR pending -> skipped.
Current field = first pending field. Complete = all fields validated/skipped.

Per field we track up to 3 attempts:
  1st attempt → classify; on-topic = validate, off-topic = re-ask
  2nd attempt → classify; on-topic = validate, off-topic = re-ask
  3rd attempt → force-validate regardless of classification
"""

import json
import logging
import re

from backend.config.tenant_schema import (
    TenantConfig, ICPConfig, ProductConfig, GTMPlaybook, BrandVoice, GTMProfile,
)
from backend.tools.claude_cli import call_claude, MODEL_HAIKU

logger = logging.getLogger("aria.onboarding")


# ──────────────────────────────────────────────────────────────────────────
# Field schema — order, canned questions, skip mappings
# ──────────────────────────────────────────────────────────────────────────

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

# Human-readable label used in the final summary.
FIELD_LABELS = {
    "business_name": "Business Name",
    "product_or_offer": "Offer",
    "target_audience": "Target Audience",
    "problem_solved": "Problem Solved",
    "differentiator": "Differentiator",
    "channels": "Channels",
    "brand_voice": "Brand Voice",
    "goal_30_days": "30-Day Goal",
}

# Config paths that stay empty when a topic is skipped (used by the profile
# editor to know which fields to prompt for later).
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

# Max attempts per field before we force-accept whatever the user said.
# 1=first ask, 2=re-ask, 3=final ask → force accept on the 3rd reply.
MAX_ATTEMPTS_PER_FIELD = 3


# ──────────────────────────────────────────────────────────────────────────
# Prompts
# ──────────────────────────────────────────────────────────────────────────

CLASSIFIER_PROMPT = """You are a binary classifier. Reply with ONLY the single word YES or NO.

The user was asked a question about a specific field. Decide if their reply is on-topic.

YES if the reply is relevant to the field's topic — even briefly, casually, or partially.
NO only if the reply is empty, random characters, or clearly about a different topic.

Field-specific guidance (all YES):
- business_name: any name, brand, product line, org name
- product_or_offer: any description of what they sell or offer
- target_audience: any customer type (person, business, school, org, industry, persona)
- problem_solved: any pain point, problem, or what the offer fixes
- differentiator: anything that makes them different, unique, or better
- channels: email, social, ads, content, newsletters, X, LinkedIn, etc.
- brand_voice: professional, casual, friendly, bold, formal, playful, luxury, etc.
- goal_30_days: any goal, milestone, KPI, or outcome for the next ~month

NO only for:
- Empty or whitespace-only replies
- Random characters / pure gibberish
- Questions back to ARIA ("what do you mean?", "can you explain?")
- Clearly different topic (e.g. pricing question when asked about audience)

Output format: just YES or NO. No explanation."""

EXTRACTION_PROMPT = """Based on the 8 onboarding answers below, extract a structured business configuration as JSON.

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
- 30_day_gtm_focus: 1-2 sentence practical GTM direction using the user's goal + channels.

recommended_agents: include "ceo" and "content_writer" always. Add "email_marketer" / "social_manager" / "ad_strategist" / "media" based on the channels and goal.

ANSWERS:
"""


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────


def _ensure_generated_fields(gp: dict) -> dict:
    """Fill in positioning_summary and 30_day_gtm_focus deterministically."""
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


def _normalize_channels(text: str) -> list[str]:
    """Parse a channels reply into a list of channel names."""
    t = text.lower()
    known = ["email", "social", "ads", "content"]
    found = [c for c in known if c in t]
    if "all" in t or "every" in t:
        return known
    if not found:
        # Fallback: split on commas/newlines
        parts = [p.strip() for p in re.split(r"[,\n]+", text) if p.strip()]
        return parts or ["content"]
    return found


# ──────────────────────────────────────────────────────────────────────────
# Agent
# ──────────────────────────────────────────────────────────────────────────


class OnboardingAgent:
    """Deterministic 8-question onboarding state machine."""

    def __init__(self):
        self.messages: list[dict] = []
        self.field_state: dict[str, str] = {f: "pending" for f in ONBOARDING_FIELDS}
        self.field_answers: dict[str, str] = {}
        self.attempts: dict[str, int] = {f: 0 for f in ONBOARDING_FIELDS}
        self.max_questions = len(ONBOARDING_FIELDS)
        self._complete = False
        self._extracted_config: dict | None = None

    # ── Legacy-compatible properties used by server.py ──────────────────────

    @property
    def validated_fields(self) -> set[str]:
        return {f for f, s in self.field_state.items() if s == "validated"}

    @property
    def skipped_topics(self) -> list[str]:
        return [f for f in ONBOARDING_FIELDS if self.field_state[f] == "skipped"]

    @property
    def questions_answered(self) -> int:
        return sum(1 for s in self.field_state.values() if s in ("validated", "skipped"))

    @property
    def current_field(self) -> str | None:
        """First pending field, or None if every field is done."""
        for f in ONBOARDING_FIELDS:
            if self.field_state[f] == "pending":
                return f
        return None

    @property
    def current_topic_index(self) -> int:
        """Index of the current field (or len(ONBOARDING_FIELDS) if complete)."""
        current = self.current_field
        return ONBOARDING_FIELDS.index(current) if current else len(ONBOARDING_FIELDS)

    def get_current_topic(self) -> str:
        return self.current_field or ""

    def get_skipped_fields(self) -> list[str]:
        fields = []
        for topic in self.skipped_topics:
            fields.extend(TOPIC_SKIPPED_FIELDS.get(topic, []))
        return fields

    def is_complete(self) -> bool:
        return self._complete

    # ── Dialogue ───────────────────────────────────────────────────────────

    def start_conversation(self) -> str:
        """Return the initial greeting with the first question."""
        greeting = (
            "Hi! I'm ARIA, your AI marketing team. "
            "I'll ask you 8 quick questions to set up your marketing strategy. "
            f"Let's start — {FIELD_QUESTIONS['business_name']}"
        )
        self.messages.append({"role": "assistant", "content": greeting})
        self.attempts["business_name"] = 1
        return greeting

    async def process_message(self, user_input: str) -> str:
        """Classify user's reply, advance state, return next assistant message."""
        self.messages.append({"role": "user", "content": user_input})

        current = self.current_field
        if current is None:
            # All fields handled; summary already produced earlier. Just echo.
            reply = self._build_final_summary()
            self.messages.append({"role": "assistant", "content": reply})
            return reply

        attempts_so_far = self.attempts.get(current, 0)
        on_topic = await self._classify_answer(current, user_input)

        force_accept = attempts_so_far >= MAX_ATTEMPTS_PER_FIELD
        if on_topic or force_accept:
            # Store the answer — use the user's most recent reply.
            self.field_answers[current] = user_input.strip() or "not specified"
            self.field_state[current] = "validated"
            if force_accept and not on_topic:
                logger.warning(
                    "Onboarding: force-accepted off-topic answer for field=%s "
                    "after %d attempts",
                    current, attempts_so_far,
                )
            self._maybe_complete()
            reply = self._next_prompt()
        else:
            # Re-ask with a brief nudge.
            reply = self._gentle_reask(current)

        self.messages.append({"role": "assistant", "content": reply})
        return reply

    def skip_current_topic(self) -> str:
        """User clicked Skip. Mark current field as skipped."""
        current = self.current_field
        if not current:
            return ""
        self.field_state[current] = "skipped"
        self.messages.append({"role": "user", "content": f"[Skipped: {current}]"})
        self._maybe_complete()
        return current

    # ── Internal state helpers ─────────────────────────────────────────────

    def _maybe_complete(self):
        if all(s in ("validated", "skipped") for s in self.field_state.values()):
            self._complete = True

    def _next_prompt(self) -> str:
        """Return either the next question or the final summary."""
        if self._complete:
            return self._build_final_summary()
        next_field = self.current_field
        if next_field is None:
            return self._build_final_summary()
        # Bump attempt counter for the field we're about to ask.
        self.attempts[next_field] = self.attempts.get(next_field, 0) + 1
        return FIELD_QUESTIONS[next_field]

    def _gentle_reask(self, field: str) -> str:
        """Re-ask the same question with a brief explanation."""
        self.attempts[field] = self.attempts.get(field, 0) + 1
        remaining = MAX_ATTEMPTS_PER_FIELD - self.attempts[field]
        nudge = (
            "I need a bit more detail that matches the question. "
            if remaining >= 1
            else "One more try — "
        )
        return f"{nudge}{FIELD_QUESTIONS[field]}"

    def _build_final_summary(self) -> str:
        """Deterministic summary — values come straight from stored answers."""
        def val(f: str) -> str:
            ans = self.field_answers.get(f, "")
            if self.field_state.get(f) == "skipped" or not ans.strip():
                return "not specified"
            return ans.strip()

        lines = ["**Onboarding Complete**", ""]
        for f in ONBOARDING_FIELDS:
            lines.append(f"**{FIELD_LABELS[f]}:** {val(f)}")
        lines += ["", "ARIA is ready for review."]
        return "\n".join(lines)

    # ── Classifier (LLM, Haiku) ────────────────────────────────────────────

    async def _classify_answer(self, field: str, text: str) -> bool:
        """Return True if `text` is on-topic for `field`."""
        if not text or not text.strip():
            return False

        user_prompt = (
            f"Field being asked about: {field}\n"
            f"Question: {FIELD_QUESTIONS[field]}\n"
            f"User's reply: {text.strip()}\n\n"
            "Is the reply on-topic for this field? Answer YES or NO."
        )
        try:
            resp = await call_claude(
                CLASSIFIER_PROMPT,
                user_prompt,
                max_tokens=8,
                model=MODEL_HAIKU,
            )
            head = (resp or "").strip().upper()[:5]
            return head.startswith("YES")
        except Exception as e:
            logger.warning(
                "Onboarding classifier failed for field=%s (%s); defaulting to accept",
                field, e,
            )
            return True  # Fail-open: don't block the user on LLM errors.

    # ── Config extraction ──────────────────────────────────────────────────

    @staticmethod
    def _repair_json(s: str) -> str:
        s = re.sub(r'^```(?:json)?\s*', '', s, flags=re.MULTILINE)
        s = re.sub(r'```\s*$', '', s, flags=re.MULTILINE)
        s = re.sub(r'//[^\n]*', '', s)
        s = re.sub(r',\s*([}\]])', r'\1', s)
        s = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', s)
        s = re.sub(r'(?<=": ")(.*?)(?=")', lambda m: m.group(0).replace('\n', '\\n'), s, flags=re.DOTALL)
        return s

    @staticmethod
    def _extract_json(raw: str) -> str:
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
        end = raw.rfind("}") + 1
        return raw[start:end] if end > start else raw

    def _try_parse_json(self, raw: str) -> dict | None:
        json_str = self._extract_json(raw)
        for text in [json_str, self._repair_json(json_str)]:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                continue
        return None

    def _build_gtm_profile_from_answers(self) -> dict:
        """Authoritative gtm_profile built from stored field answers."""
        ans = self.field_answers
        gp = {
            "business_name": ans.get("business_name", ""),
            "offer": ans.get("product_or_offer", ""),
            "audience": ans.get("target_audience", ""),
            "problem": ans.get("problem_solved", ""),
            "differentiator": ans.get("differentiator", ""),
            "primary_channels": _normalize_channels(ans.get("channels", "")),
            "brand_voice": ans.get("brand_voice", ""),
            "goal_30_days": ans.get("goal_30_days", ""),
        }
        return _ensure_generated_fields(gp)

    def _fallback_config_from_messages(self) -> dict:
        """Build a minimal config straight from stored answers (no LLM)."""
        ans = self.field_answers
        channels = _normalize_channels(ans.get("channels", ""))
        config: dict = {
            "business_name": ans.get("business_name", ""),
            "description": ans.get("product_or_offer", ""),
            "industry": "technology",
            "channels": channels,
            "gtm_profile": self._build_gtm_profile_from_answers(),
        }
        return config

    async def extract_config(self) -> dict:
        """Generate a rich structured config via LLM, backed by stored answers."""
        # Build an answer transcript for the LLM.
        answers_text = "\n".join(
            f"{FIELD_LABELS[f]}: {self.field_answers.get(f, '[skipped]') or '[skipped]'}"
            for f in ONBOARDING_FIELDS
        )

        # Attempt 1: standard extraction.
        raw = await call_claude(
            "You extract structured data. Return ONLY valid JSON, no prose.",
            EXTRACTION_PROMPT + answers_text,
            max_tokens=2000,
            model=MODEL_HAIKU,
        )
        result = self._try_parse_json(raw)

        if not result:
            logger.warning("extract_config: JSON parse failed, retrying with stricter prompt")
            raw2 = await call_claude(
                "JSON generator. Output ONLY a single valid JSON object. "
                "No comments, no trailing commas, escape quotes with \\\".",
                EXTRACTION_PROMPT + answers_text,
                max_tokens=2000,
                model=MODEL_HAIKU,
            )
            result = self._try_parse_json(raw2)

        if not result:
            logger.warning("extract_config: both LLM attempts failed, using fallback")
            result = self._fallback_config_from_messages()

        # Stored answers are authoritative — overwrite gtm_profile and the
        # top-level identity fields so the LLM can't hallucinate past them.
        result["gtm_profile"] = self._build_gtm_profile_from_answers()
        if self.field_answers.get("business_name"):
            result["business_name"] = self.field_answers["business_name"]
        if self.field_answers.get("product_or_offer"):
            result["description"] = self.field_answers["product_or_offer"]
        channels = _normalize_channels(self.field_answers.get("channels", ""))
        if channels:
            result["channels"] = channels

        self._extracted_config = result
        return result

    async def build_tenant_config(
        self,
        tenant_id: str,
        owner_email: str,
        owner_name: str,
        active_agents: list[str] | None = None,
    ) -> TenantConfig:
        extracted = self._extracted_config or await self.extract_config()
        has_skips = len(self.skipped_topics) > 0
        ans = self.field_answers  # authoritative source — stored user answers

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

        # Build nested config objects with stored answers as backstops so the
        # edit-profile view and sub-agents see real values even when the LLM
        # extraction partially fails. The edit-profile endpoint reads from
        # icp.target_titles / icp.pain_points / product.* / brand_voice.tone /
        # gtm_playbook.action_plan_30 — all of which need to be hydrated here.
        icp_raw = extracted.get("icp", {}) or {}
        if not icp_raw.get("target_titles") and ans.get("target_audience"):
            icp_raw["target_titles"] = [ans["target_audience"].strip()]
        if not icp_raw.get("pain_points") and ans.get("problem_solved"):
            icp_raw["pain_points"] = [ans["problem_solved"].strip()]

        product_raw = extracted.get("product", {}) or {}
        if not product_raw.get("description") and ans.get("product_or_offer"):
            product_raw["description"] = ans["product_or_offer"].strip()
        if not product_raw.get("name") and ans.get("business_name"):
            product_raw["name"] = ans["business_name"].strip()
        if not product_raw.get("differentiators") and ans.get("differentiator"):
            product_raw["differentiators"] = [ans["differentiator"].strip()]

        gtm_raw = extracted.get("gtm_playbook", {}) or {}
        if not gtm_raw.get("action_plan_30") and ans.get("goal_30_days"):
            gtm_raw["action_plan_30"] = ans["goal_30_days"].strip()
        if not gtm_raw.get("competitor_differentiation") and ans.get("differentiator"):
            gtm_raw["competitor_differentiation"] = ans["differentiator"].strip()
        if not gtm_raw.get("channel_strategy"):
            gtm_raw["channel_strategy"] = _normalize_channels(ans.get("channels", ""))

        brand_raw = extracted.get("brand_voice", {}) or {}
        if not brand_raw.get("tone") and ans.get("brand_voice"):
            brand_raw["tone"] = ans["brand_voice"].strip()

        # Top-level identity fields — stored answers always win.
        business_name = ans.get("business_name") or extracted.get("business_name", "")
        description = ans.get("product_or_offer") or extracted.get("description", "")
        channels = _normalize_channels(ans.get("channels", "")) or extracted.get("channels", [])

        return TenantConfig(
            tenant_id=tenant_id,
            business_name=business_name,
            industry=extracted.get("industry", "technology"),
            description=description,
            icp=ICPConfig(**icp_raw),
            product=ProductConfig(**product_raw),
            gtm_playbook=GTMPlaybook(**gtm_raw),
            brand_voice=BrandVoice(**brand_raw),
            active_agents=active_agents or extracted.get(
                "recommended_agents",
                ["ceo", "content_writer", "email_marketer", "social_manager", "ad_strategist", "media"],
            ),
            channels=channels,
            gtm_profile=gtm_profile,
            owner_email=owner_email,
            owner_name=owner_name,
            plan="starter",
            onboarding_status="completed" if not has_skips else "in_progress",
            skipped_fields=self.get_skipped_fields(),
        )
