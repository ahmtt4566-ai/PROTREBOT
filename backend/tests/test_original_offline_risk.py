"""Synthetic risk admission and literal v1 parity; no market data or performance."""

from __future__ import annotations

import ast
import copy
import inspect
import json
import subprocess
import sys
from dataclasses import FrozenInstanceError, asdict, replace
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).parents[1]))

import measure_original_counts as v1_counts
import measure_original_v2_counts as v2_counts
import test_measure_original_counts as fixtures
import test_offline_strategy_facade as native_tests
from app import execution_core as core
from app import v25_execution as live
from app.backtest_baseline import Engine, LocalSpecClient, Position
from app.strategies.offline_facade import OfflineFacade
from app.strategies.original_offline_engine import (
    OriginalOfflineRiskEngine,
    fully_verified,
    local_spec,
    trade_counts,
)
from app.strategies.original_offline_risk import (
    PROFILE,
    OfflineRiskError,
    OriginalOfflineRiskProfile,
)

offline_only = native_tests.offline_only


def order(side="LONG", stop_distance=6, **overrides):
    sign = 1 if side == "LONG" else -1
    values = {
        "symbol": fixtures.BTC, "direction": side, "margin_usdt": 50 / 3, "leverage": 3,
        "stop_loss": 100 - sign * stop_distance,
        "tp1": 100 + sign * stop_distance, "tp2": 100 + sign * stop_distance * 2,
        "tp3": 100 + sign * stop_distance * 3,
    }
    return live.LiveOrderRequest(**{**values, **overrides})


def policy():
    return PROFILE.policy({"allowed_symbols": [fixtures.BTC]})


@pytest.fixture
def original_functions():
    source = subprocess.check_output(
        ["git", "show", "HEAD:backend/app/execution_core.py"],
        cwd=Path(__file__).parents[2], text=True, encoding="utf-8")
    names = {"sanitize_execution_policy", "dynamic_stop_distance_pct", "risk_sized_order"}
    nodes = [node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in nodes} == names
    namespace = dict(vars(core))
    # Only the committed local source supplies this executable parity reference.
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<committed-core>", "exec"), namespace)  # noqa: S102
    return namespace


@pytest.mark.parametrize("cap", [0.25, 1, 5, 6, 15])
@pytest.mark.parametrize("atr", [None, 0, 0.1, 1, 4])
@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_v1_defaults_formula_sizing_and_errors_exact(cap, atr, side, original_functions):
    source = {"max_stop_distance_pct": cap, "max_leverage": 3}
    before = original_functions["sanitize_execution_policy"](source)
    assert core.sanitize_execution_policy(source) == before
    assert core.dynamic_stop_distance_pct(100, atr, source) == original_functions["dynamic_stop_distance_pct"](100, atr, source)
    stop = 94 if side == "LONG" else 106
    with pytest.raises(ValueError) as old:
        original_functions["risk_sized_order"](100, stop, source, atr=atr)
    with pytest.raises(ValueError) as current:
        core.risk_sized_order(100, stop, source, atr=atr)
    assert str(current.value) == str(old.value)
    assert core.risk_sized_order(100, 99.9 if side == "LONG" else 100.1, source, atr=None) == \
        original_functions["risk_sized_order"](100, 99.9 if side == "LONG" else 100.1, source, atr=None)


def test_profile_is_fixed_frozen_and_has_separate_hash():
    assert len(PROFILE.profile_hash) == 64
    assert PROFILE == OriginalOfflineRiskProfile()
    with pytest.raises(FrozenInstanceError):
        PROFILE.leverage = 5
    for key, value in (("stop_cap_pct", 10.0), ("leverage", 5), ("scope", "LIVE")):
        with pytest.raises(OfflineRiskError, match="UNREGISTERED_OFFLINE_PROFILE"):
            replace(PROFILE, **{key: value})
    assert policy()["max_stop_distance_pct"] == 5
    assert policy()["atr_stop_multiplier"] == 1.5


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
@pytest.mark.parametrize(("offset", "accepted"), [(-2e-9, True), (0, True), (0.5e-9, True), (2e-9, False)])
def test_six_percent_boundary_and_tolerance(side, offset, accepted):
    stop = 100 - (6 + offset) if side == "LONG" else 100 + (6 + offset)
    assert PROFILE.accepts(100, stop) is accepted
    if accepted:
        result = PROFILE.size(100, stop)
        assert result["estimated_stop_loss_usdt"] <= 3 + 1e-6
        assert result["margin_usdt"] >= 5
    else:
        with pytest.raises(OfflineRiskError, match="profile_cap"):
            PROFILE.size(100, stop)


