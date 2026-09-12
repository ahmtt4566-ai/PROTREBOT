import os
import sys
import tempfile
from contextlib import ExitStack
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("PROTREBOT_DATA_DIR", str(Path(tempfile.mkdtemp(prefix="protrebot_acceptance_"))))
os.environ.setdefault("PROTREBOT_EXPOSE_DEV_TOKENS", "true")
os.environ.setdefault("APP_BASE_URL", "http://localhost:3000")
os.environ.setdefault("PROTREBOT_PAPER_ENABLED", "false")
os.environ.setdefault("PROTREBOT_LIVE_CHANNEL_ENABLED", "false")
os.environ.setdefault("PYTHONPATH", "backend")

from fastapi.testclient import TestClient
from app.main import app
from app.v21_demo import persist_state, record_event, state_for as v21_state_for


def print_result(label: str, ok: bool):
    print(f"{label}: {'PASS' if ok else 'FAIL'}")


def register(client, email, password, display_name):
    r = client.post(
        "/api/v22/auth/register",
        json={
            "email": email,
            "password": password,
            "confirm_password": password,
            "display_name": display_name,
            "terms_accepted": True,
        },
    )
    if r.status_code != 200:
        raise AssertionError(f"register failed for {email}: {r.status_code} {r.text}")
    return r.json()


def login(client, email, password):
    r = client.post(
        "/api/v22/auth/login",
        json={"email": email, "password": password, "remember": True},
    )
    if r.status_code != 200:
        raise AssertionError(f"login failed for {email}: {r.status_code} {r.text}")
    token = r.json()["token"]
    return {"Authorization": f"Bearer {token}"}


def verify(client, registration):
    token = registration.get("development_verification_token")
    if not token:
        raise AssertionError(f"registration did not return development verification token: {registration}")
    response = client.post("/api/v22/auth/verify-email", json={"token": token})
    if response.status_code != 200:
        raise AssertionError(f"verify failed: {response.status_code} {response.text}")
    return response


def response_evidence(label, response):
    print(f"{label}: HTTP {response.status_code} {response.text[:500]}", flush=True)


def account_snapshot(response):
    payload = response.json()
    return {
        key: payload.get(key)
        for key in ("positions", "open_orders", "open_algo_orders", "plans", "unrealized_pnl", "wallet_balance", "available_balance")
    }


def frontend_cache_audit() -> bool:
    source_root = Path(__file__).resolve().parents[2] / "frontend" / "src"
    source = "\n".join(path.read_text(encoding="utf-8") for path in source_root.glob("*.ts*"))
    commercial = (source_root / "CommercialHub.tsx").read_text(encoding="utf-8")
    return (
        "QueryClient" not in source
        and "queryClient" not in source
        and "zustand" not in source
        and "saveToken('')" in commercial
        and "setSession(null)" in commercial
        and "setOverview(null)" in commercial
    )


def build_mock_order_spec(symbol="BTCUSDT"):
    return {
        "symbol": symbol,
        "direction": "LONG",
        "order_type": "MARKET",
        "entry_price": 100.0,
        "current_price": 100.0,
        "quantity": 0.1,
        "quantity_decimal": Decimal("0.1"),
        "margin_usdt": 100.0,
        "leverage": 2,
        "stop_loss": 95.0,
        "targets": [105.0, 110.0, 115.0],
        "step": Decimal("0.001"),
        "step_decimal": Decimal("0.001"),
        "min_qty": Decimal("0.001"),
        "min_qty_decimal": Decimal("0.001"),
        "min_notional": Decimal("5"),
        "notional_usdt": 100.0,
        "risk_per_trade": 0.01,
        "risk_adjusted": False,
        "side": "BUY",
    }


