"""
app/auth.py

FastAPI dependency that verifies the Firebase ID token from the
Authorization: Bearer <token> header.

Usage:
    @router.post("/some-endpoint")
    async def handler(uid: str = Depends(verify_token)):
        ...
"""

import asyncio
import logging
import os

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from firebase_admin import app_check, auth, firestore_async
from .local_notification_test import enabled, validate_environment

_bearer = HTTPBearer()
logger = logging.getLogger(__name__)


async def verify_chat_app_check(token: str | None) -> None:
    mode = os.getenv("CHAT_APP_CHECK_MODE", "off")
    if mode == "off":
        return
    allowed = set(os.getenv("CHAT_APP_CHECK_APP_IDS", "").split())
    if mode not in {"report", "enforce"} or (mode == "enforce" and not allowed):
        raise HTTPException(503, "App Check configuration unavailable")
    if token and len(token) <= 4096:
        try:
            claims = await asyncio.to_thread(app_check.verify_token, token)
            if not allowed or (claims.get("app_id") or claims.get("sub")) in allowed:
                return
        except Exception:
            pass
    logger.warning("chat_app_check_rejected mode=%s token_present=%s", mode, bool(token))
    if mode == "enforce":
        raise HTTPException(403, "App Check verification required")


def _bootstrap_admin_uids() -> set[str]:
    """Deployment-controlled break-glass identities used only for bootstrap.

    Firestore role documents are intentionally not consulted here: existing
    values may predate hardened rules and therefore are not a trustworthy
    source for permanent administrative authority.
    """
    return {
        value.strip()
        for value in os.getenv("BOOTSTRAP_ADMIN_UIDS", "").split(",")
        if value.strip()
    }


async def _decode_token(token: str, *, check_revoked: bool = False) -> dict:
    return await asyncio.to_thread(
        auth.verify_id_token,
        token,
        check_revoked=check_revoked,
    )


async def _admin_access_is_blocked(uid: str) -> bool:
    """Deny a UID whose demotion started, even if an old token still says admin."""
    snapshot = await (
        firestore_async.client()
        .collection("admin_access_blocks")
        .document(uid)
        .get()
    )
    return snapshot.exists and snapshot.to_dict().get("blocked") is True


async def verify_token(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
) -> str:
    """
    Verify a Firebase ID token.

    Returns the authenticated user's UID on success.
    Raises HTTP 401 on any authentication failure.
    """
    try:
        decoded = await _decode_token(credentials.credentials)
        return decoded["uid"]
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid or expired Firebase ID token: {exc}",
        ) from exc


async def verify_admin(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
) -> str:
    """
    Verify administrative authority from a Firebase Auth custom claim.
    """
    try:
        decoded = await _decode_token(
            credentials.credentials,
            check_revoked=not enabled(),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid, expired, or revoked Firebase ID token.",
        ) from exc

    uid = decoded["uid"]
    if enabled():
        # The token was verified against the loopback Auth emulator above.
        # This bypass exists only behind the strict local-test preflight and
        # cannot be activated in a production environment.
        validate_environment()
        return uid

    try:
        if await _admin_access_is_blocked(uid):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Administrative access has been revoked.",
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Administrative authorization is temporarily unavailable.",
        ) from exc

    if decoded.get("admin") is True:
        return uid
    if uid in _bootstrap_admin_uids():
        logger.warning(
            "Bootstrap administrator access used by uid=%s; migrate this "
            "identity to an audited custom claim and remove it from "
            "BOOTSTRAP_ADMIN_UIDS",
            uid,
        )
        return uid
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Admin privileges required.",
    )


async def verify_community_evidence_verifier(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
) -> str:
    """Verify dedicated independent Community Evidence Verifier authority (C1).

    Requires Firebase Auth custom claim `community_evidence_verifier=True`
    (separate from generic `admin` upload/moderation privileges).
    """
    try:
        decoded = await _decode_token(
            credentials.credentials,
            check_revoked=not enabled(),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid, expired, or revoked Firebase ID token.",
        ) from exc

    uid = decoded["uid"]
    if enabled():
        validate_environment()
        return uid

    try:
        if await _admin_access_is_blocked(uid):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Verifier access has been revoked.",
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Verifier authorization is temporarily unavailable.",
        ) from exc

    if decoded.get("community_evidence_verifier") is True:
        return uid
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Dedicated community_evidence_verifier role required.",
    )
