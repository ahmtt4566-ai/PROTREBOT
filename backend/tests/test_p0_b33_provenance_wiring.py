import asyncio
from unittest.mock import patch

from app import binance_demo, v21_demo


def entry_plan(plan_id="plan-1", *, state="PROVISIONAL", client_id="PTB_ENTRY_1", order_id=101):
    return {
        "id": plan_id,
        "symbol": "BTCUSDT",
        "position_side": "BOTH",
        "provenance_state": state,
        "provenance_entry_client_order_id": client_id,
        "provenance_entry_order_id": order_id,
        "provenance_trade_ids": [],
        "provenance_expected_quantity": "1",
        "provenance_last_observation_id": None,
        "provenance_broken_reason": None,
        "status": "DOLUM BEKLİYOR",
    }


def trade_event(*, trade_id="501", client_id="PTB_ENTRY_1", order_id=101, last="1", cumulative="1", event_time=1):
    return {
        "e": "ORDER_TRADE_UPDATE",
        "T": event_time,
        "o": {
            "s": "BTCUSDT",
            "i": order_id,
            "c": client_id,
            "t": trade_id,
            "l": last,
            "z": cumulative,
            "x": "TRADE",
            "X": "FILLED",
            "ps": "BOTH",
        },
    }


def stream_state(plan):
    state = v21_demo.initial_state()
    demo_state = {
        "plans": {plan["id"]: plan},
        "_provenance_reconciliation_cycle": 1,
    }
    return state, demo_state


def completed_entry_state(plan):
    state, demo_state = stream_state(plan)
    state["plans"] = demo_state["plans"]
    state["lock"] = asyncio.Lock()
    with patch.object(v21_demo, "persist_runtime"):
        v21_demo.process_stream_event(v21_demo.initial_state(), trade_event(), demo_state)
    return state


def test_exact_entry_identity_records_trade_and_fill_evidence():
    plan = entry_plan()
    state, demo_state = stream_state(plan)

    with patch.object(v21_demo, "persist_runtime"):
        assert v21_demo.process_stream_event(
            state, trade_event(last="1", cumulative="1"), demo_state
        )

    assert plan["provenance_trade_ids"] == ["501"]
    assert plan["provenance_last_fill_quantity"] == "1"
    assert plan["provenance_cumulative_fill_quantity"] == "1"
    assert plan["provenance_state"] == "PROVISIONAL"
    assert plan["provenance_last_observation_id"] == "entry-fill:101:501"


def test_mismatched_client_or_conflicting_order_is_ignored():
    plan = entry_plan()
    state, demo_state = stream_state(plan)

    with patch.object(v21_demo, "persist_runtime"):
        v21_demo.process_stream_event(
            state,
            trade_event(client_id="PTB_ENTRY_OTHER", trade_id="502"),
            demo_state,
        )
        v21_demo.process_stream_event(
            state,
            trade_event(order_id=999, trade_id="503"),
            demo_state,
        )

    assert plan["provenance_trade_ids"] == []
    assert plan.get("provenance_cumulative_fill_quantity") is None


def test_symbol_or_position_side_mismatch_is_ignored():
    plan = entry_plan()
    state, demo_state = stream_state(plan)

    symbol_mismatch = trade_event(trade_id="504", event_time=2)
    symbol_mismatch["o"]["s"] = "XRPUSDT"
    side_mismatch = trade_event(trade_id="505", event_time=3)
    side_mismatch["o"]["ps"] = "LONG"
    with patch.object(v21_demo, "persist_runtime"):
        v21_demo.process_stream_event(state, symbol_mismatch, demo_state)
        v21_demo.process_stream_event(state, side_mismatch, demo_state)

    assert plan["provenance_trade_ids"] == []


def test_ambiguous_active_entry_plans_fail_closed():
    first = entry_plan("first")
    second = entry_plan("second")
    demo_state = {"plans": {first["id"]: first, second["id"]: second}, "_provenance_reconciliation_cycle": 1}

    with patch.object(v21_demo, "persist_runtime"):
        v21_demo.process_stream_event(v21_demo.initial_state(), trade_event(trade_id="506"), demo_state)

    assert first["provenance_trade_ids"] == []
    assert second["provenance_trade_ids"] == []


def test_late_entry_trade_binds_after_confirmation():
    plan = entry_plan(state="CONFIRMED")
    state, demo_state = stream_state(plan)

    with patch.object(v21_demo, "persist_runtime"):
        v21_demo.process_stream_event(state, trade_event(trade_id="507", event_time=4), demo_state)

    assert plan["provenance_trade_ids"] == ["507"]


