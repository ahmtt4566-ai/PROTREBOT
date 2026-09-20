import copy
import asyncio
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


def diagnostic(plan=None, positions=None, *, algo_orders=None, algo_available=True, trades=None):
    return binance_demo.build_demo_ownership_diagnostic(
        {
            "positions": positions or [],
            "open_algo_orders": algo_orders or [],
            "open_algo_orders_available": algo_available,
        },
        {"plans": {plan["id"]: plan} if plan else {}},
        {"automation_trades": trades or []},
    )


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


@pytest.mark.parametrize("protection_id", [101.0, 101.5, "101.5", True, False, None, ""])
def test_malformed_protection_id_is_protection_unknown(protection_id):
    result = classify(
        plan=make_plan(position_id="exchange-1", protection_ids=[protection_id]),
        positions=[make_position(position_id="exchange-1")],
        algo_orders=[{"algo_id": 101, "symbol": "BTCUSDT", "status": "NEW"}],
    )

    assert position_classification(result) == "PROTECTION_UNKNOWN"


def test_valid_integer_and_string_protection_ids_remain_matched():
    for protection_id in (101, "101"):
        result = classify(
            plan=make_plan(position_id="exchange-1", protection_ids=[protection_id]),
            positions=[make_position(position_id="exchange-1")],
            algo_orders=[{"algo_id": 101, "symbol": "BTCUSDT", "status": "NEW"}],
        )

        assert position_classification(result) == "PLAN_AND_EXCHANGE_MATCHED"


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


def test_authenticated_production_shape_classifies_only_stale_active_automation():
    symbols = [
        "EPICUSDT", "SAGAUSDT", "IOSTUSDT", "AVAAIUSDT", "NAORISUSDT",
        "ENAUSDT", "INJUSDT", "KASUSDT", "BRUSDT",
    ]
    algo_orders = [
        {
            "symbol": symbol,
            "algo_id": 1000000212000000 + index,
            "client_algo_id": f"PTB_TP{index % 2 + 1}_synthetic{index}",
            "type": "TAKE_PROFIT_MARKET",
            "status": "NEW",
        }
        for index, symbol in enumerate(
            ["EPICUSDT", "EPICUSDT", "SAGAUSDT", "IOSTUSDT", "IOSTUSDT", "AVAAIUSDT",
             "NAORISUSDT", "NAORISUSDT", "ENAUSDT", "INJUSDT", "INJUSDT", "KASUSDT",
             "KASUSDT", "BRUSDT", "BRUSDT"]
        )
    ]
    trades = [
        {"plan_id": f"plan-{index}", "symbol": symbol, "status": "OPEN"}
        for index, symbol in enumerate(symbols[:4])
    ] + [
        {"plan_id": "plan-4", "symbol": "NAORISUSDT", "status": "GÜVENLİK İÇİN KAPATILDI"},
        {"plan_id": "plan-5", "symbol": "ENAUSDT", "status": "GÜVENLİK İÇİN KAPATILDI"},
    ] + [
        {"plan_id": None, "symbol": symbol, "status": "OPEN"}
        for symbol in symbols[6:]
    ]
    snapshot = {
        "positions": [],
        "open_algo_orders": algo_orders,
        "open_algo_orders_available": True,
    }
    demo_state = {"plans": {}}
    v21_state = {"automation_trades": trades}

    result = binance_demo.classify_demo_ownership(snapshot, demo_state, v21_state)

    assert len(snapshot["open_algo_orders"]) == 15
    assert result["position_plan"] == []
    assert all(
        row["classification"] not in {
            "PLAN_AND_EXCHANGE_MATCHED", "PLAN_ONLY", "EXCHANGE_ONLY",
        }
        for row in result["position_plan"]
    )
    assert [row["symbol"] for row in result["automation"]] == symbols[:4] + symbols[6:]
    assert len(result["automation"]) == 7
    assert all(row["classification"] == "STALE_AUTOMATION_RECORD" for row in result["automation"])
    assert "NAORISUSDT" not in {row["symbol"] for row in result["automation"]}
    assert "ENAUSDT" not in {row["symbol"] for row in result["automation"]}


def test_diagnostic_has_separate_sections_and_zero_counts():
    result = diagnostic()

    assert set(result) == {"position_plan", "automation", "protection_evidence", "aggregate_counts", "evidence"}
    assert result["position_plan"]["rows"] == []
    assert result["automation"]["rows"] == []
    assert result["aggregate_counts"] == {
        "exchange_positions": 0,
        "active_demo_plans": 0,
        "open_algo_orders": 0,
        "automation_trades": 0,
    }


