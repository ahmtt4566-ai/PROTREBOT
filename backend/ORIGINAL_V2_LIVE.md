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
