import importlib.util
import unittest
from pathlib import Path

from fastapi import HTTPException

APP_DIR = Path(__file__).parents[1] / "app"


def _load(module_name: str) -> object:
    spec = importlib.util.spec_from_file_location(module_name, APP_DIR / f"{module_name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MAINTENANCE = _load("maintenance")


class MaintenanceStateTests(unittest.TestCase):
    """Test 1 & Test 2: NORMAL allows new entries, MAINTENANCE blocks them."""

    def test_default_state_is_normal_and_allows_new_entry(self):
        state = MAINTENANCE.default_maintenance_state()
        self.assertEqual(state["mode"], "NORMAL")
        self.assertFalse(MAINTENANCE.new_entry_blocked(state))
        MAINTENANCE.guard_new_entry(state)  # must not raise

    def test_maintenance_mode_blocks_new_entry_with_clear_status(self):
        state = MAINTENANCE.default_maintenance_state()
        MAINTENANCE.set_maintenance_mode(state, "MAINTENANCE", reason="scheduled window")
        self.assertTrue(MAINTENANCE.new_entry_blocked(state))
        with self.assertRaises(HTTPException) as ctx:
            MAINTENANCE.guard_new_entry(state)
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.detail["code"], "MAINTENANCE_MODE")

    def test_emergency_mode_also_blocks_new_entry(self):
        state = MAINTENANCE.default_maintenance_state()
        MAINTENANCE.set_maintenance_mode(state, "EMERGENCY")
        self.assertTrue(MAINTENANCE.new_entry_blocked(state))

    def test_admin_can_return_maintenance_to_normal(self):
        state = MAINTENANCE.default_maintenance_state()
        MAINTENANCE.set_maintenance_mode(state, "MAINTENANCE")
        MAINTENANCE.set_maintenance_mode(state, "NORMAL", updated_by="owner@example.com")
        self.assertFalse(MAINTENANCE.new_entry_blocked(state))
        self.assertEqual(state["updated_by"], "owner@example.com")

    def test_invalid_mode_is_rejected(self):
        state = MAINTENANCE.default_maintenance_state()
        with self.assertRaises(ValueError):
            MAINTENANCE.set_maintenance_mode(state, "PAUSED")

    def test_unknown_or_missing_state_blocks_new_entry_fail_closed(self):
        """Test 9: unreadable/unknown maintenance state must fail closed for new entries."""
        self.assertEqual(MAINTENANCE.get_maintenance_mode(None), "UNKNOWN")
        self.assertTrue(MAINTENANCE.new_entry_blocked(None))
        self.assertTrue(MAINTENANCE.new_entry_blocked({}))
        self.assertTrue(MAINTENANCE.new_entry_blocked({"mode": "not-a-real-mode"}))
        with self.assertRaises(HTTPException):
            MAINTENANCE.guard_new_entry(None)


class MaintenanceWiringTests(unittest.TestCase):
    """Static wiring checks: new-entry endpoints call guard_new_entry; existing
    position protection (monitoring/SL/TP/Risk Guard/close) never does."""

    def test_new_entry_endpoints_call_guard(self):
        main_source = (APP_DIR / "main.py").read_text(encoding="utf-8")
        demo_source = (APP_DIR / "binance_demo.py").read_text(encoding="utf-8")
        live_source = (APP_DIR / "v25_execution.py").read_text(encoding="utf-8")

        paper_open_body = main_source.split("async def paper_open(order: PaperOrder):", 1)[1].split("\n\n\n", 1)[0]
        self.assertIn("guard_new_entry(", paper_open_body)

        demo_order_body = demo_source.split("async def execute_demo_order(", 1)[1]
        self.assertIn("guard_new_entry(", demo_order_body.split("armed(state)", 1)[0])

        live_order_body = live_source.split("async def execute_live_order(", 1)[1]
        self.assertIn("guard_new_entry(", live_order_body.split("recovery_ready", 1)[0])

    def test_position_protection_paths_do_not_call_guard(self):
        """Test 3-6 & Test 8: refresh/close/risk-guard code must stay untouched."""
        main_source = (APP_DIR / "main.py").read_text(encoding="utf-8")

        def body_of(marker: str, source: str) -> str:
            after = source.split(marker, 1)[1]
            return after.split("\n\n\n", 1)[0]

        refresh_body = body_of("async def refresh_paper_positions(", main_source)
        self.assertNotIn("guard_new_entry", refresh_body)

        close_body = body_of('@app.post("/api/paper/close/{position_id}")', main_source)
        self.assertNotIn("guard_new_entry", close_body)

        risk_body = body_of("def paper_risk_payload(", main_source)
        self.assertNotIn("guard_new_entry", risk_body)

    def test_maintenance_admin_endpoints_use_existing_owner_auth(self):
        main_source = (APP_DIR / "main.py").read_text(encoding="utf-8")
        admin_block = main_source.split('@app.get("/api/v22/admin/maintenance")', 1)[1].split("\n\n\n", 1)[0]
        self.assertIn("authenticated_user(request, owner=True)", admin_block)
        set_block = main_source.split('@app.post("/api/v22/admin/maintenance")', 1)[1].split("\n\n\n", 1)[0]
        self.assertIn("authenticated_user(request, owner=True)", set_block)
        self.assertIn("set_maintenance_mode(", set_block)

    def test_session_endpoint_exposes_mode_to_any_authenticated_user(self):
        """Regular (non-owner) logged-in users must be able to read the mode -
        this is what lets the frontend show/hide the maintenance screen."""
        session_source = (APP_DIR / "v22_commercial.py").read_text(encoding="utf-8")
        session_block = session_source.split('async def v22_session(request: Request):', 1)[1].split("\n\n\n", 1)[0]
        self.assertIn("get_maintenance_mode(", session_block)
        self.assertIn('"maintenance"', session_block)
        self.assertNotIn("owner=True", session_block)

    def test_maintenance_screen_only_blocks_non_owner_and_is_fail_open(self):
        auth_source = (Path(__file__).parents[2] / "AuthGate.tsx").read_text(encoding="utf-8")
        self.assertIn("session.user.role !== 'OWNER' && (maintenanceMode === 'MAINTENANCE' || maintenanceMode === 'EMERGENCY')", auth_source)
        self.assertIn("catch { /* fail-open: keep last known maintenance state */ }", auth_source)
        self.assertIn("if (document.visibilityState === 'hidden') return", auth_source)


if __name__ == "__main__":
    unittest.main()
