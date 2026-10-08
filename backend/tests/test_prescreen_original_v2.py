"""Synthetic pre-screen accounting, strict thresholds and unchanged run3 lifecycle."""

from __future__ import annotations

import copy
import sys
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

import prescreen_original_v2 as screen
import test_measure_original_counts as fixtures
import test_offline_strategy_facade as native_tests
from app.backtest_baseline import Position
from app.strategies.original_gap_engine import OriginalGapRiskEngine
from build_measurement_view import (
    TEST_START,
    MeasurementScopeError,
    load_measurement_dataset,
)
from measure_donchian_counts import locked_plan, primary_config

offline_only = native_tests.offline_only


def observed(value, *, at=fixtures.AT, identifier="synthetic", symbol=fixtures.BTC):
    number = Decimal(str(value))
    return screen.Observation(symbol, "LONG", at, at + 899, identifier,
                              Decimal(1), number, float(number), number,
                              Decimal(0), Decimal(0), Decimal(0))


def native_position(side="LONG", *, market=100, quantity="2"):
    direction = 1 if side == "LONG" else -1
    spec = {
        "quantity": quantity, "entry_price": "100", "stop_loss": str(100 - direction),
        "targets": [str(100 + index * direction) for index in (1, 2, 3)],
        "step": Decimal("0.01"), "min_qty": Decimal("0.01"), "min_notional": Decimal(1),
    }
    return Position(fixtures.BTC, side, fixtures.AT, spec, "synthetic",
                    Decimal("0.0003"), Decimal(str(market)), Decimal(str(market)))


def finish(position, *, funding=Decimal("-0.25")):
    position.fill(Decimal(str(position.spec["targets"][0])), position.tp1_quantity,
                  fixtures.AT + 450, "TP1")
    position.fill(Decimal(str(position.spec["targets"][2])), position.remaining,
                  fixtures.AT + 899, "TP3")
    position.funding_amount = funding
    return position


@pytest.mark.parametrize(("n", "mean", "pf", "expected"), [
    (150, 0.01, 1.1501, "DEVAM"),
    (149, 0.01, 2.0, "DUR"),
    (150, 0.0, 2.0, "DUR"),
    (150, -0.01, 2.0, "DUR"),
    (150, 0.01, 1.15, "DUR"),
    (150, 0.01, 1.1499, "DUR"),
    (150, 0.01, None, "DUR"),
    (150, 0.01, float("inf"), "DUR"),
    (0, None, None, "DUR"),
])
def test_exact_registered_thresholds_and_undefined_pf(n, mean, pf, expected):
    good = {"metrics": {"N": 150, "mean_net_r": 0.1, "usdt_pf": 2.0}}
    value = screen.decision({
        "TRAIN": {"metrics": {"N": n, "mean_net_r": mean, "usdt_pf": pf}},
        "VALIDATION": good,
    })
    assert value["value"] == expected
    assert bool(value["failed_conditions"]) is (expected == "DUR")


def test_both_phases_must_pass_not_pooled_n_or_mean():
    phases = {
        "TRAIN": {"metrics": {"N": 300, "mean_net_r": 2.0, "usdt_pf": 3.0}},
        "VALIDATION": {"metrics": {"N": 149, "mean_net_r": 1.0, "usdt_pf": 3.0}},
    }
    assert screen.decision(phases) == {"value": "DUR", "failed_conditions": ["VALIDATION:N_LT_150"]}
    with pytest.raises(screen.MeasurementError, match="DECISION_REQUIRES_BOTH_PHASES"):
        screen.decision({"TRAIN": phases["TRAIN"]})


def test_pf_uses_usdt_not_r_and_exact_boundary_fails():
    observations = [observed(1.15), observed(-1)] + [observed(0) for _ in range(148)]
    metrics, _ = screen.summarize(observations)
    assert metrics["N"] == 150 and metrics["mean_net_r"] > 0
    assert metrics["usdt_pf"] == 1.15
    assert screen.decision({name: {"metrics": metrics} for name in ("TRAIN", "VALIDATION")})["value"] == "DUR"
    first = screen.Observation(fixtures.BTC, "LONG", fixtures.AT, fixtures.AT + 899, "large",
                               Decimal(2), Decimal(4), 2.0, Decimal(4), Decimal(0), Decimal(0), Decimal(0))
    second = observed(-1)
    metrics, _ = screen.summarize([first, second])
    assert metrics["usdt_pf"] == 4
    assert metrics["mean_net_r"] == 0.5


