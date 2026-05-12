"""Login attempt rate-limiter — protects against brute-force credential
guessing on the Supabase Auth login flow.

Frontend integration:
  1. Before calling `supabase.auth.signInWithPassword(...)`, GET
     /api/auth/login-status?email=<x>. If `allowed:false`, refuse to
     submit and show "try again in N minutes".
  2. After Supabase returns an error (wrong password / unknown user),
     POST /api/auth/login-failed { email }. This increments BOTH the
     per-email bucket and the per-IP bucket.
  3. After Supabase returns success, POST /api/auth/login-success
     { email } so the per-email counter resets — a legitimate user who
     mistyped their password a few times shouldn't stay locked out
     after they finally succeed.

Buckets:
  - Per-email: 5 attempts / 15min. Catches a targeted attack on a
    specific account.
  - Per-IP:    20 attempts / 15min. Catches an attacker rotating
    through email addresses from one source.

The endpoints are intentionally unauthenticated (the user isn't
signed in yet during login). They live under /api/auth/ which is
already in _PUBLIC_PREFIXES in server.py.

Threat model + chosen tradeoffs:
  - The /login-status check leaks "this email is currently
    rate-limited", which technically tells an attacker "someone has
    been hammering this account recently." Tradeoff: a legitimate
    user needs to know they're locked out so the UI can show the
    message. The IP-bucket backstop limits how many emails a single
    attacker can probe.
  - /login-failed is unauthenticated and trusts the frontend to call
    it ONLY after a real Supabase failure. A malicious caller could
    artificially lock out an account by spamming it. The IP-bucket
    rate-limits the lockout endpoint itself (anyone hitting it more
    than 20 times in 15min from one IP gets blocked from triggering
    further lockouts), so this is bounded.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from backend.services import rate_limit as _rate_limit

logger = logging.getLogger("aria.routers.login_rate_limit")

router = APIRouter(prefix="/api/auth", tags=["Login Rate Limit"])


# Configurable via env so an operator can tighten/loosen without a code
# change. Defaults match the user's requested 5 / 15min per email.
def _email_limit() -> int:
    try:
        return max(1, int(os.environ.get("LOGIN_ATTEMPT_EMAIL_LIMIT", "5")))
    except ValueError:
        return 5


def _ip_limit() -> int:
    try:
        return max(1, int(os.environ.get("LOGIN_ATTEMPT_IP_LIMIT", "20")))
    except ValueError:
        return 20


def _window_seconds() -> int:
    try:
        return max(60, int(os.environ.get("LOGIN_ATTEMPT_WINDOW_SECONDS", "900")))
    except ValueError:
        return 900


# Light email-shape validation. We don't enforce RFC strict here — just
# enough to refuse obvious junk and prevent the buckets from being
# polluted with garbage strings.
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def _normalize_email(value: Optional[str]) -> str:
    if not value:
        return ""
    s = str(value).strip().lower()
    if not _EMAIL_RE.match(s) or len(s) > 254:
        return ""
    return s


def _client_ip(request: Request) -> str:
    """Best-effort client IP. Honors X-Forwarded-For (set by nginx in
    docker-compose) so we get the real public IP, not the docker bridge
    IP. Falls back to the direct peer address."""
    xff = (request.headers.get("x-forwarded-for") or "").strip()
    if xff:
        # First entry is the original client; the rest are proxies
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class LoginEmailBody(BaseModel):
    email: str


@router.get("/login-status")
async def login_status(email: str, request: Request):
    """Return whether this email is currently allowed to attempt login.

    Returns:
        {
          "allowed": bool,
          "attempts_remaining": int,
          "retry_after_seconds": int,   # only when blocked
          "limit": int,
          "window_seconds": int,
        }
    """
    safe_email = _normalize_email(email)
    if not safe_email:
        # Don't 400 — that would let an attacker probe email-shape
        # validation. Just answer "allowed" so legitimate users never
        # see weird errors before they've finished typing.
        return {
            "allowed": True,
            "attempts_remaining": _email_limit(),
            "retry_after_seconds": 0,
            "limit": _email_limit(),
            "window_seconds": _window_seconds(),
        }

    email_count, email_ttl = _rate_limit.peek("login_fail:email", safe_email)
    ip_count, ip_ttl = _rate_limit.peek("login_fail:ip", _client_ip(request))

    email_blocked = email_count >= _email_limit()
    ip_blocked = ip_count >= _ip_limit()

    if email_blocked or ip_blocked:
        # Surface the longer of the two TTLs so the user knows the real
        # wait. Floor at 60s if Redis returned -1 / 0 (key exists but
        # peek didn't get a TTL).
        retry = max(email_ttl if email_blocked else 0, ip_ttl if ip_blocked else 0, 60)
        return {
            "allowed": False,
            "attempts_remaining": 0,
            "retry_after_seconds": retry,
            "limit": _email_limit(),
            "window_seconds": _window_seconds(),
            "reason": "ip" if (ip_blocked and not email_blocked) else "email",
        }

    return {
        "allowed": True,
        "attempts_remaining": max(0, _email_limit() - email_count),
        "retry_after_seconds": 0,
        "limit": _email_limit(),
        "window_seconds": _window_seconds(),
    }


@router.post("/login-failed")
async def login_failed(body: LoginEmailBody, request: Request):
    """Record a failed login attempt for this email + the caller's IP.

    Frontend should call this AFTER Supabase Auth returns an error.
    Returns the same shape as /login-status so the UI can immediately
    show "X attempts remaining" without a second round-trip.
    """
    safe_email = _normalize_email(body.email)
    client_ip = _client_ip(request)

    if safe_email:
        _rate_limit.hit(
            "login_fail:email", safe_email, _email_limit(), _window_seconds()
        )
    # Always increment the IP bucket — even on garbage emails — so an
    # attacker spamming bad inputs from one IP gets throttled too.
    _rate_limit.hit("login_fail:ip", client_ip, _ip_limit(), _window_seconds())

    # Re-peek so the response reflects post-increment state.
    email_count, _ = _rate_limit.peek("login_fail:email", safe_email) if safe_email else (0, 0)
    ip_count, ip_ttl = _rate_limit.peek("login_fail:ip", client_ip)

    email_blocked = email_count >= _email_limit()
    ip_blocked = ip_count >= _ip_limit()

    if email_blocked or ip_blocked:
        # Light log so an operator scanning journalctl can see brute-
        # force activity. Email is logged in lower-case but otherwise
        # not redacted -- the brute-force protection IS the feature.
        logger.warning(
            "[login_rate_limit] account locked: email=%s ip=%s email_count=%d ip_count=%d",
            safe_email or "<invalid>", client_ip, email_count, ip_count,
        )

    return {
        "allowed": not (email_blocked or ip_blocked),
        "attempts_remaining": max(0, _email_limit() - email_count),
        "retry_after_seconds": ip_ttl if ip_blocked else 0,
        "limit": _email_limit(),
        "window_seconds": _window_seconds(),
    }


@router.post("/login-success")
async def login_success(body: LoginEmailBody, request: Request):
    """Reset the per-email bucket on a successful login.

    A user who recovered from a few typos shouldn't stay rate-limited
    after they correctly authenticate. The IP bucket is intentionally
    NOT reset here -- if many different accounts were attempted from
    one IP, that's still suspicious activity even if one of them
    eventually succeeds.
    """
    safe_email = _normalize_email(body.email)
    if safe_email:
        _rate_limit.reset("login_fail:email", safe_email)
    return {"ok": True}


# ── Ban status (public — banned users have no session) ────────────────────
#
# The /banned page renders WITHOUT a Supabase session: the user just got
# 403'd off every authenticated endpoint, so we can't require auth here
# without putting them in a redirect loop. Threat model is acceptable:
#  * Endpoint leaks "<user_id> is banned + their reason" to anyone who
#    knows the user_id. user_ids are uuids and not enumerable, and the
#    response carries only the reason copy the user already saw at ban
#    time — no PII or session material.
#  * The pause status / role / email do NOT leak here. Only the three
#    ban-specific fields.
#
# Rate-limited by IP via the global middleware (120/min/IP) so this
# can't be turned into a user_id-existence oracle.

@router.get("/ban-status/{user_id}")
async def ban_status(user_id: str, request: Request):
    """Return ban metadata for a user_id. Public — no JWT required.

    The frontend /banned page (which the user lands on after the
    middleware 403's their JWT with detail=BANNED) calls this with the
    user_id from the redirect URL so the page can show the reason and
    "banned until" timestamp.

    Response shape:
      {
        "banned": true,
        "banned_at": "<iso>",
        "banned_until": "<iso>" | null,    // null when indefinite
        "indefinite": false,
        "reason": "<text>" | null
      }
    or {"banned": false} for non-banned / unknown users.
    """
    # Lazy-import to dodge the heavy supabase client load when this
    # module is imported (the login-rate-limit endpoints don't need it).
    from backend.services import profiles as profiles_service

    # Light input validation — the path param is already typed str, but
    # an empty / overlong value short-circuits to a 200 {"banned": false}
    # instead of issuing a Supabase query. uuids are 36 chars; 64 is
    # generous and keeps the cap loose enough for future user_id shapes.
    uid = (user_id or "").strip()
    if not uid or len(uid) > 64:
        return {"banned": False}

    return profiles_service.get_ban_status(uid)


@router.get("/ban-status-by-email/{email}")
async def ban_status_by_email(email: str, request: Request):
    """Resolve a banned user's ban metadata from their email address.

    Why this exists: when a banned user tries to log in,
    `supabase.auth.signInWithPassword` returns a generic error and the
    frontend has NO user_id to pass to /ban-status/{uid}. This endpoint
    fills that gap — given an email, look up the matching profiles row
    and return the same payload as /ban-status, plus the resolved
    user_id so the frontend can redirect to /banned?user=<uid>.

    Same response shape as /ban-status with `user_id` added on hits:
      {
        "user_id": "<uid>",
        "banned": true,
        "banned_at": "<iso>",
        "banned_until": "<iso>" | null,
        "indefinite": false,
        "reason": "<text>" | null
      }
    or {"banned": false} for non-banned / unknown emails (no oracle).

    Public — no JWT. Rate-limited by IP via the global middleware.
    """
    from backend.services import profiles as profiles_service
    from backend.services.supabase import get_db

    # Light input validation. Real RFC 5321 cap is 254; round to 320
    # for the few edge cases that exceed it.
    e = (email or "").strip().lower()
    if not e or len(e) > 320 or "@" not in e:
        return {"banned": False}

    try:
        sb = get_db()
        res = (
            sb.table("profiles")
            .select("user_id")
            .eq("email", e)
            .limit(1)
            .execute()
        )
        rows = res.data or []
    except Exception:
        # Fail-closed quietly: a DB hiccup looks the same as "unknown
        # email", no oracle leak. Login page falls through to its
        # generic error display.
        return {"banned": False}

    if not rows:
        return {"banned": False}

    uid = rows[0].get("user_id")
    status = profiles_service.get_ban_status(uid)
    # If banned, include the resolved user_id so the frontend can
    # redirect to /banned?user=<uid>. If not banned, hide the uid (no
    # oracle).
    if status.get("banned"):
        status = dict(status)
        status["user_id"] = uid
    return status
