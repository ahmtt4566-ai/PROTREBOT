# Stage 10: safe customer technical history

## Scope and baseline

- Local branch: `stage10-events`, created from `stage8-notifications` at `ca26c70`.
- No push, deploy, database migration application, or real service requests.
- Baseline: 641 backend tests, 8 subtests; 30 frontend tests; strict TypeScript and offline production build passed.
- Baseline-plus-events run: 670 backend tests and 8 subtests passed, zero failures. XML identity comparison retained all 641 baseline cases and added 29 cases.
- Frontend: all 30 baseline cases plus 6 new cases passed (36/36); strict application and access TypeScript, changed-file ESLint and offline production build passed.
- After the legacy-verification instrumentation addition, the complete affected authentication
  selection passed 231 tests and 8 subtests. The union of both final selections is 671 distinct
  backend cases: all 641 baseline cases plus 30 additions, zero failures.

## Step 0 findings

### Existing system errors

[`app/error_monitoring.py`](app/error_monitoring.py) stores identifiers, fingerprint, source/service/kind/code,
severity/status, route/method, request ID, user ID, occurrence counts and timestamps, plus raw message,
context, details, stack and operator notes. There is no dedicated HTTP-status column.

The new reader explicitly projects only fixed kind/code/feature values, first/last timestamps,
normalized severity and resolved status. System-error HTTP status is `null`, rather than inferred
from text or context. Raw route, source/service strings, fingerprint, message, notes, stack, context,
details, email and IP are never returned.

Important existing limitation: the open-error fingerprint is not user-specific. Its conflict update
increments occurrences without changing the original user ID. Therefore a stored system error may
aggregate occurrences from different users. System rows expose **count 1**, not that unsafe aggregate,
and are labelled as system-source errors. Only the new customer-specific event rows supply reliable
repeat counts. Existing error producers were not changed; historical errors cannot be reconstructed
as a complete, user-specific history.

### Existing account activity

[`app/account_settings.py`](app/account_settings.py) retains up to 100 activity entries, including
login, profile/preferences changes, email-change requests/completion, password changes, MFA
enable/disable, session revocation and account closure. Only fixed event type and timestamp are safe
for this feature; activity message strings are not reused or exposed. No backfill is performed.

### Smallest recording points

The shared successful-login point is `login_record`, already used by password, MFA and Google login.
Existing failure branches, reset request/completion, verification delivery/result and session-revocation
success points receive non-awaited single-line calls. A central response hook handles known members'
5xx, 409 and 429 responses without examining response bodies.

Unknown/invalid tokens and challenges that cannot be securely associated with an existing user do
not create a guessed user event. The database insert guard independently rejects absent or erased
users. The writer never looks up an email address on the request path.

## Storage, concurrency and isolation

Manual-only migrations:

- [`009_customer_events`](migrations/20261010_009_customer_events.sql): no user FK, no free-text/PII
  fields, enum checks, per-user advisory lock, rolling ten-minute aggregation, guarded updates,
  retention deletion, tombstone erasure and TRUNCATE protection.
- [`010_customer_event_audit`](migrations/20261010_010_customer_event_audit.sql): preserves prior
  audit actions and adds `customer.events.viewed`.

As explicitly selected during Step 0, the rolling-hour limit reserves **49 ordinary rows plus one
fixed `api.error / event_limit / events` counter**. Further unmatched events increment that counter;
they are not falsely labelled as login or MFA failures. Matching ordinary events can still aggregate.

[`app/customer_event_writer.py`](app/customer_event_writer.py) validates fixed values and schedules
storage with `call_soon`; the request does not await it. Both the public writer and the call-site
wrapper isolate exceptions and log only fixed warning text and exception type. Each insert has one
connection/transaction; aggregation and caps live in the SQL trigger.

There are at most 64 running telemetry tasks per application. Capacity/storage failures produce
safe warnings and do not delay or fail authentication. This is deliberately **best-effort telemetry**,
not a durable outbox: process shutdown/crash or capacity exhaustion can lose an event. No success-
shaped database fallback is used.

