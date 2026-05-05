"""Log redaction — keep secrets out of stdout / journalctl.

Two layers:

1. `redact_oauth_payload(text)` — explicit per-callsite redaction. Used by
   the OAuth tools (linkedin/twitter/google) before logging or raising
   RuntimeError with provider error bodies. Provider error responses
   sometimes echo the requesting access_token back, so logging
   `resp.text` raw was leaking valid Bearer tokens to journalctl.

2. `install_global_filter()` — defense-in-depth logging.Filter that
   applies the same redaction to every log record across the app. New
   code paths that forget to call redact_oauth_payload still get
   scrubbed at the handler boundary. Idempotent — safe to call from
   multiple module loads.

The patterns here target *secret-shaped* values, not just OAuth ones.
Any future leak (Stripe key in an exception, Supabase service role key
in an env dump, etc.) gets caught by the same filter.
"""
from __future__ import annotations

import logging
import re

# Patterns we'll redact. All are designed to match the VALUE, not the
# whole substring, so adjacent context is preserved for debugging.
#
# Secrets are typically long opaque strings (16+ chars) following one
# of a small set of key tokens. Capturing groups isolate the value so
# we can replace just that span with ***REDACTED***.
_REDACT_PATTERNS = [
    # JSON: "access_token":"<value>" / "refresh_token":"<value>" / etc.
    re.compile(r'("(?:access_token|refresh_token|id_token|client_secret|api_key|password|webhook_secret|secret)"\s*:\s*")([^"]{8,})"', re.IGNORECASE),
    # Form-encoded: access_token=<value>&...
    re.compile(r'(access_token|refresh_token|id_token|client_secret|api_key|password|webhook_secret)=([^&\s"\']{8,})', re.IGNORECASE),
    # Bearer <token>
    re.compile(r'(Bearer\s+)([A-Za-z0-9_\-\.~+/=]{20,})', re.IGNORECASE),
    # whsec_..., sk_live_..., sk_test_..., rk_live_..., pk_live_... (Stripe-shaped)
    re.compile(r'\b((?:whsec|sk_live|sk_test|rk_live|pk_live|pk_test)_[A-Za-z0-9]{16,})\b'),
    # Supabase JWT (eyJ...) — anything that looks like a JWT
    re.compile(r'\b(eyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,})\b'),
]


def redact_oauth_payload(text: str | bytes | None) -> str:
    """Scrub secret-shaped values from a string. Pass-through for None / non-strings."""
    if text is None:
        return ""
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8", errors="replace")
        except Exception:
            return "<undecodable>"
    s = str(text)
    for pat in _REDACT_PATTERNS:
        # Replacement preserves the prefix (key=, "key":", Bearer ) and
        # blanks only the value. JSON pattern has 2 groups (prefix +
        # value). Form/Bearer/whsec patterns have 1 prefix + 1 value.
        # The Stripe-shaped + JWT patterns have 1 group (the whole secret).
        if pat.groups == 2:
            s = pat.sub(lambda m: f'{m.group(1)}***REDACTED***"' if m.group(0).rstrip().endswith('"') else f'{m.group(1)}***REDACTED***', s)
        else:
            s = pat.sub("***REDACTED***", s)
    return s


class _RedactingFilter(logging.Filter):
    """logging.Filter that redacts every record's message + args.

    We rebuild the record's message in place so any Handler downstream
    (StreamHandler, FileHandler, etc.) emits the redacted form. The
    record's `msg` is left as-is for repr/inspection; only the
    *formatted* message is scrubbed via record.getMessage().
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            # Format the record once with its args, redact, and stash
            # back on a stable attribute the formatter will use. Setting
            # `msg` directly + clearing `args` is the standard trick.
            formatted = record.getMessage()
            redacted = redact_oauth_payload(formatted)
            if redacted != formatted:
                record.msg = redacted
                record.args = ()
        except Exception:
            # Never block a log emit because of a redaction error —
            # better to risk a bad regex than swallow logs.
            pass
        return True


_INSTALLED = False


def install_global_filter() -> None:
    """Attach `_RedactingFilter` to the root logger so every log record
    in the process gets scrubbed before it hits stdout / journalctl.

    Idempotent — multiple imports / startup retries won't add duplicate
    filters. Call once from server.py at import time.
    """
    global _INSTALLED
    if _INSTALLED:
        return
    root = logging.getLogger()
    flt = _RedactingFilter()
    root.addFilter(flt)
    # Also attach to existing handlers so anything that bypasses the
    # logger-level filter (rare, but possible with handler-direct calls)
    # is still scrubbed.
    for h in root.handlers:
        h.addFilter(flt)
    _INSTALLED = True
