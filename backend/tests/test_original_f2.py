"""Synthetic fixed-F2 admission, native parity, safe source scope and decision boundaries."""

from __future__ import annotations

import copy
import json
import math
import sys
from dataclasses import asdict, replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

import measure_original_f2_counts as driver
import prescreen_original_f2 as screen
import test_measure_original_counts as fixtures
import test_offline_strategy_facade as native_tests
from app.backtest_data import Dataset, Series
from app.strategies.original_f2_engine import OriginalF2RiskEngine, f2_value
from app.strategies.original_f2_risk import PARENT_HASH, PROFILE
from app.strategies.original_gap_engine import OriginalGapRiskEngine
from app.strategies.original_gap_risk import PROFILE as GAP_PROFILE
from app.strategies.original_offline_risk import OfflineRiskError
from diagnose_original_v2_trades import DiagnosisEngine

offline_only = native_tests.offline_only


def signal(value=2.0, direction="LONG"):
    return {"direction": direction, "ema": {"ema20": value, "ema200": 0.0}, "atr": 1.0}


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_exact_formula_equal_threshold_rejects_and_next_float_accepts(direction, offline_only):
    engine = OriginalF2RiskEngine(fixtures.dataset(direction), fixtures.config())
    assert f2_value({"ema": {"ema20": 10, "ema200": 6}, "atr": 2}) == 2
    assert f2_value({"ema": {"ema20": 6, "ema200": 10}, "atr": 2}) == 2
    assert not engine.apply_f2(signal(PROFILE.f2_threshold, direction))
    assert engine.apply_f2(signal(math.nextafter(PROFILE.f2_threshold, math.inf), direction))
    counts = engine.filter_counts()
    assert counts["candidates_before"] == 2 and counts["candidates_after"] == 1
    assert counts["rejections_by_direction"][direction] == 1
    assert counts["at_or_below_threshold"] == 1 and counts["uncomputable"] == 0
    assert engine.rejections["f2_filter"] == 1


@pytest.mark.parametrize("broken", [
    {}, {"ema": {}, "atr": 1}, {"ema": None, "atr": 1},
    {"ema": {"ema20": float("nan"), "ema200": 1}, "atr": 1},
    {"ema": {"ema20": 2, "ema200": float("inf")}, "atr": 1},
    {"ema": {"ema20": 2, "ema200": 1}, "atr": float("nan")},
    {"ema": {"ema20": 2, "ema200": 1}, "atr": 0},
    {"ema": {"ema20": True, "ema200": 1}, "atr": 1},
])
def test_uncomputable_values_are_rejected_and_counted_separately(broken, offline_only):
    engine = OriginalF2RiskEngine(fixtures.dataset(), fixtures.config())
    assert f2_value(broken) is None
    assert not engine.apply_f2({**broken, "direction": "LONG"})
    counts = engine.filter_counts()
    assert counts["uncomputable"] == 1 and counts["uncomputable_by_direction"]["LONG"] == 1
    assert counts["at_or_below_threshold"] == 0
    assert engine.rejections["f2_filter"] == 1


def test_profile_parent_parameters_and_hash_are_immutable():
    assert GAP_PROFILE.profile_hash == PARENT_HASH
    assert PROFILE.profile_hash == screen.locked_plan()["profile_sha256"]
    for name, value in asdict(GAP_PROFILE).items():
        if name != "profile_id":
            assert asdict(PROFILE)[name] == value
    assert PROFILE.feature_window == 259 and PROFILE.feature_interval == "15m"
    with pytest.raises(OfflineRiskError, match="UNREGISTERED_OFFLINE_PROFILE"):
        replace(PROFILE, f2_threshold=1.0)
    with pytest.raises(OfflineRiskError, match="UNREGISTERED_OFFLINE_PROFILE"):
        OriginalGapRiskEngine(fixtures.dataset(), fixtures.config(), profile=PROFILE)


class NeutralF2(OriginalF2RiskEngine):
    def f2_accepts(self, signal):
        return True


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_disabled_and_neutral_filter_have_exact_parent_counts_and_lifecycle(side, offline_only):
    data, config = fixtures.dataset(side), fixtures.config()
    parent = OriginalGapRiskEngine(copy.deepcopy(data), config)
    baseline = DiagnosisEngine(copy.deepcopy(data), config)
    neutral = NeutralF2(copy.deepcopy(data), config)
    expected = offline_only.run_until_complete(screen.parent.gap.replay_counts(parent, fixtures.phase()))
    for engine, admission in ((baseline, None), (neutral, neutral.apply_f2)):
        actual = offline_only.run_until_complete(driver.replay_counts(
            engine, fixtures.phase(), admission=admission))
        candidates = actual.pop("filter_candidates")
        assert candidates["before"] == candidates["after"]
        actual["rejections"].pop("f2_filter")
        assert actual == expected
        assert parent.events == engine.events and parent.cash == engine.cash
        assert [position.row() for position in parent.trades] == [position.row() for position in engine.trades]
        assert [position.spec for position in parent.trades] == [position.spec for position in engine.trades]


