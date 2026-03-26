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


class IntegrationsConfig(BaseModel):
    """Third-party integration credentials (stored encrypted)."""
    # Email providers
    sendgrid_key: Optional[str] = None
    mailchimp_api_key: Optional[str] = None
    convertkit_api_key: Optional[str] = None
    # Social media
    twitter_api_key: Optional[str] = None
    twitter_api_secret: Optional[str] = None
    linkedin_access_token: Optional[str] = None
    facebook_page_token: Optional[str] = None
    instagram_access_token: Optional[str] = None
    # Ads
    facebook_ad_account_id: Optional[str] = None
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
