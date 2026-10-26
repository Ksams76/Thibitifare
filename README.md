# ThibitiFare

A matatu fare-verification app. Conductors can either:

1. **Send a payment prompt** — an M-Pesa STK push straight to the passenger's
   phone, with live status tracking until it succeeds or fails.
2. **Verify a payment** — match a transaction code against a receipt from a
   successful ThibitiFare STK callback, or query Safaricom directly when real
   TransactionStatusQuery mode is configured.

Backend: Python (FastAPI) talking to Safaricom's Daraja API.
Frontend: a small installable Progressive Web App (vanilla JS + Vite).

---

## 1. Prerequisites

- **Python 3.10+**
- **Node.js 18+** (for the frontend dev server)
- A **Daraja sandbox app** at https://developer.safaricom.co.ke — see step 3.

---

## 2. Project layout

```
thibitifare/
├── app/                       # FastAPI backend
│   ├── __init__.py
│   ├── auth.py                # Firebase ID token and role checks
│   ├── config.py              # reads .env, exposes `settings`
│   ├── daraja.py              # all calls to Safaricom's Daraja API
│   ├── main.py                # FastAPI routes
│   ├── storage.py             # SQLite transaction repository and migrations
│   └── security_credential.py # RSA-encrypts the initiator password (real /verify only)
├── certs/                     # put Safaricom's public cert here (real /verify only)
├── requirements.txt
├── scripts/assign_firebase_role.py # trusted admin/conductor role provisioning
├── .env.example                # copy to .env and fill in
├── package.json               # frontend (Vite) config
├── index.html
├── script.js
├── style.css
├── Manifest.json              # PWA manifest
└── Service-Worker.js          # PWA service worker
```

---

## 3. Get your Daraja credentials

1. Create an account and log in at https://developer.safaricom.co.ke
2. Create a new app. Note its **Consumer Key** and **Consumer Secret** (My Apps
   → your app → Keys).
3. Go to **APIs → M-Pesa Express (STK Push)** and open **Test Credentials** to
   see the sandbox **Shortcode** (`174379`) and **Passkey**. Copy the passkey.
4. (Only if you plan to use *real* `/verify` — see section 6) download the
   sandbox public certificate from the Daraja docs and save it at
   `certs/sandbox_public_key.pem`.

---

## 4. Backend setup

From the project root:

```bash
cd thibitifare
python -m venv venv

# Windows (PowerShell)
venv\Scripts\Activate.ps1
# macOS / Linux
source venv/bin/activate

pip install -r requirements.txt

cp .env.example .env     # Windows: copy .env.example .env
```

Open `.env` and fill in:

```
DARAJA_CONSUMER_KEY=<from step 3.2>
DARAJA_CONSUMER_SECRET=<from step 3.2>
DARAJA_SHORTCODE=174379          # sandbox default, leave as-is
DARAJA_PASSKEY=<from step 3.3>
DARAJA_ENV=sandbox
DARAJA_CALLBACK_URL=https://your-public-host.example
DARAJA_VERIFY_STUB=true
THIBITIFARE_DATABASE_PATH=data/thibitifare.sqlite3
THIBITIFARE_PHONE_ENCRYPTION_KEY=<URL-safe base64 encoding of 32 random bytes>
FIREBASE_PROJECT_ID=<your Firebase project ID>
FIREBASE_API_KEY=<your Firebase web API key>
FIREBASE_AUTH_DOMAIN=<your-project-id>.firebaseapp.com
FIREBASE_APP_ID=<your Firebase web app ID>
```

Set `DARAJA_CALLBACK_URL` to your Railway public URL (no trailing slash). The
setting is required for the backend to start, and STK Push callbacks require a
publicly reachable HTTPS URL; an empty value is not supported.

Generate a unique phone-encryption key for each environment and keep it backed
up separately from the database. For example, with Python:

```powershell
python -c "import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
```

The key is required to start the backend because passenger phone numbers are
encrypted in SQLite. Losing or rotating it without re-encrypting stored rows
will make those phone numbers unreadable. The default database path is relative
to the backend working directory; use a persistent volume path in deployed
environments. The Render blueprint mounts `/var/data`; for Railway, attach a
volume and set `THIBITIFARE_DATABASE_PATH` to a file under its mount path.
`POST /stk-push` also requires an `Idempotency-Key` UUID header; retries with
the same key and details reuse the stored checkout rather than sending a second
payment prompt.

### Configure conductor sign-in

1. Create a Firebase project, register a Web app, and enable **Email/Password**
   under Authentication sign-in providers. Add the Netlify app domain to
   Firebase Authentication's authorized domains.
