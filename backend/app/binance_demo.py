"""Binance USD-M Futures Demo connector for ProTreBot Elite X.

This module deliberately knows only the Binance Demo hosts. Credentials prefer
the current Windows user's encrypted DPAPI vault (with a local ``backend/.env``
fallback) and are never returned by an API response. Entry orders require a
short-lived manual arm; risk-reducing cancellation and close actions remain
available while disarmed.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .local_storage import DATA_DIR, migrate_legacy_files


DEMO_REST_BASE = "https://demo-fapi.binance.com"
DEMO_WS_BASE = "wss://demo-fstream.binance.com"
MAX_MARGIN_USDT = Decimal("100")
MAX_LEVERAGE = 2
MANUAL_MAX_LEVERAGE = 50
MAX_NOTIONAL_USDT = Decimal("200")
MAX_OPEN_POSITIONS = 3
ARM_SECONDS = 10 * 60
CLIENT_PREFIX = "PTB_"
logger = logging.getLogger(__name__)
DEMO_SNAPSHOT_LOCK = asyncio.Lock()
DEMO_CLOCK_LOCK = asyncio.Lock()
DEMO_CLOCK_OFFSET_MS = 0
DEMO_CLOCK_SYNCED_AT = 0.0


def request_correlation_id(request: Request | None = None) -> str:
    return str(request.headers.get("Rndr-Id") or f"local-{uuid.uuid4().hex[:16]}") if request else f"local-{uuid.uuid4().hex[:16]}"


def trace_log(event: str, request_id: str, **fields: Any) -> None:
    values = " ".join(f"{key}={str(value).replace(chr(10), ' ')[:240]}" for key, value in fields.items())
    logger.info("[DEMO_ORDER_TRACE] %s request_id=%s%s", event, request_id, f" {values}" if values else "")


def safe_trace_error(error: Exception, client: "BinanceDemoClient" | None = None) -> str:
    message = str(error)
    api_key = getattr(client, "api_key", "") if client is not None else ""
    if api_key:
        message = message.replace(api_key, "[gizli]")
    return message[:240]


async def traced_stage(stage: str, request_id: str, operation: Any, *, client: "BinanceDemoClient" | None = None) -> Any:
    started = time.monotonic()
    trace_log(f"{stage}.start", request_id)
    try:
        result = await operation
    except Exception as exc:
        trace_log(
            f"{stage}.end",
            request_id,
            duration_ms=round((time.monotonic() - started) * 1000, 2),
            success=False,
            error_type=type(exc).__name__,
            error_message=safe_trace_error(exc, client),
        )
        raise
    trace_log(f"{stage}.end", request_id, duration_ms=round((time.monotonic() - started) * 1000, 2), success=True)
    return result


@asynccontextmanager
async def traced_lock(lock: asyncio.Lock, request_id: str, name: str) -> Any:
    wait_started = time.monotonic()
    await lock.acquire()
    trace_log(f"{name}.lock", request_id, lock_wait_ms=round((time.monotonic() - wait_started) * 1000, 2))
    try:
        yield
    finally:
        lock.release()

BACKEND_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = BACKEND_ROOT / ".env"
migrate_legacy_files(("binance_demo_runtime.json",))
STATE_PATH = DATA_DIR / "binance_demo_runtime.json"

PUBLIC_PATHS = {
    "/fapi/v1/time",
    "/fapi/v1/exchangeInfo",
    "/fapi/v1/ticker/24hr",
    "/fapi/v1/ticker/price",
    "/fapi/v1/klines",
}
PRIVATE_PATHS = {
    ("GET", "/fapi/v3/account"),
    ("GET", "/fapi/v3/positionRisk"),
    ("GET", "/fapi/v1/symbolConfig"),
    ("GET", "/fapi/v1/openOrders"),
    ("GET", "/fapi/v1/order"),
    ("GET", "/fapi/v1/openAlgoOrders"),
    ("GET", "/fapi/v1/allOrders"),
    ("GET", "/fapi/v1/allAlgoOrders"),
    ("GET", "/fapi/v1/userTrades"),
    ("GET", "/fapi/v1/positionSide/dual"),
    ("POST", "/fapi/v1/positionSide/dual"),
    ("POST", "/fapi/v1/leverage"),
    ("POST", "/fapi/v1/marginType"),
    ("POST", "/fapi/v1/order/test"),
    ("POST", "/fapi/v1/order"),
    ("POST", "/fapi/v1/algoOrder"),
    ("DELETE", "/fapi/v1/order"),
    ("DELETE", "/fapi/v1/algoOrder"),
}
API_KEY_PATHS = {
    ("POST", "/fapi/v1/listenKey"),
    ("PUT", "/fapi/v1/listenKey"),
    ("DELETE", "/fapi/v1/listenKey"),
}

router = APIRouter(prefix="/api/binance-demo", tags=["Binance Futures Demo"])


class BinanceDemoError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        http_status: int = 502,
        exchange_code: int | None = None,
        unknown_execution: bool = False,
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.exchange_code = exchange_code
        self.unknown_execution = unknown_execution


class ArmRequest(BaseModel):
    confirmation: str = Field(min_length=1, max_length=32)


class DemoOrderRequest(BaseModel):
    symbol: str = Field(min_length=5, max_length=20)
    direction: Literal["LONG", "SHORT"]
    order_type: Literal["MARKET", "LIMIT"] = "MARKET"
    margin_usdt: float = Field(ge=5, le=100)
    leverage: int = Field(ge=1, le=MANUAL_MAX_LEVERAGE)
    limit_price: float | None = Field(default=None, gt=0)
    stop_loss: float = Field(gt=0)
    tp1: float = Field(gt=0)
    tp2: float = Field(gt=0)
    tp3: float = Field(gt=0)


class CancelOrderRequest(BaseModel):
    symbol: str = Field(min_length=5, max_length=20)
    order_id: int = Field(gt=0)


class CancelAlgoRequest(BaseModel):
    symbol: str = Field(min_length=5, max_length=20)
    algo_id: int = Field(gt=0)


class ClosePositionRequest(BaseModel):
    symbol: str = Field(min_length=5, max_length=20)
    confirmation: str = Field(min_length=1, max_length=32)
    position_side: Literal["BOTH", "LONG", "SHORT"] = "BOTH"


class ReducePositionRequest(BaseModel):
    symbol: str = Field(min_length=5, max_length=20)
    quantity: float = Field(gt=0)
    confirmation: str = Field(min_length=1, max_length=32)
    position_side: Literal["BOTH", "LONG", "SHORT"] = "BOTH"


class EmergencyRequest(BaseModel):
    confirmation: str = Field(min_length=1, max_length=48)
    close_positions: bool = True


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_symbol(value: str) -> str:
    symbol = re.sub(r"[^A-Z0-9]", "", value.upper())
    if not symbol.endswith("USDT") or not 5 <= len(symbol) <= 20:
        raise BinanceDemoError("Yalnızca USDT vadeli işlem pariteleri destekleniyor.", http_status=422)
    return symbol


async def resolve_demo_symbol(client: BinanceDemoClient, value: str) -> str:
    cleaned = re.sub(r"[^A-Z0-9]", "", value.strip().upper())
    if cleaned.endswith("USDT"):
        return normalize_symbol(cleaned)
    payload = await client.public_get("/fapi/v1/exchangeInfo")
    row = next((item for item in payload.get("symbols", []) if (
        str(item.get("baseAsset") or "").upper() == cleaned
        and item.get("status") == "TRADING"
        and item.get("contractType") == "PERPETUAL"
        and item.get("quoteAsset") == "USDT"
    )), None)
    if row and row.get("symbol"):
        return normalize_symbol(str(row["symbol"]))
    return normalize_symbol(cleaned)


def response_rows(payload: Any) -> list[dict[str, Any]]:
    """Normalize Binance list/object responses without trusting their shape."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        nested = payload.get("positions")
        if isinstance(nested, list):
            return [item for item in nested if isinstance(item, dict)]
        return [payload] if payload else []
    return []


def load_demo_credentials() -> tuple[str, str]:
    # V28 web deployments use the encrypted in-application vault.  Once a
    # TESTNET record exists, its active switch is authoritative and legacy
    # environment values cannot bypass it.
    try:
        from .exchange_connections import cached_credentials, vault_managed

        vault_values = cached_credentials("TESTNET", active_only=True)
        if vault_values[0] and vault_values[1]:
            return vault_values
        if vault_managed("TESTNET"):
            return "", ""
    except (ImportError, RuntimeError, ValueError):
        pass
    try:
        from .credential_store import load_credentials

        vault_api_key, vault_secret_key = load_credentials()
        if len(vault_api_key) >= 10 and len(vault_secret_key) >= 10:
            return vault_api_key, vault_secret_key
    except (ImportError, OSError, RuntimeError):
        pass
    values: dict[str, str] = {}
    if ENV_PATH.exists():
        try:
            for raw_line in ENV_PATH.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
        except OSError:
            values = {}
    api_key = str(os.environ.get("BINANCE_DEMO_API_KEY") or values.get("BINANCE_DEMO_API_KEY") or "").strip()
    secret_key = str(os.environ.get("BINANCE_DEMO_SECRET_KEY") or values.get("BINANCE_DEMO_SECRET_KEY") or "").strip()
    return api_key, secret_key


def credentials_configured(request: Request | None = None) -> bool:
    if request is not None:
        try:
            from .exchange_connections import session_credentials_for_request

            member = getattr(request.state, "member", None)
            api_key, secret_key = session_credentials_for_request(request, "TESTNET")
            if member is not None:
                return bool(api_key and secret_key)
        except (ImportError, RuntimeError, ValueError):
            pass
        if getattr(request.state, "member", None) is not None:
            return False
    api_key, secret_key = load_demo_credentials()
    return len(api_key) >= 10 and len(secret_key) >= 10


