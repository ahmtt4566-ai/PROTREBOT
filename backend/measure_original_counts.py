"""Native Original v1 TRAIN/VALIDATION counts only, never profitability.

Stage 6b native entry-reference policy/config is reused, NOT its frozen B exit
variant. No Donchian blackout, extra warmup, BE, refill or gate changes. Fresh
native Engine and Dataset cache per phase; no carry or forced final closure.
The replay orchestration mirrors Engine.replay but omits metrics/bootstrap.
Engine.enter/advance/finalize and Dataset.canonical remain literal native calls.

All-symbol diagnostics include occupied/unranked symbols, unlike native scan
evaluation counts. WAIT details need a second pure canonical call because native
Dataset.canonical strips rejected analysis. It cannot alter native admission.
WAIT causes are overlapping memberships, not a partition; ranked reasons are
counts sorted descending. Stop distributions use strategy entry/Stop, not fills.
Completed durations include every CLOSED trade; verified N also requires finite
accounting and complete fees/funding. Monetary state stays in memory for gates.
No per-trade records, prices, outcomes, performance or equity are serialized.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from build_measurement_view import load_measurement_dataset, sha256_file
from measure_donchian_counts import (
    BACKEND,
    PHASES,
    STEP,
    VIEW,
    MeasurementError,
    Phase,
    deny_network,
    digest_object,
    emit,
    locked_plan,
    month,
    primary_config,
    quiet_native,
    reason_counts,
    stamp,
    validate_loaded_scope,
)

if TYPE_CHECKING:
    from app.backtest_baseline import Config, Engine
    from app.backtest_data import Dataset

OUTPUT = Path.home() / "kaistrade-data" / "original-measurement-results"
METADATA = Path.home() / "kaistrade-data" / "donchian-holdout-evidence" / "current-metadata.json"
VIEW_SHA = "23e8d8b55a3a12ed76d562eb072a440115d3a2aeaefe6571e2c155c15f316a6e"
SCHEMA = "original-native-counts-only-v1"
COUNT_KEYS = {
    "grid_points", "closed_15m_points", "quality_passed", "native_canonical_evaluations",
    "attempted_entries", "accepted_entries", "completed", "fully_verified_completed",
    "open_at_end", "unknown_data_gap",
}
GROUP_KEYS = {"accepted_entries", "completed", "fully_verified_completed", "open_at_end", "unknown_data_gap"}
WAIT_KEYS = {"confidence", "trap", "breakout", "mtf_permission", "short_filter", "direction", "data_warmup"}
PHASE_KEYS = {
    "period", "state_at_start", "counts", "canonical_distribution", "wait_causes",
    "wait_reasons", "native_rejections", "native_first_rejections", "gate_rejections",
    "gate_evaluations", "rejections_by_stage", "stop_candidates", "stop_accepted",
    "completed_duration_hours", "by_symbol", "by_entry_month", "by_direction",
    "config_sha256", "policy_sha256", "rejection_counts",
}
REJECTION_KEYS = {
    "stop_prefilter", "sizing_stop_risk", "minimum_margin", "native_spec",
    "isolated", "liquidation_buffer", "daily_trades", "daily_loss",
    "open_loss", "consecutive_losses", "exposure", "direction_exposure",
}
CANONICAL_REASONS = {
    "INVALID_CANDLE_DATA", "UNSUPPORTED_TIMEFRAME", "NON_CHRONOLOGICAL_CANDLES",
    "INSUFFICIENT_CLOSED_CANDLES", "QUALITY_OR_MTF_GATE",
}
NATIVE_REASONS = CANONICAL_REASONS | {
    "arm", "symbol", "direction", "long", "short", "confidence", "trap", "spread",
    "one_way", "positions", "duplicate", "active_plan", "exposure", "daily_trades",
    "daily_loss", "open_loss", "accounting_verified", "consecutive_losses",
    "same_direction_positions", "direction_exposure", "allowed_symbols", "stop_risk",
    "minimum_margin", "data_missing_next_open", "cost_filter", "min_notional",
    "native_spec_rejected", "isolated", "liquidation_buffer", "available_balance",
    "data_gap_history", "canonical_wait", "stop_distance",
}
DEFINITIONS = {
    "engine": "NATIVE_ORIGINAL_ENTRY_REFERENCE_NOT_STAGE6B_FROZEN_B",
    "diagnostics": "ALL_SYMBOL_GRID_INCLUDING_OCCUPIED_UNRANKED; NATIVE_EVALUATIONS_SEPARATE",
    "wait": "OVERLAPPING_CAUSES; PURE_CANONICAL_OBSERVATION_ONLY",
    "rejections": "OVERLAPPING_NATIVE_REASONS_NOT_A_PARTITION; PREFILTER_AND_ENTER_SEPARATE",
    "stop": "STRATEGY_ENTRY_TO_INITIAL_STOP_PERCENT; OVER_2_IS_DISTANCE_NOT_MARGIN_REJECTION",
    "duration": "ALL_CLOSED; NO_FORCED_CLOSE_OR_PHASE_CARRY",
    "policy": "CURRENT_METADATA_CONDITIONAL; NO_ADDITIONAL_BLACKOUT_OR_WARMUP",
}


def distribution(values: list[float]) -> dict[str, int | float | None]:
    if any(not math.isfinite(value) or value < 0 for value in values):
        raise MeasurementError("INVALID_DISTRIBUTION")
    ordered = sorted(values)

    def quantile(fraction: float) -> float | None:
        if not ordered:
            return None
        offset = (len(ordered) - 1) * fraction
        low, high = math.floor(offset), math.ceil(offset)
        return ordered[low] + (ordered[high] - ordered[low]) * (offset - low)

    return {"count": len(values), "median": quantile(0.5), "p90": quantile(0.9), "p99": quantile(0.99)}


def wait_causes(full: dict[str, Any], policy: dict[str, Any]) -> list[str]:
    if full["decision"] != "WAIT":
        return []
    analysis = full.get("analysis")
    if not analysis:
        return ["data_warmup"]
    causes = []
    if analysis["direction"] not in {"LONG", "SHORT"}:
        causes.append("direction")
    if analysis["confidence"] < policy["min_confidence"]:
        causes.append("confidence")
    if analysis["radar"]["trap_score"] > 35:
        causes.append("trap")
    if analysis["radar"]["breakout_quality"] < 50:
        causes.append("breakout")
    if not full["mtf"]["entry_permission"]:
        causes.append("mtf_permission")
    if full["mtf"].get("blocked_by_short_filter"):
        causes.append("short_filter")
    if not causes:
        raise MeasurementError("UNCLASSIFIED_CANONICAL_WAIT")
    return causes


def fully_verified(position: Any) -> bool:
    row = position.row()
    return (
        position.status == "CLOSED" and row["commission_complete"] is True
        and row["funding_complete"] is True
        and all(type(row[key]) in (int, float) and math.isfinite(row[key]) for key in ("net_pnl", "net_r"))
    )


def trade_counts(positions: list[Any]) -> dict[str, int]:
    return {
        "accepted_entries": len(positions),
        "completed": sum(position.status == "CLOSED" for position in positions),
        "fully_verified_completed": sum(fully_verified(position) for position in positions),
        "open_at_end": sum(position.status == "OPEN_AT_END" for position in positions),
        "unknown_data_gap": sum(position.status == "UNKNOWN_DATA_GAP" for position in positions),
    }


def fresh_dataset(data: Dataset) -> Dataset:
    from app.backtest_data import Dataset

    return Dataset(data.frames, data.marks, data.funding, data.metadata, data.report, data.funding_months)


def make_engine(data: Dataset, config: Config) -> Engine:
    from app.backtest_baseline import Engine

    class CountingEngine(Engine):
        def __init__(self):
            super().__init__(fresh_dataset(data), config)
            self.attempts = 0
            self.stage = "PREFILTER"
            self.stage_rejections: dict[str, Counter] = {"PREFILTER": Counter(), "ENTER": Counter()}
            self.accepted_distances: list[float] = []
            self.accepted_above_cap = 0

        def reject(self, reasons: list[str]) -> None:
            self.stage_rejections[self.stage].update(dict.fromkeys(reasons, 1))
            super().reject(reasons)

        async def enter(self, symbol: str, signal: dict, at: int) -> None:
            self.attempts += 1
            before = len(self.trades)
            self.stage = "ENTER"
            await super().enter(symbol, signal, at)
            self.stage = "PREFILTER"
            if len(self.trades) > before:
                from app.execution_core import dynamic_stop_distance_pct

                distance = abs(signal["entry"] - signal["stop_loss"]) / signal["entry"] * 100
                self.accepted_distances.append(distance)
                self.accepted_above_cap += distance > dynamic_stop_distance_pct(
                    signal["entry"], signal.get("atr"), self.policy) + 1e-9

    return CountingEngine()


async def replay_counts(engine: Any, phase: Phase, progress: Callable[[dict], None] | None = None) -> dict:
    from app import execution_core as core
    from app import main
    from app import v25_execution as live

    if engine.positions or engine.events or engine.trades or engine.data.decisions:
        raise MeasurementError("PHASE_NOT_FRESH")
    symbols = sorted(set(engine.policy["allowed_symbols"]) & set(engine.data.frames))
    decisions, causes, reasons = Counter(), Counter(), Counter()
    distances: list[float] = []
    over_two = above_cap = closed_points = 0
    for at in range(phase.start, phase.end, STEP):
        if progress and (at - phase.start) % (30 * 86400) == 0:
            progress({"status": "progress", "phase": phase.name, "completed_bars": (at - phase.start) // STEP})
        for position in list(engine.positions.values()):
            engine.advance(position, at, opening_only=True)
        for symbol in symbols:
            closed_points += engine.data.frames[symbol]["15m"].at(at - STEP) is not None
            native = engine.data.canonical(symbol, at, engine.policy)
            full = native
            if not native.get("analysis"):
                full = main.canonical_historical_decision(
                    symbol, {interval: series.closed(at) for interval, series in engine.data.frames[symbol].items()},
                    at, required_intervals=("15m", "1h", "4h"),
                    historical_policy_override={"confidence_threshold": engine.policy["min_confidence"],
                                                "mtf_allow_either_timeframe": engine.policy["mtf_allow_either_timeframe"]})
                if any(full.get(key) != native.get(key) for key in ("decision", "entry_eligible", "reason")):
                    raise MeasurementError("DIAGNOSTIC_NATIVE_MISMATCH")
            decisions[native["decision"]] += 1
            causes.update(wait_causes(full, engine.policy))
            if native["decision"] == "WAIT":
                reasons[native["reason"]] += 1
            elif native.get("entry_eligible"):
                signal = native["analysis"]
                distance = abs(signal["entry"] - signal["stop_loss"]) / signal["entry"] * 100
                distances.append(distance)
                over_two += distance > 2
                above_cap += distance > core.dynamic_stop_distance_pct(signal["entry"], signal.get("atr"), engine.policy) + 1e-9
        tickers = []
        for symbol in symbols:
            ticker = engine.data.frames[symbol]["15m"].ticker(at, symbol)
            if ticker is not None:
                tickers.append(ticker)
        ranked = live.rank_market_tickers(engine.data.metadata["exchange_info"], tickers,
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
            distance = abs(signal["entry"] - signal["stop_loss"]) / signal["entry"] * 100
            if distance > core.dynamic_stop_distance_pct(signal["entry"], None, engine.policy):
                engine.reject(["stop_distance"])
                continue
            signals.append((candidate, signal))
        signals.sort(key=lambda pair: (pair[0]["opportunity_score"], pair[1]["confidence"]), reverse=True)
        for candidate, signal in signals[:3]:
            await engine.enter(candidate["symbol"], signal, at)
        for position in list(engine.positions.values()):
            engine.advance(position, at, opening_only=False)
        engine.data.decisions.clear()
    for position in engine.positions.values():
        position.status = "OPEN_AT_END"
        position.funding_known = position.funding_known and engine.data.funding_complete(
            position.symbol, position.opened_at, phase.end - 1)
    hours = []
    for position in engine.trades:
        if position.status == "CLOSED":
            if position.closed_at is None:
                raise MeasurementError("CLOSED_TIME_MISSING")
            hours.append((position.closed_at - position.opened_at) / 3600)
    counts = trade_counts(engine.trades)
    months = sorted({month(at) for at in range(phase.start, phase.end, 86400)})
    return {
        "period": {"start": stamp(phase.start), "end_exclusive": stamp(phase.end)},
        "state_at_start": {"positions": 0, "events": 0, "entries": 0},
        "counts": {**counts, "grid_points": sum(decisions.values()), "closed_15m_points": closed_points,
                   "quality_passed": len(distances), "native_canonical_evaluations": engine.decisions_evaluated,
                   "attempted_entries": engine.attempts},
        "canonical_distribution": {key: decisions[key] for key in ("BUY", "SELL", "WAIT")},
        "wait_causes": [{"reason": key, "count": causes[key]} for key in sorted(WAIT_KEYS, key=lambda key: (-causes[key], key))],
        "wait_reasons": [{"reason": key, "count": value} for key, value in sorted(reason_counts(reasons).items(), key=lambda pair: (-pair[1], pair[0]))],
        "native_rejections": reason_counts(engine.rejections),
        "native_first_rejections": reason_counts(engine.first_rejections),
        "gate_rejections": reason_counts(engine.gate_counts), "gate_evaluations": reason_counts(engine.gate_evaluations),
        "rejections_by_stage": {key: reason_counts(value) for key, value in engine.stage_rejections.items()},
        "rejection_counts": {
            "stop_prefilter": engine.stage_rejections["PREFILTER"]["stop_distance"],
            "sizing_stop_risk": engine.stage_rejections["ENTER"]["stop_risk"],
            "minimum_margin": engine.rejections["minimum_margin"],
            "native_spec": sum(engine.rejections[key] for key in ("native_spec_rejected", "cost_filter", "min_notional")),
            **{key: engine.rejections[key] for key in (
                "isolated", "liquidation_buffer", "daily_trades", "daily_loss",
                "open_loss", "consecutive_losses", "exposure", "direction_exposure")},
        },
        "stop_candidates": {**distribution(distances), "over_2_pct": over_two, "above_atr_cap": above_cap},
        "stop_accepted": {
            **distribution(engine.accepted_distances),
            "over_2_pct": sum(value > 2 for value in engine.accepted_distances),
            "above_atr_cap": engine.accepted_above_cap,
        },
        "completed_duration_hours": distribution(hours),
        "by_symbol": {symbol: trade_counts([p for p in engine.trades if p.symbol == symbol]) for symbol in symbols},
        "by_entry_month": {key: trade_counts([p for p in engine.trades if month(p.opened_at) == key]) for key in months},
        "by_direction": {key: trade_counts([p for p in engine.trades if p.direction == key]) for key in ("LONG", "SHORT")},
        "config_sha256": digest_object(asdict(engine.config)), "policy_sha256": digest_object(engine.policy),
    }


async def measure_phases(data: Dataset, plan: dict, *, phases: tuple[Phase, ...] = PHASES,
                         progress: Callable[[dict], None] | None = None) -> dict:
    if any(phase.start < PHASES[0].start or phase.end > PHASES[-1].end or phase.start >= phase.end
           or phase.start % STEP or phase.end % STEP for phase in phases):
        raise MeasurementError("MEASUREMENT_SCOPE_DENIED")
    return {phase.name: await replay_counts(make_engine(data, primary_config(phase, plan)), phase, progress)
            for phase in phases}


def validate_report(report: dict) -> None:
    def require(ok: bool) -> None:
        if not ok:
            raise MeasurementError("COUNTS_SCHEMA_REJECTED")

    def counts(value: dict, keys: set[str]) -> None:
        require(set(value) == keys and all(type(n) is int and n >= 0 for n in value.values()))

    def histogram(value: dict, allowed: set[str]) -> None:
        require(set(value) <= allowed)
        counts(value, set(value))
        reason_counts(value)

    def safe_keys(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                require(isinstance(key, str) and key.lower() not in {"pf", "trades"}
                        and not any(word in key.lower() for word in (
                            "net_r", "pnl", "profit", "win_rate", "winrate", "equity", "price", "return", "gross")))
                safe_keys(child)
        elif isinstance(value, list):
            for child in value:
                safe_keys(child)

    def stats(value: dict, extras: set[str] | None = None) -> None:
        require(set(value) == {"count", "median", "p90", "p99"} | (extras or set()))
        counts({key: value[key] for key in {"count"} | (extras or set())}, {"count"} | (extras or set()))
        quantiles = [value[key] for key in ("median", "p90", "p99")]
        require(all(n is None for n in quantiles) if not value["count"] else
                all(type(n) in (int, float) and math.isfinite(n) and n >= 0 for n in quantiles))
        if value["count"]:
            require(quantiles == sorted(quantiles))

    safe_keys(report)
    require(set(report) == {"schema", "definitions", "phases", "provenance", "elapsed_seconds"})
    require(report["schema"] == SCHEMA and report["definitions"] == DEFINITIONS)
    require(set(report["phases"]) == {"TRAIN", "VALIDATION"})
    require(set(report["provenance"]) == {"metadata_sha256", "measurement_manifest_sha256", "stage6b_plan_sha256", "script_sha256"})
    for value in report["provenance"].values():
        require(isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value))
    require(type(report["elapsed_seconds"]) in (int, float) and math.isfinite(report["elapsed_seconds"]) and report["elapsed_seconds"] >= 0)
    for phase in report["phases"].values():
        require(set(phase) == PHASE_KEYS)
        require(set(phase["period"]) == {"start", "end_exclusive"})
        for text in phase["period"].values():
            require(isinstance(text, str))
        require(phase["state_at_start"] == {"positions": 0, "events": 0, "entries": 0})
        counts(phase["counts"], COUNT_KEYS)
        counts(phase["rejection_counts"], REJECTION_KEYS)
        counts(phase["canonical_distribution"], {"BUY", "SELL", "WAIT"})
        for key in ("wait_causes", "wait_reasons"):
            require(isinstance(phase[key], list))
            for row in phase[key]:
                require(set(row) == {"reason", "count"})
                histogram({row["reason"]: row["count"]}, WAIT_KEYS if key == "wait_causes" else CANONICAL_REASONS)
            if key == "wait_causes":
                require({row["reason"] for row in phase[key]} == WAIT_KEYS and len(phase[key]) == len(WAIT_KEYS))
        for key in ("native_rejections", "native_first_rejections", "gate_rejections", "gate_evaluations"):
            histogram(phase[key], NATIVE_REASONS)
        require(set(phase["rejections_by_stage"]) == {"PREFILTER", "ENTER"})
        for value in phase["rejections_by_stage"].values():
            histogram(value, NATIVE_REASONS)
        stats(phase["stop_candidates"], {"over_2_pct", "above_atr_cap"})
        stats(phase["stop_accepted"], {"over_2_pct", "above_atr_cap"})
        stats(phase["completed_duration_hours"])
        require(set(phase["by_direction"]) == {"LONG", "SHORT"})
        total = phase["counts"]
        for group in ("by_symbol", "by_entry_month", "by_direction"):
            for value in phase[group].values():
                counts(value, GROUP_KEYS)
            for key in GROUP_KEYS:
                require(sum(cell[key] for cell in phase[group].values()) == total[key])
        require(total["accepted_entries"] == total["completed"] + total["open_at_end"] + total["unknown_data_gap"])
        require(total["fully_verified_completed"] <= total["completed"] and total["accepted_entries"] <= total["attempted_entries"])
        require(total["grid_points"] == sum(phase["canonical_distribution"].values()))
        require(total["quality_passed"] == phase["canonical_distribution"]["BUY"] + phase["canonical_distribution"]["SELL"])
        require(total["quality_passed"] == phase["stop_candidates"]["count"])
        require(total["accepted_entries"] == phase["stop_accepted"]["count"])
        require(total["completed"] == phase["completed_duration_hours"]["count"])
        for key in ("config_sha256", "policy_sha256"):
            require(isinstance(phase[key], str) and len(phase[key]) == 64)
    json.dumps(report, allow_nan=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata", required=True, type=Path)
    args = parser.parse_args(argv)
    stream = sys.stdout
    loop = None
    try:
        if OUTPUT.exists():
            raise MeasurementError("RESULTS_ALREADY_EXIST")
        if args.metadata.resolve() != METADATA.resolve():
            raise MeasurementError("METADATA_COPY_REQUIRED")
        plan = locked_plan()
        if sha256_file(args.metadata) != plan["input_sha256"]["metadata"]:
            raise MeasurementError("LOCKED_METADATA_MISMATCH")
        if sha256_file(VIEW / "manifest.json") != VIEW_SHA:
            raise MeasurementError("LOCKED_VIEW_MISMATCH")
        OUTPUT.mkdir(parents=True, exist_ok=False)
        os.environ.update({"DATA_DIR": str(OUTPUT / "runtime"), "PROTREBOT_DATA_DIR": str(OUTPUT / "runtime"),
                           "DATABASE_URL": "", "PROTREBOT_DURABLE_AUTH_REQUIRED": "0", "ASSISTANT_LIVE_TESTS": "0"})
        loop = asyncio.new_event_loop()
        started = time.monotonic()
        with quiet_native(), deny_network():
            data = load_measurement_dataset(VIEW, args.metadata, PHASES[0].start, PHASES[-1].end)
            validate_loaded_scope(data, plan["symbols"])
            phases = loop.run_until_complete(measure_phases(data, plan, progress=lambda value: emit(stream, value)))
        report = {
            "schema": SCHEMA, "definitions": DEFINITIONS, "phases": phases,
            "provenance": {"metadata_sha256": plan["input_sha256"]["metadata"], "measurement_manifest_sha256": VIEW_SHA,
                           "stage6b_plan_sha256": sha256_file(BACKEND / "studies" / "stage6b-plan.json"),
                           "script_sha256": sha256_file(Path(__file__))},
            "elapsed_seconds": time.monotonic() - started,
        }
        validate_report(report)
        path = OUTPUT / "counts.json"
        with path.open("x", encoding="utf-8") as output:
            json.dump(report, output, indent=2, allow_nan=False)
            output.write("\n")
        emit(stream, {"status": "completed", "output": str(path), "sha256": sha256_file(path)})
        for name, phase in phases.items():
            emit(stream, {"phase": name, "counts": phase["counts"], "wait_causes": phase["wait_causes"]})
        return 0
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, ArithmeticError) as exc:
        code = str(exc) if isinstance(exc, MeasurementError) else "NATIVE_FAILURE"
        emit(stream, {"status": "failed", "code": code, "error_class": type(exc).__name__})
        return 2
    finally:
        if loop is not None:
            loop.close()


if __name__ == "__main__":
    raise SystemExit(main())
