"""Backend-only moderator permissions; install the numbered SQL migration before use.

GET /api/mod/me exposes only the caller's role and permissions, with no specific
permission required. Protected moderator operations use require_permission.
Missing canonical storage fails closed; OWNER grants are always implicit.
"""
import json
import logging
from dataclasses import dataclass
from typing import Callable, Coroutine, Any, Literal, get_args

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from . import v22_commercial as auth

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Moderator"])
Permission = Literal[
    "customers.view",
    "subscriptions.view",
    "payments.view",
    "support.view",
    "support.manage",
    "events.view",
    "approvals.create",
]
PERMISSIONS: tuple[str, ...] = get_args(Permission)


class PermissionGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    permission: Permission


@dataclass(frozen=True)
class ModeratorIdentity:
    user_id: str
    role: str
    permissions: tuple[str, ...]


async def moderator_identity(request: Request) -> ModeratorIdentity:
    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        raise HTTPException(503, "Moderator permission storage unavailable")
    user = await auth.authenticated_user_async(request)
    version = int(user.get("auth_version", 1))
    try:
        row = await pool.fetchrow(
            """SELECT u.auth_version, u.security,
                      (a.payload->'two_factor_enabled' = 'true'::jsonb) AS mfa_enabled,
                      ARRAY(SELECT p.permission FROM moderator_permissions p
                            WHERE p.user_id = u.user_id ORDER BY p.permission) AS permissions
               FROM commercial_auth_users u
               LEFT JOIN commercial_account_settings a ON a.user_id = u.user_id
               WHERE u.user_id = $1""",
            user["id"],
        )
        if row is None:
            raise HTTPException(401, "Session revoked")
        security = json.loads(row["security"]) if isinstance(row["security"], str) else row["security"]
        if not isinstance(security, dict):
            raise ValueError("Invalid canonical security")
        if security.get("active") is not True or int(row["auth_version"]) != version:
            raise HTTPException(401, "Session revoked")
        role = security.get("role")
        if role == "OWNER":
            return ModeratorIdentity(user["id"], role, PERMISSIONS)
        if role != "MODERATOR":
            raise HTTPException(403, "Moderator access required")
        if security.get("email_verified") is not True:
            raise HTTPException(403, "Verified email required")
        if row["mfa_enabled"] is not True:
            raise HTTPException(403, {"code": "mfa_required"})
        permissions = row["permissions"]
        if not isinstance(permissions, (list, tuple)) or any(
            permission not in PERMISSIONS for permission in permissions
        ):
            raise ValueError("Invalid canonical permissions")
        return ModeratorIdentity(user["id"], role, tuple(
            permission for permission in PERMISSIONS if permission in permissions
        ))
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Moderator permission read failed (%s)", type(exc).__name__)
        raise HTTPException(503, "Moderator permission storage unavailable") from None


def require_permission(permission: str) -> Callable[..., Coroutine[Any, Any, ModeratorIdentity]]:
    if permission not in PERMISSIONS:
        raise ValueError("Unsupported moderator permission")

    async def dependency(identity: ModeratorIdentity = Depends(moderator_identity)) -> ModeratorIdentity:
        if permission not in identity.permissions:
            raise HTTPException(403, "Permission required")
        return identity

    return dependency


@router.get("/api/mod/me")
async def moderator_me(identity: ModeratorIdentity = Depends(moderator_identity)):
    return {"role": identity.role, "permissions": list(identity.permissions)}
