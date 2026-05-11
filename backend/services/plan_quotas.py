"""Plan-based usage quotas — gates agent dispatches by pricing tier.

ARIA's pricing page advertises four tiers (free, starter, growth, scale)
but until this module landed, every tenant was effectively unlimited.
This module is the single source of truth for what each plan can do.

Architecture:
  * ``PLAN_LIMITS`` is a frozen dict keyed by plan slug, with each entry
    a ``PlanLimits`` dataclass holding the three usage caps + the
    email_sequences feature flag.
  * ``get_current_usage()`` counts rows in ``agent_logs`` for the
    relevant (tenant_id, agent_name) pair in a time window. The orchestrator
    writes one ``agent_logs`` row per dispatch via ``log_agent_action()`` —
    that's our usage ledger.
  * ``check_quota()`` is the gate the orchestrator calls before dispatching.
    Returns a ``QuotaResult`` with allowed/plan/used/limit/reason so callers
    can both block AND tell the user *why* they were blocked.
  * ``is_feature_enabled()`` is the boolean flag check used for the
    "email_sequences require Growth+" rule.

Design decisions worth knowing:

1. **Counting in agent_logs, not inbox_items.** The cap is on dispatches
   (work the platform did on the tenant's behalf), not surviving rows in
   inbox_items. If a tenant deletes a draft we don't refund them; the
   cost was incurred. Using agent_logs also gives us a clean status
   filter (only completed + completed_with_warning runs count — failed
   runs don't burn quota).

2. **Calendar-month window in UTC.** First of month at 00:00 UTC ->
   first of next month at 00:00 UTC. We don't do rolling 30-day windows
   because resetting on the 1st is the standard SaaS billing-cycle
   contract and easier for users to reason about.

3. **"Content piece" vs "campaign plan" identified by agent_name.** Per
   the spec:
     content_piece  := agent in {content_writer, social_manager, media}
     campaign_plan  := agent == ad_strategist
     email_sequence := agent == email_marketer
   The CEO and other agents don't count toward any cap — they're either
   orchestration overhead (CEO) or unbilled internal tools. The mapping
   lives in ``_AGENT_TO_QUOTA`` near the top so it's easy to spot.

4. **Pure I/O, no FastAPI deps.** Takes ``tenant_id`` + ``agent_name``,
   talks to Supabase via the canonical ``get_db()`` accessor (which the
   test conftest patches per ``_GET_DB_IMPORT_SITES``), returns plain
   dataclass + dict-like results. Same module is callable from the
   orchestrator (sync-ish coroutine), the chat handler, the inbox
   router, and CI tests with the standard ``mock_supabase`` fixture.

5. **``-1`` means unlimited.** Sentinel value, not a special-case "None"
   that callers have to remember to check. ``check_quota`` short-circuits
   the count query when ``limit == -1`` — we don't burn a round-trip for
   unconditionally-allowed dispatches.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from backend.config.loader import get_tenant_config
from backend.services.supabase import get_db

logger = logging.getLogger("aria.plan_quotas")


# ── Plan limits table ────────────────────────────────────────────────────

@dataclass(frozen=True)
class PlanLimits:
    """Per-plan caps. ``-1`` means unlimited.

    ``content_pieces`` covers content_writer + social_manager + media —
    anything that generates a piece of customer-facing content.
    ``campaign_plans`` covers ad_strategist outputs.
    ``email_sequences_enabled`` is a feature gate: when False, dispatches
    to email_marketer are blocked outright regardless of count.
    """
    content_pieces: int
    campaign_plans: int
    email_sequences_enabled: bool


PLAN_LIMITS: dict[str, PlanLimits] = {
    # "Try before you buy" — enough to feel the product without
    # making it useful for serious marketing. New signups land here.
    "free":    PlanLimits(content_pieces=3,  campaign_plans=0, email_sequences_enabled=False),
    # $49/mo — solo founder dipping their toes into automated content.
    "starter": PlanLimits(content_pieces=10, campaign_plans=1, email_sequences_enabled=False),
    # $149/mo — active GTM motion: drip campaigns + multiple ad plans.
    "growth":  PlanLimits(content_pieces=30, campaign_plans=3, email_sequences_enabled=True),
    # $299/mo — uncapped for shops that have real volume.
    "scale":   PlanLimits(content_pieces=-1, campaign_plans=-1, email_sequences_enabled=True),
}


# Agents that count toward the content_pieces quota.
_CONTENT_AGENTS = frozenset({"content_writer", "social_manager", "media"})
# Agent that counts toward the campaign_plans quota.
_CAMPAIGN_AGENTS = frozenset({"ad_strategist"})
# Agent that uses the email_sequences feature flag.
_EMAIL_AGENTS = frozenset({"email_marketer"})


def _quota_bucket_for(agent_name: str) -> Optional[str]:
    """Map agent slug to its quota bucket name (``content``, ``campaign``,
    ``email``) or None if the agent doesn't burn quota.

    CEO + any unmapped agent return None — they're orchestration overhead,
    not billable output, so ``check_quota`` short-circuits to allowed.
    """
    if agent_name in _CONTENT_AGENTS:
        return "content"
    if agent_name in _CAMPAIGN_AGENTS:
        return "campaign"
    if agent_name in _EMAIL_AGENTS:
        return "email"
    return None


# ── Result types ─────────────────────────────────────────────────────────

@dataclass
class QuotaResult:
    """Outcome of a check_quota() call.

    ``allowed`` is the only field callers HAVE to read; the rest are
    informational for the message the chat/inbox surface shows the user.

    When ``allowed=False``, ``reason`` is a one-sentence human-readable
    string that's safe to surface verbatim ("monthly content quota
    reached (3/3 on free plan)"). ``used`` and ``limit`` are populated
    so the frontend can render a usage bar without a second round-trip.
    """
    allowed: bool
    plan: str
    used: int = 0
    limit: int = 0
    reason: Optional[str] = None

    def as_dict(self) -> dict:
        """Serialize for API responses / structured logs."""
        return {
            "allowed": self.allowed,
            "plan": self.plan,
            "used": self.used,
            "limit": self.limit,
            "reason": self.reason,
        }


# ── Time window helpers ──────────────────────────────────────────────────

def month_start_utc(now: Optional[datetime] = None) -> datetime:
    """Return midnight UTC of the first day of the current calendar month.

    Used as the lower bound for usage counts. Quotas reset at the
    boundary between months — a tenant maxed out on Apr 30 23:59 UTC
    can dispatch again at May 1 00:00 UTC.
    """
    n = now or datetime.now(timezone.utc)
    # Drop time-of-day + day; keep year + month
    return n.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


# ── Core queries ─────────────────────────────────────────────────────────

def get_current_usage(tenant_id: str, agent_name: str, since_utc: datetime) -> int:
    """Count agent_logs rows for this tenant + agent since ``since_utc``.

    ``agent_name`` may be a single slug (``"content_writer"``) or a
    comma-separated string of slugs (``"content_writer,social_manager,media"``).
    The latter form is what ``check_quota`` uses to sum across the three
    "content piece" agents in one round-trip.

    Only rows with status in (``completed``, ``completed_with_warning``)
    are counted. Failed / error / skipped runs don't burn quota — that's
    the contract the rest of the platform already uses (the reports
    chart at backend/services/reports.py uses the same filter).

    Returns 0 if the query errors out (defensive: a missing agent_logs
    table or a Supabase blip shouldn't 500 the dispatch path; we'd
    rather under-count and let a dispatch through than 500). The error
    is logged at WARNING so it shows up in observability if it happens.
    """
    try:
        sb = get_db()
        # Allow comma-separated multi-agent counts. content_pieces in
        # the pricing table aggregates content_writer + social_manager +
        # media, so check_quota passes "content_writer,social_manager,media".
        agent_list = [a.strip() for a in agent_name.split(",") if a.strip()]
        query = (
            sb.table("agent_logs")
            .select("id", count="exact")
            .eq("tenant_id", tenant_id)
        )
        if len(agent_list) == 1:
            query = query.eq("agent_name", agent_list[0])
        else:
            query = query.in_("agent_name", agent_list)
        result = (
            query
            .in_("status", ["completed", "completed_with_warning"])
            .gte("timestamp", since_utc.isoformat())
            .execute()
        )
        # Supabase-py returns count when the request used count="exact".
        # Some response shapes also put the rows in .data — fall back to
        # len(data) when count isn't set (covers the test mock that
        # doesn't auto-fill .count on the chain dispatch).
        count = getattr(result, "count", None)
        if count is None:
            data = getattr(result, "data", None) or []
            count = len(data) if isinstance(data, list) else 0
        return int(count)
    except Exception as e:  # pragma: no cover — defensive
        logger.warning(
            "get_current_usage failed for tenant=%s agent=%s: %s",
            tenant_id, agent_name, e,
        )
        return 0


def _plan_for(tenant_id: str) -> str:
    """Look up the tenant's plan slug. Defaults to ``free`` on lookup error.

    Defaulting to the most restrictive tier is intentional: if the config
    lookup is broken we'd rather block the dispatch (and have the user
    see "Upgrade to continue") than silently grant unlimited usage to
    a tenant whose plan column we can't read.
    """
    try:
        config = get_tenant_config(tenant_id)
        plan = getattr(config, "plan", "free") or "free"
        if plan not in PLAN_LIMITS:
            # Unknown plan slug in the column (someone set it to "team"
            # before the constraint was applied, for example). Treat as
            # free + log so we notice.
            logger.warning("Unknown plan slug %r for tenant=%s — treating as free", plan, tenant_id)
            return "free"
        return plan
    except Exception as e:
        logger.warning("plan lookup failed for tenant=%s: %s — defaulting to free", tenant_id, e)
        return "free"


def _humanize_plan(plan: str) -> str:
    """Human-readable plan name for error messages.

    ``"free"`` -> ``"Free"``. Keeps the reason strings reading naturally
    ("monthly content quota reached (3/3 on Free plan)") instead of
    leaking the lowercase slug.
    """
    return plan.title() if plan else "Free"


def is_feature_enabled(tenant_id: str, feature: str) -> bool:
    """Return True if the tenant's plan unlocks the named feature.

    Currently only ``email_sequences`` is gated this way. Unknown feature
    names return False (fail closed) so a typo in a caller doesn't
    accidentally bypass a real gate.
    """
    plan = _plan_for(tenant_id)
    limits = PLAN_LIMITS[plan]
    if feature == "email_sequences":
        return limits.email_sequences_enabled
    return False


def check_quota(tenant_id: str, agent_name: str) -> QuotaResult:
    """Decide whether ``tenant_id`` can dispatch ``agent_name`` right now.

    Lookup order:
      1. Resolve the tenant's plan (defaulting to free on error).
      2. Map agent -> quota bucket. CEO / unknown agents are always
         allowed (they don't burn quota).
      3. Email special-case: check the email_sequences feature gate
         FIRST. A free-plan tenant trying to dispatch email_marketer
         gets blocked with an upgrade-prompt message regardless of how
         many sends they've done this month.
      4. Numeric cap: limit == -1 short-circuits to allowed (no count
         query). Otherwise pull month-to-date usage and compare.

    The returned ``QuotaResult.reason`` is the string the chat handler
    surfaces to the user. Keep it actionable ("Upgrade to Growth to
    enable email sequences") not generic ("Forbidden").
    """
    plan = _plan_for(tenant_id)
    limits = PLAN_LIMITS[plan]
    bucket = _quota_bucket_for(agent_name)

    # Agents that don't have a quota bucket (CEO, unmapped) always pass.
    if bucket is None:
        return QuotaResult(allowed=True, plan=plan, used=0, limit=-1)

    plan_label = _humanize_plan(plan)

    # ── Email special-case — feature flag check happens FIRST ──
    if bucket == "email":
        if not limits.email_sequences_enabled:
            return QuotaResult(
                allowed=False,
                plan=plan,
                used=0,
                limit=0,
                reason="Email sequences require the Growth plan or higher",
            )
        # Email enabled — no numeric cap on email sequences in the
        # current pricing table; falling through means "allowed".
        return QuotaResult(allowed=True, plan=plan, used=0, limit=-1)

    # ── Numeric quotas (content, campaign) ──
    # Pick the limit AND the agent_name(s) used to count usage. The
    # content bucket aggregates across content_writer + social_manager +
    # media (per the pricing spec: "any inbox_item written by
    # content_writer, social_manager, or media"), so we hand
    # get_current_usage a comma-joined list it can splat into an
    # `in_` filter rather than running three queries.
    if bucket == "content":
        limit = limits.content_pieces
        bucket_label = "content"
        usage_agent_filter = ",".join(sorted(_CONTENT_AGENTS))
    else:  # bucket == "campaign"
        limit = limits.campaign_plans
        bucket_label = "campaign"
        usage_agent_filter = ",".join(sorted(_CAMPAIGN_AGENTS))

    # Unlimited tier — skip the count query entirely.
    if limit == -1:
        return QuotaResult(allowed=True, plan=plan, used=0, limit=-1)

    # Limit of zero on a feature the tenant is trying to use: block with
    # a feature-gate-style message rather than a "0/0" usage one. Free
    # plan + ad_strategist hits this path.
    if limit == 0:
        reason = (
            f"{bucket_label.title()} plans aren't included on the "
            f"{plan_label} plan — upgrade to unlock"
        )
        return QuotaResult(allowed=False, plan=plan, used=0, limit=0, reason=reason)

    # Count this month's usage and compare.
    since = month_start_utc()
    used = get_current_usage(tenant_id, usage_agent_filter, since)

    if used >= limit:
        reason = (
            f"Monthly {bucket_label} quota reached ({used}/{limit} on "
            f"{plan_label} plan)"
        )
        return QuotaResult(allowed=False, plan=plan, used=used, limit=limit, reason=reason)

    return QuotaResult(allowed=True, plan=plan, used=used, limit=limit)
