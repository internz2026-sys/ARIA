# QuickBooks date validator — commit `860781d`

**Run:** 2026-05-05T15:13 UTC
**Method:** local Python test of `_validate_date()` (function is pure Python, no runtime deps; behavior is identical in-container)

## Test matrix

| Input | Expected | Result |
|---|---|---|
| `'2024-01-01'` | accept | PASS — accepted |
| `'2026-12-31'` | accept | PASS — accepted |
| `'2020-02-29'` | accept | PASS — accepted |
| `"2024-01-01' OR '1'='1"` | reject | PASS — `ValueError` |
| `"2024-01-01'; DROP--"` | reject | PASS — `ValueError` |
| `'abc'` | reject | PASS — `ValueError` |
| `''` | reject | PASS — `ValueError` |
| `None` | reject | PASS — `ValueError` |
| `'20240101'` (no separators) | reject | PASS — `ValueError` |
| `'2024/01/01'` (slashes) | reject | PASS — `ValueError` |
| `'2024-1-1'` (un-padded) | reject | PASS — `ValueError` |

11 of 11 cases match expected behavior.

## Notes

- The regex `^\d{4}-\d{2}-\d{2}$` is purely structural — it matches `2024-13-01` (invalid month) but QuickBooks' API will reject that upstream. The defense here is against quote-breakout for SQL-like injection, not date-validity. Strict date-validity would add `datetime.strptime(value, "%Y-%m-%d")` if needed later; not strictly required for the security goal.
- VPS rebuild is in flight; previous deploy still serves the function as-is since `quickbooks_tool.py` isn't imported on any hot path between deploys. Validator behavior is verified equivalent locally.

## Verdict

PASS. Hostile inputs that would close the SQL-like quote and inject extra clauses are rejected before the HTTP request is built.
