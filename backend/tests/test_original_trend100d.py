"""Synthetic SMA600, pre-top3 admission, profile parity and disclosed trial tests."""

from __future__ import annotations

import copy
import inspect
import json
import sys
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

import measure_original_trend_counts as driver
import prescreen_original_trend100d as screen
import test_measure_original_counts as fixtures
import test_offline_strategy_facade as native_tests
import test_prescreen_original_v2 as performance_fixtures
from app.backtest_data import Dataset, Series
from app.strategies.original_gap_engine import OriginalGapRiskEngine
from app.strategies.original_gap_risk import PROFILE as GAP_PROFILE
from app.strategies.original_offline_risk import OfflineRiskError
from app.strategies.original_trend_engine import (
    INTERVAL,
    OriginalTrendRiskEngine,
    closed_sma600,
)
from app.strategies.original_trend_risk import PARENT_HASH, PROFILE, trial_record
from build_measurement_view import (
    TEST_START,
    MeasurementScopeError,
    load_measurement_dataset,
)
from measure_donchian_counts import locked_plan, primary_config

offline_only = native_tests.offline_only


def window(at=fixtures.AT, *, count=600, last=110, future=True):
    rows = [
        {"time": at - (count - index) * INTERVAL, "open": 100, "high": 101, "low": 99,
         "close": 100, "volume": 100, "quote_volume": 20000000}
        for index in range(count)
    ]
    if rows:
        rows[-1].update(open=last, close=last, high=last + 1, low=last - 1)
    if future:
        rows.append({"time": at, "open": 90, "close": 90, "high": 91, "low": 89,
                     "volume": 100, "quote_volume": 20000000})
    return Series("4h", rows)


def extend_original_window(data, side):
    series = data.frames[fixtures.BTC]["4h"]
    count = 600 - len(series.closed(fixtures.AT, limit=600))
    prefix = [
        native_tests.candle(series.times[0] - (count - index) * INTERVAL, 100 if side == "LONG" else 110)
        for index in range(count)
    ]
    data.frames[fixtures.BTC]["4h"] = Series("4h", prefix + series.rows)


@pytest.mark.parametrize(("last", "direction", "cause"), [
    (110, "LONG", "ALLOWED"), (90, "SHORT", "ALLOWED"), (100, "NONE", "EQUAL_TO_SMA"),
])
def test_sma600_includes_decision_candle_and_excludes_unclosed_future(last, direction, cause):
    series = window(last=last)
    observed = closed_sma600(series, fixtures.AT)
    assert observed.direction == direction and observed.cause == cause
    assert observed.sma == pytest.approx((599 * 100 + last) / 600)
    series.rows[-1]["close"] = 1000000
    assert closed_sma600(series, fixtures.AT) == observed
    assert inspect.signature(Series.closed).parameters["limit"].default == 259
    assert len(series.closed(fixtures.AT)) == 259


def test_incomplete_discontinuous_stale_and_invalid_history_never_allow_entry():
    assert closed_sma600(window(count=599), fixtures.AT).cause == "INSUFFICIENT_WINDOW"
    series = window(count=601)
    series = Series("4h", [row for index, row in enumerate(series.rows) if index != 300])
    assert closed_sma600(series, fixtures.AT).cause == "NON_CONTIGUOUS_WINDOW"
    assert closed_sma600(window(future=False), fixtures.AT + INTERVAL).cause == "MISSING_LAST_CLOSED_4H"
    series = window()
    series.rows[10]["close"] = float("nan")
    assert closed_sma600(series, fixtures.AT).cause == "INVALID_4H_CLOSE"


def test_ready_at_exact_100_days_and_validation_reads_train_history():
    start = screen.PHASES[0].start
    ready = start + 600 * INTERVAL
    series = window(ready)
    assert series.times[0] == start
    assert closed_sma600(series, ready - 900).cause == "INSUFFICIENT_WINDOW"
    assert closed_sma600(series, ready).direction == "LONG"
    validation = screen.PHASES[1].start
    history = window(validation)
    assert history.times[0] >= screen.PHASES[0].start
    assert history.times[-2] < validation
    assert closed_sma600(history, validation).direction == "LONG"


