"""Workflow templates and cron schedules for ARIA marketing agents."""

WORKFLOW_TEMPLATES = {
    "gtm_launch_workflow": {
        "agents": ["ceo", "content_writer", "email_marketer", "social_manager", "ad_strategist", "media"],
        "description": "Full GTM launch: strategy → content → email → social → ads → visuals",
        "steps": [
            {"agent": "ceo", "action": "build_gtm_playbook"},
            {"agent": "content_writer", "action": "landing_page", "depends_on": "ceo"},
            {"agent": "media", "action": "generate_image", "depends_on": "ceo"},
            {"agent": "email_marketer", "action": "launch_sequence", "depends_on": "content_writer"},
            {"agent": "social_manager", "action": "content_calendar", "depends_on": "ceo"},
            {"agent": "ad_strategist", "action": "campaign_plan", "depends_on": "content_writer"},
        ],
    },
    "weekly_content_workflow": {
        "agents": ["content_writer", "social_manager", "email_marketer"],
        "description": "Weekly content pipeline: blog → social posts → newsletter",
        "steps": [
            {"agent": "content_writer", "action": "blog_post"},
            {"agent": "social_manager", "action": "adapt_content", "depends_on": "content_writer"},
            {"agent": "email_marketer", "action": "newsletter", "depends_on": "content_writer"},
        ],
    },
    "product_hunt_launch_workflow": {
        "agents": ["content_writer", "email_marketer", "social_manager"],
        "description": "Product Hunt launch prep: listing copy + email + social",
        "steps": [
            {"agent": "content_writer", "action": "product_hunt"},
            {"agent": "email_marketer", "action": "launch_sequence", "depends_on": "content_writer"},
            {"agent": "social_manager", "action": "twitter_thread", "depends_on": "content_writer"},
        ],
    },
    "strategy_review_workflow": {
        "agents": ["ceo"],
        "description": "CEO reviews all marketing activity and adjusts strategy",
        "steps": [
            {"agent": "ceo", "action": "strategy_review"},
        ],
    },
    "ad_campaign_workflow": {
        "agents": ["ad_strategist", "content_writer", "media"],
        "description": "Create ad campaign with landing page and visuals",
        "steps": [
            {"agent": "content_writer", "action": "landing_page"},
            {"agent": "media", "action": "generate_image", "depends_on": "content_writer"},
            {"agent": "ad_strategist", "action": "campaign_plan", "depends_on": "content_writer"},
        ],
    },
}

CRON_SCHEDULES = {
    "ceo": "0 8 * * *",              # Daily 8am — strategy review
    "content_writer": "0 9 * * *",    # Daily 9am — content creation
    "email_marketer": "0 10 * * *",   # Daily 10am — email campaigns
    "social_manager": "0 9 * * *",    # Daily 9am — social media
    "ad_strategist": "0 10 * * *",    # Daily 10am — ad performance review
    "media": "0 11 * * *",              # Daily 11am — generate scheduled visuals
}