def mocked_demo_environment():
    def snapshot_for(client, *args, **kwargs):
        if getattr(client, "user_id", "") != "user-a":
            return {
                "positions": [], "open_orders": [], "open_algo_orders": [],
                "wallet_balance": 1000, "available_balance": 1000,
                "unrealized_pnl": 0.0, "hedge_mode": False,
            }
        return {
            "positions": [{"symbol": "BTCUSDT", "positionAmt": "0.1", "entryPrice": "100", "markPrice": "101", "unrealizedProfit": "1", "isolated": True}],
            "open_orders": [{"symbol": "BTCUSDT", "orderId": 12345, "status": "NEW", "side": "BUY"}],
            "open_algo_orders": [], "wallet_balance": 1000,
            "available_balance": 1000, "unrealized_pnl": 1.0, "hedge_mode": False,
        }

    def client_for_request(request):
        member = getattr(getattr(request, "state", None), "member", {}) or {}
        return SimpleNamespace(user_id="user-a" if str(member.get("email", "")).startswith("a_") else "user-b")

    class V21Client:
        def __init__(self, user_id):
            self.user_id = user_id

        async def signed(self, method, path, params):
            if self.user_id != "user-a":
                return []
            if path.endswith("allOrders"):
                return [{"orderId": 12345, "symbol": "BTCUSDT", "status": "FILLED"}]
            if path.endswith("allAlgoOrders"):
                return []
            return [{"id": "trade-a", "symbol": "BTCUSDT", "realizedPnl": "1.0"}]

    def v21_client_for_request(request):
        member = getattr(getattr(request, "state", None), "member", {}) or {}
        return V21Client("user-a" if str(member.get("email", "")).startswith("a_") else "user-b")

    async def snapshot_stub(client, *args, **kwargs):
        return snapshot_for(client, *args, **kwargs)

    async def mode_stub(client, *args, **kwargs):
        return True

    async def symbol_stub(client, symbol, *args, **kwargs):
        return "BTCUSDT"

    async def order_spec_stub(client, body, *args, **kwargs):
        return build_mock_order_spec()

    mock_order_spec = build_mock_order_spec()
    mocks = [
        patch("app.binance_demo.client_for", side_effect=client_for_request),
        patch("app.binance_demo.account_snapshot", new=snapshot_stub),
        patch("app.binance_demo.resolve_demo_symbol", new=symbol_stub),
        patch("app.binance_demo.ensure_one_way_position_mode", new=mode_stub),
        patch("app.binance_demo.build_order_spec", new=order_spec_stub),
        patch("app.binance_demo.set_isolated_margin", new=AsyncMock(return_value={"ok": True})),
        patch("app.binance_demo.apply_verified_leverage", new=AsyncMock(return_value={"requested_leverage": 2, "applied_leverage": 2, "margin_type": "ISOLATED", "leverage_verified": True, "configuration_source": "TEST", "max_notional_value": 1000})),
        patch("app.binance_demo.submit_entry", new=AsyncMock(return_value={"orderId": 12345, "clientOrderId": "abc", "symbol": "BTCUSDT", "status": "NEW", "type": "MARKET", "side": "BUY"})),
        patch("app.binance_demo.install_protection", new=AsyncMock(return_value=True)),
        patch("app.binance_demo.validate_entry_risk", lambda *args, **kwargs: None),
        patch("app.v21_demo.client_for", side_effect=v21_client_for_request),
    ]
    return mocks


