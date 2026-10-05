import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from fastapi import HTTPException

logger = logging.getLogger(__name__)


def active_usdt_perpetual(contract: dict[str, Any]) -> bool:
    return contract.get("status") == "TRADING" and contract.get("contractType") == "PERPETUAL" and contract.get("quoteAsset") == "USDT"


def market_rows(info: dict[str, Any], tickers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ticker_map = {item["symbol"]: item for item in tickers}
    result = []
    for contract in info["symbols"]:
        if not active_usdt_perpetual(contract):
            continue
        symbol = contract["symbol"]
        ticker = ticker_map.get(symbol, {})
        values: dict[str, float | None] = {}
        for field, source in (("price", "lastPrice"), ("change", "priceChangePercent"), ("volume", "quoteVolume")):
            raw = ticker.get(source)
            value = float(raw) if raw is not None else None
            if value is not None and not math.isfinite(value):
                raise ValueError(f"Non-finite {source} for {symbol}")
            values[field] = value
        result.append({
            "symbol": symbol, "display": f"{contract.get('baseAsset', symbol[:-4])}/USDT",
            **values, "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT",
        })
    result.sort(key=lambda row: row["symbol"])
    return result


class MarketUniverseCache:
    """Shared read-only bulk cache; stale responses are explicitly marked."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.lock = asyncio.Lock()
        self.info: dict[str, Any] | None = None
        self.info_at = float("-inf")
        self.rows: list[dict[str, Any]] | None = None
        self.rows_at = float("-inf")
        self.retry_at = float("-inf")
        self.stale = False

    async def get(self, fetch: Callable[[str], Awaitable[httpx.Response]]) -> tuple[list[dict[str, Any]], bool]:
        async with self.lock:
            now = self.clock()
            if self.rows is not None and (now - self.rows_at < 3 or now < self.retry_at):
                return self.rows, self.stale
            if now < self.retry_at:
                raise HTTPException(503, "Market universe unavailable; upstream backoff active.")
            try:
                info = self.info
                if info is None or now - self.info_at >= 600:
                    response = await fetch("/fapi/v1/exchangeInfo")
                    response.raise_for_status()
                    info = response.json()
                    if not isinstance(info, dict) or not isinstance(info.get("symbols"), list):
                        raise ValueError("Invalid exchangeInfo payload")
                    if any(not isinstance(item, dict) or not isinstance(item.get("symbol"), str) for item in info["symbols"]):
                        raise ValueError("Invalid exchangeInfo symbol")
                    self.info = info
                    self.info_at = self.clock()
                response = await fetch("/fapi/v1/ticker/24hr")
                response.raise_for_status()
                tickers = response.json()
                if not isinstance(tickers, list) or any(not isinstance(item, dict) or not isinstance(item.get("symbol"), str) for item in tickers):
                    raise ValueError("Invalid bulk ticker payload")
                rows = market_rows(info, tickers)
                if not rows:
                    raise ValueError("No active USDT perpetual contracts returned")
            except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
                delay = 30.0
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {418, 429}:
                    raw = exc.response.headers.get("Retry-After")
                    if raw is not None:
                        try:
                            retry = float(raw)
                            if not math.isfinite(retry) or retry < 0:
                                raise ValueError("Non-finite or negative retry delay")
                            delay = max(delay, retry)
                        except ValueError:
                            logger.warning("Invalid market Retry-After header: %r", raw)
                self.retry_at = self.clock() + delay
                self.stale = True
                logger.warning("Market universe refresh failed; cached=%s error=%s", self.rows is not None, exc)
                if self.rows is None:
                    raise HTTPException(502, "Binance Futures market universe unavailable.") from exc
                return self.rows, True
            self.rows = rows
            self.rows_at = self.clock()
            self.stale = False
            return rows, False
