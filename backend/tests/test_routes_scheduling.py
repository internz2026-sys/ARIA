"""Safety-net route tests — Scheduling + Calendar endpoints.

Pins URL + auth contracts for the schedule/* and calendar/* routes about to
move into a dedicated router file. Service-level logic (scheduler_service)
has its own tests; this file only checks routing + auth.

Routes covered:
  - POST   /api/schedule/{tenant_id}/tasks
  - GET    /api/schedule/{tenant_id}/tasks
  - GET    /api/schedule/{tenant_id}/tasks/{task_id}
  - PATCH  /api/schedule/{tenant_id}/tasks/{task_id}
  - POST   /api/schedule/{tenant_id}/tasks/{task_id}/cancel
  - POST   /api/schedule/{tenant_id}/tasks/{task_id}/approve
  - POST   /api/schedule/{tenant_id}/tasks/{task_id}/reject
  - POST   /api/schedule/{tenant_id}/tasks/{task_id}/reschedule
  - POST   /api/schedule/{tenant_id}/tasks/{task_id}/execute-now
  - GET    /api/schedule/{tenant_id}/calendar
  - GET    /api/calendar/{tenant_id}/activity
"""
from __future__ import annotations

import pytest


pytestmark = pytest.mark.asyncio


TASK_ID = "task-001"


# ─── POST /api/schedule/{tenant_id}/tasks ────────────────────────────────

async def test_create_task_schema_422(client, route_setup):
    """ScheduleTaskRequest requires task_type, title, scheduled_at."""
    resp = await client.post(
        f"/api/schedule/{route_setup.tenant_id}/tasks",
        json={"task_type": "send_email"},  # missing title + scheduled_at
        headers=route_setup.owner_headers,
    )
    assert resp.status_code == 422, resp.text


async def test_create_task_cross_tenant_403(client, route_setup):
    resp = await client.post(
        f"/api/schedule/{route_setup.tenant_id}/tasks",
        json={
            "task_type": "send_email",
            "title": "Test",
            "scheduled_at": "2026-12-01T00:00:00Z",
        },
        headers=route_setup.other_headers,
    )
    assert resp.status_code == 403, resp.text


async def test_create_task_no_auth_401(client, route_setup):
    resp = await client.post(
        f"/api/schedule/{route_setup.tenant_id}/tasks",
        json={
            "task_type": "x", "title": "y", "scheduled_at": "2026-12-01T00:00:00Z",
        },
    )
    assert resp.status_code == 401, resp.text


# ─── GET /api/schedule/{tenant_id}/tasks ─────────────────────────────────

async def test_list_tasks_cross_tenant_403(client, route_setup):
    resp = await client.get(
        f"/api/schedule/{route_setup.tenant_id}/tasks",
        headers=route_setup.other_headers,
    )
    assert resp.status_code == 403, resp.text


async def test_list_tasks_no_auth_401(client, route_setup):
    resp = await client.get(f"/api/schedule/{route_setup.tenant_id}/tasks")
    assert resp.status_code == 401, resp.text


# ─── GET /api/schedule/{tenant_id}/tasks/{task_id} ───────────────────────

async def test_get_task_cross_tenant_403(client, route_setup):
    resp = await client.get(
        f"/api/schedule/{route_setup.tenant_id}/tasks/{TASK_ID}",
        headers=route_setup.other_headers,
    )
    assert resp.status_code == 403, resp.text


async def test_get_task_no_auth_401(client, route_setup):
    resp = await client.get(
        f"/api/schedule/{route_setup.tenant_id}/tasks/{TASK_ID}",
    )
    assert resp.status_code == 401, resp.text


# ─── PATCH /api/schedule/{tenant_id}/tasks/{task_id} ─────────────────────

async def test_patch_task_no_updates_400(client, route_setup):
    """Empty body → 400 'No updates provided'."""
    resp = await client.patch(
        f"/api/schedule/{route_setup.tenant_id}/tasks/{TASK_ID}",
        json={},
        headers=route_setup.owner_headers,
    )
    assert resp.status_code == 400, resp.text


async def test_patch_task_cross_tenant_403(client, route_setup):
    resp = await client.patch(
        f"/api/schedule/{route_setup.tenant_id}/tasks/{TASK_ID}",
        json={"title": "new"},
        headers=route_setup.other_headers,
    )
    assert resp.status_code == 403, resp.text


async def test_patch_task_no_auth_401(client, route_setup):
    resp = await client.patch(
        f"/api/schedule/{route_setup.tenant_id}/tasks/{TASK_ID}",
        json={"title": "x"},
    )
    assert resp.status_code == 401, resp.text


# ─── POST /api/schedule/{tenant_id}/tasks/{task_id}/cancel ───────────────

