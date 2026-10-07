"""Synthetic only: no historical dataset, results directory or archive access."""

from __future__ import annotations

import copy
import json
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

import screen_donchian_4h as screen
import test_offline_facade_lifecycle as life
from app.analysis import atr
from app.backtest_baseline import Config, Engine, Position
from app.backtest_data import Dataset, Series
from app.main import v20_target_plan

offline_only = life.offline_only
AT, BTC, ETH = 1601510400, "BTCUSDT", "ETHUSDT"
START = AT + 22 * screen.SIGNAL_STEP
D = Decimal


def candle(at, opening=100.0, high=101.0, low=99.0, close=100.0):
    return life.candle(at, opening, high, low, close)


def data_for(*, bars=16, symbols=(BTC,)):
    end = START + bars * screen.STEP
    frames, marks, funding, months = {}, {}, {}, {}
    for symbol in symbols:
        h4 = [candle(AT + index * screen.SIGNAL_STEP) for index in range(24 + bars // 16)]
        h4[21].update(high=111.0, close=110.0)
        m15 = [candle(at, 111.0, 112.0, 110.0, 111.0) for at in range(AT, end, screen.STEP)]
        frames[symbol] = {"4h": Series("4h", h4), "15m": Series("15m", m15)}
        marks[symbol] = Series("15m", copy.deepcopy(m15))
        funding[symbol] = [
            {"time": at, "rate": 0.0001, "interval_hours": 8.0}
            for at in range(AT - 8 * 3600, end + 8 * 3600, 8 * 3600)
        ]
        months[symbol] = {screen.month(event["time"]) for event in funding[symbol]}
    return Dataset(frames, marks, funding, {}, {"missing_archives": []}, months)


def phase(bars=16, name="TRAIN"):
    return screen.Phase(name, START, START + bars * screen.STEP)


def position(direction="LONG", opened=START):
    sign = 1 if direction == "LONG" else -1
    return screen.ScreenPosition(BTC, direction, opened, D("100"), D(100 - sign),
                                 tuple(D(100 + sign * multiple) for multiple in (1, 2, 3)))


def native_position(value):
    spec = {
        "quantity": str(value.quantity), "entry_price": str(value.entry), "stop_loss": str(value.stop),
        "targets": [str(target) for target in value.targets], "step": D("0.1"),
        "min_qty": D("0.1"), "min_notional": D(0),
    }
    return Position(BTC, value.direction, value.opened_at, spec, "synthetic", screen.SLIP, value.entry, value.entry)


def remove(series, at):
    return Series(series.interval, [row for row in series.rows if row["time"] != at])


def test_closed_decision_atr_and_prior_channels_have_no_lookahead(offline_only):
    data = data_for()
    series = data.frames[BTC]["4h"]
    result = screen.observe(series, START)
    assert result.direction == "LONG"
    rows = series.closed(START, limit=len(series.rows))
    assert rows[-1]["time"] == START - screen.SIGNAL_STEP
    assert result.atr_value == atr([row["high"] for row in rows], [row["low"] for row in rows],
                                   [row["close"] for row in rows], period=14)
    assert result.atr_value != atr([row["high"] for row in rows[:-1]], [row["low"] for row in rows[:-1]],
                                   [row["close"] for row in rows[:-1]], period=14)
    series.rows[22].update(high=999999.0, close=999998.0)
    assert screen.observe(series, START) == result
    series.rows[21]["close"] = 101.0
    assert screen.observe(series, START).direction is None


def test_first_cross_previous_close_uses_its_own_channel(offline_only):
    series = data_for().frames[BTC]["4h"]
    series.rows[20].update(high=110.0, close=109.0)
    series.rows[21].update(high=120.0, close=119.0)
    assert screen.observe(series, START).reason == "already_outside"


def test_gap_resets_atr_prefix_and_requires_22_contiguous_bars(offline_only):
    series = data_for().frames[BTC]["4h"]
    series = remove(series, AT + screen.SIGNAL_STEP)
    assert screen.observe(series, START).reason == "warmup_4h"
    assert screen.WARMUP == max(screen.N + 2, screen.ATR_PERIOD + 1)


def test_entry_is_first_15m_open_after_4h_close_and_one_r_risk(offline_only):
    observation = screen.observe(data_for().frames[BTC]["4h"], START)
    value = screen.create_position(BTC, START, 111.0, observation)
    assert value.opened_at == START
    assert value.entry == D("111.0") and value.entry != D("110.0")
    assert value.actual_entry == value.entry * (1 + screen.SLIP)
    assert value.quantity * abs(value.entry - value.stop) == screen.RISK
    expected = v20_target_plan(111.0, 111.0 - 1.4 * observation.atr_value, "LONG")
    assert value.targets == tuple(D(str(expected[key])) for key in ("tp1", "tp2", "tp3"))


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_stop_first_contract_bar_even_when_tp3_is_touched(direction, offline_only):
    value = position(direction)
    value.advance(candle(START, 100.0, 104.0, 96.0), opening_only=False)
    assert value.status == "CLOSED"
    assert value.exits == [("STOP", D("3"), START + 899)]
    assert value.frictionless / screen.RISK == -1


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_native_fill_commission_and_60_percent_partial_match_exactly(direction, offline_only):
    value = position(direction)
    native = native_position(value)
    assert value.actual_entry == native.actual_entry
    assert value.commission == native.commission
    sign = value.sign
    first = candle(START, 100.0, 101.1 if sign == 1 else 100.5,
                   99.5 if sign == 1 else 98.9)
    second = candle(START + 900, 100.0, 103.1 if sign == 1 else 100.5,
                    99.5 if sign == 1 else 96.9)
    for row in (first, second):
        value.advance(row, opening_only=False)
        native.advance(row, row, "STOP_FIRST", opening_only=False)
        assert value.remaining == native.remaining
        assert value.gross == native.gross
        assert value.commission == native.commission
    assert value.exits[0][1] == D("1.8")
    assert value.exits[1][1] == D("1.2")
    assert value.frictionless / screen.RISK == D("1.8")
    assert value.net == native.gross - native.commission
    costs = value.commission + value.frictionless - value.gross - value.funding
    assert value.frictionless - costs == value.net


def test_tp1_does_not_move_stop_to_break_even(offline_only):
    value = position()
    value.advance(candle(START, 100.0, 101.1, 99.5), opening_only=False)
    value.advance(candle(START + 900, 100.0, 100.5, 99.9), opening_only=False)
    assert value.tp1_hit and value.status == "OPEN" and value.stop == 99


def test_gap_open_fill_uses_open_not_stop_level(offline_only):
    value = position()
    value.advance(candle(START + 900, 97.0, 97.5, 96.5, 97.0), opening_only=True)
    assert value.closed_at == START + 900
    assert value.frictionless / screen.RISK == -3


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_funding_matches_native_method_with_remaining_quantity(direction, offline_only):
    data = data_for()
    value = position(direction)
    native = native_position(value)
    value.remaining = native.remaining = D("1.2")
    at = START + 900
    data.funding[BTC] = [{"time": START, "rate": 0.5, "interval_hours": 8.0},
                         {"time": at, "rate": 0.0002, "interval_hours": 8.0},
                         {"time": at + 123.5, "rate": -0.0001, "interval_hours": 8.0}]
    times = [event["time"] for event in data.funding[BTC]]
    engine = Engine(data, Config(START, START + 1800, conditional_current_metadata=True))
    for opening_only in (True, False):
        screen.settle(data, value, at, times, opening_only=opening_only)
        engine.funding_at(native, at, opening_only=opening_only)
        assert value.funding == native.funding_amount


def test_contract_not_mark_ohlc_controls_exits(offline_only):
    data = data_for(bars=1)
    data.frames[BTC]["15m"].at(START).update(high=130.0)
    report = screen.run_phase(data, phase(1))
    assert report["metrics"]["N"] == 1
    assert report["metrics"]["mean_gross_r"] == pytest.approx(1.8)


def test_closed_trade_with_incomplete_funding_is_unknown_and_excluded(offline_only, monkeypatch):
    data = data_for(bars=1)
    data.frames[BTC]["15m"].at(START).update(high=130.0)
    calls = []

    def incomplete(self, symbol, opened, closed):
        assert self is data
        calls.append((symbol, opened, closed))
        return False

    monkeypatch.setattr(Dataset, "funding_complete", incomplete)
    report = screen.run_phase(data, phase(1))
    assert calls == [(BTC, START, START + 899)]
    assert report["metrics"]["accepted"] == report["metrics"]["uncertain"] == 1
    assert report["metrics"]["N"] == report["metrics"]["open_at_end"] == 0
    assert report["metrics"]["mean_gross_r"] is None
    assert report["metrics"]["mean_net_r"] is None
    assert report["metrics"]["usdt_pf"] is None
    assert report["metrics"]["pf_status"] == "UNDEFINED"
    assert report["uncertain_trades"][0]["reasons"] == ["funding_incomplete"]


def test_empty_metrics_are_undefined_and_fail_the_fixed_rule():
    value = screen.metrics([])
    assert value["N"] == value["accepted"] == 0
    assert value["usdt_pf"] is None and value["pf_status"] == "UNDEFINED"
    assert value["mean_net_r"] is None
    assert screen.decision({"TRAIN": {"metrics": value}, "VALIDATION": {"metrics": value}}) == "BIRAK"


def test_open_position_is_not_verified_n_and_has_no_performance(offline_only):
    report = screen.run_phase(data_for(bars=1), phase(1))
    assert report["metrics"]["accepted"] == 1
    assert report["metrics"]["N"] == 0
    assert report["metrics"]["open_at_end"] == 1
    assert report["metrics"]["mean_net_r"] is None
    assert report["metrics"]["mean_gross_r"] is None
    assert report["open_trades"] == [{"symbol": BTC, "opened_at": screen.stamp(START)}]


def test_overlap_skips_new_first_cross(offline_only, monkeypatch):
    real = screen.observe
    monkeypatch.setattr(screen, "observe", lambda series, at: real(series, START))
    report = screen.run_phase(data_for(bars=17), phase(17))
    assert report["first_crosses"] == 2
    assert report["overlap_skipped"] == 1
    assert report["metrics"]["accepted"] == 1


def test_phase_states_are_fresh_and_validation_has_training_warmup(offline_only):
    data = data_for(bars=2)
    first = screen.run_phase(data, phase(1))
    second_phase = screen.Phase("VALIDATION", START + 900, START + 1800)
    second = screen.run_phase(data, second_phase)
    assert first["metrics"]["open_at_end"] == 1
    assert second["metrics"]["accepted"] == 0 and second["metrics"]["open_at_end"] == 0
    assert screen.observe(data.frames[BTC]["4h"], START).direction == "LONG"


@pytest.mark.parametrize("kind", ["contract", "mark", "funding"])
def test_new_entries_fail_closed_on_missing_current_data(kind, offline_only):
    data = data_for(bars=2)
    if kind == "contract":
        data.frames[BTC]["15m"] = remove(data.frames[BTC]["15m"], START)
    elif kind == "mark":
        data.marks[BTC] = remove(data.marks[BTC], START)
    else:
        data.funding[BTC] = []
    report = screen.run_phase(data, phase(2))
    assert report["metrics"]["accepted"] == 0
    assert report["entry_rejections"]["missing_entry_data"] == 1


@pytest.mark.parametrize("kind", ["contract", "mark", "funding"])
def test_existing_position_becomes_unknown_not_n(kind, offline_only):
    data = data_for(bars=2)
    value = screen.create_position(BTC, START, 111.0, screen.observe(data.frames[BTC]["4h"], START))
    if kind == "contract":
        data.frames[BTC]["15m"] = remove(data.frames[BTC]["15m"], START + 900)
    elif kind == "mark":
        data.marks[BTC] = remove(data.marks[BTC], START + 900)
    else:
        data.funding[BTC] = []
    # Isolate fallback handling: future-gap blackout normally prevents this entry.
    with patch.object(screen, "blackout", return_value=False), patch.object(screen, "create_position", return_value=value):
        if kind == "funding":
            original = screen.missing
            with patch.object(screen, "missing", side_effect=lambda gaps, at, *, position:
                              [] if at == START else original(gaps, at, position=position)):
                report = screen.run_phase(data, phase(2))
        else:
            report = screen.run_phase(data, phase(2))
    assert report["metrics"]["uncertain"] == 1
    assert report["metrics"]["N"] == 0 and report["metrics"]["open_at_end"] == 0
    assert report["uncertain_trades"][0]["detected_at"] == screen.stamp(START + 900)


def test_blackout_uses_seconds_and_is_symbol_local(offline_only):
    gap = screen.Gap("mark", START + screen.BLACKOUT, START + screen.BLACKOUT + 900)
    assert screen.blackout([gap], START)
    assert not screen.blackout([gap], START - 900)
    assert not screen.blackout([gap], int(gap.start))
    data = data_for(bars=screen.BLACKOUT // 900 + 1, symbols=(BTC, ETH))
    data.marks[BTC] = remove(data.marks[BTC], int(gap.start))
    report = screen.run_phase(data, phase(screen.BLACKOUT // 900 + 1))
    assert report["by_symbol_counts"][BTC]["accepted"] == 0
    assert report["by_symbol_counts"][ETH]["accepted"] == 1
    assert report["entry_rejections"]["gap_blackout"] == 1


def test_funding_native_right_interval_gap_and_missing_month(offline_only):
    data = data_for()
    data.funding[BTC] = [
        {"time": START, "rate": 0, "interval_hours": 8},
        {"time": START + 4 * 3600, "rate": 0, "interval_hours": 4},
        {"time": START + 6 * 3600, "rate": 0, "interval_hours": 2},
    ]
    assert not screen.funding_gaps(data, BTC, phase())
    data.funding[BTC][1]["time"] = START + 4 * 3600 + 61
    data.funding[BTC][2]["time"] = START + 6 * 3600 + 61
    gaps = screen.funding_gaps(data, BTC, phase())
    assert gaps[0] == screen.Gap("funding", START, START + 4 * 3600 + 61)
    data.funding_months[BTC] = set()
    assert any(gap.start <= START < gap.end for gap in screen.funding_gaps(data, BTC, phase()))


@pytest.mark.parametrize(("n", "net", "pf", "expected"), [
    (150, 0.01, 1.15000001, "DEVAM"),
    (149, 0.01, 1.2, "BIRAK"),
    (150, 0.0, 1.2, "BIRAK"),
    (150, -0.01, 1.2, "BIRAK"),
    (150, 0.01, 1.15, "BIRAK"),
    (150, 0.01, None, "BIRAK"),
])
def test_fixed_decision_boundaries(n, net, pf, expected):
    value = {"metrics": {"N": n, "mean_net_r": net, "usdt_pf": pf, "pf_status": "DEFINED" if pf is not None else "UNDEFINED"}}
    assert screen.decision({"TRAIN": copy.deepcopy(value), "VALIDATION": value}) == expected


def test_both_phases_must_pass_and_no_loss_pf_is_explicit():
    good = {"metrics": {"N": 150, "mean_net_r": 0.1, "usdt_pf": None, "pf_status": "NO_LOSSES"}}
    assert screen.decision({"TRAIN": good, "VALIDATION": good}) == "DEVAM"
    bad = copy.deepcopy(good)
    bad["metrics"]["N"] = 149
    assert screen.decision({"TRAIN": good, "VALIDATION": bad}) == "BIRAK"


def test_duration_percentiles_and_cost_r_are_exact(offline_only):
    trades = [position(opened=START) for _ in range(3)]
    for trade, hours in zip(trades, (1, 2, 5), strict=True):
        trade.fill(D("103"), trade.remaining, START + hours * 3600, "TP3")
    result = screen.metrics(trades)
    assert result["duration_hours"] == {"median": 2.0, "p90": 4.4, "maximum": 5.0}
    assert result["mean_gross_r"] == 3
    assert result["mean_cost_r"]["funding"] == 0
    assert result["mean_net_r"] == pytest.approx(
        result["mean_gross_r"] - result["mean_cost_r"]["commission"] - result["mean_cost_r"]["slippage"])


def test_schema_contains_no_symbol_or_month_performance(offline_only, monkeypatch):
    monkeypatch.setattr(screen, "PHASES", (phase(1), screen.Phase("VALIDATION", START + 900, START + 1800)))
    report = screen.screen(data_for(bars=2), {"synthetic": True})
    screen.validate_report(report)
    json.dumps(report, allow_nan=False)
    assert report["disclaimer"] == screen.DISCLAIMER
    for value in report["phases"].values():
        for key in ("by_symbol_counts", "by_entry_month_counts"):
            assert all(set(cell) == {"accepted", "N", "uncertain", "open_at_end"} for cell in value[key].values())
    report["phases"]["TRAIN"]["by_symbol_counts"][BTC]["mean_net_r"] = 1
    with pytest.raises(screen.ScreeningError, match="performance"):
        screen.validate_report(report)


def test_existing_results_stop_before_any_metadata_or_dataset_read(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(screen, "OUTPUT", tmp_path)
    monkeypatch.setattr(screen, "digest", lambda _path: pytest.fail("must stop before reading input"))
    assert screen.main(["--metadata", str(tmp_path / "missing.json")]) == 2
    assert "already exists" in capsys.readouterr().err