def test_empty_and_no_losses_pf_are_explicitly_undefined():
    empty, costs = screen.summarize([])
    assert empty["N"] == 0 and empty["mean_net_r"] is None and empty["usdt_pf"] is None
    assert empty["pf_status"] == "NO_COMPLETE_TRADES"
    assert empty["max_drawdown_usdt"] is None
    assert costs["mean_cost_r"] == dict.fromkeys(("commission", "slippage", "funding"))
    winner, _ = screen.summarize([observed(1)])
    assert winner["pf_status"] == "NO_LOSSES" and winner["usdt_pf"] is None


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_native_r_cost_identity_includes_actual_partial_quantities_and_funding(side):
    position = finish(native_position(side))
    row = screen.observe(position)
    assert row.net_r == position.row()["net_r"]
    assert row.initial_risk == 2
    metrics, costs = screen.summarize([row])
    assert row.gross_frictionless == Decimal("3.6")
    assert metrics["mean_gross_r"] == 1.8
    assert costs["slippage_usdt"] > 0 and costs["commission_usdt"] > 0
    assert costs["funding_cashflow_usdt"] == -0.25
    assert costs["funding_cost_usdt"] == 0.25
    assert metrics["mean_net_r"] == pytest.approx(
        metrics["mean_gross_r"] - sum(costs["mean_cost_r"].values()), abs=1e-12)
    assert float(row.net) == pytest.approx(
        float(row.gross_frictionless) - costs["slippage_usdt"] - costs["commission_usdt"]
        + costs["funding_cashflow_usdt"], abs=1e-12)


def test_contract_open_gap_is_not_misreported_as_execution_slippage():
    position = native_position(market=110)
    position.fill(Decimal(115), position.remaining, fixtures.AT + 899, "TP3")
    row = screen.observe(position)
    assert row.gross_frictionless == 10
    assert row.slippage == Decimal("0.1350")


def test_only_fully_verified_closes_enter_n_and_ineligible_rows_are_not_read():
    good = finish(native_position())
    open_position = native_position()
    open_position.status = "OPEN_AT_END"
    unknown = native_position()
    unknown.status, unknown.funding_known = "UNKNOWN_DATA_GAP", False
    funding_missing = finish(native_position())
    funding_missing.funding_known = False
    bad_risk = finish(native_position())
    bad_risk.initial_risk = None
    with patch.object(open_position, "row", side_effect=AssertionError("open performance forbidden")), \
         patch.object(unknown, "row", side_effect=AssertionError("unknown performance forbidden")), \
         patch.object(funding_missing, "row", side_effect=AssertionError("incomplete performance forbidden")):
        observations = screen.verified_observations([good, open_position, unknown, funding_missing, bad_risk])
    assert len(observations) == 1
    assert screen.summarize(observations)[0]["N"] == 1


def test_close_timestamp_grouping_and_net_loss_streak_with_breakeven_reset():
    values = [observed(value, at=fixtures.AT + index * 900, identifier=str(index))
              for index, value in enumerate((-1, -2, 0, -1, -1, -1, 4))]
    metrics, _ = screen.summarize(values)
    assert metrics["longest_net_loss_streak"] == 3
    assert metrics["max_drawdown_usdt"] == 6
    tied = [observed(-10, identifier="a"), observed(10, identifier="b")]
    assert screen.summarize(tied)[0]["max_drawdown_usdt"] == 0


def test_bootstrap_is_seeded_nominal_95_and_includes_empty_utc_entry_days():
    rows = [observed(-1), observed(1, identifier="b"), observed(2, at=fixtures.AT + 86400)]
    phase = screen.Phase("TRAIN", fixtures.AT, fixtures.AT + 3 * 86400)
    first = screen.bootstrap_mean(rows, phase)
    second = screen.bootstrap_mean(rows, phase)
    assert first == second
    assert first["samples"] == 20000 and first["seed"] == 2026
    assert first["informational_only"] is True and first["confidence"] == 0.95
    assert first["methods"]["TRADE"]["active_clusters"] == 3
    assert first["methods"]["DAY"]["active_clusters"] == 2
    assert first["methods"]["DAY"]["calendar_or_trade_clusters"] == 3
    for result in first["methods"].values():
        low, high = result["mean_net_r_95"]
        assert -1 <= low <= high <= 2
    empty = screen.bootstrap_mean([], phase)
    assert all(value["mean_net_r_95"] is None for value in empty["methods"].values())


