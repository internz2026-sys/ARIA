"""Pydantic request/response schemas for ARIA API.

Centralized schema definitions used across routers.
"""
from __future__ import annotations

from pydantic import BaseModel
from typing import Optional


# ── Auth / OAuth ──────────────────────────────────────────────────────────────

class GoogleTokens(BaseModel):
    access_token: str
    refresh_token: str = ""


# ── Social ────────────────────────────────────────────────────────────────────

class SocialApproveRequest(BaseModel):
    inbox_item_id: str


class TweetRequest(BaseModel):
    text: str
    reply_to: Optional[str] = None


class ThreadRequest(BaseModel):
    tweets: list[str]


class LinkedInPostTargetRequest(BaseModel):
    org_urn: str = ""
    org_name: str = ""


# ── WhatsApp ──────────────────────────────────────────────────────────────────

class WhatsAppSendRequest(BaseModel):
    to: str
    message: str


class WhatsAppConnectRequest(BaseModel):
    access_token: str
    phone_number_id: str
    business_account_id: str = ""


# ── Email ─────────────────────────────────────────────────────────────────────

class GmailSendRequest(BaseModel):
    to: str
    subject: str
    body_html: str
    reply_to_message_id: str = ""


class EmailApproveRequest(BaseModel):
    inbox_item_id: str


class UpdateDraftRequest(BaseModel):
    inbox_item_id: str
    to: Optional[str] = None
    subject: Optional[str] = None
    html_body: Optional[str] = None


class DraftReplyRequest(BaseModel):
    thread_id: str
    custom_instructions: str = ""


class MarkReadRequest(BaseModel):
    notification_ids: list[str] = []


# ── Onboarding ────────────────────────────────────────────────────────────────

class OnboardingStart(BaseModel):
    session_id: str = ""


class OnboardingMessage(BaseModel):
    session_id: str
    message: str


class SaveConfig(BaseModel):
    session_id: str
    active_agents: list[str] = []


class SaveConfigDirect(BaseModel):
    config: dict


class UpdateOnboarding(BaseModel):
    answers: dict


# ── CRM ───────────────────────────────────────────────────────────────────────

class CrmContactCreate(BaseModel):
    name: str
    email: str = ""
    phone: str = ""
    company_id: Optional[str] = None
    source: str = "manual"
    status: str = "lead"
    tags: list[str] = []
    notes: str = ""


class CrmContactUpdate(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    company_id: Optional[str] = None
    source: Optional[str] = None
    status: Optional[str] = None
    tags: Optional[list[str]] = None
    notes: Optional[str] = None


class CrmCompanyCreate(BaseModel):
    name: str
    domain: str = ""
    industry: str = ""
    size: str = ""
    notes: str = ""


class CrmCompanyUpdate(BaseModel):
    name: Optional[str] = None
    domain: Optional[str] = None
    industry: Optional[str] = None
    size: Optional[str] = None
    notes: Optional[str] = None


class CrmDealCreate(BaseModel):
    title: str
    value: float = 0
    stage: str = "lead"
    contact_id: Optional[str] = None
    company_id: Optional[str] = None
    notes: str = ""
    expected_close: Optional[str] = None


class CrmDealUpdate(BaseModel):
    title: Optional[str] = None
    value: Optional[float] = None
    stage: Optional[str] = None
    contact_id: Optional[str] = None
    company_id: Optional[str] = None
    notes: Optional[str] = None
    expected_close: Optional[str] = None


class CrmActivityCreate(BaseModel):
    contact_id: Optional[str] = None
    deal_id: Optional[str] = None
    type: str
    description: str = ""
    metadata: dict = {}


# ── CEO ───────────────────────────────────────────────────────────────────────

class CEOActionRequest(BaseModel):
    action: str
    params: dict = {}
    confirmed: bool = False


class CEOChatMessage(BaseModel):
    session_id: str
    message: str
    tenant_id: str = ""


# ── Tasks ─────────────────────────────────────────────────────────────────────

class TaskUpdate(BaseModel):
    status: str
    task: str = ""


class TriageRequest(BaseModel):
    message: str
    tenant_id: str = ""
