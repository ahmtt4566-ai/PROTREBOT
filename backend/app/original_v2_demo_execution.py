"""Opt-in Demo execution; immutable exits survive feature-flag changes.

The decision adapter and fixed policy are unchanged. Exchange fills, funding
and intrabar ordering are venue facts, not next-open/STOP_FIRST guarantees.
Neither flag changes legacy Demo defaults or authorizes an owner/session.
"""

from __future__ import annotations

import logging
import math
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, NoReturn

from fastapi import HTTPException

from . import binance_demo as demo
from .strategies import original_v2_demo as strategy
from .strategies.contracts import StrategyInput, StrategyResult

SEND_FLAG = "PROTREBOT_BINANCE_DEMO_KAIS_ORIGINAL_V2_SEND_ORDERS"
EXIT_ID = "original-v2-tp60-tp3-fixed-stop-v1"
MODEL_VERSION = "original-v2-demo-execution-v1"
IDENTITY_FIELDS = (
    "strategy_id", "source_profile_id", "profile_hash", "execution_policy_id",
    "decision_time", "signal_timestamp", "plan_id", "model_version",
)
logger = logging.getLogger(__name__)


def fail(reason: str) -> NoReturn:
    logger.error("ORIGINAL_DEMO_EXECUTION_REJECTED code=%s", reason)
    raise demo.BinanceDemoError(reason, http_status=409)


def send_orders_enabled() -> bool:
    value = os.getenv(SEND_FLAG, "false").strip().lower()
    if value in {"false", "0", "no", "off"}:
        return False
    if value in {"true", "1", "yes", "on"}:
        return True
    fail("INVALID_ORIGINAL_SEND_FLAG")


def validate_result(result: StrategyResult) -> None:
    if result.signal.provenance.strategy_id != strategy.STRATEGY_ID:
        fail("UNKNOWN_DEMO_STRATEGY_ID")
    strategy.decision_record(result)
    if result.signal.exit_policy_id != EXIT_ID:
        fail("ORIGINAL_EXIT_ID_MISMATCH")
    if result.signal.symbol not in strategy.fixed_profile()["execution_policy"]["allowed_symbols"]:
        fail("SYMBOL_OUTSIDE_FIXED_UNIVERSE")


def identity(result: StrategyResult, plan_id: str) -> dict[str, Any]:
    validate_result(result)
    profile = strategy.fixed_profile()
    return {
        "strategy_id": strategy.STRATEGY_ID,
        "source_profile_id": profile["source_profile_id"],
        "profile_hash": profile["source_profile_sha256"],
        "execution_policy_id": EXIT_ID,
        "decision_time": result.signal.decision_time,
        "signal_timestamp": result.signal.signal_open_time,
        "plan_id": plan_id,
        "model_version": MODEL_VERSION,
    }


def original_plan(plan: dict[str, Any]) -> bool:
    if plan.get("strategy_id") is None:
        return False
    if plan.get("strategy_id") != strategy.STRATEGY_ID:
        fail("UNKNOWN_DEMO_STRATEGY_ID")
    profile = strategy.fixed_profile()
    if (
        plan.get("source_profile_id") != profile["source_profile_id"]
        or plan.get("profile_hash") != profile["source_profile_sha256"]
        or plan.get("execution_policy_id") != EXIT_ID
        or plan.get("model_version") != MODEL_VERSION
        or not plan.get("plan_id")
        or plan.get("plan_id") != plan.get("id")
        or type(plan.get("decision_time")) is not int
        or type(plan.get("signal_timestamp")) is not int
    ):
        fail("ORIGINAL_PLAN_IDENTITY_INVALID")
    if plan.get("stop_loss") != plan.get("initial_stop_loss"):
        fail("ORIGINAL_INITIAL_STOP_CHANGED")
    return True


