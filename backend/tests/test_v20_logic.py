import ast
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


SOURCE_PATH = Path(__file__).parents[1] / "app" / "main.py"
SOURCE_TEXT = SOURCE_PATH.read_text(encoding="utf-8")
FRONTEND_TEXT = (Path(__file__).parents[2] / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
STYLE_TEXT = (Path(__file__).parents[2] / "frontend" / "src" / "v20.css").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE_TEXT)


def load_functions(*names):
    nodes = [node for node in TREE.body if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace = {"datetime": datetime, "timedelta": timedelta, "timezone": timezone, "os": os}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE_PATH), "exec"), namespace)
    return namespace


CORE = load_functions(
    "reset_daily_risk_if_needed",
    "paper_risk_payload",
    "paper_performance_payload",
    "latest_decision_review",
    "decision_blackbox_payload",
    "emergency_brake_payload",
    "testnet_readiness",
    "v20_profile_policy",
    "v20_target_plan",
    "paper_grid_levels",
    "paper_limit_triggered",
    "update_paper_mae_mfe",
    "finalize_paper_trade_diagnostics",
    "advance_v20_position",
    "paper_lifecycle_event_message",
    "v20_max_drawdown",
    "v20_ghost_twin_payload",
    "v20_release_certificate",
)


def open_position(direction="LONG"):
    entry = 100.0
    stop = 99.0 if direction == "LONG" else 101.0
    targets = (101.0, 102.0, 103.0) if direction == "LONG" else (99.0, 98.0, 97.0)
    return {
        "id": 1, "symbol": "BTCUSDT", "direction": direction,
        "entry_price": entry, "current_price": entry, "stop_loss": stop,
        "initial_stop_loss": stop, "take_profit": targets[2],
        "tp1": targets[0], "tp2": targets[1], "tp3": targets[2],
        "amount": 100.0, "original_amount": 100.0,
        "quantity": 1.0, "original_quantity": 1.0,
        "unrealized_pnl": 0.0, "status": "AÇIK", "source": "DEMO",
        "opened_at": (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
    }


def paper_state(trades=None, memories=None):
    return {
        "balance": 10_000.0, "initial_balance": 10_000.0,
        "positions": [], "trades": trades or [], "decision_memory": memories or [],
        "risk": {
            "day": datetime.now(timezone.utc).date().isoformat(),
            "daily_realized_pnl": 0.0, "daily_loss_limit": 250.0,
            "consecutive_losses": 0, "consecutive_loss_limit": 2,
            "cooldown_until": None, "daily_locked": False, "reason": "V20 Risk Kasası normal.",
        },
        "emergency_brake": {"active": False, "reason": "Acil fren kapalı.", "source": None, "triggered_at": None},
    }


class V20UnifiedTests(unittest.TestCase):
    def test_three_profiles_are_order_locked_and_ordered_by_speed(self):
        cautious = CORE["v20_profile_policy"]("TEMKINLI")
        balanced = CORE["v20_profile_policy"]("DENGELI")
        fast = CORE["v20_profile_policy"]("HIZLI")
        self.assertGreater(cautious["cycle_seconds"], balanced["cycle_seconds"])
        self.assertGreater(balanced["cycle_seconds"], fast["cycle_seconds"])
        self.assertGreater(cautious["minimum_confidence"], fast["minimum_confidence"])
        for policy in (cautious, balanced, fast):
            self.assertFalse(policy["orders_enabled"])
            self.assertFalse(policy["testnet_orders_enabled"])

    def test_target_plan_builds_ordered_long_and_short_levels(self):
        long_plan = CORE["v20_target_plan"](100, 98, "LONG")
        short_plan = CORE["v20_target_plan"](100, 102, "SHORT")
        self.assertEqual([long_plan[key] for key in ("tp1", "tp2", "tp3")], [102, 104, 106])
        self.assertEqual([short_plan[key] for key in ("tp1", "tp2", "tp3")], [98, 96, 94])
        self.assertEqual(long_plan["partial_plan"], [35, 35, 30])

    def test_manual_limit_trigger_and_grid_map_are_direction_aware(self):
        levels = CORE["paper_grid_levels"](95, 105, 6)
        self.assertEqual(levels, [95, 97, 99, 101, 103, 105])
        self.assertTrue(CORE["paper_limit_triggered"]("LONG", 100, 99.9))
        self.assertFalse(CORE["paper_limit_triggered"]("LONG", 100, 100.1))
        self.assertTrue(CORE["paper_limit_triggered"]("SHORT", 100, 100.1))
        self.assertFalse(CORE["paper_limit_triggered"]("SHORT", 100, 99.9))

    def test_long_lifecycle_partially_closes_and_never_double_counts(self):
        position = open_position("LONG")
        first = CORE["advance_v20_position"](position, 101.1)
        self.assertAlmostEqual(position["quantity"], 0.65)
        self.assertEqual(position["partial_targets_hit"], ["TP1"])
        self.assertGreaterEqual(position["stop_loss"], 100.0)
        self.assertGreater(first["realized_delta"], 0)

        second = CORE["advance_v20_position"](position, 102.1)
        self.assertAlmostEqual(position["quantity"], 0.30)
        self.assertEqual(position["partial_targets_hit"], ["TP1", "TP2"])
        self.assertGreater(second["realized_delta"], 0)

        third = CORE["advance_v20_position"](position, 103.1)
        self.assertTrue(third["closed"])
        self.assertEqual(position["status"], "TP3")
        self.assertEqual(position["quantity"], 0.0)
        total = position["realized_pnl"]
        duplicate = CORE["advance_v20_position"](position, 104.0)
        self.assertEqual(duplicate["realized_delta"], 0.0)
        self.assertEqual(position["realized_pnl"], total)

    def test_short_stop_closes_once_with_fee(self):
        position = open_position("SHORT")
        result = CORE["advance_v20_position"](position, 101.2)
        self.assertTrue(result["closed"])
        self.assertEqual(position["status"], "STOP")
        self.assertLess(position["realized_pnl"], 0)
        self.assertGreater(position["fee"], 0)

    def test_long_paper_mae_mfe_uses_running_watermarks(self):
        position = open_position("LONG")
        CORE["update_paper_mae_mfe"](position, 102.0)
        CORE["update_paper_mae_mfe"](position, 99.5)
        position.update({"status": "TP3", "realized_pnl": 1.0})
        CORE["finalize_paper_trade_diagnostics"](position)
        self.assertEqual(position["mfe_price"], 2.0)
        self.assertEqual(position["mae_price"], 0.5)
        self.assertEqual(position["mfe_r"], 2.0)
        self.assertEqual(position["mae_r"], 0.5)

    def test_short_paper_mae_mfe_uses_running_watermarks(self):
        position = open_position("SHORT")
        CORE["update_paper_mae_mfe"](position, 98.0)
        CORE["update_paper_mae_mfe"](position, 101.5)
        position.update({"status": "TP3", "realized_pnl": 1.0})
        CORE["finalize_paper_trade_diagnostics"](position)
        self.assertEqual(position["mfe_price"], 2.0)
        self.assertEqual(position["mae_price"], 1.5)
        self.assertEqual(position["mfe_r"], 2.0)
        self.assertEqual(position["mae_r"], 1.5)

    def test_paper_stop_positive_pnl_warning_is_observational(self):
        position = open_position("LONG")
        position.update({"status": "STOP", "realized_pnl": 0.5})
        CORE["finalize_paper_trade_diagnostics"](position)
        self.assertEqual(position["status"], "STOP")
        self.assertEqual(position["realized_pnl"], 0.5)
        self.assertEqual(position["exit_consistency_warning"], "STOP_WITH_POSITIVE_PNL")

    def test_expired_position_closes_remaining_quantity(self):
        position = open_position("LONG")
        position["opened_at"] = (datetime.now(timezone.utc) - timedelta(minutes=31)).isoformat()
        position["max_holding_minutes"] = 30
        result = CORE["advance_v20_position"](position, 100.2)
        self.assertTrue(result["closed"])
        self.assertEqual(position["status"], "ZAMAN")
        self.assertEqual(position["quantity"], 0.0)

    def test_lifecycle_messages_make_partial_and_final_closes_visible(self):
        position = open_position("LONG")
        position["partial_realized_pnl"] = 0.42
        partial = CORE["paper_lifecycle_event_message"](
            position,
            {"kind": "TP1", "net_pnl": 0.42},
        )
        self.assertIn("TP1 gerçekleşti", partial)
        self.assertIn("pozisyon açık", partial)

        position.update({"status": "STOP", "realized_pnl": -0.35, "partial_realized_pnl": -0.35})
        closed = CORE["paper_lifecycle_event_message"](
            position,
            {"kind": "STOP", "net_pnl": -0.35},
        )
        self.assertIn("stop ile kapandı", closed)
        self.assertIn("-0.35 USDT", closed)

    def test_ghost_twin_separates_saves_from_missed_opportunities(self):
        now = datetime.now(timezone.utc).isoformat()
        memories = [
            {"decision": "ENGELLENDİ", "symbol": "BTCUSDT", "direction": "LONG", "reason": "risk", "created_at": now, "reviews": {"60": {"minutes": 60, "return_pct": -0.4}}},
            {"decision": "ENGELLENDİ", "symbol": "ETHUSDT", "direction": "SHORT", "reason": "likidite", "created_at": now, "reviews": {"60": {"minutes": 60, "return_pct": 0.8}}},
        ]
        ghost = CORE["v20_ghost_twin_payload"](paper_state([{"realized_pnl": 2.5}], memories))
        self.assertEqual(ghost["shield_saves"], 1)
        self.assertEqual(ghost["missed_opportunities"], 1)
        self.assertEqual(len(ghost["rows"]), 2)
        self.assertFalse(ghost["orders_enabled"])

    def test_certificate_can_pass_paper_evidence_but_never_unlocks_orders(self):
        trades = [{"realized_pnl": 1.0} for _ in range(24)] + [{"realized_pnl": -0.5} for _ in range(6)]
        now = datetime.now(timezone.utc).isoformat()
        memories = [{
            "decision": "ENGELLENDİ", "symbol": "BTCUSDT", "direction": "LONG",
            "reason": "kapı", "created_at": now,
            "reviews": {"60": {"minutes": 60, "return_pct": -0.2}},
        } for _ in range(10)]
        certificate = CORE["v20_release_certificate"](paper_state(trades, memories))
        self.assertTrue(certificate["paper_ready"])
        self.assertFalse(certificate["testnet_ready"])
        self.assertFalse(certificate["live_ready"])
        self.assertFalse(certificate["orders_enabled"])
        self.assertFalse(certificate["testnet_orders_enabled"])

    def test_source_and_ui_expose_v20_without_exchange_order_calls(self):
        lowered = SOURCE_TEXT.lower()
        self.assertIn('version="20.2.0"', SOURCE_TEXT)
        self.assertIn('/api/v20/command', SOURCE_TEXT)
        self.assertIn('/api/v20/ghost-twin', SOURCE_TEXT)
        self.assertIn('/api/v20/certificate', SOURCE_TEXT)
        self.assertIn('/api/paper/limit', SOURCE_TEXT)
        self.assertIn('refresh_paper_limit_orders', SOURCE_TEXT)
        self.assertIn('V25.1.2 · LIVE GUARD', FRONTEND_TEXT)
        self.assertIn('Limit & Pozisyon Haritası', FRONTEND_TEXT)
        self.assertIn('PAPER LİMİT EMRİNİ KAYDET', FRONTEND_TEXT)
        self.assertIn('gridLevelStrip', FRONTEND_TEXT)
        self.assertIn('v20Autopilot', FRONTEND_TEXT)
        self.assertIn('CANLI İŞLEM AKIŞI', FRONTEND_TEXT)
        self.assertIn('activityPosition', FRONTEND_TEXT)
        self.assertIn('.v20PositionStage', STYLE_TEXT)
        self.assertNotIn('/api/v3/order', lowered)
        self.assertNotIn('fapi/v1/order', lowered)
        self.assertNotIn('create_order(', lowered)
        self.assertNotIn('place_order(', lowered)


if __name__ == "__main__":
    unittest.main(verbosity=2)
