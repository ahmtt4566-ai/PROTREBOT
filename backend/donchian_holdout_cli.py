"""dry-verify -> lock -> MANUAL extraction -> run-test, exactly one bundle.

No extraction/decryption/password interface exists. Dry/lock never open TEST or
the sealed archive. Pass an identical source-manifest COPY outside independent-
block. run-test consumes its repository claim before parsing that manifest, any
TEST ZIP, or computing signals. Original is descriptive only. TP_FIRST freezes
primary specs/entries; stress recomputes entries with its fixed costs.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import sys
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.donchian_holdout import (
    PLAN_PATH,
    PROTOCOL,
    SCHEMA,
    Claim,
    HoldoutError,
    Inputs,
    claim_once,
    digest,
    dry_verify,
    exclusive_json,
    finish,
    load_claimed_dataset,
    lock,
    verify_inputs,
    verify_zip_inputs,
)
from app.donchian_holdout_statistics import criteria, scenario_report


def frozen_priority(data, primary, config):
    """Same cohort/specs/risk, native Position and funding, no admission rescan."""
    from app.backtest_baseline import Engine, Position, iso
    from app.strategies.donchian_params import DonchianParamsV2
    from app.strategies.offline_facade import DonchianOfflineEngine

    engine = Engine(data, config)
    entries = {}
    for position in primary.state.trades:
        entries.setdefault(position.opened_at, []).append(position)
    for at in range(config.start, config.end, 900):
        for position in list(engine.positions.values()):
            engine.advance(position, at, opening_only=True)
        for original in entries.get(at, []):
            if original.symbol in engine.positions:
                raise HoldoutError("Frozen primary cohort conflicts with active native position")
            contract, mark = data.frames[original.symbol]["15m"].at(at), data.marks[original.symbol].at(at)
            if contract is None or mark is None:
                raise HoldoutError("Frozen entry data unavailable")
            position = Position(
                original.symbol, original.direction, at, copy.deepcopy(original.spec), original.signal_identifier,
                Decimal(str(config.slippage_bps)) / 10000, Decimal(str(mark["open"])), Decimal(str(contract["open"])),
            )
            engine.positions[position.symbol] = position
            engine.trades.append(position)
            engine.cash -= float(position.commission)
            engine.events.insert(0, {"kind": "LIVE_ENTRY", "created_at": iso(at), "plan_id": position.signal_identifier})
        for position in list(engine.positions.values()):
            engine.advance(position, at, opening_only=False)
    for position in engine.positions.values():
        position.status = "OPEN_AT_END"
        position.funding_known = position.funding_known and data.funding_complete(position.symbol, position.opened_at, config.end - 1)
    reporter = DonchianOfflineEngine(data, config, DonchianParamsV2())
    reporter.state = engine
    for old, new in zip(primary.state.trades, engine.trades, strict=True):
        if (
            old.symbol != new.symbol or old.opened_at != new.opened_at or old.direction != new.direction
            or old.spec != new.spec or old.quantity != new.quantity or old.actual_entry != new.actual_entry
        ):
            raise HoldoutError("Frozen scenario changed entry/spec/risk")
    return reporter


def uncertainty(engine) -> dict[str, Any]:
    rows = [position.row() for position in engine.state.trades]
    unknown = engine._unknown_data_gap_report()
    funding = engine._funding_data_gap_report(rows, unknown)
    return {
        "counts": engine._measurement_counts(rows), "unknown_data_gaps": unknown, "funding_data_gaps": funding,
        "funding_incomplete": [
            {"signal_id": row["signal_id"], "symbol": row["symbol"], "opened_at": row["opened_at"],
             "closed_at": row["closed_at"], "status": row["status"]}
            for row in rows if row["funding_usdt"] is None
        ],
    }


async def bundle(data, claim: Claim) -> dict[str, Any]:
    from app.backtest_baseline import Engine
    from app.strategies.donchian_params import DonchianParamsV2
    from app.strategies.offline_facade import DonchianOfflineEngine
    from measure_donchian_counts import Phase, make_engine, primary_config, quiet_native

    start, end = (int(datetime.fromisoformat(value).timestamp()) for value in PROTOCOL["splits"]["TEST"])
    phase = Phase("TEST", start, end)
    settings = PROTOCOL["primary"]
    base = {"symbols": PROTOCOL["symbols"], **settings,
            "primary": {"slippage_bps": 3, "spread_bps": 2, "intrabar": "STOP_FIRST"}}
    config = primary_config(phase, base)
    results = {}
    with quiet_native():
        primary = make_engine(data, config)
        from measure_donchian_counts import digest_object
        if digest_object(primary.state.policy) != settings["policy_sha256"]:
            raise HoldoutError("Native policy fingerprint changed")
        primary_counts = await primary.replay_counts(phase)
        tp = frozen_priority(data, primary, replace(config, intrabar="TP_FIRST"))
        stress = make_engine(data, replace(config, slippage_bps=6, spread_bps=5))
        stress_counts = await stress.replay_counts(phase)
        bootstrap_settings = PROTOCOL["bootstrap"]
        for name, engine, counts in (("primary", primary, primary_counts), ("tp_first", tp, None), ("stress", stress, stress_counts)):
            rows = [position.row() for position in engine.state.trades]
            results[name] = scenario_report(
                rows, uncertainty(engine), start, end,
                samples=bootstrap_settings["samples"], seed=bootstrap_settings["seed"], symbols=PROTOCOL["symbols"],
            )
            results[name]["counts"] = counts
            results[name]["confirmatory"] = name == "primary"
            results[name]["entry_mode"] = "FROZEN_PRIMARY" if name == "tp_first" else "FRESH_NATIVE_GATES"
        original = Engine(data, config)
        original_raw = await original.replay()
        # Benchmark uncertainty uses the same evidence reconstruction, not Donchian entry rules.
        reporter = DonchianOfflineEngine(data, config, DonchianParamsV2())
        reporter.state = original
        benchmark = scenario_report(
            original_raw["trades"], uncertainty(reporter), start, end,
            samples=bootstrap_settings["samples"], seed=bootstrap_settings["seed"], symbols=PROTOCOL["symbols"],
        )
        benchmark.pop("criteria")
        benchmark["descriptive_only"] = True
    return {
        "schema": SCHEMA, "plan_sha256": claim.evidence["plan_sha256"], "claim_id": claim.record["claim_id"],
        "primary_scenario": "primary", "candidate_count": 1, "multiple_comparisons": False,
        "scenarios": results, "original_benchmark": benchmark,
        "decision": results["primary"]["criteria"], "declarations": PROTOCOL["declarations"],
        "archive_sha256_user_attested_not_read": claim.evidence["sealed_archive_sha256_attestation"],
    }


def validate_report(report: dict[str, Any]) -> None:
    if (
        set(report) != {
            "schema", "plan_sha256", "claim_id", "primary_scenario", "candidate_count",
            "multiple_comparisons", "scenarios", "original_benchmark", "decision", "declarations",
            "archive_sha256_user_attested_not_read",
        }
        or report["schema"] != SCHEMA or set(report["scenarios"]) != {"primary", "tp_first", "stress"}
        or report["candidate_count"] != 1 or report["multiple_comparisons"] is not False
        or report["decision"] != report["scenarios"]["primary"]["criteria"]
        or report["original_benchmark"].get("descriptive_only") is not True
        or "criteria" in report["original_benchmark"]
    ):
        raise HoldoutError("Holdout result schema/primary decision differs")
    for name, scenario in report["scenarios"].items():
        if (
            scenario["confirmatory"] is not (name == "primary")
            or set(scenario["bootstrap"]) != {"TRADE", "UTC_DAY"}
            or scenario["criteria"] != criteria(scenario["summary"])
        ):
            raise HoldoutError("Scenario confirmation/bootstrap schema differs")
        for stats in scenario["bootstrap"].values():
            if stats["samples"] != 20000 or stats["seed"] != 2026 or stats["family_size"] != 1 or stats["informational_only"] is not True:
                raise HoldoutError("Bootstrap configuration differs")
    json.dumps(report, allow_nan=False)


def execute(inputs: Inputs, *, backend=None, anchor=None, runner=None) -> dict:
    """All errors after claim, including KeyboardInterrupt, consume the attempt."""
    options = {"anchor": anchor}
    if backend is not None:
        options["backend"] = backend
    claim = claim_once(inputs, **options)
    completed = False
    loop = None
    step = "INITIALIZE_RUNTIME"
    try:
        os.environ.update({
            "DATA_DIR": str(inputs.output / "runtime"), "PROTREBOT_DATA_DIR": str(inputs.output / "runtime"),
            "DATABASE_URL": "", "PROTREBOT_DURABLE_AUTH_REQUIRED": "0", "ASSISTANT_LIVE_TESTS": "0",
        })
        from measure_donchian_counts import deny_network, quiet_native
        loop = asyncio.new_event_loop()
        with quiet_native(), deny_network():
            step = "VERIFY_AND_LOAD_TEST"
            data = load_claimed_dataset(claim, **({"backend": backend} if backend is not None else {}))
            step = "RUN_BUNDLE"
            result = loop.run_until_complete((bundle if runner is None else runner)(data, claim))
        step = "VERIFY_RESULT_AND_INPUTS"
        validate_report(result)
        if verify_inputs(inputs, **({"backend": backend} if backend is not None else {})) != claim.evidence:
            raise HoldoutError("Evidence changed during TEST bundle")
        verify_zip_inputs(claim)
        step = "PUBLISH_RESULT_AND_RECEIPT"
        exclusive_json(inputs.output / "holdout-results.json", result)
        finish(claim, "COMPLETED", {"results_sha256": digest(inputs.output / "holdout-results.json"),
                                   "decision": result["decision"]})
        completed = True
        return result
    finally:
        if loop is not None:
            loop.close()
        if not completed and not (claim.anchor / "receipt.json").exists():
            error_type = sys.exc_info()[0]
            error = sys.exc_info()[1]
            details = {"error_type": error_type.__name__ if error_type else "INCOMPLETE_BUNDLE",
                       "failure_stage": step}
            if isinstance(error, HoldoutError):
                details["protocol_error"] = str(error)
            finish(claim, "FAILED", details)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("dry-verify", "lock", "run-test"))
    parser.add_argument("--plan", type=Path, default=PLAN_PATH)
    parser.add_argument("--measurement-manifest", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True, help="Identical external manifest COPY; never independent-block")
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--counts", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--test-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    inputs = Inputs(**{name: getattr(args, name) for name in Inputs.__dataclass_fields__})
    try:
        if args.command == "dry-verify":
            dry_verify(inputs)
            print("DONCHIAN_DRY_VERIFY_PASSED signals=0 replays=0 test_files_read=0 archive_read=False")
        elif args.command == "lock":
            value = lock(inputs)
            print("DONCHIAN_LOCKED", value["evidence"]["plan_sha256"])
        else:
            value = execute(inputs)
            print("DONCHIAN_TEST_COMPLETED", json.dumps(value["decision"]))
        return 0
    except HoldoutError as error:
        print(f"DONCHIAN_HOLDOUT_FAILED protocol={error}", file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, ArithmeticError) as error:
        print(f"DONCHIAN_HOLDOUT_FAILED type={type(error).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
