# Kais Original V2 LIVE

This integration is disabled by default. Enable the server flag
`PROTREBOT_LIVE_KAIS_ORIGINAL_V2_ENABLED=true` only after deployment verification.
No migration or live setting is changed by installing this code.

Only a canonical OWNER session can see or operate the Original V2 LIVE card
in Master Trade / Canli Islem. Customers cannot start or stop this strategy.
The existing LIVE strategies, manual orders and Demo signal policy are unchanged
except that an active Original post-fill safety latch blocks all new LIVE entries
on the supervising account.

## Signal and exits

The strategy reuses the Demo Original V2 adapter and its immutable profile:
BTC, ETH, SOL, BNB, XRP, DOGE, ADA and AVAX USDT perpetuals; 3x isolated,
at most 6% stop distance. TP1 closes 60%; the remaining position exits at TP3
or the initial stop. There is no TP2 order, trailing stop or break-even move.
Quantity is rounded down to five lot steps so the initial position can split
exactly 60/40. An incompatible partial fill pauses entries after installing
the stop; it is not silently resized or automatically closed.

As approved, no new 100-day trend filter is added. The historical reference's
18-day *future-gap inventory* is not causal LIVE data and is not represented as
a LIVE look-ahead rule. LIVE requires 259 contiguous, current closed candles in
each of 15m, 1h and 4h for all eight symbols. Missing, duplicate, stale or invalid
history stops new entries instead of inventing a signal.

## Enable

1. Sign in as OWNER and enable account 2FA.
2. Connect and verify the LIVE API with Futures permission and no withdrawals.
3. Complete the existing LIVE release checks, consent and policy acknowledgement.
4. ARM LIVE using the existing confirmation.
5. In the Original V2 card, choose risk (default 0.5%, maximum 1%) and daily
   loss (default 5%, maximum 5%; a lower limit is allowed), and a supervised
   session of 1, 4, 12 or 24 hours (default 1). Other durations are rejected.
6. Enter a fresh 2FA code and `ORIGINAL V2 CANLI`, then start.

Risk sizing includes estimated fees and slippage, and cannot exceed the chosen
percentage of current wallet balance or the stricter existing per-order limits.
The Demo's margin, exposure, position, daily trade and consecutive loss caps
also apply. Actual stop fills can exceed estimates during gaps or outages;
this is not a guaranteed maximum realized loss.

The Original-only supervised session expires at the earlier of the selected
duration, signed login expiry and existing consent expiry. Neither login nor
consent is extended. Missing or expired duration authority fails closed. The
card shows the selected duration, actual expiry and remaining time, including
when authority shortened the requested duration. Its local countdown adds no
network requests. The legacy V25 session remains one hour.

The existing background loop issues one OWNER bell warning when 30 minutes or
less remain, including while an account cooldown blocks exchange reads. The
warning latch is persisted with the session and alert; it is checked on the
next loop tick, not a separate exact-time delivery service. At expiry, new
entries stop and ARM is cleared without deleting positions, intents, daily
loss, fill locks or exchange protection. Existing protection monitoring remains
in place. An active session cannot be extended; reopening an expired/stopped
session requires all existing ARM gates and a fresh mandatory 2FA code. There
is no automatic renewal and the strategy cannot switch to the legacy scanner.
A restart restores daily loss, session metadata and alert records, not entry
authority. An explicitly opened new session resets only its warning latch,
not the daily-loss/fill safety latches.

## Daily loss and emergency stop

### Shared private account reads and rate-limit recovery

OWNER can manually open **Kalici kayit teshisi** on the Original card. It uses
`GET /api/v25/original-v2/persistence-diagnostics` with the existing authoritative
OWNER/session and execution-ownership checks; it never writes or enables entries.
The three-second read selects only revision and whether the Original JSON object
exists, not its contents. It reports row existence, local/database revisions,
safe last-write metadata, deployment commit and controlled diagnostic codes.
Database exception text and credentials are never returned. No periodic poll is
added. `DATABASE_AHEAD` needs a writer/recovery investigation, not a revision reset.

General Auto Trade and Original share the same in-process revision counter and
`v25_live:state` row. An older asynchronous write may legitimately be skipped
after a newer write; this alone does not prove the latest start write failed.
Multiple workers/overlapping deployments or stale recovery must be checked when
the database is ahead. Never force a larger revision over unknown persisted
positions, intents or daily-loss state. A missing row can already be inserted
by the existing upsert; a missing table requires separately approved schema work.

