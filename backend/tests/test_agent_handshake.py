"""Integration tests — agent skill handshake (POST /api/inbox/{tenant}/items).

The aria-backend-api skill curls this endpoint from inside Paperclip agents.
``_PUBLIC_PREFIXES`` includes "/api/inbox/" so the route is reachable
without a JWT — but the handler enforces several behavioural contracts the
frontend depends on:

  * Confirmation/status messages ("✅ Saved!") are rejected without an insert.
  * email_marketer rows whose content is a parseable email draft get
    type forced to "email_sequence" regardless of the agent's submitted type.
  * Recent (within ~5min) duplicate POSTs from the same tenant+agent with
    the same first-100-char prefix UPDATE the existing row instead of
    inserting a fresh one.

We exercise the create handler at the HTTP layer so the dedupe / parsing /
type-normalization paths are real, but stub out side-effects we don't care
about (sio.emit, notifications insert, content_index, project mirror,
social-text sanitizers from server.py) via monkeypatch where needed.
"""
from __future__ import annotations

import pytest


pytestmark = pytest.mark.asyncio


TENANT_ID = "44444444-4444-4444-4444-444444444444"


# ─────────────────────────────────────────────────────────────────────────────
# Shared fixture: neutralize side-effects orthogonal to the create contract
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _stub_inbox_side_effects(monkeypatch):
    """Mute realtime emits, notifications, content index, and project
    mirroring so the tests are pure I/O on the supabase mock.

    The handler imports several helpers from backend.server lazily inside
    its body (``_canon_agent_slug``, ``_sanitize_social_post_text``,
    ``_parse_email_draft_from_text``, ``_parse_social_drafts_from_text``)
    -- those are real helpers we want to keep exercising, so we leave
    them alone. Only the noisy I/O is stubbed.
    """
    # sio.emit is awaited inside the handler. We replace it on the
    # services.realtime module the inbox router imports from.
    async def _noop_emit(*_args, **_kwargs):
        return None

    import backend.services.realtime as realtime_mod
    monkeypatch.setattr(realtime_mod.sio, "emit", _noop_emit, raising=False)

    # log_agent_action is await'd from orchestrator inside the handler;
    # turn it into a no-op so we don't pull in the orchestrator's
    # real logging stack.
    async def _noop_log_agent_action(*_args, **_kwargs):
        return None

    import backend.orchestrator as orch_mod
    monkeypatch.setattr(orch_mod, "log_agent_action", _noop_log_agent_action, raising=False)

    # content_index and projects mirror are best-effort; stub them so a
    # failure path inside doesn't pollute the call counts on supabase.
    try:
        import backend.services.content_index as ci_mod
        monkeypatch.setattr(ci_mod, "index_inbox_row", lambda *a, **kw: None, raising=False)
    except Exception:
        # Module may not be importable in the test env — fine, the
        # handler swallows the import error too.
        pass
    try:
        import backend.services.projects as proj_mod
        monkeypatch.setattr(proj_mod, "create_project_task", lambda *a, **kw: {}, raising=False)
        monkeypatch.setattr(proj_mod, "extract_campaign_metadata", lambda *a, **kw: {}, raising=False)
    except Exception:
        pass
    try:
        import backend.services.campaigns as camp_mod
        monkeypatch.setattr(camp_mod, "create_campaign_from_inbox", lambda *a, **kw: None, raising=False)
    except Exception:
        pass

    yield


# ─────────────────────────────────────────────────────────────────────────────
# 1. Happy path — agent skill curl creates inbox row
# ─────────────────────────────────────────────────────────────────────────────

