"""V25 Live Guard: fail-closed Binance USD-M Futures execution plane.

Live trading is compiled as a separate transport from Demo.  It starts read
only and requires all of the following before an entry can be submitted:
encrypted local credentials, a 24-hour key-bound local consent, a completed
Demo evidence certificate, an acknowledged risk policy, and a five-minute
owner arm.  Cancellation, protection repair, and tracked-position closure are
always available because they reduce risk.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import logging
import math
import os
import re
import time
import traceback
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal
from urllib.parse import urlencode

import httpx
import websockets
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .analysis import analyze
from .binance_rate_limit import (
    BINANCE_RATE_LIMITER,
    BINANCE_REQUEST_WEIGHT_LIMIT_1M,
    BINANCE_REQUEST_WEIGHT_THRESHOLD_RATIO,
    RATE_LIMIT_AWARE_BACKOFF_ENABLED,
    RATE_LIMIT_PROACTIVE_WAIT_SECONDS,
    retry_after_seconds,
)
from .binance_demo import (
    BinanceDemoError,
    account_snapshot,
    classify_algo_snapshot_payload,
    decimal_text,
    floor_step,
    normalize_symbol,
    response_rows,
    round_tick,
    validate_levels,
    verify_leverage_response,
    verify_symbol_configuration,
)
from .credential_store import load_live_consent
from .execution_core import (
    DEFAULT_EXECUTION_POLICY,
    configured_min_confidence,
    configured_mtf_allow_either_timeframe,
    LIVE_CLIENT_PREFIX,
    V25_VERSION,
    credential_fingerprint,
    daily_execution_metrics,
    evaluate_entry_gates,
    policy_digest,
    release_gates,
    release_ready,
    risk_sized_order,
    sanitize_execution_policy,
)
from .exchange_connections import session_credentials_for_identity, session_credentials_for_request, session_id
from .local_storage import DATA_DIR, migrate_legacy_files
from .trade_review import write_trade_review
from .v21_demo import certificate_payload
from .v22_commercial import authenticated_user


logger = logging.getLogger(__name__)
AUTOMATION_TELEMETRY_INTERVAL = 30.0
_automation_telemetry_at: dict[str, float] = {}


def automation_telemetry(message: str, *, reason: str | None = None) -> None:
    key = reason or message
    now = time.monotonic()
    if now - _automation_telemetry_at.get(key, 0.0) < AUTOMATION_TELEMETRY_INTERVAL:
        return
    _automation_telemetry_at[key] = now
    logger.info(message)


LIVE_REST_BASE = "https://fapi.binance.com"
LIVE_WS_BASE = "wss://fstream.binance.com/private"
LIVE_ARM_SECONDS = 24 * 60 * 60
LIVE_AUTO_SESSION_SECONDS = 60 * 60
LIVE_CONSENT_GRACE_SECONDS = 15 * 60
RECONCILE_SECONDS = 10
MONITORING_CREDENTIALS_STALE_SECONDS = 90
ANALYSIS_TIMEOUT_SECONDS = 15
try:
    TRANSIENT_RECONCILIATION_RECOVERY_SECONDS = max(1.0, min(300.0, float(os.getenv("PROTREBOT_TRANSIENT_RECONCILIATION_RECOVERY_SECONDS", "60"))))
except (TypeError, ValueError):
    TRANSIENT_RECONCILIATION_RECOVERY_SECONDS = 60.0
MAX_EVENTS = 500
MAX_PLANS = 250
MAX_CONFIDENCE_REJECTION_HISTORY = 20
MTF_HISTORY_SECONDS = 48 * 60 * 60
MAX_MTF_HISTORY_RECORDS = 100_000
PROVENANCE_STATES = {"NO_PROVENANCE", "PROVISIONAL", "CONFIRMED", "BROKEN"}
PROTECTION_STATES = {"MATCHED", "MISSING", "UNKNOWN"}
MARKET_SCAN_LIMIT = 100
DEEP_ANALYSIS_LIMIT = 50
MIN_24H_QUOTE_VOLUME = 1_000_000.0
MIN_24H_MOVE_PCT = 0.25
BLOCKED_BASE_ASSETS = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USD1", "USDE", "USDS"}

PUBLIC_PATHS = {
    "/fapi/v1/time",
    "/fapi/v1/exchangeInfo",
    "/fapi/v1/ticker/price",
    "/fapi/v1/ticker/bookTicker",
    "/fapi/v1/ticker/24hr",
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

router = APIRouter(prefix="/api/v25", tags=["V25 Live Guard"])
migrate_legacy_files(("v25_execution_state.json", "v25_execution_state.backup.json"))
STATE_PATH = DATA_DIR / "v25_execution_state.json"
BACKUP_PATH = DATA_DIR / "v25_execution_state.backup.json"


class LiveExchangeError(RuntimeError):
    def __init__(self, message: str, *, http_status: int = 502, exchange_code: int | None = None, unknown_execution: bool = False, timed_out: bool = False, transient_read_failure: bool = False, request_method: str | None = None, request_path: str | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.exchange_code = exchange_code
        self.unknown_execution = unknown_execution
        self.timed_out = timed_out
        self.transient_read_failure = transient_read_failure
        self.request_method = request_method
        self.request_path = request_path


class LiveRateLimitError(LiveExchangeError):
    def __init__(self, message: str, *, retry_after: int | None = None, exchange_code: int | None = None) -> None:
        super().__init__(message, http_status=429, exchange_code=exchange_code)
        self.retry_after = retry_after


def rate_limit_backoff_seconds(exc: LiveRateLimitError, generic_backoff: int) -> int:
    if RATE_LIMIT_AWARE_BACKOFF_ENABLED and exc.retry_after is not None:
        return exc.retry_after
    return generic_backoff


def lock_live_execution(
    state: dict[str, Any],
    reason: str,
    *,
    unknown: bool = False,
    symbol: str | None = None,
    client_id: str | None = None,
) -> None:
    state["armed_until"] = 0.0
    state["real_trading_locked"] = True
    state["live_auto_trade"] = False
    state["auto"].update({"enabled": False, "session_until": 0.0})
    state["execution_state"] = "UNKNOWN" if unknown else "LOCKED"
    state["reconciliation_required"] = bool(unknown)
    state.setdefault("emergency", {"active": False, "triggered_at": None, "reason": ""})
    state["emergency"].update({"active": True, "triggered_at": now_iso(), "reason": reason})
    add_event(
        state,
        "LIVE_UNKNOWN_EXECUTION" if unknown else "LIVE_FAIL_CLOSED",
        "Canlı yürütme belirsiz; yeni emirler kilitlendi ve manuel uzlaştırma gerekiyor." if unknown else "Canlı yürütme güvenlik nedeniyle kilitlendi.",
        reason=reason,
        execution_state=state["execution_state"],
        symbol=symbol,
        client_order_id_suffix=str(client_id or "")[-8:] or None,
        reconciliation_required=bool(unknown),
        unknown_execution=unknown,
    )


def unresolved_execution_evidence(state: dict[str, Any]) -> bool:
    """Return whether state contains evidence of an unresolved LIVE execution."""
    events = state.get("events") if isinstance(state.get("events"), list) else []
    for event in events:
        if not isinstance(event, dict):
            continue
        kind = str(event.get("kind") or "")
        if kind == "UNKNOWN_ORDER_RECONCILED":
            break
        if kind in {"UNKNOWN_ORDER_STATE", "LIVE_EXCEPTION_UNKNOWN"}:
            return True
        if kind == "LIVE_UNKNOWN_EXECUTION":
            if event.get("unknown_execution") is True or event.get("reason") != "RECONCILIATION_FAILURE":
                return True

    plans = state.get("plans") if isinstance(state.get("plans"), dict) else {}
    active_plan_ids: set[str] = set()
    for plan_id, plan in plans.items():
        if not isinstance(plan, dict):
            continue
        status = str(plan.get("status") or "").upper()
        if status not in {"KAPANDI", "İPTAL", "CLOSED", "CANCELLED", "CLOSED_CONFIRMED"}:
            active_plan_ids.add(str(plan_id))
        if plan.get("provenance_state") not in {None, "CONFIRMED"} or plan.get("protection_state") == "UNKNOWN":
            return True
        if plan.get("protection_cleanup_state") in {"UNKNOWN", "RETRY_REQUIRED"}:
            return True
    if active_plan_ids:
        return True

    intents = state.get("intents") if isinstance(state.get("intents"), dict) else {}
    for intent_id, intent in intents.items():
        if not isinstance(intent, dict):
            continue
        client_id = str(intent.get("client_order_id") or "")
        if not client_id.startswith(LIVE_CLIENT_PREFIX):
            continue
        resolved = any(
            str(plan.get("intent_id") or "") == str(intent_id)
            or str(plan.get("entry_client_order_id") or "") == client_id
            for plan in plans.values()
            if isinstance(plan, dict)
        )
        if not resolved or active_plan_ids:
            return True
    return False


def lock_reconciliation_failure(state: dict[str, Any]) -> None:
    """Fail closed after reconciliation without claiming an unknown order."""
    auto = state.get("auto") if isinstance(state.get("auto"), dict) else {}
    session_until = float(auto.get("session_until") or 0)
    auto_session_was_active = bool(auto.get("enabled")) and session_until > time.time()
    state["transient_reconciliation"] = {
        "failed_at_epoch": time.time(),
        "auto_session_was_active": auto_session_was_active,
        "auto_session_until": session_until if auto_session_was_active else 0.0,
    } if auto_session_was_active else None
    lock_live_execution(state, "RECONCILIATION_FAILURE", unknown=False)
    state["armed_until"] = 0.0
    state["real_trading_locked"] = True
    state["live_auto_trade"] = False
    state["auto"].update({"enabled": False, "session_until": 0.0})
    state["execution_state"] = "LOCKED"
    state["reconciliation_required"] = True


def clear_clean_reconciliation_state(state: dict[str, Any], snapshot: dict[str, Any]) -> bool:
    """Clear only a stale technical UNKNOWN after a complete clean snapshot."""
    emergency = state.get("emergency") if isinstance(state.get("emergency"), dict) else {}
    if emergency.get("reason") not in {"RECONCILIATION_FAILURE", "LIVE_EXCEPTION"}:
        return False
    if state.get("execution_state") not in {"UNKNOWN", "LOCKED"} or (not state.get("reconciliation_required") and emergency.get("reason") != "LIVE_EXCEPTION"):
        return False
    required_snapshot_keys = {
        "wallet_balance",
        "available_balance",
        "positions",
        "open_orders",
        "open_algo_orders",
        "open_algo_orders_available",
        "algo_orders_quality",
    }
    if not isinstance(snapshot, dict) or not required_snapshot_keys.issubset(snapshot):
        return False
    if any(not isinstance(snapshot.get(key), list) for key in ("positions", "open_orders", "open_algo_orders")):
        return False
    for balance_key in ("wallet_balance", "available_balance"):
        balance = snapshot.get(balance_key)
        if isinstance(balance, bool) or not isinstance(balance, (int, float, Decimal)) or not math.isfinite(float(balance)):
            return False
    if snapshot.get("open_algo_orders_available") is not True or snapshot.get("algo_orders_quality") not in {"VALID_EMPTY", "VALID_ORDERS"}:
        return False
    collection_fields = {
        "positions": {"symbol", "position_side", "direction", "quantity"},
        "open_orders": {"symbol", "order_id", "client_order_id", "side", "type", "status"},
        "open_algo_orders": {"symbol", "algo_id", "client_algo_id", "side", "type", "status"},
    }
    for collection, required_fields in collection_fields.items():
        for row in snapshot[collection]:
            if not isinstance(row, dict) or not required_fields.issubset(row):
                return False
    for row in [*snapshot["open_orders"], *snapshot["open_algo_orders"]]:
        if any(
            str(row.get(key) or "").startswith(LIVE_CLIENT_PREFIX)
            for key in ("clientOrderId", "client_order_id", "clientAlgoId", "client_algo_id")
        ):
            return False
    if unresolved_execution_evidence(state):
        return False
    transient = state.get("transient_reconciliation") if isinstance(state.get("transient_reconciliation"), dict) else None
    failed_at = float(transient.get("failed_at_epoch") or 0) if transient else 0.0
    recovery_deadline = failed_at + TRANSIENT_RECONCILIATION_RECOVERY_SECONDS
    auto_session_until = float(transient.get("auto_session_until") or 0) if transient else 0.0
    auto_recovered = bool(
        transient
        and transient.get("auto_session_was_active") is True
        and failed_at > 0
        and time.time() <= recovery_deadline
        and auto_session_until > time.time()
    )
    state["execution_state"] = "LOCKED"
    state["reconciliation_required"] = False
    state["real_trading_locked"] = not auto_recovered
    state["live_auto_trade"] = False
    state["armed_until"] = 0.0
    state["auto"].update({"enabled": False, "session_until": 0.0})
    state["emergency"].update({"active": False, "reason": "RECONCILIATION_CLEAN"})
    add_event(state, "RECONCILIATION_CLEAN", "Tam ve temiz canlı hesap uzlaştırması tamamlandı; canlı işlem kilidi korunuyor.")
    if auto_recovered:
        state["real_trading_locked"] = False
        state["live_auto_trade"] = True
        state["auto"].update({"enabled": True, "session_until": auto_session_until, "last_skip_reason": None, "last_error": None})
        add_event(
            state,
            "AUTO_RECOVERED_FROM_TRANSIENT_RECONCILIATION",
            "Geçici reconciliation kesintisi temiz snapshot ile pencere içinde düzeldi; Auto Trade devam ediyor.",
            recovery_window_seconds=TRANSIENT_RECONCILIATION_RECOVERY_SECONDS,
            failed_at_epoch=failed_at,
        )
    state["transient_reconciliation"] = None
    return True


TRANSIENT_READ_PATHS = PUBLIC_PATHS | {
    "/fapi/v3/account",
    "/fapi/v3/positionRisk",
    "/fapi/v1/symbolConfig",
    "/fapi/v1/openOrders",
    "/fapi/v1/openAlgoOrders",
    "/fapi/v1/positionSide/dual",
}
TRANSIENT_READ_ERRORS = (
    httpx.RemoteProtocolError,
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.PoolTimeout,
)


def is_transient_read_request(method: str, path: str, error: BaseException | None = None) -> bool:
    return method.upper() == "GET" and path in TRANSIENT_READ_PATHS and (error is None or isinstance(error, TRANSIENT_READ_ERRORS))


def mark_transient_market_data_failure(state: dict[str, Any], error: LiveExchangeError) -> bool:
    if not error.transient_read_failure:
        return False
    auto = state.get("auto") if isinstance(state.get("auto"), dict) else {}
    session_until = float(auto.get("session_until") or 0)
    if not bool(auto.get("enabled")) or session_until <= time.time():
        return False
    state["transient_market_data"] = {
        "failed_at_epoch": time.time(),
        "auto_session_until": session_until,
        "method": error.request_method,
        "endpoint": error.request_path,
        "error_type": type(error.__cause__).__name__ if error.__cause__ else None,
    }
    lock_live_execution(state, "TRANSIENT_MARKET_DATA_FAILURE", unknown=False)
    state["armed_until"] = 0.0
    state["execution_state"] = "LOCKED"
    state["reconciliation_required"] = False
    return True


def recover_transient_market_data(state: dict[str, Any], method: str, path: str) -> bool:
    if not isinstance(state, dict):
        return False
    transient = state.get("transient_market_data") if isinstance(state.get("transient_market_data"), dict) else None
    if not transient or not is_transient_read_request(method, path):
        return False
    session_until = float(transient.get("auto_session_until") or 0)
    if time.time() > float(transient.get("failed_at_epoch") or 0) + TRANSIENT_RECONCILIATION_RECOVERY_SECONDS or session_until <= time.time():
        state["transient_market_data"] = None
        return False
    if unresolved_execution_evidence(state):
        return False
    state["execution_state"] = "LOCKED"
    state["reconciliation_required"] = False
    state["real_trading_locked"] = False
    state["live_auto_trade"] = True
    state["auto"].update({"enabled": True, "session_until": session_until, "last_skip_reason": None, "last_error": None})
    state["emergency"].update({"active": False, "reason": "TRANSIENT_MARKET_DATA_RECOVERED"})
    add_event(
        state,
        "AUTO_RECOVERED_FROM_TRANSIENT_MARKET_DATA",
        "Geçici market-data GET kesintisi sonraki başarılı okuma ile düzeldi; Auto Trade devam ediyor.",
        endpoint=transient.get("endpoint"),
        failed_at_epoch=transient.get("failed_at_epoch"),
    )
    state["transient_market_data"] = None
    return True


class PolicyUpdate(BaseModel):
    allowed_symbols: list[str] | None = None
    interval: Literal["1m", "5m", "15m", "1h", "4h"] | None = None
    allow_long: bool | None = None
    allow_short: bool | None = None
    max_margin_per_trade: float | None = Field(default=None, ge=5, le=100)
    max_loss_per_trade: float | None = Field(default=None, ge=0.5, le=25)
    max_leverage: int | None = Field(default=None, ge=1, le=50)
    max_positions: int | None = Field(default=None, ge=1, le=5)
    max_total_exposure_usdt: float | None = Field(default=None, ge=25, le=250)
    daily_loss_limit: float | None = Field(default=None, ge=5, le=100)
    daily_trade_limit: int | None = Field(default=None, ge=1, le=12)
    consecutive_loss_limit: int | None = Field(default=None, ge=1, le=10)
    min_confidence: int | None = Field(default=None, ge=70, le=95)
    max_trap_score: int | None = Field(default=None, ge=10, le=60)
    max_spread_bps: float | None = Field(default=None, ge=0.5, le=25)
    max_stop_distance_pct: float | None = Field(default=None, ge=0.25, le=5)
    atr_stop_multiplier: float | None = Field(default=None, ge=0.5, le=3)
    fee_bps_per_side: float | None = Field(default=None, ge=0, le=25)
    slippage_bps_per_side: float | None = Field(default=None, ge=0, le=30)
    minimum_net_reward_usdt: float | None = Field(default=None, ge=0, le=25)
    scan_seconds: int | None = Field(default=None, ge=30, le=300)


class Confirmation(BaseModel):
    confirmation: str = Field(min_length=1, max_length=80)


class LiveOrderRequest(BaseModel):
    symbol: str = Field(min_length=5, max_length=20)
    direction: Literal["LONG", "SHORT"]
    order_type: Literal["MARKET", "LIMIT"] = "MARKET"
    margin_usdt: float = Field(ge=5, le=100)
    leverage: int = Field(ge=1, le=50)
    limit_price: float | None = Field(default=None, gt=0)
    stop_loss: float = Field(gt=0)
    tp1: float = Field(gt=0)
    tp2: float = Field(gt=0)
    tp3: float = Field(gt=0)
    atr: float | None = Field(default=None, gt=0)
    intent_id: str | None = Field(default=None, min_length=8, max_length=96)


class RiskPreviewRequest(BaseModel):
    entry: float = Field(gt=0)
    stop_loss: float = Field(gt=0)
    margin_usdt: float = Field(ge=5, le=100)
    leverage: int = Field(ge=1, le=50)
    atr: float | None = Field(default=None, gt=0)


class ManualLiveOrderRequest(LiveOrderRequest):
    confirmation: str = Field(min_length=1, max_length=64)


class CloseRequest(BaseModel):
    plan_id: str = Field(min_length=6, max_length=64)
    confirmation: str = Field(min_length=1, max_length=64)


class EmergencyRequest(BaseModel):
    confirmation: str = Field(min_length=1, max_length=64)
    close_tracked_positions: bool = True


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def prune_mtf_decision_history(records: Any, *, now_epoch: float | None = None) -> list[dict[str, Any]]:
    """Keep timestamped MTF decisions inside the rolling persistence window."""
    if not isinstance(records, list):
        return []
    cutoff = (time.time() if now_epoch is None else float(now_epoch)) - MTF_HISTORY_SECONDS
    kept: list[dict[str, Any]] = []
    for item in records:
        if not isinstance(item, dict):
            continue
        try:
            timestamp_epoch = float(item.get("timestamp_epoch"))
        except (TypeError, ValueError):
            continue
        if timestamp_epoch < cutoff:
            continue
        entry_direction = str(item.get("entry_direction") or "BEKLE").upper()
        if entry_direction not in {"LONG", "SHORT", "BEKLE"}:
            entry_direction = "BEKLE"
        kept.append({
            "timestamp": str(item.get("timestamp") or "")[:64],
            "timestamp_epoch": timestamp_epoch,
            "symbol": str(item.get("symbol") or "")[:32],
            "confidence": float(item.get("confidence") or 0),
            "entry_direction": entry_direction,
            "1h_direction": str(item.get("1h_direction") or "BEKLE")[:16],
            "4h_direction": str(item.get("4h_direction") or "BEKLE")[:16],
            "mtf_mismatch": bool(item.get("mtf_mismatch")),
        })
    return kept[-MAX_MTF_HISTORY_RECORDS:]


def summarize_mtf_relaxation(records: Any, *, min_confidence: float = 80.0) -> dict[str, Any]:
    """Estimate which high-confidence strict MTF rejections pass an OR gate."""
    rows = prune_mtf_decision_history(records)
    high_confidence = [
        row for row in rows
        if row["entry_direction"] in {"LONG", "SHORT"} and row["confidence"] >= min_confidence
    ]
    strict_rejections = [row for row in high_confidence if row["mtf_mismatch"]]
    rescued = [
        row for row in strict_rejections
        if row["1h_direction"] == row["entry_direction"] or row["4h_direction"] == row["entry_direction"]
    ]
    return {
        "min_confidence": min_confidence,
        "high_confidence_signals": len(high_confidence),
        "strict_mtf_rejections": len(strict_rejections),
        "rescued_by_or_gate": len(rescued),
        "rescue_rate": round(len(rescued) / len(strict_rejections), 4) if strict_rejections else 0.0,
    }


def initial_state() -> dict[str, Any]:
    default_policy = dict(DEFAULT_EXECUTION_POLICY)
    default_policy["min_confidence"] = configured_min_confidence()
    default_policy["mtf_allow_either_timeframe"] = configured_mtf_allow_either_timeframe()
    return {
        "version": V25_VERSION,
        "policy": default_policy,
        "policy_ack_digest": None,
        "connected": False,
        "connection": {"last_checked": None, "last_error": None, "clock_offset_ms": None},
        "stream": {
            "status": "ANAHTAR BEKLİYOR",
            "transport": "REST UZLAŞTIRMA",
            "last_event": None,
            "last_error": None,
            "event_count": 0,
            "reconnect_count": 0,
        },
        "snapshot": None,
        "snapshot_session_id": None,
        "live_session_authorization": {"session_id": "", "user_id": "", "fingerprint": ""},
        "events": [],
        "mtf_decision_history": [],
        "plans": {},
        "intents": {},
        "armed_until": 0.0,
        "auto": {"enabled": False, "busy": False, "cycles": 0, "last_scan": None, "last_scan_stats": None, "confidence_rejection_history": [], "last_skip_reason": None, "last_cycle_stage": "idle", "last_decision": "Kullanıcı onayı bekleniyor.", "last_error": None, "session_until": 0.0},
            "auto_authorization": {"session_id": "", "user_id": "", "fingerprint": "", "expires_at_epoch": 0.0},
        "real_trading_locked": True,
        "live_auto_trade": False,
        "emergency": {"active": False, "triggered_at": None, "reason": None},
        # Consent persists only as a fingerprint-bound, expiring record. The
        # execution authority itself remains process-bound and is never restored.
        "web_consent": {"accepted_at": None, "expires_at_epoch": 0.0, "key_fingerprint": None},
        "duplicate_blocks": 0,
        "protection_repairs": 0,
        "recovery_ready": False,
        "recovery_loaded": True,
        "recovery_error": None,
        "execution_state": "LOCKED",
        "reconciliation_required": False,
        "transient_reconciliation": None,
        "transient_market_data": None,
        # Order-submission failures are surfaced separately from connection
        # health so a rejected order never overwrites the connectivity diagnostic.
        "last_order_error": None,
        "monitoring_credentials_stale_since": None,
        "trade_review": None,
    }


def sanitized_state(payload: Any) -> dict[str, Any]:
    base = initial_state()
    if not isinstance(payload, dict):
        return base
    base["policy"] = sanitize_execution_policy(payload.get("policy"))
    base["policy"]["mtf_allow_either_timeframe"] = configured_mtf_allow_either_timeframe()
    base["policy_ack_digest"] = payload.get("policy_ack_digest") if payload.get("policy_ack_digest") == policy_digest(base["policy"]) else None
    last_order_error = payload.get("last_order_error")
    if isinstance(last_order_error, dict) and last_order_error.get("created_at"):
        base["last_order_error"] = {
            "message": str(last_order_error.get("message") or "")[:240],
            "symbol": str(last_order_error.get("symbol") or "")[:32] or None,
            "created_at": str(last_order_error.get("created_at") or "")[:64],
        }
    authorization = payload.get("live_session_authorization")
    if isinstance(authorization, dict):
        base["live_session_authorization"] = {
            "session_id": str(authorization.get("session_id") or "")[:128],
            "user_id": str(authorization.get("user_id") or "")[:128],
            "fingerprint": str(authorization.get("fingerprint") or "")[:128],
        }
    consent = payload.get("web_consent")
    if isinstance(consent, dict):
        expires_at = float(consent.get("expires_at_epoch") or 0)
        fingerprint = str(consent.get("key_fingerprint") or "").strip()
        if expires_at + LIVE_CONSENT_GRACE_SECONDS > time.time() and fingerprint:
            base["web_consent"] = {
                "accepted_at": str(consent.get("accepted_at") or "")[:64] or None,
                "expires_at_epoch": expires_at,
                "key_fingerprint": fingerprint[:128],
            }
    for key in ("events", "plans", "intents", "duplicate_blocks", "protection_repairs"):
        if key in payload and isinstance(payload[key], type(base[key])):
            base[key] = payload[key]
    if isinstance(payload.get("trade_review"), dict):
        base["trade_review"] = {
            "review_key": str(payload["trade_review"].get("review_key") or "")[:256],
            "path": str(payload["trade_review"].get("path") or "")[:512],
            "generated_at": str(payload["trade_review"].get("generated_at") or "")[:64],
            "symbols": [str(item)[:32] for item in payload["trade_review"].get("symbols", []) if item],
        }
    base["mtf_decision_history"] = prune_mtf_decision_history(payload.get("mtf_decision_history"))
    if isinstance(payload.get("emergency"), dict):
        base["emergency"] = {
            "active": bool(payload["emergency"].get("active")),
            "triggered_at": payload["emergency"].get("triggered_at"),
            "reason": str(payload["emergency"].get("reason") or "")[:240] or None,
        }
    if payload.get("execution_state") in {"LOCKED", "UNKNOWN"}:
        base["execution_state"] = payload["execution_state"]
    base["reconciliation_required"] = bool(payload.get("reconciliation_required"))
    transient = payload.get("transient_reconciliation")
    if isinstance(transient, dict):
        base["transient_reconciliation"] = {
            "failed_at_epoch": float(transient.get("failed_at_epoch") or 0),
            "auto_session_was_active": bool(transient.get("auto_session_was_active")),
            "auto_session_until": float(transient.get("auto_session_until") or 0),
        }
    base["events"] = base["events"][:MAX_EVENTS]
    if len(base["plans"]) > MAX_PLANS:
        rows = sorted(base["plans"].items(), key=lambda item: item[1].get("created_at", ""), reverse=True)[:MAX_PLANS]
        base["plans"] = dict(rows)
    if len(base["intents"]) > 1_000:
        rows = sorted(base["intents"].items(), key=lambda item: item[1].get("created_at", ""), reverse=True)[:1_000]
        base["intents"] = dict(rows)
    for plan in base["plans"].values():
        if not isinstance(plan, dict):
            continue
        plan.setdefault("provenance_state", "NO_PROVENANCE")
        if plan.get("provenance_state") not in PROVENANCE_STATES:
            plan["provenance_state"] = "BROKEN"
        plan.setdefault("protection_state", "UNKNOWN")
        if plan.get("protection_state") not in PROTECTION_STATES:
            plan["protection_state"] = "UNKNOWN"
        plan.setdefault("protection_status", plan["protection_state"])
        if plan.get("protection_status") not in PROTECTION_STATES:
            plan["protection_status"] = plan["protection_state"]
        plan.setdefault("protection_match_confidence", "NONE")
        plan.setdefault("protection_match_reason", "NOT_RECONCILED")
    # Entry authority never survives a process restart.
    base["auto"]["last_decision"] = "Güvenli yeniden başlatma: canlı otomasyon yeniden onay bekliyor."
    base["real_trading_locked"] = True
    base["live_auto_trade"] = False
    base["execution_state"] = "UNKNOWN" if base["reconciliation_required"] else "LOCKED"
    base["auto"].update({"enabled": False, "session_until": 0.0, "busy": False})
    return base


def load_state() -> dict[str, Any]:
    for path in (STATE_PATH, BACKUP_PATH):
        try:
            return sanitized_state(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
    return initial_state()


def persist_state(state: dict[str, Any]) -> None:
    application = state.get("_app")
    pool = getattr(getattr(application, "state", None), "db_pool", None)
    if application is not None and pool is not None:
        tasks = getattr(application.state, "_v25_persistence_tasks", None)
        if not isinstance(tasks, set):
            tasks = set()
            application.state._v25_persistence_tasks = tasks
        payload = sanitized_state(state)
        task = asyncio.create_task(_persist_state_snapshot(application, payload))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return
    if os.getenv("DATABASE_URL", "").strip():
        state["recovery_ready"] = False
        state["recovery_error"] = "PostgreSQL persistence unavailable."
        state["armed_until"] = 0.0
        state["real_trading_locked"] = True
        state["live_auto_trade"] = False
        state["auto"].update({"enabled": False, "session_until": 0.0})
        state["execution_state"] = "LOCKED"
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = sanitized_state(state)
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    temporary = STATE_PATH.with_suffix(".tmp")
    temporary.write_text(body, encoding="utf-8")
    if STATE_PATH.exists():
        try:
            BACKUP_PATH.write_bytes(STATE_PATH.read_bytes())
        except OSError:
            pass
    temporary.replace(STATE_PATH)


V25_SNAPSHOT_KEY = "v25_live:state"

_SENSITIVE_EXCEPTION_VALUE = re.compile(
    r"(?i)(\b(?:api[_-]?key|secret(?:[_-]?key)?|authorization|token|signature|password|credential(?:s)?)\b\s*[:=]\s*)([^\s,;}\]]+)"
)


def sanitized_exception_message(exc: BaseException | str) -> str:
    message = str(exc)[:240]
    return _SENSITIVE_EXCEPTION_VALUE.sub(r"\1[REDACTED]", message)[:240]


def latest_reconciliation_diagnostic(state: dict[str, Any]) -> dict[str, Any] | None:
    events = state.get("events") if isinstance(state.get("events"), list) else []
    for event in events:
        if not isinstance(event, dict) or event.get("kind") not in {"RECONCILIATION_FAILURE_DIAGNOSTIC", "LIVE_ORDER_EXCEPTION_DIAGNOSTIC"}:
            continue
        return {
            "exception_type": str(event.get("exception_type") or "")[:120],
            "exception_message": sanitized_exception_message(str(event.get("exception_message") or "")),
            "reconciliation_stage": str(event.get("reconciliation_stage") or "")[:120],
            "source": str(event.get("source") or "")[:160],
            "function": str(event.get("function") or "")[:160],
            "line": int(event.get("line") or 0),
            "timestamp": event.get("created_at"),
        }
    return None


async def _persist_state_snapshot(application: Any, payload: dict[str, Any]) -> None:
    pool = getattr(application.state, "db_pool", None)
    if pool is None:
        return
    await pool.execute(
        """
        INSERT INTO application_state_snapshots (state_key, updated_at, payload)
        VALUES ($1, NOW(), $2::jsonb)
        ON CONFLICT (state_key) DO UPDATE
        SET updated_at = NOW(), payload = EXCLUDED.payload
        """,
        V25_SNAPSHOT_KEY,
        json.dumps(payload, ensure_ascii=False),
    )


async def restore_v25_state(application: Any) -> bool:
    state = application.state.v25_execution
    pool = getattr(application.state, "db_pool", None)
    if pool is None:
        return False
    try:
        row = await pool.fetchrow("SELECT payload FROM application_state_snapshots WHERE state_key = $1", V25_SNAPSHOT_KEY)
        if row is not None:
            payload = row["payload"]
            restored = sanitized_state(payload)
            state.update(restored)
        state["_app"] = application
        state["recovery_loaded"] = True
        state["recovery_ready"] = False
        state["recovery_error"] = None
        return True
    except Exception:
        state["recovery_ready"] = False
        state["recovery_error"] = "PostgreSQL recovery failed."
        return False


def add_event(state: dict[str, Any], kind: str, message: str, **extra: Any) -> dict[str, Any]:
    row = {"id": uuid.uuid4().hex, "kind": kind, "message": message[:400], "created_at": now_iso(), **extra}
    state.setdefault("events", []).insert(0, row)
    del state["events"][MAX_EVENTS:]
    return row


def live_credentials_status(request: Request | None = None) -> tuple[str, str, str | None]:
    if request is None:
        return "", "", None
    api_key, secret_key = session_credentials_for_request(request, "LIVE", active_only=True)
    fingerprint = credential_fingerprint(api_key)
    return api_key, secret_key, fingerprint if len(secret_key) >= 10 else None


def usable_live_credentials(credentials: tuple[str, str] | None) -> bool:
    return bool(
        credentials
        and len(credentials) == 2
        and all(isinstance(value, str) and len(value) >= 10 for value in credentials)
    )


def normalize_consent_fingerprint(value: str | None) -> str:
    return str(value or "").strip().upper().removeprefix("SHA256:")


def update_account_snapshot(state: dict[str, Any], snapshot: dict[str, Any], *, session_binding: str | None = None) -> None:
    state["snapshot"] = snapshot
    if session_binding is not None:
        state["snapshot_session_id"] = session_binding


def consent_status(
    state: dict[str, Any] | None = None,
    request: Request | None = None,
    credentials: tuple[str, str] | None = None,
) -> dict[str, Any]:
    if credentials is None:
        api_key, secret_key, fingerprint = live_credentials_status(request)
        credentials = (api_key, secret_key)
    else:
        api_key, secret_key = credentials
        fingerprint = credential_fingerprint(api_key) if len(secret_key) >= 10 else None
    local_payload = load_live_consent()
    web_payload = state.get("web_consent", {}) if isinstance(state, dict) else {}
    candidates = [payload for payload in (web_payload, local_payload) if isinstance(payload, dict)]
    matching = [
        candidate for candidate in candidates
        if normalize_consent_fingerprint(candidate.get("key_fingerprint")) == normalize_consent_fingerprint(fingerprint)
        and float(candidate.get("expires_at_epoch") or 0) > 0
    ]
    now = time.time()
    payload = next((candidate for candidate in matching if float(candidate.get("expires_at_epoch") or 0) > now), None)
    if payload is None:
        payload = next((candidate for candidate in matching if float(candidate.get("expires_at_epoch") or 0) + LIVE_CONSENT_GRACE_SECONDS > now), {})
    expires = float(payload.get("expires_at_epoch") or 0)
    grace_until = expires + LIVE_CONSENT_GRACE_SECONDS if expires else 0.0
    active = bool(
        api_key and fingerprint and normalize_consent_fingerprint(payload.get("key_fingerprint")) == normalize_consent_fingerprint(fingerprint)
        and expires > now
    )
    grace_active = bool(not active and api_key and fingerprint and expires <= now < grace_until)
    return {
        "active": active,
        "accepted_at": payload.get("accepted_at") if active or grace_active else None,
        "expires_at": datetime.fromtimestamp(expires, timezone.utc).isoformat() if active or grace_active else None,
        "grace_active": grace_active,
        "grace_until": datetime.fromtimestamp(grace_until, timezone.utc).isoformat() if grace_active else None,
        "reauthorization_required": not active,
        "fingerprint": fingerprint,
        "storage": "SUNUCU_KALICI" if payload is web_payload and (active or grace_active) else "WINDOWS_DPAPI" if active or grace_active else "YOK",
    }


def consent_grace_expired(state: dict[str, Any]) -> bool:
    expires = float((state.get("web_consent") or {}).get("expires_at_epoch") or 0)
    return bool(expires and time.time() >= expires + LIVE_CONSENT_GRACE_SECONDS)


def execution_owner(request: Request) -> dict[str, Any]:
    """Use the same owner identity as the exchange session vault."""
    member = getattr(request.state, "member", None)
    if member and bool(getattr(request.state, "web_owner_authenticated", False)):
        return {"id": str(member.get("id") or ""), "role": "OWNER"}
    if member and member.get("role") == "OWNER":
        return member
    return authenticated_user(request, owner=True)


def is_armed(state: dict[str, Any]) -> bool:
    active = float(state.get("armed_until") or 0) > time.time()
    if not active:
        state["armed_until"] = 0.0
        if not auto_session_is_active(state):
            state["real_trading_locked"] = True
    return active


def live_execution_blocked(state: dict[str, Any]) -> bool:
    return bool(
        state.get("emergency", {}).get("active")
        or state.get("execution_state") == "UNKNOWN"
        or state.get("reconciliation_required")
    )


def auto_session_active(state: dict[str, Any]) -> bool:
    active = bool(state["auto"].get("enabled")) and float(state["auto"].get("session_until") or 0) > time.time()
    if not active:
        state["auto"]["enabled"] = False
        state["auto"]["session_until"] = 0.0
        state["live_auto_trade"] = False
        state["real_trading_locked"] = True
    return active


def auto_session_is_active(state: dict[str, Any]) -> bool:
    """Read auto-session status without changing the live execution lock."""
    return bool(state["auto"].get("enabled")) and float(state["auto"].get("session_until") or 0) > time.time()


class BinanceLiveClient:
    def __init__(self, http: httpx.AsyncClient, api_key: str, secret_key: str, *, require_credentials: bool = True, diagnostic_state: dict[str, Any] | None = None) -> None:
        if require_credentials and (len(api_key) < 10 or len(secret_key) < 10):
            raise LiveExchangeError("Canlı API bağlantısı aktif değil. Borsa Bağlantıları bölümünden gerçek hesap anahtarını kaydedip salt-okunur bağlantıyı aktifleştirin.", http_status=412)
        self.http = http
        self.api_key = api_key
        self.secret_key = secret_key
        self.diagnostic_state = diagnostic_state
        self.time_offset_ms = 0
        self.last_time_sync = 0.0
        self._clock_lock = asyncio.Lock()

    @property
    def last_used_weight_1m(self) -> int | None:
        return BINANCE_RATE_LIMITER.snapshot(LIVE_REST_BASE)["used_weight_1m"]

    async def public_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if path not in PUBLIC_PATHS:
            raise LiveExchangeError("İzin verilmeyen canlı piyasa API yolu.", http_status=500)
        return await self._request("GET", path, params or {}, signed=False)

    async def sync_clock(self) -> None:
        if time.monotonic() - self.last_time_sync < 30:
            return
        async with self._clock_lock:
            if time.monotonic() - self.last_time_sync < 30:
                return
            before = int(time.time() * 1000)
            payload = await self.public_get("/fapi/v1/time")
            after = int(time.time() * 1000)
            server_time = int(payload["serverTime"])
            self.time_offset_ms = server_time - ((before + after) // 2)
            self.last_time_sync = time.monotonic()

    async def signed(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        method = method.upper()
        if (method, path) not in PRIVATE_PATHS:
            raise LiveExchangeError("İzin verilmeyen canlı hesap API işlemi.", http_status=500)
        await self.sync_clock()
        payload = {key: value for key, value in dict(params or {}).items() if value is not None}
        payload["timestamp"] = int(time.time() * 1000) + self.time_offset_ms
        payload["recvWindow"] = 5000
        query = urlencode(list(payload.items()), doseq=True)
        signature = hmac.new(self.secret_key.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
        return await self._request(method, path, payload, signed=True, encoded_query=query, signature=signature)

    async def api_key_request(self, method: str, path: str) -> Any:
        """Call the official USER_STREAM endpoints without a request signature."""
        method = method.upper()
        if (method, path) not in API_KEY_PATHS:
            raise LiveExchangeError("İzin verilmeyen canlı kullanıcı akışı işlemi.", http_status=500)
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
        url = f"{LIVE_REST_BASE}{path}"
        if not url.startswith(f"{LIVE_REST_BASE}/"):
            raise LiveExchangeError("Canlı Binance sunucu kilidi doğrulanamadı.", http_status=500)
        headers = {"X-MBX-APIKEY": self.api_key} if signed or api_key_header else {}
        request_url = f"{url}?{encoded_query}&signature={signature}" if signed else url
        try:
            async with BINANCE_RATE_LIMITER.slot(LIVE_REST_BASE) as rate_limit:
                response = await self.http.request(method, request_url, params=None if signed else params, headers=headers)
                rate_limit.observe(response)
        except httpx.TimeoutException as exc:
            transient_read_failure = is_transient_read_request(method, path, exc)
            self._record_request_error(method, path, exc, timed_out=True, transient_read_failure=transient_read_failure)
            raise LiveExchangeError(
                "Canlı market-data okuması geçici olarak başarısız oldu." if transient_read_failure else "Canlı emir sonucu belirsiz; zaman aşımı sonrası yeni emir gönderilmedi.",
                unknown_execution=not transient_read_failure,
                timed_out=True,
                transient_read_failure=transient_read_failure,
                request_method=method,
                request_path=path,
            ) from exc
        except httpx.RequestError as exc:
            transient_read_failure = is_transient_read_request(method, path, exc)
            self._record_request_error(method, path, exc, transient_read_failure=transient_read_failure)
            raise LiveExchangeError(
                "Canlı market-data okuması geçici olarak başarısız oldu." if transient_read_failure else "Canlı emir sonucu belirsiz; ağ bağlantısı kesildi ve yeni emir gönderilmedi.",
                unknown_execution=not transient_read_failure,
                transient_read_failure=transient_read_failure,
                request_method=method,
                request_path=path,
            ) from exc
        if response.status_code >= 400:
            try:
                body = response.json()
            except (ValueError, json.JSONDecodeError):
                body = {}
            code = body.get("code") if isinstance(body, dict) else None
            message = str(body.get("msg") if isinstance(body, dict) else "Binance canlı işlemi reddetti.")
            if self.api_key:
                message = message.replace(self.api_key, "[gizli]")
            if response.status_code in {429, 418}:
                message = "Binance API hız sınırı; yeni emir gönderilmedi. Geri çekilme süresi bekleniyor."
                retry_after = retry_after_seconds(response)
                logger.warning(
                    "LIVE Binance rate limit: method=%s path=%s status=%s code=%s retry_after_seconds=%s used_weight_1m=%s",
                    method,
                    path,
                    response.status_code,
                    code,
                    retry_after,
                    self.last_used_weight_1m,
                )
                raise LiveRateLimitError(message, retry_after=retry_after, exchange_code=int(code) if isinstance(code, int) else None)
            unknown = response.status_code >= 500
            if unknown:
                message = "Emir yürütme sonucu belirsiz; benzersiz emir kimliğiyle sorgulanacak, kör tekrar yapılmayacak."
            raise LiveExchangeError(message, http_status=429 if response.status_code in {429, 418} else 502, exchange_code=int(code) if isinstance(code, int) else None, unknown_execution=unknown)
        recover_transient_market_data(self.diagnostic_state, method, path)
        try:
            return response.json()
        except (ValueError, json.JSONDecodeError):
            return {}

    def _record_request_error(self, method: str, path: str, error: BaseException, *, timed_out: bool = False, transient_read_failure: bool = False) -> None:
        state = self.diagnostic_state
        if not isinstance(state, dict):
            return
        timeout = getattr(self.http, "timeout", None)
        timeout_config = {
            "connect": getattr(timeout, "connect", None),
            "read": getattr(timeout, "read", None),
            "write": getattr(timeout, "write", None),
            "pool": getattr(timeout, "pool", None),
        }
        connection = state.get("connection") if isinstance(state.get("connection"), dict) else {}
        add_event(
            state,
            "LIVE_REQUEST_ERROR",
            "Binance canlı API isteği başarısız oldu; yeni emir gönderilmedi.",
            method=method.upper(),
            endpoint=path,
            exception_type="LiveExchangeError",
            httpx_error_type=type(error).__name__,
            error_message=sanitized_exception_message(str(error)),
            timed_out=timed_out,
            timeout_seconds=timeout_config,
            retry_count=int(connection.get("retry_count") or 0),
            request_attempt=1,
            retry_policy="NO_PER_REQUEST_RETRY",
            transient_read_failure=transient_read_failure,
        )


def client_for(
    application: Any,
    request: Request | None = None,
    credentials: tuple[str, str] | None = None,
) -> BinanceLiveClient:
    api_key, secret_key = credentials or live_credentials_status(request)[:2]
    return BinanceLiveClient(application.state.http, api_key, secret_key, diagnostic_state=getattr(application.state, "v25_execution", None))


def client_for_with_credentials(
    application: Any,
    credentials: tuple[str, str],
    request: Request | None = None,
) -> BinanceLiveClient:
    if "credentials" in inspect.signature(client_for).parameters:
        return client_for(application, request, credentials=credentials)
    return client_for(application, request)


def public_client_for(application: Any) -> BinanceLiveClient:
    """Public Futures market data remains visible before API setup."""
    return BinanceLiveClient(application.state.http, "", "", require_credentials=False, diagnostic_state=getattr(application.state, "v25_execution", None))


def safe_exchange_error(exc: LiveExchangeError | BinanceDemoError) -> HTTPException:
    code = getattr(exc, "exchange_code", None)
    suffix = f" (Binance kodu: {code})" if code is not None else ""
    return HTTPException(getattr(exc, "http_status", 502), f"{exc}{suffix}")


def validate_protection_readiness(spec: dict[str, Any], policy: dict[str, Any]) -> None:
    if not sanitize_execution_policy(policy).get("stop_required", True):
        raise LiveExchangeError("Canlı giriş için Stop koruması zorunludur.", http_status=423)
    try:
        entry = Decimal(str(spec["entry_price"]))
        stop = Decimal(str(spec["stop_loss"]))
        targets = [Decimal(str(value)) for value in spec["targets"]]
        quantity = Decimal(str(spec["quantity"]))
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        raise LiveExchangeError("Canlı koruma planı doğrulanamadı; emir gönderilmedi.", http_status=422) from exc
    values = [entry, stop, quantity, *targets]
    if not all(value.is_finite() and value > 0 for value in values):
        raise LiveExchangeError("Canlı koruma seviyeleri geçersiz; emir gönderilmedi.", http_status=422)
    if len(targets) != 3 or any(target == entry for target in targets):
        raise LiveExchangeError("Canlı TP koruma planı eksik veya geçersiz; emir gönderilmedi.", http_status=422)
    direction = str(spec.get("direction") or "").upper()
    if direction == "LONG" and not (stop < entry < targets[0] <= targets[1] <= targets[2]):
        raise LiveExchangeError("Canlı LONG koruma seviyeleri geçersiz; emir gönderilmedi.", http_status=422)
    if direction == "SHORT" and not (targets[2] <= targets[1] <= targets[0] < entry < stop):
        raise LiveExchangeError("Canlı SHORT koruma seviyeleri geçersiz; emir gönderilmedi.", http_status=422)


async def live_symbol_rules(client: BinanceLiveClient, symbol: str, order_type: str) -> dict[str, Decimal]:
    payload = await client.public_get("/fapi/v1/exchangeInfo")
    row = next((item for item in payload.get("symbols", []) if item.get("symbol") == symbol), None)
    if not row or row.get("status") != "TRADING":
        raise LiveExchangeError(f"{symbol} canlı Futures işlemlerine açık değil.", http_status=422)
    filters = {item.get("filterType"): item for item in row.get("filters", [])}
    lot = filters.get("MARKET_LOT_SIZE" if order_type == "MARKET" else "LOT_SIZE") or filters.get("LOT_SIZE", {})
    price_filter = filters.get("PRICE_FILTER", {})
    notional_filter = filters.get("MIN_NOTIONAL", {}) or filters.get("NOTIONAL", {})
    if row.get("contractType") != "PERPETUAL" or row.get("quoteAsset") != "USDT":
        raise LiveExchangeError(f"{symbol} yalnızca USDT perpetual canlı Futures sözleşmesi olmalı.", http_status=422)
    return {
        "step": Decimal(str(lot.get("stepSize", "0.001"))),
        "min_qty": Decimal(str(lot.get("minQty", "0"))),
        "max_qty": Decimal(str(lot.get("maxQty", "999999999"))),
        "tick": Decimal(str(price_filter.get("tickSize", "0.01"))),
        "min_notional": Decimal(str(notional_filter.get("notional", "0"))),
    }


async def set_live_isolated_margin(client: BinanceLiveClient, symbol: str) -> str:
    try:
        await client.signed("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "ISOLATED"})
        return "ISOLATED"
    except LiveExchangeError as exc:
        if exc.exchange_code == -4046:
            return "ISOLATED"
        if exc.exchange_code == -4168:
            return "CROSSED"
        if exc.exchange_code is not None:
            raise
        raise


async def apply_live_verified_leverage(
    client: BinanceLiveClient,
    symbol: str,
    requested: int,
    expected_margin_type: str = "ISOLATED",
) -> dict[str, Any]:
    response = await client.signed("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": requested})
    verify_leverage_response(response, symbol, requested)
    configuration = await client.signed("GET", "/fapi/v1/symbolConfig", {"symbol": symbol})
    return verify_symbol_configuration(configuration, symbol, requested, expected_margin_type=expected_margin_type)


async def ticker_price(client: BinanceLiveClient, symbol: str) -> Decimal:
    payload = await client.public_get("/fapi/v1/ticker/price", {"symbol": symbol})
    price = Decimal(str(payload.get("price", "0")))
    if price <= 0:
        raise LiveExchangeError("Canlı piyasa fiyatı alınamadı.")
    return price


async def spread_bps(client: BinanceLiveClient, symbol: str) -> float:
    payload = await client.public_get("/fapi/v1/ticker/bookTicker", {"symbol": symbol})
    bid, ask = Decimal(str(payload.get("bidPrice", "0"))), Decimal(str(payload.get("askPrice", "0")))
    midpoint = (bid + ask) / 2
    if min(bid, ask, midpoint) <= 0 or ask < bid:
        raise LiveExchangeError("Canlı alış-satış makası okunamadı.")
    return float((ask - bid) / midpoint * Decimal("10000"))


def rank_market_tickers(
    exchange_info: Any,
    tickers: Any,
    *,
    market_limit: int = MARKET_SCAN_LIMIT,
    candidate_limit: int = DEEP_ANALYSIS_LIMIT,
    excluded_symbols: set[str] | None = None,
    allowed_symbols: set[str] | None = None,
) -> list[dict[str, Any]]:
    eligible = {
        item.get("symbol")
        for item in exchange_info.get("symbols", [])
        if isinstance(item, dict)
        and item.get("status") == "TRADING"
        and item.get("contractType") == "PERPETUAL"
        and item.get("quoteAsset") == "USDT"
    } if isinstance(exchange_info, dict) else set()
    excluded = excluded_symbols or set()
    liquid: list[dict[str, Any]] = []
    for ticker in tickers if isinstance(tickers, list) else []:
        if not isinstance(ticker, dict):
            continue
        symbol = str(ticker.get("symbol") or "")
        base = symbol[:-4] if symbol.endswith("USDT") else ""
        if symbol not in eligible or symbol in excluded or (allowed_symbols is not None and symbol not in allowed_symbols) or base in BLOCKED_BASE_ASSETS:
            continue
        if any(word in base for word in ("UP", "DOWN", "BULL", "BEAR")):
            continue
        try:
            volume = float(ticker.get("quoteVolume") or 0)
            move = abs(float(ticker.get("priceChangePercent") or 0))
            price = float(ticker.get("lastPrice") or 0)
        except (TypeError, ValueError):
            continue
        if volume < MIN_24H_QUOTE_VOLUME or move < MIN_24H_MOVE_PCT or price <= 0:
            continue
        liquid.append({
            "symbol": symbol,
            "price": price,
            "volume": volume,
            "change": float(ticker.get("priceChangePercent") or 0),
            "_move": move,
        })
    liquid.sort(key=lambda item: item["volume"], reverse=True)
    ranked = []
    for item in liquid[:market_limit]:
        item["opportunity_score"] = round(item.pop("_move") * 0.35 + min(item["volume"] / 10_000_000, 100) * 0.65, 4)
        ranked.append(item)
    ranked.sort(key=lambda item: (item["opportunity_score"], item["volume"]), reverse=True)
    return ranked[:min(market_limit, candidate_limit)]


async def scan_market_candidates(
    client: BinanceLiveClient,
    snapshot: dict[str, Any],
    allowed_symbols: list[str] | None = None,
) -> list[dict[str, Any]]:
    exchange_info, tickers = await asyncio.gather(
        client.public_get("/fapi/v1/exchangeInfo"),
        client.public_get("/fapi/v1/ticker/24hr"),
    )
    occupied = {
        str(item.get("symbol"))
        for item in (snapshot.get("positions", []) + snapshot.get("open_orders", []))
        if isinstance(item, dict) and item.get("symbol")
    }
    eligible_count = sum(
        1
        for item in exchange_info.get("symbols", [])
        if isinstance(item, dict)
        and item.get("status") == "TRADING"
        and item.get("contractType") == "PERPETUAL"
        and item.get("quoteAsset") == "USDT"
    ) if isinstance(exchange_info, dict) else 0
    candidates = rank_market_tickers(exchange_info, tickers, excluded_symbols=occupied)
    client.last_scan_eligible_count = eligible_count
    return candidates


async def build_live_spec(
    client: BinanceLiveClient,
    order: LiveOrderRequest,
    policy: dict[str, Any],
    *,
    allowed_symbols: list[str] | None = None,
) -> dict[str, Any]:
    settings = sanitize_execution_policy(policy)
    symbol = normalize_symbol(order.symbol)
    symbol_scope = allowed_symbols if allowed_symbols is not None else settings["allowed_symbols"]
    if symbol_scope and symbol not in symbol_scope:
        raise LiveExchangeError(f"{symbol} canlı izin listesinde değil.", http_status=422)
    if order.direction == "LONG" and not settings["allow_long"]:
        raise LiveExchangeError("Canlı LONG işlemleri risk politikasında kapalı.", http_status=422)
    if order.direction == "SHORT" and not settings["allow_short"]:
        raise LiveExchangeError("Canlı SHORT işlemleri risk politikasında kapalı.", http_status=422)
    if order.margin_usdt > settings["max_margin_per_trade"] or order.leverage > settings["max_leverage"]:
        raise LiveExchangeError("Emir, kullanıcı risk limitindeki marjin veya kaldıraç tavanını aşıyor.", http_status=422)
    if order.order_type == "LIMIT" and order.limit_price is None:
        raise LiveExchangeError("Limit emir için fiyat zorunludur.", http_status=422)
    current, rules = await asyncio.gather(ticker_price(client, symbol), live_symbol_rules(client, symbol, order.order_type))
    raw_entry = Decimal(str(order.limit_price)) if order.order_type == "LIMIT" else current
    entry = round_tick(raw_entry, rules["tick"])
    stop = round_tick(Decimal(str(order.stop_loss)), rules["tick"])
    targets = [round_tick(Decimal(str(value)), rules["tick"]) for value in (order.tp1, order.tp2, order.tp3)]
    validate_levels(order.direction, entry if order.order_type == "LIMIT" else current, stop, targets)
    try:
        risk = risk_sized_order(
            float(entry),
            float(stop),
            {**settings, "max_margin_per_trade": order.margin_usdt, "max_leverage": order.leverage},
            atr=order.atr,
        )
    except ValueError as exc:
        raise LiveExchangeError(f"Canlı Stop riski geçersiz: {exc}", http_status=422) from exc
    if risk["estimated_stop_loss_usdt"] > settings["max_loss_per_trade"] + 1e-6:
        raise LiveExchangeError("Tahmini Stop kaybı işlem başına risk limitini aşıyor.", http_status=422)
    notional = Decimal(str(risk["notional_usdt"]))
    quantity = floor_step(notional / entry, rules["step"])
    if quantity < rules["min_qty"] or quantity * entry < rules["min_notional"]:
        raise LiveExchangeError("Hesaplanan miktar Binance minimum emir tutarını karşılamıyor.", http_status=422)
    if quantity > rules["max_qty"]:
        raise LiveExchangeError("Hesaplanan miktar Binance sembol üst sınırını aşıyor.", http_status=422)
    tp1_move = abs(float(targets[0] - entry) / float(entry))
    gross = float(quantity * entry) * tp1_move
    costs = float(quantity * entry) * (settings["fee_bps_per_side"] + settings["slippage_bps_per_side"]) * 2 / 10_000
    if gross - costs < settings["minimum_net_reward_usdt"]:
        raise LiveExchangeError("TP1, ücret ve kayma sonrası minimum net getiri eşiğini karşılamıyor.", http_status=422)
    return {
        "symbol": symbol,
        "direction": order.direction,
        "side": "BUY" if order.direction == "LONG" else "SELL",
        "close_side": "SELL" if order.direction == "LONG" else "BUY",
        "order_type": order.order_type,
        "margin_usdt": float(risk["margin_usdt"]),
        "leverage": order.leverage,
        "notional_usdt": float(quantity * entry),
        "quantity": decimal_text(quantity),
        "entry_price": decimal_text(entry),
        "stop_loss": decimal_text(stop),
        "targets": [decimal_text(value) for value in targets],
        "step": rules["step"],
        "min_qty": rules["min_qty"],
        "estimated_stop_loss_usdt": risk["estimated_stop_loss_usdt"],
    }


def client_id_for(kind: str, intent_id: str) -> str:
    digest = hashlib.sha256(f"{kind}:{intent_id}".encode("utf-8")).hexdigest()[:22]
    return f"{LIVE_CLIENT_PREFIX}{kind[:5].upper()}_{digest}"[:36]


async def find_order(client: BinanceLiveClient, symbol: str, client_id: str) -> dict[str, Any] | None:
    try:
        payload = await client.signed("GET", "/fapi/v1/order", {"symbol": symbol, "origClientOrderId": client_id})
        rows = response_rows(payload)
        matches = [row for row in rows if isinstance(row, dict) and row.get("orderId")]
        if len(matches) > 1:
            raise LiveExchangeError("Birden fazla eşleşen canlı emir bulundu; sonuç belirsiz.", unknown_execution=True)
        return matches[0] if matches else None
    except LiveExchangeError as exc:
        if exc.exchange_code in {-2011, -2013}:
            return None
        raise


def order_matches_spec(order: dict[str, Any], spec: dict[str, Any], client_id: str) -> bool:
    response_client_id = str(order.get("clientOrderId") or order.get("origClientOrderId") or "")
    if response_client_id != client_id:
        return False
    if str(order.get("symbol") or "").upper() != str(spec["symbol"]).upper():
        return False
    if str(order.get("side") or "").upper() != str(spec["side"]).upper():
        return False
    if str(order.get("positionSide") or "BOTH").upper() != "BOTH":
        return False
    if str(order.get("type") or "").upper() != str(spec["order_type"]).upper():
        return False
    try:
        return Decimal(str(order.get("origQty"))) == Decimal(str(spec["quantity"]))
    except (TypeError, ValueError, ArithmeticError):
        return False


async def submit_entry(client: BinanceLiveClient, spec: dict[str, Any], client_id: str, *, test_only: bool) -> dict[str, Any]:
    params: dict[str, Any] = {
        "symbol": spec["symbol"], "side": spec["side"], "type": spec["order_type"],
        "quantity": spec["quantity"], "newClientOrderId": client_id, "newOrderRespType": "RESULT",
    }
    if spec["order_type"] == "LIMIT":
        params.update({"price": spec["entry_price"], "timeInForce": "GTC"})
    path = "/fapi/v1/order/test" if test_only else "/fapi/v1/order"
    if not test_only:
        existing = await find_order(client, spec["symbol"], client_id)
        if existing is not None:
            if not order_matches_spec(existing, spec, client_id):
                raise LiveExchangeError("İlişkisiz veya uyuşmayan canlı emir bulundu; sahiplenilmedi.", unknown_execution=True)
            return {**existing, "recovered": True}
    try:
        payload = await client.signed("POST", path, params)
        if not test_only and (not isinstance(payload, dict) or not payload.get("orderId")):
            raise LiveExchangeError("Canlı emir yanıtı eksik; emir sonucu belirsiz ve uzlaştırma gerekiyor.", unknown_execution=True)
    except LiveExchangeError as exc:
        if not test_only and exc.unknown_execution:
            recovered = await find_order(client, spec["symbol"], client_id)
            if recovered is not None:
                if not order_matches_spec(recovered, spec, client_id):
                    raise LiveExchangeError("Uzlaştırılan canlı emir beklenen kimlik veya parametrelerle eşleşmedi.", unknown_execution=True) from exc
                return {**recovered, "recovered": True}
        raise
    if test_only:
        return payload if isinstance(payload, dict) else {}
    return payload


async def post_algo(client: BinanceLiveClient, params: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = await client.signed("POST", "/fapi/v1/algoOrder", params)
        return payload if isinstance(payload, dict) else {}
    except LiveExchangeError as exc:
        if not exc.unknown_execution:
            raise
        rows = response_rows(await client.signed("GET", "/fapi/v1/openAlgoOrders", {"symbol": params["symbol"]}))
        recovered = next((row for row in rows if row.get("clientAlgoId") == params.get("clientAlgoId")), None)
        if recovered is None:
            raise
        return recovered


async def close_tracked_symbol(client: BinanceLiveClient, symbol: str, intent: str) -> dict[str, Any] | None:
    rows = response_rows(await client.signed("GET", "/fapi/v3/positionRisk", {"symbol": symbol}))
    row = next((item for item in rows if Decimal(str(item.get("positionAmt", "0"))) != 0), None)
    if row is None:
        return None
    amount = Decimal(str(row["positionAmt"]))
    client_id = client_id_for("CLOSE", intent)
    existing = await find_order(client, symbol, client_id)
    if existing is not None:
        return existing
    params = {
        "symbol": symbol, "side": "SELL" if amount > 0 else "BUY", "type": "MARKET",
        "quantity": decimal_text(abs(amount)), "reduceOnly": "true",
        "newClientOrderId": client_id, "newOrderRespType": "RESULT",
    }
    try:
        return await client.signed("POST", "/fapi/v1/order", params)
    except LiveExchangeError as exc:
        if exc.unknown_execution:
            recovered = await find_order(client, symbol, client_id)
            if recovered is not None:
                return recovered
        raise


async def cancel_owned_algos_for_symbol(client: BinanceLiveClient, rows: list[dict[str, Any]], plan: dict[str, Any]) -> int:
    """Remove only V25-owned residual Stop/TP algos after a tracked position closes."""
    if not live_plan_can_mutate(plan):
        return 0
    cancelled = 0
    pending_ids: list[str] = []
    last_error: str | None = None
    retry_required = False
    plan["protection_cleanup_attempted_at"] = now_iso()
    for row in owned_protection_rows(plan, rows):
        algo_id = str(row["algo_id"])
        try:
            await client.signed(
                "DELETE", "/fapi/v1/algoOrder",
                {"symbol": plan["symbol"], "algoId": row["algo_id"]},
            )
            try:
                verification = await client.signed(
                    "GET", "/fapi/v1/openAlgoOrders", {"symbol": plan["symbol"]},
                )
                verification_quality = classify_algo_snapshot_payload(verification)
                verification_rows = response_rows(verification)
                still_present = any(
                    str(item.get("algoId") or item.get("algo_id") or "") == algo_id
                    for item in verification_rows
                )
                if verification_quality == "UNKNOWN":
                    pending_ids.append(algo_id)
                    last_error = "LIVE openAlgoOrders verification was unknown."
                elif still_present:
                    pending_ids.append(algo_id)
                    retry_required = True
            except LiveExchangeError as exc:
                pending_ids.append(algo_id)
                last_error = str(exc)[:240]
            cancelled += 1
        except LiveExchangeError as exc:
            # A simultaneously triggered/cancelled protection is already harmless;
            # the next reconciliation pass will verify the authoritative state.
            logger.warning(
                "cancel_owned_algos_for_symbol: DELETE failed for algoId=%s symbol=%s: %s",
                row["algo_id"], plan["symbol"], exc,
            )
            if exc.exchange_code not in {-2011, -2013}:
                pending_ids.append(algo_id)
                last_error = str(exc)[:240]
    plan["protection_cleanup_pending_ids"] = pending_ids
    plan["protection_cleanup_last_error"] = last_error
    plan["protection_cleanup_state"] = "UNKNOWN" if last_error else "RETRY_REQUIRED" if retry_required else "CLEAN"
    return cancelled


async def install_protection(client: BinanceLiveClient, state: dict[str, Any], plan: dict[str, Any]) -> None:
    symbol = plan["symbol"]
    plan.setdefault("protection_state", "UNKNOWN")
    if plan.get("provenance_state") not in {"PROVISIONAL", "CONFIRMED"}:
        plan["protection_state"] = "UNKNOWN"
        return
    positions = response_rows(await client.signed("GET", "/fapi/v3/positionRisk", {"symbol": symbol}))
    live_positions = [item for item in positions if Decimal(str(item.get("positionAmt", "0"))) != 0]
    position = live_positions[0] if len(live_positions) == 1 else None
    if position is None:
        plan["protection_state"] = "MISSING"
        plan["status"] = "DOLUM BEKLİYOR"
        return
    amount = Decimal(str(position["positionAmt"]))
    direction = "LONG" if amount > 0 else "SHORT"
    position_view = {"symbol": symbol, "direction": direction, "quantity": decimal_text(abs(amount))}
    if not live_plan_can_mutate(plan) and not confirm_live_plan_provenance(plan, position_view):
        plan["provenance_state"] = "BROKEN"
        plan["protection_state"] = "UNKNOWN"
        state["reconciliation_required"] = True
        lock_live_execution(state, "LIVE_PROVENANCE_UNCONFIRMED", unknown=True, symbol=symbol)
        add_event(
            state,
            "OWNERSHIP_UNCERTAIN",
            f"{symbol} canlı pozisyon sahipliği doğrulanamadı; Stop kurulmadı.",
            symbol=symbol,
            plan_id=plan.get("id"),
            failures=provenance_failure_details(plan, position_view),
            reconciliation_required=True,
        )
        return
    if direction != plan["direction"]:
        plan["protection_state"] = "UNKNOWN"
        plan["status"] = "YÖN UYUŞMAZLIĞI"
        state["armed_until"] = 0.0
        state["auto"]["enabled"] = False
        state["auto"]["session_until"] = 0.0
        add_event(state, "PROTECTION_BLOCK", f"{symbol} yön uyuşmazlığı; koruma kurulmadı.", symbol=symbol)
        return
    common = {"algoType": "CONDITIONAL", "symbol": symbol, "side": "SELL" if amount > 0 else "BUY", "workingType": "MARK_PRICE", "priceProtect": "TRUE"}
    stop_client = plan.setdefault("stop_client_id", client_id_for("SL", plan["intent_id"]))
    try:
        stop_result = await post_algo(client, {**common, "type": "STOP_MARKET", "triggerPrice": plan["stop_loss"], "closePosition": "true", "clientAlgoId": stop_client})
        plan["stop_algo_id"] = int(stop_result.get("algoId") or 0) or None
    except LiveExchangeError as exc:
        plan["status"] = "STOP BAŞARISIZ · KAPATILIYOR"
        add_event(state, "PROTECTION_FAIL", f"{symbol} Stop kurulamadı; tracked pozisyon reduce-only kapatılıyor.", symbol=symbol)
        close_intent = f"protection-{plan['id']}"
        close_client_id = client_id_for("CLOSE", close_intent)
        close_ids = plan.setdefault("close_client_order_ids", [])
        if close_client_id not in close_ids:
            close_ids.append(close_client_id)
        persist_state(state)
        close_result = await close_tracked_symbol(client, symbol, close_intent)
        if close_result and close_result.get("orderId"):
            order_id = int(close_result["orderId"])
            known = plan.setdefault("exchange_order_ids", [])
            if order_id not in known:
                known.append(order_id)
        plan["close_reason"] = close_reason_for_intent(close_intent)
        plan["status"] = "GÜVENLİK İÇİN KAPATILDI"
        plan["last_error"] = str(exc)[:240]
        return
    step, min_qty = Decimal(str(plan["step"])), Decimal(str(plan["min_qty"]))
    ids: list[int] = [plan["stop_algo_id"]] if plan.get("stop_algo_id") else []
    monitoring: list[str] = []
    combined_partial = floor_step(abs(amount) * Decimal("0.60"), step)
    if combined_partial >= min_qty:
        key = client_id_for("TP12", plan["intent_id"])
        try:
            result = await post_algo(client, {**common, "type": "TAKE_PROFIT_MARKET", "triggerPrice": plan["targets"][0], "quantity": decimal_text(combined_partial), "reduceOnly": "true", "clientAlgoId": key})
            if result.get("algoId"):
                ids.append(int(result["algoId"]))
                plan["tp12_algo_id"] = int(result["algoId"])
                plan["tp12_client_id"] = key
            else:
                monitoring.extend(["TP1", "TP2"])
        except LiveExchangeError:
            monitoring.extend(["TP1", "TP2"])
    else:
        monitoring.extend(["TP1", "TP2"])
    try:
        result = await post_algo(client, {**common, "type": "TAKE_PROFIT_MARKET", "triggerPrice": plan["targets"][2], "closePosition": "true", "clientAlgoId": client_id_for("TP3", plan["intent_id"])})
        if result.get("algoId"):
            ids.append(int(result["algoId"]))
    except LiveExchangeError:
        monitoring.append("TP3")
    plan.update({
        "protection_ids": ids,
        "monitoring_targets": monitoring,
        "monitoring_targets_persisted_at": now_iso() if monitoring else plan.get("monitoring_targets_persisted_at"),
        "monitoring_targets_exchange_backed": not monitoring,
        "protected_at": now_iso(),
        "protection_state": "MATCHED" if plan.get("stop_algo_id") and not monitoring else "MISSING",
        "status": "KORUMA AKTİF" if not monitoring else "STOP AKTİF · HEDEF İZLEME",
    })
    add_event(state, "PROTECTION_ACTIVE", f"{symbol} canlı Stop ve TP koruma planı kuruldu.", symbol=symbol)
    if monitoring:
        add_event(state, "TP_MONITORING_PERSISTED", f"{symbol} {','.join(monitoring)} Binance minimum miktarına ulaşamadı; kalıcı reconciliation takibine alındı.", symbol=symbol, plan_id=plan.get("id"), monitoring_targets=list(monitoring))
    persist_state(state)


def _truthy(value: Any) -> bool:
    return value is True or str(value).strip().lower() == "true"


def _stream_event_seen(state: dict[str, Any], exchange_event_id: str) -> bool:
    return any(item.get("exchange_event_id") == exchange_event_id for item in state.get("events", []))


def _active_plan_for_symbol(state: dict[str, Any], symbol: str) -> dict[str, Any] | None:
    terminal = {"KAPANDI", "İPTAL"}
    return next(
        (
            plan for plan in state.get("plans", {}).values()
            if plan.get("symbol") == symbol and plan.get("status") not in terminal
        ),
        None,
    )


def process_live_stream_event(state: dict[str, Any], payload: dict[str, Any]) -> bool:
    """Record only V25-owned order events; account-risk events always fail closed."""
    stream = state["stream"]
    stream.update({"status": "CANLI", "transport": "BINANCE USER STREAM", "last_event": now_iso(), "last_error": None})
    stream["event_count"] = int(stream.get("event_count") or 0) + 1
    event_type = str(payload.get("e") or "")
    event_time = payload.get("E", payload.get("T", 0))

    if event_type == "MARGIN_CALL":
        exchange_event_id = f"margin-{event_time}"
        if _stream_event_seen(state, exchange_event_id):
            return False
        state["armed_until"] = 0.0
        state["auto"]["enabled"] = False
        state["auto"]["session_until"] = 0.0
        state["auto"]["last_decision"] = "Binance marjin çağrısı bildirdi; yeni girişler kilitlendi."
        add_event(
            state,
            "LIVE_MARGIN_CALL",
            "Binance marjin çağrısı bildirdi; V25 yeni girişleri kilitledi. Hesabı derhal kontrol edin.",
            exchange_event_id=exchange_event_id,
        )
        return True

    if event_type == "ORDER_TRADE_UPDATE":
        order = payload.get("o") if isinstance(payload.get("o"), dict) else {}
        symbol = str(order.get("s") or "").upper()
        client_id = str(order.get("c") or "")
        plan = _active_plan_for_symbol(state, symbol)
        belongs_to_v25 = client_id.startswith(LIVE_CLIENT_PREFIX) or bool(plan and _truthy(order.get("R")))
        if not belongs_to_v25:
            return False
        execution = str(order.get("x") or "UPDATE")
        status = str(order.get("X") or "UPDATE")
        order_id = int(order.get("i") or 0)
        exchange_event_id = f"order-{event_time}-{order_id}-{execution}-{status}-{order.get('t', '')}"
        if _stream_event_seen(state, exchange_event_id):
            return False
        if plan and order_id:
            known = plan.setdefault("exchange_order_ids", [])
            if order_id not in known:
                known.append(order_id)
        if plan and execution == "TRADE" and _truthy(order.get("R")):
            reason = close_reason_for_client_id(plan, client_id)
            if reason != "UNKNOWN":
                plan["close_reason"] = reason
        realized = float(order.get("rp") or 0)
        add_event(
            state,
            "LIVE_FILL" if execution == "TRADE" else "LIVE_ORDER_UPDATE",
            f"{symbol} {execution} · {status}",
            symbol=symbol,
            status=status,
            side=order.get("S"),
            price=float(order.get("ap") or order.get("L") or order.get("p") or 0),
            quantity=float(order.get("z") or order.get("l") or order.get("q") or 0),
            realized_pnl_fill=realized,
            reduce_only=_truthy(order.get("R")),
            client_order_id=client_id,
            exchange_order_id=order_id or None,
            exchange_event_id=exchange_event_id,
        )
        return True

    if event_type == "ALGO_UPDATE":
        order = payload.get("o") if isinstance(payload.get("o"), dict) else payload.get("a", {})
        order = order if isinstance(order, dict) else {}
        symbol = str(order.get("s") or order.get("symbol") or "").upper()
        client_id = str(order.get("caid") or order.get("clientAlgoId") or order.get("c") or "")
        if not client_id.startswith(LIVE_CLIENT_PREFIX) and _active_plan_for_symbol(state, symbol) is None:
            return False
        status = str(order.get("X") or order.get("algoStatus") or order.get("status") or "UPDATE")
        algo_id = order.get("aid", order.get("algoId", ""))
        exchange_event_id = f"algo-{event_time}-{algo_id}-{status}"
        if _stream_event_seen(state, exchange_event_id):
            return False
        add_event(
            state,
            "LIVE_ALGO_UPDATE",
            f"{symbol} Stop/TP koruması · {status}",
            symbol=symbol or None,
            status=status,
            exchange_event_id=exchange_event_id,
        )
        return True

    if event_type == "listenKeyExpired":
        exchange_event_id = f"expired-{event_time}"
        if not _stream_event_seen(state, exchange_event_id):
            add_event(
                state,
                "LIVE_STREAM_EXPIRED",
                "Binance canlı kullanıcı akışı süresi doldu; yeni listenKey ile yeniden bağlanılıyor.",
                exchange_event_id=exchange_event_id,
            )
        return True
    return False


async def live_user_stream_loop(application: Any) -> None:
    """Prefer Binance's ordered private stream and retain REST reconciliation as backup."""
    state = application.state.v25_execution
    while True:
        listen_key = ""
        client: BinanceLiveClient | None = None
        try:
            credentials = await live_stream_credentials(application, state)
            if not usable_live_credentials(credentials):
                state["stream"].update({"status": "ANAHTAR BEKLİYOR", "transport": "REST UZLAŞTIRMA"})
                await asyncio.sleep(5)
                continue
            client = client_for_with_credentials(application, credentials)
            response = await client.api_key_request("POST", "/fapi/v1/listenKey")
            listen_key = str((response or {}).get("listenKey") or "")
            if not listen_key:
                raise LiveExchangeError("Binance canlı kullanıcı akışı anahtarı alınamadı.")
            state["stream"].update({"status": "BAĞLANIYOR", "transport": "BINANCE USER STREAM", "last_error": None})
            url = f"{LIVE_WS_BASE}/ws/{listen_key}"
            async with websockets.connect(url, ping_interval=20, ping_timeout=20, close_timeout=5, max_queue=512) as socket:
                state["stream"].update({"status": "CANLI", "transport": "BINANCE USER STREAM", "last_event": now_iso()})
                add_event(state, "LIVE_STREAM_CONNECTED", "Binance canlı emir ve pozisyon kullanıcı akışı bağlandı.")
                persist_state(state)
                last_keepalive = time.monotonic()
                while True:
                    try:
                        raw = await asyncio.wait_for(socket.recv(), timeout=30)
                        if isinstance(raw, bytes):
                            raw = raw.decode("utf-8")
                        payload = json.loads(raw)
                        if isinstance(payload, dict):
                            async with state["lock"]:
                                changed = process_live_stream_event(state, payload)
                                if changed:
                                    persist_state(state)
                            if payload.get("e") == "listenKeyExpired":
                                break
                    except asyncio.TimeoutError:
                        pass
                    if time.monotonic() - last_keepalive >= 45 * 60:
                        await client.api_key_request("PUT", "/fapi/v1/listenKey")
                        last_keepalive = time.monotonic()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            state["stream"]["reconnect_count"] = int(state["stream"].get("reconnect_count") or 0) + 1
            state["stream"].update({
                "status": "YENİDEN BAĞLANIYOR",
                "transport": "REST UZLAŞTIRMA",
                "last_error": str(exc)[:220],
            })
            add_event(state, "LIVE_STREAM_RECONNECT", "Canlı kullanıcı akışı kesildi; REST uzlaştırması açık ve yeniden bağlantı deneniyor.")
            persist_state(state)
            await asyncio.sleep(min(30, 2 + int(state["stream"]["reconnect_count"])))
        finally:
            if listen_key and client is not None and not asyncio.current_task().cancelling():
                try:
                    await client.api_key_request("DELETE", "/fapi/v1/listenKey")
                except Exception:
                    pass