def test_diagnostic_preserves_position_classifications_and_identity_evidence():
    result = diagnostic(
        plan=make_plan(position_id="exchange-1", protection_ids=[101]),
        positions=[make_position(position_id="exchange-1")],
        algo_orders=[{"algo_id": 101, "symbol": "BTCUSDT", "status": "NEW"}],
    )

    row = result["position_plan"]["rows"][0]
    assert row["classification"] == "PLAN_AND_EXCHANGE_MATCHED"
    assert row["identity"] == {
        "plan_identity": "exchange-1",
        "exchange_identity": "exchange-1",
        "matched": True,
        "evidence": "EXPLICIT_EQUAL",
    }
    assert row["protection"]["status"] == "MATCHED"
    assert row["protection"]["plan_protection_ids"] == [101]
    assert result["evidence"]["identity_fields_available"] is True


@pytest.mark.parametrize(
    "plan,positions,expected",
    [
        (None, [make_position(position_id="exchange-1")], "EXCHANGE_ONLY"),
        (make_plan(position_id="position-1"), [], "PLAN_ONLY"),
        (make_plan(position_id="plan-position"), [make_position(position_id="exchange-position")], "PROTECTION_UNKNOWN"),
        (make_plan(position_id=None), [make_position(position_id=None)], "PROTECTION_UNKNOWN"),
        (make_plan(position_id=None), [make_position(position_id=None, direction="SHORT", quantity="1")], "PROTECTION_UNKNOWN"),
    ],
)
def test_diagnostic_does_not_infer_identity(plan, positions, expected):
    result = diagnostic(plan=plan, positions=positions)

    assert result["position_plan"]["rows"][0]["classification"] == expected
    if expected == "PROTECTION_UNKNOWN":
        assert result["position_plan"]["rows"][0]["identity"]["evidence"] in {"MISSING", "MISMATCH"}


@pytest.mark.parametrize("protection_ids", [None, [], ["bad-id"]])
def test_diagnostic_marks_missing_or_malformed_protection_unknown(protection_ids):
    result = diagnostic(
        plan=make_plan(position_id="exchange-1", protection_ids=protection_ids) if protection_ids is not None else make_plan(position_id="exchange-1"),
        positions=[make_position(position_id="exchange-1")],
    )

    row = result["position_plan"]["rows"][0]
    assert row["classification"] == "PROTECTION_UNKNOWN"
    assert row["protection"]["status"] == "UNKNOWN"


def test_diagnostic_marks_unknown_algo_and_unavailable_snapshot():
    unknown_algo = diagnostic(
        plan=make_plan(position_id="exchange-1", protection_ids=[101]),
        positions=[make_position(position_id="exchange-1")],
        algo_orders=[{"algo_id": 999, "symbol": "BTCUSDT", "status": "NEW"}],
    )
    unavailable = diagnostic(
        plan=make_plan(position_id="exchange-1", protection_ids=[101]),
        positions=[make_position(position_id="exchange-1")],
        algo_available=False,
    )

    assert unknown_algo["protection_evidence"]["unassigned_algo_ids"] == [999]
    assert unknown_algo["protection_evidence"]["unknown"] is True
    assert unavailable["evidence"]["protection_snapshot_available"] is False
    assert unavailable["protection_evidence"]["unknown"] is True


def test_diagnostic_keeps_seven_stale_and_two_terminal_automation_records_separate():
    trades = [
        {"plan_id": None, "symbol": f"COIN{index}USDT", "status": "OPEN"}
        for index in range(7)
    ] + [
        {"plan_id": "closed-plan", "symbol": "BTCUSDT", "status": "GÜVENLİK İÇİN KAPATILDI"},
        {"plan_id": "closed-plan-2", "symbol": "ETHUSDT", "status": "CLOSED"},
    ]

    result = diagnostic(trades=trades)

    assert result["automation"]["counts"] == {"active": 7, "terminal": 2, "stale": 7, "associated": 0}
    assert all(row["classification"] == "STALE_AUTOMATION_RECORD" for row in result["automation"]["rows"][:7])
    assert all(row["terminal"] for row in result["automation"]["rows"][7:])


@pytest.mark.parametrize("status", ["CLOSED", "KAPANDI", "İPTAL", "CANCELLED", "CANCELED", "EXPIRED", "FINISHED", "REJECTED", "GÜVENLİK İÇİN KAPATILDI"])
def test_diagnostic_terminal_status_is_never_stale(status):
    result = diagnostic(trades=[{"plan_id": "missing", "symbol": "BTCUSDT", "status": status}])

    assert result["automation"]["rows"][0]["terminal"] is True
    assert result["automation"]["rows"][0]["classification"] == "TERMINAL_AUTOMATION_RECORD"