def test_cache_uses_closed_slot_not_available_index_after_missing_bar(offline_only):
    data = fixtures.dataset()
    data.frames[fixtures.BTC]["4h"] = window(future=False)
    engine = OriginalTrendRiskEngine(data, fixtures.config())
    assert engine.permission(fixtures.BTC, fixtures.AT).direction == "LONG"
    assert engine.permission(fixtures.BTC, fixtures.AT + INTERVAL).direction == "NONE"


@pytest.mark.parametrize(("last", "allowed", "blocked"), [(110, "LONG", "SHORT"), (90, "SHORT", "LONG")])
def test_direction_gate_only_allows_the_matching_candidate(last, allowed, blocked, offline_only):
    data = fixtures.dataset()
    data.frames[fixtures.BTC]["4h"] = window(last=last)
    engine = OriginalTrendRiskEngine(data, fixtures.config())
    assert engine.apply_trend(fixtures.BTC, allowed, fixtures.AT)
    assert not engine.apply_trend(fixtures.BTC, blocked, fixtures.AT)
    assert engine.candidates_before_trend == 2 and engine.candidates_after_trend == 1
    assert engine.trend_rejections[blocked] == 1
    assert engine.rejections["trend_filter"] == 1


def test_profile_parent_hash_risk_settings_and_relaxation_record_are_fixed():
    assert GAP_PROFILE.profile_hash == PARENT_HASH
    assert PROFILE.profile_hash != PARENT_HASH
    for key, value in asdict(GAP_PROFILE).items():
        if key != "profile_id":
            assert asdict(PROFILE)[key] == value
    assert (PROFILE.trend_interval, PROFILE.trend_period) == ("4h", 600)
    with pytest.raises(OfflineRiskError, match="UNREGISTERED_OFFLINE_PROFILE"):
        replace(PROFILE, trend_period=599)
    with pytest.raises(OfflineRiskError, match="UNREGISTERED_OFFLINE_PROFILE"):
        OriginalGapRiskEngine(fixtures.dataset(), fixtures.config(), profile=PROFILE)
    record = trial_record()
    assert [item["number"] for item in record["relaxation_ledger"]] == [1, 2, 3]
    assert record["validation_clean"] is False
    assert record["attempt_budget"] == 5 and record["attempts_remaining"] == 2


class UnfilteredForParity(OriginalTrendRiskEngine):
    def trend_accepts(self, symbol, direction, at):
        return True


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_disabled_local_test_seam_is_exactly_gap18d_without_global_patch(side, offline_only):
    data, cfg = fixtures.dataset(side), fixtures.config()
    old, new = OriginalGapRiskEngine(data, cfg), UnfilteredForParity(data, cfg)
    assert old.data.canonical(fixtures.BTC, fixtures.AT, old.policy) == new.data.canonical(
        fixtures.BTC, fixtures.AT, new.policy)
    old.data.decisions.clear()
    new.data.decisions.clear()
    expected = offline_only.run_until_complete(screen.parent.gap.replay_counts(old, fixtures.phase()))
    actual = offline_only.run_until_complete(driver.replay_counts(new, fixtures.phase()))
    actual.pop("trend_filter")
    actual["rejections"].pop("trend_filter")
    assert actual == expected
    assert old.events == new.events and old.cash == new.cash
    assert [position.spec for position in old.trades] == [position.spec for position in new.trades]


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_matching_filter_is_neutral_and_keeps_original_signal_stop_and_targets(side, offline_only):
    data = fixtures.dataset(side)
    original = data.canonical(fixtures.BTC, fixtures.AT, GAP_PROFILE.policy(fixtures.config().policy))
    data.decisions.clear()
    extend_original_window(data, side)
    cfg = fixtures.config()
    old, new = OriginalGapRiskEngine(data, cfg), OriginalTrendRiskEngine(data, cfg)
    assert old.data.canonical(fixtures.BTC, fixtures.AT, old.policy) == original
    assert old.data.canonical(fixtures.BTC, fixtures.AT, old.policy) == new.data.canonical(
        fixtures.BTC, fixtures.AT, new.policy)
    old.data.decisions.clear()
    new.data.decisions.clear()
    expected = offline_only.run_until_complete(screen.parent.gap.replay_counts(old, fixtures.phase()))
    actual = offline_only.run_until_complete(driver.replay_counts(new, fixtures.phase()))
    trend = actual.pop("trend_filter")
    assert trend["rejections_by_direction"] == {"LONG": 0, "SHORT": 0}
    assert trend["candidates_before"] == trend["candidates_after"] == 1
    actual["rejections"].pop("trend_filter")
    assert actual == expected


