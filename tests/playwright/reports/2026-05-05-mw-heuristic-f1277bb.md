# Drop leaky middleware heuristic — commit `f1277bb`

**Run:** 2026-05-05T14:06 UTC

| Test | Status | Result |
|---|---|---|
| Code change present in container | PASS | `heuristic block GONE: True`, `removal note present: True` |
| Owner → own tenant (200) | PASS | Returns real contacts |
| Owner → foreign tenant (404) | PASS | `Tenant not found` from auth.py |
| Foreigner → real tenant (403) | PASS | `You don't have access to this tenant` (auth.py message, not the deleted middleware "Access denied to this tenant") |
| No auth (401) | PASS | `Invalid or expired token` |

The 403 message change confirms the middleware heuristic was removed and the router-level `Depends(get_verified_tenant)` is now the sole tenant-ownership enforcer. Cross-tenant access is still blocked with no regressions.

## Notes

- VPS hit a transient docker-build space issue during the first deploy attempt. Cleared 962 MB of build cache, retry succeeded. Disk usage 31G/48G — comfortable but worth monitoring as the model layers grow.
- Test reports cannot be saved by the playwright tester subagent (it's offline today); written manually after manual SSH verification.

## Verdict

PASS. -30 lines of fail-OPEN heuristic gone; explicit Depends layer authoritative.
