import unittest
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from app import v22_commercial, v25_execution
from app.subscription_core import (
    MASTER_MODE_PRICE,
    PLAN_CATALOG,
    TRIAL_DAYS,
    active_subscription,
    entitlement_snapshot,
)


def stripe_subscription_event(event_id, status, *, plan="MASTER_MODE", customer="cus_1", subscription="sub_1", **extra):
    future = int((datetime.now(timezone.utc) + timedelta(days=30)).timestamp())
    payload = {
        "id": subscription,
        "customer": customer,
        "status": status,
        "current_period_start": future - 100,
        "current_period_end": future,
        "cancel_at_period_end": False,
        "metadata": {"user_id": "user-1", "plan": plan, "billing_interval": "monthly"},
        "items": {"data": []},
        **extra,
    }
    return {"id": event_id, "type": "customer.subscription.updated", "data": {"object": payload}}


class SubscriptionCoreTests(unittest.TestCase):
    def test_exactly_two_products_and_trial_terms(self):
        self.assertEqual(set(PLAN_CATALOG), {"TRIAL", "MASTER_MODE"})
        self.assertEqual(TRIAL_DAYS, 7)
        self.assertEqual(PLAN_CATALOG["TRIAL"]["monthly_price"], MASTER_MODE_PRICE)
        self.assertEqual(PLAN_CATALOG["MASTER_MODE"]["monthly_price"], MASTER_MODE_PRICE)
        self.assertTrue(PLAN_CATALOG["TRIAL"]["entitlements"]["canAccessMasterTrade"])

    def test_trial_and_active_grant_master_trade_access(self):
        now = datetime.now(timezone.utc)
        state = {"subscriptions": [{
            "user_id": "user-1", "plan": "TRIAL", "status": "TRIALING",
            "trial_end": (now + timedelta(days=TRIAL_DAYS)).isoformat(),
        }]}
        trial = entitlement_snapshot(state, "user-1")
        self.assertEqual(trial["status"], "TRIALING")
        self.assertTrue(trial["master_trade_access"])

        state["subscriptions"][0].update({
            "plan": "MASTER_MODE", "status": "ACTIVE",
            "current_period_end": (now + timedelta(days=30)).isoformat(),
        })
        active = entitlement_snapshot(state, "user-1")
        self.assertEqual(active["status"], "ACTIVE")
        self.assertTrue(active["master_trade_access"])

    def test_past_due_grace_allows_access_but_unpaid_and_cancelled_do_not(self):
        now = datetime.now(timezone.utc)
        row = {"user_id": "user-1", "plan": "MASTER_MODE", "status": "PAST_DUE", "grace_until": (now + timedelta(hours=1)).isoformat()}
        state = {"subscriptions": [row]}
        self.assertTrue(entitlement_snapshot(state, "user-1")["master_trade_access"])
        row["status"] = "UNPAID"
        self.assertFalse(entitlement_snapshot(state, "user-1")["master_trade_access"])
        row["status"] = "CANCELLED"
        self.assertFalse(entitlement_snapshot(state, "user-1")["master_trade_access"])

    def test_expired_subscription_has_no_access(self):
        state = {"subscriptions": [{
            "user_id": "user-1", "plan": "MASTER_MODE", "status": "ACTIVE",
            "current_period_end": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        }]}
        self.assertEqual(active_subscription(state, "user-1"), None)
        self.assertFalse(entitlement_snapshot(state, "user-1")["master_trade_access"])

    def test_server_side_price_mapping_accepts_only_the_two_monthly_products(self):
        with patch.dict("os.environ", {"STRIPE_PRICE_MASTER_MODE_MONTHLY": "price_master"}, clear=False):
            self.assertEqual(v22_commercial.stripe_price_id("TRIAL", "monthly"), "price_master")
            self.assertEqual(v22_commercial.stripe_price_id("MASTER_MODE", "monthly"), "price_master")
            with self.assertRaises(v22_commercial.HTTPException):
                v22_commercial.stripe_price_id("MASTER_MODE", "annual")
        with self.assertRaises(v22_commercial.HTTPException):
            v22_commercial.stripe_price_id("PRO", "monthly")

    def test_stripe_price_contract_requires_active_usd_monthly_119_90_price(self):
        valid = {"active": True, "unit_amount": 11_990, "currency": "usd", "recurring": {"interval": "month", "interval_count": 1}}
        v22_commercial.validate_master_mode_price(valid)
        for invalid in (
            {**valid, "unit_amount": 12_000},
            {**valid, "currency": "eur"},
            {**valid, "recurring": {"interval": "year", "interval_count": 1}},
            {**valid, "active": False},
        ):
            with self.assertRaises(v22_commercial.HTTPException):
                v22_commercial.validate_master_mode_price(invalid)

    def test_stripe_status_mapping_uses_canonical_internal_states(self):
        self.assertEqual(v22_commercial.normalize_stripe_status("trialing"), "TRIALING")
        self.assertEqual(v22_commercial.normalize_stripe_status("active"), "ACTIVE")
        self.assertEqual(v22_commercial.normalize_stripe_status("past_due"), "PAST_DUE")
        self.assertEqual(v22_commercial.normalize_stripe_status("unpaid"), "UNPAID")
        self.assertEqual(v22_commercial.normalize_stripe_status("canceled"), "CANCELLED")

    def test_subscription_webhook_is_idempotent(self):
        state = {"subscriptions": [], "stripe_event_ids": []}
        event = stripe_subscription_event("evt_sub", "trialing", plan="TRIAL")
        self.assertTrue(v22_commercial.apply_stripe_event(state, event))
        self.assertFalse(v22_commercial.apply_stripe_event(state, event))
        self.assertEqual(len(state["subscriptions"]), 1)
        self.assertEqual(state["stripe_event_ids"].count("evt_sub"), 1)

    def test_payment_failure_and_recovery_are_idempotent_state_transitions(self):
        future = int((datetime.now(timezone.utc) + timedelta(days=30)).timestamp())
        state = {"subscriptions": [{
            "user_id": "user-1", "plan": "MASTER_MODE", "status": "ACTIVE",
            "stripe_customer_id": "cus_1", "stripe_subscription_id": "sub_1",
            "current_period_end": datetime.fromtimestamp(future, timezone.utc).isoformat(),
        }], "stripe_event_ids": []}
        failed = {"id": "evt_failed", "type": "invoice.payment_failed", "data": {"object": {"customer": "cus_1", "subscription": "sub_1"}}}
        paid = {"id": "evt_paid", "type": "invoice.paid", "data": {"object": {"customer": "cus_1", "subscription": "sub_1"}}}
        self.assertTrue(v22_commercial.apply_stripe_event(state, failed))
        self.assertEqual(state["subscriptions"][0]["status"], "PAST_DUE")
        self.assertEqual(state["subscriptions"][0]["failed_payment_attempts"], 1)
        self.assertTrue(v22_commercial.apply_stripe_event(state, paid))
        self.assertEqual(state["subscriptions"][0]["status"], "ACTIVE")
        self.assertEqual(state["subscriptions"][0]["failed_payment_attempts"], 0)
        self.assertFalse(v22_commercial.apply_stripe_event(state, paid))

    def test_deleted_subscription_removes_access(self):
        state = {"subscriptions": [{
            "user_id": "user-1", "plan": "MASTER_MODE", "status": "ACTIVE",
            "stripe_customer_id": "cus_1", "stripe_subscription_id": "sub_1",
            "current_period_end": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
        }], "stripe_event_ids": []}
        event = stripe_subscription_event("evt_deleted", "canceled")
        event["type"] = "customer.subscription.deleted"
        self.assertTrue(v22_commercial.apply_stripe_event(state, event))
        self.assertEqual(state["subscriptions"][0]["status"], "CANCELLED")
        self.assertFalse(entitlement_snapshot(state, "user-1")["master_trade_access"])

    def test_legacy_plans_and_demo_license_cannot_grant_master_trade_access(self):
        future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        for plan in ("STARTER", "PRO", "ELITE"):
            state = {"subscriptions": [{"user_id": "user-1", "plan": plan, "status": "ACTIVE", "current_period_end": future}], "licenses": []}
            self.assertFalse(v22_commercial.subscription_for_user(state, "user-1")["master_trade_access"])
        state = {
            "subscriptions": [],
            "licenses": [{"user_id": "user-1", "plan": "ELITE", "status": "ACTIVE", "starts_at": future, "expires_at": future}],
        }
        self.assertFalse(v22_commercial.subscription_for_user(state, "user-1")["master_trade_access"])

    def test_duplicate_records_cannot_resurrect_access_after_canonical_denial(self):
        future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        state = {"subscriptions": [
            {"user_id": "user-1", "plan": "MASTER_MODE", "status": "UNPAID", "current_period_end": future},
            {"user_id": "user-1", "plan": "PRO", "status": "ACTIVE", "current_period_end": future},
        ], "licenses": []}
        self.assertFalse(v22_commercial.subscription_for_user(state, "user-1")["master_trade_access"])

    def test_client_supplied_access_flags_cannot_grant_backend_access(self):
        state = {"subscriptions": [], "licenses": []}
        user = {"id": "user-1", "role": "CUSTOMER", "canAccessMasterTrade": True, "isPremium": True, "plan": "MASTER_MODE"}
        access = v22_commercial.access_snapshot(state, user)
        self.assertTrue(access["canAccessMasterTrade"])
        self.assertFalse(access["isPremium"])
        self.assertFalse(access["canExecuteMasterTrade"])

    def test_unauthenticated_execution_request_is_denied(self):
        request = SimpleNamespace(state=SimpleNamespace(member=None, web_owner_authenticated=False))
        with patch.object(v25_execution, "authenticated_user", side_effect=v22_commercial.HTTPException(401, "Authentication required")):
            with self.assertRaises(v22_commercial.HTTPException):
                v25_execution.execution_owner(request)

    def test_owner_execution_bypass_is_explicit_and_preserved(self):
        request = SimpleNamespace(state=SimpleNamespace(member={"id": "owner", "role": "OWNER"}, web_owner_authenticated=False))
        self.assertEqual(v25_execution.execution_owner(request)["role"], "OWNER")

    def test_every_v25_route_requires_execution_owner(self):
        with open(v25_execution.__file__, encoding="utf-8") as source_file:
            source = source_file.read()
        routes = (
            "/status", "/history", "/mtf/history", "/market/candles", "/connect/read-only",
            "/policy", "/policy/acknowledge", "/consent", "/consent/revoke", "/order/test",
            "/risk/preview", "/adopt-external-position/preview", "/adopt-external-position", "/arm",
            "/disarm", "/recovery/check", "/order", "/auto/start", "/auto/stop", "/position/close", "/emergency",
        )
        for route in routes:
            match = re.search(r'@router\.(?:get|post|put|patch|delete)\("' + re.escape(route) + r'"\)', source)
            self.assertIsNotNone(match, route)
            start = match.start()
            end = source.find("@router.", start + 1)
            block = source[start:] if end == -1 else source[start:end]
            self.assertIn("execution_owner(request)", block, route)


if __name__ == "__main__":
    unittest.main()
