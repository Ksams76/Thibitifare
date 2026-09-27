import base64
import time
from datetime import datetime

import httpx

from .config import settings
from .security_credential import generate_security_credential, SecurityCredentialError


class DarajaError(Exception):
    """Raised whenever Daraja rejects a request, or it can't be reached at all."""


_token_cache = {"token": None, "expires_at": 0}


async def get_access_token() -> str:
    """Fetch (and cache) an OAuth access token. Tokens are valid for ~1 hour."""
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"]:
        return _token_cache["token"]

    credentials = f"{settings.CONSUMER_KEY}:{settings.CONSUMER_SECRET}"
    encoded = base64.b64encode(credentials.encode()).decode()

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.get(
                f"{settings.base_url}/oauth/v1/generate?grant_type=client_credentials",
                headers={"Authorization": f"Basic {encoded}"},
            )
        except httpx.TimeoutException as e:
            raise DarajaError(f"Timed out connecting to Safaricom: {e}")
        except httpx.RequestError as e:
            raise DarajaError(f"Network error contacting Safaricom: {e}")

    if resp.status_code != 200:
        raise DarajaError(f"Failed to get access token: {resp.status_code} {resp.text}")

    data = resp.json()
    token = data["access_token"]
    # Refresh a little early (Daraja tokens last 3600s)
    _token_cache["token"] = token
    _token_cache["expires_at"] = now + int(data.get("expires_in", 3599)) - 60
    return token


def _password_and_timestamp() -> tuple[str, str]:
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    raw = f"{settings.SHORTCODE}{settings.PASSKEY}{timestamp}"
    password = base64.b64encode(raw.encode()).decode()
    return password, timestamp


async def stk_push(phone_number: str, amount: int, account_ref: str, description: str) -> dict:
    """Send an STK Push (Lipa Na M-Pesa Online) prompt to the given phone number."""
    token = await get_access_token()
    password, timestamp = _password_and_timestamp()

    payload = {
        "BusinessShortCode": settings.SHORTCODE,
        "Password": password,
        "Timestamp": timestamp,
        "TransactionType": "CustomerPayBillOnline",
        "Amount": amount,
        "PartyA": phone_number,
        "PartyB": settings.SHORTCODE,
        "PhoneNumber": phone_number,
        "CallBackURL": f"{settings.CALLBACK_URL}/daraja/callback",
        "AccountReference": (account_ref or "ThibitiFare")[:12],
        "TransactionDesc": (description or "Fare payment")[:13],
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.post(
                f"{settings.base_url}/mpesa/stkpush/v1/processrequest",
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            )
        except httpx.TimeoutException as e:
            raise DarajaError(f"Timed out connecting to Safaricom: {e}")
        except httpx.RequestError as e:
            raise DarajaError(f"Network error contacting Safaricom: {e}")

    if resp.status_code != 200:
        raise DarajaError(f"STK push failed: {resp.status_code} {resp.text}")

    return resp.json()


async def query_stk_status(checkout_request_id: str) -> dict:
    """Poll Daraja for the outcome of a previously-sent STK push."""
    token = await get_access_token()
    password, timestamp = _password_and_timestamp()

    payload = {
        "BusinessShortCode": settings.SHORTCODE,
        "Password": password,
        "Timestamp": timestamp,
        "CheckoutRequestID": checkout_request_id,
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.post(
                f"{settings.base_url}/mpesa/stkpushquery/v1/query",
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            )
        except httpx.TimeoutException as e:
            raise DarajaError(f"Timed out connecting to Safaricom: {e}")
        except httpx.RequestError as e:
            raise DarajaError(f"Network error contacting Safaricom: {e}")

    if resp.status_code != 200:
        raise DarajaError(f"STK status query failed: {resp.status_code} {resp.text}")

    return resp.json()


async def verify_c2b_transaction(transaction_id: str) -> dict:
    """
    Kick off a TransactionStatusQuery for a commuter-typed transaction code —
    the core of the conductor-side fraud check: real code, right amount, not
    already used.

    IMPORTANT: this API is asynchronous. Safaricom's *immediate* response here
    only confirms the request was accepted (ResponseCode "0"). The real answer
    is POSTed later to ResultURL (/daraja/transaction-status-result). main.py
    wires that callback into a pending-verification store so the frontend can
    poll /verify/{transaction_id}/status the same way it polls STK push status.
    """
    token = await get_access_token()

    try:
        security_credential = generate_security_credential()
    except SecurityCredentialError as e:
        raise DarajaError(str(e))

    payload = {
        "Initiator": settings.INITIATOR_NAME,
        "SecurityCredential": security_credential,
        "CommandID": "TransactionStatusQuery",
        "TransactionID": transaction_id,
        "PartyA": settings.SHORTCODE,
        "IdentifierType": "4",
        "ResultURL": f"{settings.CALLBACK_URL}/daraja/transaction-status-result",
        "QueueTimeOutURL": f"{settings.CALLBACK_URL}/daraja/timeout",
        "Remarks": "ThibitiFare verification",
        "Occasion": "Fare check",
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.post(
                f"{settings.base_url}/mpesa/transactionstatus/v1/query",
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            )
        except httpx.TimeoutException as e:
            raise DarajaError(f"Timed out connecting to Safaricom: {e}")
        except httpx.RequestError as e:
            raise DarajaError(f"Network error contacting Safaricom: {e}")

        if resp.status_code != 200:
            raise DarajaError(f"Transaction status query failed: {resp.status_code} {resp.text}")

        return resp.json()
