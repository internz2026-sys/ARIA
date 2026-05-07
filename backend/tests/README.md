# ARIA Backend Test Suite

Pytest + httpx async-client harness for the FastAPI backend. Bootstrapped
2026-05-08 by Backend Coder #1 (Tenant Isolation / "Bouncer" track).

## Layout

```
backend/
├── tests/
│   ├── __init__.py
│   ├── conftest.py             # Shared fixtures: client, auth_headers_factory,
│   │                           # mock_supabase, mock_tenant_lookup
│   ├── test_tenant_isolation.py  # IDOR / "Bouncer" cross-tenant tests
│   └── README.md               # This file
├── pytest.ini                  # asyncio_mode=auto, testpaths, warning filters
└── requirements-test.txt       # Test-only deps (pytest, httpx, anyio)
```

The pytest configuration lives in `backend/pytest.ini`. Run pytest from
the repo root or the `backend/` directory; either works because
`testpaths` pins discovery.

## Running

```bash
# Install test deps (one-time, or after CI runs them on clean container)
pip install -r backend/requirements-test.txt

# Run everything verbose
pytest backend/tests/ -v

# Run with coverage report
pytest backend/tests/ --cov=backend --cov-report=term-missing

# Run a single file
pytest backend/tests/test_tenant_isolation.py -v

# Run a single test
pytest backend/tests/test_tenant_isolation.py::test_user_a_cannot_read_user_b_crm_contacts -v
```

## Fixtures

All defined in `conftest.py`. Sibling test modules
(`test_admin_ban.py`, `test_agent_handshake.py`, `test_rate_limit.py`,
`test_race_conditions.py`, `test_malicious_input.py`) consume these.

| Fixture | Scope | Purpose |
|---------|-------|---------|
| `client` | function | `httpx.AsyncClient` over `httpx.ASGITransport(app=app)`. No real socket. |
| `auth_headers_factory` | function | Callable: `(user_id, email) → {"Authorization": "Bearer <jwt>"}`. Mints HS256 tokens against the test secret. |
| `mock_supabase` | function | Patches `backend.services.supabase.get_db` + `backend.config.loader._get_supabase` to return an in-memory `MagicMock`. Use `.set_response("table_name", [...])` for per-table fixtures. |
| `mock_tenant_lookup` | function | Patches `get_tenant_config` so registered tenants resolve, others raise (→ 403 from `get_verified_tenant`). Returns a `register(tenant_id, owner_email)` callable. |
| `anyio_backend` | session | Returns `"asyncio"` — pins anyio-aware tests to asyncio. |

### Async test mode

`pytest.ini` sets `asyncio_mode = auto`, so any `async def test_*` is
automatically wrapped — no `@pytest.mark.asyncio` decorator needed.

### Setting Supabase responses

```python
async def test_my_endpoint(client, auth_headers_factory, mock_supabase, mock_tenant_lookup):
    tenant_id = "00000000-0000-0000-0000-000000000001"
    mock_tenant_lookup(tenant_id, "owner@example.com")
    mock_supabase.set_response("crm_contacts", [
        {"id": "c1", "tenant_id": tenant_id, "name": "Alice"},
    ])

    headers = auth_headers_factory(user_id="u1", email="owner@example.com")
    resp = await client.get(f"/api/crm/{tenant_id}/contacts", headers=headers)
    assert resp.status_code == 200
```

## Environment

`conftest.py` sets these env vars at import time (BEFORE any
`backend.*` import) so the auth module's module-level reads pick them
up:

| Var | Value | Why |
|-----|-------|-----|
| `SUPABASE_JWT_SECRET` | `test-secret-for-pytest-only-...` | Real HS256 verification path |
| `ARIA_ENV` | `test` | Avoids the "fail loud in prod" branches |
| `SUPABASE_URL` | `https://test.supabase.invalid` | Never dialed (mocked) but required for module load |
| `SUPABASE_SERVICE_ROLE_KEY` | `test-service-role-key` | Same — required for `create_client` arg parsing |
| `SUPABASE_ANON_KEY` | `test-anon-key` | Same |
| `REDIS_URL` | `""` | Forces in-memory rate-limit fallback |

The lifespan is replaced with a no-op so test startup doesn't try to
init Qdrant, Gmail polling, IMAP, or Paperclip.

## Adding a new test file

1. Create `backend/tests/test_<area>.py`.
2. Use `client`, `auth_headers_factory`, and `mock_supabase` directly —
   they're auto-discovered from `conftest.py`.
3. If the route uses `Depends(get_verified_tenant)`, also depend on
   `mock_tenant_lookup` and call `mock_tenant_lookup(tenant_id, email)`
   for tenants that should resolve.
4. `async def test_xyz(...)` — no decorator needed (auto mode).

## CI

`.github/workflows/tests.yml` runs the suite on every push to main and
every PR. No coverage gate yet — green/red signal only.
