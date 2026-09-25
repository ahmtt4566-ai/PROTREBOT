"""Evidence-based Markdown review for the requested live trade pair."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TARGET_SYMBOLS = ("4USDT", "MUBARAKUSDT")
UNKNOWN = "UNKNOWN"


def _value(value: Any) -> str:
    if value is None or value == "":
        return UNKNOWN
    return str(value)


def _closed_plans(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    plans = state.get("plans") if isinstance(state.get("plans"), dict) else {}
    return {
        symbol: plan
        for symbol in TARGET_SYMBOLS
        for plan in plans.values()
        if isinstance(plan, dict)
        and str(plan.get("symbol") or "").upper() == symbol
        and str(plan.get("status") or "").upper() == "KAPANDI"
        and plan.get("pnl_verified") is True
    }


def _events_for_plan(state: dict[str, Any], plan: dict[str, Any]) -> list[dict[str, Any]]:
    plan_id = plan.get("id")
    return [event for event in state.get("events", []) if isinstance(event, dict) and event.get("plan_id") == plan_id]


def _plan_section(state: dict[str, Any], plan: dict[str, Any]) -> list[str]:
    events = _events_for_plan(state, plan)
    close_events = [event for event in events if event.get("kind") == "LIVE_POSITION_CLOSED"]
    fills = [event for event in events if event.get("kind") == "LIVE_FILL"]
    audit = next((event.get("audit_snapshot") for event in events if event.get("kind") == "LIVE_DECISION_AUDIT"), None)
    return [
        f"## {plan.get('symbol', UNKNOWN)} {plan.get('direction', UNKNOWN)}", "",
        "| Field | Verified value |", "| --- | --- |",
        f"| Plan ID | `{_value(plan.get('id'))}` |",
        f"| Entry order ID | `{_value(plan.get('entry_order_id'))}` |",
        f"| Entry price | `{_value(plan.get('entry_price'))}` |",
        f"| Exit price | `{_value(plan.get('exit_price'))}` |",
        f"| Quantity | `{_value(plan.get('quantity'))}` |",
        f"| Stop loss | `{_value(plan.get('stop_loss'))}` |",
        f"| Targets | `{_value(', '.join(str(item) for item in plan.get('targets', [])) if plan.get('targets') else None)}` |",
        f"| Leverage | `{_value(plan.get('applied_leverage') or plan.get('leverage'))}` |",
        f"| Close reason | `{_value(plan.get('close_reason'))}` |",
        f"| Opened at | `{_value(plan.get('opened_at'))}` |",
        f"| Closed at | `{_value(plan.get('closed_at'))}` |",
        f"| Gross realized PnL | `{_value(plan.get('gross_realized_pnl'))}` USDT |",
        f"| Commission | `{_value(plan.get('commission_usdt'))}` USDT |",
        f"| Net realized PnL | `{_value(plan.get('realized_pnl'))}` USDT (funding excluded) |",
        f"| Trade rows used | `{_value(plan.get('trade_count'))}` |",
        "| PnL evidence | `userTrades + allOrders + allAlgoOrders` |",
        f"| Protection state at close | `{_value(plan.get('protection_state'))}` |",
        f"| Monitoring targets | `{_value(', '.join(plan.get('monitoring_targets', [])) if plan.get('monitoring_targets') else None)}` |",
        f"| Stream close fills observed | `{len(fills)}` |",
        f"| Verified close event count | `{len(close_events)}` |", "",
        "### Evidence", "",
        f"- Decision audit: `{_value('present' if audit else None)}`.",
        f"- Close event: `{_value(close_events[0].get('id') if close_events else None)}`.",
        f"- Non-USDT commission assets: `{_value(', '.join(plan.get('non_usdt_commission_assets', [])) if plan.get('non_usdt_commission_assets') else None)}`.",
        f"- Stop/TP trigger classification: `{_value(plan.get('close_reason'))}`; no stronger classification is claimed.", "",
    ]


def build_trade_review(state: dict[str, Any], generated_at: str) -> tuple[str, str] | None:
    plans = _closed_plans(state)
    if set(plans) != set(TARGET_SYMBOLS):
        return None
    review_key = "|".join(sorted(str(plan.get("id")) for plan in plans.values()))
    existing = state.get("trade_review") if isinstance(state.get("trade_review"), dict) else {}
    if existing.get("review_key") == review_key:
        return None
    lines = [
        "# Live Trade Review", "", f"- Generated at: `{generated_at}`",
        "- Trigger: both requested positions reached verified `KAPANDI` state.",
        "- Source: persisted V25 plans/events and verified Binance trade-history reconstruction.",
        "- Claims are limited to recorded evidence. Unavailable fields are marked `UNKNOWN`.", "",
    ]
    for symbol in TARGET_SYMBOLS:
        lines.extend(_plan_section(state, plans[symbol]))
    total = sum(float(plan.get("realized_pnl") or 0) for plan in plans.values())
    lines.extend([
        "## Pair Summary", "", f"- Net realized PnL total: `{total:.8f}` USDT, funding excluded.",
        f"- Reconciliation diagnostic: `{_value((state.get('connection') or {}).get('last_error'))}`.",
        f"- Last order error: `{_value((state.get('last_order_error') or {}).get('message'))}`.",
        "- Slippage versus requested/mark price: `UNKNOWN` unless directly recorded in the evidence above.",
        "- Liquidation distance: `UNKNOWN` unless recorded in a verified snapshot.", "", "## Event Audit", "",
        "| Timestamp | Kind | Symbol | Evidence ID |", "| --- | --- | --- | --- |",
    ])
    for event in state.get("events", []):
        if not isinstance(event, dict) or event.get("symbol") not in TARGET_SYMBOLS:
            continue
        lines.append(f"| {_value(event.get('created_at'))} | {_value(event.get('kind'))} | {_value(event.get('symbol'))} | `{_value(event.get('id') or event.get('exchange_event_id'))}` |")
    return review_key, "\n".join(lines) + "\n"


def write_trade_review(state: dict[str, Any], *, root: Path | None = None, generated_at: str | None = None) -> dict[str, Any] | None:
    generated_at = generated_at or datetime.now(timezone.utc).isoformat()
    built = build_trade_review(state, generated_at)
    if built is None:
        return None
    review_key, content = built
    output_root = root or Path(__file__).resolve().parents[2]
    docs = output_root / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    path = docs / f"trade_review_{generated_at[:10]}.md"
    path.write_text(content, encoding="utf-8")
    metadata = {"review_key": review_key, "path": str(path), "generated_at": generated_at, "symbols": list(TARGET_SYMBOLS)}
    state["trade_review"] = metadata
    return metadata