Cleanup starts once at application startup and is scheduled at most once per minute by event reads.
Each pass deletes at most 500 rows whose last event is older than 90 days. This bounds work, but a
large historical backlog may require multiple passes. Erasure deletes all matching event rows through
the existing narrow tombstone pattern, serialized against inserts by the same per-user lock.

## Safe reader and permissions

[`app/moderator_events.py`](app/moderator_events.py) adds:

`GET /api/mod/customers/{user_id}/events`

- Canonical `events.view` guard and shared 30-requests/60-seconds customer-read limit.
- Existing mandatory MFA/verified-email rules for MODERATOR; existing canonical OWNER permission
  bypass semantics remain unchanged.
- Staff, self, missing and erased targets return 404.
- Timezone-aware start/end filters, fixed kind filter, bounded pagination (maximum 50).
- Exact DTO: kind, code, feature, HTTP status, count, first/last time, eight-character opaque request
  reference, source, normalized severity and resolved flag.
- Audit is in the same read transaction before returning data. Audit or storage failure returns 503
  with no event data; malformed stored enum values fail closed.

The combined query selects a safe projection, not raw system-error records. The outer `SELECT *`
refers only to that allowlisted projection.

## Screens and OWNER access

- [`CustomerEvents.tsx`](../CustomerEvents.tsx) and
  [`customer-event-model.ts`](../customer-event-model.ts): Turkish dictionary, timeline badges,
  real repeat counts, distinct overflow label, date/kind filters, empty/error/retry and pagination.
  Unknown code labels display `Diğer olay`, never the raw code. All text is React-escaped.
- [`ModeratorPanel.tsx`](../ModeratorPanel.tsx): profile history only with `events.view`; OWNER
  return-to-admin link; OWNER cannot render the moderator approval creation form or fetch the
  moderator's own-request overview/list. The approvals section instead links to admin user management.
  Existing support-to-profile navigation is preserved.
- [`moderator-panel.css`](../moderator-panel.css): six `mod-*`-scoped timeline/filter rules; responsive
  wrapping, no new admin styles.
- [`account-role.ts`](../account-role.ts): `/moderator` permits OWNER as well as MODERATOR;
  CUSTOMER remains excluded. The corresponding routing assertion changes intentionally.
- [`AdminPanel.tsx`](../AdminPanel.tsx): only the menu entry/redirect and a users deep-link initializer
  were added, plus the necessary menu-key type. The initializer makes the OWNER notice's
  `/admin?section=users` link open the actual user-management section. Existing admin sections and CSS
  were not redesigned.

OWNER customer search, customer detail, support detail/take/note/status/release and technical-history
access are covered through canonical production guards. Target-specific audited reads and writes
retain `actor_role=OWNER`. Existing list/summary auditing conventions were not broadened; no customer
support product code changed. Approval creation remains moderator-only; existing approval permission
and OWNER-only endpoint regressions remain in the preserved baseline.

## Exact authentication additions

Existing guards, sleeps, awaited operations, token mutation and response statements were not replaced.
The authentication diff is insertions only:

| File | Line(s) | Added recording |
|---|---|---|
| `app/v22_commercial.py` | 31 | Import the isolated wrapper |
| `app/v22_commercial.py` | 1381 | Existing-user login failure after existing timing padding |
| `app/v22_commercial.py` | 1504, 1507 | Registration verification delivery success/failure |
| `app/v22_commercial.py` | 1540, 1547, 1556 | Legacy verification known-account failure, inactive-user failure, completion |
| `app/v22_commercial.py` | 1608, 1611 | Legacy resend delivery failure/success |
| `app/v22_commercial.py` | 1711, 1762 | Password-reset request/completion |
| `app/v22_commercial.py` | 2343, 2385 | OWNER reset request / session revocation |
| `app/account_settings.py` | 199, 688, 774 | Existing MFA failure branches |
| `app/account_settings.py` | 290, 298, 301 | Existing verification-only delivery failure/success |
| `app/account_settings.py` | 731 | Shared successful login |
| `app/account_settings.py` | 799, 815 | Session / other-session revocation completion |
| `app/account_settings.py` | 905 | OWNER account reset request |
| `app/account_store.py` | 263, 266 | Legacy verification-token consumption failure; v2 excluded to avoid double counting |
| `app/email_verification.py` | 245, 248 | v2 verification delivery failure/success |
| `app/email_verification.py` | 308, 311 | Existing pending-email verification delivery failure/success |
| `app/email_verification.py` | 384, 390 | Known-user verification failure/completion |
| `app/main.py` | 1413 | Non-awaited known-member 5xx / 409 / 429 response recording |

