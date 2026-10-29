import hmac
import logging
import os
import re
from contextlib import asynccontextmanager
from typing import Annotated, AsyncGenerator, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from . import daraja
from .auth import (
    AuthenticatedUser,
    FirebaseAuthUnavailable,
    FirebaseUserNotFound,
    require_conductor,
    set_firebase_user_role,
)
from .config import settings
from .storage import StorageConflict, StorageError, StorageForbidden, TransactionStore

store = TransactionStore(settings.DATABASE_PATH, settings.PHONE_ENCRYPTION_KEY)
STK_STATUS_QUERY_MIN_INTERVAL = 5.0
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    store.initialize()
    yield


app = FastAPI(title="ThibitiFare API", lifespan=lifespan)


async def require_callback_token(request: Request) -> None:
    """
    Rejects provider callbacks that lack the configured secret. When no secret is
    configured the callbacks are unauthenticated (development only).
    """
    expected = settings.CALLBACK_AUTH_TOKEN
    if not expected:
        return
    supplied = request.query_params.get("token", "")
    if not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=403, detail="Forbidden.")


class _RedactCallbackToken(logging.Filter):
    """Keeps the callback secret out of access logs, which include query strings."""

    _PATTERN = re.compile(r"([?&]token=)[^&\s\"']*")

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                self._PATTERN.sub(r"\1[redacted]", a) if isinstance(a, str) else a
                for a in record.args
            )
        if isinstance(record.msg, str):
            record.msg = self._PATTERN.sub(r"\1[redacted]", record.msg)
        return True


for _name in ("uvicorn.access", "uvicorn.error", "httpx", "httpcore"):
    logging.getLogger(_name).addFilter(_RedactCallbackToken())


def _audit_action(method: str, path: str) -> str | None:
    if method == "POST" and path == "/stk-push":
        return "stk.create"
    if method == "GET" and path.startswith("/stk-push/") and path.endswith("/status"):
        return "stk.status"
    if method == "POST" and path == "/verify":
        return "payment.verify"
    if method == "GET" and path.startswith("/verify/") and path.endswith("/status"):
        return "verification.status"
    if method == "POST" and path == "/daraja/callback":
        return "daraja.stk_callback"
    if method == "POST" and path == "/daraja/transaction-status-result":
        return "daraja.verification_callback"
    if method == "POST" and path == "/daraja/timeout":
        return "daraja.timeout_callback"
    if method == "GET" and path == "/admin/audit-events":
        return "audit.read"
    if method == "GET" and path == "/admin/stk-attempts/unknown":
        return "stk.attempt.list"
    if (
        method == "POST"
        and path.startswith("/admin/stk-attempts/")
        and path.endswith("/review")
    ):
        return "stk.attempt.review"
    if method == "POST" and path.startswith("/admin/users/") and path.endswith("/role"):
        return "identity.role.assign"
    return None


@app.middleware("http")
async def audit_authenticated_requests(request: Request, call_next):
    response = await call_next(request)
    user = getattr(request.state, "authenticated_user", None)
    action = _audit_action(request.method, request.url.path)
    provider_callback = action is not None and action.startswith("daraja.")
    if action is not None and (user is not None or provider_callback):
        try:
            await run_in_threadpool(
                store.record_audit_event,
                user.uid if user is not None else "system:daraja-callback",
                user.role if user is not None else "unverified-callback",
                request.method,
                action,
                response.status_code,
                getattr(request.state, "audit_subject_uid", None),
            )
        except StorageError:
            logger.exception("Could not persist audit event.")
            return JSONResponse(
                status_code=503,
                content={"detail": "The request could not be safely audited."},
            )
    return response


# ---------------------------------------------------------------------------
# CORS
# ---------------------------------------------------------------------------
# Set FRONTEND_ORIGINS in .env to a comma-separated list once you have a real
# deployed frontend URL (e.g. your Netlify domain). localhost:5173 (Vite's
# default dev port) is always allowed so `npm run dev` works out of the box.
_LOCAL_DEV_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]


def _parse_frontend_origins(raw: str) -> list[str]:
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


_extra_origins = _parse_frontend_origins(os.getenv("FRONTEND_ORIGINS", ""))

