import copy
import sys
import unittest
from pathlib import Path

BACKEND = Path(__file__).parents[1]
sys.path.insert(0, str(BACKEND))

from app.binance_demo import inspect_protection_state  # noqa: E402


class ProtectionStateInspectionTests(unittest.TestCase):
    def snapshot(self, **overrides):
        snapshot = {"open_algo_orders_available": True}
        snapshot.update(overrides)
        return snapshot

    def state(self, *, tp1="1", tp2="1", remaining="2"):
        return {
            "plans": {
                "plan-a": {
                    "id": "plan-a",
                    "tp1_quantity": tp1,
                    "tp2_quantity": tp2,
                    "remaining_quantity": remaining,
                }
            }
        }

    def test_clean_case_has_no_quantity_inconsistency(self):
        result = inspect_protection_state(self.snapshot(), self.state())

        self.assertTrue(result["available"])
        self.assertFalse(result["quantity_inconsistent"])
        self.assertEqual(result["inconsistencies"], [])
        self.assertIsNone(result["diagnostic"])

    def test_scenario_three_detects_tp_quantities_above_remaining(self):
        result = inspect_protection_state(
            self.snapshot(), self.state(tp1="1.1", tp2="1.0", remaining="2.0")
        )

        self.assertTrue(result["quantity_inconsistent"])
        self.assertEqual(result["inconsistencies"], [{
            "plan_id": "plan-a",
            "tp1_quantity": "1.1",
            "tp2_quantity": "1.0",
            "remaining_quantity": "2.0",
        }])

    def test_unavailable_snapshot_is_not_treated_as_empty(self):
        result = inspect_protection_state(
            self.snapshot(open_algo_orders_available=False), self.state(tp1="9", tp2="9", remaining="1")
        )

        self.assertFalse(result["available"])
        self.assertFalse(result["quantity_inconsistent"])
        self.assertEqual(result["inconsistencies"], [])
        self.assertEqual(result["diagnostic"], "Binance algo-order snapshot unavailable.")

    def test_inputs_are_unchanged(self):
        snapshot = self.snapshot()
        state = self.state(tp1="1.1", tp2="1.0", remaining="2.0")
        original_snapshot = copy.deepcopy(snapshot)
        original_state = copy.deepcopy(state)

        inspect_protection_state(snapshot, state)

        self.assertEqual(snapshot, original_snapshot)
        self.assertEqual(state, original_state)


if __name__ == "__main__":
    unittest.main()