2. Set the `FIREBASE_*` web-app values shown above in the backend environment.
   The web API key, auth domain, project ID, and app ID are public Firebase
   client configuration served by `GET /auth/config`. Never put a
   service-account key in the web app or source control.
3. Give the backend a Firebase Admin service account with
   `GOOGLE_APPLICATION_CREDENTIALS` pointing to a protected JSON file, or set
   `FIREBASE_SERVICE_ACCOUNT_JSON` as a server-side secret in the deployment
   platform. Grant only the Firebase Authentication permissions needed to
   verify/revoke tokens and manage user claims. This credential is required
   for token revocation checks and role management. Do not paste it into chat,
   commit it, or include it in frontend build variables.
4. Create the first Firebase user in the Firebase console. Assign the
   administrator role locally from the project root:

   ```powershell
   .\venv\Scripts\python.exe scripts\assign_firebase_role.py --email admin@example.com --role admin
   ```

5. Create conductor accounts in Firebase Authentication. An administrator can
   find the new user's UID in Firebase console, sign in to the PWA, and assign
   the `conductor` role from **Administrator tools**. The protected API revokes
   the user's existing refresh tokens. The local provisioning command is for
   the initial administrator/bootstrap path.

The PWA provides sign-in and password-reset only; it does not provide public
account creation. Firebase accounts without an assigned role cannot access
payment routes. Role changes require the affected user to sign in again.

Firebase tokens use trusted `role` custom claims; an account without an `admin`
or `conductor` role receives 403 from protected routes. The backend rechecks
token revocation. Both roles may submit payments and verify receipts; only
administrators may read the audit log. Daraja callback endpoints remain
unauthenticated because Safaricom calls them directly; callback authenticity
still needs a provider-supported verification mechanism. Administrators may
remove an account's application role to revoke ThibitiFare access without
deleting the Firebase account.

---

## 5. Deployment

ThibitiFare runs in production on:

- **Backend (FastAPI):** Railway, built from the `Dockerfile`
- **Frontend (PWA):** Netlify
- **Live app:** https://genuine-longma-e2b1ad.netlify.app/

Set `DARAJA_CALLBACK_URL` in the Railway service's **Variables** tab to your Railway
public URL (no trailing slash). In `script.js`, `API_BASE` also points to the Railway
URL. No tunneling tool is needed.

### Run the backend locally (optional)

From the project root, with the venv active:
```bash
uvicorn app.main:app --reload --reload-dir app
```

You should see `Application startup complete.` Visit
`https://localhost:8000/` in a browser — you should get back
`{"status": "ok", ...}`.

---

## 6. Frontend setup

In a **second terminal**, from the project root:

```bash
npm install
npm run dev
```

Vite will print a local URL, normally `http://localhost:5173/`. Open it —
you should see the ThibitiFare form.

---

## 7. Try it end-to-end

