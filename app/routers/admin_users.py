"""Privileged user-role administration."""

import re
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from ..auth import verify_admin
from ..services.admin_user_service import RoleOperationConflict, change_user_role

router = APIRouter(prefix="/admin/users", tags=["admin-users"])


class UserRoleChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "admin"]
    reason: str = Field(min_length=8, max_length=500)


@router.patch("/{target_uid}/role")
async def update_user_role(
    target_uid: str,
    request: UserRoleChangeRequest,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    admin_uid: str = Depends(verify_admin),
):
    if not target_uid or len(target_uid) > 128 or "/" in target_uid:
        raise HTTPException(status_code=422, detail="Invalid target user identifier.")
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", idempotency_key):
        raise HTTPException(status_code=422, detail="Invalid Idempotency-Key.")
    try:
        return await change_user_role(
            actor_uid=admin_uid,
            target_uid=target_uid,
            role=request.role,
            reason=request.reason,
            operation_id=idempotency_key,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RoleOperationConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
