import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from fastapi import HTTPException

from .backtest_data import INTERVALS as INTERVAL_SECONDS
from .strategies import original_v2_demo as original_strategy

logger = logging.getLogger(__name__)

ORIGINAL_DEFAULT_SYMBOL_COUNT = 40
ORIGINAL_MIN_SYMBOL_COUNT = 10
ORIGINAL_MAX_SYMBOL_COUNT = 100
ORIGINAL_MIN_QUOTE_VOLUME_USDT = 1_000_000.0
ORIGINAL_MIN_SYMBOL_AGE_DAYS = max(
    45,
    math.ceil(original_strategy.CANDLE_FETCH_LIMIT * max(
        INTERVAL_SECONDS[interval] for interval in original_strategy.INTERVALS
    ) / 86400),
)
ORIGINAL_MAX_SPREAD_BPS = 8.0


def active_usdt_perpetual(contract: dict[str, Any]) -> bool:
    return contract.get("status") == "TRADING" and contract.get("contractType") == "PERPETUAL" and contract.get("quoteAsset") == "USDT"


def ranked_original_live_symbols(
    exchange_info: Any,
    tickers_24h: Any,
    book_tickers: Any,
    *,
    now_ms: int,
    diagnostics: dict[str, Any] | None = None,
) -> tuple[list[str], int]:
    counts = {
        "active_contracts": 0,
        "age_below_required_history": 0,
        "market_data_missing": 0,
        "invalid_market_data": 0,
        "volume_below_minimum": 0,
        "spread_too_wide": 0,
        "eligible": 0,
    }

    def publish_diagnostics() -> None:
        if diagnostics is not None:
            diagnostics.clear()
            diagnostics.update(counts)

    if (
        not isinstance(exchange_info, dict)
        or not isinstance(exchange_info.get("symbols"), list)
        or not isinstance(tickers_24h, list)
        or not isinstance(book_tickers, list)
        or type(now_ms) is not int
        or now_ms <= 0
    ):
        raise ValueError("Invalid Binance Futures universe payload")

    contracts = exchange_info["symbols"]
    if any(not isinstance(row, dict) or not isinstance(row.get("symbol"), str) for row in contracts):
        raise ValueError("Invalid Binance Futures contract metadata")
    if len({row["symbol"] for row in contracts}) != len(contracts):
        raise ValueError("Duplicate Binance Futures contract symbol")

    def symbol_map(rows: list[Any], label: str) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("symbol"), str):
                raise ValueError(f"Invalid Binance Futures {label} row")
            symbol = row["symbol"]
            if symbol in result:
                raise ValueError(f"Duplicate Binance Futures {label} symbol")
            result[symbol] = row
        return result

    ticker_map = symbol_map(tickers_24h, "24h ticker")
    book_map = symbol_map(book_tickers, "book ticker")
    minimum_age_ms = ORIGINAL_MIN_SYMBOL_AGE_DAYS * 24 * 60 * 60 * 1000
    active_contracts = [contract for contract in contracts if active_usdt_perpetual(contract)]
    counts["active_contracts"] = len(active_contracts)
    if not active_contracts:
        publish_diagnostics()
        raise ValueError("No active USDT perpetual contracts returned")
    age_qualified: set[str] = set()
    for contract in active_contracts:
        try:
            onboard_ms = int(contract["onboardDate"])
        except (KeyError, TypeError, ValueError, OverflowError):
            counts["invalid_market_data"] += 1
            continue
        if onboard_ms > 0 and now_ms - onboard_ms >= minimum_age_ms:
            age_qualified.add(contract["symbol"])
        elif onboard_ms > 0:
            counts["age_below_required_history"] += 1
        else:
            counts["invalid_market_data"] += 1
    missing_market_data = age_qualified - ticker_map.keys() | age_qualified - book_map.keys()
    counts["market_data_missing"] = len(missing_market_data)
    if not age_qualified or missing_market_data:
        publish_diagnostics()
        raise ValueError("Incomplete Binance Futures daily market data")
    eligible: list[tuple[str, float]] = []
    for contract in contracts:
        if not active_usdt_perpetual(contract):
            continue
        symbol = contract["symbol"]
        if not symbol.isascii() or not symbol.isalnum() or not symbol.isupper() or not symbol.endswith("USDT"):
            continue
        try:
            onboard_ms = int(contract["onboardDate"])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if onboard_ms <= 0:
            continue
        if now_ms - onboard_ms < minimum_age_ms:
            continue

        ticker = ticker_map.get(symbol)
        book = book_map.get(symbol)
        if ticker is None or book is None:
            counts["market_data_missing"] += 1
            continue
        try:
            quote_volume = float(ticker["quoteVolume"])
            bid = float(book["bidPrice"])
            ask = float(book["askPrice"])
        except (KeyError, TypeError, ValueError, OverflowError):
            counts["invalid_market_data"] += 1
            continue
        if not all(math.isfinite(value) for value in (quote_volume, bid, ask)) or bid <= 0 or ask < bid:
            counts["invalid_market_data"] += 1
            continue
        if quote_volume < ORIGINAL_MIN_QUOTE_VOLUME_USDT:
            counts["volume_below_minimum"] += 1
            continue
        midpoint = (ask + bid) / 2
        spread_bps = (ask - bid) / midpoint * 10_000
        if spread_bps > ORIGINAL_MAX_SPREAD_BPS:
            counts["spread_too_wide"] += 1
            continue
        eligible.append((symbol, quote_volume))

    eligible.sort(key=lambda row: (-row[1], row[0]))
    ranked = [symbol for symbol, _volume in eligible[:ORIGINAL_MAX_SYMBOL_COUNT]]
    counts["eligible"] = len(eligible)
    publish_diagnostics()
    return ranked, len(eligible)


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
