"""Stage-A synthetic parity only; no global patches, exchange or runtime writes."""

from __future__ import annotations

import ast
import asyncio
import json
import os
import subprocess
import sys
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
import test_backtest_baseline as baseline_fixtures
import test_measure_original_counts as fixtures
from app import execution_core as core
from app.backtest_data import Dataset, Series
from app.main import canonical_historical_decision
from app.strategies import original_v2_demo as demo
from app.strategies.contracts import StrategyInput
from app.strategies.original_gap_risk import PROFILE
from app.strategies.original_offline_risk import OfflineRiskError
from app.strategies.provenance import parameter_hash
from verify_original_v2_demo import TraceDataset, compare, diagnostic_json, model_parity

ROOT = Path(__file__).parents[2]
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT")


def request(side="LONG"):
    data = fixtures.open_dataset() if side == "LONG" else fixtures.dataset(side)
    return StrategyInput(
        fixtures.BTC, fixtures.AT,
        {interval: series.closed(fixtures.AT) for interval, series in data.frames[fixtures.BTC].items()},
        required_intervals=demo.INTERVALS,
    )


def snapshot():
    return {
        "positions": [], "open_orders": [], "hedge_mode": False,
        "unrealized_pnl": 0, "available_balance": 1000,
    }


def failed_keys(gates):
    return {item["key"] for item in gates["gates"] if not item["passed"]}


def test_fixed_file_matches_recorded_policy_and_profile():
    profile = demo.fixed_profile()
    policy = profile["execution_policy"]
    assert parameter_hash(profile) == demo.DOCUMENT_SHA256
    assert parameter_hash(policy) == demo.EXECUTION_POLICY_SHA256
    assert core.sanitize_execution_policy(policy) == policy
    assert tuple(policy["allowed_symbols"]) == SYMBOLS
    assert profile["source_profile_sha256"] == PROFILE.profile_hash
    assert policy["max_positions"] == 5
    assert policy["max_same_direction_positions"] == 2
    assert policy["daily_trade_limit"] == 3
    assert policy["daily_loss_limit"] == 10
    assert policy["consecutive_loss_limit"] == 3
    assert policy["max_leverage"] == 3
    assert policy["max_total_exposure_usdt"] == 350
    assert policy["min_confidence"] == 80
    assert policy["mtf_allow_either_timeframe"] is False
    assert profile["canonical_policy"] == {
        "confidence_threshold": 80, "breakout_quality_threshold": 50,
        "mtf_allow_either_timeframe": 0,
    }
    profile["execution_policy"]["min_confidence"] = 95
    assert demo.fixed_profile()["execution_policy"]["min_confidence"] == 80


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_native_canonical_and_levels_are_exact_and_input_is_unchanged(side):
    value = request(side)
    before = deepcopy(value.candles_by_timeframe)
    expected = canonical_historical_decision(
        value.symbol, value.candles_by_timeframe, value.decision_time,
        required_intervals=demo.INTERVALS,
        historical_policy_override=demo.fixed_profile()["canonical_policy"],
    )
    actual = demo.evaluate(value, enabled=True)
    assert actual.legacy == expected
    assert actual.signal.direction == side
    assert actual.signal.direction == expected["analysis"]["direction"]
    assert actual.signal.entry == expected["analysis"]["entry"]
    assert actual.signal.stop == expected["analysis"]["stop_loss"]
    assert [actual.signal.tp1, actual.signal.tp2, actual.signal.tp3] == [
        expected["analysis"][key] for key in ("tp1", "tp2", "tp3")
    ]
    assert value.candles_by_timeframe == before
    record = demo.decision_record(actual)
    assert record["strategy_id"] == "kais-original-v2-demo-v1"
    assert record["order_authorized"] is False
    assert record["execution_connected"] is False
    assert actual.signal.provenance.parameter_hash == demo.DOCUMENT_SHA256


def test_missing_history_retains_native_wait_and_rejection_reasons():
    value = replace(request(), candles_by_timeframe={"15m": []})
    actual = demo.evaluate(value, enabled=True)
    expected = canonical_historical_decision(
        value.symbol, value.candles_by_timeframe, value.decision_time,
        required_intervals=demo.INTERVALS,
        historical_policy_override=demo.fixed_profile()["canonical_policy"],
    )
    assert actual.legacy == expected
    assert actual.signal.decision == "WAIT"
    assert actual.signal.reason == "INSUFFICIENT_CLOSED_CANDLES"


@pytest.mark.parametrize("enabled", [False, 1, "true"])
def test_disabled_or_invalid_explicit_feature_selection_is_explicit(enabled, caplog):
    with pytest.raises(demo.DemoDecisionError):
        demo.evaluate(request(), enabled=enabled)
    assert "ORIGINAL_DEMO_STAGE_A_REJECTED" in caplog.text


