"""Email Template — design-wrap plain agent HTML into a branded email.

The sub-agents (Email Marketer especially) produce plain, unstyled
HTML like `<p>Hi Hanz,</p><ul><li>X</li></ul>`. To get the branded
look (gradient header, card sections, CTA button, footer) without
asking every agent's prompt to re-learn the template, we wrap here.

Also exposes:
- `agent_html_already_designed(html)` — skip wrapping when the agent
  produced its own design (inline styles, tables, etc).
- `business_name_for_template(tenant_id)` — header line for the wrap.
- `strip_html_to_text(html)` — lightweight HTML → plaintext converter
  used by parsers and preview-snippet generation.

All regex patterns are compiled once at module load — repeated email
renders don't recompile. Everything here is stateless / pure modulo
the one tenant-config lookup in business_name_for_template.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone


# Compiled once at module import — the email-render path can be hit
# dozens of times per inbox list, so saving the regex compile adds up.
_EMAIL_BODY_TAG_RE = re.compile(r"<body[^>]*>(.*?)</body>", re.IGNORECASE | re.DOTALL)
_EMAIL_CTA_RE = re.compile(
    r"(?:book|schedule|claim|get|see|try|start|book a)\s+(?:your\s+)?(?:free\s+)?(?:[a-z\-]+\s+){0,3}(?:demo|call|trial|consultation|meeting)",
    re.IGNORECASE,
)
_EMAIL_LI_CALLOUT_RE = re.compile(r"<li[^>]*>\s*<strong>([^<]+?):</strong>\s*([^<]+?)</li>")
_EMAIL_P_RESULT_RE = re.compile(
    r"<p[^>]*>\s*<strong>(Result|Summary|Bottom Line)[^<]*:?</strong>([^<]+?)</p>",
    re.IGNORECASE,
)
_EMAIL_H2_RE = re.compile(r"<h2[^>]*>(.*?)</h2>", re.IGNORECASE | re.DOTALL)
_EMAIL_H3_RE = re.compile(r"<h3[^>]*>(.*?)</h3>", re.IGNORECASE | re.DOTALL)
_EMAIL_P_NOSTYLE_RE = re.compile(r"<p(?![^>]*style=)")
_EMAIL_UL_NOSTYLE_RE = re.compile(r"<ul(?![^>]*style=)")
_EMAIL_LI_NOSTYLE_RE = re.compile(r"<li(?![^>]*style=)")
_EMAIL_A_NOSTYLE_RE = re.compile(r"<a(?![^>]*style=)")
_EMAIL_STRONG_NOSTYLE_RE = re.compile(r"<strong(?![^>]*style=)")

# strip_html_to_text patterns
_STRIP_STYLE_RE = re.compile(r"<style[^>]*>[\s\S]*?</style>", re.IGNORECASE)
_STRIP_SCRIPT_RE = re.compile(r"<script[^>]*>[\s\S]*?</script>", re.IGNORECASE)
_STRIP_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_STRIP_P_CLOSE_RE = re.compile(r"</p>", re.IGNORECASE)
_STRIP_DIV_CLOSE_RE = re.compile(r"</div>", re.IGNORECASE)
_STRIP_LI_CLOSE_RE = re.compile(r"</li>", re.IGNORECASE)
_STRIP_H_CLOSE_RE = re.compile(r"</h[1-6]>", re.IGNORECASE)
_STRIP_TAGS_RE = re.compile(r"<[^>]+>")
_STRIP_BLANKS_RE = re.compile(r"\n{3,}")


def strip_html_to_text(html: str) -> str:
    """Convert an HTML body into a plain text approximation for the
    text_body / preview_snippet fields. Not perfect — just enough to
    give the user a readable plaintext version. Mirrors the same logic
    the frontend uses in stripHtml() at frontend/app/.../inbox/page.tsx.
    """
    if not html:
        return ""
    out = html
    out = _STRIP_STYLE_RE.sub("", out)
    out = _STRIP_SCRIPT_RE.sub("", out)
    out = _STRIP_BR_RE.sub("\n", out)
    out = _STRIP_P_CLOSE_RE.sub("\n\n", out)
    out = _STRIP_DIV_CLOSE_RE.sub("\n", out)
    out = _STRIP_LI_CLOSE_RE.sub("\n", out)
    out = _STRIP_H_CLOSE_RE.sub("\n\n", out)
    out = _STRIP_TAGS_RE.sub("", out)
    out = (
        out.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    out = _STRIP_BLANKS_RE.sub("\n\n", out)
    return out.strip()


def agent_html_already_designed(html: str) -> bool:
    """Return True if the agent's HTML output already has its own design.

    We only want to apply the backend's branded template wrapper to
    PLAIN, unstyled output (naked <p>/<ul>/<li> tags from the markdown
    converter). When the agent produces its own designed HTML — inline
    styles, gradients, table-based layouts, dark themes, custom CTAs —
    we leave it alone so each email can look different.

    Detection signals (any one is enough):
      - Contains a <table> (almost always email-template layout)
      - Inline `style=` attribute count >= 5 (rich styling)
      - Mentions linear-gradient, max-width: 600px, or background-color
      - Has explicit @media or CSS-in-style-attr rules
    """
    if not html:
        return False
    h = html.lower()
    if "<table" in h:
        return True
    if h.count('style="') >= 5 or h.count("style='") >= 5:
        return True
    if any(marker in h for marker in (
        "linear-gradient",
        "max-width: 600",
        "max-width:600",
        "background-color: #",
        "background:#",
        "background: #",
        "@media",
        "border-radius:",
        "box-shadow:",
    )):
        return True
    return False


def business_name_for_template(tenant_id: str = "") -> str:
    """Return the tenant's business name for the email template header.

    Falls back to 'ARIA' if no tenant is known or the lookup fails.
    Cached implicitly by get_tenant_config so repeated calls are cheap.
    """
    if not tenant_id:
        return "ARIA"
    try:
        from backend.config.loader import get_tenant_config
        tc = get_tenant_config(tenant_id)
        return (tc.business_name or "ARIA").strip() or "ARIA"
    except Exception:
        return "ARIA"


def wrap_email_in_designed_template(
    body_html: str,
    *,
    business_name: str = "ARIA",
    subject: str = "",
    preview_text: str = "",
    cta_text: str | None = None,
    cta_url: str | None = None,
) -> str:
    """Wrap plain HTML email content in a designed branded template.

    The agent produces simple `<p>Hi Hanz,</p>...<ul><li>...</li></ul>`
    output. To get the dark-themed branded design the user wants
    (gradient header, card-style sections, CTA button, footer), we
    wrap that plain content in this template shell. The agent stays
    dumb; the backend handles the design.

    Looks like: dark navy background, blue gradient header card with
    business name + tagline, dark inner card holding the body, cyan
    section headers, styled CTA button, muted footer with company
    name + year.

    If body_html already contains <html> or <!DOCTYPE, it's a complete
    document and we leave it alone (assume the agent designed it
    intentionally).
    """
    if not body_html:
        return ""

    body_lower = body_html.lower().lstrip()
    if body_lower.startswith(("<!doctype", "<html")):
        return body_html  # complete document already, don't double-wrap

    # Strip the outer <body> wrapper if the parser added one
    m = _EMAIL_BODY_TAG_RE.search(body_html)
    if m:
        body_html = m.group(1).strip()

    # Auto-extract a CTA from common phrases if not provided
    if not cta_text:
        m = _EMAIL_CTA_RE.search(strip_html_to_text(body_html))
        if m:
            cta_text = m.group(0).title()
    if not cta_text:
        cta_text = "Schedule a 15-Minute Demo"
    if not cta_url:
        cta_url = "#"

    # Style sections that look like callouts. The agent often uses
    # **Bold:** prefix lines for highlights — give them card styling
    # with a colored left border on a light background.
    def _stylize_callout(match) -> str:
        label = match.group(1)
        rest = match.group(2)
        return (
            f'<div style="background: #fffbeb; '
            f'border-left: 4px solid #f59e0b; padding: 12px 16px; '
            f'margin: 8px 0; border-radius: 4px;">'
            f'<strong style="color: #92400e;">{label}:</strong>'
            f'<span style="color: #1f2937;"> {rest}</span>'
            f"</div>"
        )

    body_html = _EMAIL_LI_CALLOUT_RE.sub(_stylize_callout, body_html)

    body_html = _EMAIL_P_RESULT_RE.sub(
        lambda m: (
            f'<div style="background: #ecfdf5; '
            f'border-left: 4px solid #10b981; padding: 14px 18px; '
            f'margin: 16px 0; border-radius: 4px;">'
            f'<strong style="color: #047857;">{m.group(1)}:</strong>'
            f'<span style="color: #064e3b;"> {m.group(2)}</span>'
            f"</div>"
        ),
        body_html,
    )

    body_html = _EMAIL_H2_RE.sub(
        r'<h2 style="color: #2563eb; font-size: 20px; font-weight: 600; margin: 28px 0 12px 0;">\1</h2>',
        body_html,
    )
    body_html = _EMAIL_H3_RE.sub(
        r'<h3 style="color: #2563eb; font-size: 17px; font-weight: 600; margin: 24px 0 10px 0;">\1</h3>',
        body_html,
    )

    body_html = _EMAIL_P_NOSTYLE_RE.sub(
        '<p style="color: #374151; font-size: 15px; line-height: 1.7; margin: 14px 0;"',
        body_html,
    )
    body_html = _EMAIL_UL_NOSTYLE_RE.sub(
        '<ul style="color: #374151; padding-left: 22px; margin: 14px 0;"',
        body_html,
    )
    body_html = _EMAIL_LI_NOSTYLE_RE.sub(
        '<li style="margin: 8px 0; line-height: 1.6;"',
        body_html,
    )
    body_html = _EMAIL_A_NOSTYLE_RE.sub(
        '<a style="color: #2563eb; text-decoration: underline;"',
        body_html,
    )
    body_html = _EMAIL_STRONG_NOSTYLE_RE.sub(
        '<strong style="color: #111827;"',
        body_html,
    )

    # Build the template
    business_name_safe = (business_name or "ARIA").strip() or "ARIA"
    title_text = subject.strip() if subject else f"News from {business_name_safe}"
    tagline = preview_text.strip() if preview_text else f"From the {business_name_safe} team"
    year = datetime.now(timezone.utc).year

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title_text}</title>
</head>
<body style="margin: 0; padding: 0; background-color: #f3f4f6; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', sans-serif;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background-color: #f3f4f6; padding: 32px 12px;">
  <tr><td align="center">
    <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width: 600px; width: 100%; background-color: #ffffff; border-radius: 12px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.08), 0 1px 2px rgba(0,0,0,0.04);">
      <!-- Header card with blue gradient -->
      <tr><td style="background: linear-gradient(135deg, #2563eb 0%, #1e40af 100%); padding: 40px 32px; text-align: center;">
        <h1 style="color: #ffffff; font-size: 26px; font-weight: 700; margin: 0 0 8px 0; line-height: 1.3;">{title_text}</h1>
        <p style="color: rgba(255,255,255,0.9); font-size: 15px; margin: 0; line-height: 1.5;">{tagline}</p>
      </td></tr>
      <!-- Body card (light/white) -->
      <tr><td style="background-color: #ffffff; padding: 36px 36px 24px 36px;">
        {body_html}
        <!-- CTA button -->
        <div style="text-align: center; margin: 32px 0 8px 0;">
          <a href="{cta_url}" style="display: inline-block; background: linear-gradient(135deg, #2563eb 0%, #1d4ed8 100%); color: #ffffff; padding: 14px 32px; border-radius: 8px; text-decoration: none; font-weight: 600; font-size: 15px; box-shadow: 0 2px 4px rgba(37, 99, 235, 0.2);">{cta_text}</a>
        </div>
      </td></tr>
      <!-- Footer -->
      <tr><td style="background-color: #f9fafb; padding: 24px 32px; border-top: 1px solid #e5e7eb; text-align: center;">
        <p style="color: #6b7280; font-size: 12px; margin: 4px 0;">&copy; {year} {business_name_safe}. All rights reserved.</p>
        <p style="color: #6b7280; font-size: 12px; margin: 4px 0;">
          <a href="#" style="color: #6b7280; text-decoration: none;">Privacy Policy</a> &nbsp;|&nbsp;
          <a href="#" style="color: #6b7280; text-decoration: none;">Contact Us</a> &nbsp;|&nbsp;
          <a href="#" style="color: #6b7280; text-decoration: none;">Unsubscribe</a>
        </p>
      </td></tr>
    </table>
  </td></tr>
</table>
</body>
</html>"""
