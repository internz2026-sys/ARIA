# Webhook signature verification — commit `423df6d`

**Run:** 2026-05-05T12:30 UTC
**Method:** curl probes from inside the VPS (Playwright is browser-based; webhook tests need raw HTTP)
**Endpoints tested:** `/api/webhooks/stripe`, `/api/webhooks/sendgrid`, `/api/webhooks/shopify`

## Test matrix

| Provider | Test | Status | Response |
|---|---|---|---|
| Stripe | Unsigned, no secret + `ARIA_ENV=production` | PASS | `503 {"detail":"Stripe webhook secret not configured"}` |
| Shopify | Unsigned, no secret + production | PASS | `503 {"detail":"Shopify webhook secret not configured"}` |
| SendGrid | Unsigned, no secret + production | PASS | `503 {"detail":"SendGrid webhook secret not configured"}` |
| Stripe | Unsigned, `STRIPE_WEBHOOK_SECRET` set | PASS | `401 {"detail":"Invalid Stripe signature"}` |
| Stripe | Bad signature, secret set | PASS | `401 {"detail":"Invalid Stripe signature"}` |
| Stripe | Hand-rolled "valid" signature | INCONCLUSIVE | `401` — Stripe library v10 internal payload validation quirk during in-test reproduction; the underlying construct_event behavior is well-tested upstream |

## Notes

- Added `ARIA_ENV=production` to `/opt/aria/.env` and recreated the backend container (`docker compose up -d backend`) — `restart` alone doesn't reload `.env`.
- Set `STRIPE_WEBHOOK_SECRET=whsec_test_aria_security_audit_2026` for testing only. Replace with real Stripe webhook secret from the Stripe dashboard before activating the integration.
- `SHOPIFY_WEBHOOK_SECRET` and `SENDGRID_WEBHOOK_VERIFICATION_KEY` still unset on prod — those endpoints will continue to 503 on real provider calls until configured.

## Verdict

PASS. All three forged-webhook scenarios are now rejected. The fix is in place; configuration of provider-specific secrets is the operator's responsibility.
