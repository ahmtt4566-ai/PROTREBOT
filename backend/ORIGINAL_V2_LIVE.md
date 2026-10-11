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
   loss (default 5%, maximum 5%; a lower limit is allowed).
6. Enter a fresh 2FA code and `ORIGINAL V2 CANLI`, then start.

Risk sizing includes estimated fees and slippage, and cannot exceed the chosen
percentage of current wallet balance or the stricter existing per-order limits.
The Demo's margin, exposure, position, daily trade and consecutive loss caps
also apply. Actual stop fills can exceed estimates during gaps or outages;
this is not a guaranteed maximum realized loss.

The supervised session expires after at most one hour, bounded by the login
and consent authorization. A restart restores daily loss and alert records,
not entry authority. Starting again requires the existing ARM gates and fresh
2FA; the strategy cannot automatically switch to the legacy scanner.

## Daily loss and emergency stop

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