On startup, PostgreSQL JSONB text is decoded before restoring the shared LIVE
snapshot. The saved revision is authoritative: revision 374 is restored as 374,
and the next write submits 375 through the unchanged revision guard. Previously,
JSONB text was treated as a non-object and silently replaced by initial state,
including revision zero, while recovery was marked successful. This affected
both General Auto Trade and Original V2. Invalid JSON, non-object records or
invalid revisions now leave recovery blocked instead of resetting local state.
Restore is read-only; daily-loss events, plans, intents, emergency and Original
daily/fill-lock records are retained. Entry authority still requires fresh gates,
ARM and the applicable owner confirmation after restart.

If a new login is blocked by the saved session owner, the explicit
`POST /api/v25/session/recover` operation requires the same canonical OWNER,
fresh required 2FA and `LIVE OTURUMUNU GERI BAGLA` (the UI sends the Turkish
confirmation). It verifies the same saved key fingerprint and trading account,
LIVE permissions and a fresh complete account snapshot including algo orders.
Active entry authority, unresolved plans/execution, any open position/order or
state changes during verification block recovery. A guarded durable write must
succeed before the local session binding changes. Read/status routes do not
transfer ownership; other users, accounts and keys remain denied.
Recovery preserves loss history, positions/plans, intents and safety latches.
It removes entry authorization, ARM, consent and policy acknowledgement, leaves
the connection unverified and never starts automation or submits an order.
The owner must separately reconnect and complete all normal entry gates.
Exchange/proxy timeouts are separate errors; recovery does not bypass them.

LIVE status polling reads local state, not Binance. Previously each full account
refresh made six signed reads (`account`, `positionRisk`, bulk `openOrders`,
`openAlgoOrders`, `positionSide/dual`, `symbolConfig`), plus `multiAssetsMargin`
when the account response did not prove the mode. The background reconciliation
loop runs every 10 seconds; connection refreshes and strategy scans also used
that full bundle. Credential tests and trade/income history are separate reads.

The supervising application now shares successful account snapshots for the
same credential hash and authorized user/session. Empty accounts refresh no
more often than every 30 seconds. A conservative initial bundle budget of 120
weight units reserves at most 30% of the configured 2400/minute limit for these
idle refreshes; larger observed header deltas lengthen the idle interval. This
is a local budget estimate, not a claim about every endpoint's official weight
or other applications using the same proxy IP. The response's actual
`X-MBX-USED-WEIGHT-1M` remains authoritative for the shared limiter.

Open-position/active-plan reconciliation retains fresh reads every 10 seconds
after the previous cycle completes and never reuses the browser cache. Its
signed requests have queue priority over ordinary reads; priority does not
bypass a Binance cooldown. Entry checks, leverage/margin verification,
post-order reads and emergency verification remain fresh. Mutations invalidate
the cache; failures cannot be replaced by an old successful cached snapshot.
Empty-account scanner reads and concurrent browser refreshes reuse the cache.
The cache and queue are process-local; additional workers or other services
sharing the same egress IP need a distributed coordinator or separate budgets.

On a signed GET rate limit, new entries are blocked immediately. A short wait
retains the original ARM expiry and Original configuration, but does not
represent the account as currently verified. The UI shows a Turkish waiting
reason. Recovery requires a complete fresh account read. The grace is 120
seconds for an empty account and 30 seconds when positions/plans need protection
monitoring. Exceeding it cancels ARM and pauses entries with a Turkish reason;
later recovery never rearms automatically. Unknown execution, missing/unproven
protection, expired/revoked session authority and rejected mutations keep their
existing immediate safety behavior. Exchange-installed stops are not removed.

Every valid `Retry-After` is honored in full, even beyond the grace period.
The loop checks local timeout/authority state without sending more Binance
reads during cooldown. At 80% of the configured minute weight limit, ordinary
calls wait until the next minute window, or longer if Binance requires it.
The default signed-request concurrency is now two per host.

`LIVE_ACCOUNT_SNAPSHOT` logs caller source, interval, elapsed time, cache hits,
upstream refresh count and budget weight. `BINANCE_ACCOUNT_METRICS` logs
per-host endpoint/status counts, source counts and observed minute weight,
without keys, signatures or query strings. A local 50-tab concurrency test
requires only one complete snapshot bundle. These are local test results;
production counts require collecting these logs after an approved deployment.

