"""Append-only administrative evidence, independent of erasable snapshots."""
from typing import Literal, get_args

AuditAction = Literal["ROLE_CHANGED", "PERMISSION_GRANTED", "PERMISSION_REVOKED"]
AUDIT_ACTIONS: tuple[str, ...] = get_args(AuditAction)