def tp1_quantity(plan: dict[str, Any], quantity: Decimal, mark_entry: Decimal) -> Decimal:
    if not original_plan(plan):
        fail("ORIGINAL_PLAN_REQUIRED")
    if plan.get("tp1_fill_confirmed") is True:
        return Decimal(0)
    initial = Decimal(str(plan.get("initial_quantity") or quantity))
    partial = demo.floor_step(initial * Decimal("0.60"), Decimal(str(plan["step"])))
    allocation = partial if (
        partial >= Decimal(str(plan["min_qty"]))
        and partial * mark_entry >= Decimal(str(plan["min_notional"]))
    ) else Decimal(0)
    remaining = max(Decimal(0), allocation - Decimal(str(plan.get("tp1_filled_quantity") or 0)))
    return min(quantity, remaining)


async def build_spec(client: demo.BinanceDemoClient, result: StrategyResult) -> dict[str, Any]:
    validate_result(result)
    if not result.signal.strategy_eligible:
        fail("CANONICAL_ENTRY_NOT_ELIGIBLE")
    signal = result.legacy["analysis"]
    risk = strategy.risk_preview(signal["entry"], signal["stop_loss"], enabled=True)
    if risk["margin_usdt"] < 5:
        fail("minimum_margin")
    symbol = result.signal.symbol
    price = await demo.ticker_price(client, symbol)
    rules = await demo.symbol_rules(client, symbol)
    entry = demo.round_tick(price, rules["tick"])
    stop = demo.round_tick(Decimal(str(signal["stop_loss"])), rules["tick"])
    targets = [demo.round_tick(Decimal(str(signal[key])), rules["tick"]) for key in ("tp1", "tp2", "tp3")]
    demo.validate_levels(signal["direction"], entry, stop, targets)
    from .strategies.original_gap_risk import PROFILE
    from .strategies.original_offline_risk import OfflineRiskError

    try:
        rounded_risk = PROFILE.size(float(entry), float(stop), margin_limit=risk["margin_usdt"])
    except OfflineRiskError as exc:
        fail(exc.reason)
    quantity = demo.floor_step(Decimal(str(rounded_risk["notional_usdt"])) / entry, rules["step"])
    if quantity < rules["min_qty"] or quantity * entry < rules["min_notional"]:
        fail("min_notional")
    if quantity > rules["max_qty"]:
        fail("native_spec_rejected")
    policy = strategy.fixed_profile()["execution_policy"]
    movement = abs(float((targets[0] - entry) / entry))
    notional = float(quantity * entry)
    costs = notional * (policy["fee_bps_per_side"] + policy["slippage_bps_per_side"]) * 2 / 10_000
    if notional * movement - costs < policy["minimum_net_reward_usdt"]:
        fail("cost_filter")
    return {
        "symbol": symbol, "direction": signal["direction"],
        "side": "BUY" if signal["direction"] == "LONG" else "SELL",
        "close_side": "SELL" if signal["direction"] == "LONG" else "BUY",
        "order_type": "MARKET", "margin_usdt": rounded_risk["margin_usdt"], "leverage": 3,
        "notional_usdt": notional, "quantity": demo.decimal_text(quantity),
        "current_price": float(price),
        "entry_price": demo.decimal_text(entry), "stop_loss": demo.decimal_text(stop),
        "targets": [demo.decimal_text(value) for value in targets],
        "step": rules["step"], "min_qty": rules["min_qty"], "min_notional": rules["min_notional"],
        "risk_per_trade": rounded_risk["estimated_stop_loss_usdt"], "risk_adjusted": False,
    }


def daily_metrics(state: dict[str, Any], demo_state: dict[str, Any]) -> dict[str, Any]:
    day = datetime.now(timezone.utc).date().isoformat()
    plans = [
        plan for plan in demo_state.get("plans", {}).values()
        if plan.get("strategy_id") == strategy.STRATEGY_ID and original_plan(plan)
    ]
    fills = [
        row for row in state.get("journal", [])
        if row.get("strategy_id") == strategy.STRATEGY_ID
        and row.get("kind") == "FILL" and row.get("verified_realized")
        and str(row.get("created_at", "")).startswith(day)
    ]
    closed = sorted(
        (plan for plan in plans if str(plan.get("closed_at", "")).startswith(day)),
        key=lambda plan: str(plan["closed_at"]), reverse=True,
    )
    losses = 0
    for plan in closed:
        if float(plan.get("verified_net_pnl", 0)) >= 0:
            break
        losses += 1
    return {
        "entries": sum(str(plan.get("created_at", "")).startswith(day) for plan in plans),
        "realized_pnl": sum(float(row["realized_pnl"]) for row in fills),
        "consecutive_losses": losses,
        "unverified_closures": sum(not plan.get("original_closure_verified") for plan in closed),
    }


