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

## Mandatory view audit and shared request cap

Profile, subscription and payments detail reads create fixed actions:
`customer.viewed`, `customer.subscription.viewed`, `customer.payments.viewed`.
Their before/after allowlists are empty; evidence contains the target ID only,
plus the normal actor/request/masked audit context. OWNER detail reads are also
audited. Ineligible/absent targets do not create view records. Listing is rate
limited but does not create per-customer profile-view audit records.

Migration [20261010_003_moderator_read_audit_actions.sql](migrations/20261010_003_moderator_read_audit_actions.sql)
extends only the action CHECK, preserving stage-3 insert-only triggers and old
evidence. It is **not applied**; required migrations must be installed separately
before use. Missing audit storage/unsupported action causes 503, never data.

Detail data is constructed inside the same transaction as the audit INSERT.
Response delivery occurs only after successful commit; INSERT/commit failure
returns 503 with no customer payload. Targets are locked FOR SHARE while checking
their canonical CUSTOMER role. Caller role/version/MFA/permission are rechecked
from DB inside the read transaction, independent of token role claims.

Existing `commercial_auth_limits` stores a dedicated SHA-256 user-ID bucket.
All four routes share 30 requests per 60-second fixed window per actor, including
OWNER; changing token or IP does not reset it. The counter is committed separately
so audit failures and 404 requests cannot undo consumed attempts. Missing rate
storage fails closed; attempt 31 returns 429 and Retry-After: 60. Permission/MFA
denials happen before customer reads/counter writes. This does not modify the
existing login rate-limit configuration.

Writes added by this module are infrastructure only: view audit rows and dedicated
rate counters. Existing authentication/session validation is preserved. There are
no POST/PATCH/PUT/DELETE customer routes, payment writes or Stripe API calls.

## Response contracts

- Profile keys: user_id, email_masked, role, active, created_at, email_verified,
  mfa_enabled.
- Subscription keys: user_id, plan, subscription_status, current_period_end,
  cancel_at_period_end.
- Payment keys: user_id, payment_status, last_failed_payment_at.
- List envelope: items, total, limit, offset; each item is the profile contract.

Models forbid extras, validate masked email/opaque user IDs and restrict status
values. Corrupt persisted values fail with type-only warning and 503 rather than
being reflected. No data is invented when dates/payment records are unavailable.

## Final validation and limits

- Baseline: 462 passed, 0 failed, 5 subtests passed.
- Final same baseline plus new read tests: **552 passed, 0 failed, 5 subtests passed**.
  90 additional test IDs; no baseline test removed.
- New test files: test_moderator_customers.py, test_moderator_customer_responses.py,
  test_moderator_customer_audit.py. Existing audit migration-content assertion
  also checks the new extension's action list; no existing assertion relaxed.
- Python syntax/compile checks and git whitespace gate pass. Existing
  Starlette/AnyIO deprecation warning remains.
- No real PostgreSQL validation was performed; SQL reads, rate counters and
  transactional audit behavior are exercised against offline fake stores.
  No database connection, migration application, push or deploy occurred.
- Earlier payment-failure timestamps after a later successful payment cannot
  be reconstructed from the current persisted schema; null is explicit.
  Snapshot registration time may be absent; it also returns null.
- Public response validation guarantees field and scalar shape, not freshness
  of external provider state; only the data currently stored in PostgreSQL
  is represented. No provider lookup or inferred entitlement is performed.
