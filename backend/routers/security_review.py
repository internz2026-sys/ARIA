"""Internal endpoint serving the per-PR security review GitHub Action.

The GitHub Actions workflow at .github/workflows/security-review.yml
runs on every pull_request. Originally it called the Anthropic API
directly (cost ~$0.10-0.50/PR). This endpoint lets it delegate to the
local Claude CLI already running on the VPS for CEO chat + agent
dispatch — same auth, no extra API tokens consumed, just adds onto the
existing Claude subscription's rate budget.

Security:
- The route prefix `/api/internal/` is added to `_PUBLIC_PREFIXES` in
  server.py so the global JWT middleware skips it. JWT can't gate this
  anyway — GitHub Actions has no JWT for ARIA users.
- HMAC-SHA256 of the raw request body in `X-Hub-Signature-256:
  sha256=<hex>` is the only gate. Same pattern adnanh/webhook uses for
  the deploy hook. The secret is `SECURITY_REVIEW_HMAC_SECRET` in the
  VPS .env, matching the `SECURITY_REVIEW_HMAC_SECRET` GitHub repo
  secret used by the workflow.
- The Claude subprocess receives the diff as USER-MESSAGE content, not
  as a command — even malicious diff contents are reviewed, not
  executed.

Returns plain markdown (text/markdown) so the workflow can pipe stdout
straight into the sticky PR comment without JSON-parsing.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os

from fastapi import APIRouter, HTTPException, Request, Response

from backend.tools.claude_cli import call_claude

logger = logging.getLogger("aria.security_review")

router = APIRouter(prefix="/api/internal", tags=["internal"])


MAX_DIFF_CHARS = 100_000
DEFAULT_MODEL = "claude-opus-4-7"


# Single source of truth for the security review prompt. The workflow
# script used to carry its own copy; keeping it server-side means we can
# tune the prompt without re-deploying the GitHub Actions runner.
SYSTEM_PROMPT = """You are an experienced application-security reviewer auditing pull requests against ARIA, a FastAPI + Supabase Postgres SaaS.

ARIA-specific context you must know:
- DB access is exclusively via supabase-py (`sb.table().select().eq().execute()`). `.eq()` / `.ilike(col, val)` / `.in_()` are parameterized — safe to interpolate user input. `.or_()` and `.filter()` accept RAW PostgREST grammar — user input MUST go through `backend.services._postgrest_util.safe_or_value()` or it's a SQL injection risk.
- Auth is JWT-based. Routes use `Depends(get_verified_tenant)` for per-tenant ownership checks. Routes whose prefix is in `_PUBLIC_PREFIXES` (`/api/inbox/`, `/api/auth/`, `/api/internal/`, etc.) skip JWT entirely — flag any new public-prefix entry as a potential auth bypass.
- Multi-tenant RLS is enforced at the Postgres level via `auth.jwt() ->> 'email' = owner_email` predicates on 14 tables. The service-role key bypasses RLS. Flag any new direct Postgres connection that uses the service role for user-scoped reads.
- The "Stripe deferred" memory means plan changes are intentionally free — don't flag missing payment gates as bugs.
- Frontend reads tokens from `localStorage` (key `sb-<project-ref>-auth-token`) — that's the supabase-js default, not a bug.

What to audit in the diff:
1. **SQL injection** — any new `.or_(f"...")` / `.filter(f"...")` without `safe_or_value()`; any raw psycopg2/asyncpg use; any f-string SQL.
2. **Auth bypass** — new routes missing `Depends(get_verified_tenant)` where tenant-scoped data is read/written; new entries in `_PUBLIC_PREFIXES`; service-role bypasses where user identity matters.
3. **IDOR** — endpoints that take a resource id without verifying the caller owns it; cross-tenant queries lacking the tenant_id filter.
4. **Secrets in code** — hardcoded API keys, passwords, signing keys, JWT secrets, OAuth client secrets.
5. **Insecure deserialization** — `pickle.load(`, `yaml.load(` without SafeLoader, `eval(`, `exec(`.
6. **Path traversal** — `open(user_input)`, file paths built from request data without normalization.
7. **Command injection** — `subprocess.run(..., shell=True)`, `os.system(`, format strings into shell commands.
8. **Open redirect / SSRF** — `redirect(url)` from user input, `requests.get(user_supplied_url)` with no allowlist.
9. **Crypto issues** — weak hashes (MD5/SHA1) used for security (not just cache keys), hardcoded encryption keys, missing HMAC verification on webhooks.
10. **XSS** — `dangerouslySetInnerHTML` with user input; backend rendering HTML that includes unescaped user data.
11. **CSRF** — POST endpoints accepting cookies without CSRF tokens (lower priority since ARIA uses Authorization headers).

