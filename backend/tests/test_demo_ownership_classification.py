import copy
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).parents[1]
sys.path.insert(0, str(BACKEND))

from app import binance_demo  # noqa: E402


def make_plan(
    *,
    plan_id="plan-1",
    symbol="BTCUSDT",
    direction="LONG",
    position_id="position-1",
    protection_ids=None,
):
    plan = {
        "id": plan_id,
        "position_id": position_id,
        "symbol": symbol,
        "direction": direction,
        "status": "OPEN",
        "position_status": "OPEN",
    }
    if protection_ids is not None:
        plan["protection_ids"] = protection_ids
    return plan


def make_position(
    *,
    symbol="BTCUSDT",
    direction="LONG",
    position_id=None,
    quantity="1",
    position_side="BOTH",
):
    return {
        "symbol": symbol,
        "direction": direction,
        "position_id": position_id,
        "quantity": quantity,
        "position_side": position_side,
    }


def classify(plan=None, positions=None, *, algo_orders=None, algo_available=True, trades=None):
    return binance_demo.classify_demo_ownership(
        {
            "positions": positions or [],
            "open_algo_orders": algo_orders or [],
            "open_algo_orders_available": algo_available,
        },
        {"plans": {plan["id"]: plan} if plan else {}},
        {"automation_trades": trades or []},
    )


def position_classification(result):
    return result["position_plan"][0]["classification"]


def automation_classification(result):
    return result["automation"][0]["classification"]


def test_exchange_position_without_plan_is_exchange_only():
    result = classify(positions=[make_position(position_id="exchange-1")])

    assert position_classification(result) == "EXCHANGE_ONLY"


def test_active_plan_without_exchange_position_is_plan_only():
    result = classify(plan=make_plan(position_id="position-1"))

    assert position_classification(result) == "PLAN_ONLY"


def test_explicit_position_identity_and_protection_ids_are_matched():
    result = classify(
        plan=make_plan(position_id="exchange-1", protection_ids=[101]),
        positions=[make_position(position_id="exchange-1")],
        algo_orders=[{"algo_id": 101, "symbol": "BTCUSDT", "status": "NEW"}],
    )

    row = result["position_plan"][0]
    assert row["classification"] == "PLAN_AND_EXCHANGE_MATCHED"
    assert row["protection_status"] == "MATCHED"
    assert row["matched_algo_ids"] == [101]


def test_same_symbol_but_different_identity_is_not_matched():
    result = classify(
        plan=make_plan(position_id="plan-position"),
        positions=[make_position(position_id="exchange-position")],
    )

    assert position_classification(result) == "PROTECTION_UNKNOWN"


@pytest.mark.parametrize(
    "plan,position",
    [
        (make_plan(position_id=None), make_position(position_id=None, direction="LONG")),
        (make_plan(position_id=None), make_position(position_id=None, direction="SHORT", quantity="1")),
        (make_plan(position_id=None), make_position(position_id=None, direction="LONG", quantity="2")),
    ],
)
def test_symbol_side_or_quantity_alone_is_not_matched(plan, position):
    result = classify(plan=plan, positions=[position])

    assert position_classification(result) != "PLAN_AND_EXCHANGE_MATCHED"


def test_ptb_prefix_alone_is_not_ownership():
    result = classify(
        plan=make_plan(position_id=None, protection_ids=[101]),
        positions=[make_position(position_id=None)],
        algo_orders=[
            {
                "algo_id": 101,
                "client_algo_id": "PTB_TP1_ownership_not_proof",
                "symbol": "BTCUSDT",
                "status": "NEW",
            }
        ],
    )

    assert position_classification(result) != "PLAN_AND_EXCHANGE_MATCHED"


def test_explicit_identity_with_empty_protection_ids_is_protection_unknown():
    result = classify(
        plan=make_plan(position_id="exchange-1", protection_ids=[]),
        positions=[make_position(position_id="exchange-1")],
    )

    assert position_classification(result) == "PROTECTION_UNKNOWN"


