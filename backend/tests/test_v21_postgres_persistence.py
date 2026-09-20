import asyncio
import os
import sys
import unittest
from types import SimpleNamespace
from uuid import uuid4

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.v21_demo import initial_state, persist_state, restore_v21_state_for_user


class V21PostgresPersistenceTests(unittest.TestCase):
    """Real SQL integration coverage; requires an explicitly temporary test DSN."""

    def test_user_state_round_trips_through_postgres(self):
        dsn = os.getenv("PROTREBOT_TEST_POSTGRES_DSN", "").strip()
        if not dsn:
            self.skipTest("PROTREBOT_TEST_POSTGRES_DSN is not configured")
        try:
            import asyncpg
        except ImportError:
            self.skipTest("asyncpg is not installed")

        asyncio.run(self._round_trip(asyncpg, dsn))

    async def _round_trip(self, asyncpg, dsn):
        connection = await asyncpg.connect(dsn, timeout=3)
        user_id = f"v21-persistence-test-{uuid4().hex}"
        key = f"v21_demo:user:{user_id}"
        try:
            await connection.execute(
                """
                CREATE TABLE IF NOT EXISTS application_state_snapshots (
                  state_key TEXT PRIMARY KEY,
                  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                  payload JSONB NOT NULL
                )
                """
            )
            application = SimpleNamespace(state=SimpleNamespace(db_pool=connection, _v21_persistence_tasks=set()))
            state = initial_state()
            state.update({"_user_id": user_id, "_app": application})
            state["settings"]["max_positions"] = 2
            state["journal"].append({"symbol": "BTCUSDT", "realized_pnl": 3.5, "verified_realized": True})
            state["paper_positions"].append({"id": "position-a", "symbol": "BTCUSDT", "status": "PAPER_OPEN"})
            state["snapshot"] = {"positions": [{"symbol": "BTCUSDT"}], "open_orders": []}
            state["reconciliation"] = {"actual_exchange_open_positions": 1, "internal_active_plans": 0, "reconciled_active_positions": 1}

            persist_state(state)
            await asyncio.gather(*list(application.state._v21_persistence_tasks))
            stored = await connection.fetchrow("SELECT payload FROM application_state_snapshots WHERE state_key = $1", key)
            self.assertIsNotNone(stored)
            self.assertEqual(stored["payload"]["user_id"], user_id)
            self.assertEqual(stored["payload"]["journal"][0]["realized_pnl"], 3.5)

            restarted = SimpleNamespace(state=SimpleNamespace(db_pool=connection, _v21_demo_user_state={}))
            restored = await restore_v21_state_for_user(restarted, user_id)
            self.assertIsNotNone(restored)
            self.assertEqual(restored["settings"]["max_positions"], 2)
            self.assertEqual(restored["journal"], state["journal"])
            self.assertEqual(restored["paper_positions"], state["paper_positions"])
            self.assertEqual(restored["snapshot"], state["snapshot"])
            self.assertEqual(restored["reconciliation"], state["reconciliation"])
            self.assertIsNone(await restore_v21_state_for_user(restarted, f"other-{user_id}"))
        finally:
            await connection.execute("DELETE FROM application_state_snapshots WHERE state_key = $1", key)
            await connection.close()


if __name__ == "__main__":
    unittest.main()
