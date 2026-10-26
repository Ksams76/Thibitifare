import json
from dataclasses import dataclass
from threading import Lock
from typing import Any

import firebase_admin
from firebase_admin import auth, credentials, exceptions
from fastapi import Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from google.auth.exceptions import DefaultCredentialsError

from .config import settings


class InvalidFirebaseToken(Exception):
    """Raised when a Firebase ID token is invalid, expired, or revoked."""


class FirebaseAuthUnavailable(Exception):
    """Raised when Firebase verification cannot be configured or reached."""


class FirebaseUserNotFound(Exception):
    """Raised when a Firebase account selected for an admin operation is missing."""


@dataclass(frozen=True)
class AuthenticatedUser:
    uid: str
    role: str | None


_app_lock = Lock()
_bearer_scheme = HTTPBearer(auto_error=False)
_ALLOWED_ROLES = frozenset({"admin", "conductor"})


def get_firebase_app() -> firebase_admin.App:
    if not settings.FIREBASE_PROJECT_ID:
        raise FirebaseAuthUnavailable("Firebase Authentication is not configured.")

    try:
        app = firebase_admin.get_app()
    except ValueError:
        pass
    else:
        if app.project_id != settings.FIREBASE_PROJECT_ID:
            raise FirebaseAuthUnavailable(
                "The initialized Firebase app does not match the configured project."
            )
        return app

    with _app_lock:
        try:
            app = firebase_admin.get_app()
        except ValueError:
            app = None
        if app is not None:
            if app.project_id != settings.FIREBASE_PROJECT_ID:
                raise FirebaseAuthUnavailable(
                    "The initialized Firebase app does not match the configured project."
                )
            return app

        try:
            if settings.FIREBASE_SERVICE_ACCOUNT_JSON:
                service_account = json.loads(settings.FIREBASE_SERVICE_ACCOUNT_JSON)
                if not isinstance(service_account, dict):
                    raise ValueError("Firebase service account must be a JSON object.")
                credential = credentials.Certificate(service_account)
            else:
                credential = credentials.ApplicationDefault()
            return firebase_admin.initialize_app(
                credential,
                options={"projectId": settings.FIREBASE_PROJECT_ID},
            )
        except (ValueError, json.JSONDecodeError, DefaultCredentialsError) as error:
            raise FirebaseAuthUnavailable(
                "Firebase server credentials are missing or invalid."
            ) from error
        except exceptions.FirebaseError as error:
            raise FirebaseAuthUnavailable(
                "Firebase Authentication could not be initialized."
            ) from error


def verify_firebase_id_token(id_token: str) -> dict[str, Any]:
    try:
        decoded = auth.verify_id_token(
            id_token,
            app=get_firebase_app(),
            check_revoked=True,
        )
    except (
        auth.InvalidIdTokenError,
        auth.ExpiredIdTokenError,
        auth.RevokedIdTokenError,
        auth.UserDisabledError,
    ) as error:
        raise InvalidFirebaseToken from error
    except (exceptions.FirebaseError, DefaultCredentialsError) as error:
        raise FirebaseAuthUnavailable(
            "Firebase Authentication is temporarily unavailable."
        ) from error

    return decoded


def set_firebase_user_role(uid: str, role: str) -> None:
    app = get_firebase_app()
    try:
        user = auth.get_user(uid, app=app)
    except auth.UserNotFoundError as error:
        raise FirebaseUserNotFound from error
    except exceptions.FirebaseError as error:
        raise FirebaseAuthUnavailable(
            "Firebase account management is temporarily unavailable."
        ) from error

    claims = dict(user.custom_claims or {})
    if role == "none":
        claims.pop("role", None)
    else:
        claims["role"] = role
    try:
        auth.set_custom_user_claims(user.uid, claims, app=app)
        auth.revoke_refresh_tokens(user.uid, app=app)
    except (exceptions.FirebaseError, ValueError) as error:
        raise FirebaseAuthUnavailable(
            "The Firebase role update could not be completed safely."
        ) from error


async def require_conductor(
    request: Request,
    credentials_: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> AuthenticatedUser:
    if credentials_ is None or credentials_.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=401,
            detail="Sign in is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        claims = await run_in_threadpool(
            verify_firebase_id_token,
            credentials_.credentials,
        )
    except InvalidFirebaseToken as error:
        raise HTTPException(
            status_code=401,
            detail="Your sign-in has expired. Please sign in again.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from error
    except FirebaseAuthUnavailable as error:
        raise HTTPException(
            status_code=503,
            detail="Sign-in verification is temporarily unavailable.",
        ) from error

    uid = claims.get("uid")
    if not isinstance(uid, str) or not uid:
        raise HTTPException(
            status_code=401,
            detail="The sign-in token is invalid.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    role_claim = claims.get("role")
    role = role_claim if isinstance(role_claim, str) else None
    user = AuthenticatedUser(uid=uid, role=role)
    request.state.authenticated_user = user

    if role not in _ALLOWED_ROLES:
        raise HTTPException(
            status_code=403,
            detail="This account is not authorized to use ThibitiFare.",
        )

    return user