def test_duplicate_trade_id_does_not_double_count_and_z_is_not_summed():
    plan = entry_plan()
    state, demo_state = stream_state(plan)

    with patch.object(v21_demo, "persist_runtime"):
        v21_demo.process_stream_event(
            state, trade_event(trade_id="501", last="0.4", cumulative="0.4"), demo_state
        )
        v21_demo.process_stream_event(
            state, trade_event(trade_id="501", last="0.4", cumulative="0.4", event_time=2), demo_state
        )
        v21_demo.process_stream_event(
            state, trade_event(trade_id="502", last="0.6", cumulative="1", event_time=3), demo_state
        )

    assert plan["provenance_trade_ids"] == ["501", "502"]
    assert plan["provenance_last_fill_quantity"] == "0.6"
    assert plan["provenance_cumulative_fill_quantity"] == "1"
    assert plan["provenance_state"] == "PROVISIONAL"


def test_missing_trade_or_quantity_cannot_create_fill_evidence():
    plan = entry_plan()
    state, demo_state = stream_state(plan)
    event = trade_event(trade_id=None, last="1", cumulative="1")

    with patch.object(v21_demo, "persist_runtime"):
        v21_demo.process_stream_event(state, event, demo_state)

    assert plan["provenance_trade_ids"] == []
    assert plan["provenance_last_observation_id"] is None


def test_same_cycle_cannot_confirm_but_later_matching_cycle_can():
    plan = entry_plan()
    state, demo_state = stream_state(plan)
    state["plans"] = demo_state["plans"]
    state["lock"] = asyncio.Lock()
    with patch.object(v21_demo, "persist_runtime"), patch.object(binance_demo, "persist_runtime"):
        v21_demo.process_stream_event(
            v21_demo.initial_state(), trade_event(), demo_state
        )
        demo_state["plans"] = {plan["id"]: plan}
        asyncio.run(
            binance_demo.confirm_provenance_from_snapshot(
                state,
                {"positions": [{"symbol": "BTCUSDT", "position_side": "BOTH", "quantity": 1}]},
                1,
            )
        )
        assert plan["provenance_state"] == "PROVISIONAL"
        asyncio.run(
            binance_demo.confirm_provenance_from_snapshot(
                state,
                {"positions": [{"symbol": "BTCUSDT", "position_side": "BOTH", "quantity": 1}]},
                2,
            )
        )

    assert plan["provenance_state"] == "CONFIRMED"


def test_confirmation_uses_exact_decimal_position_quantity():
    plan = entry_plan()
    plan["provenance_expected_quantity"] = "0.123456789123456789"
    state, demo_state = stream_state(plan)
    state["plans"] = demo_state["plans"]
    state["lock"] = asyncio.Lock()
    with patch.object(v21_demo, "persist_runtime"), patch.object(binance_demo, "persist_runtime"):
        v21_demo.process_stream_event(
            v21_demo.initial_state(),
            trade_event(last="0.123456789123456789", cumulative="0.123456789123456789"),
            demo_state,
        )
        asyncio.run(
            binance_demo.confirm_provenance_from_snapshot(
                state,
                {
                    "positions": [{"symbol": "BTCUSDT", "position_side": "BOTH", "quantity": 0.123456789}],
                    "_provenance_positions": [{
                        "symbol": "BTCUSDT",
                        "position_side": "BOTH",
                        "quantity": "0.123456789123456789",
                    }],
                },
                2,
            )
        )

    assert plan["provenance_state"] == "CONFIRMED"


def test_confirmation_avoids_float_rounding_edge_case():
    plan = entry_plan()
    plan["provenance_expected_quantity"] = "0.10000000000000001"
    state, demo_state = stream_state(plan)
    state["plans"] = demo_state["plans"]
    state["lock"] = asyncio.Lock()
    with patch.object(v21_demo, "persist_runtime"), patch.object(binance_demo, "persist_runtime"):
        v21_demo.process_stream_event(
            v21_demo.initial_state(),
            trade_event(last="0.10000000000000001", cumulative="0.10000000000000001"),
            demo_state,
        )
        asyncio.run(
            binance_demo.confirm_provenance_from_snapshot(
                state,
                {
                    "positions": [{"symbol": "BTCUSDT", "position_side": "BOTH", "quantity": 0.1}],
                    "_provenance_positions": [{
                        "symbol": "BTCUSDT",
                        "position_side": "BOTH",
                        "quantity": "0.10000000000000001",
                    }],
                },
                2,
            )
        )

    assert plan["provenance_state"] == "CONFIRMED"


def test_zero_matching_provenance_rows_cannot_confirm():
    plan = entry_plan()
    state = completed_entry_state(plan)

    with patch.object(binance_demo, "persist_runtime"):
        asyncio.run(
            binance_demo.confirm_provenance_from_snapshot(
                state,
                {"_provenance_positions": []},
                2,
            )
        )

    assert plan["provenance_state"] == "PROVISIONAL"


