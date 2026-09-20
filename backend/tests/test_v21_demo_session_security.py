import asyncio
import os
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).parents[2]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

os.environ.setdefault("PROTREBOT_VAULT_MASTER_KEY", "demo-vault-master-secret-123456")

from app.binance_demo import credentials_configured
from app.exchange_connections import SaveCredentialsRequest, exchange_connection_save, session_credentials
from app.v21_demo import AutoStartRequest, automatic_cycle, v21_auto_start


class DemoSessionSecurityTests(unittest.TestCase):
    @patch("app.exchange_connections.test_binance_credentials", new_callable=AsyncMock)
    def test_save_persists_active_session_credentials(self, test_binance_credentials):
        test_binance_credentials.return_value = {
            "mode": "TESTNET",
            "host": "https://demo-fapi.binance.com",
            "wallet_balance": 1000.0,
            "available_balance": 1000.0,
            "unrealized_pnl": 0.0,
            "active_positions": 0,
            "hedge_mode": False,
            "clock_offset_ms": 0,
            "tested_at": "2026-09-08T00:00:00+00:00",
            "orders_created": False,
        }

        class Pool:
            async def execute(self, *args, **kwargs):
                return None

            async def fetch(self, *args, **kwargs):
                return []

        pool = Pool()
        application = SimpleNamespace(
            state=SimpleNamespace(
                db_pool=pool,
                http=AsyncMock(),
                exchange_vault={"ready": True, "storage": "POSTGRESQL + FERNET", "reason": None, "loaded_at": "now", "pool_id": id(pool)},
            )
        )
        owner = {"id": "owner-session-save", "role": "OWNER", "active": True, "email_verified": True, "auth_version": 1}
        request = SimpleNamespace(
            app=application,
            headers={"authorization": "Bearer demo-session-token"},
            state=SimpleNamespace(member=owner),
        )

        result = asyncio.run(
            exchange_connection_save(
                request,
                SaveCredentialsRequest(
                    mode="TESTNET",
                    api_key="abcdefghijklmnopqrstuvwxyz",
                    secret_key="1234567890abcdef",
                    confirmation="TESTNET KASAYA KAYDET",
                ),
            )
        )

        self.assertTrue(result["connections"]["TESTNET"]["active"])
        self.assertTrue(result["connections"]["TESTNET"]["configured"])
        api_key, secret_key = session_credentials(request, "TESTNET", active_only=False)
        self.assertEqual(api_key, "abcdefghijklmnopqrstuvwxyz")
        self.assertEqual(secret_key, "1234567890abcdef")

    @patch("app.binance_demo.load_demo_credentials")
    @patch("app.exchange_connections.session_credentials_for_request")
    def test_authenticated_request_does_not_fallback_to_global_demo_credentials(self, session_credentials_mock, load_demo_credentials_mock):
        session_credentials_mock.return_value = ("", "")
        load_demo_credentials_mock.return_value = ("legacy-api-key", "legacy-secret-key")
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(http=AsyncMock())),
            headers={"authorization": "Bearer demo-session-token"},
            state=SimpleNamespace(member={"id": "user-1", "role": "OWNER"}),
        )

        self.assertFalse(credentials_configured(request))
        load_demo_credentials_mock.assert_not_called()

    @patch("app.v21_demo.emit_notification")
    @patch("app.v21_demo.record_event")
    @patch("app.v21_demo.persist_state")
    @patch("app.v21_demo.automatic_cycle", new_callable=AsyncMock)
    @patch("app.v21_demo.account_snapshot", new_callable=AsyncMock)
    @patch("app.v21_demo.credentials_configured")
    @patch("app.exchange_connections.session_credentials_for_request")
    def test_auto_start_uses_authenticated_session_credentials(self, session_credentials_mock, configured_mock, snapshot_mock, _cycle, _persist, _record, _notify):
        configured_mock.return_value = True
        session_credentials_mock.return_value = ("demo-api-key-123456", "demo-secret-key-123456")
        snapshot_mock.return_value = {"hedge_mode": False}
        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    binance_demo={"armed_until": time.time() + 60},
                    http=AsyncMock(),
                    v21_demo={
                        "settings": {"max_loss_per_trade": 5.0, "max_margin_per_trade": 50.0, "schedule_start_hour": 0, "schedule_end_hour": 24, "scan_seconds": 900, "allowed_symbols": ["BTCUSDT"], "allow_long": True, "allow_short": True, "daily_loss_limit": 30.0, "daily_trade_limit": 6, "max_positions": 3, "min_confidence": 78, "max_volatility_pct": 3.5, "max_correlation_pct": 82, "breakeven_enabled": True, "breakeven_trigger_r": 1.0, "trailing_enabled": False, "trailing_trigger_r": 1.5, "trailing_distance_r": 0.75, "notifications": True, "fee_bps_per_side": 4.0, "slippage_bps_per_side": 2.0, "consecutive_loss_limit": 3, "kill_switch": False},
                        "journal": [],
                        "seen_event_ids": [],
                        "auto": {"enabled": False, "busy": False, "cycles": 0, "last_scan": None, "user_confirmed": False, "confirmation": None, "last_decision": "Kullanıcı onayı bekleniyor.", "last_error": None, "rejection_gate": None, "rejection_reason": None, "status": "OFF", "pause_reason": None, "started_at": None},
                        "risk": {"consecutive_losses": 0, "consecutive_loss_limit": 3, "kill_switch": False},
                        "notifications": {"seen": [], "unread": 0},
                        "scanner": {"active": False, "running": False, "scan_status": "BEKLEMEDE", "coins_scanned": 0, "scan_duration_ms": 0, "last_scan_at": None, "next_scan_at": None, "last_scan": None, "next_scan": None, "top_candidates": [], "all_candidates": [], "selected_symbols": [], "last_stage": "BEKLEMEDE", "eligible_count": 0, "last_error": None},
                        "automation_trades": [],
                        "paper_positions": [],
                        "stream": {"status": "BEKLEMEDE", "transport": "REST EŞLEŞTİRME", "last_event": None, "last_sync": None, "reconnect_count": 0, "error_count": 0, "last_error": None},
                        "snapshot": None,
                        "backtest": None,
                        "drills": {"RECONNECT": None, "EMERGENCY": None, "PROTECTION": None},
                        "duplicate_blocks": 0,
                        "duplicate_submissions": 0,
                        "protection_repairs": 0,
                        "last_saved": None,
                    },
                )
            ),
            headers={"authorization": "Bearer demo-session-token"},
            state=SimpleNamespace(member={"id": "user-1", "role": "OWNER"}),
        )

        result = asyncio.run(v21_auto_start(request, AutoStartRequest(confirmation="DEMO OTOMATİK")))

        self.assertTrue(configured_mock.called)
        self.assertIs(configured_mock.call_args.args[0], request)
        self.assertTrue(result["auto"]["enabled"])
        self.assertEqual(result["auto"]["status"], "ON")

    @patch("app.v21_demo.persist_state")
    @patch("app.v21_demo.account_snapshot", new_callable=AsyncMock)
    @patch("app.v21_demo.scan_demo_universe", new_callable=AsyncMock)
    @patch("app.v21_demo.client_for")
    def test_automatic_cycle_uses_authenticated_request_credentials(self, client_for_mock, scan_demo_universe_mock, account_snapshot_mock, persist_state_mock):
        application = SimpleNamespace(
            state=SimpleNamespace(
                binance_demo={"armed_until": time.time() + 60},
                http=AsyncMock(),
                v21_demo={
                    "settings": {
                        "max_loss_per_trade": 5.0,
                        "max_margin_per_trade": 50.0,
                        "schedule_start_hour": 0,
                        "schedule_end_hour": 24,
                        "scan_seconds": 900,
                        "allowed_symbols": ["BTCUSDT"],
                        "allow_long": True,
                        "allow_short": True,
                        "daily_loss_limit": 30.0,
                        "daily_trade_limit": 6,
                        "max_positions": 3,
                        "min_confidence": 78,
                        "max_volatility_pct": 3.5,
                        "max_correlation_pct": 82,
                        "breakeven_enabled": True,
                        "breakeven_trigger_r": 1.0,
                        "trailing_enabled": False,
                        "trailing_trigger_r": 1.5,
                        "trailing_distance_r": 0.75,
                        "notifications": True,
                        "fee_bps_per_side": 4.0,
                        "slippage_bps_per_side": 2.0,
                        "consecutive_loss_limit": 3,
                        "kill_switch": False,
                    },
                    "auto": {"enabled": True, "user_confirmed": True, "busy": False, "cycles": 0, "last_error": None, "rejection_gate": None, "rejection_reason": None, "status": "ON", "pause_reason": None, "last_decision": ""},
                    "risk": {"consecutive_losses": 0, "consecutive_loss_limit": 3, "kill_switch": False},
                    "scanner": {"active": False, "running": False, "scan_status": "BEKLEMEDE", "coins_scanned": 0, "selected_symbols": [], "top_candidates": [], "all_candidates": [], "last_stage": "BEKLEMEDE", "eligible_count": 0, "last_error": None, "last_scan_at": None, "next_scan_at": None},
                    "paper_positions": [],
                    "automation_trades": [],
                    "journal": [],
                    "seen_event_ids": [],
                    "notifications": {"seen": [], "unread": 0},
                    "duplicate_blocks": 0,
                    "duplicate_submissions": 0,
                    "protection_repairs": 0,
                    "last_saved": None,
                },
            )
        )
        request = SimpleNamespace(
            app=application,
            headers={"authorization": "Bearer demo-session-token"},
            state=SimpleNamespace(member={"id": "user-1", "role": "OWNER"}),
        )
        application.state._v21_demo_user_state = {"user-1": application.state.v21_demo}
        client_for_mock.return_value = object()
        account_snapshot_mock.return_value = {"wallet_balance": 1000.0, "available_balance": 1000.0, "positions": [], "open_orders": [], "hedge_mode": False}
        scan_demo_universe_mock.return_value = []

        asyncio.run(automatic_cycle(application, request=request))

        self.assertTrue(client_for_mock.called)
        self.assertIs(client_for_mock.call_args.args[0], request)


if __name__ == "__main__":
    unittest.main()
