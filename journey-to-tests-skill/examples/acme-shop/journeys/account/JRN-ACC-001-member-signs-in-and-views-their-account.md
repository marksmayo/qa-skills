---
id: JRN-ACC-001
title: Member signs in and views their account
domain: account
platforms: [web, ios, android]
persona: member
priority: P1
priority_reason: Authentication gates every member feature
status: inferred
entry_point: /login
goal: Account page shows the member's name and order history
flags: []
automation: partial
tests: [web/e2e/login.spec.ts]
---

## Preconditions
- A member account exists with at least one past order

## Steps
1. **Action:** Open the login page
   **Expect:** Email and password fields and a "Sign in" button are visible
2. **Action:** Enter valid credentials and click "Sign in"
   **Expect:** User is redirected to /account
3. **Action:** View the account page
   **Expect:** Member's name and a list of past orders are shown

## Variants
- **V1 – wrong password** (from step 2): **Action:** Enter a wrong password **Expect:** "Email or password is incorrect" error; user stays on /login
- **V2 – account locked** (from step 2): **Action:** Sign in to an account locked after 5 failed attempts **Expect:** Locked-account message with a reset-password link

## Test data
- Seeded member: member@example.test (password from TEST_MEMBER_PASSWORD)

## Not automatable
- (none)

## Open questions
- (none)

## Evidence
- Step 1: `web/src/app/login/page.tsx:10`
- Step 1: `mobile/app/login.tsx:1`
- Step 2: `web/src/features/auth/useLogin.ts:34` — POST /api/session; router.push('/account'); track('login_succeeded')
- Step 3: `web/src/app/account/page.tsx:18`
- V1: `web/src/features/auth/useLogin.ts:41`
- V2: `web/src/app/api/session/route.ts:29` — 423 when failed_attempts >= 5