1. Fill in a real Safaricom phone number (sandbox STK pushes only reach real
   phones — there's no fake "test" number), a fare amount, and a route.
2. Click **Send prompt**. Within a few seconds you should get a real M-Pesa
   PIN prompt on that phone.
3. Enter the PIN (or cancel, to test the failure path). The page polls
   automatically and shows **"Payment done ✓"** once Safaricom confirms it.
   This can take anywhere from a few seconds up to ~1–2 minutes on Daraja's
   sandbox, so the app is deliberately patient (polls for up to 3 minutes
   before giving up).
4. Try **Verify a payment** with the M-Pesa receipt code from the payment you
   just made, and the same fare amount — it should come back **"Verified"**.
   Try it again with the same code — it should say **"already used"**. Try a
   made-up code — **"not found"**. Try the real code with a different
   amount — **"amount doesn't match"**.

---

## 8. Verify: real vs. stub (this is the "automatic verification" part)

There are two ways `/verify` can check a code, controlled by
`DARAJA_VERIFY_STUB` in `.env`:

### Stub mode (`DARAJA_VERIFY_STUB=true` — the default)

Every STK push ThibitiFare sends already gets a real callback from Safaricom
confirming success, with the real M-Pesa receipt number, amount, and phone
number attached (`/daraja/callback` in `main.py`, persisted in SQLite).
`/verify` just checks a typed-in code against those transaction records —
instant, reliable, no extra round-trip to Daraja. **This is what you want for
demos and for verifying codes ThibitiFare itself issued.**

### Real mode (`DARAJA_VERIFY_STUB=false`)

For a code ThibitiFare never issued (e.g. a passenger paid the till number
directly, outside the app), there's no local record to check — you need to
ask Safaricom directly via **TransactionStatusQuery**. This API is
**asynchronous**: Daraja's immediate response only confirms the request was
accepted; the real answer (verified / not found / amount) is POSTed later to
`/daraja/transaction-status-result`, sometimes several seconds afterward.

The asynchronous code path is implemented, but has not been verified against an
active Daraja account in this environment. Provider permissions, callback delivery,
and the account-specific response shape still need a sandbox integration check:

1. `POST /verify` kicks off the query and immediately returns
   `{"result": "pending", "transaction_id": "<code>"}`.
2. The frontend (`script.js`) automatically starts polling
   `GET /verify/{transaction_id}/status` every 3 seconds.
3. When Safaricom's callback lands at `/daraja/transaction-status-result`,
   the backend updates the pending record.
4. The next poll picks up the final result (`verified` / `amount_mismatch` /
   `failed`) and the UI updates — no page reload, no manual re-check needed.

**Real mode also needs:**
- `DARAJA_INITIATOR_NAME` / `DARAJA_INITIATOR_PASSWORD` set in `.env`
  using values issued for your Daraja account; the application does not supply
  a default initiator password.
- Safaricom's public certificate saved at `certs/sandbox_public_key.pem`
  (see `certs/README.txt` and step 3.4 above).
- The `cryptography` package (already in `requirements.txt`) to RSA-encrypt
  the initiator password into the required `SecurityCredential`.

If any of that's missing, real-mode `/verify` will fail with a clear error
message (not a silent crash) telling you what's missing.

### Automated checks

Run the local checks from the project root:

```bash
python -m unittest discover -s tests -v
npm run build
```

The backend tests mock Daraja and do not send payment requests. Do not point
automated checks at production credentials or trigger live-money transactions.

### Current operational limitations

- Transaction requests, used receipt codes, and verification callbacks are
  stored in SQLite and survive process restarts when the configured database
  path is on persistent storage. SQLite on ephemeral storage is not durable
  across deployment replacement. Back up the database and its encryption key.
- STK idempotency keys and receipt claims are persisted and use database
  transactions for atomic updates. Every STK API caller must send an
  `Idempotency-Key`; the PWA already does.
- Firebase sign-in is required for conductor payment, verification, and status
  endpoints. Callback authenticity and provider-specific callback security
  still need a separate live integration review.
- Authenticated API activity and Daraja callback outcomes are stored in the
  SQLite audit log. Administrators can read it using `GET /admin/audit-events`;
  entries intentionally omit phone numbers and transaction codes.
- A network timeout while submitting STK Push has an unknown outcome. Check the
  passenger's phone before submitting again to avoid creating another prompt.
- The production Daraja flow, provider callback delivery, and callback security
  have not been certified in this environment.

---

## 9. Common problems

| Symptom | Likely cause | Fix |
|---|---|---|
| Browser console shows a CORS error | `API_BASE` in `script.js` doesn't match your Railway backend URL, the backend's CORS settings don't allow your Netlify domain, or the Railway service needs a redeploy after changing variables | Double-check `API_BASE` in `script.js` and `DARAJA_CALLBACK_URL` in Railway's Variables tab, make sure both use your **current** Railway URL, and allow `https://genuine-longma-e2b1ad.netlify.app` in the backend's CORS origins |

| "Still waiting" even though you entered your PIN | Daraja's sandbox callback can genuinely take a minute or two | Just wait — polling runs for up to 3 minutes before giving up |
| "Rule limited" as the failure reason | Safaricom's sandbox is rate-limiting you from rapid repeated test requests | Wait a few minutes between test runs |
| Backend crashes with `httpx.ConnectTimeout` | Your machine briefly couldn't reach Safaricom's servers | Already handled — all Daraja calls now catch this and report it as a normal `DarajaError` instead of a 500 |
| `/verify` in real mode never resolves | Missing `certs/sandbox_public_key.pem`, or `DARAJA_INITIATOR_*` unset | See section 8 — check the error message from `/verify`, it will say exactly what's missing |

---

## 10. Next steps (post-hackathon)

- Plan a move to managed relational storage if concurrency or operational scale
  outgrows SQLite. The current SQLite database is intended for a single host
  with a mounted persistent volume.
- Move `API_BASE` in `script.js` to a build-time environment variable instead
  of a hardcoded string.
- Deploy the backend somewhere with a stable HTTPS URL (Render, Fly.io,
  Railway, etc.)
- Get your own production Shortcode + Passkey once you have a real
  paybill/till number, and set `DARAJA_ENV=production`.
