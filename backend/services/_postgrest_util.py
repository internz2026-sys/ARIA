"""PostgREST grammar helpers used when supabase-py's parameterization
isn't enough (.or_() / .filter() pass raw PostgREST expressions).

PostgREST's `or=` grammar parses `column.operator.value,column.operator.value`.
If a user-controlled `value` contains `,` `(` `)` `:` etc. without proper
quoting, an attacker can append a new condition and exfiltrate adjacent
rows. PostgREST DOES support a double-quoted form where any `"` or `\\`
inside is backslash-escaped — that's what this helper produces.

Audited 2026-05-12 as the fix for findings F1/F2 (Medium) in the SQL
injection audit. The CI lint `tests/test_lint_postgrest_or_safety.py`
fails any future `.or_(f"...")` / `.filter(f"...")` that doesn't go
through this helper, so don't bypass it.
"""
from __future__ import annotations

import re

# ASCII control characters + DEL — strip defensively. They have no
# legitimate place in a search term and some PostgREST versions
# treat 0x00 specially.
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


def safe_or_value(value: str | None) -> str:
    """Quote a value for safe use in PostgREST `or=` / `filter=` grammar.

    Wraps in double quotes and backslash-escapes embedded `"` and `\\`,
    matching PostgREST's documented grammar. Always returns a token that
    is safe to interpolate as the value portion of
    `column.operator.<token>` — the caller does NOT need to add quotes.

    Empty/None input returns the literal quoted-empty token `""` so the
    surrounding filter expression still parses.
    """
    if not value:
        return '""'
    cleaned = _CTRL_RE.sub("", str(value))
    return '"' + cleaned.replace("\\", "\\\\").replace('"', '\\"') + '"'