def signed_query(secret_key: str, params: dict[str, Any]) -> tuple[str, str]:
    """Return Binance-compatible query text and HMAC signature."""
    query = urlencode([(key, value) for key, value in params.items() if value is not None], doseq=True)
    signature = hmac.new(secret_key.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
    return query, signature


def decimal_text(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return "0" if text in {"-0", ""} else text


def position_amount(value: Any) -> Decimal:
    try:
        return Decimal("0") if value is None else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def floor_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def round_tick(value: Decimal, tick: Decimal) -> Decimal:
    if tick <= 0:
        return value
    return (value / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick


class BinanceDemoClient:
    def __init__(self, http: httpx.AsyncClient, api_key: str, secret_key: str, *, public_only: bool = False) -> None:
        if (not api_key or not secret_key) and not public_only:
            raise BinanceDemoError(
                "Demo API bağlantısı aktif değil. Programdaki Borsa Bağlantıları bölümünden Testnet anahtarını kaydedip aktifleştirin.",
                http_status=412,
            )
        self.http = http
        self.api_key = api_key
        self.secret_key = secret_key
        self.public_only = public_only
        self.time_offset_ms = 0
        self.last_time_sync = 0.0
        self.last_status_code: int | None = None
        self.trace_request_id: str | None = None
        self._clock_lock = asyncio.Lock()

    async def public_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if path not in PUBLIC_PATHS:
            raise BinanceDemoError("İzin verilmeyen Demo API yolu.", http_status=500)
        return await self._request("GET", path, params or {}, signed=False)

    async def sync_clock(self, *, force: bool = False) -> None:
        global DEMO_CLOCK_OFFSET_MS, DEMO_CLOCK_SYNCED_AT
        request_started = time.monotonic()
        if not force and request_started - DEMO_CLOCK_SYNCED_AT < 30:
            self.time_offset_ms = DEMO_CLOCK_OFFSET_MS
            self.last_time_sync = DEMO_CLOCK_SYNCED_AT
            return
        async with DEMO_CLOCK_LOCK:
            if (
                (not force and time.monotonic() - DEMO_CLOCK_SYNCED_AT < 30)
                or (force and DEMO_CLOCK_SYNCED_AT > request_started)
            ):
                self.time_offset_ms = DEMO_CLOCK_OFFSET_MS
                self.last_time_sync = DEMO_CLOCK_SYNCED_AT
                return
            before = int(time.time() * 1000)
            payload = await self.public_get("/fapi/v1/time")
            after = int(time.time() * 1000)
            server_time = int(payload["serverTime"])
            DEMO_CLOCK_OFFSET_MS = server_time - ((before + after) // 2)
            DEMO_CLOCK_SYNCED_AT = time.monotonic()
            self.time_offset_ms = DEMO_CLOCK_OFFSET_MS
            self.last_time_sync = DEMO_CLOCK_SYNCED_AT

    async def signed(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        method = method.upper()
        if (method, path) not in PRIVATE_PATHS:
            raise BinanceDemoError("İzin verilmeyen özel Demo API işlemi.", http_status=500)
        for attempt in range(2 if method == "GET" else 1):
            await self.sync_clock(force=attempt == 1)
            payload = dict(params or {})
            payload["timestamp"] = int(time.time() * 1000) + self.time_offset_ms
            payload["recvWindow"] = 60000
            query, signature = signed_query(self.secret_key, payload)
            try:
                return await self._request(
                    method,
                    path,
                    payload,
                    signed=True,
                    encoded_query=query,
                    signature=signature,
                )
            except BinanceDemoError as exc:
                if method != "GET" or exc.exchange_code != -1021 or attempt == 1:
                    raise
        raise BinanceDemoError("Binance Demo zaman senkronizasyonu başarısız oldu.", http_status=502)

    async def api_key_request(self, method: str, path: str) -> Any:
        """Call a USER_STREAM endpoint with the API key but without a signature."""
        method = method.upper()
        if (method, path) not in API_KEY_PATHS:
            raise BinanceDemoError("İzin verilmeyen Demo kullanıcı akışı işlemi.", http_status=500)
        return await self._request(method, path, {}, signed=False, api_key_header=True)

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any],
        *,
        signed: bool,
        encoded_query: str = "",
        signature: str = "",
        api_key_header: bool = False,
    ) -> Any:
        url = f"{DEMO_REST_BASE}{path}"
        if not url.startswith(f"{DEMO_REST_BASE}/"):
            raise BinanceDemoError("Demo sunucu kilidi doğrulanamadı.", http_status=500)
        headers = {"X-MBX-APIKEY": self.api_key} if signed or api_key_header else {}
        request_url = f"{url}?{encoded_query}&signature={signature}" if signed else url
        attempts = 2 if method == "GET" and (method, path) in PRIVATE_PATHS else 1
        trace_request_id = self.trace_request_id if path in {"/fapi/v1/order", "/fapi/v1/algoOrder"} else None
        if trace_request_id:
            logger.info("[DEMO_HTTP_TRACE] request_start request_id=%s method=%s url_path=%s", trace_request_id, method, path)
        for attempt in range(attempts):
            try:
                response = await self.http.request(
                    method,
                    request_url,
                    params=None if signed else params,
                    headers=headers,
                    timeout=30,
                )
                break
            except httpx.RequestError as exc:
                if attempt + 1 == attempts:
                    if trace_request_id:
                        logger.warning(
                            "[DEMO_HTTP_TRACE] request_end request_id=%s method=%s url_path=%s success=false error_type=%s",
                            trace_request_id,
                            method,
                            path,
                            type(exc).__name__,
                        )
                    raise BinanceDemoError(
                        f"Binance Demo {method} {path} bağlantısı başarısız ({type(exc).__name__})."
                    ) from exc
                logger.warning(
                    "[DEMO_HTTP_TRACE] retry method=%s attempt=%s url_path=%s reason=%s delay_ms=250",
                    method,
                    attempt + 2,
                    path,
                    type(exc).__name__,
                )
                await asyncio.sleep(0.25)

        self.last_status_code = response.status_code
        if trace_request_id:
            logger.info(
                "[DEMO_HTTP_TRACE] request_end request_id=%s method=%s url_path=%s success=%s http_status=%s",
                trace_request_id,
                method,
                path,
                response.status_code < 400,
                response.status_code,
            )
        if response.status_code >= 400:
            try:
                body = response.json()
            except (ValueError, json.JSONDecodeError):
                body = {}
            code = body.get("code") if isinstance(body, dict) else None
            raw_message = body.get("msg") if isinstance(body, dict) else None
            safe_message = str(raw_message or "Binance Demo işlemi reddetti.").replace(self.api_key, "[gizli]")
            if response.status_code in {429, 418}:
                safe_message = "Demo API hız sınırına ulaşıldı; kısa süre bekleyip tekrar deneyin."
            unknown = response.status_code == 503
            if unknown:
                safe_message = "Emir durumu belirsiz döndü; sistem açık emirlerden doğrulama yapacak."
            raise BinanceDemoError(
                safe_message,
                http_status=429 if response.status_code in {429, 418} else 502,
                exchange_code=int(code) if isinstance(code, int) else None,
                unknown_execution=unknown,
            )
        try:
            return response.json()
        except (ValueError, json.JSONDecodeError):
            return {}


def client_for(request: Request) -> BinanceDemoClient:
    has_member_session = getattr(request.state, "member", None) is not None
    try:
        from .exchange_connections import session_credentials_for_request

        api_key, secret_key = session_credentials_for_request(request, "TESTNET")
    except (ImportError, RuntimeError, ValueError):
        api_key, secret_key = "", ""

    if has_member_session:
        if not api_key or not secret_key:
            raise BinanceDemoError(
                "Demo API bağlantısı aktif değil. Programdaki Borsa Bağlantıları bölümünden Testnet anahtarını kaydedip aktifleştirin.",
                http_status=412,
            )
        return BinanceDemoClient(request.app.state.http, api_key, secret_key)

    if not api_key or not secret_key:
        api_key, secret_key = load_demo_credentials()
    return BinanceDemoClient(request.app.state.http, api_key, secret_key)


def state_for(request: Request) -> dict[str, Any]:
    return request.app.state.binance_demo


def armed(state: dict[str, Any]) -> bool:
    return float(state.get("armed_until", 0)) > time.time()


def add_event(state: dict[str, Any], kind: str, message: str) -> None:
    state.setdefault("events", []).insert(0, {"kind": kind, "message": message, "created_at": utc_now()})
    del state["events"][40:]


def public_status(state: dict[str, Any]) -> dict[str, Any]:
    active = armed(state)
    if not active:
        state["armed_until"] = 0
    return {
        "version": "21.0.0",
        "mode": "BINANCE_FUTURES_DEMO_ONLY",
        "configured": credentials_configured(),
        "connected": bool(state.get("connected")),
        "armed": active,
        "armed_until": datetime.fromtimestamp(state["armed_until"], timezone.utc).isoformat() if active else None,
        "rest_host": DEMO_REST_BASE,
        "websocket_host": DEMO_WS_BASE,
        "real_trading_locked": True,
        "limits": {
            "max_margin_usdt": float(MAX_MARGIN_USDT),
            "max_leverage": MANUAL_MAX_LEVERAGE,
            "max_notional_usdt": float(MAX_NOTIONAL_USDT),
            "max_open_positions": MAX_OPEN_POSITIONS,
            "arm_minutes": ARM_SECONDS // 60,
        },
        "last_checked": state.get("last_checked"),
        "last_error": state.get("last_error"),
        "events": state.get("events", [])[:12],
        "reconciliation": state.get("reconciliation", {
            "actual_exchange_open_positions": 0,
            "internal_active_plans": 0,
            "reconciled_active_positions": 0,
            "stale_positions_removed": 0,
        }),
    }


def position_risk_summary(payload: Any) -> dict[str, Any]:
    """Classify raw Binance positionRisk rows without consulting local state."""
    diagnostics = []
    actual_count = 0
    for item in response_rows(payload):
        amount = position_amount(item.get("positionAmt"))
        is_actual = amount != 0
        if is_actual:
            actual_count += 1
        diagnostics.append({
            "symbol": str(item.get("symbol") or "").upper(),
            "positionAmt": str(amount),
            "positionSide": str(item.get("positionSide") or "BOTH").upper(),
            "markPrice": str(item.get("markPrice") or "0"),
            "entryPrice": str(item.get("entryPrice") or "0"),
            "unrealizedProfit": str(item.get("unRealizedProfit", item.get("unrealizedProfit", "0")) or "0"),
            "exchange_actual_position": is_actual,
        })
    return {
        "raw_position_risk_count": len(diagnostics),
        "actual_exchange_open_positions": actual_count,
        "exchange_position_diagnostics": diagnostics,
    }


def reconcile_demo_plans(state: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    """Make durable plans follow the exchange position snapshot, never vice versa."""
    actual_positions = [
        position for position in snapshot.get("positions", [])
        if str(position.get("symbol") or "") and float(position.get("quantity") or 0) > 0
    ]
    actual_by_symbol = {str(position["symbol"]): position for position in actual_positions}
    active_statuses = {"OPEN", "KORUMA ONARILDI", "KORUMA AKTİF", "STOP AKTİF · HEDEF İZLEME"}
    internal_active = 0
    stale_removed = 0
    changed = False
    for plan in state.get("plans", {}).values():
        if plan.get("status") not in active_statuses and plan.get("position_status") != "OPEN":
            continue
        internal_active += 1
        actual = actual_by_symbol.get(str(plan.get("symbol") or ""))
        if actual is None:
            plan.update({"status": "KAPANDI", "position_status": "CLOSED", "remaining_quantity": "0", "tp3_status": "FILLED", "closed_at": plan.get("closed_at") or utc_now(), "last_reconciled": utc_now()})
            stale_removed += 1
            changed = True
            continue
        amount = Decimal(str(actual.get("quantity") or 0))
        before = (plan.get("remaining_quantity"), plan.get("position_status"))
        update_position_lifecycle(plan, amount)
        plan["last_reconciled"] = utc_now()
        changed = changed or before != (plan.get("remaining_quantity"), plan.get("position_status"))
    state["reconciliation"] = {
        "actual_exchange_open_positions": int(snapshot.get("actual_exchange_open_positions", len(actual_positions))),
        "internal_active_plans": internal_active - stale_removed,
        "reconciled_active_positions": len(actual_positions),
        "stale_positions_removed": stale_removed,
        "last_sync": utc_now(),
    }
    return {"changed": changed, **state["reconciliation"]}


def safe_exchange_error(exc: BinanceDemoError) -> HTTPException:
    suffix = f" (Demo kodu: {exc.exchange_code})" if exc.exchange_code is not None else ""
    return HTTPException(status_code=exc.http_status, detail=f"{exc}{suffix}")


def verify_leverage_response(payload: Any, symbol: str, requested: int) -> dict[str, Any]:
    """Fail closed unless Binance confirms the exact requested leverage."""
    if not isinstance(payload, dict):
        raise BinanceDemoError("Binance Demo kaldıraç doğrulama yanıtı okunamadı; emir gönderilmedi.", http_status=409)
    response_symbol = str(payload.get("symbol") or "").upper()
    try:
        applied = int(payload.get("leverage"))
    except (TypeError, ValueError):
        applied = 0
    if response_symbol != symbol or applied != requested:
        raise BinanceDemoError(
            f"Binance Demo {requested}x yerine {applied or 'belirsiz'}x bildirdi; güvenlik için emir gönderilmedi.",
            http_status=409,
        )
    return {
        "symbol": symbol,
        "requested_leverage": requested,
        "applied_leverage": applied,
        "max_notional_value": str(payload.get("maxNotionalValue") or ""),
        "leverage_verified": True,
    }


def verify_symbol_configuration(payload: Any, symbol: str, requested: int) -> dict[str, Any]:
    """Verify leverage and isolated margin from the account symbol configuration."""
    row = next((item for item in response_rows(payload) if str(item.get("symbol") or "").upper() == symbol), None)
    if row is None:
        raise BinanceDemoError(f"{symbol} hesap yapılandırması Binance Demo'dan doğrulanamadı; emir gönderilmedi.", http_status=409)
    try:
        applied = int(row.get("leverage"))
    except (TypeError, ValueError):
        applied = 0
    margin_type = str(row.get("marginType") or "").upper()
    if applied != requested or margin_type != "ISOLATED":
        raise BinanceDemoError(
            f"{symbol} güvenlik ayarı uyuşmadı: istenen {requested}x ISOLATED, uygulanan {applied or 'belirsiz'}x {margin_type or 'belirsiz'}; emir gönderilmedi.",
            http_status=409,
        )
    return {
        "symbol": symbol,
        "requested_leverage": requested,
        "applied_leverage": applied,
        "margin_type": "isolated",
        "max_notional_value": str(row.get("maxNotionalValue") or ""),
        "leverage_verified": True,
        "configuration_source": "BINANCE_SYMBOL_CONFIG",
    }


async def set_isolated_margin(client: BinanceDemoClient, symbol: str) -> None:
    """Force isolated margin; Binance code -4046 means it is already isolated."""
    try:
        await client.signed("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "ISOLATED"})
    except BinanceDemoError as exc:
        if exc.exchange_code != -4046:
            raise


async def apply_verified_leverage(client: BinanceDemoClient, symbol: str, requested: int) -> dict[str, Any]:
    response = await client.signed("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": requested})
    verify_leverage_response(response, symbol, requested)
    configuration = await client.signed("GET", "/fapi/v1/symbolConfig", {"symbol": symbol})
    return verify_symbol_configuration(configuration, symbol, requested)


async def position_mode(client: BinanceDemoClient) -> bool:
    payload = await client.signed("GET", "/fapi/v1/positionSide/dual")
    value = payload.get("dualSidePosition", payload.get("dualPosition", False))
    return value is True or str(value).lower() == "true"


async def ensure_one_way_position_mode(client: BinanceDemoClient) -> int:
    """Read the mode before an order; never change it as part of order submission."""
    if not DEMO_REST_BASE.endswith("demo-fapi.binance.com"):
        raise BinanceDemoError("Demo sunucu kilidi doğrulanamadı.", http_status=500)
    if not await position_mode(client):
        return 0

    positions = await client.signed("GET", "/fapi/v3/positionRisk")
    orders = await client.signed("GET", "/fapi/v1/openOrders")
    algo_orders = await client.signed("GET", "/fapi/v1/openAlgoOrders")
    actual_positions = [item for item in response_rows(positions) if position_amount(item.get("positionAmt")) != 0]
    if actual_positions or response_rows(orders) or response_rows(algo_orders):
        raise BinanceDemoError(
            "Demo hesap Hedge Mode'da ve açık emir/pozisyon var; position mode değiştirilmedi.",
            http_status=409,
        )
    raise BinanceDemoError(
        "Demo hesap Hedge Mode'da; yeni emir öncesi position mode otomatik değiştirilmedi.",
        http_status=409,
    )


async def optional_symbol_configurations(client: BinanceDemoClient) -> Any:
    """Keep read-only account visibility if an older Demo deployment lacks this endpoint."""
    try:
        return await client.signed("GET", "/fapi/v1/symbolConfig")
    except BinanceDemoError:
        return []


async def optional_open_algo_orders(client: BinanceDemoClient) -> Any:
    """Keep account/ARM snapshots usable when Demo omits the algo-order read endpoint."""
    try:
        return await client.signed("GET", "/fapi/v1/openAlgoOrders")
    except BinanceDemoError:
        return []


async def snapshot_request(client: BinanceDemoClient, path: str, request_id: str) -> Any:
    started = time.monotonic()
    trace_log(f"account_snapshot.{path}.start", request_id)
    try:
        result = await client.signed("GET", path)
    except Exception as exc:
        trace_log(
            f"account_snapshot.{path}.end",
            request_id,
            duration_ms=round((time.monotonic() - started) * 1000, 2),
            success=False,
            http_status=getattr(client, "last_status_code", None),
            error_type=type(exc).__name__,
            error_message=safe_trace_error(exc, client),
        )
        raise
    trace_log(
        f"account_snapshot.{path}.end",
        request_id,
        duration_ms=round((time.monotonic() - started) * 1000, 2),
        success=True,
        http_status=getattr(client, "last_status_code", None),
    )
    return result


async def account_snapshot(client: BinanceDemoClient, request_id: str | None = None) -> dict[str, Any]:
    correlation_id = request_id or f"local-{uuid.uuid4().hex[:16]}"
    wait_started = time.monotonic()
    await DEMO_SNAPSHOT_LOCK.acquire()
    lock_wait_ms = round((time.monotonic() - wait_started) * 1000, 2)
    if lock_wait_ms > 0:
        trace_log("account_snapshot.lock", correlation_id, lock_wait_ms=lock_wait_ms)
    try:
        started = time.monotonic()
        trace_log("account_snapshot.start", correlation_id)
        try:
            result = await _account_snapshot(client, correlation_id)
        except Exception as exc:
            trace_log(
                "account_snapshot.end",
                correlation_id,
                duration_ms=round((time.monotonic() - started) * 1000, 2),
                success=False,
                error_type=type(exc).__name__,
                error_message=safe_trace_error(exc, client),
            )
            raise
        trace_log("account_snapshot.end", correlation_id, duration_ms=round((time.monotonic() - started) * 1000, 2), success=True)
        return result
    finally:
        DEMO_SNAPSHOT_LOCK.release()


async def _account_snapshot(client: BinanceDemoClient, request_id: str | None = None) -> dict[str, Any]:
    # Keep private snapshot reads sequential on the shared HTTP client.  This
    # avoids a burst of signed requests competing with the protection loop for
    # the same Render connection pool.
    correlation_id = request_id or f"local-{uuid.uuid4().hex[:16]}"
    account = await snapshot_request(client, "/fapi/v3/account", correlation_id)
    positions = await snapshot_request(client, "/fapi/v3/positionRisk", correlation_id)
    orders = await snapshot_request(client, "/fapi/v1/openOrders", correlation_id)
    try:
        algo_orders = await snapshot_request(client, "/fapi/v1/openAlgoOrders", correlation_id)
    except BinanceDemoError:
        algo_orders = []
    hedge_mode = await snapshot_request(client, "/fapi/v1/positionSide/dual", correlation_id)
    try:
        configurations = await snapshot_request(client, "/fapi/v1/symbolConfig", correlation_id)
    except BinanceDemoError:
        configurations = []
    config_by_symbol = {
        str(item.get("symbol") or "").upper(): item
        for item in response_rows(configurations)
        if item.get("symbol")
    }
    position_risk = position_risk_summary(positions)
    logger.info(
        "DEMO_POSITION_RECONCILIATION raw_position_risk_count=%s actual_exchange_open_positions=%s entries=%s",
        position_risk["raw_position_risk_count"],
        position_risk["actual_exchange_open_positions"],
        position_risk["exchange_position_diagnostics"],
    )
    open_positions = []
    for item in response_rows(positions):
        amount = position_amount(item.get("positionAmt"))
        symbol = str(item.get("symbol") or "").upper()
        if amount == 0:
            continue
        configuration = config_by_symbol.get(symbol, {})
        raw_leverage = item.get("leverage", configuration.get("leverage"))
        raw_margin_type = item.get("marginType", configuration.get("marginType"))
        try:
            leverage = int(raw_leverage) if raw_leverage is not None else None
        except (TypeError, ValueError):
            leverage = None
        margin_type = str(raw_margin_type).lower() if raw_margin_type else None
        open_positions.append({
            "symbol": symbol,
            "position_side": str(item.get("positionSide") or "BOTH").upper(),
            "direction": "LONG" if amount > 0 else "SHORT",
            "quantity": abs(float(amount)),
            "entry_price": float(item.get("entryPrice", 0)),
            "mark_price": float(item.get("markPrice", 0)),
            "liquidation_price": float(item.get("liquidationPrice", 0)),
            "unrealized_pnl": float(item.get("unRealizedProfit", item.get("unrealizedProfit", 0))),
            "leverage": leverage,
            "margin_type": margin_type,
            "requested_leverage": None,
            "applied_leverage": leverage,
            "leverage_verified": bool(configuration and leverage and margin_type),
            "configuration_source": "BINANCE_SYMBOL_CONFIG" if configuration else "UNAVAILABLE",
        })
    open_orders = [{
        "symbol": item.get("symbol"),
        "order_id": int(item.get("orderId", 0)),
        "client_order_id": item.get("clientOrderId"),
        "side": item.get("side"),
        "type": item.get("type"),
        "status": item.get("status"),
        "price": float(item.get("price", 0)),
        "quantity": float(item.get("origQty", 0)),
        "executed_quantity": float(item.get("executedQty", 0)),
        "reduce_only": bool(item.get("reduceOnly", False)),
    } for item in response_rows(orders)]
    open_algos = [{
        "symbol": item.get("symbol"),
        "algo_id": int(item.get("algoId", 0)),
        "client_algo_id": item.get("clientAlgoId"),
        "side": item.get("side"),
        "type": item.get("orderType", item.get("type")),
        "status": item.get("algoStatus", item.get("status")),
        "trigger_price": float(item.get("triggerPrice", item.get("stopPrice", 0))),
        "quantity": float(item.get("quantity", item.get("origQty", 0)) or 0),
        "close_position": str(item.get("closePosition", "false")).lower() == "true",
    } for item in response_rows(algo_orders)]
    return {
        "wallet_balance": float(account.get("totalWalletBalance", 0)),
        "available_balance": float(account.get("availableBalance", 0)),
        "margin_balance": float(account.get("totalMarginBalance", 0)),
        "unrealized_pnl": float(account.get("totalUnrealizedProfit", 0)),
        "positions": open_positions,
        "open_orders": open_orders,
        "open_algo_orders": open_algos,
        "hedge_mode": hedge_mode,
        **position_risk,
    }


async def connect_snapshot(client: BinanceDemoClient) -> dict[str, Any]:
    """Retry only the read-only CONNECT snapshot after a Binance clock rejection."""
    try:
        return await account_snapshot(client)
    except BinanceDemoError as exc:
        if exc.exchange_code != -1021:
            raise
        await client.sync_clock(force=True)
        return await account_snapshot(client)


def enrich_snapshot_with_plans(snapshot: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Add the local request audit without inventing missing exchange values."""
    plans = list(state.get("plans", {}).values())
    active_by_symbol: dict[str, dict[str, Any]] = {}
    for plan in plans:
        if plan.get("status") not in {"KAPANDI", "İPTAL", "GÜVENLİK İÇİN KAPATILDI", "ACİL DURDURULDU"}:
            active_by_symbol[str(plan.get("symbol") or "")] = plan
    for position in snapshot.get("positions", []):
        plan = active_by_symbol.get(str(position.get("symbol") or ""))
        if not plan:
            continue
        requested = int(plan.get("requested_leverage") or plan.get("leverage") or 0) or None
        applied = int(plan.get("applied_leverage") or 0) or position.get("leverage")
        position["requested_leverage"] = requested
        position["applied_leverage"] = position.get("leverage") or applied
        if position.get("leverage") is None and applied:
            position["leverage"] = applied
            position["configuration_source"] = "VERIFIED_LEVERAGE_RESPONSE"
        if not position.get("margin_type") and plan.get("margin_type"):
            position["margin_type"] = plan["margin_type"]
        position["leverage_verified"] = bool(
            position.get("leverage_verified")
            or (
                plan.get("leverage_verified")
                and requested == position.get("leverage")
                and str(position.get("margin_type") or "").lower() == "isolated"
            )
        )
    return snapshot


async def symbol_rules(client: BinanceDemoClient, symbol: str) -> dict[str, Decimal]:
    payload = await client.public_get("/fapi/v1/exchangeInfo")
    row = next((item for item in payload.get("symbols", []) if item.get("symbol") == symbol), None)
    if (
        not row
        or row.get("status") != "TRADING"
        or row.get("contractType") != "PERPETUAL"
        or row.get("quoteAsset") != "USDT"
    ):
        raise BinanceDemoError(f"{symbol} Demo USDT perpetual market olarak geçerli değil.", http_status=422)
    filters = {item.get("filterType"): item for item in row.get("filters", [])}
    lot = filters.get("LOT_SIZE", {})
    price_filter = filters.get("PRICE_FILTER", {})
    notional_filter = filters.get("MIN_NOTIONAL", {})
    return {
        "step": Decimal(str(lot.get("stepSize", "0.001"))),
        "min_qty": Decimal(str(lot.get("minQty", "0"))),
        "max_qty": Decimal(str(lot.get("maxQty", "999999999"))),
        "tick": Decimal(str(price_filter.get("tickSize", "0.01"))),
        "min_notional": Decimal(str(notional_filter.get("notional", "0"))),
    }


async def ticker_price(client: BinanceDemoClient, symbol: str) -> Decimal:
    payload = await client.public_get("/fapi/v1/ticker/price", {"symbol": symbol})
    price = Decimal(str(payload.get("price", "0")))
    if price <= 0:
        raise BinanceDemoError("Demo piyasa fiyatı alınamadı.")
    return price


def validate_levels(direction: str, entry: Decimal, stop: Decimal, targets: list[Decimal]) -> None:
    if direction == "LONG":
        valid = stop < entry < targets[0] < targets[1] < targets[2]
        message = "LONG için sıralama Stop < Giriş < TP1 < TP2 < TP3 olmalı."
    else:
        valid = targets[2] < targets[1] < targets[0] < entry < stop
        message = "SHORT için sıralama TP3 < TP2 < TP1 < Giriş < Stop olmalı."
    if not valid:
        raise BinanceDemoError(message, http_status=422)


async def build_order_spec(client: BinanceDemoClient, order: DemoOrderRequest) -> dict[str, Any]:
    symbol = await resolve_demo_symbol(client, order.symbol)
    current_price, rules = await asyncio.gather(ticker_price(client, symbol), symbol_rules(client, symbol))
    if order.order_type == "LIMIT" and order.limit_price is None:
        raise BinanceDemoError("Limit emir için limit fiyatı zorunludur.", http_status=422)
    entry = Decimal(str(order.limit_price)) if order.order_type == "LIMIT" else current_price
    margin = Decimal(str(order.margin_usdt))
    notional = margin * Decimal(order.leverage)
    if margin > MAX_MARGIN_USDT or order.leverage > MANUAL_MAX_LEVERAGE or notional > MAX_NOTIONAL_USDT:
        raise BinanceDemoError(
            f"Demo güvenlik tavanı: en fazla 100 USDT marjin, {MANUAL_MAX_LEVERAGE}x kaldıraç ve {decimal_text(MAX_NOTIONAL_USDT)} USDT notional.",
            http_status=422,
        )
    quantity = floor_step(notional / entry, rules["step"])
    minimum_notional = max(rules["min_notional"], rules["min_qty"] * entry)
    if quantity < rules["min_qty"] or quantity * entry < rules["min_notional"]:
        min_margin = minimum_notional / Decimal(order.leverage)
        raise BinanceDemoError(
            f"{symbol} için borsa minimumu bu güvenlik tavanını aşıyor. Yaklaşık en az {decimal_text(min_margin.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))} USDT marjin gerekir; daha düşük fiyatlı bir parite seçin.",
            http_status=422,
        )
    if quantity > rules["max_qty"]:
        raise BinanceDemoError("Hesaplanan miktar borsa üst sınırını aşıyor.", http_status=422)
    stop = round_tick(Decimal(str(order.stop_loss)), rules["tick"])
    targets = [round_tick(Decimal(str(value)), rules["tick"]) for value in (order.tp1, order.tp2, order.tp3)]
    limit_price = round_tick(entry, rules["tick"])
    validate_levels(order.direction, limit_price if order.order_type == "LIMIT" else current_price, stop, targets)
    return {
        "symbol": symbol,
        "direction": order.direction,
        "side": "BUY" if order.direction == "LONG" else "SELL",
        "close_side": "SELL" if order.direction == "LONG" else "BUY",
        "order_type": order.order_type,
        "margin_usdt": float(margin),
        "leverage": order.leverage,
        "notional_usdt": float(quantity * entry),
        "quantity": decimal_text(quantity),
        "quantity_decimal": quantity,
        "current_price": float(current_price),
        "entry_price": decimal_text(limit_price),
        "stop_loss": decimal_text(stop),
        "targets": [decimal_text(value) for value in targets],
        "step": rules["step"],
        "min_qty": rules["min_qty"],
        "min_notional": float(minimum_notional),
    }


def runtime_payload(state: dict[str, Any]) -> dict[str, Any]:
    return {"plans": state.get("plans", {}), "saved_at": utc_now()}


def persist_runtime(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(runtime_payload(state), ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(STATE_PATH)


def load_runtime() -> dict[str, Any]:
    try:
        payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return payload.get("plans", {}) if isinstance(payload.get("plans"), dict) else {}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}


def new_client_id(kind: str) -> str:
    return f"{CLIENT_PREFIX}{kind}_{uuid.uuid4().hex[:18]}"


async def find_order_by_client_id(client: BinanceDemoClient, symbol: str, client_id: str) -> dict[str, Any] | None:
    try:
        result = await client.signed("GET", "/fapi/v1/order", {"symbol": symbol, "origClientOrderId": client_id})
        return result if isinstance(result, dict) and result.get("orderId") else None
    except BinanceDemoError:
        return None


async def recover_pending_entry_intents(client: BinanceDemoClient, state: dict[str, Any]) -> bool:
    changed = False
    for plan in state.get("plans", {}).values():
        if plan.get("status") != "ENTRY_INTENT_PENDING" or not plan.get("entry_client_order_id"):
            continue
        found = await find_order_by_client_id(client, str(plan["symbol"]), str(plan["entry_client_order_id"]))
        if found is None:
            rows = response_rows(await client.signed("GET", "/fapi/v3/positionRisk", {"symbol": plan["symbol"]}))
            found = {"orderId": None, "clientOrderId": plan["entry_client_order_id"]} if any(position_amount(item.get("positionAmt")) != 0 for item in rows) else None
        if found is None:
            continue
        plan["entry_order_id"] = int(found.get("orderId") or 0) or None
        plan["status"] = "DOLUM BEKLİYOR"
        plan["recovered_at"] = utc_now()
        changed = True
    if changed:
        persist_runtime(state)
    return changed


async def submit_entry(client: BinanceDemoClient, spec: dict[str, Any], *, test_only: bool, client_id: str | None = None) -> dict[str, Any]:
    client_id = client_id or new_client_id("TEST" if test_only else "ENTRY")
    params: dict[str, Any] = {
        "symbol": spec["symbol"],
        "side": spec["side"],
        "type": spec["order_type"],
        "quantity": spec["quantity"],
        "newClientOrderId": client_id,
        "newOrderRespType": "RESULT",
    }
    if spec["order_type"] == "LIMIT":
        params.update({"price": spec["entry_price"], "timeInForce": "GTC"})
    path = "/fapi/v1/order/test" if test_only else "/fapi/v1/order"
    try:
        response = await client.signed("POST", path, params)
    except BinanceDemoError as exc:
        if not test_only and exc.unknown_execution:
            recovered = await find_order_by_client_id(client, spec["symbol"], client_id)
            if recovered is not None:
                return recovered
        raise
    return response if isinstance(response, dict) else {}


async def cancel_entry_if_open(client: BinanceDemoClient, plan: dict[str, Any]) -> None:
    order_id = plan.get("entry_order_id")
    if not order_id:
        return
    try:
        await client.signed("DELETE", "/fapi/v1/order", {"symbol": plan["symbol"], "orderId": order_id})
    except BinanceDemoError as exc:
        if exc.exchange_code not in {-2011, -2013}:
            raise


async def close_symbol_position(client: BinanceDemoClient, symbol: str, position_side: str = "BOTH") -> dict[str, Any] | None:
    rows = await client.signed("GET", "/fapi/v3/positionRisk", {"symbol": symbol})
    normalized_side = str(position_side or "BOTH").upper()
    row = next((item for item in response_rows(rows)
                if str(item.get("positionSide") or "BOTH").upper() == normalized_side
                and position_amount(item.get("positionAmt")) != 0), None)
    if row is None:
        return None
    amount = position_amount(row.get("positionAmt"))
    params = {
        "symbol": symbol,
        "side": "SELL" if amount > 0 else "BUY",
        "type": "MARKET",
        "quantity": decimal_text(abs(amount)),
        "reduceOnly": "true",
        "newClientOrderId": new_client_id("CLOSE"),
        "newOrderRespType": "RESULT",
    }
    if normalized_side != "BOTH":
        params["positionSide"] = normalized_side
        params.pop("reduceOnly", None)
    return await client.signed("POST", "/fapi/v1/order", params)


async def reduce_symbol_position(client: BinanceDemoClient, symbol: str, quantity: Decimal, position_side: str = "BOTH") -> dict[str, Any] | None:
    rows = await client.signed("GET", "/fapi/v3/positionRisk", {"symbol": symbol})
    normalized_side = str(position_side or "BOTH").upper()
    row = next((item for item in response_rows(rows)
                if str(item.get("positionSide") or "BOTH").upper() == normalized_side
                and position_amount(item.get("positionAmt")) != 0), None)
    if row is None:
        return None
    amount = abs(position_amount(row.get("positionAmt")))
    rules = await symbol_rules(client, symbol)
    rounded = floor_step(quantity, rules["step"])
    if rounded <= 0 or rounded > amount:
        raise BinanceDemoError("Reduce-only miktarı mevcut pozisyon miktarı içinde olmalı.", http_status=422)
    mark_price = Decimal(str(row.get("markPrice") or "0"))
    if rounded < rules["min_qty"] or rounded * mark_price < rules["min_notional"]:
        raise BinanceDemoError("Reduce-only miktarı borsa minimum miktar/notional kurallarını sağlamıyor.", http_status=422)
    raw_amount = position_amount(row.get("positionAmt"))
    params = {
        "symbol": symbol,
        "side": "SELL" if raw_amount > 0 else "BUY",
        "type": "MARKET",
        "quantity": decimal_text(rounded),
        "reduceOnly": "true",
        "newClientOrderId": new_client_id("REDUCE"),
        "newOrderRespType": "RESULT",
    }
    if normalized_side != "BOTH":
        params["positionSide"] = normalized_side
        params.pop("reduceOnly", None)
    return await client.signed("POST", "/fapi/v1/order", params)


def update_position_lifecycle(plan: dict[str, Any], amount: Decimal) -> None:
    """Keep the durable demo plan aligned with the exchange position amount."""
    remaining = abs(amount)
    initial = Decimal(str(plan.get("initial_quantity") or plan.get("quantity") or "0"))
    plan["remaining_quantity"] = decimal_text(remaining)
    plan["position_status"] = "OPEN" if remaining else "CLOSED"
    if initial <= 0 or remaining <= 0:
        if remaining <= 0:
            plan["tp3_status"] = "FILLED"
        return
    reduction = (initial - remaining) / initial
    if reduction >= Decimal("0.60"):
        plan["tp2_status"] = "FILLED"
    elif reduction >= Decimal("0.30"):
        plan["tp1_status"] = "FILLED"


def mark_cancelled_protection(plans: dict[str, Any], symbol: str, algo_id: int) -> dict[str, Any] | None:
    for plan in plans.values():
        if plan.get("symbol") != symbol or int(plan.get("stop_algo_id") or 0) != algo_id:
            continue
        plan["stop_protection_cancelled"] = True
        plan["protection_status"] = "KORUMA İPTAL"
        plan["status"] = "KORUMA İPTAL"
        plan["last_error"] = "Kullanıcı STOP_MARKET koruma emrini iptal etti."
        return plan
    return None


def duplicate_entry_reason(snapshot: dict[str, Any], symbol: str) -> str | None:
    if any(item.get("symbol") == symbol for item in snapshot.get("positions", [])):
        return f"{symbol} için zaten açık pozisyon var; önce mevcut pozisyonu kapatın veya başka parite seçin."
    order = next((item for item in snapshot.get("open_orders", []) if item.get("symbol") == symbol), None)
    if order:
        order_id = order.get("orderId") or order.get("order_id")
        suffix = f" (emir: {order_id})" if order_id else ""
        return f"{symbol} için zaten açık normal emir var{suffix}; önce emri iptal edin veya başka parite seçin."
    return None


def validate_entry_risk(
    snapshot: dict[str, Any],
    body: DemoOrderRequest,
    spec: dict[str, Any],
    settings: dict[str, Any],
    *,
    daily_realized_pnl: float,
    paper_positions: list[dict[str, Any]] | None = None,
    use_auto_universe: bool = True,
) -> None:
    """Final fail-closed gate shared by manual and automatic Demo entries."""
    symbol = normalize_symbol(body.symbol)
    if use_auto_universe:
        allowed = {normalize_symbol(value) for value in settings.get("_auto_universe", []) if value}
        if allowed and symbol not in allowed:
            raise BinanceDemoError(f"{symbol} izinli pariteler dışında; emir açılmadı.", http_status=409)
    if body.direction == "LONG" and not settings.get("allow_long", True):
        raise BinanceDemoError("LONG girişleri risk politikası tarafından kapatıldı.", http_status=409)
    if body.direction == "SHORT" and not settings.get("allow_short", True):
        raise BinanceDemoError("SHORT girişleri risk politikası tarafından kapatıldı.", http_status=409)
    if body.leverage > MANUAL_MAX_LEVERAGE or body.margin_usdt > MAX_MARGIN_USDT:
        raise BinanceDemoError("Demo kaldıraç veya marjin güvenlik sınırını aşıyor.", http_status=422)
    if float(spec["notional_usdt"]) > float(MAX_NOTIONAL_USDT):
        raise BinanceDemoError("Demo notional güvenlik sınırını aşıyor.", http_status=422)
    if daily_realized_pnl <= -float(settings.get("daily_loss_limit", 0)):
        raise BinanceDemoError("Günlük doğrulanmış zarar limiti aktif; yeni giriş kilitli.", http_status=409)
    existing_positions = len(snapshot.get("positions", []))
    pending_entries = sum(1 for item in snapshot.get("open_orders", []) if not bool(item.get("reduce_only", False)))
    pending_entries += len(paper_positions or [])
    if existing_positions + pending_entries >= min(int(settings.get("max_positions", MAX_OPEN_POSITIONS)), MAX_OPEN_POSITIONS):
        raise BinanceDemoError("Açık veya bekleyen girişler maksimum pozisyon sınırını dolduruyor.", http_status=409)
    duplicate_reason = duplicate_entry_reason(snapshot, symbol)
    if duplicate_reason:
        raise BinanceDemoError(duplicate_reason, http_status=409)
    if float(snapshot.get("available_balance", 0)) < float(body.margin_usdt):
        raise BinanceDemoError("Demo hesabında seçilen marjin için yeterli kullanılabilir bakiye yok.", http_status=409)
    estimated_loss = entry_risk(spec)
    if estimated_loss > float(settings.get("max_loss_per_trade", 0)) + 1e-9:
        raise BinanceDemoError("Stop Loss riski işlem başı maksimum zarar limitini aşıyor.", http_status=409)


def entry_risk(spec: dict[str, Any]) -> float:
    distance = abs(float(spec["current_price"]) - float(spec["stop_loss"]))
    return float(spec["notional_usdt"]) * distance / max(float(spec["current_price"]), 1e-12)


def adjust_manual_spec_to_risk(spec: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    """Reduce only manual Demo quantity until the existing risk gate passes."""
    max_allowed_risk = float(settings.get("max_loss_per_trade", 0))
    current_risk = entry_risk(spec)
    spec["risk_per_trade"] = current_risk
    spec["risk_adjusted"] = False
    if current_risk <= max_allowed_risk + 1e-9:
        return spec

    quantity = spec["quantity_decimal"]
    entry = Decimal(str(spec["current_price"]))
    step = spec["step"]
    min_qty = spec["min_qty"]
    min_notional = Decimal(str(spec["min_notional"]))
    candidate = floor_step(quantity * Decimal(str(max_allowed_risk)) / Decimal(str(current_risk)), step)
    if candidate >= quantity:
        candidate = floor_step(quantity - step, step)

    while candidate >= min_qty:
        if candidate * entry < min_notional:
            break
        spec["quantity_decimal"] = candidate
        spec["quantity"] = decimal_text(candidate)
        spec["notional_usdt"] = float(candidate * entry)
        current_risk = entry_risk(spec)
        if current_risk <= max_allowed_risk + 1e-9:
            spec["risk_per_trade"] = current_risk
            spec["risk_adjusted"] = True
            return spec
        candidate = floor_step(candidate - step, step)

    raise BinanceDemoError("Stop Loss riski için borsa minimum miktarı yeterince küçültülemiyor; emir açılmadı.", http_status=409)


def verified_realized_pnl(state: dict[str, Any]) -> float:
    return sum(
        float(item.get("realized_pnl") or 0)
        for item in state.get("journal", [])
        if item.get("verified_realized") is True
    )


async def post_algo(client: BinanceDemoClient, params: dict[str, Any]) -> dict[str, Any]:
    """Create one conditional order without blind retry on ambiguous responses."""
    try:
        result = await client.signed("POST", "/fapi/v1/algoOrder", params)
        return result if isinstance(result, dict) else {}
    except BinanceDemoError as exc:
        if not exc.unknown_execution:
            raise
        open_algos = response_rows(
            await client.signed("GET", "/fapi/v1/openAlgoOrders", {"symbol": params["symbol"]})
        )
        recovered = next(
            (item for item in open_algos if item.get("clientAlgoId") == params.get("clientAlgoId")),
            None,
        )
        if recovered is None:
            raise
        return recovered


async def install_protection(client: BinanceDemoClient, state: dict[str, Any], plan: dict[str, Any], *, request_id: str | None = None) -> None:
    symbol = plan["symbol"]
    correlation_id = request_id or f"local-{uuid.uuid4().hex[:16]}"
    if plan.get("stop_protection_cancelled"):
        return
    rows = await traced_stage("protection.positionRisk", correlation_id, client.signed("GET", "/fapi/v3/positionRisk", {"symbol": symbol}), client=client)
    position = next((item for item in response_rows(rows) if position_amount(item.get("positionAmt")) != 0), None)
    if position is None:
        plan["status"] = "DOLUM BEKLİYOR"
        return
    amount = position_amount(position.get("positionAmt"))
    update_position_lifecycle(plan, amount)
    actual_direction = "LONG" if amount > 0 else "SHORT"
    if actual_direction != plan["direction"]:
        plan["status"] = "YÖN UYUŞMAZLIĞI"
        add_event(state, "KORUMA KİLİDİ", f"{symbol} pozisyon yönü planla uyuşmadı; otomatik koruma kurulmadı.")
        return

    close_side = "SELL" if amount > 0 else "BUY"
    common = {
        "algoType": "CONDITIONAL",
        "symbol": symbol,
        "side": close_side,
        "workingType": "MARK_PRICE",
        "priceProtect": "TRUE",
    }
    stop_client_id = plan.setdefault("stop_client_id", new_client_id("SL"))
    stop_params = {
        **common,
        "type": "STOP_MARKET",
        "triggerPrice": plan["stop_loss"],
        "closePosition": "true",
        "clientAlgoId": stop_client_id,
    }
    protection_ids: list[int] = []
    stop_started = time.monotonic()
    trace_log("protection.SL.start", correlation_id)
    try:
        stop_result = await post_algo(client, stop_params)
        if stop_result.get("algoId"):
            stop_algo_id = int(stop_result["algoId"])
            protection_ids.append(stop_algo_id)
            plan["stop_algo_id"] = stop_algo_id
        else:
            raise BinanceDemoError("Binance Demo Stop koruma kimliği doğrulanamadı.", http_status=409)
        trace_log("protection.SL.end", correlation_id, duration_ms=round((time.monotonic() - stop_started) * 1000, 2), success=True, http_status=getattr(client, "last_status_code", None))
    except BinanceDemoError as exc:
        trace_log("protection.SL.end", correlation_id, duration_ms=round((time.monotonic() - stop_started) * 1000, 2), success=False, http_status=getattr(client, "last_status_code", None), error_type=type(exc).__name__, error_message=safe_trace_error(exc, client))
        plan["status"] = "CRITICAL / UNPROTECTED"
        plan["protection_status"] = "CRITICAL / UNPROTECTED"
        plan["recovery_attempts"] = int(plan.get("recovery_attempts", 0)) + 1
        plan["last_error"] = str(exc)
        add_event(state, "ACİL KORUMA", f"{symbol} Stop kurulamadı; Demo pozisyon güvenlik için kapatılıyor.")
        try:
            await cancel_entry_if_open(client, plan)
        except BinanceDemoError as cancel_exc:
            plan["last_error"] = f"Stop: {exc}; Entry iptali: {cancel_exc}"
        try:
            await close_symbol_position(client, symbol)
        except BinanceDemoError as close_exc:
            plan["last_error"] = f"Stop: {exc}; Kapatma: {close_exc}"
        try:
            confirmation = response_rows(await client.signed("GET", "/fapi/v3/positionRisk", {"symbol": symbol}))
            still_open = any(position_amount(item.get("positionAmt")) != 0 for item in confirmation)
        except BinanceDemoError as verify_exc:
            plan["last_error"] = f"{plan.get('last_error', str(exc))}; Durum doğrulama: {verify_exc}"
            plan["position_status"] = "OPEN"
            persist_runtime(state)
            raise
        if still_open:
            plan["position_status"] = "OPEN"
            persist_runtime(state)
            raise BinanceDemoError(f"{symbol} CRITICAL / UNPROTECTED; güvenli kapatma doğrulanamadı.", http_status=409)
        plan["position_status"] = "CLOSED"
        plan["status"] = "GÜVENLİK İÇİN KAPATILDI"
        plan["protection_status"] = "CLOSED_AFTER_PROTECTION_FAILURE"
        persist_runtime(state)
        raise BinanceDemoError(f"{symbol} Stop kurulamadı; pozisyon güvenli biçimde kapatıldı.", http_status=409)

    step = Decimal(str(plan["step"]))
    min_qty = Decimal(str(plan["min_qty"]))
    total_qty = abs(amount)
    partial_qty = floor_step(total_qty * Decimal("0.30"), step)
    targets = plan["targets"]
    monitoring_targets: list[str] = []
    if partial_qty >= min_qty:
        for index, trigger in enumerate(targets[:2], start=1):
            client_key = f"tp{index}_client_id"
            algo_client_id = plan.setdefault(client_key, new_client_id(f"TP{index}"))
            params = {
                **common,
                "type": "TAKE_PROFIT_MARKET",
                "triggerPrice": trigger,
                "quantity": decimal_text(partial_qty),
                "reduceOnly": "true",
                "clientAlgoId": algo_client_id,
            }
            try:
                target_started = time.monotonic()
                trace_log(f"protection.TP{index}.start", correlation_id)
                result = await post_algo(client, params)
                if result.get("algoId"):
                    protection_ids.append(int(result["algoId"]))
                trace_log(f"protection.TP{index}.end", correlation_id, duration_ms=round((time.monotonic() - target_started) * 1000, 2), success=True, http_status=getattr(client, "last_status_code", None))
            except BinanceDemoError as exc:
                trace_log(f"protection.TP{index}.end", correlation_id, duration_ms=round((time.monotonic() - target_started) * 1000, 2), success=False, http_status=getattr(client, "last_status_code", None), error_type=type(exc).__name__, error_message=safe_trace_error(exc, client))
                monitoring_targets.append(f"TP{index}")
    else:
        monitoring_targets.extend(["TP1", "TP2"])

    tp3_client_id = plan.setdefault("tp3_client_id", new_client_id("TP3"))
    tp3_started = time.monotonic()
    trace_log("protection.TP3.start", correlation_id)
    try:
        tp3_result = await post_algo(client, {
            **common,
            "type": "TAKE_PROFIT_MARKET",
            "triggerPrice": targets[2],
            "closePosition": "true",
            "clientAlgoId": tp3_client_id,
        })
        if tp3_result.get("algoId"):
            protection_ids.append(int(tp3_result["algoId"]))
        trace_log("protection.TP3.end", correlation_id, duration_ms=round((time.monotonic() - tp3_started) * 1000, 2), success=True, http_status=getattr(client, "last_status_code", None))
    except BinanceDemoError as exc:
        trace_log("protection.TP3.end", correlation_id, duration_ms=round((time.monotonic() - tp3_started) * 1000, 2), success=False, http_status=getattr(client, "last_status_code", None), error_type=type(exc).__name__, error_message=safe_trace_error(exc, client))
        monitoring_targets.append("TP3")

    plan["protection_ids"] = protection_ids
    plan["monitoring_targets"] = monitoring_targets
    plan["status"] = "OPEN"
    plan["protection_status"] = "KORUMA AKTİF" if not monitoring_targets else "STOP AKTİF · HEDEF İZLEME"
    plan["protected_at"] = utc_now()
    add_event(state, "KORUMA KURULDU", f"{symbol} Stop aktif; hedef planı Demo hesabına işlendi.")


async def cleanup_closed_plan(client: BinanceDemoClient, plan: dict[str, Any]) -> None:
    for algo_id in plan.get("protection_ids", []):
        try:
            await client.signed("DELETE", "/fapi/v1/algoOrder", {"symbol": plan["symbol"], "algoId": algo_id})
        except BinanceDemoError as exc:
            if exc.exchange_code not in {-2011, -2013}:
                continue


async def protection_loop(application: Any) -> None:
    while True:
        await asyncio.sleep(2)
        state = application.state.binance_demo
        if not credentials_configured() or not state.get("plans"):
            continue
        try:
            api_key, secret_key = load_demo_credentials()
            client = BinanceDemoClient(application.state.http, api_key, secret_key)
            await recover_pending_entry_intents(client, state)
            changed = False
            for plan in list(state["plans"].values()):
                if plan.get("status") in {"KAPANDI", "İPTAL", "GÜVENLİK İÇİN KAPATILDI"}:
                    continue
                rows = response_rows(
                    await client.signed("GET", "/fapi/v3/positionRisk", {"symbol": plan["symbol"]})
                )
                active_position = next((row for row in rows if Decimal(str(row.get("positionAmt", "0"))) != 0), None)
                if active_position is not None and plan.get("stop_protection_cancelled"):
                    continue
                if active_position is not None:
                    lifecycle_before = (
                        plan.get("remaining_quantity"), plan.get("position_status"),
                        plan.get("tp1_status"), plan.get("tp2_status"), plan.get("tp3_status"),
                    )
                    update_position_lifecycle(plan, Decimal(str(active_position["positionAmt"])))
                    lifecycle_after = (
                        plan.get("remaining_quantity"), plan.get("position_status"),
                        plan.get("tp1_status"), plan.get("tp2_status"), plan.get("tp3_status"),
                    )
                    changed = changed or lifecycle_before != lifecycle_after
                if active_position is not None and not plan.get("protection_ids"):
                    await install_protection(client, state, plan)
                    changed = True
                elif active_position is None and plan.get("position_status") == "OPEN":
                    await cleanup_closed_plan(client, plan)
                    plan["status"] = "KAPANDI"
                    plan["position_status"] = "CLOSED"
                    plan["remaining_quantity"] = "0"
                    plan["tp3_status"] = "FILLED"
                    plan["closed_at"] = utc_now()
                    add_event(state, "POZİSYON KAPANDI", f"{plan['symbol']} Demo pozisyonu kapandı; kalan bot emirleri temizlendi.")
                    changed = True
            if changed:
                persist_runtime(state)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            state["last_error"] = str(exc)[:240]


def init_binance_demo(application: Any) -> None:
    application.state.binance_demo = {
        "connected": False,
        "armed_until": 0,
        "last_checked": None,
        "last_error": None,
        "events": [],
        "plans": load_runtime(),
        "lock": asyncio.Lock(),
    }
    add_event(application.state.binance_demo, "GÜVENLİ BAŞLANGIÇ", "Demo emir kilidi kapalı başladı; yeniden elle açılması gerekir.")
    application.state.binance_demo_task = asyncio.create_task(protection_loop(application))


async def shutdown_binance_demo(application: Any) -> None:
    task = getattr(application.state, "binance_demo_task", None)
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@router.get("/status")
async def demo_status(request: Request) -> dict[str, Any]:
    try:
        from .exchange_connections import ensure_session_cache

        await ensure_session_cache(request)
    except (ImportError, RuntimeError, ValueError):
        pass
    result = public_status(state_for(request))
    result["configured"] = credentials_configured(request)
    return result


@router.post("/connect")
async def demo_connect(request: Request) -> dict[str, Any]:
    state = state_for(request)
    try:
        client = client_for(request)
        snapshot = await connect_snapshot(client)
        reconciliation = reconcile_demo_plans(state, snapshot)
        if reconciliation["changed"]:
            persist_runtime(state)
        snapshot = enrich_snapshot_with_plans(snapshot, state)
        state.update({"connected": True, "last_checked": utc_now(), "last_error": None})
        add_event(state, "BAĞLANTI BAŞARILI", "Binance Futures Demo hesabı doğrulandı; gerçek hesap kanalı kilitli.")
        return {**public_status(state), "account": snapshot}
    except BinanceDemoError as exc:
        state.update({"connected": False, "last_checked": utc_now(), "last_error": str(exc)[:240]})
        raise safe_exchange_error(exc) from exc


@router.get("/account")
async def demo_account(request: Request) -> dict[str, Any]:
    state = state_for(request)
    request_id = request_correlation_id(request)
    started = time.monotonic()
    trace_log("account.start", request_id)
    try:
        snapshot = await account_snapshot(client_for(request), request_id)
        reconciliation = reconcile_demo_plans(state, snapshot)
        if reconciliation["changed"]:
            persist_runtime(state)
        snapshot = enrich_snapshot_with_plans(snapshot, state)
        state.update({"connected": True, "last_checked": utc_now(), "last_error": None})
        result = {**public_status(state), **snapshot, "plans": list(state.get("plans", {}).values())[-12:]}
        result["configured"] = credentials_configured(request)
        trace_log("account.complete", request_id, duration_ms=round((time.monotonic() - started) * 1000, 2), success=True)
        return result
    except BinanceDemoError as exc:
        state.update({"connected": False, "last_checked": utc_now(), "last_error": str(exc)[:240]})
        trace_log("account.failed", request_id, duration_ms=round((time.monotonic() - started) * 1000, 2), success=False, error_type=type(exc).__name__, error_message=safe_trace_error(exc))
        raise safe_exchange_error(exc) from exc


@router.post("/arm")
async def demo_arm(request: Request, body: ArmRequest) -> dict[str, Any]:
    state = state_for(request)
    if body.confirmation.strip().upper() != "DEMO":
        raise HTTPException(422, "Onay alanına DEMO yazın.")
    try:
        snapshot = await account_snapshot(client_for(request))
    except BinanceDemoError as exc:
        raise safe_exchange_error(exc) from exc
    if snapshot["hedge_mode"]:
        raise HTTPException(409, "Binance Demo hesabında Pozisyon Modu 'Tek Yön / One-way' olmalı.")
    state["armed_until"] = time.time() + ARM_SECONDS
    state["connected"] = True
    state["last_checked"] = utc_now()
    add_event(state, "DEMO EMİR KİLİDİ AÇILDI", "Yalnızca Binance Futures Demo emirleri 10 dakika için açıldı.")
    return public_status(state)


@router.post("/disarm")
async def demo_disarm(request: Request) -> dict[str, Any]:
    state = state_for(request)
    state["armed_until"] = 0
    add_event(state, "DEMO EMİR KİLİDİ KAPANDI", "Yeni Demo giriş emirleri durduruldu; koruma emirleri çalışmaya devam eder.")
    return public_status(state)


@router.post("/order/test")
async def demo_order_test(request: Request, body: DemoOrderRequest) -> dict[str, Any]:
    state = state_for(request)
    try:
        client = client_for(request)
        if await position_mode(client):
            raise BinanceDemoError("Pozisyon Modu 'Tek Yön / One-way' olmalı.", http_status=409)
        spec = await build_order_spec(client, body)
        policy = getattr(request.app.state, "v21_demo", {}).get("settings", {})
        adjust_manual_spec_to_risk(spec, policy)
        await submit_entry(client, spec, test_only=True)
        add_event(state, "EMİR TESTİ BAŞARILI", f"{spec['symbol']} {spec['direction']} planı Demo doğrulamasından geçti; emir oluşmadı.")
        return {
            "ok": True,
            "message": "Demo emir testi başarılı; hiçbir pozisyon veya emir oluşturulmadı.",
            "preview": {key: value for key, value in spec.items() if not key.endswith("_decimal") and key not in {"step"}},
            "risk_adjusted": bool(spec.get("risk_adjusted")),
            "real_trading_locked": True,
        }
    except BinanceDemoError as exc:
        state["last_error"] = str(exc)[:240]
        raise safe_exchange_error(exc) from exc


@router.post("/order")
async def demo_order(request: Request, body: DemoOrderRequest) -> dict[str, Any]:
    return await execute_demo_order(request.app, body, source="MANUAL", request=request)


async def execute_demo_order(application: Any, body: DemoOrderRequest, *, source: str = "MANUAL", request: Request | None = None) -> dict[str, Any]:
    """Submit one hard-capped Demo order for the manual or V21 automation path."""
    state = application.state.binance_demo
    request_id = request_correlation_id(request)
    total_started = time.monotonic()
    trace_log("start", request_id, symbol=body.symbol, side="BUY" if body.direction == "LONG" else "SELL")
    if not armed(state) and source != "MANUAL":
        raise HTTPException(423, "Demo emir kilidi kapalı veya süresi doldu; önce 10 dakikalık kilidi açın.")
    async with traced_lock(state["lock"], request_id, "state"):
        try:
            client = client_for(request) if request is not None else BinanceDemoClient(application.state.http, *load_demo_credentials())
            client.trace_request_id = request_id
            await traced_stage("ensure_one_way_position_mode", request_id, ensure_one_way_position_mode(client), client=client)
            snapshot = await traced_stage("first_account_snapshot", request_id, account_snapshot(client, request_id), client=client)
            symbol = await resolve_demo_symbol(client, body.symbol)
            body = body.model_copy(update={"symbol": symbol})
            reconciliation = reconcile_demo_plans(state, snapshot)
            if reconciliation["changed"]:
                persist_runtime(state)
            spec = await traced_stage("build_order_spec", request_id, build_order_spec(client, body), client=client)
            policy = getattr(application.state, "v21_demo", {}).get("settings", {})
            v21_state = getattr(application.state, "v21_demo", {})
            if source == "MANUAL":
                adjust_manual_spec_to_risk(spec, policy)
            validate_entry_risk(
                snapshot, body, spec, policy,
                daily_realized_pnl=verified_realized_pnl(v21_state),
                paper_positions=v21_state.get("paper_positions", []),
                use_auto_universe=source != "MANUAL",
            )
            await traced_stage("set_isolated_margin", request_id, set_isolated_margin(client, spec["symbol"]), client=client)
            leverage_audit = await traced_stage("apply_verified_leverage", request_id, apply_verified_leverage(client, spec["symbol"], spec["leverage"]), client=client)
            spec = await traced_stage("second_build_order_spec", request_id, build_order_spec(client, body), client=client)
            if source == "MANUAL":
                adjust_manual_spec_to_risk(spec, policy)
            snapshot = await traced_stage("second_account_snapshot", request_id, account_snapshot(client, request_id), client=client)
            validate_entry_risk(
                snapshot, body, spec, policy,
                daily_realized_pnl=verified_realized_pnl(v21_state),
                paper_positions=v21_state.get("paper_positions", []),
                use_auto_universe=source != "MANUAL",
            )
            plan_id = uuid.uuid4().hex[:12]
            client_order_id = new_client_id("ENTRY")
            plan = {
                "id": plan_id,
                "position_id": plan_id,
                "symbol": spec["symbol"],
                "direction": spec["direction"],
                "order_type": spec["order_type"],
                "entry_price": spec["entry_price"],
                "quantity": spec["quantity"],
                "initial_quantity": spec["quantity"],
                "remaining_quantity": spec["quantity"],
                "margin_usdt": spec["margin_usdt"],
                "leverage": spec["leverage"],
                "requested_leverage": leverage_audit["requested_leverage"],
                "applied_leverage": leverage_audit["applied_leverage"],
                "margin_type": leverage_audit["margin_type"],
                "leverage_verified": leverage_audit["leverage_verified"],
                "configuration_source": leverage_audit["configuration_source"],
                "max_notional_value": leverage_audit["max_notional_value"],
                "leverage_verified_at": utc_now(),
                "stop_loss": spec["stop_loss"],
                "targets": spec["targets"],
                "step": decimal_text(spec["step"]),
                "min_qty": decimal_text(spec["min_qty"]),
                "entry_order_id": None,
                "entry_client_order_id": client_order_id,
                "status": "ENTRY_INTENT_PENDING",
                "position_status": "PENDING",
                "created_at": utc_now(),
                "demo_only": True,
                "source": source,
                "initial_stop_loss": spec["stop_loss"],
                "notional_usdt": spec["notional_usdt"],
                "risk_per_trade": spec["risk_per_trade"],
                "risk_adjusted": bool(spec.get("risk_adjusted")),
            }
            state.setdefault("plans", {})[plan_id] = plan
            persist_runtime(state)
            submit_started = time.monotonic()
            trace_log("submit_entry.start", request_id, entry_client_order_id=client_order_id, symbol=spec["symbol"], side=spec["side"], quantity=spec["quantity"])
            try:
                result = await submit_entry(client, spec, test_only=False, client_id=client_order_id)
            except Exception as exc:
                trace_log(
                    "submit_entry.end",
                    request_id,
                    entry_client_order_id=client_order_id,
                    duration_ms=round((time.monotonic() - submit_started) * 1000, 2),
                    http_status=getattr(client, "last_status_code", None),
                    success=False,
                    unknown_execution=getattr(exc, "unknown_execution", False),
                    symbol=spec["symbol"],
                    side=spec["side"],
                    quantity=spec["quantity"],
                    error_type=type(exc).__name__,
                    error_message=safe_trace_error(exc, client),
                )
                raise
            trace_log(
                "submit_entry.end",
                request_id,
                entry_client_order_id=client_order_id,
                duration_ms=round((time.monotonic() - submit_started) * 1000, 2),
                http_status=getattr(client, "last_status_code", None),
                success=True,
                unknown_execution=False,
                symbol=spec["symbol"],
                side=spec["side"],
                quantity=spec["quantity"],
            )
            plan["entry_order_id"] = int(result.get("orderId", 0)) or None
            plan["entry_client_order_id"] = result.get("clientOrderId") or client_order_id
            plan["status"] = "DOLUM BEKLİYOR"
            persist_runtime(state)
            add_event(
                state,
                "KALDIRAÇ DOĞRULANDI",
                f"{spec['symbol']} istenen {spec['leverage']}x, uygulanan {leverage_audit['applied_leverage']}x ISOLATED; emir güvenlik kontrolünden geçti.",
            )
            add_event(state, "DEMO EMİR GÖNDERİLDİ", f"{spec['symbol']} {spec['direction']} {spec['order_type']} emri Demo hesabına gönderildi ({source}).")
            if spec["order_type"] == "MARKET":
                await traced_stage("install_protection", request_id, install_protection(client, state, plan, request_id=request_id), client=client)
                persist_runtime(state)
            verified_snapshot = await traced_stage("final_account_snapshot", request_id, account_snapshot(client, request_id), client=client)
            response = {
                "ok": True,
                "message": f"Emir yalnızca Binance Futures Demo hesabına gönderildi; {leverage_audit['applied_leverage']}x ISOLATED doğrulandı.",
                "order": {
                    "symbol": result.get("symbol", spec["symbol"]),
                    "order_id": result.get("orderId"),
                    "client_order_id": result.get("clientOrderId"),
                    "status": result.get("status", plan["status"]),
                    "type": result.get("type", spec["order_type"]),
                    "side": result.get("side", spec["side"]),
                    "quantity": spec["quantity"],
                },
                "plan": plan,
                "risk_adjusted": bool(spec.get("risk_adjusted")),
                "risk_per_trade": spec["risk_per_trade"],
                "open_order_count": len(verified_snapshot["open_orders"]),
                "open_algo_order_count": len(verified_snapshot["open_algo_orders"]),
                "real_trading_locked": True,
            }
            trace_log("complete", request_id, duration_ms=round((time.monotonic() - total_started) * 1000, 2), success=True)
            return response
        except BinanceDemoError as exc:
            state["last_error"] = str(exc)[:240]
            trace_log("failed", request_id, duration_ms=round((time.monotonic() - total_started) * 1000, 2), success=False, error_type=type(exc).__name__, error_message=safe_trace_error(exc, client if "client" in locals() else None))
            raise safe_exchange_error(exc) from exc
        except Exception as exc:
            trace_log("failed", request_id, duration_ms=round((time.monotonic() - total_started) * 1000, 2), success=False, error_type=type(exc).__name__, error_message=safe_trace_error(exc, client if "client" in locals() else None))
            raise


@router.post("/order/cancel")
async def demo_cancel_order(request: Request, body: CancelOrderRequest) -> dict[str, Any]:
    try:
        symbol = normalize_symbol(body.symbol)
        result = await client_for(request).signed("DELETE", "/fapi/v1/order", {"symbol": symbol, "orderId": body.order_id})
        add_event(state_for(request), "EMİR İPTAL", f"{symbol} Demo emri iptal edildi.")
        return {"ok": True, "symbol": symbol, "order_id": result.get("orderId", body.order_id)}
    except BinanceDemoError as exc:
        raise safe_exchange_error(exc) from exc


@router.post("/algo/cancel")
async def demo_cancel_algo(request: Request, body: CancelAlgoRequest) -> dict[str, Any]:
    try:
        symbol = normalize_symbol(body.symbol)
        state = state_for(request)
        result = await client_for(request).signed("DELETE", "/fapi/v1/algoOrder", {"symbol": symbol, "algoId": body.algo_id})
        plan = mark_cancelled_protection(state.get("plans", {}), symbol, body.algo_id)
        if plan:
            persist_runtime(state)
            message = f"{symbol} STOP koruması iptal edildi; pozisyon artık otomatik korunmuyor."
        else:
            message = f"{symbol} koşullu Demo emri iptal edildi."
        add_event(state, "KORUMA İPTAL", message)
        return {"ok": True, "symbol": symbol, "algo_id": result.get("algoId", body.algo_id)}
    except BinanceDemoError as exc:
        raise safe_exchange_error(exc) from exc


@router.post("/position/close")
async def demo_close_position(request: Request, body: ClosePositionRequest) -> dict[str, Any]:
    if body.confirmation.strip().upper() != "DEMO KAPAT":
        raise HTTPException(422, "Pozisyonu kapatmak için DEMO KAPAT yazın.")
    try:
        symbol = normalize_symbol(body.symbol)
        state = state_for(request)
        client = client_for(request)
        result = await close_symbol_position(client, symbol, body.position_side)
        if result is None:
            raise BinanceDemoError("Bu paritede açık Demo pozisyonu yok.", http_status=404)
        for plan in state.get("plans", {}).values():
            if plan.get("symbol") == symbol and plan.get("position_status") == "OPEN":
                await cleanup_closed_plan(client, plan)
                plan.update({"status": "KAPANDI", "position_status": "CLOSED", "remaining_quantity": "0", "tp3_status": "FILLED", "closed_at": utc_now()})
        add_event(state, "POZİSYON KAPATILDI", f"{symbol} Demo pozisyonu reduce-only piyasa emriyle kapatıldı.")
        persist_runtime(state)
        return {"ok": True, "symbol": symbol, "order_id": result.get("orderId")}
    except BinanceDemoError as exc:
        raise safe_exchange_error(exc) from exc


@router.post("/position/reduce")
async def demo_reduce_position(request: Request, body: ReducePositionRequest) -> dict[str, Any]:
    if body.confirmation.strip().upper() != "DEMO AZALT":
        raise HTTPException(422, "Pozisyonu azaltmak için DEMO AZALT yazın.")
    try:
        symbol = normalize_symbol(body.symbol)
        result = await reduce_symbol_position(client_for(request), symbol, Decimal(str(body.quantity)), body.position_side)
        if result is None:
            raise BinanceDemoError("Bu paritede açık Demo pozisyonu yok.", http_status=404)
        add_event(state_for(request), "POZİSYON AZALTILDI", f"{symbol} Demo pozisyonu reduce-only piyasa emriyle azaltıldı.")
        return {"ok": True, "symbol": symbol, "order_id": result.get("orderId")}
    except BinanceDemoError as exc:
        raise safe_exchange_error(exc) from exc


@router.post("/emergency")
async def demo_emergency(request: Request, body: EmergencyRequest) -> dict[str, Any]:
    if body.confirmation.strip().upper() != "DEMO ACİL DURDUR":
        raise HTTPException(422, "Acil işlem için DEMO ACİL DURDUR yazın.")
    state = state_for(request)
    async with state["lock"]:
        try:
            client = client_for(request)
            snapshot = await account_snapshot(client)
            cancelled_orders = 0
            cancelled_algos = 0
            closed_positions = 0
            for item in snapshot["open_orders"]:
                if str(item.get("client_order_id") or "").startswith(CLIENT_PREFIX):
                    try:
                        await client.signed("DELETE", "/fapi/v1/order", {"symbol": item["symbol"], "orderId": item["order_id"]})
                        cancelled_orders += 1
                    except BinanceDemoError:
                        pass
            for item in snapshot["open_algo_orders"]:
                if str(item.get("client_algo_id") or "").startswith(CLIENT_PREFIX):
                    try:
                        await client.signed("DELETE", "/fapi/v1/algoOrder", {"symbol": item["symbol"], "algoId": item["algo_id"]})
                        cancelled_algos += 1
                    except BinanceDemoError:
                        pass
            if body.close_positions:
                for item in snapshot["positions"]:
                    if await close_symbol_position(client, item["symbol"]) is not None:
                        closed_positions += 1
            state["armed_until"] = 0
            for plan in state.get("plans", {}).values():
                if plan.get("status") not in {"KAPANDI", "İPTAL"}:
                    plan["status"] = "ACİL DURDURULDU"
            persist_runtime(state)
            add_event(state, "ACİL DEMO DURDURMA", f"{cancelled_orders} giriş, {cancelled_algos} koruma iptal; {closed_positions} Demo pozisyon kapatma emri.")
            return {
                "ok": True,
                "cancelled_bot_orders": cancelled_orders,
                "cancelled_bot_algos": cancelled_algos,
                "closed_demo_positions": closed_positions,
                "armed": False,
                "real_trading_locked": True,
            }
        except BinanceDemoError as exc:
            raise safe_exchange_error(exc) from exc
