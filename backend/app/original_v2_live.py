"""OWNER-only Original V2 integration; signal policy remains the Demo policy."""
from __future__ import annotations

import copy
import asyncio
import logging
import math
import os
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import original_v2_demo_execution as original
from .market_universe import (
    ORIGINAL_DEFAULT_SYMBOL_COUNT,
    ORIGINAL_MAX_SYMBOL_COUNT,
    ORIGINAL_MIN_SYMBOL_COUNT,
    ORIGINAL_MIN_SYMBOL_AGE_DAYS,
    ranked_original_live_symbols,
)
from .strategies import original_v2_demo as strategy
from .strategies.contracts import StrategyResult
from . import original_live_risk as risk_settings
from .v22_commercial import authenticated_user

router = APIRouter(prefix="/api/v25/original-v2", tags=["Original V2 LIVE"])
FLAG = "PROTREBOT_LIVE_KAIS_ORIGINAL_V2_ENABLED"
SOURCE = "ORIGINAL_V2_LIVE"
SCANNER_HISTORY_LIMIT = 128
logger = logging.getLogger(__name__)


class Start(risk_settings.RiskChoice):
    model_config = ConfigDict(extra="forbid")
    confirmation: str = "ANLADIM"
    totp_code: str | None = Field(default=None, min_length=6, max_length=32, repr=False)
    symbol_count: int = Field(default=ORIGINAL_DEFAULT_SYMBOL_COUNT, ge=ORIGINAL_MIN_SYMBOL_COUNT, le=ORIGINAL_MAX_SYMBOL_COUNT, strict=True)
    session_hours: int = Field(default=1, strict=True)

    @field_validator("session_hours")
    @classmethod
    def validate_session_hours(cls, value: int) -> int:
        if value not in {1, 4, 12, 24}:
            raise ValueError("Original V2 oturumu 1, 4, 12 veya 24 saat olabilir.")
        return value

class FillLockApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lock_id: str = Field(min_length=1, max_length=64)
    confirmation: str
    totp_code: str = Field(min_length=6, max_length=32, repr=False)


def fill_locked(state: dict[str, Any]) -> bool:
    return (settings(state).get("fill_lock") or {}).get("active") is True


def require_fill_unlocked(state: dict[str, Any]) -> None:
    if fill_locked(state):
        raise HTTPException(423, "Original V2 dolum güvenlik kilidi aktif; OWNER onayı olmadan yeni LIVE giriş açılamaz.")


def available() -> bool:
    value = os.getenv(FLAG, "false").strip().lower()
    if value not in {"true", "false", "1", "0"}:
        raise HTTPException(503, "Original V2 LIVE ayarı geçersiz.")
    return value in {"true", "1"}


def settings(state: dict[str, Any]) -> dict[str, Any]:
    return state.get("original_v2_live", {})


def running(state: dict[str, Any]) -> bool:
    return settings(state).get("enabled") is True


def _valid_symbol_count(config: dict[str, Any]) -> int:
    count = config.get("symbol_count", ORIGINAL_DEFAULT_SYMBOL_COUNT)
    if type(count) is not int or not ORIGINAL_MIN_SYMBOL_COUNT <= count <= ORIGINAL_MAX_SYMBOL_COUNT:
        raise HTTPException(503, "Original V2 sembol sayısı geçersiz; yeni taramalar kapalı.")
    return count


def _today_universe(config: dict[str, Any]) -> dict[str, Any] | None:
    universe = config.get("universe")
    if not isinstance(universe, dict) or universe.get("updated_day") != datetime.now(timezone.utc).date().isoformat():
        return None
    if universe.get("minimum_age_days") != ORIGINAL_MIN_SYMBOL_AGE_DAYS:
        return None
    ranked = universe.get("ranked_symbols")
    if (
        not isinstance(ranked, list)
        or len(ranked) > ORIGINAL_MAX_SYMBOL_COUNT
        or any(not isinstance(symbol, str) or not symbol.isascii() or not symbol.isalnum() or not symbol.isupper() or not symbol.endswith("USDT") for symbol in ranked)
        or len(set(ranked)) != len(ranked)
    ):
        return None
    eligible_count = universe.get("eligible_count")
    if type(eligible_count) is not int or eligible_count < len(ranked):
        return None
    return universe


def _scanner_history_summary(scanner: dict[str, Any], now: float) -> dict[str, int]:
    history = scanner.get("history")
    if not isinstance(history, list):
        return {"scans": 0, "candidates": 0, "trades": 0}
    recent = []
    for item in history:
        if not isinstance(item, dict):
            continue
        at = item.get("at")
        candidates = item.get("candidates")
        trades = item.get("trades")
        if not isinstance(at, (int, float)) or isinstance(at, bool):
            continue
        try:
            at_value = float(at)
        except (OverflowError, ValueError):
            continue
        if (
            math.isfinite(at_value) and now - 86400 <= at_value <= now
            and type(candidates) is int and candidates >= 0
            and type(trades) is int and trades >= 0
        ):
            recent.append({**item, "at": at_value})
    return {
        "scans": len(recent),
        "candidates": sum(item["candidates"] for item in recent),
        "trades": sum(item["trades"] for item in recent),
    }


def _record_scanner_scan(scanner: dict[str, Any], at: float, candidates: int) -> dict[str, Any]:
    history = scanner.get("history")
    if not isinstance(history, list):
        history = []
    history.append({"at": at, "candidates": candidates, "trades": 0})
    scanner["history"] = history[-SCANNER_HISTORY_LIMIT:]
    return scanner["history"][-1]


def selected_symbols(config: dict[str, Any]) -> list[str]:
    universe = _today_universe(config)
    if universe is None:
        return []
    return universe["ranked_symbols"][:_valid_symbol_count(config)]


async def resolve_live_universe(application: Any, client: Any, config: dict[str, Any]) -> list[str]:
    from . import v25_execution as live
    universe = _today_universe(config)
    if universe is None:
        info = await client.public_get("/fapi/v1/exchangeInfo")
        tickers = await client.public_get("/fapi/v1/ticker/24hr")
        book_tickers = await client.public_get("/fapi/v1/ticker/bookTicker")
        filter_counts: dict[str, Any] = {}
        try:
            ranked, eligible_count = ranked_original_live_symbols(
                info, tickers, book_tickers, now_ms=int(time.time() * 1000),
                diagnostics=filter_counts,
            )
        except ValueError:
            config["universe_filter_failure"] = filter_counts
            raise
        universe = {
            "updated_day": datetime.now(timezone.utc).date().isoformat(),
            "updated_at": live.now_iso(),
            "ranked_symbols": ranked,
            "eligible_count": eligible_count,
            "minimum_age_days": ORIGINAL_MIN_SYMBOL_AGE_DAYS,
            "filter_counts": filter_counts,
        }
        config.pop("universe_filter_failure", None)
        config["universe"] = universe
    symbols = universe["ranked_symbols"][:_valid_symbol_count(config)]
    service = getattr(application.state, "public_market_data", None)
    if service is not None:
        service.set_original_symbols(symbols)
    await configure_original_kline_stream(application, symbols)
    universe["selected_symbols"] = symbols
    return symbols


async def configure_original_kline_stream(application: Any, symbols: list[str]) -> None:
    stream = getattr(application.state, "original_kline_stream", None)
    if stream is None:
        return
    from .public_market_data import ORIGINAL_INTERVALS
    await stream.configure(symbols, ORIGINAL_INTERVALS)


def plan_is_original(plan: dict[str, Any]) -> bool:
    return plan.get("source") == SOURCE


def preserve(payload: dict[str, Any]) -> dict[str, Any] | None:
    value = payload.get("original_v2_live")
    if not isinstance(value, dict):
        return None
    result = copy.deepcopy(value)
    result["enabled"] = False
    result["paused"] = True
    result["pause_reason"] = "Yeniden başlatma sonrası risk onayı ve ARM gerekli."
    return result


