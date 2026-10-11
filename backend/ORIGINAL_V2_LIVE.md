# Kais Original V2 LIVE

This integration is disabled by default. Enable the server flag
`PROTREBOT_LIVE_KAIS_ORIGINAL_V2_ENABLED=true` only after deployment verification.
No migration or live setting is changed by installing this code.

Only a canonical OWNER session can see or operate the Original V2 LIVE card
in Master Trade / Canli Islem. Customers cannot start or stop this strategy.
The existing LIVE strategies, manual orders and Demo signal policy are unchanged.

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

Recovery checks Original TP prices, quantities, close/reduce-only flags,
working type and order identity, not just their IDs. A changed target pauses
entries without closing positions. An already-installed TP1 is not resized or
rejected on repair merely because the current market notional has fallen.
The general emergency control explicitly warns that it cancels bot protection
and sends reduce-only closes; Original's entry-only stop does neither.
It is now directly visible beside Master Trade's Auto Trade controls, rather
than only being available in the separate legacy LIVE panel.

### Remaining execution limitations (strategy rules unchanged)

Offline characterization confirms two post-fill limits are not enforced:

- A partially filled order originally sized at 5 USDT margin can settle at
  4 USDT while its exact 60% TP1 still satisfies Binance minimums.
- A market fill at 51,000 against a planned entry of 50,000 and fixed stop at
  47,000 has a 7.84% stop distance. Protection preserves that initial stop;
  actual fill price is not revalidated against the pre-entry 6% ceiling.

These are not changes to the canonical signals or exit policy. Fixing the
post-fill response requires an explicit decision because an already-open
position must not be automatically closed, resized or have its stop moved.
Do not enable Original LIVE until that response has been agreed and verified.
The estimated risk percentage likewise is not a guarantee for an actual fill
or a stop executed through a gap.

Final Decision remains a read-only analysis result and never submits an order.
Legacy maximum-loss and daily-loss controls are USDT amounts, not percentages;
the dedicated Original risk and daily-loss controls are percentages.

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
