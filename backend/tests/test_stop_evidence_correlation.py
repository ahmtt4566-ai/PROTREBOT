import copy
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).parents[1]
sys.path.insert(0, str(BACKEND))

from app import binance_demo, stop_evidence, v21_demo  # noqa: E402


def confirmed_plan(**overrides):
    plan = {
        "id": "plan-1",
        "symbol": "BTCUSDT",
        "provenance_state": "CONFIRMED",
        "stop_algo_id": 77,
        "stop_client_id": "PTB_SL_1",
        "remaining_quantity": "2",
        "position_status": "OPEN",
        "tp1_status": "PENDING",
        "tp2_status": "PENDING",
        "tp3_status": "PENDING",
    }
    plan.update(overrides)
    return plan


def algo_update(**overrides):
    event = {
        "e": "ALGO_UPDATE",
        "T": 100,
        "o": {
            "s": "BTCUSDT",
            "aid": 77,
            "ca": "PTB_SL_1",
            "ai": 9001,
            "ac": "PTB_ACTUAL_1",
            "X": "TRIGGERED",
        },
    }
    event["o"].update(overrides)
    return event


def trade_update(**overrides):
    event = {
        "e": "ORDER_TRADE_UPDATE",
        "T": 101,
        "o": {
            "s": "BTCUSDT",
            "i": 9001,
            "c": "PTB_ACTUAL_1",
            "t": 5001,
            "x": "TRADE",
            "X": "FILLED",
            "l": "2",
            "z": "2",
            "R": True,
        },
    }
    event["o"].update(overrides)
    return event


def state_with_plan(plan=None):
    return {
        "evidence_sequence": 0,
        "evidence_observations": [],
        "stop_correlations": [],
        "plans": {"plan-1": plan or confirmed_plan()},
    }


def test_quantity_drop_and_zero_position_do_not_attribute_stop():
    plan = confirmed_plan()
    state = {"plans": {"plan-1": plan}}
    binance_demo.reconcile_demo_plans(
        state,
        {"positions": [{"symbol": "BTCUSDT", "quantity": "1"}]},
    )
    assert not plan.get("stop_fill_confirmed")
    assert plan["tp1_status"] == "PENDING"

    binance_demo.reconcile_demo_plans(state, {"positions": []})
    assert not plan.get("stop_fill_confirmed")
    assert plan["position_status"] == "CLOSED"


@pytest.mark.parametrize("payload", [
    {"e": "ORDER_TRADE_UPDATE", "o": {"s": "BTCUSDT", "x": "TRADE", "t": 1}},
    {"e": "ORDER_TRADE_UPDATE", "o": {"s": "BTCUSDT", "X": "FILLED", "i": 9001, "t": 1}},
])
def test_manual_or_external_or_uncorrelated_execution_does_not_attribute_stop(payload):
    plan = confirmed_plan()
    state = state_with_plan(plan)
    stop_evidence.observe_stream_payload(state, payload, demo_state=state)
    assert not plan.get("stop_fill_confirmed")
    assert state["stop_correlations"] == []


def test_correlated_trade_with_trade_id_confirms_stop_without_touching_tp():
    plan = confirmed_plan()
    state = state_with_plan(plan)
    stop_evidence.observe_stream_payload(state, algo_update(), demo_state=state)
    stop_evidence.observe_stream_payload(state, trade_update(), demo_state=state)

    assert plan["stop_fill_confirmed"] is True
    assert plan["stop_execution_trade_id"] == "5001"
    assert plan["tp1_status"] == "PENDING"
    assert state["stop_correlations"][0]["status"] == "CONFIRMED"
    assert state["stop_correlations"][0]["execution_confirmed"] is True


def test_duplicate_trade_is_idempotent():
    plan = confirmed_plan()
    state = state_with_plan(plan)
    stop_evidence.observe_stream_payload(state, algo_update(), demo_state=state)
    stop_evidence.observe_stream_payload(state, trade_update(), demo_state=state)
    first = copy.deepcopy(state["stop_correlations"])
    stop_evidence.observe_stream_payload(state, trade_update(), demo_state=state)

    assert state["stop_correlations"] == first
    assert plan["stop_execution_trade_id"] == "5001"


def test_order_before_algo_and_restart_incomplete_state_fail_closed():
    plan = confirmed_plan()
    state = state_with_plan(plan)
    stop_evidence.observe_stream_payload(state, trade_update(), demo_state=state)
    assert not plan.get("stop_fill_confirmed")

    restarted = {
        "plans": {"plan-1": confirmed_plan()},
        "stop_correlations": [{
            "status": "INCOMPLETE",
            "plan_id": "plan-1",
            "symbol": "BTCUSDT",
            "actual_order_id": None,
            "actual_client_algo_id": None,
        }],
    }
    stop_evidence.observe_stream_payload(restarted, trade_update(), demo_state=restarted)
    assert not restarted["plans"]["plan-1"].get("stop_fill_confirmed")


def test_p0_b_gate_and_foreign_stop_ownership_are_fail_closed():
    assert binance_demo.can_mutate_lifecycle(confirmed_plan()) is True
    assert binance_demo.can_mutate_lifecycle(confirmed_plan(provenance_state="PROVISIONAL")) is False
    state = state_with_plan(confirmed_plan(stop_algo_id=88, stop_client_id="FOREIGN"))
    stop_evidence.observe_stream_payload(state, algo_update(), demo_state=state)
    assert state["stop_correlations"] == []


def test_summary_evidence_bridge_keeps_bounded_arrays():
    state = v21_demo.initial_state()
    state["evidence_observations"] = [{"observation_id": f"OBS-{index:06d}"} for index in range(250)]
    state["stop_correlations"] = [{"status": "INCOMPLETE"}]
    summary = v21_demo.summary_payload(state)

    assert len(summary["evidence"]["observations"]) == 200
    assert isinstance(summary["evidence"]["correlation"], list)
    assert summary["evidence"]["stop_correlations"] == [{"status": "INCOMPLETE"}]