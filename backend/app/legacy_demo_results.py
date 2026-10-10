"""Passive, user-scoped accounting evidence for legacy Auto Trade plans."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from .binance_demo import can_mutate_lifecycle

FILL_FIELDS = ("i", "c", "t", "s", "ps", "S", "R", "l", "z", "rp", "n", "N", "T")
PROTECTION_LABELS = ("stop", "tp1", "tp2", "tp3")


def _id(value: Any) -> str:
    return str(value or "").strip()


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _identity_matches(event_id: str, event_client: str, known_id: str, known_client: str) -> bool:
    return bool(
        (event_id and known_id and event_id == known_id or event_client and known_client and event_client == known_client)
        and not (event_id and known_id and event_id != known_id)
        and not (event_client and known_client and event_client != known_client)
    )


def _plans(state: dict[str, Any], demo: dict[str, Any]) -> list[dict[str, Any]]:
    uid = _id(state.get("_user_id"))
    if not uid or uid != _id(demo.get("_user_id")):
        return []
    return [
        plan for key, plan in demo.get("plans", {}).items()
        if isinstance(plan, dict) and _id(plan.get("id")) == str(key)
        and _id(plan.get("user_id")) == uid
        and plan.get("source") == "AUTO_SCANNER" and plan.get("strategy_id") is None
    ]


def _record(state: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    records = state.setdefault("legacy_trade_results", {})
    row = records.setdefault(plan["id"], {
        "plan_id": plan["id"], "user_id": plan["user_id"], "symbol": plan["symbol"],
        "fills": {}, "exit_orders": [], "algo_orders": [], "conflicts": [],
        "reported": [], "status": "pending", "net_pnl": None, "result": None,
        "closed_at": None, "reasons": [],
    })
    if (row.get("plan_id"), row.get("user_id"), row.get("symbol")) != (plan["id"], plan["user_id"], plan["symbol"]):
        raise ValueError("Legacy result ownership mismatch")
    for label in PROTECTION_LABELS:
        algo = {"id": _id(plan.get(f"{label}_algo_id")), "client": _id(plan.get(f"{label}_client_id"))}
        if any(algo.values()) and algo not in row["algo_orders"]:
            row["algo_orders"].append(algo)
        actual = {"id": _id(plan.get(f"{label}_actual_order_id")), "client": _id(plan.get(f"{label}_actual_client_order_id"))}
        if any(actual.values()) and actual not in row["exit_orders"]:
            row["exit_orders"].append(actual)
    return row


def _role(plan: dict[str, Any], row: dict[str, Any], fill: dict[str, Any]) -> str | None:
    order_id, client = _id(fill.get("i")), _id(fill.get("c"))
    entry_id = _id(plan.get("entry_order_id") or plan.get("provenance_entry_order_id"))
    entry_client = _id(plan.get("entry_client_order_id") or plan.get("provenance_entry_client_order_id"))
    entry = _identity_matches(order_id, client, entry_id, entry_client)
    exit_match = any(_identity_matches(order_id, client, item["id"], item["client"]) for item in row["exit_orders"])
    if entry == exit_match:
        return None
    return "entry" if entry else "exit"


def _valid_fill(plan: dict[str, Any], fill: dict[str, Any], role: str) -> tuple[dict[str, Any] | None, str | None]:
    if not _id(fill.get("i")).isdigit() or int(fill["i"]) <= 0 or not _id(fill.get("t")).isdigit() or int(fill["t"]) < 0:
        return None, "fill_identity_missing"
    if fill.get("s") != plan["symbol"] or fill.get("ps") != plan.get("position_side", "BOTH"):
        return None, "fill_position_mismatch"
    side = {"LONG": "BUY", "SHORT": "SELL"}.get(plan.get("direction"))
    expected_side = side if role == "entry" else {"BUY": "SELL", "SELL": "BUY"}.get(side)
    if expected_side is None or fill.get("S") != expected_side or type(fill.get("R")) is not bool or fill["R"] != (role == "exit"):
        return None, "fill_side_unverified"
    quantity, cumulative, pnl, fee = (_decimal(fill.get(key)) for key in ("l", "z", "rp", "n"))
    if quantity is None or cumulative is None or pnl is None or fee is None:
        return None, "fill_values_missing"
    if quantity <= 0 or cumulative < quantity or fee < 0 or role == "entry" and pnl != 0:
        return None, "fill_values_invalid"
    if fill.get("N") not in (None, "", "USDT") or fee > 0 and fill.get("N") != "USDT":
        return None, "commission_unverified"
    timestamp = _decimal(fill.get("T"))
    if timestamp is None or timestamp <= 0:
        return None, "fill_time_missing"
    try:
        at = datetime.fromtimestamp(float(timestamp / 1000), timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None, "fill_time_invalid"
    return {"quantity": quantity, "cumulative": cumulative, "net": pnl - fee, "at": at, "order": str(fill["i"]), "role": role}, None


def _notice(row: dict[str, Any], reason: str) -> dict[str, Any] | None:
    if reason in row["reported"]:
        return None
    row["reported"].append(reason)
    verified = reason == "verified"
    return {
        "kind": "LEGACY_RESULT_VERIFIED" if verified else "LEGACY_RESULT_UNVERIFIED",
        "message": (
            f"{row['symbol']} eski Demo Auto Trade kapanışı doğrulandı; net sonuç {row['net_pnl']} USDT."
            if verified else f"{row['symbol']} eski Demo Auto Trade sonucu doğrulanamadı ({reason}); kâr veya zarar sonucu üretilmedi."
        ),
        "symbol": row["symbol"], "reason": reason,
        "event_id": f"legacy-result-{row['plan_id']}-{reason}",
    }


def _recompute(plan: dict[str, Any], row: dict[str, Any]) -> list[dict[str, Any]]:
    reasons = set(row["conflicts"])
    valid = []
    for fill in row["fills"].values():
        role = _role(plan, row, fill)
        if role is None:
            reasons.add("exit_identity_unverified")
            continue
        parsed, reason = _valid_fill(plan, fill, role)
        if reason:
            reasons.add(reason)
        elif parsed is not None:
            valid.append(parsed)
    entry = [fill for fill in valid if fill["role"] == "entry"]
    exits = [fill for fill in valid if fill["role"] == "exit"]
    expected = _decimal(plan.get("provenance_expected_quantity") or plan.get("initial_quantity"))
    initial = _decimal(plan.get("initial_quantity"))
    if expected is None or expected <= 0 or initial is not None and initial != expected:
        reasons.add("expected_quantity_unverified")
    if not can_mutate_lifecycle(plan):
        reasons.add("plan_provenance_unverified")
    groups: dict[str, list[dict[str, Any]]] = {}
    for fill in valid:
        groups.setdefault(fill["order"], []).append(fill)
    for fills in groups.values():
        cumulative = Decimal(0)
        for fill in sorted(fills, key=lambda item: item["cumulative"]):
            cumulative += fill["quantity"]
            if cumulative != fill["cumulative"]:
                reasons.add("fill_history_incomplete")
    entered = sum((fill["quantity"] for fill in entry), Decimal(0))
    exited = sum((fill["quantity"] for fill in exits), Decimal(0))
    closing = bool(exits or row.get("snapshot_missing") or plan.get("position_status") == "CLOSED")
    if expected is not None and closing:
        if entered != expected:
            reasons.add("entry_history_incomplete")
        if exited > entered or exited > expected:
            reasons.add("exit_quantity_exceeds_entry")
        elif exited < expected:
            reasons.add("closure_incomplete")
    row.update(net_pnl=None, result=None, closed_at=None, reasons=sorted(reasons))
    if not reasons and closing and expected is not None and entered == exited == expected:
        net = sum((fill["net"] for fill in valid), Decimal(0))
        row.update(
            status="verified", net_pnl=str(net), result="loss" if net < 0 else "profit" if net > 0 else "breakeven",
            closed_at=max(fill["at"] for fill in exits).isoformat(),
        )
        notice = _notice(row, "verified")
        return [notice] if notice else []
    row["status"] = "unverified" if reasons else "pending"
    notices = [_notice(row, reason) for reason in sorted(reasons)]
    return [notice for notice in notices if notice is not None]


def observe_stream(state: dict[str, Any], demo: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
    event = payload.get("o") if isinstance(payload.get("o"), dict) else payload.get("a", {})
    if not isinstance(event, dict):
        return []
    plans = _plans(state, demo)
    symbol = str(event.get("s") or event.get("symbol") or "")
    candidates = [plan for plan in plans if plan.get("symbol") == symbol]
    notices = []
    if payload.get("e") == "ALGO_UPDATE":
        if str(event.get("X") or event.get("algoStatus") or event.get("status") or "").upper() not in {"TRIGGERED", "FILLED", "EXECUTED"}:
            return []
        matches = []
        for plan in candidates:
            row = _record(state, plan)
            if any(_identity_matches(
                _id(event.get("aid") or event.get("algoId")), _id(event.get("ca") or event.get("clientAlgoId")),
                algo["id"], algo["client"],
            ) for algo in row["algo_orders"]):
                matches.append((plan, row))
        if len(matches) == 1:
            plan, row = matches[0]
            actual = {"id": _id(event.get("ai") or event.get("actualOrderId")), "client": _id(event.get("ac") or event.get("actualClientAlgoId"))}
            if any(actual.values()) and actual not in row["exit_orders"]:
                row["exit_orders"].append(actual)
            notices.extend(_recompute(plan, row))
        elif len(matches) > 1:
            for plan, row in matches:
                if "algo_identity_ambiguous" not in row["conflicts"]:
                    row["conflicts"].append("algo_identity_ambiguous")
                notices.extend(_recompute(plan, row))
        return notices
    if payload.get("e") != "ORDER_TRADE_UPDATE" or event.get("x") != "TRADE":
        return []
    fill = {key: event.get(key) for key in FILL_FIELDS}
    fill["T"] = event.get("T", payload.get("T", payload.get("E")))
    matches = [(plan, _record(state, plan)) for plan in candidates]
    identified = [(plan, row) for plan, row in matches if _role(plan, row, fill) is not None]
    if identified:
        matches = identified
    else:
        matches = [(plan, row) for plan, row in matches if plan.get("position_status") != "CLOSED" and fill.get("R") is True]
    fingerprint = hashlib.sha256(json.dumps(fill, sort_keys=True).encode()).hexdigest()
    key = f"{_id(fill.get('i'))}:{_id(fill.get('t'))}" if _id(fill.get("i")) and _id(fill.get("t")) else fingerprint
    for plan, row in matches:
        if len(matches) > 1 and "fill_identity_ambiguous" not in row["conflicts"]:
            row["conflicts"].append("fill_identity_ambiguous")
        previous = row["fills"].get(key)
        if previous is not None and previous != fill:
            if all(previous.get(field) is None or fill.get(field) is None or previous[field] == fill[field] for field in FILL_FIELDS):
                row["fills"][key] = {field: fill.get(field) if fill.get(field) is not None else previous.get(field) for field in FILL_FIELDS}
            elif "fill_identity_conflict" not in row["conflicts"]:
                row["conflicts"].append("fill_identity_conflict")
        else:
            row["fills"][key] = fill
        notices.extend(_recompute(plan, row))
    return notices


def observe_missing_positions(state: dict[str, Any], demo: dict[str, Any], previous: dict[str, Any] | None, current: dict[str, Any]) -> list[dict[str, Any]]:
    current_symbols = {item["symbol"] for item in current.get("positions", [])}
    missing = {item["symbol"] for item in (previous or {}).get("positions", [])} - current_symbols
    notices = []
    for plan in _plans(state, demo):
        if plan.get("symbol") not in missing and not (plan.get("position_status") == "CLOSED" and plan.get("symbol") not in current_symbols):
            continue
        row = _record(state, plan)
        row["snapshot_missing"] = True
        notices.extend(_recompute(plan, row))
    return notices
