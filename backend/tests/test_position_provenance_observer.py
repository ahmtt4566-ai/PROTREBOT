import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND = Path(__file__).parents[1]
sys.path.insert(0, str(BACKEND))

from app import binance_demo  # noqa: E402


def entry_plan(**overrides):
    plan = {
        "id": "plan-1",
        "symbol": "BTCUSDT",
        "position_side": "BOTH",
        "provenance_state": "PROVISIONAL",
        "provenance_entry_client_order_id": "PTB_ENTRY_1",
        "provenance_entry_order_id": 101,
        "provenance_trade_ids": [],
        "provenance_pre_entry_quantity": "0",
        "provenance_expected_quantity": "1",
        "provenance_last_observed_quantity": None,
        "provenance_last_reconciliation_at": None,
        "provenance_last_observation_id": None,
        "provenance_broken_reason": None,
        "status": "OPEN",
    }
    plan.update(overrides)
    return plan


def test_no_provenance_is_no_provenance():
    assert binance_demo.derive_provenance_state() is binance_demo.ProvenanceState.NO_PROVENANCE


def test_entry_recorded_is_provisional():
    assert binance_demo.derive_provenance_state(entry_recorded=True) is binance_demo.ProvenanceState.PROVISIONAL


def test_first_observation_cannot_directly_confirm():
    assert binance_demo.derive_provenance_state(
        entry_recorded=True,
        trade_ids=[1],
        expected_quantity="1",
        observed_quantity="1",
        provenance_linked_observation=True,
        current_observation_id="obs-1",
    ) is binance_demo.ProvenanceState.PROVISIONAL


def test_independent_reconciliation_exact_quantity_confirms():
    assert binance_demo.derive_provenance_state(
        entry_recorded=True,
        trade_ids=[1],
        expected_quantity="1",
        observed_quantity="1",
        independent_reconciliation=True,
        provenance_linked_observation=True,
        previous_observation_id="obs-1",
        current_observation_id="obs-2",
        last_observation_id="obs-1",
    ) is binance_demo.ProvenanceState.CONFIRMED


def test_same_observation_cannot_confirm():
    plan = entry_plan(provenance_last_observation_id="obs-1")
    binance_demo.observe_plan_provenance(
        plan,
        observed_quantity="1",
        trade_ids=[1],
        independent_reconciliation=True,
        provenance_linked_observation=True,
        previous_observation_id="obs-1",
        current_observation_id="obs-1",
    )
    assert plan["provenance_state"] == "PROVISIONAL"


def test_boolean_cannot_forge_independence():
    assert binance_demo.derive_provenance_state(
        entry_recorded=True,
        trade_ids=[1],
        expected_quantity="1",
        observed_quantity="1",
        independent_reconciliation=True,
        provenance_linked_observation=True,
    ) is binance_demo.ProvenanceState.PROVISIONAL


def test_unexplained_quantity_delta_is_broken():
    plan = entry_plan()
    binance_demo.observe_plan_provenance(
        plan,
        observed_quantity="2",
        trade_ids=[1],
        independent_reconciliation=True,
        provenance_linked_observation=True,
    )
    assert plan["provenance_state"] == "BROKEN"
    assert plan["provenance_broken_reason"]


def test_broken_does_not_auto_heal():
    plan = entry_plan(provenance_state="BROKEN", provenance_broken_reason="operator_unknown_delta")
    binance_demo.observe_plan_provenance(
        plan,
        observed_quantity="1",
        trade_ids=[1, 2],
        independent_reconciliation=True,
        provenance_linked_observation=True,
    )
    assert plan["provenance_state"] == "BROKEN"
    assert plan["provenance_broken_reason"] == "operator_unknown_delta"


def test_restart_of_confirmed_is_provisional():
    application = SimpleNamespace()
    state = binance_demo._state_from_demo_payload(
        {"plans": {"confirmed": entry_plan(
            provenance_state="CONFIRMED",
            provenance_trade_ids=[1],
            provenance_last_observation_id="obs-1",
        )}},
        "user-1",
        application,
        "test",
        "restored",
        "available",
    )
    assert state["plans"]["confirmed"]["provenance_state"] == "PROVISIONAL"


def test_legacy_plan_restore_is_no_provenance():
    state = binance_demo._state_from_demo_payload(
        {"plans": {"legacy": {"entry_order_id": 101, "entry_client_order_id": "PTB_ENTRY_1", "position_id": "local-plan"}}},
        "user-1",
        SimpleNamespace(),
        "test",
        "restored",
        "available",
    )
    assert state["plans"]["legacy"]["provenance_state"] == "NO_PROVENANCE"


def test_legacy_entry_order_id_only_is_no_provenance():
    plan = {"entry_order_id": 101, "position_id": "local-plan"}
    binance_demo.ensure_plan_provenance_fields(plan)
    assert plan["provenance_state"] == "NO_PROVENANCE"
    assert plan["provenance_entry_order_id"] is None