UTC opening wallet is reconstructed from the verified current wallet and the
day's Binance income history. Transfers do not count as trading losses.
Realized net income plus negative unrealized PnL is compared with the fixed
opening-wallet percentage. Once breached, the UTC day is latched closed even
if balance recovers, funds are deposited or the session restarts. Unverifiable
income history blocks entry. The record is bound to OWNER and API fingerprint;
switching accounts cannot reset it through this card.

Use **Acil durdur / Yeni girisleri kapat** to disable entries immediately.
Disconnects, rejected orders and missing stop/TP protection also pause entries
and create an OWNER bell notification plus LIVE event. Existing positions
are not automatically closed. Protection reconciliation continues when valid
credentials are available; TP1 is never recreated as 60% of the remainder.
Inspect Binance directly if protection cannot be confirmed.

The general LIVE emergency-close controls remain separate, explicit actions.

## Final Master Trade audit

Non-Original LIVE Auto Trade now requires a fresh TOTP when account 2FA is
enabled; accounts without 2FA retain their existing flow. Original V2 always
requires 2FA. The verified internal start path does not consume its TOTP twice.
ARM, consent, policy and ownership gates remain mandatory.

Master Trade's connection tab supports confirmed deactivation and deletion.
Active keys cannot be deleted, and verified open positions block deletion.
Changing or deactivating the supervising key pauses Original entries.
Key fields cannot be revealed; arbitrary upstream error messages are replaced
with controlled Turkish messages. LIVE safety events are projected into the
OWNER's notification bell with durable read status.

Connection failures are shown immediately beside the Master Trade credential
form, before background status refresh completes. Verification requests have a
longer bounded client timeout to cover the four default 15-second read-only
checks. Key/secret fields remain masked and known credentials are redacted from
displayed error text. Proxy, network, invalid-response and server failures have
Turkish explanations. Signed requests never fall back to a different egress
transport. Binance code -2015 is ambiguous (key, IP or permissions); an IP hint
is shown only when Binance explicitly reports a valid public request IP.
No static IP is invented, and the code does not change live proxy settings.
Clock synchronization measures only the network round trip, excluding
rate-limit queue time. Credential checks and LIVE requests generate each
timestamp and signature after acquiring their rate-limit slot, so waiting
does not consume the unchanged 5-second receive window. No automatic order
retry or direct-transport fallback is introduced.

Public LIVE market data now uses only the direct, environment-proxy-disabled
client. Signed permissions, account reads and order traffic retain the
configured fixed-IP proxy; there is no signed direct fallback. Public clock
reads are direct too. No proxy environment value is modified.

A process-wide singleflight cache shares exchange metadata (10 minutes), bulk
tickers and candles (15 seconds) across users, charts, scanners and LIVE.
Candle requests up to 500 rows share one upstream series and receive exactly
their requested latest row count. Candle-boundary expiry prevents reusing the
previous bar at an Original decision boundary. Expired/failed candle reads do
not become stale successful trading inputs.

Original's fixed eight symbols have priority over queued general scans. A
read-only 15-second warmer runs even without users or enabled automation and
maintains the unchanged 15m/1h/4h frames. It cannot start automation or send
orders. Public reads are capped at two concurrent upstream calls and do not
retry the same failed host in a loop.

HTTP 418/429 applies the full valid `Retry-After`, including values above
60 seconds, across all public paths and symbols. During that interval no
upstream public request is sent; responses preserve the remaining wait.
The browser also shares a market-only cooldown without delaying account
reads or changing execution authority. Public request/cache/status counts are
logged at most once per minute, including public cooldown periods, as
`PUBLIC_MARKET_METRICS transport=direct`. The log includes upstream GET counts
by endpoint and candle interval, cache/shared-waiter counts, Binance's observed
one-minute request weight and the remaining cooldown. Legacy Spot depth and
price reads also use the direct public cache, retaining their Spot host.
Account endpoint/status counts are logged per host as `BINANCE_ACCOUNT_METRICS`;
queries, API keys and signatures are excluded. `BINANCE_TRANSPORT` records
whether accounts/orders use QuotaGuard without logging its URL.

An offline one-minute load test with 50 users polling all eight symbols and
three Original frames every five seconds makes 14,400 internal candle reads
but only 96 upstream reads. This is not a measurement of Render production,
does not include unrelated feeds/account traffic, and assumes one backend
process and a healthy upstream. The cache and cooldown are process-local;
multiple workers/replicas require a shared coordinator before scaling.