def validate_gates(
    result: StrategyResult, snapshot: dict[str, Any], state: dict[str, Any],
    demo_state: dict[str, Any], notional: float = 0, *, margin_usdt: float = 0,
) -> None:
    gates = strategy.account_gates(
        result, snapshot=snapshot, daily=daily_metrics(state, demo_state),
        active_plans=list(demo_state.get("plans", {}).values()),
        candidate_notional_usdt=notional, enabled=True,
    )
    reasons = [row["key"] for row in gates["gates"] if not row["passed"]]
    if state.get("risk", {}).get("kill_switch"):
        reasons.append("kill_switch")
    try:
        balance = float(snapshot["available_balance"])
    except (KeyError, TypeError, ValueError):
        fail("available_balance_unknown")
    if not math.isfinite(balance) or balance < margin_usdt:
        reasons.append("available_balance")
    pending = sum(not bool(row.get("reduce_only")) for row in snapshot.get("open_orders", []))
    if len(snapshot.get("positions", [])) + pending >= 5:
        reasons.append("positions")
    if reasons:
        fail(",".join(reasons))


def record_dry_run(
    result: StrategyResult, demo_state: dict[str, Any], state: dict[str, Any],
) -> dict[str, Any]:
    from .v21_demo import persist_state, record_event

    validate_result(result)
    key = f"{result.signal.symbol}:{result.signal.decision_time}"
    runs = state.setdefault("original_v2", {}).setdefault("dry_run_plans", {})
    if key in runs:
        return {"ok": True, "dry_run": True, "plan": runs[key], "order": None}
    plan_id = demo.new_client_id("DRY")
    record = strategy.decision_record(result)
    plan = {
        **record, **identity(result, plan_id), "id": plan_id,
        "status": "DRY_RUN", "user_id": demo_state.get("_user_id"),
        "order_authorized": False, "execution_connected": False,
        "quantity": None, "created_at": demo.utc_now(),
    }
    runs[key] = plan
    while len(runs) > 512:
        del runs[next(iter(runs))]
    record_event(
        state, "ORIGINAL_DRY_RUN", "Original decision only; no order sent.",
        symbol=result.signal.symbol, event_id=f"original-dry-{key}",
        source="ORIGINAL_V2", plan=plan,
    )
    persist_state(state)
    return {"ok": True, "dry_run": True, "plan": plan, "order": None}


def match_plan(demo_state: dict[str, Any], order: dict[str, Any]) -> dict[str, Any] | None:
    order_id = str(order.get("i") or order.get("ai") or order.get("actualOrderId") or "")
    client_id = str(order.get("c") or order.get("ac") or order.get("actualClientAlgoId") or "")
    algo_id = str(order.get("aid") or order.get("algoId") or "")
    algo_client_id = str(order.get("ca") or order.get("clientAlgoId") or "")
    matches = []
    for plan in demo_state.get("plans", {}).values():
        if plan.get("strategy_id") != strategy.STRATEGY_ID:
            continue
        if not original_plan(plan):
            continue
        ids = [plan.get("entry_order_id")] + [plan.get(f"{kind}_actual_order_id") for kind in ("stop", "tp1", "tp3")]
        clients = [plan.get("entry_client_order_id")] + [plan.get(f"{kind}_actual_client_order_id") for kind in ("stop", "tp1", "tp3")]
        algo_ids = [plan.get(f"{kind}_algo_id") for kind in ("stop", "tp1", "tp3")]
        algo_clients = [plan.get(f"{kind}_client_id") for kind in ("stop", "tp1", "tp3")]
        if (
            order_id and order_id in {str(value) for value in ids if value}
            or client_id and client_id in {str(value) for value in clients if value}
            or algo_id and algo_id in {str(value) for value in algo_ids if value}
            or algo_client_id and algo_client_id in {str(value) for value in algo_clients if value}
        ):
            if str(order.get("s") or order.get("symbol") or "") != plan["symbol"]:
                fail("ORIGINAL_FILL_SYMBOL_MISMATCH")
            matches.append(plan)
    if len(matches) > 1:
        fail("ORIGINAL_FILL_IDENTITY_AMBIGUOUS")
    return matches[0] if matches else None


