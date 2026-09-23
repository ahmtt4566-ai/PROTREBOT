import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

BACKEND = Path(__file__).parents[1]
sys.path.insert(0, str(BACKEND))

from app import binance_demo, main, v21_demo  # noqa: E402
from app.exchange_connections import session_id  # noqa: E402


class AuthenticatedHydrationTests(unittest.TestCase):
    def setUp(self):
        self.plan = {
            "id": "plan-user-a",
            "user_id": "user-a",
            "symbol": "BRUSDT",
            "direction": "LONG",
            "entry_price": "100",
            "initial_stop_loss": "90",
            "stop_loss": "90",
            "targets": ["110", "120", "130"],
            "risk_per_trade": "10",
            "status": "OPEN",
            "position_status": "OPEN",
        }
        self.demo_payload = {
            "user_id": "user-a",
            "plans": {self.plan["id"]: self.plan},
            "events": [],
            "connected": True,
            "armed_until": 0,
            "reconciliation": {},
        }
        self.v21_payload = {"settings": dict(v21_demo.DEFAULT_SETTINGS)}
        self.pool = SimpleNamespace(fetchrow=AsyncMock(side_effect=self.fetchrow))
        self.application = SimpleNamespace(
            state=SimpleNamespace(
                db_pool=self.pool,
                _binance_demo_user_state={},
                _v21_demo_user_state={},
                binance_demo={"plans": {}},
                v21_demo=v21_demo.initial_state(),
            )
        )
        self.request = SimpleNamespace(
            app=self.application,
            state=SimpleNamespace(member={"id": "user-a"}),
            headers={"authorization": "Bearer current-session"},
        )

    async def fetchrow(self, _query, key):
        if key == "binance_demo:user:user-a":
            return {"payload": self.demo_payload}
        if key == "v21_demo:user:user-a":
            return {"payload": self.v21_payload}
        return None

    def test_demo_payload_round_trip_excludes_runtime_credentials(self):
        state = {
            "_user_id": "user-a",
            "_session_id": "session-hash",
            "api_key": "must-not-persist",
            "secret_key": "must-not-persist",
            **self.demo_payload,
        }

        payload = binance_demo.runtime_payload(state)
        encoded = json.dumps(payload)

        self.assertEqual(payload["user_id"], "user-a")
        self.assertEqual(payload["plans"][self.plan["id"]]["symbol"], "BRUSDT")
        self.assertNotIn("_session_id", encoded)
        self.assertNotIn("must-not-persist", encoded)

        restored = asyncio.run(binance_demo.restore_demo_state_for_user(self.application, "user-a"))
        self.assertEqual(restored["_user_id"], "user-a")
        self.assertNotIn("_session_id", self.demo_payload)
        self.assertEqual(restored["plans"][self.plan["id"]]["initial_stop_loss"], "90")

    def test_valid_empty_postgres_snapshot_restores_as_empty_state(self):
        self.pool.fetchrow = AsyncMock(return_value={"payload": {**self.demo_payload, "plans": {}}})

        restored = asyncio.run(binance_demo.restore_demo_state_for_user(self.application, "user-a"))

        self.assertEqual(restored["plans"], {})
        self.assertEqual(restored["_restore_origin"], "postgres")
        self.assertEqual(restored["_restore_status"], "restored")
        self.assertFalse(restored["_persistence_blocked"])
        self.assertEqual(restored["_restore_diagnostic"]["plan_count"], 0)

    def test_confirmed_missing_postgres_snapshot_initializes_empty_state(self):
        self.pool.fetchrow = AsyncMock(return_value=None)

        restored = asyncio.run(binance_demo.restore_demo_state_for_user(self.application, "user-a"))

        self.assertEqual(restored["plans"], {})
        self.assertEqual(restored["_restore_origin"], "postgres")
        self.assertEqual(restored["_restore_status"], "no_snapshot")
        self.assertFalse(restored["_persistence_blocked"])
        self.assertEqual(restored["_restore_diagnostic"]["persistence_action"], "initialized_empty")

    def test_restore_exception_marks_restore_failed(self):
        self.pool.fetchrow = AsyncMock(side_effect=RuntimeError("database unavailable"))

        restored = asyncio.run(binance_demo.restore_demo_state_for_user(self.application, "user-a"))

        self.assertEqual(restored["plans"], {})
        self.assertEqual(restored["_restore_status"], "restore_failed")
        self.assertTrue(restored["_persistence_blocked"])
        self.assertEqual(restored["_restore_diagnostic"]["persistence_action"], "persistence_suppressed")

    def test_restore_failed_suppresses_empty_state_persistence(self):
        self.pool.fetchrow = AsyncMock(side_effect=RuntimeError("database unavailable"))
        restored = asyncio.run(binance_demo.restore_demo_state_for_user(self.application, "user-a"))

        with tempfile.TemporaryDirectory() as data_dir, patch.object(
            binance_demo, "STATE_PATH", Path(data_dir) / "binance_demo_runtime.json"
        ):
            binance_demo.persist_runtime(restored)
            self.assertFalse(binance_demo.STATE_PATH.exists())

        self.pool.execute = AsyncMock()
        self.assertTrue(restored["_persistence_blocked"])
        self.pool.execute.assert_not_called()

    def test_runtime_persistence_is_tracked_until_db_write_completes(self):
        self.pool.execute = AsyncMock()
        state = {
            "_user_id": "user-a",
            "_app": self.application,
            "plans": {self.plan["id"]: self.plan},
            "events": [],
        }

        async def persist_and_wait():
            binance_demo.persist_runtime(state)
            tasks = self.application.state._binance_demo_persistence_tasks["user-a"]
            await asyncio.gather(*tasks)

        asyncio.run(persist_and_wait())

        self.pool.execute.assert_awaited_once()
        self.assertEqual(self.pool.execute.await_args.args[1], "binance_demo:user:user-a")

    def test_authenticated_request_hydrates_once_and_binds_current_session(self):
        current_session = session_id(self.request)
        with patch.object(main, "ensure_session_cache", new=AsyncMock()), \
                patch.object(main, "session_credentials_for_request", return_value=("api-key", "secret-key")):
            asyncio.run(main.hydrate_authenticated_user_state(self.request))
            demo_state = self.application.state._binance_demo_user_state["user-a"]
            v21_state = self.application.state._v21_demo_user_state["user-a"]
            plan = demo_state["plans"][self.plan["id"]]

            asyncio.run(main.hydrate_authenticated_user_state(self.request))

        self.assertIs(self.application.state._binance_demo_user_state["user-a"], demo_state)
        self.assertIs(demo_state["plans"][self.plan["id"]], plan)
        self.assertEqual(demo_state["_user_id"], "user-a")
        self.assertEqual(demo_state["_session_id"], current_session)
        self.assertEqual(v21_state["_user_id"], "user-a")
        self.assertEqual(v21_state["_session_id"], current_session)
        self.assertEqual(self.pool.fetchrow.await_count, 2)

    def test_file_fallback_returns_full_state_and_preserves_plans(self):
        with tempfile.TemporaryDirectory() as data_dir, patch.object(
            binance_demo, "STATE_PATH", Path(data_dir) / "binance_demo_runtime.json"
        ):
            binance_demo.STATE_PATH.write_text(json.dumps({
                "users": {
                    "user-a": {
                        "connected": True,
                        "armed_until": 123,
                        "last_checked": "2026-09-20T00:00:00+00:00",
                        "last_error": None,
                        "events": [],
                        "plans": {self.plan["id"]: self.plan},
                        "reconciliation": {"internal_active_plans": 1},
                    }
                }
            }), encoding="utf-8")
            application = SimpleNamespace(state=SimpleNamespace(
                db_pool=None,
                binance_demo={"plans": {}},
                _binance_demo_user_state={},
            ))
            request = SimpleNamespace(
                app=application,
                state=SimpleNamespace(member={"id": "user-a"}),
            )

            loaded = binance_demo.load_runtime("user-a", application=application)
            restored = binance_demo.state_for(request)

        self.assertEqual(loaded["plans"][self.plan["id"]], self.plan)
        self.assertTrue(loaded["connected"])
        self.assertEqual(loaded["_user_id"], "user-a")
        self.assertEqual(loaded["_restore_origin"], "file_fallback")
        self.assertEqual(loaded["_restore_status"], "restored")
        self.assertIn(self.plan["id"], restored["plans"])
        self.assertFalse(not restored["plans"])

    def test_failed_restore_cannot_overwrite_existing_persisted_state(self):
        self.pool.fetchrow = AsyncMock(side_effect=RuntimeError("database unavailable"))
        self.pool.execute = AsyncMock()
        restored = asyncio.run(binance_demo.restore_demo_state_for_user(self.application, "user-a"))

        binance_demo.persist_runtime(restored)

        self.assertEqual(restored["plans"], {})
        self.assertEqual(restored["_restore_status"], "restore_failed")
        self.pool.execute.assert_not_called()

    def test_missing_credentials_keeps_plan_state_only_and_warns(self):
        with patch.object(main, "ensure_session_cache", new=AsyncMock()), \
                patch.object(main, "session_credentials_for_request", return_value=("", "")), \
                patch.object(binance_demo, "BinanceDemoClient") as client_mock, \
                patch.object(binance_demo, "post_algo") as post_algo_mock, \
                self.assertLogs("app.main", level="WARNING") as logs:
            asyncio.run(main.hydrate_authenticated_user_state(self.request))

        state = self.application.state._binance_demo_user_state["user-a"]
        self.assertIn(self.plan["id"], state["plans"])
        self.assertNotIn("positions", state)
        client_mock.assert_not_called()
        post_algo_mock.assert_not_called()
        self.assertIn("currently unmanaged", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()