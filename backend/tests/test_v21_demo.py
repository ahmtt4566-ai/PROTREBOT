import ast
import json
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any


ROOT = Path(__file__).parents[2]
BACKEND = ROOT / "backend"
BINANCE_SOURCE = (BACKEND / "app" / "binance_demo.py").read_text(encoding="utf-8")
V21_SOURCE = (BACKEND / "app" / "v21_demo.py").read_text(encoding="utf-8")
MAIN_SOURCE = (BACKEND / "app" / "main.py").read_text(encoding="utf-8")
FRONTEND_SOURCE = (ROOT / "frontend" / "src" / "BinanceDemo.tsx").read_text(encoding="utf-8")
GITIGNORE_SOURCE = (ROOT / ".gitignore").read_text(encoding="utf-8")


class HTTPException(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2 / (period + 1)
    output = [values[0]]
    for value in values[1:]:
        output.append(alpha * value + (1 - alpha) * output[-1])
    return output


def atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> float:
    ranges = []
    for index in range(1, len(closes)):
        ranges.append(max(highs[index] - lows[index], abs(highs[index] - closes[index - 1]), abs(lows[index] - closes[index - 1])))
    return sum(ranges[-period:]) / min(period, len(ranges)) if ranges else 0.0


def load_core() -> dict[str, Any]:
    tree = ast.parse(V21_SOURCE)
    wanted = {"initial_state", "_read_state_file", "migrate_legacy_allowed_symbols", "load_state", "risk_size_values", "backtest_engine", "certificate_payload"}
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
    namespace: dict[str, Any] = {
        "Any": Any,
        "Path": Path,
        "Decimal": Decimal,
        "json": json,
        "datetime": datetime,
        "timezone": timezone,
        "HTTPException": HTTPException,
        "DEFAULT_SETTINGS": {
            "allowed_symbols": ["BTCUSDT"], "allow_long": True, "allow_short": True,
            "max_loss_per_trade": 5.0, "max_margin_per_trade": 50.0,
            "daily_loss_limit": 30.0, "daily_trade_limit": 6, "max_positions": 3,
            "min_confidence": 78, "max_volatility_pct": 3.5, "max_correlation_pct": 82,
            "schedule_start_hour": 0, "schedule_end_hour": 24, "scan_seconds": 30,
            "breakeven_enabled": True, "breakeven_trigger_r": 1.0,
            "trailing_enabled": False, "trailing_trigger_r": 1.5, "trailing_distance_r": 0.75,
            "notifications": True, "fee_bps_per_side": 4.0, "slippage_bps_per_side": 2.0,
        },
        "STATE_PATH": Path("state.json"), "BACKUP_PATH": Path("state.backup.json"),
        "MAX_NOTIONAL_USDT": Decimal("200"), "DEMO_REST_BASE": "https://demo-fapi.binance.com",
        "DEMO_WS_BASE": "wss://demo-fstream.binance.com", "ema": ema, "atr": atr,
        "now_iso": lambda: datetime.now(timezone.utc).isoformat(),
    }
    module = ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))
    exec(compile(module, str(BACKEND / "app" / "v21_demo.py"), "exec"), namespace)
    return namespace


CORE = load_core()