def observe_fill(plan: dict[str, Any], order: dict[str, Any]) -> float:
    original_plan(plan)
    if any(key not in order for key in ("i", "t", "l", "z", "rp", "n", "R")):
        fail("ORIGINAL_FILL_FIELDS_MISSING")
    if type(order["R"]) is not bool:
        fail("ORIGINAL_REDUCE_ONLY_INVALID")
    if order.get("N") not in {None, "", "USDT"}:
        fail("ORIGINAL_COMMISSION_ASSET_UNVERIFIED")
    trade_id = str(order.get("t") or "")
    if not trade_id or trade_id == "-1":
        fail("ORIGINAL_TRADE_ID_MISSING")
    seen = plan.setdefault("original_trade_ids", [])
    key = f"{order.get('i')}:{trade_id}"
    if key in seen:
        fail("ORIGINAL_DUPLICATE_FILL")
    try:
        net = Decimal(str(order["rp"])) - Decimal(str(order["n"]))
        filled = Decimal(str(order["l"]))
        cumulative = Decimal(str(order["z"]))
        previous = Decimal(str(plan["remaining_quantity"]))
    except (InvalidOperation, TypeError, ValueError):
        fail("ORIGINAL_FILL_VALUES_INVALID")
    if (
        not all(value.is_finite() for value in (net, filled, cumulative, previous))
        or filled <= 0 or cumulative < filled or previous < 0
    ):
        fail("ORIGINAL_FILL_VALUES_INVALID")
    if order["R"] and filled > previous:
        fail("ORIGINAL_FILL_EXCEEDS_REMAINING")
    seen.append(key)
    plan["verified_net_pnl"] = str(Decimal(str(plan.get("verified_net_pnl") or 0)) + net)
    if bool(order.get("R")):
        remaining = previous - filled
        plan["remaining_quantity"] = demo.decimal_text(remaining)
        if str(order.get("i")) == str(plan.get("tp1_actual_order_id") or ""):
            plan["tp1_filled_quantity"] = str(Decimal(str(plan.get("tp1_filled_quantity") or 0)) + filled)
            plan["tp1_fill_confirmed"] = cumulative >= Decimal(str(plan["tp1_quantity"]))
            plan["tp1_status"] = "FILLED" if plan["tp1_fill_confirmed"] else "PARTIALLY_FILLED"
        if remaining == 0:
            plan.update({
                "closed_at": demo.utc_now(), "position_status": "CLOSED", "status": "KAPANDI",
                "original_closure_verified": True,
            })
    return float(net)


