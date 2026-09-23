import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.v27_cloud_core import durable_payload, evidence_rows, json_safe, restore_payload


ROOT = Path(__file__).parents[2]
MAIN = (ROOT / "backend" / "app" / "main.py").read_text(encoding="utf-8")
FRONTEND = (ROOT / "frontend" / "src" / "CloudOpsCenter.tsx").read_text(encoding="utf-8")
SHELL = (ROOT / "frontend" / "src" / "TestnetFirstApp.tsx").read_text(encoding="utf-8")
RENDER = (ROOT / "render.yaml").read_text(encoding="utf-8")


def fake_application():
    return SimpleNamespace(state=SimpleNamespace(
        v21_demo={
            "settings": {"allowed_symbols": ["BTCUSDT"], "max_positions": 2},
            "journal": [{
                "id": "event-1", "created_at": "2026-08-11T00:00:00+00:00",
                "kind": "AUTO_ORDER", "symbol": "BTCUSDT", "message": "Demo giri┼ş",
            }],
            "seen_event_ids": ["event-1"], "backtest": None, "drills": {},
            "duplicate_blocks": 1, "duplicate_submissions": 0, "protection_repairs": 2,
            "auto": {"enabled": True, "busy": True, "last_decision": "eski"},
            "lock": asyncio.Lock(),
        },
        binance_demo={
            "plans": {"plan-1": {"symbol": "BTCUSDT", "status": "KORUMA AKT─░F"}},
            "events": [{"kind": "KORUMA", "message": "Stop kuruldu"}],
            "armed_until": 99_999_999_999, "connected": True, "lock": asyncio.Lock(),
        },
    ))


class V27CloudOperationsTests(unittest.TestCase):
    def test_payload_excludes_secret_and_runtime_objects(self):
        app = fake_application()
        app.state.v21_demo["secret_key"] = "must-not-leak"
        payload = durable_payload(app)
        text = str(payload)
        self.assertNotIn("must-not-leak", text)
        self.assertNotIn("lock", payload["v21"])
        self.assertFalse(payload["safety"]["real_orders_enabled"])

    def test_restore_is_fail_closed(self):
        source = fake_application()
        payload = durable_payload(source)
        target = fake_application()
        self.assertTrue(restore_payload(target, payload))
        self.assertFalse(target.state.v21_demo["auto"]["enabled"])
        self.assertFalse(target.state.v21_demo["auto"]["busy"])
        self.assertEqual(target.state.binance_demo["armed_until"], 0)
        self.assertFalse(target.state.binance_demo["connected"])

    def test_evidence_has_stable_identity(self):
        rows = evidence_rows(durable_payload(fake_application()))
        self.assertEqual(rows[0][0], "event-1")
        self.assertEqual(rows[0][1], "AUTO_ORDER")
        self.assertEqual(rows[0][2], "BTCUSDT")

    def test_non_finite_numbers_are_null(self):
        self.assertIsNone(json_safe(float("nan")))
        self.assertIsNone(json_safe(float("inf")))

    def test_v27_contract_is_wired(self):
        self.assertIn('version="27.0.0"', MAIN)
        self.assertIn("init_v27_cloud(app)", MAIN)
        self.assertIn("app.include_router(v27_cloud_router)", MAIN)
        self.assertIn("OPERASYON & KANIT", SHELL)
        self.assertIn("KANITI ┼Ş─░MD─░ KAYDET", FRONTEND)
        self.assertIn("PROTREBOT_DEPLOYMENT_TIER", RENDER)


if __name__ == "__main__":
    unittest.main(verbosity=2)
