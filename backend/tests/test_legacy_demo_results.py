import copy
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from app import legacy_demo_results as results
from app import v21_demo


def context(direction="LONG"):
    state = v21_demo.initial_state()
    state["_user_id"] = "user-a"
    state["auto"].update(enabled=True, user_confirmed=True, status="ON")
    state["risk"]["consecutive_losses"] = 2
    plan = {
        "id": "plan-a", "user_id": "user-a", "symbol": "BTCUSDT",
        "source": "AUTO_SCANNER", "direction": direction, "position_side": "BOTH",
        "provenance_state": "CONFIRMED", "provenance_expected_quantity": "2",
        "initial_quantity": "2", "position_status": "OPEN",
        "entry_order_id": 10, "entry_client_order_id": "entry-a",
        "stop_algo_id": 20, "stop_client_id": "stop-a",
        "stop_actual_order_id": 30, "stop_actual_client_order_id": "exit-a",
    }
    demo = {"_user_id": "user-a", "plans": {"plan-a": plan}}
    return state, demo, plan


def fill(*, entry=False, trade_id=1, quantity="2", cumulative=None, pnl="0", fee="0.1", at=1791660000000, **overrides):
    order = {
        "i": 10 if entry else 30, "c": "entry-a" if entry else "exit-a",
        "t": trade_id, "s": "BTCUSDT", "ps": "BOTH",
        "S": "BUY" if entry else "SELL", "R": not entry,
        "l": quantity, "z": cumulative or quantity, "rp": pnl, "n": fee, "N": "USDT",
        "T": at, "x": "TRADE", "X": "FILLED",
    }
    order.update(overrides)
    return {"e": "ORDER_TRADE_UPDATE", "T": at, "o": order}


def observe(state, demo, payload):
    return results.observe_stream(state, demo, payload)


def result(state):
    return state["legacy_trade_results"]["plan-a"]


@pytest.mark.parametrize("pnl,fee,expected,classification", [
    ("-2", "0.1", "-2.2", "loss"),
    ("1", "0.1", "0.8", "profit"),
    ("0.2", "0.1", "0.0", "breakeven"),
    ("0.1", "0.1", "-0.1", "loss"),
])
def test_complete_owned_trade_has_net_result_including_entry_and_exit_fees(pnl, fee, expected, classification):
    state, demo, plan = context()
    before_plan, before_risk, before_auto = copy.deepcopy(plan), copy.deepcopy(state["risk"]), copy.deepcopy(state["auto"])
    observe(state, demo, fill(entry=True))
    notices = observe(state, demo, fill(trade_id=2, pnl=pnl, fee=fee))
    assert result(state)["status"] == "verified"
    assert result(state)["net_pnl"] == expected
    assert result(state)["result"] == classification
    assert result(state)["closed_at"].endswith("+00:00")
    assert [notice["kind"] for notice in notices] == ["LEGACY_RESULT_VERIFIED"]
    assert plan == before_plan
    assert state["risk"] == before_risk
    assert state["auto"] == before_auto


def test_partial_entry_and_partial_exits_are_one_closed_trade():
    state, demo, _ = context()
    observe(state, demo, fill(entry=True, quantity="1", cumulative="1", fee="0.01"))
    observe(state, demo, fill(entry=True, trade_id=2, quantity="1", cumulative="2", fee="0.01"))
    observe(state, demo, fill(trade_id=3, quantity="1", cumulative="1", pnl="2", fee="0.01"))
    assert result(state)["net_pnl"] is None
    assert result(state)["result"] is None
    observe(state, demo, fill(trade_id=4, quantity="1", cumulative="2", pnl="-3", fee="0.01"))
    assert result(state)["status"] == "verified"
    assert result(state)["net_pnl"] == "-1.04"
    assert result(state)["result"] == "loss"
    assert len(state["legacy_trade_results"]) == 1


def test_duplicate_fill_and_restart_do_not_count_fees_twice():
    state, demo, _ = context()
    entry, exit_fill = fill(entry=True), fill(trade_id=2, pnl="-1")
    observe(state, demo, entry)
    observe(state, demo, exit_fill)
    expected = copy.deepcopy(result(state))
    for _ in range(3):
        assert observe(state, demo, entry) == []
        assert observe(state, demo, exit_fill) == []
    assert result(state) == expected
    payload = json.loads(json.dumps(v21_demo.serializable_state(state)))
    restored = v21_demo._state_from_payload(payload, "user-a")
    assert observe(restored, demo, exit_fill) == []
    assert result(restored) == expected


