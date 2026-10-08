"""Pinned Original v2 performance pre-screen; no signal, profile or lifecycle changes.

Decision: both TRAIN and VALIDATION require fully verified N >= 150,
mean native net R > 0, and finite USDT PF > 1.15. Undefined PF fails.
R uses recorded initial entry risk, not the nominal 3-USDT budget.
Gross excludes commission, adverse fill slippage and funding on the SAME exits;
it is not a frictionless replay. Funding cashflow is signed; funding cost negates
it. Spread stays the native 2-bp gate input, with no new execution charge.
Drawdown groups verified net closes at equal timestamps; loss streaks order by
close, entry, symbol and signal ID, with breakevens breaking streaks.
Bootstrap is informational: 20000, seed 2026, nominal 95%, trades and all UTC
ENTRY days (including empty days), using the existing pure statistics helper.
Month groups use UTC entry month. Symbol output contains counts only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from statistics import fmean, median
from typing import Any

import measure_original_gap_counts as gap
from app.strategies.original_gap_risk import PROFILE
from build_measurement_view import load_measurement_dataset, sha256_file
from measure_donchian_counts import (
    BACKEND,
    PHASES,
    VIEW,
    MeasurementError,
    Phase,
    deny_network,
    emit,
    locked_plan,
    month,
    primary_config,
    quiet_native,
    stamp,
    validate_loaded_scope,
)
from measure_original_counts import METADATA, VIEW_SHA

MIN_N = 150
MIN_MEAN_NET_R = 0.0
MIN_USDT_PF = 1.15
BOOTSTRAP_SAMPLES = 20000
BOOTSTRAP_SEED = 2026
BOOTSTRAP_ALPHA = 0.05

OUTPUT = Path.home() / "kaistrade-data" / "original-v2-prescreen"
RUN3 = Path.home() / "kaistrade-data" / "original-v2-measurement-results-run3"
RUN3_HASHES = {
    "TRAIN": "a5a02a839c8c81b7d1851f0e060337ae4623e1225bb900e2f1ada9b58eb8f09e",
    "VALIDATION": "fce191eb3dc6b178151eab255cdc750010483b7c25b04ab7bd1f4e55d2167613",
}
PROFILE_HASH = "b21a9fbf7916808c6b0b9b26e6700725defe38dc4fb6a0151731deab8b251d81"
SCHEMA = "kais-original-v2-performance-prescreen-v1"
RULE = {
    "minimum_fully_verified_completed": MIN_N,
    "mean_net_r_strictly_above": MIN_MEAN_NET_R,
    "usdt_pf_strictly_above": MIN_USDT_PF,
    "undefined_pf": "FAIL",
    "both_phases_required": True,
}
DEFINITIONS = {
    "N": "NATIVE_FULLY_VERIFIED_CLOSED_ONLY_EXCLUDES_OPEN_UNKNOWN_INCOMPLETE_FUNDING",
    "R": "NATIVE_RECORDED_INITIAL_ENTRY_RISK",
    "gross": "SAME_EXECUTED_EXIT_QUANTITIES_AND_EXPECTED_PRICES_MINUS_UNSLIPPED_MARKET_OPEN_NO_FEES_OR_FUNDING",
    "funding": "SIGNED_CASHFLOW_CREDIT_POSITIVE_COST_IS_NEGATIVE_CASHFLOW",
    "spread": "NATIVE_2BP_GATE_INPUT_NO_ADDITIONAL_FILL_CHARGE",
    "drawdown": "VERIFIED_REALIZED_NET_CLOSES_GROUPED_BY_UTC_TIMESTAMP_NOT_MARK_TO_MARKET",
    "loss_streak": "NET_LOSSES_CLOSE_ENTRY_SYMBOL_SIGNAL_ORDER_BREAKEVEN_RESETS",
    "monthly": "UTC_ENTRY_MONTH",
    "bootstrap": "INFORMATIONAL_NOMINAL_95_TRADE_AND_ALL_UTC_ENTRY_DAY_BLOCKS_WITH_EMPTY_DAYS",
}
SYMBOL_COUNT_KEYS = {
    "accepted_entries", "completed", "fully_verified_completed", "open_at_end", "unknown_data_gap",
}


def require(ok: bool, message: str) -> None:
    if not ok:
        raise MeasurementError(message)


@dataclass(frozen=True)
class Observation:
    symbol: str
    direction: str
    opened_at: int
    closed_at: int
    signal_id: str
    initial_risk: Decimal
    net: Decimal
    net_r: float
    gross_frictionless: Decimal
    commission: Decimal
    slippage: Decimal
    funding: Decimal


def observe(position: Any) -> Observation:
    from app.strategies.original_offline_engine import fully_verified

    require(fully_verified(position), "OBSERVATION_REQUIRES_FULLY_VERIFIED_CLOSE")
    row = position.row()
    require(all(type(row[key]) in (int, float) and math.isfinite(row[key])
                for key in ("net_pnl", "net_r", "initial_risk_usdt")), "INVALID_NATIVE_ACCOUNTING")
    entry = position.market_open if position.market_open is not None else Decimal(str(position.spec["entry_price"]))
    quantities = [Decimal(str(exit_row["quantity"])) for exit_row in position.exits]
    require(sum(quantities, Decimal(0)) == position.quantity, "INCOMPLETE_EXIT_QUANTITY")
    frictionless = sum((
        quantity * position.sign * (Decimal(str(exit_row["expected_price"])) - entry)
        for quantity, exit_row in zip(quantities, position.exits, strict=True)), Decimal(0))
    slippage = frictionless - position.gross
    require(slippage >= Decimal("-1e-10"), "NEGATIVE_ADVERSE_SLIPPAGE")
    require(position.closed_at is not None, "CLOSED_TIME_MISSING")
    return Observation(
        position.symbol, position.direction, position.opened_at, position.closed_at, position.signal_identifier,
        Decimal(str(position.initial_risk)), Decimal(str(row["net_pnl"])), row["net_r"],
        frictionless, position.commission, slippage, position.funding_amount,
    )


def verified_observations(positions: list[Any]) -> list[Observation]:
    from app.strategies.original_offline_engine import fully_verified

    return [observe(position) for position in positions if fully_verified(position)]


def summarize(observations: list[Observation]) -> tuple[dict, dict]:
    ordered = sorted(observations, key=lambda row: (row.closed_at, row.opened_at, row.symbol, row.signal_id))
    values = [row.net_r for row in ordered]
    gains = sum((max(row.net, Decimal(0)) for row in ordered), Decimal(0))
    losses = -sum((min(row.net, Decimal(0)) for row in ordered), Decimal(0))
    net = sum((row.net for row in ordered), Decimal(0))
    streak = longest = 0
    curve: dict[int, Decimal] = {}
    for row in ordered:
        streak = streak + 1 if row.net < 0 else 0
        longest = max(longest, streak)
        curve[row.closed_at] = curve.get(row.closed_at, Decimal(0)) + row.net
    current = peak = drawdown = Decimal(0)
    for at in sorted(curve):
        current += curve[at]
        peak = max(peak, current)
        drawdown = max(drawdown, peak - current)
    costs = {
        "commission_usdt": float(sum((row.commission for row in ordered), Decimal(0))),
        "slippage_usdt": float(sum((row.slippage for row in ordered), Decimal(0))),
        "funding_cashflow_usdt": float(sum((row.funding for row in ordered), Decimal(0))),
        "funding_cost_usdt": float(-sum((row.funding for row in ordered), Decimal(0))),
        "mean_cost_r": {
            "commission": fmean(float(row.commission / row.initial_risk) for row in ordered) if ordered else None,
            "slippage": fmean(float(row.slippage / row.initial_risk) for row in ordered) if ordered else None,
            "funding": fmean(float(-row.funding / row.initial_risk) for row in ordered) if ordered else None,
        },
    }
    metrics = {
        "N": len(ordered), "mean_net_r": fmean(values) if values else None,
        "median_net_r": median(values) if values else None,
        "usdt_pf": float(gains / losses) if losses else None,
        "pf_status": "DEFINED" if losses else "NO_LOSSES" if ordered else "NO_COMPLETE_TRADES",
        "win_rate": sum(row.net > 0 for row in ordered) / len(ordered) if ordered else None,
        "total_net_pnl_usdt": float(net), "longest_net_loss_streak": longest,
        "max_drawdown_usdt": float(drawdown) if ordered else None,
        "mean_gross_r": fmean(float(row.gross_frictionless / row.initial_risk) for row in ordered) if ordered else None,
    }
    require(all(value is None or not isinstance(value, (int, float)) or math.isfinite(value)
                for value in metrics.values()), "NONFINITE_AGGREGATE")
    return metrics, costs


def bootstrap_mean(observations: list[Observation], phase: Phase) -> dict:
    from app.holdout_statistics import bootstrap

    rows = [{
        "status": "CLOSED", "net_pnl": float(row.net), "net_r": row.net_r, "opened_at": stamp(row.opened_at),
    } for row in observations]
    # Reuse the registered sampler; only its nominal mean-R intervals are reported.
    sampled = bootstrap(rows, [(phase.start, phase.end)], {
        "samples": BOOTSTRAP_SAMPLES, "seed": BOOTSTRAP_SEED, "alpha": BOOTSTRAP_ALPHA, "family_size": 3,
    })
    return {
        "samples": BOOTSTRAP_SAMPLES, "seed": BOOTSTRAP_SEED, "confidence": 0.95, "informational_only": True,
        "methods": {
            method: {
                "status": value["status"], "mean_net_r_95": value["nominal_95"]["expectancy_r"],
                "active_clusters": value["active_clusters"],
                "calendar_or_trade_clusters": value["calendar_or_trade_clusters"],
                "valid_resamples": value["valid_r_resamples"], "empty_resamples": value["empty_resamples"],
            } for method, value in sampled.items()
        },
    }


def decision(phases: dict[str, dict]) -> dict:
    require(set(phases) == {"TRAIN", "VALIDATION"}, "DECISION_REQUIRES_BOTH_PHASES")
    failures = []
    for name in ("TRAIN", "VALIDATION"):
        value = phases[name]["metrics"]
        require(type(value["N"]) is int and value["N"] >= 0, f"INVALID_N:{name}")
        if value["N"] < MIN_N:
            failures.append(f"{name}:N_LT_150")
        mean, pf = value["mean_net_r"], value["usdt_pf"]
        if mean is None or not math.isfinite(mean) or mean <= MIN_MEAN_NET_R:
            failures.append(f"{name}:MEAN_NET_R_NOT_GT_ZERO")
        if pf is None or not math.isfinite(pf) or pf <= MIN_USDT_PF:
            failures.append(f"{name}:USDT_PF_UNDEFINED_OR_NOT_GT_1.15")
    return {"value": "DUR" if failures else "DEVAM", "failed_conditions": failures}


def assert_count_parity(actual: dict, expected: dict, name: str) -> None:
    # Exact full count-report equality also covers exits, gates, sizing and inventory.
    if actual != expected:
        different = sorted(key for key in set(actual) | set(expected) if actual.get(key) != expected.get(key))
        raise MeasurementError(f"RUN3_COUNT_PARITY_MISMATCH phase={name} keys={','.join(different)}")


async def phase_result(data: Any, plan: dict, phase: Phase, reference: dict, progress=None) -> dict:
    from app.strategies.original_gap_engine import OriginalGapRiskEngine

    engine = OriginalGapRiskEngine(data, primary_config(phase, plan))
    counts = await gap.replay_counts(engine, phase, progress)
    assert_count_parity(counts, reference, phase.name)
    observations = verified_observations(engine.trades)
    require(len(observations) == counts["counts"]["fully_verified_completed"], "VERIFIED_N_MISMATCH")
    metrics, costs = summarize(observations)
    return {
        "native_counts": counts, "metrics": metrics, "costs": costs,
        "by_direction": {
            side: {
                "N": len(selected := [row for row in observations if row.direction == side]),
                "mean_net_r": fmean(row.net_r for row in selected) if selected else None,
            } for side in ("LONG", "SHORT")
        },
        "by_entry_month": {
            key: {
                "N": len(selected := [row for row in observations if month(row.opened_at) == key]),
                "net_pnl_usdt": float(sum((row.net for row in selected), Decimal(0))),
            } for key in counts["by_entry_month"]
        },
        "by_symbol_counts": counts["by_symbol"], "bootstrap": bootstrap_mean(observations, phase),
    }


async def run_phases(data: Any, plan: dict, references: dict, *, phases=PHASES, progress=None) -> dict:
    require(len(phases) == 2 and {phase.name for phase in phases} == {"TRAIN", "VALIDATION"}, "BOTH_PHASES_REQUIRED")
    require(all(PHASES[0].start <= phase.start < phase.end <= PHASES[-1].end
                and phase.start % 900 == phase.end % 900 == 0 for phase in phases), "MEASUREMENT_SCOPE_DENIED")
    return {
        phase.name: await phase_result(data, plan, phase, references[phase.name], progress) for phase in phases
    }


def load_references() -> dict:
    references = {}
    for phase in PHASES:
        path = RUN3 / phase.name / "counts.json"
        require(sha256_file(path) == RUN3_HASHES[phase.name], f"RUN3_HASH_MISMATCH:{phase.name}")
        report = json.loads(path.read_text(encoding="utf-8"))
        gap.validate_report(report, expected_phases={phase.name})
        require(report["provenance"]["profile_hash"] == PROFILE_HASH, "RUN3_PROFILE_MISMATCH")
        references[phase.name] = report
    require(references["TRAIN"]["provenance"] == references["VALIDATION"]["provenance"], "RUN3_PROVENANCE_MISMATCH")
    return references


def provenance(plan: dict, references: dict) -> dict:
    current = gap.provenance(plan)
    source = references["TRAIN"]["provenance"]
    require({key: value for key, value in current.items() if key != "source_commit"}
            == {key: value for key, value in source.items() if key != "source_commit"},
            "RUN3_SOURCES_OR_SETTINGS_CHANGED")
    require(PROFILE.profile_hash == PROFILE_HASH, "PINNED_PROFILE_CHANGED")
    return {
        "profile_hash": PROFILE_HASH, "run3_provenance": source,
        "run3_report_sha256": RUN3_HASHES,
        "prescreen_source_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=BACKEND.parent, text=True).strip(),
        "prescreen_script_sha256": sha256_file(Path(__file__)),
        "statistics_source_sha256": {
            name: sha256_file(BACKEND.joinpath(*name.split("/"))) for name in
            ("app/holdout_statistics.py", "app/regime_statistics.py", "app/trade_r_metrics.py")
        },
    }


def validate_report(report: dict) -> None:
    def keys(value: Any, expected: set[str], path: str) -> None:
        require(isinstance(value, dict), f"PRESCREEN_SCHEMA mapping:{path}")
        require(set(value) == expected, f"PRESCREEN_SCHEMA keys:{path}")

    def number(value: Any, path: str, *, nullable: bool = False) -> None:
        require((nullable and value is None) or (type(value) in (int, float) and math.isfinite(value)),
                f"PRESCREEN_SCHEMA finite_number:{path}")

    def count(value: Any, path: str) -> None:
        require(type(value) is int and value >= 0, f"PRESCREEN_SCHEMA nonnegative_integer:{path}")

    keys(report, {"schema", "decision_rule", "definitions", "profile", "provenance",
                  "phases", "decision", "elapsed_seconds"}, "report")
    require(report["schema"] == SCHEMA and report["decision_rule"] == RULE
            and report["definitions"] == DEFINITIONS and report["profile"] == asdict(PROFILE),
            "PRESCREEN_SCHEMA fixed_registration")
    record = report["provenance"]
    keys(record, {"profile_hash", "run3_provenance", "run3_report_sha256",
                  "prescreen_source_commit", "prescreen_script_sha256", "statistics_source_sha256"}, "provenance")
    require(record["profile_hash"] == PROFILE_HASH and record["run3_report_sha256"] == RUN3_HASHES,
            "PRESCREEN_SCHEMA pinned_provenance")
    keys(record["statistics_source_sha256"], {
        "app/holdout_statistics.py", "app/regime_statistics.py", "app/trade_r_metrics.py",
    }, "provenance.statistics")
    for name, digest in {
        "prescreen_source_commit": record["prescreen_source_commit"],
        "prescreen_script_sha256": record["prescreen_script_sha256"], **record["statistics_source_sha256"],
    }.items():
        require(isinstance(digest, str) and len(digest) == (40 if name == "prescreen_source_commit" else 64)
                and all(char in "0123456789abcdef" for char in digest), f"PRESCREEN_SCHEMA digest:{name}")
    number(report["elapsed_seconds"], "elapsed_seconds")
    require(report["elapsed_seconds"] >= 0, "PRESCREEN_SCHEMA elapsed_seconds")
    keys(report["phases"], {"TRAIN", "VALIDATION"}, "phases")
    metric_keys = {"N", "mean_net_r", "median_net_r", "usdt_pf", "pf_status", "win_rate",
                   "total_net_pnl_usdt", "longest_net_loss_streak", "max_drawdown_usdt", "mean_gross_r"}
    for name, phase in report["phases"].items():
        keys(phase, {"native_counts", "metrics", "costs", "by_direction", "by_entry_month",
                     "by_symbol_counts", "bootstrap"}, f"phases.{name}")
        keys(phase["metrics"], metric_keys, f"phases.{name}.metrics")
        counts = phase["native_counts"]
        gap.validate_report({
            "schema": gap.SCHEMA, "exception_record": gap.exception_record(), "profile": asdict(PROFILE),
            "provenance": record["run3_provenance"], "phases": {name: counts}, "elapsed_seconds": 0,
        }, expected_phases={name})
        require(phase["metrics"]["N"] == counts["counts"]["fully_verified_completed"], f"PRESCREEN_SCHEMA N:{name}")
        for key, value in phase["metrics"].items():
            if key in {"N", "longest_net_loss_streak"}:
                count(value, f"{name}.metrics.{key}")
            elif key != "pf_status":
                number(value, f"{name}.metrics.{key}", nullable=key != "total_net_pnl_usdt")
        metrics = phase["metrics"]
        require(metrics["pf_status"] in {"DEFINED", "NO_LOSSES", "NO_COMPLETE_TRADES"}
                and (metrics["usdt_pf"] is not None) == (metrics["pf_status"] == "DEFINED"),
                f"PRESCREEN_SCHEMA pf_status:{name}")
        require(metrics["usdt_pf"] is None or metrics["usdt_pf"] >= 0, f"PRESCREEN_SCHEMA pf_sign:{name}")
        require(metrics["win_rate"] is None or 0 <= metrics["win_rate"] <= 1, f"PRESCREEN_SCHEMA win_rate:{name}")
        require(metrics["max_drawdown_usdt"] is None or metrics["max_drawdown_usdt"] >= 0,
                f"PRESCREEN_SCHEMA drawdown:{name}")
        keys(phase["by_symbol_counts"], set(counts["by_symbol"]), f"phases.{name}.symbols")
        for symbol, value in phase["by_symbol_counts"].items():
            keys(value, SYMBOL_COUNT_KEYS, f"phases.{name}.symbols.{symbol}")
        require(phase["by_symbol_counts"] == counts["by_symbol"], f"PRESCREEN_SCHEMA symbol_counts:{name}")
        keys(phase["by_direction"], {"LONG", "SHORT"}, f"phases.{name}.by_direction")
        for side, value in phase["by_direction"].items():
            keys(value, {"N", "mean_net_r"}, f"phases.{name}.direction")
            count(value["N"], f"{name}.{side}.N")
            number(value["mean_net_r"], f"{name}.{side}.mean_net_r", nullable=value["N"] == 0)
            require(value["N"] == counts["by_direction"][side]["fully_verified_completed"],
                    f"PRESCREEN_SCHEMA direction_N:{name}.{side}")
        keys(phase["by_entry_month"], set(counts["by_entry_month"]), f"phases.{name}.months")
        for key, value in phase["by_entry_month"].items():
            keys(value, {"N", "net_pnl_usdt"}, f"phases.{name}.month")
            count(value["N"], f"{name}.{key}.N")
            number(value["net_pnl_usdt"], f"{name}.{key}.net_pnl_usdt")
            require(value["N"] == counts["by_entry_month"][key]["fully_verified_completed"],
                    f"PRESCREEN_SCHEMA month_N:{name}.{key}")
        require(sum(value["N"] for value in phase["by_direction"].values()) == phase["metrics"]["N"]
                == sum(value["N"] for value in phase["by_entry_month"].values()), f"PRESCREEN_SCHEMA grouped_N:{name}")
        require(math.isclose(sum(value["net_pnl_usdt"] for value in phase["by_entry_month"].values()),
                             metrics["total_net_pnl_usdt"], rel_tol=1e-12, abs_tol=1e-10),
                f"PRESCREEN_SCHEMA monthly_accounting:{name}")
        costs = phase["costs"]
        keys(costs, {"commission_usdt", "slippage_usdt", "funding_cashflow_usdt", "funding_cost_usdt",
                     "mean_cost_r"}, f"phases.{name}.costs")
        keys(costs["mean_cost_r"], {"commission", "slippage", "funding"}, f"phases.{name}.cost_r")
        for key, value in costs.items():
            if key != "mean_cost_r":
                number(value, f"{name}.costs.{key}")
        for key, value in costs["mean_cost_r"].items():
            number(value, f"{name}.mean_cost_r.{key}", nullable=metrics["N"] == 0)
        require(costs["funding_cost_usdt"] == -costs["funding_cashflow_usdt"],
                f"PRESCREEN_SCHEMA funding_sign:{name}")
        if metrics["N"]:
            require(math.isclose(metrics["mean_net_r"],
                                 metrics["mean_gross_r"] - sum(costs["mean_cost_r"].values()),
                                 rel_tol=1e-10, abs_tol=1e-9), f"PRESCREEN_SCHEMA cost_identity:{name}")
        boot = phase["bootstrap"]
        keys(boot, {"samples", "seed", "confidence", "informational_only", "methods"}, f"phases.{name}.bootstrap")
        require((boot["samples"], boot["seed"], boot["confidence"], boot["informational_only"])
                == (BOOTSTRAP_SAMPLES, BOOTSTRAP_SEED, 0.95, True), f"PRESCREEN_SCHEMA bootstrap_settings:{name}")
        keys(boot["methods"], {"TRADE", "DAY"}, f"phases.{name}.bootstrap.methods")
        for method in boot["methods"].values():
            keys(method, {"status", "mean_net_r_95", "active_clusters", "calendar_or_trade_clusters",
                          "valid_resamples", "empty_resamples"}, f"phases.{name}.bootstrap.method")
            for key in ("active_clusters", "calendar_or_trade_clusters", "valid_resamples", "empty_resamples"):
                count(method[key], f"{name}.bootstrap.{key}")
            bounds = method["mean_net_r_95"]
            require(bounds is None or (isinstance(bounds, list) and len(bounds) == 2),
                    f"PRESCREEN_SCHEMA bootstrap_interval:{name}")
            if bounds is not None:
                for value in bounds:
                    number(value, f"{name}.bootstrap.bounds")
                require(bounds[0] <= bounds[1], f"PRESCREEN_SCHEMA bootstrap_bounds:{name}")
    require(report["decision"] == decision(report["phases"]), "PRESCREEN_SCHEMA decision")
    json.dumps(report, allow_nan=False)


def guarded_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata", required=True, type=Path)
    args = parser.parse_args(argv)
    loop, stream = None, sys.stdout
    try:
        require(not OUTPUT.exists(), "RESULTS_ALREADY_EXIST")
        require(args.metadata.resolve() == METADATA.resolve(), "METADATA_COPY_REQUIRED")
        plan = locked_plan()
        require(sha256_file(args.metadata) == plan["input_sha256"]["metadata"], "LOCKED_METADATA_MISMATCH")
        require(sha256_file(VIEW / "manifest.json") == VIEW_SHA, "LOCKED_VIEW_MISMATCH")
        references = load_references()
        OUTPUT.mkdir(parents=True, exist_ok=False)
        os.environ.update({"DATA_DIR": str(OUTPUT / "runtime"), "PROTREBOT_DATA_DIR": str(OUTPUT / "runtime"),
                           "DATABASE_URL": "", "PROTREBOT_DURABLE_AUTH_REQUIRED": "0", "ASSISTANT_LIVE_TESTS": "0"})
        loop = asyncio.new_event_loop()
        started = time.monotonic()
        with quiet_native(), deny_network():
            record = provenance(plan, references)
            data = load_measurement_dataset(VIEW, args.metadata, PHASES[0].start, PHASES[-1].end)
            validate_loaded_scope(data, plan["symbols"])
            phases = loop.run_until_complete(run_phases(
                data, plan, {name: value["phases"][name] for name, value in references.items()},
                progress=lambda value: emit(stream, value)))
        report = {"schema": SCHEMA, "decision_rule": RULE, "definitions": DEFINITIONS,
                  "profile": asdict(PROFILE), "provenance": record, "phases": phases,
                  "decision": decision(phases), "elapsed_seconds": time.monotonic() - started}
        validate_report(report)
        path = OUTPUT / "prescreen.json"
        with path.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, allow_nan=False)
            handle.write("\n")
        emit(stream, {"status": "completed", "output": str(path), "sha256": sha256_file(path),
                      "decision": report["decision"]})
        return 0
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, ArithmeticError) as exc:
        emit(stream, {"status": "failed", "code": str(exc) if isinstance(exc, MeasurementError) else "NATIVE_FAILURE",
                      "error_class": type(exc).__name__})
        return 2
    finally:
        if loop is not None:
            loop.close()


def main(argv: list[str] | None = None) -> int:
    with gap.previous.research_path_guard():
        return guarded_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