async def test_cancel_task_cross_tenant_403(client, route_setup):
    resp = await client.post(
        f"/api/schedule/{route_setup.tenant_id}/tasks/{TASK_ID}/cancel",
        headers=route_setup.other_headers,
    )
    assert resp.status_code == 403, resp.text


async def test_cancel_task_no_auth_401(client, route_setup):
    resp = await client.post(
        f"/api/schedule/{route_setup.tenant_id}/tasks/{TASK_ID}/cancel",
    )
    assert resp.status_code == 401, resp.text


# ─── POST /api/schedule/{tenant_id}/tasks/{task_id}/approve ──────────────

async def test_approve_task_cross_tenant_403(client, route_setup):
    resp = await client.post(
        f"/api/schedule/{route_setup.tenant_id}/tasks/{TASK_ID}/approve",
        headers=route_setup.other_headers,
    )
    assert resp.status_code == 403, resp.text


async def test_approve_task_no_auth_401(client, route_setup):
    resp = await client.post(
        f"/api/schedule/{route_setup.tenant_id}/tasks/{TASK_ID}/approve",
    )
    assert resp.status_code == 401, resp.text


# ─── POST /api/schedule/{tenant_id}/tasks/{task_id}/reject ───────────────

async def test_reject_task_cross_tenant_403(client, route_setup):
    resp = await client.post(
        f"/api/schedule/{route_setup.tenant_id}/tasks/{TASK_ID}/reject",
        headers=route_setup.other_headers,
    )
    assert resp.status_code == 403, resp.text


async def test_reject_task_no_auth_401(client, route_setup):
    resp = await client.post(
        f"/api/schedule/{route_setup.tenant_id}/tasks/{TASK_ID}/reject",
    )
    assert resp.status_code == 401, resp.text


# ─── POST /api/schedule/{tenant_id}/tasks/{task_id}/reschedule ───────────

async def test_reschedule_schema_422(client, route_setup):
    """Reschedule requires scheduled_at."""
    resp = await client.post(
        f"/api/schedule/{route_setup.tenant_id}/tasks/{TASK_ID}/reschedule",
        json={},
        headers=route_setup.owner_headers,
    )
    assert resp.status_code == 422, resp.text


async def test_reschedule_cross_tenant_403(client, route_setup):
    resp = await client.post(
        f"/api/schedule/{route_setup.tenant_id}/tasks/{TASK_ID}/reschedule",
        json={"scheduled_at": "2026-12-01T00:00:00Z"},
        headers=route_setup.other_headers,
    )
    assert resp.status_code == 403, resp.text


async def test_reschedule_no_auth_401(client, route_setup):
    resp = await client.post(
        f"/api/schedule/{route_setup.tenant_id}/tasks/{TASK_ID}/reschedule",
        json={"scheduled_at": "2026-12-01T00:00:00Z"},
    )
    assert resp.status_code == 401, resp.text


# ─── POST /api/schedule/{tenant_id}/tasks/{task_id}/execute-now ──────────

async def test_execute_now_cross_tenant_403(client, route_setup):
    resp = await client.post(
        f"/api/schedule/{route_setup.tenant_id}/tasks/{TASK_ID}/execute-now",
        headers=route_setup.other_headers,
    )
    assert resp.status_code == 403, resp.text


async def test_execute_now_no_auth_401(client, route_setup):
    resp = await client.post(
        f"/api/schedule/{route_setup.tenant_id}/tasks/{TASK_ID}/execute-now",
    )
    assert resp.status_code == 401, resp.text


# ─── GET /api/schedule/{tenant_id}/calendar ──────────────────────────────

async def test_calendar_cross_tenant_403(client, route_setup):
    resp = await client.get(
        f"/api/schedule/{route_setup.tenant_id}/calendar",
        headers=route_setup.other_headers,
    )
    assert resp.status_code == 403, resp.text


async def test_calendar_no_auth_401(client, route_setup):
    resp = await client.get(f"/api/schedule/{route_setup.tenant_id}/calendar")
    assert resp.status_code == 401, resp.text


# ─── GET /api/calendar/{tenant_id}/activity ──────────────────────────────

async def test_calendar_activity_owner_happy_path(client, route_setup):
    """Owner gets a 200 with the documented event-feed shape."""
    route_setup.mock_supabase.set_response("inbox_items", [])
    resp = await client.get(
        f"/api/calendar/{route_setup.tenant_id}/activity",
        headers=route_setup.owner_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "events" in body and "counts" in body


async def test_calendar_activity_cross_tenant_403(client, route_setup):
    resp = await client.get(
        f"/api/calendar/{route_setup.tenant_id}/activity",
        headers=route_setup.other_headers,
    )
    assert resp.status_code == 403, resp.text


async def test_calendar_activity_no_auth_401(client, route_setup):
    resp = await client.get(
        f"/api/calendar/{route_setup.tenant_id}/activity",
    )
    assert resp.status_code == 401, resp.text