@pytest.mark.parametrize("invalid", [0, -1, float("nan"), float("inf")])
def test_invalid_prices_fail_explicitly(invalid):
    with pytest.raises(OfflineRiskError, match="INVALID_OFFLINE_STOP"):
        PROFILE.size(invalid, 94)


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_fixed_cap_ignores_atr_and_uses_budget_ceiling(side):
    stop = 94 if side == "LONG" else 106
    risk = PROFILE.size(100, stop)
    assert risk["notional_usdt"] == 50
    assert risk["margin_usdt"] == 16.666667
    assert risk["estimated_stop_loss_usdt"] == 3
    assert PROFILE.size(100, 99)["notional_usdt"] == 75
    assert PROFILE.size(100, 99)["margin_usdt"] == 25


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_local_spec_matches_native_when_both_caps_accept(side, offline_only):
    metadata = native_tests.metadata()["exchange_info"]
    request = order(side, 1, margin_usdt=25)
    expected = offline_only.run_until_complete(live.build_live_spec(
        LocalSpecClient(metadata, 100), request, policy(), allowed_symbols=[fixtures.BTC]))
    actual = offline_only.run_until_complete(local_spec(LocalSpecClient(metadata, 100), request, policy(), PROFILE))
    assert actual == expected


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_six_percent_spec_liquidation_and_quantity_floor(side, offline_only):
    from app.liquidation_risk import isolated_liquidation_risk

    metadata = native_tests.metadata()
    spec = offline_only.run_until_complete(local_spec(
        LocalSpecClient(metadata["exchange_info"], 100), order(side), policy(), PROFILE))
    assert spec["quantity"] == "0.5"
    assert spec["notional_usdt"] == 50
    assert isolated_liquidation_risk(spec, metadata["brackets"][fixtures.BTC], policy())["verified"] is True
    too_close = {**spec, "stop_loss": "60" if side == "LONG" else "140"}
    with pytest.raises(ValueError, match="required buffer"):
        isolated_liquidation_risk(too_close, metadata["brackets"][fixtures.BTC], policy())


def test_tick_can_reject_a_raw_boundary_signal_without_inflating_risk(offline_only):
    metadata = native_tests.metadata()["exchange_info"]
    assert PROFILE.accepts(100.0051, 94.0049)
    with pytest.raises(OfflineRiskError, match="profile_cap"):
        offline_only.run_until_complete(local_spec(
            LocalSpecClient(metadata, 100.0051), order(stop_loss=94.0049), policy(), PROFILE))


def test_minimum_notional_and_quantity_remain_rejected(offline_only):
    metadata = native_tests.metadata()["exchange_info"]
    metadata["symbols"][0]["filters"][2]["notional"] = "50"
    # 50 / 101 is rounded down to 0.49, leaving 49.49, not an inflated order.
    request = order(stop_loss=94.94, tp1=107.06, tp2=113.12, tp3=119.18)
    with pytest.raises(OfflineRiskError, match="min_notional"):
        offline_only.run_until_complete(local_spec(LocalSpecClient(metadata, 101), request, policy(), PROFILE))
    metadata["symbols"][0]["filters"][0].update(minQty="1", stepSize="1")
    with pytest.raises(OfflineRiskError, match="min_notional"):
        offline_only.run_until_complete(local_spec(LocalSpecClient(metadata, 100), order(), policy(), PROFILE))


def test_minimum_margin_and_local_client_scope_are_mandatory(offline_only):
    class WrongClient:
        pass

    with pytest.raises(OfflineRiskError, match="LOCAL_SPEC_CLIENT_REQUIRED"):
        offline_only.run_until_complete(local_spec(WrongClient(), order(), policy(), PROFILE))
    spec = offline_only.run_until_complete(local_spec(
        LocalSpecClient(native_tests.metadata()["exchange_info"], 100),
        order(margin_usdt=5), policy(), PROFILE))
    assert spec["margin_usdt"] == 5
    invalid = live.LiveOrderRequest.model_construct(**{**order().model_dump(), "margin_usdt": 4.999})
    with pytest.raises(OfflineRiskError, match="minimum_margin"):
        offline_only.run_until_complete(local_spec(
            LocalSpecClient(native_tests.metadata()["exchange_info"], 100), invalid, policy(), PROFILE))


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_native_lifecycle_and_signals_remain_identical(side, offline_only):
    data = fixtures.dataset(side)
    cfg = fixtures.config(max_leverage=3)
    native, experimental = Engine(copy.deepcopy(data), cfg), OriginalOfflineRiskEngine(data, cfg)
    assert native.data.canonical(fixtures.BTC, fixtures.AT, native.policy) == \
        experimental.data.canonical(fixtures.BTC, fixtures.AT, experimental.policy)
    sign = 1 if side == "LONG" else -1
    signal = {"direction": side, "entry": 100, "stop_loss": 100 - sign,
              "tp1": 100 + sign, "tp2": 100 + sign * 2, "tp3": 100 + sign * 3,
              "confidence": 95, "radar": {"trap_score": 0}, "atr": None}
    # Use the same synthetic opening for exact native spec parity.
    for engine in (native, experimental):
        for series in (engine.data.frames[fixtures.BTC]["15m"], engine.data.marks[fixtures.BTC]):
            series.rows[-1]["open"] = 100
        offline_only.run_until_complete(engine.enter(fixtures.BTC, signal, fixtures.AT))
    assert len(native.trades) == len(experimental.trades) == 1
    assert vars(native.trades[0]) == vars(experimental.trades[0])
    for engine in (native, experimental):
        engine.advance(engine.trades[0], fixtures.AT, opening_only=False)
    assert vars(native.trades[0]) == vars(experimental.trades[0])
    assert native.events == experimental.events and native.cash == experimental.cash
    assert fully_verified(experimental.trades[0])


