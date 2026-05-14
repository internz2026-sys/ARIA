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

    def _get_child_mock(self, **kw: Any) -> MagicMock:
        """Force child attribute mocks to be plain MagicMock, NOT _MockSupabase.

        MagicMock's default `_get_child_mock` reuses the parent class for
        children, which means accessing `self.auth` on a `_MockSupabase`
        would create another `_MockSupabase` whose __init__ again accesses
        `self.auth.admin.update_user_by_id` — infinite recursion. Returning
        a plain MagicMock breaks the cycle while still letting the fluent
        builder pattern work for child attributes.
        """
        return MagicMock(**kw)

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Per-table insert/update payload buffers AND per-table response
        # registry. Plain attribute set would be intercepted by
        # MagicMock's __setattr__; using object.__setattr__ side-steps
        # that and stores the dicts on the underlying object dict so
        # `mock.<attr>` returns the dict, not a fresh MagicMock.
        # Critically, `_test_table_responses` MUST be a real dict and
        # NOT a MagicMock — set_response and _table_dispatch both rely
        # on real dict.get / dict.__setitem__ semantics. Storing it on a
        # MagicMock attribute would auto-create another MagicMock (truthy)
        # and `existing.get(name, [])` would yield a MagicMock the chain
        # treats as "no rows", silently returning .data=None for every
        # lookup.
        object.__setattr__(self, "_inserts_by_table", {})
        object.__setattr__(self, "_updates_by_table", {})
        object.__setattr__(self, "_test_table_responses", {})
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
        # ALSO expose at `mock.auth.admin.calls`. Tests use
        #   `getattr(auth_admin, "update_user_by_id_calls", None) or
        #    getattr(auth_admin, "calls", None) or []`
        # to be lenient about which name the conftest picks. Without
        # this, when no ban happened the `update_user_by_id_calls`
        # is `[]` (falsy), the `or` falls through to `auth_admin.calls`
        # which auto-creates a child MagicMock (truthy), the test reads
        # *that* as the recorded calls, and `assert not calls` fails.
        self.auth.admin.calls = calls_list
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
        # Store the per-table response on the real dict we created in
        # __init__ via object.__setattr__. We CANNOT use
        # `getattr(self.table, "_test_side_effect", None) or {}` here —
        # MagicMock auto-creates a child mock for any unknown attribute,
        # so the getattr returns a MagicMock (truthy), the `or` short-
        # circuits, and `existing_side_effect` is a MagicMock instead of
        # a dict. Any subsequent `.get(name, [])` then returns yet
        # another MagicMock, fails the isinstance(..., list) check in
        # the chain dispatch, and `single_result.data` silently becomes
        # None — exactly the symptom we hit in CI.
        existing_side_effect = self._test_table_responses
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

        self.table.side_effect = _table_dispatch


# Modules that imported `get_db` at module load via
# `from backend.services.supabase import get_db`. Each one holds a local
# reference to the original function; patching only the source module's
# attribute leaves all of these dangling at the real client. Discovered
# the hard way when test_agent_handshake hit
#     httpx.ConnectError: [Errno -2] Name or service not known
# from backend/routers/inbox.py:1046 because `inbox.get_db` still pointed
# at the real Supabase factory. Keep this list in sync with
#     grep -rn "^from backend\.services\.supabase import get_db" backend
_GET_DB_IMPORT_SITES = (
    "backend.ceo_actions",
    "backend.paperclip_office_sync",
    "backend.orchestrator",
    "backend.routers.ceo",
    "backend.routers.crm",
    "backend.routers.tasks",
    "backend.routers.email",
    "backend.routers.inbox",
    "backend.routers.plans",
    "backend.agents.media_agent",
    "backend.services.campaigns",
    "backend.services.chat",
    "backend.services.crm",
    "backend.services.content_library",
    "backend.services.email_inbound",
    "backend.services.email_sender",
    "backend.services.imap_inbound",
    "backend.services.inbox",
    "backend.services.plan_quotas",
    "backend.services.reports",
    "backend.services.projects",
    "backend.services.profiles",
    "backend.services.reports_campaign_roi",
    "backend.services.reports_channel_spend",
    "backend.services.reports_daily_pulse",
    "backend.services.scheduler",
    "backend.services.visualizer",
)