def test_indicator_values_use_last_closed_native_window_not_future(offline_only):
    data, config = fixtures.dataset(), fixtures.config()
    policy = GAP_PROFILE.policy(config.policy)
    original = data.canonical(fixtures.BTC, fixtures.AT, policy)
    analysis = original["analysis"]
    assert f2_value(analysis) == abs(analysis["ema"]["ema20"] - analysis["ema"]["ema200"]) / analysis["atr"]
    old_rows = data.frames[fixtures.BTC]["15m"].rows
    future_changed = [
        {**row, "open": 1e8, "close": 1e8, "high": 1e8 + 1, "low": 1e8 - 1}
        if row["time"] >= fixtures.AT else row for row in old_rows
    ]
    data.frames[fixtures.BTC]["15m"] = Series("15m", future_changed)
    data.decisions.clear()
    assert data.canonical(fixtures.BTC, fixtures.AT, policy) == original
    assert f2_value(data.canonical(fixtures.BTC, fixtures.AT, policy)["analysis"]) == f2_value(analysis)


class SyntheticSignals(Dataset):
    def canonical(self, symbol, at, policy):
        return {
            "decision": "BUY", "entry_eligible": True, "reason": "APPROVED",
            "analysis": {
                **signal(PROFILE.f2_threshold if symbol == fixtures.BTC else 2),
                "entry": 100, "stop_loss": 99, "tp1": 101, "tp2": 102, "tp3": 103,
                "confidence": 99 if symbol == fixtures.BTC else 95, "radar": {"trap_score": 0},
            },
        }


class RecordingSelections(OriginalF2RiskEngine):
    async def enter(self, symbol, signal, at):
        self.attempts += 1
        self.selected.append(symbol)


def test_rejected_best_candidate_never_consumes_top_three(offline_only):
    data = fixtures.dataset()
    symbols = ("ADAUSDT", "BNBUSDT", fixtures.BTC, "ETHUSDT")
    frames, mark = data.frames[fixtures.BTC], data.marks[fixtures.BTC]
    rules = data.metadata["exchange_info"]["symbols"][0]
    data.metadata["exchange_info"]["symbols"] = []
    for symbol in symbols:
        data.frames[symbol] = copy.deepcopy(frames)
        data.marks[symbol] = copy.deepcopy(mark)
        data.funding[symbol] = copy.deepcopy(data.funding[fixtures.BTC])
        data.metadata["exchange_info"]["symbols"].append({**copy.deepcopy(rules), "symbol": symbol})
    engine = RecordingSelections(data, fixtures.config(allowed_symbols=list(symbols)))
    source = engine.data
    engine.data = SyntheticSignals(source.frames, source.marks, source.funding, source.metadata,
                                   source.report, source.funding_months)
    engine.selected = []
    result = offline_only.run_until_complete(driver.replay_counts(
        engine, fixtures.phase(), admission=engine.apply_f2))
    assert len(engine.selected) == 3 and set(engine.selected) == set(symbols) - {fixtures.BTC}
    assert result["filter_candidates"] == {"before": 4, "after": 3}
    assert engine.filter_counts()["rejections_by_direction"] == {"LONG": 1, "SHORT": 0}
    assert engine.risk_stage_arrivals == 3


def test_filter_rejection_does_not_close_resize_or_move_open_position(offline_only):
    data = fixtures.open_dataset()
    config = fixtures.config(bars=2)
    engine = screen.F2DiagnosisEngine(data, config)
    native = data.canonical(fixtures.BTC, fixtures.AT, engine.policy)["analysis"]
    permitted = copy.deepcopy(native)
    permitted["ema"]["ema200"] = permitted["ema"]["ema20"] - 2 * permitted["atr"]
    offline_only.run_until_complete(engine.enter(fixtures.BTC, permitted, fixtures.AT))
    assert len(engine.positions) == 1
    position = engine.positions[fixtures.BTC]
    snapshot = (copy.deepcopy(position.spec), position.remaining, position.closed_at, engine.cash)
    assert not engine.apply_f2(signal(PROFILE.f2_threshold))
    assert not engine.apply_f2({"direction": "SHORT"})
    assert engine.positions[fixtures.BTC] is position
    assert (position.spec, position.remaining, position.closed_at, engine.cash) == snapshot
    assert id(position) in engine.entry_facts
    assert engine.entry_f2[id(position)]["f2"] == pytest.approx(2)


