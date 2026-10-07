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
from datetime import datetime, timezone
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
    reason_counts,
    stamp,
    validate_loaded_scope,
)
from measure_original_counts import METADATA, NATIVE_REASONS, VIEW_SHA, distribution

OUTPUT = Path.home() / "kaistrade-data" / "original-v2-measurement-results-run2"
SCHEMA = "original-v2-offline-risk-counts-v2"
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
    first_accounting_rejection = None
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
    normalized = {key: 0 for key in sorted(REASONS)}
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
        "closure_trace": closure_trace(engine, first_accounting_rejection),
        "minimum_notional_quantity_by_symbol": {symbol: engine.minimum_by_symbol[symbol] for symbol in symbols},
        "stop_accepted": distribution(engine.accepted_distances),
        "completed_duration_hours": distribution(durations),
        "by_symbol": {symbol: trade_counts([p for p in engine.trades if p.symbol == symbol]) for symbol in symbols},
        "by_direction": {side: trade_counts([p for p in engine.trades if p.direction == side]) for side in ("LONG", "SHORT")},
        "by_entry_month": {key: trade_counts([p for p in engine.trades if month(p.opened_at) == key])
                           for key in sorted({month(at) for at in range(phase.start, phase.end, 86400)})},
        "policy_sha256": digest_object(engine.policy), "config_sha256": digest_object(asdict(engine.config)),
    }


def closure_trace(engine: Any, first_rejection: str | None) -> dict:
    unverified = {
        event["plan_id"]: event["created_at"] for event in engine.events
        if event.get("kind") == "LIVE_POSITION_CLOSED_UNVERIFIED"
    }
    unknown, funding_incomplete = [], []
    for position in engine.trades:
        if position.status == "UNKNOWN_DATA_GAP":
            detected = unverified.get(position.signal_identifier)
            if detected is None:
                raise MeasurementError("UNKNOWN_CLOSURE_EVIDENCE_MISSING")
            unknown.append({"symbol": position.symbol, "opened_at": stamp(position.opened_at),
                            "detected_at": detected})
        elif position.status == "CLOSED" and not position.funding_known:
            if position.closed_at is None:
                raise MeasurementError("CLOSED_TIME_MISSING")
            funding_incomplete.append({"symbol": position.symbol, "opened_at": stamp(position.opened_at),
                                       "closed_at": stamp(position.closed_at)})
    unknown.sort(key=lambda row: (row["detected_at"], row["symbol"], row["opened_at"]))
    funding_incomplete.sort(key=lambda row: (row["closed_at"], row["symbol"], row["opened_at"]))
    return {
        "unknown_data_gap": {"count": len(unknown), "items": unknown},
        "funding_incomplete_closed": {"count": len(funding_incomplete), "items": funding_incomplete},
        "accounting_lock": {
            "first_lock_at": unknown[0]["detected_at"] if unknown else None,
            "first_rejection_at": first_rejection,
            "rejected_candidates_since_lock": engine.rejections["pnl_verified"],
        },
    }


async def measure_phases(data: Any, plan: dict, *, phases: tuple[Phase, ...] = PHASES,
                         progress: Callable[[dict], None] | None = None) -> dict:
    from app.strategies.original_offline_engine import OriginalOfflineRiskEngine

    if any(phase.start < PHASES[0].start or phase.end > PHASES[-1].end or phase.start >= phase.end
           or phase.start % STEP or phase.end % STEP for phase in phases):
        raise MeasurementError("MEASUREMENT_SCOPE_DENIED")
    return {phase.name: await replay_counts(
        OriginalOfflineRiskEngine(data, primary_config(phase, plan)), phase, progress) for phase in phases}


