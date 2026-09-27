import os
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import daraja
from .config import settings

app = FastAPI(title="ThibitiFare API")

# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------
# Set FRONTEND_ORIGINS in .env to a comma-separated list once you have a real
# deployed frontend URL (e.g. your Netlify domain). localhost:5173 (Vite's
# default dev port) is always allowed so `npm run dev` works out of the box.
_extra_origins = [
    origin.strip()
    for origin in os.getenv("FRONTEND_ORIGINS", "").split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", *_extra_origins],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# In-memory stores for the hackathon demo
# ---------------------------------------------------------------------------
# Swap these for a real database (see /README.md "Next steps") before you
# rely on this past a demo — everything here is lost on restart.
stk_requests: dict[str, dict] = {}       # checkout_request_id -> {phone, amount, route, status, ...}
used_codes: set[str] = set()             # transaction codes already accepted, either flow
known_receipts: dict[str, dict] = {}     # mpesa_receipt -> {amount, phone, route} — from real STK callbacks
pending_verifications: dict[str, dict] = {}  # transaction_id -> {status, ...} — real TransactionStatusQuery flow


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------
class StkPushRequest(BaseModel):
    phone_number: str = Field(..., pattern=r"^0\d{9}$", description="10 digits, starting with 0")
    amount: int = Field(..., gt=0)
    route: str = Field(..., min_length=1, max_length=40)


class VerifyRequest(BaseModel):
    transaction_code: str = Field(..., min_length=6, max_length=15)
    amount: int = Field(..., gt=0)


def _to_daraja_phone(local_number: str) -> str:
    """Converts the conductor-friendly '0712345678' into Daraja's required '254712345678'."""
    return "254" + local_number[1:]


# ---------------------------------------------------------------------------
# Flow 1: conductor sends an STK push prompt straight to the passenger's phone
# ---------------------------------------------------------------------------
@app.post("/stk-push")
async def trigger_stk_push(req: StkPushRequest):
    try:
        result = await daraja.stk_push(
            phone_number=_to_daraja_phone(req.phone_number),
            amount=req.amount,
            account_ref=req.route,
            description="Fare payment",
        )
    except daraja.DarajaError as e:
        raise HTTPException(status_code=502, detail=str(e))

    checkout_id = result.get("CheckoutRequestID")
    if not checkout_id:
        raise HTTPException(status_code=502, detail=f"Unexpected Daraja response: {result}")

    stk_requests[checkout_id] = {
        "phone_number": req.phone_number,
        "amount": req.amount,
        "route": req.route,
        "status": "pending",
        "created_at": datetime.utcnow().isoformat(),
    }
    return {"checkout_request_id": checkout_id, "status": "pending"}


@app.get("/stk-push/{checkout_request_id}/status")
async def get_stk_push_status(checkout_request_id: str):
    local = stk_requests.get(checkout_request_id)
    if not local:
        raise HTTPException(status_code=404, detail="Unknown checkout_request_id")

    # If the callback already updated us, trust that — it's faster and free.
    if local["status"] != "pending":
        return local

    # Otherwise actively poll Daraja (useful if the callback hasn't arrived yet).
    try:
        result = await daraja.query_stk_status(checkout_request_id)
    except daraja.DarajaError as e:
        raise HTTPException(status_code=502, detail=str(e))

    result_code = result.get("ResultCode")
    result_desc = result.get("ResultDesc", "") or ""

    if result_code == "0" or result_code == 0:
        local["status"] = "success"
    elif result_code is not None:
        if "processing" in result_desc.lower() or "being processed" in result_desc.lower():
            pass  # Safaricom hasn't resolved it yet — keep status as "pending"
        else:
            local["status"] = "failed"
            local["reason"] = result_desc

    return local


@app.post("/daraja/callback")
async def daraja_callback(request: Request):
    """Daraja POSTs the outcome here once the commuter enters their PIN (or cancels)."""
    body = await request.json()
    stk_callback = body.get("Body", {}).get("stkCallback", {})
    checkout_id = stk_callback.get("CheckoutRequestID")
    result_code = stk_callback.get("ResultCode")

    local = stk_requests.get(checkout_id)
    if not local:
        # Nothing we're tracking — acknowledge anyway so Daraja doesn't retry.
        return {"ResultCode": 0, "ResultDesc": "Accepted"}

    if result_code == 0:
        items = {
            item["Name"]: item.get("Value")
            for item in stk_callback.get("CallbackMetadata", {}).get("Item", [])
        }
        receipt = items.get("MpesaReceiptNumber")
        local["status"] = "success"
        local["receipt"] = receipt
        if receipt:
            known_receipts[str(receipt).upper()] = {
                "amount": int(items.get("Amount", local["amount"])),
                "phone_number": local["phone_number"],
                "route": local["route"],
            }
    else:
        local["status"] = "failed"
        local["reason"] = stk_callback.get("ResultDesc", "Payment was not completed.")

    return {"ResultCode": 0, "ResultDesc": "Accepted"}


# ---------------------------------------------------------------------------
# Flow 2: conductor verifies a code the commuter typed in (or showed on an
# old screenshot) against Safaricom's own record.
# ---------------------------------------------------------------------------
@app.post("/verify")
async def verify_transaction(req: VerifyRequest):
    code = req.transaction_code.strip().upper()

    if code in used_codes:
        return {"result": "already_used"}

    if settings.VERIFY_STUB_MODE:
        return _verify_against_known_receipts(code, req.amount)

    # --- real path: Safaricom TransactionStatusQuery (asynchronous) ---
    try:
        await daraja.verify_c2b_transaction(code)
    except daraja.DarajaError as e:
        raise HTTPException(status_code=502, detail=str(e))

    # Safaricom accepted the query; the real outcome lands later at
    # /daraja/transaction-status-result. Track it so the frontend can poll.
    pending_verifications[code] = {
        "status": "pending",
        "expected_amount": req.amount,
        "created_at": datetime.utcnow().isoformat(),
    }
    return {"result": "pending", "transaction_id": code}


@app.get("/verify/{transaction_id}/status")
async def get_verify_status(transaction_id: str):
    """Poll this once /verify returns {"result": "pending"} in real (non-stub) mode."""
    code = transaction_id.strip().upper()
    pending = pending_verifications.get(code)
    if not pending:
        raise HTTPException(status_code=404, detail="Unknown or already-resolved transaction_id")
    return pending


def _verify_against_known_receipts(code: str, amount: int) -> dict:
    """
    Demo-safe verification: check a code against receipts ThibitiFare has
    actually seen from real STK Push callbacks, instead of round-tripping to
    Daraja's asynchronous TransactionStatusQuery live in front of judges.
    """
    receipt = known_receipts.get(code)
    if not receipt:
        return {"result": "not_found"}

    if receipt["amount"] != amount:
        return {"result": "amount_mismatch", "expected": receipt["amount"]}

    used_codes.add(code)
    return {"result": "verified", "phone_number": receipt["phone_number"], "route": receipt["route"]}


@app.post("/daraja/transaction-status-result")
async def transaction_status_result(request: Request):
    """
    Daraja posts the real TransactionStatusQuery result here, asynchronously,
    some time after /verify accepted the query. This is what makes real
    (non-stub) verification "automatic": the frontend polls
    /verify/{id}/status and this endpoint is what fills that in.
    """
    body = await request.json()
    result = body.get("Result", {})
    transaction_id = result.get("TransactionID")
    result_code = result.get("ResultCode")

    if not transaction_id:
        return {"ResultCode": 0, "ResultDesc": "Accepted"}

    code = str(transaction_id).strip().upper()
    pending = pending_verifications.get(code)
    if pending is None:
        # We weren't waiting on this one (e.g. server restarted mid-flight) —
        # acknowledge and move on.
        return {"ResultCode": 0, "ResultDesc": "Accepted"}

    if result_code != 0:
        pending["status"] = "failed"
        pending["reason"] = result.get("ResultDesc", "Transaction not found.")
        return {"ResultCode": 0, "ResultDesc": "Accepted"}

    params = {
        item.get("Key"): item.get("Value")
        for item in result.get("ResultParameters", {}).get("ResultParameter", [])
    }

    actual_amount = params.get("Amount")
    expected_amount = pending.get("expected_amount")

    if actual_amount is not None and expected_amount is not None and int(actual_amount) != int(expected_amount):
        pending["status"] = "amount_mismatch"
        pending["expected"] = expected_amount
        pending["actual"] = actual_amount
        return {"ResultCode": 0, "ResultDesc": "Accepted"}

    pending["status"] = "verified"
    pending["phone_number"] = params.get("DebitPartyName")
    used_codes.add(code)
    return {"ResultCode": 0, "ResultDesc": "Accepted"}


@app.post("/daraja/timeout")
async def daraja_timeout(request: Request):
    """Daraja posts here if a TransactionStatusQuery times out in its own queue."""
    return {"ResultCode": 0, "ResultDesc": "Accepted"}


@app.get("/")
async def health_check():
    return {"status": "ok", "service": "ThibitiFare API", "verify_mode": "stub" if settings.VERIFY_STUB_MODE else "live"}