def test_legacy_client_order_id_only_is_no_provenance():
    plan = {"entry_client_order_id": "PTB_ENTRY_1"}
    binance_demo.ensure_plan_provenance_fields(plan)
    assert plan["provenance_state"] == "NO_PROVENANCE"
    assert plan["provenance_entry_client_order_id"] is None


def test_legacy_local_position_id_only_is_no_provenance():
    plan = {"position_id": "local-plan"}
    binance_demo.ensure_plan_provenance_fields(plan)
    assert plan["provenance_state"] == "NO_PROVENANCE"


def test_missing_trade_provenance_is_not_confirmed():
    assert binance_demo.derive_provenance_state(
        entry_recorded=True,
        expected_quantity="1",
        observed_quantity="1",
        independent_reconciliation=True,
        provenance_linked_observation=True,
    ) is not binance_demo.ProvenanceState.CONFIRMED


def test_same_symbol_and_side_multiple_plans_are_ambiguous():
    state = {"plans": {
        "a": entry_plan(id="a"),
        "b": entry_plan(id="b"),
    }}
    result = binance_demo.provenance_observer_diagnostic(state)
    assert result["ambiguities"] == [{
        "symbol": "BTCUSDT",
        "position_side": "BOTH",
        "plan_ids": ["a", "b"],
    }]


def test_symbol_only_observation_does_not_confirm():
    plan = entry_plan()
    binance_demo.observe_plan_provenance(
        plan,
        observed_quantity="1",
        trade_ids=[1],
        independent_reconciliation=True,
        provenance_linked_observation=False,
        previous_observation_id="obs-1",
        current_observation_id="obs-2",
    )
    assert plan["provenance_state"] == "PROVISIONAL"


def test_quantity_price_time_matching_does_not_confirm():
    assert binance_demo.derive_provenance_state(
        entry_recorded=True,
        trade_ids=[],
        expected_quantity="1",
        observed_quantity="1",
        independent_reconciliation=True,
        provenance_linked_observation=False,
        previous_observation_id="obs-1",
        current_observation_id="obs-2",
    ) is not binance_demo.ProvenanceState.CONFIRMED


def test_order_id_alone_cannot_confirm():
    assert binance_demo.derive_provenance_state(
        entry_recorded=True,
        expected_quantity="1",
        observed_quantity="1",
        independent_reconciliation=True,
        provenance_linked_observation=True,
        previous_observation_id="obs-1",
        current_observation_id="obs-2",
        last_observation_id="obs-1",
    ) is not binance_demo.ProvenanceState.CONFIRMED


def test_client_order_id_alone_cannot_confirm():
    plan = entry_plan(provenance_trade_ids=[])
    binance_demo.observe_plan_provenance(
        plan,
        observed_quantity="1",
        independent_reconciliation=True,
        provenance_linked_observation=True,
        previous_observation_id="obs-1",
        current_observation_id="obs-2",
    )
    assert plan["provenance_state"] != "CONFIRMED"


def test_side_and_position_side_cannot_confirm():
    assert binance_demo.derive_provenance_state(
        entry_recorded=True,
        trade_ids=[],
        expected_quantity="1",
        observed_quantity="1",
        independent_reconciliation=True,
        provenance_linked_observation=True,
        previous_observation_id="obs-1",
        current_observation_id="obs-2",
        last_observation_id="obs-1",
    ) is not binance_demo.ProvenanceState.CONFIRMED


def test_observer_does_not_invent_exchange_position_identity():
    plan = entry_plan()
    diagnostic = binance_demo.provenance_diagnostic(plan)
    aggregate = binance_demo.provenance_observer_diagnostic(
        {"plans": {plan["id"]: plan}},
        {"positions": [{"symbol": "BTCUSDT", "quantity": 1}]},
    )
    assert "position_id" not in plan
    assert "exchange_position_id" not in plan
    assert "position_identity" not in diagnostic
    assert aggregate["exchange_position_identity_available"] is False


def test_trade_ids_are_append_only():
    plan = entry_plan(provenance_trade_ids=[1])
    binance_demo.observe_plan_provenance(plan, trade_ids=[1, 2])
    binance_demo.observe_plan_provenance(plan, trade_ids=[2, 3])
    assert plan["provenance_trade_ids"] == [1, 2, 3]


def lifecycle_plan(plan_id="plan-1", state="NO_PROVENANCE", position_side="BOTH"):
    return {
        "id": plan_id,
        "symbol": "BTCUSDT",
        "position_side": position_side,
        "provenance_state": state,
        "status": "OPEN",
        "position_status": "OPEN",
        "quantity": "2",
        "initial_quantity": "2",
        "remaining_quantity": "2",
        "tp1_status": "PENDING",
        "tp2_status": "PENDING",
        "tp3_status": "PENDING",
    }