def test_out_of_order_fills_and_late_algo_binding_resolve_without_inventing_result():
    state, demo, plan = context()
    del plan["stop_actual_order_id"]
    del plan["stop_actual_client_order_id"]
    observe(state, demo, fill(trade_id=3, quantity="1", cumulative="2", pnl="-1", at=1791660002000))
    assert result(state)["status"] == "unverified"
    assert result(state)["net_pnl"] is None
    observe(state, demo, fill(trade_id=2, quantity="1", cumulative="1", pnl="0", at=1791660001000))
    observe(state, demo, fill(entry=True, at=1791659999000))
    notices = observe(state, demo, {
        "e": "ALGO_UPDATE",
        "o": {"s": "BTCUSDT", "aid": 20, "ca": "stop-a", "ai": 30, "ac": "exit-a", "X": "TRIGGERED"},
    })
    assert result(state)["status"] == "verified"
    assert result(state)["net_pnl"] == "-1.3"
    assert result(state)["closed_at"] == "2026-10-10T19:20:02+00:00"
    assert notices[-1]["kind"] == "LEGACY_RESULT_VERIFIED"
    assert "stop_actual_order_id" not in plan


@pytest.mark.parametrize("changes,reason", [
    ({"rp": None}, "fill_values_missing"),
    ({"n": None}, "fill_values_missing"),
    ({"l": "NaN"}, "fill_values_missing"),
    ({"rp": "Infinity"}, "fill_values_missing"),
    ({"n": "-1"}, "fill_values_invalid"),
    ({"N": "BNB"}, "commission_unverified"),
    ({"N": None}, "commission_unverified"),
    ({"R": "true"}, "fill_side_unverified"),
    ({"R": False}, "fill_side_unverified"),
    ({"ps": "LONG"}, "fill_position_mismatch"),
    ({"S": "BUY"}, "fill_side_unverified"),
    ({"t": -1}, "fill_identity_missing"),
    ({"T": None}, "fill_time_missing"),
])
def test_missing_or_invalid_exit_data_never_becomes_profit_or_resets_counter(changes, reason):
    state, demo, _ = context()
    observe(state, demo, fill(entry=True))
    observe(state, demo, fill(trade_id=2, pnl="10", **changes))
    row = result(state)
    assert row["status"] == "unverified"
    assert row["result"] is None
    assert row["net_pnl"] is None
    assert reason in row["reasons"]
    assert state["risk"]["consecutive_losses"] == 2


def test_missing_commission_can_be_completed_by_identical_fill_replay():
    state, demo, _ = context()
    observe(state, demo, fill(entry=True))
    observe(state, demo, fill(trade_id=2, pnl="1", n=None))
    assert result(state)["net_pnl"] is None
    observe(state, demo, fill(trade_id=2, pnl="1"))
    assert result(state)["status"] == "verified"
    assert result(state)["net_pnl"] == "0.8"


def test_conflicting_replay_is_not_accepted_as_profit():
    state, demo, _ = context()
    observe(state, demo, fill(entry=True))
    observe(state, demo, fill(trade_id=2, pnl="-1"))
    observe(state, demo, fill(trade_id=2, pnl="10"))
    assert result(state)["status"] == "unverified"
    assert "fill_identity_conflict" in result(state)["reasons"]
    assert result(state)["net_pnl"] is None


@pytest.mark.parametrize("entry_quantity,exit_quantity,exit_cumulative,reason", [
    ("1", "2", "2", "entry_history_incomplete"),
    ("2", "3", "3", "exit_quantity_exceeds_entry"),
    ("2", "2", "3", "fill_history_incomplete"),
])
def test_quantity_completeness_is_required(entry_quantity, exit_quantity, exit_cumulative, reason):
    state, demo, _ = context()
    observe(state, demo, fill(entry=True, quantity=entry_quantity))
    observe(state, demo, fill(trade_id=2, quantity=exit_quantity, cumulative=exit_cumulative, pnl="10"))
    assert result(state)["net_pnl"] is None
    assert reason in result(state)["reasons"]


