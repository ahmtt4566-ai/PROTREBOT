"""Pure shadow gates and feature projection, used only by the observer worker."""

from __future__ import annotations

from typing import Any

from .analysis import analyze
from .execution_core import evaluate_entry_gates, risk_sized_order, sanitize_execution_policy
from .liquidation_risk import isolated_liquidation_risk
from .signal_journal import Journal, number, signal_id


def build_record(round_data: dict[str, Any], candidate: dict[str, Any], journal: Journal) -> dict[str, Any]:
    canonical = candidate.get("canonical") or {}
    signal = canonical.get("analysis") or {}
    mtf = canonical.get("mtf") or {}
    direction = str(signal.get("direction") or "UNKNOWN").upper()
    timestamp = number(canonical.get("signal_timestamp"))
    close_time = int(timestamp) + 900 if timestamp is not None else int(candidate.get("decision_time") or round_data["ts_decision"])
    policy = sanitize_execution_policy(round_data["policy"], preserve_empty_allowed_symbols=True)
    frames = mtf.get("timeframes") or {}
    ema, bb, radar = (signal.get(key) or {} for key in ("ema", "bollinger", "radar"))
    scores: dict[str, int] = {}
    primary = candidate.get("candles") or []
    closed = [row for row in primary if number(row.get("time")) is not None and row["time"] + 900 <= close_time]
    if len(closed) >= 220:
        analyze(closed, observation_scores=scores)
    middle = number(bb.get("middle"))
    upper, lower = number(bb.get("upper")), number(bb.get("lower"))
    record = {
        "signal_id": signal_id(candidate["symbol"], direction, close_time),
        "ts_decision": close_time, "decision_time_verified": timestamp is not None,
        "ts_observed": round_data["ts_scan"], "symbol": candidate["symbol"], "direction": direction,
        "long_score": scores.get("long_score"), "short_score": scores.get("short_score"),
        "confidence": number(signal.get("confidence")), "trap": number(radar.get("trap_score")),
        "breakout": number(radar.get("breakout_quality")), "mtf_alignment": number(mtf.get("alignment")),
        "alignment_15m": number((frames.get("15m") or {}).get("confidence")),
        "alignment_1h": number((frames.get("1h") or {}).get("confidence")),
        "alignment_4h": number((frames.get("4h") or {}).get("confidence")),
        "mtf_directions": {key: (frames.get(key) or {}).get("direction") for key in ("15m", "1h", "4h")},
        "ema20": number(ema.get("ema20")), "ema50": number(ema.get("ema50")), "ema200": number(ema.get("ema200")),
        "macd_hist": number(signal.get("macd")), "rsi": number(signal.get("rsi")),
        "atr": number(signal.get("atr")), "adx": number(signal.get("adx")),
        "bb_width": (upper - lower) / middle if middle and upper is not None and lower is not None else None,
        "vol_ratio": number(signal.get("volume_ratio")), "spread_bp": number(candidate.get("spread_bp")),
        "quote_volume_24h": number(candidate.get("quote_volume_24h")),
        "price_change_pct_24h": number(candidate.get("price_change_pct_24h")),
        "market_price": number(candidate.get("market_price")),
        "funding_rate": number(candidate.get("funding_rate")),
        "entry": number(signal.get("entry")), "stop": number(signal.get("stop_loss")),
        "tp1": number(signal.get("tp1")), "tp3": number(signal.get("tp3")),
        "fee_bps_per_side": policy["fee_bps_per_side"], "slippage_bps_per_side": policy["slippage_bps_per_side"],
        "decision": "ACCEPTED" if candidate.get("accepted") else "REJECTED",
        "reject_reason": None if candidate.get("accepted") else candidate.get("first_reject"),
        "intent_id": candidate.get("intent_id"), "gate_results": [],
        "policy": policy,
        "gate_context": {
            "mtf": mtf,
            "universe_gates": candidate.get("universe_gates") or [],
            "snapshot": {key: (candidate.get("snapshot") or round_data["snapshot"]).get(key)
                         for key in ("positions", "open_orders", "hedge_mode", "multi_assets_mode", "unrealized_pnl", "available_balance")},
            "daily": round_data["daily"], "plans": round_data["plans"],
            "cycle_candidates": candidate.get("cycle_candidates"),
        },
    }
    gates = record["gate_results"]
    gates.extend(candidate.get("universe_gates") or [])

    def gate(key: str, passed: bool | None, detail: str = "") -> None:
        gates.append({"key": key, "passed": passed, "detail": detail, "source": "SHADOW"})

    def threshold(key: str, value: float | None, limit: float, maximum: bool = False) -> None:
        gate(key, None if value is None else value <= limit if maximum else value >= limit)

    gate("DIRECTION_SCORE_MARGIN_NOT_MET", direction in {"LONG", "SHORT"} if direction != "UNKNOWN" else None)
    gate("ALLOWED_SYMBOLS", bool(policy["allowed_symbols"]) and record["symbol"] in policy["allowed_symbols"])
    threshold("CONFIDENCE_BELOW_MIN", record["confidence"], policy["min_confidence"])
    threshold("TRAP_SCORE_ABOVE_MAX", record["trap"], 35, True)
    threshold("BREAKOUT_QUALITY_BELOW_MIN", record["breakout"], 50)
    gate("MTF_HIGHER_TIMEFRAME_MISMATCH", mtf.get("higher_timeframe_confirmation"))
    gate("MTF_SHORT_ALIGNMENT_FILTER", not mtf["blocked_by_short_filter"] if "blocked_by_short_filter" in mtf else None)
    snapshot = candidate.get("snapshot") or round_data["snapshot"]
    mode = snapshot.get("multi_assets_mode")
    gate("ISOLATED_ACCOUNT", None if mode is None else mode is False and policy["require_isolated"] is True)
    risk = None
    if record["entry"] and record["stop"] and direction in {"LONG", "SHORT"}:
        try:
            risk = risk_sized_order(record["entry"], record["stop"], policy, atr=record["atr"])
            gate("STOP_RISK", True)
            gate("MINIMUM_MARGIN", risk["margin_usdt"] >= 5)
            distance = abs(record["entry"] - record["stop"]) / record["entry"] * 100
            gate("STOP_DISTANCE_ABOVE_MAX", distance <= policy["max_stop_distance_pct"])
        except ValueError as exc:
            gate("STOP_RISK", False, str(exc))
    else:
        gate("STOP_RISK", None, "NO_PRICE_EVIDENCE")
    balance = number(snapshot.get("available_balance"))
    gate("AVAILABLE_BALANCE", balance >= risk["margin_usdt"] if balance is not None and risk else None)
    candidate_notional = number((candidate.get("spec") or {}).get("notional_usdt"))
    if candidate_notional is None and risk:
        candidate_notional = risk["notional_usdt"]
    spread = record["spread_bp"]
    account_gates = evaluate_entry_gates(
        symbol=record["symbol"], signal=signal, snapshot=candidate.get("snapshot") or round_data["snapshot"],
        policy=policy, daily=round_data["daily"], spread_bps=spread if spread is not None else 0,
        armed=True, allowed_symbols=policy["allowed_symbols"],
        active_plans=round_data["plans"], cycle_candidates=candidate.get("cycle_candidates"),
        candidate_notional_usdt=candidate_notional if candidate_notional is not None else 0,
    )["gates"]
    for item in account_gates:
        passed = item["passed"]
        if (item["key"] == "confidence" and record["confidence"] is None) or (item["key"] == "trap" and record["trap"] is None):
            passed = None
        if item["key"] in {"direction", "long", "short"} and direction == "UNKNOWN":
            passed = None
        if (item["key"] == "spread" and spread is None) or (item["key"] in {"exposure", "direction_exposure"} and candidate_notional is None):
            passed = None
        gate(item["key"].upper(), passed, item["detail"])
    spec, brackets = candidate.get("spec"), candidate.get("brackets")
    if spec and brackets:
        try:
            isolated_liquidation_risk(spec, brackets, policy)
            gate("LIQUIDATION_BUFFER", True)
        except ValueError as exc:
            gate("LIQUIDATION_BUFFER", False, str(exc))
    else:
        gate("LIQUIDATION_BUFFER", candidate.get("liquidation_passed"), "NO_MAINTENANCE_EVIDENCE" if not brackets else "")
    for actual in candidate.get("actual_gates") or []:
        gates.append({**actual, "key": actual["key"].upper(), "source": "ACTUAL"})
    rejects = [item["key"] for item in gates if item["passed"] is False]
    record["shadow_reject_reasons"] = list(dict.fromkeys(rejects))
    record["reject_reasons"] = list(dict.fromkeys([*([record["reject_reason"]] if record["reject_reason"] else []),
                                                 *(candidate.get("actual_reasons") or []), *rejects]))
    if candidate.get("accepted"):
        record["reject_reasons"] = []
    record["unknown_gates"] = list(dict.fromkeys(item["key"] for item in gates if item["passed"] is None))
    return record
