"""Per-PR security review via ARIA's local Claude CLI on the VPS.

Reads a unified diff from argv[1] and POSTs it (HMAC-signed) to ARIA's
internal security-review endpoint, which delegates to the local Claude
CLI running on the VPS. Prints the markdown review to stdout for the
sticky PR comment.

Invoked by .github/workflows/security-review.yml. Requires the
SECURITY_REVIEW_HMAC_SECRET env var (set as a GitHub Actions secret)
matching the value in /opt/aria/.env on the VPS.

Why VPS instead of Anthropic API:
- Free (consumes the existing Claude subscription, not metered tokens).
- Re-uses the same call_claude() machinery that powers CEO chat — auto-
  restore of ~/.claude.json, retry on transient CLI failures, etc.
- The auditor system prompt lives server-side now so it can be tuned
  without redeploying the Actions runner.

If the VPS is unreachable or returns an error, we still print a
markdown note so the workflow keeps moving and posts SOMETHING on the
PR rather than crashing.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import urllib.error
import urllib.request


DEFAULT_URL = "https://aria.hoversight.agency/api/internal/security-review"
TIMEOUT_SECS = 300  # Opus can take ~30-60s; pad generously for cold subprocess


def main() -> int:
    if len(sys.argv) < 2:
        print("_usage: security_review.py <diff_path>_", file=sys.stderr)
        return 0  # Don't fail the job — just print a note

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

    secret = os.environ.get("SECURITY_REVIEW_HMAC_SECRET", "")
    if not secret:
        print(
            "_SECURITY_REVIEW_HMAC_SECRET not set — skipping security review. "
            "Add it under Settings → Secrets and variables → Actions, "
            "matching the value in /opt/aria/.env on the VPS._"
        )
        return 0

    url = os.environ.get("SECURITY_REVIEW_URL", DEFAULT_URL)
    body = json.dumps({"diff": diff}).encode("utf-8")
    sig = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()

    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "x-hub-signature-256": sig,
            "content-type": "application/json",
            "user-agent": "aria-security-review-github-actions",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECS) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body_preview = exc.read().decode("utf-8", errors="replace")[:300]
        print(
            f"_Security review endpoint returned HTTP {exc.code}: {body_preview}_\n\n"
            "Check the VPS logs (`journalctl -u docker --since '5 min ago'`) "
            "or `docker logs aria-backend --tail 60`."
        )
        return 0
    except Exception as exc:
        print(
            f"_Could not reach the security-review endpoint: {exc}_\n\n"
            f"URL: `{url}`. Verify the VPS is up and the workflow can reach it."
        )
        return 0

    print(text.strip() or "_The auditor returned an empty review._")
    return 0


if __name__ == "__main__":
    sys.exit(main())