async def test_agent_skill_curl_creates_inbox_item(client, mock_supabase):
    """A vanilla content_writer POST creates a row, returns the new id, and
    the supabase mock records the insert into inbox_items."""
    # Recent-row dedupe lookup runs first; return [] so we bypass the
    # placeholder/prefix-match branches and hit the fresh insert path.
    mock_supabase.set_response("inbox_items", [])
    # The actual insert echoes the row back with an id assigned. Our mock
    # auto-generates ids on insert by default; if it doesn't, set_response
    # for the post-insert read shape gives us a deterministic value.
    mock_supabase.set_response(
        "inbox_items_insert_result",
        [{"id": "new-row-id-001", "tenant_id": TENANT_ID, "title": "Test blog draft"}],
    )

    payload = {
        "title": "Test blog draft",
        "content": (
            "## Five things every dev should know about JWT\n\n"
            "1. Don't put secrets in the payload.\n"
            "2. Verify alg explicitly.\n"
            "3. Rotate keys on a schedule."
        ),
        "type": "blog",
        "agent": "content_writer",
        "priority": "medium",
        "status": "needs_review",
    }
    resp = await client.post(f"/api/inbox/{TENANT_ID}/items", json=payload)

    assert resp.status_code == 200, f"got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body.get("item") is not None, f"expected item dict, got {body}"
    assert body["item"].get("id"), f"new item missing id: {body}"

    # Verify the insert went out to inbox_items with the right fields.
    inserts = mock_supabase.inserts_for("inbox_items")
    assert inserts, "no insert recorded on inbox_items"
    last_insert = inserts[-1]
    assert last_insert.get("tenant_id") == TENANT_ID
    assert last_insert.get("title") == "Test blog draft"
    assert last_insert.get("agent") == "content_writer"
    assert last_insert.get("type") == "blog"
    assert "JWT" in (last_insert.get("content") or "")


