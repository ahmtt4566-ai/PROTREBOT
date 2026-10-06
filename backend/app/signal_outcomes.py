"""Closed-candle counterfactual observations, not an entry/backtest engine."""

from __future__ import annotations

import json
from typing import Any

from .signal_journal import Journal, number


def hypothetical(record: dict[str, Any], bars: list[dict[str, Any]]) -> dict[str, Any]:
    if not bars:
        return {}
    entry, stop, tp1, tp3 = (number(record.get(key)) for key in ("entry", "stop", "tp1", "tp3"))
    direction = record["direction"] if record["direction"] in {"LONG", "SHORT"} else record.get("hypothetical_direction")
    if direction not in {"LONG", "SHORT"} or any(value is None or value <= 0 for value in (entry, stop, tp1, tp3)):
        return {}
    sign = 1 if direction == "LONG" else -1
    if sign * (entry - stop) <= 0 or not 0 < sign * (tp1 - entry) < sign * (tp3 - entry):
        return {}
    risk = abs(entry - stop)
    fee = number(record.get("fee_bps_per_side"))
    slip = number(record.get("slippage_bps_per_side"))
    cost_rate = (fee + slip) / 10_000 if fee is not None and slip is not None else None
    remaining, realized, exit_notional = 1.0, 0.0, 0.0
    tp1_hit, ambiguous = False, False
    result = "TIMEOUT"
    mae, mfe = 0.0, 0.0
    worst_price, best_price = entry, entry
    for bar in bars:
        adverse = bar["low"] if sign == 1 else bar["high"]
        favorable = bar["high"] if sign == 1 else bar["low"]
        adverse_r, favorable_r = sign * (adverse - entry) / risk, sign * (favorable - entry) / risk
        if adverse_r < mae:
            mae, worst_price = adverse_r, adverse
        if favorable_r > mfe:
            mfe, best_price = favorable_r, favorable
        stop_touched = sign * (adverse - stop) <= 0
        tp1_touched = sign * (favorable - tp1) >= 0
        tp3_touched = sign * (favorable - tp3) >= 0
        ambiguous = ambiguous or (stop_touched and tp1_touched)
        if stop_touched:
            realized += remaining * sign * (stop - entry)
            exit_notional += remaining * stop
            result = "STOP"
            remaining = 0
            break
        if not tp1_hit and tp1_touched:
            realized += 0.6 * sign * (tp1 - entry)
            exit_notional += 0.6 * tp1
            remaining, tp1_hit = 0.4, True
        if tp3_touched:
            realized += remaining * sign * (tp3 - entry)
            exit_notional += remaining * tp3
            result = "TP3"
            remaining = 0
            break
    if remaining:
        close = bars[-1]["close"]
        realized += remaining * sign * (close - entry)
        exit_notional += remaining * close
        result = "TP1" if tp1_hit else "TIMEOUT"
    gross_r = realized / risk
    return {
        "hypothetical_result": result, "hypothetical_tp1_hit": tp1_hit,
        "hypothetical_r_gross": gross_r,
        "hypothetical_r_costs": gross_r - (entry + exit_notional) * cost_rate / risk if cost_rate is not None else None,
        "mae_r_gross": mae, "mfe_r_gross": mfe,
        "mae_r_costs": mae - (entry + worst_price) * cost_rate / risk if cost_rate is not None else None,
        "mfe_r_costs": mfe - (entry + best_price) * cost_rate / risk if cost_rate is not None else None,
        "intrabar_ambiguous": ambiguous, "intrabar_policy": "STOP_FIRST",
        "excursion_scope": "THROUGH_EXIT_BAR", "hypothetical_timeout_hours": 4,
        "hypothetical_funding_included": False,
    }


def fill_outcomes(journal: Journal, as_of: int) -> dict[str, int]:
    updated, incomplete = 0, 0
    records = journal.rows()
    if any(record.get("outcomes_as_of") is not None and as_of < record["outcomes_as_of"] for record in records):
        raise ValueError("Outcome as_of cannot move backwards; use an isolated journal for an earlier snapshot")
    for record in records:
        decision = record["ts_decision"]
        if record.get("decision_time_verified") is not True:
            incomplete += 1
            continue
        bars = [bar for bar in journal.closed_candles(record["symbol"], "15m", as_of) if bar["time"] >= decision]
        by_open = {int(bar["time"]): bar for bar in bars}
        entry = number(record.get("entry"))
        outcomes: dict[str, Any] = {}
        for hours in (1, 2, 4):
            expected = [decision + step * 900 for step in range(hours * 4)]
            complete = as_of >= decision + hours * 3600 and all(opening in by_open for opening in expected)
            if not complete:
                continue
            window = [by_open[opening] for opening in expected]
            outcomes[f"return_{hours}h"] = window[-1]["close"] / entry - 1 if entry and entry > 0 else None
            if hours == 4:
                outcomes.update(hypothetical(record, window))
        if not outcomes:
            incomplete += 1
            continue
        outcomes["outcomes_as_of"] = as_of
        with journal.connect() as db:
            db.execute("UPDATE signals SET outcomes=? WHERE signal_id=?",
                       (json.dumps(outcomes, allow_nan=False), record["signal_id"]))
        updated += 1
    return {"updated": updated, "incomplete": incomplete}


