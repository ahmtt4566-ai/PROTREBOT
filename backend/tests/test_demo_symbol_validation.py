import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

BACKEND = Path(__file__).parents[1]
sys.path.insert(0, str(BACKEND))

from app.binance_demo import DemoOrderRequest, BinanceDemoError, resolve_demo_symbol, symbol_rules, validate_entry_risk
from app.v21_demo import DEFAULT_SETTINGS


def exchange_info(symbol: str, *, status: str = "TRADING", contract_type: str = "PERPETUAL", quote_asset: str = "USDT") -> dict:
    return {
        "symbols": [{
            "symbol": symbol,
            "baseAsset": symbol.removesuffix("USDT"),
            "status": status,
            "contractType": contract_type,
            "quoteAsset": quote_asset,
            "filters": [
                {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "1000"},
                {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
            ],
        }],
    }


class DemoSymbolValidationTests(unittest.TestCase):
    def test_demo_symbol_normalization_formats(self):
        client = SimpleNamespace(public_get=AsyncMock(return_value=exchange_info("FLOCKUSDT")))
        for raw in ("FLOCKUSDT", "FLOCK/USDT", "FLOCK-USDT", "FLOCK_USDT", " flock/usdt "):
            self.assertEqual(asyncio.run(resolve_demo_symbol(client, raw)), "FLOCKUSDT")
        for raw in ("BTCUSDT", "ETHUSDT"):
            self.assertEqual(asyncio.run(resolve_demo_symbol(client, raw)), raw)

    def test_suffixless_symbol_requires_a_real_demo_market(self):
        client = SimpleNamespace(public_get=AsyncMock(return_value=exchange_info("FLOCKUSDT")))
        self.assertEqual(asyncio.run(resolve_demo_symbol(client, "FLOCK")), "FLOCKUSDT")
        with self.assertRaisesRegex(BinanceDemoError, "USDT vadeli"):
            asyncio.run(resolve_demo_symbol(client, "FAKECOIN"))

    def test_valid_demo_usdt_perpetual_symbols_pass_exchange_validation(self):
        for symbol in ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "FLOCKUSDT", "RAYUSDT"):
            client = SimpleNamespace(public_get=AsyncMock(return_value=exchange_info(symbol)))
            try:
                asyncio.run(symbol_rules(client, symbol))
            except BinanceDemoError as error:
                self.fail(f"{symbol} was rejected as a valid Demo market: {error}")

    def test_non_perpetual_demo_symbol_gets_market_validation_error(self):
        client = SimpleNamespace(public_get=AsyncMock(return_value=exchange_info("BODUSDT", contract_type="SPOT")))
        with self.assertRaisesRegex(BinanceDemoError, "USDT perpetual market"):
            asyncio.run(symbol_rules(client, "BODUSDT"))

    def test_empty_default_allowlist_does_not_block_valid_symbol(self):
        body = DemoOrderRequest(symbol="BODUSDT", direction="LONG", margin_usdt=20, leverage=2, stop_loss=99, tp1=101, tp2=102, tp3=103)
        spec = {"notional_usdt": 40, "current_price": 100, "stop_loss": "99"}
        settings = dict(DEFAULT_SETTINGS)
        validate_entry_risk(
            {"positions": [], "open_orders": [], "available_balance": 1000},
            body,
            spec,
            settings,
            daily_realized_pnl=0,
        )

    def test_auto_trade_universe_does_not_restrict_manual_default_validation(self):
        body = DemoOrderRequest(symbol="FLOCKUSDT", direction="LONG", margin_usdt=20, leverage=2, stop_loss=99, tp1=101, tp2=102, tp3=103)
        validate_entry_risk(
            {"positions": [], "open_orders": [], "available_balance": 1000},
            body,
            {"notional_usdt": 40, "current_price": 100, "stop_loss": "99"},
            {**DEFAULT_SETTINGS, "_auto_universe": ["BTCUSDT"]},
            daily_realized_pnl=0,
            use_auto_universe=False,
        )

    def test_manual_order_ignores_explicit_allowlist(self):
        body = DemoOrderRequest(symbol="ETHUSDT", direction="LONG", margin_usdt=20, leverage=2, stop_loss=99, tp1=101, tp2=102, tp3=103)
        validate_entry_risk(
            {"positions": [], "open_orders": [], "available_balance": 1000},
            body,
            {"notional_usdt": 40, "current_price": 100, "stop_loss": "99"},
            {**DEFAULT_SETTINGS, "allowed_symbols": ["BTCUSDT"]},
            daily_realized_pnl=0,
            use_auto_universe=False,
        )

    def test_manual_order_ignores_legacy_allowlist_for_rayusdt(self):
        body = DemoOrderRequest(symbol="RAYUSDT", direction="LONG", margin_usdt=20, leverage=2, stop_loss=99, tp1=101, tp2=102, tp3=103)
        validate_entry_risk(
            {"positions": [], "open_orders": [], "available_balance": 1000},
            body,
            {"notional_usdt": 40, "current_price": 100, "stop_loss": "99"},
            {**DEFAULT_SETTINGS, "allowed_symbols": ["BTCUSDT", "ETHUSDT"]},
            daily_realized_pnl=0,
            use_auto_universe=False,
        )

    def test_auto_trade_universe_still_restricts_candidates(self):
        body = DemoOrderRequest(symbol="RAYUSDT", direction="LONG", margin_usdt=20, leverage=2, stop_loss=99, tp1=101, tp2=102, tp3=103)
        with self.assertRaisesRegex(BinanceDemoError, "izinli pariteler"):
            validate_entry_risk(
                {"positions": [], "open_orders": [], "available_balance": 1000},
                body,
                {"notional_usdt": 40, "current_price": 100, "stop_loss": "99"},
                {**DEFAULT_SETTINGS, "_auto_universe": ["BTCUSDT"]},
                daily_realized_pnl=0,
                use_auto_universe=True,
            )

    def test_existing_notional_limit_still_blocks(self):
        body = DemoOrderRequest(symbol="BTCUSDT", direction="LONG", margin_usdt=20, leverage=2, stop_loss=99, tp1=101, tp2=102, tp3=103)
        with self.assertRaisesRegex(BinanceDemoError, "notional"):
            validate_entry_risk(
                {"positions": [], "open_orders": [], "available_balance": 1000},
                body,
                {"notional_usdt": 10_000, "current_price": 100, "stop_loss": "99"},
                dict(DEFAULT_SETTINGS),
                daily_realized_pnl=0,
            )