def test_snapshot_disappearance_and_restored_closed_plan_are_unverified_not_profit():
    state, demo, plan = context()
    before_risk = copy.deepcopy(state["risk"])
    observe(state, demo, fill(entry=True))
    previous = {"positions": [{"symbol": "BTCUSDT"}]}
    current = {"positions": []}
    notices = results.observe_missing_positions(state, demo, previous, current)
    assert any(notice["kind"] == "LEGACY_RESULT_UNVERIFIED" for notice in notices)
    assert "closure_incomplete" in result(state)["reasons"]
    assert result(state)["result"] is None
    assert state["risk"] == before_risk
    assert results.observe_missing_positions(state, demo, previous, current) == []
    plan["position_status"] = "CLOSED"
    state["legacy_trade_results"] = {}
    assert results.observe_missing_positions(state, demo, None, current)
    assert result(state)["net_pnl"] is None


@pytest.mark.parametrize("change", ["owner", "context", "manual", "original", "global"])
def test_unrelated_users_manual_and_original_plans_are_excluded(change):
    state, demo, plan = context()
    if change == "owner":
        plan["user_id"] = "user-b"
    elif change == "context":
        demo["_user_id"] = "user-b"
    elif change == "manual":
        plan["source"] = "MANUAL"
    elif change == "original":
        plan["strategy_id"] = "kais-original-v2-demo-v1"
    else:
        del state["_user_id"]
    assert observe(state, demo, fill(entry=True)) == []
    assert state["legacy_trade_results"] == {}


def test_ambiguous_identity_is_never_verified():
    state, demo, plan = context()
    other = copy.deepcopy(plan)
    other["id"] = "plan-b"
    demo["plans"]["plan-b"] = other
    observe(state, demo, fill(entry=True))
    observe(state, demo, fill(trade_id=2, pnl="10"))
    for row in state["legacy_trade_results"].values():
        assert row["net_pnl"] is None
        assert "fill_identity_ambiguous" in row["reasons"]


def test_short_trade_requires_inverse_sides():
    state, demo, _ = context(direction="SHORT")
    observe(state, demo, fill(entry=True, S="SELL"))
    observe(state, demo, fill(trade_id=2, pnl="-1", S="BUY"))
    assert result(state)["result"] == "loss"


def test_unconfirmed_provenance_cannot_produce_verified_result():
    state, demo, plan = context()
    plan["provenance_state"] = "PROVISIONAL"
    observe(state, demo, fill(entry=True))
    observe(state, demo, fill(trade_id=2, pnl="10"))
    assert result(state)["net_pnl"] is None
    assert "plan_provenance_unverified" in result(state)["reasons"]


def test_observer_journal_records_do_not_double_daily_pnl_and_leave_engine_behavior_unchanged():
    state, demo, _ = context()
    baseline_state, baseline_demo = copy.deepcopy(state), copy.deepcopy(demo)
    before_auto, before_risk = copy.deepcopy(state["auto"]), copy.deepcopy(state["risk"])
    with patch.object(v21_demo, "persist_runtime"):
        for payload in [fill(entry=True), fill(trade_id=2, pnl="-1")]:
            v21_demo._process_stream_event(baseline_state, payload, baseline_demo)
            v21_demo.process_stream_event(state, payload, demo)
    assert result(state)["result"] == "loss"
    assert demo == baseline_demo
    assert state["risk"] == before_risk
    assert state["auto"] == before_auto
    assert v21_demo.daily_metrics(state) == v21_demo.daily_metrics(baseline_state)
    assert any(row["kind"] == "LEGACY_RESULT_VERIFIED" for row in v21_demo.user_journal_items(state))
    assert all(not row["verified_realized"] for row in state["legacy_result_journal"]["journal"])


def test_observer_failure_is_logged_and_cannot_interrupt_existing_stream_processing(caplog):
    state, demo, _ = context()
    with patch.object(v21_demo, "persist_runtime"), \
            patch.object(v21_demo, "observe_legacy_stream", side_effect=RuntimeError("observer failed")):
        assert v21_demo.process_stream_event(state, fill(entry=True), demo)
    assert any(row["kind"] == "FILL" for row in state["journal"])
    assert state["legacy_result_observer_errors"] == ["RuntimeError"]
    assert "Legacy Demo result observation failed" in caplog.text
    assert state["auto"]["status"] == "ON"
    assert state["risk"]["consecutive_losses"] == 2


def test_unverified_result_is_logged_without_notification_or_counter_change():
    state, demo, _ = context()
    with patch.object(v21_demo, "persist_runtime"):
        v21_demo.process_stream_event(state, fill(entry=True), demo)
        v21_demo.process_stream_event(state, fill(trade_id=2, n=None, pnl="10"), demo)
    assert any(row["kind"] == "LEGACY_RESULT_UNVERIFIED" for row in v21_demo.user_journal_items(state))
    assert v21_demo.notification_payload(state) == []
    assert state["risk"]["consecutive_losses"] == 2