Master Trade does not scan the hidden analysis universe on the LIVE tab.
Successful account polls replace snapshots without temporarily clearing
Original's card, preserving typed inputs and scroll. Transient failed polls
keep the last displayed consent, policy and Original card, but mark the status
unverified and disable new orders/automation starts. The header also shows
unverified/locked rather than presenting old ARM as current authority. A 401,
403 or missing-account response clears the previous account's status. A fresh
successful poll restores only the actual server state, never synthesizes ARM.
Market failures never reload the document or reset the live form.

The server exposes `live_lock_reason` with a code, Turkish message and timestamp
for safety cancellation, ARM expiry, session failure, consent renewal/revocation
and restart. Renewing 24-hour consent still resets ARM. Entry authority still
cannot survive a process restart. Safety locks, ownership, MFA, risk limits,
fresh-data gates and order execution behavior are not relaxed.

Recovery checks Original TP prices, quantities, close/reduce-only flags,
working type and order identity, not just their IDs. A changed target pauses
entries without closing positions. An already-installed TP1 is not resized or
rejected on repair merely because the current market notional has fallen.
The general emergency control explicitly warns that it cancels bot protection
and sends reduce-only closes; Original's entry-only stop does neither.
It is now directly visible beside Master Trade's Auto Trade controls, rather
than only being available in the separate legacy LIVE panel.

### Post-fill safety latch (strategy rules unchanged)

After the entry remainder settles, actual entry price and filled quantity are
checked against the immutable initial stop, original risk budget (including
estimated fees/slippage) and 3x leverage. Stop distance above 6%, risk above its
budget, or margin below 5 USDT disables all new LIVE entries and creates an OWNER
notification. Exact thresholds are allowed. Missing or invalid evidence,
including old recovered intents without cost evidence, fails closed. A plan
rebuilt solely from an accepted intent has requested quantity, not a durable
settled-fill audit; it also latches rather than treating that quantity as proof.

The open position is never closed or resized by this check. The initial stop,
60% TP1 and TP3 policy remain unchanged; protective orders and reduce-only
actions remain allowed. The check is persisted once for the settled entry, not
repeated against the smaller remainder after TP1. Recovery preserves the latch
without restoring entry authority. Storage failures pause entries and retry the
audit; durable TP-attempt recording remains mandatory before TP submission.

Only the supervising canonical OWNER can clear the latch:

1. Inspect the fill metrics, open position and protection directly.
2. Enter `DOLUM RİSKİNİ İNCELEDİM` and a fresh 2FA code in the Original card.
3. Select the explicit fill-lock approval button.
4. Re-authorize LIVE and ARM separately before starting with fresh 2FA; existing
   tracked-position and all other safety gates still apply.

Approval only acknowledges the current lock; it does not repair the position,
start automation, ARM, or clear daily-loss restrictions. A new violation during
verification invalidates the old approval. Persistence failure keeps the lock
active. Actual stop execution through a gap can still exceed the estimate;
the risk percentage is not a guaranteed maximum realized loss.

Final Decision remains a read-only analysis result and never submits an order.
Legacy maximum-loss and daily-loss controls are USDT amounts, not percentages;
the dedicated Original risk and daily-loss controls are percentages.

### Read-only history compatibility

`GET /api/v25/history` reads the restored LIVE snapshot and Binance read-only
history; it does not query a separate history table or require a new migration.
The snapshot's order identity fields accept old missing/null values and single
identities as well as current lists. Existing managed-order filtering is retained.
Malformed identities or exchange rows are isolated, logged without raw record
values, and reported in the response's `errors` list while valid rows remain
available. Missing optional exchange fields remain null, not invented numbers.
Connection setup errors retain their explicit HTTP status instead of becoming
an uncaught 500. The history endpoint never mutates plans or sends orders.

### Former 45-failure backend baseline

The failures were stale test contracts, not 45 demonstrated exchange defects:
41 cases mocked the removed `subscription_for_user` instead of the current
request-scoped entitlement resolver, so they never exercised ownership checks.
The updated fixtures include request paths, paid-plan status and account MFA
state; denial assertions and controller immutability checks are unchanged.
The remaining four contracts now cover the Binance OWNER route inventory,
the full ordered permission/audit migration extensions and the current Vercel
API rewrite before the SPA fallback. No production migration, permission
allowlist, subscription guard or deployment setting was changed for these
baseline repairs.
