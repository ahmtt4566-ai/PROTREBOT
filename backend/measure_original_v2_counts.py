"""Pinned Original v2 offline-risk counts. Required gate accounting stays private."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

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
    stamp,
    validate_loaded_scope,
)
from measure_original_counts import METADATA, NATIVE_REASONS, VIEW_SHA, distribution

OUTPUT = Path.home() / "kaistrade-data" / "original-v2-measurement-results"
SCHEMA = "original-v2-offline-risk-counts-v1"
GROUP_KEYS = {"accepted_entries", "completed", "fully_verified_completed", "open_at_end", "unknown_data_gap"}
REASONS = NATIVE_REASONS | {"profile_cap", "INVALID_OFFLINE_STOP"}


@contextmanager
def research_path_guard():
    active = True

    def audit(event: str, args: tuple) -> None:
        if not active or event not in {"open", "os.listdir", "os.scandir", "os.remove",
                                       "os.rename", "os.mkdir", "os.rmdir"}:
            return
        for value in args[:2]:
            if isinstance(value, (str, bytes, os.PathLike)):
                text = os.fsdecode(value).lower().replace("\\", "/")
                if any(part in text for part in ("donchian-test-", "test-sealed.7z", "independent-block",
                                                 "donchian-holdout-results", "protrebot-research")):
                    raise MeasurementError("FORBIDDEN_RESEARCH_PATH_ACCESS")

    sys.addaudithook(audit)
    try:
        yield
    finally:
        active = False


async def replay_counts(
    engine: Any, phase: Phase, progress: Callable[[dict], None] | None = None,
) -> dict[str, Any]:
    from app import v25_execution as live
    from app.strategies.original_offline_engine import trade_counts

    if engine.positions or engine.events or engine.trades or engine.data.decisions:
        raise MeasurementError("PHASE_NOT_FRESH")
    symbols = sorted(set(engine.policy["allowed_symbols"]) & set(engine.data.frames))
    decisions: Counter[str] = Counter()
    closed_points = 0
    for at in range(phase.start, phase.end, STEP):
        if progress and (at - phase.start) % (30 * 86400) == 0:
            progress({"status": "progress", "phase": phase.name, "completed_bars": (at - phase.start) // STEP})
        for position in list(engine.positions.values()):
            engine.advance(position, at, opening_only=True)
        for symbol in symbols:
            closed_points += engine.data.frames[symbol]["15m"].at(at - STEP) is not None
            decisions[engine.data.canonical(symbol, at, engine.policy)["decision"]] += 1
        tickers = []
        for symbol in symbols:
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
            if engine.prefilter(native["analysis"]):
                signals.append((candidate, native["analysis"]))
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
        "rejections": {key: engine.rejections[key] for key in sorted(REASONS)},
        "rejections_by_stage": {stage: dict(sorted(values.items())) for stage, values in engine.stage_rejections.items()},
        "minimum_notional_quantity_by_symbol": {symbol: engine.minimum_by_symbol[symbol] for symbol in symbols},
        "stop_accepted": distribution(engine.accepted_distances),
        "completed_duration_hours": distribution(durations),
        "by_symbol": {symbol: trade_counts([p for p in engine.trades if p.symbol == symbol]) for symbol in symbols},
        "by_direction": {side: trade_counts([p for p in engine.trades if p.direction == side]) for side in ("LONG", "SHORT")},
        "by_entry_month": {key: trade_counts([p for p in engine.trades if month(p.opened_at) == key])
                           for key in sorted({month(at) for at in range(phase.start, phase.end, 86400)})},
        "policy_sha256": digest_object(engine.policy), "config_sha256": digest_object(asdict(engine.config)),
    }


async def measure_phases(data: Any, plan: dict, *, phases: tuple[Phase, ...] = PHASES,
                         progress: Callable[[dict], None] | None = None) -> dict:
    from app.strategies.original_offline_engine import OriginalOfflineRiskEngine

    if any(phase.start < PHASES[0].start or phase.end > PHASES[-1].end or phase.start >= phase.end
           or phase.start % STEP or phase.end % STEP for phase in phases):
        raise MeasurementError("MEASUREMENT_SCOPE_DENIED")
    return {phase.name: await replay_counts(
        OriginalOfflineRiskEngine(data, primary_config(phase, plan)), phase, progress) for phase in phases}


def validate_report(report: dict) -> None:
    def require(ok: bool) -> None:
        if not ok:
            raise MeasurementError("V2_COUNTS_SCHEMA_REJECTED")

    def count_map(value: dict, keys: set[str]) -> None:
        require(set(value) == keys and all(type(n) is int and n >= 0 for n in value.values()))

    def stats(value: dict) -> None:
        require(set(value) == {"count", "median", "p90", "p99"})
        count_map({"count": value["count"]}, {"count"})
        values = [value[key] for key in ("median", "p90", "p99")]
        require(all(n is None for n in values) if not value["count"] else
                all(type(n) in (float, int) and math.isfinite(n) and n >= 0 for n in values))
        if value["count"]:
            require(values == sorted(values))

    require(set(report) == {"schema", "profile", "provenance", "phases", "elapsed_seconds"})
    require(report["schema"] == SCHEMA)
    from app.strategies.original_offline_risk import PROFILE

    require(report["profile"] == asdict(PROFILE))
    provenance = report["provenance"]
    require(set(provenance) == {
        "profile_hash", "original_parameter_hash", "source_commit", "source_sha256",
        "metadata_sha256", "measurement_manifest_sha256", "stage6b_plan_sha256",
    })
    require(provenance["profile_hash"] == PROFILE.profile_hash)
    for key in ("profile_hash", "original_parameter_hash", "metadata_sha256",
                "measurement_manifest_sha256", "stage6b_plan_sha256"):
        require(isinstance(provenance[key], str) and len(provenance[key]) == 64
                and all(char in "0123456789abcdef" for char in provenance[key]))
    require(isinstance(provenance["source_commit"], str) and len(provenance["source_commit"]) == 40
            and all(char in "0123456789abcdef" for char in provenance["source_commit"]))
    sources = {"app/analysis.py", "app/main.py", "app/backtest_baseline.py", "app/execution_core.py",
               "app/v25_execution.py", "app/strategies/original_offline_risk.py",
               "app/strategies/original_offline_engine.py", "measure_original_v2_counts.py"}
    require(set(provenance["source_sha256"]) == sources)
    for value in provenance["source_sha256"].values():
        require(isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value))
    require(set(report["phases"]) == {"TRAIN", "VALIDATION"})
    require(type(report["elapsed_seconds"]) in (int, float)
            and math.isfinite(report["elapsed_seconds"]) and report["elapsed_seconds"] >= 0)
    phase_keys = {
        "period", "state_at_start", "counts", "canonical_distribution", "stop_rejection", "rejections",
        "rejections_by_stage", "minimum_notional_quantity_by_symbol", "stop_accepted", "completed_duration_hours",
        "by_symbol", "by_direction", "by_entry_month", "policy_sha256", "config_sha256",
    }
    for phase in report["phases"].values():
        require(set(phase) == phase_keys)
        require(set(phase["period"]) == {"start", "end_exclusive"}
                and all(isinstance(value, str) for value in phase["period"].values()))
        require(phase["state_at_start"] == {"positions": 0, "events": 0, "entries": 0})
        counts = phase["counts"]
        count_map(counts, GROUP_KEYS | {"grid_points", "closed_15m_points", "quality_passed",
                                       "native_canonical_evaluations", "attempted_entries", "risk_stage_arrivals"})
        count_map(phase["canonical_distribution"], {"BUY", "SELL", "WAIT"})
        count_map(phase["rejections"], REASONS)
        require(set(phase["rejections_by_stage"]) == {"PREFILTER", "ENTER"})
        for stage in phase["rejections_by_stage"].values():
            require(set(stage) <= REASONS)
            count_map(stage, set(stage))
        require(set(phase["by_direction"]) == {"LONG", "SHORT"})
        require(set(phase["by_symbol"]) <= {
            "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT",
        })
        for group in ("by_symbol", "by_direction", "by_entry_month"):
            for value in phase[group].values():
                count_map(value, GROUP_KEYS)
            for key in GROUP_KEYS:
                require(sum(value[key] for value in phase[group].values()) == counts[key])
        count_map(phase["minimum_notional_quantity_by_symbol"], set(phase["by_symbol"]))
        require(sum(phase["minimum_notional_quantity_by_symbol"].values()) == phase["rejections"]["min_notional"])
        for name in ("stop_accepted", "completed_duration_hours"):
            stats(phase[name])
        require(counts["accepted_entries"] == counts["completed"] + counts["open_at_end"] + counts["unknown_data_gap"])
        require(counts["fully_verified_completed"] <= counts["completed"])
        require(counts["accepted_entries"] <= counts["attempted_entries"])
        require(phase["stop_accepted"]["count"] == counts["accepted_entries"])
        require(phase["completed_duration_hours"]["count"] == counts["completed"])
        require(counts["grid_points"] == sum(phase["canonical_distribution"].values()))
        require(counts["quality_passed"] == phase["canonical_distribution"]["BUY"] + phase["canonical_distribution"]["SELL"])
        pre = phase["rejections_by_stage"]["PREFILTER"]
        require(counts["risk_stage_arrivals"] == counts["native_canonical_evaluations"]
                - sum(pre.get(key, 0) for key in NATIVE_REASONS if key.isupper() or key == "canonical_wait"))
        stop = phase["stop_rejection"]
        require(set(stop) == {"numerator", "denominator", "ratio"})
        count_map({key: stop[key] for key in ("numerator", "denominator")}, {"numerator", "denominator"})
        require(stop["denominator"] == counts["risk_stage_arrivals"] and stop["numerator"] <= stop["denominator"])
        require(stop["numerator"] == sum(phase["rejections"][key] for key in ("profile_cap", "stop_risk", "minimum_margin")))
        require(stop["ratio"] == (stop["numerator"] / stop["denominator"] if stop["denominator"] else None))
        for key in ("policy_sha256", "config_sha256"):
            require(isinstance(phase[key], str) and len(phase[key]) == 64
                    and all(c in "0123456789abcdef" for c in phase[key]))
    json.dumps(report, allow_nan=False)


def provenance(plan: dict) -> dict:
    from app import main as native
    from app.strategies.original_offline_risk import PROFILE
    from app.strategies.provenance import original_provenance

    policy = PROFILE.policy(primary_config(PHASES[0], plan).policy)
    effective = native._resolve_historical_policy_override({
        "confidence_threshold": policy["min_confidence"],
        "mtf_allow_either_timeframe": policy["mtf_allow_either_timeframe"],
    })
    sources = ("app/analysis.py", "app/main.py", "app/backtest_baseline.py", "app/execution_core.py",
               "app/v25_execution.py", "app/strategies/original_offline_risk.py",
               "app/strategies/original_offline_engine.py", "measure_original_v2_counts.py")
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=BACKEND.parent, text=True).strip()
    return {
        "profile_hash": PROFILE.profile_hash,
        "original_parameter_hash": original_provenance(effective, ("15m", "1h", "4h")).parameter_hash,
        "source_commit": commit,
        "source_sha256": {name: sha256_file(BACKEND.joinpath(*name.split("/"))) for name in sources},
        "metadata_sha256": plan["input_sha256"]["metadata"],
        "measurement_manifest_sha256": VIEW_SHA,
        "stage6b_plan_sha256": sha256_file(BACKEND / "studies" / "stage6b-plan.json"),
    }


def guarded_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata", required=True, type=Path)
    args = parser.parse_args(argv)
    stream, loop = sys.stdout, None
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
            from app.strategies.original_offline_risk import PROFILE

            record = provenance(plan)
            data = load_measurement_dataset(VIEW, args.metadata, PHASES[0].start, PHASES[-1].end)
            validate_loaded_scope(data, plan["symbols"])
            phases = loop.run_until_complete(measure_phases(data, plan, progress=lambda value: emit(stream, value)))
        report = {"schema": SCHEMA, "profile": asdict(PROFILE), "provenance": record,
                  "phases": phases, "elapsed_seconds": time.monotonic() - started}
        validate_report(report)
        path = OUTPUT / "counts.json"
        with path.open("x", encoding="utf-8") as output:
            json.dump(report, output, indent=2, allow_nan=False)
            output.write("\n")
        emit(stream, {"status": "completed", "output": str(path), "sha256": sha256_file(path)})
        for name, phase in phases.items():
            emit(stream, {"phase": name, "counts": phase["counts"], "stop_rejection": phase["stop_rejection"]})
        return 0
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, ArithmeticError) as exc:
        code = str(exc) if isinstance(exc, MeasurementError) else "NATIVE_FAILURE"
        emit(stream, {"status": "failed", "code": code, "error_class": type(exc).__name__})
        return 2
    finally:
        if loop is not None:
            loop.close()


def main(argv: list[str] | None = None) -> int:
    with research_path_guard():
        return guarded_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