def test_test_dates_are_rejected_by_real_measurement_loader_before_any_io(tmp_path):
    with patch.object(Path, "open", side_effect=AssertionError("TEST must be rejected before any file read")):
        with pytest.raises(MeasurementScopeError, match="TEST access denied"):
            load_measurement_dataset(tmp_path, tmp_path / "metadata.json", TEST_START, TEST_START + 900)
        with pytest.raises(MeasurementScopeError, match="TEST access denied"):
            load_measurement_dataset(tmp_path, tmp_path / "metadata.json", TEST_START - 900, TEST_START + 900)


def synthetic_report(loop):
    data, plan = fixtures.dataset(), locked_plan()
    phases = (fixtures.phase(name="TRAIN"), fixtures.phase(name="VALIDATION"))
    references = {}
    for phase in phases:
        references[phase.name] = loop.run_until_complete(screen.gap.replay_counts(
            OriginalGapRiskEngine(data, primary_config(phase, plan)), phase))
    result = loop.run_until_complete(screen.run_phases(data, plan, references, phases=phases))
    source = screen.gap.provenance(plan)
    provenance = screen.provenance(plan, {name: {"provenance": source} for name in ("TRAIN", "VALIDATION")})
    return {
        "schema": screen.SCHEMA, "decision_rule": copy.deepcopy(screen.RULE),
        "definitions": copy.deepcopy(screen.DEFINITIONS), "profile": asdict(screen.PROFILE),
        "provenance": provenance, "phases": result, "decision": screen.decision(result), "elapsed_seconds": 0,
    }, data, plan, phases, references


def test_phases_have_fresh_engine_state_and_exact_run3_counts(offline_only):
    report, data, plan, phases, references = synthetic_report(offline_only)
    screen.validate_report(report)
    for name in ("TRAIN", "VALIDATION"):
        assert report["phases"][name]["native_counts"] == references[name]
        assert references[name]["state_at_start"] == {"positions": 0, "events": 0, "entries": 0}
        assert report["phases"][name]["metrics"]["N"] == references[name]["counts"]["fully_verified_completed"]
    assert report["phases"]["TRAIN"]["metrics"] == report["phases"]["VALIDATION"]["metrics"]
    assert data.decisions == {}
    changed = copy.deepcopy(references)
    changed["TRAIN"]["counts"]["accepted_entries"] += 1
    with pytest.raises(screen.MeasurementError, match="RUN3_COUNT_PARITY_MISMATCH phase=TRAIN keys=counts"):
        offline_only.run_until_complete(screen.run_phases(data, plan, changed, phases=phases))


def test_symbol_output_rejects_performance_and_rule_cannot_be_relaxed(offline_only):
    report, *_ = synthetic_report(offline_only)
    report["phases"]["TRAIN"]["by_symbol_counts"][fixtures.BTC]["mean_net_r"] = 1
    with pytest.raises(screen.MeasurementError, match=(
        r"^V2_COUNTS_SCHEMA_REJECTED control=mapping_keys "
        r"path=phases\.TRAIN\.by_symbol\.BTCUSDT keys=mean_net_r$"
    )):
        screen.validate_report(report)
    report, *_ = synthetic_report(offline_only)
    report["decision_rule"]["usdt_pf_strictly_above"] = 1.0
    with pytest.raises(screen.MeasurementError, match="fixed_registration"):
        screen.validate_report(report)


def test_existing_output_fails_before_source_or_data_reads(tmp_path):
    with patch.object(screen, "OUTPUT", tmp_path), \
         patch.object(screen, "locked_plan", side_effect=AssertionError("must not read")):
        assert screen.main(["--metadata", str(screen.METADATA)]) == 2
