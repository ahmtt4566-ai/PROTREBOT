"""Descriptive diagnosis only: unchanged gap18d replay, no new rule or trial.

Persist every fully verified trade and its contract/mark 15m envelopes outside
the repository. Intrabar ordering is unknown. Counterfactuals are arithmetic on
recorded fills, not an executable backtest, recommendation or decision input.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import math
import os
import time
from bisect import bisect_left, bisect_right
from decimal import Decimal
from pathlib import Path
from statistics import fmean

import prescreen_original_v2 as parent
from app.backtest_baseline import Position, percentile
from app.strategies.original_gap_engine import OriginalGapRiskEngine
from app.strategies.original_offline_engine import fully_verified
from build_measurement_view import load_measurement_dataset, sha256_file
from measure_donchian_counts import (
    PHASES,
    VIEW,
    MeasurementError,
    emit,
    locked_plan,
    primary_config,
    stamp,
    validate_loaded_scope,
)
from measure_original_counts import METADATA, VIEW_SHA

OUTPUT = Path.home() / "kaistrade-data" / "original-v2-diagnosis"
SCHEMA = "original-v2-descriptive-trade-diagnosis-v1"
PATHS = ("STOP_BEFORE_TP1", "STOP_AFTER_TP1", "TP3", "OTHER")
EXPECTED = {"TRAIN": (366, 364, 364), "VALIDATION": (158, 156, 156)}
DEFINITIONS = {
    "purpose": "DESCRIPTIVE_ONLY_NOT_A_DECISION_INPUT",
    "profile": "UNCHANGED_ORIGINAL_FIXED_CAP6_LEV3_GAP18D_V1_NO_TREND_FILTER",
    "protocol_relaxation": False, "experiment_attempt_used": False,
    "ema": "EMA20_ON_LAST_259_CLOSED_CONTRACT_15M_CANDLES_NATIVE_ANALYSIS",
    "atr": "NATIVE_ATR14_ON_SAME_CLOSED_15M_WINDOW",
    "ema_distance_atr": "ABS_DECISION_CLOSE_MINUS_EMA20_DIVIDED_BY_ATR14",
    "stop_distance_pct": "ROUNDED_SPEC_ENTRY_TO_INITIAL_STOP_DIVIDED_BY_SPEC_ENTRY_TIMES_100",
    "net_r": "NATIVE_NET_R_ONLY_FULLY_VERIFIED_COMPLETED",
    "excursion": "CONTRACT_15M_PRICE_ENVELOPE_FROM_ACTUAL_FILL_DIVIDED_BY_NATIVE_INITIAL_RISK_PER_UNIT",
    "mae": "NONNEGATIVE_ADVERSE_EXCURSION_MAGNITUDE",
    "intrabar": "UNKNOWN_ORDER_EXIT_BAR_ENVELOPE_CAN_INCLUDE_MOVEMENT_AFTER_EXIT",
    "opening_exit": "EXIT_AT_BAR_OPEN_USES_OPEN_ONLY_NOT_LATER_HIGH_LOW",
    "time_to_mfe": "FIRST_MAXIMUM_BAR_OPEN_MINUS_ENTRY_SECONDS_NOT_EXACT_INTRABAR_TIME",
    "before_stop": "CONFIRMED_PREVIOUS_BARS_OR_EXIT_BAR_OPEN_SEPARATE_FROM_TERMINAL_INTRABAR_UNRESOLVED",
    "tp1_seen": "NATIVE_TP1_FILL_NOT_PRICE_TOUCH_WHEN_PARTIAL_ORDER_IS_UNAVAILABLE",
    "stop_share": "FINAL_NATIVE_EXIT_REASON_STOP_INCLUDES_BOTH_TP1_PATHS",
    "tertiles": "LINEAR_INTERPOLATED_TRAIN_P33_P67_VALIDATION_USES_SAME_CUTS_TIES_GO_LOWER",
    "reliability": "N_LT_30_GUVENILMEZ_ONLY_A_REPORT_LABEL_NOT_A_GATE",
    "counterfactual": (
        "BU BIR BACKTEST DEGILDIR, KURAL ONERISI DEGILDIR. Recorded paths only; "
        "no earlier break-even-crossing search, new exits, sizing, entries or minimum-order simulation. "
        "Gross ideal fills use contract market open without entry slippage and recorded expected exit prices. "
        "BE replaces only recorded post-TP1 STOP remaining payoff with zero; other recorded exits unchanged. "
        "Immediate close prices all original quantity at first recorded TP1 expected fill."
    ),
}


class DiagnosisEngine(OriginalGapRiskEngine):
    def __init__(self, data, config):
        super().__init__(data, config)
        self.entry_facts: dict[int, dict[str, float]] = {}

    async def _enter(self, symbol: str, signal: dict, at: int) -> None:
        before = len(self.trades)
        await super()._enter(symbol, signal, at)
        parent.require(len(self.trades) in (before, before + 1), "DIAGNOSIS_UNEXPECTED_ENTRY_DELTA")
        if len(self.trades) == before + 1:
            price, average, atr = signal["entry"], signal["ema"]["ema20"], signal["atr"]
            parent.require(all(math.isfinite(value) for value in (price, average, atr, signal["confidence"]))
                           and price > 0 and atr > 0, "DIAGNOSIS_INVALID_ACCEPTED_ENTRY_CONTEXT")
            self.entry_facts[id(self.trades[-1])] = {
                "decision_price": price, "ema20_15m": average, "atr14_15m": atr,
                "ema_distance_atr": abs(price - average) / atr, "confidence": signal["confidence"],
            }


def classify(exits: list[dict], tp1_seen: bool) -> str:
    parent.require(bool(exits), "DIAGNOSIS_MISSING_EXITS")
    if exits[-1]["reason"] == "STOP":
        return "STOP_AFTER_TP1" if tp1_seen else "STOP_BEFORE_TP1"
    return "TP3" if exits[-1]["reason"] == "TP3" else "OTHER"


def excursions(bars: list[dict], entry: float, risk_unit: float, direction: str,
               opened: int, closed: int) -> dict:
    parent.require(direction in {"LONG", "SHORT"} and risk_unit > 0 and math.isfinite(risk_unit),
                   "DIAGNOSIS_INVALID_EXCURSION_INPUT")
    expected = list(range(opened, closed // 900 * 900 + 900, 900))
    parent.require([row["time"] for row in bars] == expected, "DIAGNOSIS_NONCONTIGUOUS_TRADE_BARS")
    parent.require(closed % 900 in (0, 899), "DIAGNOSIS_UNSUPPORTED_EXIT_TIMESTAMP")
    sign = 1 if direction == "LONG" else -1
    mfe = mae = confirmed = 0.0
    reached = opened
    terminal = expected[-1]
    for bar in bars:
        candle, at = bar["contract"], bar["time"]
        parent.require(all(math.isfinite(candle[key]) and candle[key] > 0
                           for key in ("open", "high", "low", "close")), "DIAGNOSIS_INVALID_TRADE_CANDLE")
        open_r = sign * (candle["open"] - entry) / risk_unit
        if at < terminal:
            high, low = candle["high"], candle["low"]
        elif closed == terminal:
            high = low = candle["open"]
        else:
            high, low = candle["high"], candle["low"]
        favorable = sign * ((high if sign == 1 else low) - entry) / risk_unit
        adverse = -sign * ((low if sign == 1 else high) - entry) / risk_unit
        if favorable > mfe:
            mfe, reached = favorable, at
        mae = max(mae, adverse)
        confirmed = max(confirmed, favorable if at < terminal else open_r)
    return {
        "mfe_r": mfe, "mae_r": mae, "time_to_mfe_seconds": reached - opened,
        "mfe_first_bar_at": stamp(reached), "confirmed_before_stop_mfe_r": confirmed,
        "terminal_intrabar_unresolved": closed != terminal,
    }


def arithmetic_scenarios(position: Position) -> dict:
    observation = parent.observe(position)
    entry = position.market_open if position.market_open is not None else Decimal(str(position.spec["entry_price"]))
    first = next((item for item in position.exits if item["reason"] == "TP1"), None)
    gross = observation.gross_frictionless
    be, immediate = gross, gross
    if first is not None:
        immediate = position.quantity * position.sign * (Decimal(str(first["expected_price"])) - entry)
        be -= sum((
            Decimal(str(item["quantity"])) * position.sign * (Decimal(str(item["expected_price"])) - entry)
            for item in position.exits if item["reason"] == "STOP"
        ), Decimal(0))
    return {
        "recorded_gross_r": float(gross / observation.initial_risk),
        "recorded_post_tp1_stop_at_entry_gross_r": float(be / observation.initial_risk),
        "recorded_tp1_close_remaining_gross_r": float(immediate / observation.initial_risk),
    }


def trade_record(engine: DiagnosisEngine, position: Position) -> dict:
    parent.require(fully_verified(position), "DIAGNOSIS_REQUIRES_FULLY_VERIFIED")
    row = position.row()
    parent.require(position.closed_at is not None and position.initial_risk is not None,
                   "DIAGNOSIS_MISSING_VERIFIED_RISK_OR_CLOSE")
    series, mark = engine.data.frames[position.symbol]["15m"], engine.data.marks[position.symbol]
    start = bisect_left(series.times, position.opened_at)
    end = bisect_right(series.times, position.closed_at // 900 * 900)
    bars = []
    for candle in series.rows[start:end]:
        matching = mark.at(candle["time"])
        parent.require(matching is not None, "DIAGNOSIS_MISSING_VERIFIED_MARK")
        bars.append({
            "time": candle["time"],
            "contract": {key: candle[key] for key in ("open", "high", "low", "close")},
            "mark": {key: matching[key] for key in ("open", "high", "low", "close")},
        })
    risk_unit = position.initial_risk / float(position.quantity)
    entry, stop = float(position.spec["entry_price"]), float(position.spec["stop_loss"])
    facts = engine.entry_facts[id(position)]
    parent.require(position.tp1_hit == any(item["reason"] == "TP1" for item in position.exits),
                   "DIAGNOSIS_TP1_FILL_FLAG_MISMATCH")
    return {
        "symbol": position.symbol, "direction": position.direction, "signal_id": position.signal_identifier,
        "opened_at": stamp(position.opened_at), "closed_at": stamp(position.closed_at),
        "opened_at_epoch": position.opened_at, "closed_at_epoch": position.closed_at,
        "duration_seconds": position.closed_at - position.opened_at,
        "spec_entry_price": entry, "actual_fill_price": float(position.actual_entry),
        "market_open_price": float(position.market_open) if position.market_open is not None else entry,
        "initial_stop_price": stop, "initial_quantity": float(position.quantity),
        "initial_risk_usdt": position.initial_risk, "initial_risk_per_unit": risk_unit,
        "stop_distance_pct": abs(entry - stop) / entry * 100,
        **facts, "exit_reason": position.exits[-1]["reason"], "tp1_seen": position.tp1_hit,
        "path": classify(position.exits, position.tp1_hit), "net_r": row["net_r"],
        **excursions(bars, float(position.actual_entry), risk_unit, position.direction,
                     position.opened_at, position.closed_at),
        "arithmetic_scenarios": arithmetic_scenarios(position),
        "native_row": row, "bars_15m": bars,
    }


def cell(value, n: int) -> dict:
    return {"N": n, "value": value, "reliability": "guvenilmez" if n < 30 else "descriptive_only"}


def quantiles(values: list[float]) -> dict:
    return {label: cell(percentile(values, fraction) if values else None, len(values))
            for label, fraction in (("p05", .05), ("p25", .25), ("p50", .5), ("p75", .75), ("p95", .95))}


def paths_table(rows: list[dict]) -> dict:
    return {
        path: {
            "count": cell(len(group := [row for row in rows if row["path"] == path]), len(group)),
            "share_pct": cell(len(group) / len(rows) * 100 if rows else None, len(rows)),
            "mean_net_r": cell(fmean(row["net_r"] for row in group) if group else None, len(group)),
        } for path in PATHS
    }


def before_stop_table(rows: list[dict]) -> dict:
    stops = [row for row in rows if row["path"] == "STOP_BEFORE_TP1"]
    n = len(stops)
    return {
        "N": n, "reliability": "guvenilmez" if n < 30 else "descriptive_only",
        "duration_seconds": {
            label: cell(percentile([row["duration_seconds"] for row in stops], q) if n else None, n)
            for label, q in (("p25", .25), ("median", .5), ("p75", .75))
        },
        "thresholds": {
            str(threshold): {
                "confirmed_pct": cell(sum(row["confirmed_before_stop_mfe_r"] >= threshold
                                          for row in stops) / n * 100 if n else None, n),
                "envelope_upper_bound_pct": cell(sum(row["mfe_r"] >= threshold
                                                    for row in stops) / n * 100 if n else None, n),
                "terminal_intrabar_unresolved_pct": cell(sum(
                    row["confirmed_before_stop_mfe_r"] < threshold <= row["mfe_r"] for row in stops
                ) / n * 100 if n else None, n),
            } for threshold in (.5, 1.0)
        },
    }


def train_cuts(rows: list[dict]) -> dict:
    parent.require(bool(rows), "DIAGNOSIS_TRAIN_CUTS_REQUIRE_VERIFIED_TRADES")
    return {
        "source": "TRAIN", "N": len(rows),
        **{field: [percentile([row[field] for row in rows], q) for q in (1 / 3, 2 / 3)]
           for field in ("ema_distance_atr", "confidence")},
    }


def tertile(value: float, cuts: list[float]) -> str:
    return "LOW" if value <= cuts[0] else "MIDDLE" if value <= cuts[1] else "HIGH"


def tables(rows: list[dict], cuts: dict) -> dict:
    return {
        "N": len(rows), "paths": paths_table(rows), "stop_before_tp1": before_stop_table(rows),
        "excursions": {
            group: {field: quantiles([row[field] for row in selected])
                    for field in ("mfe_r", "mae_r", "time_to_mfe_seconds")}
            for group, selected in [("ALL", rows), *[
                (side, [row for row in rows if row["direction"] == side]) for side in ("LONG", "SHORT")
            ]]
        },
        "by_direction": {
            side: {"paths": paths_table(selected := [row for row in rows if row["direction"] == side]),
                   "stop_before_tp1": before_stop_table(selected)} for side in ("LONG", "SHORT")
        },
        "tertiles": {
            field: {
                group: {
                    "N": len(selected := [row for row in rows if tertile(row[field], cuts[field]) == group]),
                    "mean_net_r": cell(fmean(row["net_r"] for row in selected) if selected else None, len(selected)),
                    "stop_share_pct": cell(sum(row["exit_reason"] == "STOP" for row in selected)
                                          / len(selected) * 100 if selected else None, len(selected)),
                } for group in ("LOW", "MIDDLE", "HIGH")
            } for field in ("ema_distance_atr", "confidence")
        },
        "arithmetic_not_backtest_not_recommendation": {
            key: cell(fmean(row["arithmetic_scenarios"][key] for row in rows) if rows else None, len(rows))
            for key in ("recorded_gross_r", "recorded_post_tp1_stop_at_entry_gross_r",
                        "recorded_tp1_close_remaining_gross_r")
        },
    }


async def phase_result(data, plan, phase, reference, progress=None):
    parent.require(PHASES[0].start <= phase.start < phase.end <= PHASES[-1].end
                   and phase.start % 900 == phase.end % 900 == 0, "MEASUREMENT_SCOPE_DENIED")
    engine = DiagnosisEngine(data, primary_config(phase, plan))
    counts = await parent.gap.replay_counts(engine, phase, progress)
    parent.assert_count_parity(counts, reference, phase.name)
    rows = [trade_record(engine, position) for position in engine.trades if fully_verified(position)]
    parent.require(len(rows) == counts["counts"]["fully_verified_completed"], "DIAGNOSIS_VERIFIED_N_MISMATCH")
    return counts, rows


def validate_rows(rows: list[dict], name: str, counts: dict) -> None:
    parent.require(len(rows) == counts["counts"]["fully_verified_completed"], f"DIAGNOSIS_ROW_N:{name}")
    ids = set()
    for row in rows:
        key = (row["symbol"], row["opened_at_epoch"], row["signal_id"])
        parent.require(key not in ids, f"DIAGNOSIS_DUPLICATE_TRADE:{name}")
        ids.add(key)
        parent.require(row["direction"] in {"LONG", "SHORT"}
                       and row["path"] == classify(row["native_row"]["exits"], row["tp1_seen"]),
                       f"DIAGNOSIS_PATH:{name}")
        parent.require(row["native_row"]["status"] == "CLOSED"
                       and row["native_row"]["funding_complete"]
                       and row["native_row"]["remaining_quantity"] == 0
                       and row["net_r"] == row["native_row"]["net_r"], f"DIAGNOSIS_VERIFIED_ROW:{name}")
        parent.require(row["closed_at_epoch"] >= row["opened_at_epoch"]
                       and row["duration_seconds"] == row["closed_at_epoch"] - row["opened_at_epoch"],
                       f"DIAGNOSIS_DURATION:{name}")
        for field in ("net_r", "ema_distance_atr", "confidence", "stop_distance_pct", "mfe_r", "mae_r"):
            parent.require(type(row[field]) in (int, float) and math.isfinite(row[field]),
                           f"DIAGNOSIS_NONFINITE:{name}:{field}")
        parent.require(row["mfe_r"] >= row["confirmed_before_stop_mfe_r"] >= 0 and row["mae_r"] >= 0,
                       f"DIAGNOSIS_EXCURSION_ORDER:{name}")
    json.dumps(rows, allow_nan=False)


def write_raw(output: Path, name: str, rows: list[dict]) -> dict:
    path = output / f"trades-{name}.jsonl"
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False))
            handle.write("\n")
    loaded = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    parent.require(loaded == rows, f"DIAGNOSIS_RAW_ROUNDTRIP:{name}")
    return {"file": path.name, "sha256": sha256_file(path), "N": len(rows)}


def validate_report(report: dict, rows: dict[str, list[dict]]) -> None:
    parent.require(set(report) == {"schema", "definitions", "profile", "provenance", "train_cutpoints",
                                  "phases", "elapsed_seconds"}
                   and report["schema"] == SCHEMA and report["definitions"] == DEFINITIONS,
                   "DIAGNOSIS_FIXED_REGISTRATION")
    parent.require(report["profile"] == parent.asdict(parent.PROFILE)
                   and report["provenance"]["parent_prescreen_provenance"]["profile_hash"] == parent.PROFILE_HASH,
                   "DIAGNOSIS_PROFILE_CHANGED")
    parent.require(set(report["phases"]) == set(rows) == {"TRAIN", "VALIDATION"}, "DIAGNOSIS_PHASES")
    parent.require(report["train_cutpoints"] == train_cuts(rows["TRAIN"]), "DIAGNOSIS_TRAIN_ONLY_CUTS")
    for name, phase in report["phases"].items():
        counts = phase["native_counts"]
        parent.gap.validate_report({
            "schema": parent.gap.SCHEMA, "exception_record": parent.gap.exception_record(),
            "profile": parent.asdict(parent.PROFILE),
            "provenance": report["provenance"]["parent_prescreen_provenance"]["run3_provenance"],
            "phases": {name: counts}, "elapsed_seconds": 0,
        }, expected_phases={name})
        validate_rows(rows[name], name, counts)
        parent.require(phase["tables"] == tables(rows[name], report["train_cutpoints"]),
                       f"DIAGNOSIS_TABLES:{name}")
        parent.require(set(phase) == {"native_counts", "raw_trades", "tables"}, f"DIAGNOSIS_PHASE_KEYS:{name}")
        raw = phase["raw_trades"]
        parent.require(set(raw) == {"file", "sha256", "N"} and raw["file"] == f"trades-{name}.jsonl"
                       and raw["N"] == len(rows[name]) and isinstance(raw["sha256"], str)
                       and len(raw["sha256"]) == 64
                       and all(c in "0123456789abcdef" for c in raw["sha256"]), f"DIAGNOSIS_RAW_MANIFEST:{name}")
    parent.require(math.isfinite(report["elapsed_seconds"]) and report["elapsed_seconds"] >= 0,
                   "DIAGNOSIS_ELAPSED")
    json.dumps(report, allow_nan=False)


def guarded_main(argv=None) -> int:
    import sys

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata", required=True, type=Path)
    args = parser.parse_args(argv)
    loop, stream = None, sys.stdout
    try:
        parent.require(not OUTPUT.exists(), "RESULTS_ALREADY_EXIST")
        parent.require(args.metadata.resolve() == METADATA.resolve(), "METADATA_COPY_REQUIRED")
        plan = locked_plan()
        parent.require(sha256_file(args.metadata) == plan["input_sha256"]["metadata"], "LOCKED_METADATA_MISMATCH")
        parent.require(sha256_file(VIEW / "manifest.json") == VIEW_SHA, "LOCKED_VIEW_MISMATCH")
        references = parent.load_references()
        for name, expected in EXPECTED.items():
            c = references[name]["phases"][name]["counts"]
            parent.require(tuple(c[key] for key in ("accepted_entries", "completed", "fully_verified_completed"))
                           == expected, f"DIAGNOSIS_RUN3_PIN:{name}")
        OUTPUT.mkdir(parents=True, exist_ok=False)
        os.environ.update({"DATA_DIR": str(OUTPUT / "runtime"), "PROTREBOT_DATA_DIR": str(OUTPUT / "runtime"),
                           "DATABASE_URL": "", "PROTREBOT_DURABLE_AUTH_REQUIRED": "0", "ASSISTANT_LIVE_TESTS": "0"})
        loop = asyncio.new_event_loop()
        started = time.monotonic()
        with parent.quiet_native(), parent.deny_network():
            record = {
                "parent_prescreen_provenance": parent.provenance(plan, references),
                "diagnosis_script_sha256": sha256_file(Path(__file__)),
                "diagnosis_only": True, "protocol_relaxation": False, "experiment_attempt_used": False,
            }
            data = load_measurement_dataset(VIEW, args.metadata, PHASES[0].start, PHASES[-1].end)
            validate_loaded_scope(data, plan["symbols"])
            phases, raw_rows = {}, {}
            for phase in PHASES:
                counts, rows = loop.run_until_complete(phase_result(
                    data, plan, phase, references[phase.name]["phases"][phase.name],
                    progress=lambda value: emit(stream, value)))
                validate_rows(rows, phase.name, counts)
                phases[phase.name] = {"native_counts": counts}
                raw_rows[phase.name] = rows
                emit(stream, {"status": "phase_parity_verified", "phase": phase.name, "N": len(rows)})
        cuts = train_cuts(raw_rows["TRAIN"])
        for name, rows in raw_rows.items():
            phases[name].update(raw_trades=write_raw(OUTPUT, name, rows), tables=tables(rows, cuts))
        report = {
            "schema": SCHEMA, "definitions": copy.deepcopy(DEFINITIONS), "profile": parent.asdict(parent.PROFILE),
            "provenance": record, "train_cutpoints": cuts, "phases": phases,
            "elapsed_seconds": time.monotonic() - started,
        }
        validate_report(report, raw_rows)
        path = OUTPUT / "diagnosis.json"
        with path.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, allow_nan=False)
            handle.write("\n")
        emit(stream, {"status": "completed", "output": str(path), "sha256": sha256_file(path),
                      "raw_trades": {name: phase["raw_trades"] for name, phase in phases.items()}})
        return 0
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, ArithmeticError) as exc:
        emit(stream, {"status": "failed", "code": str(exc) if isinstance(exc, MeasurementError) else "NATIVE_FAILURE",
                      "error_class": type(exc).__name__, "detail": str(exc)})
        return 2
    finally:
        if loop is not None:
            loop.close()


def main(argv=None) -> int:
    with parent.gap.previous.research_path_guard():
        return guarded_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