def test_utc_exchange_closure_time_survives_day_crossing_and_does_not_unpause():
    state, demo, _ = context()
    state["auto"].update(status="PAUSED", pause_reason="CONSECUTIVE_LOSSES")
    observe(state, demo, fill(entry=True, at=1791676799000))
    observe(state, demo, fill(trade_id=2, pnl="-1", at=1791676801000))
    assert result(state)["closed_at"] == "2026-10-11T00:00:01+00:00"
    assert state["auto"]["status"] == "PAUSED"
    assert state["auto"]["pause_reason"] == "CONSECUTIVE_LOSSES"


def test_result_logging_cannot_evict_trading_fills_or_change_daily_loss_budget():
    state, demo, _ = context()
    state["journal"] = [
        {"id": f"fill-{i}", "kind": "FILL", "created_at": v21_demo.now_iso(),
         "realized_pnl": -1, "verified_realized": True}
        for i in range(v21_demo.JOURNAL_LIMIT)
    ]
    before = copy.deepcopy(state["journal"])
    metrics = v21_demo.daily_metrics(state)
    v21_demo._observe_legacy_results(state, demo, results.observe_missing_positions, {"positions": [{"symbol": "BTCUSDT"}]}, {"positions": []})
    assert state["journal"] == before
    assert v21_demo.daily_metrics(state) == metrics
    assert len(v21_demo.user_journal_items(state)) > len(before)
    restored = v21_demo._state_from_payload(json.loads(json.dumps(v21_demo.serializable_state(state))), "user-a")
    assert restored["journal"] == before
    assert restored["legacy_result_journal"] == state["legacy_result_journal"]


def test_recording_failure_preserves_trading_journal_and_marks_observation_error(caplog):
    state, demo, _ = context()
    with patch.object(v21_demo, "record_event", side_effect=RuntimeError("journal write failed")):
        assert v21_demo._observe_legacy_results(
            state, demo, results.observe_missing_positions,
            {"positions": [{"symbol": "BTCUSDT"}]}, {"positions": []},
        )
    assert state["journal"] == []
    assert state["legacy_trade_results"] == {}
    assert state["legacy_result_observer_errors"] == ["RuntimeError"]
    assert "results remain unverified" in caplog.text


def test_overlapping_cumulative_fills_cannot_masquerade_as_complete_history():
    state, demo, _ = context()
    observe(state, demo, fill(entry=True))
    observe(state, demo, fill(trade_id=2, quantity="1", cumulative="2", pnl="10"))
    observe(state, demo, fill(trade_id=3, quantity="1", cumulative="2", pnl="10"))
    assert result(state)["net_pnl"] is None
    assert "fill_history_incomplete" in result(state)["reasons"]


def test_separate_tp_and_stop_orders_are_aggregated_to_one_plan_result():
    state, demo, plan = context()
    plan["tp1_actual_order_id"] = 31
    plan["tp1_actual_client_order_id"] = "tp1-exit"
    observe(state, demo, fill(entry=True))
    observe(state, demo, fill(trade_id=2, i=31, c="tp1-exit", quantity="1", cumulative="1", pnl="2"))
    assert result(state)["net_pnl"] is None
    observe(state, demo, fill(trade_id=3, quantity="1", cumulative="1", pnl="-3"))
    assert result(state)["net_pnl"] == "-1.3"
    assert result(state)["result"] == "loss"


def test_unlinked_manual_or_safety_close_never_becomes_profit_by_symbol_alone():
    state, demo, _ = context()
    observe(state, demo, fill(entry=True))
    notices = observe(state, demo, fill(trade_id=2, i=99, c="unlinked-close", pnl="10"))
    assert any(notice["reason"] == "exit_identity_unverified" for notice in notices)
    assert result(state)["net_pnl"] is None
    assert state["risk"]["consecutive_losses"] == 2


def test_result_stream_observation_handles_fills_hidden_by_existing_event_deduplication():
    state, demo, _ = context()
    with patch.object(v21_demo, "persist_runtime"):
        v21_demo.process_stream_event(state, fill(entry=True), demo)
        v21_demo.process_stream_event(state, fill(trade_id=2, quantity="1", cumulative="1", pnl="1"), demo)
        assert v21_demo.process_stream_event(state, fill(trade_id=3, quantity="1", cumulative="2", pnl="-2"), demo)
    assert result(state)["net_pnl"] == "-1.3"
    assert len(result(state)["fills"]) == 3