async def live_stream_credentials(application: Any, state: dict[str, Any]) -> tuple[str, str]:
    """Resolve the active supervised vault session for the private Binance stream."""
    return await auto_session_credentials(application, state)


def _iso_epoch_ms(value: Any) -> int:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1000)
    except (TypeError, ValueError, OverflowError):
        return int(time.time() * 1000) - 24 * 60 * 60 * 1000


async def verified_plan_pnl(client: BinanceLiveClient, plan: dict[str, Any]) -> dict[str, Any]:
    """Rebuild a closed plan's trade PnL from official account trades.

    Funding payments are intentionally reported separately by Binance and are
    not represented here.  If the trade history cannot prove the close, the
    live entry gate stays locked rather than assuming zero PnL.
    """
    start_ms = max(_iso_epoch_ms(plan.get("created_at")) - 60_000, int(time.time() * 1000) - 180 * 24 * 60 * 60 * 1000)
    now_ms = int(time.time() * 1000)
    window_ms = 6 * 24 * 60 * 60 * 1000 + 23 * 60 * 60 * 1000
    rows: list[dict[str, Any]] = []
    normal_history: list[dict[str, Any]] = []
    algo_history: list[dict[str, Any]] = []
    cursor = start_ms
    while cursor <= now_ms:
        end = min(now_ms, cursor + window_ms)
        trades_payload, normal_payload, algo_payload = await asyncio.gather(
            client.signed(
                "GET", "/fapi/v1/userTrades",
                {"symbol": plan["symbol"], "startTime": cursor, "endTime": end, "limit": 1000},
            ),
            client.signed(
                "GET", "/fapi/v1/allOrders",
                {"symbol": plan["symbol"], "startTime": cursor, "endTime": end, "limit": 1000},
            ),
            client.signed(
                "GET", "/fapi/v1/allAlgoOrders",
                {"symbol": plan["symbol"], "startTime": cursor, "endTime": end, "limit": 1000},
            ),
        )
        rows.extend(item for item in response_rows(trades_payload) if isinstance(item, dict))
        normal_history.extend(item for item in response_rows(normal_payload) if isinstance(item, dict))
        algo_history.extend(item for item in response_rows(algo_payload) if isinstance(item, dict))
        cursor = end + 1

    known_ids = {int(value) for value in plan.get("exchange_order_ids", []) if str(value).isdigit()}
    if plan.get("entry_order_id"):
        known_ids.add(int(plan["entry_order_id"]))
    expected_normal_clients = {
        str(value) for value in [plan.get("entry_client_order_id"), *plan.get("close_client_order_ids", [])]
        if value
    }
    expected_algo_clients = {
        str(value) for value in [
            plan.get("stop_client_id"),
            client_id_for("TP1", str(plan.get("intent_id") or "")),
            client_id_for("TP2", str(plan.get("intent_id") or "")),
            client_id_for("TP3", str(plan.get("intent_id") or "")),
        ] if value
    }
    known_algo_ids = {int(value) for value in plan.get("protection_ids", []) if str(value).isdigit()}
    for item in normal_history:
        if str(item.get("clientOrderId") or "") in expected_normal_clients and str(item.get("orderId") or "").isdigit():
            known_ids.add(int(item["orderId"]))
    for item in algo_history:
        if (
            str(item.get("clientAlgoId") or "") in expected_algo_clients
            or (str(item.get("algoId") or "").isdigit() and int(item["algoId"]) in known_algo_ids)
        ):
            actual_order_id = str(item.get("actualOrderId") or "")
            if actual_order_id.isdigit():
                known_ids.add(int(actual_order_id))
    close_side = "SELL" if plan.get("direction") == "LONG" else "BUY"
    selected = [item for item in rows if int(item.get("orderId") or 0) in known_ids]
    close_rows = [item for item in selected if str(item.get("side") or "").upper() == close_side]
    if not close_rows:
        raise LiveExchangeError("Binance işlem geçmişi V25'e ait kapanış dolumunu henüz kimlikle doğrulamadı.")
    entry_side = "BUY" if plan.get("direction") == "LONG" else "SELL"
    entry_rows = [item for item in selected if str(item.get("side") or "").upper() == entry_side]
    opened_at = None
    entry_times = [int(item.get("time")) for item in entry_rows if str(item.get("time") or "").isdigit()]
    if entry_times:
        opened_at = datetime.fromtimestamp(min(entry_times) / 1000, timezone.utc).isoformat()
    exit_notional = Decimal("0")
    exit_quantity = Decimal("0")
    for item in close_rows:
        try:
            price = Decimal(str(item.get("price")))
            quantity = Decimal(str(item.get("qty")))
        except (TypeError, ValueError, ArithmeticError):
            continue
        if price.is_finite() and quantity.is_finite() and price > 0 and quantity > 0:
            exit_notional += price * quantity
            exit_quantity += quantity
    exit_price = round(float(exit_notional / exit_quantity), 8) if exit_quantity > 0 else None
    gross = sum(Decimal(str(item.get("realizedPnl") or "0")) for item in close_rows)
    commission_usdt = sum(
        Decimal(str(item.get("commission") or "0"))
        for item in selected
        if str(item.get("commissionAsset") or "").upper() == "USDT"
    )
    non_usdt_commission = sorted({
        str(item.get("commissionAsset") or "").upper()
        for item in selected
        if item.get("commissionAsset") and str(item.get("commissionAsset") or "").upper() != "USDT"
    })
    return {
        "gross_realized_pnl": round(float(gross), 8),
        "commission_usdt": round(float(commission_usdt), 8),
        "realized_pnl": round(float(gross - commission_usdt), 8),
        "trade_count": len(selected),
        "exit_price": exit_price,
        "opened_at": opened_at,
        "non_usdt_commission_assets": non_usdt_commission,
        "funding_included": False,
    }


