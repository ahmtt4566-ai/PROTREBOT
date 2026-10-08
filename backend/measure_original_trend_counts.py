"""Additive run3 driver mirror with one post-canonical, pre-stop/rank trend exclusion.

No existing driver can accept this hook without editing a protected file.
Native decisions are counted for all symbols; trend percentages sample every 15m
decision point, including times with positions or other exclusions. Rejected
trend candidates never consume the final top-three slots.
"""

from collections import Counter
from dataclasses import asdict

import measure_original_v2_counts as previous
from measure_donchian_counts import (
    STEP,
    MeasurementError,
    digest_object,
    month,
    reason_counts,
    stamp,
)
from measure_original_counts import distribution


async def replay_counts(engine, phase, progress=None) -> dict:
    from app import v25_execution as live
    from app.strategies.original_offline_engine import trade_counts

    if engine.positions or engine.events or engine.trades or engine.data.decisions:
        raise MeasurementError("PHASE_NOT_FRESH")
    symbols = sorted(set(engine.policy["allowed_symbols"]) & set(engine.data.frames))
    decisions: Counter[str] = Counter()
    closed_points = 0
    first_accounting_rejection = None
    for at in range(phase.start, phase.end, STEP):
        if progress and (at - phase.start) % (30 * 86400) == 0:
            progress({"status": "progress", "phase": phase.name, "completed_bars": (at - phase.start) // STEP})
        for position in list(engine.positions.values()):
            engine.advance(position, at, opening_only=True)
        engine.sample_permissions(symbols, at)
        for symbol in symbols:
            closed_points += engine.data.frames[symbol]["15m"].at(at - STEP) is not None
            decisions[engine.data.canonical(symbol, at, engine.policy)["decision"]] += 1
        tickers = []
        for symbol in symbols:
            if symbol not in engine.positions and engine.entry_data_exclusion(symbol, at, "before_market_ranking"):
                continue
            ticker = engine.data.frames[symbol]["15m"].ticker(at, symbol)
            if ticker is not None:
                tickers.append(ticker)
        ranked = live.rank_market_tickers(
            engine.data.metadata["exchange_info"], tickers,
            excluded_symbols=set(engine.positions), allowed_symbols=set(symbols))
        signals = []
        for candidate in ranked:
            symbol = candidate["symbol"]
            if not all(series.history_complete(at) for series in engine.data.frames[symbol].values()):
                engine.reject(["data_gap_history"])
                continue
            native = engine.data.canonical(symbol, at, engine.policy)
            engine.decisions_evaluated += 1
            if not native.get("entry_eligible"):
                engine.reject([native.get("reason", "canonical_wait")])
                continue
            signal = native["analysis"]
            if engine.apply_trend(symbol, signal["direction"], at) and engine.prefilter(signal):
                signals.append((candidate, signal))
        signals.sort(key=lambda pair: (pair[0]["opportunity_score"], pair[1]["confidence"]), reverse=True)
        for candidate, signal in signals[:3]:
            before_accounting = engine.rejections["pnl_verified"]
            await engine.enter(candidate["symbol"], signal, at)
            if engine.rejections["pnl_verified"] > before_accounting and first_accounting_rejection is None:
                first_accounting_rejection = stamp(at)
        for position in list(engine.positions.values()):
            engine.advance(position, at, opening_only=False)
        engine.data.decisions.clear()
    for position in engine.positions.values():
        position.status = "OPEN_AT_END"
        position.funding_known = position.funding_known and engine.data.funding_complete(
            position.symbol, position.opened_at, phase.end - 1)
    durations = []
    for position in engine.trades:
        if position.status == "CLOSED":
            if position.closed_at is None:
                raise MeasurementError("CLOSED_TIME_MISSING")
            durations.append((position.closed_at - position.opened_at) / 3600)
    numerator = sum(engine.rejections[key] for key in ("profile_cap", "stop_risk", "minimum_margin"))
    denominator = engine.risk_stage_arrivals
    if numerator > denominator:
        raise MeasurementError("INVALID_STOP_REJECTION_COUNTS")
    normalized = {key: 0 for key in sorted(previous.REASONS | {"gap_blackout", "trend_filter"})}
    normalized.update(reason_counts(engine.rejections))
    return {
        "period": {"start": stamp(phase.start), "end_exclusive": stamp(phase.end)},
        "state_at_start": {"positions": 0, "events": 0, "entries": 0},
        "counts": {
            **trade_counts(engine.trades), "grid_points": sum(decisions.values()),
            "closed_15m_points": closed_points, "quality_passed": decisions["BUY"] + decisions["SELL"],
            "native_canonical_evaluations": engine.decisions_evaluated,
            "attempted_entries": engine.attempts, "risk_stage_arrivals": denominator,
        },
        "canonical_distribution": {key: decisions[key] for key in ("BUY", "SELL", "WAIT")},
        "stop_rejection": {"numerator": numerator, "denominator": denominator,
                           "ratio": numerator / denominator if denominator else None},
        "rejections": normalized,
        "rejections_by_stage": {stage: reason_counts(values) for stage, values in engine.stage_rejections.items()},
        "gate_rejections": reason_counts(engine.gate_counts),
        "gate_evaluations": reason_counts(engine.gate_evaluations),
        "closure_trace": previous.closure_trace(engine, first_accounting_rejection),
        "minimum_notional_quantity_by_symbol": {symbol: engine.minimum_by_symbol[symbol] for symbol in symbols},
        "stop_accepted": distribution(engine.accepted_distances),
        "completed_duration_hours": distribution(durations),
        "by_symbol": {symbol: trade_counts([p for p in engine.trades if p.symbol == symbol]) for symbol in symbols},
        "by_direction": {side: trade_counts([p for p in engine.trades if p.direction == side]) for side in ("LONG", "SHORT")},
        "by_entry_month": {key: trade_counts([p for p in engine.trades if month(p.opened_at) == key])
                           for key in sorted({month(at) for at in range(phase.start, phase.end, 86400)})},
        "policy_sha256": digest_object(engine.policy), "config_sha256": digest_object(asdict(engine.config)),
        "gap_blackout": engine.blackout_counts(symbols), "trend_filter": engine.trend_counts(symbols),
    }
