# Stage 2b role consistency inventory

Reviewed against `stage2-moderator-role` at `094fe4a`; line numbers below refer
to that base, before stage 3 additions. Tests, fixtures, HTML accessibility roles
and LLM user/assistant message roles are not account authorization decisions.
No frontend, payment, subscription, trading, vault or login product code changed.

Risk labels: **a** = unintended OWNER/Premium authority; **b** = compatibility or
normal-user restriction; **none** = correct behavior. No role-comparison-based
type-a escalation was found in the active backend. None of the report-only
type-b or frontend findings has been fixed in this stage.

## Authority and snapshot decisions

Stage 5 follow-up: pending-registration email correction accepts MODERATOR
under the same password, version, active, verification and MFA guards as CUSTOMER.
Erasure ownership classification also recognizes MODERATOR: a different user ID
cannot be matched through a shared email. OWNER protections remain unchanged.

Production entrypoint is [render.yaml:8](../render.yaml#L8):
`rootDir: backend`, `uvicorn app.main:app`. Middleware authenticates protected
requests using the canonical PostgreSQL security row before route handlers
([main.py:1360](app/main.py#L1360)).
`authenticated_user_async` refreshes the worker's user projection from that row
([v22_commercial.py:784](app/v22_commercial.py#L784)).
The synchronous guard requires request-scoped canonical proof whenever the
durable store is enabled ([v22_commercial.py:965](app/v22_commercial.py#L965)).
Thus a Python dict originating in a snapshot is not necessarily stale authority.
Offline non-durable fallback exists; new moderator and audit endpoints require DB.

| Decision (base file:line) | Role source | MODERATOR behavior / risk | Fixed? |
|---|---|---|---|
| [v22_commercial.py:715](app/v22_commercial.py#L715) background session email gate | canonical security row | Requires verified email like CUSTOMER; none | Not needed |
| [v22_commercial.py:980](app/v22_commercial.py#L980), [985](app/v22_commercial.py#L985) email and OWNER guard | snapshot user projection, refreshed per HTTP request; local overlay in non-durable mode | Email verification required; OWNER access denied; none | Not needed |
| [v22_commercial.py:1008](app/v22_commercial.py#L1008) admin count | snapshot users, no per-target refresh here | MODERATOR not counted as admin; reporting, not permission | Not needed |
| [v22_commercial.py:1304](app/v22_commercial.py#L1304) bootstrap post-grant guard | user refreshed from canonical security | Requires OWNER and current version; none | Not needed |
| [v22_commercial.py:1323](app/v22_commercial.py#L1323) bootstrap restoration | snapshot owner_user_id and user identity | Only designated bootstrap identity is restored; other moderators unaffected; no generic a escalation | Not needed |
| [v22_commercial.py:1798](app/v22_commercial.py#L1798) self-erasure | refreshed caller projection | Not protected as OWNER; CUSTOMER erasure path; none | Not needed |
| [v22_commercial.py:1814](app/v22_commercial.py#L1814) Premium/admin access | supplied user dict; HTTP callers refreshed, pure helper itself does no DB read | Only OWNER bypasses subscription; MODERATOR requires own entitlement; none | Tests added |
| [v22_commercial.py:2282](app/v22_commercial.py#L2282) OWNER assignment | request value, not authority | Assignment rejected for any target; none | Not needed |
| [v22_commercial.py:2290](app/v22_commercial.py#L2290) role target protection | target refreshed from canonical DB plus snapshot bootstrap owner_user_id | Moderator can be demoted unless bootstrap identity; none | Not needed |
| [v22_commercial.py:2389](app/v22_commercial.py#L2389) admin erasure precheck | snapshot target; erasure helper refreshes before destructive action | Moderator follows CUSTOMER erasure; none | Not needed |
| [v22_commercial.py:2449](app/v22_commercial.py#L2449) status target protection | canonical-refreshed target | Moderator can be activated/suspended like CUSTOMER; none | Not needed |
| [v22_commercial.py:2504](app/v22_commercial.py#L2504) license target protection | snapshot target, no target refresh in this operation | Moderator not treated as OWNER; stale target role is an existing snapshot consistency limitation, not moderator elevation | Report only |
| [v22_commercial.py:2614](app/v22_commercial.py#L2614) agent revoke | refreshed caller projection; agent ownership in snapshot | Moderator may revoke only own agent like CUSTOMER; none | Not needed |
| [account_settings.py:404](app/account_settings.py#L404) closure blocker | caller refreshed by member dependency | No OWNER closure protection for moderator; normal safety blockers apply; none | Not needed |
| [account_erasure.py:58](app/account_erasure.py#L58) ownership classification | arbitrary snapshot row | OWNER/CUSTOMER row IDs recognized, MODERATOR missing: **b**, erasure classification inconsistency for email fallback | Report only |
| [account_erasure.py:512](app/account_erasure.py#L512), [528](app/account_erasure.py#L528) erasure protection | caller projection initially; explicit canonical refresh at 527 before revocation | Moderator follows CUSTOMER; none | Not needed |
| [email_verification.py:283](app/email_verification.py#L283) pending registration email change | canonical load_user; local overlay only without durable mode | Only CUSTOMER accepted: **b** for promoted pending/unverified account, not ordinary verified moderator | Report only |
| [exchange_connections.py:872](app/exchange_connections.py#L872) time diagnostics | request.state.member, canonical middleware | Moderator denied like CUSTOMER; none | Not needed |
| [main.py:1442](app/main.py#L1442), [1515](app/main.py#L1515) monitoring guards | authenticated caller projection with canonical request proof | Moderator denied like CUSTOMER; none | Not needed |
| [moderator_access.py:73](app/moderator_access.py#L73), [75](app/moderator_access.py#L75) moderator identity | fresh joined canonical SQL every request | OWNER implicit grants; MODERATOR MFA+grants; CUSTOMER denied intentionally | Not needed |
| [moderator_access.py:139](app/moderator_access.py#L139), [145](app/moderator_access.py#L145) grants | canonical actor and target, locked in transaction | Moderator cannot manage permissions; target must be MODERATOR | Not needed |
| [v25_execution.py:1162](app/v25_execution.py#L1162), [1166](app/v25_execution.py#L1166) LIVE gate | canonical-refreshed member/user; subscription state separate | Moderator takes same subscription/session ownership gates as CUSTOMER | Tests added |
| [v25_execution.py:1159](app/v25_execution.py#L1159) web-owner override | request.state.web_owner_authenticated set by configured owner credential, not role | Explicit existing credential bypass applies equally to CUSTOMER and MODERATOR; no role-based elevation | Report only; protected trading code unchanged |
| [exchange_connections.py:289](app/exchange_connections.py#L289) synthetic WEB_OWNER | no member plus authenticated owner-preview credential | Fallback does not replace an existing moderator member | Not needed |

All `owner=True` call sites use the shared OWNER-only guard. They include admin
accounts, monitoring, maintenance, system health, commerce, customer/status,
manual demo licenses, plans and release evidence; the exhaustive executable
endpoint inventory is [test_moderator_access.py:104](tests/test_moderator_access.py#L104).
`account_settings.member(owner=True)` awaits canonical authentication directly.
No token role claim is used as authority by these guards.

Role declarations/assignments (not comparisons): v22 RoleUpdateRequest:1201,
bootstrap:1278/1288, registration:1448/1468, erasure retry:2386,
customer creation:2428, email_verification pending token:97,
google_oauth registration:374. Profile `role: "user"` is a separate profile
field, not commercial permission authority. ADMIN has no backend role grant.

## Frontend comparisons (read-only)

Frontend uses server response/session projections, not direct SQL. A changed
client role can affect presentation but cannot replace backend checks.
Each comma-separated number identifies an individual comparison in that file.
Root production and frontend compatibility sources are both inventoried.

| Base file:lines | MODERATOR outcome | Risk / fixed? |
|---|---|---|
| [AuthGate.tsx:349](../AuthGate.tsx#L349), 461, 604, 608, 611, 613 | Non-OWNER maintenance/email rules, no admin route/menu/banner; CUSTOMER behavior | none / unchanged |
| [premium-access.tsx:39](../premium-access.tsx#L39) | No role-only Premium bypass | none / unchanged |
| [AdminAccountUsers.tsx:52](../AdminAccountUsers.tsx#L52), 58, 70, 108 | Labeled ordinary user; can be targeted for non-OWNER status/reset operations | **b**: no moderator label / report only |
| [ProfileSettings.tsx:158](../ProfileSettings.tsx#L158), 160 | Ordinary-user label and normal paid plan presentation | **b**: no moderator label / report only |
| [AdminPanel.tsx:54](../AdminPanel.tsx#L54), three comparisons | Legacy toggle attempts OWNER assignment for any non-OWNER; backend rejects it; OWNER deletion blocked | **b**: obsolete role-change UI / report only |
| [CommercialHub.tsx:110](../CommercialHub.tsx#L110), 162, 187, 325, 334, 336 (two) | No OWNER overview/edit UI; non-OWNER customer actions remain targetable | none at runtime / unchanged |
| [CommercialHub.tsx:17](../CommercialHub.tsx#L17) | Type excludes MODERATOR | **b** type compatibility / report only |
| [CommerceCenter.tsx:53](../CommerceCenter.tsx#L53), 67, 101, 116, 145, 146, 147, 151, 156 | Normal customer commerce/support path; OWNER writes/buttons unavailable | none at runtime / unchanged |
| [CommerceCenter.tsx:28](../CommerceCenter.tsx#L28) | Prop type excludes MODERATOR | **b** type compatibility / report only |
| [frontend/src/CommercialHub.tsx:99](../frontend/src/CommercialHub.tsx#L99), 160, 185, 322, 331, 333 (two) | Same guards as root copy | none at runtime / unchanged |
| [frontend/src/CommercialHub.tsx:16](../frontend/src/CommercialHub.tsx#L16) | Type excludes MODERATOR | **b** type compatibility / report only |
| [frontend/src/CommerceCenter.tsx:53](../frontend/src/CommerceCenter.tsx#L53), 67, 101, 116, 145, 146, 147, 151, 156 | Same customer/OWNER branches as root copy | none at runtime / unchanged |
| [frontend/src/CommerceCenter.tsx:28](../frontend/src/CommerceCenter.tsx#L28) | Type excludes MODERATOR | **b** type compatibility / report only |

[frontend/src/AuthGate.tsx:1](../frontend/src/AuthGate.tsx#L1) reexports the root
guard; it adds no comparison. Constants such as CUSTOMER TRUST are display copy.

## Legacy copies on disk (not the configured Render entrypoint)

No legacy copy was edited. A separate nested checkout/copy exists at
`tradbt458-main/`; its production use is **UNKNOWN**, not assumed.

| File:lines | Source / MODERATOR behavior | Risk / fixed? |
|---|---|---|
| [root v22_commercial.py:266](../v22_commercial.py#L266), 588, 633, 743 | Snapshot-only guard, target OWNER protections and own-agent restriction; moderator is non-OWNER | No new a elevation, legacy authority lacks current canonical guard / report only |
| [nested v22_commercial.py:266](../tradbt458-main/v22_commercial.py#L266), 588, 633, 743 | Same legacy snapshot-only decisions | Report only |
| [nested backend v22_commercial.py:395](../tradbt458-main/backend/app/v22_commercial.py#L395), 399, 422, 888, 1229 (two), 1292, 1357, 1403, 1513 | Snapshot-based email/OWNER checks, admin count, erase/role/status/license protections, agent ownership; moderator takes CUSTOMER branches | Legacy role endpoint accepts OWNER assignment by OWNER; not moderator self-escalation / report only |
| [nested backend exchange_connections.py:723](../tradbt458-main/backend/app/exchange_connections.py#L723) | Member must be OWNER; moderator denied | none / unchanged |
| [nested backend v25_execution.py:971](../tradbt458-main/backend/app/v25_execution.py#L971) | Only OWNER bypasses normal execution checks | none / unchanged |
| [nested AuthGate.tsx:62](../tradbt458-main/AuthGate.tsx#L62), 190, 250, 252, 253 | Moderator follows ordinary profile/email/admin-route/menu branches | none / unchanged |
| [nested AdminPanel.tsx:34](../tradbt458-main/AdminPanel.tsx#L34), 41 | Legacy role toggle and ADMIN display label; backend guards still separate | **b** obsolete UI / report only |
| [nested CommercialHub.tsx:17](../tradbt458-main/CommercialHub.tsx#L17), 110, 162, 187, 325, 334, 336 (two) | Same union limitation and OWNER-only root branches | **b** type compatibility / report only |
| [nested CommerceCenter.tsx:28](../tradbt458-main/CommerceCenter.tsx#L28), 53, 67, 101, 116, 145, 146, 147, 151, 156 | Same union limitation and OWNER-only root branches | **b** type compatibility / report only |
| [nested frontend CommercialHub.tsx:16](../tradbt458-main/frontend/src/CommercialHub.tsx#L16), 99, 160, 185, 322, 331, 333 (two) | Same compatibility copy | **b** type compatibility / report only |
| [nested frontend CommerceCenter.tsx:28](../tradbt458-main/frontend/src/CommerceCenter.tsx#L28), 53, 67, 101, 116, 145, 146, 147, 151, 156 | Same compatibility copy | **b** type compatibility / report only |
| [nested TestnetFirstApp.tsx:210](../tradbt458-main/TestnetFirstApp.tsx#L210) | OWNER or canAccessMasterTrade controls view access, not paid execution | No role-only moderator Premium / unchanged |
| [nested root v25_execution.py:290](../tradbt458-main/v25_execution.py#L290) | Synthetic WEB_OWNER for explicit web credential, not moderator role | none / unchanged |

## Verification

Stage 3 baseline: 288 passed, 5 subtests passed, zero failed.
Additional unchanged subscription/LIVE baseline: 108 passed, zero failed.
New stage-2b tests cover no-subscription MODERATOR denial, forged/stale OWNER
projection denial, and CUSTOMER/MODERATOR identical LIVE gate outcomes for
read/write, subscription and cross-session ownership.

## Stage 3 audit implementation and final verification

- New, **unapplied** migration:
  [20261010_002_audit_log.sql](migrations/20261010_002_audit_log.sql).
  No actor/target FK or erasure integration; rows survive user deletion.
  BEFORE UPDATE/DELETE/TRUNCATE statement trigger rejects mutations, including
  empty-table operations; ENABLE ALWAYS keeps it active in replication mode.
  BEFORE INSERT overrides caller-supplied created_at with server clock time.
- [audit_log.py](app/audit_log.py) defines three fixed actions and one writer,
  `write_audit(conn, actor, action, target_type, target_id, before, after,
  reason=None, approval_request_id=None)`. It refuses autocommit connections.
  ROLE_CHANGED permits only validated role; permission actions permit only a
  validated permission name and boolean granted flag. All other JSON fields
  are dropped. Free-form reason is replaced by `[REDACTED]` when nonempty.
  IDs are opaque; correlation IDs are UUIDs (invalid inbound values generate
  a new audit UUID), IPv4/IPv6 peers are masked to /24 and /48. Forwarded
  IP headers are not trusted. Invalid context/persistence warnings log type,
  never supplied values.
- Only role changes and permission grant/revoke are connected. Role revocation
  uses the existing account-store transaction so failed audit/commit restores
  the local user projection and auth baseline too. Actor and target canonical
  security rows are locked/rechecked in stable order. The old snapshot audit
  structure is retained, not converted to this table.
- Permission idempotent retries are logged with their actual before/after
  booleans, including true->true grants and false->false deletions. Original
  permission attribution remains unchanged.
- `GET /api/v22/admin/audit` is OWNER-only. Response:
  `{items, total, limit, offset}`; limit defaults to 50 and is constrained to
  1..100. Filters: actor, target, action, created_from, created_to. Date bounds
  must include a timezone; reversed bounds are rejected. Ordering is
  created_at DESC, id DESC. Missing storage returns 503, never empty success.
- Main only imports/includes the audit router. The only v22 product change
  is the role administration endpoint. No frontend, trading, vault, login,
  payment/subscription or snapshot schema change was made.
- Final affected tests: **462 passed, 0 failed, 5 subtests passed**. Baselines:
  **288 + 108 passed, 0 failed, 5 subtests passed**. Existing baseline test IDs
  are retained. Python syntax/compile checks and git diff whitespace check pass.
  One existing Starlette/AnyIO deprecation warning remains.
- Added tests:
  [test_moderator_role_consistency.py](tests/test_moderator_role_consistency.py),
  [test_audit_log.py](tests/test_audit_log.py),
  [test_audit_mutations.py](tests/test_audit_mutations.py),
  [test_audit_read.py](tests/test_audit_read.py).
  Existing moderator fake transactions now model audit/settings rollback and
  active-transaction proof; assertions were not weakened.
- **Not verified on real PostgreSQL**: no DB connection or migration execution
  occurred. Trigger behavior is checked as SQL text, and atomicity is exercised
  against offline transaction fakes. No push or deploy occurred. A database
  superuser/table owner capable of changing triggers remains outside this
  append-only guarantee. Production DB role/DDL permissions are **UNKNOWN**.