def public_config(state: dict[str, Any]) -> dict[str, Any]:
    config = settings(state)
    symbols = selected_symbols(config)
    cached_universe = _today_universe(config)
    universe = {
        "requested_count": _valid_symbol_count(config),
        "selected_count": len(symbols),
        "eligible_count": int(cached_universe.get("eligible_count", 0)) if cached_universe is not None else 0,
        "minimum_age_days": ORIGINAL_MIN_SYMBOL_AGE_DAYS,
        "updated_at": cached_universe.get("updated_at") if cached_universe is not None else None,
        "filter_counts": (
            copy.deepcopy(cached_universe.get("filter_counts", {}))
            if cached_universe is not None
            else copy.deepcopy(config.get("universe_filter_failure", {}))
        ),
    }
    scanner = copy.deepcopy(config.get("scanner"))
    if isinstance(scanner, dict):
        scanner["last_24h"] = _scanner_history_summary(scanner, time.time())
    expires = float(config.get("session_expires_at_epoch") or ((state.get("auto") or {}).get("session_until") if running(state) else 0) or 0)
    return {"available": available(), "enabled": running(state),
            "paused": config.get("paused", False), "pause_reason": config.get("pause_reason"),
            "risk_percent": config.get("risk_percent", 0.5), "daily_loss_percent": config.get("daily_loss_percent", 5),
            "risk_revision": config.get("risk_revision", 0),
            "risk_limits": risk_settings.limits(state),
            "risk_projection": copy.deepcopy(config.get("risk_projection")),
            "last_risk_adjustment": copy.deepcopy(config.get("last_risk_adjustment")),
            "daily": copy.deepcopy(config.get("daily")),
            "fill_lock": copy.deepcopy(config.get("fill_lock")),
            "scanner": scanner,
            "universe": universe,
            "session": {"hours": config.get("session_hours", 1),
                        "started_at": config.get("session_started_at"),
                        "expires_at": datetime.fromtimestamp(expires, timezone.utc).isoformat() if expires else None,
                        "remaining_seconds": max(0, int(expires - time.time())) if running(state) else 0,
                        "authority_limited": config.get("session_authority_limited", False),
                        "warning_sent": bool(config.get("session_warning_sent"))},
            "symbols": symbols}


def pause(state: dict[str, Any], reason: str) -> None:
    from . import v25_execution as live
    config = settings(state)
    if not config:
        return
    already = config.get("paused") and config.get("pause_reason") == reason
    config.update(enabled=False, paused=True, pause_reason=reason)
    state["auto"].update(enabled=False, session_until=0.0)
    state.update(live_auto_trade=False, real_trading_locked=True, armed_until=0.0)
    if not already:
        event = live.add_event(state, "ORIGINAL_LIVE_PAUSED",
                               f"Original V2 LIVE durduruldu: {reason}. Açık pozisyon otomatik kapatılmadı.",
                               recipient_user_id=config.get("owner_id"), source=SOURCE)
        config.setdefault("alerts", []).insert(0, {
            "id": "original-live:" + event["id"], "type": "ORIGINAL_LIVE_PAUSED", "severity": "CRITICAL",
            "title": "Original V2 LIVE güvenlik kilidi", "message": event["message"],
            "timestamp": event["created_at"], "target": "master-trade", "read": False,
        })
        del config["alerts"][100:]
    live.persist_state(state)


def check_session_time(state: dict[str, Any]) -> None:
    from . import v25_execution as live
    if not running(state):
        return
    config = settings(state)
    expires = float(config.get("session_expires_at_epoch") or (state.get("auto") or {}).get("session_until") or 0)
    remaining = expires - time.time()
    if remaining <= 0:
        pause(state, "Original V2 oturum süresi doldu; yeni oturum için ARM ve güncel 2FA gerekli.")
    elif remaining <= 30 * 60 and not config.get("session_warning_sent"):
        config["session_warning_sent"] = True
        event = live.add_event(state, "ORIGINAL_SESSION_EXPIRING", "Original V2 LIVE oturumu 30 dakika içinde bitecek; otomatik yenilenmez.", recipient_user_id=config.get("owner_id"), source=SOURCE)
        config.setdefault("alerts", []).insert(0, {
            "id": "original-live:" + event["id"], "type": "ORIGINAL_SESSION_EXPIRING", "severity": "WARNING",
            "title": "Original V2 oturumu bitmek üzere", "message": event["message"],
            "timestamp": event["created_at"], "target": "master-trade", "read": False,
        })
        del config["alerts"][100:]
        live.persist_state(state)


def merge_bell(request: Request, items: list[dict[str, Any]], *, read_id: str | None = None,
               read_all: bool = False, limit: int = 100) -> list[dict[str, Any]]:
    from . import v25_execution as live
    from .live_notifications import merge_bell as merge_live_bell
    if read_id and read_id.startswith("live:"):
        items = merge_live_bell(request, items, read_id=read_id, limit=limit)
        read_id = None
    else:
        items = merge_live_bell(request, items, read_all=read_all, limit=limit)
    application = getattr(request, "app", None)
    state = getattr(getattr(application, "state", None), "v25_execution", {})
    config = settings(state)
    if not config:
        if read_id:
            raise HTTPException(404, "Bildirim bulunamadı.")
        return items
    user = authenticated_user(request)
    if user.get("role") != "OWNER" or user["id"] != config.get("owner_id"):
        if read_id:
            raise HTTPException(404, "Bildirim bulunamadı.")
        return items
    alerts = config.get("alerts", [])
    if read_id and not any(item["id"] == read_id for item in alerts):
        raise HTTPException(404, "Bildirim bulunamadı.")
    for alert in alerts:
        if read_all or alert["id"] == read_id:
            alert["read"] = True
    if read_id or read_all:
        live.persist_state(request.app.state.v25_execution)
    return sorted([*items, *copy.deepcopy(alerts)], key=lambda item: str(item.get("timestamp") or ""), reverse=True)[:limit]


async def require_owner_authority(application: Any, state: dict[str, Any]) -> None:
    from . import v25_execution as live
    from .v22_commercial import validate_authoritative_session
    authorization = state.get("auto_authorization") or {}
    if authorization.get("user_id") != settings(state).get("owner_id"):
        raise HTTPException(403, "Original V2 OWNER oturumu değişti.")
    if not await live.background_authorization_active(application, state):
        raise HTTPException(403, "Original V2 OWNER oturumu doğrulanamadı.")
    pool = getattr(application.state, "db_pool", None)
    if pool is None:
        raise HTTPException(503, "Original V2 OWNER yetkisi doğrulanamadı.")
    user = await validate_authoritative_session(
        application, settings(state)["owner_id"], authorization.get("auth_version"),
    )
    if user["role"] != "OWNER":
        raise HTTPException(403, "Original V2 yalnız OWNER tarafından çalıştırılabilir.")
    if authorization.get("fingerprint") != settings(state).get("fingerprint"):
        raise HTTPException(423, "Original V2 LIVE hesap bağlantısı değişti.")