class SyntheticSignals(Dataset):
    def canonical(self, symbol, at, policy):
        return {
            "decision": "BUY", "entry_eligible": True, "reason": "APPROVED",
            "analysis": {"direction": "LONG", "entry": 100, "stop_loss": 99,
                         "tp1": 101, "tp2": 102, "tp3": 103,
                         "confidence": 99 if symbol == fixtures.BTC else 95,
                         "radar": {"trap_score": 0}, "atr": None},
        }


class RecordingSelections(OriginalTrendRiskEngine):
    async def enter(self, symbol, signal, at):
        self.attempts += 1
        self.selected.append(symbol)


def test_rejected_high_confidence_candidate_does_not_consume_top_three(offline_only):
    data = fixtures.dataset()
    symbols = ("ADAUSDT", "BNBUSDT", fixtures.BTC, "ETHUSDT")
    original_frames, original_mark = data.frames[fixtures.BTC], data.marks[fixtures.BTC]
    original_rules = data.metadata["exchange_info"]["symbols"][0]
    data.metadata["exchange_info"]["symbols"] = []
    for symbol in symbols:
        data.frames[symbol] = copy.deepcopy(original_frames)
        data.frames[symbol]["4h"] = window(last=90 if symbol == fixtures.BTC else 110)
        data.marks[symbol] = copy.deepcopy(original_mark)
        data.funding[symbol] = copy.deepcopy(data.funding[fixtures.BTC])
        data.metadata["exchange_info"]["symbols"].append({**copy.deepcopy(original_rules), "symbol": symbol})
    cfg = fixtures.config(allowed_symbols=list(symbols))
    engine = RecordingSelections(data, cfg)
    source = engine.data
    engine.data = SyntheticSignals(source.frames, source.marks, source.funding, source.metadata,
                                   source.report, source.funding_months)
    engine.selected = []
    result = offline_only.run_until_complete(driver.replay_counts(engine, fixtures.phase()))
    assert len(engine.selected) == 3 and set(engine.selected) == set(symbols) - {fixtures.BTC}
    assert result["trend_filter"]["candidates_before"] == 4
    assert result["trend_filter"]["candidates_after"] == 3
    assert result["trend_filter"]["rejections_by_direction"] == {"LONG": 1, "SHORT": 0}


def test_trend_change_does_not_close_or_move_an_existing_position(offline_only):
    data = fixtures.dataset(bars=17)
    data.frames[fixtures.BTC]["4h"] = window(last=110)
    for series in (data.frames[fixtures.BTC]["15m"], data.marks[fixtures.BTC]):
        for row in series.rows:
            if row["time"] >= fixtures.AT:
                row.update(open=100, close=100, high=100.1, low=99.9)
    engine = OriginalTrendRiskEngine(data, fixtures.config(bars=17))
    signal = SyntheticSignals.canonical(None, fixtures.BTC, fixtures.AT, engine.policy)["analysis"]
    offline_only.run_until_complete(engine.enter(fixtures.BTC, signal, fixtures.AT))
    assert len(engine.trades) == 1
    position, spec = engine.trades[0], copy.deepcopy(engine.trades[0].spec)
    assert engine.permission(fixtures.BTC, fixtures.AT + INTERVAL).direction == "SHORT"
    engine.advance(position, fixtures.AT + INTERVAL, opening_only=True)
    assert engine.positions[fixtures.BTC] is position
    assert position.spec == spec and position.remaining == position.quantity


