"""Centralized maintenance-mode state used to gate brand-new trade entries.

Scope is intentionally narrow: this module only decides whether a *new*
position/automation entry may be created. It must never be consulted by, or
have any effect on, existing-position protection - monitoring, Stop-Loss,
Take-Profit, Risk Guard, trailing/risk-reduction, closes, or exchange
reconciliation all keep running regardless of maintenance mode.

State lives in ``app.state.maintenance`` (a plain dict, same pattern as the
other in-memory runtime state such as ``app.state.paper``) - no new database
table, cache layer, or background loop is introduced.
"""

from __future__ import annotations

import time
from typing import Any, Literal

from fastapi import HTTPException

MaintenanceMode = Literal["NORMAL", "MAINTENANCE", "EMERGENCY"]
VALID_MODES: frozenset[str] = frozenset({"NORMAL", "MAINTENANCE", "EMERGENCY"})


def default_maintenance_state() -> dict[str, Any]:
    """Initial value for ``app.state.maintenance``. Default is always NORMAL."""
    return {"mode": "NORMAL", "reason": "", "updated_at": None, "updated_by": None}


def get_maintenance_mode(state: dict[str, Any] | None) -> str:
    """Read the current mode. Fails closed to ``UNKNOWN`` if state is missing
    or holds an unrecognized value - callers must treat UNKNOWN like MAINTENANCE
    for new entries while never touching existing-position protection."""
    if not isinstance(state, dict):
        return "UNKNOWN"
    mode = state.get("mode")
    return mode if mode in VALID_MODES else "UNKNOWN"


def new_entry_blocked(state: dict[str, Any] | None) -> bool:
    """True whenever a brand-new position/automation entry must not be created."""
    return get_maintenance_mode(state) != "NORMAL"


def guard_new_entry(state: dict[str, Any] | None) -> None:
    """Raise 503 MAINTENANCE_MODE if new entries are currently blocked. No-op otherwise.

    Call this only at the start of functions that OPEN a brand-new position or
    START a brand-new automation session - never inside position monitoring,
    protection (SL/TP/Risk Guard), or close/cancel code paths.
    """
    if new_entry_blocked(state):
        raise HTTPException(
            status_code=503,
            detail={
                "code": "MAINTENANCE_MODE",
                "message": (
                    "Yeni işlem girişleri bakım modu nedeniyle geçici olarak durduruldu. "
                    "Açık pozisyonlar ve risk koruması etkilenmez."
                ),
                "mode": get_maintenance_mode(state),
            },
        )


def set_maintenance_mode(
    state: dict[str, Any],
    mode: str,
    *,
    reason: str = "",
    updated_by: str | None = None,
) -> dict[str, Any]:
    """Admin-only mutation of the mode flag. Never starts/stops automation or
    touches open positions - it only changes whether new entries are allowed."""
    if mode not in VALID_MODES:
        raise ValueError(f"Invalid maintenance mode: {mode!r}")
    state["mode"] = mode
    state["reason"] = reason
    state["updated_at"] = time.time()
    state["updated_by"] = updated_by
    return dict(state)
