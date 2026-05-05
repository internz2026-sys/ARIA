# Inbound webhook secret enforcement — commit `90eb650`

**Run:** 2026-05-05T13:08 UTC
**Endpoint:** `POST /api/email/inbound`

| Test | Status | Response |
|---|---|---|
| Unsigned with secret set + production | PASS | `401 {"detail":"Invalid signature"}` |
| Bad signature with secret set | PASS | `401 {"detail":"Invalid signature"}` |

`INBOUND_WEBHOOK_SECRET` set on prod (44-char urlsafe). `ARIA_ENV=production`. The endpoint now correctly refuses any unsigned/bad-sig inbound webhook. Postmark/Resend dashboards still need to be updated with the same secret value to make signed real provider webhooks succeed.

## Verdict

PASS. Forged-inbound-reply attack vector closed.
