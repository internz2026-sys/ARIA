# /api/media/ token gate — commit `a2aa672`

**Run:** 2026-05-05T13:14 UTC
**Endpoint:** `POST /api/media/{tenant_id}/generate`

| Test | Status | Response |
|---|---|---|
| No `X-Aria-Agent-Token` header | PASS | `401 {"detail":"Invalid agent token"}` |
| Wrong token | PASS | `401 {"detail":"Invalid agent token"}` |
| Valid token + empty prompt | PASS | `200 {"status":"failed","error":"prompt is required"}` (auth passed; rejected for input reason) |

`ARIA_INTERNAL_AGENT_TOKEN` set on prod (44-char urlsafe). Cost-amplification attack via unauth'd image generation is closed.

## Operator action required

The Paperclip-side `aria-backend-api` skill MD must be updated to include the header on its curl call to `/api/media/.../generate`. Otherwise the Media Designer agent will start receiving 401s on every legitimate run.

Token value lives in `/opt/aria/.env` on the VPS. Update Paperclip → Skills → `aria-backend-api` → Edit, append the header to the relevant curl example:

```
curl -X POST http://172.17.0.1:8000/api/media/$TENANT_ID/generate \
  -H "Content-Type: application/json" \
  -H "X-Aria-Agent-Token: <token-from-env>" \
  -d '{"prompt": "..."}'
```

## Verdict

PASS. Endpoint is fail-closed in production. Frontend code does not call this endpoint, so no client-side breakage.