def test_profile_not_selectable_by_live_demo_or_original_facade():
    with pytest.raises(ValidationError):
        live.PolicyUpdate(max_stop_distance_pct=6)
    request = live.LiveOrderRequest(**{**order().model_dump(), "risk_profile": PROFILE.profile_id})
    assert "risk_profile" not in request.model_dump()
    with pytest.raises(ValueError, match="Unsupported offline strategy"):
        OfflineFacade(fixtures.dataset(), fixtures.config(), strategy_id=PROFILE.experiment_id)
    for module in (live, __import__("app.binance_demo", fromlist=[""])):
        assert "original_offline_risk" not in inspect.getsource(module)


def test_counts_do_not_call_row_r_metrics_or_performance(offline_only):
    data, cfg = fixtures.dataset(), fixtures.config(max_leverage=3)
    engine = OriginalOfflineRiskEngine(data, cfg)
    with patch.object(Position, "row", side_effect=AssertionError("Position.row forbidden")), \
         patch("app.backtest_baseline.metrics", side_effect=AssertionError("performance forbidden")), \
         patch("app.backtest_baseline.bootstrap", side_effect=AssertionError("bootstrap forbidden")), \
         patch("app.backtest_baseline.calculate_r_multiple", side_effect=AssertionError("R forbidden")):
        result = offline_only.run_until_complete(v2_counts.replay_counts(engine, fixtures.phase()))
    assert result["counts"]["fully_verified_completed"] == result["counts"]["completed"]
    assert result["counts"]["risk_stage_arrivals"] == 1
    assert trade_counts(engine.trades)["accepted_entries"] == result["counts"]["accepted_entries"]


def test_prefilter_sizing_and_spec_share_profile_and_denominator(offline_only):
    data = fixtures.dataset()
    engine = OriginalOfflineRiskEngine(data, fixtures.config())
    assert engine.profile is PROFILE and engine.policy["max_leverage"] == 3
    assert engine.prefilter({"entry": 100, "stop_loss": 94})
    assert not engine.prefilter({"entry": 100, "stop_loss": 93})
    assert engine.risk_stage_arrivals == 2
    assert engine.stage_rejections["PREFILTER"]["profile_cap"] == 1
    with pytest.raises(OfflineRiskError, match="COUNTS_RUNNER_REQUIRED"):
        offline_only.run_until_complete(engine.replay())


def test_minimum_order_counted_by_symbol_without_risk_padding(offline_only):
    data = fixtures.dataset()
    data.metadata["exchange_info"]["symbols"][0]["filters"][2]["notional"] = "50"
    engine = OriginalOfflineRiskEngine(data, fixtures.config())
    engine.data.frames[fixtures.BTC]["15m"].rows[-1]["open"] = 101
    signal = {"direction": "LONG", "entry": 101, "stop_loss": 94.94,
              "tp1": 107.06, "tp2": 113.12, "tp3": 119.18,
              "confidence": 95, "radar": {"trap_score": 0}, "atr": None}
    assert engine.prefilter(signal)
    offline_only.run_until_complete(engine.enter(fixtures.BTC, signal, fixtures.AT))
    assert engine.trades == []
    assert engine.rejections["min_notional"] == engine.minimum_by_symbol[fixtures.BTC] == 1
    assert engine.stage_rejections["ENTER"]["min_notional"] == 1


