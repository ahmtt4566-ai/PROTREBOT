"""Stripe-backed subscription products and server-side entitlement helpers."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

TRIAL_DAYS = 7
MASTER_MODE_PRICE = 119.90
SUBSCRIPTION_STATUSES = {"TRIALING", "ACTIVE", "PAST_DUE", "UNPAID", "CANCELLED", "EXPIRED"}
ACCESS_STATUSES = {"TRIALING", "ACTIVE"}
PAST_DUE_GRACE_SECONDS = max(0, int(os.getenv("STRIPE_PAST_DUE_GRACE_SECONDS", str(3 * 24 * 60 * 60))))
CANCELLATION_RULES = {
    "cancel_anytime": True,
    "default_effective": "period_end",
    "immediate_cancellation_supported": True,
    "refund_policy": None,
}

PLAN_CATALOG: dict[str, dict[str, Any]] = {
    "TRIAL": {
        "name": "7-Day Free Trial",
        "monthly_price": MASTER_MODE_PRICE,
        "annual_price": None,
        "features": ["Master Trade access", "Payment method required", "Automatic conversion after 7 days", "Cancel anytime"],
        "entitlements": {"canAccessMasterTrade": True},
    },
    "MASTER_MODE": {
        "name": "Master Mode",
        "monthly_price": MASTER_MODE_PRICE,
        "annual_price": None,
        "features": ["Immediate Master Trade access", "Automatic monthly renewal", "Premium execution workspace", "Cancel anytime"],
        "entitlements": {"canAccessMasterTrade": True},
    },
}


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def canonical_status(value: Any) -> str:
    return {
        "TRIAL": "TRIALING",
        "trialing": "TRIALING",
        "active": "ACTIVE",
        "past_due": "PAST_DUE",
        "unpaid": "UNPAID",
        "canceled": "CANCELLED",
        "cancelled": "CANCELLED",
        "expired": "EXPIRED",
    }.get(str(value or "").upper(), str(value or "").upper())


def past_due_grace_active(row: dict[str, Any], now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    grace_until = parse_datetime(row.get("grace_until") or row.get("graceUntil"))
    if grace_until:
        return grace_until > now
    updated = parse_datetime(row.get("updated_at") or row.get("updatedAt"))
    return bool(updated and updated.timestamp() + PAST_DUE_GRACE_SECONDS > now.timestamp())


def active_subscription(state: dict[str, Any], user_id: str) -> dict[str, Any] | None:
    now = datetime.now(timezone.utc)
    rows = [
        row for row in state.get("subscriptions", [])
        if row.get("user_id") == user_id
        and str(row.get("plan") or "").upper() in PLAN_CATALOG
        and canonical_status(row.get("status")) in SUBSCRIPTION_STATUSES
    ]
    valid = []
    for row in rows:
        status = canonical_status(row.get("status"))
        end = parse_datetime(row.get("current_period_end") or row.get("currentPeriodEnd") or row.get("trial_end") or row.get("trialEnd") or row.get("period_end"))
        if status == "PAST_DUE" and past_due_grace_active(row, now):
            valid.append(row)
        elif status in {"TRIALING", "ACTIVE"} and end and end > now:
            valid.append(row)
    return max(valid, key=lambda row: row.get("current_period_end") or row.get("currentPeriodEnd") or row.get("trial_end") or row.get("trialEnd") or "", default=None)


def entitlement_snapshot(state: dict[str, Any], user_id: str) -> dict[str, Any]:
    row = active_subscription(state, user_id)
    if not row:
        latest = [item for item in state.get("subscriptions", []) if item.get("user_id") == user_id]
        status = canonical_status(max(latest, key=lambda item: item.get("updated_at") or item.get("updatedAt") or "", default={}).get("status")) if latest else "EXPIRED"
        if status not in SUBSCRIPTION_STATUSES:
            status = "EXPIRED"
        return {"status": status, "plan": None, "master_trade_access": False, "features": [], "entitlements": {"canAccessMasterTrade": False}, "mode": "STRIPE", "cancel_at_period_end": False, "cancelAtPeriodEnd": False}
    plan = str(row.get("plan") or "MASTER_MODE").upper()
    catalog = PLAN_CATALOG.get(plan, PLAN_CATALOG["MASTER_MODE"])
    status = canonical_status(row.get("status"))
    access = status in ACCESS_STATUSES or (status == "PAST_DUE" and past_due_grace_active(row))
    current_end = row.get("current_period_end") or row.get("currentPeriodEnd") or row.get("period_end")
    trial_end = row.get("trial_end") or row.get("trialEnd")
    cancel_at_period_end = bool(row.get("cancel_at_period_end", row.get("cancelAtPeriodEnd", False)))
    return {
        "status": status,
        "plan": plan,
        "master_trade_access": access,
        "trial_start": row.get("trial_start") or row.get("trialStart"),
        "trial_end": trial_end,
        "current_period_start": row.get("current_period_start") or row.get("currentPeriodStart"),
        "current_period_end": current_end,
        "next_payment_amount": catalog["monthly_price"] if status in {"TRIALING", "ACTIVE", "PAST_DUE"} else None,
        "currency": "USD",
        "features": catalog["features"] if access else [],
        "entitlements": {"canAccessMasterTrade": access},
        "mode": "STRIPE",
        "cancel_at_period_end": cancel_at_period_end,
        "cancelAtPeriodEnd": cancel_at_period_end,
        "grace_until": row.get("grace_until") or row.get("graceUntil"),
    }