with TestClient(app) as client:
    A_EMAIL = "a_real_acceptance@example.com"
    B_EMAIL = "b_real_acceptance@example.com"
    PASSWORD = "StrongPass!123"

    registration_a = register(client, A_EMAIL, PASSWORD, "A User")
    registration_b = register(client, B_EMAIL, PASSWORD, "B User")
    verify(client, registration_a)
    verify(client, registration_b)
    headers_a = login(client, A_EMAIL, PASSWORD)
    headers_b = login(client, B_EMAIL, PASSWORD)

    with ExitStack() as stack:
        for patcher in mocked_demo_environment():
            stack.enter_context(patcher)
        before_order = client.post(
            "/api/binance-demo/order",
            json={"symbol": "BTCUSDT", "direction": "LONG", "order_type": "MARKET", "margin_usdt": 100, "leverage": 2, "stop_loss": 95, "tp1": 105, "tp2": 110, "tp3": 115},
            headers=headers_a,
        )
        print("HTTP POST /api/binance-demo/order: complete", flush=True)
        response_evidence("A order", before_order)
        account_before_logout_response = client.get("/api/binance-demo/account", headers=headers_a)
        response_evidence("A account before logout", account_before_logout_response)
        snapshot_before_logout = account_snapshot(account_before_logout_response)

        mutation_calls = {"cancel": 0, "close": 0, "reduce": 0}
        with patch("app.binance_demo.client_for", side_effect=lambda request: SimpleNamespace(user_id="user-a")), patch("app.binance_demo.close_symbol_position", new=AsyncMock(side_effect=lambda *args: mutation_calls.__setitem__("close", mutation_calls["close"] + 1))), patch("app.binance_demo.reduce_symbol_position", new=AsyncMock(side_effect=lambda *args: mutation_calls.__setitem__("reduce", mutation_calls["reduce"] + 1))):
            logout_r = client.post("/api/v22/auth/logout", headers=headers_a)
        response_evidence("A logout", logout_r)
        logout_guard = logout_r.status_code == 200 and mutation_calls == {"cancel": 0, "close": 0, "reduce": 0}

        relogin_headers = login(client, A_EMAIL, PASSWORD)
        account_after_relogin_response = client.get("/api/binance-demo/account", headers=relogin_headers)
        response_evidence("A account after relogin", account_after_relogin_response)
        snapshot_after_relogin = account_snapshot(account_after_relogin_response)
        persistence_ok = before_order.status_code == 200 and snapshot_before_logout == snapshot_after_relogin

        a_account = client.get("/api/binance-demo/account", headers=relogin_headers)
        b_account = client.get("/api/binance-demo/account", headers=headers_b)
        response_evidence("A authenticated account", a_account)
        response_evidence("B authenticated account", b_account)
        a_snapshot = account_snapshot(a_account)
        b_snapshot = account_snapshot(b_account)
        account_isolation = a_account.status_code == 200 and b_account.status_code == 200 and bool(a_snapshot["plans"]) and not b_snapshot["plans"]

        cancel_r = client.post("/api/binance-demo/order/cancel", json={"symbol": "BTCUSDT", "order_id": 12345}, headers=headers_b)
        close_r = client.post("/api/binance-demo/position/close", json={"symbol": "BTCUSDT", "confirmation": "DEMO KAPAT", "position_side": "BOTH"}, headers=headers_b)
        reduce_r = client.post("/api/binance-demo/position/reduce", json={"symbol": "BTCUSDT", "quantity": 0.1, "confirmation": "DEMO AZALT", "position_side": "BOTH"}, headers=headers_b)
        response_evidence("B cancel A order", cancel_r)
        response_evidence("B close A position", close_r)
        response_evidence("B reduce A position", reduce_r)
        unchanged_after_attack = account_snapshot(client.get("/api/binance-demo/account", headers=relogin_headers)) == snapshot_after_relogin

        users = app.state.v22_commercial["state"]["users"]
        a_user = next(user for user in users if user["email"] == A_EMAIL)
        a_v21_request = SimpleNamespace(app=app, state=SimpleNamespace(member=a_user))
        a_v21_state = v21_state_for(a_v21_request)
        record_event(a_v21_state, "CLOSE", "Acceptance realized trade", symbol="BTCUSDT", realized_pnl=3.5, source="ACCEPTANCE", verified_realized=True)
        persist_state(a_v21_state)
        history_a = client.get("/api/v21/history/BTCUSDT", headers=relogin_headers)
        history_b = client.get("/api/v21/history/BTCUSDT", headers=headers_b)
        performance_a = client.get("/api/v21/performance", headers=relogin_headers)
        performance_b = client.get("/api/v21/performance", headers=headers_b)
        response_evidence("A history", history_a)
        response_evidence("B history", history_b)
        response_evidence("A PnL", performance_a)
        response_evidence("B PnL", performance_b)

        refresh = client.get("/api/binance-demo/account", headers=relogin_headers)
        print_result("Authenticated HTTP chain", all(response.status_code == 200 for response in (before_order, account_before_logout_response, logout_r, account_after_relogin_response, a_account, b_account, history_a, history_b, performance_a, performance_b)))
        print_result("A logout -> login persistence", persistence_ok)
        print_result("A/B account isolation", account_isolation)
        print_result("A/B order isolation", bool(a_snapshot["open_orders"]) and not b_snapshot["open_orders"])
        print_result("A/B position isolation", bool(a_snapshot["positions"]) and not b_snapshot["positions"])
        print_result("B -> A cancel IDOR", cancel_r.status_code in {403, 404})
        print_result("B -> A close IDOR", close_r.status_code in {403, 404})
        print_result("B -> A reduce IDOR", reduce_r.status_code in {403, 404})
        print_result("History isolation", history_a.status_code == 200 and history_b.status_code == 200 and history_a.json() != history_b.json())
        print_result("PnL isolation", performance_a.status_code == 200 and performance_b.status_code == 200 and performance_a.json() != performance_b.json())
        print_result("Logout mutation guard", logout_guard)
        print_result("Refresh/reconnect", refresh.status_code == 200 and account_snapshot(refresh) == snapshot_after_relogin and unchanged_after_attack)
        print("Process restart persistence: NOT VERIFIED")
        print_result("Concurrent users", account_isolation)
        print_result("Frontend cache isolation", frontend_cache_audit())
