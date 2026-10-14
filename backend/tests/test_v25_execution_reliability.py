import asyncio
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


sys.path.insert(0, str(Path(__file__).parents[1]))

from app import v25_execution as execution  # noqa: E402
from app import original_v2_live  # noqa: E402


def application_for_loop():
    state = execution.initial_state()
    state["recovery_loaded"] = True
    state["lock"] = asyncio.Lock()
    return SimpleNamespace(state=SimpleNamespace(v25_execution=state, db_pool=None)), state


STOP_PARAMS = {
    "symbol": "BTCUSDT", "side": "SELL", "type": "STOP_MARKET",
    "triggerPrice": "49000", "closePosition": "true",
    "clientAlgoId": execution.client_id_for("SL", "offline-stop"),
}


class StopClient(execution.BinanceLiveClient):
    application = None

    def __init__(self, *, rejections=0, ambiguous=False, verification="valid", first_read_error=None):
        self.rejections = rejections
        self.ambiguous = ambiguous
        self.verification = verification
        self.first_read_error = first_read_error
        self.posts = []
        self.reads = 0
        self.active = False

    async def signed(self, method, path, params=None):
        if (method, path) == ("POST", "/fapi/v1/algoOrder"):
            self.posts.append(dict(params))
            if len(self.posts) <= self.rejections:
                raise execution.LiveExchangeError("Mock known rejection", exchange_code=-4000)
            if self.ambiguous:
                raise execution.LiveExchangeError("Mock POST timeout", unknown_execution=True)
            self.active = True
            return {"algoId": 101}
        if (method, path) == ("GET", "/fapi/v1/openAlgoOrders"):
            self.reads += 1
            if self.reads == 1 and self.first_read_error:
                raise self.first_read_error
            if self.verification == "unknown":
                return {"invalid": "snapshot"}
            if not self.active or self.verification == "absent":
                return []
            row = {**STOP_PARAMS, "algoId": 101, "algoStatus": "NEW"}
            if self.verification == "foreign":
                row["clientAlgoId"] = "MANUAL_FOREIGN"
            elif self.verification == "wrong_shape":
                row["closePosition"] = "false"
            return [row]
        raise AssertionError(f"Unexpected mock request: {method} {path}")


