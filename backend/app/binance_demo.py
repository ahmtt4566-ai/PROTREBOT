"""Binance USD-M Futures Demo connector for ProTreBot Elite X.

This module deliberately knows only the Binance Demo hosts. Credentials prefer
the current Windows user's encrypted DPAPI vault (with a local ``backend/.env``
fallback) and are never returned by an API response. Entry orders require a
short-lived manual arm; risk-reducing cancellation and close actions remain
available while disarmed.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import json
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP
from enum import Enum
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .local_storage import DATA_DIR, migrate_legacy_files
from .stop_evidence import observe_position_snapshot


DEMO_REST_BASE = "https://demo-fapi.binance.com"
DEMO_WS_BASE = "wss://demo-fstream.binance.com"
MAX_MARGIN_USDT = Decimal("100")
MAX_LEVERAGE = 2
MANUAL_MAX_LEVERAGE = 50
MAX_NOTIONAL_USDT = Decimal("200")
MAX_OPEN_POSITIONS = 3
ARM_SECONDS = 10 * 60
CLIENT_PREFIX = "PTB_"
PLAN_RECONCILIATION_GRACE_SECONDS = 15
_PROTECTION_INSTALL_LOCKS: dict[str, asyncio.Lock] = {}
_PROTECTION_CLEANUP_LOCKS: dict[str, asyncio.Lock] = {}
logger = logging.getLogger(__name__)
DEMO_SNAPSHOT_LOCK = asyncio.Lock()
DEMO_CLOCK_LOCK = asyncio.Lock()
DEMO_CLOCK_OFFSET_MS = 0
DEMO_CLOCK_SYNCED_AT = 0.0


class ProvenanceState(str, Enum):
    NO_PROVENANCE = "NO_PROVENANCE"
    PROVISIONAL = "PROVISIONAL"
    CONFIRMED = "CONFIRMED"
    BROKEN = "BROKEN"


_ACTIVE_ENTRY_INSTALLATION_CONTEXTS: dict[object, dict[str, Any]] = {}


def _retire_entry_installation_context(context: object) -> None:
    _ACTIVE_ENTRY_INSTALLATION_CONTEXTS.pop(context, None)


def _valid_entry_installation_context(context: object | None, plan: dict[str, Any]) -> bool:
    try:
        record = _ACTIVE_ENTRY_INSTALLATION_CONTEXTS.get(context) if context is not None else None
    except TypeError:
        return False
    return isinstance(record, dict) and record.get("plan") is plan


def can_mutate_lifecycle(plan: dict[str, Any]) -> bool:
    """Allow lifecycle mutation only for an explicitly confirmed provenance record."""
    return plan.get("provenance_state") == ProvenanceState.CONFIRMED.value


def confirmed_slot_key(plan: dict[str, Any]) -> tuple[str, str] | None:
    """Return the current one-way internal provenance slot for a plan."""
    try:
        symbol = normalize_symbol(str(plan.get("symbol") or ""))
    except BinanceDemoError:
        return None
    return symbol, "BOTH"


def find_conflicting_confirmed_plan(
    plans: dict[str, Any] | None,
    candidate: dict[str, Any],
) -> dict[str, Any] | None:
    """Find another confirmed plan occupying the candidate's internal slot."""
    candidate_key = confirmed_slot_key(candidate)
    if candidate_key is None:
        return None
    for plan in (plans or {}).values():
        if plan is candidate or not isinstance(plan, dict) or not can_mutate_lifecycle(plan):
            continue
        if confirmed_slot_key(plan) == candidate_key:
            return plan
    return None


