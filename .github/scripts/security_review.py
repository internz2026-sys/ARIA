"""Per-PR security review via the Anthropic API.

Reads a unified diff from argv[1] and sends it to Claude with a
security-focused system prompt. Prints the review as markdown to stdout.

Invoked by .github/workflows/security-review.yml. Requires the
ANTHROPIC_API_KEY env var (set as a GitHub Actions secret).

Design notes:
- Uses urllib instead of the anthropic SDK to keep the workflow's
  install step minimal — no extra pip step needed.
- Truncates diffs over 100k chars to keep token cost predictable; very
  large PRs get a partial review with a note saying so.
- System prompt is ARIA-specific: it knows the supabase-py + RLS +
  public-prefix patterns so it can flag deviations rather than producing
  generic OWASP advice.
- Model: claude-opus-4-7 (most capable). Drop to sonnet-4-6 to halve
  cost if PR volume picks up.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
import urllib.error


MAX_DIFF_CHARS = 100_000
MODEL = "claude-opus-4-7"
SYSTEM_PROMPT = """You are an experienced application-security reviewer auditing pull requests against ARIA, a FastAPI + Supabase Postgres SaaS.

ARIA-specific context you must know:
- DB access is exclusively via supabase-py (`sb.table().select().eq().execute()`). `.eq()` / `.ilike(col, val)` / `.in_()` are parameterized — safe to interpolate user input. `.or_()` and `.filter()` accept RAW PostgREST grammar — user input MUST go through `backend.services._postgrest_util.safe_or_value()` or it's a SQL injection risk.
- Auth is JWT-based. Routes use `Depends(get_verified_tenant)` for per-tenant ownership checks. Routes whose prefix is in `_PUBLIC_PREFIXES` (`/api/inbox/`, `/api/auth/`, etc.) skip JWT entirely — flag any new public-prefix entry as a potential auth bypass.
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


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: security_review.py <diff_path>", file=sys.stderr)
        return 2

    diff_path = sys.argv[1]
    try:
        with open(diff_path, "r", encoding="utf-8", errors="replace") as fp:
            diff = fp.read()
    except OSError as exc:
        print(f"_Could not read diff at {diff_path}: {exc}_")
        return 0

    if not diff.strip():
        print("_No backend/frontend code changes in this PR — skipping security review._")
        return 0

    truncated = False
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS]
        truncated = True

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("_ANTHROPIC_API_KEY not set — skipping security review._")
        return 0

    user_msg = f"PR diff to audit:\n\n```diff\n{diff}\n```"
    if truncated:
        user_msg += (
            f"\n\n*Note: diff was truncated to {MAX_DIFF_CHARS} chars. "
            f"Findings below cover only the first chunk; very large PRs should be split.*"
        )

    payload = {
        "model": MODEL,
        "max_tokens": 4096,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_msg}],
    }

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        print(f"_Anthropic API returned {exc.code}: {body}_")
        return 0
    except Exception as exc:
        print(f"_Anthropic API call failed: {exc}_")
        return 0

    content = data.get("content", [])
    text_blocks = [b.get("text", "") for b in content if b.get("type") == "text"]
    review = "\n\n".join(text_blocks).strip()

    if not review:
        print("_The auditor returned an empty review._")
        return 0

    if truncated:
        review = (
            "> Diff was over 100k characters; only the first chunk was reviewed.\n\n"
            + review
        )

    print(review)
    return 0


if __name__ == "__main__":
    sys.exit(main())