def close_reason_for_intent(intent: str) -> str:
    value = str(intent or "").lower()
    if value.startswith("manual-close-"):
        return "MANUAL"
    if value.startswith("protection-"):
        return "STOP"
    if value.startswith("emergency-close-"):
        return "EMERGENCY"
    return "UNKNOWN"


def close_reason_for_client_id(plan: dict[str, Any], client_id: str) -> str:
    value = str(client_id or "")
    if value == str(plan.get("stop_client_id") or ""):
        return "STOP"
    for index in range(1, 4):
        if value == client_id_for(f"TP{index}", str(plan.get("intent_id") or "")):
            return f"TP{index}"
    return "UNKNOWN"


async def settle_closed_plan(client: BinanceLiveClient, state: dict[str, Any], plan: dict[str, Any]) -> None:
    plan_id = str(plan.get("id") or "")
    verified = next(
        (item for item in state.get("events", []) if item.get("kind") == "LIVE_POSITION_CLOSED" and item.get("plan_id") == plan_id),
        None,
    )
    if verified:
        plan.update({
            "status": "KAPANDI",
            "pnl_verified": True,
            "closed_at": verified.get("created_at"),
            "realized_pnl": verified.get("realized_pnl"),
            "exit_price": verified.get("exit_price"),
            "opened_at": verified.get("opened_at"),
            "close_reason": verified.get("close_reason") or plan.get("close_reason") or "UNKNOWN",
        })
        review = write_trade_review(state)
        if review:
            add_event(state, "TRADE_REVIEW_GENERATED", "4USDT ve MUBARAKUSDT için doğrulanmış canlı işlem inceleme raporu oluşturuldu.", **review)
        return
    try:
        result = await verified_plan_pnl(client, plan)
    except LiveExchangeError as exc:
        plan.update({"status": "PNL DOĞRULANIYOR", "pnl_verified": False, "pnl_verification_error": str(exc)[:220]})
        state["armed_until"] = 0.0
        state["auto"]["enabled"] = False
        state["auto"]["session_until"] = 0.0
        if not any(item.get("kind") == "LIVE_POSITION_CLOSED_UNVERIFIED" and item.get("plan_id") == plan_id for item in state.get("events", [])):
            add_event(
                state,
                "LIVE_POSITION_CLOSED_UNVERIFIED",
                f"{plan['symbol']} pozisyonu kapalı; kesin PnL doğrulanana kadar yeni girişler kilitlendi.",
                symbol=plan["symbol"],
                plan_id=plan_id,
            )
        return
    close_reason = plan.get("close_reason") or "UNKNOWN"
    plan.update({"status": "KAPANDI", "closed_at": now_iso(), "pnl_verified": True, "close_reason": close_reason, **result})
    add_event(
        state,
        "LIVE_POSITION_CLOSED",
        f"{plan['symbol']} tracked pozisyon kapandı; net işlem PnL {result['realized_pnl']:+.4f} USDT (funding hariç).",
        symbol=plan["symbol"],
        plan_id=plan_id,
        close_reason=close_reason,
        **result,
    )
    review = write_trade_review(state)
    if review:
        add_event(state, "TRADE_REVIEW_GENERATED", "4USDT ve MUBARAKUSDT için doğrulanmış canlı işlem inceleme raporu oluşturuldu.", **review)


