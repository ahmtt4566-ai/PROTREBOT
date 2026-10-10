"""Backend-only moderator permissions; install the numbered SQL migration before use.

GET /api/mod/me exposes only the caller's role and permissions, with no specific
permission required. Protected moderator operations use require_permission.
Missing canonical storage fails closed; OWNER grants are always implicit.
OWNER manages grants using POST /api/v22/admin/users/{user_id}/permissions
with {"permission": "..."} or DELETE on that path plus /{permission}.
GET on the same path reads the canonical target's current permission list.
Both operations require a canonical MODERATOR target; repeated grants preserve
their original attribution, and repeated deletion reports removed=false.
"""
import json
import logging
from dataclasses import dataclass
from typing import Callable, Coroutine, Any, Literal, get_args

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from . import v22_commercial as auth
from .audit_log import AuditActor, write_audit

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
    auth_version: int


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
            return ModeratorIdentity(user["id"], role, PERMISSIONS, version)
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
        ), version)
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


async def locked_owner_target(connection, owner_id: str, version: int, user_id: str):
    rows = await connection.fetch(
        """SELECT user_id, auth_version, security FROM commercial_auth_users
           WHERE user_id = ANY($1::text[]) ORDER BY user_id FOR UPDATE""",
        [owner_id, user_id],
    )
    users = {
        row["user_id"]: {
            "auth_version": int(row["auth_version"]),
            "security": json.loads(row["security"]) if isinstance(row["security"], str) else row["security"],
        } for row in rows
    }
    current_owner = users.get(owner_id)
    if not current_owner or current_owner["auth_version"] != version:
        raise HTTPException(401, "Session revoked")
    if (current_owner["security"].get("role") != "OWNER"
            or current_owner["security"].get("active") is not True):
        raise HTTPException(403, "Owner access required")
    target = users.get(user_id)
    if target is None:
        raise HTTPException(404, "User not found")
    return current_owner, target


async def manage_permission(
    user_id: str, permission: Permission, request: Request, *, grant: bool,
) -> dict[str, Any]:
    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        raise HTTPException(503, "Moderator permission storage unavailable")
    owner = await auth.authenticated_user_async(request, owner=True)
    owner_id = owner["id"]
    version = int(owner.get("auth_version", 1))
    try:
        async with pool.acquire() as connection, connection.transaction():
            # Stable lock order serializes grants with both parties' role changes.
            current_owner, target = await locked_owner_target(
                connection, owner_id, version, user_id,
            )
            if target["security"].get("role") != "MODERATOR":
                raise HTTPException(409, "Permissions require a MODERATOR target")
            actor = AuditActor.from_request(request, {"id": owner_id, **current_owner["security"]})
            previous = await connection.fetchrow(
                """SELECT user_id, permission, granted_by, granted_at
                   FROM moderator_permissions WHERE user_id = $1 AND permission = $2""",
                user_id, permission,
            )
            if grant:
                row = await connection.fetchrow(
                    """INSERT INTO moderator_permissions (user_id, permission, granted_by)
                       VALUES ($1, $2, $3) ON CONFLICT (user_id, permission) DO NOTHING
                       RETURNING user_id, permission, granted_by, granted_at""",
                    user_id, permission, owner_id,
                )
                if row is None:
                    row = await connection.fetchrow(
                        """SELECT user_id, permission, granted_by, granted_at
                           FROM moderator_permissions WHERE user_id = $1 AND permission = $2""",
                        user_id, permission,
                    )
                if row is None:
                    raise HTTPException(409, "Permission grant was not stored")
                await write_audit(
                    connection, actor, "PERMISSION_GRANTED", "MODERATOR_PERMISSION", user_id,
                    {"permission": permission, "granted": previous is not None},
                    {"permission": permission, "granted": True},
                )
                return dict(row)
            row = await connection.fetchrow(
                """DELETE FROM moderator_permissions WHERE user_id = $1 AND permission = $2
                   RETURNING permission""",
                user_id, permission,
            )
            await write_audit(
                connection, actor, "PERMISSION_REVOKED", "MODERATOR_PERMISSION", user_id,
                {"permission": permission, "granted": previous is not None},
                {"permission": permission, "granted": False},
            )
            return {"user_id": user_id, "permission": permission, "removed": row is not None}
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Moderator permission update failed (%s)", type(exc).__name__)
        raise HTTPException(503, "Moderator permission storage unavailable") from None


@router.post("/api/v22/admin/users/{user_id}/permissions")
async def grant_permission(user_id: str, payload: PermissionGrant, request: Request):
    return await manage_permission(user_id, payload.permission, request, grant=True)


@router.get("/api/v22/admin/users/{user_id}/permissions")
async def read_permissions(user_id: str, request: Request):
    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        raise HTTPException(503, "Moderator permission storage unavailable")
    owner = await auth.authenticated_user_async(request, owner=True)
    try:
        async with pool.acquire() as connection, connection.transaction():
            _, target = await locked_owner_target(
                connection, owner["id"], int(owner.get("auth_version", 1)), user_id,
            )
            if target["security"].get("role") != "MODERATOR":
                raise HTTPException(409, "Permissions require a MODERATOR target")
            rows = await connection.fetch(
                "SELECT permission FROM moderator_permissions WHERE user_id = $1 ORDER BY permission",
                user_id,
            )
            stored = [row["permission"] for row in rows]
            if any(permission not in PERMISSIONS for permission in stored):
                raise ValueError("Invalid canonical permissions")
            return {"user_id": user_id, "permissions": [p for p in PERMISSIONS if p in stored]}
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Moderator permission read failed (%s)", type(exc).__name__)
        raise HTTPException(503, "Moderator permission storage unavailable") from None


@router.delete("/api/v22/admin/users/{user_id}/permissions/{permission}")
async def revoke_permission(user_id: str, permission: Permission, request: Request):
    return await manage_permission(user_id, permission, request, grant=False)
