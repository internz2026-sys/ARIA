"""Email Parser — turn a sub-agent's free-form reply into structured fields.

The Email Marketer's output lands here as a mix of markdown, raw HTML,
and `**Subject:**` / `**To:**` prefix lines. The inbox row needs a
clean `email_draft` dict with `subject / to / html_body / text_body /
preview_snippet / send_time / status` so the frontend's EmailEditor can
render the approve/schedule/send buttons.

Two parser entry points:
  - `parse_email_draft_from_text(text, fallback_to)` — markdown-first
  - `parse_html_email_draft(text, fallback_to)` — HTML-first, auto-
    dispatched to by the markdown parser when the content is clearly
    raw HTML (so the markdown parser doesn't grab `<html>...` as the
    subject line).

Plus:
  - `markdown_to_basic_html(text)` — cheap markdown-to-HTML converter
    used as the fallback when the agent didn't emit a ```html``` block.
  - `parse_social_drafts_from_text(text)` — X / LinkedIn split.

All pure / stateless. Design wrap happens via email_template service.
"""
from __future__ import annotations

import html as _html
import re

from backend.services.email_template import (
    agent_html_already_designed,
    business_name_for_template,
    strip_html_to_text,
    wrap_email_in_designed_template,
)


def markdown_to_basic_html(text: str) -> str:
    """Quick markdown -> HTML converter for email body rendering.

    This is the fallback used when the agent's reply doesn't include a
    fenced ```html``` block. The frontend's email editor renders this in
    its Source / Preview tab so users see the body content instead of
    an empty editor. Not a full markdown parser — just covers the
    common cases that show up in agent output: bold, italic, headers,
    bullet/numbered lists, paragraph breaks, and inline links.
    """
    if not text:
        return ""

    out = _html.escape(text)

    # Inline links: [text](url) — BEFORE bold/italic so brackets aren't eaten.
    out = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', out)

    # Bold / italic
    out = re.sub(r"\*\*([^*\n]+?)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"<em>\1</em>", out)

    # Headers
    out = re.sub(r"(?m)^###\s+(.+)$", r"<h3>\1</h3>", out)
    out = re.sub(r"(?m)^##\s+(.+)$", r"<h2>\1</h2>", out)
    out = re.sub(r"(?m)^#\s+(.+)$", r"<h1>\1</h1>", out)

    # Horizontal rule
    out = re.sub(r"(?m)^---+\s*$", "<hr/>", out)

    # Lists — group consecutive bullet/numbered lines into <ul>/<ol>
    lines = out.split("\n")
    rendered: list[str] = []
    in_ul = False
    in_ol = False
    for line in lines:
        bullet = re.match(r"^\s*[-*]\s+(.+)$", line)
        ordered = re.match(r"^\s*\d+\.\s+(.+)$", line)
        if bullet:
            if not in_ul:
                if in_ol:
                    rendered.append("</ol>")
                    in_ol = False
                rendered.append("<ul>")
                in_ul = True
            rendered.append(f"<li>{bullet.group(1)}</li>")
        elif ordered:
            if not in_ol:
                if in_ul:
                    rendered.append("</ul>")
                    in_ul = False
                rendered.append("<ol>")
                in_ol = True
            rendered.append(f"<li>{ordered.group(1)}</li>")
        else:
            if in_ul:
                rendered.append("</ul>")
                in_ul = False
            if in_ol:
                rendered.append("</ol>")
                in_ol = False
            rendered.append(line)
    if in_ul:
        rendered.append("</ul>")
    if in_ol:
        rendered.append("</ol>")
    out = "\n".join(rendered)

    # Paragraph wrapping
    paragraphs = re.split(r"\n\s*\n", out)
    wrapped: list[str] = []
    for p in paragraphs:
        p = p.strip()
        if not p:
            continue
        if re.match(r"^\s*<(h\d|ul|ol|li|hr|p|div|table|blockquote)", p):
            wrapped.append(p)
        else:
            wrapped.append("<p>" + p.replace("\n", "<br/>") + "</p>")
    body_html = "\n".join(wrapped)

    return (
        '<div style="font-family: -apple-system, system-ui, sans-serif; '
        'line-height: 1.5; color: #1f2937;">'
        f'{body_html}</div>'
    )


def parse_html_email_draft(text: str, fallback_to: str = "") -> dict | None:
    """Parse an email_draft when the agent's content IS raw HTML.

    Detection: content starts with <!DOCTYPE, <html>, or has many HTML
    tags relative to length. The markdown parser would otherwise extract
    the `<html><body style="...">` opening tag as the SUBJECT field via
    the first-sentence fallback — exactly the bug that prompted splitting
    this into its own parser.

    Strategy:
      - Subject: prefer <title>, then first <h1>/<h2>, then any
        SUBJECT: marker in the rendered text.
      - To: any email-shaped token in the rendered text (NOT in
        attribute values like style="font-family: ...@...").
      - html_body: the inner HTML between <body> tags, or the whole
        thing if no body tag.
      - text_body: stripped HTML.
    """
    if not text or len(text) < 30:
        return None

    # Subject: <title> -> <h1>/<h2>/<h3> -> Subject markers in stripped
    # text -> first non-greeting <p> content.
    subject = None
    m = re.search(r"<title[^>]*>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
    if m:
        subject = re.sub(r"<[^>]+>", "", m.group(1)).strip()
    if not subject:
        for tag in ("h1", "h2", "h3"):
            m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", text, re.IGNORECASE | re.DOTALL)
            if m:
                candidate = re.sub(r"<[^>]+>", "", m.group(1)).strip()
                if candidate and len(candidate) > 5:
                    subject = candidate
                    break

    if not subject:
        stripped = strip_html_to_text(text)
        m = re.search(
            r"Subject[^\n]{0,40}\n+\s*A[):]\s*[\"']?([^\"'\n]+)[\"']?",
            stripped, re.IGNORECASE,
        )
        if m:
            subject = m.group(1).strip()
        if not subject:
            m = re.search(
                r"(?:^|\n)\s*Subject\s*(?:Line)?\s*[:\-]\s*[\"']?([^\"'\n]+)[\"']?",
                stripped, re.IGNORECASE,
            )
            if m:
                cand = m.group(1).strip()
                if cand and len(cand) > 5:
                    subject = cand
        if not subject:
            m = re.search(
                r"(?:^|\n)\s*Preview\s*(?:Text)?\s*[:\-]\s*[\"']?([^\"'\n]+)[\"']?",
                stripped, re.IGNORECASE,
            )
            if m:
                cand = m.group(1).strip()
                if cand and len(cand) > 5:
                    subject = cand
        if not subject:
            for m in re.finditer(r"<p[^>]*>(.*?)</p>", text, re.IGNORECASE | re.DOTALL):
                cand = re.sub(r"<[^>]+>", "", m.group(1)).strip()
                if not cand or len(cand) < 15:
                    continue
                if re.match(
                    r"^(hi|hello|hey|dear|best|sincerely|cheers|thanks|p\.?s\.?)\b",
                    cand, re.IGNORECASE,
                ):
                    continue
                if len(cand) > 100:
                    cand = cand[:100].rsplit(" ", 1)[0] + "..."
                subject = cand
                break

    if subject:
        subject = subject.replace("&amp;", "&").replace("&nbsp;", " ").strip()
        subject = subject.split("\n")[0].strip()[:200]

    # html_body: prefer the inner contents of <body>...</body>
    html_body = text
    m = re.search(r"<body[^>]*>(.*?)</body>", text, re.IGNORECASE | re.DOTALL)
    if m:
        html_body = m.group(1).strip()

    if html_body and not agent_html_already_designed(html_body):
        preview_text = ""
        pm = re.search(
            r"Preview\s*Text[^:]*:\s*([^\n]+)",
            strip_html_to_text(text), re.IGNORECASE,
        )
        if pm:
            preview_text = pm.group(1).strip().strip('"').strip("*").strip()[:120]
        html_body = wrap_email_in_designed_template(
            html_body,
            business_name=business_name_for_template(),
            subject=subject or "",
            preview_text=preview_text,
        )

    text_body = strip_html_to_text(text)
    preview = text_body[:200]

    to = fallback_to or ""
    if not to:
        text_only = re.sub(r"<[^>]+>", " ", text)
        m = re.search(
            r"\b([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b",
            text_only,
        )
        if m:
            to = m.group(1)

    if not (subject or text_body):
        return None

    return {
        "subject": (subject or "Untitled email")[:300],
        "to": to or "",
        "send_time": "",
        "text_body": text_body[:5000],
        "html_body": html_body or "",
        "preview_snippet": preview,
        "status": "draft_pending_approval",
    }


def parse_email_draft_from_text(text: str, fallback_to: str = "") -> dict | None:
    """Extract structured email fields from a free-form agent reply.

    Patterns recognised (case-insensitive, all optional):
      **Subject:** ... | Subject: ...                  -> subject
      **To:** | **Recipient:** | **Send to:** ...      -> to (email address)
      **Send Time:** | **When:** ...                   -> send_time
      ```html ... ```  fenced code block               -> body_html
      everything else                                  -> body (plaintext)

    Returns a dict suitable for the inbox_items.email_draft column, or
    None if nothing email-shaped was found. The frontend renders the
    Approve & Send / Schedule / Cancel draft buttons whenever this
    column is non-null.
    """
    if not text or len(text) < 30:
        return None

    # HTML-first fast path so the markdown parser doesn't eat a
    # <html><body style="..."> opening tag as the subject.
    _stripped = text.lstrip()
    _looks_like_html = (
        _stripped.startswith("<!DOCTYPE")
        or _stripped[:200].lower().startswith(("<html", "<body"))
        or text.count("<") > 20
    )
    if _looks_like_html:
        html_result = parse_html_email_draft(text, fallback_to=fallback_to)
        if html_result:
            return html_result

    # Subject — three formats:
    #   1. **Subject:** "value"  /  Subject: value
    #   2. **Subject Line A/B Testing:**\n  **A)** "..."\n  **B)** "..."   (use A)
    #   3. Subject line header followed by quoted line.
    subject = None
    m = re.search(
        r"\*\*\s*Subject\s*Line\s*(?:A/B\s*)?(?:Testing|Test|Variants?)?\s*:?\s*\*\*\s*\n+\s*\*\*?\s*A\)?\s*\*?\*?\s*[:\-]?\s*(.+)",
        text, re.IGNORECASE,
    )
    if m:
        subject = m.group(1).strip().splitlines()[0]
    if not subject:
        for pat in (
            r"\*\*\s*Subject\s*(?:Line)?\s*:?\s*\*\*\s*[:\-]?\s*(.+)",
            r"(?:^|\n)\s*Subject\s*(?:Line)?\s*[:\-]\s*(.+)",
        ):
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                candidate = m.group(1).strip().splitlines()[0]
                if candidate and not candidate.startswith("**"):
                    subject = candidate
                    break
    if not subject:
        m = re.search(
            r"\*\*\s*Subject[^\n]*\*\*\s*\n+\s*\*?\*?\s*A\)?\s*\*?\*?\s*[:\-]?\s*(.+)",
            text, re.IGNORECASE,
        )
        if m:
            subject = m.group(1).strip().splitlines()[0]
    if subject:
        subject = subject.strip()
        subject = re.sub(r'^[\s\*"\'`]+|[\s\*"\'`]+$', "", subject).strip()
        if not subject:
            subject = None

    # Fallback 1: Preview Text.
    if not subject:
        m = re.search(
            r"\*\*\s*Preview\s*(?:Text)?\s*(?:\([^)]*\))?\s*:?\s*\*\*\s*[:\-]?\s*(.+)",
            text, re.IGNORECASE,
        )
        if m:
            candidate = m.group(1).strip().splitlines()[0]
            candidate = re.sub(r'^[\s\*"\'`]+|[\s\*"\'`]+$', "", candidate).strip()
            if candidate and not candidate.startswith("**") and len(candidate) > 5:
                subject = candidate[:200]

    # Fallback 2: first non-trivial sentence.
    if not subject:
        cleaned = re.sub(r"\*\*[^*]+\*\*\s*[:\-]?", "", text)
        cleaned = re.sub(r"```[\s\S]*?```", "", cleaned)
        cleaned = re.sub(r"^---+\s*$", "", cleaned, flags=re.MULTILINE)
        for line in cleaned.split("\n"):
            line = line.strip()
            if not line or len(line) < 15:
                continue
            if re.match(
                r"^(hi|hello|hey|dear|best|sincerely|cheers|thanks|p\.?s\.?)\b",
                line, re.IGNORECASE,
            ):
                continue
            if line.startswith(("#", "-", "*", "[", ">")):
                continue
            candidate = line[:120]
            if len(line) > 120:
                candidate = candidate.rsplit(" ", 1)[0] + "..."
            subject = candidate
            break

    # Recipient
    to = fallback_to or ""
    if not to:
        for pat in (
            r"\*\*\s*(?:To|Recipient|Send\s*to)\s*:?\s*\*\*\s*[:\-]?\s*([^\s\n*]+@[^\s\n*]+)",
            r"(?:^|\n)\s*(?:To|Recipient)\s*[:\-]\s*([^\s\n]+@[^\s\n]+)",
        ):
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                to = m.group(1).strip().rstrip(".,;:")
                break
    if not to:
        m = re.search(
            r"\b([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b",
            text,
        )
        if m:
            to = m.group(1)

    # Send time
    send_time = None
    for pat in (
        r"\*\*\s*(?:Send\s*Time|When)\s*:?\s*\*\*\s*[:\-]?\s*(.+)",
        r"(?:^|\n)\s*Send\s*Time\s*[:\-]\s*(.+)",
    ):
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            send_time = m.group(1).strip().splitlines()[0].strip("*").strip()
            break

    # HTML body in fenced code block
    body_html = None
    m = re.search(r"```html\s*\n(.*?)\n```", text, re.DOTALL | re.IGNORECASE)
    if m:
        body_html = m.group(1).strip()

    # Plain body
    body = text
    body = re.sub(r"```html\s*\n.*?\n```", "", body, flags=re.DOTALL | re.IGNORECASE)
    body = re.sub(r"\*\*\s*Subject[^\n]*\n", "", body, flags=re.IGNORECASE)
    body = re.sub(r"\*\*\s*(?:To|Recipient|Send\s*to)[^\n]*\n", "", body, flags=re.IGNORECASE)
    body = re.sub(r"\*\*\s*(?:Send\s*Time|When)[^\n]*\n", "", body, flags=re.IGNORECASE)
    body = body.strip()

    if not (subject or body_html or body):
        return None

    if not body_html and body:
        body_html = markdown_to_basic_html(body)

    preview_text = ""
    pm = re.search(
        r"\*\*\s*Preview\s*(?:Text)?[^*]*\*\*\s*[:\-]?\s*([^\n]+)",
        text, re.IGNORECASE,
    )
    if pm:
        preview_text = pm.group(1).strip().strip('"').strip("*").strip()[:120]

    if body_html and not agent_html_already_designed(body_html):
        final_html = wrap_email_in_designed_template(
            body_html,
            business_name=business_name_for_template(),
            subject=subject or "",
            preview_text=preview_text,
        )
    else:
        final_html = body_html

    # IMPORTANT: field names must match the frontend's EmailDraft
    # interface — it reads `email_draft.html_body` / `email_draft.text_body`,
    # NOT `body_html` / `body`.
    return {
        "subject": (subject or "Untitled email")[:300],
        "to": to or "",
        "send_time": send_time or "",
        "text_body": body[:5000],
        "html_body": final_html,
        "preview_snippet": (body or text)[:200],
        "status": "draft_pending_approval",
    }


def parse_social_drafts_from_text(text: str) -> dict | None:
    """Extract X/Twitter and LinkedIn post variants from an agent reply.

    Returns a dict like `{twitter: "...", linkedin: "..."}` for the
    inbox row's social_draft column, or None when nothing
    recognisable was found. The frontend uses this to render the
    Publish to X / Publish to LinkedIn buttons.
    """
    if not text or len(text) < 30:
        return None

    def _grab_section(label_pattern: str) -> str | None:
        pat = (
            rf"(?:\*\*\s*{label_pattern}\s*[:\-]?\s*\*\*|##\s*{label_pattern})"
            rf"\s*[:\-]?\s*(.*?)"
            rf"(?=\n\s*\*\*\s*\w[^*]*\*\*\s*[:\-]|\n\s*##\s+|\Z)"
        )
        m = re.search(pat, text, re.IGNORECASE | re.DOTALL)
        if not m:
            return None
        section = m.group(1).strip()
        section = re.sub(r"^[\s>\-*]+", "", section)
        section = re.sub(r"[\s>\-*]+$", "", section)
        return section or None

    twitter = _grab_section(r"(?:Twitter|X(?:/Twitter)?|Tweet)")
    linkedin = _grab_section(r"LinkedIn")

    if not (twitter or linkedin):
        return None

    return {
        "twitter": (twitter or "")[:1000],
        "linkedin": (linkedin or "")[:5000],
    }