def recover_plan_from_intent(intent_id: str, intent: dict[str, Any], order: dict[str, Any]) -> dict[str, Any] | None:
    """Rebuild the local plan after a crash between exchange acceptance and plan persistence."""
    spec = intent.get("spec") if isinstance(intent.get("spec"), dict) else {}
    required = {"symbol", "direction", "order_type", "entry_price", "quantity", "stop_loss", "targets", "step", "min_qty"}
    if not required.issubset(spec) or str(order.get("status") or "") not in {"FILLED", "PARTIALLY_FILLED"}:
        return None
    order_id = int(order.get("orderId") or 0)
    if order_id <= 0:
        return None
    plan_id = hashlib.sha256(f"recover:{intent_id}".encode("utf-8")).hexdigest()[:16]
    return {
        "id": plan_id,
        "intent_id": intent_id,
        "symbol": spec["symbol"],
        "direction": spec["direction"],
        "order_type": spec["order_type"],
        "entry_price": spec["entry_price"],
        "quantity": spec["quantity"],
        "margin_usdt": spec.get("margin_usdt"),
        "notional_usdt": spec.get("notional_usdt"),
        "leverage": spec.get("leverage"),
        "applied_leverage": intent.get("applied_leverage"),
        "margin_type": intent.get("margin_type", "isolated"),
        "stop_loss": spec["stop_loss"],
        "targets": list(spec["targets"]),
        "step": spec["step"],
        "min_qty": spec["min_qty"],
        "entry_order_id": order_id,
        "entry_client_order_id": intent.get("client_order_id"),
        "status": "KURTARILDI · KORUMA KONTROLÜ",
        "created_at": intent.get("created_at") or now_iso(),
        "source": intent.get("source", "V25_RECOVERY"),
        "live": True,
        "recovered_after_restart": True,
        "protection_ids": [],
        "protection_cleanup_state": "IDLE",
        "protection_cleanup_pending_ids": [],
        "protection_cleanup_last_error": None,
        "protection_cleanup_attempted_at": None,
        "exchange_order_ids": [order_id],
    }


