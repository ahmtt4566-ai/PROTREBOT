import ast
import hashlib
import hmac
import json
import re
import unittest
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from pathlib import Path
from urllib.parse import urlencode


ROOT = Path(__file__).parents[2]
SOURCE_PATH = ROOT / "backend" / "app" / "binance_demo.py"
SOURCE_TEXT = SOURCE_PATH.read_text(encoding="utf-8")
V21_SOURCE = (ROOT / "backend" / "app" / "v21_demo.py").read_text(encoding="utf-8")
ROOT_SOURCE_TEXT = (ROOT / "binance_demo.py").read_text(encoding="utf-8")
MAIN_TEXT = (ROOT / "backend" / "app" / "main.py").read_text(encoding="utf-8")
FRONTEND_TEXT = (ROOT / "frontend" / "src" / "BinanceDemo.tsx").read_text(encoding="utf-8")
PRODUCTION_FRONTEND_TEXT = (ROOT / "BinanceDemo.tsx").read_text(encoding="utf-8")
STYLE_TEXT = (ROOT / "frontend" / "src" / "binance-demo.css").read_text(encoding="utf-8")
CONFIG_TEXT = (ROOT / "backend" / "configure_demo.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE_TEXT)


def load_core():
    wanted = {
        "BinanceDemoError", "signed_query", "decimal_text", "floor_step", "round_tick", "normalize_symbol",
        "response_rows", "validate_levels", "verify_leverage_response", "verify_symbol_configuration",
        "set_isolated_margin", "apply_verified_leverage", "position_mode", "ensure_one_way_position_mode",
        "update_position_lifecycle", "position_amount", "position_risk_summary", "reconcile_demo_plans", "mark_cancelled_protection", "duplicate_entry_reason",
        "close_symbol_position",
    }
    nodes = [node for node in TREE.body if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted]
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    namespace = {
        "hashlib": hashlib,
        "hmac": hmac,
        "urlencode": urlencode,
        "Decimal": Decimal,
        "ROUND_DOWN": ROUND_DOWN,
        "ROUND_HALF_UP": ROUND_HALF_UP,
        "DEMO_REST_BASE": "https://demo-fapi.binance.com",
        "re": re,
        "Any": object,
        "utc_now": lambda: "2026-08-31T00:00:00+00:00",
    }
    module = ast.fix_missing_locations(ast.Module(body=[future, *nodes], type_ignores=[]))
    exec(compile(module, str(SOURCE_PATH), "exec"), namespace)
    return namespace


CORE = load_core()


class V203BinanceDemoSafetyTests(unittest.TestCase):
    def test_reconciliation_removes_stale_plan_but_preserves_real_positions(self):
        reconcile = CORE["reconcile_demo_plans"]
        state = {"plans": {
            "stale": {"symbol": "BTCUSDT", "status": "OPEN", "position_status": "OPEN", "remaining_quantity": "3"},
            "real": {"symbol": "ETHUSDT", "status": "OPEN", "position_status": "OPEN", "remaining_quantity": "4"},
        }}
        result = reconcile(state, {"positions": [{"symbol": "ETHUSDT", "quantity": 4}]})
        self.assertEqual(result["reconciled_active_positions"], 1)
        self.assertEqual(result["stale_positions_removed"], 1)
        self.assertEqual(state["plans"]["stale"]["position_status"], "CLOSED")
        self.assertEqual(state["plans"]["real"]["position_status"], "OPEN")

    def test_raw_zero_and_restored_local_position_never_count_as_reconciled(self):
        reconcile = CORE["reconcile_demo_plans"]
        state = {"plans": {"restored": {"symbol": "BTCUSDT", "status": "OPEN", "position_status": "OPEN", "remaining_quantity": "0.001"}}}
        result = reconcile(state, {"positions": []})
        self.assertEqual(result["actual_exchange_open_positions"], 0)
        self.assertEqual(result["reconciled_active_positions"], 0)
        self.assertEqual(result["stale_positions_removed"], 1)

    def test_raw_exchange_position_amount_zero_vs_nonzero_is_explicit(self):
        summarize = CORE["position_risk_summary"]
        for raw_amount, expected in ((None, 0), (0, 0), ("0.000", 0), ("0.001", 1), ("-0.001", 1)):
            result = summarize([{"symbol": "BTCUSDT", "positionAmt": raw_amount}])
            self.assertEqual(result["actual_exchange_open_positions"], expected)
            self.assertEqual(result["exchange_position_diagnostics"][0]["positionAmt"], str(Decimal(str(raw_amount)) if raw_amount is not None else Decimal("0")))

    def test_raw_position_risk_fields_are_returned_without_local_state(self):
        result = CORE["position_risk_summary"]([{
            "symbol": "BTCUSDT", "positionAmt": "-0.001", "positionSide": "BOTH",
            "markPrice": "65000", "entryPrice": "64000", "unRealizedProfit": "-1.25",
        }])
        self.assertEqual(result["raw_position_risk_count"], 1)
        self.assertEqual(result["actual_exchange_open_positions"], 1)
        self.assertEqual(result["exchange_position_diagnostics"][0]["positionSide"], "BOTH")
        self.assertEqual(result["exchange_position_diagnostics"][0]["unrealizedProfit"], "-1.25")

    def test_reconciled_position_count_matches_zero_one_and_three_exchange_positions(self):
        reconcile = CORE["reconcile_demo_plans"]
        state = {"plans": {}}
        for count in (0, 1, 3):
            positions = [{"symbol": f"SAMPLE{index}USDT", "quantity": 1} for index in range(count)]
            result = reconcile(state, {"positions": positions})
            self.assertEqual(result["actual_exchange_open_positions"], count)
            self.assertEqual(result["reconciled_active_positions"], count)

    def test_position_limit_uses_exchange_reconciled_count_not_stale_plans(self):
        self.assertIn("validate_entry_risk(", SOURCE_TEXT)
        self.assertIn("pending_entries", SOURCE_TEXT)
        self.assertNotIn('if len(snapshot["positions"]) >= MAX_OPEN_POSITIONS:', SOURCE_TEXT)
        self.assertIn('"exchange_position_diagnostics"', SOURCE_TEXT)
        self.assertIn('"raw_position_risk_count"', SOURCE_TEXT)

    def test_all_demo_position_cards_use_canonical_reconciled_count(self):
        self.assertIn("account?.reconciliation?.reconciled_active_positions", PRODUCTION_FRONTEND_TEXT)
        self.assertIn("accountRefreshId", PRODUCTION_FRONTEND_TEXT)
        self.assertIn("requestId !== accountRefreshId.current", PRODUCTION_FRONTEND_TEXT)
        self.assertNotIn("account?.positions.length ?? 0} /", PRODUCTION_FRONTEND_TEXT)

    def test_v21_summary_exposes_reconciled_count(self):
        self.assertIn('"reconciled_active_positions"', V21_SOURCE)

    def test_analysis_direction_mapping_has_no_short_fallback(self):
        for value in ("LONG", "SHORT", "BUY", "SELL"):
            self.assertIn(f"normalized === '{value}'", FRONTEND_TEXT)
        self.assertIn("return null", FRONTEND_TEXT)
        self.assertIn("Yön belirlenemedi; mevcut yön korunuyor.", FRONTEND_TEXT)
        self.assertNotIn("|| 'SHORT'", FRONTEND_TEXT)

    def test_production_frontend_uses_reconciled_count_and_analysis_mapping(self):
        self.assertIn("reconciled_active_positions", PRODUCTION_FRONTEND_TEXT)
        self.assertIn("normalizeAnalysisPlan", PRODUCTION_FRONTEND_TEXT)
        self.assertIn("nested.normalized_signal", PRODUCTION_FRONTEND_TEXT)
        self.assertIn('data-build-marker="BUILD_COMMIT"', PRODUCTION_FRONTEND_TEXT)

    def test_manual_demo_order_preserves_request_scoped_testnet_credentials(self):
        self.assertIn('execute_demo_order(request.app, body, source="MANUAL", request=request)', SOURCE_TEXT)
        self.assertIn('request: Request | None = None', SOURCE_TEXT)
        self.assertIn('client_for(request) if request is not None', SOURCE_TEXT)
        self.assertIn('BinanceDemoClient(application.state.http, *load_demo_credentials())', SOURCE_TEXT)

    def test_demo_confirmations_are_browser_compatible_and_backend_confirmations_remain(self):
        self.assertNotIn("window.prompt", FRONTEND_TEXT)
        self.assertIn("demoConfirmationBackdrop", FRONTEND_TEXT)
        self.assertIn("confirmation:'DEMO'", FRONTEND_TEXT)
        self.assertIn("confirmation:'DEMO KAPAT'", FRONTEND_TEXT)
        self.assertIn("confirmation:'DEMO ACİL DURDUR'", FRONTEND_TEXT)
        self.assertIn('body.confirmation.strip().upper() != "DEMO"', SOURCE_TEXT)

    def test_demo_account_configured_flag_uses_request_scoped_credentials(self):
        self.assertIn('result["configured"] = credentials_configured(request)', SOURCE_TEXT)
        self.assertNotIn('return {**public_status(state), **snapshot, "plans":', SOURCE_TEXT)

    def test_analysis_fill_uses_only_directional_plans_and_refreshes_scanner_candidates(self):
        self.assertIn("isCompleteAnalysisPlan(plan)", PRODUCTION_FRONTEND_TEXT)
        self.assertIn("/scanner/candidates", PRODUCTION_FRONTEND_TEXT)
        self.assertIn("Güncel analiz BEKLE durumunda", PRODUCTION_FRONTEND_TEXT)
        self.assertIn("Güncel analizde güvenli LONG/SHORT planı veya geçerli V21 adayı bulunamadı.", PRODUCTION_FRONTEND_TEXT)
        self.assertNotIn("direction: 'SHORT'", PRODUCTION_FRONTEND_TEXT)

    def test_analysis_progress_tracks_real_fetch_stages_and_errors(self):
        testnet_source = (ROOT / "TestnetFirstApp.tsx").read_text(encoding="utf-8")
        for progress in (5, 45, 85, 100, -1):
            self.assertIn(f"onAnalysisProgress?.({progress})", testnet_source)
        self.assertIn("analysisProgress > 0", testnet_source)
        self.assertIn("ANALİZ HATASI", testnet_source)
        self.assertIn("onAnalysisProgress={setAnalysisProgress}", testnet_source)

    def test_root_vercel_build_chain_is_explicit_and_deterministic(self):
        package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
        vercel = json.loads((ROOT / "vercel.json").read_text(encoding="utf-8"))
        self.assertEqual(package["scripts"]["build"], "node ./tools/build-frontend.mjs")
        self.assertEqual(vercel["buildCommand"], "npm run build")
        self.assertEqual(vercel["outputDirectory"], "dist")
        self.assertEqual(vercel["framework"], "vite")
        self.assertIn("VITE_BUILD_COMMIT", (ROOT / "vite.config.ts").read_text(encoding="utf-8"))

    def test_connector_is_hard_locked_to_official_demo_hosts(self):
        self.assertIn('DEMO_REST_BASE = "https://demo-fapi.binance.com"', SOURCE_TEXT)
        self.assertIn('DEMO_WS_BASE = "wss://demo-fstream.binance.com"', SOURCE_TEXT)
        self.assertNotIn('"https://fapi.binance.com"', SOURCE_TEXT)
        self.assertNotIn('"wss://fstream.binance.com"', SOURCE_TEXT)
        self.assertIn("real_trading_locked", SOURCE_TEXT)

    def test_hmac_signature_is_deterministic_and_matches_sha256(self):
        params = {"symbol": "ETHUSDT", "side": "BUY", "timestamp": 123456789, "recvWindow": 5000}
        query, signature = CORE["signed_query"]("demo-secret", params)
        expected_query = urlencode(list(params.items()))
        expected = hmac.new(b"demo-secret", expected_query.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(query, expected_query)
        self.assertEqual(signature, expected)

    def test_quantity_and_price_rounding_never_inflate_quantity(self):
        floor_step = CORE["floor_step"]
        round_tick = CORE["round_tick"]
        self.assertEqual(floor_step(Decimal("1.239"), Decimal("0.01")), Decimal("1.23"))
        self.assertEqual(round_tick(Decimal("64.126"), Decimal("0.05")), Decimal("64.15"))

    def test_direction_levels_are_strictly_ordered(self):
        validate = CORE["validate_levels"]
        validate("LONG", Decimal("100"), Decimal("98"), [Decimal("102"), Decimal("104"), Decimal("106")])
        validate("SHORT", Decimal("100"), Decimal("102"), [Decimal("98"), Decimal("96"), Decimal("94")])
        with self.assertRaises(CORE["BinanceDemoError"]):
            validate("LONG", Decimal("100"), Decimal("101"), [Decimal("102"), Decimal("104"), Decimal("106")])

    def test_symbol_sanitizer_only_accepts_usdt_contracts(self):
        normalize = CORE["normalize_symbol"]
        self.assertEqual(normalize("eth/usdt"), "ETHUSDT")
        with self.assertRaises(CORE["BinanceDemoError"]):
            normalize("BTCUSD")

    def test_manual_arm_and_fixed_risk_caps_are_present(self):
        self.assertIn('MAX_MARGIN_USDT = Decimal("100")', SOURCE_TEXT)
        self.assertIn("MAX_LEVERAGE = 2", SOURCE_TEXT)
        self.assertIn("ARM_SECONDS = 10 * 60", SOURCE_TEXT)
        self.assertIn('body.confirmation.strip().upper() != "DEMO"', SOURCE_TEXT)
        self.assertIn("if not armed(state)", SOURCE_TEXT)
        self.assertIn("reduceOnly", SOURCE_TEXT)

    def test_leverage_and_isolated_margin_are_verified_before_entry(self):
        leverage = CORE["verify_leverage_response"](
            {"symbol": "BTCUSDT", "leverage": 2, "maxNotionalValue": "50000000"},
            "BTCUSDT",
            2,
        )
        self.assertTrue(leverage["leverage_verified"])
        self.assertEqual(leverage["applied_leverage"], 2)
        configuration = CORE["verify_symbol_configuration"](
            [{"symbol": "BTCUSDT", "marginType": "ISOLATED", "leverage": 2, "maxNotionalValue": "50000000"}],
            "BTCUSDT",
            2,
        )
        self.assertEqual(configuration["margin_type"], "isolated")
        with self.assertRaises(CORE["BinanceDemoError"]):
            CORE["verify_leverage_response"]({"symbol": "BTCUSDT", "leverage": 1}, "BTCUSDT", 2)
        with self.assertRaises(CORE["BinanceDemoError"]):
            CORE["verify_symbol_configuration"](
                [{"symbol": "BTCUSDT", "marginType": "CROSSED", "leverage": 2}], "BTCUSDT", 2,
            )

    def test_position_v3_missing_fields_are_never_invented_as_1x_cross(self):
        self.assertIn('("GET", "/fapi/v1/symbolConfig")', SOURCE_TEXT)
        self.assertIn('("POST", "/fapi/v1/marginType")', SOURCE_TEXT)
        self.assertIn('"marginType": "ISOLATED"', SOURCE_TEXT)
        self.assertNotIn('int(item.get("leverage", 1))', SOURCE_TEXT)
        self.assertNotIn('item.get("marginType", "cross")', SOURCE_TEXT)

    def test_entry_post_is_not_blindly_retried_on_unknown_status(self):
        self.assertIn("unknown_execution", SOURCE_TEXT)
        self.assertIn("find_order_by_client_id", SOURCE_TEXT)
        self.assertIn('for attempt in range(2 if method == "GET" else 1)', SOURCE_TEXT)
        self.assertIn('if method != "GET" or exc.exchange_code != -1021', SOURCE_TEXT)
        self.assertIn("origClientOrderId", SOURCE_TEXT)

    def test_connect_only_resyncs_after_timestamp_rejection(self):
        self.assertIn("async def connect_snapshot", SOURCE_TEXT)
        self.assertIn("if exc.exchange_code != -1021", SOURCE_TEXT)
        self.assertIn("await client.sync_clock(force=True)", SOURCE_TEXT)
        self.assertIn("snapshot = await connect_snapshot(client)", SOURCE_TEXT)

    def test_clock_sync_is_process_serialized_and_shared(self):
        self.assertIn("DEMO_CLOCK_LOCK = asyncio.Lock()", SOURCE_TEXT)
        self.assertIn("DEMO_CLOCK_OFFSET_MS = 0", SOURCE_TEXT)
        self.assertIn("async with DEMO_CLOCK_LOCK", SOURCE_TEXT)
        self.assertIn("DEMO_CLOCK_SYNCED_AT > request_started", SOURCE_TEXT)

    def test_account_snapshot_reads_are_sequential_and_algo_orders_optional(self):
        self.assertIn('await snapshot_request(client, "/fapi/v3/account"', SOURCE_TEXT)
        self.assertIn('await snapshot_request(client, "/fapi/v3/positionRisk"', SOURCE_TEXT)
        self.assertIn('await snapshot_request(client, "/fapi/v1/openOrders"', SOURCE_TEXT)
        self.assertIn('await snapshot_request(client, "/fapi/v1/openAlgoOrders"', SOURCE_TEXT)
        self.assertIn('await snapshot_request(client, "/fapi/v1/symbolConfig"', SOURCE_TEXT)
        self.assertNotIn("account, positions, orders, algo_orders, hedge_mode, configurations = await asyncio.gather(", SOURCE_TEXT)

    def test_analysis_universe_supports_full_100_symbol_market_scope(self):
        main_source = (ROOT / "backend" / "app" / "main.py").read_text(encoding="utf-8")
        frontend_source = (ROOT / "CoinAnalysisCenter.tsx").read_text(encoding="utf-8")
        self.assertIn('limit: int = Query(100, ge=1, le=100)', main_source)
        self.assertIn('/analysis-universe?interval=${interval}&limit=100', frontend_source)
        self.assertNotIn('slice(0, 12)', frontend_source)
        self.assertNotIn('slice(0,12)', frontend_source)

    def test_position_lifecycle_tracks_partial_targets_and_full_close(self):
        lifecycle = CORE["update_position_lifecycle"]
        plan = {"position_id": "demo-123", "initial_quantity": "10", "quantity": "10"}
        lifecycle(plan, Decimal("10"))
        self.assertEqual(plan["position_status"], "OPEN")
        self.assertEqual(plan["remaining_quantity"], "10")
        lifecycle(plan, Decimal("7"))
        self.assertEqual(plan["tp1_status"], "FILLED")
        self.assertEqual(plan["position_id"], "demo-123")
        lifecycle(plan, Decimal("4"))
        self.assertEqual(plan["tp2_status"], "FILLED")
        lifecycle(plan, Decimal("0"))
        self.assertEqual(plan["position_status"], "CLOSED")
        self.assertEqual(plan["tp3_status"], "FILLED")

    def test_cancelled_stop_is_persistently_marked_for_reconciliation(self):
        plans = {"demo-123": {"symbol": "BTCUSDT", "stop_algo_id": 77, "status": "OPEN"}}
        plan = CORE["mark_cancelled_protection"](plans, "BTCUSDT", 77)
        self.assertIs(plan, plans["demo-123"])
        self.assertTrue(plan["stop_protection_cancelled"])
        self.assertEqual(plan["status"], "KORUMA İPTAL")
        self.assertIsNone(CORE["mark_cancelled_protection"](plans, "BTCUSDT", 78))

    def test_duplicate_entry_reason_distinguishes_position_and_normal_order(self):
        helper = CORE["duplicate_entry_reason"]
        self.assertIn("açık pozisyon", helper({"positions": [{"symbol": "BTCUSDT"}], "open_orders": []}, "BTCUSDT"))
        message = helper({"positions": [], "open_orders": [{"symbol": "BTCUSDT", "orderId": 42}]}, "BTCUSDT")
        self.assertIn("normal emir", message)
        self.assertIn("42", message)
        self.assertIsNone(helper({"positions": [], "open_orders": []}, "BTCUSDT"))

    def test_close_position_selects_position_side_and_preserves_reduce_only_rules(self):
        source = SOURCE_TEXT
        self.assertIn('"position_side": str(item.get("positionSide") or "BOTH").upper()', source)
        self.assertIn('if normalized_side != "BOTH":', source)
        self.assertIn('params.pop("reduceOnly", None)', source)

    def test_credentials_stay_server_side_and_out_of_user_interface(self):
        self.assertIn("getpass.getpass", CONFIG_TEXT)
        self.assertIn("PANODAN YAPIŞTIR", CONFIG_TEXT)
        self.assertIn("backend/.env", (ROOT / "V20-3-BINANCE-FUTURES-DEMO.md").read_text(encoding="utf-8"))
        self.assertNotIn("BINANCE_DEMO_SECRET_KEY", FRONTEND_TEXT)
        self.assertNotIn("secret_key", FRONTEND_TEXT.lower())
        self.assertIn("backend/.env", (ROOT / ".gitignore").read_text(encoding="utf-8"))

    def test_main_router_and_modern_demo_panel_are_integrated(self):
        self.assertIn("binance_demo_router", MAIN_TEXT)
        self.assertIn('version="25.0.0"', MAIN_TEXT)
        self.assertIn("Binance Futures Demo Komuta Merkezi", FRONTEND_TEXT)
        self.assertIn("demoLiveChart", FRONTEND_TEXT)
        self.assertIn("apiErrorMessage", FRONTEND_TEXT)
        self.assertIn("parsed <= 100", FRONTEND_TEXT)
        self.assertIn("PositionMap", FRONTEND_TEXT)
        self.assertIn("ACİL DEMO DURDUR", FRONTEND_TEXT)
        self.assertIn("KALDIRAÇ VE MARJİN DENETİMİ", FRONTEND_TEXT)
        self.assertIn("İstenen kaldıraç", FRONTEND_TEXT)
        self.assertIn("demoTicketFeedback", FRONTEND_TEXT)
        self.assertIn('traced_stage("ensure_one_way_position_mode"', SOURCE_TEXT)
        self.assertIn("await ensure_one_way_position_mode(client)", ROOT_SOURCE_TEXT)
        self.assertIn(".demoPositionMap", STYLE_TEXT)
        self.assertIn(".appShell.view-v20-demo>.binanceDemoDeck", STYLE_TEXT)


class V203BinanceDemoAsyncSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_way_mode_rejects_without_cancelling_existing_protection(self):
        class FakeClient:
            def __init__(self):
                self.calls = []
                self.hedge = True
                self.orders = [{"symbol": "BTCUSDT", "orderId": 11}]
                self.algo_orders = [{"symbol": "ETHUSDT", "algoId": 22}]

            async def signed(self, method, path, params=None):
                self.calls.append((method, path, params or {}))
                if path == "/fapi/v1/positionSide/dual" and method == "GET":
                    return {"dualSidePosition": self.hedge}
                if path == "/fapi/v3/positionRisk":
                    return []
                if path == "/fapi/v1/openOrders":
                    return list(self.orders)
                if path == "/fapi/v1/openAlgoOrders":
                    return list(self.algo_orders)
                if path == "/fapi/v1/order" and method == "DELETE":
                    self.orders.clear()
                    return {"orderId": 11, "status": "CANCELED"}
                if path == "/fapi/v1/algoOrder" and method == "DELETE":
                    self.algo_orders.clear()
                    return {"algoId": 22, "algoStatus": "CANCELED"}
                if path == "/fapi/v1/positionSide/dual" and method == "POST":
                    self.hedge = False
                    return {"dualSidePosition": False}
                raise AssertionError((method, path, params))

        client = FakeClient()
        with self.assertRaises(CORE["BinanceDemoError"]):
            await CORE["ensure_one_way_position_mode"](client)
        self.assertFalse(any(call[0] == "DELETE" for call in client.calls))
        self.assertEqual(client.orders, [{"symbol": "BTCUSDT", "orderId": 11}])
        self.assertEqual(client.algo_orders, [{"symbol": "ETHUSDT", "algoId": 22}])

    async def test_one_way_mode_is_noop_when_already_one_way(self):
        class FakeClient:
            async def signed(self, method, path, params=None):
                self.calls = getattr(self, "calls", 0) + 1
                return {"dualSidePosition": False}

        client = FakeClient()
        self.assertEqual(await CORE["ensure_one_way_position_mode"](client), 0)
        self.assertEqual(client.calls, 1)

    async def test_one_way_mode_rejects_failed_transition_without_touching_orders(self):
        class FakeClient:
            def __init__(self):
                self.hedge = True
                self.mode_attempts = 0
                self.orders = [{"symbol": "BTCUSDT", "orderId": 11}]

            async def signed(self, method, path, params=None):
                if path == "/fapi/v1/positionSide/dual" and method == "GET":
                    return {"dualSidePosition": self.hedge}
                if path == "/fapi/v3/positionRisk":
                    return []
                if path == "/fapi/v1/openOrders":
                    return list(self.orders)
                if path == "/fapi/v1/openAlgoOrders":
                    return []
                if path == "/fapi/v1/order" and method == "DELETE":
                    self.orders.clear()
                    return {"orderId": 11, "status": "CANCELED"}
                if path == "/fapi/v1/positionSide/dual" and method == "POST":
                    self.mode_attempts += 1
                    if self.mode_attempts == 1:
                        raise CORE["BinanceDemoError"]("open orders", exchange_code=-4067)
                    self.hedge = False
                    return {"dualSidePosition": False}
                raise AssertionError((method, path, params))

        client = FakeClient()
        with self.assertRaises(CORE["BinanceDemoError"]):
            await CORE["ensure_one_way_position_mode"](client)
        self.assertEqual(client.mode_attempts, 0)
        self.assertEqual(client.orders, [{"symbol": "BTCUSDT", "orderId": 11}])

    async def test_isolated_margin_then_exact_leverage_and_symbol_config_are_required(self):
        class FakeClient:
            def __init__(self):
                self.calls = []

            async def signed(self, method, path, params=None):
                self.calls.append((method, path, params or {}))
                if path == "/fapi/v1/marginType":
                    return {"code": 200, "msg": "success"}
                if path == "/fapi/v1/leverage":
                    return {"symbol": "BTCUSDT", "leverage": 2, "maxNotionalValue": "50000000"}
                if path == "/fapi/v1/symbolConfig":
                    return [{"symbol": "BTCUSDT", "marginType": "ISOLATED", "leverage": 2}]
                raise AssertionError(path)

        client = FakeClient()
        await CORE["set_isolated_margin"](client, "BTCUSDT")
        audit = await CORE["apply_verified_leverage"](client, "BTCUSDT", 2)
        self.assertEqual(audit["applied_leverage"], 2)
        self.assertEqual(audit["margin_type"], "isolated")
        self.assertEqual([call[1] for call in client.calls], [
            "/fapi/v1/marginType", "/fapi/v1/leverage", "/fapi/v1/symbolConfig",
        ])

    async def test_already_isolated_exchange_code_is_safe_and_other_errors_fail(self):
        class FakeClient:
            def __init__(self, code):
                self.code = code

            async def signed(self, method, path, params=None):
                raise CORE["BinanceDemoError"]("margin response", exchange_code=self.code)

        await CORE["set_isolated_margin"](FakeClient(-4046), "BTCUSDT")
        with self.assertRaises(CORE["BinanceDemoError"]):
            await CORE["set_isolated_margin"](FakeClient(-4047), "BTCUSDT")


if __name__ == "__main__":
    unittest.main(verbosity=2)