app.add_middleware(
    CORSMiddleware,
    allow_origins=[*_LOCAL_DEV_ORIGINS, *_extra_origins],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------
class StkPushRequest(BaseModel):
    phone_number: str = Field(
        ..., pattern=r"^0[17]\d{8}$", description="Kenyan mobile number, 10 digits starting with 01 or 07"
    )
    amount: int = Field(..., gt=0, strict=True)
    route: str = Field(..., min_length=1, max_length=40)

    @field_validator("route")
    @classmethod
    def strip_route(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Route must not be blank.")
        return value


class VerifyRequest(BaseModel):
    transaction_code: str = Field(..., pattern=r"^[A-Z0-9]{6,15}$")
    amount: int = Field(..., gt=0, strict=True)

    @field_validator("transaction_code", mode="before")
    @classmethod
    def normalize_transaction_code(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value


class FirebaseRoleUpdateRequest(BaseModel):
    role: Literal["admin", "conductor", "none"]


class AttemptReviewRequest(BaseModel):
    """Evidence review for an unknown attempt. It records a review; it never settles one."""

    model_config = {"extra": "forbid"}

    outcome: Literal["no_change_review"]
    evidence_type: Literal["daraja_portal", "mpesa_statement", "passenger_report", "other"]
    evidence_reference: str = Field(..., pattern=r"^[A-Za-z0-9_.:-]{3,64}$")
    reason: str = Field(..., min_length=10, max_length=300)

    @field_validator("evidence_reference")
    @classmethod
    def reject_phone_numbers(cls, value: str) -> str:
        if re.fullmatch(r"\+?254\d{9}|0[17]\d{8}", value):
            raise ValueError("Evidence reference must not be a phone number.")
        return value

    @field_validator("reason")
    @classmethod
    def keep_passenger_data_out(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 10:
            raise ValueError("Reason is too short.")
        if re.search(r"\d{9,}", value.replace(" ", "")):
            raise ValueError("Reason must not contain phone numbers or long identifiers.")
        return value


def _to_daraja_phone(local_number: str) -> str:
    """Converts the conductor-friendly '0712345678' into Daraja's required '254712345678'."""
    return "254" + local_number[1:]


def _is_success_code(value: object) -> bool:
    return (type(value) is int and value == 0) or value == "0"


def _is_definite_result_code(value: object) -> bool:
    """A well-formed numeric provider result code (int or digit string, not bool)."""
    if type(value) is int:
        return True
    return isinstance(value, str) and value.isascii() and value.isdigit()


def _callback_phone_matches(phone: object, expected_local_phone: str) -> bool:
    if phone is None or phone == "":
        return True
    if isinstance(phone, int) and not isinstance(phone, bool):
        phone = str(phone)
    if not isinstance(phone, str):
        return False
    if phone.startswith("0") and len(phone) == 10:
        phone = _to_daraja_phone(phone)
    return phone == _to_daraja_phone(expected_local_phone)


def _acknowledge_callback() -> dict:
    return {"ResultCode": 0, "ResultDesc": "Accepted"}


def _public_stk_status(local: dict) -> dict[str, str]:
    status: dict[str, str] = {"status": local["status"]}
    if local.get("reason"):
        status["reason"] = local["reason"]
    return status


# ---------------------------------------------------------------------------
# Flow 1: conductor sends an STK push prompt straight to the passenger's phone
# ---------------------------------------------------------------------------
@app.post("/stk-push")
async def trigger_stk_push(
    req: StkPushRequest,
    idempotency_key: str = Header(
        ...,
        alias="Idempotency-Key",
        min_length=36,
        max_length=36,
        pattern=r"^[0-9a-fA-F-]{36}$",
    ),
    _user: AuthenticatedUser = Depends(require_conductor),
):
    try:
        previous, created = await run_in_threadpool(
            store.reserve_stk_submission,
            idempotency_key,
            req.phone_number,
            req.amount,
            req.route,
            _user.uid,
        )
    except StorageConflict as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    if not created:
        if previous["state"] == "submitting":
            raise HTTPException(status_code=409, detail="This payment request is still being submitted.")
        if previous["state"] == "unknown":
            raise HTTPException(
                status_code=504,
                detail=previous["reason"] or "The payment request outcome is unknown.",
            )
        return {
            "checkout_request_id": previous["checkout_request_id"],
            "status": "pending",
        }

    try:
        result = await daraja.stk_push(
            phone_number=_to_daraja_phone(req.phone_number),
            amount=req.amount,
            account_ref=req.route,
            description="Fare payment",
        )
    except daraja.DarajaOutcomeUnknown as e:
        await run_in_threadpool(
            store.mark_stk_submission_unknown,
            idempotency_key,
            str(e),
        )
        raise HTTPException(status_code=504, detail=str(e)) from e
    except daraja.DarajaError as e:
        await run_in_threadpool(store.release_stk_submission, idempotency_key)
        raise HTTPException(status_code=502, detail=str(e)) from e

    if not _is_success_code(result.get("ResponseCode")):
        await run_in_threadpool(store.release_stk_submission, idempotency_key)
        raise HTTPException(
            status_code=502,
            detail=result.get("ResponseDescription", "Safaricom did not accept the payment request."),
        )

    checkout_id = result.get("CheckoutRequestID")
    if not checkout_id:
        detail = "Safaricom may have received this request. Check the passenger's phone before trying again."
        await run_in_threadpool(
            store.mark_stk_submission_unknown,
            idempotency_key,
            detail,
        )
        raise HTTPException(status_code=504, detail=detail)

    try:
        await run_in_threadpool(
            store.create_stk_transaction,
            checkout_id,
            req.phone_number,
            req.amount,
            req.route,
            idempotency_key,
        )
    except StorageError as e:
        raise HTTPException(
            status_code=503,
            detail=(
                "Safaricom may have accepted this payment, but it could not be safely recorded. "
                "Check the passenger's phone and transaction status before trying again."
            ),
        ) from e
    return {"checkout_request_id": checkout_id, "status": "pending"}


@app.get("/stk-push/attempts/{idempotency_key}")
async def recover_stk_attempt(
    idempotency_key: str = Path(pattern=r"^[0-9a-fA-F-]{36}$"),
    _user: AuthenticatedUser = Depends(require_conductor),
):
    """
    Lets the conductor who created a payment attempt reconnect to it after a
    reload or sign-in. Unknown keys and keys owned by anyone else are
    indistinguishable (404), so another account's attempts are never revealed.
    A 404 is not proof that no prompt was sent; it only means nothing is
    recoverable for this account.
    """
    attempt = await run_in_threadpool(store.get_stk_attempt, idempotency_key, _user.uid)
    if attempt is None:
        raise HTTPException(status_code=404, detail="No recoverable payment attempt was found.")
    response: dict[str, str] = {"state": attempt["state"]}
    if attempt["checkout_request_id"]:
        response["checkout_request_id"] = attempt["checkout_request_id"]
    if attempt["status"]:
        response["status"] = attempt["status"]
    if attempt["reason"]:
        response["reason"] = attempt["reason"]
    return response


@app.get("/stk-push/{checkout_request_id}/status")
async def get_stk_push_status(
    checkout_request_id: str,
    _user: AuthenticatedUser = Depends(require_conductor),
):
    # Ownership is checked before any storage read of the result or Daraja call.
    # Missing, foreign-owned and legacy ownerless checkouts are indistinguishable.
    if not await run_in_threadpool(
        store.stk_checkout_owned_by, checkout_request_id, _user.uid
    ):
        raise HTTPException(status_code=404, detail="Unknown checkout_request_id")
    local = await run_in_threadpool(store.get_stk_transaction, checkout_request_id)
    if local is None:
        raise HTTPException(status_code=404, detail="Unknown checkout_request_id")

    # If the callback already updated us, trust that — it's faster and free.
    if local["status"] != "pending":
        return _public_stk_status(local)

    if not await run_in_threadpool(
        store.claim_stk_status_query,
        checkout_request_id,
        STK_STATUS_QUERY_MIN_INTERVAL,
    ):
        local = await run_in_threadpool(store.get_stk_transaction, checkout_request_id)
        if local is None:
            raise HTTPException(status_code=404, detail="Unknown checkout_request_id")
        return _public_stk_status(local)

    # Otherwise actively poll Daraja (useful if the callback hasn't arrived yet).
    try:
        result = await daraja.query_stk_status(checkout_request_id)
    except daraja.DarajaError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    response_checkout_id = result.get("CheckoutRequestID")
    if response_checkout_id != checkout_request_id:
        raise HTTPException(status_code=502, detail="Safaricom returned status for a different payment request.")

    result_code = result.get("ResultCode")

    if _is_success_code(result_code):
        await run_in_threadpool(
            store.update_stk_status_from_query,
            checkout_request_id,
            "success",
        )
    # A non-success or malformed query result is never finalized as a failure: the
    # query can report an in-progress payment (its exact shape is unverified) and
    # a valid callback may still arrive. Only the callback can mark a payment
    # failed, so an unconfirmed payment stays pending and unresolved.

    local = await run_in_threadpool(store.get_stk_transaction, checkout_request_id)
    if local is None:
        raise HTTPException(status_code=404, detail="Unknown checkout_request_id")
    return _public_stk_status(local)


@app.post("/daraja/callback", dependencies=[Depends(require_callback_token)])
async def daraja_callback(request: Request):
    """Daraja POSTs the outcome here once the commuter enters their PIN (or cancels)."""
    try:
        body = await request.json()
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid callback payload.") from e

    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid callback payload.")
    callback_body = body.get("Body")
    if not isinstance(callback_body, dict):
        raise HTTPException(status_code=400, detail="Invalid callback payload.")
    stk_callback = callback_body.get("stkCallback")
    if not isinstance(stk_callback, dict):
        raise HTTPException(status_code=400, detail="Invalid callback payload.")
    checkout_id = stk_callback.get("CheckoutRequestID")
    result_code = stk_callback.get("ResultCode")

    local = (
        await run_in_threadpool(store.get_stk_transaction, checkout_id)
        if isinstance(checkout_id, str)
        else None
    )
    if not local:
        return _acknowledge_callback()

    # A callback without a well-formed result code proves nothing either way.
    if not _is_definite_result_code(result_code):
        return _acknowledge_callback()

    metadata = stk_callback.get("CallbackMetadata", {})
    metadata_items = metadata.get("Item", []) if isinstance(metadata, dict) else []
    if not isinstance(metadata_items, list):
        metadata_items = []
    items = {
        item["Name"]: item.get("Value")
        for item in metadata_items
        if isinstance(item, dict) and item.get("Name")
    }
    receipt = items.get("MpesaReceiptNumber")
    try:
        if isinstance(items.get("Amount"), bool):
            raise ValueError("Amount must be a number.")
        paid_amount = int(items.get("Amount"))
    except (TypeError, ValueError):
        paid_amount = None

    await run_in_threadpool(
        store.apply_stk_callback,
        checkout_id,
        _is_success_code(result_code),
        receipt if isinstance(receipt, str) else None,
        paid_amount,
        (
            local["phone_number"] is None
            or _callback_phone_matches(items.get("PhoneNumber"), local["phone_number"])
        ),
    )

    return _acknowledge_callback()


# ---------------------------------------------------------------------------
# Flow 2: conductor verifies a code the commuter typed in (or showed on an
# old screenshot) against Safaricom's own record.
# ---------------------------------------------------------------------------
@app.post("/verify")
async def verify_transaction(
    req: VerifyRequest,
    _user: AuthenticatedUser = Depends(require_conductor),
):
    code = req.transaction_code

    if settings.VERIFY_STUB_MODE:
        return await _verify_against_known_receipts(code, req.amount)

    try:
        verification, created, already_used = await run_in_threadpool(
            store.begin_verification,
            code,
            req.amount,
            _user.uid,
        )
    except StorageConflict as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    if already_used:
        return {"result": "already_used"}
    if not created:
        if verification is None:
            raise HTTPException(status_code=500, detail="Verification state could not be loaded.")
        return {
            "result": verification["status"],
            "transaction_id": code,
            "expected": verification["expected"],
            "detail": verification["reason"],
        }

    # --- real path: Safaricom TransactionStatusQuery (asynchronous) ---
    try:
        result = await daraja.verify_c2b_transaction(code)
    except daraja.DarajaError as e:
        await run_in_threadpool(
            store.complete_verification,
            code,
            "failed",
            None,
            "Safaricom could not complete verification.",
        )
        raise HTTPException(status_code=502, detail=str(e)) from e

    if not _is_success_code(result.get("ResponseCode")):
        reason = "Safaricom did not accept the verification request."
        await run_in_threadpool(store.complete_verification, code, "failed", None, reason)
        raise HTTPException(
            status_code=502,
            detail=reason,
        )

    # Safaricom accepted the query; the real outcome lands later at
    # /daraja/transaction-status-result. Track it so the frontend can poll.
    return {"result": "pending", "transaction_id": code}


@app.get("/verify/{transaction_id}/status")
async def get_verify_status(
    transaction_id: str,
    _user: AuthenticatedUser = Depends(require_conductor),
):
    """Poll this once /verify returns {"result": "pending"} in real (non-stub) mode."""
    code = transaction_id.strip().upper()
    # Missing, foreign-owned and legacy ownerless records are indistinguishable.
    pending = await run_in_threadpool(store.get_verification_for_owner, code, _user.uid)
    if not pending:
        raise HTTPException(status_code=404, detail="Unknown or already-resolved transaction_id")
    return pending


async def _verify_against_known_receipts(code: str, amount: int) -> dict:
    """
    Demo-safe verification: check a code against receipts ThibitiFare has
    actually seen from real STK Push callbacks, instead of round-tripping to
    Daraja's asynchronous TransactionStatusQuery live in front of judges.
    """
    result, expected = await run_in_threadpool(store.claim_known_receipt, code, amount)
    if result == "amount_mismatch":
        return {"result": result, "expected": expected}
    return {"result": result}


@app.post("/daraja/transaction-status-result", dependencies=[Depends(require_callback_token)])
async def transaction_status_result(request: Request):
    """
    Daraja posts the real TransactionStatusQuery result here, asynchronously,
    some time after /verify accepted the query. This is what makes real
    (non-stub) verification "automatic": the frontend polls
    /verify/{id}/status and this endpoint is what fills that in.
    """
    try:
        body = await request.json()
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid callback payload.") from e

    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid callback payload.")
    result = body.get("Result", {})
    if not isinstance(result, dict):
        raise HTTPException(status_code=400, detail="Invalid callback payload.")
    transaction_id = result.get("TransactionID")
    result_code = result.get("ResultCode")

    if not transaction_id:
        return _acknowledge_callback()

    code = str(transaction_id).strip().upper()
    pending = await run_in_threadpool(store.get_verification, code)
    if pending is None:
        return _acknowledge_callback()

    if pending["status"] != "pending":
        return _acknowledge_callback()

    if not _is_success_code(result_code):
        await run_in_threadpool(
            store.complete_verification,
            code,
            "failed",
            None,
            "Safaricom could not verify this transaction.",
        )
        return _acknowledge_callback()

    result_parameters = result.get("ResultParameters", {})
    if not isinstance(result_parameters, dict):
        result_parameters = {}
    result_items = result_parameters.get("ResultParameter", [])
    if not isinstance(result_items, list):
        result_items = []
    params = {
        item.get("Key"): item.get("Value")
        for item in result_items
        if isinstance(item, dict) and item.get("Key")
    }

    actual_amount = params.get("Amount")

    try:
        if isinstance(actual_amount, bool):
            raise ValueError("Amount must be a number.")
        actual_amount = int(actual_amount)
    except (TypeError, ValueError):
        await run_in_threadpool(
            store.complete_verification,
            code,
            "verified",
            None,
            "Safaricom's response did not include a verifiable amount.",
        )
        return _acknowledge_callback()

    await run_in_threadpool(
        store.complete_verification,
        code,
        "verified",
        actual_amount,
    )
    return _acknowledge_callback()


@app.post("/daraja/timeout", dependencies=[Depends(require_callback_token)])
async def daraja_timeout(request: Request):
    """Daraja posts here if a TransactionStatusQuery times out in its own queue."""
    try:
        body = await request.json()
    except ValueError:
        body = {}
    result = body.get("Result", {}) if isinstance(body, dict) else {}
    transaction_id = result.get("TransactionID") if isinstance(result, dict) else None
    if transaction_id:
        await run_in_threadpool(
            store.complete_verification,
            str(transaction_id),
            "failed",
            None,
            "Safaricom could not complete the verification request.",
        )
    return _acknowledge_callback()


@app.get("/")
async def health_check():
    return {
        "status": "ok",
        "service": "ThibitiFare API",
        "daraja_env": "production" if settings.ENV == "production" else "sandbox",
        "verify_mode": "stub" if settings.VERIFY_STUB_MODE else "live",
        "callback_auth": "enabled" if settings.CALLBACK_AUTH_TOKEN else "disabled",
    }


async def require_admin(
    user: AuthenticatedUser = Depends(require_conductor),
) -> AuthenticatedUser:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Administrator access is required.")
    return user


@app.get("/auth/config")
async def auth_config():
    firebase_values = {
        "apiKey": settings.FIREBASE_API_KEY,
        "authDomain": settings.FIREBASE_AUTH_DOMAIN,
        "projectId": settings.FIREBASE_PROJECT_ID,
        "appId": settings.FIREBASE_APP_ID,
    }
    return {
        "enabled": all(firebase_values.values()),
        "firebase": firebase_values,
    }


@app.get("/admin/audit-events")
async def list_audit_events(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    _user: AuthenticatedUser = Depends(require_admin),
):
    try:
        events = await run_in_threadpool(store.list_audit_events, limit, offset)
    except StorageError as error:
        raise HTTPException(
            status_code=503,
            detail="Audit events are temporarily unavailable.",
        ) from error
    return {"events": events, "limit": limit, "offset": offset}


@app.get("/admin/stk-attempts/unknown")
async def list_unknown_stk_attempts(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    _user: AuthenticatedUser = Depends(require_admin),
):
    try:
        attempts = await run_in_threadpool(store.list_unknown_stk_attempts, limit, offset)
    except StorageError as error:
        raise HTTPException(
            status_code=503,
            detail="Payment attempts are temporarily unavailable.",
        ) from error
    return {"attempts": attempts, "limit": limit, "offset": offset}


@app.post("/admin/stk-attempts/{idempotency_key}/review")
async def review_unknown_stk_attempt(
    req: AttemptReviewRequest,
    request: Request,
    idempotency_key: str = Path(pattern=r"^[0-9a-fA-F-]{36}$"),
    _user: AuthenticatedUser = Depends(require_admin),
):
    """
    Records an administrator's evidence review for an attempt stuck in `unknown`.
    It is review-only: the attempt stays `unknown` (so no new prompt can be sent
    under it) because an unknown attempt has no provider checkout ID to validate a
    receipt, amount or phone against. Settlement still requires a provider callback.
    """
    try:
        review = await run_in_threadpool(
            store.review_unknown_stk_attempt,
            idempotency_key,
            _user.uid,
            req.evidence_type,
            req.evidence_reference,
            req.reason,
        )
    except StorageForbidden as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    except StorageConflict as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except StorageError as error:
        raise HTTPException(
            status_code=503,
            detail="The review could not be recorded.",
        ) from error
    if review is None:
        raise HTTPException(status_code=404, detail="Payment attempt was not found.")
    if review["owner_uid"]:
        request.state.audit_subject_uid = review["owner_uid"]
    return {
        "review_id": review["review_id"],
        "idempotency_key": review["idempotency_key"],
        "previous_state": review["previous_state"],
        "new_state": review["new_state"],
        "outcome": review["outcome"],
        "created_at": review["created_at"],
    }


@app.post("/admin/users/{uid}/role")
async def update_firebase_user_role(
    uid: Annotated[str, Path(min_length=1, max_length=128)],
    req: FirebaseRoleUpdateRequest,
    request: Request,
    _user: AuthenticatedUser = Depends(require_admin),
):
    try:
        await run_in_threadpool(set_firebase_user_role, uid, req.role)
    except FirebaseUserNotFound as error:
        raise HTTPException(status_code=404, detail="Firebase user was not found.") from error
    except FirebaseAuthUnavailable as error:
        raise HTTPException(
            status_code=503,
            detail="Firebase account management is temporarily unavailable.",
        ) from error
    request.state.audit_subject_uid = uid
    return {
        "uid": uid,
        "role": req.role,
        "requires_sign_in": True,
    }