class ExecutionReliabilityTests(unittest.TestCase):
    def setUp(self):
        persistence = patch.object(execution, "persist_state")
        persistence.start()
        self.addCleanup(persistence.stop)

    def test_nontransient_exchange_errors_back_off_and_loop_reaches_success(self):
        application, state = application_for_loop()
        sleeps = []
        reconcile = AsyncMock(side_effect=[
            execution.LiveExchangeError("Mock exchange failure"),
            execution.LiveExchangeError("Mock second failure"), None,
        ])

        async def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) <= 2:
                self.assertIsNone(state["heartbeat"].get("last_successful_cycle"))
            else:
                raise asyncio.CancelledError

        with patch.multiple(
            execution,
            auto_session_credentials=AsyncMock(return_value=("OFFLINE_KEY_PLACEHOLDER", "OFFLINE_SECRET_PLACEHOLDER")),
            reconcile=reconcile, automatic_cycle=AsyncMock(), persist_state=lambda *_: None,
        ), patch.object(execution.asyncio, "sleep", side_effect=sleep):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(execution.execution_loop(application))
        self.assertEqual(reconcile.await_count, 3)
        self.assertEqual(sleeps, [5, 10, execution.RECONCILE_SECONDS])
        self.assertIsNotNone(state["heartbeat"]["last_successful_cycle"])
        self.assertEqual(state["connection"]["retry_count"], 0)
        self.assertEqual(len([event for event in state["events"] if event["kind"] == "RECONCILIATION_FAILURE_DIAGNOSTIC"]), 2)

    def test_unknown_exchange_error_keeps_fail_closed_state_while_loop_continues(self):
        application, state = application_for_loop()
        reconcile = AsyncMock(side_effect=[execution.LiveExchangeError("Mock unknown execution", unknown_execution=True), None])
        sleeps = []

        async def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 2:
                raise asyncio.CancelledError

        with patch.multiple(
            execution,
            auto_session_credentials=AsyncMock(return_value=("OFFLINE_KEY_PLACEHOLDER", "OFFLINE_SECRET_PLACEHOLDER")),
            reconcile=reconcile, automatic_cycle=AsyncMock(),
        ), patch.object(execution.asyncio, "sleep", side_effect=sleep):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(execution.execution_loop(application))
        self.assertEqual(reconcile.await_count, 2)
        self.assertTrue(state["real_trading_locked"])
        self.assertEqual(state["execution_state"], "UNKNOWN")
        self.assertFalse(state["auto"]["enabled"])

    def test_generic_error_continues_but_error_cycle_does_not_advance_heartbeat(self):
        application, state = application_for_loop()
        reconcile = AsyncMock(side_effect=[RuntimeError("Mock failure"), None])
        sleeps = []

        async def failed_cycle(*args, **kwargs):
            state["auto"]["last_cycle_stage"] = "error"

        async def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 2:
                raise asyncio.CancelledError

        with patch.multiple(
            execution,
            auto_session_credentials=AsyncMock(return_value=("OFFLINE_KEY_PLACEHOLDER", "OFFLINE_SECRET_PLACEHOLDER")),
            reconcile=reconcile, automatic_cycle=failed_cycle,
        ), patch.object(execution.asyncio, "sleep", side_effect=sleep):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(execution.execution_loop(application))
        self.assertEqual(reconcile.await_count, 2)
        self.assertIsNone(state["heartbeat"]["last_successful_cycle"])

    def test_heartbeat_is_in_status_and_request_heartbeat_is_separate(self):
        application, state = application_for_loop()
        state["heartbeat"].update({"last_heartbeat": "request-time", "last_successful_cycle": "loop-time", "last_successful_cycle_epoch": 123})
        with patch.object(execution, "readiness", return_value={"ready": True}), \
                patch.object(execution, "consent_status", return_value={"fingerprint": None}):
            result = execution.public_status(application)
        self.assertEqual(result["heartbeat"]["last_successful_cycle"], "loop-time")
        self.assertEqual(result["heartbeat"]["last_successful_cycle_epoch"], 123)
        self.assertEqual(result["heartbeat"]["last_heartbeat"], "request-time")

    def test_last_successful_heartbeat_is_retained_after_later_failure(self):
        application, state = application_for_loop()
        reconcile = AsyncMock(side_effect=[None, execution.LiveExchangeError("Mock later failure")])
        sleeps = []
        with patch.object(execution, "now_iso", return_value="successful-cycle") as clock:
            async def sleep(seconds):
                sleeps.append(seconds)
                if len(sleeps) == 1:
                    clock.return_value = "failed-cycle"
                else:
                    raise asyncio.CancelledError

            with patch.multiple(
                execution,
                auto_session_credentials=AsyncMock(return_value=("OFFLINE_KEY_PLACEHOLDER", "OFFLINE_SECRET_PLACEHOLDER")),
                reconcile=reconcile, automatic_cycle=AsyncMock(),
            ), patch.object(execution.asyncio, "sleep", side_effect=sleep):
                with self.assertRaises(asyncio.CancelledError):
                    asyncio.run(execution.execution_loop(application))
        self.assertEqual(state["heartbeat"]["last_successful_cycle"], "successful-cycle")
        self.assertEqual(sleeps, [execution.RECONCILE_SECONDS, 5])

    def test_original_session_recovers_transient_market_read_without_rearm(self):
        application, state = application_for_loop()
        session_until = execution.time.time() + 300
        state["auto"].update({"enabled": True, "session_until": session_until})
        state.update(live_auto_trade=True, real_trading_locked=False)
        state["original_v2_live"] = {
            "enabled": True,
            "paused": False,
            "pause_reason": None,
            "risk_percent": 0.5,
            "session_expires_at_epoch": session_until,
        }
        failure = execution.LiveExchangeError(
            "Canlı market-data okuması geçici olarak başarısız oldu.",
            transient_read_failure=True,
            request_method="GET",
            request_path="/fapi/v3/account",
        )
        reconcile_calls = 0

        async def reconcile(*args, **kwargs):
            nonlocal reconcile_calls
            reconcile_calls += 1
            if reconcile_calls == 1:
                raise failure
            self.assertTrue(execution.recover_transient_market_data(state, "GET", "/fapi/v3/account"))

        sleeps = []

        async def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 2:
                raise asyncio.CancelledError

        with patch.multiple(
            execution,
            auto_session_credentials=AsyncMock(return_value=("OFFLINE_KEY_PLACEHOLDER", "OFFLINE_SECRET_PLACEHOLDER")),
            reconcile=reconcile,
            automatic_cycle=AsyncMock(),
        ), patch.object(execution.asyncio, "sleep", side_effect=sleep):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(execution.execution_loop(application))

        self.assertEqual(sleeps, [1, execution.RECONCILE_SECONDS])
        self.assertTrue(state["original_v2_live"]["enabled"])
        self.assertFalse(state["original_v2_live"]["paused"])
        self.assertIsNone(state["original_v2_live"]["pause_reason"])
        self.assertTrue(state["live_auto_trade"])
        self.assertFalse(state["real_trading_locked"])
        self.assertTrue(state["auto"]["enabled"])
        self.assertFalse(any(event["kind"] == "ORIGINAL_LIVE_PAUSED" for event in state["events"]))
        self.assertTrue(any(event["kind"] == "AUTO_RECOVERED_FROM_TRANSIENT_MARKET_DATA" for event in state["events"]))

    def test_income_read_timeout_skips_entries_without_cancelling_arm(self):
        application, state = application_for_loop()
        session_until = execution.time.time() + 300
        state["auto"].update({"enabled": True, "session_until": session_until})
        state.update(live_auto_trade=True, real_trading_locked=False, armed_until=session_until)
        failure = execution.LiveExchangeError(
            "Günlük gelir verisi geçici olarak okunamadı.",
            timed_out=True,
            transient_read_failure=True,
            request_method="GET",
            request_path="/fapi/v1/income",
        )

        async def stop_after_retry(_seconds):
            raise asyncio.CancelledError

        with patch.multiple(
            execution,
            auto_session_credentials=AsyncMock(return_value=("OFFLINE_KEY_PLACEHOLDER", "OFFLINE_SECRET_PLACEHOLDER")),
            reconcile=AsyncMock(),
            automatic_cycle=AsyncMock(side_effect=failure),
        ), patch.object(execution.asyncio, "sleep", side_effect=stop_after_retry):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(execution.execution_loop(application))

        self.assertEqual(state["armed_until"], session_until)
        self.assertFalse(state["real_trading_locked"])
        self.assertTrue(state["auto"]["enabled"])
        self.assertFalse(state["emergency"]["active"])
        self.assertEqual(state["auto"]["last_skip_reason"], "daily_risk_data_unavailable")
        self.assertEqual(state["auto"]["last_cycle_stage"], "skipped")
        self.assertTrue(any(event["kind"] == "LIVE_DAILY_RISK_DATA_RETRY" for event in state["events"]))
        self.assertFalse(any(event["kind"] == "RECONCILIATION_FAILURE_DIAGNOSTIC" for event in state["events"]))

    def test_transient_recovery_survives_stale_reconciliation_unknown_event(self):
        application, state = application_for_loop()
        session_until = execution.time.time() + 300
        state["auto"].update({"enabled": True, "session_until": session_until})
        state.update(live_auto_trade=True, real_trading_locked=False)
        state["original_v2_live"] = {
            "enabled": True, "paused": False, "pause_reason": None,
            "session_expires_at_epoch": session_until,
        }
        execution.add_event(
            state, "LIVE_UNKNOWN_EXECUTION", "Önceki ARM iptalinden kalan kayıt.",
            reason="RECONCILIATION_FAILURE", symbol=None, unknown_execution=True,
        )
        failure = execution.LiveExchangeError(
            "Canlı market-data okuması geçici olarak başarısız oldu.",
            transient_read_failure=True, request_method="GET", request_path="/fapi/v3/account",
        )
        self.assertTrue(execution.mark_transient_market_data_failure(state, failure))
        self.assertTrue(execution.transient_market_data_recovery_active(state))

        with patch.object(original_v2_live, "cycle", new=AsyncMock()) as cycle, \
                patch.object(original_v2_live, "running", return_value=True):
            asyncio.run(execution.automatic_cycle(application, credentials=("k", "s")))
            cycle.assert_not_awaited()

        self.assertTrue(execution.recover_transient_market_data(state, "GET", "/fapi/v3/account"))
        self.assertTrue(state["live_auto_trade"])
        self.assertFalse(state["real_trading_locked"])
        self.assertTrue(state["auto"]["enabled"])
        self.assertFalse(state["original_v2_live"]["paused"])
        self.assertFalse(any(event["kind"] == "ORIGINAL_LIVE_PAUSED" for event in state["events"]))
        self.assertTrue(any(event["kind"] == "AUTO_RECOVERED_FROM_TRANSIENT_MARKET_DATA" for event in state["events"]))

    def test_transient_window_expiry_keeps_live_locked(self):
        application, state = application_for_loop()
        session_until = execution.time.time() + 300
        state["auto"].update({"enabled": True, "session_until": session_until})
        state.update(live_auto_trade=True, real_trading_locked=False)
        failure = execution.LiveExchangeError(
            "Canlı market-data okuması geçici olarak başarısız oldu.",
            transient_read_failure=True, request_method="GET", request_path="/fapi/v3/account",
        )
        self.assertTrue(execution.mark_transient_market_data_failure(state, failure))
        state["transient_market_data"]["failed_at_epoch"] -= execution.TRANSIENT_RECONCILIATION_RECOVERY_SECONDS + 5
        self.assertFalse(execution.transient_market_data_recovery_active(state))
        self.assertFalse(execution.recover_transient_market_data(state, "GET", "/fapi/v3/account"))
        self.assertTrue(state["real_trading_locked"])
        self.assertFalse(state["live_auto_trade"])
        self.assertIsNone(state["transient_market_data"])

    def test_public_market_rate_limit_preserves_original_session_and_arm(self):
        application, state = application_for_loop()
        session_until = execution.time.time() + 300
        state["auto"].update({"enabled": True, "session_until": session_until})
        state.update(live_auto_trade=True, real_trading_locked=False)
        state["original_v2_live"] = {
            "enabled": True,
            "paused": False,
            "pause_reason": None,
            "session_expires_at_epoch": session_until,
        }
        failure = execution.LiveRateLimitError(
            "Binance piyasa verisi hız sınırında; yeni giriş gönderilmedi.",
            retry_after=7,
            exchange_code=-1003,
        )
        failure.request_method = "GET"
        failure.request_path = "/fapi/v1/klines"

        async def sleep(seconds):
            self.assertEqual(seconds, 7)
            raise asyncio.CancelledError

        with patch.multiple(
            execution,
            auto_session_credentials=AsyncMock(
                return_value=("OFFLINE_KEY_PLACEHOLDER", "OFFLINE_SECRET_PLACEHOLDER"),
            ),
            reconcile=AsyncMock(),
            automatic_cycle=AsyncMock(side_effect=failure),
        ), patch.object(execution.asyncio, "sleep", side_effect=sleep):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(execution.execution_loop(application))

        self.assertTrue(state["original_v2_live"]["enabled"])
        self.assertFalse(state["original_v2_live"]["paused"])
        self.assertIsNone(state["original_v2_live"]["pause_reason"])
        self.assertTrue(state["live_auto_trade"])
        self.assertFalse(state["real_trading_locked"])
        self.assertTrue(state["auto"]["enabled"])
        self.assertFalse(any(event["kind"] == "ORIGINAL_LIVE_PAUSED" for event in state["events"]))
        self.assertFalse(any(event["kind"] == "LIVE_FAIL_CLOSED" for event in state["events"]))
        self.assertTrue(any(event["kind"] == "LIVE_MARKET_RATE_LIMIT_WAIT" for event in state["events"]))

    def test_public_market_rate_limit_records_get_request_metadata(self):
        application, state = application_for_loop()
        response = execution.httpx.Response(
            429,
            headers={"Retry-After": "7"},
            request=execution.httpx.Request("GET", "https://fapi.binance.com/fapi/v1/klines"),
        )
        client = execution.BinanceLiveClient(
            http=AsyncMock(),
            api_key="OFFLINE_KEY_PLACEHOLDER",
            secret_key="OFFLINE_SECRET_PLACEHOLDER",
            diagnostic_state=state,
            application=application,
        )
        with patch("app.main.market_data_request", new=AsyncMock(return_value=response)):
            with self.assertRaises(execution.LiveRateLimitError) as raised:
                asyncio.run(client.public_get("/fapi/v1/klines", {"symbol": "BTCUSDT"}))

        self.assertEqual(raised.exception.request_method, "GET")
        self.assertEqual(raised.exception.request_path, "/fapi/v1/klines")
        self.assertEqual(raised.exception.retry_after, 7)

    def test_global_ban_blocks_live_account_read_without_http(self):
        application, state = application_for_loop()
        response = execution.httpx.Response(
            418,
            headers={"Retry-After": "180", "X-MBX-USED-WEIGHT-1M": "1660"},
            json={"code": -1003},
            request=execution.httpx.Request("GET", "https://fapi.binance.com/fapi/v3/account"),
        )
        execution.BINANCE_GLOBAL_BAN.record(
            response,
            source="original_live",
            egress_route="render_direct",
        )
        http = SimpleNamespace(request=AsyncMock())
        client = execution.BinanceLiveClient(
            http,
            "OFFLINE_KEY_PLACEHOLDER",
            "OFFLINE_SECRET_PLACEHOLDER",
            diagnostic_state=state,
            application=application,
        )
        with self.assertRaises(execution.LiveRateLimitError) as raised:
            asyncio.run(client.signed("GET", "/fapi/v3/account"))
        self.assertEqual(raised.exception.exchange_code, -1003)
        self.assertEqual(raised.exception.request_method, "GET")
        self.assertTrue(execution.account_rate_limit_active(state))
        http.request.assert_not_awaited()

    def test_original_rate_limited_scan_retries_same_candle(self):
        application, state = application_for_loop()
        session_until = execution.time.time() + 300
        state["auto"].update({"enabled": True, "session_until": session_until})
        state.update(live_auto_trade=True, real_trading_locked=False)
        state["original_v2_live"] = {
            "enabled": True,
            "paused": False,
            "pause_reason": None,
            "risk_percent": 0.5,
            "session_expires_at_epoch": session_until,
            "scanner": {},
            "symbol_count": 40,
            "universe": {
                "updated_day": datetime.now(timezone.utc).date().isoformat(),
                "minimum_age_days": original_v2_live.ORIGINAL_MIN_SYMBOL_AGE_DAYS,
                "ranked_symbols": ["BTCUSDT"],
                "eligible_count": 1,
            },
        }
        failure = execution.LiveRateLimitError("Mock public market rate limit", retry_after=7)
        failure.request_method = "GET"
        failure.request_path = "/fapi/v1/klines"
        candidates = AsyncMock(side_effect=[failure, ([], {})])
        snapshot = {"wallet_balance": 1000, "positions": [], "open_orders": []}
        decision_time = int(execution.time.time()) // 900 * 900

        common = (
            patch.object(execution, "readiness_for", return_value={"ready": True}),
            patch.object(execution, "client_for_with_credentials", return_value=SimpleNamespace()),
            patch.object(execution, "account_snapshot", new=AsyncMock(return_value=snapshot)),
            patch.object(original_v2_live, "check_entry", new=AsyncMock()),
            patch.object(original_v2_live.original, "live_candidates", new=candidates),
        )
        with common[0], common[1], common[2], common[3], common[4]:
            with self.assertRaises(execution.LiveRateLimitError):
                asyncio.run(original_v2_live.cycle(
                    application,
                    ("OFFLINE_KEY_PLACEHOLDER", "OFFLINE_SECRET_PLACEHOLDER"),
                ))
            self.assertNotIn("last_decision_time", state["original_v2_live"])
            asyncio.run(original_v2_live.cycle(
                application,
                ("OFFLINE_KEY_PLACEHOLDER", "OFFLINE_SECRET_PLACEHOLDER"),
            ))

        self.assertEqual(candidates.await_count, 2)
        self.assertEqual(state["original_v2_live"]["last_decision_time"], decision_time)
        self.assertEqual(state["original_v2_live"]["scanner"]["stage"], "SCAN_COMPLETE")
        self.assertFalse(state["original_v2_live"]["paused"])

    def test_ban_starting_during_queued_owner_recheck_blocks_live_dispatch(self):
        import httpx

        application, state = application_for_loop()
        state["real_trading_locked"] = False
        calls = 0

        async def authority(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                execution.BINANCE_GLOBAL_BAN.record(httpx.Response(
                    429, headers={"Retry-After": "60"}, json={"code": -1003},
                    request=httpx.Request("GET", "https://fapi.binance.com/fapi/v1/klines"),
                ), source="original_live", egress_route="render_direct")
            return True

        http = SimpleNamespace(request=AsyncMock(return_value=httpx.Response(
            200, json={}, request=httpx.Request("POST", "https://offline.invalid/fapi/v1/order/test"),
        )))
        client = execution.BinanceLiveClient(
            http, "OFFLINE_KEY_PLACEHOLDER", "OFFLINE_SECRET_PLACEHOLDER",
            application=application, diagnostic_state=state, background_session=True,
        )
        with patch.object(execution, "background_authorization_active", new=authority), \
             patch.object(execution, "is_armed", return_value=True):
            with self.assertRaises(execution.LiveRateLimitError) as error:
                asyncio.run(client._request("POST", "/fapi/v1/order/test", {}, signed=True))
        self.assertEqual(error.exception.request_method, "POST")
        self.assertEqual(error.exception.request_path, "/fapi/v1/order/test")
        self.assertFalse(error.exception.unknown_execution)
        http.request.assert_not_awaited()

    def test_supervisor_restarts_even_when_failure_reporting_raises(self):
        async def exercise():
            application, _state = application_for_loop()
            restarted = asyncio.Event()
            calls = []

            async def worker(*args):
                calls.append(1)
                if len(calls) == 1:
                    raise RuntimeError("Mock worker failure")
                restarted.set()
                await asyncio.Event().wait()

            with patch.object(execution, "execution_loop", side_effect=worker), \
                    patch.object(execution, "record_execution_loop_failure", side_effect=RuntimeError("Mock reporting failure")), \
                    patch.object(execution.asyncio, "sleep", new=AsyncMock()), \
                    patch.object(execution.logger, "error") as log:
                supervisor = asyncio.create_task(execution.execution_supervisor(application))
                application.state.v25_execution_task = supervisor
                try:
                    await asyncio.wait_for(restarted.wait(), timeout=1)
                    self.assertEqual(len(calls), 2)
                    log.assert_called()
                finally:
                    await execution.shutdown_v25_execution(application)

        asyncio.run(exercise())

    def test_supervisor_restarts_exception_return_and_independent_worker_cancellation(self):
        for mode in ["exception", "return", "cancel"]:
            with self.subTest(mode=mode):
                async def exercise():
                    application, state = application_for_loop()
                    restarted = asyncio.Event()
                    calls = []

                    async def worker(*args):
                        calls.append(mode)
                        if len(calls) == 1:
                            if mode == "exception":
                                raise RuntimeError("Mock worker failure")
                            if mode == "cancel":
                                raise asyncio.CancelledError
                            return
                        restarted.set()
                        await asyncio.Event().wait()

                    with patch.object(execution, "execution_loop", side_effect=worker), \
                            patch.object(execution.asyncio, "sleep", new=AsyncMock()):
                        supervisor = asyncio.create_task(execution.execution_supervisor(application))
                        application.state.v25_execution_task = supervisor
                        try:
                            await asyncio.wait_for(restarted.wait(), timeout=1)
                            self.assertEqual(len(calls), 2)
                            self.assertTrue(any(event["kind"] == "LIVE_EXECUTION_RESTART" for event in state["events"]))
                        finally:
                            await execution.shutdown_v25_execution(application)
                        self.assertTrue(supervisor.done())
                        self.assertTrue(application.state.v25_execution_worker_task.done())
                        self.assertEqual(len(calls), 2)

                asyncio.run(exercise())

    def test_init_and_shutdown_supervisor_do_not_respawn_worker(self):
        async def exercise():
            application, state = application_for_loop()
            entered = asyncio.Event()
            calls = []

            async def worker(*args):
                calls.append(1)
                entered.set()
                await asyncio.Event().wait()

            async def stream(*args):
                await asyncio.Event().wait()

            with patch.object(execution, "load_state", return_value=state), \
                    patch.object(execution, "execution_loop", side_effect=worker), \
                    patch.object(execution, "live_user_stream_loop", side_effect=stream):
                execution.init_v25_execution(application)
                try:
                    await asyncio.wait_for(entered.wait(), timeout=1)
                finally:
                    await execution.shutdown_v25_execution(application)
                self.assertTrue(application.state.v25_execution_stopping)
                self.assertTrue(application.state.v25_execution_task.done())
                self.assertTrue(application.state.v25_execution_worker_task.done())
                self.assertTrue(application.state.v25_live_stream_task.done())
                self.assertEqual(calls, [1])

        asyncio.run(exercise())

    def install_stop(self, client):
        _application, state = application_for_loop()
        plan = {"symbol": "BTCUSDT"}
        with patch.object(execution.asyncio, "sleep", new=AsyncMock()) as sleep:
            result = asyncio.run(execution.install_verified_stop(client, state, plan, STOP_PARAMS, None))
        return result, plan, sleep

    def test_stop_known_rejection_retries_same_identity_then_verifies(self):
        client = StopClient(rejections=1)
        result, plan, sleep = self.install_stop(client)
        self.assertEqual(result["algoId"], 101)
        self.assertEqual(plan["stop_install_attempts"], 2)
        self.assertEqual(client.posts, [STOP_PARAMS, STOP_PARAMS])
        self.assertEqual(client.reads, 2)
        sleep.assert_awaited_once_with(execution.STOP_RETRY_BASE_SECONDS)

    def test_stop_rejections_are_bounded_to_three_attempts(self):
        client = StopClient(rejections=10)
        _application, state = application_for_loop()
        plan = {"symbol": "BTCUSDT"}
        with patch.object(execution.asyncio, "sleep", new=AsyncMock()) as sleep:
            with self.assertRaises(execution.LiveExchangeError):
                asyncio.run(execution.install_verified_stop(client, state, plan, STOP_PARAMS, None))
        self.assertEqual(len(client.posts), 3)
        self.assertEqual(client.reads, 3)
        self.assertEqual(plan["stop_install_attempts"], 3)
        self.assertEqual(sleep.await_count, 2)

    def test_acknowledged_or_ambiguous_stop_is_never_blindly_reposted(self):
        for client in [StopClient(verification="absent"), StopClient(ambiguous=True)]:
            with self.subTest(ambiguous=client.ambiguous):
                _application, state = application_for_loop()
                with patch.object(execution.asyncio, "sleep", new=AsyncMock()):
                    with self.assertRaises(execution.LiveExchangeError):
                        asyncio.run(execution.install_verified_stop(client, state, {"symbol": "BTCUSDT"}, STOP_PARAMS, None))
                self.assertEqual(len(client.posts), 1)
                self.assertLessEqual(client.reads, 6)

    def test_ambiguous_algo_post_recovers_delayed_exact_client_algo_id(self):
        class DelayedAlgoClient:
            def __init__(self):
                self.posts = 0
                self.reads = 0

            async def signed(self, method, path, params=None):
                if method == "POST":
                    self.posts += 1
                    raise execution.LiveExchangeError("Mock POST timeout", unknown_execution=True)
                self.reads += 1
                if self.reads == 1:
                    return []
                return [{**STOP_PARAMS, "algoId": 101, "algoStatus": "NEW"}]

        client = DelayedAlgoClient()
        with patch.object(execution.asyncio, "sleep", new=AsyncMock()) as sleep:
            result = asyncio.run(execution.post_algo(client, STOP_PARAMS))

        self.assertEqual(result["algoId"], 101)
        self.assertEqual(client.posts, 1)
        self.assertEqual(client.reads, 2)
        sleep.assert_awaited_once_with(0.25)

    def test_stop_read_timeout_recovers_without_second_post(self):
        client = StopClient(first_read_error=execution.LiveExchangeError("Mock read timeout"))
        result, plan, _sleep = self.install_stop(client)
        self.assertEqual(result["algoId"], 101)
        self.assertEqual(plan["stop_install_attempts"], 2)
        self.assertEqual(len(client.posts), 1)

    def test_stop_rate_limit_verification_honors_retry_after(self):
        client = StopClient(first_read_error=execution.LiveRateLimitError("Mock rate limit", retry_after=7))
        _result, _plan, sleep = self.install_stop(client)
        sleep.assert_awaited_once_with(7)
        self.assertEqual(len(client.posts), 1)

    def test_foreign_mismatched_and_unknown_stop_snapshots_are_not_confirmed(self):
        for mode in ["foreign", "wrong_shape", "unknown"]:
            with self.subTest(mode=mode):
                client = StopClient(verification=mode)
                _application, state = application_for_loop()
                with patch.object(execution.asyncio, "sleep", new=AsyncMock()):
                    with self.assertRaises(execution.LiveExchangeError):
                        asyncio.run(execution.install_verified_stop(client, state, {"symbol": "BTCUSDT"}, STOP_PARAMS, None))
                self.assertEqual(len(client.posts), 1)
                self.assertEqual(client.reads, 3)


if __name__ == "__main__":
    unittest.main()
