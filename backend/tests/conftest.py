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

    Plus three recording APIs for tests that need to assert the agent
    handshake / admin-ban write paths landed in the right shape:

      * `inserts_for(table)`  — list of every dict passed to
        `.table(<table>).insert(payload)` during the test.
      * `updates_for(table)`  — list of every dict passed to
        `.table(<table>).update(payload)`.
      * `auth_admin_update_user_by_id_calls` — list of (user_id, attrs)
        tuples passed to `sb.auth.admin.update_user_by_id(...)`. Mirrored
        as `mock.auth.admin.update_user_by_id_calls` for the test
        introspection style in test_admin_ban.py.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Per-table insert/update payload buffers. Plain attribute set
        # would be intercepted by MagicMock's __setattr__; using
        # object.__setattr__ side-steps that and stores the lists on the
        # underlying object dict so `mock.<attr>` returns the list, not
        # a fresh MagicMock.
        object.__setattr__(self, "_inserts_by_table", {})
        object.__setattr__(self, "_updates_by_table", {})
        object.__setattr__(self, "_auth_admin_update_user_calls", [])

        # Wire the auth.admin.update_user_by_id recorder. The production
        # code path (services/profiles.ban_user) calls it as a sync
        # function: sb.auth.admin.update_user_by_id(uid, {"ban_duration": ...}).
        # We store calls on a real list AND expose two access patterns
        # (snake_case attribute on the recorder + .called / .call_args
        # via MagicMock's call recording) so test code that uses either
        # works.
        calls_list = self._auth_admin_update_user_calls

        def _record_update_user_by_id(user_id: Any, attrs: Any = None, *_args: Any, **_kwargs: Any) -> Any:
            calls_list.append((user_id, attrs))
            # Mirror onto the object's recorder list so test code can
            # introspect via `mock.auth.admin.update_user_by_id_calls`.
            return MagicMock(user=MagicMock(id=user_id))

        # Force the chain `auth.admin.update_user_by_id` to be a real
        # callable that records, not the auto-generated MagicMock.
        # Doing it on __init__ means every fresh _MockSupabase is wired
        # before the first ban_user call.
        self.auth.admin.update_user_by_id.side_effect = _record_update_user_by_id
        # Expose the recorder list at the canonical attribute path the
        # tests look for: mock.auth.admin.update_user_by_id_calls.
        self.auth.admin.update_user_by_id_calls = calls_list
        # Also expose the recorder list at the top level so tests can
        # introspect via `mock.auth_admin_update_user_by_id_calls` per
        # the conftest contract documented in the class docstring.
        self.auth_admin_update_user_by_id_calls = calls_list

    def inserts_for(self, table: str) -> list[dict]:
        """Return all dicts ever passed to `.table(<table>).insert(...).execute()`.

        Records the payload at the moment `.insert()` is called on the
        chain, BEFORE `.execute()`. That's intentional — supabase-py
        executes the insert at `.execute()` time, but for assertion
        purposes the payload shape is what matters and it never changes
        between `.insert(payload)` and `.execute()`.
        """
        return list(self._inserts_by_table.get(table, []))

    def updates_for(self, table: str) -> list[dict]:
        """Return all dicts ever passed to `.table(<table>).update(...).execute()`."""
        return list(self._updates_by_table.get(table, []))

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
        inserts_buffer = self._inserts_by_table
        updates_buffer = self._updates_by_table

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
            # `.single()` and `.maybe_single()` return ONE row, not a
            # list — the production code paths unwrap with
            # `result.data["field"]` not `result.data[0]["field"]`. Mirror
            # the real supabase-py shape: `.data` is the dict, or None.
            single_result = MagicMock()
            single_result.data = (
                chain_data[0] if isinstance(chain_data, list) and chain_data else (
                    chain_data if isinstance(chain_data, dict) else None
                )
            )
            single_result.count = None
            single_result.error = None
            chain.single.return_value.execute.return_value = single_result
            chain.maybe_single.return_value.execute.return_value = single_result
            # Common chain methods all return self so they can be chained
            # in any order before .execute().
            for method in ("select", "upsert", "delete",
                           "eq", "neq", "in_", "ilike", "like", "lt", "lte",
                           "gt", "gte", "order", "limit", "range", "match",
                           "is_", "or_", "filter"):
                getattr(chain, method).return_value = chain

            # `.insert(payload)` and `.update(payload)` need to record
            # the payload into the per-table buffer before returning
            # the chain so tests can assert on what was written. `.insert`
            # additionally needs to echo the row back from `.execute()`
            # so the create handler can read `result.data[0]["id"]` and
            # return a non-null `item` to the caller — supabase-py does
            # the same when you call .insert(...).execute() against a
            # real DB. Auto-assign an id if the payload didn't carry one.
            import uuid as _uuid_mod

            def _record_insert(payload: Any = None, *_a: Any, **_kw: Any) -> MagicMock:
                inserts_buffer.setdefault(name, []).append(payload)
                if isinstance(payload, dict):
                    echoed = dict(payload)
                    if "id" not in echoed or not echoed.get("id"):
                        echoed["id"] = f"mock-{name}-{_uuid_mod.uuid4().hex[:8]}"
                    insert_result = MagicMock()
                    insert_result.data = [echoed]
                    insert_result.count = 1
                    insert_result.error = None
                    insert_chain = MagicMock()
                    insert_chain.execute.return_value = insert_result
                    return insert_chain
                return chain

            def _record_update(payload: Any = None, *_a: Any, **_kw: Any) -> MagicMock:
                updates_buffer.setdefault(name, []).append(payload)
                return chain

            chain.insert.side_effect = _record_insert
            chain.update.side_effect = _record_update
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
