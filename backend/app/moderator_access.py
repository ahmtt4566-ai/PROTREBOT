"""Backend-only moderator permissions; install the numbered SQL migration before use."""
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict

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
