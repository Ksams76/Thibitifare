# ThibitiFare

A matatu fare-verification app. Conductors can either:

1. **Send a payment prompt** — an M-Pesa STK push straight to the passenger's
   phone, with live status tracking until it succeeds or fails.
2. **Verify a payment** — check a transaction code the passenger typed in (or
   showed on an old screenshot) against Safaricom's own record, catching
   fabricated codes, reused codes, and amount mismatches.

Backend: Python (FastAPI) talking to Safaricom's Daraja API.
Frontend: a small installable Progressive Web App (vanilla JS + Vite).

---

## 1. Prerequisites

- **Python 3.10+**
- **Node.js 18+** (for the frontend dev server)
- **ngrok** (or any tunneling tool) — Daraja needs a public HTTPS URL to send
  callbacks to, and `localhost` isn't reachable from Safaricom's servers.
- A **Daraja sandbox app** at https://developer.safaricom.co.ke — see step 3.

---

## 2. Project layout

```
thibitifare/
├── app/                       # FastAPI backend
│   ├── __init__.py
│   ├── config.py              # reads .env, exposes `settings`
│   ├── daraja.py              # all calls to Safaricom's Daraja API
│   ├── main.py                # FastAPI routes
│   └── security_credential.py # RSA-encrypts the initiator password (real /verify only)
├── certs/                     # put Safaricom's public cert here (real /verify only)
├── requirements.txt
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
DARAJA_CALLBACK_URL=             # fill this in AFTER starting ngrok — see step 5
DARAJA_VERIFY_STUB=true          # leave true unless you've done step 6
```

Leave `DARAJA_CALLBACK_URL` blank for now — you'll fill it in once ngrok is
running, in the next step.

---

## 5. Expose your backend with ngrok

Daraja can't send callbacks to `localhost`, so you need a public URL.

```bash
ngrok http 8000
```

Copy the `https://...ngrok-free.dev` "Forwarding" URL it prints, then:

1. Paste it into `.env` as `DARAJA_CALLBACK_URL` (no trailing slash), e.g.:
   ```
   DARAJA_CALLBACK_URL=https://anybody-fall-snippet.ngrok-free.dev
   ```
2. Paste the **same URL** into `script.js` at the top:
   ```js
   const API_BASE = "https://anybody-fall-snippet.ngrok-free.dev";
   ```

> **The free ngrok URL changes every time you restart ngrok.** Whenever that
> happens, update it in both places above and restart the backend.

Now start the backend (from the project root, with the venv active):

```bash
uvicorn app.main:app --reload --reload-dir app
```

You should see `Application startup complete.` Visit
`https://<your-ngrok-url>/` in a browser — you should get back
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
number attached (`/daraja/callback` in `main.py`, feeding the
`known_receipts` store). `/verify` just checks a typed-in code against that
store — instant, reliable, no extra round-trip to Daraja. **This is what you
want for demos and for verifying codes ThibitiFare itself issued.**

### Real mode (`DARAJA_VERIFY_STUB=false`)

For a code ThibitiFare never issued (e.g. a passenger paid the till number
directly, outside the app), there's no local record to check — you need to
ask Safaricom directly via **TransactionStatusQuery**. This API is
**asynchronous**: Daraja's immediate response only confirms the request was
accepted; the real answer (verified / not found / amount) is POSTed later to
`/daraja/transaction-status-result`, sometimes several seconds afterward.

This is fully wired up and automatic:

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
  (sandbox defaults are `testapi` / `Safaricom999!*!` — already filled in
  `.env.example`).
- Safaricom's public certificate saved at `certs/sandbox_public_key.pem`
  (see `certs/README.txt` and step 3.4 above).
- The `cryptography` package (already in `requirements.txt`) to RSA-encrypt
  the initiator password into the required `SecurityCredential`.

If any of that's missing, real-mode `/verify` will fail with a clear error
message (not a silent crash) telling you what's missing.

---

## 9. Common problems

| Symptom | Likely cause | Fix |
|---|---|---|
| Browser console shows a CORS error | Your ngrok URL in `script.js` doesn't match your running backend, or you forgot to restart uvicorn after editing `.env` | Double-check `API_BASE` in `script.js` and `DARAJA_CALLBACK_URL` in `.env` match your **current** ngrok URL |
| `net::ERR_FAILED` on every request, with a CORS-looking message | ngrok's free-tier interstitial warning page | Already handled — `script.js` sends `ngrok-skip-browser-warning: true` on every request |
| "Still waiting" even though you entered your PIN | Daraja's sandbox callback can genuinely take a minute or two | Just wait — polling runs for up to 3 minutes before giving up |
| "Rule limited" as the failure reason | Safaricom's sandbox is rate-limiting you from rapid repeated test requests | Wait a few minutes between test runs |
| Backend crashes with `httpx.ConnectTimeout` | Your machine briefly couldn't reach Safaricom's servers | Already handled — all Daraja calls now catch this and report it as a normal `DarajaError` instead of a 500 |
| `/verify` in real mode never resolves | Missing `certs/sandbox_public_key.pem`, or `DARAJA_INITIATOR_*` unset | See section 8 — check the error message from `/verify`, it will say exactly what's missing |

---

## 10. Next steps (post-hackathon)

- Swap the in-memory `stk_requests` / `used_codes` / `known_receipts` /
  `pending_verifications` dicts in `main.py` for a real database (you already
  have a MySQL schema in progress for this).
- Move `API_BASE` in `script.js` to a build-time environment variable instead
  of a hardcoded string.
- Deploy the backend somewhere with a stable HTTPS URL (Render, Fly.io,
  Railway, etc.) so you're not dependent on ngrok's rotating free URLs.
- Get your own production Shortcode + Passkey once you have a real
  paybill/till number, and set `DARAJA_ENV=production`.