def test_phases_have_fresh_native_state_and_no_cache_carry(offline_only):
    from measure_donchian_counts import locked_plan

    data = fixtures.dataset()
    data.decisions[("previous",)] = {"unused": True}
    phases = (fixtures.phase(name="TRAIN"), fixtures.phase(name="VALIDATION"))
    values = offline_only.run_until_complete(v2_counts.measure_phases(data, locked_plan(), phases=phases))
    assert values["TRAIN"] == values["VALIDATION"]
    assert values["TRAIN"]["state_at_start"] == {"positions": 0, "events": 0, "entries": 0}
    assert data.decisions == {("previous",): {"unused": True}}


@pytest.mark.parametrize("status", ["OPEN_AT_END", "UNKNOWN_DATA_GAP"])
def test_open_unknown_and_funding_incomplete_excluded_from_verified(status, offline_only):
    engine = OriginalOfflineRiskEngine(fixtures.dataset(), fixtures.config())
    offline_only.run_until_complete(v2_counts.replay_counts(engine, fixtures.phase()))
    assert len(engine.trades) == 1
    position = engine.trades[0]
    position.funding_known = False
    assert fully_verified(position) is False
    position.status = status
    counts = trade_counts(engine.trades)
    assert counts["completed"] == counts["fully_verified_completed"] == 0
    assert counts["open_at_end" if status == "OPEN_AT_END" else "unknown_data_gap"] == 1


@pytest.mark.parametrize("path", [
    r"C:\forbidden\donchian-test-synthetic\data.zip",
    r"C:\forbidden\test-sealed.7z", r"C:\forbidden\independent-block\data.zip",
    r"C:\forbidden\donchian-holdout-results\results.json",
    r"C:\forbidden\protrebot-research\test-once.json",
])
def test_guard_blocks_before_any_forbidden_path_is_opened(path):
    hooks = []
    with patch.object(sys, "addaudithook", side_effect=hooks.append), v2_counts.research_path_guard():
        assert len(hooks) == 1
        with pytest.raises(v2_counts.MeasurementError, match="FORBIDDEN_RESEARCH_PATH_ACCESS"):
            hooks[0]("open", (path, "r", 0))
    hooks[0]("open", (path, "r", 0))


def test_test_dates_rejected_before_engine_creation(offline_only):
    with pytest.raises(v2_counts.MeasurementError, match="MEASUREMENT_SCOPE_DENIED"):
        offline_only.run_until_complete(v2_counts.measure_phases(
            fixtures.dataset(), {}, phases=(v2_counts.Phase("TEST", v2_counts.PHASES[-1].end,
                                                          v2_counts.PHASES[-1].end + 900),)))


def test_existing_output_stops_before_any_input_read(tmp_path):
    with patch.object(v2_counts, "OUTPUT", tmp_path), \
         patch.object(v2_counts, "locked_plan", side_effect=AssertionError("input must not be read")):
        assert v2_counts.main(["--metadata", str(v1_counts.METADATA)]) == 2


def make_report(loop):
    from measure_donchian_counts import locked_plan

    engine = OriginalOfflineRiskEngine(fixtures.dataset(), fixtures.config())
    phase = loop.run_until_complete(v2_counts.replay_counts(engine, fixtures.phase()))
    return {"schema": v2_counts.SCHEMA, "profile": asdict(PROFILE),
            "provenance": v2_counts.provenance(locked_plan()),
            "phases": {"TRAIN": phase, "VALIDATION": copy.deepcopy(phase)}, "elapsed_seconds": 0}


def test_report_schema_and_provenance(offline_only):
    report = make_report(offline_only)
    v2_counts.validate_report(report)
    assert report["provenance"]["profile_hash"] == PROFILE.profile_hash
    assert json.loads(json.dumps(report, allow_nan=False)) == report


@pytest.mark.parametrize("key", ["net_r", "pf", "win_rate", "pnl", "return", "trades", "prices", "extra"])
@pytest.mark.parametrize("level", ["root", "phase", "counts", "profile", "provenance", "symbol", "stats", "ratio"])
def test_schema_rejects_every_unapproved_field(key, level, offline_only):
    report = make_report(offline_only)
    phase = report["phases"]["TRAIN"]
    targets = {"root": report, "phase": phase, "counts": phase["counts"], "profile": report["profile"],
               "provenance": report["provenance"], "symbol": phase["by_symbol"][fixtures.BTC],
               "stats": phase["stop_accepted"], "ratio": phase["stop_rejection"]}
    targets[level][key] = 1
    with pytest.raises(v2_counts.MeasurementError, match="V2_COUNTS_SCHEMA_REJECTED"):
        v2_counts.validate_report(report)