def validate_report(report: dict, *, expected_phases: set[str] | None = None) -> None:
    from app.strategies.original_offline_risk import PROFILE

    def require(ok: bool, control: str, path: str, keys: set[str] | None = None) -> None:
        if not ok:
            detail = f" keys={','.join(sorted(str(key) for key in keys))}" if keys else ""
            raise MeasurementError(f"V2_COUNTS_SCHEMA_REJECTED control={control} path={path}{detail}")

    def key_map(value: Any, keys: set[str], path: str) -> None:
        require(isinstance(value, dict), "mapping_type", path)
        require(set(value) == keys, "mapping_keys", path, set(value) ^ keys)

    def count_map(value: dict, keys: set[str], path: str) -> None:
        key_map(value, keys, path)
        bad = {key for key, n in value.items() if type(n) is not int or n < 0}
        require(not bad, "nonnegative_integer", path, bad)

    def digest(value: Any, path: str, length: int = 64) -> None:
        require(isinstance(value, str) and len(value) == length
                and all(c in "0123456789abcdef" for c in value), "hex_digest", path)

    def stats(value: dict, path: str) -> None:
        key_map(value, {"count", "median", "p90", "p99"}, path)
        count_map({"count": value["count"]}, {"count"}, f"{path}.count")
        names = ("median", "p90", "p99")
        bad = {key for key in names if
               (value[key] is not None if not value["count"] else
                type(value[key]) not in (int, float) or not math.isfinite(value[key]) or value[key] < 0)}
        require(not bad, "quantile_values", path, bad)
        if value["count"]:
            require([value[key] for key in names] == sorted(value[key] for key in names), "quantile_order", path)

    def timestamp(value: Any, path: str) -> datetime:
        require(isinstance(value, str), "timestamp_type", path)
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise MeasurementError(f"V2_COUNTS_SCHEMA_REJECTED control=timestamp_format path={path}") from exc
        require(parsed.tzinfo is not None and parsed.utcoffset() == timezone.utc.utcoffset(parsed),
                "timestamp_utc", path)
        return parsed

    key_map(report, {"schema", "profile", "provenance", "phases", "elapsed_seconds"}, "report")
    require(report["schema"] == SCHEMA, "schema_id", "schema")
    key_map(report["profile"], set(asdict(PROFILE)), "profile")
    require(report["profile"] == asdict(PROFILE), "fixed_profile", "profile")
    record = report["provenance"]
    key_map(record, {"profile_hash", "original_parameter_hash", "source_commit", "source_sha256",
                     "metadata_sha256", "measurement_manifest_sha256", "stage6b_plan_sha256"}, "provenance")
    require(record["profile_hash"] == PROFILE.profile_hash, "profile_hash", "provenance.profile_hash")
    for key in ("profile_hash", "original_parameter_hash", "metadata_sha256",
                "measurement_manifest_sha256", "stage6b_plan_sha256"):
        digest(record[key], f"provenance.{key}")
    digest(record["source_commit"], "provenance.source_commit", 40)
    sources = {"app/analysis.py", "app/main.py", "app/backtest_baseline.py", "app/execution_core.py",
               "app/v25_execution.py", "app/strategies/original_offline_risk.py",
               "app/strategies/original_offline_engine.py", "measure_original_v2_counts.py"}
    key_map(record["source_sha256"], sources, "provenance.source_sha256")
    for key, value in record["source_sha256"].items():
        digest(value, f"provenance.source_sha256.{key}")
    require(isinstance(report["phases"], dict), "mapping_type", "phases")
    names = set(report["phases"])
    require(bool(names) and names <= {"TRAIN", "VALIDATION"}, "phase_selection", "phases", names - {"TRAIN", "VALIDATION"})
    if expected_phases is not None:
        require(names == expected_phases, "requested_phases", "phases", names ^ expected_phases)
    require(type(report["elapsed_seconds"]) in (int, float)
            and math.isfinite(report["elapsed_seconds"]) and report["elapsed_seconds"] >= 0,
            "elapsed_seconds", "elapsed_seconds")
    phase_keys = {
        "period", "state_at_start", "counts", "canonical_distribution", "stop_rejection", "rejections",
        "rejections_by_stage", "minimum_notional_quantity_by_symbol", "stop_accepted", "completed_duration_hours",
        "by_symbol", "by_direction", "by_entry_month", "policy_sha256", "config_sha256",
        "gate_rejections", "gate_evaluations", "closure_trace",
    }
    for name, phase in report["phases"].items():
        path = f"phases.{name}"
        key_map(phase, phase_keys, path)
        key_map(phase["period"], {"start", "end_exclusive"}, f"{path}.period")
        start = timestamp(phase["period"]["start"], f"{path}.period.start")
        end = timestamp(phase["period"]["end_exclusive"], f"{path}.period.end_exclusive")
        require(start < end, "period_order", f"{path}.period")
        require(phase["state_at_start"] == {"positions": 0, "events": 0, "entries": 0},
                "fresh_state", f"{path}.state_at_start")
        counts = phase["counts"]
        count_map(counts, GROUP_KEYS | {"grid_points", "closed_15m_points", "quality_passed",
                                       "native_canonical_evaluations", "attempted_entries", "risk_stage_arrivals"},
                  f"{path}.counts")
        count_map(phase["canonical_distribution"], {"BUY", "SELL", "WAIT"}, f"{path}.canonical_distribution")
        count_map(phase["rejections"], REASONS, f"{path}.rejections")
        key_map(phase["rejections_by_stage"], {"PREFILTER", "ENTER"}, f"{path}.rejections_by_stage")
        for group in ("gate_rejections", "gate_evaluations", "rejections_by_stage"):
            tables = phase[group] if group == "rejections_by_stage" else {group: phase[group]}
            for key, values in tables.items():
                here = f"{path}.{group}.{key}" if group == "rejections_by_stage" else f"{path}.{group}"
                require(isinstance(values, dict), "mapping_type", here)
                require(set(values) <= REASONS, "normalized_native_reasons", here, set(values) - REASONS)
                count_map(values, set(values), here)
        key_map(phase["by_direction"], {"LONG", "SHORT"}, f"{path}.by_direction")
        require(isinstance(phase["by_symbol"], dict), "mapping_type", f"{path}.by_symbol")
        require(set(phase["by_symbol"]) <= {
            "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT",
        }, "symbol_scope", f"{path}.by_symbol", set(phase["by_symbol"]))
        for group in ("by_symbol", "by_direction", "by_entry_month"):
            require(isinstance(phase[group], dict), "mapping_type", f"{path}.{group}")
            for key, value in phase[group].items():
                count_map(value, GROUP_KEYS, f"{path}.{group}.{key}")
            for key in GROUP_KEYS:
                require(sum(value[key] for value in phase[group].values()) == counts[key],
                        "group_count_sum", f"{path}.{group}.{key}")
        count_map(phase["minimum_notional_quantity_by_symbol"], set(phase["by_symbol"]),
                  f"{path}.minimum_notional_quantity_by_symbol")
        require(sum(phase["minimum_notional_quantity_by_symbol"].values()) == phase["rejections"]["min_notional"],
                "minimum_order_sum", f"{path}.minimum_notional_quantity_by_symbol")
        for key in ("stop_accepted", "completed_duration_hours"):
            stats(phase[key], f"{path}.{key}")
        checks = {
            "accepted_partition": counts["accepted_entries"] == counts["completed"] + counts["open_at_end"] + counts["unknown_data_gap"],
            "verified_completed": counts["fully_verified_completed"] <= counts["completed"],
            "accepted_attempts": counts["accepted_entries"] <= counts["attempted_entries"],
            "accepted_stop_count": phase["stop_accepted"]["count"] == counts["accepted_entries"],
            "completed_duration_count": phase["completed_duration_hours"]["count"] == counts["completed"],
            "grid_distribution": counts["grid_points"] == sum(phase["canonical_distribution"].values()),
            "quality_distribution": counts["quality_passed"] == phase["canonical_distribution"]["BUY"] + phase["canonical_distribution"]["SELL"],
        }
        for control, ok in checks.items():
            require(ok, control, f"{path}.counts")
        pre = phase["rejections_by_stage"]["PREFILTER"]
        require(counts["risk_stage_arrivals"] == counts["native_canonical_evaluations"]
                - sum(pre.get(key, 0) for key in NATIVE_REASONS if key.isupper() or key == "canonical_wait"),
                "risk_stage_denominator", f"{path}.counts.risk_stage_arrivals")
        stop = phase["stop_rejection"]
        key_map(stop, {"numerator", "denominator", "ratio"}, f"{path}.stop_rejection")
        count_map({key: stop[key] for key in ("numerator", "denominator")}, {"numerator", "denominator"},
                  f"{path}.stop_rejection")
        require(stop["denominator"] == counts["risk_stage_arrivals"] and stop["numerator"] <= stop["denominator"],
                "stop_denominator", f"{path}.stop_rejection.denominator")
        require(stop["numerator"] == sum(phase["rejections"][key] for key in ("profile_cap", "stop_risk", "minimum_margin")),
                "stop_numerator", f"{path}.stop_rejection.numerator")
        require(stop["ratio"] == (stop["numerator"] / stop["denominator"] if stop["denominator"] else None),
                "stop_ratio", f"{path}.stop_rejection.ratio")
        trace = phase["closure_trace"]
        key_map(trace, {"unknown_data_gap", "funding_incomplete_closed", "accounting_lock"}, f"{path}.closure_trace")
        for group, end_key in (("unknown_data_gap", "detected_at"), ("funding_incomplete_closed", "closed_at")):
            here = f"{path}.closure_trace.{group}"
            table = trace[group]
            key_map(table, {"count", "items"}, here)
            count_map({"count": table["count"]}, {"count"}, f"{here}.count")
            require(isinstance(table["items"], list) and len(table["items"]) == table["count"],
                    "closure_item_count", f"{here}.items")
            for index, row in enumerate(table["items"]):
                item_path = f"{here}.items[{index}]"
                key_map(row, {"symbol", "opened_at", end_key}, item_path)
                require(row["symbol"] in phase["by_symbol"], "closure_symbol", f"{item_path}.symbol")
                opened = timestamp(row["opened_at"], f"{item_path}.opened_at")
                closed = timestamp(row[end_key], f"{item_path}.{end_key}")
                require(start <= opened <= closed < end, "closure_times", item_path)
        require(trace["unknown_data_gap"]["count"] == counts["unknown_data_gap"],
                "unknown_closure_count", f"{path}.closure_trace.unknown_data_gap.count")
        require(trace["funding_incomplete_closed"]["count"] <= counts["completed"] - counts["fully_verified_completed"],
                "funding_incomplete_count", f"{path}.closure_trace.funding_incomplete_closed.count")
        lock = trace["accounting_lock"]
        key_map(lock, {"first_lock_at", "first_rejection_at", "rejected_candidates_since_lock"},
                f"{path}.closure_trace.accounting_lock")
        count_map({"rejected_candidates_since_lock": lock["rejected_candidates_since_lock"]},
                  {"rejected_candidates_since_lock"}, f"{path}.closure_trace.accounting_lock")
        expected_lock = min((row["detected_at"] for row in trace["unknown_data_gap"]["items"]), default=None)
        require(lock["first_lock_at"] == expected_lock, "first_accounting_lock",
                f"{path}.closure_trace.accounting_lock.first_lock_at")
        require(lock["rejected_candidates_since_lock"] == phase["rejections"]["accounting_verified"],
                "accounting_rejection_count", f"{path}.closure_trace.accounting_lock.rejected_candidates_since_lock")
        if lock["rejected_candidates_since_lock"]:
            require(lock["first_lock_at"] is not None, "accounting_lock_evidence",
                    f"{path}.closure_trace.accounting_lock.first_lock_at")
            rejected = timestamp(lock["first_rejection_at"], f"{path}.closure_trace.accounting_lock.first_rejection_at")
            locked = timestamp(lock["first_lock_at"], f"{path}.closure_trace.accounting_lock.first_lock_at")
            require(start <= locked <= rejected < end, "accounting_lock_order", f"{path}.closure_trace.accounting_lock")
        else:
            require(lock["first_rejection_at"] is None, "no_accounting_rejection_time",
                    f"{path}.closure_trace.accounting_lock.first_rejection_at")
        for key in ("policy_sha256", "config_sha256"):
            digest(phase[key], f"{path}.{key}")
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