async def candidates(
    client: demo.BinanceDemoClient, at: int, occupied: set[str],
) -> tuple[list[tuple[dict[str, Any], StrategyResult]], dict[str, StrategyResult]]:
    from .backtest_data import normalize_candles
    from .v25_execution import rank_market_tickers

    symbols = sorted(strategy.fixed_profile()["execution_policy"]["allowed_symbols"])
    info = await client.public_get("/fapi/v1/exchangeInfo")
    results, tickers, complete = {}, [], set()
    for symbol in symbols:
        frames, series_by_interval = {}, {}
        for interval in strategy.INTERVALS:
            raw = await client.public_get(
                "/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": 260},
            )
            if not isinstance(raw, list) or any(not isinstance(row, list) or len(row) < 8 for row in raw):
                fail("INVALID_ORIGINAL_KLINES")
            rows = [
                {
                    "open_time": row[0], "open": row[1], "high": row[2], "low": row[3],
                    "close": row[4], "volume": row[5], "close_time": row[6], "quote_volume": row[7],
                }
                for row in raw
            ]
            try:
                series, _duplicates = normalize_candles(rows, interval)
            except (ValueError, TypeError, KeyError):
                fail("INVALID_ORIGINAL_KLINES")
            frames[interval] = series.closed(at)
            series_by_interval[interval] = series
        results[symbol] = strategy.evaluate(StrategyInput(symbol, at, frames), enabled=True)
        if all(series.history_complete(at) for series in series_by_interval.values()):
            complete.add(symbol)
        ticker = series_by_interval["15m"].ticker(at, symbol)
        if ticker is not None:
            tickers.append(ticker)
    ranked = rank_market_tickers(info, tickers, excluded_symbols=occupied, allowed_symbols=set(symbols))
    eligible = []
    for candidate in ranked:
        result = results[candidate["symbol"]]
        if candidate["symbol"] not in complete:
            logger.warning("ORIGINAL_DEMO_PREFILTER_REJECTED symbol=%s reason=data_gap_history", candidate["symbol"])
            continue
        if not result.signal.strategy_eligible:
            continue
        signal = result.legacy["analysis"]
        try:
            strategy.risk_preview(signal["entry"], signal["stop_loss"], enabled=True)
        except strategy.DemoDecisionError as exc:
            if str(exc) != "profile_cap":
                raise
            logger.warning("ORIGINAL_DEMO_PREFILTER_REJECTED symbol=%s reason=profile_cap", candidate["symbol"])
            continue
        eligible.append((candidate, result))
    eligible.sort(key=lambda pair: (
        pair[0]["opportunity_score"], pair[1].legacy["analysis"]["confidence"],
    ), reverse=True)
    return eligible, results


async def automatic_cycle(
    application: Any, *, request: Any, user_id: str,
    demo_state: dict[str, Any], state: dict[str, Any],
) -> None:
    from . import v21_demo as control

    demo._validate_execution_context(request, demo_state, state)
    if not user_id or not state["auto"].get("user_confirmed") or not demo.armed(demo_state):
        fail("ORIGINAL_OWNER_CONFIRMATION_ARM_REQUIRED")
    if state.get("risk", {}).get("kill_switch"):
        fail("kill_switch")
    at = int(datetime.now(timezone.utc).timestamp()) // 900 * 900
    original = state.setdefault("original_v2", {})
    if original.get("last_decision_time", -1) >= at:
        return
    original["last_decision_time"] = at
    control.persist_state(state)
    client = control.market_client_for(application)
    occupied = {
        plan["symbol"] for plan in demo_state.get("plans", {}).values()
        if plan.get("position_status") != "CLOSED"
    }
    eligible, results = await candidates(client, at, occupied)
    dry_only = not (strategy.feature_enabled() and send_orders_enabled())
    for result in results.values():
        if dry_only or not result.signal.strategy_eligible:
            record_dry_run(result, demo_state, state)
    for candidate, result in eligible[:3]:
        signal = result.legacy["analysis"]
        risk = strategy.risk_preview(signal["entry"], signal["stop_loss"], enabled=True)
        body = demo.DemoOrderRequest(
            symbol=candidate["symbol"], direction=signal["direction"], order_type="MARKET",
            margin_usdt=risk["margin_usdt"], leverage=3,
            stop_loss=signal["stop_loss"], tp1=signal["tp1"], tp2=signal["tp2"], tp3=signal["tp3"],
        )
        try:
            await demo.execute_demo_order(
                application, body, source="ORIGINAL_V2", request=request,
                demo_state=demo_state, v21_state=state, strategy_result=result,
            )
        except demo.BinanceDemoError as exc:
            control.record_event(
                state, "ORIGINAL_REJECTED", str(exc), symbol=result.signal.symbol,
                source="ORIGINAL_V2", plan={**identity(result, demo.new_client_id("REJECT"))},
            )
        except HTTPException as exc:
            if exc.status_code not in {409, 422}:
                raise
            control.record_event(
                state, "ORIGINAL_REJECTED", str(exc.detail), symbol=result.signal.symbol,
                source="ORIGINAL_V2", plan={**identity(result, demo.new_client_id("REJECT"))},
            )
    control.persist_state(state)