def import_data(journal: Journal, data: dict[str, Any], as_of: int) -> dict[str, int]:
    from .analysis import analyze
    from .signal_observation import build_record

    for batch in data.get("candles", []):
        journal.candles(batch["symbol"], batch["interval"], batch["rows"], as_of)
    enriched, future = 0, 0
    records = {record["signal_id"]: record for record in journal.rows()}
    for market in data.get("market", []):
        record = records.get(market["signal_id"])
        if record is None:
            raise ValueError("Unknown market-enrichment signal ID")
        observed = number(market.get("observed_at"))
        if observed is None:
            raise ValueError("Market enrichment requires observed_at")
        if observed > record["ts_observed"] or observed > as_of:
            future += 1
            continue
        context = record["gate_context"]
        primary = journal.closed_candles(record["symbol"], "15m", record["ts_decision"])
        features: dict[str, Any] = {}
        if len(primary) >= 220:
            scores: dict[str, int] = {}
            analysis = analyze(primary, observation_scores=scores)
            features.update({key: value for key, value in scores.items() if record.get(key) is None})
            features.update(
                ema20=analysis["ema"]["ema20"], ema50=analysis["ema"]["ema50"], ema200=analysis["ema"]["ema200"],
                macd_hist=analysis["macd"], rsi=analysis["rsi"], atr=analysis["atr"], adx=analysis["adx"],
                bb_width=(analysis["bollinger"]["upper"] - analysis["bollinger"]["lower"]) / analysis["bollinger"]["middle"],
                vol_ratio=analysis["volume_ratio"],
                hypothetical_direction=analysis["direction"],
            )
            for key, value in (("entry", analysis["entry"]), ("stop", analysis["stop_loss"]),
                               ("tp1", analysis["tp1"]), ("tp3", analysis["tp3"])):
                if record.get(key) is None:
                    features[key] = value
            features["feature_enrichment_source"] = "OFFLINE_CLOSED_CANDLES"
            features = {key: value for key, value in features.items() if record.get(key) is None}
        for key in ("spread_bp", "funding_rate"):
            if key in market:
                value = number(market[key])
                if market[key] is not None and (value is None or (key == "spread_bp" and value < 0)):
                    raise ValueError(f"Invalid market observation: {key}")
                if record.get(key) is None:
                    features[key] = value
        signal = {key: record.get(name) for key, name in [
            ("direction", "direction"), ("confidence", "confidence"), ("entry", "entry"), ("stop_loss", "stop"),
            ("tp1", "tp1"), ("tp3", "tp3"), ("atr", "atr"),
        ]}
        signal["radar"] = {"trap_score": record["trap"], "breakout_quality": record["breakout"]}
        candidate = {
            "symbol": record["symbol"], "canonical": {"analysis": signal, "signal_timestamp": record["ts_decision"] - 900,
                                                       "mtf": context.get("mtf", {})},
            "first_reject": record["reject_reason"], "spread_bp": features.get("spread_bp", record["spread_bp"]),
            "brackets": market.get("brackets"), "spec": market.get("spec"),
            "universe_gates": context.get("universe_gates", []),
            "cycle_candidates": context["cycle_candidates"], "accepted": record["decision"] == "ACCEPTED",
        }
        shadow = build_record({
            "policy": record["policy"], "snapshot": context["snapshot"], "daily": context["daily"], "plans": context["plans"],
            "ts_scan": record["ts_observed"], "ts_decision": record["ts_decision"],
        }, candidate, journal)
        if "spec" not in market and "brackets" not in market:
            original = next((gate for gate in record["gate_results"] if gate["key"] == "LIQUIDATION_BUFFER"), None)
            if original is not None:
                shadow["gate_results"] = [original if gate["key"] == "LIQUIDATION_BUFFER" else gate
                                          for gate in shadow["gate_results"]]
                shadow["reject_reasons"] = list(dict.fromkeys([
                    *([record["reject_reason"]] if record["reject_reason"] else []),
                    *(gate["key"] for gate in shadow["gate_results"] if gate["passed"] is False),
                ])) if record["decision"] == "REJECTED" else []
        features.update(enriched_gate_results=shadow["gate_results"], enriched_reject_reasons=shadow["reject_reasons"],
                        enrichment_observed_at=observed)
        journal.enrich(record["signal_id"], features)
        enriched += 1
    for trade in data.get("trades", []):
        observed = number(trade.get("observed_at"))
        if observed is None or observed > as_of:
            raise ValueError("Trade enrichment requires observed_at no later than as_of")
        intent_id = trade["intent_id"]
        record = next((record for record in records.values()
                       if record.get("intent_id") == intent_id and record["decision"] == "ACCEPTED"), None)
        if record is None or observed < record["ts_decision"]:
            raise ValueError("Trade enrichment requires an accepted intent and causal observation time")
        allowed = {"gross_pnl", "commission_usdt", "commission_complete", "funding_usdt", "funding_complete",
                   "actual_fill_price", "actual_slippage_price", "expected_entry"}
        journal.trade(intent_id, {**{key: value for key, value in trade.items() if key in allowed},
                                  "trade_accounting_observed_at": observed})
    return {"market_enriched": enriched, "future_market_ignored": future}
