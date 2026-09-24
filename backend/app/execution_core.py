"""Pure V25 execution policy helpers.

This module deliberately contains no network or credential code.  It is the
auditable, fail-closed policy layer shared by manual and automatic execution.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


V25_VERSION = "25.0.0"
LIVE_CLIENT_PREFIX = "PTBLV_"
HARD_MAX_MARGIN_USDT = 100.0
HARD_MAX_LEVERAGE = 3
HARD_MAX_POSITIONS = 5
HARD_MAX_DAILY_LOSS_USDT = 100.0
HARD_MAX_DAILY_TRADES = 12
HARD_MAX_CONSECUTIVE_LOSSES = 10
HARD_MAX_TOTAL_EXPOSURE_USDT = 250.0
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
    "max_leverage": 2,
    "max_positions": 5,
    "max_total_exposure_usdt": 100.0,
    "daily_loss_limit": 10.0,
    "daily_trade_limit": 3,
    "consecutive_loss_limit": 3,
    "min_confidence": DEFAULT_MIN_CONFIDENCE,
    "mtf_allow_either_timeframe": DEFAULT_MTF_ALLOW_EITHER_TIMEFRAME,
    "max_trap_score": 35,
    "max_spread_bps": 8.0,
    "max_stop_distance_pct": 5.0,
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


def sanitize_execution_policy(payload: Any) -> dict[str, Any]:
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
    base["allowed_symbols"] = symbols or list(DEFAULT_EXECUTION_POLICY["allowed_symbols"])
    interval = str(source.get("interval", base["interval"]))
    base["interval"] = interval if interval in {"1m", "5m", "15m", "1h", "4h"} else "15m"
    for name in ("allow_long", "allow_short", "require_one_way", "require_isolated", "stop_required"):
        base[name] = bool(source.get(name, base[name]))
    base["max_margin_per_trade"] = _number(source.get("max_margin_per_trade"), 25, 5, HARD_MAX_MARGIN_USDT)
    base["max_loss_per_trade"] = _number(source.get("max_loss_per_trade"), 3, 0.5, 25)
    base["max_leverage"] = _integer(source.get("max_leverage"), 2, 1, HARD_MAX_LEVERAGE)
    base["max_positions"] = _integer(source.get("max_positions"), 5, 1, HARD_MAX_POSITIONS)
    base["max_total_exposure_usdt"] = _number(source.get("max_total_exposure_usdt"), 100, 25, HARD_MAX_TOTAL_EXPOSURE_USDT)
    base["daily_loss_limit"] = _number(source.get("daily_loss_limit"), 10, 5, HARD_MAX_DAILY_LOSS_USDT)
    base["daily_trade_limit"] = _integer(source.get("daily_trade_limit"), 3, 1, HARD_MAX_DAILY_TRADES)
    base["consecutive_loss_limit"] = _integer(source.get("consecutive_loss_limit"), 3, 1, HARD_MAX_CONSECUTIVE_LOSSES)
    base["min_confidence"] = _integer(source.get("min_confidence"), configured_min_confidence(), 70, 95)
    base["mtf_allow_either_timeframe"] = bool(source.get("mtf_allow_either_timeframe", configured_mtf_allow_either_timeframe()))
    base["max_trap_score"] = _integer(source.get("max_trap_score"), 35, 10, 60)
    base["max_spread_bps"] = _number(source.get("max_spread_bps"), 8, 0.5, 25)
    base["max_stop_distance_pct"] = _number(source.get("max_stop_distance_pct"), 5.0, 0.25, 5)
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
    closed_rows = [item for item in rows if item.get("kind") == "LIVE_POSITION_CLOSED"]
    consecutive_losses = 0
    for item in reversed(closed_rows):
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


def risk_sized_order(entry: float, stop: float, policy: dict[str, Any]) -> dict[str, Any]:
    settings = sanitize_execution_policy(policy)
    if entry <= 0 or stop <= 0 or entry == stop:
        raise ValueError("Giriş ve Stop sıfırdan büyük ve birbirinden farklı olmalı")
    stop_pct = abs(entry - stop) / entry * 100
    if stop_pct > float(settings["max_stop_distance_pct"]) + 1e-9:
        raise ValueError(f"Stop mesafesi %{stop_pct:.2f}; izin verilen üst sınır %{settings['max_stop_distance_pct']:.2f}")
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
    existing_exposure = sum(
        abs(float(item.get("notional") or item.get("notional_usdt") or 0))
        for item in positions
        if isinstance(item, dict)
    )
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
        GateResult(existing_exposure + max(0.0, float(candidate_notional_usdt)) <= float(settings["max_total_exposure_usdt"]), "exposure", "Toplam maruziyet sınırı", f"{existing_exposure + max(0.0, float(candidate_notional_usdt)):.2f} / {settings['max_total_exposure_usdt']:.2f} USDT"),
        GateResult(int(daily.get("entries", 0)) < settings["daily_trade_limit"], "daily_trades", "Günlük işlem sınırı", f"{daily.get('entries', 0)} / {settings['daily_trade_limit']}"),
        GateResult(float(daily.get("realized_pnl", 0)) > -float(settings["daily_loss_limit"]), "daily_loss", "Günlük kayıp kilidi", f"{daily.get('realized_pnl', 0):.2f} USDT"),
        GateResult(float(snapshot.get("unrealized_pnl") or 0) > -float(settings["daily_loss_limit"]), "open_loss", "Açık zarar kilidi", f"{float(snapshot.get('unrealized_pnl') or 0):.2f} USDT"),
        GateResult(int(daily.get("unverified_closures", 0)) == 0, "pnl_verified", "Kesinleşmiş PnL", f"Doğrulanmamış kapanış: {daily.get('unverified_closures', 0)}"),
        GateResult(int(daily.get("consecutive_losses", 0)) < settings["consecutive_loss_limit"], "consecutive_losses", "Ardışık kayıp kilidi", f"{daily.get('consecutive_losses', 0)} / {settings['consecutive_loss_limit']} kayıp"),
    ]
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
