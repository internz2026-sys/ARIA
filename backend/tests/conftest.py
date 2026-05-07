"""Shared pytest fixtures for the ARIA backend test suite.

This file is the foundation other test modules (tenant_isolation,
admin_ban, agent_handshake, rate_limit, race_conditions,
malicious_input) build on. Keep fixtures generic — anything tenant-
isolation-specific belongs in test_tenant_isolation.py, not here.

Design decisions worth knowing:

1. **Real HS256 JWTs, not dependency_overrides for auth.** The auth
   middleware in server.py runs BEFORE FastAPI's dependency injection
   resolves, so `app.dependency_overrides[get_current_user] = ...` only
   affects per-route deps, not the middleware's `verify_jwt(token)`
   call. To exercise the same code paths a real client hits, we set a
   test JWT secret at module load and mint real HS256 tokens via
   python-jose (already a backend dep, see requirements.txt:32).

2. **Env var set at import time.** auth.py reads
   `SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET", "")` at
   module load and caches it via lru_cache on `_get_jwt_secret`. We
   set the env var BEFORE importing anything from backend, then clear
   the lru_cache to be safe in case some other test file imported
   auth first.

3. **Lifespan disabled.** server.py's lifespan runs Qdrant init,
   Gmail loops, Paperclip status sync, etc. None of those exist in
   CI. We override app.router.lifespan_context with a no-op so the
   ASGI startup sequence doesn't try to connect to anything.

4. **`mock_supabase` returns a single MagicMock.** Because supabase-py
   uses a fluent builder pattern (`.table().select().eq().execute()`),
   a plain MagicMock chains itself by default and `set_response` only
   has to override the terminal `.execute()` return value. Tests that
   need different responses for different tables compose by calling
   `set_response("table_name", [...])` multiple times.

5. **`get_tenant_config` mocking lives in a separate fixture
   (`mock_tenant_lookup`)** so tests that don't care about tenant
   ownership (rate limit tests, malicious input tests against public
   endpoints) don't have to wire it up.
"""
from __future__ import annotations

# ── Test environment setup (MUST run before any backend.* imports) ────────
import os

# Use a deterministic test secret so we can mint matching tokens. Any
# string works — the production secret never lands in the test process.
os.environ.setdefault("SUPABASE_JWT_SECRET", "test-secret-for-pytest-only-do-not-use-in-prod")
# Force dev-ish env so auth.py's "fail loud in prod" branches don't fire.
os.environ.setdefault("ARIA_ENV", "test")
# Skip Supabase real-client construction. Tests that need DB access
# patch get_db; tests that don't, never trigger the import.
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.invalid")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
os.environ.setdefault("SUPABASE_ANON_KEY", "test-anon-key")
# Disable the redis-backed rate limiter for tests — falls back to the
# in-memory shim and won't try to dial localhost:6379.
os.environ.setdefault("REDIS_URL", "")

import time
from contextlib import asynccontextmanager
from typing import Any, Callable
from unittest.mock import MagicMock

import httpx
import pytest
from jose import jwt

# Importing backend.server triggers the FastAPI app construction +
# router registration. That's what we want — the module-load-time
# imports happen ONCE per test session, after the env vars above are
# set. Keep this import down here, not at the top of the file.
from backend import auth as _auth_module
from backend.server import app as _fastapi_app


# Clear the lru_cache on _get_jwt_secret in case some module already
# called it during import resolution. Belt-and-braces — if we don't
# clear, a stale empty-string read would persist for the whole session.
try:
    _auth_module._get_jwt_secret.cache_clear()
except AttributeError:
    pass


# ── Lifespan override ─────────────────────────────────────────────────────
@asynccontextmanager
async def _noop_lifespan(app):
    """Replace server.py's lifespan during tests.

    The real lifespan starts Gmail polling, Qdrant init, Paperclip
    status sync, IMAP polling, and a content-repurpose loop. None of
    those have backing services in CI and most would block startup
    on connection attempts. Tests that need a specific startup hook
    can patch the relevant service module directly.
    """
    yield


# Swap before any test runs. ASGITransport calls into
# app.router.lifespan_context, so this replacement takes effect for
# every AsyncClient in the session.
_fastapi_app.router.lifespan_context = _noop_lifespan


