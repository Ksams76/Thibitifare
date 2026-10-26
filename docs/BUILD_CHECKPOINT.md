# ThibitiFare Build Checkpoint

Updated: 2026-10-26

## Phase 0 — Baseline audit

### Architecture and entry points

- Backend: Python 3.11 Docker image, FastAPI, Pydantic 2, and `httpx`.
- Frontend: vanilla JavaScript and CSS served/built with Vite; the service worker
  caches the static app shell and does not cache API responses.
- Backend entry point: `app/main.py` (`uvicorn app.main:app`).
- Frontend entry point: `index.html` and `script.js`.
- Daraja client and security credential helper: `app/daraja.py` and
  `app/security_credential.py`.
- Deployment configuration: `Dockerfile`, `render.yaml`, and `netlify.toml`.
- `.env.example` documents the required settings with placeholders only.
- No database, migrations, authentication routes, or backend test suite existed
  at the start of this work.

### Existing functionality

- `POST /stk-push` submits an STK Push and records the checkout request in
  process memory.
- `POST /daraja/callback` updates a known STK request from its callback.
- `GET /stk-push/{checkout_request_id}/status` checks local state, then queries
  Daraja while the request is pending.
- `POST /verify` checks receipts seen by this app when stub mode is enabled, or
  starts a Daraja TransactionStatusQuery when disabled.
- `POST /daraja/transaction-status-result` and `GET
  /verify/{transaction_id}/status` implement the asynchronous verification
  callback/poll flow.
- The conductor frontend collects a Kenyan phone number, amount, and route;
  passenger authorization is expected to happen privately on the passenger's
  phone.
- The UI is a single-column mobile form with inline pending/success/error
  messages and a dark-mode stylesheet. It has no transaction history, receipt
  view, conductor account/session, operator summary, or explicit offline mode;
  route is free text and must be re-entered for each request.
- CORS restricts browser origins, but it does not authenticate callers. Daraja
  application credentials are loaded from environment variables; callback
  routes have no application-level authentication.

### Baseline risks and gaps

- All payment/verification data and used-code tracking are in memory; restarts
  lose them and multiple instances do not share state.
- There are no user accounts or server-side permissions. Payment initiation,
  verification, and transaction status are publicly callable.
- No automated tests were present.
- The deployment and live Daraja behavior were not reachable for verification.
- `DARAJA_VERIFY_STUB=true` only recognizes receipts produced by known STK
  callbacks; it is not an account-wide provider lookup.
- The repository has a `.env` file, but its contents were not inspected. The
  app's environment loader warned that it could not parse its first line, and
  required Daraja settings were not available to the initial test process.

### Baseline checks

- Python syntax: `python -m compileall -q app` — passed.
- Frontend production build: `npm run build` — passed.
- No project tests existed at baseline. `pytest` was not installed; tests added
  in this phase use Python's built-in `unittest`.

## Phase 1 — Payment workflow stabilization

### Changes completed

- Validate Kenyan mobile numbers (`01`/`07` prefixes), positive integer fare
  amounts, nonblank routes, and alphanumeric transaction codes on the server;
  align the phone-number form hint and pattern with backend validation.
- Add a placeholder-only `.env.example` and correct the README's claim that the
  required callback URL can be left blank.
- Remove the hardcoded TransactionStatusQuery initiator password; real
  verification now requires provider-issued credentials from environment
  variables.
- Parse Daraja responses safely, reject invalid or unsuccessful response shapes,
  handle short/malformed token expiry values, and avoid surfacing raw network
  exception details.
- Use an explicit Kenya UTC+3 timestamp for the STK password.
- Treat STK submission timeouts as an unknown outcome and tell the conductor to
  check the passenger's phone before retrying.
- Add an idempotency key to conductor STK requests; same-key retries reuse the
  original checkout or preserve an unknown outcome instead of sending a second
  prompt. A key cannot be reused with different payment details.
- Limit STK status-query requests to one per checkout every five seconds, while
  retaining the frontend's three-minute polling window.
- Confirm the provider's checkout reference matches the request before using a
  status-query result.
- Accept STK success callbacks only when the receipt and amount are present,
  the amount matches the submitted fare, and any returned phone matches the
  submitted phone. Prevent duplicate receipts and ignore repeated terminal
  callbacks.
- Keep asynchronous verification idempotent for pending codes, require a
  verifiable amount before success, and avoid exposing phone numbers in
  verification responses, the payment status endpoint, or the payment status
  message.
- Add mocked backend workflow tests. Tests do not contact Safaricom or initiate
  real payments.

### Commands

