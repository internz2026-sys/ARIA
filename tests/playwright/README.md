# ARIA Playwright E2E Tests

## Quick start

```bash
cd tests/playwright
npm install
npx playwright install chromium
npx playwright test
```

## Run against local dev

```bash
PLAYWRIGHT_BASE_URL=http://localhost:3000 npx playwright test
```

## Run a single spec

```bash
npx playwright test specs/virtual-office-sync.spec.ts
```

## View the HTML report

```bash
npm run test:report
```

## Auth

Tests try to reuse a cached Google OAuth session for `hdlcruz03@gmail.com`.
If the session has expired the tests will call `test.fail()` with a clear
message rather than trying to drive Google's OAuth UI (which Playwright
cannot reliably automate through 2FA).

To refresh the cached session, log in manually via `npx playwright codegen`
targeting the app URL, complete Google OAuth once, and save the resulting
`storageState` JSON to `fixtures/auth-session.json`.

## Destructive tests (onboarding funnel)

The onboarding funnel spec will **overwrite the real tenant_configs row**
for the test account. It is skipped by default.

To enable it:

```bash
PLAYWRIGHT_DESTRUCTIVE=1 npx playwright test specs/onboarding-funnel.spec.ts
```

Do not run with `PLAYWRIGHT_DESTRUCTIVE=1` against the production URL unless
you intend to overwrite the real onboarding profile.

## Specs

| File | What it covers |
|------|---------------|
| `specs/onboarding-funnel.spec.ts` | Full onboarding flow: /welcome → /describe (8 Qs) → /review → /select-agents → /launching → /dashboard |
| `specs/virtual-office-sync.spec.ts` | Agent status transitions Idle → Running via Socket.IO after triggering CEO chat delegation |
