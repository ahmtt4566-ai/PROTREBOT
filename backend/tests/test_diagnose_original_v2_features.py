"""Synthetic feature/context parity, frozen buckets and TRAIN-only ranking."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

import diagnose_original_v2_features as diagnosis
import test_offline_strategy_facade as fixtures
from app.analysis import analyze


def context(side="LONG"):
    series = fixtures.dataset("original", side).frames[fixtures.SYMBOL]["15m"]
    result = analyze(series.closed(fixtures.AT))
    row = {
        "opened_at_epoch": fixtures.AT, "decision_price": result["entry"], "direction": side,
        "ema20_15m": result["ema"]["ema20"], "atr14_15m": result["atr"], "confidence": result["confidence"],
    }
    return series, row, result


@pytest.mark.parametrize("side", ["LONG", "SHORT"])
def test_four_features_are_original_15m_values_and_exact_24h_open_to_close(side):
    series, row, native = context(side)
    result = diagnosis.feature_values(series, row)
    assert set(result) == {"F1", "F2", "F3", "F4"}
    assert result["F1"] == native["adx"]
    assert result["F2"] == abs(native["ema"]["ema20"] - native["ema"]["ema200"]) / native["atr"]
    assert result["F3"] == native["atr"] / native["entry"]
    assert result["F4"] == (1 if side == "LONG" else -1) * (
        native["entry"] - series.closed(fixtures.AT)[-96]["open"]) / native["atr"]
    series.rows[-1].update(high=1000000, low=.001, close=999999)
    assert diagnosis.feature_values(series, row) == result


def test_context_mismatch_and_insufficient_history_stop_without_imputation():
    series, row, _ = context()
    row["confidence"] += 1
    with pytest.raises(diagnosis.MeasurementError, match="NATIVE_CONTEXT_PARITY"):
        diagnosis.feature_values(series, row)
    series, row, _ = context()
    row["opened_at_epoch"] = series.times[0] + 96 * 900
    with pytest.raises(diagnosis.MeasurementError, match="INCOMPLETE_CLOSED_HISTORY"):
        diagnosis.feature_values(series, row)


def sample(value, net=-1, path="STOP_BEFORE_TP1"):
    return {"net_r": net, "path": path, "features": {feature: value for feature in diagnosis.FEATURES}}


def groups(n=40, low=-1):
    return {
        "LOW": [sample(10, low) for _ in range(n)],
        "MIDDLE": [sample(20, .5, "TP3") for _ in range(n)],
        "HIGH": [sample(30, 1, "TP3") for _ in range(n)],
    }


def assessment(train=None, validation=None):
    grouped = {"TRAIN": groups() if train is None else train,
               "VALIDATION": groups(52) if validation is None else validation}
    rows = {phase: [row for bucket in diagnosis.BUCKETS for row in items[bucket]]
            for phase, items in grouped.items()}
    return diagnosis.assess(grouped, rows)


def test_exact_candidate_n_and_remaining_n_boundaries():
    assert assessment()["candidate"] is True
    v = groups(30)
    v["MIDDLE"] = [sample(20, .5, "TP3") for _ in range(50)]
    v["HIGH"] = [sample(30, 1, "TP3") for _ in range(50)]
    assert assessment(validation=v)["candidate"] is True
    v["HIGH"].pop()
    result = assessment(validation=v)
    assert result["candidate"] is False
    assert "VALIDATION:remaining_N_ge_100" in result["failed_conditions"]
    v["HIGH"].append(sample(30, 1, "TP3"))
    v["LOW"].pop()
    result = assessment(validation=v)
    assert "VALIDATION:excluded_N_ge_30" in result["failed_conditions"]


def test_selected_train_worst_bucket_cannot_be_replaced_using_validation():
    v = groups(52)
    v["LOW"] = [sample(10, 2, "TP3") for _ in range(52)]
    v["HIGH"] = [sample(30, -10) for _ in range(52)]
    result = assessment(validation=v)
    assert result["excluded_bucket"] == "LOW"
    assert not result["candidate"]
    assert "VALIDATION:excluded_mean_below_overall" in result["failed_conditions"]


def test_strict_mean_condition_and_tp3_le_stop_boundary():
    t = groups(40, low=0)
    t["MIDDLE"] = [sample(20, 0) for _ in range(40)]
    t["HIGH"] = [sample(30, 0) for _ in range(40)]
    result = assessment(train=t)
    assert result["excluded_bucket"] == "LOW"
    assert "TRAIN:excluded_mean_below_overall" in result["failed_conditions"]
    t = groups()
    for row in t["LOW"][:20]:
        row["path"] = "TP3"
    assert assessment(train=t)["candidate"]
    t["LOW"][20]["path"] = "TP3"
    result = assessment(train=t)
    assert "TRAIN:excluded_tp3_share_le_stop_before_tp1_share" in result["failed_conditions"]


def test_too_small_worst_bucket_is_not_skipped_for_a_more_eligible_bucket():
    t = groups()
    t["LOW"] = [sample(10, -10) for _ in range(29)]
    result = assessment(train=t)
    assert result["excluded_bucket"] == "LOW" and not result["candidate"]
    assert "TRAIN:excluded_N_ge_30" in result["failed_conditions"]


def test_train_only_cutpoints_validation_membership_and_unpooled_denominators():
    plan = diagnosis.load_plan()
    train = [row for group in groups().values() for row in group]
    val = [sample(100, -1) for _ in range(156)]
    result = diagnosis.summarize({"TRAIN": train, "VALIDATION": val}, plan)
    first = result["cutpoints"]["features"]["F1"]
    assert first == [
        diagnosis.percentile([row["features"]["F1"] for row in train], .333),
        diagnosis.percentile([row["features"]["F1"] for row in train], .667),
    ]
    assert result["tables"]["F1"]["VALIDATION"]["HIGH"]["N"] == 156
    assert result["tables"]["F1"]["VALIDATION"]["LOW"]["N"] == 0
    assert result["tables"]["F1"]["TRAIN"]["LOW"]["N"] == 40
    assert result["cutpoints"]["N"] == 120


def test_feature_ranking_uses_train_lift_even_when_validation_lift_orders_oppositely():
    train, validation = [], []
    for index in range(120):
        row = sample(10 if index < 40 else 20 if index < 80 else 30,
                     -1 if index < 40 else 0, "STOP_BEFORE_TP1" if index < 40 else "TP3")
        second = 30 if index < 32 or 40 <= index < 48 else 10 if index < 80 else 20
        for feature in ("F2", "F3", "F4"):
            row["features"][feature] = second
        train.append(row)
    for index in range(156):
        net = -4 if index < 26 else -1 if index < 52 else -2 if index < 104 else .5
        row = sample(10 if index < 52 else 20 if index < 104 else 30, net,
                     "STOP_BEFORE_TP1" if index < 104 else "TP3")
        second = 30 if index < 26 or 52 <= index < 78 else 10 if index < 104 else 20
        for feature in ("F2", "F3", "F4"):
            row["features"][feature] = second
        validation.append(row)
    result = diagnosis.summarize({"TRAIN": train, "VALIDATION": validation}, diagnosis.load_plan())
    a, b = result["assessments"]["F1"], result["assessments"]["F2"]
    assert a["candidate"] and b["candidate"]
    assert a["train_ranking_score"] > b["train_ranking_score"]
    av, bv = a["phases"]["VALIDATION"], b["phases"]["VALIDATION"]
    assert av["remaining_mean_net_r"] - av["overall_mean_net_r"] < (
        bv["remaining_mean_net_r"] - bv["overall_mean_net_r"])
    assert result["mechanical_candidate"]["selected_feature"] == "F1"


def test_ties_choose_registered_bucket_and_feature_order_and_empty_means_stay_undefined():
    rows = [sample(10, 0) for _ in range(120)]
    result = diagnosis.summarize({"TRAIN": rows, "VALIDATION": rows}, diagnosis.load_plan())
    assert result["mechanical_candidate"]["value"] == "ADAY YOK"
    assert all(value["excluded_bucket"] == "LOW" for value in result["assessments"].values())
    assert result["tables"]["F1"]["TRAIN"]["MIDDLE"]["mean_net_r"] == diagnosis.cell(None, 0)
    assert diagnosis.cell(0, 29)["reliability"] == "guvenilmez"
    assert diagnosis.cell(0, 30)["reliability"] == "descriptive_only"


def test_tp3_share_of_all_tp3_uses_phase_tp3_count_not_bucket_n():
    result = diagnosis.stats([sample(10, 1, "TP3"), sample(10, -1)], 10)
    assert result["tp3_share_pct"] == diagnosis.cell(50, 2)
    assert result["share_of_all_phase_tp3_pct"] == diagnosis.cell(10, 10)


def test_frozen_plan_hash_no_replay_and_only_four_registered_features():
    plan = diagnosis.load_plan()
    assert diagnosis.sha256_file(diagnosis.PLAN) == diagnosis.PLAN_SHA
    assert [feature["id"] for feature in plan["features"]] == list(diagnosis.FEATURES)
    assert plan["candidate_rule"]["ranking_user_confirmed_before_results"] is True
    assert plan["protocol_relaxation"] is False and plan["experiment_attempt_used"] is False
    assert plan["feature_context"]["calculation"] == "EXISTING_APP_ANALYSIS_ANALYZE_NO_CANONICAL_DECISIONS_OR_ENGINE"


def test_existing_output_stops_before_source_reads(tmp_path):
    with patch.object(diagnosis, "OUTPUT", tmp_path), \
         patch.object(diagnosis, "load_plan", side_effect=AssertionError("source read forbidden")):
        assert diagnosis.main() == 2


def test_source_count_mismatch_stops_before_features():
    with pytest.raises(diagnosis.MeasurementError, match="FEATURE_INPUT_N:TRAIN"):
        diagnosis.validate_trades([], "TRAIN", diagnosis.load_plan())


def test_feature_jsonl_persists_exact_values_hash_and_never_overwrites(tmp_path):
    rows = [{"features": {"F1": 20, "F2": 1, "F3": .02, "F4": -1}}]
    before = copy.deepcopy(rows)
    manifest = diagnosis.write_features(tmp_path, "TRAIN", rows)
    path = tmp_path / manifest["file"]
    assert json.loads(path.read_text()) == rows[0]
    assert manifest["N"] == 1 and manifest["sha256"] == diagnosis.sha256_file(path)
    assert rows == before
    with pytest.raises(FileExistsError):
        diagnosis.write_features(tmp_path, "TRAIN", rows)