async def recover_orphan_plans(client: BinanceLiveClient, state: dict[str, Any], positions: dict[str, dict[str, Any]]) -> int:
    recovered_count = 0
    active_symbols = {
        str(plan.get("symbol") or "") for plan in state.get("plans", {}).values()
        if plan.get("status") not in {"KAPANDI", "İPTAL"}
    }
    intents = sorted(
        state.get("intents", {}).items(),
        key=lambda item: str(item[1].get("created_at") or ""),
        reverse=True,
    )
    for intent_id, intent in intents:
        symbol = str(intent.get("symbol") or "")
        client_id = str(intent.get("client_order_id") or "")
        if not symbol or symbol not in positions or symbol in active_symbols or not client_id.startswith(LIVE_CLIENT_PREFIX):
            continue
        order = await find_order(client, symbol, client_id)
        if not order:
            continue
        recovery_spec = intent.get("spec") if isinstance(intent.get("spec"), dict) else {}
        recovery_spec = {
            **recovery_spec,
            "symbol": symbol,
            "side": "BUY" if str(recovery_spec.get("direction") or "").upper() == "LONG" else "SELL",
        }
        if not order_matches_spec(order, recovery_spec, client_id):
            add_event(state, "LIVE_FOREIGN_ORDER", "İlişkisiz canlı emir uzlaştırılmadı; sahiplenilmedi.", symbol=symbol, client_order_id_suffix=client_id[-8:])
            continue
        plan = recover_plan_from_intent(intent_id, intent, order)
        if plan is None:
            continue
        state["plans"] = {plan["id"]: plan, **state.get("plans", {})}
        active_symbols.add(symbol)
        add_event(
            state,
            "LIVE_PLAN_RECOVERED",
            f"{symbol} canlı pozisyonu kalıcı niyet kaydından kurtarıldı; Stop/TP denetimi başlatıldı.",
            symbol=symbol,
            plan_id=plan["id"],
        )
        recovered_count += 1
    return recovered_count


async def cleanup_orphan_protection_orders(
    client: BinanceLiveClient,
    state: dict[str, Any],
    positions: dict[str, dict[str, Any]],
    open_algos: list[dict[str, Any]],
) -> int:
    """Cancel V25-owned Stop/TP algos left on the exchange with no matching
    position and no tracked plan (e.g. after a plan record was lost on restart)."""
    active_plan_symbols = {
        str(plan.get("symbol") or "").upper() for plan in state.get("plans", {}).values()
        if plan.get("status") not in {"KAPANDI", "İPTAL"}
    }
    cancelled = 0
    for row in open_algos:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").upper()
        client_algo_id = str(row.get("client_algo_id") or "")
        algo_id = row.get("algo_id")
        if not symbol or not algo_id or not client_algo_id.startswith(LIVE_CLIENT_PREFIX):
            continue
        if symbol in positions or symbol in active_plan_symbols:
            continue
        try:
            await client.signed("DELETE", "/fapi/v1/algoOrder", {"symbol": symbol, "algoId": algo_id})
        except LiveExchangeError:
            continue
        cancelled += 1
        add_event(
            state,
            "ORPHAN_PROTECTION_CLEANUP",
            f"{symbol} için pozisyonu ve plan kaydı olmayan sahipsiz V25 koruma emri iptal edildi.",
            symbol=symbol,
            algo_id=int(algo_id),
        )
    return cancelled


def demo_certificate(application: Any) -> dict[str, Any]:
    state = getattr(application.state, "v21_demo", None)
    if not state:
        return {}
    return {
        **certificate_payload(state),
        "live_allowed": True,
        "live_allowance_status": "LIVE ALLOWED — DEMO CERTIFICATION WAIVED",
    }


def readiness(
    application: Any,
    state: dict[str, Any],
    request: Request | None = None,
    credentials: tuple[str, str] | None = None,
) -> dict[str, Any]:
    consent = consent_status(state, request, credentials)
    snapshot = state.get("snapshot") or {}
    gates = release_gates(
        credentials=bool(consent.get("fingerprint")), consent_active=bool(consent.get("active")),
        connected=bool(state.get("connected")), one_way=not bool(snapshot.get("hedge_mode", True)),
        policy_acknowledged=state.get("policy_ack_digest") == policy_digest(state["policy"]),
        demo_certificate=demo_certificate(application),
    )
    return {"ready": release_ready(gates), "score": round(sum(1 for item in gates if item["passed"]) / len(gates) * 100), "gates": gates, "demo_certificate": demo_certificate(application)}


def readiness_for(
    application: Any,
    state: dict[str, Any],
    request: Request | None = None,
    credentials: tuple[str, str] | None = None,
) -> dict[str, Any]:
    if "credentials" in inspect.signature(readiness).parameters:
        return readiness(application, state, request, credentials)
    return readiness(application, state, request)


def live_plan_is_active(plan: dict[str, Any]) -> bool:
    return str(plan.get("status") or "").upper() not in {"KAPANDI", "İPTAL", "CLOSED", "CANCELLED"}


def live_plan_can_mutate(plan: dict[str, Any]) -> bool:
    return plan.get("provenance_state") == "CONFIRMED"


def confirm_live_plan_provenance(plan: dict[str, Any], position: dict[str, Any]) -> bool:
    if plan.get("provenance_state") in {"BROKEN", "NO_PROVENANCE"}:
        return False
    if str(position.get("symbol") or "").upper() != str(plan.get("symbol") or "").upper():
        return False
    if str(position.get("direction") or "").upper() != str(plan.get("direction") or "").upper():
        return False
    try:
        quantity = Decimal(str(position.get("quantity")))
        expected = Decimal(str(plan.get("quantity")))
    except (TypeError, ValueError, ArithmeticError):
        return False
    if not quantity.is_finite() or quantity <= 0 or quantity != expected:
        return False
    if not plan.get("entry_order_id") or not plan.get("entry_client_order_id"):
        return False
    plan["provenance_state"] = "CONFIRMED"
    return True


def provenance_failure_details(plan: dict[str, Any], position: dict[str, Any] | None) -> list[str]:
    if position is None:
        return ["position_missing"]
    failures: list[str] = []
    if str(position.get("symbol") or "").upper() != str(plan.get("symbol") or "").upper():
        failures.append("symbol_mismatch")
    if str(position.get("direction") or "").upper() != str(plan.get("direction") or "").upper():
        failures.append("direction_mismatch")
    try:
        quantity = Decimal(str(position.get("quantity")))
        expected = Decimal(str(plan.get("quantity")))
        if not quantity.is_finite() or quantity <= 0 or quantity != expected:
            failures.append("quantity_mismatch")
    except (TypeError, ValueError, ArithmeticError):
        failures.append("quantity_unreadable")
    if not plan.get("entry_order_id") or not plan.get("entry_client_order_id"):
        failures.append("entry_identity_missing")
    return failures or ["plan_not_confirmed"]


def owned_protection_rows(plan: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    expected = {
        str(value) for value in (
            plan.get("stop_client_id"),
            client_id_for("TP1", str(plan.get("intent_id") or "")),
            client_id_for("TP2", str(plan.get("intent_id") or "")),
            client_id_for("TP12", str(plan.get("intent_id") or "")),
            client_id_for("TP3", str(plan.get("intent_id") or "")),
        ) if value
    }
    return [
        row for row in rows
        if isinstance(row, dict)
        and str(row.get("symbol") or "").upper() == str(plan.get("symbol") or "").upper()
        and str(row.get("client_algo_id") or "") in expected
    ]


def classify_plan_protection(
    plan: dict[str, Any],
    rows: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]], str]:
    """Classify the required Stop without claiming an unrelated order."""
    symbol = str(plan.get("symbol") or "").upper()
    direction = str(plan.get("direction") or "").upper()
    expected_side = "SELL" if direction == "LONG" else "BUY" if direction == "SHORT" else ""
    stop_client_id = str(plan.get("stop_client_id") or "")
    expected_algo_id = str(plan.get("stop_algo_id") or "")
    active_statuses = {"NEW", "WORKING", "PENDING_NEW", "PARTIALLY_FILLED"}
    same_shape: list[dict[str, Any]] = []
    exact: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        row_symbol = str(row.get("symbol") or "").upper()
        row_side = str(row.get("side") or "").upper()
        row_type = str(row.get("type") or "").upper()
        row_status = str(row.get("status") or "").upper()
        if row_symbol != symbol or row_side != expected_side or row_type != "STOP_MARKET":
            continue
        if row_status not in active_statuses:
            continue
        same_shape.append(row)
        if (
            (stop_client_id and str(row.get("client_algo_id") or "") == stop_client_id)
            or (expected_algo_id and str(row.get("algo_id") or "") == expected_algo_id)
        ):
            exact.append(row)
    if len(exact) == 1:
        return "MATCHED", exact, "EXACT_IDENTITY"
    if len(exact) > 1:
        return "UNKNOWN", exact, "DUPLICATE_EXACT_IDENTITY"
    if len(same_shape) == 1:
        return "UNKNOWN", same_shape, "FALLBACK_SYMBOL_DIRECTION_TYPE"
    if len(same_shape) > 1:
        return "UNKNOWN", same_shape, "AMBIGUOUS_SYMBOL_DIRECTION_TYPE"
    return "MISSING", [], "REQUIRED_STOP_NOT_FOUND"


def record_protection_ownership_uncertain(
    state: dict[str, Any],
    plan: dict[str, Any],
    reason: str,
) -> None:
    plan_id = str(plan.get("id") or "")
    if any(
        isinstance(event, dict)
        and event.get("kind") == "OWNERSHIP_UNCERTAIN"
        and event.get("plan_id") == plan_id
        and event.get("reason") == reason
        for event in state.get("events", [])
    ):
        return
    add_event(
        state,
        "OWNERSHIP_UNCERTAIN",
        f"{plan.get('symbol')} Stop sahipliği doğrulanamadı; otomatik koruma değişikliği yapılmadı.",
        symbol=plan.get("symbol"),
        plan_id=plan_id,
        reason=reason,
        protection_status="UNKNOWN",
    )


