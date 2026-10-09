"""Bounded stage-A model parity, never Testnet execution or a performance trial.

Use only the previously permitted, checksum-locked BASELINE source. Two fresh
offline accounts consume the same fixed policy and a named, fixed window. The Demo
side replaces only canonical decision evaluation with the inert adapter;
existing offline admission/lifecycle is the reference, not stage-B integration.
No outcome selection, thresholds, profit statistics or runtime activation.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.backtest_baseline import Config
from app.backtest_data import Dataset
from app.strategies.contracts import StrategyInput
from app.strategies.original_gap_engine import OriginalGapRiskEngine
from app.strategies.original_v2_demo import (
    DOCUMENT_SHA256,
    EXECUTION_POLICY_SHA256,
    INTERVALS,
    POLICY_PATH,
    decision_record,
    evaluate,
    fixed_profile,
)
from app.strategies.provenance import parameter_hash

OUTPUT = Path.home() / "kaistrade-data" / "original-demo-stage-a-parity"
MARCH_OUTPUT = Path.home() / "kaistrade-data" / "original-demo-stage-a-parity-march-2025"
MARCH_WINDOW = {
    "start": "2025-03-01T00:00:00Z",
    "end_exclusive": "2025-04-01T00:00:00Z",
}
REFERENCE = Path.home() / "kaistrade-data" / "original-v2-f2-test-run2" / "report-BASELINE.json"
REFERENCE_SHA256 = "4b83f6433b954e3a9ae4e7b09a5d960e8b2611cf731d4b1459c9fe9ff41557e0"


@dataclass
class TraceDataset(Dataset):
    demo_adapter: bool = False
    trace: list[dict[str, Any]] = field(default_factory=list)

    def canonical(self, symbol: str, at: int, policy: dict[str, Any]) -> dict[str, Any]:
        if parameter_hash(policy) != EXECUTION_POLICY_SHA256:
            raise ValueError("PARITY_POLICY_CHANGED")
        key = (symbol, at, policy["min_confidence"], policy["mtf_allow_either_timeframe"])
        if key in self.decisions:
            return self.decisions[key]
        record = None
        if self.demo_adapter:
            result = evaluate(StrategyInput(
                symbol=symbol,
                decision_time=at,
                candles_by_timeframe={
                    interval: series.closed(at) for interval, series in self.frames[symbol].items()
                },
                required_intervals=INTERVALS,
            ), enabled=True)
            value = deepcopy(result.legacy)
            record = decision_record(result)
            if value.get("analysis"):
                value["analysis"].pop("series", None)
            if not value.get("entry_eligible"):
                value = {name: value.get(name) for name in (
                    "decision", "signal_timestamp", "entry_eligible", "reason", "reasons",
                )}
            self.decisions[key] = value
        else:
            value = super().canonical(symbol, at, policy)
        self.trace.append({
            "symbol": symbol, "decision_time": at,
            "canonical": deepcopy(value), "strategy_record": record,
        })
        return value


class ParityEngine(OriginalGapRiskEngine):
    def __init__(self, data: Dataset, config: Config):
        super().__init__(data, config)
        self.ranking_trace: list[dict[str, Any]] = []
        self.entry_trace: list[dict[str, Any]] = []

    def prefilter(self, signal: dict[str, Any]) -> bool:
        accepted = super().prefilter(signal)
        self.ranking_trace.append({"signal": deepcopy(signal), "accepted": accepted})
        return accepted

    async def enter(self, symbol: str, signal: dict, at: int) -> None:
        self.entry_trace.append({
            "symbol": symbol, "decision_time": at, "signal": deepcopy(signal),
        })
        await super().enter(symbol, signal, at)


def traced_engine(data: Dataset, config: Config, *, demo_adapter: bool) -> ParityEngine:
    engine = ParityEngine(data, config)
    fresh = engine.data
    engine.data = TraceDataset(
        fresh.frames, fresh.marks, fresh.funding, fresh.metadata, fresh.report,
        fresh.funding_months, demo_adapter=demo_adapter,
    )
    return engine


def compare(
    expected: Any, actual: Any, path: str, differences: list[dict[str, Any]],
) -> None:
    if type(expected) is not type(actual):
        differences.append({"path": path, "expected": expected, "actual": actual})
    elif isinstance(expected, dict):
        if expected.keys() != actual.keys():
            differences.append({
                "path": path, "expected_keys": sorted(expected), "actual_keys": sorted(actual),
            })
        for key in expected.keys() & actual.keys():
            compare(expected[key], actual[key], f"{path}.{key}", differences)
    elif isinstance(expected, list):
        if len(expected) != len(actual):
            differences.append({"path": path, "expected_N": len(expected), "actual_N": len(actual)})
        for index, (left, right) in enumerate(zip(expected, actual)):
            compare(left, right, f"{path}[{index}]", differences)
    elif expected != actual:
        differences.append({"path": path, "expected": expected, "actual": actual})


def diagnostic_json(value: Any) -> dict[str, str]:
    if isinstance(value, Decimal):
        return {"type": "Decimal", "value": str(value)}
    raise TypeError(f"Unsupported parity diagnostic type: {type(value).__name__}")


def edge_observations(engine: ParityEngine) -> dict[str, Any]:
    rounded = [
        {
            "symbol": position.symbol,
            "opened_at": position.opened_at,
            "quantity": str(position.quantity),
            "unrounded_tp1_quantity": str(position.quantity * Decimal("0.60")),
            "effective_tp1_quantity": str(position.tp1_quantity),
            "tp1_hit": position.tp1_hit,
        }
        for position in engine.trades
        if position.tp1_quantity != position.quantity * Decimal("0.60")
    ]
    return {
        "stop_cap_rejections": engine.rejections["profile_cap"],
        "stop_risk_rejections": engine.rejections["stop_risk"],
        "minimum_quantity_or_notional_rejections": engine.rejections["min_notional"],
        "minimum_margin_rejections": engine.rejections["minimum_margin"],
        "tp1_rounding_or_minimum_adjustments": len(rounded),
        "tp1_adjustment_details": rounded,
        "tp1_hits": sum(position.tp1_hit for position in engine.trades),
        "daily_entry_limit_rejections": engine.rejections["daily_trades"],
        "daily_loss_limit_rejections": engine.rejections["daily_loss"],
        "same_direction_limit_rejections": engine.rejections["same_direction_positions"],
        "note": "Observed events only; zero means this window did not exercise the edge.",
    }


async def model_parity(
    data: Dataset, *, window_name: str = "initial",
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    from measure_donchian_counts import Phase
    from measure_original_v2_counts import replay_counts

    profile = fixed_profile()
    if window_name not in {"initial", "march-2025"}:
        raise ValueError("UNREGISTERED_PARITY_WINDOW")
    window = deepcopy(profile["parity_window"])
    if window_name == "march-2025":
        window.update(MARCH_WINDOW)
    start = int(datetime.fromisoformat(window["start"]).timestamp())
    end = int(datetime.fromisoformat(window["end_exclusive"]).timestamp())
    config = Config(
        start, end,
        initial_equity=profile["model"]["initial_equity_usdt"],
        spread_bps=profile["model"]["spread_bps"],
        slippage_bps=profile["model"]["slippage_bps"],
        intrabar=profile["model"]["intrabar"],
        conditional_current_metadata=True,
        policy=deepcopy(profile["execution_policy"]),
    )
    native = traced_engine(data, config, demo_adapter=False)
    demo = traced_engine(data, config, demo_adapter=True)
    phase = Phase("DEMO_STAGE_A", start, end)
    native_counts = await replay_counts(
        native, phase, entry_data_exclusion=native.entry_data_exclusion,
    )
    demo_counts = await replay_counts(
        demo, phase, entry_data_exclusion=demo.entry_data_exclusion,
    )
    if not isinstance(native.data, TraceDataset) or not isinstance(demo.data, TraceDataset):
        raise TypeError("TRACE_DATASET_REQUIRED")
    differences: list[dict[str, Any]] = []
    compare(native_counts, demo_counts, "counts_and_gate_rejections", differences)
    compare(native.ranking_trace, demo.ranking_trace, "ranked_prefilter_order", differences)
    compare(native.entry_trace, demo.entry_trace, "selected_top_three_entry_order", differences)
    compare(native.events, demo.events, "account_gate_events", differences)
    compare(native.cash, demo.cash, "model_account_cash", differences)
    compare(
        [deepcopy(position.spec) for position in native.trades],
        [deepcopy(position.spec) for position in demo.trades],
        "accepted_entry_specs", differences,
    )
    native_edges, demo_edges = edge_observations(native), edge_observations(demo)
    compare(native_edges, demo_edges, "edge_observations", differences)
    if len(native.data.trace) != len(demo.data.trace):
        differences.append({
            "path": "decision_N", "expected": len(native.data.trace), "actual": len(demo.data.trace),
        })
    for index, (left, right) in enumerate(zip(native.data.trace, demo.data.trace)):
        for key in ("symbol", "decision_time", "canonical"):
            compare(left[key], right[key], f"decisions[{index}].{key}", differences)
    expected_n = len(profile["execution_policy"]["allowed_symbols"]) * (end - start) // 900
    if len(native.data.trace) != expected_n or len(demo.data.trace) != expected_n:
        raise ValueError("DECISION_GRID_N_MISMATCH")
    report = {
        "schema": "original-demo-stage-a-model-parity-v1",
        "strategy_id": profile["strategy_id"],
        "policy_document_sha256": DOCUMENT_SHA256,
        "policy_file_sha256": hashlib.sha256(POLICY_PATH.read_bytes()).hexdigest(),
        "resolved_policy_sha256": EXECUTION_POLICY_SHA256,
        "source_profile_sha256": profile["source_profile_sha256"],
        "window": window,
        "model_parity": {
            "native_decisions": len(native.data.trace),
            "demo_adapter_decisions": len(demo.data.trace),
            "accepted_entries": len(native.trades),
            "canonical_distribution": native_counts["canonical_distribution"],
            "ranking_comparison": "RANKED_PREFILTER_AND_SELECTED_TOP_THREE_ENTRY_ORDER",
            "edge_observations": native_edges,
            "difference_count": len(differences),
            "differences": differences,
            "comparison": "EXACT_TYPES_VALUES_ORDER_REASONS_AND_ROUNDED_DECIMALS",
            "serialization_tolerance_used": 0,
            "native_counts": native_counts,
            "demo_counts": demo_counts,
            "scope": "DECISION_ADAPTER_WITH_EXISTING_OFFLINE_ACCOUNT_MODEL_NOT_STAGE_B",
        },
        "real_execution_differences": {
            "order_path_connected": False,
            "exchange_requests": 0,
            "next_open_fill": "MODEL_ONLY_NOT_A_TESTNET_FILL_GUARANTEE",
            "STOP_FIRST": "OHLC_MODEL_NOT_EXCHANGE_ORDERING",
            "funding": "HISTORICAL_MODEL_NOT_TESTNET_FUNDING",
            "gap18d": "FUTURE_GAP_INVENTORY_NOT_CAUSALLY_AVAILABLE_LIVE",
            "production_or_live_modified": False,
        },
    }
    return report, demo.data.trace


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window", choices=("initial", "march-2025"), default="initial")
    args = parser.parse_args()
    output = OUTPUT if args.window == "initial" else MARCH_OUTPUT
    if output.exists() or output.is_symlink():
        raise ValueError("OUTPUT_EXISTS_STOP")
    if hashlib.sha256(REFERENCE.read_bytes()).hexdigest() != REFERENCE_SHA256:
        raise ValueError("REFERENCE_REPORT_CHANGED")
    reference = json.loads(REFERENCE.read_bytes())
    if reference["native_counts"]["policy_sha256"] != EXECUTION_POLICY_SHA256:
        raise ValueError("REFERENCE_POLICY_MISMATCH")
    os.environ.update({
        "PYTHONDONTWRITEBYTECODE": "1",
        "PROTREBOT_DATA_DIR": str(output / "runtime"),
        "DATA_DIR": str(output / "runtime"),
        "DATABASE_URL": "",
        "ASSISTANT_LIVE_TESTS": "0",
    })
    from diagnose_original_components import offline_guard
    from prescreen_original_f2 import load_registered_dataset, locked_plan

    with asyncio.Runner() as runner:
        runner.get_loop()
        with offline_guard(output):
            output.mkdir()
            plan = locked_plan()
            data = load_registered_dataset(plan)
            report, trace = runner.run(model_parity(data, window_name=args.window))
            report["reference"] = {
                "report_sha256": REFERENCE_SHA256,
                "manifest_sha256": plan["inputs"]["manifest_sha256"],
                "metadata_sha256": plan["inputs"]["metadata_sha256"],
            }
            with (output / "decisions.jsonl").open("x", encoding="utf-8", newline="\n") as stream:
                for row in trace:
                    stream.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
            report["decision_trace_sha256"] = hashlib.sha256(
                (output / "decisions.jsonl").read_bytes(),
            ).hexdigest()
            destination = output / "parity.json"
            with destination.open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(
                    report, stream, indent=2, sort_keys=True, allow_nan=False,
                    default=diagnostic_json,
                )
            print(json.dumps({
                "report": str(destination),
                "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
                "decisions": report["model_parity"]["native_decisions"],
                "differences": report["model_parity"]["difference_count"],
            }))
    return 0 if report["model_parity"]["difference_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