async def daily_guard(client: Any, state: dict[str, Any], snapshot: dict[str, Any]) -> None:
    now = datetime.now(timezone.utc)
    start_ms = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)
    config = settings(state)
    wallet = float(snapshot["wallet_balance"])
    unrealized = float(snapshot["unrealized_pnl"])
    if not all(math.isfinite(value) for value in (wallet, unrealized)) or wallet <= 0:
        raise HTTPException(423, "Günlük bakiye doğrulanamadı.")
    rows = []
    for page in range(1, 9):
        batch = await client.signed("GET", "/fapi/v1/income", {
            "startTime": start_ms, "endTime": end_ms, "limit": 1000, "page": page,
        })
        if not isinstance(batch, list):
            raise HTTPException(423, "Günlük hesap hareketleri doğrulanamadı.")
        rows.extend(batch)
        if len(batch) < 1000:
            break
    else:
        raise HTTPException(423, "Günlük hesap hareketleri eksik; yeni girişler kapalı.")
    seen, total, pnl = set(), Decimal(0), Decimal(0)
    for row in rows:
        if (not isinstance(row, dict) or row.get("asset") != "USDT"
                or not {"incomeType", "tranId", "time", "income"}.issubset(row)):
            raise HTTPException(423, "USDT günlük kayıp hesabı doğrulanamadı.")
        key = (str(row["incomeType"]), str(row["tranId"]))
        if key in seen or not start_ms <= int(row["time"]) <= end_ms:
            raise HTTPException(423, "Günlük hesap hareketlerinde çakışma var.")
        seen.add(key)
        amount = Decimal(str(row["income"]))
        if not amount.is_finite():
            raise HTTPException(423, "Günlük hesap hareketi geçersiz.")
        total += amount
        if row["incomeType"] not in {"TRANSFER", "INTERNAL_TRANSFER"}:
            pnl += amount
    day = now.date().isoformat()
    day_state = config.get("daily", {})
    if day_state.get("day") != day:
        opening = float(Decimal(str(wallet)) - total)
        if not math.isfinite(opening) or opening <= 0:
            raise HTTPException(423, "Gün başlangıç bakiyesi doğrulanamadı.")
        day_state = {"day": day, "opening_wallet": opening, "locked": False}
        config["daily"] = day_state
    limit = day_state["opening_wallet"] * config["daily_loss_percent"] / 100
    loss = max(0, -float(pnl) - min(0, unrealized))
    day_state.update(loss=loss, limit=limit)
    if loss >= limit:
        day_state["locked"] = True
    if day_state["locked"]:
        raise HTTPException(423, "Günlük kayıp sınırı aşıldı; bugün yeni işlem açılmaz.")