def test_missing_protection_ids_is_protection_unknown():
    result = classify(
        plan=make_plan(position_id="exchange-1"),
        positions=[make_position(position_id="exchange-1")],
    )

    assert position_classification(result) == "PROTECTION_UNKNOWN"


def test_unknown_algo_id_is_protection_unknown():
    result = classify(
        plan=make_plan(position_id="exchange-1", protection_ids=[101]),
        positions=[make_position(position_id="exchange-1")],
        algo_orders=[{"algo_id": 999, "symbol": "BTCUSDT", "status": "NEW"}],
    )

    assert position_classification(result) == "PROTECTION_UNKNOWN"


def test_unavailable_algo_snapshot_is_protection_unknown():
    result = classify(
        plan=make_plan(position_id="exchange-1", protection_ids=[101]),
        positions=[make_position(position_id="exchange-1")],
        algo_available=False,
    )

    assert position_classification(result) == "PROTECTION_UNKNOWN"


def test_active_automation_without_plan_or_position_is_stale():
    result = classify(
        trades=[{"plan_id": "missing-plan", "symbol": "BTCUSDT", "status": "OPEN"}]
    )

    assert automation_classification(result) == "STALE_AUTOMATION_RECORD"


def test_null_plan_id_automation_is_stale_without_plan_or_position():
    result = classify(
        trades=[{"plan_id": None, "symbol": "BTCUSDT", "status": "OPEN"}]
    )

    assert automation_classification(result) == "STALE_AUTOMATION_RECORD"


def test_missing_plan_with_exchange_position_is_not_stale_automation():
    result = classify(
        positions=[make_position(position_id="exchange-1")],
        trades=[{"plan_id": "missing-plan", "symbol": "BTCUSDT", "status": "OPEN"}],
    )

    assert position_classification(result) == "EXCHANGE_ONLY"
    assert automation_classification(result) == "ASSOCIATED_AUTOMATION_RECORD"


def test_closed_automation_trade_is_not_active_stale():
    result = classify(
        trades=[{"plan_id": "missing-plan", "symbol": "BTCUSDT", "status": "CLOSED"}]
    )

    assert result["automation"] == []


@pytest.mark.parametrize("status", ["CANCELLED", "CANCELED", "EXPIRED", "FINISHED", "REJECTED"])
def test_alternate_terminal_automation_status_is_not_stale(status):
    result = classify(
        trades=[{"plan_id": "missing-plan", "symbol": "BTCUSDT", "status": status}]
    )

    assert result["automation"] == []


def test_classifier_does_not_mutate_inputs():
    snapshot = {
        "positions": [make_position(position_id="exchange-1")],
        "open_algo_orders": [{"algo_id": 101, "symbol": "BTCUSDT", "status": "NEW"}],
        "open_algo_orders_available": True,
    }
    demo_state = {"plans": {"plan-1": make_plan(position_id="exchange-1", protection_ids=[101])}}
    v21_state = {"automation_trades": [{"plan_id": "plan-1", "symbol": "BTCUSDT", "status": "OPEN"}]}
    before = copy.deepcopy((snapshot, demo_state, v21_state))

    binance_demo.classify_demo_ownership(snapshot, demo_state, v21_state)

    assert (snapshot, demo_state, v21_state) == before


def test_classifier_does_not_call_network_or_persistence(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("forbidden side effect")

    monkeypatch.setattr(binance_demo, "account_snapshot", forbidden)
    monkeypatch.setattr(binance_demo, "persist_runtime", forbidden)

    result = classify(plan=make_plan(position_id="exchange-1"), positions=[make_position(position_id="exchange-1")])

    assert position_classification(result) == "PROTECTION_UNKNOWN"


def test_classifier_output_is_deterministic():
    plan = make_plan(position_id="exchange-1", protection_ids=[101])
    positions = [make_position(position_id="exchange-1")]
    algos = [{"algo_id": 101, "symbol": "BTCUSDT", "status": "NEW"}]
    trades = [{"plan_id": "plan-1", "symbol": "BTCUSDT", "status": "OPEN"}]

    first = classify(plan, positions, algo_orders=algos, trades=trades)
    second = classify(plan, positions, algo_orders=algos, trades=trades)

    assert first == second
