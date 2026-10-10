# Stage 11: informational announcements

Branch: `stage11-announcements`, based on `stage10-events` (`7a89761`).
This feature is informational only: `kind=info`. No advertising audience,
campaign marketing workflow, payment, trading or customer-support changes.

## Read-only findings and design

- Stage 8 uses an atomic outbox claim, a 120-second lease, fenced result
  updates, retry delays of 1/5/15/60/360 minutes, and at most eight attempts.
  Request/startup sweeps and the existing coalesced wake timer are reused.
- Existing mail has purpose-specific authentication templates, not a generic
  announcement sender. `notification_email.py` is also approval-specific.
  The new `campaign_email.py` reuses `email_service.validate_configuration`,
  `validate_app_base_url`, `CANONICAL_EMAIL_ORIGIN`, sender configuration and
  `EmailDeliveryError`. Neither existing mail module was edited.
  The small announcement-only Resend adapter supports custom
  `List-Unsubscribe` and `List-Unsubscribe-Post` headers, unlike the existing
  template interfaces.
- Account approvals require a customer target, a moderator requester and
  `approvals.create`. A typed `campaign.send` alternative has **no target
  user**, an empty target snapshot, and only `campaign_id`/`content_hash`.
  The existing account branch retains its required target and snapshot
  checks. Campaign dispatch uses a separate executor; the account-status
  executor was not edited. Requesters need both `campaigns.manage` and
  `approvals.create`; canonical role, MFA, permissions and self-decision
  protections remain in place.
- Audience selection reads canonical active, email-verified, non-erased users.
  Premium selection calls the existing
  `subscription_core.entitlement_snapshot(...)[master_trade_access]`;
  subscription decision logic is not duplicated or modified.
