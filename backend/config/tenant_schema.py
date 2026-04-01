"""Tenant configuration schema for ARIA — AI Marketing Team for Developer Founders."""

from datetime import datetime
from typing import Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class ICPConfig(BaseModel):
    """Ideal Customer Profile — who the product is for."""
    target_titles: list[str] = Field(default_factory=list)
    target_industries: list[str] = Field(default_factory=list)
    company_size: str = ""
    pain_points: list[str] = Field(default_factory=list)
    language_patterns: list[str] = Field(default_factory=list)
    online_hangouts: list[str] = Field(default_factory=list)


class ProductConfig(BaseModel):
    """Product/service being marketed."""
    name: str = ""
    description: str = ""
    value_props: list[str] = Field(default_factory=list)
    pricing_info: str = ""
    competitors: list[str] = Field(default_factory=list)
    differentiators: list[str] = Field(default_factory=list)
    product_type: str = ""  # saas, developer_tool, api, app


class GTMPlaybook(BaseModel):
    """Go-to-market strategy built by CEO agent during onboarding."""
    positioning: str = ""
    messaging_pillars: list[str] = Field(default_factory=list)
    content_themes: list[str] = Field(default_factory=list)
    channel_strategy: list[str] = Field(default_factory=list)
    action_plan_30: str = ""
    action_plan_60: str = ""
    action_plan_90: str = ""
    kpis: list[str] = Field(default_factory=list)
    competitor_differentiation: str = ""


class BrandVoice(BaseModel):
    """Brand voice learned during onboarding and refined via feedback."""
    tone: str = "professional"
    example_phrases: list[str] = Field(default_factory=list)
    do_guidelines: list[str] = Field(default_factory=list)
    dont_guidelines: list[str] = Field(default_factory=list)


class GTMProfile(BaseModel):
    """Flat GTM profile — directly maps to the 8 onboarding answers."""
    business_name: str = ""
    offer: str = ""
    audience: str = ""
    problem: str = ""
    differentiator: str = ""
    positioning_summary: str = ""
    primary_channels: list[str] = Field(default_factory=list)
    brand_voice: str = ""
    goal_30_days: str = ""
    thirty_day_gtm_focus: str = ""  # JSON key is "30_day_gtm_focus"


class IntegrationsConfig(BaseModel):
    """Third-party integration credentials (stored encrypted)."""
    # Email providers
    sendgrid_key: Optional[str] = None
    mailchimp_api_key: Optional[str] = None
    convertkit_api_key: Optional[str] = None
    # Twitter / X (per-user OAuth 2.0)
    twitter_access_token: Optional[str] = None
    twitter_refresh_token: Optional[str] = None
    twitter_username: Optional[str] = None
    # LinkedIn (OAuth 2.0)
    linkedin_access_token: Optional[str] = None
    linkedin_member_urn: Optional[str] = None
    linkedin_name: Optional[str] = None
    # Facebook
    facebook_page_token: Optional[str] = None
    facebook_page_id: Optional[str] = None
    instagram_access_token: Optional[str] = None
    # Ads
    facebook_ad_account_id: Optional[str] = None
    # Google OAuth (Gmail sending + inbox sync)
    google_access_token: Optional[str] = None
    google_refresh_token: Optional[str] = None
    gmail_last_sync_at: Optional[str] = None  # ISO timestamp of last inbound sync
    gmail_history_id: Optional[str] = None    # Gmail history ID for incremental sync
    # WhatsApp Cloud API (per-tenant)
    whatsapp_access_token: Optional[str] = None
    whatsapp_phone_number_id: Optional[str] = None
    whatsapp_business_account_id: Optional[str] = None
    # Payments
    stripe_customer_id: Optional[str] = None


class TenantConfig(BaseModel):
    """Complete tenant configuration loaded by every agent at runtime."""
    tenant_id: UUID = Field(default_factory=uuid4)
    business_name: str = ""
    industry: str = "technology"
    description: str = ""
    icp: ICPConfig = Field(default_factory=ICPConfig)
    product: ProductConfig = Field(default_factory=ProductConfig)
    gtm_playbook: GTMPlaybook = Field(default_factory=GTMPlaybook)
    brand_voice: BrandVoice = Field(default_factory=BrandVoice)
    active_agents: list[str] = Field(default_factory=list)
    channels: list[str] = Field(default_factory=list)
    integrations: IntegrationsConfig = Field(default_factory=IntegrationsConfig)
    plan: str = "starter"
    trial_ends: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    timezone: str = "UTC"
    owner_email: str = ""
    owner_name: str = ""
    onboarding_status: str = "not_started"  # not_started | in_progress | completed
    skipped_fields: list[str] = Field(default_factory=list)  # e.g. ["brand_voice", "competitors"]
    gtm_profile: GTMProfile = Field(default_factory=GTMProfile)
    agent_brief: str = ""  # Condensed ~150 token context for all agents (generated after onboarding)
