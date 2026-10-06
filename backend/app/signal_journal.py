"""Separate, best-effort LIVE observation storage. No exchange or execution I/O."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import queue
import sqlite3
import threading
import time
from contextlib import contextmanager
from collections.abc import Generator
from collections import deque
from contextvars import ContextVar, Token
from itertools import count
from pathlib import Path
from typing import Any

from .local_storage import DATA_DIR
from .web_security import env_flag

logger = logging.getLogger(__name__)
INTERVALS = {"15m": 900, "1h": 3600, "4h": 14400}
ROUND: ContextVar[str | None] = ContextVar("signal_observation_round", default=None)
SEQUENCE = count()
PRODUCER_ERRORS: deque[str] = deque(maxlen=16)
NULL_FIELDS = (
    "return_1h", "return_2h", "return_4h", "mae_r_gross", "mfe_r_gross",
    "mae_r_costs", "mfe_r_costs", "hypothetical_result", "hypothetical_r_gross",
    "hypothetical_r_costs", "outcomes_as_of", "gross_pnl", "commission_usdt",
    "funding_usdt", "actual_fill_price", "actual_slippage_price", "initial_risk_usdt",
    "net_pnl", "net_r",
)


def report_error(message: str, *args: Any) -> None:
    try:
        logger.error(message, *args)
    except Exception:
        # Even a broken logging handler must not escape into the order path.
        return


def number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def signal_id(symbol: str, direction: str, close_time: int) -> str:
    return hashlib.sha256(f"{symbol.upper()}|{direction.upper()}|{close_time}".encode()).hexdigest()


class Journal:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self) -> Generator[sqlite3.Connection, None, None]:
        if self.path.suffix != ".sqlite3":
            raise ValueError("Observation storage must use a separate .sqlite3 file")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=0.25)
        try:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if (tables and "journal_metadata" not in tables) or tables - {"signals", "rounds", "candles", "trades", "journal_metadata"}:
                raise ValueError("Observation storage must not share another application's database")
            if "journal_metadata" in tables and [row[0] for row in db.execute("SELECT version FROM journal_metadata")] != [1]:
                raise ValueError("Unsupported observation schema")
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS journal_metadata(version INTEGER PRIMARY KEY);
                INSERT OR IGNORE INTO journal_metadata VALUES(1);
                CREATE TABLE IF NOT EXISTS signals(
                    signal_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, direction TEXT NOT NULL,
                    ts_decision INTEGER NOT NULL, decision TEXT NOT NULL, intent_id TEXT,
                    payload TEXT NOT NULL, outcomes TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS signals_intent ON signals(intent_id);
                CREATE TABLE IF NOT EXISTS rounds(
                    round_id TEXT NOT NULL, signal_id TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(round_id, signal_id)
                );
                CREATE TABLE IF NOT EXISTS candles(
                    symbol TEXT NOT NULL, interval TEXT NOT NULL, open_time INTEGER NOT NULL,
                    close_time INTEGER NOT NULL, observed_at REAL NOT NULL,
                    payload TEXT NOT NULL, PRIMARY KEY(symbol, interval, open_time)
                );
                CREATE TABLE IF NOT EXISTS trades(
                    intent_id TEXT PRIMARY KEY, signal_id TEXT, payload TEXT NOT NULL
                );
            """)
            versions = [row[0] for row in db.execute("SELECT version FROM journal_metadata")]
            if versions != [1]:
                raise ValueError("Unsupported observation schema")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def record(self, record: dict[str, Any], round_id: str) -> None:
        encoded = json.dumps(record, allow_nan=False, sort_keys=True)
        with self.connect() as db:
            db.execute("""
                INSERT INTO signals(signal_id,symbol,direction,ts_decision,decision,intent_id,payload)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(signal_id) DO UPDATE SET
                    decision=CASE WHEN excluded.decision='ACCEPTED' THEN 'ACCEPTED' ELSE signals.decision END,
                    intent_id=COALESCE(signals.intent_id,excluded.intent_id),
                    payload=CASE WHEN excluded.decision='ACCEPTED' AND signals.decision!='ACCEPTED'
                        THEN excluded.payload ELSE signals.payload END
            """, (record["signal_id"], record["symbol"], record["direction"], record["ts_decision"],
                  record["decision"], record.get("intent_id"), encoded))
            db.execute("INSERT OR IGNORE INTO rounds VALUES(?,?,?)", (round_id, record["signal_id"], encoded))
            if record["decision"] == "ACCEPTED":
                db.execute("UPDATE trades SET signal_id=? WHERE intent_id=? AND signal_id IS NULL",
                           (record["signal_id"], record.get("intent_id")))

    def candles(self, symbol: str, interval: str, rows: list[Any], as_of: float) -> None:
        duration = INTERVALS.get(interval)
        if duration is None:
            raise ValueError("Unsupported observation candle interval")
        values = []
        for row in rows:
            if isinstance(row, list) and len(row) >= 6:
                candle = dict(zip(("time", "open", "high", "low", "close", "volume"), [row[0] / 1000, *row[1:6]]))
            elif isinstance(row, dict):
                candle = row
            else:
                raise ValueError("Invalid observation candle")
            opening_value = number(candle.get("time"))
            if opening_value is None:
                raise ValueError("Invalid candle opening time")
            opening = int(opening_value)
            closing = opening + duration
            if isinstance(row, list) and len(row) >= 7:
                declared_close = number(row[6])
                if declared_close is None:
                    raise ValueError("Invalid provider candle closing time")
                if declared_close / 1000 >= as_of:
                    continue
                if abs(declared_close / 1000 - (closing - 0.001)) > 0.001:
                    raise ValueError("Provider candle duration does not match interval")
            if closing > as_of:
                continue
            numbers = {key: number(candle.get(key)) for key in ("time", "open", "high", "low", "close", "volume")}
            if any(value is None for value in numbers.values()):
                raise ValueError("Invalid/non-finite observation OHLCV")
            if not 0 < numbers["low"] <= min(numbers["open"], numbers["close"]) <= max(numbers["open"], numbers["close"]) <= numbers["high"] or numbers["volume"] < 0:
                raise ValueError("Invalid observation OHLCV bounds")
            values.append((symbol, interval, opening, closing, as_of, json.dumps(numbers, allow_nan=False)))
        with self.connect() as db:
            db.executemany("INSERT OR IGNORE INTO candles VALUES(?,?,?,?,?,?)", values)

    def closed_candles(self, symbol: str, interval: str, as_of: int) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT payload FROM candles WHERE symbol=? AND interval=? AND close_time<=? ORDER BY open_time",
                              (symbol, interval, as_of)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def trade(self, intent_id: str, update: dict[str, Any]) -> None:
        update = dict(update)
        for key in ("gross_pnl", "commission_usdt", "funding_usdt", "initial_risk_usdt", "expected_entry",
                    "actual_fill_price", "actual_slippage_price"):
            if key in update and update[key] is not None:
                parsed = number(update[key])
                if parsed is None:
                    raise ValueError(f"Invalid trade observation: {key}")
                update[key] = parsed
        with self.connect() as db:
            previous = db.execute("SELECT payload FROM trades WHERE intent_id=?", (intent_id,)).fetchone()
            payload = json.loads(previous[0]) if previous else {}
            for key in ("gross_pnl", "commission_usdt", "funding_usdt", "actual_fill_price", "actual_slippage_price", "initial_risk_usdt"):
                payload.setdefault(key, None)
            payload.setdefault("funding_complete", False)
            payload.setdefault("commission_complete", False)
            update = {key: value for key, value in update.items()
                      if key not in {"initial_risk_usdt", "expected_entry"} or payload.get(key) is None}
            payload.update(update)
            gross, commission, funding = (number(payload.get(key)) for key in ("gross_pnl", "commission_usdt", "funding_usdt"))
            complete = payload.get("commission_complete") is True and payload.get("funding_complete") is True
            net = gross - commission + funding if complete and all(value is not None for value in (gross, commission, funding)) else None
            risk = number(payload.get("initial_risk_usdt"))
            payload.update(net_pnl=net, net_r=net / risk if net is not None and risk is not None and risk > 0 else None)
            linked = db.execute("SELECT signal_id FROM signals WHERE intent_id=? AND decision='ACCEPTED'", (intent_id,)).fetchone()
            db.execute("""
                INSERT INTO trades VALUES(?,?,?) ON CONFLICT(intent_id) DO UPDATE SET
                signal_id=COALESCE(trades.signal_id,excluded.signal_id),payload=excluded.payload
            """, (intent_id, linked[0] if linked else None, json.dumps(payload, allow_nan=False)))

    def rows(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("""
                SELECT s.payload,s.decision,s.outcomes,t.payload FROM signals s
                LEFT JOIN trades t ON t.signal_id=s.signal_id ORDER BY s.ts_decision,s.signal_id
            """).fetchall()
        result = []
        for raw, decision, outcomes, trade in rows:
            record = json.loads(raw)
            for key in NULL_FIELDS:
                record.setdefault(key, None)
            record.update(decision=decision, **json.loads(outcomes))
            if decision == "ACCEPTED":
                record["reject_reason"] = None
            if trade:
                record.update(json.loads(trade))
            result.append(record)
        return result

    def expected_entry(self, intent_id: str) -> float | None:
        with self.connect() as db:
            row = db.execute("SELECT payload FROM trades WHERE intent_id=?", (intent_id,)).fetchone()
        return number(json.loads(row[0]).get("expected_entry")) if row else None

    def enrich(self, identifier: str, update: dict[str, Any]) -> None:
        with self.connect() as db:
            row = db.execute("SELECT payload FROM signals WHERE signal_id=?", (identifier,)).fetchone()
            if row is None:
                raise ValueError("Unknown signal ID")
            payload = json.loads(row[0])
            payload.update(update)
            db.execute("UPDATE signals SET payload=? WHERE signal_id=?", (json.dumps(payload, allow_nan=False), identifier))


class Observer:
    def __init__(self, journal: Journal, capacity: int = 256):
        self.journal = journal
        self.queue: queue.Queue[tuple[str, str | None, dict[str, Any]]] = queue.Queue(capacity)
        self.thread: threading.Thread | None = None
        self.start_lock = threading.Lock()
        self.stopping = threading.Event()
        self.dropped = 0
        self.failures = 0
        self.rounds: dict[str, dict[str, Any]] = {}

    def publish(self, kind: str, round_id: str | None, data: dict[str, Any]) -> None:
        try:
            if self.thread is None:
                # No database operation is performed on the caller's thread.
                if not self.start_lock.acquire(blocking=False):
                    self.dropped += 1
                    return
                try:
                    if self.thread is None:
                        self.thread = threading.Thread(target=self.run, name="live-signal-observer", daemon=True)
                        self.thread.start()
                finally:
                    self.start_lock.release()
            self.queue.put_nowait((kind, round_id, data))
        except Exception as exc:
            self.thread = None if self.thread is not None and not self.thread.is_alive() else self.thread
            self.dropped += 1
            PRODUCER_ERRORS.append(type(exc).__name__)

    def run(self) -> None:
        reported_drops = 0
        while not self.stopping.is_set() or not self.queue.empty():
            while PRODUCER_ERRORS:
                report_error("SIGNAL_OBSERVATION_ENQUEUE_FAILED error_type=%s", PRODUCER_ERRORS.popleft())
            try:
                item = self.queue.get(timeout=0.1)
            except queue.Empty:
                item = None
            if self.dropped != reported_drops:
                report_error("SIGNAL_OBSERVATION_DROPPED total=%s", self.dropped)
                reported_drops = self.dropped
            if item is None:
                continue
            kind, round_id, data = item
            try:
                self.handle(kind, round_id, data)
            except Exception as exc:
                self.failures += 1
                report_error("SIGNAL_OBSERVATION_FAILED operation=%s error_type=%s", kind, type(exc).__name__)
            finally:
                self.queue.task_done()

    def handle(self, kind: str, round_id: str | None, data: dict[str, Any]) -> None:
        if kind == "candles":
            self.journal.candles(**data)
            return
        if kind == "trade":
            self.journal.trade(**data)
            return
        if kind == "fills":
            entries = data.pop("entries")
            quantities = [(number(row.get("qty")), number(row.get("price"))) for row in entries]
            valid = [(quantity, price) for quantity, price in quantities if quantity is not None and price is not None and quantity > 0 and price > 0]
            quantity = sum(item[0] for item in valid)
            price = sum(qty * value for qty, value in valid) / quantity if quantity and len(valid) == len(quantities) else None
            expected = self.journal.expected_entry(data["intent_id"])
            if expected is None:
                expected = number(data.get("expected_entry"))
            if "commission_rows" in data:
                fees = data.pop("commission_rows")
                data["commission_complete"] = bool(fees) and all(
                    row.get("commissionAsset") == "USDT" and number(row.get("commission")) is not None
                    for row in fees
                )
                if not data["commission_complete"]:
                    data["commission_usdt"] = None
            if "close_rows" in data:
                closes = data.pop("close_rows")
                amounts = [number(row.get("realizedPnl")) for row in closes]
                data["gross_pnl"] = sum(amounts) if amounts and all(value is not None for value in amounts) else None
                close_quantities = [number(row.get("qty")) for row in closes]
                complete = quantity > 0 and bool(close_quantities) and all(qty is not None and qty > 0 for qty in close_quantities)
                complete = complete and math.isclose(sum(close_quantities), quantity, rel_tol=1e-9, abs_tol=1e-12)
                data["commission_complete"] = data.get("commission_complete") is True and complete
                if not data["commission_complete"]:
                    data["commission_usdt"] = None
            if price is not None:
                data.update(actual_fill_price=price, actual_slippage_price=price - expected if expected is not None else None)
            intent_id = data.pop("intent_id")
            self.journal.trade(intent_id, data)
            return
        if round_id is None:
            return
        if kind == "start":
            self.rounds[round_id] = {**data, "candidates": {}}
            if len(self.rounds) > 64:
                oldest = next(iter(self.rounds))
                report_error("SIGNAL_OBSERVATION_INCOMPLETE round=%s", oldest)
                self.finish(oldest, "OBSERVATION_INCOMPLETE")
            return
        current = self.rounds.get(round_id)
        if current is None:
            return
        if kind == "universe":
            exchange = data.get("exchange_info")
            eligible = {row.get("symbol") for row in exchange.get("symbols", [])
                        if isinstance(row, dict) and row.get("status") == "TRADING"
                        and row.get("contractType") == "PERPETUAL" and row.get("quoteAsset") == "USDT"} if isinstance(exchange, dict) else None
            selected_symbols = {row["symbol"] for row in data["selected"]}
            for ticker in data["tickers"] if isinstance(data["tickers"], list) else []:
                if isinstance(ticker, dict) and ticker.get("symbol") in data["allowed_symbols"]:
                    symbol = ticker["symbol"]
                    base = symbol[:-4] if symbol.endswith("USDT") else ""
                    volume = number(ticker.get("quoteVolume") or 0)
                    move = number(ticker.get("priceChangePercent") or 0)
                    price = number(ticker.get("lastPrice") or 0)
                    checks = [
                        ("SYMBOL_ELIGIBILITY", symbol in eligible if eligible is not None else None),
                        ("SYMBOL_OCCUPIED", symbol not in data["occupied"]),
                        ("BLOCKED_BASE_ASSET", base not in data.get("blocked_bases", ())),
                        ("LEVERAGED_TOKEN", not any(word in base for word in ("UP", "DOWN", "BULL", "BEAR"))),
                        ("VOLUME_BELOW_MIN", volume >= data["minimum_volume"] if volume is not None else None),
                        ("MOVE_BELOW_MIN", abs(move) >= data["minimum_move"] if move is not None else None),
                        ("INVALID_MARKET_PRICE", price > 0 if price is not None else None),
                    ]
                    reason = next((key for key, passed in checks if passed is False), None)
                    if symbol not in selected_symbols and reason is None:
                        reason = "INVALID_TICKER_DATA" if None in (volume, move, price) else "MARKET_UNIVERSE_FILTER"
                    current["candidates"][symbol] = {
                        "symbol": symbol, "first_reject": None if symbol in selected_symbols else reason,
                        "quote_volume_24h": volume, "price_change_pct_24h": move, "market_price": price,
                        "universe_gates": [{"key": key, "passed": passed, "source": "SHADOW", "detail": ""}
                                           for key, passed in checks],
                    }
        elif kind == "candidate":
            candidate = current["candidates"].setdefault(data["symbol"], {"symbol": data["symbol"]})
            if candidate.get("first_reject") and data.get("first_reject"):
                data = {key: value for key, value in data.items() if key != "first_reject"}
            candidate.update(data)
            if candidate.get("execution_reject") and not candidate.get("first_reject"):
                candidate["first_reject"] = candidate["execution_reject"]
        elif kind == "candidates":
            for item in data["candidates"]:
                current["candidates"].setdefault(item["symbol"], {"symbol": item["symbol"]})
        elif kind == "scope":
            for symbol, item in current["candidates"].items():
                if symbol not in data["allowed_symbols"] and not item.get("first_reject"):
                    item["first_reject"] = "SYMBOL_NOT_ALLOWED"
        elif kind == "selected":
            for symbol, item in current["candidates"].items():
                if symbol not in data["symbols"] and not item.get("first_reject"):
                    item["first_reject"] = "RANK_NOT_SELECTED"
        elif kind == "finish":
            self.finish(round_id, data.get("reason"))

    def finish(self, round_id: str, reason: str | None) -> None:
        from .signal_observation import build_record

        current = self.rounds.pop(round_id)
        for candidate in current["candidates"].values():
            try:
                record = build_record(current, candidate, self.journal)
                if not candidate.get("accepted") and not candidate.get("first_reject"):
                    record["reject_reason"] = reason or "NOT_EXECUTED"
                    record["reject_reasons"] = list(dict.fromkeys([record["reject_reason"], *record["reject_reasons"]]))
                self.journal.record(record, round_id)
            except Exception as exc:
                self.failures += 1
                report_error("SIGNAL_OBSERVATION_FAILED operation=record symbol=%s error_type=%s", candidate["symbol"], type(exc).__name__)

    def close(self, timeout: float = 2) -> None:
        self.stopping.set()
        if self.thread:
            self.thread.join(timeout)
            if self.thread.is_alive():
                report_error("SIGNAL_OBSERVATION_SHUTDOWN_INCOMPLETE pending=%s", self.queue.qsize())


_observer: Observer | None = None


def emit(kind: str, **data: Any) -> None:
    """Only bounded, nonblocking enqueue; failures never escape to trading."""
    global _observer
    try:
        if not env_flag("PROTREBOT_SIGNAL_JOURNAL_ENABLED", default=True):
            return
        if _observer is None or _observer.stopping.is_set():
            path = Path(os.environ.get("PROTREBOT_SIGNAL_JOURNAL_PATH") or DATA_DIR / "live-signal-journal.sqlite3")
            _observer = Observer(Journal(path))
        _observer.publish(kind, ROUND.get(), data)
    except Exception as exc:
        if _observer is not None:
            _observer.dropped += 1
        PRODUCER_ERRORS.append(type(exc).__name__)


def start_round(policy: dict[str, Any], snapshot: dict[str, Any], daily: dict[str, Any], plans: dict[str, Any]) -> Token | None:
    token = None
    try:
        if not env_flag("PROTREBOT_SIGNAL_JOURNAL_ENABLED", default=True):
            return None
        identifier = f"{time.time_ns()}-{next(SEQUENCE)}"
        token = ROUND.set(identifier)
        emit("start", policy={**policy, "allowed_symbols": list(policy.get("allowed_symbols") or [])},
             snapshot={**snapshot, "positions": [dict(row) for row in snapshot.get("positions", [])],
                       "open_orders": [dict(row) for row in snapshot.get("open_orders", [])]},
             daily=dict(daily), plans=[dict(row) for row in plans.values()],
             ts_scan=time.time(), ts_decision=int(time.time() // 900) * 900)
        return token
    except Exception as exc:
        if token is not None:
            ROUND.reset(token)
        PRODUCER_ERRORS.append(type(exc).__name__)
        return None


def finish_round(token: Token | None, reason: str | None) -> None:
    try:
        if token is not None:
            emit("finish", reason=reason)
            ROUND.reset(token)
    except Exception:
        if _observer is not None:
            _observer.dropped += 1
        PRODUCER_ERRORS.append("FinishRoundError")


def shutdown() -> None:
    global _observer
    if _observer is not None:
        try:
            _observer.close()
        except Exception as exc:
            report_error("SIGNAL_OBSERVATION_SHUTDOWN_FAILED error_type=%s", type(exc).__name__)
        finally:
            _observer = None
