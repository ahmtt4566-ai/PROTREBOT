# Stage 4 moderator customer reads

Base: `stage3-audit-log` at `ab42296`. No provider API calls, customer mutations,
frontend edits, snapshot restructuring or migration execution.

## Step 0: persisted sources

| Response field | Source (base file:line) |
|---|---|
| user_id | commercial_auth_users.user_id, [v22_commercial.py:271](app/v22_commercial.py#L271) |
| email_masked | security.email from the same canonical row; existing [masked_recipient:126](app/email_service.py#L126) |
| role / active / email_verified | security JSON, [AUTH_SECURITY_FIELDS:98](app/v22_commercial.py#L98); never worker-local users |
| created_at | matching users entry in persisted application_state_snapshots with state_key v22-commercial; [registration:1468](app/v22_commercial.py#L1468), [Google registration:372](app/google_oauth.py#L372). Canonical security lacks registration time; absent persisted value returns null. No full snapshot is returned. |
| mfa_enabled | commercial_account_settings.payload.two_factor_enabled, [account_settings.py:186](app/account_settings.py#L186); missing settings means MFA not enabled |
| plan / subscription_status / current_period_end / cancel_at_period_end | latest persisted subscriptions row by updated_at DESC, id DESC, [schema:337](app/v22_commercial.py#L337); no entitlement calculation or snapshot fallback |
| payment_status | subscriptions.last_payment_status, [persistence:477](app/v22_commercial.py#L477); null/missing record returns literal `veri yok` |
| last_failed_payment_at | subscriptions.last_payment_at ONLY when last_payment_status=FAILED, [invoice bookkeeping:1981](app/v22_commercial.py#L1981). A later successful payment overwrites this status/time; previous failure history is not available and is returned as null, never guessed. |
| erased-user exclusion | commercial_erased_users hash matched against canonical user_id, [guard:185](app/account_erasure.py#L185) |

## GET API

- `/api/mod/customers`: customers.view, limit 1..50 (default 25), offset >= 0,
  search exact user_id OR case-insensitive complete email equality. No LIKE,
  prefix, substring or wildcard search. Page envelope: items/total/limit/offset.
- `/api/mod/customers/{user_id}`: customers.view; profile fields only.
- `/api/mod/customers/{user_id}/subscription`: subscriptions.view.
- `/api/mod/customers/{user_id}/payments`: payments.view.

All routes use require_permission, hence canonical role/grants and required
MODERATOR MFA. OWNER has implicit permissions, CUSTOMER is denied. Only canonical
CUSTOMER targets are visible; self, OWNER, MODERATOR, absent and erased targets
all return the same 404. Profile DTOs do not contain names or preferences.

Only allowlisted scalar columns are selected from customer/billing stores.
Response models forbid extra fields. Stripe identifiers/raw payloads, session
data, exchange credentials/connections, IPs and trading state are never included.

## Baseline

462 passed, 0 failed, 5 subtests passed. Real PostgreSQL remains unverified;
all validation is offline and no migration is applied.