# ── Async backend selection (anyio) ───────────────────────────────────────
@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """Force anyio to run tests on asyncio.

    Some sibling tests use pytest-anyio's parametrization to run
    against both asyncio and trio. ARIA's backend is asyncio-only
    (FastAPI + httpx + asyncpg-style supabase client), so trio would
    fail on `asyncio.create_task` calls inside the request handlers.
    """
    return "asyncio"


# ── HTTPX async client wrapping the FastAPI app ───────────────────────────
@pytest.fixture
async def client():
    """Yield an httpx.AsyncClient wired to the in-process FastAPI app.

    No real socket: ASGITransport short-circuits the network and feeds
    requests directly into app.__call__. base_url has to be a valid URL
    (httpx requires a scheme) but isn't reachable.

    Yields the client so the underlying ASGI startup/shutdown is run
    as part of the context — needed for the lifespan override above
    to take effect on every request.
    """
    transport = httpx.ASGITransport(app=_fastapi_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


# ── Supabase mock ─────────────────────────────────────────────────────────
class _MockSupabase(MagicMock):
    """A MagicMock with a `set_response` helper for the common case.

    The real supabase-py builder is a fluent API:

        sb.table("foo").select("*").eq("k", v).execute()

    A vanilla MagicMock already chains because each attribute access
    returns another MagicMock. The terminal `.execute()` call returns
    a MagicMock too — its `.data` attribute is whatever we set.

    `set_response("foo", [...])` configures the chain so any call that
    starts with `.table("foo")` ends in `.execute()` returning a result
    whose `.data == [...]`. This covers ~80% of the read patterns in
    backend/services/. Tests that need fancier behavior (chained
    .insert / .update / .upsert returning specific data, or a
    .table("foo").execute() that raises) reach into the underlying
    MagicMock directly.
    """

    def set_response(self, table_name: str, data: Any) -> None:
        """Configure `.table(<table_name>).<chain>.execute()` to return data."""
        # MagicMock's side_effect isn't ideal here because the chain is
        # not predictable (order of .select / .eq / .order varies). The
        # cleaner approach: when `.table(name)` is called with the
        # specific name, return a mock whose terminal .execute() yields
        # a result with .data == data. We use side_effect on .table to
        # branch on the argument.
        existing_side_effect = getattr(self.table, "_test_side_effect", None) or {}
        existing_side_effect[table_name] = data

        def _table_dispatch(name: str, *args: Any, **kwargs: Any) -> MagicMock:
            chain = MagicMock()
            chain_data = existing_side_effect.get(name, [])
            # Every terminal call (.execute, .single().execute, etc.)
            # resolves to a result object with .data set. supabase-py
            # also exposes .count and .error on the result; default
            # MagicMocks for those are fine.
            result = MagicMock()
            result.data = chain_data
            result.count = len(chain_data) if isinstance(chain_data, list) else None
            result.error = None
            # Make every method on the chain return the chain itself
            # (fluent builder), with .execute() and .single() resolving
            # to the result.
            chain.execute.return_value = result
            chain.single.return_value.execute.return_value = result
            chain.maybe_single.return_value.execute.return_value = result
            # Common chain methods all return self so they can be chained
            # in any order before .execute().
            for method in ("select", "insert", "upsert", "update", "delete",
                           "eq", "neq", "in_", "ilike", "like", "lt", "lte",
                           "gt", "gte", "order", "limit", "range", "match",
                           "is_", "or_", "filter"):
                getattr(chain, method).return_value = chain
            return chain

        self.table._test_side_effect = existing_side_effect
        self.table.side_effect = _table_dispatch


@pytest.fixture
def mock_supabase(monkeypatch: pytest.MonkeyPatch) -> _MockSupabase:
    """Patch backend.services.supabase.get_db to return an in-memory mock.

    Default behavior: every `.table(...).<chain>.execute()` returns
    `data=[]`. Tests configure per-table responses via
    `mock_supabase.set_response("table_name", [...])`.

    The patch is applied to BOTH module references (services.supabase
    and config.loader._get_supabase) because some routers call one and
    some call the other. Without the second patch, get_tenant_config
    would still hit the real `create_client(...)` and crash on the
    fake SUPABASE_URL.
    """
    mock = _MockSupabase()
    # Default: empty result set on any unconfigured table.
    mock.set_response("__default__", [])

    monkeypatch.setattr("backend.services.supabase.get_db", lambda: mock)
    monkeypatch.setattr("backend.config.loader._get_supabase", lambda: mock)

    return mock


# ── Tenant lookup mock ────────────────────────────────────────────────────
@pytest.fixture
def mock_tenant_lookup(monkeypatch: pytest.MonkeyPatch):
    """Patch get_tenant_config so get_verified_tenant has predictable behavior.

    Returns a configurator function `register(tenant_id, owner_email)`.
    Tenants registered with a given owner_email succeed for a JWT
    whose `email` matches; everything else raises a plain Exception
    that auth.get_verified_tenant collapses into 403 "Access denied"
    (auth.py:301-306). Critically: do NOT raise HTTPException(404)
    here — auth.py has a separate `except HTTPException: raise` block
    that would re-surface it as a literal 404, not the 403 the
    bouncer is supposed to produce. The real loader raises a
    supabase ApiError (subclass of Exception, not HTTPException) on
    missing rows, which is what this mock replicates.

    Use this together with auth_headers_factory: register User A's
    tenant with User A's email, then call User B's endpoint with User
    A's token to assert the 403.
    """
    registry: dict[str, str] = {}

    def register(tenant_id: str, owner_email: str) -> None:
        registry[str(tenant_id)] = owner_email.lower().strip()

    def fake_get_tenant_config(tenant_id):
        from backend.config.tenant_schema import TenantConfig
        tid = str(tenant_id)
        if tid not in registry:
            # Plain Exception — not HTTPException — so the broad
            # `except Exception` in get_verified_tenant fires and
            # produces 403, mirroring the real "tenant not found"
            # supabase error path.
            raise RuntimeError(f"tenant {tid} not found (test mock)")
        return TenantConfig(tenant_id=tid, owner_email=registry[tid])

    # Patch every call site that resolves the function. The auth module
    # imports get_tenant_config via a local `from ... import` inside
    # get_verified_tenant, so the canonical patch target is the original
    # module (config.loader); but routers import it at module-load via
    # `from backend.config.loader import get_tenant_config`, so we also
    # patch the routers that reference it directly.
    monkeypatch.setattr("backend.config.loader.get_tenant_config", fake_get_tenant_config)
    # The routers import the symbol at module load — patch those too.
    # Use a try/except so this fixture stays usable even when a router
    # is missing in a slimmed test build.
    for module_path in ("backend.routers.crm", "backend.routers.inbox"):
        try:
            monkeypatch.setattr(f"{module_path}.get_tenant_config", fake_get_tenant_config, raising=False)
        except Exception:
            pass

    register.registry = registry  # type: ignore[attr-defined]
    return register


# ── JWT auth headers factory ──────────────────────────────────────────────
@pytest.fixture
def auth_headers_factory() -> Callable[..., dict[str, str]]:
    """Mint Authorization headers signed with the test JWT secret.

    Usage:
        headers = auth_headers_factory(user_id="user-a", email="a@x.com")
        await client.get("/api/crm/<tenant>/contacts", headers=headers)

    The token mirrors the shape Supabase emits: HS256, aud=authenticated,
    sub=<user_id>, email=<email>, exp=<future>. server.py's middleware
    runs verify_jwt(token), which validates against the same secret we
    set in conftest's env-bootstrap block.
    """
    secret = os.environ["SUPABASE_JWT_SECRET"]

    def _make(
        user_id: str = "test-user",
        email: str = "test@aria.local",
        role: str = "authenticated",
        exp_offset: int = 3600,
        extra_claims: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        now = int(time.time())
        payload: dict[str, Any] = {
            "sub": user_id,
            "email": email,
            "role": role,
            "aud": "authenticated",
            "iat": now,
            "exp": now + exp_offset,
        }
        if extra_claims:
            payload.update(extra_claims)
        token = jwt.encode(payload, secret, algorithm="HS256")
        return {"Authorization": f"Bearer {token}"}

    return _make