@pytest.mark.parametrize("symbol", ["LTCUSDT", "BTC", "btcusdt"])
def test_fixed_universe_cannot_expand(symbol):
    with pytest.raises(demo.DemoDecisionError, match="SYMBOL_OUTSIDE_FIXED_UNIVERSE"):
        demo.evaluate(replace(request(), symbol=symbol), enabled=True)


def test_caller_cannot_override_reference_policy_or_window():
    with pytest.raises(demo.DemoDecisionError, match="CALLER_POLICY_OVERRIDE_DENIED"):
        demo.evaluate(replace(request(), historical_policy_override={"confidence_threshold": 90}), enabled=True)
    value = request()
    frames = deepcopy(value.candles_by_timeframe)
    frames["15m"].append(deepcopy(frames["15m"][-1]))
    with pytest.raises(demo.DemoDecisionError, match="CLOSED_WINDOW_EXCEEDED"):
        demo.evaluate(replace(value, candles_by_timeframe=frames), enabled=True)


@pytest.mark.parametrize("side", [1, -1])
@pytest.mark.parametrize("distance", [0.5, 1, 2, 6, 6.001])
def test_fixed_cap_risk_preview_matches_native_not_dynamic_five_percent(side, distance):
    stop = 100 - side * distance
    try:
        expected = PROFILE.size(100, stop)
    except OfflineRiskError as exc:
        with pytest.raises(demo.DemoDecisionError, match=exc.reason):
            demo.risk_preview(100, stop, enabled=True)
    else:
        actual = demo.risk_preview(100, stop, enabled=True)
        assert actual == expected
        assert actual["leverage"] == 3
        assert actual["margin_usdt"] <= 25
        assert actual["estimated_stop_loss_usdt"] <= 3


@pytest.mark.parametrize(("daily", "key"), [
    ({"entries": 3}, "daily_trades"),
    ({"realized_pnl": -10}, "daily_loss"),
    ({"consecutive_losses": 3}, "consecutive_losses"),
    ({"unverified_closures": 1}, "pnl_verified"),
])
def test_account_gate_boundaries_use_fixed_policy(daily, key):
    result = demo.evaluate(request(), enabled=True)
    actual = demo.account_gates(result, snapshot=snapshot(), daily=daily, enabled=True)
    expected = core.evaluate_entry_gates(
        symbol=fixtures.BTC, signal=result.legacy["analysis"], snapshot=snapshot(),
        policy=demo.fixed_profile()["execution_policy"], daily=daily, spread_bps=2,
        armed=True, allowed_symbols=list(SYMBOLS),
    )
    assert actual == expected
    assert key in failed_keys(actual)


@pytest.mark.parametrize("count", [1, 2, 5])
def test_same_direction_and_total_position_boundaries(count):
    result = demo.evaluate(request(), enabled=True)
    side = result.signal.direction
    current = snapshot()
    current["positions"] = [
        {"symbol": symbol, "direction": side, "quantity": 0.1, "mark_price": 100}
        for symbol in SYMBOLS[1:count + 1]
    ]
    keys = failed_keys(demo.account_gates(result, snapshot=current, daily={}, enabled=True))
    assert ("same_direction_positions" in keys) == (count >= 2)
    assert ("positions" in keys) == (count >= 5)


def test_duplicate_exposure_and_open_loss_gates_are_not_bypassed():
    result = demo.evaluate(request(), enabled=True)
    current = snapshot()
    current["positions"] = [{
        "symbol": fixtures.BTC, "direction": result.signal.direction,
        "quantity": 3, "mark_price": 100,
    }]
    current["unrealized_pnl"] = -10
    actual = demo.account_gates(
        result, snapshot=current, daily={}, candidate_notional_usdt=51, enabled=True,
    )
    assert {"duplicate", "exposure", "open_loss"} <= failed_keys(actual)


def child_code():
    return """
import json,sys
sys.path.insert(0,'backend')
sys.path.insert(0,'backend\\\\tests')
def guard(event,args):
    if event in {'socket.connect','socket.getaddrinfo'}:
        raise RuntimeError('NETWORK_FORBIDDEN')
sys.addaudithook(guard)
from app.strategies import original_v2_demo as demo
from app.strategies.contracts import StrategyInput
import test_measure_original_counts as fixtures
data=fixtures.open_dataset()
request=StrategyInput(fixtures.BTC,fixtures.AT,{
    key:value.closed(fixtures.AT) for key,value in data.frames[fixtures.BTC].items()
})
result=demo.evaluate(request,enabled=True)
print(json.dumps({'feature':demo.feature_enabled(),
    'legacy':result.legacy,'record':demo.decision_record(result)},sort_keys=True))
"""