@pytest.mark.parametrize(("n", "mean", "pf", "value"), [
    (99, 0.1, 2, "DUR"), (100, 0.1, 1.1501, "DEVAM"),
    (100, 0, 2, "DUR"), (100, 0.1, 1.15, "DUR"), (100, 0.1, None, "DUR"),
])
def test_new_n_threshold_is_fixed_before_results(n, mean, pf, value):
    result = screen.decision({"VALIDATION": {"metrics": {"N": n, "mean_net_r": mean, "usdt_pf": pf}}})
    assert result["value"] == value and result["scope"] == "VALIDATION_ONLY"


def test_each_phase_must_independently_pass_all_three_rules():
    passed = {"metrics": {"N": 100, "mean_net_r": 0.01, "usdt_pf": 1.16}}
    failed = {"metrics": {"N": 99, "mean_net_r": 1000, "usdt_pf": 1000}}
    result = screen.decision({"TRAIN": failed, "VALIDATION": passed})
    assert result["value"] == "DUR" and result["failed_conditions"] == ["TRAIN:N_LT_100"]
    assert screen.decision({"TRAIN": passed, "VALIDATION": passed})["value"] == "DEVAM"
    assert screen.decision({"TRAIN": passed, "VALIDATION": failed})["failed_conditions"] == ["VALIDATION:N_LT_100"]


def test_n_excludes_open_unknown_and_funding_incomplete_closures():
    good = performance_fixtures.finish(performance_fixtures.native_position())
    ineligible = [performance_fixtures.native_position() for _ in range(3)]
    ineligible[0].status = "OPEN_AT_END"
    ineligible[1].status, ineligible[1].funding_known = "UNKNOWN_DATA_GAP", False
    performance_fixtures.finish(ineligible[2])
    ineligible[2].funding_known = False
    with patch.object(ineligible[0], "row", side_effect=AssertionError("open performance forbidden")), \
         patch.object(ineligible[1], "row", side_effect=AssertionError("unknown performance forbidden")), \
         patch.object(ineligible[2], "row", side_effect=AssertionError("incomplete performance forbidden")):
        metrics, _ = screen.parent.summarize(screen.parent.verified_observations([good, *ineligible]))
    assert metrics["N"] == 1
    empty, _ = screen.parent.summarize([])
    assert empty["N"] == 0 and empty["usdt_pf"] is None
    assert screen.decision({"VALIDATION": {"metrics": empty}})["value"] == "DUR"


def test_test_dates_are_denied_before_io_or_native_replay(tmp_path, offline_only):
    with patch.object(Path, "open", side_effect=AssertionError("TEST input read forbidden")), \
         pytest.raises(MeasurementScopeError, match="TEST access denied"):
        load_measurement_dataset(tmp_path, tmp_path / "metadata.json", TEST_START, TEST_START + 900)
    phase = screen.parent.Phase("VALIDATION", TEST_START, TEST_START + 900)
    with pytest.raises(screen.MeasurementError, match="MEASUREMENT_SCOPE_DENIED"):
        offline_only.run_until_complete(screen.phase_result(None, None, phase, None))


def synthetic_report(loop):
    data, plan, phase = fixtures.dataset(), locked_plan(), fixtures.phase(name="VALIDATION")
    cfg = primary_config(phase, plan)
    reference = loop.run_until_complete(screen.parent.gap.replay_counts(OriginalGapRiskEngine(data, cfg), phase))
    result = loop.run_until_complete(screen.phase_result(data, plan, phase, reference))
    original = screen.parent.gap.provenance(plan)
    record = screen.provenance(plan, {name: {"provenance": original} for name in ("TRAIN", "VALIDATION")})
    phases = {"VALIDATION": result}
    return {
        "trial_record": trial_record(), "schema": screen.SCHEMA, "decision_rule": copy.deepcopy(screen.RULE),
        "definitions": copy.deepcopy(screen.DEFINITIONS), "profile": asdict(PROFILE), "provenance": record,
        "phases": phases, "decision": screen.decision(phases), "elapsed_seconds": 0,
    }