# ─────────────────────────────────────────────────────────────────────────────
# 2. Confirmation-message short circuit — must NOT insert
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "confirmation_text",
    [
        "✅ Email draft saved to ARIA Inbox",
        "Draft created and saved to inbox.",
        "Successfully saved to ARIA inbox!",
        "## Task Complete",
        "Draft ID: 12345",
    ],
)
async def test_agent_skill_confirmation_message_rejected(
    client, mock_supabase, confirmation_text
):
    """The handler short-circuits when ``_looks_like_confirmation_message``
    matches, returning {item: null, skipped: "confirmation_message"} and
    NOT inserting a row."""
    # Make sure the dedupe lookup returns [] in case we get past the gate
    # somehow — that way a missed rejection produces a fresh insert we
    # can detect.
    mock_supabase.set_response("inbox_items", [])

    payload = {
        "title": "Done",
        "content": confirmation_text,
        "type": "blog",
        "agent": "email_marketer",
        "priority": "medium",
        "status": "needs_review",
    }
    resp = await client.post(f"/api/inbox/{TENANT_ID}/items", json=payload)

    assert resp.status_code == 200, f"got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body.get("item") is None, (
        f"confirmation message should yield null item, got {body}"
    )
    assert body.get("skipped") == "confirmation_message", (
        f"expected skipped=confirmation_message, got {body}"
    )

    # And: no insert should have hit inbox_items.
    inserts = mock_supabase.inserts_for("inbox_items")
    assert not inserts, (
        f"confirmation message should NOT insert into inbox_items; got "
        f"{inserts}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 3. email_marketer type normalization
# ─────────────────────────────────────────────────────────────────────────────

async def test_agent_skill_email_marketer_normalizes_type(client, mock_supabase):
    """An email_marketer POST with parseable email content gets type
    overwritten to 'email_sequence' and status forced to
    'draft_pending_approval' regardless of what the agent submitted."""
    mock_supabase.set_response("inbox_items", [])

    # A clearly parseable email draft: subject line + body. The shape
    # _parse_email_draft_from_text recognizes uses **Subject:** / **To:**
    # markdown labels.
    email_content = (
        "**Subject:** Welcome to ARIA — your AI marketing team is online\n"
        "**To:** founder@example.com\n\n"
        "Hi there,\n\n"
        "Welcome aboard! Here are three things to do in your first hour:\n\n"
        "1. Review your GTM playbook.\n"
        "2. Check the inbox.\n"
        "3. Book a kick-off chat with the CEO agent.\n\n"
        "— ARIA"
    )

    payload = {
        "title": "Welcome email",
        "content": email_content,
        "type": "blog",  # WRONG on purpose — handler must normalize
        "agent": "email_marketer",
        "priority": "high",
        "status": "needs_review",  # also should be overridden
    }
    resp = await client.post(f"/api/inbox/{TENANT_ID}/items", json=payload)

    assert resp.status_code == 200, f"got {resp.status_code}: {resp.text}"
    body = resp.json()
    item = body.get("item")
    assert item is not None, f"expected item, got {body}"

    # The frontend EmailEditor only renders the form when:
    #   type == 'email_sequence'
    #   status == 'draft_pending_approval'
    #   email_draft is present
    # If any of those three drift, the buttons disappear silently.
    assert item.get("type") == "email_sequence", (
        f"email_marketer with parseable email should normalize "
        f"type=email_sequence, got {item.get('type')!r}"
    )
    assert item.get("status") == "draft_pending_approval", (
        f"email_marketer with parseable email should normalize "
        f"status=draft_pending_approval, got {item.get('status')!r}"
    )
    assert item.get("email_draft"), (
        f"email_marketer create should populate email_draft, got {item}"
    )

    # Confirm the same shape lives in the supabase insert payload so
    # the row that lands in the DB matches the API response.
    inserts = mock_supabase.inserts_for("inbox_items")
    assert inserts, "no insert recorded on inbox_items"
    last = inserts[-1]
    assert last.get("type") == "email_sequence"
    assert last.get("status") == "draft_pending_approval"
    assert isinstance(last.get("email_draft"), dict)
    # The frontend's EmailDraft interface uses html_body / text_body --
    # verify the parser landed those keys (NOT the legacy body_html /
    # body that broke the contenteditable iframe pre-2026-04-11).
    draft = last["email_draft"]
    assert any(
        k in draft for k in ("html_body", "text_body", "subject", "to")
    ), f"email_draft missing canonical fields: {draft}"


# ─────────────────────────────────────────────────────────────────────────────
# 4. Recent-row dedupe — second POST with same prefix updates instead of inserting
# ─────────────────────────────────────────────────────────────────────────────

async def test_agent_skill_recent_row_dedupes(client, mock_supabase):
    """Two POSTs with same tenant + agent + same first-100-char content
    prefix within 5min: the second one updates the existing row rather
    than inserting a new one (Strategy 2 prefix-match dedupe in
    routers/inbox.py:create_inbox_item)."""
    # Pre-seed an existing row that the dedupe lookup will find. The
    # handler's recent-row query selects (id, content, type, status, title)
    # and returns up to 8 rows ordered by created_at desc.
    existing_content = (
        "## How to ship faster — a developer's guide\n\n"
        "Shipping is a habit, not a target. Three loops to build today...\n"
        "(more body)"
    )
    existing_row = {
        "id": "existing-row-id-001",
        "content": existing_content,
        "type": "blog",
        "status": "needs_review",
        "title": "How to ship faster",
        # title is checked for "is working on" placeholder pattern
    }
    # First call to set_response wins for the recent-row lookup.
    mock_supabase.set_response("inbox_items", [existing_row])

    # Second POST: same agent (content_writer), same first-100 chars.
    payload = {
        "title": "How to ship faster — v2",
        "content": existing_content,  # identical first 100 chars
        "type": "blog",
        "agent": "content_writer",
        "priority": "medium",
        "status": "needs_review",
    }
    resp = await client.post(f"/api/inbox/{TENANT_ID}/items", json=payload)

    assert resp.status_code == 200, f"got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body.get("deduped") is True, (
        f"second POST should be deduped, got {body}"
    )
    # Item.id should match the pre-seeded row -- we updated, not inserted.
    item = body.get("item") or {}
    assert item.get("id") == "existing-row-id-001", (
        f"deduped row id should match existing, got {item.get('id')!r}"
    )

    # And: NO fresh insert should have hit inbox_items. The dedupe path
    # uses .update().eq("id", ...).execute() instead.
    inserts = mock_supabase.inserts_for("inbox_items")
    assert not inserts, (
        f"prefix-match dedupe should update, not insert; got inserts: "
        f"{inserts}"
    )

    # Verify the update fired with the new content/title.
    updates = mock_supabase.updates_for("inbox_items")
    assert updates, "expected an update on inbox_items for the deduped row"
    last_update = updates[-1]
    # The handler writes title, content, type, status, updated_at on the
    # dedupe path. updates_for is expected to return a list of update
    # payload dicts (matching set on .update()).
    assert last_update.get("title") == "How to ship faster — v2"
    assert last_update.get("content") == existing_content
