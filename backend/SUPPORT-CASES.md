# Stage 6: internal support cases

Branch: `stage6-support`, based on `stage5-mod-panel` at `916ab93`.
No migration was applied. No push/deployment was performed.

## Step 0: existing system (read-only inspection)

The existing customer flow is
[v24_commerce.py:202](app/v24_commerce.py#L202), registered under
`/api/v22/commerce/support`. A snapshot ticket contains:

| Field | Source/behavior |
| --- | --- |
| `id` | `uuid.uuid4().hex` |
| `user_id` | authenticated customer's ID |
| `customer` | `public_user(user)` projection, not copied into the new cases |
| `subject` | 3-160 characters |
| `message` | 5-2000 characters |
| `priority` | LOW, NORMAL, HIGH |
| `status` | initially OPEN |
| `created_at`, `updated_at` | server ISO timestamps |
| `demo_only` | true |
| `response_note`, `updated_by` | added by an OWNER update |

The new ticket is prepended; entries beyond 500 are removed from the snapshot.
The old OWNER PUT changes only `status`, trimmed `response_note`, `updated_at`
and `updated_by`; its statuses are OPEN/IN_PROGRESS/RESOLVED/CLOSED.
Neither that route nor the old customer route/UI was changed.

`support_tickets` is an erasable personal list in
[account_erasure.py:20](app/account_erasure.py#L20). Its existing database flow
inserts a `commercial_erased_users` tombstone in the erasure transaction.
The new migration attaches a content-cleaning trigger to that existing insertion;
the Python erasure implementation is unchanged.

## Step 1: unapplied migrations

- [004](migrations/20261010_004_support_cases.sql): separate `support_cases`,
  append-only `support_notes`, and a durable 30-second `support_sync_state`.
  Legacy IDs are unique; case/customer/assignee/author identities have no user FK.
  Notes have a case FK with no cascading deletion.
- [005](migrations/20261010_005_support_audit_actions.sql): preserves previous audit
  actions/types and adds five support actions plus the SUPPORT_CASE target type.

Notes reject UPDATE, DELETE and TRUNCATE. The user explicitly approved the sole
exception: a nested erasure trigger can set `body=''` only when a matching
customer/author tombstone exists, with every other field unchanged. Direct
updates, other changes and normal empty-note insertion remain rejected.

Customer erasure clears case subject/message/legacy response and all its note
bodies. Author erasure clears that author's note bodies, including notes on other
customers' cases. Opaque IDs/history are retained. A shared transaction advisory
lock serializes imports/writes with tombstone scrubbing; guards reject erased
customers/authors to prevent stale content resurrection.

SQL contracts are tested, but PostgreSQL trigger execution was NOT tested.
The offline erasure test calls the real Python `erase_database` with a model of
the migration trigger, not a real PostgreSQL engine. Privileged DB administrators
can alter/disable triggers; backups/external logs are not erased by this feature.

## Step 2: one-way sync

[support_sync.py](app/support_sync.py) SELECTs the persisted `v22-commercial`
snapshot. It does not use legacy `save_state`/persistence helpers or write to
`application_state_snapshots`. Only canonical CUSTOMER, non-tombstoned users are
imported. Snapshot customer projections cannot grant visibility or authority.

On conflict it updates only subject, message, priority, legacy status/response
and sync timestamp. Moderator status, assignee, notes, version and legacy creation
time are preserved. A ticket ID cannot move to another user. Initial internal
status is NEW; legacy state remains separate and is never a moderator action.
Cases that fall out of the old 500-ticket snapshot remain as team history.

List and overview-summary requests run the idempotent sync, with a database
cache shared across workers for 30 seconds. Validation/storage failures roll back
the sync/cache transaction and are surfaced without logging ticket content.

## Step 3: protected endpoints

| Endpoint under `/api/mod/support` | Permission |
| --- | --- |
| GET `/cases`, GET `/cases/{id}`, GET `/summary` | support.view |
| POST `/cases/{id}/take`, `/release`, `/status`, `/notes` | support.manage |

Canonical permission/MFA dependencies are reused. CUSTOMER is denied; OWNER has
implicit permissions. Every request also rechecks the canonical actor in its
transaction and uses the existing durable shared customer/support 30-per-60s
bucket, including failed requests and all writes.

The shared transaction actor SELECT now includes `email_verified`, which its
existing guard already checked; this fixes a missing selected column rather than
weakening the guard.

Targets that are OWNER, MODERATOR, self, absent or erased return 404. Lists use
the same canonical rules. Customer detail reuses the exact stage 4 CustomerSummary
DTO; no full email, credentials, preferences or provider data are returned.
Assignments and note authors are opaque IDs, not names/emails.

Take has an atomic `WHERE assignee_user_id IS NULL`. Release/status/note require
the actor's own assignment. Status also requires the expected integer version;
conflicts are 409. Every successful team mutation increments version.
Notes are plain text, nonblank, 1-2000 characters.

Case detail and every mutation write an audit row in the same transaction.
`before`/`after` allowlists are empty: no customer, message, note or secret values
can enter audit; the target is the case ID. Audit/commit failure rolls back writes
or suppresses detail with 503. List/summary do not create view-audit rows.

Subject, message, legacy response and note body are masked before responses.
Patterns cover sk_/pk_, Bearer values, explicit API key/secret/token/password
assignments, 13-19-digit card-like sequences and contiguous 24+ character
token-like strings. Replacements are `[GİZLİ]`. This is intentionally conservative
pattern-based redaction, not a guarantee of recognizing every possible secret.
Stored bodies remain personal data and are scrubbed by erasure.

Summary counts NEW/OPEN/WAITING as open. Unassigned/mine exclude RESOLVED/CLOSED.
Read failure logs its error type and yields null count fields, never invented zero.
Authorization/MFA/rate errors remain errors and are not converted into counts.

## Step 4: moderator UI

- [ModeratorSupport.tsx](../ModeratorSupport.tsx): filtered, paginated card list,
  detail loader and team mutations; permission-aware overview counts.
- [support-ui.tsx](../support-ui.tsx): status/priority pills, summary cards and
  message/customer/team/note cards.
- [support-model.ts](../support-model.ts): allowlisted response projection,
  filter construction, Turkish labels and edit guards.
- [ModeratorPanel.tsx](../ModeratorPanel.tsx): replaces support's Yakinda screen
  and links customer cards into the existing profile view only with customers.view.
- Existing moderator transport/deadline/error helpers are shared. Customer calls
  remain GET-only; only support actions POST. A 409 refresh reloads detail and
  NEVER repeats the mutation.
- Styles are moderator-only `mod-*`, with no admin stylesheet changes.
  Messages/notes use normal React text rendering, not HTML interpretation.
  Without manage+own-assignment, note/status inputs are disabled. Mobile cards,
  44px controls, keyboard focus and reduced-motion rules are preserved.

The existing customer list is not requested while a linked profile is open;
empty search is omitted instead of sending an invalid zero-length query.
This small integration correction keeps support-to-customer navigation usable
against the real backend validation.

## Validation and scope

Baseline: 112 backend tests, 11 small frontend tests, strict active-frontend
TypeScript and offline production build passed.

Final: 163 backend tests passed, zero failures. Exact JUnit ID comparison:
51 new cases, zero missing baseline cases. The unchanged registered OWNER-route
inventory remains covered by parameterized MODERATOR-403 tests.
18 frontend tests passed, zero failures (all 11 baseline tests retained, 7 new).
Strict active frontend/access TypeScript, targeted ESLint and production build
passed. Existing Starlette/AnyIO deprecation warning remains.
No full/long backend or Playwright suite was run.

Snapshot no-write evidence:

- `test_sync_is_idempotent_preserves_team_fields_and_never_writes_snapshot`
  rejects a non-SELECT SQL statement mentioning the snapshot store.
- `test_all_support_reads_and_writes_fail_test_if_snapshot_writer_is_called`
  covers list, summary, detail, take, status, note and release with the old
  save/persist helpers patched to fail; every captured snapshot SQL is SELECT.
- `test_list_triggers_cached_sync_filters_page_limit_and_never_writes_snapshot`
  verifies one snapshot scan for repeated lists.

Other new tests cover concurrent take, assignment/version, exact 2000-character
boundary, HTML text, hidden targets, masking, all audit writes, audit/commit
rollback, shared rate limit, summary nulls and tombstone content cleanup.

Local offline Chromium (1440px desktop/390px mobile), with all APIs mocked and
third-party requests blocked, verified UI reads/writes, expected-version payload,
plain-text XSS samples, read-only guards, customer profile navigation and
refresh-only 409 behavior. No page errors or mobile horizontal overflow.
Screenshots and test/build outputs are outside the repository:
`stage6-support-desktop-list.png`, `stage6-support-desktop-detail.png`,
`stage6-support-mobile-detail.png`, `stage6-baseline.xml`, `stage6-final.xml`,
`stage6-build-baseline.txt`, `stage6-build-final.txt` in the session artifact folder.

Admin component/style files, the old customer-support flow in v24_commerce.py,
login, payments/subscriptions/trading/vault product code and the erasure Python
implementation have no diff from stage 5. The only main.py changes register the
new router. All migrations remain unapplied; no live DB, Stripe or other provider
was contacted for validation. Production functionality is not claimed verified.