def reconcile_monitoring_targets(state: dict[str, Any], plan: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    targets = [str(value) for value in plan.get("monitoring_targets", []) if str(value)]
    if not targets:
        return
    owned_ids = {str(row.get("client_algo_id") or "") for row in owned_protection_rows(plan, rows)}
    combined_id = client_id_for("TP12", str(plan.get("intent_id") or ""))
    exchange_backed = combined_id in owned_ids
    previous = plan.get("monitoring_targets_exchange_backed")
    plan["monitoring_targets_exchange_backed"] = exchange_backed
    plan["monitoring_targets_reconciled_at"] = now_iso()
    logger.warning(
        "TP_MONITORING_RECONCILED symbol=%s targets=%s exchange_backed=%s",
        plan.get("symbol"), ",".join(targets), exchange_backed,
    )
    if previous != exchange_backed or not any(
        event.get("kind") == "TP_MONITORING_RECONCILED" and event.get("plan_id") == plan.get("id")
        for event in state.get("events", []) if isinstance(event, dict)
    ):
        add_event(
            state,
            "TP_MONITORING_RECONCILED",
            f"{plan.get('symbol')} monitoring targets reconciliation tamamlandı; exchange_backed={exchange_backed}.",
            symbol=plan.get("symbol"),
            plan_id=plan.get("id"),
            monitoring_targets=targets,
            exchange_backed=exchange_backed,
        )


async def auto_session_credentials(
    application: Any,
    state: dict[str, Any],
    *,
    force_refresh: bool = False,
) -> tuple[str, str]:
    authorizations = [state.get("auto_authorization") or {}, state.get("live_session_authorization") or {}]
    attempted: set[tuple[str, str, str]] = set()
    for index, authorization in enumerate(authorizations):
        if index == 0 and float(authorization.get("expires_at_epoch") or 0) <= time.time():
            continue
        identity = tuple(str(authorization.get(key) or "").strip() for key in ("session_id", "user_id", "fingerprint"))
        if not all(identity) or identity in attempted:
            continue
        attempted.add(identity)
        credentials = await session_credentials_for_identity(
            application,
            identity[0],
            identity[1],
            "LIVE",
            identity[2],
            force_refresh=force_refresh,
        )
        # Monitoring must continue for an existing protected position after
        # entry consent expires. Entry paths independently require active
        # consent through readiness_for/fresh_auto_submission_credentials.
        if usable_live_credentials(credentials):
            return credentials
    return "", ""


async def fresh_auto_submission_credentials(application: Any, state: dict[str, Any]) -> tuple[str, str]:
    credentials = await auto_session_credentials(application, state, force_refresh=True)
    if not usable_live_credentials(credentials):
        return "", ""
    if not consent_status(state, credentials=credentials).get("active"):
        return "", ""
    if (
        state.get("real_trading_locked") is not False
        or not auto_session_active(state)
        or live_execution_blocked(state)
        or not state.get("recovery_ready", False)
        or not readiness_for(application, state, credentials=credentials)["ready"]
    ):
        return "", ""
    return credentials


def live_auto_start_gate(application: Any, state: dict[str, Any], request: Request | None = None) -> tuple[bool, str]:
    """Require every entry authority to be valid before opening automation."""
    if state.get("real_trading_locked") is not False:
        return False, "Gerçek işlem kilidi açık."
    if live_execution_blocked(state):
        return False, "Acil durdurma veya belirsiz uzlaştırma aktif."
    if state.get("recovery_ready") is not True or state.get("recovery_error"):
        return False, "Canlı recovery hazır değil."
    if not is_armed(state):
        return False, "Süreli canlı kilit açık değil."
    snapshot = state.get("snapshot")
    if not isinstance(snapshot, dict):
        return False, "Canlı hesap snapshot'ı mevcut değil."
    active_plans = [plan for plan in state.get("plans", {}).values() if isinstance(plan, dict) and live_plan_is_active(plan)]
    if active_plans:
        return False, "Aktif LIVE plan varken otomasyon açılamaz."
    tracked_symbols = {str(plan.get("symbol") or "").upper() for plan in active_plans}
    unmanaged_positions = [
        position for position in snapshot.get("positions", [])
        if str(position.get("symbol") or "").upper() not in tracked_symbols
    ]
    if unmanaged_positions:
        symbols = ", ".join(str(position.get("symbol") or "UNKNOWN") for position in unmanaged_positions[:3])
        return False, f"V25 dışı açık pozisyon protection kapsamı dışında: {symbols}. Önce manuel pozisyonu yönetin."
    release = readiness(application, state, request)
    if not release["ready"]:
        return False, "Canlı yayın kapıları tamamlanmadı."
    if snapshot.get("hedge_mode") is not False:
        return False, "Canlı hesap One-way modunda değil."
    available_balance = snapshot.get("available_balance")
    if available_balance is None or not math.isfinite(float(available_balance)) or float(available_balance) <= 0:
        return False, "Canlı hesap bakiyesi hazır değil."
    daily = live_daily_metrics(state)
    if int(daily.get("unverified_closures", 0)) != 0:
        return False, "Doğrulanmamış kapanış recovery gerektiriyor."
    allowed_symbols = state["policy"].get("allowed_symbols") or ["BTCUSDT"]
    risk = evaluate_entry_gates(
        symbol=str(allowed_symbols[0]),
        signal={"direction": "LONG", "confidence": 100, "radar": {"trap_score": 0}},
        snapshot=snapshot,
        policy=state["policy"],
        daily=daily,
        spread_bps=0.0,
        armed=True,
        active_plans=[],
        candidate_notional_usdt=0.0,
    )
    if not risk["passed"]:
        return False, f"Risk kapısı bloklu: {risk['reason']}"
    return True, "Tüm LIVE otomasyon kapıları geçti."


def live_daily_metrics(state: dict[str, Any]) -> dict[str, Any]:
    metrics = daily_execution_metrics(state.get("events", []))
    plan_blocks = sum(
        1 for plan in state.get("plans", {}).values()
        if plan.get("status") == "PNL DOĞRULANIYOR" or plan.get("pnl_verified") is False
    )
    metrics["unverified_closures"] = max(int(metrics.get("unverified_closures") or 0), plan_blocks)
    return metrics


def public_status(application: Any, request: Request | None = None) -> dict[str, Any]:
    state = application.state.v25_execution
    consent = consent_status(state, request)
    release = readiness(application, state, request)
    raw_snapshot = state.get("snapshot") or {}
    snapshot_session_id = str(state.get("snapshot_session_id") or "")
    current_session_id = session_id(request) if request is not None else ""
    snapshot = raw_snapshot if current_session_id and current_session_id == snapshot_session_id else {}
    account_snapshot_ready = isinstance(snapshot, dict) and all(
        key in snapshot for key in ("wallet_balance", "available_balance", "positions", "open_orders")
    )
    scan_stats = state["auto"].get("last_scan_stats") or {}
    return {
        "version": V25_VERSION,
        "mode": "LIVE_GUARD",
        "host": LIVE_REST_BASE,
        "websocket_host": LIVE_WS_BASE,
        "credentials": {"configured": bool(consent.get("fingerprint")), "fingerprint": consent.get("fingerprint"), "storage": "OTURUM_KASASI" if consent.get("fingerprint") else "YOK"},
        "consent": consent,
        "connected": bool(state.get("connected")) and account_snapshot_ready,
        "connection": state.get("connection"),
        "stream": state.get("stream"),
        "armed": is_armed(state),
        "real_trading_locked": bool(state.get("real_trading_locked", True)),
        "live_auto_trade": bool(state.get("live_auto_trade", False)),
        "reconciliation_required": bool(state.get("reconciliation_required", False)),
        "execution_state": state.get("execution_state", "LOCKED"),
        "recovery_ready": bool(state.get("recovery_ready", False)),
        "recovery_error": state.get("recovery_error"),
        "armed_until": datetime.fromtimestamp(state["armed_until"], timezone.utc).isoformat() if is_armed(state) else None,
        "auto": state["auto"],
        "scanner": {
            "last_scan_at": state["auto"].get("last_scan"),
            "scanned_symbol_count": scan_stats.get("scanned_symbol_count", len(scan_stats.get("candidate_symbols", []))),
            "candidate_symbols": scan_stats.get("candidate_symbols", scan_stats.get("selected_candidates", [])),
            "deep_analysis_symbols": scan_stats.get("deep_analysis_symbols", []),
            "candidate_count": scan_stats.get("candidate_count", scan_stats.get("deep_analysis_candidates", 0)),
            "rejection_reason_counts": scan_stats.get("rejection_reason_counts", {}),
            "rejection_reason_breakdown": scan_stats.get("rejection_reason_breakdown", {}),
            "confidence_below_min_scores": scan_stats.get("confidence_below_min_scores", []),
            "confidence_below_min_distribution": scan_stats.get("confidence_below_min_distribution", {}),
            "confidence_below_min_history": state["auto"].get("confidence_rejection_history", []),
            "signal_thresholds": scan_stats.get("signal_thresholds", {}),
            "deep_analysis_count": scan_stats.get("deep_analysis_count", len(scan_stats.get("deep_analysis_symbols", []))),
            "selected_symbols": scan_stats.get("selected_symbols", scan_stats.get("selected_candidates", [])),
            "selected_symbols_count": scan_stats.get("selected_symbols_count", len(scan_stats.get("selected_symbols", scan_stats.get("selected_candidates", [])))),
            "executed_symbols": scan_stats.get("executed_symbols", []),
            "executed_symbols_count": scan_stats.get("executed_symbols_count", len(scan_stats.get("executed_symbols", []))),
            "last_skip_reason": state["auto"].get("last_skip_reason"),
            "last_cycle_stage": state["auto"].get("last_cycle_stage"),
        },
        "auto_session_until": datetime.fromtimestamp(float(state["auto"].get("session_until") or 0), timezone.utc).isoformat() if auto_session_is_active(state) else None,
        "policy": state["policy"],
        "policy_digest": policy_digest(state["policy"]),
        "policy_acknowledged": state.get("policy_ack_digest") == policy_digest(state["policy"]),
        "reconciliation_diagnostic": latest_reconciliation_diagnostic(state),
        "last_order_error": state.get("last_order_error"),
        "trade_review": state.get("trade_review"),
        "readiness": release,
        "account": {"wallet_balance": snapshot.get("wallet_balance"), "available_balance": snapshot.get("available_balance"), "unrealized_pnl": snapshot.get("unrealized_pnl"), "positions": snapshot.get("positions", []), "open_orders": snapshot.get("open_orders", []), "open_algo_orders": snapshot.get("open_algo_orders", []), "hedge_mode": snapshot.get("hedge_mode")},
        "daily": live_daily_metrics(state),
        "plans": list(state.get("plans", {}).values())[:50],
        "events": state.get("events", [])[:80],
        "emergency": state.get("emergency"),
        "withdrawals_supported": False,
        "secret_inputs_in_browser": False,
        "profit_guaranteed": False,
    }


async def live_candles(client: BinanceLiveClient, symbol: str, interval: str, limit: int = 260) -> tuple[list[dict[str, float]], int]:
    rows = await client.public_get("/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    candles: list[dict[str, float]] = []
    last_open_time = 0
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, list) or len(row) < 6:
            continue
        candles.append({"time": int(row[0] / 1000), "open": float(row[1]), "high": float(row[2]), "low": float(row[3]), "close": float(row[4]), "volume": float(row[5])})
        last_open_time = int(row[0])
    return candles, last_open_time


async def canonical_live_decision(
    application: Any,
    client: BinanceLiveClient,
    symbol: str,
    interval: str,
    primary_candles: list[dict[str, float]] | None = None,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the side-effect-free canonical decision before live account gates."""
    from .main import canonical_historical_decision

    if interval != "15m":
        return {"decision": "WAIT", "symbol": symbol, "entry_eligible": False, "reason": "CANONICAL_BASE_INTERVAL_REQUIRES_15M"}

    frames: dict[str, list[dict[str, float]]] = {interval: primary_candles} if primary_candles is not None else {}
    for timeframe in (interval, "1h", "4h"):
        if timeframe not in frames:
            frames[timeframe], _ = await asyncio.wait_for(
                live_candles(client, symbol, timeframe),
                timeout=ANALYSIS_TIMEOUT_SECONDS,
            )
    fifteen = frames.get("15m", frames.get(interval, []))
    if len(fifteen) < 2:
        return {"decision": "WAIT", "symbol": symbol, "entry_eligible": False, "reason": "INSUFFICIENT_CLOSED_CANDLES"}
    decision_time = int(fifteen[-1]["time"])
    return canonical_historical_decision(
        symbol,
        frames,
        decision_time,
        required_intervals=("15m", "1h", "4h"),
        historical_policy_override={
            "confidence_threshold": int((policy or {}).get("min_confidence", configured_min_confidence())),
            "mtf_allow_either_timeframe": bool((policy or {}).get("mtf_allow_either_timeframe", False)),
        },
    )


async def execute_live_order(
    application: Any,
    body: LiveOrderRequest,
    *,
    source: str,
    allowed_symbols: list[str] | None = None,
    request: Request | None = None,
    credentials: tuple[str, str] | None = None,
) -> dict[str, Any]:
    state = application.state.v25_execution
    if not state.get("recovery_ready", False):
        raise HTTPException(503, "Canlı durum kurtarma tamamlanmadı; yeni emir gönderilmedi.")
    if source == "V25_AUTO":
        if not auto_session_active(state):
            raise HTTPException(423, "Bir saatlik gözetimli canlı otomasyon oturumu kapalı veya süresi doldu.")
    elif source != "MANUAL" and not is_armed(state):
        raise HTTPException(423, "24 saatlik canlı emir kilidi kapalı veya süresi doldu.")
    if live_execution_blocked(state):
        raise HTTPException(423, "Canlı yürütme kilitli; acil durum veya belirsiz emir uzlaştırması tamamlanmadı.")
    if source == "V25_AUTO" and credentials is None:
        credentials = await auto_session_credentials(application, state)
        if not usable_live_credentials(credentials):
            raise HTTPException(423, "Canlı API bağlantısı aktif değil; otomasyon credential doğrulaması başarısız.")
    if not readiness_for(application, state, request, credentials)["ready"]:
        raise HTTPException(423, "Canlı yayın kapıları tamamlanmadı; emir gönderilmedi.")
    submission_started = False
    async with state["lock"]:
        try:
            client = client_for_with_credentials(application, credentials, request)
            snapshot = await account_snapshot(client)
            if source != "MANUAL" and state.get("real_trading_locked") is not False:
                raise LiveExchangeError("Gerçek işlem kilidi kapalı; açık canlı onay olmadan emir gönderilmedi.", http_status=423)
            if source == "V25_AUTO" and state.get("live_auto_trade") is not True:
                raise LiveExchangeError("Canlı otomasyon açık değil; otomatik emir gönderilmedi.", http_status=423)
            if snapshot.get("hedge_mode"):
                raise LiveExchangeError("Canlı hesap One-way / Tek Yön modunda olmalı.", http_status=409)
            symbol = normalize_symbol(body.symbol)
            daily = live_daily_metrics(state)
            manual_signal = {"direction": body.direction, "confidence": 100, "radar": {"trap_score": 0}}
            manual_symbol_scope = [] if source == "MANUAL" else allowed_symbols
            current_spread = await spread_bps(client, symbol)
            if current_spread > float(state["policy"]["max_spread_bps"]):
                await asyncio.sleep(0.25)
                current_spread = await spread_bps(client, symbol)
            guard = evaluate_entry_gates(
                symbol=symbol,
                signal=manual_signal,
                snapshot=snapshot,
                policy=state["policy"],
                daily=daily,
                spread_bps=current_spread,
                armed=True,
                allowed_symbols=manual_symbol_scope,
                active_plans=list(state.get("plans", {}).values()),
                candidate_notional_usdt=body.margin_usdt * body.leverage,
            )
            if not guard["passed"]:
                raise LiveExchangeError(f"Canlı risk kapısı: {guard['reason']}", http_status=409)
            if float(snapshot.get("available_balance") or 0) < body.margin_usdt:
                raise LiveExchangeError("Canlı hesap kullanılabilir bakiyesi seçilen marjinden düşük.", http_status=409)
            spec = await build_live_spec(client, body, state["policy"], allowed_symbols=manual_symbol_scope)
            validate_protection_readiness(spec, state["policy"])
            expected_margin_type = "CROSSED" if snapshot.get("multi_assets_mode", False) else "ISOLATED"
            if expected_margin_type == "ISOLATED":
                expected_margin_type = await set_live_isolated_margin(client, spec["symbol"])
            leverage_audit = await apply_live_verified_leverage(
                client,
                spec["symbol"],
                spec["leverage"],
                expected_margin_type=expected_margin_type,
            )
            intent_id = body.intent_id or f"manual-{uuid.uuid4().hex}"
            client_id = client_id_for("ENTRY", intent_id)
            existing_exposure = sum(
                abs(float(item.get("notional") or item.get("notional_usdt") or 0))
                for item in snapshot.get("positions", [])
                if isinstance(item, dict)
            )
            failed_gate_keys = {item["key"] for item in guard["gates"] if not item["passed"]}
            serializable_spec = {
                "symbol": spec["symbol"], "direction": spec["direction"], "order_type": spec["order_type"],
                "entry_price": spec["entry_price"], "quantity": spec["quantity"],
                "margin_usdt": spec["margin_usdt"], "notional_usdt": spec["notional_usdt"],
                "leverage": spec["leverage"], "stop_loss": spec["stop_loss"],
                "targets": list(spec["targets"]), "step": decimal_text(spec["step"]),
                "min_qty": decimal_text(spec["min_qty"]),
            }
            audit_snapshot = MappingProxyType({
                "decision_id": intent_id,
                "timestamp": now_iso(),
                "mode": "LIVE",
                "symbol": spec["symbol"],
                "side": spec["side"],
                "position_side": "BOTH",
                "quantity": spec["quantity"],
                "leverage": spec["leverage"],
                "margin_usdt": spec["margin_usdt"],
                "estimated_notional_usdt": spec["notional_usdt"],
                "stop_loss": spec["stop_loss"],
                "tp_levels": list(spec["targets"]),
                "risk_amount_usdt": spec["estimated_stop_loss_usdt"],
                "exposure_usdt": existing_exposure + spec["notional_usdt"],
                "exposure_limit_usdt": state["policy"]["max_total_exposure_usdt"],
                "active_plan_conflict": "active_plan" in failed_gate_keys,
                "protection_readiness": True,
                "lock_state": bool(state.get("real_trading_locked", True)),
                "order_parameters": {
                    "type": spec["order_type"],
                    "entry_price": spec["entry_price"],
                    "stop_loss": spec["stop_loss"],
                    "targets": list(spec["targets"]),
                },
            })
            add_event(state, "LIVE_DECISION_AUDIT", "Canlı emir öncesi değişmez karar özeti oluşturuldu.", symbol=spec["symbol"], decision_id=intent_id, audit_snapshot=dict(audit_snapshot))
            state["intents"][intent_id] = {
                "symbol": spec["symbol"], "client_order_id": client_id, "created_at": now_iso(),
                "source": source, "spec": serializable_spec,
                "applied_leverage": leverage_audit["applied_leverage"],
                "margin_type": leverage_audit["margin_type"],
            }
            persist_state(state)
            if source == "V25_AUTO":
                fresh_credentials = await fresh_auto_submission_credentials(application, state)
                if not fresh_credentials:
                    raise LiveExchangeError("Canlı otomasyon yetkilendirmesi artık geçerli değil; emir gönderilmedi.", http_status=423)
                credentials = fresh_credentials
                client = client_for_with_credentials(application, credentials, request)
            submission_started = True
            result = await submit_entry(client, spec, client_id, test_only=False)
            plan_id = uuid.uuid4().hex[:16]
            plan = {
                "id": plan_id, "intent_id": intent_id, "symbol": spec["symbol"], "direction": spec["direction"],
                "order_type": spec["order_type"], "entry_price": spec["entry_price"], "quantity": spec["quantity"],
                "margin_usdt": spec["margin_usdt"], "notional_usdt": spec["notional_usdt"], "leverage": spec["leverage"],
                "applied_leverage": leverage_audit["applied_leverage"], "margin_type": leverage_audit["margin_type"],
                "stop_loss": spec["stop_loss"], "targets": spec["targets"], "step": decimal_text(spec["step"]),
                "min_qty": decimal_text(spec["min_qty"]), "entry_order_id": int(result.get("orderId") or 0) or None,
                "entry_client_order_id": client_id, "status": "DOLUM BEKLİYOR", "created_at": now_iso(),
                "source": source, "live": True, "protection_ids": [],
                "provenance_state": "PROVISIONAL", "protection_state": "UNKNOWN",
                "protection_cleanup_state": "IDLE", "protection_cleanup_pending_ids": [],
                "protection_cleanup_last_error": None, "protection_cleanup_attempted_at": None,
                "exchange_order_ids": [int(result.get("orderId"))] if result.get("orderId") else [],
            }
            state["plans"] = {plan_id: plan, **state.get("plans", {})}
            add_event(state, "LIVE_ENTRY_RECOVERED" if result.get("recovered") else "LIVE_ENTRY", f"{spec['symbol']} {spec['direction']} {spec['order_type']} canlı emir gönderildi ({source}).", symbol=spec["symbol"], direction=spec["direction"], client_order_id=client_id)
            # Persist exchange acceptance before any later API call. If the process
            # stops now, reconciliation can recover the exact intent and protection.
            persist_state(state)
            if spec["order_type"] == "MARKET":
                await install_protection(client, state, plan)
            update_account_snapshot(state, await account_snapshot(client))
            persist_state(state)
            return {"ok": True, "order": {"order_id": result.get("orderId"), "client_order_id": client_id, "status": result.get("status", plan["status"])}, "plan": plan, "risk_guard": guard, "profit_guaranteed": False}
        except (LiveExchangeError, BinanceDemoError) as exc:
            if isinstance(exc, LiveExchangeError) and exc.unknown_execution:
                lock_live_execution(
                    state,
                    "UNKNOWN_ORDER_STATE",
                    unknown=True,
                    symbol=body.symbol,
                    client_id=body.intent_id,
                )
            if not (isinstance(exc, LiveExchangeError) and exc.unknown_execution):
                add_event(
                    state,
                    "LIVE_ORDER_BLOCKED",
                    "Canlı emir risk veya hesap kapısında durduruldu; Binance emri gönderilmedi.",
                    reason=sanitized_exception_message(exc),
                    symbol=normalize_symbol(body.symbol),
                    source=source,
                )
            state["last_order_error"] = {"message": str(exc)[:240], "symbol": normalize_symbol(body.symbol), "created_at": now_iso()}
            persist_state(state)
            raise safe_exchange_error(exc) from exc
        except Exception as exc:
            safe_message = sanitized_exception_message(exc)
            frame = traceback.extract_tb(exc.__traceback__)[-1] if exc.__traceback__ else None
            lock_live_execution(
                state,
                "LIVE_EXCEPTION_UNKNOWN" if submission_started else "LIVE_EXCEPTION",
                unknown=submission_started,
                symbol=body.symbol,
                client_id=body.intent_id,
            )
            state["last_order_error"] = {"message": safe_message, "symbol": str(body.symbol or "")[:32], "created_at": now_iso()}
            add_event(
                state,
                "LIVE_ORDER_EXCEPTION_DIAGNOSTIC",
                "Manuel canlı emir akışı beklenmeyen hata nedeniyle durduruldu; yeni emir gönderilmedi.",
                exception_type=type(exc).__name__,
                exception_message=safe_message,
                source=os.path.basename(frame.filename) if frame else os.path.basename(__file__),
                function=frame.name if frame else "execute_live_order",
                line=frame.lineno if frame else 0,
                submission_started=submission_started,
            )
            persist_state(state)
            raise HTTPException(502, "Canlı emir akışı güvenli şekilde kilitlendi; manuel inceleme gerekli.") from exc


async def automatic_cycle(application: Any, credentials: tuple[str, str] | None = None) -> None:
    state = application.state.v25_execution
    was_enabled = bool(state["auto"].get("enabled"))
    session_until = float(state["auto"].get("session_until") or 0)
    if not auto_session_active(state):
        reason = "session_expired" if was_enabled and session_until <= time.time() else "automation_inactive"
        state["auto"]["last_skip_reason"] = reason
        state["auto"]["last_cycle_stage"] = "skipped"
        automation_telemetry(f"AUTOMATION_SKIP reason={reason}", reason=reason)
        return
    if credentials is None:
        credentials = await auto_session_credentials(application, state)
    if not usable_live_credentials(credentials):
        state["auto"]["last_skip_reason"] = "no_credentials"
        state["auto"]["last_cycle_stage"] = "skipped"
        automation_telemetry("AUTOMATION_SKIP reason=no_credentials", reason="no_credentials")
        return
    consent = consent_status(state, credentials=credentials)
    if consent.get("grace_active"):
        if state["auto"].get("last_skip_reason") != "consent_reauthorization_required":
            add_event(state, "LIVE_CONSENT_REAUTH_REQUIRED", "24 saatlik canlı izin sona erdi; 15 dakika içinde yeniden onay gerekli. Açık pozisyon korunuyor.")
        state["auto"]["last_skip_reason"] = "consent_reauthorization_required"
        state["auto"]["last_cycle_stage"] = "skipped"
        state["auto"]["last_decision"] = "Yeniden onay bekleniyor; yeni Auto Trade girişleri durduruldu, açık pozisyon korunuyor."
        automation_telemetry("AUTOMATION_SKIP reason=consent_reauthorization_required", reason="consent_reauthorization_required")
        return
    if not readiness_for(application, state, credentials=credentials)["ready"]:
        state["auto"]["last_skip_reason"] = "not_ready"
        state["auto"]["last_cycle_stage"] = "skipped"
        automation_telemetry("AUTOMATION_SKIP reason=not_ready", reason="not_ready")
        state["auto"].update({
            "enabled": False,
            "session_until": 0.0,
            "last_decision": "Canlı yayın kapılarından biri kapandı; otomatik oturum kilitlendi.",
        })
        add_event(state, "LIVE_AUTO_FAIL_CLOSED", "Canlı yayın kapısı kapanınca otomatik yeni girişler durduruldu.")
        persist_state(state)
        return
    last_scan = _iso_epoch_ms(state["auto"].get("last_scan")) if state["auto"].get("last_scan") else 0
    if int(time.time() * 1000) - last_scan < int(state["policy"]["scan_seconds"]) * 1000:
        state["auto"]["last_skip_reason"] = "scan_throttled"
        state["auto"]["last_cycle_stage"] = "skipped"
        automation_telemetry("AUTOMATION_SKIP reason=scan_throttled", reason="scan_throttled")
        return
    state["auto"]["last_scan"] = now_iso()
    state["auto"]["busy"] = True
    try:
        client = client_for_with_credentials(application, credentials)
        snapshot = await account_snapshot(client)
        daily = live_daily_metrics(state)
        state["auto"]["last_skip_reason"] = None
        state["auto"]["last_cycle_stage"] = "scanning"
        candidates = await scan_market_candidates(client, snapshot)
        logger.info(
            "MULTI_SYMBOL_SCAN started eligible symbols: %s top 100 selected deep analysis candidates: %s",
            getattr(client, "last_scan_eligible_count", 0),
            len(candidates),
        )
        signals: list[dict[str, Any]] = []
        analyzed_symbols: list[str] = []
        rejected_risk_symbols: list[str] = []
        analysis_timeout_symbols: list[str] = []
        rejection_reason_counts = {
            "signal_wait_or_invalid": 0,
            "entry_ineligible": 0,
            "stop_distance": 0,
            "spread": 0,
        }
        rejection_reason_breakdown = {
            "signal_wait_or_invalid": {},
            "entry_ineligible": {},
            "stop_distance": {},
            "spread": {},
        }
        confidence_below_min_scores: list[dict[str, Any]] = []
        confidence_below_min_distribution = {
            "below_70": 0,
            "70-75": 0,
            "75-80": 0,
            "80-86": 0,
        }
        signal_thresholds = {
            "min_confidence": int(state["policy"]["min_confidence"]),
            "canonical_max_trap_score": 35,
            "entry_gate_max_trap_score": int(state["policy"]["max_trap_score"]),
            "min_breakout_quality": 50,
            "direction_score_margin": 10,
            "adx_score_min": 20,
            "rsi_long_range": [52, 72],
            "rsi_short_range": [28, 48],
            "volume_ratio_min": 1.05,
            "mtf_required_timeframes": ["1h", "4h"],
            "mtf_allow_either_timeframe": bool(state["policy"].get("mtf_allow_either_timeframe", False)),
            "mtf_minimum_closed_candles": 50,
            "short_mtf_alignment_max": 80,
            "primary_minimum_closed_candles": 220,
            "max_stop_distance_pct": float(state["policy"]["max_stop_distance_pct"]),
            "max_spread_bps": float(state["policy"]["max_spread_bps"]),
        }

        def count_rejection(category: str, reason: str) -> None:
            rejection_reason_counts[category] += 1
            reasons = rejection_reason_breakdown[category]
            reasons[reason] = int(reasons.get(reason, 0)) + 1

        state["auto"]["last_cycle_stage"] = "deep_analysis"
        for candidate in candidates:
            symbol = candidate["symbol"]
            try:
                candles, candle_id = await asyncio.wait_for(
                    live_candles(client, symbol, state["policy"]["interval"]),
                    timeout=ANALYSIS_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                analysis_timeout_symbols.append(symbol)
                logger.warning("MULTI_SYMBOL_SCAN candidate timeout during candles: %s", symbol)
                continue
            except LiveExchangeError as exc:
                if not exc.timed_out:
                    raise
                analysis_timeout_symbols.append(symbol)
                logger.warning("MULTI_SYMBOL_SCAN candidate exchange timeout during candles: %s", symbol)
                continue
            if len(candles) < 220:
                continue
            analyzed_symbols.append(symbol)
            try:
                canonical = await asyncio.wait_for(
                    canonical_live_decision(
                        application,
                        client,
                        symbol,
                        state["policy"]["interval"],
                        candles,
                        state["policy"],
                    ),
                    timeout=ANALYSIS_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                analysis_timeout_symbols.append(symbol)
                logger.warning("MULTI_SYMBOL_SCAN candidate timeout during deep analysis: %s", symbol)
                continue
            except LiveExchangeError as exc:
                if not exc.timed_out:
                    raise
                analysis_timeout_symbols.append(symbol)
                logger.warning("MULTI_SYMBOL_SCAN candidate exchange timeout during deep analysis: %s", symbol)
                continue
            signal = canonical.get("analysis") or {}
            mtf = canonical.get("mtf") if isinstance(canonical.get("mtf"), dict) else {}
            timeframe_rows = mtf.get("timeframes") if isinstance(mtf.get("timeframes"), dict) else {}
            raw_direction = str(signal.get("direction") or "BEKLE").upper()
            mtf_history = state.setdefault("mtf_decision_history", [])
            mtf_history.append({
                "timestamp": now_iso(),
                "timestamp_epoch": time.time(),
                "symbol": symbol,
                "confidence": float(signal.get("confidence") or 0),
                "entry_direction": raw_direction,
                "1h_direction": str((timeframe_rows.get("1h") or {}).get("direction") or "BEKLE"),
                "4h_direction": str((timeframe_rows.get("4h") or {}).get("direction") or "BEKLE"),
                "mtf_mismatch": bool(raw_direction in {"LONG", "SHORT"} and not mtf.get("higher_timeframe_confirmation", False)),
            })
            state["mtf_decision_history"] = prune_mtf_decision_history(mtf_history)
            intent_id = f"auto-{symbol}-{state['policy']['interval']}-{candle_id}"
            if intent_id in state["intents"]:
                state["duplicate_blocks"] += 1
                continue
            if raw_direction not in {"LONG", "SHORT"}:
                signal_reason = "DIRECTION_SCORE_MARGIN_NOT_MET" if signal else str(canonical.get("reason") or "SIGNAL_WAIT")
                count_rejection("signal_wait_or_invalid", signal_reason)
                continue
            if not canonical.get("entry_eligible"):
                detailed_reasons = []
                confidence = float(signal.get("confidence") or 0)
                trap_score = float((signal.get("radar") or {}).get("trap_score") or 0)
                breakout_quality = float((signal.get("radar") or {}).get("breakout_quality") or 0)
                if confidence < float(signal_thresholds["min_confidence"]):
                    detailed_reasons.append("CONFIDENCE_BELOW_MIN")
                if trap_score > float(signal_thresholds["canonical_max_trap_score"]):
                    detailed_reasons.append("TRAP_SCORE_ABOVE_MAX")
                if breakout_quality < float(signal_thresholds["min_breakout_quality"]):
                    detailed_reasons.append("BREAKOUT_QUALITY_BELOW_MIN")
                if mtf and not mtf.get("higher_timeframe_confirmation", True):
                    detailed_reasons.append("MTF_HIGHER_TIMEFRAME_MISMATCH")
                if mtf.get("blocked_by_short_filter"):
                    detailed_reasons.append("MTF_SHORT_ALIGNMENT_FILTER")
                if not detailed_reasons and isinstance(canonical.get("reasons"), list):
                    detailed_reasons = [str(reason) for reason in canonical["reasons"]]
                if not detailed_reasons:
                    detailed_reasons = [str(canonical.get("reason") or "ENTRY_INELIGIBLE")]
                if "CONFIDENCE_BELOW_MIN" in detailed_reasons:
                    confidence_below_min_scores.append({
                        "symbol": symbol,
                        "confidence": round(confidence, 4),
                        "threshold": float(signal_thresholds["min_confidence"]),
                    })
                    if confidence < 70:
                        confidence_bucket = "below_70"
                    elif confidence < 75:
                        confidence_bucket = "70-75"
                    elif confidence < 80:
                        confidence_bucket = "75-80"
                    else:
                        confidence_bucket = "80-86"
                    confidence_below_min_distribution[confidence_bucket] += 1
                    logger.info(
                        "CONFIDENCE_BELOW_MIN symbol=%s confidence=%.4f threshold=%.4f",
                        symbol,
                        confidence,
                        float(signal_thresholds["min_confidence"]),
                    )
                for reason in detailed_reasons:
                    count_rejection("entry_ineligible", str(reason))
                continue
            entry_price = float(signal.get("entry") or 0)
            stop_price = float(signal.get("stop_loss") or 0)
            stop_distance_pct = abs(entry_price - stop_price) / entry_price * 100 if entry_price > 0 and stop_price > 0 else float("inf")
            if stop_distance_pct > float(state["policy"]["max_stop_distance_pct"]):
                count_rejection("stop_distance", "STOP_DISTANCE_ABOVE_MAX")
                rejected_risk_symbols.append(f"{symbol}:%{stop_distance_pct:.2f}")
                continue
            signals.append({"candidate": candidate, "signal": signal, "intent_id": intent_id})
        signals.sort(key=lambda item: (float(item["candidate"].get("opportunity_score") or 0), int(item["signal"].get("confidence") or 0)), reverse=True)
        selected = signals[:3]
        selected_symbols = [item["candidate"]["symbol"] for item in selected]
        executed_symbols: list[str] = []
        logger.info(
            "MULTI_SYMBOL_SCAN deep analysis completed: %s symbols: %s",
            len(analyzed_symbols),
            ",".join(analyzed_symbols) or "NONE",
        )
        state["auto"]["last_scan_stats"] = {
            "scanned_symbol_count": len(candidates),
            "eligible_symbols": getattr(client, "last_scan_eligible_count", 0),
            "candidate_symbols": [item["symbol"] for item in candidates],
            "candidate_count": len(signals),
            "rejection_reason_counts": rejection_reason_counts,
            "rejection_reason_breakdown": rejection_reason_breakdown,
            "confidence_below_min_scores": confidence_below_min_scores,
            "confidence_below_min_distribution": confidence_below_min_distribution,
            "signal_thresholds": signal_thresholds,
            "deep_analysis_candidates": len(candidates),
            "deep_analysis_symbols": analyzed_symbols,
            "signals_found": len(signals),
            "rejected_risk_symbols": rejected_risk_symbols,
            "analysis_timeout_symbols": analysis_timeout_symbols,
            "selected_symbols": selected_symbols,
            "selected_symbols_count": len(selected_symbols),
            "selected_candidates": selected_symbols,
            "positions_open": len(snapshot.get("positions", [])),
            "position_capacity": int(state["policy"]["max_positions"]),
        }
        state["auto"].setdefault("confidence_rejection_history", []).append({
            "cycle": int(state["auto"].get("cycles") or 0) + 1,
            "scan_at": state["auto"].get("last_scan"),
            "scores": confidence_below_min_scores,
            "distribution": confidence_below_min_distribution,
        })
        state["auto"]["confidence_rejection_history"] = state["auto"]["confidence_rejection_history"][-MAX_CONFIDENCE_REJECTION_HISTORY:]
        logger.info(
            "MULTI_SYMBOL_SCAN signals found: %s selected candidates: %s positions open: %s/%s",
            len(signals),
            ",".join(selected_symbols) or "NONE",
            len(snapshot.get("positions", [])),
            int(state["policy"]["max_positions"]),
        )
        for item in selected:
            candidate = item["candidate"]
            signal = item["signal"]
            symbol = candidate["symbol"]
            intent_id = item["intent_id"]
            try:
                spread = await spread_bps(client, symbol)
            except LiveExchangeError as exc:
                state["auto"]["last_decision"] = f"{symbol}: BEKLE · piyasa verisi reddedildi: {exc}"
                continue
            guard = evaluate_entry_gates(symbol=symbol, signal=signal, snapshot=snapshot, policy=state["policy"], daily=daily, spread_bps=spread, armed=True, allowed_symbols=[symbol])
            if any(not gate["passed"] and gate["key"] == "spread" for gate in guard.get("gates", [])):
                count_rejection("spread", "SPREAD_ABOVE_MAX")
            if not guard["passed"]:
                state["auto"]["last_decision"] = f"{symbol}: BEKLE · {guard['reason']}"
                continue
            try:
                risk = risk_sized_order(float(signal["entry"]), float(signal["stop_loss"]), state["policy"], atr=signal.get("atr"))
            except ValueError as exc:
                state["auto"]["last_decision"] = f"{symbol}: BEKLE · risk hesabı reddedildi: {exc}"
                continue
            if risk["margin_usdt"] < 5:
                state["auto"]["last_decision"] = f"{symbol}: Binance minimum güvenli marjin eşiği altında; BEKLE."
                continue
            body = LiveOrderRequest(
                symbol=symbol, direction=signal["direction"], order_type="MARKET",
                margin_usdt=risk["margin_usdt"], leverage=risk["leverage"], stop_loss=signal["stop_loss"],
                tp1=signal["tp1"], tp2=signal["tp2"], tp3=signal["tp3"], intent_id=intent_id,
                atr=signal.get("atr"),
            )
            try:
                await execute_live_order(application, body, source="V25_AUTO", allowed_symbols=[symbol], credentials=credentials)
            except LiveExchangeError as exc:
                state["auto"]["last_decision"] = f"{symbol}: BEKLE · emir risk kontrolünden geçmedi: {exc}"
                continue
            executed_symbols.append(symbol)
            state["auto"]["last_decision"] = f"{symbol} {signal['direction']} canlı işlem açıldı; Stop/TP doğrulandı."
            snapshot = await account_snapshot(client)
            if len(snapshot.get("positions", [])) >= int(state["policy"]["max_positions"]):
                break
        state["auto"]["last_scan_stats"]["executed_symbols"] = executed_symbols
        state["auto"]["last_scan_stats"]["executed_symbols_count"] = len(executed_symbols)
        state["auto"]["last_cycle_stage"] = "completed"
        state["auto"]["cycles"] += 1
    except Exception as exc:
        state["auto"]["last_skip_reason"] = "reconcile_failed"
        state["auto"]["last_cycle_stage"] = "error"
        state["auto"].update({"last_error": str(exc)[:240], "last_decision": "Canlı otomasyon turu güvenli biçimde durduruldu."})
        lock_live_execution(state, "AUTO_EXCEPTION")
        add_event(state, "AUTO_ERROR", "Canlı otomasyon turu hata nedeniyle yeni emir göndermedi.")
    finally:
        state["auto"]["busy"] = False
        persist_state(state)


async def reconcile(application: Any, credentials: tuple[str, str] | None = None) -> None:
    state = application.state.v25_execution
    client = client_for_with_credentials(application, credentials)
    try:
        snapshot = await account_snapshot(client)
    except LiveRateLimitError as exc:
        state["connected"] = False
        state["connection"].update({"last_checked": now_iso(), "last_error": str(exc)[:240]})
        state["auto"].update({"last_skip_reason": "rate_limited", "last_error": str(exc)[:240]})
        persist_state(state)
        raise
    update_account_snapshot(state, snapshot)
    state["recovery_ready"] = True
    state["recovery_error"] = None
    state["connected"] = True
    state["connection"].update({"last_checked": now_iso(), "last_error": None, "clock_offset_ms": client.time_offset_ms})
    positions = {item["symbol"]: item for item in snapshot.get("positions", [])}
    open_algos = snapshot.get("open_algo_orders", [])
    recovered_count = await recover_orphan_plans(client, state, positions)
    if recovered_count and state.get("reconciliation_required"):
        state["execution_state"] = "LOCKED"
        state["reconciliation_required"] = False
        state["emergency"].update({"active": False, "reason": "UNKNOWN_ORDER_RECONCILED"})
        add_event(state, "UNKNOWN_ORDER_RECONCILED", "Belirsiz canlı emir exact kimlik ve parametrelerle uzlaştırıldı; yeniden arm gerekiyor.", recovery_state="LOCKED")
    await cleanup_orphan_protection_orders(client, state, positions, open_algos)
    for plan in state.get("plans", {}).values():
        if plan.get("status") in {"KAPANDI", "İPTAL"}:
            continue
        symbol = plan.get("symbol")
        position = positions.get(symbol)
        if not live_plan_can_mutate(plan) and position is not None and confirm_live_plan_provenance(plan, position):
            add_event(
                state,
                "LIVE_PLAN_PROVENANCE_CONFIRMED",
                f"{symbol} canlı pozisyonu giriş emri ve miktarıyla doğrulandı; koruma yönetimi etkin.",
                symbol=str(symbol),
                plan_id=plan.get("id"),
            )
        if not live_plan_can_mutate(plan):
            state["reconciliation_required"] = True
            lock_live_execution(state, "LIVE_PLAN_PROVENANCE_UNKNOWN", unknown=True, symbol=str(plan.get("symbol") or ""))
            add_event(
                state,
                "OWNERSHIP_UNCERTAIN",
                f"{symbol} canlı plan sahipliği doğrulanamadı; koruma yönetimi kilitlendi.",
                symbol=str(symbol or ""),
                plan_id=plan.get("id"),
                failures=provenance_failure_details(plan, position),
                reconciliation_required=True,
            )
            continue
        if position is None:
            if plan.get("status") == "DOLUM BEKLİYOR":
                entry = await find_order(client, symbol, str(plan.get("entry_client_order_id") or ""))
                entry_status = str((entry or {}).get("status") or "")
                if entry_status in {"CANCELED", "EXPIRED", "REJECTED", "EXPIRED_IN_MATCH"}:
                    plan.update({"status": "İPTAL", "closed_at": now_iso()})
                    add_event(state, "LIVE_ENTRY_CANCELLED", f"{symbol} giriş emri {entry_status}; pozisyon oluşmadı.", symbol=symbol, plan_id=plan.get("id"))
                elif entry_status not in {"FILLED"}:
                    continue
            cancelled = await cancel_owned_algos_for_symbol(client, open_algos, plan)
            if cancelled:
                add_event(
                    state,
                    "PROTECTION_CLEANUP",
                    f"{symbol} kapandı; {cancelled} artık V25 Stop/TP emri temizlendi.",
                    symbol=symbol,
                    plan_id=plan.get("id"),
                )
            cleanup_state = str(plan.get("protection_cleanup_state") or "UNKNOWN")
            if cleanup_state == "CLEAN":
                await settle_closed_plan(client, state, plan)
            else:
                plan["settle_deferred_reason"] = f"protection_cleanup_{cleanup_state.lower()}"
                if cleanup_state == "UNKNOWN":
                    lock_live_execution(state, reason=f"protection cleanup unknown for {plan['symbol']}", unknown=True)
            continue
        if snapshot.get("open_algo_orders_available") is not True:
            plan["protection_state"] = "UNKNOWN"
            state["reconciliation_required"] = True
            lock_live_execution(state, "PROTECTION_SNAPSHOT_UNKNOWN", unknown=True, symbol=str(symbol))
            continue
        reconcile_monitoring_targets(state, plan, open_algos)
        protection_state, _matched_stops, protection_reason = classify_plan_protection(plan, open_algos)
        plan["protection_state"] = protection_state
        plan["protection_status"] = protection_state
        plan["protection_match_confidence"] = "EXACT" if protection_reason == "EXACT_IDENTITY" else "LOW" if protection_state == "UNKNOWN" else "NONE"
        plan["protection_match_reason"] = protection_reason
        if protection_state == "UNKNOWN":
            record_protection_ownership_uncertain(state, plan, protection_reason)
            continue
        if protection_state == "MISSING":
            if not live_plan_can_mutate(plan):
                plan["protection_state"] = "UNKNOWN"
                plan["protection_status"] = "UNKNOWN"
                state["reconciliation_required"] = True
                lock_live_execution(state, "PROTECTION_PROVENANCE_UNKNOWN", unknown=True, symbol=str(symbol))
                continue
            state["protection_repairs"] += 1
            add_event(state, "PROTECTION_REPAIR", f"{symbol} Stop eksik; koruma yeniden kuruluyor.", symbol=symbol)
            await install_protection(client, state, plan)
        elif protection_state == "MATCHED" and plan.get("status") == "DOLUM BEKLİYOR":
            plan["status"] = "KORUMA AKTİF"
    clear_clean_reconciliation_state(state, snapshot)
    persist_state(state)


async def execution_loop(application: Any) -> None:
    backoff = 5
    while True:
        reconciliation_stage = "credential_resolution"
        try:
            recovery_loaded = application.state.v25_execution.get("recovery_loaded", not bool(os.getenv("DATABASE_URL", "").strip()))
            database_unavailable = bool(os.getenv("DATABASE_URL", "").strip()) and getattr(application.state, "db_pool", None) is None
            if not recovery_loaded or database_unavailable:
                application.state.v25_execution["auto"]["last_skip_reason"] = "recovery_unavailable"
                automation_telemetry("AUTOMATION_SKIP reason=recovery_unavailable", reason="recovery_unavailable")
                logger.warning(
                    "LIVE automation skipped: recovery_unavailable recovery_loaded=%s database_unavailable=%s recovery_error=%s",
                    recovery_loaded,
                    database_unavailable,
                    str(application.state.v25_execution.get("recovery_error") or "")[:160],
                )
                await asyncio.sleep(5)
                continue
            automation_telemetry("AUTOMATION_LOOP running", reason="loop_running")
            credentials = await auto_session_credentials(application, application.state.v25_execution)
            if not usable_live_credentials(credentials):
                credentials = await auto_session_credentials(
                    application,
                    application.state.v25_execution,
                    force_refresh=True,
                )
                refresh_succeeded = usable_live_credentials(credentials)
                refresh_event = "CREDENTIAL_REFRESH_RETRY_SUCCEEDED" if refresh_succeeded else "CREDENTIAL_REFRESH_RETRY_FAILED"
                add_event(
                    application.state.v25_execution,
                    refresh_event,
                    "İlk credential çözümlemesi boş döndü; tek zorunlu yenileme denemesi tamamlandı.",
                )
                automation_telemetry(refresh_event, reason=refresh_event)
            if not usable_live_credentials(credentials):
                state = application.state.v25_execution
                if state["auto"].get("enabled") and consent_grace_expired(state):
                    state["auto"].update({"enabled": False, "session_until": 0.0, "last_skip_reason": "consent_expired", "last_cycle_stage": "skipped", "last_decision": "24 saatlik canlı izin sona erdi; yeni girişler durduruldu."})
                    state["live_auto_trade"] = False
                    state["real_trading_locked"] = True
                    state["armed_until"] = 0.0
                    add_event(state, "LIVE_CONSENT_GRACE_EXPIRED", "15 dakikalık yeniden onay süresi doldu; Auto Trade kapatıldı, açık pozisyon korunuyor.")
                    persist_state(state)
                    await asyncio.sleep(5)
                    continue
                monitoring_stale_since = state.get("monitoring_credentials_stale_since")
                now_epoch = time.time()
                if monitoring_stale_since is None:
                    state["monitoring_credentials_stale_since"] = now_epoch
                elif state.get("connected") and now_epoch - float(monitoring_stale_since) > MONITORING_CREDENTIALS_STALE_SECONDS:
                    state["connected"] = False
                    state["connection"].update({
                        "last_checked": now_iso(),
                        "last_error": "Canlı hesap izleme oturum yetkisi süresi doldu; hesabı yeniden bağlayın.",
                    })
                    add_event(state, "LIVE_MONITORING_CREDENTIALS_STALE", "Canlı hesap izleme oturumu yenilenemedi; manuel yeniden bağlantı gerekiyor.")
                    persist_state(state)
                application.state.v25_execution["auto"]["last_skip_reason"] = "no_credentials"
                application.state.v25_execution["auto"]["last_cycle_stage"] = "skipped"
                automation_telemetry("AUTOMATION_SKIP reason=no_credentials", reason="no_credentials")
                await asyncio.sleep(5)
                continue
            reconciliation_stage = "account_reconciliation"
            application.state.v25_execution["monitoring_credentials_stale_since"] = None
            async with application.state.v25_execution["lock"]:
                await reconcile(application, credentials=credentials)
            reconciliation_stage = "automatic_cycle"
            await automatic_cycle(application, credentials=credentials)
            backoff = 5
            application.state.v25_execution["connection"]["retry_count"] = 0
            await asyncio.sleep(RECONCILE_SECONDS)
        except asyncio.CancelledError:
            raise
        except LiveRateLimitError as exc:
            automation_telemetry("AUTOMATION_SKIP reason=binance_rate_limited", reason="binance_rate_limited")
            state = application.state.v25_execution
            state["connected"] = False
            state["connection"].update({"last_checked": now_iso(), "last_error": str(exc)[:240]})
            state["connection"]["retry_count"] = int(state["connection"].get("retry_count") or 0) + 1
            lock_reconciliation_failure(state)
            persist_state(state)
            await asyncio.sleep(rate_limit_backoff_seconds(exc, backoff))
            backoff = min(60, backoff * 2) if exc.retry_after is None else 5
        except LiveExchangeError as exc:
            if exc.transient_read_failure:
                state = application.state.v25_execution
                if mark_transient_market_data_failure(state, exc):
                    state["connected"] = False
                    state["connection"].update({"last_checked": now_iso(), "last_error": str(exc)[:240]})
                    persist_state(state)
                    await asyncio.sleep(1)
                    continue
            raise
        except Exception as exc:
            automation_telemetry("AUTOMATION_SKIP reason=reconcile_failed", reason="reconcile_failed")
            state = application.state.v25_execution
            safe_message = sanitized_exception_message(exc)
            state["connected"] = False
            state["connection"].update({"last_checked": now_iso(), "last_error": safe_message})
            state["connection"]["retry_count"] = int(state["connection"].get("retry_count") or 0) + 1
            if unresolved_execution_evidence(state):
                lock_live_execution(state, "RECONCILIATION_FAILURE", unknown=True)
            else:
                lock_reconciliation_failure(state)
            frame = traceback.extract_tb(exc.__traceback__)[-1] if exc.__traceback__ else None
            add_event(
                state,
                "RECONCILIATION_FAILURE_DIAGNOSTIC",
                "Reconciliation failure diagnostic metadata recorded.",
                exception_type=type(exc).__name__,
                exception_message=safe_message,
                source=os.path.basename(frame.filename) if frame else os.path.basename(__file__),
                function=frame.name if frame else "execution_loop",
                line=frame.lineno if frame else 0,
                reconciliation_stage=reconciliation_stage,
            )
            persist_state(state)
            await asyncio.sleep(backoff)
            backoff = min(60, backoff * 2)


def init_v25_execution(application: Any) -> None:
    state = load_state()
    state["lock"] = asyncio.Lock()
    state["_app"] = application
    state["recovery_loaded"] = not bool(os.getenv("DATABASE_URL", "").strip())
    state["recovery_ready"] = state["recovery_loaded"]
    state["recovery_error"] = None if state["recovery_ready"] else "PostgreSQL recovery pending."
    application.state.v25_execution = state
    add_event(state, "V25_START", "V25 Live Guard başladı; canlı giriş ve otomasyon güvenlik için kapalı.")
    application.state.v25_execution_task = asyncio.create_task(execution_loop(application))
    application.state.v25_live_stream_task = asyncio.create_task(live_user_stream_loop(application))


async def shutdown_v25_execution(application: Any) -> None:
    state = getattr(application.state, "v25_execution", None)
    tasks = [
        task for task in (
            getattr(application.state, "v25_execution_task", None),
            getattr(application.state, "v25_live_stream_task", None),
        )
        if task is not None
    ]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    persistence_tasks = list(getattr(application.state, "_v25_persistence_tasks", set()))
    if persistence_tasks:
        await asyncio.gather(*persistence_tasks, return_exceptions=True)
    if state:
        state["armed_until"] = 0.0
        state["auto"]["enabled"] = False
        state["auto"]["session_until"] = 0.0
        persist_state(state)


@router.get("/status")
async def v25_status(request: Request) -> dict[str, Any]:
    execution_owner(request)
    return public_status(request.app, request)


@router.get("/mtf/history")
async def v25_mtf_history(request: Request, hours: int = Query(default=48, ge=24, le=48)) -> dict[str, Any]:
    """Return the persisted MTF decisions and the non-production OR simulation."""
    execution_owner(request)
    state = request.app.state.v25_execution
    cutoff = time.time() - hours * 60 * 60
    history = [
        row for row in prune_mtf_decision_history(state.get("mtf_decision_history"))
        if float(row.get("timestamp_epoch") or 0) >= cutoff
    ]
    return {
        "window_hours": hours,
        "records": history,
        "or_gate_simulation": summarize_mtf_relaxation(history, min_confidence=float(state["policy"]["min_confidence"])),
        "production_gate_unchanged": True,
    }


@router.get("/market/candles")
async def v25_market_candles(
    request: Request,
    symbol: str = Query(min_length=5, max_length=20),
    interval: Literal["1m", "5m", "15m", "1h", "4h"] = "15m",
    limit: int = Query(default=360, ge=50, le=500),
) -> dict[str, Any]:
    """Owner-only live chart feed; never exposes credentials to the browser."""
    execution_owner(request)
    try:
        normalized = normalize_symbol(symbol)
        candles, last_open_time = await live_candles(public_client_for(request.app), normalized, interval, limit)
        return {
            "symbol": normalized,
            "interval": interval,
            "candles": candles,
            "last_open_time": last_open_time,
            "updated_at": now_iso(),
            "orders_created": False,
        }
    except (LiveExchangeError, BinanceDemoError) as exc:
        raise safe_exchange_error(exc) from exc


async def connect_read_only_for_request(application: Any, request: Request, *, actor: str | None = None) -> dict[str, Any]:
    state = application.state.v25_execution
    try:
        async with state["lock"]:
            client = client_for(application, request)
            snapshot = await account_snapshot(client)
            _, _, fingerprint = live_credentials_status(request)
            update_account_snapshot(state, snapshot, session_binding=session_id(request))
            state["live_session_authorization"] = {
                "session_id": session_id(request),
                "user_id": str(actor or ""),
                "fingerprint": fingerprint or "",
            }
            if not state.get("reconciliation_required") and not unresolved_execution_evidence(state):
                state["recovery_ready"] = True
                state["recovery_error"] = None
            state["connected"] = True
            state["connection"].update({"last_checked": now_iso(), "last_error": None, "clock_offset_ms": client.time_offset_ms})
            add_event(state, "READ_ONLY_CONNECTED", "Canlı hesap salt-okunur bağlantısı doğrulandı; emir gönderilmedi.", actor=actor)
            persist_state(state)
        return public_status(application, request)
    except (LiveExchangeError, BinanceDemoError) as exc:
        state["connected"] = False
        state["snapshot_session_id"] = ""
        state["snapshot"] = {}
        state["connection"].update({"last_checked": now_iso(), "last_error": str(exc)[:240]})
        raise safe_exchange_error(exc) from exc


@router.post("/connect/read-only")
async def v25_connect(request: Request) -> dict[str, Any]:
    user = execution_owner(request)
    return await connect_read_only_for_request(request.app, request, actor=str(user["id"]))


@router.put("/policy")
async def v25_policy(request: Request, body: PolicyUpdate) -> dict[str, Any]:
    user = execution_owner(request)
    state = request.app.state.v25_execution
    updates = body.model_dump(exclude_none=True)
    state["policy"] = sanitize_execution_policy({**state["policy"], **updates})
    if consent_status(state, request).get("active"):
        state["policy_ack_digest"] = policy_digest(state["policy"])
    else:
        state["policy_ack_digest"] = None
    state["auto"]["enabled"] = False
    state["auto"]["session_until"] = 0.0
    state["live_auto_trade"] = False
    add_event(state, "POLICY_CHANGED", "Canlı risk limitleri değişti; aktif 24 saatlik izin korunurken otomasyon durduruldu.", actor=user["id"])
    persist_state(state)
    return public_status(request.app, request)


@router.post("/policy/acknowledge")
async def v25_policy_ack(request: Request, body: Confirmation) -> dict[str, Any]:
    user = execution_owner(request)
    if body.confirmation.strip().upper() != "RİSK LİMİTLERİNİ ONAYLIYORUM":
        raise HTTPException(422, "Onay için RİSK LİMİTLERİNİ ONAYLIYORUM yazın.")
    state = request.app.state.v25_execution
    state["policy_ack_digest"] = policy_digest(state["policy"])
    add_event(state, "POLICY_ACK", "Mevcut canlı risk politikası sahibi tarafından onaylandı.", actor=user["id"])
    persist_state(state)
    return public_status(request.app, request)


@router.post("/consent")
async def v25_web_consent(request: Request, body: Confirmation) -> dict[str, Any]:
    """Create a fingerprint-bound, expiring 24-hour live consent."""
    user = execution_owner(request)
    if body.confirmation.strip().upper() != "CANLI İŞLEM RİSKİNİ 24 SAAT KABUL EDİYORUM":
        raise HTTPException(422, "Onay için CANLI İŞLEM RİSKİNİ 24 SAAT KABUL EDİYORUM yazın.")
    state = request.app.state.v25_execution
    api_key, secret_key, fingerprint = live_credentials_status(request)
    if not api_key or len(secret_key) < 10 or not fingerprint:
        raise HTTPException(412, "Önce programdaki Borsa Bağlantıları bölümünden canlı Binance API anahtarını kaydedip aktifleştirin.")
    state["live_session_authorization"] = {
        "session_id": session_id(request),
        "user_id": str(user["id"]),
        "fingerprint": fingerprint,
    }
    state["web_consent"] = {
        "accepted_at": now_iso(),
        "expires_at_epoch": time.time() + (24 * 60 * 60),
        "key_fingerprint": fingerprint,
    }
    state["policy_ack_digest"] = policy_digest(state["policy"])
    state["armed_until"] = 0.0
    state["auto"]["enabled"] = False
    state["auto"]["session_until"] = 0.0
    add_event(state, "LIVE_WEB_CONSENT", "24 saatlik canlı risk izni verildi; süre veya API key fingerprint değişene kadar korunur.", actor=user["id"])
    persist_state(state)
    return public_status(request.app, request)


@router.post("/consent/revoke")
async def v25_revoke_web_consent(request: Request, body: Confirmation) -> dict[str, Any]:
    user = execution_owner(request)
    if body.confirmation.strip().upper() != "24 SAATLİK CONSENTİ KALDIR":
        raise HTTPException(422, "Onay için 24 SAATLİK CONSENTİ KALDIR yazın.")
    state = request.app.state.v25_execution
    state["web_consent"] = {"accepted_at": None, "expires_at_epoch": 0.0, "key_fingerprint": None}
    state["live_session_authorization"] = None
    state["armed_until"] = 0.0
    state["auto"]["enabled"] = False
    state["auto"]["session_until"] = 0.0
    add_event(state, "LIVE_WEB_CONSENT_REVOKED", "24 saatlik canlı risk izni kaldırıldı; LIVE kilidi kapatıldı.", actor=user["id"])
    persist_state(state)
    return public_status(request.app, request)


@router.post("/order/test")
async def v25_order_test(request: Request, body: LiveOrderRequest) -> dict[str, Any]:
    user = execution_owner(request)
    state = request.app.state.v25_execution
    try:
        client = client_for(request.app, request)
        spec = await build_live_spec(client, body, state["policy"])
        result = await submit_entry(client, spec, client_id_for("TEST", body.intent_id or uuid.uuid4().hex), test_only=True)
        add_event(state, "LIVE_ORDER_TEST", f"{spec['symbol']} imzalı /order/test doğrulandı; gerçek emir oluşmadı.", actor=user["id"], symbol=spec["symbol"])
        persist_state(state)
        return {"ok": True, "exchange_response": result, "created_order": False, "message": "Binance canlı imza ve emir şeması doğrulandı; gerçek emir oluşturulmadı."}
    except (LiveExchangeError, BinanceDemoError) as exc:
        raise safe_exchange_error(exc) from exc


@router.post("/risk/preview")
async def v25_risk_preview(request: Request, body: RiskPreviewRequest) -> dict[str, Any]:
    execution_owner(request)
    state = request.app.state.v25_execution
    policy = sanitize_execution_policy({
        **state["policy"],
        "max_margin_per_trade": body.margin_usdt,
        "max_leverage": body.leverage,
    })
    try:
        risk = risk_sized_order(body.entry, body.stop_loss, policy, atr=body.atr)
    except ValueError as exc:
        raise HTTPException(422, f"Risk preview reddedildi: {exc}") from exc
    return {
        "ok": True,
        "orders_created": False,
        "entry": risk["entry"],
        "stop": risk["stop"],
        "stop_distance_pct": risk["stop_distance_pct"],
        "max_stop_distance_pct": risk["max_stop_distance_pct"],
        "leverage": risk["leverage"],
        "notional_usdt": risk["notional_usdt"],
        "margin_usdt": risk["margin_usdt"],
        "estimated_stop_loss_usdt": risk["estimated_stop_loss_usdt"],
        "capped": risk["capped"],
        "atr": risk["atr"],
    }


@router.post("/arm")
async def v25_arm(request: Request, body: Confirmation) -> dict[str, Any]:
    user = execution_owner(request)
    if body.confirmation.strip().upper() != "CANLI EMİR RİSKİNİ KABUL EDİYORUM":
        raise HTTPException(422, "Kilidi açmak için CANLI EMİR RİSKİNİ KABUL EDİYORUM yazın.")
    state = request.app.state.v25_execution
    if live_execution_blocked(state):
        raise HTTPException(423, "Canlı acil durdurma veya belirsiz uzlaştırma aktif; önce recovery tamamlanmalı.")
    release = readiness(request.app, state, request)
    if not release["ready"]:
        pending = next((item["label"] for item in release["gates"] if not item["passed"]), "hazırlık kapısı")
        raise HTTPException(423, f"Canlı kilit açılamadı: {pending} bekleniyor.")
    state["armed_until"] = time.time() + LIVE_ARM_SECONDS
    state["real_trading_locked"] = False
    add_event(state, "LIVE_ARM", "Canlı yeni giriş izni 24 saat için açıldı.", actor=user["id"])
    return public_status(request.app, request)


@router.post("/disarm")
async def v25_disarm(request: Request) -> dict[str, Any]:
    user = execution_owner(request)
    state = request.app.state.v25_execution
    state["armed_until"] = 0.0
    state["real_trading_locked"] = True
    state["live_auto_trade"] = False
    state["auto_authorization"] = initial_state()["auto_authorization"]
    state["auto"]["enabled"] = False
    state["auto"]["session_until"] = 0.0
    add_event(state, "LIVE_DISARM", "Canlı yeni girişler ve otomasyon kilitlendi; korumalar çalışmaya devam eder.", actor=user["id"])
    return public_status(request.app, request)


@router.post("/recovery/check")
async def v25_recovery_check(request: Request, body: Confirmation) -> dict[str, Any]:
    """Reconcile a fail-closed state before allowing the owner to re-arm."""
    execution_owner(request)
    if body.confirmation.strip().upper() != "LIVE RECOVERY CHECK":
        raise HTTPException(422, "Recovery kontrolü için LIVE RECOVERY CHECK yazın.")
    state = request.app.state.v25_execution
    async with state["lock"]:
        try:
            await reconcile(request.app, credentials=live_credentials_status(request)[:2])
        except (LiveExchangeError, BinanceDemoError) as exc:
            raise safe_exchange_error(exc) from exc
    return public_status(request.app, request)


@router.post("/order")
async def v25_order(request: Request, body: ManualLiveOrderRequest) -> dict[str, Any]:
    execution_owner(request)
    if body.confirmation.strip().upper() != "CANLI EMİR GÖNDER":
        raise HTTPException(422, "Canlı manuel emir için CANLI EMİR GÖNDER yazın.")
    payload = body.model_dump(exclude={"confirmation"})
    return await execute_live_order(request.app, LiveOrderRequest(**payload), source="MANUAL", request=request)


@router.post("/auto/start")
async def v25_auto_start(request: Request, body: Confirmation) -> dict[str, Any]:
    user = execution_owner(request)
    if body.confirmation.strip().upper() != "CANLI OTOMATİK":
        raise HTTPException(422, "Otomasyonu açmak için CANLI OTOMATİK yazın.")
    state = request.app.state.v25_execution
    allowed, reason = live_auto_start_gate(request.app, state, request)
    if not allowed:
        raise HTTPException(423, reason)
    state["auto"].update({"enabled": True, "session_until": time.time() + LIVE_AUTO_SESSION_SECONDS, "last_error": None, "last_decision": "Bir saatlik gözetimli canlı tarama başlatıldı."})
    state["live_auto_trade"] = True
    state["real_trading_locked"] = False
    state["armed_until"] = 0.0
    api_key, secret_key, fingerprint = live_credentials_status(request)
    live_authorization = state.get("live_session_authorization") or {}
    if not fingerprint and all(str(live_authorization.get(key) or "").strip() for key in ("session_id", "user_id", "fingerprint")):
        live_authorization = dict(live_authorization)
        fingerprint = str(live_authorization["fingerprint"])
    else:
        live_authorization = {
            "session_id": session_id(request),
            "user_id": str(user["id"]),
            "fingerprint": fingerprint if api_key and secret_key else "",
        }
    state["auto_authorization"] = {
        **live_authorization,
        "expires_at_epoch": state["auto"]["session_until"],
    }
    add_event(state, "LIVE_AUTO_START", "Canlı otomasyon 24 saatlik ARM penceresi içinden bir saatlik gözetimli oturum için açıldı.", actor=user["id"])
    persist_state(state)
    return public_status(request.app, request)


@router.post("/auto/stop")
async def v25_auto_stop(request: Request) -> dict[str, Any]:
    user = execution_owner(request)
    state = request.app.state.v25_execution
    state["auto"]["enabled"] = False
    state["auto"]["session_until"] = 0.0
    state["real_trading_locked"] = True
    state["live_auto_trade"] = False
    state["auto_authorization"] = initial_state()["auto_authorization"]
    state["auto"]["last_decision"] = "Yeni otomatik canlı girişler durduruldu."
    add_event(state, "LIVE_AUTO_STOP", "Canlı otomasyon durduruldu; mevcut Stop/TP korumaları açık.", actor=user["id"])
    persist_state(state)
    return public_status(request.app, request)


@router.post("/position/close")
async def v25_close(request: Request, body: CloseRequest) -> dict[str, Any]:
    user = execution_owner(request)
    if body.confirmation.strip().upper() != "CANLI POZİSYONU KAPAT":
        raise HTTPException(422, "Kapatmak için CANLI POZİSYONU KAPAT yazın.")
    state = request.app.state.v25_execution
    plan = state.get("plans", {}).get(body.plan_id)
    if not plan:
        raise HTTPException(404, "Tracked canlı plan bulunamadı.")
    if not live_plan_can_mutate(plan):
        raise HTTPException(423, "Canlı plan provenance doğrulanmadı; pozisyon sahiplenilmedi.")
    try:
        close_intent = f"manual-close-{plan['id']}"
        close_client_id = client_id_for("CLOSE", close_intent)
        close_ids = plan.setdefault("close_client_order_ids", [])
        if close_client_id not in close_ids:
            close_ids.append(close_client_id)
        persist_state(state)
        result = await close_tracked_symbol(client_for(request.app, request), plan["symbol"], close_intent)
        if result and result.get("orderId"):
            order_id = int(result["orderId"])
            known = plan.setdefault("exchange_order_ids", [])
            if order_id not in known:
                known.append(order_id)
        plan["close_reason"] = close_reason_for_intent(close_intent)
        plan["status"] = "KAPATMA EMRİ GÖNDERİLDİ" if result else "KAPANDI"
        add_event(state, "LIVE_CLOSE", f"{plan['symbol']} tracked pozisyon reduce-only kapatıldı.", actor=user["id"], symbol=plan["symbol"])
        persist_state(state)
        return {"ok": True, "order_id": result.get("orderId") if result else None, "plan": plan}
    except LiveExchangeError as exc:
        raise safe_exchange_error(exc) from exc


@router.post("/emergency")
async def v25_emergency(request: Request, body: EmergencyRequest) -> dict[str, Any]:
    user = execution_owner(request)
    if body.confirmation.strip().upper() != "CANLI ACİL DURDUR":
        raise HTTPException(422, "Acil işlem için CANLI ACİL DURDUR yazın.")
    state = request.app.state.v25_execution
    lock_live_execution(state, "MANUAL_EMERGENCY_STOP")
    cancelled_orders = cancelled_algos = closed = 0
    try:
        client = client_for(request.app, request)
        confirmed_plans = [
            plan for plan in state.get("plans", {}).values()
            if isinstance(plan, dict) and live_plan_can_mutate(plan)
        ]
        snapshot = await account_snapshot(client)
        owned_algo_ids = {
            str(row.get("client_algo_id") or "")
            for plan in confirmed_plans
            for row in owned_protection_rows(plan, snapshot.get("open_algo_orders", []))
        }
        for row in snapshot.get("open_orders", []):
            if str(row.get("client_order_id") or "").startswith(LIVE_CLIENT_PREFIX):
                try:
                    await client.signed("DELETE", "/fapi/v1/order", {"symbol": row["symbol"], "orderId": row["order_id"]})
                    cancelled_orders += 1
                except LiveExchangeError:
                    pass
        for row in snapshot.get("open_algo_orders", []):
            if str(row.get("client_algo_id") or "") in owned_algo_ids:
                try:
                    await client.signed("DELETE", "/fapi/v1/algoOrder", {"symbol": row["symbol"], "algoId": row["algo_id"]})
                    cancelled_algos += 1
                except LiveExchangeError:
                    pass
        if body.close_tracked_positions:
            active_plans = [
                plan for plan in state.get("plans", {}).values()
                if live_plan_can_mutate(plan)
                and plan.get("symbol")
                and plan.get("status") not in {"KAPANDI", "İPTAL", "ACİL DURDURULDU"}
            ]
            handled_symbols: set[str] = set()
            for plan in active_plans:
                symbol = str(plan["symbol"])
                if symbol in handled_symbols:
                    continue
                handled_symbols.add(symbol)
                close_intent = f"emergency-close-{plan['id']}"
                close_client_id = client_id_for("CLOSE", close_intent)
                close_ids = plan.setdefault("close_client_order_ids", [])
                if close_client_id not in close_ids:
                    close_ids.append(close_client_id)
                plan["close_reason"] = close_reason_for_intent(close_intent)
                persist_state(state)
                close_result = await close_tracked_symbol(client, symbol, close_intent)
                if close_result:
                    closed += 1
                    if close_result.get("orderId"):
                        order_id = int(close_result["orderId"])
                        known = plan.setdefault("exchange_order_ids", [])
                        if order_id not in known:
                            known.append(order_id)
        for plan in state.get("plans", {}).values():
            if live_plan_can_mutate(plan) and plan.get("status") not in {"KAPANDI", "İPTAL"}:
                plan["status"] = "ACİL DURDURULDU"
        state["emergency"] = {"active": True, "triggered_at": now_iso(), "reason": "Kullanıcı canlı acil durdurma komutu"}
        add_event(state, "LIVE_EMERGENCY", f"{cancelled_orders} giriş, {cancelled_algos} koruma iptal; {closed} tracked pozisyon kapanış emri.", actor=user["id"])
        persist_state(state)
        return {"ok": True, "cancelled_bot_orders": cancelled_orders, "cancelled_bot_algos": cancelled_algos, "closed_tracked_positions": closed, "armed": False}
    except (LiveExchangeError, BinanceDemoError) as exc:
        raise safe_exchange_error(exc) from exc