def test_environment_cannot_change_fixed_confidence_or_mtf_and_default_flag_is_off():
    baseline = dict(os.environ)
    baseline.pop(demo.FEATURE_FLAG, None)
    baseline.update({"PYTHONDONTWRITEBYTECODE": "1", "DATABASE_URL": ""})
    variants = [
        {"PROTREBOT_MIN_CONFIDENCE": "70", "PROTREBOT_MTF_ALLOW_EITHER_TIMEFRAME": "1"},
        {"PROTREBOT_MIN_CONFIDENCE": "95", "PROTREBOT_MTF_ALLOW_EITHER_TIMEFRAME": "0"},
    ]
    results = [
        json.loads(subprocess.check_output(
            [sys.executable, "-B", "-c", child_code()],
            cwd=ROOT, env={**baseline, **variant}, text=True,
        ).splitlines()[-1])
        for variant in variants
    ]
    assert results[0] == results[1]
    assert results[0]["feature"] is False


def test_adapter_contains_no_order_or_persistence_calls():
    tree = ast.parse(Path(demo.__file__).read_text(encoding="utf-8"))
    forbidden = {
        "execute_demo_order", "submit_entry", "install_protection", "persist_state",
        "persist_runtime", "signed", "public_get", "post", "write_text", "write_bytes",
    }
    called = {
        node.func.id if isinstance(node.func, ast.Name) else node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, (ast.Name, ast.Attribute))
    }
    assert not called & forbidden


def test_difference_report_detects_values_types_reasons_and_order():
    differences = []
    compare(
        {"entry": "100.00", "reasons": ["daily_trades", "exposure"], "qty": 1},
        {"entry": "100.01", "reasons": ["exposure", "daily_trades"], "qty": 1.0},
        "decision", differences,
    )
    assert len(differences) == 4
    assert {row["path"] for row in differences} == {
        "decision.entry", "decision.reasons[0]", "decision.reasons[1]", "decision.qty",
    }


def test_rounded_decimal_differences_remain_exact_and_serializable():
    differences = []
    compare(Decimal("0.01"), Decimal("0.02"), "spec.step", differences)
    assert json.loads(json.dumps(differences, default=diagnostic_json)) == [{
        "path": "spec.step",
        "expected": {"type": "Decimal", "value": "0.01"},
        "actual": {"type": "Decimal", "value": "0.02"},
    }]
    with pytest.raises(TypeError, match="Unsupported parity diagnostic type"):
        diagnostic_json(object())


def test_trace_dataset_rejects_changed_reference_policy():
    data = fixtures.open_dataset()
    trace = TraceDataset(data.frames, data.marks, data.funding, data.metadata, data.report, data.funding_months)
    policy = deepcopy(demo.fixed_profile()["execution_policy"])
    policy["min_confidence"] = 90
    with pytest.raises(ValueError, match="PARITY_POLICY_CHANGED"):
        trace.canonical(fixtures.BTC, fixtures.AT, policy)


def test_full_fixed_two_day_synthetic_grid_has_exact_model_parity():
    start = 1725148800
    base = baseline_fixtures.dataset(SYMBOLS, bars=192)
    shift = start - baseline_fixtures.T
    frames, marks, funding = {}, {}, {}
    for symbol in SYMBOLS:
        frames[symbol] = {
            interval: Series(interval, [{**row, "time": row["time"] + shift} for row in series.rows])
            for interval, series in base.frames[symbol].items()
        }
        marks[symbol] = Series("15m", [
            {**row, "time": row["time"] + shift} for row in base.marks[symbol].rows
        ])
        funding[symbol] = [{**row, "time": row["time"] + shift} for row in base.funding[symbol]]
    data = Dataset(
        frames, marks, funding, base.metadata, {}, {symbol: {"2024-08", "2024-09"} for symbol in SYMBOLS},
    )
    report, trace = asyncio.run(model_parity(data))
    assert report["model_parity"]["difference_count"] == 0
    assert report["model_parity"]["native_decisions"] == len(trace) == 1536
    assert sum(report["model_parity"]["canonical_distribution"].values()) == 1536
    assert report["model_parity"]["ranking_comparison"] == "RANKED_PREFILTER_AND_SELECTED_TOP_THREE_ENTRY_ORDER"
    assert report["model_parity"]["edge_observations"]["stop_cap_rejections"] == (
        report["model_parity"]["native_counts"]["rejections"]["profile_cap"]
    )
    with pytest.raises(ValueError, match="UNREGISTERED_PARITY_WINDOW"):
        asyncio.run(model_parity(data, window_name="unapproved"))
    assert report["real_execution_differences"]["order_path_connected"] is False
    assert report["real_execution_differences"]["exchange_requests"] == 0