@pytest.fixture
def mock_supabase(monkeypatch: pytest.MonkeyPatch) -> _MockSupabase:
    """Patch every Supabase client accessor to return an in-memory mock.

    Default behavior: every `.table(...).<chain>.execute()` returns
    `data=[]`. Tests configure per-table responses via
    `mock_supabase.set_response("table_name", [...])`.

    Patch surface:
      * `backend.services.supabase.get_db` — canonical source
      * `backend.config.loader._get_supabase` — config loader's private
        accessor (separate caching path; without this, get_tenant_config
        crashes on the real create_client(SUPABASE_URL) lookup)
      * `backend.server._get_supabase` — server.py imports under an alias
      * Every entry in _GET_DB_IMPORT_SITES — modules that did
        `from backend.services.supabase import get_db` at module load
        and now have their own stale reference. Without patching these
        the routers/services bypass the mock entirely and hit the real
        network.
    """
    mock = _MockSupabase()
    # Default: empty result set on any unconfigured table.
    mock.set_response("__default__", [])

    _factory = lambda: mock  # noqa: E731 — readable as a one-liner

    monkeypatch.setattr("backend.services.supabase.get_db", _factory)
    monkeypatch.setattr("backend.config.loader._get_supabase", _factory)
    # server.py: `from backend.services.supabase import get_db as _get_supabase`
    monkeypatch.setattr("backend.server._get_supabase", _factory, raising=False)

    for module_path in _GET_DB_IMPORT_SITES:
        # raising=False: tolerate modules that aren't importable in a
        # slimmed test build (e.g. media_agent depends on optional deps).
        monkeypatch.setattr(f"{module_path}.get_db", _factory, raising=False)

    # Clear get_tenant_config's TTL cache. Without this, a tenant_configs
    # row set up via mock.set_response("tenant_configs", ...) is shadowed
    # by whatever stale TenantConfig was cached by an earlier test (or
    # an earlier real-network call before the patch landed). Tests like
    # test_unbanned_user_can_access use only mock_supabase (no
    # mock_tenant_lookup) and rely on the loader actually reading from
    # the mock — the cache short-circuits that read.
    try:
        from backend.config.loader import _config_cache
        _config_cache.clear()
    except Exception:
        pass

    # Same story for the role/status caches in profiles.py — without
    # invalidation, a real-network failure caches "user" + "active"
    # defaults that override the mock's `set_response("profiles", ...)`.
    try:
        from backend.services import profiles as _profiles_mod
        _profiles_mod.invalidate_role_cache()
        _profiles_mod.invalidate_status_cache()
        # The ban cache lives next to role/status — same reason for the
        # blanket invalidation per-test: a prior test's "not banned"
        # cache hit would shadow this test's banned profile mock.
        if hasattr(_profiles_mod, "invalidate_ban_cache"):
            _profiles_mod.invalidate_ban_cache()
    except Exception:
        pass

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
    # module (config.loader); but most routers and services import it at
    # module-load via `from backend.config.loader import get_tenant_config`,
    # so each one has its own stale local reference that the source-module
    # patch cannot reach. Keep this list in sync with
    #     grep -rn "^from backend\.config\.loader import .*get_tenant_config" backend
    monkeypatch.setattr("backend.config.loader.get_tenant_config", fake_get_tenant_config)
    for module_path in (
        "backend.agents.base",
        "backend.ceo_actions",
        "backend.orchestrator",
        "backend.server",
        "backend.routers.ceo",
        "backend.routers.crm",
        "backend.routers.email",
        "backend.routers.inbox",
        "backend.routers.plans",
        "backend.tools.campaign_analyzer",
        "backend.agents.media_agent",
        "backend.services.email_sender",
    ):
        # raising=False: tolerate optional modules that don't import in a
        # slimmed test build.
        monkeypatch.setattr(
            f"{module_path}.get_tenant_config", fake_get_tenant_config, raising=False
        )

    # Clear loader's TTL cache so a value set up by an earlier test (or
    # an earlier fixture invocation) doesn't shadow this test's mock.
    try:
        from backend.config.loader import _config_cache
        _config_cache.clear()
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


# ── Router-split safety net: shared owner/other-user fixture ──────────────
# Used by the test_routes_*.py files that pin behaviour of routes about to
# move from server.py to dedicated router modules. Each route gets at
# minimum: (1) happy path with owner JWT, (2) cross-tenant 403 with another
# user's JWT, (3) 401 with no JWT. This fixture centralises the boilerplate
# so a route file is just ~3 lines per case instead of repeating tenant
# registration + JWT minting in every test.
@pytest.fixture
def route_setup(
    mock_supabase,
    mock_tenant_lookup,
    auth_headers_factory,
):
    """Set up owner + other-user JWT headers + two tenant_ids.

    Returns a SimpleNamespace with:
      - owner_headers       : Authorization header dict for User A's JWT
      - other_headers       : Authorization header dict for User B's JWT
      - tenant_id           : User A's tenant_id (registered as owned)
      - other_tenant_id     : User B's tenant_id (NOT registered — used for
                              the cross-tenant lookup path; owner_headers
                              against this tenant_id should 403)
      - mock_supabase       : the mock so per-test responses can be wired
      - owner_email / other_email : the email claims used

    Per the safety-net brief: a test author writes one assertion per case
    (200/403/401), keeps the existing fixture wiring out of the test body,
    and the parallel server.py-splitting agent can move routes around
    without breaking these tests as long as the URL + auth contract stays
    fixed. If a route file silently drops `Depends(get_verified_tenant)`,
    the cross-tenant 403 case here will start returning 200 — that's the
    canonical failure signal.
    """
    import types
    import uuid as _uuid

    owner_email = "owner@aria.test"
    other_email = "other@aria.test"
    owner_id = "owner-uuid-1111-1111-1111-111111111111"
    other_id = "other-uuid-2222-2222-2222-222222222222"

    tenant_id = str(_uuid.uuid4())
    other_tenant_id = str(_uuid.uuid4())

    # Register only User A's tenant — User B's tenant_id stays unknown to
    # the (mocked) DB so the bouncer fires its tenant-not-found branch.
    mock_tenant_lookup(tenant_id, owner_email)

    owner_headers = auth_headers_factory(user_id=owner_id, email=owner_email)
    other_headers = auth_headers_factory(user_id=other_id, email=other_email)

    return types.SimpleNamespace(
        owner_headers=owner_headers,
        other_headers=other_headers,
        tenant_id=tenant_id,
        other_tenant_id=other_tenant_id,
        mock_supabase=mock_supabase,
        owner_email=owner_email,
        other_email=other_email,
        owner_id=owner_id,
        other_id=other_id,
    )