def test_report_discloses_warmup_permissions_and_dirty_validation(offline_only):
    report = synthetic_report(offline_only)
    screen.validate_report(report)
    trend = report["phases"]["VALIDATION"]["native_counts"]["trend_filter"]
    assert report["trial_record"] == report["provenance"]["trial_record"]
    assert report["trial_record"]["validation_clean"] is False
    assert trend["all_symbols_full_warmup_no_entry_days"] == 1
    assert trend["by_symbol"][fixtures.BTC]["permission_pct"] == {"LONG": 0, "SHORT": 0, "NONE": 100}
    report["trial_record"]["validation_clean"] = True
    with pytest.raises(screen.MeasurementError, match="TREND_SCHEMA registration"):
        screen.validate_report(report)


def test_schema_does_not_allow_symbol_performance_rule_change_or_false_trend_denominator(offline_only):
    report = synthetic_report(offline_only)
    report["phases"]["VALIDATION"]["by_symbol_counts"] = copy.deepcopy(
        report["phases"]["VALIDATION"]["by_symbol_counts"])
    report["phases"]["VALIDATION"]["by_symbol_counts"][fixtures.BTC]["mean_net_r"] = 1
    with pytest.raises(screen.MeasurementError, match="TREND_SCHEMA keys:symbol.counts_only"):
        screen.validate_report(report)
    report = synthetic_report(offline_only)
    report["decision_rule"]["minimum_fully_verified_completed"] = 99
    with pytest.raises(screen.MeasurementError, match="TREND_SCHEMA registration"):
        screen.validate_report(report)
    report = synthetic_report(offline_only)
    report["phases"]["VALIDATION"]["native_counts"]["trend_filter"]["candidates_after"] += 1
    with pytest.raises(screen.MeasurementError, match="field=candidate.partition"):
        screen.validate_report(report)


def test_new_phase_engines_do_not_carry_positions_events_or_cached_decisions(offline_only):
    data, plan = fixtures.dataset(), locked_plan()
    results = []
    for name in ("VALIDATION", "TRAIN"):
        phase = fixtures.phase(name=name)
        reference = offline_only.run_until_complete(
            screen.parent.gap.replay_counts(OriginalGapRiskEngine(data, primary_config(phase, plan)), phase))
        results.append(offline_only.run_until_complete(screen.phase_result(data, plan, phase, reference)))
    assert all(value["native_counts"]["state_at_start"] == {"positions": 0, "events": 0, "entries": 0}
               for value in results)
    assert results[0]["metrics"] == results[1]["metrics"]
    assert data.decisions == {}

def test_existing_output_and_missing_validation_stop_before_input_read(tmp_path):
    with patch.object(screen, "OUTPUT", tmp_path), \
         patch.object(screen, "locked_plan", side_effect=AssertionError("input read forbidden")):
        assert screen.main(["--metadata", str(screen.METADATA), "--phase", "VALIDATION"]) == 2
        assert screen.main(["--metadata", str(screen.METADATA), "--phase", "TRAIN"]) == 2


def test_failed_validation_cannot_start_train(tmp_path, offline_only):
    report = synthetic_report(offline_only)
    screen.validate_report(report)
    assert report["decision"]["value"] == "DUR"
    directory = tmp_path / "VALIDATION"
    directory.mkdir()
    (directory / "prescreen.json").write_text(json.dumps(report), encoding="utf-8")
    with patch.object(screen, "OUTPUT", tmp_path), \
         patch.object(screen, "locked_plan", side_effect=AssertionError("TRAIN input forbidden")):
        assert screen.main(["--metadata", str(screen.METADATA), "--phase", "TRAIN"]) == 2
    assert not (tmp_path / "TRAIN").exists()