def test_raw_verified_records_are_persisted_with_native_r_feature_and_hash(offline_only, tmp_path):
    data = fixtures.dataset()
    phase = fixtures.phase(name="BASELINE")
    plan = screen.locked_plan()
    plan["symbols"] = [fixtures.BTC]
    result = offline_only.run_until_complete(screen.run_once(data, plan, phase, tmp_path, lambda value: None))
    raw_path = tmp_path / "trades-BASELINE.jsonl"
    rows = [json.loads(line) for line in raw_path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == result["metrics"]["N"] == result["native_counts"]["counts"]["fully_verified_completed"]
    assert len(rows) == 1
    assert result["raw_trades"]["sha256"] == screen.sha256_file(raw_path)
    assert rows[0]["net_r"] == rows[0]["native_row"]["net_r"]
    assert rows[0]["f2"] == abs(rows[0]["ema20_15m"] - rows[0]["ema200_15m"]) / rows[0]["atr14_15m"]


def archive(month="2024-07", path=None):
    return {
        "symbol": fixtures.BTC, "kind": "klines", "interval": "15m", "status": "OK",
        "month": month, "path": path or f"klines\\{fixtures.BTC}\\15m\\{fixtures.BTC}-15m-{month}.zip",
    }


@pytest.mark.parametrize("record", [
    archive("2023-11"), archive("2024-06"), archive("2026-10"),
    archive(path="..\\outside\\BTCUSDT-15m-2024-07.zip"),
    archive(path="sealed\\BTCUSDT-15m-2024-07.zip"),
    archive(path="registry\\BTCUSDT-15m-2024-07.zip"),
    archive(path="klines\\BTCUSDT\\15m\\BTCUSDT-15m-2023-11.zip"),
])
def test_preopen_scope_guard_rejects_test_registry_escape_and_mislabelled_date(record, tmp_path):
    with pytest.raises(screen.MeasurementError):
        screen.validate_manifest(tmp_path, {"archives": [record]}, {"symbols": [fixtures.BTC]})


def test_real_daily_supplement_shape_is_accepted_without_opening_files(tmp_path):
    record = {
        **archive("2026-06-29", "daily\\markPriceKlines\\BTCUSDT\\15m\\BTCUSDT-15m-2026-06-29.zip"),
        "kind": "markPriceKlines",
    }
    screen.validate_manifest(tmp_path, {"archives": [archive(), record]}, {"symbols": [fixtures.BTC]})
    with pytest.raises(screen.MeasurementError, match="SEALED_MANIFEST_DENIED"):
        screen.validate_manifest(tmp_path, {"sealed": True, "archives": []}, {"symbols": [fixtures.BTC]})


@pytest.mark.parametrize(("n", "mean", "pf", "baseline", "passes"), [
    (100, .1, 1.16, 0, True),
    (99, .1, 1.16, 0, False),
    (100, 0, 1.16, -.1, False),
    (100, .1, 1.15, 0, False),
    (100, .1, None, 0, False),
    (100, .1, float("inf"), 0, False),
    (100, .1, 1.16, .1, False),
    (100, .1, 1.16, None, False),
    (0, None, None, None, False),
])
def test_pinned_success_rule_boundaries_and_undefined_metrics(n, mean, pf, baseline, passes):
    value = screen.decision({"mean_net_r": baseline}, {"N": n, "mean_net_r": mean, "usdt_pf": pf})
    assert (value["value"] == "DEVAM (Demo'da izlemeye aday)") is passes
    assert bool(value["failed_conditions"]) is not passes
    assert value["verification_claim"] is False


def test_halves_use_entry_time_at_fixed_midpoint_not_exit_or_outcome():
    from test_prescreen_original_v2 import observed

    phase = screen.Phase("F2", fixtures.AT, fixtures.AT + 86400)
    midpoint = fixtures.AT + 43200
    before = replace(observed(1), opened_at=midpoint - 1)
    after = replace(observed(-2), opened_at=midpoint)
    assert screen.halves([before, after], phase) == {
        "FIRST": {"N": 1, "mean_net_r": 1},
        "SECOND": {"N": 1, "mean_net_r": -2},
    }


def test_plan_locks_costs_sequence_cleanliness_and_no_runtime_permission():
    plan = screen.locked_plan()
    assert plan["primary"] == {"slippage_bps": 3, "spread_bps": 2, "intrabar": "STOP_FIRST"}
    assert (plan["fee_bps_per_side"], plan["initial_equity"], plan["max_total_exposure_usdt"]) == (5, 1000, 350)
    assert plan["run_sequence"] == ["BASELINE", "F2"]
    assert plan["provenance"]["window_cleanliness"] == "PARTIALLY_CLEAN"
    assert plan["ledger"]["record"] == 4 and plan["ledger"]["attempts_remaining"] == 1
    assert plan["execution"]["sleep_confirmation_required"] is True
    assert plan["network_or_trade"] is False and plan["sealed_registry_or_test_window_reads"] is False
    assert screen.PLAN_SHA == screen.sha256_file(screen.PLAN)
    assert json.loads(screen.PLAN.read_bytes()) == plan
