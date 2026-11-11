"""Idempotent, audited administration of Firebase Auth role claims.

Firebase Auth custom claims are the authorization source of truth.  The
Firestore ``users/{uid}.role`` field is maintained only as a UI projection and
must never be used by the backend to bootstrap trust. A backend-only deny
record closes the old-token window while a demotion is being completed.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from firebase_admin import auth, firestore
from firebase_admin import firestore_async
from google.api_core.exceptions import AlreadyExists, Conflict

logger = logging.getLogger(__name__)

UserRole = Literal["user", "admin"]
AUDIT_COLLECTION = "admin_role_audit"
ACCESS_BLOCK_COLLECTION = "admin_access_blocks"
ROLE_LOCK_COLLECTION = "admin_role_locks"
ROLE_STATE_COLLECTION = "admin_role_state"


class RoleOperationConflict(Exception):
    """Another role mutation owns the target UID lock."""


def _audit_result(data: dict[str, Any], audit_id: str) -> dict[str, Any]:
    return {
        "uid": data["targetUid"],
        "role": data["requestedRole"],
        "previousRole": data["previousRoleFromClaims"],
        "auditId": audit_id,
        "requiresTokenRefresh": True,
    }


async def change_user_role(
    *,
    actor_uid: str,
    target_uid: str,
    role: UserRole,
    reason: str,
    operation_id: str,
    db: Any | None = None,
) -> dict[str, Any]:
    if actor_uid == target_uid:
        raise ValueError("Administrators cannot change their own role.")

    normalized_reason = " ".join(reason.split()).strip()
    if len(normalized_reason) < 8:
        raise ValueError("A review reason of at least 8 characters is required.")

    db = db or firestore_async.client()
    audit_ref = db.collection(AUDIT_COLLECTION).document(operation_id)
    block_ref = db.collection(ACCESS_BLOCK_COLLECTION).document(target_uid)
    lock_ref = db.collection(ROLE_LOCK_COLLECTION).document(target_uid)
    state_ref = db.collection(ROLE_STATE_COLLECTION).document(target_uid)
    audit_snapshot = await audit_ref.get()

    if audit_snapshot.exists:
        audit = audit_snapshot.to_dict()
        identity = (
            audit.get("actorUid"),
            audit.get("targetUid"),
            audit.get("requestedRole"),
            audit.get("reason"),
        )
        if identity != (actor_uid, target_uid, role, normalized_reason):
            raise ValueError("Idempotency-Key was already used for another operation.")
        if audit.get("status") == "applied":
            state_snapshot = await state_ref.get()
            state = state_snapshot.to_dict() if state_snapshot.exists else {}
            if state.get("operationId") != operation_id:
                raise RoleOperationConflict(
                    "This role operation was superseded by a newer change."
                )
            return _audit_result(audit, operation_id)
        lock_snapshot = await lock_ref.get()
        lock = lock_snapshot.to_dict() if lock_snapshot.exists else {}
        if lock.get("operationId") != operation_id:
            raise RoleOperationConflict(
                "This role operation no longer owns the target UID lock."
            )
        previous_role: UserRole = audit["previousRoleFromClaims"]
    else:
        try:
            initial_target = await asyncio.to_thread(auth.get_user, target_uid)
        except auth.UserNotFoundError as exc:
            raise LookupError("Target Firebase Auth user was not found.") from exc
        initial_claims = dict(initial_target.custom_claims or {})
        previous_role = "admin" if initial_claims.get("admin") is True else "user"
        audit = {
            "actorUid": actor_uid,
            "targetUid": target_uid,
            "previousRoleFromClaims": previous_role,
            "requestedRole": role,
            "reason": normalized_reason,
            "status": "requested",
            "requestedAt": firestore.SERVER_TIMESTAMP,
        }
        setup = db.batch()
        setup.create(
            lock_ref,
            {
                "operationId": operation_id,
                "actorUid": actor_uid,
                "requestedRole": role,
                "acquiredAt": firestore.SERVER_TIMESTAMP,
            },
        )
        setup.create(audit_ref, audit)
        setup.set(
            state_ref,
            {
                "operationId": operation_id,
                "requestedRole": role,
                "actorUid": actor_uid,
                "updatedAt": firestore.SERVER_TIMESTAMP,
            },
            merge=True,
        )
        if role == "user":
            setup.set(
                block_ref,
                {
                    "blocked": True,
                    "auditId": operation_id,
                    "blockedAt": firestore.SERVER_TIMESTAMP,
                },
                merge=True,
            )
        try:
            await setup.commit()
        except (AlreadyExists, Conflict) as exc:
            raise RoleOperationConflict(
                "Another role change is already in progress for this user."
            ) from exc

    try:
        target = await asyncio.to_thread(auth.get_user, target_uid)
        requested_claims = dict(target.custom_claims or {})
        if role == "admin":
            requested_claims["admin"] = True
        else:
            requested_claims.pop("admin", None)
        await asyncio.to_thread(
            auth.set_custom_user_claims, target_uid, requested_claims or None
        )
        await audit_ref.update(
            {"status": "claim_updated", "claimUpdatedAt": firestore.SERVER_TIMESTAMP}
        )
    except auth.UserNotFoundError as exc:
        missing = db.batch()
        missing.update(
            audit_ref,
            {"status": "target_missing", "completedAt": firestore.SERVER_TIMESTAMP},
        )
        missing.delete(lock_ref)
        await missing.commit()
        raise LookupError("Target Firebase Auth user was not found.") from exc
    except Exception as exc:
        await audit_ref.update(
            {
                "status": "claim_update_failed",
                "error": str(exc)[:500],
                "lastAttemptAt": firestore.SERVER_TIMESTAMP,
            }
        )
        raise RuntimeError("Firebase Auth role claim could not be updated.") from exc

    try:
        await asyncio.to_thread(auth.revoke_refresh_tokens, target_uid)
        await audit_ref.update(
            {"status": "tokens_revoked", "tokensRevokedAt": firestore.SERVER_TIMESTAMP}
        )
    except Exception as exc:
        await audit_ref.update(
            {
                "status": "revocation_failed",
                "error": str(exc)[:500],
                "lastAttemptAt": firestore.SERVER_TIMESTAMP,
            }
        )
        raise RuntimeError(
            "Role claim changed, but token revocation must be retried with the same Idempotency-Key."
        ) from exc

    try:
        batch = db.batch()
        batch.set(
            db.collection("users").document(target_uid),
            {
                "role": role,
                "roleUpdatedAt": firestore.SERVER_TIMESTAMP,
                "roleUpdatedBy": actor_uid,
            },
            merge=True,
        )
        if role == "admin":
            batch.delete(block_ref)
        batch.delete(lock_ref)
        batch.update(
            audit_ref,
            {
                "status": "applied",
                "completedAt": firestore.SERVER_TIMESTAMP,
                "error": firestore.DELETE_FIELD,
            },
        )
        await batch.commit()
    except Exception as exc:
        logger.exception(
            "Role claim changed but Firestore projection/audit failed for target=%s audit=%s",
            target_uid,
            operation_id,
        )
        try:
            await audit_ref.update(
                {
                    "status": "claim_applied_projection_failed",
                    "error": str(exc)[:500],
                    "completedAt": firestore.SERVER_TIMESTAMP,
                }
            )
        except Exception:
            logger.exception("Could not mark failed role audit %s", operation_id)
        raise RuntimeError(
            "Role claim was updated, but its Firestore projection needs reconciliation. "
            "Retry with the same Idempotency-Key."
        ) from exc

    logger.info(
        "Administrative role changed actor=%s target=%s previous=%s requested=%s audit=%s",
        actor_uid,
        target_uid,
        previous_role,
        role,
        operation_id,
    )
    return _audit_result(
        {
            "targetUid": target_uid,
            "requestedRole": role,
            "previousRoleFromClaims": previous_role,
        },
        operation_id,
    )