def test_exactly_one_matching_provenance_row_confirms():
    plan = entry_plan()
    state = completed_entry_state(plan)

    with patch.object(binance_demo, "persist_runtime"):
        asyncio.run(
            binance_demo.confirm_provenance_from_snapshot(
                state,
                {"_provenance_positions": [{
                    "symbol": "BTCUSDT",
                    "position_side": "BOTH",
                    "quantity": "1",
                }]},
                2,
            )
        )

    assert plan["provenance_state"] == "CONFIRMED"


def test_duplicate_matching_provenance_rows_cannot_confirm():
    plan = entry_plan()
    state = completed_entry_state(plan)
    rows = [
        {"symbol": "BTCUSDT", "position_side": "BOTH", "quantity": "1"},
        {"symbol": "BTCUSDT", "position_side": "BOTH", "quantity": "1"},
    ]

    with patch.object(binance_demo, "persist_runtime"):
        asyncio.run(binance_demo.confirm_provenance_from_snapshot(
            state, {"_provenance_positions": rows}, 2
        ))

    assert plan["provenance_state"] == "PROVISIONAL"


def test_duplicate_matching_rows_cannot_select_expected_quantity():
    plan = entry_plan()
    state = completed_entry_state(plan)
    rows = [
        {"symbol": "BTCUSDT", "position_side": "BOTH", "quantity": "1"},
        {"symbol": "BTCUSDT", "position_side": "BOTH", "quantity": "0.5"},
    ]

    with patch.object(binance_demo, "persist_runtime"):
        asyncio.run(binance_demo.confirm_provenance_from_snapshot(
            state, {"_provenance_positions": rows}, 2
        ))

    assert plan["provenance_state"] == "PROVISIONAL"


def test_duplicate_different_symbol_rows_do_not_block_matching_symbol():
    plan = entry_plan()
    state = completed_entry_state(plan)
    rows = [
        {"symbol": "ETHUSDT", "position_side": "BOTH", "quantity": "1"},
        {"symbol": "ETHUSDT", "position_side": "BOTH", "quantity": "2"},
        {"symbol": "BTCUSDT", "position_side": "BOTH", "quantity": "1"},
    ]

    with patch.object(binance_demo, "persist_runtime"):
        asyncio.run(binance_demo.confirm_provenance_from_snapshot(
            state, {"_provenance_positions": rows}, 2
        ))

    assert plan["provenance_state"] == "CONFIRMED"


def test_partial_decimal_fill_remains_provisional():
    plan = entry_plan()
    plan["provenance_expected_quantity"] = "0.123456789123456789"
    state, demo_state = stream_state(plan)

    with patch.object(v21_demo, "persist_runtime"):
        v21_demo.process_stream_event(
            state,
            trade_event(
                last="0.123456789123456788",
                cumulative="0.123456789123456788",
            ),
            demo_state,
        )

    assert plan["provenance_state"] == "PROVISIONAL"


def test_later_cycle_quantity_mismatch_marks_broken():
    plan = entry_plan()
    state, demo_state = stream_state(plan)
    state["plans"] = demo_state["plans"]
    state["lock"] = asyncio.Lock()
    with patch.object(v21_demo, "persist_runtime"), patch.object(binance_demo, "persist_runtime"):
        v21_demo.process_stream_event(v21_demo.initial_state(), trade_event(), demo_state)
        asyncio.run(
            binance_demo.confirm_provenance_from_snapshot(
                state,
                {"positions": [{"symbol": "BTCUSDT", "position_side": "BOTH", "quantity": "0.5"}]},
                2,
            )
        )

    assert plan["provenance_state"] == "BROKEN"
    assert plan["provenance_broken_reason"]


def test_existing_confirmed_slot_blocks_candidate_confirmation():
    existing = entry_plan("existing", state="CONFIRMED")
    candidate = entry_plan("candidate", client_id="PTB_ENTRY_2", order_id=102)
    state = {
        "plans": {existing["id"]: existing, candidate["id"]: candidate},
        "lock": asyncio.Lock(),
    }
    demo_state = {"plans": {candidate["id"]: candidate}, "_provenance_reconciliation_cycle": 1}

    with patch.object(v21_demo, "persist_runtime"), patch.object(binance_demo, "persist_runtime"):
        v21_demo.process_stream_event(
            v21_demo.initial_state(),
            trade_event(client_id="PTB_ENTRY_2", order_id=102, trade_id="601"),
            demo_state,
        )
        asyncio.run(
            binance_demo.confirm_provenance_from_snapshot(
                state,
                {"positions": [{"symbol": "BTCUSDT", "position_side": "BOTH", "quantity": 1}]},
                2,
            )
        )

    assert existing["provenance_state"] == "CONFIRMED"
    assert candidate["provenance_state"] == "PROVISIONAL"
