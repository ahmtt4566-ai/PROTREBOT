import asyncio
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

BACKEND = Path(__file__).parents[1]
sys.path.insert(0, str(BACKEND))

from app import binance_demo, v21_demo  # noqa: E402


class UserStateReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.application = SimpleNamespace(
            state=SimpleNamespace(
                http=AsyncMock(),
                binance_demo={"plans": {}},
                v21_demo={"settings": dict(v21_demo.DEFAULT_SETTINGS)},
                _binance_demo_user_state={},
                _v21_demo_user_state={},
            )
        )

    @staticmethod
    def plan(user_id="user-a", symbol="BRUSDT", stop_loss="90", entry_price="100"):
        return {
            "id": f"plan-{user_id}",
            "user_id": user_id,
            "symbol": symbol,
            "direction": "LONG",
            "status": "OPEN",
            "position_status": "OPEN",
            "entry_price": entry_price,
            "initial_stop_loss": stop_loss,
            "stop_loss": stop_loss,
            "protection_ids": [101],
            "stop_algo_id": 101,
            "tp1": "110",
            "tp2": "120",
            "tp3": "130",
            "last_dynamic_update_epoch": 0,
        }

    def user_demo_state(self, user_id, session_id, plan):
        return {
            "_user_id": user_id,
            "_session_id": session_id,
            "_app": self.application,
            "plans": {plan["id"]: plan},
        }

    def user_v21_state(self, user_id):
        state = v21_demo.initial_state()
        state.update({"_user_id": user_id, "_app": self.application})
        return state

    def test_user_specific_plan_is_found_by_scoped_reconciliation(self):
        plan = self.plan()
        demo_state = self.user_demo_state("user-a", "session-a", plan)
        self.application.state._binance_demo_user_state["user-a"] = demo_state
        self.application.state._v21_demo_user_state["user-a"] = self.user_v21_state("user-a")

        contexts = v21_demo._background_contexts(self.application)
        self.assertTrue(any(user_id == "user-a" and state is demo_state for user_id, state, _ in contexts))
        found = v21_demo.active_plan(self.application, "BRUSDT", demo_state)

        self.assertIs(found, plan)
        self.assertEqual(found["user_id"], "user-a")

    def test_recently_protected_plan_survives_temporary_empty_snapshot(self):
        plan = self.plan()
        plan["protected_at"] = datetime.now(timezone.utc).isoformat()
        state = {"plans": {plan["id"]: plan}}

        result = binance_demo.reconcile_demo_plans(state, {"positions": []})

        self.assertEqual(result["internal_active_plans"], 1)
        self.assertEqual(plan["status"], "OPEN")
        self.assertEqual(plan["position_status"], "OPEN")

    def test_old_protected_plan_is_closed_by_empty_snapshot(self):
        plan = self.plan()
        plan["protected_at"] = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        state = {"plans": {plan["id"]: plan}}

        result = binance_demo.reconcile_demo_plans(state, {"positions": []})

        self.assertEqual(result["internal_active_plans"], 0)
        self.assertEqual(plan["status"], "KAPANDI")
        self.assertEqual(plan["position_status"], "CLOSED")
        self.assertEqual(plan["remaining_quantity"], "0")

    async def _run_protection_loop_once(self, plan, position_rows):
        state = self.user_demo_state("user-a", "session-a", plan)
        self.application.state._binance_demo_user_state["user-a"] = state
        client = SimpleNamespace(signed=AsyncMock(return_value=position_rows))
        sleep_calls = 0

        async def one_cycle(_delay):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls > 1:
                raise asyncio.CancelledError

        with patch.object(binance_demo, "client_for_state", return_value=client), \
                patch.object(binance_demo, "persist_runtime"):
            with patch.object(binance_demo.asyncio, "sleep", side_effect=one_cycle):
                with self.assertRaises(asyncio.CancelledError):
                    await binance_demo.protection_loop(self.application)

    def test_protection_loop_graces_recently_protected_empty_snapshot(self):
        plan = self.plan()
        plan["protected_at"] = datetime.now(timezone.utc).isoformat()

        asyncio.run(self._run_protection_loop_once(plan, []))

        self.assertEqual(plan["status"], "OPEN")
        self.assertEqual(plan["position_status"], "OPEN")

    def test_protection_loop_closes_expired_protected_empty_snapshot(self):
        plan = self.plan()
        plan["protected_at"] = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()

        asyncio.run(self._run_protection_loop_once(plan, []))

        self.assertEqual(plan["status"], "KAPANDI")
        self.assertEqual(plan["position_status"], "CLOSED")

    def test_protection_loop_closes_open_plan_without_protected_at(self):
        plan = self.plan()

        asyncio.run(self._run_protection_loop_once(plan, []))

        self.assertEqual(plan["status"], "KAPANDI")
        self.assertEqual(plan["position_status"], "CLOSED")

    def test_protection_loop_keeps_plan_open_when_exchange_position_exists(self):
        plan = self.plan()

        asyncio.run(self._run_protection_loop_once(plan, [{"symbol": "BRUSDT", "positionAmt": "1"}]))

        self.assertEqual(plan["status"], "OPEN")
        self.assertEqual(plan["position_status"], "OPEN")

    def test_background_client_uses_matching_user_credentials_and_no_cross_user_fallback(self):
        state_a = self.user_demo_state("user-a", "session-a", self.plan("user-a"))
        state_b = self.user_demo_state("user-b", "session-b", self.plan("user-b", symbol="KASUSDT"))
        from app import exchange_connections

        with patch.dict(exchange_connections._SESSION_CACHE, {
            ("session-a", "TESTNET"): ("api-a-1234567890", "secret-a-1234567890"),
            ("session-b", "TESTNET"): ("api-b-1234567890", "secret-b-1234567890"),
        }, clear=True), patch.dict(exchange_connections._SESSION_META, {
            ("session-a", "TESTNET"): {"active": True, "user_id": "user-a"},
            ("session-b", "TESTNET"): {"active": True, "user_id": "user-b"},
        }, clear=True):
            client_a = binance_demo.client_for_state(self.application, state_a)
            client_b = binance_demo.client_for_state(self.application, state_b)

        self.assertEqual(client_a.api_key, "api-a-1234567890")
        self.assertEqual(client_b.api_key, "api-b-1234567890")
        self.assertNotEqual(client_a.api_key, client_b.api_key)

    def test_request_and_demo_state_mismatch_rejects_before_binance(self):
        demo_b = self.user_demo_state("user-b", "session-b", self.plan("user-b"))
        v21_a = self.user_v21_state("user-a")
        request_a = SimpleNamespace(app=self.application, state=SimpleNamespace(member={"id": "user-a"}))
        body = SimpleNamespace(symbol="BRUSDT", direction="LONG")

        with patch.object(binance_demo, "client_for") as request_client, patch.object(binance_demo, "client_for_state") as state_client:
            with self.assertRaises(binance_demo.BinanceDemoError):
                asyncio.run(binance_demo.execute_demo_order(
                    self.application,
                    body,
                    source="AUTO_SCANNER",
                    request=request_a,
                    demo_state=demo_b,
                    v21_state=v21_a,
                ))

        request_client.assert_not_called()
        state_client.assert_not_called()
        self.assertEqual(demo_b["plans"], {"plan-user-b": demo_b["plans"]["plan-user-b"]})
        self.assertEqual(v21_a["automation_trades"], [])

    def test_demo_and_v21_state_mismatch_rejects_before_execution(self):
        demo_a = self.user_demo_state("user-a", "session-a", self.plan("user-a"))
        v21_b = self.user_v21_state("user-b")
        body = SimpleNamespace(symbol="BRUSDT", direction="LONG")

        with patch.object(binance_demo, "client_for_state") as state_client:
            with self.assertRaises(binance_demo.BinanceDemoError):
                asyncio.run(binance_demo.execute_demo_order(
                    self.application,
                    body,
                    source="AUTO_SCANNER",
                    demo_state=demo_a,
                    v21_state=v21_b,
                ))

        state_client.assert_not_called()
        self.assertEqual(v21_b["automation_trades"], [])

    def test_session_credential_ownership_mismatch_fails_closed(self):
        state = self.user_demo_state("user-a", "session-a", self.plan("user-a"))
        from app import exchange_connections

        with patch.dict(exchange_connections._SESSION_CACHE, {
            ("session-a", "TESTNET"): ("api-b-1234567890", "secret-b-1234567890"),
        }, clear=True), patch.dict(exchange_connections._SESSION_META, {
            ("session-a", "TESTNET"): {"active": True, "user_id": "user-b"},
        }, clear=True), patch.object(binance_demo, "BinanceDemoClient") as client_type:
            with self.assertRaises(binance_demo.BinanceDemoError):
                binance_demo.client_for_state(self.application, state)

        client_type.assert_not_called()

    def test_user_specific_global_states_are_not_emitted_as_global_context(self):
        global_demo = {"_user_id": "user-a", "plans": {}}
        global_v21 = v21_demo.initial_state()
        global_v21["_user_id"] = "user-a"
        user_demo = self.user_demo_state("user-a", "session-a", self.plan("user-a"))
        user_v21 = self.user_v21_state("user-a")
        self.application.state.binance_demo = global_demo
        self.application.state.v21_demo = global_v21
        self.application.state._binance_demo_user_state["user-a"] = user_demo
        self.application.state._v21_demo_user_state["user-a"] = user_v21

        contexts = v21_demo._background_contexts(self.application)

        self.assertEqual([user_id for user_id, _, _ in contexts], ["user-a"])
        self.assertIs(contexts[0][1], user_demo)
        self.assertIs(contexts[0][2], user_v21)

    def test_true_global_context_remains_available(self):
        self.application.state.binance_demo = {"plans": {}}
        self.application.state.v21_demo = v21_demo.initial_state()

        contexts = v21_demo._background_contexts(self.application)

        self.assertEqual(len(contexts), 1)
        self.assertEqual(contexts[0][0], "")
        self.assertIs(contexts[0][1], self.application.state.binance_demo)
        self.assertIs(contexts[0][2], self.application.state.v21_demo)

    def test_automation_loop_dispatches_each_context_pair_once(self):
        self.application.state.v21_demo = v21_demo.initial_state()
        demo_a = self.user_demo_state("user-a", "session-a", self.plan("user-a"))
        demo_b = self.user_demo_state("user-b", "session-b", self.plan("user-b", symbol="KASUSDT"))
        v21_a = self.user_v21_state("user-a")
        v21_b = self.user_v21_state("user-b")
        for state in (v21_a, v21_b):
            state["auto"].update({"enabled": True, "user_confirmed": True})
        self.application.state._binance_demo_user_state.update({"user-a": demo_a, "user-b": demo_b})
        self.application.state._v21_demo_user_state.update({"user-a": v21_a, "user-b": v21_b})
        calls = []

        async def capture(*args, **kwargs):
            calls.append((kwargs["user_id"], kwargs["demo_state"], kwargs["v21_state"]))

        async def stop_loop(_delay):
            raise asyncio.CancelledError

        with patch.object(v21_demo, "automatic_cycle", new=capture), patch.object(v21_demo.asyncio, "sleep", side_effect=stop_loop):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(v21_demo.automation_loop(self.application))

        self.assertEqual({user_id for user_id, _, _ in calls}, {"user-a", "user-b"})
        self.assertEqual({id(demo) for _, demo, _ in calls}, {id(demo_a), id(demo_b)})
        self.assertEqual({id(state) for _, _, state in calls}, {id(v21_a), id(v21_b)})

    def test_failed_user_context_does_not_abort_other_contexts(self):
        self.application.state.v21_demo = v21_demo.initial_state()
        demo_a = self.user_demo_state("user-a", "session-a", self.plan("user-a"))
        demo_b = self.user_demo_state("user-b", "session-b", self.plan("user-b", symbol="KASUSDT"))
        v21_a = self.user_v21_state("user-a")
        v21_b = self.user_v21_state("user-b")
        for state in (v21_a, v21_b):
            state["auto"].update({"enabled": True, "user_confirmed": True})
        self.application.state._binance_demo_user_state.update({"user-a": demo_a, "user-b": demo_b})
        self.application.state._v21_demo_user_state.update({"user-a": v21_a, "user-b": v21_b})
        calls = []

        async def capture(*args, **kwargs):
            calls.append(kwargs["user_id"])
            if kwargs["user_id"] == "user-a":
                raise binance_demo.BinanceDemoError("missing user session")

        async def stop_loop(_delay):
            raise asyncio.CancelledError

        with patch.object(v21_demo, "automatic_cycle", new=capture), patch.object(v21_demo.asyncio, "sleep", side_effect=stop_loop):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(v21_demo.automation_loop(self.application))

        self.assertEqual(set(calls), {"user-a", "user-b"})
        self.assertIn("missing user session", v21_a["auto"]["last_error"])

    def test_user_context_cycle_does_not_use_global_client_or_state(self):
        demo_state = self.user_demo_state("user-a", "missing-session", self.plan("user-a"))
        v21_state = self.user_v21_state("user-a")
        v21_state["auto"].update({"enabled": True, "user_confirmed": True})
        demo_state["armed_until"] = time.time() + 60
        self.application.state.binance_demo["armed_until"] = time.time() + 60
        with patch.object(v21_demo, "client_for_state", side_effect=binance_demo.BinanceDemoError("missing user session")) as client_mock, \
                patch.object(v21_demo, "client_for") as global_client_mock, \
                patch.object(v21_demo, "execute_demo_order", new=AsyncMock()) as order_mock:
            with self.assertRaises(binance_demo.BinanceDemoError):
                asyncio.run(v21_demo.automatic_cycle(
                    self.application,
                    user_id="user-a",
                    demo_state=demo_state,
                    v21_state=v21_state,
                ))

        client_mock.assert_called_once_with(self.application, demo_state)
        global_client_mock.assert_not_called()
        order_mock.assert_not_awaited()
        self.assertEqual(self.application.state.binance_demo.get("plans"), {})
        self.assertEqual(self.application.state.v21_demo.get("automation_trades", []), [])

    def test_automatic_cycle_passes_same_context_to_execution(self):
        demo_state = self.user_demo_state("user-a", "session-a", self.plan("user-a"))
        v21_state = self.user_v21_state("user-a")
        v21_state["auto"].update({"enabled": True, "user_confirmed": True})
        candidate = {
            "symbol": "BRUSDT", "direction": "LONG", "status": "SELECTED", "entry": 100,
            "stop_loss": 99, "tp1": 101, "tp2": 102, "tp3": 103, "score": 95,
            "opportunity_score": 95, "confidence": "HIGH", "reasons": [],
        }
        result = {"plan": {"id": "plan-user-a", "entry_price": 100, "targets": [101, 102, 103], "stop_loss": 99, "margin_usdt": 5, "leverage": 2, "status": "OPEN"}}
        self.application.state._binance_demo_user_state["user-a"] = demo_state
        self.application.state._v21_demo_user_state["user-a"] = v21_state
        with patch.object(v21_demo, "armed", return_value=True), \
                patch.object(v21_demo, "client_for_state", return_value=object()), \
                patch.object(v21_demo, "account_snapshot", new=AsyncMock(return_value={"positions": [], "open_orders": []})), \
                patch.object(v21_demo, "scan_demo_universe", new=AsyncMock(return_value=[candidate])), \
                patch.object(v21_demo, "execute_demo_order", new=AsyncMock(return_value=result)) as order_mock, \
                patch.object(v21_demo, "persist_state"):
            asyncio.run(v21_demo.automatic_cycle(
                self.application,
                user_id="user-a",
                demo_state=demo_state,
                v21_state=v21_state,
            ))

        self.assertIs(order_mock.await_args.kwargs["demo_state"], demo_state)
        self.assertIs(order_mock.await_args.kwargs["v21_state"], v21_state)
        self.assertEqual(v21_state["automation_trades"][0]["plan_id"], result["plan"]["id"])

    def test_background_contexts_deduplicate_string_equivalent_user_ids(self):
        demo_state = self.user_demo_state("1", "session-1", self.plan("1"))
        v21_state = self.user_v21_state("1")
        self.application.state._binance_demo_user_state = {1: demo_state, "1": demo_state}
        self.application.state._v21_demo_user_state = {1: v21_state, "1": v21_state}

        contexts = [context for context in v21_demo._background_contexts(self.application) if context[0] == "1"]

        self.assertEqual(len(contexts), 1)
        self.assertIs(contexts[0][1], demo_state)
        self.assertIs(contexts[0][2], v21_state)

    def test_missing_user_credentials_fail_closed_without_global_fallback(self):
        state = self.user_demo_state("user-a", "missing-session", self.plan("user-a"))
        from app import exchange_connections

        with patch.dict(exchange_connections._SESSION_CACHE, {}, clear=True), \
                patch.dict(exchange_connections._SESSION_META, {}, clear=True), \
                patch.object(binance_demo, "load_demo_credentials", return_value=("global-api", "global-secret")) as global_credentials:
            with self.assertRaises(binance_demo.BinanceDemoError):
                binance_demo.client_for_state(self.application, state)

        global_credentials.assert_not_called()

    def test_user_a_plan_is_not_visible_in_user_b_state(self):
        plan_a = self.plan("user-a")
        plan_b = self.plan("user-b", symbol="KASUSDT")
        state_a = self.user_demo_state("user-a", "session-a", plan_a)
        state_b = self.user_demo_state("user-b", "session-b", plan_b)

        self.assertIs(v21_demo.active_plan(self.application, "BRUSDT", state_a), plan_a)
        self.assertIsNone(v21_demo.active_plan(self.application, "BRUSDT", state_b))
        self.assertIsNone(v21_demo.active_plan(self.application, "KASUSDT", state_a))

    def test_r_below_one_performs_zero_order_mutation(self):
        plan = self.plan(stop_loss="90", entry_price="100")
        demo_state = self.user_demo_state("user-a", "session-a", plan)
        v21_state = self.user_v21_state("user-a")
        client = SimpleNamespace(signed=AsyncMock(), public_get=AsyncMock())
        snapshot = {"positions": [{
            "symbol": "BRUSDT", "direction": "LONG", "entry_price": "100", "mark_price": "105",
        }]}

        with patch.object(v21_demo, "symbol_rules", new=AsyncMock(return_value={"tick": 1})):
            changed = asyncio.run(v21_demo.improve_dynamic_stops(
                self.application, snapshot, demo_state=demo_state, v21_state=v21_state, client=client,
            ))

        self.assertFalse(changed)
        client.signed.assert_not_awaited()

    def test_r_at_or_above_one_can_trigger_existing_break_even_mechanism(self):
        plan = self.plan(stop_loss="90", entry_price="100")
        demo_state = self.user_demo_state("user-a", "session-a", plan)
        v21_state = self.user_v21_state("user-a")
        client = SimpleNamespace(signed=AsyncMock(), public_get=AsyncMock())
        snapshot = {"positions": [{
            "symbol": "BRUSDT", "direction": "LONG", "entry_price": "100", "mark_price": "110",
        }]}

        with patch.object(v21_demo, "symbol_rules", new=AsyncMock(return_value={"tick": 1})) as rules_mock, \
            patch.object(v21_demo, "post_algo", new=AsyncMock(return_value={"algoId": 202})) as post_mock, \
            patch.object(v21_demo, "persist_runtime") as persist_mock:
            changed = asyncio.run(v21_demo.improve_dynamic_stops(
                self.application, snapshot, demo_state=demo_state, v21_state=v21_state, client=client,
            ))

        self.assertTrue(changed)
        rules_mock.assert_awaited_once()
        post_mock.assert_awaited_once()
        self.assertEqual(post_mock.await_args.args[1]["triggerPrice"], "100")
        client.signed.assert_awaited_once()
        self.assertEqual(client.signed.await_args.args[0], "DELETE")
        persist_mock.assert_called_once_with(demo_state)

    def test_user_specific_ensure_stop_protection_persists_only_demo_state(self):
        plan = self.plan("user-a")
        plan["protection_ids"] = []
        plan["stop_algo_id"] = None
        demo_state = self.user_demo_state("user-a", "session-a", plan)
        v21_state = self.user_v21_state("user-a")
        client = SimpleNamespace(signed=AsyncMock(), public_get=AsyncMock())
        snapshot = {
            "positions": [{"symbol": "BRUSDT", "direction": "LONG", "entry_price": "100", "mark_price": "105"}],
            "open_algo_orders": [],
        }

        with patch.object(v21_demo, "post_algo", new=AsyncMock(return_value={"algoId": 303})), \
                patch.object(v21_demo, "persist_runtime") as persist_mock:
            changed = asyncio.run(v21_demo.ensure_stop_protection(
                self.application, snapshot, demo_state=demo_state, v21_state=v21_state, client=client,
            ))

        self.assertTrue(changed)
        persist_mock.assert_called_once_with(demo_state)
        self.assertIsNot(persist_mock.call_args.args[0], self.application.state.binance_demo)

    def test_missing_protective_stop_repair_is_separate_from_dynamic_stop(self):
        plan = self.plan("user-a")
        plan["protection_ids"] = []
        plan["stop_algo_id"] = None
        demo_state = self.user_demo_state("user-a", "session-a", plan)
        v21_state = self.user_v21_state("user-a")
        client = SimpleNamespace(signed=AsyncMock(), public_get=AsyncMock())
        snapshot = {
            "positions": [{"symbol": "BRUSDT", "direction": "LONG", "entry_price": "100", "mark_price": "105"}],
            "open_algo_orders": [],
        }

        with patch.object(v21_demo, "post_algo", new=AsyncMock(return_value={"algoId": 404})) as repair_mock, \
                patch.object(v21_demo, "persist_runtime"):
            changed = asyncio.run(v21_demo.ensure_stop_protection(
                self.application, snapshot, demo_state=demo_state, v21_state=v21_state, client=client,
            ))

        self.assertTrue(changed)
        repair_mock.assert_awaited_once()
        self.assertEqual(repair_mock.await_args.args[1]["type"], "STOP_MARKET")
        self.assertNotIn("DYNAMICSL", repair_mock.await_args.args[1]["clientAlgoId"])

    def test_existing_protective_stop_is_not_cancelled_or_recreated(self):
        plan = self.plan(stop_loss="90", entry_price="100")
        demo_state = self.user_demo_state("user-a", "session-a", plan)
        v21_state = self.user_v21_state("user-a")
        client = SimpleNamespace(signed=AsyncMock(), public_get=AsyncMock())
        snapshot = {
            "positions": [{"symbol": "BRUSDT", "direction": "LONG", "entry_price": "100", "mark_price": "105"}],
            "open_algo_orders": [{"symbol": "BRUSDT", "type": "STOP_MARKET", "algoId": 101}],
        }

        with patch.object(v21_demo, "post_algo", new=AsyncMock()) as post_mock:
            changed = asyncio.run(v21_demo.ensure_stop_protection(
                self.application, snapshot, demo_state=demo_state, v21_state=v21_state, client=client,
            ))

        self.assertFalse(changed)
        post_mock.assert_not_awaited()
        client.signed.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