def lifecycle_snapshot(position_side="BOTH", quantity="1"):
    return {"positions": [{
        "symbol": "BTCUSDT",
        "position_side": position_side,
        "quantity": quantity,
    }]}


@pytest.mark.parametrize("provenance_state", ["NO_PROVENANCE", "PROVISIONAL", "BROKEN"])
def test_non_confirmed_same_symbol_position_cannot_mutate_lifecycle(provenance_state):
    plan = lifecycle_plan(state=provenance_state)
    before = plan.copy()
    binance_demo.reconcile_demo_plans({"plans": {plan["id"]: plan}}, lifecycle_snapshot())
    assert plan == before


def test_confirmed_matching_position_preserves_existing_lifecycle_mutation():
    plan = lifecycle_plan(state="CONFIRMED")
    binance_demo.reconcile_demo_plans({"plans": {plan["id"]: plan}}, lifecycle_snapshot(quantity="1"))
    assert plan["remaining_quantity"] == "1"
    assert plan["position_status"] == "OPEN"


@pytest.mark.parametrize("quantities", [("2", "1.4"), ("1.4", "0.8"), ("0.8", "0")])
def test_position_reductions_never_infer_tp_or_stop_execution(quantities):
    plan = lifecycle_plan(state="CONFIRMED")
    plan.update({
        "initial_quantity": "2",
        "quantity": "2",
        "tp1_quantity": "0.6",
        "tp2_quantity": "0.6",
        "tp1_status": "PENDING",
        "tp2_status": "PENDING",
        "tp3_status": "PENDING",
    })
    state = {"plans": {plan["id"]: plan}}

    binance_demo.reconcile_demo_plans(state, lifecycle_snapshot(quantity=quantities[1]))

    assert plan["remaining_quantity"] == quantities[1]
    assert plan["tp1_status"] == "PENDING"
    assert plan["tp2_status"] == "PENDING"
    assert plan["tp3_status"] == "PENDING"
    if quantities[1] == "0":
        assert plan["position_status"] == "CLOSED"
    else:
        assert plan["position_status"] == "OPEN"


def test_restart_reconciliation_and_repeated_snapshot_are_idempotent_without_tp_attribution():
    plan = lifecycle_plan(state="CONFIRMED")
    plan.update({
        "initial_quantity": "2",
        "quantity": "2",
        "tp1_quantity": "0.6",
        "tp2_quantity": "0.6",
        "tp1_status": "PENDING",
        "tp2_status": "PENDING",
        "tp3_status": "PENDING",
    })
    state = {"plans": {plan["id"]: plan}}
    snapshot = lifecycle_snapshot(quantity="0.5")

    binance_demo.reconcile_demo_plans(state, snapshot)
    first_remaining = plan["remaining_quantity"]
    first_position_status = plan["position_status"]
    first_tp_states = (plan["tp1_status"], plan["tp2_status"], plan["tp3_status"])
    binance_demo.reconcile_demo_plans(state, snapshot)

    assert plan["remaining_quantity"] == first_remaining
    assert plan["position_status"] == first_position_status
    assert (plan["tp1_status"], plan["tp2_status"], plan["tp3_status"]) == first_tp_states
    assert plan["remaining_quantity"] == "0.5"
    assert plan["tp1_status"] == "PENDING"
    assert plan["tp2_status"] == "PENDING"


def test_same_symbol_different_position_side_is_not_provenance():
    plan = lifecycle_plan(state="PROVISIONAL", position_side="LONG")
    before = plan.copy()
    binance_demo.reconcile_demo_plans({"plans": {plan["id"]: plan}}, lifecycle_snapshot(position_side="SHORT"))
    assert plan == before


def test_multiple_non_confirmed_same_symbol_plans_cannot_mutate():
    first = lifecycle_plan("first", state="PROVISIONAL")
    second = lifecycle_plan("second", state="BROKEN")
    plans = {first["id"]: first, second["id"]: second}
    before = {key: value.copy() for key, value in plans.items()}
    binance_demo.reconcile_demo_plans({"plans": plans}, lifecycle_snapshot())
    assert plans == before


@pytest.mark.parametrize("provenance_state", [None, "UNKNOWN", " malformed "])
def test_lifecycle_gate_fails_closed_for_missing_or_malformed_state(provenance_state):
    plan = lifecycle_plan(state=provenance_state)
    assert binance_demo.can_mutate_lifecycle(plan) is False


def test_reconciliation_does_not_auto_confirm_provenance():
    plan = lifecycle_plan(state="PROVISIONAL")
    binance_demo.reconcile_demo_plans({"plans": {plan["id"]: plan}}, lifecycle_snapshot())
    assert plan["provenance_state"] == "PROVISIONAL"
