# Error message sanitization — commit `d3dc81e`

**Run:** 2026-05-05T14:23 UTC

## Test inputs

Synthetic exception: `ValueError('access_token=eyJabcdef.bigsecret expired')` — simulates an OAuth provider error that echoes back the access token. Public message: `"Email send failed"`.

| Mode | Output | Token leaked? | Correlation ID? |
|---|---|---|---|
| Production (`ARIA_ENV=production`) | `Email send failed (ref: e65058e82f81)` | ❌ NO | ✅ YES |
| Dev (`ARIA_ENV` unset) | `Email send failed: access_token=eyJabcdef.bigsecret expired` | ✅ (intentional, dev debugging) | n/a |

Server log line captured during the prod-mode call (visible in `journalctl`):

```
[safe_errors e65058e82f81] Email send failed: access_token=eyJabcdef.bigsecret expired
```

## Verdict

PASS. Operator can grep `journalctl -u docker | grep "ref: e65058e82f81"` to recover the full detail without the client ever seeing it. All 8 audit-flagged callsites converted (`server.py:2668/1705/1871`, `crm.py:257/262`, `email.py:187/603`, `campaigns.py:429`).
