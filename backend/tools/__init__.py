"""ARIA tool wrappers — organized by service category."""

from . import (
    apollo_tool,
    hunter_tool,
    sendgrid_tool,
    gmail_tool,
    hubspot_tool,
    twilio_tool,
    calendly_tool,
    stripe_tool,
    shopify_tool,
    quickbooks_tool,
    social_tool,
    google_business_tool,
    slack_tool,
)

TOOLS_BY_CATEGORY = {
    "lead_data": [apollo_tool, hunter_tool],
    "communication": [sendgrid_tool, gmail_tool, twilio_tool, slack_tool],
    "crm": [hubspot_tool],
    "scheduling": [calendly_tool],
    "payments": [stripe_tool],
    "ecommerce": [shopify_tool],
    "finance": [quickbooks_tool],
    "social": [social_tool, google_business_tool],
}