- Resend supports custom headers and RFC 8058 one-click unsubscribe:
  [send-email API](https://resend.com/docs/api-reference/emails/send-email),
  [unsubscribe headers](https://resend.com/docs/dashboard/emails/add-unsubscribe-to-transactional-emails).
  The actual account/plan limits still need checking in the Resend dashboard.

## Manual migrations: not applied

Apply only through an independently approved database release process:

1. [011 campaigns](migrations/20261010_011_campaigns.sql): campaigns,
   unique campaign recipients, preferences, persistent delivery/test budgets,
   content/status guards and narrow tombstone erasure.
2. [012 approval/outbox contracts](migrations/20261010_012_campaign_approval_outbox.sql):
   targetless typed campaign approval, ID-only outbox payload, campaign
   priority and campaign-only cancellation/deferral transitions.
3. [013 permissions/audit](migrations/20261010_013_campaign_permissions_audit.sql):
   `campaigns.manage`, campaign audit actions and the CAMPAIGN audit target.

No foreign keys or stored recipient email addresses were introduced.
SQL content contracts were tested; these migrations were **not executed
against PostgreSQL**. Real trigger execution, lock ordering, cross-process
contention and connection-pool cancellation recovery remain release gates.

## Content, authorization and delivery

- Subject: 3-120 characters, no header newlines. Body: plain text, at most
  5000 characters; HTML, images, manual email headers and foreign URL origins
  are rejected. Allowed links use `https://kaistrade.com`.
- Rendering escapes text. The system adds its informational disclaimer and
  personalized unsubscribe footer. Preview returns the final text structure,
  the caller's signed footer, real audience/opt-out counts and shared warnings.
  Other recipients necessarily have different signed footer URLs.
- Investment-language warnings are advisory, not automatic rejection.
  The same warnings appear in OWNER approval and send confirmation views.
- Moderators can manage their own drafts; OWNER can access all drafts.
  Pending approval freezes content. Withdrawal is required before editing.
  Approval validates the actual content hash and canonical requester again.
- OWNER direct send requires a content hash, an idempotency key and the
  current confirmed audience count. Count/hash mismatches return 409.
  Approval/direct send atomically writes the campaign start, recipient
  snapshot, ID-only outbox entries and audit. Failure rolls back this start;
  approval notifications retain their original fail-open savepoint behavior.
- Opt-outs are snapshot `skipped` entries, not provider recipients.
  Each delivery rechecks the canonical user, erasure, preference and campaign
  status with database locks. Cancellation closes queued work before send.
- Approval notifications have priority 0; campaign messages have priority 10.
  A separately tracked campaign task cannot block the approval sweep on a
  slow campaign HTTP call. A nonblocking session advisory lock serializes
  campaign claim/delivery across processes; a database-clock delay fences
  actual provider starts at least 0.5 seconds apart. Persistent UTC daily
  accounting includes retries and test attempts, conservatively also counting
  claims that are later skipped. Midnight-crossing sends charge the new day.
- Permanent provider rejection (400/422) is terminal. Other failures use the
  existing retry schedule and become dead after eight attempts. Crashed
  leases and recipient terminal states are reconciled. Completion updates
  counts and writes `campaign.completed`. "Sent" means provider acceptance,
  not confirmed inbox delivery.
- `campaign.test_sent` records **test work queued**, not confirmed delivery.
  Test sends are caller-only, at most five/hour, and excluded from campaign
  recipient/report counts.
- Scheduling accepts 10 minutes to 30 days ahead. Render sleep can delay
  processing; the UI explicitly calls the schedule approximate.

### Configuration

`CAMPAIGN_EMAIL_ENABLED` defaults to **false**. Disabled messages remain queued
and the UI reports that delivery is disabled; they are never marked sent.
If the enabled adapter encounters absent/invalid provider configuration, it
returns `provider_unavailable` for normal retry/dead handling.

`CAMPAIGN_DAILY_LIMIT` defaults to **50**, and must be an integer from 1 to
100000. Campaign delivery additionally requires the existing valid Resend
configuration and canonical `APP_BASE_URL=https://kaistrade.com`.
No environment file was created or edited.

### Unsubscribe

Tokens use an HMAC key derived with announcement-specific domain separation
from the existing secret, a user-bound signature and a 180-day timestamp.
Production sends issue their timestamp from campaign start, not the age of
the draft. GET always renders the same Turkish page and never changes data.
POST changes only announcement preferences, with a 30/minute limit and a
generic response for invalid/unknown tokens. The page also offers resubscribe.
Account/security mail does not read these preferences.

The exact `/announcements/unsubscribe` route is public and both Vercel
configurations route it to the backend before the SPA fallback; other
protected paths retain their existing gate. No login implementation changed.

## Changed surfaces

- Backend new modules: `campaign_content`, `campaign_service`,
  `campaign_executor`, `campaign_routes`, `campaign_email`, `campaign_worker`.
- Existing backend: typed approval models/dispatch, permission/audit
  allowlists, startup router registration, notification scheduling/summary
  kind filtering, and the exact public preference route.
- Frontend: `ModeratorCampaigns.tsx`, `campaign-model.ts`, `campaign-ui.tsx`,
  `CampaignApprovalPreview.tsx`; scoped `mod-*` styling, moderator menu,
  typed approval parsers/cards, and a small `AdminApprovals.tsx` preview branch.
  The shared permission list also supplies the existing admin checkboxes.
  `AdminPanel.tsx` and admin styles were not edited.
- Tests: four campaign backend modules, audit allowlist assertion,
  eight new component cases, and the explicit moderator menu expectation.
  Existing menu/security expectations were not loosened.
- Deployment routing: exact unsubscribe rewrite in both Vercel files.

## Verified results

- Baseline: **671 backend tests + 8 subtests**, **36 frontend tests**.
- Expanded main backend run: **736 passed + 8 subtests**, no failures.
- After final targeted corrections: **141 passed** for campaign,
  approval/executor, outbox/worker and audit modules.
- XML test-identity union: **743 distinct passed tests**, baseline missing
  **0**, new failures **0**. Added: campaign lifecycle 33, content 11,
  transport 8, migrations 3, permission parametrization 1, and unchanged
  canonical subscription tests 16.
- Frontend: **44/44** (36 preserved + 8 new), application strict TypeScript,
  access TypeScript and scoped ESLint passed.
- Both root and frontend production builds passed with
  `COIN_LOGO_OFFLINE=1`.
- Offline Chromium verified moderator draft/live preview/warnings/test/request,
  OWNER exact-count confirmation, inert background/focus containment,
  double-click protection, 409 refresh, queue/report, and 390px no horizontal
  overflow. Every API was mocked; nonlocal browser requests were blocked.
- Pylance syntax checks passed for new service, route, worker and executor.

Artifacts are outside the repository in the session's `files` directory:
`stage11-baseline.xml`, `stage11-final.xml`, `stage11-regressions.xml`,
`stage11-root-build.txt`, `stage11-frontend-build.txt`, `stage11-browser.mjs`,
`stage11-moderator-desktop.png`, `stage11-owner-confirm-mobile.png`,
`stage11-owner-report-desktop.png`.

Protected-module comparison against `stage10-events` showed no edits to
`email_service.py`, `notification_email.py`, `subscription_core.py`,
`approval_executor.py`, `v2_auth.py`, `v22_commercial.py`,
`account_erasure.py` or `moderator_support.py`. Existing mail/auth/reset/
verification/payment/support regression selections passed unchanged.

## Not performed / limits

- No real Resend send, database connection/migration application, push or deploy.
- No bounce/complaint webhook processing; no inbox-delivery certification.
- No profile-settings resubscribe screen. Resubscribe exists only on the
  signed preference page in this stage.
- No exactly-once guarantee beyond the provider's own idempotency retention
  after an ambiguous send/commit crash. Stable provider keys and fenced leases
  reduce duplicates but cannot turn an external provider into a DB transaction.
- Actual PostgreSQL trigger/locking behavior and real Resend plan limits must
  be verified before enabling delivery. Provider credentials and shared
  secret must remain available and unchanged for live signatures/dispatch.
