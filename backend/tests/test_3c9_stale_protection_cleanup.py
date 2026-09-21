import asyncio
import sys
import unittest
from pathlib import Path

BACKEND = Path(__file__).parents[1]
sys.path.insert(0, str(BACKEND))

from app import binance_demo  # noqa: E402


class StaleProtectionCleanupTests(unittest.IsolatedAsyncioTestCase):
    def plan(self, protection_ids=(101, 102), *, provenance="PROVISIONAL"):
        return {
            "id": "plan-doge",
            "user_id": "user-a",
            "symbol": "DOGEUSDT",
            "direction": "LONG",
            "position_side": "BOTH",
            "provenance_state": provenance,
            "status": "OPEN",
            "position_status": "OPEN",
            "protection_ids": list(protection_ids),
            "stop_algo_id": 101,
            "cleanup_pending_ids": [],
            "tp1_status": "PENDING",
            "tp2_status": "PENDING",
            "tp3_status": "PENDING",
        }

    def snapshot(self, orders):
        return {"open_algo_orders_available": True, "open_algo_orders": orders}

    def order(self, algo_id, order_type="TAKE_PROFIT_MARKET", side="SELL", status="NEW"):
        return {
            "symbol": "DOGEUSDT",
            "algo_id": algo_id,
            "type": order_type,
            "side": side,
            "status": status,
        }

    async def test_zero_position_cleanup_closes_provisional_plan_without_filling_targets(self):
        plan = self.plan()
        deleted = set()

        class Client:
            async def signed(self, method, path, params=None):
                if method == "DELETE":
                    deleted.add(params["algoId"])
                    return {}
                return [self_order for self_order in [
                    self_order_for(101, "STOP_MARKET"),
                    self_order_for(102),
                    ] if self_order["algoId"] not in deleted]

        def self_order_for(algo_id, order_type="TAKE_PROFIT_MARKET"):
            return {"symbol": "DOGEUSDT", "algoId": algo_id, "orderType": order_type, "side": "SELL", "algoStatus": "NEW"}

        state = {"plans": {plan["id"]: plan}}
        changed = await binance_demo._cleanup_reconciled_closed_plans(
            Client(), state, {"positions": []}
        )

        self.assertTrue(changed)
        self.assertEqual(plan["status"], "KAPANDI")
        self.assertEqual(plan["position_status"], "CLOSED")
        self.assertEqual(plan["protection_ids"], [])
        self.assertEqual(plan["tp1_status"], "PENDING")
        self.assertEqual(plan["tp2_status"], "PENDING")
        self.assertEqual(plan["tp3_status"], "PENDING")

    async def test_foreign_and_unknown_records_are_never_deleted(self):
        plan = self.plan((101, 102))
        calls = []
        client = type("Client", (), {
            "signed": lambda _self, method, path, params=None: self._signed(calls, method, path, params)
        })()

        async def signed(method, path, params=None):
            calls.append((method, params))
            return {}

        client.signed = signed
        await binance_demo.cleanup_closed_plan(
            client,
            plan,
            snapshot=self.snapshot([
                self.order(101, "STOP_MARKET"),
                self.order(102, "LIMIT"),
                {"symbol": "DOGEUSDT", "algo_id": 999, "type": "TAKE_PROFIT_MARKET", "side": "SELL", "status": "NEW"},
            ]),
            allow_closed_position_cleanup=True,
            plans=[plan],
        )

        self.assertEqual([call[1]["algoId"] for call in calls if call[0] == "DELETE"], [101])
        self.assertEqual(plan["protection_ids"], [102])

    async def test_delete_or_fresh_verification_failure_preserves_ownership(self):
        plan = self.plan((101,))
        calls = []

        async def signed(_self, method, path, params=None):
            calls.append(method)
            if method == "DELETE":
                raise binance_demo.BinanceDemoError("temporary delete failure")
            return [self.order(101, "STOP_MARKET")]

        client = type("Client", (), {"signed": signed})()
        cleaned = await binance_demo.cleanup_closed_plan(
            client,
            plan,
            snapshot=self.snapshot([self.order(101, "STOP_MARKET")]),
            allow_closed_position_cleanup=True,
            plans=[plan],
        )

        self.assertFalse(cleaned)
        self.assertEqual(plan["protection_ids"], [101])
        self.assertEqual(calls, ["DELETE", "GET"])

    async def test_duplicate_cleanup_deletes_each_owned_id_once(self):
        plan = self.plan((101, 102))
        deleted = set()
        calls = []

        async def signed(_self, method, path, params=None):
            if method == "DELETE":
                calls.append(params["algoId"])
                deleted.add(params["algoId"])
                return {}
            return [self.order(101, "STOP_MARKET"), self.order(102)] if not deleted else [
                order for order in [self.order(101, "STOP_MARKET"), self.order(102)]
                if order["algo_id"] not in deleted
            ]

        client = type("Client", (), {"signed": signed})()
        await asyncio.gather(
            binance_demo.cleanup_closed_plan(client, plan, allow_closed_position_cleanup=True, plans=[plan], snapshot=self.snapshot([
                self.order(101, "STOP_MARKET"), self.order(102)
            ])),
            binance_demo.cleanup_closed_plan(client, plan, allow_closed_position_cleanup=True, plans=[plan], snapshot=self.snapshot([
                self.order(101, "STOP_MARKET"), self.order(102)
            ])),
        )

        self.assertEqual(calls, [101, 102])
        self.assertEqual(plan["protection_ids"], [])


if __name__ == "__main__":
    unittest.main()