From the project root:

```powershell
.\venv\Scripts\python.exe -m unittest discover -s tests -v
.\venv\Scripts\python.exe -m compileall -q app tests
npm run build
```

Run the backend with `uvicorn app.main:app --reload --reload-dir app` and the
frontend with `npm run dev` in separate terminals. Daraja credentials and a
public HTTPS callback URL are required for manual sandbox integration.

Verification result: 17 mocked `unittest` cases passed, Python compilation
passed, and `npm run build` passed. The test run reports a pre-existing
`python-dotenv` warning that it cannot parse the first line of `.env`, and a
Starlette deprecation warning about the installed `httpx` test client. Test
settings use dummy credentials and no external payment requests are made.

### Phase 1 audit before Phase 2

The checkpoint was compared with the checked-out implementation before Phase
2 work. The 17 Phase 1 payment-workflow tests passed on that recheck. The
Daraja client was confirmed to send Bearer authorization for API requests.
Payment and idempotency state were still process-local at that point, so the
checkpoint's statement that data was not durable was accurate. Provider
behavior, callback authenticity, and live Daraja integration were not verified.

### Provider verification and remaining risks

- The Safaricom developer documentation URL returned 404 in this environment,
  so no claim is made that the configured Daraja callback/result parameter
  shapes or account permissions have been live-verified.
- Automated tests mock the provider; a sandbox-only integration check remains
  necessary before trusting actual callbacks. Do not use production credentials
  or real-money payments for automated tests.
- Callback authenticity must be checked against Safaricom's supported
  mechanisms before live use. The current public callback endpoints have no
  application-level authentication.
- Authentication/authorization and audit logging remain unimplemented. Do
  not expose this service as a production payment system.
- The `.env` parse warning remains unresolved; do not print, commit, or copy its
  contents while diagnosing it.

## Phase 2 — Persistent transaction storage and safe updates

### Changes completed

- Add a SQLite transaction repository with tracked schema migrations. Migration
  1 creates persistent idempotency, transaction, and used-code tables;
  migration 2 adds currency and query indexes. Migration execution is
  serialized and rechecks the current schema version under a write lock. WAL
  activation retries transient database-lock errors, so concurrent startup
  does not apply a migration twice or fail during journal-mode setup.
- Persist STK submissions, provider-verification requests/results, receipt
  claims, status-query throttles, and idempotency reservations. Atomic
  transactions and unique constraints prevent duplicate receipt use and
  conflicting state changes.
- Encrypt passenger phone numbers at rest with AES-GCM using a key derived from
  the required 32-byte URL-safe Base64 encryption key. Remove stored phone
  ciphertext after a terminal callback. HMAC-derived request
  fingerprints prevent storing raw phone numbers in idempotency records.
- Require an `Idempotency-Key` UUID on `POST /stk-push`; persistent same-key
  retries reuse the checkout and mismatched request details are rejected.
  Stale submissions become `unknown` rather than risking a duplicate prompt.
- Initialize the store on application startup and run synchronous SQLite work
  off the async event loop. Document the database path and key requirements,
  ignore local SQLite data files, and configure a 1 GB Render persistent disk
  at `/var/data`.

No pre-existing database or transaction rows were present to migrate. The
legacy migration test constructs a representative version-1 schema and
confirms that its transaction row is preserved while version 2 is applied.

### Verification

The final Phase 2 verification run reports 33 mocked `unittest` cases passing
(19 payment-flow/API tests and 14 storage tests), Python compilation passing,
`git diff --check` passing, and `npm run build` passing. The concurrent
initialization test also passed 10 consecutive stress runs. The tests include
restart persistence, encryption and ciphertext erasure, migration preservation
and concurrent initialization, idempotency, atomic receipt claims, and
concurrent verification. Daraja calls are mocked; no real payment requests
were sent. After the `.env` update, the dotenv parse warning no longer appears;
the Starlette/httpx test-client deprecation warning remains.

### Operational limits and remaining risks

- Persistence requires the configured database path to be on a persistent
  volume. Render is configured for `/var/data`; Railway and other deployments
  must attach their own persistent volume and set the path appropriately.
- SQLite is intended for one host with a mounted persistent volume, not
  horizontally scaled/multi-host deployments. Back up the database and
  encryption key separately. Losing or changing the key without re-encrypting
  stored data makes any remaining encrypted phone records unreadable.
- No existing production database was available, and deployment persistence,
  provider callbacks, callback authenticity, and live Daraja behavior have not
  been verified.
- Authentication/authorization and audit logging were still outstanding at
  the end of Phase 2; Phase 3 below addresses the application/API layer.