def test_diagnostic_is_deterministic_and_does_not_mutate_or_call_side_effects(monkeypatch):
    snapshot = {
        "positions": [make_position(position_id="exchange-1")],
        "open_algo_orders": [{"algo_id": 101, "client_algo_id": "PTB_TP1", "symbol": "BTCUSDT", "status": "NEW"}],
        "open_algo_orders_available": True,
    }
    demo_state = {"plans": {"plan-1": make_plan(position_id="exchange-1", protection_ids=[101])}}
    v21_state = {"automation_trades": [{"plan_id": "missing", "symbol": "BTCUSDT", "status": "OPEN"}]}
    before = copy.deepcopy((snapshot, demo_state, v21_state))

    def forbidden(*args, **kwargs):
        raise AssertionError("forbidden side effect")

    monkeypatch.setattr(binance_demo, "account_snapshot", forbidden)
    monkeypatch.setattr(binance_demo, "persist_runtime", forbidden)
    monkeypatch.setattr(binance_demo, "reconcile_demo_plans", forbidden)
    first = binance_demo.build_demo_ownership_diagnostic(snapshot, demo_state, v21_state)
    second = binance_demo.build_demo_ownership_diagnostic(snapshot, demo_state, v21_state)

    assert first == second
    assert (snapshot, demo_state, v21_state) == before


def make_request(*, user_id=None, state_user_id=None, query_string=b""):
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/binance-demo/ownership-diagnostic",
        "query_string": query_string,
        "headers": [],
        "state": {},
    }
    request = Request(scope)
    if user_id is not None:
        request.state.member = {"id": user_id}
    if state_user_id is not None:
        request.state.user = {"id": state_user_id}
    return request


def test_diagnostic_endpoint_requires_authenticated_member(monkeypatch):
    with pytest.raises(binance_demo.HTTPException) as error:
        asyncio.run(binance_demo.demo_ownership_diagnostic(make_request()))

    assert error.value.status_code == 401


def test_diagnostic_endpoint_allows_authenticated_member_without_user_override(monkeypatch):
    request = make_request(user_id="user-1", query_string=b"user_id=user-2")
    demo_state = {"_user_id": "user-1", "plans": {}}
    v21_state = {"_user_id": "user-1", "automation_trades": []}
    seen_member_ids = []

    def demo_state_for(request):
        seen_member_ids.append(request.state.member["id"])
        return demo_state

    monkeypatch.setattr(binance_demo, "state_for", demo_state_for)
    monkeypatch.setattr(binance_demo, "client_for", lambda request: object())
    monkeypatch.setattr(binance_demo, "account_snapshot", lambda client, request_id: _async_snapshot())

    import app.v21_demo as v21_demo

    monkeypatch.setattr(v21_demo, "state_for", lambda request: v21_state)
    result = asyncio.run(binance_demo.demo_ownership_diagnostic(request))

    assert result["aggregate_counts"]["exchange_positions"] == 0
    assert result["aggregate_counts"]["automation_trades"] == 0
    assert seen_member_ids == ["user-1"]


async def _async_snapshot():
    return {"positions": [], "open_algo_orders": [], "open_algo_orders_available": True}


def test_diagnostic_endpoint_rejects_user_context_without_member_before_any_resolution(monkeypatch):
    request = make_request(state_user_id="user-2")

    def forbidden(*args, **kwargs):
        raise AssertionError("memberless request reached a protected resolver")

    monkeypatch.setattr(binance_demo, "state_for", forbidden)
    monkeypatch.setattr(binance_demo, "client_for", forbidden)
    monkeypatch.setattr(binance_demo, "account_snapshot", forbidden)
    monkeypatch.setattr(binance_demo, "load_demo_credentials", forbidden)

    import app.v21_demo as v21_demo

    monkeypatch.setattr(v21_demo, "state_for", forbidden)
    with pytest.raises(binance_demo.HTTPException) as error:
        asyncio.run(binance_demo.demo_ownership_diagnostic(request))

    assert error.value.status_code == 401


def test_diagnostic_endpoint_rejects_cross_user_state_and_ignores_user_id_override(monkeypatch):
    request = make_request(user_id="user-1", query_string=b"user_id=user-2")
    monkeypatch.setattr(binance_demo, "state_for", lambda request: {"_user_id": "user-2", "plans": {}})

    with pytest.raises(binance_demo.HTTPException) as error:
        asyncio.run(binance_demo.demo_ownership_diagnostic(request))

    assert error.value.status_code == 403
