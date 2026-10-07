"""Synthetic counts/parity only; no archive, market history, network or orders."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

import measure_original_counts as measurement
import test_offline_strategy_facade as native_tests
from app import backtest_baseline as baseline
from app import execution_core as core
from app.backtest_data import Series
from build_measurement_view import MeasurementScopeError, load_measurement_dataset

offline_only = native_tests.offline_only
AT = measurement.PHASES[0].start + 86400
BTC = native_tests.SYMBOL


def dataset(side="LONG", mode="original", bars=1):
    data = native_tests.dataset(mode, side)
    shift = AT - native_tests.AT
    for symbol, frames in data.frames.items():
        for interval, series in frames.items():
            rows = [{**row, "time": row["time"] + shift} for row in series.rows]
            if interval == "15m":
                rows.extend({**rows[-1], "time": AT + index * 900} for index in range(1, bars))
            frames[interval] = Series(interval, rows)
        rows = [{**row, "time": row["time"] + shift} for row in data.marks[symbol].rows]
        rows.extend({**rows[-1], "time": AT + index * 900} for index in range(1, bars))
        data.marks[symbol] = Series("15m", rows)
        for event in data.funding[symbol]:
            event["time"] += shift
        data.funding_months[symbol] = {"2020-09", "2020-10"}
    return data


def config(bars=1, **policy):
    return baseline.Config(
        AT, AT + bars * 900, initial_equity=1000, slippage_bps=3, spread_bps=2,
        intrabar="STOP_FIRST", conditional_current_metadata=True, bootstrap_samples=4,
        policy={"allowed_symbols": [BTC], "max_total_exposure_usdt": 350,
                "max_leverage": 10, "mtf_allow_either_timeframe": True, **policy},
    )


def phase(bars=1, name="TRAIN"):
    return measurement.Phase(name, AT, AT + bars * 900)


def open_dataset():
    data = dataset(bars=2)
    data.metadata["exchange_info"]["symbols"][0]["filters"][1]["tickSize"] = "0.001"
    for series in data.frames[BTC].values():
        for row in series.rows:
            if row["time"] < AT:
                row.update(open=row["close"] - 0.65, high=row["close"] + 0.05, low=row["close"] - 0.70)
    for series in (data.frames[BTC]["15m"], data.marks[BTC]):
        for row in series.rows:
            if row["time"] >= AT:
                row.update(high=row["open"] + 0.01, low=row["open"] - 0.01)
    return data


def replay(data, cfg, loop):
    engine = measurement.make_engine(data, cfg)
    return engine, loop.run_until_complete(measurement.replay_counts(engine, phase((cfg.end - cfg.start) // 900)))


def report(loop):
    _, value = replay(dataset(), config(), loop)
    return {
        "schema": measurement.SCHEMA, "definitions": copy.deepcopy(measurement.DEFINITIONS),
        "phases": {"TRAIN": value, "VALIDATION": copy.deepcopy(value)},
        "provenance": {key: "0" * 64 for key in (
            "metadata_sha256", "measurement_manifest_sha256", "stage6b_plan_sha256", "script_sha256")},
        "elapsed_seconds": 0,
    }


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_full_native_engine_state_and_counts_match(side, offline_only):
    data, cfg = dataset(side), config()
    native = baseline.Engine(copy.deepcopy(data), cfg)
    expected = offline_only.run_until_complete(native.replay())
    engine, value = replay(data, cfg, offline_only)
    assert len(expected["trades"]) == len(engine.trades) == 1
    native_tests.exact([position.row() for position in engine.trades], expected["trades"])
    assert engine.events == native.events
    assert engine.cash == native.cash
    assert engine.rejections == native.rejections
    assert engine.first_rejections == native.first_rejections
    assert engine.gate_counts == native.gate_counts
    assert engine.gate_evaluations == native.gate_evaluations
    assert engine.decisions_evaluated == native.decisions_evaluated
    assert value["counts"]["accepted_entries"] == value["counts"]["completed"] == 1
    assert value["counts"]["fully_verified_completed"] == 1
    assert value["canonical_distribution"]["BUY" if side == "LONG" else "SELL"] == 1
    assert value["by_direction"][side]["accepted_entries"] == 1
    assert value["by_symbol"][BTC]["completed"] == 1
    assert value["by_entry_month"]["2020-10"]["completed"] == 1
    assert value["completed_duration_hours"]["median"] == 899 / 3600
    signal = data.canonical(BTC, AT, engine.policy)["analysis"]
    distance = abs(signal["entry"] - signal["stop_loss"]) / signal["entry"] * 100
    assert value["stop_candidates"]["count"] == value["stop_accepted"]["count"] == 1
    assert value["stop_candidates"]["median"] == value["stop_accepted"]["median"] == distance
    assert value["stop_candidates"]["over_2_pct"] == int(value["stop_candidates"]["median"] > 2)


def test_native_wait_details_and_cache_projection_are_unchanged(offline_only):
    data, cfg = dataset(mode="donchian"), config(min_confidence=95)
    native = baseline.Engine(copy.deepcopy(data), cfg)
    offline_only.run_until_complete(native.replay())
    engine, value = replay(data, cfg, offline_only)
    assert engine.rejections == native.rejections
    assert value["canonical_distribution"] == {"BUY": 0, "SELL": 0, "WAIT": 1}
    assert value["counts"]["quality_passed"] == value["counts"]["accepted_entries"] == 0
    assert any(row["count"] for row in value["wait_causes"])
    assert engine.data.decisions == {}
    assert data.decisions == {}


def test_counts_orchestration_never_calls_metrics_bootstrap_or_full_replay(offline_only):
    with patch.object(baseline, "metrics", side_effect=AssertionError("performance forbidden")), \
         patch.object(baseline, "bootstrap", side_effect=AssertionError("bootstrap forbidden")), \
         patch.object(baseline.Engine, "replay", side_effect=AssertionError("full replay forbidden")):
        _, value = replay(dataset(), config(), offline_only)
    assert value["counts"]["completed"] == 1


def test_two_fresh_phases_have_no_position_event_or_cache_carry(offline_only):
    data = open_dataset()
    data.decisions[("prior",)] = {"not": "used"}
    engines = []
    native_make = measurement.make_engine

    def recording(source, cfg):
        engine = native_make(source, cfg)
        engines.append(engine)
        return engine

    plan = measurement.locked_plan()
    phases = (phase(1), measurement.Phase("VALIDATION", AT + 900, AT + 1800))
    with patch.object(measurement, "make_engine", side_effect=recording):
        values = offline_only.run_until_complete(measurement.measure_phases(data, plan, phases=phases))
    assert len(engines) == 2 and engines[0] is not engines[1]
    assert engines[0].data is not engines[1].data
    assert engines[0].events is not engines[1].events
    assert all(value["state_at_start"] == {"positions": 0, "events": 0, "entries": 0} for value in values.values())
    assert values["TRAIN"]["counts"]["open_at_end"] == 1, values["TRAIN"]["native_rejections"]
    assert values["TRAIN"]["counts"]["completed"] == 0
    assert values["TRAIN"]["counts"]["fully_verified_completed"] == 0
    assert data.decisions == {("prior",): {"not": "used"}}


def test_native_missing_mark_becomes_unknown_outside_verified_n(offline_only):
    data = open_dataset()
    data.marks[BTC] = Series("15m", [row for row in data.marks[BTC].rows if row["time"] != AT + 900])
    cfg = measurement.primary_config(phase(2), measurement.locked_plan())
    _, value = replay(data, cfg, offline_only)
    assert value["counts"]["accepted_entries"] == value["counts"]["unknown_data_gap"] == 1
    assert value["counts"]["completed"] == value["counts"]["fully_verified_completed"] == 0
    assert value["counts"]["open_at_end"] == 0


@pytest.mark.parametrize(("stop", "reason"), [(90, "stop_risk"), (97.5, "minimum_margin")])
def test_real_native_sizing_rejections_have_correct_stage(stop, reason, offline_only):
    data, cfg = dataset(), config(max_leverage=30)
    engine = measurement.make_engine(data, cfg)
    signal = copy.deepcopy(engine.data.canonical(BTC, AT, engine.policy)["analysis"])
    signal["stop_loss"] = stop
    offline_only.run_until_complete(engine.enter(BTC, signal, AT))
    assert engine.rejections[reason] == engine.stage_rejections["ENTER"][reason] == 1
    assert engine.attempts == 1 and not engine.trades


def test_test_dates_rejected_before_any_file_open(tmp_path, offline_only):
    with patch.object(Path, "read_bytes", side_effect=AssertionError("TEST input must not be read")):
        with pytest.raises(MeasurementScopeError, match="TEST access denied"):
            load_measurement_dataset(tmp_path, tmp_path / "metadata.json",
                                     measurement.PHASES[-1].end, measurement.PHASES[-1].end + 900)
        with pytest.raises(measurement.MeasurementError, match="SCOPE_DENIED"):
            offline_only.run_until_complete(measurement.measure_phases(
                dataset(), {}, phases=(measurement.Phase("TEST", measurement.PHASES[-1].end,
                                                         measurement.PHASES[-1].end + 900),)))


@pytest.mark.parametrize(("values", "expected"), [
    ([], {"count": 0, "median": None, "p90": None, "p99": None}),
    ([1], {"count": 1, "median": 1, "p90": 1, "p99": 1}),
    ([0, 1, 2, 3, 4], {"count": 5, "median": 2, "p90": 3.6, "p99": 3.96}),
])
def test_stop_and_duration_percentiles(values, expected):
    assert measurement.distribution(values) == expected


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1])
def test_invalid_percentiles_fail_explicitly(value):
    with pytest.raises(measurement.MeasurementError, match="INVALID_DISTRIBUTION"):
        measurement.distribution([value])


def test_wait_memberships_are_overlapping_not_a_partition():
    full = {"decision": "WAIT", "analysis": {
        "direction": "SHORT", "confidence": 79, "radar": {"trap_score": 36, "breakout_quality": 49}},
        "mtf": {"entry_permission": False, "blocked_by_short_filter": True}}
    assert measurement.wait_causes(full, {"min_confidence": 80}) == [
        "confidence", "trap", "breakout", "mtf_permission", "short_filter"]
    assert measurement.wait_causes({"decision": "WAIT", "reason": "INSUFFICIENT_CLOSED_CANDLES"},
                                   {"min_confidence": 80}) == ["data_warmup"]


@pytest.mark.parametrize(("reason", "stage"), [
    ("stop_distance", "PREFILTER"), ("stop_risk", "ENTER"), ("minimum_margin", "ENTER"),
    ("native_spec_rejected", "ENTER"), ("liquidation_buffer", "ENTER"),
    ("daily_trades", "ENTER"), ("exposure", "ENTER"),
])
def test_stage_counters_deduplicate_like_native(reason, stage, offline_only):
    engine = measurement.make_engine(dataset(), config())
    engine.stage = stage
    engine.reject([reason, reason])
    assert engine.rejections[reason] == engine.first_rejections[reason] == 1
    assert engine.stage_rejections[stage][reason] == 1


def test_production_stage6b_primary_config_is_exact(offline_only):
    plan = measurement.locked_plan()
    cfg = measurement.primary_config(measurement.PHASES[0], plan)
    engine = measurement.make_engine(dataset(), cfg)
    assert (cfg.initial_equity, cfg.slippage_bps, cfg.spread_bps, cfg.intrabar) == (1000, 3, 2, "STOP_FIRST")
    assert cfg.policy == {"allowed_symbols": plan["symbols"], "max_total_exposure_usdt": 350}
    assert engine.policy == core.sanitize_execution_policy(
        {**cfg.policy, "fee_bps_per_side": 5, "slippage_bps_per_side": 3}, preserve_empty_allowed_symbols=True)
    assert engine.policy["daily_trade_limit"] == 3 and engine.policy["max_loss_per_trade"] == 3


@pytest.mark.parametrize("field", [
    "net_r", "PF", "win_rate", "pnl", "return", "trade_price", "equity_curve", "trades", "expectancy_r",
])
@pytest.mark.parametrize("location", ["top", "phase", "counts", "group", "stats", "histogram", "definitions", "provenance"])
def test_performance_and_trade_fields_rejected_everywhere(field, location, offline_only):
    value = report(offline_only)
    targets = {
        "top": value, "phase": value["phases"]["TRAIN"], "counts": value["phases"]["TRAIN"]["counts"],
        "group": value["phases"]["TRAIN"]["by_symbol"][BTC], "stats": value["phases"]["TRAIN"]["stop_candidates"],
        "histogram": value["phases"]["TRAIN"]["native_rejections"], "definitions": value["definitions"],
        "provenance": value["provenance"],
    }
    targets[location][field] = 1
    with pytest.raises(measurement.MeasurementError):
        measurement.validate_report(value)


def test_counts_only_report_roundtrip(offline_only):
    value = report(offline_only)
    measurement.validate_report(value)
    measurement.validate_report(json.loads(json.dumps(value, allow_nan=False)))


def test_existing_output_stops_before_inputs(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(measurement, "OUTPUT", tmp_path)
    monkeypatch.setattr(measurement, "locked_plan", lambda: pytest.fail("must not read inputs"))
    assert measurement.main(["--metadata", str(tmp_path / "missing.json")]) == 2
    assert "RESULTS_ALREADY_EXIST" in capsys.readouterr().out