## Phase 3 — Firebase authentication, authorization, and audit logging

### Changes completed

- Add Firebase Email/Password sign-in and password reset to the PWA, with no
  public in-app account creation. Public Firebase Web configuration is served
  by `GET /auth/config`; service-account credentials remain server-side.
- Verify Firebase ID tokens on the backend with the Firebase Admin SDK,
  including revocation and disabled-account checks. Require trusted `admin` or
  `conductor` custom role claims for payment initiation, verification, and
  their status endpoints. Missing/invalid tokens fail with 401, unassigned or
  insufficient roles with 403, and Firebase verification outages with 503.
- Add an admin-only PWA panel to assign roles to existing Firebase users.
  Role changes preserve other claims and revoke refresh tokens; administrators
  can also remove the application role to revoke ThibitiFare access. Add a
  local bootstrap command for the first administrator.
- Add SQLite migration 3 for audit events. Record authenticated payment,
  verification, role-management, and audit-read requests plus Daraja callback
  endpoint outcomes. Events retain actor/role, action, HTTP status, time, and
  a role-change subject UID; request bodies, passenger phone numbers, and
  transaction codes are intentionally excluded. Administrators can inspect a
  bounded, paginated recent-event list in the PWA/API.
- Keep Daraja callback routes public for provider delivery; the audit identifies
  them as unverified callbacks and does not claim callback authenticity.
- Replace the full Firebase umbrella package with only the Firebase App and
  Authentication modules to avoid adding unused Firestore dependencies. Bump
  the PWA service-worker cache version so existing installs receive the sign-in
  app shell.
- Document Firebase project configuration, service-account handling, account
  provisioning, role behavior, and remaining deployment requirements.

### Verification

Final Phase 3 checks: 45 mocked backend tests passed, Python compilation passed,
`npm run build` passed, `git diff --check` passed, and
`npm audit --omit=dev --offline` reported zero production dependency
vulnerabilities. Tests cover missing/invalid tokens, role enforcement,
revocation checks, admin role assignment/removal and auditing, public callback
handling, and omission of sensitive transaction input from the audit schema.
No live Firebase account or Daraja transaction was used.

The full npm audit report still identifies existing development-tool findings
in the repository's Vite 5/esbuild/source-map-js toolchain. The production-only
audit passed from the local cache; a live npm registry audit was unavailable
because the registry could not be resolved during that check.

### Configuration and remaining limits

- Firebase project web settings and a server-side Firebase Admin credential
  must be configured in the deployment environment before protected API access
  works. These provider credentials were not supplied or tested in this session.
- The first administrator must be created in Firebase and assigned the
  `admin` claim using the documented bootstrap command. Subsequent role
  management is available only to an authenticated administrator.
- No in-app user account creation is provided. Accounts are created in Firebase
  Authentication, then assigned a role. Firebase identities without one of the
  trusted roles remain blocked from protected API routes.
- Daraja callbacks remain unauthenticated because provider calls do not use
  conductor credentials. Callback authenticity, sandbox delivery, and
  production integration remain unverified.
- Persistent storage and audit events still depend on the configured durable
  SQLite volume. This work does not replace provider callback security,
  deployment verification, or an operational backup/retention plan.

### Live deployment check — 2026-10-26

- Commit `c3cd542` is present on `origin/main`; the local working tree was clean
  after publishing.
- The public Railway health endpoint returned HTTP 200 and reported
  `verify_mode: stub`.
- The public Netlify page served the Phase 3 sign-in screen, but the sign-in
  configuration remained in its loading state and then displayed an
  initialization failure.
- `GET /auth/config` on the public Railway backend returned HTTP 404. The
  deployed backend therefore does not yet expose the Phase 3 API, so
  authentication, role authorization, and audit features are **not verified
  operationally in the live deployment**.
- This does not invalidate the local verification above. The live rollout
  remains blocked until Railway deploys the pushed commit and its Firebase
  environment variables/service credential are configured privately. Do not
  mark Phase 3 complete for production until the config endpoint, sign-in,
  role enforcement, role updates, and audit view have been verified live.

## Next verification

Deploy commit `c3cd542` to Railway and configure the Firebase project settings
and server credential in Railway's private environment. Confirm that
`GET /auth/config` returns HTTP 200 with `enabled: true` (without exposing any
server credential), bootstrap the first administrator, and verify sign-in,
role enforcement, role assignment/removal, and audit visibility against the
real Firebase project. Continue to keep Daraja callback authentication and
sandbox verification as explicit deployment prerequisites.