`app/main.py` also imports/registers the new reader and schedules startup cleanup/shutdown task
cancellation. No new awaited telemetry operation was added to authentication request handling.

The new isolation tests inject a throwing recorder into successful/failed login and invoke the
**unchanged existing** reset/MFA and cross-worker verification/replay regressions under the same
failure. The legacy replay test additionally requires exactly one failure event. Existing login,
MFA, reset, verification and Google tests remain unchanged.

## Test files and evidence

New backend tests:

- [`test_customer_event_migrations.py`](tests/test_customer_event_migrations.py): SQL contracts.
- [`test_customer_event_writer.py`](tests/test_customer_event_writer.py): allowlists, aggregation,
  rolling-hour 50-row ceiling, parallel submissions, absent users, safe logging, non-waiting scheduling,
  retention and production erasure integration using offline pools.
- [`test_customer_event_auth.py`](tests/test_customer_event_auth.py): recorder-failure isolation,
  nonexistent login and legacy verification replay.
- [`test_moderator_events.py`](tests/test_moderator_events.py): exact safe DTO, unsafe-system-field
  scan, enums, permission/MFA/staff/self/erasure, OWNER grants/audit, audit failure, pagination and rate limit.
- [`test_owner_moderator_events.py`](tests/test_owner_moderator_events.py): OWNER search/detail and
  support operations, role-aware audits and staff/self hiding.

The existing [`test_audit_log.py`](tests/test_audit_log.py) now checks migration 010's exact action list
while preserving previous-action inclusion assertions. No security expectation was relaxed.

Frontend regressions:
[`customer-events.test.mjs`](../tools/customer-events.test.mjs), plus the intentional OWNER routing
assertion in [`moderator-routing.test.mjs`](../tools/moderator-routing.test.mjs).

Offline Chromium additionally verified OWNER moderator/admin/users navigation, hidden request
creation, no moderator-approval requests, timeline, kind filtering, no-data/503-safe retry, GET-only
API requests, mobile no-horizontal-overflow and zero page errors. All API responses were intercepted,
and non-local traffic was blocked. An initial browser attempt had an incomplete mock of the existing
admin accounts endpoint; correcting the test fixture made the same scenario pass without product changes.

Screenshots outside the repository:

- `C:\Users\ahmtt\.copilot\session-state\301a903d-273a-4d29-87b4-b58003522a5e\files\stage10-owner-events-desktop.png`
- `C:\Users\ahmtt\.copilot\session-state\301a903d-273a-4d29-87b4-b58003522a5e\files\stage10-owner-events-mobile.png`

## Boundaries not claimed as tested

- **Real PostgreSQL was not used.** SQL trigger execution, actual advisory-lock concurrency,
  timestamp behavior and database erasure are supported by migration inspection and an offline model,
  not a live PostgreSQL certification.
- Migrations were not applied to any database. The live schema must be reviewed/applied separately
  before deploying this feature.
- No real Resend, Stripe, trading, Binance, Render or production authentication request was made.
- No wall-clock latency benchmark was performed. Structural non-waiting behavior and original
  authentication responses/security regressions are tested; scheduling has a small synchronous cost.
- No live production UI validation or deployment was performed.
- Existing error-log cross-user aggregation cannot be repaired or backfilled within this read-only
  feature. Its unsafe occurrence count is not exposed.

Protected files/flows such as `email_service.py`, `google_oauth.py`, `moderator_support.py` and
`v24_commerce.py` have zero diff from Stage 8. Subscription, payment, trading, custody and existing
OWNER account-status behavior were not modified.