def execution_policy(state: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    from pydantic import ValidationError
    config = settings(state)
    try:
        risk_settings.RiskChoice(
            risk_percent=config.get("risk_percent", 0.5),
            daily_loss_percent=config.get("daily_loss_percent", 5),
            risk_acknowledged=config.get("risk_acknowledged", False),
            high_risk_confirmation=risk_settings.HIGH_RISK_CONFIRMATION if config.get("high_risk_confirmed") is True else "",
        )
    except ValidationError as exc:
        logger.error("ORIGINAL_RISK_CONSENT_INVALID")
        raise HTTPException(423, "Original V2 risk sınırları veya yüksek risk onayı geçersiz; yeni giriş kapalı.") from exc
    fixed = strategy.fixed_profile()["execution_policy"]
    symbols = selected_symbols(settings(state))
    if not symbols:
        raise HTTPException(423, "Original V2 günlük sembol listesi hazır değil; yeni girişler kapalı.")
    current = state["policy"]
    result = {**current, **fixed, "max_stop_distance_pct": 6}
    result["allowed_symbols"] = symbols
    for key in ("max_margin_per_trade", "max_positions", "max_same_direction_positions",
                "max_total_exposure_usdt", "daily_trade_limit", "consecutive_loss_limit", "max_spread_bps"):
        result[key] = min(current[key], fixed[key])
    result["max_direction_exposure_usdt"] = current.get("max_direction_exposure_usdt")
    for key in ("allow_long", "allow_short"):
        result[key] = current[key] and fixed[key]
    result["max_trap_score"] = min(current["max_trap_score"], fixed["max_trap_score"])
    result["daily_loss_limit"] = min(current["daily_loss_limit"], fixed["daily_loss_limit"])
    for key in ("min_confidence", "fee_bps_per_side", "slippage_bps_per_side", "minimum_net_reward_usdt"):
        result[key] = max(current[key], fixed[key])
    result["max_loss_per_trade"] = min(float(snapshot["wallet_balance"]) * settings(state)["risk_percent"] / 100,
                                      current["max_loss_per_trade"], fixed["max_loss_per_trade"])
    return result


async def build_spec(client: Any, result: StrategyResult, state: dict[str, Any],
                     snapshot: dict[str, Any], *,
                     check_results: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    from . import v25_execution as live
    symbols = selected_symbols(settings(state))
    try:
        original.validate_live_result(result, symbols)
    except HTTPException as exc:
        detail = str(exc.detail)
        live.record_live_entry_check(check_results, "strategy_signal", False, detail, symbol=result.signal.symbol)
        raise live.LiveExchangeError(detail, http_status=exc.status_code, check_key="strategy_signal") from exc
    if not result.signal.strategy_eligible:
        detail = "Original V2 sinyali girişe uygun değil."
        live.record_live_entry_check(check_results, "strategy_signal", False, detail, symbol=result.signal.symbol)
        raise live.LiveExchangeError(detail, http_status=423, check_key="strategy_signal")
    live.record_live_entry_check(check_results, "strategy_signal", True, "Original V2 sinyali güncel ve girişe uygun.", symbol=result.signal.symbol)
    try:
        spec = await original.build_live_spec(client, result, symbols)
    except HTTPException as exc:
        detail = str(exc.detail)
        live.record_live_entry_check(check_results, "strategy_sizing", False, detail, symbol=result.signal.symbol)
        raise live.LiveExchangeError(detail, http_status=exc.status_code, check_key="strategy_sizing") from exc
    policy = execution_policy(state, snapshot)
    entry, stop = Decimal(spec["entry_price"]), Decimal(spec["stop_loss"])
    budget = Decimal(str(policy["max_loss_per_trade"]))
    rules = await live.live_symbol_rules(client, spec["symbol"], "MARKET")
    # The percentage budget includes estimated commission and slippage, not only stop distance.
    cost_per_unit = entry * Decimal(str(policy["fee_bps_per_side"] + policy["slippage_bps_per_side"])) * 2 / 10000
    native_quantity = Decimal(spec["quantity"])
    risk_quantity = budget / (abs(entry - stop) + cost_per_unit)
    margin_quantity = Decimal(str(policy["max_margin_per_trade"])) * 3 / entry
    unrounded_quantity = min(native_quantity, risk_quantity, margin_quantity)
    quantity = live.floor_step(unrounded_quantity, rules["step"] * 5)
    size_metrics = original.minimum_order_parts(
        quantity, entry, rules["step"], rules["min_qty"], rules["min_notional"],
    )
    if not size_metrics["passes"]:
        live.record_live_entry_check(
            check_results, "minimum_quantity", False,
            "Toplam miktar, TP1 %60 veya kalan miktar Binance minimum miktar/notional şartını karşılamıyor.",
            symbol=spec["symbol"],
        )
        raise live.MinimumTradeSizeError(spec["symbol"], {
            key: size_metrics[key] for key in (
                "total_quantity", "total_notional_usdt", "tp1_quantity", "tp1_notional_usdt",
                "remaining_quantity", "remaining_notional_usdt", "min_qty", "min_notional_usdt",
            )
        })
    live.record_live_entry_check(
        check_results, "minimum_quantity", True,
        "Toplam miktar, TP1 %60 ve kalan miktar Binance minimum miktar/notional şartını karşılıyor.",
        symbol=spec["symbol"],
    )
    margin = float(quantity * entry / 3)
    if margin < 5:
        live.record_live_entry_check(
            check_results, "available_collateral", False,
            "Original V2 risk bütçesi minimum 5 USDT marjini karşılamıyor.",
            symbol=spec["symbol"],
        )
        raise HTTPException(423, "Original V2 risk bütçesi minimum marjini karşılamıyor.")
    live.record_live_entry_check(
        check_results, "available_collateral", True,
        "Hesaplanan Original V2 marjini minimum 5 USDT şartını karşılıyor.",
        symbol=spec["symbol"],
    )
    notional = float(quantity * entry)
    net_reward = float(quantity * abs(Decimal(spec["targets"][0]) - entry) - quantity * cost_per_unit)
    if net_reward < policy["minimum_net_reward_usdt"]:
        live.record_live_entry_check(
            check_results, "risk_reward", False,
            "TP1, ücret ve kayma sonrası minimum net getiri eşiğini karşılamıyor.",
            symbol=spec["symbol"],
        )
        raise HTTPException(423, "Original V2 maliyet veya borsa miktar kapısı geçilmedi.")
    live.record_live_entry_check(
        check_results, "risk_reward", True,
        "TP1 maliyetlerden sonra minimum net getiri eşiğini karşılıyor.",
        symbol=spec["symbol"],
    )
    if quantity > rules["max_qty"]:
        live.record_live_entry_check(
            check_results, "maximum_quantity", False,
            "Hesaplanan miktar Binance sembol üst sınırını aşıyor.",
            symbol=spec["symbol"],
        )
        raise HTTPException(423, "Original V2 maliyet veya borsa miktar kapısı geçilmedi.")
    live.record_live_entry_check(
        check_results, "maximum_quantity", True,
        "Hesaplanan miktar Binance sembol üst sınırını aşmıyor.",
        symbol=spec["symbol"],
    )
    spec.update(quantity=live.decimal_text(quantity), margin_usdt=margin, notional_usdt=float(quantity * entry),
                step=rules["step"], min_qty=rules["min_qty"], min_notional=rules["min_notional"],
                estimated_stop_loss_usdt=float(quantity * (abs(entry - stop) + cost_per_unit)),
                original_risk_budget=float(budget), initial_stop_loss=spec["stop_loss"],
                original_cost_bps=policy["fee_bps_per_side"] * 2 + policy["slippage_bps_per_side"] * 2,
                initial_quantity=live.decimal_text(quantity))
    wallet = risk_settings.verified_wallet(snapshot)
    selected_percent = settings(state)["risk_percent"]
    requested_loss = wallet * selected_percent / 100
    effective_loss = float(spec["estimated_stop_loss_usdt"])
    reasons = []
    if float(budget) < requested_loss - 1e-8:
        reasons.append("Sabit Original V2 / V25 işlem kaybı tavanı")
    requested_quantity = Decimal(str(requested_loss)) / (abs(entry - stop) + cost_per_unit)
    if native_quantity == unrounded_quantity and native_quantity < requested_quantity:
        reasons.append("Original V2 sabit miktar profili (mevcut teminat/kaldıraç sınırları dahil)")
    if margin_quantity == unrounded_quantity and margin_quantity < requested_quantity:
        reasons.append("Mevcut işlem başı teminat tavanı")
    if quantity < unrounded_quantity:
        reasons.append("Binance miktar adımı ve TP miktar bölüşümü için aşağı yuvarlama")
    adjustment = {
        "symbol": spec["symbol"], "selected_percent": selected_percent,
        "effective_percent": effective_loss / wallet * 100,
        "requested_loss_usdt": requested_loss, "effective_loss_usdt": effective_loss,
        "reasons": reasons,
    }
    spec["risk_adjustment"] = adjustment
    settings(state)["last_risk_adjustment"] = adjustment
    loss_budget_passed = float(spec["estimated_stop_loss_usdt"]) <= float(budget) + 1e-8
    live.record_live_entry_check(
        check_results, "risk_budget", loss_budget_passed,
        "Tahmini Original V2 stop kaybı işlem başı risk bütçesi içinde." if loss_budget_passed else "Tahmini Original V2 stop kaybı işlem başı risk bütçesini aşıyor.",
        symbol=spec["symbol"],
    )
    if not loss_budget_passed:
        raise live.LiveExchangeError("Original V2 stop kaybı işlem başı risk bütçesini aşıyor.", http_status=423, check_key="risk_budget")
    if notional > policy["max_margin_per_trade"] * 3 + 1e-8:
        live.record_live_entry_check(
            check_results, "risk_budget", False,
            "Original V2 pozisyon büyüklüğü işlem başı marjin sınırını aşıyor.",
            symbol=spec["symbol"],
        )
        raise HTTPException(423, "Original V2 marjin limiti aşıldı.")
    live.record_live_entry_check(
        check_results, "risk_budget", True,
        "Original V2 pozisyon büyüklüğü işlem başı marjin sınırında.",
        symbol=spec["symbol"],
    )
    return spec


async def check_entry(application: Any, client: Any, state: dict[str, Any],
                      snapshot: dict[str, Any]) -> None:
    from . import v25_execution as live
    check_session_time(state)
    require_fill_unlocked(state)
    if not available() or not running(state) or settings(state).get("paused"):
        raise HTTPException(423, "Original V2 LIVE kapalı veya güvenlik kilidi aktif.")
    await require_owner_authority(application, state)
    if state.get("real_trading_locked") is not False:
        raise HTTPException(423, "Original V2 gerçek işlem kilidi kapalı.")
    if not live.auto_session_active(state) or live.live_execution_blocked(state):
        raise HTTPException(423, "Original V2 LIVE yürütme izni sona erdi.")
    await daily_guard(client, state, snapshot)


async def persist_required(state: dict[str, Any]) -> None:
    from . import v25_execution as live
    try:
        result = await live.persist_state_awaited(state)
    except (OSError, RuntimeError) as exc:
        raise live.LiveExchangeError("Original V2 kalıcı işlem kaydı doğrulanamadı.", http_status=503) from exc
    if result not in {"INSERTED", "UPDATED"}:
        raise live.LiveExchangeError("Original V2 kalıcı işlem kaydı doğrulanamadı.", http_status=503)

async def audit_fill(state: dict[str, Any], plan: dict[str, Any], position: dict[str, Any]) -> None:
    """Check the settled entry once, not the smaller remainder after a TP exit."""
    from . import v25_execution as live
    if plan.get("original_fill_audit"):
        return
    violations = []
    evidence: dict[str, Any] = {}
    try:
        if plan.get("recovered_after_restart"):
            raise ValueError("Recovered intent has no durable settled-fill audit")
        entry = Decimal(str(position.get("entry_price")))
        quantity = Decimal(str(plan["initial_quantity"] if plan.get("original_tp_attempted") else position["quantity"]))
        stop = Decimal(str(plan["initial_stop_loss"]))
        budget = Decimal(str(plan["original_risk_budget"]))
        cost_bps = Decimal(str(plan["original_cost_bps"]))
        leverage = Decimal(str(plan["leverage"]))
        if not all(value.is_finite() for value in (entry, quantity, stop, budget, cost_bps, leverage)) \
                or min(entry, quantity, stop, budget) <= 0 or leverage != 3 or cost_bps < 0:
            raise ValueError("Invalid fill evidence")
        stop_percent = abs(entry - stop) / entry * 100
        risk = quantity * (abs(entry - stop) + entry * cost_bps / 10000)
        margin = quantity * entry / leverage
        evidence = {
            "entry_price": live.decimal_text(entry), "quantity": live.decimal_text(quantity),
            "stop_percent": live.decimal_text(stop_percent), "risk_usdt": live.decimal_text(risk),
            "risk_budget_usdt": live.decimal_text(budget), "margin_usdt": live.decimal_text(margin),
        }
        if stop_percent > 6:
            violations.append("Stop mesafesi %6 sınırını aştı")
        if risk > budget:
            violations.append("Dolum riski işlem risk bütçesini aştı")
        if margin < 5:
            violations.append("Dolum teminatı 5 USDT altına düştü")
        if (plan["direction"] == "LONG" and stop >= entry) or (plan["direction"] == "SHORT" and stop <= entry):
            violations.append("İlk stop gerçekleşen giriş yönüyle uyuşmuyor")
    except (KeyError, TypeError, ValueError, ArithmeticError):
        violations.append("Gerçek dolum risk kanıtı doğrulanamadı")
    audit = {"id": uuid.uuid4().hex, "checked_at": live.now_iso(), "violations": violations, **evidence}
    plan["original_fill_audit"] = audit
    if violations:
        config = settings(state)
        previous = config.get("fill_lock") or {}
        config["fill_lock"] = {
            "id": audit["id"],
            "active": True, "created_at": previous.get("created_at") if previous.get("active") else audit["checked_at"],
            "plan_id": plan["id"], "symbol": plan["symbol"], "violations": violations, "evidence": evidence,
        }
        pause(state, f"{plan['symbol']} dolum güvenlik ihlali: {'; '.join(violations)}. Kilit için OWNER onayı gerekli")
    persisted = False
    try:
        await persist_required(state)
        persisted = True
    finally:
        if not persisted:
            plan.pop("original_fill_audit", None)
            pause(state, f"{plan['symbol']} dolum risk kontrolünün kalıcı kaydı doğrulanamadı")


async def install_targets(client: Any, state: dict[str, Any], plan: dict[str, Any],
                          position: dict[str, Any], existing_algos: list[dict[str, Any]] | None) -> None:
    from . import v25_execution as live
    quantity = Decimal(position["quantity"])
    if not plan.get("original_tp_attempted"):
        plan["initial_quantity"] = live.decimal_text(quantity)
    partial = live.floor_step(Decimal(plan["initial_quantity"]) * Decimal("0.60"), Decimal(plan["step"]))
    if not plan.get("original_tp_attempted") and partial != Decimal(plan["initial_quantity"]) * Decimal("0.60"):
        pause(state, f"{plan['symbol']} doğrulanan dolum tam %60 TP1 bölünmesine uygun değil")
        raise live.LiveExchangeError("Original V2 TP1 tam %60 miktarı kurulamadı; pozisyon kapatılmadı.", http_status=423)
    if not plan.get("original_tp_attempted") and (
        partial < Decimal(plan["min_qty"]) or partial * Decimal(str(position["mark_price"])) < Decimal(plan["min_notional"])
    ):
        pause(state, f"{plan['symbol']} TP1 %60 borsa minimumunu karşılamıyor")
        raise live.LiveExchangeError("Original V2 TP1 miktarı kurulamadı; pozisyon kapatılmadı.", http_status=423)
    common = {"algoType": "CONDITIONAL", "symbol": plan["symbol"],
              "side": "SELL" if plan["direction"] == "LONG" else "BUY",
              "type": "TAKE_PROFIT_MARKET", "workingType": "MARK_PRICE", "priceProtect": "TRUE"}
    ids = [plan["stop_algo_id"]]
    try:
        # Never reallocate 60% of the remainder on a protection repair or restart.
        if not plan.get("original_tp_attempted"):
            plan["original_tp_attempted"] = True
            await persist_required(state)
            result = await live.post_entry_protection_algo(client, {
                **common, "triggerPrice": plan["targets"][0], "quantity": live.decimal_text(partial),
                "reduceOnly": "true", "clientAlgoId": live.client_id_for("TP12", plan["intent_id"]),
            }, existing_algos)
            if not result.get("algoId"):
                raise live.LiveExchangeError("Original V2 TP1 kimliği doğrulanamadı.", unknown_execution=True)
            plan["tp12_algo_id"] = int(result["algoId"])
            plan["tp12_client_id"] = live.client_id_for("TP12", plan["intent_id"])
            await persist_required(state)
        if not plan.get("tp12_algo_id"):
            raise live.LiveExchangeError("Original V2 önceki TP1 kurulumu belirsiz.", unknown_execution=True)
        ids.append(plan["tp12_algo_id"])
        result = await live.post_entry_protection_algo(client, {
            **common, "triggerPrice": plan["targets"][2], "closePosition": "true",
            "clientAlgoId": live.client_id_for("TP3", plan["intent_id"]),
        }, existing_algos)
        if not result.get("algoId"):
            raise live.LiveExchangeError("Original V2 TP3 kimliği doğrulanamadı.", unknown_execution=True)
        plan["original_tp3_algo_id"] = int(result["algoId"])
        ids.append(plan["original_tp3_algo_id"])
    except Exception as exc:
        plan.update(protection_state="MISSING", tp_protection_state="UNPROTECTED",
                    status="STOP AKTİF · ORIGINAL TP DOĞRULANAMADI", last_error=live.sanitized_exception_message(exc))
        pause(state, f"{plan['symbol']} TP kurulamadı: {live.sanitized_exception_message(exc)}")
        raise
    plan.update(protection_ids=ids, monitoring_targets=[], tp_protection_state="EXCHANGE_BACKED",
                protection_state="MATCHED", status="KORUMA AKTİF", protected_at=live.now_iso(),
                entry_remainder_pending=False)
    await persist_required(state)


async def verify_targets(client: Any, state: dict[str, Any], plan: dict[str, Any],
                         rows: list[dict[str, Any]]) -> None:
    from . import v25_execution as live
    if plan.get("stop_loss") != plan.get("initial_stop_loss"):
        pause(state, f"{plan['symbol']} ilk stop değişti")
        raise live.LiveExchangeError("Original V2 ilk stop değiştirilemez.", unknown_execution=True)
    if not plan.get("original_tp_attempted"):
        return
    normalized = [{
        **row,
        "algo_id": row.get("algo_id", row.get("algoId")),
        "client_algo_id": row.get("client_algo_id", row.get("clientAlgoId")),
    } for row in rows]
    for target, identity, quantity in [
        (plan["targets"][2], plan.get("original_tp3_algo_id"), None),
        (plan["targets"][0], plan.get("tp12_algo_id"),
         live.decimal_text(live.floor_step(Decimal(plan["initial_quantity"]) * Decimal("0.60"), Decimal(plan["step"])))),
    ]:
        if not any(row.get("algo_id") == identity for row in normalized):
            continue
        params = {
            "symbol": plan["symbol"], "side": "SELL" if plan["direction"] == "LONG" else "BUY",
            "type": "TAKE_PROFIT_MARKET", "triggerPrice": target, "workingType": "MARK_PRICE",
            "clientAlgoId": live.client_id_for("TP3" if quantity is None else "TP12", plan["intent_id"]),
            **({"closePosition": "true"} if quantity is None else {"quantity": quantity, "reduceOnly": "true"}),
        }
        try:
            match = live.existing_entry_protection_algo(params, normalized)
            if not match or int(match["algoId"]) != identity:
                raise live.LiveExchangeError("Original V2 TP kimliği eşleşmiyor.", unknown_execution=True)
        except live.LiveExchangeError:
            plan["tp_protection_state"] = "UNPROTECTED"
            pause(state, f"{plan['symbol']} TP fiyatı, miktarı veya kimliği değişti")
            raise
    ids = {int(row.get("algo_id", row.get("algoId", 0)) or 0) for row in rows}
    if plan.get("original_tp3_algo_id") not in ids:
        plan["tp_protection_state"] = "UNPROTECTED"
        pause(state, f"{plan['symbol']} TP3 borsada yok")
    if plan.get("tp12_algo_id") in ids or plan.get("original_tp1_complete"):
        return
    if not plan.get("tp12_algo_id"):
        pause(state, f"{plan['symbol']} TP1 kimliği eksik")
        return
    native = await client.signed("GET", "/fapi/v1/algoOrder", {"algoId": plan["tp12_algo_id"]})
    if not isinstance(native, dict) or int(native.get("algoId") or 0) != plan["tp12_algo_id"]:
        raise live.LiveExchangeError("Original V2 TP1 geçmişi doğrulanamadı.", unknown_execution=True)
    actual = int(native.get("actualOrderId") or 0)
    if actual <= 0:
        pause(state, f"{plan['symbol']} TP1 yürütme kanıtı yok")
        return
    order = await client.signed("GET", "/fapi/v1/order", {"symbol": plan["symbol"], "orderId": actual})
    allocation = live.floor_step(Decimal(plan["initial_quantity"]) * Decimal("0.60"), Decimal(plan["step"]))
    if (not isinstance(order, dict) or int(order.get("orderId") or 0) != actual
            or order.get("symbol") != plan["symbol"] or order.get("side") != ("SELL" if plan["direction"] == "LONG" else "BUY")
            or not live._truthy(order.get("reduceOnly")) or order.get("status") != "FILLED"
            or Decimal(str(order.get("executedQty", 0))) != allocation):
        pause(state, f"{plan['symbol']} TP1 %60 dolumu doğrulanamadı")
        return
    plan["original_tp1_complete"] = True
    live.persist_state(state)


def _entry_rejection_category(error: Exception) -> str:
    key = str(getattr(error, "check_key", "") or "").lower()
    detail = str(getattr(error, "detail", error)).lower()
    combined = f"{key} {detail}"
    if any(token in combined for token in ("daily", "günlük", "kayip", "kayıp", "loss limit", "trade limit")):
        return "daily_limit"
    if any(token in combined for token in ("position", "pozisyon", "occupancy", "duplicate", "same_direction")):
        return "position_limit"
    if "spread" in combined:
        return "spread_wide"
    if any(token in combined for token in ("minimum_margin", "margin floor")):
        return "minimum_margin"
    if "minimum_trade_size" in combined or "minimum_quantity" in combined or "notional" in combined:
        return "minimum_trade_size"
    return "risk_or_account_limit"


def _record_order_rejection(
    scanner: dict[str, Any], result: StrategyResult, category: str, reason: str,
    size_metrics: dict[str, str] | None = None,
) -> None:
    counts = scanner.setdefault("rejection_counts", {})
    if not isinstance(counts, dict):
        counts = scanner["rejection_counts"] = {}
    counts[category] = int(counts.get(category, 0)) + 1
    near_misses = scanner.get("near_misses")
    if not isinstance(near_misses, list):
        near_misses = []
    analysis = result.legacy.get("analysis") or {}
    updated = {
        "symbol": result.signal.symbol,
        "reason_code": category,
        "reason": reason,
        "confidence": analysis.get("confidence"),
        "confidence_threshold": next(
            (check.threshold for check in result.signal.quality_checks if check.key == "confidence"),
            None,
        ),
        "failed_checks": [],
        "progress": 5,
        **(size_metrics or {}),
    }
    near_misses = [item for item in near_misses if not isinstance(item, dict) or item.get("symbol") != updated["symbol"]]
    scanner["near_misses"] = [updated, *near_misses][:3]


async def cycle(application: Any, credentials: tuple[str, str] | None) -> None:
    from . import v25_execution as live
    state = application.state.v25_execution
    config = settings(state)
    scanner = config.setdefault("scanner", {})
    scanner["last_cycle_at"] = live.now_iso()
    check_session_time(state)
    if not running(state):
        scanner.update(stage="BLOCKED")
        return
    stored_universe = config.get("universe")
    ranked_symbols = stored_universe.get("ranked_symbols") if isinstance(stored_universe, dict) else None
    if isinstance(ranked_symbols, list):
        cached_symbols = ranked_symbols[:_valid_symbol_count(config)]
        if cached_symbols and all(
            isinstance(symbol, str) and symbol.isascii() and symbol.isalnum()
            and symbol.isupper() and symbol.endswith("USDT")
            for symbol in cached_symbols
        ) and len(set(cached_symbols)) == len(cached_symbols):
            service = getattr(application.state, "public_market_data", None)
            if service is not None:
                service.set_original_symbols(cached_symbols)
            await configure_original_kline_stream(application, cached_symbols)
    if live.account_rate_limit_active(state):
        scanner.update(stage="RATE_LIMIT_WAIT", last_failure={
            "stage": "cooldown", "reason_code": "active_cooldown",
            "message": "Binance istek sınırı bekleniyor; bu döngüde tarama yapılmadı.",
        })
        scanner["last_24h"] = _scanner_history_summary(scanner, time.time())
        return
    global_ban_seconds = live.BINANCE_GLOBAL_BAN.remaining_seconds()
    if global_ban_seconds > 0:
        scanner.update(stage="RATE_LIMIT_WAIT", last_failure={
            "stage": "cooldown", "reason_code": "binance_global_ban",
            "message": f"Binance geçici engeli sürüyor; {global_ban_seconds} saniye bekleniyor.",
        })
        scanner["last_24h"] = _scanner_history_summary(scanner, time.time())
        return
    at = int(time.time()) // 900 * 900
    if config.get("last_decision_time", -1) >= at:
        scanner.update(stage="WAITING_NEXT_CANDLE")
        return
    scan_diagnostics: dict[str, Any] = {}
    try:
        if credentials is None:
            credentials = await live.auto_session_credentials(application, state)
        if not live.usable_live_credentials(credentials):
            raise HTTPException(423, "Original V2 LIVE bağlantısı kesildi.")
        if not live.readiness_for(application, state, credentials=credentials)["ready"]:
            raise HTTPException(423, "Original V2 LIVE yayın kapıları kapalı.")
        client = live.client_for_with_credentials(application, credentials)
        snapshot = await live.account_snapshot(client, cached=True, source="original_strategy")
        await check_entry(application, client, state, snapshot)
        occupied = {
            item["symbol"]
            for item in (
                snapshot.get("positions", [])
                + snapshot.get("open_orders", [])
                + snapshot.get("open_algo_orders", [])
            )
        }
        scanner.update(stage="UNIVERSE_REFRESH")
        symbols = await resolve_live_universe(application, client, config)
        if not symbols:
            config["last_decision_time"] = at
            scanner["rejection_counts"] = {}
            scanner["near_misses"] = []
            _record_scanner_scan(scanner, time.time(), 0)
            scanner.update(stage="NO_ELIGIBLE_SYMBOLS", scanned_symbol_count=0,
                           scanned_symbols=[], candidate_count=0, last_scan_at=live.now_iso(),
                           last_failure=None, last_24h=_scanner_history_summary(scanner, time.time()))
            live.persist_state(state)
            return
        scanner.update(stage="SCANNING")
        sizing_policy = execution_policy(state, snapshot)
        sizing_context = {
            "max_loss_per_trade": sizing_policy["max_loss_per_trade"],
            "max_margin_per_trade": sizing_policy["max_margin_per_trade"],
            "fee_bps_per_side": sizing_policy["fee_bps_per_side"],
            "slippage_bps_per_side": sizing_policy["slippage_bps_per_side"],
        }
        stream = getattr(application.state, "original_kline_stream", None)

        async def fetch_streamed_klines(symbol: str, interval: str, limit: int) -> Any:
            from .binance_kline_stream import KlineStreamUnavailable

            if stream is None or not stream.is_subscribed(symbol, interval):
                raise RuntimeError("Original V2 kline stream is not configured for the active universe")
            try:
                return await stream.get_candles(
                    symbol,
                    interval,
                    limit,
                    lambda params: client.public_get("/fapi/v1/klines", params),
                )
            except KlineStreamUnavailable as exc:
                raise live.LiveRateLimitError(
                    "Binance geçici engeli sürüyor; eksik mum verisi nedeniyle tarama bekletildi.",
                    retry_after=exc.retry_after,
                    exchange_code=-1003,
                ) from exc

        eligible, _results = await original.live_candidates(
            client, at, occupied, symbols, require_complete=True, diagnostics=scan_diagnostics,
            sizing_context=sizing_context,
            kline_fetch=fetch_streamed_klines if stream is not None else None,
        )
        config["last_decision_time"] = at
        history_item = _record_scanner_scan(scanner, time.time(), len(eligible))
        scanner.update(stage="SCAN_COMPLETE", last_scan_at=live.now_iso(),
                       scanned_symbol_count=len(symbols), scanned_symbols=symbols,
                       data_complete_count=scan_diagnostics.get("data_complete_count", 0),
                       candidate_count=len(eligible),
                       rejection_counts=scan_diagnostics.get("rejection_counts", {}),
                       near_misses=scan_diagnostics.get("near_misses", []),
                       last_failure=None,
                       last_24h=_scanner_history_summary(scanner, time.time()))
        for candidate, result in eligible[:3]:
            if not running(state):
                break
            signal = result.legacy["analysis"]
            try:
                preview = await build_spec(client, result, state, snapshot)
                body = live.LiveOrderRequest(
                    symbol=candidate["symbol"], direction=signal["direction"], margin_usdt=preview["margin_usdt"],
                    leverage=3, stop_loss=signal["stop_loss"], tp1=signal["tp1"], tp2=signal["tp2"], tp3=signal["tp3"],
                    intent_id=f"original-live-{config['owner_id']}-{at}-{candidate['symbol']}",
                )
                await live.execute_live_order(
                    application,
                    body,
                    source=SOURCE,
                    credentials=credentials,
                    strategy_result=result,
                )
                history_item["trades"] += 1
            except live.MinimumTradeSizeError as exc:
                category = "minimum_trade_size"
                _record_order_rejection(scanner, result, category, live.sanitized_exception_message(exc),
                                         exc.sizing_metrics)
                logger.warning(
                    "ORIGINAL_LIVE_MINIMUM_TRADE_SIZE_SKIPPED symbol=%s total_quantity=%s total_notional_usdt=%s "
                    "tp1_quantity=%s tp1_notional_usdt=%s remaining_quantity=%s remaining_notional_usdt=%s "
                    "min_qty=%s min_notional_usdt=%s",
                    exc.symbol,
                    *(exc.sizing_metrics[key] for key in (
                        "total_quantity", "total_notional_usdt", "tp1_quantity", "tp1_notional_usdt",
                        "remaining_quantity", "remaining_notional_usdt", "min_qty", "min_notional_usdt",
                    )),
                )
                scanner["candidate_count"] = max(0, int(scanner.get("candidate_count") or 0) - 1)
                history_item["candidates"] = scanner["candidate_count"]
                continue
            except Exception as exc:
                category = _entry_rejection_category(exc)
                _record_order_rejection(
                    scanner, result, category, live.sanitized_exception_message(exc),
                )
                if getattr(exc, "check_key", None) == "symbol_occupancy":
                    config["last_skip_reason"] = "symbol_occupied"
                    scanner.update(stage="SYMBOL_OCCUPIED", last_skip_reason="symbol_occupied")
                    continue
                raise
        scanner["last_24h"] = _scanner_history_summary(scanner, time.time())
        live.persist_state(state)
    except live.LiveRateLimitError as exc:
        failure = scan_diagnostics.get("failure") or {}
        scanner.update(stage="RATE_LIMIT_WAIT", last_failure={
            **failure,
            "stage": "cooldown",
            "reason_code": "binance_rate_limit",
            "message": live.sanitized_exception_message(exc),
        })
        scanner["last_24h"] = _scanner_history_summary(scanner, time.time())
        live.persist_state(state)
        if exc.request_method not in {None, "GET"}:
            pause(state, live.sanitized_exception_message(exc))
        raise
    except Exception as exc:
        if (isinstance(exc, live.LiveExchangeError) and exc.transient_read_failure
                and not exc.unknown_execution and exc.request_path == "/fapi/v1/income"):
            scanner.update(stage="WAITING_RISK_DATA", last_failure={
                "stage": "risk_data",
                "reason_code": "risk_data_unavailable",
                "message": live.sanitized_exception_message(exc),
            })
            scanner["last_24h"] = _scanner_history_summary(scanner, time.time())
            live.persist_state(state)
            raise
        scanner.update(stage="BLOCKED")
        failure = scan_diagnostics.get("failure") or {}
        scanner["last_failure"] = {
            **failure,
            "stage": failure.get("stage", "cycle"),
            "reason_code": failure.get("reason_code") or (
                _entry_rejection_category(exc)
                if isinstance(exc, (HTTPException, live.LiveExchangeError))
                else "cycle_error"
            ),
            "message": live.sanitized_exception_message(exc),
        }
        scanner["last_24h"] = _scanner_history_summary(scanner, time.time())
        pause(state, live.sanitized_exception_message(exc))
        raise


@router.get("")
async def status(request: Request) -> dict[str, Any]:
    authenticated_user(request, owner=True)
    from . import v25_execution as live
    live.guard_execution_ownership(request, claim=False)
    return public_config(request.app.state.v25_execution)


@router.get("/persistence-diagnostics")
async def persistence_diagnostics(request: Request, response: Response = None) -> dict[str, Any]:
    authenticated_user(request, owner=True)
    if response is not None:
        response.headers["Cache-Control"] = "no-store"
    from . import v25_execution as live
    live.guard_execution_ownership(request, claim=False)
    state = request.app.state.v25_execution
    pool = getattr(request.app.state, "db_pool", None)
    result = {
        "read_only": True, "state_key": live.V25_SNAPSHOT_KEY,
        "build_commit": os.getenv("RENDER_GIT_COMMIT") or os.getenv("GIT_COMMIT") or None,
        "database_status": "UNAVAILABLE", "record_exists": None,
        "database_revision": None, "original_record_exists": None,
        "local_revision": state.get("persistence_revision", 0),
        "recovery_loaded": bool(state.get("recovery_loaded")),
        "last_write": getattr(request.app.state, "v25_last_persistence_write", None),
        "last_awaited_write": getattr(request.app.state, "v25_last_awaited_persistence_write", None),
        "diagnosis": "DATABASE_UNAVAILABLE",
    }
    if pool is None:
        return result
    try:
        row = await asyncio.wait_for(pool.fetchrow(
            """SELECT payload->>'persistence_revision' AS revision,
                      jsonb_typeof(payload->'original_v2_live') = 'object' AS original_record_exists
               FROM application_state_snapshots WHERE state_key = $1""", live.V25_SNAPSHOT_KEY), timeout=3)
    except Exception as exc:
        result["database_status"] = "TABLE_MISSING" if getattr(exc, "sqlstate", None) == "42P01" else "READ_FAILED"
        result["diagnosis"] = result["database_status"]
        return result
    result.update(database_status="CONNECTED", record_exists=row is not None, local_revision=state.get("persistence_revision", 0))
    if row is None:
        result.update(original_record_exists=False, diagnosis="FIRST_RECORD_MISSING")
        return result
    revision = row["revision"]
    try:
        revision = 0 if revision is None or revision == "" else int(revision)
        if revision < 0 or revision > 9_223_372_036_854_775_807:
            raise ValueError("Invalid revision")
    except (ValueError, TypeError, OverflowError):
        result["diagnosis"] = "INVALID_DATABASE_REVISION"
        return result
    result.update(database_revision=revision, original_record_exists=bool(row["original_record_exists"]))
    if revision > result["local_revision"]:
        result["diagnosis"] = "DATABASE_AHEAD"
    elif (result["last_awaited_write"] or {}).get("result") == "SKIPPED":
        result["diagnosis"] = "WRITE_SKIPPED_REQUIRES_REVIEW"
    elif (result["last_awaited_write"] or {}).get("result") in {"TABLE_MISSING", "DATABASE_ERROR"}:
        result["diagnosis"] = "PREVIOUS_WRITE_FAILED"
    else:
        result["diagnosis"] = "NO_REVISION_CONFLICT_OBSERVED"
    return result


@router.post("/start")
async def start(request: Request, body: Start) -> dict[str, Any]:
    from . import v25_execution as live
    user = authenticated_user(request, owner=True)
    if not available():
        raise HTTPException(423, "Original V2 LIVE sunucu ayarı kapalı.")
    await live.require_live_understanding(request, user, body.understood)
    state = request.app.state.v25_execution
    async with state["lock"]:
        require_fill_unlocked(state)
        check_session_time(state)
        if live.auto_session_is_active(state) or running(state):
            raise HTTPException(409, "Önce mevcut LIVE otomasyonunu durdurun.")
        allowed, reason = live.live_auto_start_gate(request.app, state, request)
        if not allowed:
            raise HTTPException(423, reason)
        previous = settings(state)
        _, _, fingerprint = live.live_credentials_status(request)
        if not fingerprint:
            raise HTTPException(423, "Original V2 için doğrulanmış LIVE bağlantısı gerekli.")
        if previous and (previous.get("owner_id") != user["id"] or previous.get("fingerprint") != fingerprint):
            raise HTTPException(423, "Original V2 hesap bağlantısı değişti; günlük limit kaydı korunuyor.")
        client = live.client_for(request.app, request)
        try:
            snapshot = await live.account_snapshot(client)
        except (live.LiveExchangeError, live.BinanceDemoError) as exc:
            raise live.safe_exchange_error(exc) from exc
        previous = await risk_settings.save_choice(request, user, state, body, snapshot, fingerprint)
        config = {**previous, "owner_id": user["id"], "risk_percent": body.risk_percent,
                  "fingerprint": fingerprint, "symbol_count": body.symbol_count,
                  "daily_loss_percent": body.daily_loss_percent, "enabled": False, "paused": False}
        scanner = config.setdefault("scanner", {})
        scanner.update(stage="UNIVERSE_PENDING", last_scan_at=None, scanned_symbol_count=0,
                       scanned_symbols=[], candidate_count=0)
        state["original_v2_live"] = config
        try:
            await daily_guard(client, state, snapshot)
            if config.get("paused"):
                raise HTTPException(423, "Original V2 başlatma sırasında durduruldu.")
            await live._start_live_auto_session(
                request, live.Confirmation(understood=True),
                state_lock_held=True,
            )
            started = time.time()
            consent = live.consent_status(state, request)
            if not consent.get("active") or not consent.get("expires_at"):
                raise HTTPException(423, "Original V2 canlı risk izni doğrulanamadı.")
            consent_expiry = datetime.fromisoformat(consent["expires_at"]).timestamp()
            requested_expiry = started + body.session_hours * 3600
            expires = min(requested_expiry, consent_expiry)
            if expires <= started:
                raise HTTPException(423, "Original V2 canlı risk izni süresi doldu.")
            state["auto"].update(session_until=expires, last_decision=f"Original V2 {body.session_hours} saatlik sunucu gözetimli oturumu açıldı; bitiş canlı risk izniyle sınırlıdır.")
            state["auto_authorization"]["expires_at_epoch"] = expires
            config.update(session_hours=body.session_hours, session_id=uuid.uuid4().hex,
                          session_started_at=live.now_iso(), session_expires_at_epoch=expires,
                          session_authority_limited=expires < requested_expiry, session_warning_sent=False)
            config.update(enabled=True, paused=False, pause_reason=None)
            if await live.persist_state_awaited(state) not in {"INSERTED", "UPDATED"}:
                raise HTTPException(503, "Original V2 kalıcı durum kaydı doğrulanamadı.")
        except Exception as exc:
            pause(state, live.sanitized_exception_message(exc))
            if isinstance(exc, (live.LiveExchangeError, live.BinanceDemoError)):
                raise live.safe_exchange_error(exc) from exc
            raise
    return await status(request)

@router.post("/risk-settings")
async def update_risk(request: Request, body: risk_settings.RiskUpdate) -> dict[str, Any]:
    from . import v25_execution as live
    user = authenticated_user(request, owner=True)
    if not available():
        raise HTTPException(423, "Original V2 LIVE sunucu ayarı kapalı.")
    live.execution_owner(request)
    state = request.app.state.v25_execution
    async with state["lock"]:
        if not state.get("recovery_ready"):
            raise HTTPException(503, "Canlı durum kurtarma tamamlanmadı; risk ayarı değiştirilmedi.")
        await live.require_live_understanding(request, user, body.understood)
        _, _, fingerprint = live.live_credentials_status(request)
        if not fingerprint:
            raise HTTPException(423, "Risk ayarı için doğrulanmış LIVE bağlantısı gerekli.")
        client = live.client_for(request.app, request)
        try:
            snapshot = await live.account_snapshot(client)
        except (live.LiveExchangeError, live.BinanceDemoError) as exc:
            raise live.safe_exchange_error(exc) from exc
        await risk_settings.save_choice(request, user, state, body, snapshot, fingerprint)
    return await status(request)

@router.post("/fill-lock/approve")
async def approve_fill_lock(request: Request, body: FillLockApproval) -> dict[str, Any]:
    from . import account_settings, v25_execution as live
    user = authenticated_user(request, owner=True)
    live.guard_execution_ownership(request, claim=False)
    state = request.app.state.v25_execution
    if body.confirmation != "DOLUM RİSKİNİ İNCELEDİM":
        raise HTTPException(422, "Kilidi kaldırmak için DOLUM RİSKİNİ İNCELEDİM yazın.")
    async with state["lock"]:
        config = settings(state)
        lock = config.get("fill_lock") or {}
        if config.get("owner_id") != user["id"]:
            raise HTTPException(403, "Dolum kilidi yalnız ilgili OWNER tarafından kaldırılabilir.")
        if not fill_locked(state) or lock.get("id") != body.lock_id:
            raise HTTPException(409, "Dolum kilidi değişti; güncel durumu inceleyin.")
        await account_settings.require_totp(request, user, body.totp_code, required=True)
        if not fill_locked(state) or config.get("fill_lock", {}).get("id") != body.lock_id:
            raise HTTPException(409, "Dolum kilidi doğrulama sırasında değişti.")
        config["fill_lock"] = {**lock, "active": False, "approved_by": user["id"], "approved_at": live.now_iso()}
        persisted = False
        try:
            await persist_required(state)
            persisted = True
        except live.LiveExchangeError:
            raise HTTPException(503, "Dolum kilidi onayı kalıcı kaydedilemedi; kilit aktif kaldı.") from None
        finally:
            if not persisted and config.get("fill_lock", {}).get("id") == body.lock_id:
                config["fill_lock"] = lock
                live.persist_state(state)
        if config.get("fill_lock", {}).get("id") != body.lock_id or fill_locked(state):
            raise HTTPException(409, "Dolum kilidi kalıcı kayıt sırasında değişti; güncel durumu inceleyin.")
        live.add_event(state, "ORIGINAL_FILL_LOCK_APPROVED", "OWNER dolum riskini inceleyip kilidi kaldırdı; otomasyon başlamadı.",
                       actor=user["id"], lock_id=body.lock_id)
        live.persist_state(state)
    return await status(request)


@router.post("/stop")
async def stop(request: Request) -> dict[str, Any]:
    from . import v25_execution as live
    user = authenticated_user(request, owner=True)
    live.execution_owner(request)
    state = request.app.state.v25_execution
    async with state["lock"]:
        if settings(state).get("owner_id") != user["id"]:
            raise HTTPException(409, "Bu hesap için Original V2 LIVE oturumu yok.")
        pause(state, "OWNER acil durdurma")
        if await live.persist_state_awaited(state) not in {"INSERTED", "UPDATED"}:
            raise HTTPException(503, "Durdurma kalıcı kaydı doğrulanamadı; LIVE durumunu kontrol edin.")
    await configure_original_kline_stream(request.app, [])
    return await status(request)
