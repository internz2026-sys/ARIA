"""Unit tests for backend/services/_postgrest_util.py.

Closes findings F1/F2 from the 2026-05-12 SQL injection audit. The
helper produces PostgREST-grammar-safe quoted values for `or=` /
`filter=` expressions; these tests pin the escape behaviour so future
edits can't silently weaken it.
"""
from __future__ import annotations

import pytest

from backend.services._postgrest_util import safe_or_value


def test_basic_alphanumeric_quoted():
    assert safe_or_value("foo") == '"foo"'


def test_comma_preserved_inside_quotes():
    # Quoting is the escape — `,` no longer breaks out of the value
    # position because PostgREST sees a quoted token.
    assert safe_or_value("foo,bar") == '"foo,bar"'


def test_parens_preserved_inside_quotes():
    assert safe_or_value("foo(bar)") == '"foo(bar)"'


def test_embedded_double_quote_escaped():
    # PostgREST requires `\"` inside a quoted value.
    assert safe_or_value('say "hi"') == '"say \\"hi\\""'


def test_embedded_backslash_doubled():
    # `\` must double to `\\` so PostgREST's tokenizer doesn't try to
    # consume the NEXT char as the escape target.
    assert safe_or_value("path\\file") == '"path\\\\file"'


def test_control_chars_stripped():
    assert safe_or_value("a\x00b\x07c\x1fd\x7fe") == '"abcde"'


def test_newline_and_tab_stripped():
    # \n=0x0a, \t=0x09 — both in the stripped 0x00-0x1f range.
    assert safe_or_value("a\nb\tc") == '"abc"'


def test_empty_string_returns_quoted_empty():
    assert safe_or_value("") == '""'


def test_none_returns_quoted_empty():
    assert safe_or_value(None) == '""'


def test_unicode_preserved():
    # PostgREST handles UTF-8 in quoted values fine; we don't need to
    # strip non-ASCII.
    assert safe_or_value("café") == '"café"'


def test_injection_attempt_neutralized():
    """The original F1 attack vector: trying to append a new condition.

    Before this fix, an input like `foo",baz.ilike.evil` could close the
    quoted value early and chain a new `or=` condition. The helper
    backslash-escapes the closing quote so the entire string remains a
    single value token.
    """
    out = safe_or_value('foo",baz.ilike.evil')
    # Single token — opens with `"`, closes with `"`, every embedded `"`
    # is preceded by `\`. The literal `,` and `.` inside are NOT
    # PostgREST grammar tokens once inside the quotes.
    assert out == '"foo\\",baz.ilike.evil"'
    # And the result starts/ends with exactly one un-escaped quote pair.
    assert out.startswith('"') and out.endswith('"')
    # Number of unescaped `"` is exactly 2 (the outer pair).
    # Count `"` not preceded by `\`.
    unescaped_quotes = sum(
        1 for i, ch in enumerate(out)
        if ch == '"' and (i == 0 or out[i - 1] != '\\')
    )
    assert unescaped_quotes == 2


def test_non_string_input_coerced():
    # Defensive: caller might pass an int or other non-str. str() it.
    assert safe_or_value(42) == '"42"'  # type: ignore[arg-type]
