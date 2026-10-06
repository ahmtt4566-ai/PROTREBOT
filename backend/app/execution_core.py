"""Pure V25 execution policy helpers.

This module deliberately contains no network or credential code.  It is the
auditable, fail-closed policy layer shared by manual and automatic execution.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


V25_VERSION = "25.0.0"
LIVE_CLIENT_PREFIX = "PTBLV_"
HARD_MAX_MARGIN_USDT = 100.0
HARD_MAX_LEVERAGE = 50
HARD_MAX_POSITIONS = 5
HARD_MAX_DAILY_LOSS_USDT = 100.0
HARD_MAX_DAILY_TRADES = 12
HARD_MAX_CONSECUTIVE_LOSSES = 10
HARD_MAX_TOTAL_EXPOSURE_USDT = 350.0
DEFAULT_MIN_CONFIDENCE = 80
MIN_CONFIDENCE_ENV = "PROTREBOT_MIN_CONFIDENCE"
DEFAULT_MTF_ALLOW_EITHER_TIMEFRAME = False
MTF_ALLOW_EITHER_TIMEFRAME_ENV = "PROTREBOT_MTF_ALLOW_EITHER_TIMEFRAME"


DEFAULT_EXECUTION_POLICY: dict[str, Any] = {
    "allowed_symbols": ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"],
    "interval": "15m",
    "allow_long": True,
    "allow_short": True,
    "max_margin_per_trade": 25.0,
    "max_loss_per_trade": 3.0,
    "max_leverage": 30,
    "max_positions": 5,
    "max_same_direction_positions": 2,
    "max_direction_exposure_usdt": None,
    "max_total_exposure_usdt": 350.0,
    "daily_loss_limit": 10.0,
    "daily_trade_limit": 3,
    "consecutive_loss_limit": 3,
    "min_confidence": DEFAULT_MIN_CONFIDENCE,
    "mtf_allow_either_timeframe": DEFAULT_MTF_ALLOW_EITHER_TIMEFRAME,
    "max_trap_score": 35,
    "max_spread_bps": 8.0,
    "max_stop_distance_pct": 5.0,
    "atr_stop_multiplier": 1.5,
    "liquidation_buffer_pct": 0.5,
    "fee_bps_per_side": 5.0,
    "slippage_bps_per_side": 3.0,
    "minimum_net_reward_usdt": 0.25,
    "scan_seconds": 120,
    "require_one_way": True,
    "require_isolated": True,
    "stop_required": True,
}


def _number(value: Any, default: float, low: float, high: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return min(high, max(low, parsed))


def _integer(value: Any, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return min(high, max(low, parsed))


def configured_min_confidence() -> int:
    return _integer(os.getenv(MIN_CONFIDENCE_ENV), DEFAULT_MIN_CONFIDENCE, 70, 95)


def configured_mtf_allow_either_timeframe() -> bool:
    value = str(os.getenv(MTF_ALLOW_EITHER_TIMEFRAME_ENV, "")).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return DEFAULT_MTF_ALLOW_EITHER_TIMEFRAME


def normalize_live_symbol(value: str) -> str:
    symbol = re.sub(r"[^A-Z0-9]", "", str(value).upper())
    if not symbol.endswith("USDT") or not 5 <= len(symbol) <= 20:
        raise ValueError("Yalnızca USDT vadeli işlem pariteleri destekleniyor")
    return symbol


def sanitize_execution_policy(payload: Any, *, preserve_empty_allowed_symbols: bool = False) -> dict[str, Any]:
    source = payload if isinstance(payload, dict) else {}
    base = dict(DEFAULT_EXECUTION_POLICY)
    symbols: list[str] = []
    for raw in source.get("allowed_symbols", base["allowed_symbols"]):
        try:
            symbol = normalize_live_symbol(str(raw))
        except ValueError:
            continue
        if symbol not in symbols:
            symbols.append(symbol)
        if len(symbols) >= 8:
            break
    base["allowed_symbols"] = symbols if preserve_empty_allowed_symbols else symbols or list(DEFAULT_EXECUTION_POLICY["allowed_symbols"])
    interval = str(source.get("interval", base["interval"]))
    base["interval"] = interval if interval in {"1m", "5m", "15m", "1h", "4h"} else "15m"
    for name in ("allow_long", "allow_short", "require_one_way", "require_isolated", "stop_required"):
        base[name] = bool(source.get(name, base[name]))
    base["max_margin_per_trade"] = _number(source.get("max_margin_per_trade"), 25, 5, HARD_MAX_MARGIN_USDT)
    base["max_loss_per_trade"] = _number(source.get("max_loss_per_trade"), 3, 0.5, 25)
    base["max_leverage"] = _integer(source.get("max_leverage"), 30, 1, HARD_MAX_LEVERAGE)
    base["max_positions"] = _integer(source.get("max_positions"), 5, 1, HARD_MAX_POSITIONS)
    base["max_same_direction_positions"] = _integer(source.get("max_same_direction_positions"), 2, 1, HARD_MAX_POSITIONS)
    direction_cap = source.get("max_direction_exposure_usdt")
    if direction_cap is not None:
        try:
            direction_cap = float(direction_cap)
        except (TypeError, ValueError) as exc:
            logger.error("Invalid max_direction_exposure_usdt policy value")
            raise ValueError("Invalid max_direction_exposure_usdt") from exc
        if not math.isfinite(direction_cap) or not 0 < direction_cap <= HARD_MAX_TOTAL_EXPOSURE_USDT:
            logger.error("max_direction_exposure_usdt outside policy bounds")
            raise ValueError("max_direction_exposure_usdt must be positive and at most 350")
    base["max_direction_exposure_usdt"] = direction_cap
    base["max_total_exposure_usdt"] = _number(source.get("max_total_exposure_usdt"), 350, 25, HARD_MAX_TOTAL_EXPOSURE_USDT)
    base["daily_loss_limit"] = _number(source.get("daily_loss_limit"), 10, 5, HARD_MAX_DAILY_LOSS_USDT)
    base["daily_trade_limit"] = _integer(source.get("daily_trade_limit"), 3, 1, HARD_MAX_DAILY_TRADES)
    base["consecutive_loss_limit"] = _integer(source.get("consecutive_loss_limit"), 3, 1, HARD_MAX_CONSECUTIVE_LOSSES)
    base["min_confidence"] = _integer(source.get("min_confidence"), configured_min_confidence(), 70, 95)
    base["mtf_allow_either_timeframe"] = bool(source.get("mtf_allow_either_timeframe", configured_mtf_allow_either_timeframe()))
    base["max_trap_score"] = _integer(source.get("max_trap_score"), 35, 10, 60)
    base["max_spread_bps"] = _number(source.get("max_spread_bps"), 8, 0.5, 25)
    base["max_stop_distance_pct"] = _number(source.get("max_stop_distance_pct"), 5.0, 0.25, 5)
    base["atr_stop_multiplier"] = _number(source.get("atr_stop_multiplier"), 1.5, 0.5, 3)
    base["liquidation_buffer_pct"] = _number(source.get("liquidation_buffer_pct"), 0.5, 0.05, 5)
    base["fee_bps_per_side"] = _number(source.get("fee_bps_per_side"), 5, 0, 25)
    base["slippage_bps_per_side"] = _number(source.get("slippage_bps_per_side"), 3, 0, 30)
    base["minimum_net_reward_usdt"] = _number(source.get("minimum_net_reward_usdt"), 0.25, 0, 25)
    base["scan_seconds"] = _integer(source.get("scan_seconds"), 120, 30, 300)
    if not base["allow_long"] and not base["allow_short"]:
        base["allow_long"] = True
    return base


def policy_digest(policy: dict[str, Any]) -> str:
    clean = sanitize_execution_policy(policy)
    body = json.dumps(clean, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(body).hexdigest()[:16]


def credential_fingerprint(api_key: str) -> str | None:
    clean = str(api_key or "").strip()
    if len(clean) < 10:
        return None
    return hashlib.sha256(clean.encode("utf-8")).hexdigest()[:12].upper()


def daily_execution_metrics(events: list[dict[str, Any]], now: datetime | None = None) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    day = current.astimezone(timezone.utc).date().isoformat()
    rows = [item for item in events if str(item.get("created_at", "")).startswith(day)]
    entries = [item for item in rows if item.get("kind") in {"LIVE_ENTRY", "LIVE_ENTRY_RECOVERED"}]
    realized = sum(float(item.get("realized_pnl") or 0) for item in rows if item.get("kind") == "LIVE_POSITION_CLOSED")
    verified_plan_ids = {
        str(item.get("plan_id")) for item in events
        if item.get("kind") == "LIVE_POSITION_CLOSED" and item.get("plan_id")
    }
    unverified_plan_ids = {
        str(item.get("plan_id")) for item in events
        if item.get("kind") == "LIVE_POSITION_CLOSED_UNVERIFIED" and item.get("plan_id")
    } - verified_plan_ids
    closed_rows = sorted(
        (item for item in rows if item.get("kind") == "LIVE_POSITION_CLOSED"),
        key=lambda item: str(item.get("created_at", "")),
        reverse=True,
    )
    consecutive_losses = 0
    for item in closed_rows:
        if float(item.get("realized_pnl") or 0) < 0:
            consecutive_losses += 1
        else:
            break
    return {
        "date": day,
        "entries": len(entries),
        "realized_pnl": round(realized, 6),
        "unverified_closures": len(unverified_plan_ids),
        "consecutive_losses": consecutive_losses,
        "events": len(rows),
    }


def dynamic_stop_distance_pct(entry: float, atr: float | None, policy: dict[str, Any]) -> float:
    settings = sanitize_execution_policy(policy)
    configured_cap = float(settings["max_stop_distance_pct"])
    if atr is None or float(atr) <= 0 or entry <= 0:
        return configured_cap
    atr_pct = abs(float(atr)) / float(entry) * 100
    return min(configured_cap, 5.0, max(1.0, atr_pct * float(settings["atr_stop_multiplier"])))


def risk_sized_order(entry: float, stop: float, policy: dict[str, Any], *, atr: float | None = None) -> dict[str, Any]:
    settings = sanitize_execution_policy(policy)
    if entry <= 0 or stop <= 0 or entry == stop:
        raise ValueError("Giriş ve Stop sıfırdan büyük ve birbirinden farklı olmalı")
    stop_pct = abs(entry - stop) / entry * 100
    stop_cap = dynamic_stop_distance_pct(entry, atr, settings)
    if stop_pct > stop_cap + 1e-9:
        raise ValueError(f"Stop mesafesi %{stop_pct:.2f}; izin verilen üst sınır %{stop_cap:.2f}")
    risk_fraction = stop_pct / 100
    risk_notional = float(settings["max_loss_per_trade"]) / risk_fraction
    leverage = int(settings["max_leverage"])
    max_notional = float(settings["max_margin_per_trade"]) * leverage
    notional = min(risk_notional, max_notional)
    margin = notional / leverage
    return {
        "entry": round(entry, 10),
        "stop": round(stop, 10),
        "stop_distance_pct": round(stop_pct, 5),
        "max_stop_distance_pct": round(stop_cap, 5),
        "atr": round(float(atr), 10) if atr is not None else None,
        "leverage": leverage,
        "notional_usdt": round(notional, 6),
        "margin_usdt": round(margin, 6),
        "estimated_stop_loss_usdt": round(notional * risk_fraction, 6),
        "capped": notional + 1e-9 < risk_notional,
    }


@dataclass(frozen=True)
class GateResult:
    passed: bool
    key: str
    label: str
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "key": self.key, "label": self.label, "detail": self.detail}


def _finite_value(row: dict[str, Any], *names: str) -> float | None:
    for name in names:
        if row.get(name) is None:
            continue
        if isinstance(row[name], bool):
            return None
        try:
            value = float(row[name])
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) else None
    return None


def position_notional_usdt(position: dict[str, Any]) -> float:
    quantity = _finite_value(position, "quantity", "positionAmt")
    if quantity == 0:
        return 0.0
    if any(key in position for key in ("quantity", "positionAmt", "mark_price", "markPrice")):
        mark = _finite_value(position, "mark_price", "markPrice")
        if quantity is None or mark is None or mark <= 0:
            raise ValueError("Position quantity or mark price is unavailable")
        notional = abs(quantity) * mark
    else:
        notional = _finite_value(position, "notional", "notional_usdt")
        if notional is None:
            raise ValueError("Position notional is unavailable")
        notional = abs(notional)
    if not math.isfinite(notional):
        raise ValueError("Position notional is not finite")
    return notional


def _closing_order(row: dict[str, Any]) -> bool:
    return any(str(row.get(key, "")).lower() in {"true", "1"} for key in (
        "reduce_only", "reduceOnly", "close_position", "closePosition",
    ))


def directional_entry_gates(
    *,
    symbol: str,
    direction: str,
    snapshot: dict[str, Any],
    policy: dict[str, Any],
    active_plans: list[dict[str, Any]] | None = None,
    cycle_candidates: list[dict[str, Any]] | None = None,
    candidate_notional_usdt: float = 0.0,
) -> list[GateResult]:
    settings = sanitize_execution_policy(policy)
    positions = snapshot.get("positions") or []
    orders = snapshot.get("open_orders") or []
    reservations = cycle_candidates or []
    prices: dict[str, float] = {}
    for row in [*(active_plans or []), *reservations, *positions]:
        price = _finite_value(row, "mark_price") or _finite_value(row, "entry_price", "entryPrice")
        if price is not None and price > 0:
            prices[str(row.get("symbol", "")).upper()] = price

    physical: dict[str, float | None] = {}
    reserved: dict[str, float | None] = {}
    for rows, pending, target in ((positions, False, physical), (orders, True, physical), (reservations, False, reserved)):
        for index, row in enumerate(rows):
            if pending and (_closing_order(row) or str(row.get("status", "")).upper() in {
                "FILLED", "CANCELED", "CANCELLED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH",
            }):
                continue
            row_direction = str(row.get("direction") or row.get("position_side") or "").upper()
            if row_direction not in {"LONG", "SHORT"}:
                side = str(row.get("side") or "").upper()
                row_direction = {"BUY": "LONG", "SELL": "SHORT", "LONG": "LONG", "SHORT": "SHORT"}.get(side, "")
                signed_quantity = _finite_value(row, "positionAmt")
                if not row_direction and signed_quantity:
                    row_direction = "LONG" if signed_quantity > 0 else "SHORT"
            if row_direction and row_direction != direction:
                continue
            quantity = _finite_value(row, "quantity", "origQty", "positionAmt")
            if quantity is not None:
                quantity = abs(quantity)
                if pending:
                    executed = _finite_value(row, "executed_quantity", "executedQty")
                    if executed is None and not any(key in row for key in ("executed_quantity", "executedQty")):
                        executed = 0.0
                    quantity = max(0.0, quantity - executed) if executed is not None and executed >= 0 else None
                if quantity == 0:
                    continue
            row_symbol = str(row.get("symbol") or f"UNKNOWN_{id(rows)}_{index}").upper()
            if pending:
                price = _finite_value(row, "price")
                if price is None or price <= 0:
                    price = prices.get(row_symbol)
                notional = quantity * price if quantity is not None and price is not None and price > 0 else None
            else:
                notional = _finite_value(row, "notional", "notional_usdt")
                if notional is None or notional == 0:
                    price = prices.get(row_symbol)
                    notional = quantity * price if quantity is not None and price is not None else None
                if notional is not None:
                    notional = abs(notional)
            previous = target.get(row_symbol, 0.0)
            target[row_symbol] = previous + notional if previous is not None and notional is not None else None

    # A reservation covers snapshot lag; once visible, it is not a second position.
    slots = physical.keys() | reserved.keys()
    amounts = [
        None if physical.get(key, 0.0) is None or reserved.get(key, 0.0) is None
        else max(physical.get(key, 0.0) or 0.0, reserved.get(key, 0.0) or 0.0)
        for key in slots
    ]
    count = len(slots) + (str(symbol).upper() not in slots)
    cap = settings["max_direction_exposure_usdt"]
    candidate = _finite_value({"value": candidate_notional_usdt}, "value")
    known = all(value is not None and math.isfinite(value) for value in amounts) and candidate is not None and candidate >= 0
    projected = sum(value for value in amounts if value is not None) + (candidate or 0.0)
    checks = [
        GateResult(direction not in {"LONG", "SHORT"} or count <= settings["max_same_direction_positions"],
                   "same_direction_positions", "Aynı yön pozisyon sınırı",
                   f"{direction} aynı yön pozisyon sınırı: {count} / {settings['max_same_direction_positions']}"),
        GateResult(cap is None or (known and math.isfinite(projected) and projected <= cap + 1e-9),
                   "direction_exposure", "Yön başına maruziyet sınırı",
                   "Yön başına maruziyet sınırı kapalı." if cap is None else
                   f"{direction} maruziyeti doğrulanamadı; yeni giriş reddedildi." if not known else
                   f"{direction} maruziyet sınırı: {projected:.2f} / {cap:.2f} USDT"),
    ]
    for gate in checks:
        if not gate.passed:
            logger.info("DIRECTIONAL_ENTRY_BLOCKED symbol=%s direction=%s gate=%s reason=%s", symbol, direction, gate.key, gate.detail)
    return checks


def evaluate_entry_gates(
    *,
    symbol: str,
    signal: dict[str, Any],
    snapshot: dict[str, Any],
    policy: dict[str, Any],
    daily: dict[str, Any],
    spread_bps: float,
    armed: bool,
    allowed_symbols: list[str] | None = None,
    active_plans: list[dict[str, Any]] | None = None,
    candidate_notional_usdt: float = 0.0,
    cycle_candidates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    settings = sanitize_execution_policy(policy)
    safe_symbol = normalize_live_symbol(symbol)
    symbol_scope = allowed_symbols if allowed_symbols is not None else settings["allowed_symbols"]
    direction = str(signal.get("direction") or "BEKLE").upper()
    confidence = int(signal.get("confidence") or 0)
    radar = signal.get("radar") if isinstance(signal.get("radar"), dict) else {}
    trap_score = int(radar.get("trap_score") if radar.get("trap_score") is not None else 100)
    positions = snapshot.get("positions", []) if isinstance(snapshot.get("positions"), list) else []
    orders = snapshot.get("open_orders", []) if isinstance(snapshot.get("open_orders"), list) else []
    plans = active_plans if isinstance(active_plans, list) else []
    exposure_known = True
    existing_exposure = 0.0
    try:
        for item in positions:
            if not isinstance(item, dict):
                raise ValueError("Position payload is invalid")
            existing_exposure += position_notional_usdt(item)
    except ValueError as exc:
        logger.error("LIVE_ENTRY_EXPOSURE_UNKNOWN: %s", exc)
        exposure_known = False
    existing_plan_match = any(
        isinstance(item, dict)
        and item.get("symbol") == safe_symbol
        and str(item.get("direction") or "").upper() == direction
        and item.get("status") not in {"KAPANDI", "İPTAL", "CLOSED_FOR_SECURITY"}
        for item in plans
    )
    gates = [
        GateResult(armed, "arm", "Süreli canlı kilit", "Canlı kilit yalnızca kısa süreli kullanıcı onayıyla açılır."),
        GateResult(not symbol_scope or safe_symbol in symbol_scope, "symbol", "Parite izin listesi", safe_symbol),
        GateResult(direction in {"LONG", "SHORT"}, "direction", "Net yön", direction),
        GateResult(direction != "LONG" or settings["allow_long"], "long", "LONG izni", "Açık" if settings["allow_long"] else "Kapalı"),
        GateResult(direction != "SHORT" or settings["allow_short"], "short", "SHORT izni", "Açık" if settings["allow_short"] else "Kapalı"),
        GateResult(confidence >= settings["min_confidence"], "confidence", "Güven eşiği", f"%{confidence} / ≥ %{settings['min_confidence']}"),
        GateResult(trap_score <= settings["max_trap_score"], "trap", "Tuzak radarı", f"%{trap_score} / ≤ %{settings['max_trap_score']}"),
        GateResult(spread_bps <= settings["max_spread_bps"], "spread", "Spread", f"{spread_bps:.2f} bp / ≤ {settings['max_spread_bps']:.2f} bp"),
        GateResult(not snapshot.get("hedge_mode", False), "one_way", "One-way pozisyon modu", "Hedge kapalı olmalı."),
        GateResult(len(positions) < settings["max_positions"], "positions", "Pozisyon sınırı", f"{len(positions)} / {settings['max_positions']}"),
        GateResult(not any(item.get("symbol") == safe_symbol for item in positions + orders), "duplicate", "Yinelenen parite", "Aynı paritede açık pozisyon/emir bulunmamalı."),
        GateResult(not existing_plan_match, "active_plan", "Aktif plan çakışması", "Aynı parite ve yönde aktif plan bulunmamalı."),
        GateResult(exposure_known and existing_exposure + max(0.0, float(candidate_notional_usdt)) <= float(settings["max_total_exposure_usdt"]), "exposure", "Toplam maruziyet sınırı",
                   f"{existing_exposure + max(0.0, float(candidate_notional_usdt)):.2f} / {settings['max_total_exposure_usdt']:.2f} USDT" if exposure_known else
                   "Mevcut pozisyon maruziyeti doğrulanamadı; yeni giriş reddedildi."),
        GateResult(int(daily.get("entries", 0)) < settings["daily_trade_limit"], "daily_trades", "Günlük işlem sınırı", f"{daily.get('entries', 0)} / {settings['daily_trade_limit']}"),
        GateResult(float(daily.get("realized_pnl", 0)) > -float(settings["daily_loss_limit"]), "daily_loss", "Günlük kayıp kilidi", f"{daily.get('realized_pnl', 0):.2f} USDT"),
        GateResult(float(snapshot.get("unrealized_pnl") or 0) > -float(settings["daily_loss_limit"]), "open_loss", "Açık zarar kilidi", f"{float(snapshot.get('unrealized_pnl') or 0):.2f} USDT"),
        GateResult(int(daily.get("unverified_closures", 0)) == 0, "pnl_verified", "Kesinleşmiş PnL", f"Doğrulanmamış kapanış: {daily.get('unverified_closures', 0)}"),
        GateResult(int(daily.get("consecutive_losses", 0)) < settings["consecutive_loss_limit"], "consecutive_losses", "Ardışık kayıp kilidi", f"{daily.get('consecutive_losses', 0)} / {settings['consecutive_loss_limit']} kayıp"),
    ]
    gates.extend(directional_entry_gates(
        symbol=safe_symbol, direction=direction, snapshot=snapshot, policy=settings,
        active_plans=plans, cycle_candidates=cycle_candidates,
        candidate_notional_usdt=candidate_notional_usdt,
    ))
    failed = [gate for gate in gates if not gate.passed]
    return {
        "passed": not failed,
        "decision": direction if not failed else "BEKLE",
        "reason": "Tüm canlı giriş kapıları geçti." if not failed else failed[0].detail,
        "gates": [gate.as_dict() for gate in gates],
    }


def release_gates(
    *,
    credentials: bool,
    consent_active: bool,
    connected: bool,
    one_way: bool,
    policy_acknowledged: bool,
    demo_certificate: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    certificate = demo_certificate or {}
    checks = [
        GateResult(credentials, "credentials", "Yerel canlı anahtar kasası", "API ve Secret yalnızca Windows DPAPI kasasında."),
        GateResult(consent_active, "local_consent", "24 saatlik yerel canlı izin", "CANLI-ISLEM-IZNI.bat ile bu cihazda verilmelidir."),
        GateResult(connected, "read_only", "Canlı hesap salt-okunur bağlantı", "Bakiye ve pozisyon modu imzalı API ile doğrulanır."),
        GateResult(one_way, "one_way", "One-way pozisyon modu", "Hedge modu kapalı olmalıdır."),
        GateResult(policy_acknowledged, "policy", "Risk politikası onayı", "Limitler değiştiğinde onay yeniden alınır."),
        GateResult(
            certificate.get("status") == "DEMO SERTİFİKALI" or certificate.get("live_allowed") is True,
            "demo_certificate",
            "30 gün / 100 Demo işlem kanıtı",
            "LIVE için Demo sertifikası açıkça waiver edildi." if certificate.get("live_allowed") is True else f"Demo sertifika puanı %{certificate.get('score', 0)}.",
        ),
    ]
    return [item.as_dict() for item in checks]


def release_ready(gates: list[dict[str, Any]]) -> bool:
    return bool(gates) and all(bool(item.get("passed")) for item in gates)
logger = logging.getLogger(__name__)
