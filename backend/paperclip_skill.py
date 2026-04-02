"""Create and manage the ARIA API skill in Paperclip.

This skill teaches Paperclip agents how to call ARIA's backend API
to create inbox items, send emails, publish posts, manage CRM, etc.
"""
from __future__ import annotations

import logging
import os

from backend.paperclip_sync import _urllib_request, get_company_id

logger = logging.getLogger("aria.paperclip_skill")

SKILL_NAME = "aria-backend-api"
SKILL_DESCRIPTION = "ARIA Backend API — allows agents to create inbox items, manage CRM, send emails, and publish posts"


def _get_api_url() -> str:
    """Get the ARIA backend URL that Paperclip agents can reach."""
    # Paperclip agents run inside Docker, so they need the host-accessible URL
    return os.environ.get("API_URL", "http://172.17.0.1:8000")


def get_skill_markdown() -> str:
    """Generate the SKILL.md content for the ARIA API skill."""
    api_url = _get_api_url()

    return f"""---
name: aria-backend-api
description: ARIA Backend API — create inbox items, manage CRM, send emails, publish posts
---

# ARIA Backend API

You are an ARIA marketing agent. Use these API endpoints to store your work results.

## Base URL
`{api_url}`

## Authentication
All requests must include the tenant_id in the URL path. No auth headers needed for internal calls.

## Core Workflow
1. You receive a task from the CEO or scheduler
2. You generate the content/output
3. You call the ARIA API to save results to the inbox
4. The user reviews your work in the ARIA dashboard

---

## Create Inbox Item (PRIMARY — use this for all outputs)

Save your work to the user's inbox for review.

```bash
curl -X POST {api_url}/api/inbox/{{tenant_id}}/items \\
  -H "Content-Type: application/json" \\
  -d '{{
    "title": "Your output title",
    "content": "Your full output content (markdown or JSON)",
    "type": "blog|social_post|email|ad_campaign|follow_up",
    "agent": "content_writer|email_marketer|social_manager|ad_strategist|ceo",
    "priority": "low|medium|high",
    "status": "needs_review"
  }}'
```

### Content Types
- `blog` — blog posts, articles, landing page copy
- `social_post` — tweets and LinkedIn posts (use JSON format with posts array)
- `email` — email drafts with subject, body, recipient
- `ad_campaign` — ad copy, targeting, budget plans
- `follow_up` — follow-up tasks and reminders

### Social Post Format
For social posts, use this JSON structure in the content field:
```json
{{
  "posts": [
    {{"platform": "twitter", "text": "Tweet text here (max 280 chars)", "hashtags": ["tag1", "tag2"]}},
    {{"platform": "linkedin", "text": "LinkedIn post here (up to 3000 chars)", "hashtags": ["tag1", "tag2"]}}
  ]
}}
```

### Email Draft Format
For email drafts, use this structure:
```json
{{
  "to": "recipient@example.com",
  "subject": "Email subject line",
  "html_body": "<p>Email body in HTML</p>",
  "text_body": "Plain text version",
  "preview_snippet": "Preview text for email client"
}}
```

---

## Read Tenant Config (get business context)

Get the user's business profile, GTM playbook, and brand voice.

```bash
curl {api_url}/api/dashboard/{{tenant_id}}/config
```

Returns business_name, description, icp, product, gtm_playbook, brand_voice, etc.

---

## Read Recent Inbox Items (check existing content)

See what's already been created to avoid duplicates.

```bash
curl {api_url}/api/inbox/{{tenant_id}}/items?status=needs_review&page_size=5
```

---

## CRM — Read Contacts

Get contacts for email targeting or personalization.

```bash
curl {api_url}/api/crm/{{tenant_id}}/contacts?search=keyword
```

---

## Important Rules
1. ALWAYS save your output via the Create Inbox Item endpoint
2. ALWAYS include the tenant_id from the task context
3. Use the correct `type` and `agent` fields
4. Set status to `needs_review` — never auto-publish
5. Read the tenant config first to get business context and brand voice
6. Check recent inbox items to avoid duplicating work
"""


async def ensure_skill(company_id: str) -> str | None:
    """Create or update the ARIA API skill in Paperclip. Returns skill_id."""
    # Check if skill already exists
    skills = _urllib_request("GET", f"/api/companies/{company_id}/skills")
    if skills:
        skill_list = skills if isinstance(skills, list) else skills.get("data", [])
        for s in skill_list:
            if s.get("name") == SKILL_NAME or s.get("slug") == SKILL_NAME:
                skill_id = s["id"]
                # Update the skill content
                _urllib_request("PUT", f"/api/companies/{company_id}/skills/{skill_id}/files/SKILL.md", data={
                    "content": get_skill_markdown(),
                })
                logger.info(f"Updated ARIA API skill: {skill_id}")
                return skill_id

    # Create new skill
    result = _urllib_request("POST", f"/api/companies/{company_id}/skills", data={
        "name": SKILL_NAME,
        "description": SKILL_DESCRIPTION,
        "sourceType": "inline",
    })
    if result and result.get("id"):
        skill_id = result["id"]
        # Write the SKILL.md content
        _urllib_request("PUT", f"/api/companies/{company_id}/skills/{skill_id}/files/SKILL.md", data={
            "content": get_skill_markdown(),
        })
        logger.info(f"Created ARIA API skill: {skill_id}")
        return skill_id

    logger.error("Failed to create ARIA API skill")
    return None


async def attach_skill_to_agents(company_id: str, skill_id: str, agent_ids: dict[str, str]):
    """Attach the ARIA API skill to all agents."""
    for agent_name, agent_id in agent_ids.items():
        result = _urllib_request("POST", f"/api/agents/{agent_id}/skills", data={
            "skillId": skill_id,
        })
        if result:
            logger.info(f"Attached ARIA skill to {agent_name}")
        else:
            logger.warning(f"Failed to attach ARIA skill to {agent_name}")