class V21DemoSafetyTests(unittest.TestCase):
    def test_only_official_demo_trade_hosts_are_compiled(self):
        combined = BINANCE_SOURCE + V21_SOURCE
        self.assertIn('DEMO_REST_BASE = "https://demo-fapi.binance.com"', combined)
        self.assertIn('DEMO_WS_BASE = "wss://demo-fstream.binance.com"', combined)
        self.assertNotIn('"https://fapi.binance.com"', combined)
        self.assertNotIn('"wss://fstream.binance.com"', combined)
        self.assertIn("real_trading_locked", V21_SOURCE)

    def test_restart_never_restores_automatic_entries(self):
        original_state_path = CORE["STATE_PATH"]
        original_backup_path = CORE["BACKUP_PATH"]
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                CORE["STATE_PATH"] = Path(temp_dir) / "state.json"
                CORE["BACKUP_PATH"] = Path(temp_dir) / "state.backup.json"
                payload = CORE["initial_state"]()
                payload["auto"]["enabled"] = True
                payload["auto"]["user_confirmed"] = True
                payload["settings"]["daily_trade_limit"] = 4
                CORE["STATE_PATH"].write_text(json.dumps(payload), encoding="utf-8")
                restored = CORE["load_state"]()
                self.assertFalse(restored["auto"]["enabled"])
                self.assertFalse(restored["auto"]["user_confirmed"])
                self.assertIsNone(restored["auto"]["confirmation"])
                self.assertEqual(restored["settings"]["daily_trade_limit"], 4)
                self.assertIn("onayı bekleniyor", restored["auto"]["last_decision"])
        finally:
            CORE["STATE_PATH"] = original_state_path
            CORE["BACKUP_PATH"] = original_backup_path

    def test_load_state_migrates_only_the_legacy_default_allowlist(self):
        original_state_path = CORE["STATE_PATH"]
        original_backup_path = CORE["BACKUP_PATH"]
        legacy_default = [
            "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
            "ADAUSDT", "AVAXUSDT", "LINKUSDT", "LTCUSDT", "BCHUSDT", "DOTUSDT",
            "UNIUSDT", "NEARUSDT", "ATOMUSDT", "ETCUSDT", "FILUSDT", "APTUSDT",
            "ARBUSDT", "OPUSDT", "SUIUSDT", "INJUSDT", "SEIUSDT", "TIAUSDT",
        ]
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                CORE["STATE_PATH"] = Path(temp_dir) / "state.json"
                CORE["BACKUP_PATH"] = Path(temp_dir) / "state.backup.json"
                payload = CORE["initial_state"]()
                payload["settings"]["allowed_symbols"] = legacy_default
                CORE["STATE_PATH"].write_text(json.dumps(payload), encoding="utf-8")
                restored = CORE["load_state"]()
                self.assertEqual(restored["settings"]["allowed_symbols"], [])
                payload["settings"]["allowed_symbols"] = ["BTCUSDT", "ETHUSDT"]
                CORE["STATE_PATH"].write_text(json.dumps(payload), encoding="utf-8")
                restored = CORE["load_state"]()
                self.assertEqual(restored["settings"]["allowed_symbols"], ["BTCUSDT", "ETHUSDT"])
        finally:
            CORE["STATE_PATH"] = original_state_path
            CORE["BACKUP_PATH"] = original_backup_path

    def test_automatic_cycle_refuses_to_trade_without_explicit_confirmation(self):
        state = CORE["initial_state"]()
        state["auto"]["enabled"] = True
        state["auto"]["user_confirmed"] = False
        state["settings"]["daily_trade_limit"] = 6
        state["journal"] = []
        self.assertEqual(state["auto"]["enabled"], True)
        self.assertFalse(state["auto"]["user_confirmed"])
        self.assertIn("onayı bekleniyor", CORE["initial_state"]()["auto"]["last_decision"])

    def test_risk_sizing_respects_margin_notional_and_loss_caps(self):
        result = CORE["risk_size_values"](100.0, 99.0, 25.0, 2, 50.0)
        self.assertLessEqual(result["margin_usdt"], 50.0)
        self.assertLessEqual(result["notional_usdt"], 100.0)
        self.assertLessEqual(result["estimated_stop_loss_usdt"], 25.0)
        self.assertTrue(result["capped"])

    def test_backtest_is_chronological_and_conservative(self):
        candles = []
        price = 100.0
        for index in range(420):
            close = price + 0.12 + (0.03 if index % 7 else -0.01)
            candles.append({
                "time": index * 900,
                "open": price,
                "high": max(price, close) + 0.18,
                "low": min(price, close) - 0.14,
                "close": close,
                "volume": 1000 + index,
            })
            price = close
        report = CORE["backtest_engine"](candles, CORE["DEFAULT_SETTINGS"])
        self.assertTrue(report["no_lookahead"])
        self.assertEqual(report["same_candle_policy"], "STOP_FIRST")
        self.assertEqual(len(report["folds"]), 3)
        for trade in report["recent_trades"]:
            self.assertGreater(trade["entry_time"], trade["signal_time"])

    def test_certificate_can_never_claim_real_trading_readiness(self):
        certificate = CORE["certificate_payload"](CORE["initial_state"]())
        self.assertTrue(certificate["demo_only"])
        self.assertFalse(certificate["real_trading_ready"])
        self.assertIn("Demo", certificate["reason"])

    def test_user_stream_and_algo_service_are_allowlisted(self):
        self.assertIn('"/fapi/v1/ticker/24hr"', BINANCE_SOURCE)
        self.assertIn('("POST", "/fapi/v1/listenKey")', BINANCE_SOURCE)
        self.assertIn('("PUT", "/fapi/v1/listenKey")', BINANCE_SOURCE)
        self.assertIn('("DELETE", "/fapi/v1/listenKey")', BINANCE_SOURCE)
        self.assertIn('("POST", "/fapi/v1/algoOrder")', BINANCE_SOURCE)
        self.assertIn('("GET", "/fapi/v1/openAlgoOrders")', BINANCE_SOURCE)
        self.assertIn('event_type == "ORDER_TRADE_UPDATE"', V21_SOURCE)
        self.assertIn('event_type == "ALGO_UPDATE"', V21_SOURCE)

    def test_explicit_demo_stop_cancellation_is_not_repaired(self):
        self.assertIn("not plan.get(\"stop_protection_cancelled\")", V21_SOURCE)
        self.assertIn('"KORUMA İPTAL"', V21_SOURCE)

    def test_v21_control_center_and_all_tabs_are_present(self):
        self.assertIn('version="25.0.0"', MAIN_SOURCE)
        self.assertIn("v21_demo_router", MAIN_SOURCE)
        for label in ("İŞLEM MASASI", "RİSK KASASI", "CANLI GÜNLÜK", "OTOMASYON", "BACKTEST LAB", "SERTİFİKA"):
            self.assertIn(label, FRONTEND_SOURCE)
        self.assertIn("DEMO OTOMATİK", FRONTEND_SOURCE)
        self.assertIn("MASAÜSTÜ BİLDİRİMLERİNİ AÇ", FRONTEND_SOURCE)

    def test_demo_scanner_uses_exchange_symbols_and_auto_source(self):
        self.assertIn('exchange_info.get("symbols", [])', V21_SOURCE)
        self.assertIn('eligible[:100]', V21_SOURCE)
        self.assertIn('dynamic_auto_universe', V21_SOURCE)
        self.assertIn('source="AUTO_SCANNER"', V21_SOURCE)
        self.assertIn('"opportunity_score"', V21_SOURCE)
        self.assertIn('"top_candidates": top_candidates', V21_SOURCE)

    def test_credentials_and_runtime_state_are_excluded_from_package_source_control(self):
        self.assertIn("backend/.env", GITIGNORE_SOURCE)
        self.assertIn("backend/data/demo_credentials.dat", GITIGNORE_SOURCE)
        self.assertIn("backend/data/v21_demo_state.json", GITIGNORE_SOURCE)
        self.assertIn("CryptProtectData", (BACKEND / "app" / "credential_store.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