def _usable_identifier(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def find_entry_plan_for_order(
    plans: dict[str, Any] | None,
    *,
    symbol: Any,
    client_order_id: Any,
    order_id: Any,
) -> dict[str, Any] | None:
    """Match a stream event to its exact persisted entry intent."""
    event_client_id = _usable_identifier(client_order_id)
    event_order_id = _usable_identifier(order_id)
    if not event_client_id:
        return None
    try:
        normalized_symbol = normalize_symbol(str(symbol or ""))
    except BinanceDemoError:
        return None
    for plan in (plans or {}).values():
        if not isinstance(plan, dict) or plan.get("provenance_state") in {
            ProvenanceState.BROKEN.value,
            ProvenanceState.CONFIRMED.value,
        }:
            continue
        try:
            if normalize_symbol(str(plan.get("symbol") or "")) != normalized_symbol:
                continue
        except BinanceDemoError:
            continue
        if _usable_identifier(plan.get("provenance_entry_client_order_id")) != event_client_id:
            continue
        plan_order_id = _usable_identifier(plan.get("provenance_entry_order_id"))
        if plan_order_id and event_order_id and plan_order_id != event_order_id:
            continue
        if plan_order_id and not event_order_id:
            continue
        return plan
    return None


def _provenance_decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed >= 0 else None


def record_entry_trade_observation(
    state: dict[str, Any],
    *,
    symbol: Any,
    order_id: Any,
    client_order_id: Any,
    trade_id: Any,
    last_fill_quantity: Any,
    cumulative_fill_quantity: Any,
    observation_cycle: int,
) -> dict[str, Any] | None:
    """Record exact entry-fill evidence without authorizing confirmation."""
    plan = find_entry_plan_for_order(
        state.get("plans"),
        symbol=symbol,
        client_order_id=client_order_id,
        order_id=order_id,
    )
    if plan is None:
        return None
    normalized_trade_id = _usable_identifier(trade_id)
    last_fill = _provenance_decimal(last_fill_quantity)
    cumulative_fill = _provenance_decimal(cumulative_fill_quantity)
    expected = _provenance_decimal(plan.get("provenance_expected_quantity"))
    if not normalized_trade_id or last_fill is None or cumulative_fill is None or expected is None:
        return None
    trade_ids = plan.get("provenance_trade_ids")
    if not isinstance(trade_ids, list):
        trade_ids = []
        plan["provenance_trade_ids"] = trade_ids
    if normalized_trade_id in {str(value).strip() for value in trade_ids}:
        return plan
    trade_ids.append(normalized_trade_id)
    plan["provenance_last_fill_quantity"] = decimal_text(last_fill)
    plan["provenance_cumulative_fill_quantity"] = decimal_text(cumulative_fill)
    plan["provenance_fill_observation_cycle"] = int(observation_cycle)
    if cumulative_fill > expected:
        plan["provenance_state"] = ProvenanceState.BROKEN.value
        if not plan.get("provenance_broken_reason"):
            plan["provenance_broken_reason"] = "expected_quantity_differs_from_cumulative_entry_fill"
        return plan
    if cumulative_fill < expected:
        plan["provenance_state"] = ProvenanceState.PROVISIONAL.value
        return plan
    observation_id = f"entry-fill:{_usable_identifier(order_id) or 'unknown'}:{normalized_trade_id}"
    observe_plan_provenance(
        plan,
        observed_quantity=decimal_text(cumulative_fill),
        trade_ids=[normalized_trade_id],
        provenance_linked_observation=True,
        current_observation_id=observation_id,
    )
    return plan


def derive_provenance_state(
    *,
    current_state: str | ProvenanceState | None = None,
    entry_recorded: bool = False,
    trade_ids: list[Any] | tuple[Any, ...] | None = None,
    expected_quantity: Any = None,
    observed_quantity: Any = None,
    independent_reconciliation: bool = False,
    provenance_linked_observation: bool = False,
    previous_observation_id: str | None = None,
    current_observation_id: str | None = None,
    last_observation_id: str | None = None,
    restored: bool = False,
) -> ProvenanceState:
    """Derive observer provenance without inventing exchange position identity."""
    try:
        state_value = current_state.value if isinstance(current_state, ProvenanceState) else str(current_state or ProvenanceState.NO_PROVENANCE.value)
        state = ProvenanceState(state_value)
    except ValueError:
        state = ProvenanceState.NO_PROVENANCE
    if state is ProvenanceState.BROKEN:
        return state
    if restored and state is ProvenanceState.CONFIRMED:
        return ProvenanceState.PROVISIONAL
    if not entry_recorded:
        return ProvenanceState.NO_PROVENANCE
    try:
        expected = Decimal(str(expected_quantity))
        observed = Decimal(str(observed_quantity))
    except (InvalidOperation, TypeError, ValueError):
        return ProvenanceState.BROKEN if independent_reconciliation else ProvenanceState.PROVISIONAL
    if expected != observed:
        return ProvenanceState.BROKEN
    if not independent_reconciliation or not provenance_linked_observation:
        return ProvenanceState.PROVISIONAL
    normalized_previous = str(previous_observation_id or "").strip()
    normalized_current = str(current_observation_id or "").strip()
    normalized_last = str(last_observation_id or "").strip()
    if not normalized_previous or not normalized_current or normalized_previous == normalized_current or normalized_previous != normalized_last:
        return ProvenanceState.PROVISIONAL
    normalized_trade_ids = {str(value).strip() for value in (trade_ids or ()) if str(value).strip()}
    if not normalized_trade_ids:
        return ProvenanceState.PROVISIONAL
    return ProvenanceState.CONFIRMED


def provenance_entry_fields(
    *,
    entry_client_order_id: Any = None,
    entry_order_id: Any = None,
    pre_entry_quantity: Any = None,
    expected_quantity: Any = None,
) -> dict[str, Any]:
    """Build persisted observer fields from local entry provenance only."""
    return {
        "provenance_state": ProvenanceState.PROVISIONAL.value if entry_client_order_id or entry_order_id else ProvenanceState.NO_PROVENANCE.value,
        "provenance_broken_reason": None,
        "provenance_entry_client_order_id": entry_client_order_id,
        "provenance_entry_order_id": entry_order_id,
        "provenance_trade_ids": [],
        "provenance_pre_entry_quantity": pre_entry_quantity,
        "provenance_expected_quantity": expected_quantity,
        "provenance_last_observed_quantity": None,
        "provenance_last_reconciliation_at": None,
        "provenance_last_observation_id": None,
        "provenance_last_fill_quantity": None,
        "provenance_cumulative_fill_quantity": None,
        "provenance_fill_observation_cycle": None,
    }


def ensure_plan_provenance_fields(plan: dict[str, Any]) -> dict[str, Any]:
    """Materialize observer fields for legacy plans without changing lifecycle state."""
    defaults = provenance_entry_fields()
    for key, value in defaults.items():
        plan.setdefault(key, value)
    if not isinstance(plan.get("provenance_trade_ids"), list):
        plan["provenance_trade_ids"] = []
    return plan


def _record_plan_observation(
    plan: dict[str, Any],
    *,
    observed_quantity: Any = None,
    trade_ids: list[Any] | tuple[Any, ...] | None = None,
    observed_at: str | None = None,
    independent_reconciliation: bool = False,
    provenance_linked_observation: bool = False,
    previous_observation_id: str | None = None,
    current_observation_id: str | None = None,
    restored: bool = False,
) -> tuple[dict[str, Any], bool]:
    """Record observer data; this does not authorize lifecycle mutations."""
    existing_trade_ids = plan.get("provenance_trade_ids")
    merged_trade_ids = list(existing_trade_ids) if isinstance(existing_trade_ids, list) else []
    for trade_id in trade_ids or ():
        if str(trade_id).strip() and trade_id not in merged_trade_ids:
            merged_trade_ids.append(trade_id)
    plan["provenance_trade_ids"] = merged_trade_ids
    if observed_quantity is not None:
        plan["provenance_last_observed_quantity"] = observed_quantity
    if observed_at:
        plan["provenance_last_reconciliation_at"] = observed_at
    next_state = derive_provenance_state(
        current_state=plan.get("provenance_state"),
        entry_recorded=bool(plan.get("provenance_entry_client_order_id") or plan.get("provenance_entry_order_id")),
        trade_ids=merged_trade_ids,
        expected_quantity=plan.get("provenance_expected_quantity"),
        observed_quantity=observed_quantity if observed_quantity is not None else plan.get("provenance_last_observed_quantity"),
        independent_reconciliation=independent_reconciliation,
        provenance_linked_observation=provenance_linked_observation,
        previous_observation_id=previous_observation_id,
        current_observation_id=current_observation_id,
        last_observation_id=plan.get("provenance_last_observation_id"),
        restored=restored,
    )
    confirmation_eligible = next_state is ProvenanceState.CONFIRMED
    if confirmation_eligible:
        next_state = ProvenanceState.PROVISIONAL
    plan["provenance_state"] = next_state.value
    if current_observation_id:
        plan["provenance_last_observation_id"] = current_observation_id
    if next_state is ProvenanceState.BROKEN and not plan.get("provenance_broken_reason"):
        plan["provenance_broken_reason"] = "expected_quantity_differs_from_observed_quantity"
    return plan, confirmation_eligible


def observe_plan_provenance(
    plan: dict[str, Any],
    *,
    plans: dict[str, Any] | None = None,
    observed_quantity: Any = None,
    trade_ids: list[Any] | tuple[Any, ...] | None = None,
    observed_at: str | None = None,
    independent_reconciliation: bool = False,
    provenance_linked_observation: bool = False,
    previous_observation_id: str | None = None,
    current_observation_id: str | None = None,
    restored: bool = False,
) -> dict[str, Any]:
    """Record observer data without authorizing a CONFIRMED transition."""
    previous_state = plan.get("provenance_state")
    result, confirmation_eligible = _record_plan_observation(
        plan,
        observed_quantity=observed_quantity,
        trade_ids=trade_ids,
        observed_at=observed_at,
        independent_reconciliation=independent_reconciliation,
        provenance_linked_observation=provenance_linked_observation,
        previous_observation_id=previous_observation_id,
        current_observation_id=current_observation_id,
        restored=restored,
    )
    if confirmation_eligible and previous_state == ProvenanceState.CONFIRMED.value and not restored:
        result["provenance_state"] = ProvenanceState.CONFIRMED.value
    return result


async def observe_plan_provenance_locked(
    state: dict[str, Any],
    plan: dict[str, Any],
    *,
    observation_cycle: int | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Apply one provenance observation under the owning state's mutation lock."""
    plans = state.setdefault("plans", {})
    lock = state.get("lock")
    if lock is None:
        result = observe_plan_provenance(plan, plans=plans, **kwargs)
        persist_runtime(state)
        return result
    async with lock:
        if observation_cycle is not None:
            fill_cycle = plan.get("provenance_fill_observation_cycle")
            try:
                if fill_cycle is None or int(observation_cycle) <= int(fill_cycle):
                    return plan
            except (TypeError, ValueError):
                return plan
            if plan.get("provenance_state") != ProvenanceState.PROVISIONAL.value:
                return plan
            if not plan.get("provenance_last_observation_id") or not plan.get("provenance_trade_ids"):
                return plan
            expected = _provenance_decimal(plan.get("provenance_expected_quantity"))
            completed_fill = _provenance_decimal(plan.get("provenance_cumulative_fill_quantity"))
            if expected is None or completed_fill != expected:
                return plan
        previous_state = plan.get("provenance_state")
        result, confirmation_eligible = _record_plan_observation(plan, **kwargs)
        if confirmation_eligible:
            if previous_state == ProvenanceState.CONFIRMED.value and not kwargs.get("restored", False):
                result["provenance_state"] = ProvenanceState.CONFIRMED.value
            elif isinstance(plans, dict) and not find_conflicting_confirmed_plan(plans, plan):
                result["provenance_state"] = ProvenanceState.CONFIRMED.value
        persist_runtime(state)
        return result


async def confirm_provenance_from_snapshot(
    state: dict[str, Any],
    snapshot: dict[str, Any],
    observation_cycle: int,
) -> None:
    """Use a later position snapshot as the second provenance observation."""
    positions = []
    if isinstance(snapshot, dict):
        raw_positions = snapshot.get("_provenance_positions")
        positions = raw_positions if isinstance(raw_positions, list) else snapshot.get("positions", [])
    if not isinstance(positions, list):
        return
    for plan in list(state.get("plans", {}).values()):
        if not isinstance(plan, dict) or plan.get("provenance_state") != ProvenanceState.PROVISIONAL.value:
            continue
        expected = _provenance_decimal(plan.get("provenance_expected_quantity"))
        completed_fill = _provenance_decimal(plan.get("provenance_cumulative_fill_quantity"))
        if expected is None or completed_fill != expected:
            continue
        try:
            normalized_symbol = normalize_symbol(str(plan.get("symbol") or ""))
        except BinanceDemoError:
            continue
        position_side = str(plan.get("position_side") or "BOTH").upper()
        matching_positions = [
            item for item in positions
            if isinstance(item, dict)
            and str(item.get("symbol") or "").upper() == normalized_symbol
            and str(item.get("position_side") or "BOTH").upper() == position_side
        ]
        if len(matching_positions) != 1:
            continue
        position = matching_positions[0]
        previous_observation_id = _usable_identifier(plan.get("provenance_last_observation_id"))
        if previous_observation_id is None:
            continue
        current_observation_id = f"position-cycle:{int(observation_cycle)}:{plan.get('id') or 'unknown'}"
        await observe_plan_provenance_locked(
            state,
            plan,
            observation_cycle=observation_cycle,
            observed_quantity=position.get("quantity"),
            trade_ids=list(plan.get("provenance_trade_ids") or []),
            independent_reconciliation=True,
            provenance_linked_observation=True,
            previous_observation_id=previous_observation_id,
            current_observation_id=current_observation_id,
        )


def provenance_diagnostic(plan: dict[str, Any]) -> dict[str, Any]:
    """Return non-secret observer data without asserting position ownership."""
    return {
        "provenance_state": plan.get("provenance_state", ProvenanceState.NO_PROVENANCE.value),
        "expected_quantity": plan.get("provenance_expected_quantity"),
        "observed_quantity": plan.get("provenance_last_observed_quantity"),
        "broken_reason": plan.get("provenance_broken_reason"),
        "entry_order_id": plan.get("provenance_entry_order_id"),
        "entry_client_order_id": plan.get("provenance_entry_client_order_id"),
        "trade_ids": list(plan.get("provenance_trade_ids") or []),
        "last_reconciliation_at": plan.get("provenance_last_reconciliation_at"),
    }


def provenance_observer_diagnostic(state: dict[str, Any], snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    """Report provenance and ambiguity without associating exchange positions to plans."""
    terminal_statuses = {"KAPANDI", "CLOSED", "İPTAL", "GÜVENLİK İÇİN KAPATILDI", "ACİL DURDURULDU"}
    active_plans = [
        plan for plan in state.get("plans", {}).values()
        if isinstance(plan, dict) and str(plan.get("status") or "").upper() not in terminal_statuses
    ]
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for plan in active_plans:
        key = (
            str(plan.get("symbol") or "").upper(),
            str(plan.get("position_side") or "BOTH").upper(),
        )
        grouped.setdefault(key, []).append(plan)
    ambiguities = [
        {"symbol": symbol, "position_side": position_side, "plan_ids": [str(plan.get("id") or "") for plan in plans]}
        for (symbol, position_side), plans in grouped.items()
        if len(plans) > 1
    ]
    positions = snapshot.get("positions", []) if isinstance(snapshot, dict) else []
    return {
        "exchange_position_identity_available": False,
        "plans": [provenance_diagnostic(plan) | {"plan_id": plan.get("id")} for plan in active_plans],
        "observed_position_count": len(positions) if isinstance(positions, list) else 0,
        "ambiguities": ambiguities,
    }


def request_correlation_id(request: Request | None = None) -> str:
    return str(request.headers.get("Rndr-Id") or f"local-{uuid.uuid4().hex[:16]}") if request else f"local-{uuid.uuid4().hex[:16]}"


def trace_log(event: str, request_id: str, **fields: Any) -> None:
    values = " ".join(f"{key}={str(value).replace(chr(10), ' ')[:240]}" for key, value in fields.items())
    logger.info("[DEMO_ORDER_TRACE] %s request_id=%s%s", event, request_id, f" {values}" if values else "")


def safe_trace_error(error: Exception, client: "BinanceDemoClient" | None = None) -> str:
    message = str(error)
    api_key = getattr(client, "api_key", "") if client is not None else ""
    if api_key:
        message = message.replace(api_key, "[gizli]")
    return message[:240]


async def traced_stage(stage: str, request_id: str, operation: Any, *, client: "BinanceDemoClient" | None = None) -> Any:
    started = time.monotonic()
    trace_log(f"{stage}.start", request_id)
    try:
        result = await operation
    except Exception as exc:
        trace_log(
            f"{stage}.end",
            request_id,
            duration_ms=round((time.monotonic() - started) * 1000, 2),
            success=False,
            error_type=type(exc).__name__,
            error_message=safe_trace_error(exc, client),
        )
        raise
    trace_log(f"{stage}.end", request_id, duration_ms=round((time.monotonic() - started) * 1000, 2), success=True)
    return result


@asynccontextmanager
async def traced_lock(lock: asyncio.Lock, request_id: str, name: str) -> Any:
    wait_started = time.monotonic()
    await lock.acquire()
    trace_log(f"{name}.lock", request_id, lock_wait_ms=round((time.monotonic() - wait_started) * 1000, 2))
    try:
        yield
    finally:
        lock.release()

BACKEND_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = BACKEND_ROOT / ".env"
migrate_legacy_files(("binance_demo_runtime.json",))
STATE_PATH = DATA_DIR / "binance_demo_runtime.json"

PUBLIC_PATHS = {
    "/fapi/v1/time",
    "/fapi/v1/exchangeInfo",
    "/fapi/v1/ticker/24hr",
    "/fapi/v1/ticker/price",
    "/fapi/v1/klines",
}
PRIVATE_PATHS = {
    ("GET", "/fapi/v3/account"),
    ("GET", "/fapi/v3/positionRisk"),
    ("GET", "/fapi/v1/symbolConfig"),
    ("GET", "/fapi/v1/openOrders"),
    ("GET", "/fapi/v1/order"),
    ("GET", "/fapi/v1/openAlgoOrders"),
    ("GET", "/fapi/v1/allOrders"),
    ("GET", "/fapi/v1/allAlgoOrders"),
    ("GET", "/fapi/v1/userTrades"),
    ("GET", "/fapi/v1/positionSide/dual"),
    ("POST", "/fapi/v1/positionSide/dual"),
    ("POST", "/fapi/v1/leverage"),
    ("POST", "/fapi/v1/marginType"),
    ("POST", "/fapi/v1/order/test"),
    ("POST", "/fapi/v1/order"),
    ("POST", "/fapi/v1/algoOrder"),
    ("DELETE", "/fapi/v1/order"),
    ("DELETE", "/fapi/v1/algoOrder"),
}
API_KEY_PATHS = {
    ("POST", "/fapi/v1/listenKey"),
    ("PUT", "/fapi/v1/listenKey"),
    ("DELETE", "/fapi/v1/listenKey"),
}

router = APIRouter(prefix="/api/binance-demo", tags=["Binance Futures Demo"])


class BinanceDemoError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        http_status: int = 502,
        exchange_code: int | None = None,
        unknown_execution: bool = False,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.exchange_code = exchange_code
        self.unknown_execution = unknown_execution


class ArmRequest(BaseModel):
    confirmation: str = Field(min_length=1, max_length=32)


class DemoOrderRequest(BaseModel):
    symbol: str = Field(min_length=5, max_length=20)
    direction: Literal["LONG", "SHORT"]
    order_type: Literal["MARKET", "LIMIT"] = "MARKET"
    margin_usdt: float = Field(ge=5, le=100)
    leverage: int = Field(ge=1, le=MANUAL_MAX_LEVERAGE)
    limit_price: float | None = Field(default=None, gt=0)
    stop_loss: float = Field(gt=0)
    tp1: float = Field(gt=0)
    tp2: float = Field(gt=0)
    tp3: float = Field(gt=0)


class CancelOrderRequest(BaseModel):
    symbol: str = Field(min_length=5, max_length=20)
    order_id: int = Field(gt=0)


class CancelAlgoRequest(BaseModel):
    symbol: str = Field(min_length=5, max_length=20)
    algo_id: int = Field(gt=0)


class ClosePositionRequest(BaseModel):
    symbol: str = Field(min_length=5, max_length=20)
    confirmation: str = Field(min_length=1, max_length=32)
    position_side: Literal["BOTH", "LONG", "SHORT"] = "BOTH"


class ReducePositionRequest(BaseModel):
    symbol: str = Field(min_length=5, max_length=20)
    quantity: float = Field(gt=0)
    confirmation: str = Field(min_length=1, max_length=32)
    position_side: Literal["BOTH", "LONG", "SHORT"] = "BOTH"


class EmergencyRequest(BaseModel):
    confirmation: str = Field(min_length=1, max_length=48)
    close_positions: bool = True


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_symbol(value: str) -> str:
    symbol = re.sub(r"[^A-Z0-9]", "", value.upper())
    if not symbol.endswith("USDT") or not 5 <= len(symbol) <= 20:
        raise BinanceDemoError("Yalnızca USDT vadeli işlem pariteleri destekleniyor.", http_status=422)
    return symbol


async def resolve_demo_symbol(client: BinanceDemoClient, value: str) -> str:
    cleaned = re.sub(r"[^A-Z0-9]", "", value.strip().upper())
    if cleaned.endswith("USDT"):
        return normalize_symbol(cleaned)
    payload = await client.public_get("/fapi/v1/exchangeInfo")
    row = next((item for item in payload.get("symbols", []) if (
        str(item.get("baseAsset") or "").upper() == cleaned
        and item.get("status") == "TRADING"
        and item.get("contractType") == "PERPETUAL"
        and item.get("quoteAsset") == "USDT"
    )), None)
    if row and row.get("symbol"):
        return normalize_symbol(str(row["symbol"]))
    return normalize_symbol(cleaned)


def response_rows(payload: Any) -> list[dict[str, Any]]:
    """Normalize Binance list/object responses without trusting their shape."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        nested = payload.get("positions")
        if isinstance(nested, list):
            return [item for item in nested if isinstance(item, dict)]
        return [payload] if payload else []
    return []


def load_demo_credentials() -> tuple[str, str]:
    # V28 web deployments use the encrypted in-application vault.  Once a
    # TESTNET record exists, its active switch is authoritative and legacy
    # environment values cannot bypass it.
    try:
        from .exchange_connections import cached_credentials, vault_managed

        vault_values = cached_credentials("TESTNET", active_only=True)
        if vault_values[0] and vault_values[1]:
            return vault_values
        if vault_managed("TESTNET"):
            return "", ""
    except (ImportError, RuntimeError, ValueError):
        pass
    try:
        from .credential_store import load_credentials

        vault_api_key, vault_secret_key = load_credentials()
        if len(vault_api_key) >= 10 and len(vault_secret_key) >= 10:
            return vault_api_key, vault_secret_key
    except (ImportError, OSError, RuntimeError):
        pass
    values: dict[str, str] = {}
    if ENV_PATH.exists():
        try:
            for raw_line in ENV_PATH.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
        except OSError:
            values = {}
    api_key = str(os.environ.get("BINANCE_DEMO_API_KEY") or values.get("BINANCE_DEMO_API_KEY") or "").strip()
    secret_key = str(os.environ.get("BINANCE_DEMO_SECRET_KEY") or values.get("BINANCE_DEMO_SECRET_KEY") or "").strip()
    return api_key, secret_key


def credentials_configured(request: Request | None = None) -> bool:
    if request is not None:
        try:
            from .exchange_connections import session_credentials_for_request

            member = getattr(request.state, "member", None)
            api_key, secret_key = session_credentials_for_request(request, "TESTNET")
            if member is not None:
                return bool(api_key and secret_key)
        except (ImportError, RuntimeError, ValueError):
            pass
        if getattr(request.state, "member", None) is not None:
            return False
    api_key, secret_key = load_demo_credentials()
    return len(api_key) >= 10 and len(secret_key) >= 10


def signed_query(secret_key: str, params: dict[str, Any]) -> tuple[str, str]:
    """Return Binance-compatible query text and HMAC signature."""
    query = urlencode([(key, value) for key, value in params.items() if value is not None], doseq=True)
    signature = hmac.new(secret_key.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
    return query, signature


def decimal_text(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return "0" if text in {"-0", ""} else text


def position_amount(value: Any) -> Decimal:
    try:
        return Decimal("0") if value is None else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def floor_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def round_tick(value: Decimal, tick: Decimal) -> Decimal:
    if tick <= 0:
        return value
    return (value / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick


class BinanceDemoClient:
    def __init__(self, http: httpx.AsyncClient, api_key: str, secret_key: str, *, public_only: bool = False) -> None:
        if (not api_key or not secret_key) and not public_only:
            raise BinanceDemoError(
                "Demo API bağlantısı aktif değil. Programdaki Borsa Bağlantıları bölümünden Testnet anahtarını kaydedip aktifleştirin.",
                http_status=412,
            )
        self.http = http
        self.api_key = api_key
        self.secret_key = secret_key
        self.public_only = public_only
        self.time_offset_ms = 0
        self.last_time_sync = 0.0
        self.last_status_code: int | None = None
        self.trace_request_id: str | None = None
        self._clock_lock = asyncio.Lock()

    async def public_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if path not in PUBLIC_PATHS:
            raise BinanceDemoError("İzin verilmeyen Demo API yolu.", http_status=500)
        return await self._request("GET", path, params or {}, signed=False)

    async def sync_clock(self, *, force: bool = False) -> None:
        global DEMO_CLOCK_OFFSET_MS, DEMO_CLOCK_SYNCED_AT
        request_started = time.monotonic()
        if not force and request_started - DEMO_CLOCK_SYNCED_AT < 30:
            self.time_offset_ms = DEMO_CLOCK_OFFSET_MS
            self.last_time_sync = DEMO_CLOCK_SYNCED_AT
            return
        async with DEMO_CLOCK_LOCK:
            if (
                (not force and time.monotonic() - DEMO_CLOCK_SYNCED_AT < 30)
                or (force and DEMO_CLOCK_SYNCED_AT > request_started)
            ):
                self.time_offset_ms = DEMO_CLOCK_OFFSET_MS
                self.last_time_sync = DEMO_CLOCK_SYNCED_AT
                return
            before = int(time.time() * 1000)
            payload = await self.public_get("/fapi/v1/time")
            after = int(time.time() * 1000)
            server_time = int(payload["serverTime"])
            DEMO_CLOCK_OFFSET_MS = server_time - ((before + after) // 2)
            DEMO_CLOCK_SYNCED_AT = time.monotonic()
            self.time_offset_ms = DEMO_CLOCK_OFFSET_MS
            self.last_time_sync = DEMO_CLOCK_SYNCED_AT

    async def signed(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        method = method.upper()
        if (method, path) not in PRIVATE_PATHS:
            raise BinanceDemoError("İzin verilmeyen özel Demo API işlemi.", http_status=500)
        for attempt in range(2 if method == "GET" else 1):
            await self.sync_clock(force=attempt == 1)
            payload = dict(params or {})
            payload["timestamp"] = int(time.time() * 1000) + self.time_offset_ms
            payload["recvWindow"] = 60000
            query, signature = signed_query(self.secret_key, payload)
            try:
                return await self._request(
                    method,
                    path,
                    payload,
                    signed=True,
                    encoded_query=query,
                    signature=signature,
                )
            except BinanceDemoError as exc:
                if method != "GET" or exc.exchange_code != -1021 or attempt == 1:
                    raise
        raise BinanceDemoError("Binance Demo zaman senkronizasyonu başarısız oldu.", http_status=502)

    async def api_key_request(self, method: str, path: str) -> Any:
        """Call a USER_STREAM endpoint with the API key but without a signature."""
        method = method.upper()
        if (method, path) not in API_KEY_PATHS:
            raise BinanceDemoError("İzin verilmeyen Demo kullanıcı akışı işlemi.", http_status=500)
        return await self._request(method, path, {}, signed=False, api_key_header=True)

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any],
        *,
        signed: bool,
        encoded_query: str = "",
        signature: str = "",
        api_key_header: bool = False,
    ) -> Any:
        url = f"{DEMO_REST_BASE}{path}"
        if not url.startswith(f"{DEMO_REST_BASE}/"):
            raise BinanceDemoError("Demo sunucu kilidi doğrulanamadı.", http_status=500)
        headers = {"X-MBX-APIKEY": self.api_key} if signed or api_key_header else {}
        request_url = f"{url}?{encoded_query}&signature={signature}" if signed else url
        attempts = 2 if method == "GET" and (method, path) in PRIVATE_PATHS else 1
        trace_request_id = self.trace_request_id if path in {"/fapi/v1/order", "/fapi/v1/algoOrder"} else None
        if trace_request_id:
            logger.info("[DEMO_HTTP_TRACE] request_start request_id=%s method=%s url_path=%s", trace_request_id, method, path)
        for attempt in range(attempts):
            try:
                response = await self.http.request(
                    method,
                    request_url,
                    params=None if signed else params,
                    headers=headers,
                    timeout=30,
                )
                break
            except httpx.RequestError as exc:
                if attempt + 1 == attempts:
                    if trace_request_id:
                        logger.warning(
                            "[DEMO_HTTP_TRACE] request_end request_id=%s method=%s url_path=%s success=false error_type=%s",
                            trace_request_id,
                            method,
                            path,
                            type(exc).__name__,
                        )
                    raise BinanceDemoError(
                        f"Binance Demo {method} {path} bağlantısı başarısız ({type(exc).__name__})."
                    ) from exc
                logger.warning(
                    "[DEMO_HTTP_TRACE] retry method=%s attempt=%s url_path=%s reason=%s delay_ms=250",
                    method,
                    attempt + 2,
                    path,
                    type(exc).__name__,
                )
                await asyncio.sleep(0.25)

        self.last_status_code = response.status_code
        if trace_request_id:
            logger.info(
                "[DEMO_HTTP_TRACE] request_end request_id=%s method=%s url_path=%s success=%s http_status=%s",
                trace_request_id,
                method,
                path,
                response.status_code < 400,
                response.status_code,
            )
        if response.status_code >= 400:
            try:
                body = response.json()
            except (ValueError, json.JSONDecodeError):
                body = {}
            code = body.get("code") if isinstance(body, dict) else None
            raw_message = body.get("msg") if isinstance(body, dict) else None
            safe_message = str(raw_message or "Binance Demo işlemi reddetti.").replace(self.api_key, "[gizli]")
            if response.status_code in {429, 418}:
                safe_message = "Demo API hız sınırına ulaşıldı; kısa süre bekleyip tekrar deneyin."
            unknown = response.status_code == 503
            if unknown:
                safe_message = "Emir durumu belirsiz döndü; sistem açık emirlerden doğrulama yapacak."
            raise BinanceDemoError(
                safe_message,
                http_status=429 if response.status_code in {429, 418} else 502,
                exchange_code=int(code) if isinstance(code, int) else None,
                unknown_execution=unknown,
            )
        try:
            return response.json()
        except (ValueError, json.JSONDecodeError):
            return {}


def client_for(request: Request) -> BinanceDemoClient:
    has_member_session = getattr(request.state, "member", None) is not None
    try:
        from .exchange_connections import session_credentials_for_request

        api_key, secret_key = session_credentials_for_request(request, "TESTNET")
    except (ImportError, RuntimeError, ValueError):
        api_key, secret_key = "", ""

    if has_member_session:
        if not api_key or not secret_key:
            raise BinanceDemoError(
                "Demo API bağlantısı aktif değil. Programdaki Borsa Bağlantıları bölümünden Testnet anahtarını kaydedip aktifleştirin.",
                http_status=412,
            )
        return BinanceDemoClient(request.app.state.http, api_key, secret_key)

    if not api_key or not secret_key:
        api_key, secret_key = load_demo_credentials()
    return BinanceDemoClient(request.app.state.http, api_key, secret_key)


def client_for_state(application: Any, state: dict[str, Any]) -> BinanceDemoClient:
    user_id = str(state.get("_user_id") or "").strip()
    if user_id:
        session_key = str(state.get("_session_id") or "").strip()
        try:
            from .exchange_connections import _SESSION_CACHE, _SESSION_META

            cache_key = (session_key, "TESTNET")
            session_meta = _SESSION_META.get(cache_key, {})
            if (
                not session_key
                or not session_meta.get("active")
                or str(session_meta.get("user_id") or "").strip() != user_id
            ):
                raise BinanceDemoError(
                    "Kullanıcı Demo API oturumu aktif değil; arka plan işlemi atlandı.",
                    http_status=412,
                )
            api_key, secret_key = _SESSION_CACHE.get(cache_key, ("", ""))
        except (ImportError, RuntimeError, ValueError) as exc:
            raise BinanceDemoError("Kullanıcı Demo API credential bağlamı okunamadı.", http_status=412) from exc
        if not api_key or not secret_key:
            raise BinanceDemoError("Kullanıcı Demo API credential bağlamı eksik.", http_status=412)
        return BinanceDemoClient(application.state.http, api_key, secret_key)
    api_key, secret_key = load_demo_credentials()
    return BinanceDemoClient(application.state.http, api_key, secret_key)


def _request_user_id(request: Request | None) -> str:
    if request is None:
        return ""
    request_state = getattr(request, "state", None)
    user = getattr(request_state, "member", None) or getattr(request_state, "user", None)
    return str((user or {}).get("id") or "").strip()


def _validate_execution_context(
    request: Request | None,
    demo_state: dict[str, Any],
    v21_state: dict[str, Any] | None,
) -> None:
    request_user_id = _request_user_id(request)
    demo_user_id = str(demo_state.get("_user_id") or "").strip()
    v21_user_id = str((v21_state or {}).get("_user_id") or "").strip()
    expected_user_id = request_user_id or demo_user_id or v21_user_id
    if not expected_user_id:
        return
    if (
        not request_user_id and not demo_user_id
        or not demo_user_id
        or not v21_user_id
        or request_user_id not in {"", expected_user_id}
        or demo_user_id != expected_user_id
        or v21_user_id != expected_user_id
    ):
        raise BinanceDemoError("Demo execution context kullanıcı eşleşmesi başarısız; işlem atlandı.", http_status=409)


def _demo_user_store(application: Any) -> dict[str, dict[str, Any]]:
    store = getattr(application.state, "_binance_demo_user_state", None)
    if not isinstance(store, dict):
        store = {}
        application.state._binance_demo_user_state = store
    return store


def _state_application(state: dict[str, Any]) -> Any | None:
    app = state.get("_app") if isinstance(state, dict) else None
    return app


def _current_user_id(request: Request | None = None, state: dict[str, Any] | None = None) -> str:
    if state is not None:
        value = str(state.get("_user_id") or "").strip()
        if value:
            return value
    if request is None:
        return ""
    request_state = getattr(request, "state", None)
    user = getattr(request_state, "member", None) or getattr(request_state, "user", None)
    return str((user or {}).get("id") or "").strip()


def _is_valid_demo_payload(payload: Any) -> bool:
    return isinstance(payload, dict) and isinstance(payload.get("plans"), dict)


async def restore_demo_state_for_user(application: Any, user_id: str) -> dict[str, Any] | None:
    pool = getattr(application.state, "db_pool", None)
    if not user_id:
        return None
    if pool is None:
        file_state = _load_file_demo_state(user_id)
        if file_state is not None:
            return _store_restored_demo_state(application, user_id, file_state, "file_fallback", "restored", "available")
        return _store_restored_demo_state(application, user_id, {}, "unknown", "restore_failed", "persistence_suppressed")
    try:
        row = await pool.fetchrow(
            "SELECT payload FROM application_state_snapshots WHERE state_key = $1",
            _db_demo_snapshot_key(user_id),
        )
    except Exception:
        file_state = _load_file_demo_state(user_id)
        if file_state is not None:
            return _store_restored_demo_state(application, user_id, file_state, "file_fallback", "restored", "available")
        return _store_restored_demo_state(application, user_id, {}, "unknown", "restore_failed", "persistence_suppressed")
    if row is None:
        return _store_restored_demo_state(application, user_id, {}, "postgres", "no_snapshot", "initialized_empty")
    payload = row["payload"]
    if not _is_valid_demo_payload(payload):
        return _store_restored_demo_state(application, user_id, {}, "postgres", "restore_failed", "persistence_suppressed")
    return _store_restored_demo_state(application, user_id, payload, "postgres", "restored", "available")


def _restore_diagnostic(origin: str, status: str, plan_count: int, persistence_action: str) -> dict[str, Any]:
    return {
        "restore_origin": origin,
        "restore_status": status,
        "plan_count": plan_count,
        "persistence_action": persistence_action,
    }


def _state_from_demo_payload(
    payload: dict[str, Any],
    user_id: str,
    application: Any,
    origin: str,
    status: str,
    persistence_action: str,
) -> dict[str, Any]:
    plans = payload.get("plans", {}) if isinstance(payload.get("plans"), dict) else {}
    for plan in plans.values():
        if isinstance(plan, dict):
            ensure_plan_provenance_fields(plan)
            if plan.get("provenance_state") == ProvenanceState.CONFIRMED.value:
                observe_plan_provenance(plan, restored=True)
    state = {
        "connected": bool(payload.get("connected")),
        "armed_until": payload.get("armed_until", 0),
        "last_checked": payload.get("last_checked"),
        "last_error": payload.get("last_error"),
        "events": payload.get("events", []) if isinstance(payload.get("events"), list) else [],
        "plans": plans,
        "reconciliation": payload.get("reconciliation", {}) if isinstance(payload.get("reconciliation"), dict) else {},
        "lock": asyncio.Lock(),
        "_user_id": user_id,
        "_app": application,
        "_restore_origin": origin,
        "_restore_status": status,
        "_restore_diagnostic": _restore_diagnostic(origin, status, len(plans), persistence_action),
        "_persistence_blocked": status == "restore_failed",
    }
    logger.info(
        "Demo restore diagnostic restore_origin=%s restore_status=%s plan_count=%d persistence_action=%s",
        origin,
        status,
        len(plans),
        persistence_action,
    )
    return state


def _store_restored_demo_state(
    application: Any,
    user_id: str,
    payload: dict[str, Any],
    origin: str,
    status: str,
    persistence_action: str,
) -> dict[str, Any]:
    state = _state_from_demo_payload(payload, user_id, application, origin, status, persistence_action)
    _demo_user_store(application)[user_id] = state
    return state


def _load_file_demo_state(user_id: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("users"), dict):
        return None
    user_state = payload["users"].get(user_id)
    return user_state if isinstance(user_state, dict) and isinstance(user_state.get("plans"), dict) else None


def _default_demo_state(application: Any) -> dict[str, Any]:
    state = getattr(application.state, "binance_demo", None)
    if isinstance(state, dict):
        return state
    state = {
        "connected": False,
        "armed_until": 0,
        "last_checked": None,
        "last_error": None,
        "events": [],
        "plans": {},
        "lock": asyncio.Lock(),
    }
    application.state.binance_demo = state
    return state


def _db_demo_snapshot_key(user_id: str) -> str:
    return f"binance_demo:user:{user_id}"


def persist_runtime(state: dict[str, Any]) -> None:
    if state.get("_persistence_blocked") or state.get("_restore_status") == "restore_failed":
        return
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    user_id = str(state.get("_user_id") or "").strip()
    payload: Any
    app = _state_application(state)
    if user_id and app is not None:
        pool = getattr(app.state, "db_pool", None)
        if pool is not None:
            snapshot = {
                "user_id": user_id,
                "plans": state.get("plans", {}),
                "events": state.get("events", []),
                "connected": bool(state.get("connected")),
                "armed_until": state.get("armed_until", 0),
                "last_checked": state.get("last_checked"),
                "last_error": state.get("last_error"),
                "reconciliation": state.get("reconciliation", {}),
                "saved_at": utc_now(),
            }
            try:
                import asyncio
                asyncio.get_running_loop()
            except RuntimeError:
                pass
            try:
                import asyncio
                loop = asyncio.get_running_loop()
                if loop.is_running():
                    loop.create_task(_persist_demo_snapshot_db(app, user_id, snapshot))
                    return
            except RuntimeError:
                pass
            try:
                import asyncio
                asyncio.run(_persist_demo_snapshot_db(app, user_id, snapshot))
            except RuntimeError:
                pass
            return
    try:
        existing = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        existing = {}
    if not isinstance(existing, dict):
        existing = {}
    if user_id:
        users = existing.get("users") if isinstance(existing.get("users"), dict) else {}
        users[user_id] = runtime_payload(state)
        payload = {"users": users}
    elif isinstance(existing, dict) and "plans" in existing and isinstance(existing.get("plans"), dict):
        payload = runtime_payload(state)
    else:
        payload = runtime_payload(state)
    temporary = STATE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(STATE_PATH)


async def _persist_demo_snapshot_db(application: Any, user_id: str, payload: dict[str, Any]) -> None:
    pool = getattr(application.state, "db_pool", None)
    if pool is None:
        return
    try:
        await pool.execute(
            """
            INSERT INTO application_state_snapshots (state_key, updated_at, payload)
            VALUES ($1, NOW(), $2::jsonb)
            ON CONFLICT (state_key) DO UPDATE
            SET updated_at = NOW(), payload = EXCLUDED.payload
            """,
            _db_demo_snapshot_key(user_id),
            json.dumps(payload, ensure_ascii=False),
        )
    except Exception:
        pass


def load_runtime(user_id: str | None = None, *, application: Any | None = None) -> dict[str, Any]:
    if application is not None:
        pool = getattr(application.state, "db_pool", None)
        if pool is not None and user_id:
            try:
                import asyncio
                loop = asyncio.get_running_loop()
                if loop.is_running():
                    raise RuntimeError("async fetch not allowed in sync loader")
            except RuntimeError:
                pass
            try:
                row = __import__("asyncio").run(pool.fetchrow("SELECT payload FROM application_state_snapshots WHERE state_key = $1", _db_demo_snapshot_key(user_id)))
            except Exception:
                file_state = _load_file_demo_state(user_id)
                if file_state is not None:
                    return _state_from_demo_payload(file_state, user_id, application, "file_fallback", "restored", "available")
                return _state_from_demo_payload({}, user_id, application, "unknown", "restore_failed", "persistence_suppressed")
            if row is not None:
                payload = row["payload"] if isinstance(row, dict) else row
                if _is_valid_demo_payload(payload):
                    return _state_from_demo_payload(payload, user_id, application, "postgres", "restored", "available")
                return _state_from_demo_payload({}, user_id, application, "postgres", "restore_failed", "persistence_suppressed")
            return _state_from_demo_payload({}, user_id, application, "postgres", "no_snapshot", "initialized_empty")
        if user_id:
            file_state = _load_file_demo_state(user_id)
            if file_state is not None:
                return _state_from_demo_payload(file_state, user_id, application, "file_fallback", "restored", "available")
            return _state_from_demo_payload({}, user_id, application, "unknown", "restore_failed", "persistence_suppressed")
    try:
        payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}
    if isinstance(payload, dict) and isinstance(payload.get("users"), dict):
        if user_id:
            user_state = payload["users"].get(user_id, {})
            if not isinstance(user_state, dict):
                return {}
            return {
                "connected": bool(user_state.get("connected")),
                "armed_until": user_state.get("armed_until", 0),
                "last_checked": user_state.get("last_checked"),
                "last_error": user_state.get("last_error"),
                "events": user_state.get("events", []) if isinstance(user_state.get("events"), list) else [],
                "plans": user_state.get("plans", {}) if isinstance(user_state.get("plans"), dict) else {},
                "reconciliation": user_state.get("reconciliation", {}),
                "_user_id": user_id,
                "_restore_origin": "file_fallback",
                "_restore_status": "restored",
                "_restore_diagnostic": _restore_diagnostic("file_fallback", "restored", len(user_state.get("plans", {})), "available"),
            }
        return next(iter(payload["users"].values()), {}).get("plans", {}) if payload["users"] else {}
    return payload.get("plans", {}) if isinstance(payload.get("plans"), dict) else {}


def state_for(request: Request) -> dict[str, Any]:
    app = request.app
    request_state = getattr(request, "state", None)
    user = getattr(request_state, "member", None) or getattr(request_state, "user", None)
    user_id = str((user or {}).get("id") or "").strip()
    if user_id:
        store = _demo_user_store(app)
        try:
            from .exchange_connections import session_id

            request_session_id = session_id(request)
        except (ImportError, RuntimeError, ValueError):
            request_session_id = ""
        state = store.get(user_id)
        if state is None:
            persisted = load_runtime(user_id, application=app)
            base = copy.deepcopy(_default_demo_state(app))
            restore_failed = isinstance(persisted, dict) and persisted.get("_restore_status") == "restore_failed"
            state = {
                **base,
                "plans": dict(base.get("plans", {}) if restore_failed else (persisted.get("plans", {}) if isinstance(persisted, dict) else {})),
                "events": list(base.get("events", []) if restore_failed else ((persisted.get("events", [])) if isinstance(persisted, dict) and isinstance(persisted.get("events"), list) else [])),
                "connected": bool(base.get("connected", False) if restore_failed else (persisted or {}).get("connected", base.get("connected", False))),
                "armed_until": base.get("armed_until", 0) if restore_failed else (persisted or {}).get("armed_until", base.get("armed_until", 0)),
                "last_checked": base.get("last_checked") if restore_failed else (persisted or {}).get("last_checked"),
                "last_error": base.get("last_error") if restore_failed else (persisted or {}).get("last_error"),
                "reconciliation": base.get("reconciliation", {}) if restore_failed else (persisted or {}).get("reconciliation", {}),
                "_user_id": user_id,
                "_session_id": request_session_id,
                "_app": app,
            }
            for key in ("_restore_origin", "_restore_status", "_restore_diagnostic", "_persistence_blocked"):
                if isinstance(persisted, dict) and key in persisted:
                    state[key] = persisted[key]
            store[user_id] = state
        state["_user_id"] = user_id
        state["_session_id"] = request_session_id or state.get("_session_id", "")
        state["_app"] = app
        return state
    return _default_demo_state(app)


def _has_user_plan_access(state: dict[str, Any], *, symbol: str | None = None, order_id: int | None = None, request: Request | None = None) -> bool:
    user_id = _current_user_id(request=request, state=state)
    if not user_id:
        return True
    if symbol is not None:
        for plan in state.get("plans", {}).values():
            if str(plan.get("symbol") or "").upper() == str(symbol).upper() and str(plan.get("user_id") or plan.get("_user_id") or "").strip() == user_id:
                return True
        return False
    if order_id is not None:
        for plan in state.get("plans", {}).values():
            if int(plan.get("entry_order_id") or 0) == int(order_id) and str(plan.get("user_id") or plan.get("_user_id") or "").strip() == user_id:
                return True
        return False
    return True


def armed(state: dict[str, Any]) -> bool:
    return float(state.get("armed_until", 0)) > time.time()


def add_event(state: dict[str, Any], kind: str, message: str) -> None:
    state.setdefault("events", []).insert(0, {"kind": kind, "message": message, "created_at": utc_now()})
    del state["events"][40:]


def public_status(state: dict[str, Any]) -> dict[str, Any]:
    active = armed(state)
    if not active:
        state["armed_until"] = 0
    return {
        "version": "21.0.0",
        "mode": "BINANCE_FUTURES_DEMO_ONLY",
        "configured": credentials_configured(),
        "connected": bool(state.get("connected")),
        "armed": active,
        "armed_until": datetime.fromtimestamp(state["armed_until"], timezone.utc).isoformat() if active else None,
        "rest_host": DEMO_REST_BASE,
        "websocket_host": DEMO_WS_BASE,
        "real_trading_locked": True,
        "limits": {
            "max_margin_usdt": float(MAX_MARGIN_USDT),
            "max_leverage": MANUAL_MAX_LEVERAGE,
            "max_notional_usdt": float(MAX_NOTIONAL_USDT),
            "max_open_positions": MAX_OPEN_POSITIONS,
            "arm_minutes": ARM_SECONDS // 60,
        },
        "last_checked": state.get("last_checked"),
        "last_error": state.get("last_error"),
        "events": state.get("events", [])[:12],
        "reconciliation": state.get("reconciliation", {
            "actual_exchange_open_positions": 0,
            "internal_active_plans": 0,
            "reconciled_active_positions": 0,
            "stale_positions_removed": 0,
        }),
    }


def position_risk_summary(payload: Any) -> dict[str, Any]:
    """Classify raw Binance positionRisk rows without consulting local state."""
    diagnostics = []
    actual_count = 0
    for item in response_rows(payload):
        amount = position_amount(item.get("positionAmt"))
        is_actual = amount != 0
        if is_actual:
            actual_count += 1
        diagnostics.append({
            "symbol": str(item.get("symbol") or "").upper(),
            "positionAmt": str(amount),
            "positionSide": str(item.get("positionSide") or "BOTH").upper(),
            "markPrice": str(item.get("markPrice") or "0"),
            "entryPrice": str(item.get("entryPrice") or "0"),
            "unrealizedProfit": str(item.get("unRealizedProfit", item.get("unrealizedProfit", "0")) or "0"),
            "exchange_actual_position": is_actual,
        })
    return {
        "raw_position_risk_count": len(diagnostics),
        "actual_exchange_open_positions": actual_count,
        "exchange_position_diagnostics": diagnostics,
    }


def _sync_v21_automation_trade(state: dict[str, Any], plan: dict[str, Any]) -> None:
    """Keep the V21 activity record aligned with the exchange-owned plan."""
    application = _state_application(state)
    if application is None:
        return
    user_id = str(state.get("_user_id") or "").strip()
    if user_id:
        v21_store = getattr(application.state, "_v21_demo_user_state", {})
        v21_state = v21_store.get(user_id) if isinstance(v21_store, dict) else None
    else:
        v21_state = getattr(application.state, "v21_demo", None)
    if not isinstance(v21_state, dict):
        return
    trades = v21_state.get("automation_trades", [])
    if not isinstance(trades, list):
        return
    plan_id = str(plan.get("id") or plan.get("position_id") or "")
    candidates = [
        trade for trade in trades
        if plan_id and str(trade.get("plan_id") or "") == plan_id
    ]
    if not candidates:
        candidates = [
            trade for trade in trades
            if str(trade.get("symbol") or "").upper() == str(plan.get("symbol") or "").upper()
            and str(trade.get("status") or "").upper() not in {"KAPANDI", "CLOSED", "İPTAL"}
        ][:1]
    for trade in candidates:
        trade["status"] = plan.get("status", trade.get("status"))
        trade["position_status"] = plan.get("position_status")
        if plan.get("closed_at"):
            trade["closed_at"] = plan["closed_at"]


def reconcile_demo_plans(state: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    """Make durable plans follow the exchange position snapshot, never vice versa."""
    actual_positions = [
        position for position in snapshot.get("positions", [])
        if str(position.get("symbol") or "") and float(position.get("quantity") or 0) > 0
    ]
    actual_by_symbol = {str(position["symbol"]): position for position in actual_positions}
    active_statuses = {"OPEN", "KORUMA ONARILDI", "KORUMA AKTİF", "STOP AKTİF · HEDEF İZLEME"}
    internal_active = 0
    stale_removed = 0
    changed = False
    for plan in state.get("plans", {}).values():
        if plan.get("status") not in active_statuses and plan.get("position_status") != "OPEN":
            continue
        internal_active += 1
        if not can_mutate_lifecycle(plan):
            continue
        actual = actual_by_symbol.get(str(plan.get("symbol") or ""))
        if actual is None:
            if _within_plan_reconciliation_grace(plan):
                continue
            plan.update({"status": "KAPANDI", "position_status": "CLOSED", "remaining_quantity": "0", "closed_at": plan.get("closed_at") or utc_now(), "last_reconciled": utc_now()})
            if "_sync_v21_automation_trade" in globals():
                _sync_v21_automation_trade(state, plan)
            stale_removed += 1
            changed = True
            continue
        amount = Decimal(str(actual.get("quantity") or 0))
        before = (plan.get("remaining_quantity"), plan.get("position_status"))
        update_position_lifecycle(plan, amount)
        plan["last_reconciled"] = utc_now()
        if "_sync_v21_automation_trade" in globals():
            _sync_v21_automation_trade(state, plan)
        changed = changed or before != (plan.get("remaining_quantity"), plan.get("position_status"))
    state["reconciliation"] = {
        "actual_exchange_open_positions": int(snapshot.get("actual_exchange_open_positions", len(actual_positions))),
        "internal_active_plans": internal_active - stale_removed,
        "reconciled_active_positions": len(actual_positions),
        "stale_positions_removed": stale_removed,
        "last_sync": utc_now(),
    }
    return {"changed": changed, **state["reconciliation"]}


async def _cleanup_reconciled_closed_plans(
    client: BinanceDemoClient,
    state: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    reconciliation_changed: bool = False,
) -> bool:
    actual_symbols = {
        str(position.get("symbol") or "").upper()
        for position in snapshot.get("positions", [])
        if isinstance(position, dict) and float(position.get("quantity") or 0) > 0
    }
    plans = [candidate for candidate in state.get("plans", {}).values() if isinstance(candidate, dict)]
    changed = False
    for plan in plans:
        symbol = str(plan.get("symbol") or "").upper()
        if not symbol or symbol in actual_symbols or not _has_protection_identity(plan):
            continue
        if plan.get("status") in {"İPTAL", "GÜVENLİK İÇİN KAPATILDI"}:
            continue
        if plan.get("status") == "KAPANDI" and not reconciliation_changed:
            continue
        if plan.get("status") != "KAPANDI" and can_mutate_lifecycle(plan):
            continue
        cleaned = await cleanup_closed_plan(
            client,
            plan,
            allow_closed_position_cleanup=True,
            plans=plans,
        )
        if not cleaned:
            continue
        plan.update({
            "status": "KAPANDI",
            "position_status": "CLOSED",
            "remaining_quantity": "0",
            "closed_at": plan.get("closed_at") or utc_now(),
        })
        changed = True
    return changed


def _within_plan_reconciliation_grace(plan: dict[str, Any]) -> bool:
    protected_at = plan.get("protected_at")
    if not protected_at:
        return False
    try:
        protected_epoch = datetime.fromisoformat(str(protected_at).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return False
    age = time.time() - protected_epoch
    return 0 <= age < PLAN_RECONCILIATION_GRACE_SECONDS


def safe_exchange_error(exc: BinanceDemoError) -> HTTPException:
    suffix = f" (Demo kodu: {exc.exchange_code})" if exc.exchange_code is not None else ""
    return HTTPException(status_code=exc.http_status, detail=f"{exc}{suffix}")


def verify_leverage_response(payload: Any, symbol: str, requested: int) -> dict[str, Any]:
    """Fail closed unless Binance confirms the exact requested leverage."""
    if not isinstance(payload, dict):
        raise BinanceDemoError("Binance Demo kaldıraç doğrulama yanıtı okunamadı; emir gönderilmedi.", http_status=409)
    response_symbol = str(payload.get("symbol") or "").upper()
    try:
        applied = int(payload.get("leverage"))
    except (TypeError, ValueError):
        applied = 0
    if response_symbol != symbol or applied != requested:
        raise BinanceDemoError(
            f"Binance Demo {requested}x yerine {applied or 'belirsiz'}x bildirdi; güvenlik için emir gönderilmedi.",
            http_status=409,
        )
    return {
        "symbol": symbol,
        "requested_leverage": requested,
        "applied_leverage": applied,
        "max_notional_value": str(payload.get("maxNotionalValue") or ""),
        "leverage_verified": True,
    }


def verify_symbol_configuration(payload: Any, symbol: str, requested: int) -> dict[str, Any]:
    """Verify leverage and isolated margin from the account symbol configuration."""
    row = next((item for item in response_rows(payload) if str(item.get("symbol") or "").upper() == symbol), None)
    if row is None:
        raise BinanceDemoError(f"{symbol} hesap yapılandırması Binance Demo'dan doğrulanamadı; emir gönderilmedi.", http_status=409)
    try:
        applied = int(row.get("leverage"))
    except (TypeError, ValueError):
        applied = 0
    margin_type = str(row.get("marginType") or "").upper()
    if applied != requested or margin_type != "ISOLATED":
        raise BinanceDemoError(
            f"{symbol} güvenlik ayarı uyuşmadı: istenen {requested}x ISOLATED, uygulanan {applied or 'belirsiz'}x {margin_type or 'belirsiz'}; emir gönderilmedi.",
            http_status=409,
        )
    return {
        "symbol": symbol,
        "requested_leverage": requested,
        "applied_leverage": applied,
        "margin_type": "isolated",
        "max_notional_value": str(row.get("maxNotionalValue") or ""),
        "leverage_verified": True,
        "configuration_source": "BINANCE_SYMBOL_CONFIG",
    }


async def set_isolated_margin(client: BinanceDemoClient, symbol: str) -> None:
    """Force isolated margin; Binance code -4046 means it is already isolated."""
    try:
        await client.signed("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "ISOLATED"})
    except BinanceDemoError as exc:
        if exc.exchange_code != -4046:
            raise


async def apply_verified_leverage(client: BinanceDemoClient, symbol: str, requested: int) -> dict[str, Any]:
    response = await client.signed("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": requested})
    verify_leverage_response(response, symbol, requested)
    configuration = await client.signed("GET", "/fapi/v1/symbolConfig", {"symbol": symbol})
    return verify_symbol_configuration(configuration, symbol, requested)


async def position_mode(client: BinanceDemoClient) -> bool:
    payload = await client.signed("GET", "/fapi/v1/positionSide/dual")
    value = payload.get("dualSidePosition", payload.get("dualPosition", False))
    return value is True or str(value).lower() == "true"


async def ensure_one_way_position_mode(client: BinanceDemoClient) -> int:
    """Read the mode before an order; never change it as part of order submission."""
    if not DEMO_REST_BASE.endswith("demo-fapi.binance.com"):
        raise BinanceDemoError("Demo sunucu kilidi doğrulanamadı.", http_status=500)
    if not await position_mode(client):
        return 0

    positions = await client.signed("GET", "/fapi/v3/positionRisk")
    orders = await client.signed("GET", "/fapi/v1/openOrders")
    algo_orders = await client.signed("GET", "/fapi/v1/openAlgoOrders")
    actual_positions = [item for item in response_rows(positions) if position_amount(item.get("positionAmt")) != 0]
    if actual_positions or response_rows(orders) or response_rows(algo_orders):
        raise BinanceDemoError(
            "Demo hesap Hedge Mode'da ve açık emir/pozisyon var; position mode değiştirilmedi.",
            http_status=409,
        )
    raise BinanceDemoError(
        "Demo hesap Hedge Mode'da; yeni emir öncesi position mode otomatik değiştirilmedi.",
        http_status=409,
    )


async def optional_symbol_configurations(client: BinanceDemoClient) -> Any:
    """Keep read-only account visibility if an older Demo deployment lacks this endpoint."""
    try:
        return await client.signed("GET", "/fapi/v1/symbolConfig")
    except BinanceDemoError:
        return []


async def optional_open_algo_orders(client: BinanceDemoClient) -> Any:
    """Keep account/ARM snapshots usable when Demo omits the algo-order read endpoint."""
    try:
        return await client.signed("GET", "/fapi/v1/openAlgoOrders")
    except BinanceDemoError:
        return []


async def snapshot_request(client: BinanceDemoClient, path: str, request_id: str) -> Any:
    started = time.monotonic()
    trace_log(f"account_snapshot.{path}.start", request_id)
    try:
        result = await client.signed("GET", path)
    except Exception as exc:
        trace_log(
            f"account_snapshot.{path}.end",
            request_id,
            duration_ms=round((time.monotonic() - started) * 1000, 2),
            success=False,
            http_status=getattr(client, "last_status_code", None),
            error_type=type(exc).__name__,
            error_message=safe_trace_error(exc, client),
        )
        raise
    trace_log(
        f"account_snapshot.{path}.end",
        request_id,
        duration_ms=round((time.monotonic() - started) * 1000, 2),
        success=True,
        http_status=getattr(client, "last_status_code", None),
    )
    return result


async def account_snapshot(
    client: BinanceDemoClient,
    request_id: str | None = None,
    evidence_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    correlation_id = request_id or f"local-{uuid.uuid4().hex[:16]}"
    wait_started = time.monotonic()
    await DEMO_SNAPSHOT_LOCK.acquire()
    lock_wait_ms = round((time.monotonic() - wait_started) * 1000, 2)
    if lock_wait_ms > 0:
        trace_log("account_snapshot.lock", correlation_id, lock_wait_ms=lock_wait_ms)
    try:
        started = time.monotonic()
        trace_log("account_snapshot.start", correlation_id)
        try:
            result = await _account_snapshot(client, correlation_id, evidence_state=evidence_state)
        except Exception as exc:
            trace_log(
                "account_snapshot.end",
                correlation_id,
                duration_ms=round((time.monotonic() - started) * 1000, 2),
                success=False,
                error_type=type(exc).__name__,
                error_message=safe_trace_error(exc, client),
            )
            raise
        trace_log("account_snapshot.end", correlation_id, duration_ms=round((time.monotonic() - started) * 1000, 2), success=True)
        return result
    finally:
        DEMO_SNAPSHOT_LOCK.release()


async def _account_snapshot(
    client: BinanceDemoClient,
    request_id: str | None = None,
    evidence_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # Keep private snapshot reads sequential on the shared HTTP client.  This
    # avoids a burst of signed requests competing with the protection loop for
    # the same Render connection pool.
    correlation_id = request_id or f"local-{uuid.uuid4().hex[:16]}"
    account = await snapshot_request(client, "/fapi/v3/account", correlation_id)
    positions = await snapshot_request(client, "/fapi/v3/positionRisk", correlation_id)
    if evidence_state is not None:
        observe_position_snapshot(evidence_state, response_rows(positions))
    orders = await snapshot_request(client, "/fapi/v1/openOrders", correlation_id)
    open_algo_orders_available = True
    try:
        algo_orders = await snapshot_request(client, "/fapi/v1/openAlgoOrders", correlation_id)
    except BinanceDemoError:
        algo_orders = []
        open_algo_orders_available = False
    hedge_payload = await snapshot_request(client, "/fapi/v1/positionSide/dual", correlation_id)
    hedge_value = hedge_payload.get("dualSidePosition", hedge_payload.get("dualPosition", False))
    hedge_mode = hedge_value is True or str(hedge_value).lower() == "true"
    try:
        configurations = await snapshot_request(client, "/fapi/v1/symbolConfig", correlation_id)
    except BinanceDemoError:
        configurations = []
    config_by_symbol = {
        str(item.get("symbol") or "").upper(): item
        for item in response_rows(configurations)
        if item.get("symbol")
    }
    position_risk = position_risk_summary(positions)
    logger.info(
        "DEMO_POSITION_RECONCILIATION raw_position_risk_count=%s actual_exchange_open_positions=%s entries=%s",
        position_risk["raw_position_risk_count"],
        position_risk["actual_exchange_open_positions"],
        position_risk["exchange_position_diagnostics"],
    )
    open_positions = []
    provenance_positions = []
    for item in response_rows(positions):
        amount = position_amount(item.get("positionAmt"))
        symbol = str(item.get("symbol") or "").upper()
        if amount == 0:
            continue
        provenance_positions.append({
            "symbol": symbol,
            "position_side": str(item.get("positionSide") or "BOTH").upper(),
            "quantity": decimal_text(abs(amount)),
        })
        configuration = config_by_symbol.get(symbol, {})
        raw_leverage = item.get("leverage", configuration.get("leverage"))
        raw_margin_type = item.get("marginType", configuration.get("marginType"))
        try:
            leverage = int(raw_leverage) if raw_leverage is not None else None
        except (TypeError, ValueError):
            leverage = None
        margin_type = str(raw_margin_type).lower() if raw_margin_type else None
        open_positions.append({
            "symbol": symbol,
            "position_side": str(item.get("positionSide") or "BOTH").upper(),
            "direction": "LONG" if amount > 0 else "SHORT",
            "quantity": abs(float(amount)),
            "entry_price": float(item.get("entryPrice", 0)),
            "mark_price": float(item.get("markPrice", 0)),
            "liquidation_price": float(item.get("liquidationPrice", 0)),
            "unrealized_pnl": float(item.get("unRealizedProfit", item.get("unrealizedProfit", 0))),
            "leverage": leverage,
            "margin_type": margin_type,
            "requested_leverage": None,
            "applied_leverage": leverage,
            "leverage_verified": bool(configuration and leverage and margin_type),
            "configuration_source": "BINANCE_SYMBOL_CONFIG" if configuration else "UNAVAILABLE",
        })
    open_orders = [{
        "symbol": item.get("symbol"),
        "order_id": int(item.get("orderId", 0)),
        "client_order_id": item.get("clientOrderId"),
        "side": item.get("side"),
        "type": item.get("type"),
        "status": item.get("status"),
        "price": float(item.get("price", 0)),
        "quantity": float(item.get("origQty", 0)),
        "executed_quantity": float(item.get("executedQty", 0)),
        "reduce_only": bool(item.get("reduceOnly", False)),
    } for item in response_rows(orders)]
    open_algos = [{
        "symbol": item.get("symbol"),
        "algo_id": int(item.get("algoId", 0)),
        "client_algo_id": item.get("clientAlgoId"),
        "side": item.get("side"),
        "type": item.get("orderType", item.get("type")),
        "status": item.get("algoStatus", item.get("status")),
        "trigger_price": float(item.get("triggerPrice", item.get("stopPrice", 0))),
        "quantity": float(item.get("quantity", item.get("origQty", 0)) or 0),
        "close_position": str(item.get("closePosition", "false")).lower() == "true",
    } for item in response_rows(algo_orders)]
    return {
        "wallet_balance": float(account.get("totalWalletBalance", 0)),
        "available_balance": float(account.get("availableBalance", 0)),
        "margin_balance": float(account.get("totalMarginBalance", 0)),
        "unrealized_pnl": float(account.get("totalUnrealizedProfit", 0)),
        "positions": open_positions,
        "_provenance_positions": provenance_positions,
        "open_orders": open_orders,
        "open_algo_orders": open_algos,
        "open_algo_orders_available": open_algo_orders_available,
        "hedge_mode": hedge_mode,
        **position_risk,
    }


async def connect_snapshot(client: BinanceDemoClient) -> dict[str, Any]:
    """Retry only the read-only CONNECT snapshot after a Binance clock rejection."""
    try:
        return await account_snapshot(client)
    except BinanceDemoError as exc:
        if exc.exchange_code != -1021:
            raise
        await client.sync_clock(force=True)
        return await account_snapshot(client)


def enrich_snapshot_with_plans(snapshot: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Add the local request audit without inventing missing exchange values."""
    plans = list(state.get("plans", {}).values())
    active_by_symbol: dict[str, dict[str, Any]] = {}
    for plan in plans:
        if plan.get("status") not in {"KAPANDI", "İPTAL", "GÜVENLİK İÇİN KAPATILDI", "ACİL DURDURULDU"}:
            active_by_symbol[str(plan.get("symbol") or "")] = plan
    for position in snapshot.get("positions", []):
        plan = active_by_symbol.get(str(position.get("symbol") or ""))
        if not plan:
            continue
        requested = int(plan.get("requested_leverage") or plan.get("leverage") or 0) or None
        applied = int(plan.get("applied_leverage") or 0) or position.get("leverage")
        position["requested_leverage"] = requested
        position["applied_leverage"] = position.get("leverage") or applied
        if position.get("leverage") is None and applied:
            position["leverage"] = applied
            position["configuration_source"] = "VERIFIED_LEVERAGE_RESPONSE"
        if not position.get("margin_type") and plan.get("margin_type"):
            position["margin_type"] = plan["margin_type"]
        position["leverage_verified"] = bool(
            position.get("leverage_verified")
            or (
                plan.get("leverage_verified")
                and requested == position.get("leverage")
                and str(position.get("margin_type") or "").lower() == "isolated"
            )
        )
    return snapshot


async def symbol_rules(client: BinanceDemoClient, symbol: str) -> dict[str, Decimal]:
    payload = await client.public_get("/fapi/v1/exchangeInfo")
    row = next((item for item in payload.get("symbols", []) if item.get("symbol") == symbol), None)
    if (
        not row
        or row.get("status") != "TRADING"
        or row.get("contractType") != "PERPETUAL"
        or row.get("quoteAsset") != "USDT"
    ):
        raise BinanceDemoError(f"{symbol} Demo USDT perpetual market olarak geçerli değil.", http_status=422)
    filters = {item.get("filterType"): item for item in row.get("filters", [])}
    lot = filters.get("LOT_SIZE", {})
    price_filter = filters.get("PRICE_FILTER", {})
    notional_filter = filters.get("MIN_NOTIONAL", {})
    return {
        "step": Decimal(str(lot.get("stepSize", "0.001"))),
        "min_qty": Decimal(str(lot.get("minQty", "0"))),
        "max_qty": Decimal(str(lot.get("maxQty", "999999999"))),
        "tick": Decimal(str(price_filter.get("tickSize", "0.01"))),
        "min_notional": Decimal(str(notional_filter.get("notional", "0"))),
    }


async def ticker_price(client: BinanceDemoClient, symbol: str) -> Decimal:
    payload = await client.public_get("/fapi/v1/ticker/price", {"symbol": symbol})
    price = Decimal(str(payload.get("price", "0")))
    if price <= 0:
        raise BinanceDemoError("Demo piyasa fiyatı alınamadı.")
    return price


def validate_levels(direction: str, entry: Decimal, stop: Decimal, targets: list[Decimal]) -> None:
    if direction == "LONG":
        valid = stop < entry < targets[0] < targets[1] < targets[2]
        message = "LONG için sıralama Stop < Giriş < TP1 < TP2 < TP3 olmalı."
    else:
        valid = targets[2] < targets[1] < targets[0] < entry < stop
        message = "SHORT için sıralama TP3 < TP2 < TP1 < Giriş < Stop olmalı."
    if not valid:
        raise BinanceDemoError(message, http_status=422)


async def build_order_spec(client: BinanceDemoClient, order: DemoOrderRequest) -> dict[str, Any]:
    symbol = await resolve_demo_symbol(client, order.symbol)
    current_price, rules = await asyncio.gather(ticker_price(client, symbol), symbol_rules(client, symbol))
    if order.order_type == "LIMIT" and order.limit_price is None:
        raise BinanceDemoError("Limit emir için limit fiyatı zorunludur.", http_status=422)
    entry = Decimal(str(order.limit_price)) if order.order_type == "LIMIT" else current_price
    margin = Decimal(str(order.margin_usdt))
    notional = margin * Decimal(order.leverage)
    if margin > MAX_MARGIN_USDT or order.leverage > MANUAL_MAX_LEVERAGE or notional > MAX_NOTIONAL_USDT:
        raise BinanceDemoError(
            f"Demo güvenlik tavanı: en fazla 100 USDT marjin, {MANUAL_MAX_LEVERAGE}x kaldıraç ve {decimal_text(MAX_NOTIONAL_USDT)} USDT notional.",
            http_status=422,
        )
    quantity = floor_step(notional / entry, rules["step"])
    minimum_notional = max(rules["min_notional"], rules["min_qty"] * entry)
    if quantity < rules["min_qty"] or quantity * entry < rules["min_notional"]:
        min_margin = minimum_notional / Decimal(order.leverage)
        raise BinanceDemoError(
            f"{symbol} için borsa minimumu bu güvenlik tavanını aşıyor. Yaklaşık en az {decimal_text(min_margin.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))} USDT marjin gerekir; daha düşük fiyatlı bir parite seçin.",
            http_status=422,
        )
    if quantity > rules["max_qty"]:
        raise BinanceDemoError("Hesaplanan miktar borsa üst sınırını aşıyor.", http_status=422)
    stop = round_tick(Decimal(str(order.stop_loss)), rules["tick"])
    targets = [round_tick(Decimal(str(value)), rules["tick"]) for value in (order.tp1, order.tp2, order.tp3)]
    limit_price = round_tick(entry, rules["tick"])
    validate_levels(order.direction, limit_price if order.order_type == "LIMIT" else current_price, stop, targets)
    return {
        "symbol": symbol,
        "direction": order.direction,
        "side": "BUY" if order.direction == "LONG" else "SELL",
        "close_side": "SELL" if order.direction == "LONG" else "BUY",
        "order_type": order.order_type,
        "margin_usdt": float(margin),
        "leverage": order.leverage,
        "notional_usdt": float(quantity * entry),
        "quantity": decimal_text(quantity),
        "quantity_decimal": quantity,
        "current_price": float(current_price),
        "entry_price": decimal_text(limit_price),
        "stop_loss": decimal_text(stop),
        "targets": [decimal_text(value) for value in targets],
        "step": rules["step"],
        "min_qty": rules["min_qty"],
        "min_notional": float(minimum_notional),
    }


def runtime_payload(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "plans": state.get("plans", {}),
        "events": state.get("events", []),
        "connected": bool(state.get("connected")),
        "armed_until": state.get("armed_until", 0),
        "last_checked": state.get("last_checked"),
        "last_error": state.get("last_error"),
        "reconciliation": state.get("reconciliation", {}),
        "saved_at": utc_now(),
        "user_id": str(state.get("_user_id") or ""),
    }


def new_client_id(kind: str) -> str:
    return f"{CLIENT_PREFIX}{kind}_{uuid.uuid4().hex[:18]}"


def _protection_lock_key(plan: dict[str, Any]) -> str:
    return f"{plan.get('user_id') or ''}:{plan.get('id') or id(plan)}"


def _has_protection_identity(plan: dict[str, Any]) -> bool:
    return bool(str(plan.get("id") or "").strip())


def _protection_install_lock(plan: dict[str, Any]) -> asyncio.Lock:
    key = _protection_lock_key(plan)
    lock = _PROTECTION_INSTALL_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _PROTECTION_INSTALL_LOCKS[key] = lock
    return lock


def protection_lock(plan: dict[str, Any]) -> asyncio.Lock:
    return _protection_install_lock(plan)


def _protection_cleanup_lock(plan: dict[str, Any]) -> asyncio.Lock:
    key = _protection_lock_key(plan)
    lock = _PROTECTION_CLEANUP_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _PROTECTION_CLEANUP_LOCKS[key] = lock
    return lock


async def find_order_by_client_id(client: BinanceDemoClient, symbol: str, client_id: str) -> dict[str, Any] | None:
    try:
        result = await client.signed("GET", "/fapi/v1/order", {"symbol": symbol, "origClientOrderId": client_id})
        return result if isinstance(result, dict) and result.get("orderId") else None
    except BinanceDemoError:
        return None


async def recover_pending_entry_intents(client: BinanceDemoClient, state: dict[str, Any]) -> bool:
    changed = False
    for plan in state.get("plans", {}).values():
        if plan.get("status") != "ENTRY_INTENT_PENDING" or not plan.get("entry_client_order_id"):
            continue
        found = await find_order_by_client_id(client, str(plan["symbol"]), str(plan["entry_client_order_id"]))
        if found is None:
            rows = response_rows(await client.signed("GET", "/fapi/v3/positionRisk", {"symbol": plan["symbol"]}))
            found = {"orderId": None, "clientOrderId": plan["entry_client_order_id"]} if any(position_amount(item.get("positionAmt")) != 0 for item in rows) else None
        if found is None:
            continue
        plan["entry_order_id"] = int(found.get("orderId") or 0) or None
        plan["status"] = "DOLUM BEKLİYOR"
        plan["recovered_at"] = utc_now()
        changed = True
    if changed:
        persist_runtime(state)
    return changed


async def submit_entry(client: BinanceDemoClient, spec: dict[str, Any], *, test_only: bool, client_id: str | None = None) -> dict[str, Any]:
    client_id = client_id or new_client_id("TEST" if test_only else "ENTRY")
    params: dict[str, Any] = {
        "symbol": spec["symbol"],
        "side": spec["side"],
        "type": spec["order_type"],
        "quantity": spec["quantity"],
        "newClientOrderId": client_id,
        "newOrderRespType": "RESULT",
    }
    if spec["order_type"] == "LIMIT":
        params.update({"price": spec["entry_price"], "timeInForce": "GTC"})
    path = "/fapi/v1/order/test" if test_only else "/fapi/v1/order"
    try:
        response = await client.signed("POST", path, params)
    except BinanceDemoError as exc:
        if not test_only and exc.unknown_execution:
            recovered = await find_order_by_client_id(client, spec["symbol"], client_id)
            if recovered is not None:
                return recovered
        raise
    return response if isinstance(response, dict) else {}


async def cancel_entry_if_open(client: BinanceDemoClient, plan: dict[str, Any]) -> None:
    order_id = plan.get("entry_order_id")
    if not order_id:
        return
    try:
        await client.signed("DELETE", "/fapi/v1/order", {"symbol": plan["symbol"], "orderId": order_id})
    except BinanceDemoError as exc:
        if exc.exchange_code not in {-2011, -2013}:
            raise


async def close_symbol_position(client: BinanceDemoClient, symbol: str, position_side: str = "BOTH") -> dict[str, Any] | None:
    rows = await client.signed("GET", "/fapi/v3/positionRisk", {"symbol": symbol})
    normalized_side = str(position_side or "BOTH").upper()
    row = next((item for item in response_rows(rows)
                if str(item.get("positionSide") or "BOTH").upper() == normalized_side
                and position_amount(item.get("positionAmt")) != 0), None)
    if row is None:
        return None
    amount = position_amount(row.get("positionAmt"))
    params = {
        "symbol": symbol,
        "side": "SELL" if amount > 0 else "BUY",
        "type": "MARKET",
        "quantity": decimal_text(abs(amount)),
        "reduceOnly": "true",
        "newClientOrderId": new_client_id("CLOSE"),
        "newOrderRespType": "RESULT",
    }
    if normalized_side != "BOTH":
        params["positionSide"] = normalized_side
        params.pop("reduceOnly", None)
    return await client.signed("POST", "/fapi/v1/order", params)


async def reduce_symbol_position(client: BinanceDemoClient, symbol: str, quantity: Decimal, position_side: str = "BOTH") -> dict[str, Any] | None:
    rows = await client.signed("GET", "/fapi/v3/positionRisk", {"symbol": symbol})
    normalized_side = str(position_side or "BOTH").upper()
    row = next((item for item in response_rows(rows)
                if str(item.get("positionSide") or "BOTH").upper() == normalized_side
                and position_amount(item.get("positionAmt")) != 0), None)
    if row is None:
        return None
    amount = abs(position_amount(row.get("positionAmt")))
    rules = await symbol_rules(client, symbol)
    rounded = floor_step(quantity, rules["step"])
    if rounded <= 0 or rounded > amount:
        raise BinanceDemoError("Reduce-only miktarı mevcut pozisyon miktarı içinde olmalı.", http_status=422)
    mark_price = Decimal(str(row.get("markPrice") or "0"))
    if rounded < rules["min_qty"] or rounded * mark_price < rules["min_notional"]:
        raise BinanceDemoError("Reduce-only miktarı borsa minimum miktar/notional kurallarını sağlamıyor.", http_status=422)
    raw_amount = position_amount(row.get("positionAmt"))
    params = {
        "symbol": symbol,
        "side": "SELL" if raw_amount > 0 else "BUY",
        "type": "MARKET",
        "quantity": decimal_text(rounded),
        "reduceOnly": "true",
        "newClientOrderId": new_client_id("REDUCE"),
        "newOrderRespType": "RESULT",
    }
    if normalized_side != "BOTH":
        params["positionSide"] = normalized_side
        params.pop("reduceOnly", None)
    return await client.signed("POST", "/fapi/v1/order", params)


def update_position_lifecycle(plan: dict[str, Any], amount: Decimal) -> None:
    """Keep the durable demo plan aligned with the exchange position amount."""
    if not can_mutate_lifecycle(plan):
        return
    remaining = abs(amount)
    plan["remaining_quantity"] = decimal_text(remaining)
    plan["position_status"] = "OPEN" if remaining else "CLOSED"


def inspect_protection_state(
    snapshot: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    """Inspect configured TP quantities without changing local or exchange state."""
    available = snapshot.get("open_algo_orders_available") is True
    result: dict[str, Any] = {
        "available": available,
        "quantity_inconsistent": False,
        "diagnostic": None,
        "inconsistencies": [],
    }
    if not available:
        result["diagnostic"] = "Binance algo-order snapshot unavailable."
        return result

    plans = state.get("plans", {})
    if not isinstance(plans, dict):
        return result

    for plan_key, plan in plans.items():
        if not isinstance(plan, dict):
            continue
        try:
            tp1_quantity = Decimal(str(plan["tp1_quantity"]))
            tp2_quantity = Decimal(str(plan["tp2_quantity"]))
            remaining_quantity = Decimal(str(plan["remaining_quantity"]))
        except (KeyError, TypeError, ValueError, InvalidOperation):
            continue
        if tp1_quantity + tp2_quantity <= remaining_quantity:
            continue

        plan_id = str(plan.get("id") or plan_key)
        result["quantity_inconsistent"] = True
        result["inconsistencies"].append({
            "plan_id": plan_id,
            "tp1_quantity": str(tp1_quantity),
            "tp2_quantity": str(tp2_quantity),
            "remaining_quantity": str(remaining_quantity),
        })
        logger.info(
            "[PROTECTION_STATE] quantity_inconsistent plan_id=%s tp1_quantity=%s tp2_quantity=%s remaining_quantity=%s",
            plan_id,
            tp1_quantity,
            tp2_quantity,
            remaining_quantity,
        )

    if result["quantity_inconsistent"]:
        result["diagnostic"] = "Configured TP quantities exceed remaining quantity."
    return result


def mark_cancelled_protection(
    plans: dict[str, Any],
    symbol: str,
    algo_id: int,
    *,
    owner: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    for plan in plans.values():
        if owner is not None and plan is not owner:
            continue
        if not can_mutate_lifecycle(plan) or plan.get("symbol") != symbol or int(plan.get("stop_algo_id") or 0) != algo_id:
            continue
        plan["stop_protection_cancelled"] = True
        plan["protection_status"] = "KORUMA İPTAL"
        plan["status"] = "KORUMA İPTAL"
        plan["last_error"] = "Kullanıcı STOP_MARKET koruma emrini iptal etti."
        return plan
    return None


def duplicate_entry_reason(snapshot: dict[str, Any], symbol: str) -> str | None:
    if any(item.get("symbol") == symbol for item in snapshot.get("positions", [])):
        return f"{symbol} için zaten açık pozisyon var; önce mevcut pozisyonu kapatın veya başka parite seçin."
    order = next((item for item in snapshot.get("open_orders", []) if item.get("symbol") == symbol), None)
    if order:
        order_id = order.get("orderId") or order.get("order_id")
        suffix = f" (emir: {order_id})" if order_id else ""
        return f"{symbol} için zaten açık normal emir var{suffix}; önce emri iptal edin veya başka parite seçin."
    return None


def _plan_is_active(plan: dict[str, Any]) -> bool:
    terminal_statuses = {"KAPANDI", "İPTAL", "CLOSED", "CANCELLED"}
    status = str(plan.get("status") or "").strip().upper()
    position_status = str(plan.get("position_status") or "").strip().upper()
    return status not in terminal_statuses and position_status != "CLOSED"


def _plan_protection_ids(plan: dict[str, Any]) -> set[int] | None:
    raw_ids = plan.get("protection_ids")
    if not isinstance(raw_ids, (list, tuple, set)):
        return None
    protection_ids: set[int] = set()
    for raw_id in raw_ids:
        if isinstance(raw_id, bool):
            return None
        if isinstance(raw_id, int):
            algo_id = raw_id
        elif isinstance(raw_id, str) and raw_id.strip():
            try:
                algo_id = int(raw_id.strip())
            except ValueError:
                return None
        else:
            return None
        if algo_id <= 0:
            return None
        if algo_id in protection_ids:
            return None
        protection_ids.add(algo_id)
    return protection_ids


def _owned_protection_orders(
    plan: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    plans: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    stop_algo_id = _single_plan_algo_id(plan, "stop_algo_id")
    if stop_algo_id is None:
        return []
    state, matched_ids, _missing_ids = _protection_classification(
        plan,
        snapshot,
        required_ids={stop_algo_id},
        plans=plans,
    )
    if state != "MATCHED" or stop_algo_id not in matched_ids:
        return []
    return [
        order for order in snapshot.get("open_algo_orders", [])
        if isinstance(order, dict) and _algo_id(order) == stop_algo_id
    ]


def _symbol_matches(value: Any, expected: str) -> bool:
    raw_symbol = str(value or "")
    if not raw_symbol:
        return False
    try:
        return normalize_symbol(raw_symbol) == expected
    except BinanceDemoError:
        return False


def _direct_position_identity_match(plan: dict[str, Any], position: dict[str, Any]) -> bool:
    identity_keys = ("exchange_position_id", "position_identity", "position_id")
    for key in identity_keys:
        plan_identity = str(plan.get(key) or "").strip()
        position_identity = str(position.get(key) or "").strip()
        if plan_identity and position_identity and plan_identity == position_identity:
            return True
    return False


def _single_plan_algo_id(plan: dict[str, Any], field: str) -> int | None:
    raw_id = plan.get(field)
    if raw_id in (None, ""):
        return None
    if isinstance(raw_id, bool):
        return None
    try:
        algo_id = int(raw_id)
    except (TypeError, ValueError):
        return None
    return algo_id if algo_id > 0 else None


def _protection_ownership_metadata_valid(plan: dict[str, Any]) -> bool:
    protection_ids = _plan_protection_ids(plan) if "protection_ids" in plan else set()
    if protection_ids is None:
        return False
    ownership_ids: list[int] = []
    for field in ("stop_algo_id", "tp1_algo_id", "tp2_algo_id", "tp3_algo_id"):
        if field not in plan or plan[field] in (None, ""):
            continue
        algo_id = _single_plan_algo_id(plan, field)
        if algo_id is None or algo_id in ownership_ids or algo_id not in protection_ids:
            return False
        ownership_ids.append(algo_id)
    return True


def _algo_id(order: dict[str, Any]) -> int | None:
    raw_id = order.get("algo_id", order.get("algoId"))
    try:
        algo_id = int(raw_id)
    except (TypeError, ValueError):
        return None
    return algo_id if algo_id > 0 else None


def _validate_new_protection_identity(
    plan: dict[str, Any],
    algo: dict[str, Any],
    plans: list[dict[str, Any]],
    *,
    expected_type: str,
    expected_side: str,
) -> int | None:
    if not isinstance(algo, dict):
        return None
    if not _protection_ownership_metadata_valid(plan):
        return None
    new_id = _algo_id(algo)
    if new_id is None:
        return None
    current_ids = set() if "protection_ids" not in plan else _plan_protection_ids(plan)
    if current_ids is None or new_id in current_ids:
        return None
    for candidate in plans:
        if candidate is plan or not isinstance(candidate, dict) or not _plan_is_active(candidate):
            continue
        if not _protection_ownership_metadata_valid(candidate):
            return None
        candidate_ids = _plan_protection_ids(candidate)
        if new_id in candidate_ids:
            return None
    if not _symbol_matches(algo.get("symbol"), str(plan.get("symbol") or "").upper()):
        return None
    actual_type = algo.get("type", algo.get("orderType"))
    if str(actual_type or "").upper() != expected_type:
        return None
    if str(algo.get("side") or "").upper() != expected_side:
        return None
    actual_status = algo.get("status", algo.get("algoStatus"))
    if str(actual_status or "").upper() not in {"NEW", "WORKING", "PENDING_NEW", "PARTIALLY_FILLED"}:
        return None
    return new_id


def _protection_classification(
    plan: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    required_ids: set[int] | None = None,
    plans: list[dict[str, Any]] | None = None,
) -> tuple[str, list[int], list[int]]:
    if snapshot.get("open_algo_orders_available") is not True:
        return "UNKNOWN", [], []
    protection_ids = set() if "protection_ids" not in plan else _plan_protection_ids(plan)
    if protection_ids is None or not _protection_ownership_metadata_valid(plan):
        return "UNKNOWN", [], []
    orders = snapshot.get("open_algo_orders")
    if not isinstance(orders, list):
        return "UNKNOWN", [], []
    expected_symbol = str(plan.get("symbol") or "").upper()
    if not expected_symbol:
        return "UNKNOWN", [], []
    expected_side = "SELL" if str(plan.get("direction") or "").upper() == "LONG" else "BUY"
    required = set(protection_ids if required_ids is None else required_ids)
    if not required.issubset(protection_ids):
        return "UNKNOWN", [], []
    if plans is not None:
        for candidate in plans:
            if candidate is plan or not isinstance(candidate, dict) or not _plan_is_active(candidate):
                continue
            if not _protection_ownership_metadata_valid(candidate):
                return "UNKNOWN", [], []
            candidate_ids = _plan_protection_ids(candidate)
            if protection_ids.intersection(candidate_ids):
                return "UNKNOWN", [], []
    seen_ids: set[int] = set()
    parsed_orders: list[tuple[int, dict[str, Any]]] = []
    for order in orders:
        if not isinstance(order, dict):
            return "UNKNOWN", [], []
        algo_id = _algo_id(order)
        if algo_id is None or algo_id in seen_ids:
            return "UNKNOWN", [], []
        seen_ids.add(algo_id)
        symbol = str(order.get("symbol") or "").upper()
        if algo_id in protection_ids and symbol != expected_symbol:
            return "UNKNOWN", [], []
        if symbol == expected_symbol:
            order_type = str(order.get("type") or "").upper()
            status = str(order.get("status") or "").upper()
            side = str(order.get("side") or "").upper()
            if not order_type or not status or not side:
                return "UNKNOWN", [], []
            parsed_orders.append((algo_id, order))

    active_statuses = {"NEW", "WORKING", "PENDING_NEW", "PARTIALLY_FILLED"}
    expected_stop_id = _single_plan_algo_id(plan, "stop_algo_id")
    same_symbol_stop_ids = {
        algo_id
        for algo_id, order in parsed_orders
        if str(order.get("type") or "").upper() == "STOP_MARKET"
    }
    if expected_stop_id is None:
        listed_stop_ids = same_symbol_stop_ids & protection_ids
        if len(listed_stop_ids) == 1:
            expected_stop_id = next(iter(listed_stop_ids))
        elif len(listed_stop_ids) > 1:
            return "UNKNOWN", [], []
    if expected_stop_id is None:
        if same_symbol_stop_ids:
            return "UNKNOWN", [], []
        if not required:
            return "MISSING", [], []
    if expected_stop_id is not None and expected_stop_id in same_symbol_stop_ids and any(
        algo_id != expected_stop_id for algo_id in same_symbol_stop_ids
    ):
        return "UNKNOWN", [], [expected_stop_id]
    matched_ids: list[int] = []
    missing_ids: list[int] = []
    for expected_id in sorted(required):
        matches = [order for algo_id, order in parsed_orders if algo_id == expected_id]
        if not matches:
            missing_ids.append(expected_id)
            continue
        order = matches[0]
        expected_type = "STOP_MARKET" if expected_id == expected_stop_id else "TAKE_PROFIT_MARKET"
        if (
            str(order.get("type") or "").upper() != expected_type
            or str(order.get("side") or "").upper() != expected_side
            or str(order.get("status") or "").upper() not in active_statuses
        ):
            return "UNKNOWN", [], [expected_id]
        matched_ids.append(expected_id)

    if missing_ids:
        return "MISSING", matched_ids, missing_ids
    return "MATCHED", matched_ids, []


async def _fresh_protection_snapshot(client: BinanceDemoClient, symbol: str) -> dict[str, Any]:
    try:
        payload = await client.signed("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol})
    except BinanceDemoError:
        return {"open_algo_orders_available": False, "open_algo_orders": []}
    orders: list[dict[str, Any]] = []
    for item in response_rows(payload):
        if not isinstance(item, dict):
            return {"open_algo_orders_available": True, "open_algo_orders": [item]}
        orders.append({
            "symbol": item.get("symbol"),
            "algo_id": item.get("algoId"),
            "client_algo_id": item.get("clientAlgoId"),
            "side": item.get("side"),
            "type": item.get("orderType", item.get("type")),
            "status": item.get("algoStatus", item.get("status")),
            "trigger_price": item.get("triggerPrice", item.get("stopPrice")),
        })
    return {"open_algo_orders_available": True, "open_algo_orders": orders}


def classify_demo_ownership(
    snapshot: dict[str, Any],
    demo_state: dict[str, Any],
    v21_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify Demo ownership without changing state or contacting the exchange."""
    plans = demo_state.get("plans", {})
    plans = plans if isinstance(plans, dict) else {}
    active_plans = [plan for plan in plans.values() if isinstance(plan, dict) and _plan_is_active(plan)]
    positions = [
        position for position in snapshot.get("positions", [])
        if isinstance(position, dict) and str(position.get("symbol") or "").strip()
    ]
    position_rows: list[dict[str, Any]] = []
    used_plan_ids: set[str] = set()
    used_position_indexes: set[int] = set()

    for plan in active_plans:
        plan_id = str(plan.get("id") or "").strip()
        matching_indexes = [
            index for index, position in enumerate(positions)
            if index not in used_position_indexes
            and _symbol_matches(position.get("symbol"), normalize_symbol(str(plan.get("symbol") or "")))
            and _direct_position_identity_match(plan, position)
        ]
        if matching_indexes:
            position_index = matching_indexes[0]
            position = positions[position_index]
            used_plan_ids.add(plan_id)
            used_position_indexes.add(position_index)
            protection_status, matched_ids, unmatched_ids = _protection_classification(
                plan, snapshot, plans=active_plans
            )
            classification = (
                "PROTECTION_UNKNOWN"
                if protection_status == "UNKNOWN"
                else "PLAN_AND_EXCHANGE_MATCHED"
            )
            position_rows.append({
                "classification": classification,
                "symbol": normalize_symbol(str(plan.get("symbol") or "")),
                "plan_id": plan_id or None,
                "position_index": position_index,
                "protection_status": protection_status,
                "matched_algo_ids": matched_ids,
                "unmatched_algo_ids": unmatched_ids,
            })
            continue

        same_symbol_position = next(
            (
                (index, position) for index, position in enumerate(positions)
                if index not in used_position_indexes
                and _symbol_matches(position.get("symbol"), normalize_symbol(str(plan.get("symbol") or "")))
            ),
            None,
        )
        if same_symbol_position is None:
            classification = "PLAN_ONLY"
            position_index = None
        else:
            classification = "PROTECTION_UNKNOWN"
            position_index = same_symbol_position[0]
        position_rows.append({
            "classification": classification,
            "symbol": normalize_symbol(str(plan.get("symbol") or "")),
            "plan_id": plan_id or None,
            "position_index": position_index,
            "protection_status": "NOT_APPLICABLE" if classification == "PLAN_ONLY" else "UNKNOWN",
            "matched_algo_ids": [],
            "unmatched_algo_ids": [],
        })
        if plan_id:
            used_plan_ids.add(plan_id)

    for index, position in enumerate(positions):
        if index in used_position_indexes:
            continue
        symbol = normalize_symbol(str(position.get("symbol") or ""))
        position_rows.append({
            "classification": "EXCHANGE_ONLY",
            "symbol": symbol,
            "plan_id": None,
            "position_index": index,
            "protection_status": "NOT_APPLICABLE",
            "matched_algo_ids": [],
            "unmatched_algo_ids": [],
        })

    automation_rows: list[dict[str, Any]] = []
    automation_trades = (v21_state or {}).get("automation_trades", [])
    terminal_statuses = {
        "KAPANDI", "CLOSED", "İPTAL", "GÜVENLİK İÇİN KAPATILDI", "CANCELLED", "CANCELED",
        "EXPIRED", "FINISHED", "REJECTED",
    }
    for index, trade in enumerate(automation_trades if isinstance(automation_trades, list) else []):
        if not isinstance(trade, dict):
            continue
        status = str(trade.get("status") or "").strip().upper()
        if status in terminal_statuses:
            continue
        trade_plan_id = str(trade.get("plan_id") or "").strip()
        trade_symbol = str(trade.get("symbol") or "").strip()
        has_plan = any(str(plan.get("id") or "").strip() == trade_plan_id for plan in active_plans) if trade_plan_id else False
        has_position = any(_symbol_matches(position.get("symbol"), normalize_symbol(trade_symbol)) for position in positions) if trade_symbol else False
        automation_rows.append({
            "classification": "STALE_AUTOMATION_RECORD" if not has_plan and not has_position else "ASSOCIATED_AUTOMATION_RECORD",
            "trade_index": index,
            "plan_id": trade_plan_id or None,
            "symbol": normalize_symbol(trade_symbol) if trade_symbol else None,
        })

    return {
        "position_plan": position_rows,
        "automation": automation_rows,
    }


def _ownership_identity_value(value: Any) -> str | None:
    identity = str(value or "").strip()
    return identity or None


def build_demo_ownership_diagnostic(
    account_snapshot: dict[str, Any],
    demo_state: dict[str, Any],
    v21_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a deterministic, read-only ownership diagnostic from snapshots."""
    classification = classify_demo_ownership(account_snapshot, demo_state, v21_state)
    positions = [
        position for position in account_snapshot.get("positions", [])
        if isinstance(position, dict) and str(position.get("symbol") or "").strip()
    ]
    plans = demo_state.get("plans", {})
    plans = plans if isinstance(plans, dict) else {}
    active_plans = {
        str(plan.get("id") or plan_key).strip(): plan
        for plan_key, plan in plans.items()
        if isinstance(plan, dict) and _plan_is_active(plan)
    }
    position_rows = []
    for row in classification["position_plan"]:
        position_index = row.get("position_index")
        position = positions[position_index] if isinstance(position_index, int) and position_index < len(positions) else None
        plan = active_plans.get(str(row.get("plan_id") or ""))
        plan_identity = next(
            (_ownership_identity_value(plan.get(key)) for key in ("exchange_position_id", "position_identity", "position_id") if plan and _ownership_identity_value(plan.get(key))),
            None,
        )
        exchange_identity = next(
            (_ownership_identity_value(position.get(key)) for key in ("exchange_position_id", "position_identity", "position_id") if position and _ownership_identity_value(position.get(key))),
            None,
        )
        if plan_identity and exchange_identity:
            identity_evidence = "EXPLICIT_EQUAL" if plan_identity == exchange_identity else "MISMATCH"
        else:
            identity_evidence = "MISSING"
        protection_ids = sorted(_plan_protection_ids(plan) or []) if plan else []
        position_rows.append({
            **row,
            "identity": {
                "plan_identity": plan_identity,
                "exchange_identity": exchange_identity,
                "matched": identity_evidence == "EXPLICIT_EQUAL",
                "evidence": identity_evidence,
            },
            "protection": {
                "status": row.get("protection_status", "UNKNOWN"),
                "plan_protection_ids": protection_ids,
                "matched_algo_ids": list(row.get("matched_algo_ids") or []),
                "unmatched_plan_ids": list(row.get("unmatched_algo_ids") or []),
                "unassigned_exchange_algo_ids": [],
            },
        })

    raw_algo_orders = account_snapshot.get("open_algo_orders", [])
    raw_algo_orders = raw_algo_orders if isinstance(raw_algo_orders, list) else []
    exchange_algo_ids: set[int] = set()
    malformed_algo_ids = False
    for order in raw_algo_orders:
        if not isinstance(order, dict):
            malformed_algo_ids = True
            continue
        try:
            algo_id = int(order.get("algo_id"))
        except (TypeError, ValueError):
            malformed_algo_ids = True
            continue
        if algo_id <= 0:
            malformed_algo_ids = True
            continue
        exchange_algo_ids.add(algo_id)
    assigned_algo_ids = {
        algo_id
        for plan in active_plans.values()
        for algo_id in (_plan_protection_ids(plan) or set())
    }
    unassigned_algo_ids = sorted(exchange_algo_ids - assigned_algo_ids)
    for row in position_rows:
        row["protection"]["unassigned_exchange_algo_ids"] = unassigned_algo_ids

    automation_rows = []
    trades = (v21_state or {}).get("automation_trades", [])
    trades = trades if isinstance(trades, list) else []
    classified_by_index = {row["trade_index"]: row for row in classification["automation"]}
    terminal_statuses = {
        "KAPANDI", "CLOSED", "İPTAL", "GÜVENLİK İÇİN KAPATILDI", "CANCELLED", "CANCELED",
        "EXPIRED", "FINISHED", "REJECTED",
    }
    for index, trade in enumerate(trades):
        if not isinstance(trade, dict):
            continue
        status = str(trade.get("status") or "").strip().upper()
        if status in terminal_statuses:
            automation_rows.append({
                "classification": "TERMINAL_AUTOMATION_RECORD",
                "trade_index": index,
                "plan_id": str(trade.get("plan_id") or "").strip() or None,
                "symbol": str(trade.get("symbol") or "").strip() or None,
                "status": status or None,
                "terminal": True,
            })
            continue
        row = classified_by_index.get(index)
        if row is not None:
            automation_rows.append({**row, "status": status or None, "terminal": False})

    identity_keys = ("exchange_position_id", "position_identity", "position_id")
    identity_fields_available = bool(positions) and all(
        any(_ownership_identity_value(position.get(key)) for key in identity_keys)
        for position in positions
    )
    protection_unknown = account_snapshot.get("open_algo_orders_available") is not True or malformed_algo_ids
    protection_unknown = protection_unknown or any(
        row["protection"]["status"] == "UNKNOWN" for row in position_rows
    )
    position_counts = {
        "exchange_only": sum(row["classification"] == "EXCHANGE_ONLY" for row in position_rows),
        "plan_only": sum(row["classification"] == "PLAN_ONLY" for row in position_rows),
        "matched": sum(row["classification"] == "PLAN_AND_EXCHANGE_MATCHED" for row in position_rows),
        "protection_unknown": sum(row["classification"] == "PROTECTION_UNKNOWN" for row in position_rows),
    }
    automation_counts = {
        "active": sum(not row["terminal"] for row in automation_rows),
        "terminal": sum(row["terminal"] for row in automation_rows),
        "stale": sum(row["classification"] == "STALE_AUTOMATION_RECORD" for row in automation_rows),
        "associated": sum(row["classification"] == "ASSOCIATED_AUTOMATION_RECORD" for row in automation_rows),
    }
    uncertainty_reasons = []
    if positions and not identity_fields_available:
        uncertainty_reasons.append("Exchange positions do not expose explicit position identity.")
    if account_snapshot.get("open_algo_orders_available") is not True:
        uncertainty_reasons.append("Exchange algo-order snapshot is unavailable.")
    if malformed_algo_ids:
        uncertainty_reasons.append("At least one exchange algo order has a missing or invalid numeric algo ID.")
    if unassigned_algo_ids:
        uncertainty_reasons.append("Some exchange algo IDs are not assigned to an active Demo plan.")
    if any(_plan_protection_ids(plan) is None for plan in active_plans.values()):
        uncertainty_reasons.append("At least one active Demo plan has missing or malformed protection IDs.")

    return {
        "position_plan": {"rows": position_rows, "counts": position_counts},
        "automation": {"rows": automation_rows, "counts": automation_counts},
        "protection_evidence": {
            "orders_seen": len(raw_algo_orders),
            "orders_with_numeric_algo_id": len(exchange_algo_ids),
            "orders_with_client_id": sum(bool(order.get("client_algo_id")) for order in raw_algo_orders if isinstance(order, dict)),
            "assigned_algo_ids": sorted(assigned_algo_ids),
            "unassigned_algo_ids": unassigned_algo_ids,
            "unknown": protection_unknown,
        },
        "aggregate_counts": {
            "exchange_positions": len(positions),
            "active_demo_plans": len(active_plans),
            "open_algo_orders": len(raw_algo_orders),
            "automation_trades": len(trades),
        },
        "evidence": {
            "identity_fields_available": identity_fields_available,
            "protection_snapshot_available": account_snapshot.get("open_algo_orders_available") is True,
            "ownership_complete": bool(position_rows) and not uncertainty_reasons,
            "uncertainty_reasons": uncertainty_reasons,
        },
    }


def stale_protection_entry_reason(
    snapshot: dict[str, Any],
    state: dict[str, Any],
    symbol: str,
) -> str | None:
    """Return a fail-closed reason for same-symbol entry conflicts."""
    normalized_symbol = normalize_symbol(symbol)
    if snapshot.get("open_algo_orders_available") is not True:
        return (
            f"Entry blocked: Binance algo-order snapshot unavailable; "
            f"entry fail-closed for {normalized_symbol}."
        )

    if any(_symbol_matches(item.get("symbol"), normalized_symbol)
           for item in snapshot.get("positions", [])):
        return f"Entry blocked: active position already exists for {normalized_symbol}."

    if any(_symbol_matches(item.get("symbol"), normalized_symbol)
           for item in snapshot.get("open_orders", [])):
        return f"Entry blocked: active normal order already exists for {normalized_symbol}."

    plans = state.get("plans")
    if plans is None:
        plans = {}
    if not isinstance(plans, dict):
        return f"Entry blocked: protection ownership could not be verified for {normalized_symbol}."

    plan_rows: list[tuple[str, dict[str, Any], set[int] | None]] = []
    protection_references: dict[int, list[str]] = {}
    for plan_key, plan in plans.items():
        if not isinstance(plan, dict):
            return f"Entry blocked: protection ownership could not be verified for {normalized_symbol}."
        plan_id = str(plan.get("id") or plan_key)
        protection_ids = _plan_protection_ids(plan)
        plan_rows.append((plan_id, plan, protection_ids))
        if protection_ids is None:
            continue
        for algo_id in protection_ids:
            protection_references.setdefault(algo_id, []).append(plan_id)

    duplicate_ids = {algo_id for algo_id, references in protection_references.items() if len(references) > 1}
    terminal_algo_statuses = {"CANCELED", "CANCELLED", "EXPIRED", "FINISHED", "REJECTED"}
    active_algos = []
    for algo in snapshot.get("open_algo_orders", []):
        if not str(algo.get("symbol") or ""):
            return f"Entry blocked: protection ownership could not be verified for {normalized_symbol}."
        if not _symbol_matches(algo.get("symbol"), normalized_symbol):
            continue
        status = str(algo.get("status") or "").strip().upper()
        if status not in terminal_algo_statuses:
            active_algos.append(algo)

    for algo in active_algos:
        try:
            algo_id = int(algo.get("algo_id"))
        except (TypeError, ValueError):
            return f"Entry blocked: protection ownership could not be verified for {normalized_symbol}."
        if algo_id <= 0:
            return f"Entry blocked: protection ownership could not be verified for {normalized_symbol}."
        references = protection_references.get(algo_id, [])
        if algo_id in duplicate_ids:
            return f"Entry blocked: protection ownership is ambiguous for {normalized_symbol}."
        if len(references) != 1:
            return f"Entry blocked: active protection/algo order detected for {normalized_symbol}."
        matching_plan = next((plan for plan_id, plan, _ in plan_rows if plan_id == references[0]), None)
        if matching_plan is None:
            return f"Entry blocked: protection ownership could not be verified for {normalized_symbol}."
        if not _symbol_matches(matching_plan.get("symbol"), normalized_symbol):
            return f"Entry blocked: protection ownership could not be verified for {normalized_symbol}."
        if not _plan_is_active(matching_plan):
            return f"Entry blocked: active protection belongs to a closed plan for {normalized_symbol}."
        return f"Entry blocked: active protection/algo order detected for {normalized_symbol}."

    for _, plan, _ in plan_rows:
        if not _symbol_matches(plan.get("symbol"), normalized_symbol):
            continue
        if _plan_is_active(plan):
            return f"Entry blocked: active internal plan already exists for {normalized_symbol}."

    return None


def validate_entry_risk(
    snapshot: dict[str, Any],
    body: DemoOrderRequest,
    spec: dict[str, Any],
    settings: dict[str, Any],
    *,
    daily_realized_pnl: float,
    paper_positions: list[dict[str, Any]] | None = None,
    use_auto_universe: bool = True,
) -> None:
    """Final fail-closed gate shared by manual and automatic Demo entries."""
    symbol = normalize_symbol(body.symbol)
    if use_auto_universe:
        allowed = {normalize_symbol(value) for value in settings.get("_auto_universe", []) if value}
        if allowed and symbol not in allowed:
            raise BinanceDemoError(f"{symbol} izinli pariteler dışında; emir açılmadı.", http_status=409)
    if body.direction == "LONG" and not settings.get("allow_long", True):
        raise BinanceDemoError("LONG girişleri risk politikası tarafından kapatıldı.", http_status=409)
    if body.direction == "SHORT" and not settings.get("allow_short", True):
        raise BinanceDemoError("SHORT girişleri risk politikası tarafından kapatıldı.", http_status=409)
    if body.leverage > MANUAL_MAX_LEVERAGE or body.margin_usdt > MAX_MARGIN_USDT:
        raise BinanceDemoError("Demo kaldıraç veya marjin güvenlik sınırını aşıyor.", http_status=422)
    if float(spec["notional_usdt"]) > float(MAX_NOTIONAL_USDT):
        raise BinanceDemoError("Demo notional güvenlik sınırını aşıyor.", http_status=422)
    if daily_realized_pnl <= -float(settings.get("daily_loss_limit", 0)):
        raise BinanceDemoError("Günlük doğrulanmış zarar limiti aktif; yeni giriş kilitli.", http_status=409)
    existing_positions = len(snapshot.get("positions", []))
    pending_entries = sum(1 for item in snapshot.get("open_orders", []) if not bool(item.get("reduce_only", False)))
    pending_entries += len(paper_positions or [])
    if existing_positions + pending_entries >= min(int(settings.get("max_positions", MAX_OPEN_POSITIONS)), MAX_OPEN_POSITIONS):
        raise BinanceDemoError("Açık veya bekleyen girişler maksimum pozisyon sınırını dolduruyor.", http_status=409)
    duplicate_reason = duplicate_entry_reason(snapshot, symbol)
    if duplicate_reason:
        raise BinanceDemoError(duplicate_reason, http_status=409)
    if float(snapshot.get("available_balance", 0)) < float(body.margin_usdt):
        raise BinanceDemoError("Demo hesabında seçilen marjin için yeterli kullanılabilir bakiye yok.", http_status=409)
    estimated_loss = entry_risk(spec)
    if estimated_loss > float(settings.get("max_loss_per_trade", 0)) + 1e-9:
        raise BinanceDemoError("Stop Loss riski işlem başı maksimum zarar limitini aşıyor.", http_status=409)


def entry_risk(spec: dict[str, Any]) -> float:
    distance = abs(float(spec["current_price"]) - float(spec["stop_loss"]))
    return float(spec["notional_usdt"]) * distance / max(float(spec["current_price"]), 1e-12)


def adjust_manual_spec_to_risk(spec: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    """Reduce only manual Demo quantity until the existing risk gate passes."""
    max_allowed_risk = float(settings.get("max_loss_per_trade", 0))
    current_risk = entry_risk(spec)
    spec["risk_per_trade"] = current_risk
    spec["risk_adjusted"] = False
    if current_risk <= max_allowed_risk + 1e-9:
        return spec

    quantity = spec["quantity_decimal"]
    entry = Decimal(str(spec["current_price"]))
    step = spec["step"]
    min_qty = spec["min_qty"]
    min_notional = Decimal(str(spec["min_notional"]))
    candidate = floor_step(quantity * Decimal(str(max_allowed_risk)) / Decimal(str(current_risk)), step)
    if candidate >= quantity:
        candidate = floor_step(quantity - step, step)

    while candidate >= min_qty:
        if candidate * entry < min_notional:
            break
        spec["quantity_decimal"] = candidate
        spec["quantity"] = decimal_text(candidate)
        spec["notional_usdt"] = float(candidate * entry)
        current_risk = entry_risk(spec)
        if current_risk <= max_allowed_risk + 1e-9:
            spec["risk_per_trade"] = current_risk
            spec["risk_adjusted"] = True
            return spec
        candidate = floor_step(candidate - step, step)

    raise BinanceDemoError("Stop Loss riski için borsa minimum miktarı yeterince küçültülemiyor; emir açılmadı.", http_status=409)


def verified_realized_pnl(state: dict[str, Any]) -> float:
    return sum(
        float(item.get("realized_pnl") or 0)
        for item in state.get("journal", [])
        if item.get("verified_realized") is True
    )


async def post_algo(client: BinanceDemoClient, params: dict[str, Any]) -> dict[str, Any]:
    """Create one conditional order without blind retry on ambiguous responses."""
    try:
        result = await client.signed("POST", "/fapi/v1/algoOrder", params)
        return result if isinstance(result, dict) else {}
    except BinanceDemoError as exc:
        if not exc.unknown_execution and "duplicate" not in str(exc).lower():
            raise
        open_algos = response_rows(
            await client.signed("GET", "/fapi/v1/openAlgoOrders", {"symbol": params["symbol"]})
        )
        recovered = next(
            (
                item for item in open_algos
                if str(item.get("symbol")) == str(params["symbol"])
                and item.get("clientAlgoId") == params.get("clientAlgoId")
            ),
            None,
        )
        if recovered is None:
            raise
        return recovered


async def install_protection(
    client: BinanceDemoClient,
    state: dict[str, Any],
    plan: dict[str, Any],
    *,
    request_id: str | None = None,
    entry_context: object | None = None,
    state_lock: asyncio.Lock | None = None,
) -> None:
    if not _has_protection_identity(plan):
        return
    entry_context_valid = _valid_entry_installation_context(entry_context, plan)
    if not entry_context_valid and not can_mutate_lifecycle(plan):
        return
    lock = _protection_install_lock(plan)
    if lock.locked():
        return
    async with lock:
        parsed_ids = _plan_protection_ids(plan)
        if parsed_ids is None and "protection_ids" in plan:
            return
        if parsed_ids:
            return
        await _install_protection(
            client,
            state,
            plan,
            request_id=request_id,
            entry_context=entry_context,
            state_lock=state_lock,
        )


async def _repair_missing_stop_protection(
    client: BinanceDemoClient,
    state: dict[str, Any],
    plan: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    plans: list[dict[str, Any]],
) -> bool:
    if not _has_protection_identity(plan):
        return False
    lock = protection_lock(plan)
    async with lock:
        if plan.get("protection_repair_pending"):
            return False
        protection_state, _matched_ids, missing_ids = _protection_classification(
            plan, snapshot, plans=plans
        )
        stop_algo_id = _single_plan_algo_id(plan, "stop_algo_id")
        if protection_state != "MISSING" or stop_algo_id is None or stop_algo_id not in missing_ids:
            return False
        direction = str(plan.get("direction") or "").upper()
        if direction not in {"LONG", "SHORT"}:
            return False
        plan["protection_repair_pending"] = True
        persist_runtime(state)

    result = await post_algo(client, {
        "algoType": "CONDITIONAL",
        "symbol": plan["symbol"],
        "side": "SELL" if direction == "LONG" else "BUY",
        "type": "STOP_MARKET",
        "triggerPrice": plan["stop_loss"],
        "closePosition": "true",
        "workingType": "MARK_PRICE",
        "priceProtect": "TRUE",
        "clientAlgoId": new_client_id("REPAIRSL"),
    })
    new_id = _validate_new_protection_identity(
        plan,
        result,
        plans,
        expected_type="STOP_MARKET",
        expected_side="SELL" if direction == "LONG" else "BUY",
    )
    if new_id is None:
        raise BinanceDemoError("Binance Demo Stop onarım kimliği doğrulanamadı.", http_status=409)
    async with lock:
        protection_ids = _plan_protection_ids(plan)
        if protection_ids is None or not plan.get("protection_repair_pending"):
            return False
        plan["protection_ids"] = [new_id if algo_id == stop_algo_id else algo_id for algo_id in protection_ids]
        if new_id not in plan["protection_ids"]:
            plan["protection_ids"].append(new_id)
        plan["stop_algo_id"] = new_id
        plan["protection_status"] = "KORUMA ONARILDI"
        plan["protection_repair_pending"] = False
        persist_runtime(state)
    return True


async def _install_protection(
    client: BinanceDemoClient,
    state: dict[str, Any],
    plan: dict[str, Any],
    *,
    request_id: str | None = None,
    entry_context: object | None = None,
    state_lock: asyncio.Lock | None = None,
) -> None:
    entry_context_valid = _valid_entry_installation_context(entry_context, plan)
    if not entry_context_valid and not can_mutate_lifecycle(plan):
        return
    symbol = plan["symbol"]
    active_plans = [
        candidate for candidate in state.get("plans", {}).values()
        if isinstance(candidate, dict)
    ]
    correlation_id = request_id or f"local-{uuid.uuid4().hex[:16]}"
    if plan.get("stop_protection_cancelled"):
        return
    rows = await traced_stage("protection.positionRisk", correlation_id, client.signed("GET", "/fapi/v3/positionRisk", {"symbol": symbol}), client=client)
    position = next((item for item in response_rows(rows) if position_amount(item.get("positionAmt")) != 0), None)
    if position is None:
        plan["status"] = "DOLUM BEKLİYOR"
        return
    amount = position_amount(position.get("positionAmt"))
    update_position_lifecycle(plan, amount)
    actual_direction = "LONG" if amount > 0 else "SHORT"
    if actual_direction != plan["direction"]:
        plan["status"] = "YÖN UYUŞMAZLIĞI"
        add_event(state, "KORUMA KİLİDİ", f"{symbol} pozisyon yönü planla uyuşmadı; otomatik koruma kurulmadı.")
        return

    close_side = "SELL" if amount > 0 else "BUY"
    common = {
        "algoType": "CONDITIONAL",
        "symbol": symbol,
        "side": close_side,
        "workingType": "MARK_PRICE",
        "priceProtect": "TRUE",
    }
    stop_client_id = plan.get("stop_client_id") or new_client_id("SL")
    stop_params = {
        **common,
        "type": "STOP_MARKET",
        "triggerPrice": plan["stop_loss"],
        "closePosition": "true",
        "clientAlgoId": stop_client_id,
    }
    protection_ids: list[int] = list(_plan_protection_ids(plan) or [])
    stop_started = time.monotonic()
    trace_log("protection.SL.start", correlation_id)
    stop_response_received = False
    try:
        stop_result = await post_algo(client, stop_params)
        stop_response_received = True
        stop_algo_id = _validate_new_protection_identity(
            plan,
            stop_result,
            active_plans,
            expected_type="STOP_MARKET",
            expected_side=close_side,
        )
        if stop_algo_id is None:
            raise BinanceDemoError("Binance Demo Stop koruma kimliği doğrulanamadı.", http_status=409)
        protection_ids.append(stop_algo_id)
        plan["protection_ids"] = list(protection_ids)
        plan["stop_client_id"] = stop_client_id
        plan["stop_algo_id"] = stop_algo_id
        trace_log("protection.SL.end", correlation_id, duration_ms=round((time.monotonic() - stop_started) * 1000, 2), success=True, http_status=getattr(client, "last_status_code", None))
    except BinanceDemoError as exc:
        if not stop_response_received and "protection_ids" not in plan:
            plan["protection_ids"] = list(protection_ids)
        trace_log("protection.SL.end", correlation_id, duration_ms=round((time.monotonic() - stop_started) * 1000, 2), success=False, http_status=getattr(client, "last_status_code", None), error_type=type(exc).__name__, error_message=safe_trace_error(exc, client))
        await _abort_protection_installation(
            client, state, plan, exc,
            f"{symbol} Stop kurulamadı; Demo pozisyon güvenlik için kapatılıyor.",
            f"{symbol} Stop kurulamadı; pozisyon güvenli biçimde kapatıldı.",
            entry_context=entry_context,
        )

    step = Decimal(str(plan["step"]))
    min_qty = Decimal(str(plan["min_qty"]))
    total_qty = abs(amount)
    partial_qty = floor_step(total_qty * Decimal("0.30"), step)
    tp_quantity = decimal_text(partial_qty)
    update_position_lifecycle(plan, amount)
    targets = plan["targets"]
    monitoring_targets: list[str] = []
    if partial_qty >= min_qty:
        for index, trigger in enumerate(targets[:2], start=1):
            client_key = f"tp{index}_client_id"
            algo_client_id = plan.get(client_key) or new_client_id(f"TP{index}")
            params = {
                **common,
                "type": "TAKE_PROFIT_MARKET",
                "triggerPrice": trigger,
                "quantity": decimal_text(partial_qty),
                "reduceOnly": "true",
                "clientAlgoId": algo_client_id,
            }
            try:
                target_started = time.monotonic()
                trace_log(f"protection.TP{index}.start", correlation_id)
                result = await post_algo(client, params)
                algo_id = _validate_new_protection_identity(
                    plan,
                    result,
                    active_plans,
                    expected_type="TAKE_PROFIT_MARKET",
                    expected_side=close_side,
                )
                if algo_id is None:
                    raise BinanceDemoError("Binance Demo hedef koruma kimliği doğrulanamadı.", http_status=409)
                plan[f"tp{index}_algo_id"] = algo_id
                plan[client_key] = algo_client_id
                plan[f"tp{index}_quantity"] = tp_quantity
                protection_ids.append(algo_id)
                plan["protection_ids"] = list(protection_ids)
                trace_log(f"protection.TP{index}.end", correlation_id, duration_ms=round((time.monotonic() - target_started) * 1000, 2), success=True, http_status=getattr(client, "last_status_code", None))
            except BinanceDemoError as exc:
                trace_log(f"protection.TP{index}.end", correlation_id, duration_ms=round((time.monotonic() - target_started) * 1000, 2), success=False, http_status=getattr(client, "last_status_code", None), error_type=type(exc).__name__, error_message=safe_trace_error(exc, client))
                await _abort_protection_installation(
                    client, state, plan, exc,
                    f"{symbol} TP{index} kurulamadı; Demo pozisyon güvenlik için kapatılıyor.",
                    f"{symbol} TP{index} kurulamadı; pozisyon güvenli biçimde kapatıldı.",
                    entry_context=entry_context,
                )
    else:
        monitoring_targets.extend(["TP1", "TP2"])

    tp3_client_id = plan.get("tp3_client_id") or new_client_id("TP3")
    tp3_started = time.monotonic()
    trace_log("protection.TP3.start", correlation_id)
    try:
        tp3_result = await post_algo(client, {
            **common,
            "type": "TAKE_PROFIT_MARKET",
            "triggerPrice": targets[2],
            "closePosition": "true",
            "clientAlgoId": tp3_client_id,
        })
        algo_id = _validate_new_protection_identity(
            plan,
            tp3_result,
            active_plans,
            expected_type="TAKE_PROFIT_MARKET",
            expected_side=close_side,
        )
        if algo_id is None:
            raise BinanceDemoError("Binance Demo TP3 koruma kimliği doğrulanamadı.", http_status=409)
        plan["tp3_client_id"] = tp3_client_id
        protection_ids.append(algo_id)
        plan["protection_ids"] = list(protection_ids)
        trace_log("protection.TP3.end", correlation_id, duration_ms=round((time.monotonic() - tp3_started) * 1000, 2), success=True, http_status=getattr(client, "last_status_code", None))
    except BinanceDemoError as exc:
        trace_log("protection.TP3.end", correlation_id, duration_ms=round((time.monotonic() - tp3_started) * 1000, 2), success=False, http_status=getattr(client, "last_status_code", None), error_type=type(exc).__name__, error_message=safe_trace_error(exc, client))
        await _abort_protection_installation(
            client, state, plan, exc,
            f"{symbol} TP3 kurulamadı; Demo pozisyon güvenlik için kapatılıyor.",
            f"{symbol} TP3 kurulamadı; pozisyon güvenli biçimde kapatıldı.",
            entry_context=entry_context,
        )

    verification_ids = tuple(sorted(protection_ids))
    verification_metadata = copy.deepcopy({
        field: plan.get(field)
        for field in ("protection_ids", "stop_algo_id", "tp1_algo_id", "tp2_algo_id", "tp3_algo_id")
    })
    if state_lock is not None:
        state_lock.release()
    try:
        final_snapshot = await _fresh_protection_snapshot(client, symbol)
    finally:
        if state_lock is not None:
            await state_lock.acquire()

    try:
        current_plan = state.get("plans", {}).get(plan.get("id"))
        current_ids = _plan_protection_ids(plan)
        current_metadata = {
            field: plan.get(field)
            for field in ("protection_ids", "stop_algo_id", "tp1_algo_id", "tp2_algo_id", "tp3_algo_id")
        }
        active_plans = [
            candidate for candidate in state.get("plans", {}).values()
            if isinstance(candidate, dict)
        ]
        if (
            current_plan is not plan
            or current_ids is None
            or tuple(sorted(current_ids)) != verification_ids
            or current_metadata != verification_metadata
        ):
            raise BinanceDemoError(
                "Binance Demo koruma kurulumu sırasında plan ownership değişti.",
                http_status=409,
            )
        final_state, final_matched_ids, _missing_ids = _protection_classification(
            plan,
            final_snapshot,
            required_ids=set(protection_ids),
            plans=active_plans,
        )
        if final_state != "MATCHED" or set(final_matched_ids) != set(protection_ids):
            raise BinanceDemoError(
                "Binance Demo koruma kurulumu final snapshot ile doğrulanamadı.",
                http_status=409,
            )
    except BinanceDemoError as exc:
        await _abort_protection_installation(
            client, state, plan, exc,
            f"{symbol} korumaların tamamı doğrulanamadı; Demo pozisyon güvenlik için kapatılıyor.",
            f"{symbol} korumaların tamamı doğrulanamadı; pozisyon güvenli biçimde kapatıldı.",
            entry_context=entry_context,
        )

    plan["protection_ids"] = protection_ids
    plan["monitoring_targets"] = monitoring_targets
    plan["status"] = "OPEN"
    plan["position_status"] = "OPEN"
    plan["protection_status"] = "KORUMA AKTİF" if not monitoring_targets else "STOP AKTİF · HEDEF İZLEME"
    plan["protected_at"] = utc_now()
    add_event(state, "KORUMA KURULDU", f"{symbol} Stop aktif; hedef planı Demo hesabına işlendi.")


async def cleanup_closed_plan(
    client: BinanceDemoClient,
    plan: dict[str, Any],
    *,
    entry_context: object | None = None,
    snapshot: dict[str, Any] | None = None,
    plans: list[dict[str, Any]] | None = None,
    allow_closed_position_cleanup: bool = False,
) -> bool:
    if not _has_protection_identity(plan):
        return True
    if (
        not _valid_entry_installation_context(entry_context, plan)
        and not can_mutate_lifecycle(plan)
        and not allow_closed_position_cleanup
    ):
        return False
    lock = _protection_cleanup_lock(plan)
    async with lock:
        protection_ids = _plan_protection_ids(plan)
        if protection_ids is None:
            return False
        pending_ids = plan.get("cleanup_pending_ids", [])
        if not isinstance(pending_ids, list) or any(not isinstance(item, int) or item <= 0 for item in pending_ids):
            return False
        claimed_ids = sorted(protection_ids - set(pending_ids))
        if not claimed_ids:
            return True
        plan["cleanup_pending_ids"] = sorted(set(pending_ids).union(claimed_ids))

    snapshot = snapshot or await _fresh_protection_snapshot(client, plan["symbol"])
    removed_ids: set[int] = set()
    delete_attempted: set[int] = set()
    for algo_id in claimed_ids:
        ownership, matched_ids, missing_ids = _protection_classification(
            plan,
            snapshot,
            required_ids={algo_id},
            plans=plans,
        )
        if ownership == "MISSING" and algo_id in missing_ids:
            removed_ids.add(algo_id)
            continue
        if ownership != "MATCHED" or algo_id not in matched_ids:
            continue
        try:
            delete_attempted.add(algo_id)
            await client.signed("DELETE", "/fapi/v1/algoOrder", {"symbol": plan["symbol"], "algoId": algo_id})
        except BinanceDemoError as exc:
            if exc.exchange_code in {-2011, -2013}:
                removed_ids.add(algo_id)

    if delete_attempted:
        verified_snapshot = await _fresh_protection_snapshot(client, plan["symbol"])
        for algo_id in delete_attempted:
            ownership, matched_ids, missing_ids = _protection_classification(
                plan,
                verified_snapshot,
                required_ids={algo_id},
                plans=plans,
            )
            if ownership == "MISSING" and algo_id in missing_ids:
                removed_ids.add(algo_id)

    async with lock:
        current_ids = _plan_protection_ids(plan)
        if current_ids is None:
            return False
        pending_ids = plan.get("cleanup_pending_ids", [])
        if not isinstance(pending_ids, list):
            return False
        plan["protection_ids"] = sorted(current_ids - removed_ids)
        plan["cleanup_pending_ids"] = [item for item in pending_ids if item not in claimed_ids]
        return not plan["protection_ids"]


async def _abort_protection_installation(
    client: BinanceDemoClient,
    state: dict[str, Any],
    plan: dict[str, Any],
    error: BinanceDemoError,
    event_message: str,
    close_message: str,
    *,
    entry_context: object | None = None,
) -> None:
    if not _valid_entry_installation_context(entry_context, plan) and not can_mutate_lifecycle(plan):
        return
    symbol = plan["symbol"]
    plan["status"] = "CRITICAL / UNPROTECTED"
    plan["protection_status"] = "CRITICAL / UNPROTECTED"
    plan["recovery_attempts"] = int(plan.get("recovery_attempts", 0)) + 1
    plan["last_error"] = str(error)
    add_event(state, "ACİL KORUMA", event_message)
    await cleanup_closed_plan(
        client,
        plan,
        entry_context=entry_context,
        plans=[candidate for candidate in state.get("plans", {}).values() if isinstance(candidate, dict)],
    )
    try:
        await cancel_entry_if_open(client, plan)
    except BinanceDemoError as cancel_exc:
        plan["last_error"] = f"Stop: {error}; Entry iptali: {cancel_exc}"
    try:
        await close_symbol_position(client, symbol)
    except BinanceDemoError as close_exc:
        plan["last_error"] = f"Stop: {error}; Kapatma: {close_exc}"
    try:
        confirmation = response_rows(await client.signed("GET", "/fapi/v3/positionRisk", {"symbol": symbol}))
        still_open = any(position_amount(item.get("positionAmt")) != 0 for item in confirmation)
    except BinanceDemoError as verify_exc:
        plan["last_error"] = f"{plan.get('last_error', str(error))}; Durum doğrulama: {verify_exc}"
        plan["position_status"] = "OPEN"
        persist_runtime(state)
        raise
    if still_open:
        plan["position_status"] = "OPEN"
        persist_runtime(state)
        raise BinanceDemoError(f"{symbol} CRITICAL / UNPROTECTED; güvenli kapatma doğrulanamadı.", http_status=409)
    plan["position_status"] = "CLOSED"
    plan["status"] = "GÜVENLİK İÇİN KAPATILDI"
    plan["protection_status"] = "CLOSED_AFTER_PROTECTION_FAILURE"
    persist_runtime(state)
    raise BinanceDemoError(close_message, http_status=409)


async def protection_loop(application: Any) -> None:
    while True:
        await asyncio.sleep(2)
        states = [("", application.state.binance_demo)] + list(_demo_user_store(application).items())
        for user_id, state in states:
            if not state.get("plans"):
                continue
            if not user_id and not credentials_configured():
                continue
            try:
                client = client_for_state(application, state)
                await recover_pending_entry_intents(client, state)
                changed = False
                plans = [candidate for candidate in state["plans"].values() if isinstance(candidate, dict)]
                for plan in plans:
                    if plan.get("status") in {"KAPANDI", "İPTAL", "GÜVENLİK İÇİN KAPATILDI"}:
                        continue
                    rows = response_rows(
                        await client.signed("GET", "/fapi/v3/positionRisk", {"symbol": plan["symbol"]})
                    )
                    active_position = next((row for row in rows if Decimal(str(row.get("positionAmt", "0"))) != 0), None)
                    if not can_mutate_lifecycle(plan):
                        if active_position is None and plan.get("position_status") == "OPEN":
                            if _within_plan_reconciliation_grace(plan):
                                continue
                            cleaned = await cleanup_closed_plan(
                                client,
                                plan,
                                allow_closed_position_cleanup=True,
                                plans=plans,
                            )
                            if cleaned:
                                plan.update({"status": "KAPANDI", "position_status": "CLOSED", "remaining_quantity": "0", "closed_at": utc_now()})
                                add_event(state, "POZİSYON KAPANDI", f"{plan['symbol']} Demo pozisyonu kapandı; kalan bot emirleri temizlendi.")
                                changed = True
                        continue
                    if active_position is not None and plan.get("stop_protection_cancelled"):
                        continue
                    if active_position is not None:
                        lifecycle_before = (
                            plan.get("remaining_quantity"), plan.get("position_status"),
                            plan.get("tp1_status"), plan.get("tp2_status"), plan.get("tp3_status"),
                        )
                        update_position_lifecycle(plan, Decimal(str(active_position["positionAmt"])))
                        lifecycle_after = (
                            plan.get("remaining_quantity"), plan.get("position_status"),
                            plan.get("tp1_status"), plan.get("tp2_status"), plan.get("tp3_status"),
                        )
                        changed = changed or lifecycle_before != lifecycle_after
                    if active_position is not None:
                        protection_snapshot = await _fresh_protection_snapshot(client, plan["symbol"])
                        protection_state, _matched_ids, _missing_ids = _protection_classification(
                            plan, protection_snapshot, plans=plans
                        )
                        if protection_state == "MISSING":
                            changed = await _repair_missing_stop_protection(
                                client, state, plan, protection_snapshot, plans=plans
                            ) or changed
                        elif protection_state == "UNKNOWN":
                            continue
                    elif active_position is None and plan.get("position_status") == "OPEN":
                        if _within_plan_reconciliation_grace(plan):
                            continue
                        cleaned = await cleanup_closed_plan(client, plan, plans=plans)
                        if cleaned:
                            plan["status"] = "KAPANDI"
                            plan["position_status"] = "CLOSED"
                            plan["remaining_quantity"] = "0"
                            plan["closed_at"] = utc_now()
                            add_event(state, "POZİSYON KAPANDI", f"{plan['symbol']} Demo pozisyonu kapandı; kalan bot emirleri temizlendi.")
                            changed = True
                if changed:
                    persist_runtime(state)
            except BinanceDemoError as exc:
                state["last_error"] = str(exc)[:240]
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                state["last_error"] = str(exc)[:240]


def init_binance_demo(application: Any) -> None:
    application.state.binance_demo = {
        "connected": False,
        "armed_until": 0,
        "last_checked": None,
        "last_error": None,
        "events": [],
        "plans": load_runtime(),
        "lock": asyncio.Lock(),
    }
    add_event(application.state.binance_demo, "GÜVENLİ BAŞLANGIÇ", "Demo emir kilidi kapalı başladı; yeniden elle açılması gerekir.")
    application.state.binance_demo_task = asyncio.create_task(protection_loop(application))


async def shutdown_binance_demo(application: Any) -> None:
    task = getattr(application.state, "binance_demo_task", None)
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@router.get("/status")
async def demo_status(request: Request) -> dict[str, Any]:
    try:
        from .exchange_connections import ensure_session_cache

        await ensure_session_cache(request)
    except (ImportError, RuntimeError, ValueError):
        pass
    result = public_status(state_for(request))
    result["configured"] = credentials_configured(request)
    return result


@router.post("/connect")
async def demo_connect(request: Request) -> dict[str, Any]:
    state = state_for(request)
    try:
        client = client_for(request)
        snapshot = await connect_snapshot(client)
        reconciliation = reconcile_demo_plans(state, snapshot)
        if reconciliation["changed"]:
            persist_runtime(state)
        snapshot = enrich_snapshot_with_plans(snapshot, state)
        state.update({"connected": True, "last_checked": utc_now(), "last_error": None})
        add_event(state, "BAĞLANTI BAŞARILI", "Binance Futures Demo hesabı doğrulandı; gerçek hesap kanalı kilitli.")
        return {**public_status(state), "account": snapshot}
    except BinanceDemoError as exc:
        state.update({"connected": False, "last_checked": utc_now(), "last_error": str(exc)[:240]})
        raise safe_exchange_error(exc) from exc


@router.get("/account")
async def demo_account(request: Request) -> dict[str, Any]:
    state = state_for(request)
    request_id = request_correlation_id(request)
    started = time.monotonic()
    trace_log("account.start", request_id)
    try:
        snapshot = await account_snapshot(client_for(request), request_id)
        reconciliation = reconcile_demo_plans(state, snapshot)
        if reconciliation["changed"]:
            persist_runtime(state)
        snapshot = enrich_snapshot_with_plans(snapshot, state)
        state.update({"connected": True, "last_checked": utc_now(), "last_error": None})
        result = {**public_status(state), **snapshot, "plans": list(state.get("plans", {}).values())[-12:]}
        result["configured"] = credentials_configured(request)
        trace_log("account.complete", request_id, duration_ms=round((time.monotonic() - started) * 1000, 2), success=True)
        return result
    except BinanceDemoError as exc:
        state.update({"connected": False, "last_checked": utc_now(), "last_error": str(exc)[:240]})
        if exc.http_status == 412 and not credentials_configured(request):
            empty_result = {
                **public_status(state),
                "configured": False,
                "positions": [],
                "open_orders": [],
                "open_algo_orders": [],
                "plans": list(state.get("plans", {}).values())[-12:],
                "last_error": str(exc)[:240],
            }
            trace_log(
                "account.empty",
                request_id,
                duration_ms=round((time.monotonic() - started) * 1000, 2),
                success=True,
                error_type=type(exc).__name__,
                error_message=safe_trace_error(exc),
            )
            return empty_result
        trace_log("account.failed", request_id, duration_ms=round((time.monotonic() - started) * 1000, 2), success=False, error_type=type(exc).__name__, error_message=safe_trace_error(exc))
        raise safe_exchange_error(exc) from exc


@router.get("/ownership-diagnostic")
async def demo_ownership_diagnostic(request: Request) -> dict[str, Any]:
    member = getattr(getattr(request, "state", None), "member", None)
    user_id = str(member.get("id") or "").strip() if isinstance(member, dict) else ""
    if not user_id:
        raise HTTPException(status_code=401, detail="Authenticated member context required.")
    demo_state = state_for(request)
    if str(demo_state.get("_user_id") or "").strip() != user_id:
        raise HTTPException(status_code=403, detail="Demo ownership context mismatch.")
    from .v21_demo import state_for as v21_state_for

    v21_state = v21_state_for(request)
    if str(v21_state.get("_user_id") or "").strip() != user_id:
        raise HTTPException(status_code=403, detail="V21 ownership context mismatch.")
    try:
        snapshot = await account_snapshot(client_for(request), request_correlation_id(request))
    except BinanceDemoError as exc:
        raise safe_exchange_error(exc) from exc
    result = build_demo_ownership_diagnostic(snapshot, demo_state, v21_state)
    result["provenance"] = provenance_observer_diagnostic(demo_state, snapshot)
    return result


@router.post("/arm")
async def demo_arm(request: Request, body: ArmRequest) -> dict[str, Any]:
    state = state_for(request)
    if body.confirmation.strip().upper() != "DEMO":
        raise HTTPException(422, "Onay alanına DEMO yazın.")
    try:
        snapshot = await account_snapshot(client_for(request))
    except BinanceDemoError as exc:
        raise safe_exchange_error(exc) from exc
    if snapshot["hedge_mode"]:
        raise HTTPException(409, "Binance Demo hesabında Pozisyon Modu 'Tek Yön / One-way' olmalı.")
    state["armed_until"] = time.time() + ARM_SECONDS
    state["connected"] = True
    state["last_checked"] = utc_now()
    add_event(state, "DEMO EMİR KİLİDİ AÇILDI", "Yalnızca Binance Futures Demo emirleri 10 dakika için açıldı.")
    return public_status(state)


@router.post("/disarm")
async def demo_disarm(request: Request) -> dict[str, Any]:
    state = state_for(request)
    state["armed_until"] = 0
    add_event(state, "DEMO EMİR KİLİDİ KAPANDI", "Yeni Demo giriş emirleri durduruldu; koruma emirleri çalışmaya devam eder.")
    return public_status(state)


@router.post("/order/test")
async def demo_order_test(request: Request, body: DemoOrderRequest) -> dict[str, Any]:
    state = state_for(request)
    try:
        client = client_for(request)
        if await position_mode(client):
            raise BinanceDemoError("Pozisyon Modu 'Tek Yön / One-way' olmalı.", http_status=409)
        spec = await build_order_spec(client, body)
        policy = getattr(request.app.state, "v21_demo", {}).get("settings", {})
        adjust_manual_spec_to_risk(spec, policy)
        await submit_entry(client, spec, test_only=True)
        add_event(state, "EMİR TESTİ BAŞARILI", f"{spec['symbol']} {spec['direction']} planı Demo doğrulamasından geçti; emir oluşmadı.")
        return {
            "ok": True,
            "message": "Demo emir testi başarılı; hiçbir pozisyon veya emir oluşturulmadı.",
            "preview": {key: value for key, value in spec.items() if not key.endswith("_decimal") and key not in {"step"}},
            "risk_adjusted": bool(spec.get("risk_adjusted")),
            "real_trading_locked": True,
        }
    except BinanceDemoError as exc:
        state["last_error"] = str(exc)[:240]
        raise safe_exchange_error(exc) from exc


@router.post("/order")
async def demo_order(request: Request, body: DemoOrderRequest) -> dict[str, Any]:
    return await execute_demo_order(request.app, body, source="MANUAL", request=request)


async def execute_demo_order(
    application: Any,
    body: DemoOrderRequest,
    *,
    source: str = "MANUAL",
    request: Request | None = None,
    demo_state: dict[str, Any] | None = None,
    v21_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Submit one hard-capped Demo order for the manual or V21 automation path."""
    state = demo_state if demo_state is not None else (state_for(request) if request is not None else application.state.binance_demo)
    resolved_v21_state = v21_state
    if resolved_v21_state is None and request is not None:
        from .v21_demo import state_for as v21_state_for

        resolved_v21_state = v21_state_for(request)
    _validate_execution_context(request, state, resolved_v21_state)
    request_id = request_correlation_id(request)
    total_started = time.monotonic()
    trace_log("start", request_id, symbol=body.symbol, side="BUY" if body.direction == "LONG" else "SELL")
    if not armed(state) and source != "MANUAL":
        raise HTTPException(423, "Demo emir kilidi kapalı veya süresi doldu; önce 10 dakikalık kilidi açın.")
    async with traced_lock(state["lock"], request_id, "state"):
        try:
            client = client_for(request) if request is not None else (
                client_for_state(application, state)
                if str(state.get("_user_id") or "").strip()
                else BinanceDemoClient(application.state.http, *load_demo_credentials())
            )
            client.trace_request_id = request_id
            await traced_stage("ensure_one_way_position_mode", request_id, ensure_one_way_position_mode(client), client=client)
            snapshot = await traced_stage("first_account_snapshot", request_id, account_snapshot(client, request_id), client=client)
            symbol = await resolve_demo_symbol(client, body.symbol)
            body = body.model_copy(update={"symbol": symbol})
            reconciliation = reconcile_demo_plans(state, snapshot)
            cleanup_changed = await _cleanup_reconciled_closed_plans(
                client,
                state,
                snapshot,
                reconciliation_changed=reconciliation["changed"],
            )
            if reconciliation["changed"] or cleanup_changed:
                persist_runtime(state)
            stale_reason = stale_protection_entry_reason(snapshot, state, symbol)
            if stale_reason:
                raise BinanceDemoError(stale_reason, http_status=409)
            confirmed_slot_conflict = find_conflicting_confirmed_plan(
                state.get("plans"),
                {"symbol": symbol},
            )
            if confirmed_slot_conflict is not None:
                raise BinanceDemoError(
                    f"Entry blocked: confirmed provenance slot already exists for {symbol}.",
                    http_status=409,
                )
            spec = await traced_stage("build_order_spec", request_id, build_order_spec(client, body), client=client)
            if resolved_v21_state is not None:
                policy = resolved_v21_state.get("settings", {})
            else:
                policy = getattr(application.state, "v21_demo", {}).get("settings", {})
            risk_state = resolved_v21_state or getattr(application.state, "v21_demo", {})
            if source == "MANUAL":
                adjust_manual_spec_to_risk(spec, policy)
            else:
                spec["risk_per_trade"] = entry_risk(spec)
                spec["risk_adjusted"] = False
            validate_entry_risk(
                snapshot, body, spec, policy,
                daily_realized_pnl=verified_realized_pnl(risk_state),
                paper_positions=risk_state.get("paper_positions", []),
                use_auto_universe=source != "MANUAL",
            )
            await traced_stage("set_isolated_margin", request_id, set_isolated_margin(client, spec["symbol"]), client=client)
            leverage_audit = await traced_stage("apply_verified_leverage", request_id, apply_verified_leverage(client, spec["symbol"], spec["leverage"]), client=client)
            spec = await traced_stage("second_build_order_spec", request_id, build_order_spec(client, body), client=client)
            if source == "MANUAL":
                adjust_manual_spec_to_risk(spec, policy)
            else:
                spec["risk_per_trade"] = entry_risk(spec)
                spec["risk_adjusted"] = False
            snapshot = await traced_stage("second_account_snapshot", request_id, account_snapshot(client, request_id), client=client)
            stale_reason = stale_protection_entry_reason(snapshot, state, body.symbol)
            if stale_reason:
                raise BinanceDemoError(stale_reason, http_status=409)
            validate_entry_risk(
                snapshot, body, spec, policy,
                daily_realized_pnl=verified_realized_pnl(risk_state),
                paper_positions=risk_state.get("paper_positions", []),
                use_auto_universe=source != "MANUAL",
            )
            plan_id = uuid.uuid4().hex[:12]
            client_order_id = new_client_id("ENTRY")
            plan = {
                "id": plan_id,
                "position_id": plan_id,
                "user_id": _current_user_id(request=request, state=state),
                "symbol": spec["symbol"],
                "direction": spec["direction"],
                "order_type": spec["order_type"],
                "entry_price": spec["entry_price"],
                "quantity": spec["quantity"],
                "initial_quantity": spec["quantity"],
                "remaining_quantity": spec["quantity"],
                "margin_usdt": spec["margin_usdt"],
                "leverage": spec["leverage"],
                "requested_leverage": leverage_audit["requested_leverage"],
                "applied_leverage": leverage_audit["applied_leverage"],
                "margin_type": leverage_audit["margin_type"],
                "leverage_verified": leverage_audit["leverage_verified"],
                "configuration_source": leverage_audit["configuration_source"],
                "max_notional_value": leverage_audit["max_notional_value"],
                "leverage_verified_at": utc_now(),
                "stop_loss": spec["stop_loss"],
                "targets": spec["targets"],
                "step": decimal_text(spec["step"]),
                "min_qty": decimal_text(spec["min_qty"]),
                "entry_order_id": None,
                "entry_client_order_id": client_order_id,
                "status": "ENTRY_INTENT_PENDING",
                "position_status": "PENDING",
                "created_at": utc_now(),
                "demo_only": True,
                "source": source,
                "initial_stop_loss": spec["stop_loss"],
                "notional_usdt": spec["notional_usdt"],
                "risk_per_trade": spec["risk_per_trade"],
                "risk_adjusted": bool(spec.get("risk_adjusted")),
            }
            plan.update(provenance_entry_fields(expected_quantity=spec["quantity"]))
            state.setdefault("plans", {})[plan_id] = plan
            persist_runtime(state)
            submit_started = time.monotonic()
            trace_log("submit_entry.start", request_id, entry_client_order_id=client_order_id, symbol=spec["symbol"], side=spec["side"], quantity=spec["quantity"])
            try:
                result = await submit_entry(client, spec, test_only=False, client_id=client_order_id)
            except Exception as exc:
                trace_log(
                    "submit_entry.end",
                    request_id,
                    entry_client_order_id=client_order_id,
                    duration_ms=round((time.monotonic() - submit_started) * 1000, 2),
                    http_status=getattr(client, "last_status_code", None),
                    success=False,
                    unknown_execution=getattr(exc, "unknown_execution", False),
                    symbol=spec["symbol"],
                    side=spec["side"],
                    quantity=spec["quantity"],
                    error_type=type(exc).__name__,
                    error_message=safe_trace_error(exc, client),
                )
                raise
            trace_log(
                "submit_entry.end",
                request_id,
                entry_client_order_id=client_order_id,
                duration_ms=round((time.monotonic() - submit_started) * 1000, 2),
                http_status=getattr(client, "last_status_code", None),
                success=True,
                unknown_execution=False,
                symbol=spec["symbol"],
                side=spec["side"],
                quantity=spec["quantity"],
            )
            plan["entry_order_id"] = int(result.get("orderId", 0)) or None
            plan["entry_client_order_id"] = result.get("clientOrderId") or client_order_id
            plan.update(provenance_entry_fields(
                entry_client_order_id=plan["entry_client_order_id"],
                entry_order_id=plan["entry_order_id"],
                expected_quantity=spec["quantity"],
            ))
            plan["status"] = "DOLUM BEKLİYOR"
            persist_runtime(state)
            add_event(
                state,
                "KALDIRAÇ DOĞRULANDI",
                f"{spec['symbol']} istenen {spec['leverage']}x, uygulanan {leverage_audit['applied_leverage']}x ISOLATED; emir güvenlik kontrolünden geçti.",
            )
            add_event(state, "DEMO EMİR GÖNDERİLDİ", f"{spec['symbol']} {spec['direction']} {spec['order_type']} emri Demo hesabına gönderildi ({source}).")
            if spec["order_type"] == "MARKET":
                entry_context = object()
                _ACTIVE_ENTRY_INSTALLATION_CONTEXTS[entry_context] = {"plan": plan}
                try:
                    await traced_stage(
                        "install_protection",
                        request_id,
                        install_protection(
                            client,
                            state,
                            plan,
                            request_id=request_id,
                            entry_context=entry_context,
                            state_lock=state["lock"],
                        ),
                        client=client,
                    )
                finally:
                    _retire_entry_installation_context(entry_context)
                persist_runtime(state)
            verified_snapshot = await traced_stage("final_account_snapshot", request_id, account_snapshot(client, request_id), client=client)
            response = {
                "ok": True,
                "message": f"Emir yalnızca Binance Futures Demo hesabına gönderildi; {leverage_audit['applied_leverage']}x ISOLATED doğrulandı.",
                "order": {
                    "symbol": result.get("symbol", spec["symbol"]),
                    "order_id": result.get("orderId"),
                    "client_order_id": result.get("clientOrderId"),
                    "status": result.get("status", plan["status"]),
                    "type": result.get("type", spec["order_type"]),
                    "side": result.get("side", spec["side"]),
                    "quantity": spec["quantity"],
                },
                "plan": plan,
                "risk_adjusted": bool(spec.get("risk_adjusted")),
                "risk_per_trade": spec["risk_per_trade"],
                "open_order_count": len(verified_snapshot["open_orders"]),
                "open_algo_order_count": len(verified_snapshot["open_algo_orders"]),
                "real_trading_locked": True,
            }
            trace_log("complete", request_id, duration_ms=round((time.monotonic() - total_started) * 1000, 2), success=True)
            return response
        except BinanceDemoError as exc:
            state["last_error"] = str(exc)[:240]
            trace_log("failed", request_id, duration_ms=round((time.monotonic() - total_started) * 1000, 2), success=False, error_type=type(exc).__name__, error_message=safe_trace_error(exc, client if "client" in locals() else None))
            raise safe_exchange_error(exc) from exc
        except Exception as exc:
            trace_log("failed", request_id, duration_ms=round((time.monotonic() - total_started) * 1000, 2), success=False, error_type=type(exc).__name__, error_message=safe_trace_error(exc, client if "client" in locals() else None))
            raise


@router.post("/order/cancel")
async def demo_cancel_order(request: Request, body: CancelOrderRequest) -> dict[str, Any]:
    try:
        symbol = normalize_symbol(body.symbol)
        state = state_for(request)
        if not _has_user_plan_access(state, order_id=body.order_id, request=request):
            raise HTTPException(404, "Bu emir sizde değil veya bulunamadı")
        result = await client_for(request).signed("DELETE", "/fapi/v1/order", {"symbol": symbol, "orderId": body.order_id})
        add_event(state, "EMİR İPTAL", f"{symbol} Demo emri iptal edildi.")
        persist_runtime(state)
        return {"ok": True, "symbol": symbol, "order_id": result.get("orderId", body.order_id)}
    except BinanceDemoError as exc:
        raise safe_exchange_error(exc) from exc


@router.post("/algo/cancel")
async def demo_cancel_algo(request: Request, body: CancelAlgoRequest) -> dict[str, Any]:
    try:
        symbol = normalize_symbol(body.symbol)
        state = state_for(request)
        client = client_for(request)
        snapshot = await _fresh_protection_snapshot(client, symbol)
        owner = None
        for candidate in state.get("plans", {}).values():
            if not can_mutate_lifecycle(candidate) or not _symbol_matches(candidate.get("symbol"), symbol):
                continue
            protection_ids = _plan_protection_ids(candidate)
            if protection_ids is None or body.algo_id not in protection_ids:
                continue
            ownership, matched_ids, _missing_ids = _protection_classification(
                candidate,
                snapshot,
                required_ids={body.algo_id},
                plans=[item for item in state.get("plans", {}).values() if isinstance(item, dict)],
            )
            if ownership == "MATCHED" and body.algo_id in matched_ids:
                owner = candidate
                break
        if owner is None:
            raise HTTPException(409, "Protection ownership could not be verified; algo order was not cancelled.")
        result = await client.signed("DELETE", "/fapi/v1/algoOrder", {"symbol": symbol, "algoId": body.algo_id})
        plan = mark_cancelled_protection(
            state.get("plans", {}), symbol, body.algo_id, owner=owner
        )
        if plan:
            persist_runtime(state)
            message = f"{symbol} STOP koruması iptal edildi; pozisyon artık otomatik korunmuyor."
        else:
            message = f"{symbol} koşullu Demo emri iptal edildi."
        add_event(state, "KORUMA İPTAL", message)
        return {"ok": True, "symbol": symbol, "algo_id": result.get("algoId", body.algo_id)}
    except BinanceDemoError as exc:
        raise safe_exchange_error(exc) from exc


@router.post("/position/close")
async def demo_close_position(request: Request, body: ClosePositionRequest) -> dict[str, Any]:
    if body.confirmation.strip().upper() != "DEMO KAPAT":
        raise HTTPException(422, "Pozisyonu kapatmak için DEMO KAPAT yazın.")
    try:
        symbol = normalize_symbol(body.symbol)
        state = state_for(request)
        client = client_for(request)
        result = await close_symbol_position(client, symbol, body.position_side)
        if result is None:
            raise BinanceDemoError("Bu paritede açık Demo pozisyonu yok.", http_status=404)
        for plan in state.get("plans", {}).values():
            if str(plan.get("symbol") or "").upper() == symbol and str(plan.get("user_id") or plan.get("_user_id") or "").strip() == _current_user_id(request=request, state=state) and plan.get("position_status") == "OPEN":
                cleaned = await cleanup_closed_plan(
                    client,
                    plan,
                    allow_closed_position_cleanup=True,
                    plans=[candidate for candidate in state.get("plans", {}).values() if isinstance(candidate, dict)],
                )
                if cleaned:
                    plan.update({"status": "KAPANDI", "position_status": "CLOSED", "remaining_quantity": "0", "closed_at": utc_now()})
        add_event(state, "POZİSYON KAPATILDI", f"{symbol} Demo pozisyonu reduce-only piyasa emriyle kapatıldı.")
        persist_runtime(state)
        return {"ok": True, "symbol": symbol, "order_id": result.get("orderId")}
    except BinanceDemoError as exc:
        raise safe_exchange_error(exc) from exc


@router.post("/position/reduce")
async def demo_reduce_position(request: Request, body: ReducePositionRequest) -> dict[str, Any]:
    if body.confirmation.strip().upper() != "DEMO AZALT":
        raise HTTPException(422, "Pozisyonu azaltmak için DEMO AZALT yazın.")
    try:
        symbol = normalize_symbol(body.symbol)
        state = state_for(request)
        if not _has_user_plan_access(state, symbol=symbol, request=request):
            raise HTTPException(404, "Bu pozisyon sizde değil veya bulunamadı")
        result = await reduce_symbol_position(client_for(request), symbol, Decimal(str(body.quantity)), body.position_side)
        if result is None:
            raise BinanceDemoError("Bu paritede açık Demo pozisyonu yok.", http_status=404)
        add_event(state, "POZİSYON AZALTILDI", f"{symbol} Demo pozisyonu reduce-only piyasa emriyle azaltıldı.")
        persist_runtime(state)
        return {"ok": True, "symbol": symbol, "order_id": result.get("orderId")}
    except BinanceDemoError as exc:
        raise safe_exchange_error(exc) from exc


@router.post("/emergency")
async def demo_emergency(request: Request, body: EmergencyRequest) -> dict[str, Any]:
    if body.confirmation.strip().upper() != "DEMO ACİL DURDUR":
        raise HTTPException(422, "Acil işlem için DEMO ACİL DURDUR yazın.")
    state = state_for(request)
    async with state["lock"]:
        try:
            client = client_for(request)
            snapshot = await account_snapshot(client)
            cancelled_orders = 0
            cancelled_algos = 0
            closed_positions = 0
            for item in snapshot["open_orders"]:
                if str(item.get("client_order_id") or "").startswith(CLIENT_PREFIX):
                    try:
                        await client.signed("DELETE", "/fapi/v1/order", {"symbol": item["symbol"], "orderId": item["order_id"]})
                        cancelled_orders += 1
                    except BinanceDemoError:
                        pass
            for item in snapshot["open_algo_orders"]:
                algo_id = _algo_id(item)
                if algo_id is None or not str(item.get("client_algo_id") or "").startswith(CLIENT_PREFIX):
                    continue
                owned_algo_ids: set[int] = set()
                for plan in state.get("plans", {}).values():
                    if not can_mutate_lifecycle(plan):
                        continue
                    ownership, matched_ids, _missing_ids = _protection_classification(
                        plan,
                        snapshot,
                        plans=[candidate for candidate in state.get("plans", {}).values() if isinstance(candidate, dict)],
                    )
                    if ownership == "MATCHED":
                        owned_algo_ids.update(matched_ids)
                if algo_id in owned_algo_ids:
                    try:
                        await client.signed("DELETE", "/fapi/v1/algoOrder", {"symbol": item["symbol"], "algoId": algo_id})
                        cancelled_algos += 1
                    except BinanceDemoError:
                        pass
            if body.close_positions:
                for item in snapshot["positions"]:
                    if await close_symbol_position(client, item["symbol"]) is not None:
                        closed_positions += 1
            state["armed_until"] = 0
            for plan in state.get("plans", {}).values():
                if not can_mutate_lifecycle(plan):
                    continue
                if plan.get("status") not in {"KAPANDI", "İPTAL"}:
                    plan["status"] = "ACİL DURDURULDU"
            persist_runtime(state)
            add_event(state, "ACİL DEMO DURDURMA", f"{cancelled_orders} giriş, {cancelled_algos} koruma iptal; {closed_positions} Demo pozisyon kapatma emri.")
            return {
                "ok": True,
                "cancelled_bot_orders": cancelled_orders,
                "cancelled_bot_algos": cancelled_algos,
                "closed_demo_positions": closed_positions,
                "armed": False,
                "real_trading_locked": True,
            }
        except BinanceDemoError as exc:
            raise safe_exchange_error(exc) from exc