def selected_phases(name: str) -> tuple[Phase, ...]:
    if name not in {"TRAIN", "VALIDATION"}:
        raise MeasurementError("PHASE_SELECTION_REQUIRED")
    return tuple(phase for phase in PHASES if phase.name == name)


def validation_allows_train(report: dict) -> bool:
    return report["phases"]["VALIDATION"]["counts"]["fully_verified_completed"] >= 150


def guarded_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata", required=True, type=Path)
    parser.add_argument("--phase", choices=("VALIDATION", "TRAIN"))
    args = parser.parse_args(argv)
    stream, loop = sys.stdout, None
    try:
        output_directory = OUTPUT / args.phase if args.phase else OUTPUT
        if output_directory.exists():
            raise MeasurementError("RESULTS_ALREADY_EXIST")
        phases_to_measure = selected_phases(args.phase)
        if args.metadata.resolve() != METADATA.resolve():
            raise MeasurementError("METADATA_COPY_REQUIRED")
        validation_record = None
        if args.phase == "TRAIN":
            validation_path = OUTPUT / "VALIDATION" / "counts.json"
            if not validation_path.is_file():
                raise MeasurementError("TRAIN_REQUIRES_COMPLETED_VALIDATION")
            validation_record = json.loads(validation_path.read_text(encoding="utf-8"))
            validate_report(validation_record, expected_phases={"VALIDATION"})
            if not validation_allows_train(validation_record):
                raise MeasurementError("TRAIN_DENIED_VALIDATION_BELOW_150")
        plan = locked_plan()
        if sha256_file(args.metadata) != plan["input_sha256"]["metadata"]:
            raise MeasurementError("LOCKED_METADATA_MISMATCH")
        if sha256_file(VIEW / "manifest.json") != VIEW_SHA:
            raise MeasurementError("LOCKED_VIEW_MISMATCH")
        output_directory.mkdir(parents=True, exist_ok=False)
        os.environ.update({"DATA_DIR": str(output_directory / "runtime"), "PROTREBOT_DATA_DIR": str(output_directory / "runtime"),
                           "DATABASE_URL": "", "PROTREBOT_DURABLE_AUTH_REQUIRED": "0", "ASSISTANT_LIVE_TESTS": "0"})
        loop = asyncio.new_event_loop()
        started = time.monotonic()
        with quiet_native(), deny_network():
            from app.strategies.original_offline_risk import PROFILE

            record = provenance(plan)
            if validation_record is not None and validation_record["provenance"] != record:
                raise MeasurementError("TRAIN_VALIDATION_PROVENANCE_MISMATCH")
            # Keep the identical loader window so VALIDATION retains TRAIN warmup.
            data = load_measurement_dataset(VIEW, args.metadata, PHASES[0].start, PHASES[-1].end)
            validate_loaded_scope(data, plan["symbols"])
            phases = loop.run_until_complete(measure_phases(
                data, plan, phases=phases_to_measure, progress=lambda value: emit(stream, value)))
        report = {"schema": SCHEMA, "profile": asdict(PROFILE), "provenance": record,
                  "phases": phases, "elapsed_seconds": time.monotonic() - started}
        validate_report(report, expected_phases={args.phase})
        path = output_directory / "counts.json"
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
