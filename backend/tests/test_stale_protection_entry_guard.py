import copy
import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

BACKEND = Path(__file__).parents[1]
sys.path.insert(0, str(BACKEND))

from fastapi import HTTPException  # noqa: E402

from app import binance_demo  # noqa: E402
from app.binance_demo import DemoOrderRequest, execute_demo_order, stale_protection_entry_reason  # noqa: E402


class StaleProtectionEntryGuardTests(unittest.TestCase):
    def snapshot(self, **overrides):
        snapshot = {
            "positions": [],
            "open_orders": [],
            "open_algo_orders": [],
            "open_algo_orders_available": True,
        }
        snapshot.update(overrides)
        return snapshot

    def plan(self, *, status="OPEN", position_status="OPEN", protection_ids=None, symbol="BTCUSDT", plan_id="plan-a"):
        plan = {
            "id": plan_id,
            "symbol": symbol,
            "status": status,
            "position_status": position_status,
        }
        if protection_ids is not None:
            plan["protection_ids"] = protection_ids
        return plan

    def algo(self, algo_id=101, symbol="BTCUSDT"):
        return {"algo_id": algo_id, "symbol": symbol, "status": "NEW", "type": "STOP_MARKET"}

    def assert_blocked(self, snapshot, plans, text):
        reason = stale_protection_entry_reason(snapshot, {"plans": plans}, "BTCUSDT")
        self.assertIsNotNone(reason)
        self.assertIn(text, reason)

    def test_active_position_blocks(self):
        self.assert_blocked(self.snapshot(positions=[{"symbol": "BTCUSDT"}]), {}, "active position")

    def test_active_internal_plan_blocks(self):
        self.assert_blocked(self.snapshot(), {"plan-a": self.plan()}, "active internal plan")

    def test_normal_open_order_blocks(self):
        self.assert_blocked(self.snapshot(open_orders=[{"symbol": "BTCUSDT", "order_id": 7}]), {}, "normal order")

    def test_known_active_protection_blocks(self):
        snapshot = self.snapshot(open_algo_orders=[self.algo()])
        self.assert_blocked(snapshot, {"plan-a": self.plan(protection_ids=[101])}, "active protection")

    def test_active_protection_from_closed_plan_blocks(self):
        snapshot = self.snapshot(open_algo_orders=[self.algo()])
        plans = {"plan-a": self.plan(status="KAPANDI", position_status="CLOSED", protection_ids=[101])}
        self.assert_blocked(snapshot, plans, "closed plan")

    def test_unknown_active_protection_blocks(self):
        self.assert_blocked(self.snapshot(open_algo_orders=[self.algo()]), {}, "active protection")

    def test_missing_protection_ids_blocks(self):
        snapshot = self.snapshot(open_algo_orders=[self.algo()])
        self.assert_blocked(snapshot, {"plan-a": self.plan()}, "protection")

    def test_duplicate_protection_id_blocks(self):
        snapshot = self.snapshot(open_algo_orders=[self.algo()])
        plans = {
            "plan-a": self.plan(protection_ids=[101], plan_id="plan-a"),
            "plan-b": self.plan(protection_ids=[101], plan_id="plan-b"),
        }
        self.assert_blocked(snapshot, plans, "ambiguous")

    def test_unavailable_algo_snapshot_blocks(self):
        self.assert_blocked(
            self.snapshot(open_algo_orders_available=False),
            {},
            "snapshot unavailable",
        )

    def test_clean_snapshot_allows_entry(self):
        self.assertIsNone(stale_protection_entry_reason(self.snapshot(), {"plans": {}}, "BTCUSDT"))

    def test_guard_is_read_only(self):
        snapshot = self.snapshot(open_algo_orders=[self.algo()])
        state = {"plans": {"plan-a": self.plan(protection_ids=[101])}}
        original_snapshot = copy.deepcopy(snapshot)
        original_state = copy.deepcopy(state)

        stale_protection_entry_reason(snapshot, state, "BTCUSDT")

        self.assertEqual(snapshot, original_snapshot)
        self.assertEqual(state, original_state)

    def test_existing_open_position_is_not_modified(self):
        snapshot = self.snapshot(positions=[{"symbol": "BTCUSDT", "quantity": 1}])
        state = {"plans": {}}
        stale_protection_entry_reason(snapshot, state, "BTCUSDT")
        self.assertEqual(snapshot["positions"][0]["quantity"], 1)
        self.assertEqual(state, {"plans": {}})

    def test_second_snapshot_conflict_returns_409_before_entry_submission(self):
        application = SimpleNamespace(state=SimpleNamespace(http=object(), v21_demo={}))
        demo_state = {
            "lock": asyncio.Lock(),
            "_user_id": "user-a",
            "plans": {},
            "events": [],
            "last_error": None,
        }
        v21_state = {"_user_id": "user-a", "settings": {}, "paper_positions": []}
        body = DemoOrderRequest(
            symbol="BTCUSDT",
            direction="LONG",
            order_type="MARKET",
            margin_usdt=50,
            leverage=2,
            stop_loss=95,
            tp1=105,
            tp2=110,
            tp3=115,
        )
        clean = self.snapshot()
        conflicting = self.snapshot(open_algo_orders=[self.algo(909)])
        client = SimpleNamespace(trace_request_id=None)
        spec = {
            "symbol": "BTCUSDT",
            "direction": "LONG",
            "order_type": "MARKET",
            "quantity": "1",
            "entry_price": "100",
            "stop_loss": "95",
            "targets": ["105", "110", "115"],
            "leverage": 2,
            "margin_usdt": 50,
            "notional_usdt": 100,
            "risk_per_trade": 1,
            "risk_adjusted": False,
        }

        async def next_snapshot(*_args, **_kwargs):
            result = clean if next_snapshot.calls == 0 else conflicting
            next_snapshot.calls += 1
            return result

        next_snapshot.calls = 0

        submit_entry = AsyncMock()
        with patch.object(binance_demo, "client_for_state", return_value=client), \
                patch.object(binance_demo, "ensure_one_way_position_mode", new=AsyncMock(return_value=0)), \
                patch.object(binance_demo, "account_snapshot", new=next_snapshot), \
                patch.object(binance_demo, "resolve_demo_symbol", new=AsyncMock(return_value="BTCUSDT")), \
                patch.object(binance_demo, "build_order_spec", new=AsyncMock(return_value=spec)), \
                patch.object(binance_demo, "adjust_manual_spec_to_risk", return_value=spec), \
                patch.object(binance_demo, "validate_entry_risk"), \
                patch.object(binance_demo, "set_isolated_margin", new=AsyncMock(return_value={})), \
                patch.object(binance_demo, "apply_verified_leverage", new=AsyncMock(return_value={
                    "requested_leverage": 2,
                    "applied_leverage": 2,
                    "margin_type": "ISOLATED",
                    "leverage_verified": True,
                    "configuration_source": "TEST",
                    "max_notional_value": 100,
                })), \
                patch.object(binance_demo, "submit_entry", new=submit_entry), \
                patch.object(binance_demo, "persist_runtime"):
            with self.assertRaises(HTTPException) as error:
                asyncio.run(execute_demo_order(
                    application,
                    body,
                    source="MANUAL",
                    demo_state=demo_state,
                    v21_state=v21_state,
                ))

        self.assertEqual(error.exception.status_code, 409)
        self.assertIn("active protection/algo order", error.exception.detail)
        self.assertEqual(next_snapshot.calls, 2)
        submit_entry.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()