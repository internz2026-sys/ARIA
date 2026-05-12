"""CI lint: prevent future `.or_(f"...")` / `.filter(f"...")` regressions.

The 2026-05-12 SQL injection audit found two places where PostgREST raw
filter strings were built from user-controlled input with brittle inline
sanitization (findings F1/F2). The fix was to route both through
`backend.services._postgrest_util.safe_or_value()`.

This test guards the rule going forward: any module that uses an
f-string inside `.or_(` or `.filter(` MUST import `safe_or_value`. If a
new code path introduces a raw f-string filter without the helper, this
test fails the build, before the gap reaches prod.

Allowlist: `_postgrest_util.py` itself is exempt (it's where the helper
lives), and test files are exempt (they may construct adversarial
strings on purpose to test escape behaviour).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

# Source root we audit. Test files + the helper itself are excluded.
_BACKEND_DIR = Path(__file__).resolve().parent.parent
_ALLOWLIST = {
    _BACKEND_DIR / "services" / "_postgrest_util.py",
}

# Matches `.or_(f"...` or `.filter(f"...` — the dangerous shape. The
# safe shape uses an already-quoted variable: `.or_(needle)` /
# `.or_(",".join(parts))`.
_RAW_FSTRING_FILTER_RE = re.compile(r"\.(or_|filter)\(\s*f[\"']")


def _python_files(root: Path):
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        if any(part == "tests" for part in path.parts):
            # Tests are exempt — they may build adversarial strings.
            continue
        if path in _ALLOWLIST:
            continue
        yield path


def test_no_raw_fstring_or_filter_without_helper():
    """Fail if any non-allowlisted file builds a `.or_(f"...")` filter
    without importing `safe_or_value`. Importing the helper is the
    contract: the reviewer can see at-a-glance that the f-string went
    through the escape.
    """
    offenders: list[str] = []
    for path in _python_files(_BACKEND_DIR):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if not _RAW_FSTRING_FILTER_RE.search(text):
            continue
        # Found an f-string-built filter. The file is OK iff it imports
        # safe_or_value — that's the human signal that the f-string
        # interpolates an already-escaped token.
        if "safe_or_value" in text:
            continue
        # Report each offending line so the test failure points at the
        # exact location to fix.
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _RAW_FSTRING_FILTER_RE.search(line):
                rel = path.relative_to(_BACKEND_DIR.parent)
                offenders.append(f"{rel}:{lineno}: {line.strip()}")

    if offenders:
        msg = (
            "Raw f-string PostgREST filter without safe_or_value():\n"
            + "\n".join(f"  {o}" for o in offenders)
            + "\n\nFix: import safe_or_value from backend.services._postgrest_util "
              "and wrap the interpolated value. See backend/services/profiles.py "
              "or backend/services/asset_lookup.py for examples."
        )
        pytest.fail(msg)
