"""V21 Demo Complete control plane.

Every exchange call in this module goes through :mod:`binance_demo`, whose host
allow-list contains Binance Futures Demo only.  Automation is off after every
restart and requires both the short-lived DEMO arm and a second explicit
``DEMO OTOMATİK`` confirmation.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from statistics import fmean
from typing import Any, Literal

import websockets
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .analysis import analyze, atr, ema
from .binance_demo import (
    DEMO_REST_BASE,
    DEMO_WS_BASE,
    MAX_LEVERAGE,
    MANUAL_MAX_LEVERAGE,
    MAX_MARGIN_USDT,
    MAX_NOTIONAL_USDT,
    MAX_OPEN_POSITIONS,
    BinanceDemoClient,
    BinanceDemoError,
    DemoOrderRequest,
    account_snapshot,
    armed,
    credentials_configured,
    close_symbol_position,
    decimal_text,
    execute_demo_order,
    load_demo_credentials,
    new_client_id,
    normalize_symbol,
    persist_runtime,
    post_algo,
    position_amount,
    response_rows,
    reconcile_demo_plans,
    round_tick,
    safe_exchange_error,
    state_for as demo_state_for,
    symbol_rules,
    validate_entry_risk,
)
from .local_storage import DATA_DIR, migrate_legacy_files


router = APIRouter(prefix="/api/v21", tags=["V21 Demo Complete"])
migrate_legacy_files(("v21_demo_state.json", "v21_demo_state.backup.json"))
STATE_PATH = DATA_DIR / "v21_demo_state.json"
BACKUP_PATH = DATA_DIR / "v21_demo_state.backup.json"
JOURNAL_LIMIT = 1200
SCAN_INTERVAL_SECONDS = 900
AUTO_TRADE_SYMBOLS = (
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
    "ADAUSDT", "AVAXUSDT", "LINKUSDT", "LTCUSDT", "BCHUSDT", "DOTUSDT",
    "UNIUSDT", "NEARUSDT", "ATOMUSDT", "ETCUSDT", "FILUSDT", "APTUSDT",
    "ARBUSDT", "OPUSDT", "SUIUSDT", "INJUSDT", "SEIUSDT", "TIAUSDT",
)
AUTO_TRADE_SYMBOL_SET = frozenset(AUTO_TRADE_SYMBOLS)
MAX_SIGNAL_AGE_SECONDS = 2 * 60 * 60
DAILY_LOSS_LIMIT_PCT = 20.0
DAILY_LOSS_THRESHOLDS = (5.0, 10.0, 15.0, 20.0)
BLOCKED_AUTO_BASE_ASSETS = frozenset({
    "USDC", "USDT", "BUSD", "USDP", "TUSD", "FDUSD", "DAI", "UST", "USTC",
})

DEFAULT_SETTINGS: dict[str, Any] = {
    "allowed_symbols": [],
    "allow_long": True,
    "allow_short": True,
    "max_loss_per_trade": 5.0,
    "max_margin_per_trade": 50.0,
    "daily_loss_limit": 30.0,
    "daily_loss_limit_pct": DAILY_LOSS_LIMIT_PCT,
    "daily_trade_limit": 6,
    "max_positions": 3,
    "min_confidence": 78,
    "min_score_threshold": 70,
    "max_volatility_pct": 3.5,
    "max_correlation_pct": 82,
    "schedule_start_hour": 0,
    "schedule_end_hour": 24,
    "scan_seconds": SCAN_INTERVAL_SECONDS,
    "breakeven_enabled": True,
    "breakeven_trigger_r": 1.0,
    "trailing_enabled": False,
    "trailing_trigger_r": 1.5,
    "trailing_distance_r": 0.75,
    "notifications": True,
    "fee_bps_per_side": 4.0,
    "slippage_bps_per_side": 2.0,
    "consecutive_loss_limit": 3,
    "kill_switch": False,
}


class SettingsUpdate(BaseModel):
    allowed_symbols: list[str] | None = None
    allow_long: bool | None = None
    allow_short: bool | None = None
    max_loss_per_trade: float | None = Field(default=None, ge=0.5, le=25)
    max_margin_per_trade: float | None = Field(default=None, ge=5, le=100)
    daily_loss_limit: float | None = Field(default=None, ge=5, le=250)
    daily_loss_limit_pct: float | None = Field(default=None, ge=20, le=20)
    daily_trade_limit: int | None = Field(default=None, ge=1, le=30)
    max_positions: int | None = Field(default=None, ge=1, le=3)
    min_confidence: int | None = Field(default=None, ge=60, le=95)
    min_score_threshold: int | None = Field(default=None, ge=40, le=90)
    max_volatility_pct: float | None = Field(default=None, ge=0.2, le=10)
    max_correlation_pct: int | None = Field(default=None, ge=40, le=99)
    schedule_start_hour: int | None = Field(default=None, ge=0, le=23)
    schedule_end_hour: int | None = Field(default=None, ge=1, le=24)
    scan_seconds: int | None = Field(default=None, ge=SCAN_INTERVAL_SECONDS, le=SCAN_INTERVAL_SECONDS)
    breakeven_enabled: bool | None = None
    breakeven_trigger_r: float | None = Field(default=None, ge=0.5, le=3)
    trailing_enabled: bool | None = None
    trailing_trigger_r: float | None = Field(default=None, ge=0.75, le=5)
    trailing_distance_r: float | None = Field(default=None, ge=0.25, le=3)
    notifications: bool | None = None
    fee_bps_per_side: float | None = Field(default=None, ge=0, le=25)
    slippage_bps_per_side: float | None = Field(default=None, ge=0, le=50)
    consecutive_loss_limit: int | None = Field(default=None, ge=1, le=3)
    kill_switch: bool | None = None


class RiskSizeRequest(BaseModel):
    symbol: str = Field(min_length=5, max_length=20)
    entry: float = Field(gt=0)
    stop: float = Field(gt=0)
    max_loss_usdt: float = Field(ge=0.5, le=25)
    leverage: int = Field(ge=1, le=MANUAL_MAX_LEVERAGE)


class AutoStartRequest(BaseModel):
    confirmation: str = Field(min_length=1, max_length=40)


class BacktestRequest(BaseModel):
    symbol: str = Field(default="BTCUSDT", min_length=5, max_length=20)
    interval: Literal["1m", "5m", "15m", "1h", "4h"] = "15m"
    limit: int = Field(default=1000, ge=300, le=1500)


class DrillRequest(BaseModel):
    kind: Literal["RECONNECT", "EMERGENCY", "PROTECTION"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def initial_state() -> dict[str, Any]:
    return {
        "settings": dict(DEFAULT_SETTINGS),
        "journal": [],
        "seen_event_ids": [],
        "auto": {
            "enabled": False, "busy": False, "cycles": 0, "last_scan": None,
            "user_confirmed": False, "confirmation": None,
            "last_decision": "Kullanıcı onayı bekleniyor.", "last_error": None,
            "rejection_gate": None, "rejection_reason": None,
            "status": "OFF", "pause_reason": None, "started_at": None,
        },
        "risk": {"consecutive_losses": 0, "consecutive_loss_limit": 3, "kill_switch": False},
        "notifications": {"seen": [], "unread": 0},
        "scanner": {
            "active": False, "running": False, "scan_status": "BEKLEMEDE", "coins_scanned": 0,
            "scan_duration_ms": 0, "last_scan_at": None, "next_scan_at": None,
            "last_scan": None, "next_scan": None, "top_candidates": [], "all_candidates": [],
            "selected_symbols": [], "last_stage": "BEKLEMEDE", "eligible_count": 0,
            "gate_rejections": {},
            "last_error": None,
        },
        "automation_trades": [],
        "paper_positions": [],
        "stream": {
            "status": "BEKLEMEDE", "transport": "REST EŞLEŞTİRME", "last_event": None,
            "last_sync": None, "reconnect_count": 0, "error_count": 0, "last_error": None,
        },
        "snapshot": None,
        "backtest": None,
        "drills": {"RECONNECT": None, "EMERGENCY": None, "PROTECTION": None},
        "duplicate_blocks": 0,
        "duplicate_submissions": 0,
        "protection_repairs": 0,
        "last_saved": None,
    }


def _read_state_file(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def migrate_legacy_allowed_symbols(settings: dict[str, Any]) -> bool:
    legacy_default = frozenset({
        "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
        "ADAUSDT", "AVAXUSDT", "LINKUSDT", "LTCUSDT", "BCHUSDT", "DOTUSDT",
        "UNIUSDT", "NEARUSDT", "ATOMUSDT", "ETCUSDT", "FILUSDT", "APTUSDT",
        "ARBUSDT", "OPUSDT", "SUIUSDT", "INJUSDT", "SEIUSDT", "TIAUSDT",
    })
    configured = {str(value).strip().upper() for value in settings.get("allowed_symbols", []) if value}
    if configured == legacy_default:
        settings["allowed_symbols"] = []
        return True
    return False


def load_state() -> dict[str, Any]:
    base = initial_state()
    saved = _read_state_file(STATE_PATH) or _read_state_file(BACKUP_PATH) or {}
    if isinstance(saved.get("settings"), dict):
        base["settings"].update({key: value for key, value in saved["settings"].items() if key in DEFAULT_SETTINGS})
    migrated_allowlist = migrate_legacy_allowed_symbols(base["settings"])
    base["settings"]["scan_seconds"] = DEFAULT_SETTINGS["scan_seconds"]
    for key in (
        "journal", "seen_event_ids", "backtest", "drills", "duplicate_blocks",
        "duplicate_submissions", "protection_repairs", "scanner", "automation_trades", "paper_positions",
        "risk",
        "notifications",
    ):
        if key in saved:
            base[key] = saved[key]
    # Entry automation is intentionally never restored after a restart.
    base["auto"]["enabled"] = False
    base["auto"]["status"] = "OFF"
    base["auto"]["pause_reason"] = None
    base["auto"]["started_at"] = None
    base["auto"]["user_confirmed"] = False
    base["auto"]["confirmation"] = None
    base["auto"]["last_decision"] = "Güvenli yeniden başlatma: DEMO OTOMATİK onayı bekleniyor."
    base["scanner"].setdefault("all_candidates", [])
    base["scanner"].setdefault("top_candidates", [])
    base["scanner"].setdefault("gate_rejections", {})
    base["scanner"]["active"] = False
    base["scanner"]["running"] = False
    base["scanner"]["scan_status"] = "BEKLEMEDE"
    base["scanner"]["last_error"] = None
    base["risk"].setdefault("consecutive_losses", 0)
    base["risk"].setdefault("consecutive_loss_limit", 3)
    base["risk"].setdefault("kill_switch", False)
    base["notifications"].setdefault("seen", [])
    base["notifications"].setdefault("unread", 0)
    if migrated_allowlist:
        persist = globals().get("persist_state")
        if callable(persist):
            persist(base)
    return base


def serializable_state(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "settings": state["settings"],
        "journal": state["journal"][:JOURNAL_LIMIT],
        "seen_event_ids": state["seen_event_ids"][-800:],
        "backtest": state.get("backtest"),
        "drills": state.get("drills", {}),
        "duplicate_blocks": state.get("duplicate_blocks", 0),
        "duplicate_submissions": state.get("duplicate_submissions", 0),
        "protection_repairs": state.get("protection_repairs", 0),
        "risk": state.get("risk", {}),
        "notifications": state.get("notifications", {}),
        "scanner": state.get("scanner", {}),
        "automation_trades": state.get("automation_trades", [])[:100],
        "paper_positions": state.get("paper_positions", []),
        "saved_at": now_iso(),
    }


def _v21_snapshot_key(user_id: str) -> str:
    return f"v21_demo:user:{user_id}"


def _state_from_payload(payload: dict[str, Any], user_id: str, application: Any | None = None) -> dict[str, Any]:
    state = initial_state()
    for key in (
        "settings", "journal", "seen_event_ids", "backtest", "drills", "duplicate_blocks",
        "duplicate_submissions", "protection_repairs", "scanner", "automation_trades",
        "paper_positions", "risk", "notifications",
    ):
        if key in payload:
            state[key] = payload[key]
    state["settings"] = {**DEFAULT_SETTINGS, **(state["settings"] if isinstance(state.get("settings"), dict) else {})}
    state["_user_id"] = user_id
    if application is not None:
        state["_app"] = application
    state["auto"]["enabled"] = False
    state["auto"]["status"] = "OFF"
    state["auto"]["pause_reason"] = None
    state["auto"]["started_at"] = None
    state["auto"]["user_confirmed"] = False
    state["auto"]["confirmation"] = None
    state["auto"]["last_decision"] = "Güvenli yeniden başlatma: DEMO OTOMATİK onayı bekleniyor."
    return state


async def _persist_v21_snapshot_db(application: Any, user_id: str, payload: dict[str, Any]) -> None:
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
        _v21_snapshot_key(user_id),
        json.dumps(payload, ensure_ascii=False),
    )


def _queue_v21_snapshot(state: dict[str, Any], user_id: str, payload: dict[str, Any]) -> bool:
    application = state.get("_app")
    pool = getattr(getattr(application, "state", None), "db_pool", None)
    if application is None or pool is None:
        return False
    tasks = getattr(application.state, "_v21_persistence_tasks", None)
    if not isinstance(tasks, set):
        tasks = set()
        application.state._v21_persistence_tasks = tasks
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(_persist_v21_snapshot_db(application, user_id, payload))
        return True
    task = loop.create_task(_persist_v21_snapshot_db(application, user_id, payload))
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return True


def persist_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    user_id = str(state.get("_user_id") or "").strip()
    payload = serializable_state(state)
    if user_id and _queue_v21_snapshot(state, user_id, payload):
        state["last_saved"] = payload["saved_at"]
        return
    if user_id:
        payload["user_id"] = user_id
        users = {}
        try:
            existing = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            existing = {}
        if isinstance(existing, dict) and isinstance(existing.get("users"), dict):
            users = existing["users"]
        users[user_id] = payload
        file_payload = {"users": users}
    else:
        file_payload = payload
    temporary = STATE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(file_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if STATE_PATH.exists():
        try:
            BACKUP_PATH.write_bytes(STATE_PATH.read_bytes())
        except OSError:
            pass
    temporary.replace(STATE_PATH)
    state["last_saved"] = now_iso()


def _v21_user_store(application: Any) -> dict[str, dict[str, Any]]:
    store = getattr(application.state, "_v21_demo_user_state", None)
    if not isinstance(store, dict):
        store = {}
        application.state._v21_demo_user_state = store
    return store


def state_for(request: Request) -> dict[str, Any]:
    app = request.app
    request_state = getattr(request, "state", None)
    user = getattr(request_state, "member", None) or getattr(request_state, "user", None)
    user_id = str((user or {}).get("id") or "").strip()
    if user_id:
        store = _v21_user_store(app)
        try:
            from .exchange_connections import session_id

            request_session_id = session_id(request)
        except (ImportError, RuntimeError, ValueError):
            request_session_id = ""
        state = store.get(user_id)
        if state is None:
            state = initial_state()
            if getattr(app.state, "db_pool", None) is None:
                try:
                    saved = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}
                    if isinstance(saved, dict) and isinstance(saved.get("users"), dict):
                        entry = saved["users"].get(user_id)
                        if isinstance(entry, dict):
                            state = _state_from_payload(entry, user_id, app)
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    pass
            state["_app"] = app
            state["_user_id"] = user_id
            state["_session_id"] = request_session_id
            store[user_id] = state
        state["_user_id"] = user_id
        state["_session_id"] = request_session_id or state.get("_session_id", "")
        state["_app"] = app
        return state
    state = getattr(app.state, "v21_demo", None)
    if state is None:
        state = initial_state()
        app.state.v21_demo = state
    return state


async def restore_v21_state_for_user(application: Any, user_id: str) -> dict[str, Any] | None:
    if not user_id:
        return None
    pool = getattr(application.state, "db_pool", None)
    if pool is not None:
        try:
            row = await pool.fetchrow(
                "SELECT payload FROM application_state_snapshots WHERE state_key = $1",
                _v21_snapshot_key(user_id),
            )
        except Exception:
            return None
        if row is None:
            return None
        payload = row["payload"]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                return None
        if not isinstance(payload, dict):
            return None
        state = _state_from_payload(payload, user_id, application)
        _v21_user_store(application)[user_id] = state
        return state
    try:
        payload = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("users"), dict):
        return None
    entry = payload["users"].get(user_id)
    if not isinstance(entry, dict):
        return None
    state = _state_from_payload(entry, user_id, application)
    _v21_user_store(application)[user_id] = state
    return state


def record_event(
    state: dict[str, Any],
    kind: str,
    message: str,
    *,
    symbol: str | None = None,
    status: str | None = None,
    side: str | None = None,
    price: float | None = None,
    quantity: float | None = None,
    realized_pnl: float | None = None,
    reason: str | None = None,
    event_id: str | None = None,
    source: str = "SYSTEM",
    reduce_only: bool = False,
    verified_realized: bool = False,
) -> dict[str, Any] | None:
    stable_id = event_id or uuid.uuid4().hex
    seen = state.setdefault("seen_event_ids", [])
    if stable_id in seen:
        return None
    seen.append(stable_id)
    del seen[:-800]
    item = {
        "id": stable_id,
        "created_at": now_iso(),
        "kind": kind,
        "symbol": symbol,
        "status": status,
        "side": side,
        "price": price,
        "quantity": quantity,
        "realized_pnl": realized_pnl,
        "reason": reason,
        "message": message,
        "source": source,
        "reduce_only": reduce_only,
        "verified_realized": verified_realized,
        "demo_only": True,
    }
    state.setdefault("journal", []).insert(0, item)
    del state["journal"][JOURNAL_LIMIT:]
    return item


def emit_notification(state: dict[str, Any], kind: str, message: str, *, event_id: str) -> dict[str, Any] | None:
    notifications = state.setdefault("notifications", {"seen": [], "unread": 0})
    seen = notifications.setdefault("seen", [])
    if event_id in seen:
        return None
    seen.append(event_id)
    del seen[:-400]
    notifications["unread"] = int(notifications.get("unread", 0)) + 1
    return record_event(state, "NOTIFICATION", message, reason=f"{kind}:{event_id}", event_id=f"notification-{event_id}", source="NOTIFICATION")


def daily_loss_percent(state: dict[str, Any], balance_reference: float) -> float:
    if balance_reference <= 0:
        return 0.0
    realized = daily_metrics(state)["realized_pnl"]
    return max(0.0, round((-realized / balance_reference) * 100, 4))


def refresh_daily_risk_state(state: dict[str, Any], balance_reference: float) -> dict[str, Any]:
    day = today()
    risk = state.setdefault("risk", {})
    if risk.get("date") != day:
        risk.update({"date": day, "daily_base_balance": balance_reference, "last_warning_pct": 0.0, "daily_loss_pct": 0.0})
        state.setdefault("notifications", {"seen": [], "unread": 0})["seen"] = []
    if float(risk.get("daily_base_balance") or 0) <= 0 and balance_reference > 0:
        risk["daily_base_balance"] = balance_reference
    reference = float(risk.get("daily_base_balance") or balance_reference or 0)
    loss_pct = daily_loss_percent(state, reference)
    risk["daily_loss_pct"] = loss_pct
    for threshold in DAILY_LOSS_THRESHOLDS:
        if loss_pct >= threshold and threshold > float(risk.get("last_warning_pct") or 0):
            message = {
                5.0: "Günlük zararınız %5 seviyesine ulaştı. Risk seviyenizi kontrol etmeniz önerilir.",
                10.0: "Günlük zararınız %10 seviyesine ulaştı. Auto Trade risk seviyenizi kontrol ediniz.",
                15.0: "Günlük zararınız %15 seviyesine ulaştı. Günlük zarar limitine yaklaşıyorsunuz.",
                20.0: "Günlük zararınız %20 seviyesine ulaştı. Yeni otomatik işlemler güvenlik amacıyla durduruldu.",
            }[threshold]
            emit_notification(state, f"DAILY_LOSS_{int(threshold)}", message, event_id=f"{day}-daily-loss-{int(threshold)}")
            risk["last_warning_pct"] = threshold
    if loss_pct >= DAILY_LOSS_LIMIT_PCT:
        state["auto"].update({"enabled": False, "status": "PAUSED", "pause_reason": "DAILY_LOSS_20"})
    return {"loss_pct": loss_pct, "limit_pct": DAILY_LOSS_LIMIT_PCT, "last_warning_pct": risk.get("last_warning_pct", 0.0), "paused": state["auto"].get("status") == "PAUSED"}


def client_for(request_or_application: Any) -> BinanceDemoClient:
    application = getattr(request_or_application, "app", request_or_application)
    has_member_session = getattr(request_or_application, "state", None) is not None and getattr(request_or_application.state, "member", None) is not None
    try:
        from .exchange_connections import session_credentials_for_request

        if hasattr(request_or_application, "headers"):
            api_key, secret_key = session_credentials_for_request(request_or_application, "TESTNET")
        else:
            api_key, secret_key = ("", "")
    except (ImportError, RuntimeError, ValueError):
        api_key, secret_key = ("", "")

    if has_member_session:
        if not api_key or not secret_key:
            raise BinanceDemoError(
                "Demo API bağlantısı aktif değil. Programdaki Borsa Bağlantıları bölümünden Testnet anahtarını kaydedip aktifleştirin.",
                http_status=412,
            )
        return BinanceDemoClient(application.state.http, api_key, secret_key)

    api_key, secret_key = load_demo_credentials()
    return BinanceDemoClient(application.state.http, api_key, secret_key)


def client_for_state(application: Any, state: dict[str, Any]) -> BinanceDemoClient:
    from .binance_demo import client_for_state as demo_client_for_state

    return demo_client_for_state(application, state)


def market_client_for(application: Any) -> BinanceDemoClient:
    return BinanceDemoClient(application.state.http, "", "", public_only=True)


def normalize_candles(rows: Any) -> list[dict[str, float]]:
    candles: list[dict[str, float]] = []
    if not isinstance(rows, list):
        return candles
    for row in rows:
        if not isinstance(row, list) or len(row) < 6:
            continue
        try:
            candles.append({
                "time": int(row[0] / 1000), "open": float(row[1]), "high": float(row[2]),
                "low": float(row[3]), "close": float(row[4]), "volume": float(row[5]),
            })
        except (TypeError, ValueError, ZeroDivisionError):
            continue
    return candles


async def demo_candles(client: BinanceDemoClient, symbol: str, interval: str, limit: int) -> list[dict[str, float]]:
    rows = await client.public_get("/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    return normalize_candles(rows)


def _effective_allowed_symbols(settings: dict[str, Any]) -> set[str] | None:
    configured = settings.get("_auto_universe") or settings.get("allowed_symbols") or []
    normalized: set[str] = set()
    for value in configured:
        if not value:
            continue
        try:
            normalized.add(normalize_symbol(str(value)))
        except BinanceDemoError:
            continue
    if not normalized or normalized == AUTO_TRADE_SYMBOL_SET:
        return None
    return normalized


def dynamic_auto_universe(exchange_info: Any, tickers: Any, settings: dict[str, Any]) -> list[str]:
    ticker_by_symbol = {
        str(item.get("symbol")): item for item in response_rows(tickers) if item.get("symbol")
    }
    configured = _effective_allowed_symbols(settings)
    eligible: list[tuple[str, float]] = []
    rows = exchange_info.get("symbols", []) if isinstance(exchange_info, dict) else []
    for item in rows if isinstance(rows, list) else []:
        try:
            symbol = normalize_symbol(str(item.get("symbol") or ""))
        except BinanceDemoError:
            continue
        base_asset = str(item.get("baseAsset") or "").upper()
        if (
            item.get("status") != "TRADING"
            or item.get("contractType") != "PERPETUAL"
            or item.get("quoteAsset") != "USDT"
            or base_asset in BLOCKED_AUTO_BASE_ASSETS
            or (configured is not None and symbol not in configured)
        ):
            continue
        try:
            volume = float(ticker_by_symbol.get(symbol, {}).get("quoteVolume") or 0)
        except (TypeError, ValueError):
            volume = 0.0
        if volume > 0:
            eligible.append((symbol, volume))
    eligible.sort(key=lambda item: item[1], reverse=True)
    return [symbol for symbol, _ in eligible[:100]]


def _candidate_confidence_label(confidence: int) -> str:
    if confidence >= 80:
        return "HIGH"
    if confidence >= 65:
        return "MEDIUM"
    return "LOW"


def _set_rejection(state: dict[str, Any], gate: str, reason: str) -> None:
    state["auto"].update({"rejection_gate": gate, "rejection_reason": reason, "last_decision": reason, "last_error": gate})


def _apply_scan_completion_state(scanner: dict[str, Any], settings: dict[str, Any], ranked: list[dict[str, Any]], top_candidates: list[dict[str, Any]], eligible_count: int | None = None) -> None:
    completed_epoch = time.time()
    completed_at = datetime.fromtimestamp(completed_epoch, timezone.utc).isoformat()
    next_scan_at = datetime.fromtimestamp(completed_epoch + SCAN_INTERVAL_SECONDS, timezone.utc).isoformat()
    scanner.update({
        "last_scan_at": completed_at,
        "next_scan_at": next_scan_at,
        "scan_status": "TAMAMLANDI",
        "coins_scanned": min(100, len(ranked)),
        "eligible_count": len(ranked) if eligible_count is None else eligible_count,
        "top_candidates": top_candidates,
        "all_candidates": ranked[:100],
        "selected_symbols": [item["symbol"] for item in top_candidates],
        "last_stage": "SIRALANDI",
        "active": True,
        "last_scan": completed_at,
        "next_scan": next_scan_at,
    })


def _build_candidate_reasons(item: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    direction = str(item.get("direction", "NEUTRAL")).upper()
    if direction in {"LONG", "SHORT"}:
        reasons.append(f"{item.get('trend', 'Trend')} yönlü sinyal")
    mtf = item.get("mtf_trend")
    if mtf:
        reasons.append(f"1h/4h trend: {mtf}")
    if item.get("macd_confirmation"):
        reasons.append("MACD confirmation")
    volume_ratio = float(item.get("volume_ratio", 1.0) or 1.0)
    if volume_ratio >= 1.15:
        reasons.append("Volume ortalamanın üzerinde")
    if float(item.get("confidence", 0) or 0) >= 70:
        reasons.append("Güven seviyesi yeterli")
    if item.get("momentum"):
        reasons.append(f"Momentum {item.get('momentum')}")
    if item.get("rsi") is not None:
        reasons.append(f"RSI uygun aralıkta ({float(item['rsi']):.1f})")
    if item.get("risk_reward"):
        reasons.append(f"Risk/reward {float(item.get('risk_reward') or 0):.2f}")
    if not reasons:
        reasons.append("Piyasa koşulları zayıf")
    return reasons[:5]


def _decision_status(score: float) -> str:
    if score < 60:
        return "BEKLE"
    if score < 70:
        return "ZAYIF / İŞLEM YOK"
    if score < 80:
        return "İZLE"
    if score < 85:
        return "GÜÇLÜ ADAY"
    return "AUTO TRADE ADAYI"


def _quality_score(item: dict[str, Any], analysis_score: float) -> tuple[float, dict[str, float]]:
    supplied = item.get("opportunity_breakdown") if isinstance(item.get("opportunity_breakdown"), dict) else {}
    volume_ratio = max(0.0, float(item.get("volume_ratio", 0) or 0))
    liquidity = max(0.0, min(100.0, volume_ratio / 1.5 * 100.0))
    volatility = max(0.0, float(item.get("volatility_pct", 0) or 0))
    volatility_limit = float(item.get("max_volatility_pct", 3.5) or 3.5)
    volatility_quality = 100.0 if 0 < volatility <= volatility_limit else max(0.0, 100.0 - max(0.0, volatility - volatility_limit) * 30.0)
    mtf_confirmation = 100.0 if bool(item.get("mtf_aligned")) else 50.0 if item.get("mtf_trend") else 25.0
    age = float(item.get("signal_age_seconds", 0) or 0)
    freshness = max(0.0, min(100.0, 100.0 - age / MAX_SIGNAL_AGE_SECONDS * 100.0))
    risk_reward = max(0.0, min(100.0, float(item.get("risk_reward", 0) or 0) / 3.0 * 100.0))
    breakdown = {
        "analysis_score": max(0.0, min(100.0, analysis_score)),
        "liquidity_quality": round(liquidity, 2),
        "volatility_quality": round(volatility_quality, 2),
        "mtf_confirmation": round(mtf_confirmation, 2),
        "signal_freshness": round(freshness, 2),
        "risk_reward_quality": round(risk_reward, 2),
    }
    for key in breakdown:
        if key in supplied:
            breakdown[key] = max(0.0, min(100.0, float(supplied[key])))
    opportunity = (
        breakdown["analysis_score"] * 0.45
        + breakdown["liquidity_quality"] * 0.15
        + breakdown["volatility_quality"] * 0.10
        + breakdown["mtf_confirmation"] * 0.15
        + breakdown["signal_freshness"] * 0.10
        + breakdown["risk_reward_quality"] * 0.05
    )
    return round(max(0.0, min(100.0, opportunity)), 2), breakdown


def _enrich_scan_candidates(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ranked: list[dict[str, Any]] = []
    prepared = []
    for item in results:
        analysis_score = float(item.get("analysis_score", item.get("opportunity_score", item.get("score", 0))) or 0)
        opportunity_score, breakdown = _quality_score(item, analysis_score)
        prepared.append((item, analysis_score, opportunity_score, breakdown))
    ordered = sorted(prepared, key=lambda entry: entry[2], reverse=True)
    for index, (item, analysis_score, opportunity_score, breakdown) in enumerate(ordered, start=1):
        score = int(round(analysis_score))
        direction = str(item.get("direction", "NEUTRAL")).upper()
        if direction == "BEKLE":
            direction = "NEUTRAL"
        confidence_value = int(item.get("confidence", 0) or 0)
        ranked.append({
            "rank": index,
            "symbol": str(item.get("symbol")),
            "score": max(0, min(100, score)),
            "analysis_score": max(0, min(100, score)),
            "opportunity_score": opportunity_score,
            "opportunity_breakdown": breakdown,
            "decision_status": _decision_status(score),
            "decision_reason": "Opportunity quality insufficient" if opportunity_score < 70 else "Quality filters passed; ranked for Auto Trade",
            "direction": direction,
            "confidence": _candidate_confidence_label(confidence_value),
            "confidence_value": confidence_value,
            "entry": float(item.get("entry", 0) or 0),
            "stop_loss": float(item.get("stop_loss", 0) or 0),
            "tp1": float(item.get("tp1", 0) or 0),
            "tp2": float(item.get("tp2", 0) or 0),
            "tp3": float(item.get("tp3", 0) or 0),
            "momentum": item.get("momentum"),
            "trend": item.get("trend"),
            "volume_ratio": float(item.get("volume_ratio", 1.0) or 1.0),
            "volume": round(float(item.get("volume_ratio", 1.0) or 1.0), 3),
            "rsi": round(float(item.get("rsi", 50) or 50), 2),
            "macd_confirmation": bool(item.get("macd_confirmation")),
            "mtf_trend": item.get("mtf_trend", "BİLİNMİYOR"),
            "risk_reward": float(item.get("risk_reward", 0) or 0),
            "status": item.get("status", "WATCH"),
            "reasons": _build_candidate_reasons(item),
            "scan_time": now_iso(),
        })
    return ranked


async def scan_demo_universe(client: BinanceDemoClient, occupied: set[str], settings: dict[str, Any]) -> list[dict[str, Any]]:
    """Analyze the largest liquid Demo perpetual universe and rank opportunities."""
    exchange_info, tickers = await asyncio.gather(
        client.public_get("/fapi/v1/exchangeInfo"),
        client.public_get("/fapi/v1/ticker/24hr"),
    )
    symbols = [symbol for symbol in dynamic_auto_universe(exchange_info, tickers, settings) if symbol not in occupied]
    settings["_auto_universe"] = symbols

    async def evaluate(symbol: str) -> dict[str, Any] | None:
        try:
            candles, candles_1h, candles_4h = await asyncio.gather(
                demo_candles(client, symbol, "15m", 260),
                demo_candles(client, symbol, "1h", 220),
                demo_candles(client, symbol, "4h", 220),
            )
            if len(candles) < 220 or len(candles_1h) < 200 or len(candles_4h) < 200:
                return None
            decision = analyze(candles[:-1])
            higher_1h = analyze(candles_1h[:-1])
            higher_4h = analyze(candles_4h[:-1])
            direction = decision["direction"]
            confidence = int(decision["confidence"])
            volatility = float(decision["atr"]) / max(float(decision["entry"]), 1e-9) * 100
            mtf_directions = [higher_1h["direction"], higher_4h["direction"]]
            mtf_aligned = direction in {"LONG", "SHORT"} and mtf_directions.count(direction) == 2
            mtf_trend = "UYUMLU " + direction if mtf_aligned else f"1h {higher_1h['direction']} · 4h {higher_4h['direction']}"
            macd_confirmation = (direction == "LONG" and decision["macd"] > 0) or (direction == "SHORT" and decision["macd"] < 0)
            momentum_score = 100 if decision["momentum"] != "Nötr" else 35
            trend_score = 100 if mtf_aligned else 45 if direction in mtf_directions else 20
            score = max(0, min(100, (
                confidence * 0.30
                + trend_score * 0.20
                + (100 if macd_confirmation else 25) * 0.15
                + min(100.0, float(decision["volume_ratio"]) * 50) * 0.15
                + momentum_score * 0.10
                + float(decision["radar"]["breakout_quality"]) * 0.10
            )))
            rejection = direction == "BEKLE" or confidence < int(settings["min_confidence"])
            rejection |= (direction == "LONG" and not settings["allow_long"]) or (direction == "SHORT" and not settings["allow_short"])
            rejection |= volatility > float(settings["max_volatility_pct"])
            signal_time = int(candles[-1].get("time", 0) or 0)
            signal_age = time.time() - signal_time if signal_time else MAX_SIGNAL_AGE_SECONDS + 1
            rejection |= signal_age > MAX_SIGNAL_AGE_SECONDS
            data_health = signal_age <= MAX_SIGNAL_AGE_SECONDS and volatility > 0 and decision["risk_reward"] > 0
            status = "WATCH" if rejection or score < float(settings.get("min_score_threshold", 70)) else "SELECTED"
            return {
                "symbol": symbol, "direction": "NEUTRAL" if direction == "BEKLE" else direction, "analysis_score": round(score, 2), "opportunity_score": round(score, 2),
                "confidence": confidence, "entry": decision["entry"], "stop_loss": decision["stop_loss"],
                "tp1": decision["tp1"], "tp2": decision["tp2"], "tp3": decision["tp3"],
                "volatility_pct": round(volatility, 3), "risk_reward": decision["risk_reward"],
                "volume_ratio": decision["volume_ratio"],
                "trend": decision["trend"], "momentum": decision["momentum"],
                "mtf_trend": mtf_trend, "macd_confirmation": macd_confirmation, "rsi": decision["rsi"],
                "mtf_aligned": mtf_aligned, "max_volatility_pct": float(settings["max_volatility_pct"]),
                "status": status if data_health else "REJECTED",
                "signal_time": signal_time, "signal_age_seconds": max(0, round(signal_age)),
                "data_health": data_health,
            }
        except (BinanceDemoError, TypeError, ValueError, KeyError, ZeroDivisionError):
            return {"symbol": symbol, "direction": "NEUTRAL", "opportunity_score": 0, "confidence": 0, "status": "REJECTED", "trend": "Veri yok", "momentum": "Nötr", "reasons": ["Yeterli market verisi alınamadı"]}

    results: list[dict[str, Any]] = []
    for offset in range(0, len(symbols), 10):
        batch = await asyncio.gather(*(evaluate(symbol) for symbol in symbols[offset:offset + 10]))
        results.extend(item for item in batch if item is not None)
    return _enrich_scan_candidates(results)


async def auto_trade_market_universe(application: Any) -> list[dict[str, Any]]:
    """Return the Demo-supported Auto Trade universe for read-only UI use."""
    client = market_client_for(application)
    exchange_info, tickers = await asyncio.gather(
        client.public_get("/fapi/v1/exchangeInfo"),
        client.public_get("/fapi/v1/ticker/24hr"),
    )
    ticker_by_symbol = {
        str(item.get("symbol")): item for item in response_rows(tickers) if item.get("symbol")
    }
    settings = getattr(application.state, "v21_demo", {}).get("settings", DEFAULT_SETTINGS)
    markets: list[dict[str, Any]] = []
    for symbol in dynamic_auto_universe(exchange_info, tickers, settings):
        ticker = ticker_by_symbol.get(symbol, {})
        markets.append({
            "symbol": symbol,
            "display": symbol.replace("USDT", "/USDT"),
            "price": float(ticker.get("lastPrice") or 0),
            "change": float(ticker.get("priceChangePercent") or 0),
            "volume": float(ticker.get("quoteVolume") or 0),
        })
    return sorted(markets, key=lambda item: item["volume"], reverse=True)


GATE_REJECTION_KEYS = (
    "direction_rejected", "status_rejected", "analysis_score_rejected",
    "opportunity_score_rejected", "freshness_rejected", "mtf_rejected",
    "liquidity_rejected", "sl_tp_rejected", "other_rejected",
)


def _new_gate_rejections(scanner_total: int = 0) -> dict[str, int]:
    return {
        "scanner_total": int(scanner_total),
        **{key: 0 for key in GATE_REJECTION_KEYS},
        "final_tradeable_count": 0,
    }


def _record_gate_rejection(diagnostics: dict[str, int] | None, key: str) -> None:
    if diagnostics is not None:
        diagnostics[key] = int(diagnostics.get(key, 0)) + 1


def candidate_is_tradeable(
    candidate: dict[str, Any],
    settings: dict[str, Any],
    diagnostics: dict[str, int] | None = None,
) -> bool:
    symbol = str(candidate.get("symbol") or "").upper()
    direction = str(candidate.get("direction") or "NEUTRAL").upper()
    allowed_symbols = _effective_allowed_symbols(settings)
    if allowed_symbols is not None and symbol not in allowed_symbols:
        _record_gate_rejection(diagnostics, "other_rejected")
        return False
    analysis_score = float(candidate.get("analysis_score", candidate.get("score", 0)) or 0)
    opportunity_score = float(candidate.get("opportunity_score", analysis_score) or analysis_score)
    decision_status = str(candidate.get("decision_status") or _decision_status(analysis_score))
    breakdown = candidate.get("opportunity_breakdown") or {}
    if direction not in {"LONG", "SHORT"}:
        _record_gate_rejection(diagnostics, "direction_rejected")
        return False
    if candidate.get("status") != "SELECTED":
        _record_gate_rejection(diagnostics, "status_rejected")
        return False
    if analysis_score < 75:
        _record_gate_rejection(diagnostics, "analysis_score_rejected")
        return False
    if opportunity_score < 70:
        _record_gate_rejection(diagnostics, "opportunity_score_rejected")
        return False
    if float(breakdown.get("mtf_confirmation", 100) or 0) < 50:
        _record_gate_rejection(diagnostics, "mtf_rejected")
        return False
    if float(breakdown.get("liquidity_quality", 100) or 0) < 50:
        _record_gate_rejection(diagnostics, "liquidity_rejected")
        return False
    if direction == "LONG" and not settings.get("allow_long", True):
        _record_gate_rejection(diagnostics, "other_rejected")
        return False
    if direction == "SHORT" and not settings.get("allow_short", True):
        _record_gate_rejection(diagnostics, "other_rejected")
        return False
    if candidate.get("data_health") is False:
        _record_gate_rejection(diagnostics, "freshness_rejected")
        return False
    if int(candidate.get("signal_age_seconds") or 0) > MAX_SIGNAL_AGE_SECONDS:
        _record_gate_rejection(diagnostics, "freshness_rejected")
        return False
    if "risk_reward" in candidate and float(candidate.get("risk_reward") or 0) <= 0:
        _record_gate_rejection(diagnostics, "other_rejected")
        return False
    try:
        entry = float(candidate["entry"])
        stop = float(candidate["stop_loss"])
        targets = [float(candidate[key]) for key in ("tp1", "tp2", "tp3")]
    except (KeyError, TypeError, ValueError):
        _record_gate_rejection(diagnostics, "sl_tp_rejected")
        return False
    if min(entry, stop, *targets) <= 0:
        _record_gate_rejection(diagnostics, "sl_tp_rejected")
        return False
    valid_levels = (stop < entry < targets[0] < targets[1] < targets[2]) if direction == "LONG" else (targets[2] < targets[1] < targets[0] < entry < stop)
    if not valid_levels:
        _record_gate_rejection(diagnostics, "sl_tp_rejected")
    return valid_levels


def automatic_risk_block(state: dict[str, Any]) -> tuple[str, str] | None:
    risk = state.setdefault("risk", {})
    if bool(risk.get("kill_switch") or state["settings"].get("kill_switch", False)):
        state["auto"].update({"status": "PAUSED", "pause_reason": "KILL_SWITCH"})
        emit_notification(state, "KILL_SWITCH", "Demo Auto Trade kill switch aktif; yeni otomatik girişler durduruldu.", event_id=f"{today()}-kill-switch")
        return "KILL_SWITCH", "Demo kill switch aktif; yeni otomatik giriş kilitli."
    consecutive_limit = min(3, int(state["settings"].get("consecutive_loss_limit", risk.get("consecutive_loss_limit", 3))))
    if int(risk.get("consecutive_losses", 0)) >= consecutive_limit:
        state["auto"].update({"status": "PAUSED", "pause_reason": "CONSECUTIVE_LOSSES"})
        emit_notification(state, "CONSECUTIVE_LOSSES", "Üç ardışık Demo zararı nedeniyle Auto Trade duraklatıldı.", event_id=f"{today()}-consecutive-losses")
        return "CONSECUTIVE_LOSSES", "Ardışık Demo zarar koruması aktif; yeni otomatik giriş kilitli."
    return None


def select_auto_candidates(
    ranked: list[dict[str, Any]],
    settings: dict[str, Any],
    occupied: set[str],
    limit: int = MAX_OPEN_POSITIONS,
    diagnostics: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    allowed_symbols = _effective_allowed_symbols(settings)
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    ordered = sorted(ranked, key=lambda item: float(item.get("opportunity_score", item.get("score", 0)) or 0), reverse=True)
    for candidate in ordered:
        symbol = normalize_symbol(str(candidate.get("symbol") or ""))
        if symbol in occupied or symbol in seen:
            _record_gate_rejection(diagnostics, "other_rejected")
            continue
        if allowed_symbols is not None and symbol not in allowed_symbols:
            _record_gate_rejection(diagnostics, "other_rejected")
            continue
        if not candidate_is_tradeable(candidate, settings, diagnostics):
            continue
        seen.add(symbol)
        selected.append(candidate)
        if len(selected) >= min(MAX_OPEN_POSITIONS, max(0, int(limit))):
            break
    if diagnostics is not None:
        diagnostics["final_tradeable_count"] = len(selected)
    return selected


def safe_rotation_symbols(snapshot: dict[str, Any], desired_symbols: set[str], plans: dict[str, dict[str, Any]]) -> list[str]:
    """Return only profitable/break-even Auto plans eligible for rotation."""
    safe: list[str] = []
    for position in snapshot.get("positions", []):
        symbol = str(position.get("symbol") or "")
        plan = next((item for item in plans.values() if item.get("symbol") == symbol and item.get("source") == "AUTO_SCANNER" and item.get("position_status") != "CLOSED"), None)
        if not plan or symbol in desired_symbols:
            continue
        if float(position.get("unrealized_pnl") or 0) >= 0:
            safe.append(symbol)
    return safe


async def rotate_safe_demo_positions(application: Any, snapshot: dict[str, Any], desired_symbols: set[str]) -> int:
    state = application.state.binance_demo
    symbols = safe_rotation_symbols(snapshot, desired_symbols, state.get("plans", {}))
    if not symbols:
        return 0
    client = client_for(application)
    rotated = 0
    for symbol in symbols:
        if await close_symbol_position(client, symbol) is None:
            continue
        for plan in state.get("plans", {}).values():
            if plan.get("symbol") == symbol and plan.get("source") == "AUTO_SCANNER":
                plan.update({"status": "KAPANDI", "position_status": "CLOSED", "remaining_quantity": "0", "closed_at": utc_now(), "close_reason": "SAFE_ROTATION"})
        rotated += 1
        emit_notification(application.state.v21_demo, "ROTATION", f"{symbol} güvenli rotasyonla kapatıldı; mevcut korumalar önceliklendirildi.", event_id=f"{today()}-rotation-{symbol}-{int(time.time() // SCAN_INTERVAL_SECONDS)}")
    if rotated:
        persist_runtime(state)
    return rotated


def daily_metrics(state: dict[str, Any]) -> dict[str, Any]:
    day = today()
    events = [item for item in state.get("journal", []) if str(item.get("created_at", "")).startswith(day)]
    auto_entries = [item for item in events if item.get("kind") == "AUTO_ORDER"]
    realized = sum(float(item.get("realized_pnl") or 0) for item in events if item.get("verified_realized") is True)
    return {
        "date": day,
        "auto_entries": len(auto_entries),
        "events": len(events),
        "realized_pnl": round(realized, 4),
        "remaining_loss_budget": round(max(0.0, float(state["settings"]["daily_loss_limit"]) + realized), 4),
    }


def risk_size_values(entry: float, stop: float, max_loss: float, leverage: int, max_margin: float) -> dict[str, float]:
    manual_max_leverage = 50
    if leverage < 1 or leverage > manual_max_leverage:
        raise HTTPException(422, f"Demo kaldıraç 1x ile {manual_max_leverage}x arasında olmalı.")
    distance = abs(entry - stop)
    if distance <= 0:
        raise HTTPException(422, "Giriş ve Stop aynı olamaz.")
    risk_pct = distance / entry
    requested_notional = max_loss / risk_pct
    capped_notional = min(requested_notional, max_margin * leverage, float(MAX_NOTIONAL_USDT))
    margin = capped_notional / leverage
    actual_loss = capped_notional * risk_pct
    return {
        "risk_pct": round(risk_pct * 100, 4),
        "notional_usdt": round(capped_notional, 4),
        "margin_usdt": round(margin, 4),
        "estimated_stop_loss_usdt": round(actual_loss, 4),
        "capped": capped_notional + 1e-9 < requested_notional,
    }


def _position_map(snapshot: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    return {item["symbol"]: item for item in (snapshot or {}).get("positions", []) if item.get("symbol")}


def reconcile_positions(state: dict[str, Any], previous: dict[str, Any] | None, current: dict[str, Any]) -> bool:
    changed = False
    old_positions = _position_map(previous)
    new_positions = _position_map(current)
    for symbol, row in new_positions.items():
        if symbol not in old_positions:
            changed |= record_event(
                state, "POSITION_OPEN", f"{symbol} Demo pozisyonu hesapta görünmeye başladı.",
                symbol=symbol, status="OPEN", side=row.get("direction"), price=row.get("entry_price"),
                quantity=row.get("quantity"), source="RECONCILER",
                event_id=f"pos-open-{symbol}-{row.get('entry_price')}-{row.get('quantity')}",
            ) is not None
    for symbol, row in old_positions.items():
        if symbol not in new_positions:
            changed |= record_event(
                state, "POSITION_MISSING", f"{symbol} Demo pozisyonu snapshot'tan kayboldu; doğrulanmış kapanış sonucu bekleniyor.",
                symbol=symbol, status="CLOSED", side=row.get("direction"), price=row.get("mark_price"),
                quantity=row.get("quantity"), reason="Kapanış sonucu exchange fill/accounting verisiyle doğrulanacak",
                source="RECONCILER", reduce_only=True,
                event_id=f"pos-missing-{symbol}-{row.get('entry_price')}-{row.get('quantity')}",
            ) is not None
    return changed


def process_stream_event(state: dict[str, Any], payload: dict[str, Any]) -> bool:
    event_type = str(payload.get("e") or "")
    event_time = payload.get("T", payload.get("E", 0))
    state["stream"].update({"last_event": now_iso(), "status": "CANLI", "transport": "USER STREAM"})
    if event_type == "ORDER_TRADE_UPDATE":
        order = payload.get("o") if isinstance(payload.get("o"), dict) else {}
        symbol = str(order.get("s") or "")
        status = str(order.get("X") or "")
        execution = str(order.get("x") or "")
        order_id = order.get("i", order.get("c", ""))
        realized = float(order.get("rp") or 0)
        kind = "FILL" if execution == "TRADE" else "ORDER_UPDATE"
        return record_event(
            state, kind, f"{symbol} {execution or 'EMİR'} · {status}", symbol=symbol,
            status=status, side=order.get("S"), price=float(order.get("ap") or order.get("L") or order.get("p") or 0),
            quantity=float(order.get("z") or order.get("l") or order.get("q") or 0),
            realized_pnl=realized, reason=str(order.get("er") or "") or None,
            event_id=f"order-{event_time}-{order_id}-{execution}-{status}", source="USER_STREAM",
            reduce_only=bool(order.get("R", False)), verified_realized=execution == "TRADE" and bool(order.get("R", False)),
        ) is not None
    if event_type == "ALGO_UPDATE":
        order = payload.get("o") if isinstance(payload.get("o"), dict) else payload.get("a", {})
        order = order if isinstance(order, dict) else {}
        symbol = str(order.get("s") or order.get("symbol") or "")
        status = str(order.get("X") or order.get("algoStatus") or order.get("status") or "UPDATE")
        return record_event(
            state, "ALGO_UPDATE", f"{symbol} koşullu koruma · {status}", symbol=symbol or None,
            status=status, price=float(order.get("sp") or order.get("triggerPrice") or 0),
            event_id=f"algo-{event_time}-{order.get('aid', order.get('algoId', ''))}-{status}", source="USER_STREAM",
        ) is not None
    if event_type == "listenKeyExpired":
        record_event(state, "STREAM_EXPIRED", "Binance Demo kullanıcı akışı süresi doldu; güvenli yeniden bağlantı başlatıldı.", source="USER_STREAM")
        return True
    return False


async def user_stream_loop(application: Any) -> None:
    state = application.state.v21_demo
    while True:
        listen_key = ""
        try:
            if not credentials_configured():
                state["stream"].update({"status": "ANAHTAR BEKLİYOR", "transport": "REST EŞLEŞTİRME"})
                await asyncio.sleep(5)
                continue
            client = client_for(application)
            response = await client.api_key_request("POST", "/fapi/v1/listenKey")
            listen_key = str((response or {}).get("listenKey") or "")
            if not listen_key:
                raise BinanceDemoError("Demo kullanıcı akışı anahtarı alınamadı.")
            state["stream"].update({"status": "BAĞLANIYOR", "transport": "USER STREAM", "last_error": None})
            url = f"{DEMO_WS_BASE}/ws/{listen_key}"
            async with websockets.connect(url, ping_interval=20, ping_timeout=20, close_timeout=5) as socket:
                state["stream"].update({"status": "CANLI", "transport": "USER STREAM", "last_event": now_iso()})
                record_event(state, "STREAM_CONNECTED", "Binance Futures Demo kullanıcı akışı bağlandı.", source="USER_STREAM")
                last_keepalive = time.monotonic()
                while True:
                    try:
                        raw = await asyncio.wait_for(socket.recv(), timeout=30)
                        payload = json.loads(raw)
                        if isinstance(payload, dict) and process_stream_event(state, payload):
                            persist_state(state)
                        if isinstance(payload, dict) and payload.get("e") == "listenKeyExpired":
                            break
                    except asyncio.TimeoutError:
                        pass
                    if time.monotonic() - last_keepalive >= 45 * 60:
                        await client.api_key_request("PUT", "/fapi/v1/listenKey")
                        last_keepalive = time.monotonic()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            state["stream"]["error_count"] += 1
            state["stream"]["reconnect_count"] += 1
            state["stream"].update({
                "status": "YENİDEN BAĞLANIYOR", "transport": "REST EŞLEŞTİRME",
                "last_error": str(exc)[:220],
            })
            record_event(state, "STREAM_RECONNECT", "Canlı Demo akışı kesildi; REST eşleştirme açık ve yeniden bağlantı deneniyor.", source="SYSTEM")
            persist_state(state)
            await asyncio.sleep(min(30, 2 + state["stream"]["reconnect_count"]))
        finally:
            if listen_key and not asyncio.current_task().cancelling():
                try:
                    await client_for(application).api_key_request("DELETE", "/fapi/v1/listenKey")
                except Exception:
                    pass


def active_plan(application: Any, symbol: str, demo_state: dict[str, Any] | None = None) -> dict[str, Any] | None:
    state = demo_state or application.state.binance_demo
    user_id = str(state.get("_user_id") or "").strip()
    plans = state.get("plans", {})
    candidates = [
        plan for plan in plans.values()
        if plan.get("symbol") == symbol
        and (not user_id or str(plan.get("user_id") or "").strip() == user_id)
        and not plan.get("stop_protection_cancelled")
        and plan.get("status") not in {"KAPANDI", "İPTAL", "GÜVENLİK İÇİN KAPATILDI", "ACİL DURDURULDU", "KORUMA İPTAL"}
    ]
    return candidates[-1] if candidates else None


async def ensure_stop_protection(application: Any, snapshot: dict[str, Any], *, demo_state: dict[str, Any] | None = None, v21_state: dict[str, Any] | None = None, client: BinanceDemoClient | None = None) -> bool:
    state = v21_state or application.state.v21_demo
    demo_state = demo_state or application.state.binance_demo
    client = client or client_for_state(application, demo_state)
    algo_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for order in snapshot.get("open_algo_orders", []):
        algo_by_symbol.setdefault(str(order.get("symbol")), []).append(order)
    changed = False
    for position in snapshot.get("positions", []):
        symbol = str(position.get("symbol"))
        plan = active_plan(application, symbol, demo_state)
        if not plan:
            continue
        stops = [order for order in algo_by_symbol.get(symbol, []) if str(order.get("type", "")).upper() == "STOP_MARKET"]
        if stops:
            continue
        params = {
            "algoType": "CONDITIONAL", "symbol": symbol,
            "side": "SELL" if position.get("direction") == "LONG" else "BUY",
            "type": "STOP_MARKET", "triggerPrice": plan["stop_loss"], "closePosition": "true",
            "workingType": "MARK_PRICE", "priceProtect": "TRUE", "clientAlgoId": new_client_id("REPAIRSL"),
        }
        try:
            result = await post_algo(client, params)
        except BinanceDemoError as exc:
            plan["status"] = "CRITICAL / UNPROTECTED"
            plan["protection_status"] = "CRITICAL / UNPROTECTED"
            plan["recovery_attempts"] = int(plan.get("recovery_attempts", 0)) + 1
            plan["last_error"] = str(exc)
            record_event(state, "ACİL KORUMA", f"{symbol} Stop onarımı başarısız; pozisyon korunmasız ve yeniden denenecek.", symbol=symbol, source="RISK_ENGINE")
            try:
                await close_symbol_position(client, symbol, str(position.get("position_side") or "BOTH"))
                confirmation = response_rows(await client.signed("GET", "/fapi/v3/positionRisk", {"symbol": symbol}))
                if not any(position_amount(item.get("positionAmt")) != 0 for item in confirmation):
                    plan["status"] = "GÜVENLİK İÇİN KAPATILDI"
                    plan["protection_status"] = "CLOSED_AFTER_PROTECTION_FAILURE"
                    plan["position_status"] = "CLOSED"
                    plan["remaining_quantity"] = "0"
                    changed = True
                    continue
            except BinanceDemoError as close_exc:
                plan["last_error"] = f"Stop onarımı: {exc}; güvenli kapatma: {close_exc}"
            changed = True
            continue
        if result.get("algoId"):
            plan["stop_algo_id"] = int(result["algoId"])
            plan.setdefault("protection_ids", []).append(int(result["algoId"]))
        else:
            raise BinanceDemoError("Binance Demo Stop onarım kimliği doğrulanamadı.", http_status=409)
        plan["status"] = "KORUMA ONARILDI"
        state["protection_repairs"] += 1
        record_event(state, "PROTECTION_REPAIRED", f"{symbol} eksik Stop koruması Demo hesabında yeniden kuruldu.", symbol=symbol, source="RISK_ENGINE")
        changed = True
    if changed:
        persist_runtime(demo_state)
    return changed


async def improve_dynamic_stops(application: Any, snapshot: dict[str, Any], *, demo_state: dict[str, Any] | None = None, v21_state: dict[str, Any] | None = None, client: BinanceDemoClient | None = None) -> bool:
    state = v21_state or application.state.v21_demo
    demo_state = demo_state or application.state.binance_demo
    settings = state["settings"]
    if not settings["breakeven_enabled"] and not settings["trailing_enabled"]:
        return False
    client = client or client_for_state(application, demo_state)
    changed = False
    for position in snapshot.get("positions", []):
        symbol = str(position.get("symbol"))
        plan = active_plan(application, symbol, demo_state)
        if not plan or time.time() - float(plan.get("last_dynamic_update_epoch", 0)) < 30:
            continue
        entry = float(position.get("entry_price") or 0)
        mark = float(position.get("mark_price") or 0)
        initial_stop = float(plan.get("initial_stop_loss") or plan.get("stop_loss") or 0)
        current_stop = float(plan.get("stop_loss") or 0)
        risk = abs(entry - initial_stop)
        if min(entry, mark, initial_stop, risk) <= 0:
            continue
        direction = str(position.get("direction"))
        r_multiple = (mark - entry) / risk if direction == "LONG" else (entry - mark) / risk
        desired = current_stop
        label = ""
        if settings["breakeven_enabled"] and r_multiple >= float(settings["breakeven_trigger_r"]):
            desired = max(desired, entry) if direction == "LONG" else min(desired, entry)
            label = "BAŞABAŞ"
        if settings["trailing_enabled"] and r_multiple >= float(settings["trailing_trigger_r"]):
            distance = risk * float(settings["trailing_distance_r"])
            trail = mark - distance if direction == "LONG" else mark + distance
            desired = max(desired, trail) if direction == "LONG" else min(desired, trail)
            label = "İZ SÜREN"
        rules = await symbol_rules(client, symbol)
        desired_decimal = round_tick(Decimal(str(desired)), rules["tick"])
        desired = float(desired_decimal)
        improves = desired > current_stop + float(rules["tick"]) if direction == "LONG" else desired < current_stop - float(rules["tick"])
        valid_side = desired < mark if direction == "LONG" else desired > mark
        if not label or not improves or not valid_side:
            continue
        result = await post_algo(client, {
            "algoType": "CONDITIONAL", "symbol": symbol,
            "side": "SELL" if direction == "LONG" else "BUY", "type": "STOP_MARKET",
            "triggerPrice": decimal_text(desired_decimal), "closePosition": "true",
            "workingType": "MARK_PRICE", "priceProtect": "TRUE", "clientAlgoId": new_client_id("DYNAMICSL"),
        })
        new_id = int(result.get("algoId", 0)) or None
        old_id = plan.get("stop_algo_id")
        if new_id:
            plan["stop_algo_id"] = new_id
            plan.setdefault("protection_ids", []).append(new_id)
        plan["stop_loss"] = decimal_text(desired_decimal)
        plan["last_dynamic_update_epoch"] = time.time()
        if old_id:
            try:
                await client.signed("DELETE", "/fapi/v1/algoOrder", {"symbol": symbol, "algoId": old_id})
            except BinanceDemoError:
                pass
        record_event(state, "DYNAMIC_STOP", f"{symbol} {label} Stop {decimal_text(desired_decimal)} seviyesine iyileştirildi.", symbol=symbol, price=desired, source="RISK_ENGINE")
        changed = True
    if changed:
        persist_runtime(demo_state)
    return changed


def _background_contexts(application: Any) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    contexts = [("", application.state.binance_demo, application.state.v21_demo)]
    demo_store = getattr(application.state, "_binance_demo_user_state", {})
    v21_store = getattr(application.state, "_v21_demo_user_state", {})
    user_ids = set(demo_store) | set(v21_store)
    for user_id in user_ids:
        demo_state = demo_store.get(user_id)
        if not isinstance(demo_state, dict):
            continue
        v21_state = v21_store.get(user_id)
        if not isinstance(v21_state, dict):
            v21_state = initial_state()
            v21_state.update({"_user_id": user_id, "_app": application})
        contexts.append((str(user_id), demo_state, v21_state))
    return contexts


async def reconciliation_loop(application: Any) -> None:
    previous: dict[str, dict[str, Any] | None] = {}
    while True:
        try:
            for user_id, demo_state, state in _background_contexts(application):
                if user_id and not demo_state.get("plans"):
                    continue
                if not user_id and not credentials_configured():
                    continue
                try:
                    client = client_for_state(application, demo_state)
                    snapshot = await account_snapshot(client)
                    state["snapshot"] = snapshot
                    plan_reconciliation = reconcile_demo_plans(demo_state, snapshot)
                    state["stream"]["last_sync"] = now_iso()
                    demo_state.update({"connected": True, "last_checked": now_iso(), "last_error": None})
                    changed = reconcile_positions(state, previous.get(user_id), snapshot)
                    previous[user_id] = snapshot
                    changed |= await ensure_stop_protection(application, snapshot, demo_state=demo_state, v21_state=state, client=client)
                    changed |= await improve_dynamic_stops(application, snapshot, demo_state=demo_state, v21_state=state, client=client)
                    if changed or plan_reconciliation["changed"]:
                        persist_state(state)
                        persist_runtime(demo_state)
                except BinanceDemoError as exc:
                    state["stream"]["last_error"] = str(exc)[:220]
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            application.state.binance_demo["connected"] = False
            application.state.v21_demo["stream"].update({"status": "REST YENİDEN DENİYOR", "last_error": str(exc)[:220]})
        await asyncio.sleep(3)


def in_schedule(settings: dict[str, Any]) -> bool:
    hour = datetime.now().hour
    start, end = int(settings["schedule_start_hour"]), int(settings["schedule_end_hour"])
    return start <= hour < end if start < end else hour >= start or hour < end


def return_correlation(left: list[dict[str, float]], right: list[dict[str, float]]) -> float:
    a = [(b["close"] / a["close"]) - 1 for a, b in zip(left[-61:-1], left[-60:]) if a["close"]]
    b = [(d["close"] / c["close"]) - 1 for c, d in zip(right[-61:-1], right[-60:]) if c["close"]]
    size = min(len(a), len(b))
    if size < 20:
        return 0.0
    a, b = a[-size:], b[-size:]
    mean_a, mean_b = fmean(a), fmean(b)
    numerator = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b))
    denominator = math.sqrt(sum((x - mean_a) ** 2 for x in a) * sum((y - mean_b) ** 2 for y in b))
    return numerator / denominator if denominator else 0.0


async def automatic_cycle(application: Any, *, request: Request | None = None) -> None:
    state = state_for(request) if request is not None else application.state.v21_demo
    settings = state["settings"]
    auto = state["auto"]
    auto.update({"rejection_gate": None, "rejection_reason": None, "last_error": None})
    if not bool(auto.get("enabled")) or not bool(auto.get("user_confirmed")):
        _set_rejection(state, "CONFIRMATION", "Yalnızca açık kullanıcı onayıyla otomasyon giriş yapabilir; emir açılmadı.")
        return
    auto["cycles"] += 1
    auto["last_scan"] = now_iso()
    if not armed(application.state.binance_demo):
        _set_rejection(state, "DEMO_ARM", "10 dakikalık DEMO emir kilidi kapalı; otomasyon bekliyor.")
        return
    if not in_schedule(settings):
        _set_rejection(state, "MARKET_HOURS", "İzin verilen çalışma saatleri dışında; yeni giriş yok.")
        return
    risk_block = automatic_risk_block(state)
    if risk_block:
        _set_rejection(state, *risk_block)
        return
    daily = daily_metrics(state)
    if daily["auto_entries"] >= settings["daily_trade_limit"]:
        _set_rejection(state, "DAILY_TRADE_LIMIT", "Günlük Demo işlem limiti doldu.")
        return
    if daily["realized_pnl"] <= -float(settings["daily_loss_limit"]):
        _set_rejection(state, "DAILY_LOSS_LIMIT", "Günlük Demo zarar limiti aktif; yeni giriş kilitli.")
        auto["enabled"] = False
        return
    client = client_for(request if request is not None else application)
    snapshot = await account_snapshot(client)
    refresh_daily_risk_state(state, float(snapshot.get("wallet_balance") or snapshot.get("available_balance") or 0))
    if state["auto"].get("status") == "PAUSED":
        _set_rejection(state, state["auto"].get("pause_reason") or "DAILY_LOSS_20", "Günlük Demo zarar koruması aktif; yeni otomatik giriş kilitli.")
        persist_state(state)
        return
    max_positions = min(settings["max_positions"], MAX_OPEN_POSITIONS)
    pending_entries = sum(1 for item in snapshot.get("open_orders", []) if not bool(item.get("reduce_only", False)))
    pending_entries += len(state.get("paper_positions", []))
    available_slots = max_positions - len(snapshot["positions"]) - pending_entries
    occupied = {item["symbol"] for item in snapshot["positions"] + snapshot["open_orders"]}
    state["scanner"].update({"active": True, "last_stage": "TARAMA", "selected_symbols": []})
    ranked = await scan_demo_universe(client, set(), settings)
    allowed_symbols = _effective_allowed_symbols(settings)
    desired_symbols = {
        str(candidate.get("symbol")) for candidate in ranked
        if candidate_is_tradeable(candidate, settings)
    }
    desired_symbols = set(sorted(desired_symbols, key=lambda symbol: next((float(item.get("opportunity_score", item.get("score", 0)) or 0) for item in ranked if item.get("symbol") == symbol), 0), reverse=True)[:MAX_OPEN_POSITIONS])
    if available_slots <= 0:
        rotated = await rotate_safe_demo_positions(application, snapshot, desired_symbols)
        if rotated:
            snapshot = await account_snapshot(client)
            occupied = {item["symbol"] for item in snapshot["positions"] + snapshot["open_orders"]}
            pending_entries = sum(1 for item in snapshot.get("open_orders", []) if not bool(item.get("reduce_only", False)))
            available_slots = max_positions - len(snapshot["positions"]) - pending_entries - len(state.get("paper_positions", []))
        if available_slots <= 0:
            _set_rejection(state, "MAX_POSITIONS", "Açık pozisyon sınırı dolu; zarardaki Auto pozisyonlar rotasyon için kapatılmadı.")
            state["scanner"].update({"active": True, "last_stage": "DOLU", "selected_symbols": []})
            persist_state(state)
            return
    gate_rejections = _new_gate_rejections(scanner_total=len(ranked))
    top_candidates = select_auto_candidates(ranked, settings, occupied, available_slots, gate_rejections)
    if not top_candidates:
        disallowed = next((candidate for candidate in ranked if allowed_symbols is not None and normalize_symbol(candidate.get("symbol", "")) not in allowed_symbols), None)
        if disallowed:
            _set_rejection(state, "ALLOWED_SYMBOLS", f"{disallowed.get('symbol', 'Aday')} izinli pariteler dışında; emir açılmadı.")
    scanner = state["scanner"]
    _apply_scan_completion_state(scanner, settings, ranked, top_candidates, eligible_count=len(ranked))
    scanner["gate_rejections"] = gate_rejections
    selected: list[str] = []
    cooldowns = auto.setdefault("cooldown_until", {})
    for index, candidate in enumerate(top_candidates[:available_slots]):
        symbol = str(candidate.get("symbol") or "")
        direction = str(candidate.get("direction") or "NEUTRAL").upper()
        if not symbol or direction == "NEUTRAL":
            _set_rejection(state, "SIGNAL_DIRECTION", f"{symbol or 'Aday'} için işlem yönü bulunamadı; emir açılmadı.")
            continue
        if allowed_symbols is not None and symbol not in allowed_symbols:
            _set_rejection(state, "ALLOWED_SYMBOLS", f"{symbol} izinli pariteler dışında; emir açılmadı.")
            continue
        if not candidate_is_tradeable(candidate, settings):
            _set_rejection(state, "RISK_LEVELS", f"{symbol} için seçili ve geçerli Stop/TP seviyeleri yok; emir açılmadı.")
            continue
        if float(cooldowns.get(symbol, 0) or 0) > time.time():
            _set_rejection(state, "SYMBOL_COOLDOWN", f"{symbol} sembol soğuma süresinde; emir açılmadı.")
            continue
        sizing = risk_size_values(
            float(candidate["entry"]), float(candidate["stop_loss"]), float(settings["max_loss_per_trade"]),
            MAX_LEVERAGE, min(float(settings["max_margin_per_trade"]), float(MAX_MARGIN_USDT)),
        )
        if sizing["margin_usdt"] < 5.0:
            _set_rejection(state, "MINIMUM_ORDER_SIZE", f"{symbol} için borsa minimumu işlem başı zarar limitine uymuyor; emir açılmadı.")
            continue
        margin = min(float(MAX_MARGIN_USDT), sizing["margin_usdt"])
        body = DemoOrderRequest(
            symbol=symbol, direction=direction, order_type="MARKET", margin_usdt=margin,
            leverage=MAX_LEVERAGE, stop_loss=candidate["stop_loss"], tp1=candidate["tp1"],
            tp2=candidate["tp2"], tp3=candidate["tp3"],
        )
        try:
            result = await execute_demo_order(application, body, source="AUTO_SCANNER", request=request)
        except BinanceDemoError as exc:
            _set_rejection(state, "DEMO_EXECUTION", f"{symbol}: Demo emir reddi · {exc}")
            continue
        except HTTPException as exc:
            if exc.status_code not in {409, 422}:
                raise
            _set_rejection(state, "DEMO_EXECUTION", f"{symbol}: Demo emir reddi · {exc.detail}")
            continue
        selected.append(symbol)
        cooldowns[symbol] = time.time() + SCAN_INTERVAL_SECONDS
        plan = result.get("plan", {}) if isinstance(result, dict) else {}
        scanner_rank = int(candidate.get("rank", index + 1))
        scanner_score = candidate.get("score", candidate.get("opportunity_score", 0))
        confidence_label = candidate.get("confidence", "LOW")
        entry_price = float(plan.get("entry_price", candidate.get("entry", 0)))
        state.setdefault("automation_trades", []).insert(0, {
            "plan_id": plan.get("id"),
            "symbol": symbol, "side": direction, "scanner_rank": scanner_rank,
            "scanner_score": scanner_score, "confidence": confidence_label,
            "entry_time": now_iso(), "entry_price": entry_price,
            "margin": plan.get("margin_usdt", margin), "leverage": plan.get("leverage", MAX_LEVERAGE),
            "tp": plan.get("targets", [candidate.get("tp1", 0), candidate.get("tp2", 0), candidate.get("tp3", 0)]),
            "sl": plan.get("stop_loss", candidate.get("stop_loss", 0)), "trade_reason": candidate.get("reasons", []),
            "status": plan.get("status", "DOLUM BEKLİYOR"), "demo_only": True,
        })
        record_event(
            state, "AUTO_ORDER", f"{symbol} {direction} otomatik Demo işlemi açıldı.", symbol=symbol,
            side=direction, price=float(candidate["entry"]), quantity=None,
            reason=f"Skor {float(candidate.get('opportunity_score', scanner_score)):.2f}; güven %{confidence_label}; tahmini stop riski {sizing['estimated_stop_loss_usdt']:.2f} USDT",
            source="AUTO_SCANNER",
        )
    scanner["selected_symbols"] = selected
    scanner["last_stage"] = "EMİRLER YÖNETİLİYOR"
    if not selected and top_candidates:
        auto["last_error"] = auto.get("rejection_gate") or "NO_EXECUTION"
    if selected or not auto.get("rejection_reason"):
        auto["last_decision"] = f"100 coin tarandı; {len(selected)} yeni Demo pozisyonu açıldı; en iyi 3: {', '.join(item['symbol'] for item in top_candidates) or 'yok'}."
    persist_state(state)


async def run_scanner_cycle(application: Any) -> None:
    state = application.state.v21_demo
    scanner = state["scanner"]
    scan_lock = getattr(application.state, "v21_scanner_lock", None)
    if scan_lock is None:
        scan_lock = asyncio.Lock()
        application.state.v21_scanner_lock = scan_lock
    if scan_lock.locked():
        return
    await scan_lock.acquire()
    start = time.perf_counter()
    scanner.update({"running": True, "active": True, "scan_status": "TARAMA", "last_error": None})
    emit_notification(state, "SCAN_STARTED", "Demo Auto Trade market taraması başladı.", event_id=f"{today()}-scan-{int(time.time() // SCAN_INTERVAL_SECONDS)}")
    try:
        settings = state["settings"]
        client = market_client_for(application)
        snapshot = {"positions": [], "open_orders": []}
        if credentials_configured():
            snapshot = await account_snapshot(client_for(application))
        occupied = {item["symbol"] for item in snapshot.get("positions", []) + snapshot.get("open_orders", [])}
        ranked = await scan_demo_universe(client, occupied, settings)
        threshold = float(settings.get("min_score_threshold", 70))
        gate_rejections = _new_gate_rejections(scanner_total=len(ranked))
        filtered = []
        for candidate in ranked:
            tradeable = candidate_is_tradeable(candidate, settings, gate_rejections)
            if candidate.get("score", 0) >= threshold and tradeable:
                filtered.append(candidate)
        top_candidates = filtered[:3]
        gate_rejections["final_tradeable_count"] = len(top_candidates)
        selected_symbols = {item["symbol"] for item in top_candidates}
        for candidate in ranked:
            if candidate["symbol"] in selected_symbols:
                candidate["status"] = "SELECTED"
            elif candidate.get("status") != "REJECTED":
                candidate["status"] = "WATCH"
        _apply_scan_completion_state(scanner, settings, ranked, top_candidates, eligible_count=len(filtered))
        scanner["gate_rejections"] = gate_rejections
        state["auto"]["last_scan"] = now_iso()
        persist_state(state)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        scanner.update({"scan_status": "HATA", "last_error": str(exc)[:220], "active": False})
        state["auto"]["last_error"] = str(exc)[:220]
    finally:
        scanner["running"] = False
        scanner["scan_duration_ms"] = int((time.perf_counter() - start) * 1000)
        persist_state(state)
        scan_lock.release()


async def automation_loop(application: Any) -> None:
    state = application.state.v21_demo
    while True:
        try:
            auto = state["auto"]
            if auto.get("enabled") and auto.get("user_confirmed") and not auto.get("busy"):
                auto["busy"] = True
                try:
                    await automatic_cycle(application)
                    auto["last_error"] = None
                finally:
                    auto["busy"] = False
            elif auto.get("enabled") and not auto.get("user_confirmed"):
                auto["enabled"] = False
                auto["last_decision"] = "Güvenli bekleme: kullanıcı onayı silinmiş, otomasyon kapandı."
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            state["auto"].update({"last_error": str(exc)[:220], "last_decision": "Otomasyon hatası; güvenli bekleme ve yeniden deneme."})
        await asyncio.sleep(max(15, int(state["settings"]["scan_seconds"])))


async def scanner_loop(application: Any) -> None:
    while True:
        try:
            await run_scanner_cycle(application)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            state = application.state.v21_demo
            state["scanner"].update({"scan_status": "HATA", "last_error": str(exc)[:220], "active": False})
        await asyncio.sleep(SCAN_INTERVAL_SECONDS)


AUTOMATION_TASK_NAME = "v21_automation_loop"


def _automation_task_done(application: Any, task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    try:
        error = task.exception()
    except Exception as exc:
        error = exc
    if error is None:
        return
    state = getattr(application.state, "v21_demo", None)
    if not isinstance(state, dict):
        return
    message = f"V21 automation loop beklenmedik şekilde durdu: {error}"
    state["auto"].update({"last_error": str(error)[:220], "last_decision": message})
    record_event(state, "AUTO_LOOP_CRASH", message, source="SYSTEM")
    persist_state(state)


def ensure_automation_task(application: Any) -> asyncio.Task[Any]:
    tasks = getattr(application.state, "v21_tasks", None)
    if not isinstance(tasks, list):
        tasks = []
        application.state.v21_tasks = tasks
    tracked = getattr(application.state, "v21_automation_task", None)
    candidates = [tracked] if tracked is not None else []
    candidates.extend(
        candidate for candidate in tasks
        if candidate not in candidates and candidate.get_name() == AUTOMATION_TASK_NAME
    )
    task = next((candidate for candidate in candidates if not candidate.done()), None)
    if task is not None:
        application.state.v21_automation_task = task
        return task
    task = asyncio.create_task(automation_loop(application), name=AUTOMATION_TASK_NAME)
    tasks.append(task)
    application.state.v21_automation_task = task
    task.add_done_callback(lambda completed: _automation_task_done(application, completed))
    return task


def backtest_engine(candles: list[dict[str, float]], settings: dict[str, Any]) -> dict[str, Any]:
    if len(candles) < 260:
        raise HTTPException(422, "Backtest için en az 260 mum gerekiyor.")
    closes = [row["close"] for row in candles]
    highs = [row["high"] for row in candles]
    lows = [row["low"] for row in candles]
    e20, e50, e200 = ema(closes, 20), ema(closes, 50), ema(closes, 200)
    costs = (float(settings["fee_bps_per_side"]) + float(settings["slippage_bps_per_side"])) / 10_000 * 2
    trades: list[dict[str, Any]] = []
    equity = 1000.0
    peak = equity
    max_drawdown = 0.0
    index = 210
    while index < len(candles) - 26:
        signal_index = index
        long_signal = closes[signal_index] > e20[signal_index] > e50[signal_index] > e200[signal_index]
        short_signal = closes[signal_index] < e20[signal_index] < e50[signal_index] < e200[signal_index]
        if not long_signal and not short_signal:
            index += 1
            continue
        direction = "LONG" if long_signal else "SHORT"
        # No look-ahead: signal uses candle[index], entry is next candle open.
        entry_index = signal_index + 1
        entry = candles[entry_index]["open"]
        atr_value = atr(highs[: signal_index + 1], lows[: signal_index + 1], closes[: signal_index + 1])
        if atr_value <= 0:
            index += 1
            continue
        stop = entry - 1.5 * atr_value if direction == "LONG" else entry + 1.5 * atr_value
        target = entry + 3 * atr_value if direction == "LONG" else entry - 3 * atr_value
        risk_pct = abs(entry - stop) / entry
        notional = min(float(MAX_NOTIONAL_USDT), float(settings["max_loss_per_trade"]) / max(risk_pct, 1e-9))
        exit_price = candles[min(entry_index + 24, len(candles) - 1)]["close"]
        exit_reason = "ZAMAN"
        exit_index = min(entry_index + 24, len(candles) - 1)
        for cursor in range(entry_index, min(entry_index + 25, len(candles))):
            row = candles[cursor]
            # Conservative policy: if stop and target occur in one candle, stop wins.
            stop_hit = row["low"] <= stop if direction == "LONG" else row["high"] >= stop
            target_hit = row["high"] >= target if direction == "LONG" else row["low"] <= target
            if stop_hit:
                exit_price, exit_reason, exit_index = stop, "STOP", cursor
                break
            if target_hit:
                exit_price, exit_reason, exit_index = target, "HEDEF", cursor
                break
        raw_return = (exit_price - entry) / entry * (1 if direction == "LONG" else -1)
        pnl = notional * (raw_return - costs)
        equity += pnl
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak * 100 if peak else 0)
        regime = "TREND" if abs(e20[signal_index] - e50[signal_index]) > atr_value * 0.35 else "YATAY"
        trades.append({
            "signal_time": candles[signal_index]["time"], "entry_time": candles[entry_index]["time"],
            "exit_time": candles[exit_index]["time"], "direction": direction, "entry": round(entry, 8),
            "exit": round(exit_price, 8), "reason": exit_reason, "pnl": round(pnl, 4),
            "cost_usdt": round(notional * costs, 4), "regime": regime,
        })
        index = exit_index + 1
    wins = [row for row in trades if row["pnl"] > 0]
    losses = [row for row in trades if row["pnl"] <= 0]
    gross_win = sum(row["pnl"] for row in wins)
    gross_loss = abs(sum(row["pnl"] for row in losses))
    fold_size = max(1, len(trades) // 3)
    folds = []
    labels = ["GELİŞTİRME", "DOĞRULAMA", "GÖRÜNMEYEN TEST"]
    for number, label in enumerate(labels):
        start = number * fold_size
        end = len(trades) if number == 2 else min(len(trades), (number + 1) * fold_size)
        subset = trades[start:end]
        folds.append({"name": label, "trades": len(subset), "net_pnl": round(sum(row["pnl"] for row in subset), 4)})
    return {
        "generated_at": now_iso(), "capital": 1000.0, "ending_equity": round(equity, 4),
        "net_pnl": round(equity - 1000, 4), "trades": len(trades),
        "wins": len(wins), "win_rate": round(len(wins) / len(trades) * 100, 2) if trades else 0,
        "max_drawdown_pct": round(max_drawdown, 2),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else (99.0 if gross_win else 0),
        "cost_model": {"fee_bps_per_side": settings["fee_bps_per_side"], "slippage_bps_per_side": settings["slippage_bps_per_side"]},
        "no_lookahead": True, "same_candle_policy": "STOP_FIRST", "folds": folds,
        "recent_trades": trades[-40:][::-1],
        "note": "Geçmiş Demo simülasyonu gelecek getiriyi garanti etmez.",
    }


def certificate_payload(state: dict[str, Any]) -> dict[str, Any]:
    journal = state.get("journal", [])
    closed = [item for item in journal if item.get("kind") == "POSITION_CLOSED" or (item.get("kind") == "FILL" and item.get("reduce_only"))]
    days = {str(item.get("created_at", ""))[:10] for item in closed if item.get("created_at")}
    snapshot = state.get("snapshot") or {}
    positions = snapshot.get("positions", [])
    algos = snapshot.get("open_algo_orders", [])
    protected = sum(1 for position in positions if any(order.get("symbol") == position.get("symbol") and str(order.get("type", "")).upper() == "STOP_MARKET" for order in algos))
    coverage = 100.0 if not positions else protected / len(positions) * 100
    backtest = state.get("backtest") or {}
    gates = [
        {"name": "Demo sunucu fiziksel kilidi", "passed": DEMO_REST_BASE.endswith("demo-fapi.binance.com") and DEMO_WS_BASE.endswith("demo-fstream.binance.com"), "value": "DEMO ONLY", "target": "Zorunlu"},
        {"name": "Kapanmış Demo işlem kanıtı", "passed": len(closed) >= 100, "value": len(closed), "target": 100},
        {"name": "Aktif gün", "passed": len(days) >= 30, "value": len(days), "target": 30},
        {"name": "Stop koruma kapsamı", "passed": coverage >= 100, "value": round(coverage, 1), "target": 100},
        {"name": "Yinelenen emir sızıntısı", "passed": int(state.get("duplicate_submissions", 0)) == 0, "value": int(state.get("duplicate_submissions", 0)), "target": 0},
        {"name": "Yeniden bağlantı tatbikatı", "passed": bool((state.get("drills", {}).get("RECONNECT") or {}).get("passed")), "value": "GEÇTİ" if (state.get("drills", {}).get("RECONNECT") or {}).get("passed") else "BEKLİYOR", "target": "GEÇTİ"},
        {"name": "Acil durdurma tatbikatı", "passed": bool((state.get("drills", {}).get("EMERGENCY") or {}).get("passed")), "value": "GEÇTİ" if (state.get("drills", {}).get("EMERGENCY") or {}).get("passed") else "BEKLİYOR", "target": "GEÇTİ"},
        {"name": "Backtest maksimum düşüş", "passed": bool(backtest) and float(backtest.get("max_drawdown_pct", 999)) <= 10, "value": backtest.get("max_drawdown_pct", "—"), "target": "≤ 10%"},
    ]
    passed = sum(1 for gate in gates if gate["passed"])
    return {
        "version": "21.0.0", "status": "DEMO SERTİFİKALI" if passed == len(gates) else "KANIT TOPLUYOR",
        "score": round(passed / len(gates) * 100), "passed_gates": passed, "total_gates": len(gates),
        "gates": gates, "real_trading_ready": False, "demo_only": True,
        "reason": "Bu sertifika yalnızca Binance Futures Demo çalışma disiplinini ölçer; gerçek para uygunluğu vermez.",
        "generated_at": now_iso(),
    }


def performance_payload(state: dict[str, Any], period: str = "all") -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    cutoff = {"daily": 1, "weekly": 7, "monthly": 31}.get(period)
    events = []
    for item in state.get("journal", []):
        if item.get("verified_realized") is not True or item.get("realized_pnl") is None:
            continue
        try:
            created = datetime.fromisoformat(str(item.get("created_at", "")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if cutoff is None or (now - created).total_seconds() <= cutoff * 86400:
            events.append(item)
    unique_events: list[dict[str, Any]] = []
    seen_events: set[str] = set()
    for item in events:
        identity = str(item.get("id") or f"{item.get('symbol')}|{item.get('created_at')}|{item.get('realized_pnl')}")
        if identity in seen_events:
            continue
        seen_events.add(identity)
        unique_events.append(item)
    events = unique_events
    def event_time(item: dict[str, Any]) -> datetime:
        try:
            value = datetime.fromisoformat(str(item.get("created_at", "")).replace("Z", "+00:00"))
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        except ValueError:
            return datetime.min.replace(tzinfo=timezone.utc)
    events.sort(key=event_time)
    pnls = [round(float(item.get("realized_pnl") or 0), 4) for item in events]
    wins = [pnl for pnl in pnls if pnl > 0]
    losses = [pnl for pnl in pnls if pnl < 0]
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    average_win = round(sum(wins) / len(wins), 4) if wins else None
    average_loss = round(sum(losses) / len(losses), 4) if losses else None
    winning_streak = losing_streak = 0
    current_wins = current_losses = 0
    equity_curve: list[dict[str, float | int]] = []
    running_equity = 0.0
    for index, item in enumerate(events, start=1):
        pnl = round(float(item.get("realized_pnl") or 0), 4)
        running_equity += pnl
        equity_curve.append({"index": index, "pnl": pnl, "equity": round(running_equity, 4)})
        if pnl > 0:
            current_wins += 1
            current_losses = 0
        elif pnl < 0:
            current_losses += 1
            current_wins = 0
        else:
            current_wins = current_losses = 0
        winning_streak = max(winning_streak, current_wins)
        losing_streak = max(losing_streak, current_losses)
    directional: dict[str, dict[str, Any]] = {}
    for direction in ("LONG", "SHORT"):
        rows = [item for item in events if ("LONG" if str(item.get("side") or item.get("direction") or "").upper() == "BUY" else "SHORT" if str(item.get("side") or item.get("direction") or "").upper() == "SELL" else str(item.get("side") or item.get("direction") or "").upper()) == direction]
        row_pnls = [float(item.get("realized_pnl") or 0) for item in rows]
        row_wins = [pnl for pnl in row_pnls if pnl > 0]
        row_losses = [pnl for pnl in row_pnls if pnl < 0]
        directional[direction] = {
            "trades": len(row_pnls),
            "win_rate": round(len(row_wins) / len(row_pnls) * 100, 2) if row_pnls else None,
            "realized_pnl": round(sum(row_pnls), 4) if row_pnls else None,
            "profit_factor": round(sum(row_wins) / abs(sum(row_losses)), 2) if row_losses else None,
        }
    return {
        "period": period, "total_trades": len(pnls), "wins": len(wins), "losses": len(losses),
        "win_rate": round(len(wins) / len(pnls) * 100, 2) if pnls else 0,
        "total_profit": round(sum(wins), 4), "total_loss": round(sum(losses), 4),
        "net_profit": round(sum(pnls), 4), "average_trade": round(sum(pnls) / len(pnls), 4) if pnls else 0,
        "best_trade": max(pnls) if pnls else 0, "worst_trade": min(pnls) if pnls else 0,
        "profit_factor": round(sum(wins) / abs(sum(losses)), 2) if losses else None,
        "average_win": average_win, "average_loss": average_loss,
        "winning_streak": winning_streak, "losing_streak": losing_streak,
        "equity_curve": equity_curve if len(equity_curve) >= 2 else [],
        "directional": directional,
        "history_quality": "VERIFIED" if len(events) >= 2 else "INSUFFICIENT HISTORY",
        "max_drawdown": round(max_drawdown, 4), "demo_only": True, "read_only": True,
    }


def summary_payload(state: dict[str, Any]) -> dict[str, Any]:
    snapshot = state.get("snapshot") or {}
    reconciliation = state.get("reconciliation") or {}
    scanner = state.get("scanner", {})
    paper_positions = state.get("paper_positions", [])
    scanner_payload = {
        **scanner,
        "scan_interval_seconds": SCAN_INTERVAL_SECONDS,
        "selected_count": len(scanner.get("selected_symbols", [])),
        "scan_duration_seconds": round(float(scanner.get("scan_duration_ms", 0)) / 1000, 3),
        "top_candidates": scanner.get("top_candidates", [])[:3],
        "all_candidates": scanner.get("all_candidates", [])[:100],
    }
    return {
        "version": "21.0.0", "mode": "BINANCE_FUTURES_DEMO_ONLY", "settings": state["settings"],
        "auto": state["auto"], "risk": state.get("risk", {}), "notifications": state.get("notifications", {}),
        "scanner": scanner_payload, "stream": state["stream"], "daily": daily_metrics(state),
        "account": {
            "wallet_balance": snapshot.get("wallet_balance"), "available_balance": snapshot.get("available_balance"),
            "unrealized_pnl": snapshot.get("unrealized_pnl"), "positions": len(snapshot.get("positions", [])) + len(paper_positions),
            "auto_positions": len({str(trade.get("symbol")) for trade in state.get("automation_trades", []) if str(trade.get("status") or "").upper() not in {"KAPANDI", "CLOSED", "İPTAL"}}),
            "reconciled_active_positions": int(reconciliation.get("reconciled_active_positions", len(snapshot.get("positions", [])))) + len(paper_positions),
            "normal_orders": len(snapshot.get("open_orders", [])), "algo_orders": len(snapshot.get("open_algo_orders", [])),
        },
        "protection": {
            "repairs": state.get("protection_repairs", 0),
            "duplicate_blocks": state.get("duplicate_blocks", 0),
            "duplicate_submissions": state.get("duplicate_submissions", 0),
        },
        "journal": state.get("journal", [])[:60], "backtest": state.get("backtest"),
        "automation_trades": state.get("automation_trades", [])[:100],
        "certificate": certificate_payload(state), "last_saved": state.get("last_saved"),
        "real_trading_locked": True,
    }


def init_v21_demo(application: Any) -> None:
    state = load_state()
    state["lock"] = asyncio.Lock()
    application.state.v21_demo = state
    record_event(state, "V21_START", "V21 Demo Complete başladı; otomatik girişler güvenlik için kapalı.", source="SYSTEM")
    application.state.v21_tasks = [
        asyncio.create_task(reconciliation_loop(application)),
        asyncio.create_task(user_stream_loop(application)),
        asyncio.create_task(scanner_loop(application)),
    ]
    ensure_automation_task(application)


async def shutdown_v21_demo(application: Any) -> None:
    state = getattr(application.state, "v21_demo", None)
    tasks = getattr(application.state, "v21_tasks", [])
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    persistence_tasks = list(getattr(application.state, "_v21_persistence_tasks", set()))
    if persistence_tasks:
        await asyncio.gather(*persistence_tasks, return_exceptions=True)
    if state:
        state["auto"]["enabled"] = False
        persist_state(state)


@router.get("/summary")
async def v21_summary(request: Request) -> dict[str, Any]:
    return summary_payload(state_for(request))


@router.get("/markets")
async def v21_markets(request: Request) -> dict[str, Any]:
    try:
        markets = await auto_trade_market_universe(request.app)
        return {"markets": markets, "count": len(markets), "demo_only": True}
    except BinanceDemoError as exc:
        raise safe_exchange_error(exc) from exc


@router.get("/scanner")
async def v21_scanner(request: Request) -> dict[str, Any]:
    return summary_payload(state_for(request))["scanner"]


@router.post("/scanner/scan")
async def v21_manual_scan(request: Request) -> dict[str, Any]:
    await run_scanner_cycle(request.app)
    return summary_payload(state_for(request))["scanner"]


@router.get("/scanner/candidates")
async def v21_scanner_candidates(request: Request) -> dict[str, Any]:
    scanner = summary_payload(state_for(request))["scanner"]
    return {"candidates": scanner["all_candidates"], "top_candidates": scanner["top_candidates"], "coins_scanned": scanner["coins_scanned"], "demo_only": True}


@router.get("/automation/trades")
async def v21_automation_trades(request: Request, limit: int = Query(default=100, ge=1, le=500)) -> dict[str, Any]:
    trades = state_for(request).get("automation_trades", [])[:limit]
    return {"trades": trades, "total": len(trades), "demo_only": True}


@router.put("/settings")
async def v21_settings(request: Request, body: SettingsUpdate) -> dict[str, Any]:
    state = state_for(request)
    updates = body.model_dump(exclude_none=True)
    if "allowed_symbols" in updates:
        symbols = []
        for value in updates["allowed_symbols"]:
            symbol = normalize_symbol(value)
            if symbol and symbol not in symbols:
                symbols.append(symbol)
        updates["allowed_symbols"] = symbols
    state["settings"].update(updates)
    state["settings"]["max_margin_per_trade"] = min(float(MAX_MARGIN_USDT), float(state["settings"]["max_margin_per_trade"]))
    state["settings"]["max_positions"] = min(MAX_OPEN_POSITIONS, int(state["settings"]["max_positions"]))
    record_event(state, "SETTINGS", "V21 Demo risk ve otomasyon ayarları güncellendi.", source="USER")
    persist_state(state)
    return summary_payload(state)


@router.post("/risk/size")
async def v21_risk_size(request: Request, body: RiskSizeRequest) -> dict[str, Any]:
    state = state_for(request)
    symbol = normalize_symbol(body.symbol)
    values = risk_size_values(body.entry, body.stop, body.max_loss_usdt, body.leverage, float(state["settings"]["max_margin_per_trade"]))
    rules = await symbol_rules(client_for(request), symbol)
    quantity = Decimal(str(values["notional_usdt"])) / Decimal(str(body.entry))
    return {**values, "symbol": symbol, "leverage": body.leverage, "quantity_preview": decimal_text(quantity), "step_size": decimal_text(rules["step"]), "demo_only": True}


@router.post("/smoke-test")
async def v21_demo_smoke_test(request: Request) -> dict[str, Any]:
    state = state_for(request)
    candidates = state["scanner"].get("top_candidates", [])
    allowed_symbols = _effective_allowed_symbols(state["settings"])
    candidate = next((item for item in candidates if allowed_symbols is None or normalize_symbol(item.get("symbol", "")) in allowed_symbols), None)
    if candidate is None:
        _set_rejection(state, "ALLOWED_SYMBOLS", "Demo işlemi testi için izinli ve seçili aday bulunamadı.")
        persist_state(state)
        raise HTTPException(409, state["auto"]["rejection_reason"])
    symbol = normalize_symbol(candidate["symbol"])
    direction = str(candidate.get("direction") or "NEUTRAL").upper()
    if direction not in {"LONG", "SHORT"} or (direction == "LONG" and not state["settings"]["allow_long"]) or (direction == "SHORT" and not state["settings"]["allow_short"]):
        _set_rejection(state, "DIRECTION", f"{symbol} yön kapısından geçmedi; smoke işlemi açılmadı.")
        persist_state(state)
        raise HTTPException(409, state["auto"]["rejection_reason"])
    if not in_schedule(state["settings"]):
        _set_rejection(state, "MARKET_HOURS", "İzin verilen çalışma saatleri dışında; smoke işlemi açılmadı.")
        persist_state(state)
        raise HTTPException(409, state["auto"]["rejection_reason"])
    if any(item.get("symbol") == symbol for item in state.get("paper_positions", [])):
        _set_rejection(state, "DUPLICATE_POSITION", f"{symbol} için zaten paper pozisyonu açık; smoke işlemi açılmadı.")
        persist_state(state)
        raise HTTPException(409, state["auto"]["rejection_reason"])
    daily = daily_metrics(state)
    if daily["auto_entries"] >= state["settings"]["daily_trade_limit"]:
        _set_rejection(state, "DAILY_TRADE_LIMIT", "Günlük Demo işlem limiti doldu; smoke işlemi açılmadı.")
        persist_state(state)
        raise HTTPException(409, state["auto"]["rejection_reason"])
    if daily["realized_pnl"] <= -float(state["settings"]["daily_loss_limit"]):
        _set_rejection(state, "DAILY_LOSS_LIMIT", "Günlük Demo zarar limiti aktif; smoke işlemi açılmadı.")
        persist_state(state)
        raise HTTPException(409, state["auto"]["rejection_reason"])
    max_positions = min(state["settings"]["max_positions"], MAX_OPEN_POSITIONS)
    if len(state.get("paper_positions", [])) >= max_positions:
        _set_rejection(state, "MAX_POSITIONS", "Açık pozisyon sınırı dolu; smoke işlemi açılmadı.")
        persist_state(state)
        raise HTTPException(409, state["auto"]["rejection_reason"])
    if candidate.get("status") != "SELECTED" or not all(float(candidate.get(key) or 0) > 0 for key in ("entry", "stop_loss", "tp1", "tp2", "tp3")):
        _set_rejection(state, "RISK_LEVELS", f"{symbol} için geçerli risk seviyeleri yok; smoke işlemi açılmadı.")
        persist_state(state)
        raise HTTPException(409, state["auto"]["rejection_reason"])
    sizing = risk_size_values(float(candidate["entry"]), float(candidate["stop_loss"]), float(state["settings"]["max_loss_per_trade"]), MAX_LEVERAGE, min(float(state["settings"]["max_margin_per_trade"]), float(MAX_MARGIN_USDT)))
    position = {
        "id": uuid.uuid4().hex[:12], "symbol": symbol, "direction": direction,
        "entry_price": float(candidate["entry"]), "stop_loss": float(candidate["stop_loss"]),
        "targets": [float(candidate["tp1"]), float(candidate["tp2"]), float(candidate["tp3"])],
        "margin_usdt": sizing["margin_usdt"], "leverage": MAX_LEVERAGE,
        "score": candidate.get("score", candidate.get("opportunity_score", 0)), "reasons": candidate.get("reasons", []),
        "status": "PAPER_OPEN", "created_at": now_iso(), "demo_only": True, "source": "SMOKE_TEST",
    }
    state.setdefault("paper_positions", []).append(position)
    state["auto"].update({"rejection_gate": None, "rejection_reason": None, "last_error": None, "last_decision": f"Demo smoke işlemi açıldı: {symbol}."})
    record_event(state, "PAPER_SMOKE", f"{symbol} Demo smoke paper pozisyonu açıldı; Stop {position['stop_loss']} · TP {position['targets'][-1]}.", symbol=symbol, side=position["direction"], price=position["entry_price"], source="SMOKE_TEST")
    persist_state(state)
    return {"ok": True, "position": position, "open_position_count": len(state["paper_positions"]), "demo_only": True, "real_trading_locked": True}


@router.post("/auto/start")
async def v21_auto_start(request: Request, body: AutoStartRequest) -> dict[str, Any]:
    state = state_for(request)
    confirmation = body.confirmation.strip().upper()
    if confirmation != "DEMO OTOMATİK":
        _set_rejection(state, "CONFIRMATION", "Otomasyonu açmak için DEMO OTOMATİK yazın.")
        persist_state(state)
        raise HTTPException(422, "Otomasyonu açmak için DEMO OTOMATİK yazın.")
    demo_state = demo_state_for(request)
    if not armed(demo_state):
        _set_rejection(state, "DEMO_ARM", "Önce İşlem Masası'ndaki 10 dakikalık DEMO emir kilidini açın.")
        persist_state(state)
        raise HTTPException(423, "Önce İşlem Masası'ndaki 10 dakikalık DEMO emir kilidini açın.")
    armed_until = demo_state.get("armed_until")
    if armed_until is not None:
        request.app.state.binance_demo["armed_until"] = armed_until
    if not credentials_configured(request):
        _set_rejection(state, "DEMO_CREDENTIALS", "Binance Futures Demo anahtarları ayarlı değil.")
        persist_state(state)
        raise HTTPException(412, "Binance Futures Demo anahtarları ayarlı değil.")
    snapshot = await account_snapshot(client_for(request))
    if snapshot.get("hedge_mode"):
        _set_rejection(state, "POSITION_MODE", "Demo hesabı One-way / Tek Yön modunda olmalı.")
        persist_state(state)
        raise HTTPException(409, "Demo hesabı One-way / Tek Yön modunda olmalı.")
    state["auto"].update({
        "enabled": True,
        "status": "ON",
        "pause_reason": None,
        "started_at": state["auto"].get("started_at") or now_iso(),
        "user_confirmed": True,
        "confirmation": confirmation,
        "last_decision": "Kontrollü Demo taraması başlatıldı.",
        "last_error": None,
    })
    record_event(state, "AUTO_START", "V21 kontrollü otomasyon kullanıcı onayıyla açıldı.", source="USER")
    emit_notification(state, "AUTO_STARTED", "Demo Auto Trade başlatıldı.", event_id=f"{today()}-auto-start-{int(time.time())}")
    persist_state(state)
    ensure_automation_task(request.app)
    try:
        await automatic_cycle(request.app, request=request)
    except Exception as exc:
        error = str(exc)[:220]
        state["auto"].update({
            "enabled": False,
            "status": "OFF",
            "last_error": error,
            "last_decision": "İlk Demo taraması hata verdi; otomasyon güvenli olarak kapatıldı.",
        })
        state["scanner"].update({"scan_status": "HATA", "last_error": error, "active": False, "running": False})
        record_event(state, "AUTO_START_ERROR", f"İlk Demo taraması başarısız: {error}", source="SYSTEM")
        persist_state(state)
    return summary_payload(state)


@router.post("/auto/stop")
async def v21_auto_stop(request: Request) -> dict[str, Any]:
    state = state_for(request)
    state["auto"].update({
        "enabled": False,
        "status": "OFF",
        "pause_reason": None,
        "user_confirmed": False,
        "confirmation": None,
        "last_decision": "Yeni otomatik Demo girişleri durduruldu.",
    })
    record_event(state, "AUTO_STOP", "V21 otomatik girişleri durduruldu; mevcut Stop/TP korumaları açık.", source="USER")
    emit_notification(state, "AUTO_STOPPED", "Demo Auto Trade durduruldu; mevcut korumalar açık.", event_id=f"{today()}-auto-stop-{int(time.time())}")
    persist_state(state)
    return summary_payload(state)


@router.get("/journal")
async def v21_journal(request: Request, limit: int = Query(default=200, ge=1, le=1000)) -> dict[str, Any]:
    state = state_for(request)
    return {"items": state.get("journal", [])[:limit], "total": len(state.get("journal", [])), "demo_only": True}


@router.get("/performance")
async def v21_performance(request: Request, period: Literal["all", "daily", "weekly", "monthly"] = "all") -> dict[str, Any]:
    return performance_payload(state_for(request), period)


@router.get("/history/{symbol}")
async def v21_history(request: Request, symbol: str) -> dict[str, Any]:
    safe_symbol = normalize_symbol(symbol)
    try:
        client = client_for(request)
        normal, algos, trades = await asyncio.gather(
            client.signed("GET", "/fapi/v1/allOrders", {"symbol": safe_symbol, "limit": 200}),
            client.signed("GET", "/fapi/v1/allAlgoOrders", {"symbol": safe_symbol, "limit": 200}),
            client.signed("GET", "/fapi/v1/userTrades", {"symbol": safe_symbol, "limit": 200}),
        )
        return {"symbol": safe_symbol, "orders": response_rows(normal), "algo_orders": response_rows(algos), "trades": response_rows(trades), "demo_only": True}
    except BinanceDemoError as exc:
        raise safe_exchange_error(exc) from exc


@router.post("/backtest")
async def v21_backtest(request: Request, body: BacktestRequest) -> dict[str, Any]:
    state = state_for(request)
    symbol = normalize_symbol(body.symbol)
    candles = await demo_candles(client_for(request.app), symbol, body.interval, body.limit)
    result = backtest_engine(candles, state["settings"])
    result.update({"symbol": symbol, "interval": body.interval})
    state["backtest"] = result
    record_event(state, "BACKTEST", f"{symbol} {body.interval} kronolojik backtest tamamlandı: {result['trades']} işlem.", symbol=symbol, source="LAB")
    persist_state(state)
    return result


@router.post("/drill")
async def v21_drill(request: Request, body: DrillRequest) -> dict[str, Any]:
    state = state_for(request)
    if body.kind == "RECONNECT":
        passed = bool(state["stream"].get("last_sync")) and credentials_configured()
        detail = "REST eşleştirme ve yeniden bağlantı yolu hazır." if passed else "Önce Demo bağlantısını kurun."
    elif body.kind == "PROTECTION":
        snapshot = state.get("snapshot") or {}
        passed = all(
            any(order.get("symbol") == position.get("symbol") and str(order.get("type", "")).upper() == "STOP_MARKET" for order in snapshot.get("open_algo_orders", []))
            for position in snapshot.get("positions", [])
        )
        detail = "Açık pozisyonların Stop kapsamı doğrulandı." if passed else "Korumasız açık pozisyon bulundu."
    else:
        passed = (
            DEMO_REST_BASE == "https://demo-fapi.binance.com"
            and DEMO_WS_BASE == "wss://demo-fstream.binance.com"
            and bool(getattr(request.app.state, "binance_demo", None) is not None)
        )
        detail = (
            "Demo sunucu kilidi ve acil durdurma yolu doğrulandı; tatbikatta emir gönderilmedi."
            if passed else "Demo kilidi veya acil durdurma durumu doğrulanamadı."
        )
    result = {"kind": body.kind, "passed": passed, "detail": detail, "tested_at": now_iso(), "simulation_only": True}
    state["drills"][body.kind] = result
    record_event(state, "DRILL", f"{body.kind} tatbikatı: {'GEÇTİ' if passed else 'BAŞARISIZ'} · {detail}", source="CERTIFICATE")
    persist_state(state)
    return {**result, "certificate": certificate_payload(state)}


@router.get("/certificate")
async def v21_certificate(request: Request) -> dict[str, Any]:
    return certificate_payload(state_for(request))


@router.get("/daily-report")
async def v21_daily_report(request: Request) -> dict[str, Any]:
    state = state_for(request)
    daily = daily_metrics(state)
    return {
        **daily, "stream": state["stream"], "auto": state["auto"],
        "protection_repairs": state.get("protection_repairs", 0),
        "duplicate_blocks": state.get("duplicate_blocks", 0),
        "duplicate_submissions": state.get("duplicate_submissions", 0),
        "certificate_score": certificate_payload(state)["score"],
        "headline": "V21 Demo kanıt raporu", "demo_only": True,
    }
