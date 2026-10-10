"""Conservative, idempotent loss streaks for owned legacy Demo closures."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from .legacy_demo_results import owned_legacy_plans


def loss_limit(state: dict[str, Any]) -> int:
    value = state["settings"].get("consecutive_loss_limit", state["risk"].get("consecutive_loss_limit", 3))
    if isinstance(value, bool) or int(value) != Decimal(str(value)) or int(value) < 1:
        raise ValueError("Invalid consecutive loss limit")
    return min(3, int(value))


def _number(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise TypeError("Invalid accounting number")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Missing accounting number") from exc
    if not number.is_finite():
        raise ValueError("Non-finite accounting number")
    return number


def _at(value: str) -> datetime:
    at = datetime.fromisoformat(value)
    if at.tzinfo is None:
        raise ValueError("Missing closure timezone")
    return at.astimezone(timezone.utc)


def _closure(row: dict[str, Any], plan: dict[str, Any], now: datetime) -> tuple[str, datetime] | None:
    if row["status"] == "verified":
        pnl = _number(row["net_pnl"])
        result = "loss" if pnl < 0 else "profit" if pnl > 0 else "breakeven"
        if row["result"] != result:
            raise ValueError("Conflicting verified result")
        return result, _at(row["closed_at"])
    if row["status"] not in {"pending", "unverified"}:
        raise ValueError("Unknown legacy result status")
    reducing = [fill for fill in row["fills"].values() if fill.get("R") is True]
    closed = bool(row.get("snapshot_missing") or plan.get("position_status") == "CLOSED")
    if not closed:
        expected = _number(plan.get("initial_quantity"))
        quantities = [_number(fill.get("l")) for fill in reducing]
        closed = expected > 0 and all(qty > 0 for qty in quantities) and sum(quantities, Decimal(0)) >= expected
    if not closed:
        return None
    times = []
    for fill in reducing:
        if fill.get("T") is not None:
            timestamp = _number(fill["T"])
            if timestamp > 0:
                times.append(datetime.fromtimestamp(float(timestamp / 1000), timezone.utc))
    at = max(times) if times else _at(plan["closed_at"]) if plan.get("closed_at") else now
    return "unverified", at


def update(
    state: dict[str, Any], demo: dict[str, Any], now: datetime, *, restart: bool = False,
) -> list[dict[str, Any]]:
    now = now.astimezone(timezone.utc)
    limit = loss_limit(state)
    risk = state["risk"]
    counter = risk["consecutive_losses"]
    if type(counter) is not int or counter < 0:
        raise ValueError("Invalid consecutive loss counter")
    if state.get("legacy_result_observer_errors"):
        raise ValueError("Unresolved legacy result observation error")
    plans = owned_legacy_plans(state, demo)
    if not plans and not state.get("legacy_loss_guard") and counter == 0:
        return []
    if state.get("_user_id") and state.get("_user_id") != demo.get("_user_id"):
        raise ValueError("Loss guard user context mismatch")
    guard = state.setdefault("legacy_loss_guard", {
        "date": now.date().isoformat(),
        "baseline": 0 if risk.get("date") not in (None, now.date().isoformat()) else counter,
        "applied": {}, "user_id": state.get("_user_id"),
        "paused": False, "error": None, "restart_at": None, "pause_sequence": 0,
    })
    if type(guard["paused"]) is not bool or not isinstance(guard["applied"], dict):
        raise ValueError("Invalid consecutive loss lock")
    if guard["user_id"] != state.get("_user_id"):
        raise ValueError("Loss guard ownership mismatch")
    date.fromisoformat(guard["date"])
    if type(guard["pause_sequence"]) is not int or guard["pause_sequence"] < 0:
        raise ValueError("Invalid pause sequence")
    if guard["date"] != now.date().isoformat():
        guard.update(date=now.date().isoformat(), baseline=0)
    notices = []
    rows = state.get("legacy_trade_results", {})
    for plan in plans:
        plan_id = plan["id"]
        if plan_id in guard["applied"]:
            continue
        row = rows.get(plan_id)
        if row is None:
            if plan.get("position_status") != "CLOSED":
                continue
            row = {
                "plan_id": plan_id, "user_id": plan["user_id"], "symbol": plan["symbol"],
                "status": "unverified", "fills": {}, "reasons": ["closure_history_missing"],
            }
        if (row.get("plan_id"), row.get("user_id"), row.get("symbol")) != (plan_id, plan["user_id"], plan["symbol"]):
            raise ValueError("Loss guard result ownership mismatch")
        closure = _closure(row, plan, now)
        if closure is None:
            continue
        result, at = closure
        if at > now:
            raise ValueError("Future legacy closure time")
        reasons = list(row.get("reasons", []))
        guard["applied"][plan_id] = {"result": result, "at": at.isoformat(), "reasons": reasons}
        if result == "unverified":
            reason = ", ".join(reasons) or "closure_evidence_missing"
            notices.append({
                "kind": "LEGACY_UNVERIFIED_LOSS",
                "message": (
                    f"{plan['symbol']} kapanışı doğrulanamadı ({reason}); gerçek zarar doğrulanmadı, "
                    "güvenlik gereği ardışık zarar kaydına eklendi. Seri sıfırlanmadı."
                ),
                "symbol": plan["symbol"], "reason": reason,
                "event_id": f"legacy-conservative-loss-{plan_id}",
            })
    count = guard["baseline"]
    if type(count) is not int or count < 0:
        raise ValueError("Invalid loss guard baseline")
    reached = count >= limit
    cutoff = _at(guard["restart_at"]) if guard["restart_at"] else None
    entries = sorted(
        guard["applied"].items(),
        key=lambda item: (_at(item[1]["at"]), item[1]["result"] in {"loss", "unverified"}, item[0]),
    )
    for _, entry in entries:
        at = _at(entry["at"])
        if at.date().isoformat() != guard["date"] or cutoff is not None and at <= cutoff:
            continue
        if entry["result"] in {"loss", "unverified"}:
            count += 1
        elif entry["result"] in {"profit", "breakeven"}:
            count = 0
        else:
            raise ValueError("Invalid applied closure classification")
        reached |= count >= limit
    risk["consecutive_losses"] = count
    if reached and not guard["paused"]:
        guard["paused"] = True
        guard["pause_sequence"] += 1
    if restart:
        guard.update(baseline=0, paused=False, error=None, restart_at=now.isoformat())
        risk["consecutive_losses"] = 0
    if guard["paused"] or guard.get("error"):
        state["auto"].update(
            enabled=False, status="PAUSED",
            pause_reason="CONSECUTIVE_LOSS_ERROR" if guard.get("error") else "CONSECUTIVE_LOSSES",
        )
    return notices