Output format — STRICT markdown, no preamble:

If you find nothing: `**No security issues detected.**` on a single line.

If you find issues, use this exact structure:

```
**Findings:** N (M critical, K high, J medium, L low)

### F1: <short title> [Severity: Critical/High/Medium/Low]
- **File:** `path/to/file.py:line`
- **Issue:** <one sentence>
- **Why it matters:** <one sentence with concrete attack>
- **Fix:** <one sentence; reference existing patterns where possible>

### F2: ...
```

Be surgical:
- Only report real, exploitable issues. Do NOT pad with style nits or "consider adding a comment" suggestions.
- If the diff only contains test files, frontend styles, docs, or other low-risk changes, say `**No security issues detected.**` and stop.
- If you're unsure whether something is exploitable, mark it Low and note the uncertainty.
- Do not invent line numbers — use the line numbers visible in the diff hunks (the `@@ -X,Y +A,B @@` markers).
- Keep the whole report under 400 words unless there are genuinely many findings.
"""


def _verify_hmac(secret: str, body: bytes, sig_header: str) -> bool:
    """GitHub-style signature: header value is `sha256=<hex>`.

    Returns False on any mismatch (including missing/malformed header)
    so callers can fail closed without leaking which check tripped.
    """
    if not sig_header or not sig_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    provided = sig_header[len("sha256="):]
    return hmac.compare_digest(expected, provided)


@router.post("/security-review")
async def security_review(request: Request) -> Response:
    secret = os.environ.get("SECURITY_REVIEW_HMAC_SECRET", "").strip()
    if not secret:
        # Treat unconfigured as 503 — operator hasn't finished setup yet.
        # The workflow turns this into a "skipped" comment, doesn't fail
        # the PR.
        logger.error("SECURITY_REVIEW_HMAC_SECRET is not set; refusing to call Claude")
        raise HTTPException(status_code=503, detail="security-review not configured")

    raw_body = await request.body()
    sig_header = request.headers.get("x-hub-signature-256", "")
    if not _verify_hmac(secret, raw_body, sig_header):
        logger.warning("security-review HMAC mismatch from %s", request.client.host if request.client else "?")
        raise HTTPException(status_code=401, detail="invalid signature")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json")

    diff = (payload or {}).get("diff") or ""
    if not isinstance(diff, str) or not diff.strip():
        return Response(
            content="_No backend/frontend code changes in this PR._",
            media_type="text/markdown",
        )

    truncated = False
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS]
        truncated = True

    model = (payload.get("model") or DEFAULT_MODEL).strip() or DEFAULT_MODEL

    user_msg = f"PR diff to audit:\n\n```diff\n{diff}\n```"
    if truncated:
        user_msg += (
            f"\n\n*Note: diff was truncated to {MAX_DIFF_CHARS} chars. "
            "Findings cover the first chunk; very large PRs should be split.*"
        )

    try:
        review = await call_claude(
            SYSTEM_PROMPT,
            user_msg,
            model=model,
            max_tokens=4096,
            # Per-tenant rate limit bucket. "github-ci" keeps PR reviews
            # from competing with real tenants on the same hourly budget.
            tenant_id="github-ci",
            agent_id="security_reviewer",
        )
    except Exception as exc:
        logger.exception("security-review call_claude failed: %s", exc)
        return Response(
            content=f"_Security review failed: {exc}_",
            media_type="text/markdown",
            status_code=200,  # 200 so the workflow still posts a comment
        )

    body = (review or "").strip() or "_The auditor returned an empty review._"
    if truncated:
        body = (
            "> Diff was over 100k characters; only the first chunk was reviewed.\n\n"
            + body
        )
    return Response(content=body, media_type="text/markdown")
