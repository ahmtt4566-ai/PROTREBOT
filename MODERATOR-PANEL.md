# Moderator panel (stage 5)

Stage 6 adds internal support case management and real support overview counts.
Customer/subscription/payment screens remain read-only. See
[support cases](backend/SUPPORT-CASES.md) for migrations, erasure rules and tests.
The stage 5 results below describe the original baseline.

## Integration decisions

- The admin shell, navigation and top bar are private, stateful pieces of
  `AdminPanel.tsx`. Extracting them would affect admin polling and workflows.
  The moderator panel instead shares the existing `--admin-*` tokens and
  `system-ui, sans-serif` font from `admin.css`, without editing that stylesheet.
- `AuthGate.tsx` receives the role from `/api/v22/session`. Permissions are not
  part of that response: the panel uses the existing canonical `/api/mod/me`.
  Authentication, MFA enrollment and session creation are unchanged.
- Authenticated landing routes are OWNER -> `/admin`, MODERATOR -> `/moderator`,
  CUSTOMER -> `/dashboard`. Moderator admin URLs redirect to the moderator panel;
  customer moderator URLs redirect to the dashboard. `/settings`, `/profile`,
  normal user pages and maintenance guards are preserved. Backend authorization
  remains the authority, not frontend routing.
- Pending registration email correction accepts MODERATOR with exactly the
  CUSTOMER password/version/active/verification/MFA guards. Erasure classification
  recognizes MODERATOR IDs rather than falling back to another account's email.
  No OWNER or last-account protection was changed.

## Read-only interface

`ModeratorPanel.tsx`, `moderator-ui.tsx`, `moderator-model.ts`,
`moderator-api.ts` and `moderator-panel.css` provide the independent shell and
small card, badge, empty-state, error, search and resource components.

All moderator resource requests are GET, with cancellation and a 20-second
deadline. Search is submitted explicitly rather than per keystroke; pagination
requests 25 results. The server still enforces exact email/ID matching, max 50,
permissions, MFA, the durable request cap, target hiding and mandatory read audit.
Subscription/payment lookup also works when the actor lacks `customers.view`,
using the customer's user ID without a customer-profile request.

Response parsers project only allowlisted fields and reject malformed data,
unmasked email and staff profiles. No full actor email, raw errors, credentials,
provider IDs, preferences or trading data are displayed. Null data is not
converted into a price, plan or successful status. Unimplemented sections say
`Yakında`. Errors include Turkish MFA, 401, 403, 404 and 429 guidance.

The mobile menu is collapsible, controls have at least 44-pixel height,
keyboard focus is visible, a skip link is available and reduced-motion disables
transitions. Moderator styles use only `mod-*` selectors and existing admin
tokens; there are no admin stylesheet edits.

## Admin edits

- `AdminPanel.tsx`: only the legacy role mutation and role-button label/OWNER
  disable guard were changed. It assigns MODERATOR or CUSTOMER, never OWNER.
- `AdminAccountUsers.tsx`: moderator role labels in the detail and list, plus
  the small role/permission component. Existing account actions are unchanged;
  their busy state is shared to prevent overlapping mutations.
- New `AdminModeratorAccess.tsx`: role toggle and seven permission checkboxes,
  mounted only inside the existing OWNER-only admin surface. Checkbox state is
  displayed only after the actual stored grants load; writes reload that state.
- `backend/app/moderator_access.py`: one necessary OWNER-only GET at
  `/api/v22/admin/users/{user_id}/permissions`, returning target ID and current
  grants. It reuses canonical owner/target checks under a transaction, refuses
  non-MODERATOR targets, and fails closed on storage errors. Existing POST/DELETE
  perform writes with the existing audit transaction.
- `CommercialHub.tsx`, `CommerceCenter.tsx` and their frontend compatibility
  copies have type-union additions only. Payment/subscription behavior is not
  modified. `account-settings-api.ts` shares the account role type;
  `ProfileSettings.tsx` only changes the role label.

## Verification

- Baseline: strict access TypeScript and production build passed; 38 existing
  email-verification tests and 25 permission-management tests passed.
- Final: active frontend strict TypeScript, access TypeScript, targeted ESLint
  and production build passed.
- `node --test tools/moderator-routing.test.mjs tools/moderator-panel.test.mjs`:
  11 passed, 0 failed. Covers role routing/profile access, permission menu,
  Turkish MFA/profile link, empty data, safe errors, parser allowlists, admin
  role controls and read-only request/styling contracts.
- Targeted backend: 94 passed, 0 failed, 3 erasure-role subtests passed.
  Includes changed email guards, erasure ownership, permission management and
  every registered admin route's MODERATOR denial. Existing Starlette/AnyIO
  deprecation warning remains. Full backend/Playwright suites were not run.
- Local headless Chromium rendered desktop (1440x1000) and mobile (390x844).
  Every API response was mocked; third-party browser requests were blocked.
  Search/detail, subscription/payment no-data states, permission menu, MFA,
  404/429, customer/moderator routing, GET-only reads, zero page errors and
  no mobile horizontal overflow were verified.
- Screenshots and backend XML/build output are outside the repository in the
  session artifact directory (`stage5-moderator-*.png`,
  `stage5-backend-final.xml`, `stage5-build-final.txt`).
- The initial baseline build ran the existing automatic logo-refresh hook;
  its generated timestamp-only manifest change was removed. Subsequent builds
  use `COIN_LOGO_OFFLINE=1`, preserving bundled assets.

No migrations were added or applied. No live database or Stripe validation,
push or deployment was performed. Real production data/MFA configuration still
requires the unapplied stage 2-4 migrations and an appropriately configured
environment; the panel cannot replace those server prerequisites.
