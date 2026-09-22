# Threshold Map: LIVE Auto Trade and Demo Scanner

Status: read-only analysis plus approved traffic-reduction config adjustment, 2026-09-22

This document maps the current thresholds without changing strategy, risk sizing, or execution behavior.

## 1. LIVE scan cadence

| Item | Current value | Location | Effect |
| --- | ---: | --- | --- |
| `scan_seconds` default | `120` seconds | `backend/app/execution_core.py:48` | Minimum delay between V25 automatic market scans. |
| `scan_seconds` validation | `30..300` seconds | `backend/app/execution_core.py:111`; `backend/app/v25_execution.py:189` | Values outside the range are clamped/rejected by policy validation. |
| execution loop interval | `10` seconds | `backend/app/v25_execution.py:88` | Reconciliation loop cadence; it does not force a market scan every loop. |
| scan throttle | policy-driven | `backend/app/v25_execution.py:1762` | A scan is skipped as `scan_throttled` until `scan_seconds` has elapsed. |

Increasing `scan_seconds` reduces the frequency of the existing scan cycle. It does not change signal, risk, position sizing, or exit logic.

## 2. LIVE candidate volume

| Item | Current value | Location | Effect |
| --- | ---: | --- | --- |
| market preselection limit | `100` | `backend/app/v25_execution.py:89` | At most 100 liquid symbols enter the ranking stage. |
| deep-analysis limit | `50` | `backend/app/v25_execution.py:90` | At most 50 ranked symbols are passed to the 15m/1h/4h analysis loop. |
| limit defaults | `rank_market_tickers()` arguments | `backend/app/v25_execution.py:653-654` | Both limits are read from the constants above. |
| LIVE call site | no explicit limit override | `backend/app/v25_execution.py:1778` | `scan_market_candidates()` uses the default 100/100 values. |

The market is first filtered to `TRADING` USDT perpetual symbols, excluding occupied symbols, blocked base assets, leveraged-token names, symbols below `1,000,000` USDT 24h quote volume, and symbols below `0.25%` absolute 24h movement. The remaining symbols are volume-ranked, capped at 100, scored, then capped again at `min(market_limit, candidate_limit)`.

The opportunity score is:

```text
abs(24h price change %) * 0.35
+ min(24h quote volume / 10,000,000, 100) * 0.65
```

The current single-line reduction point for deep analysis is `DEEP_ANALYSIS_LIMIT = 50`.

Note: the signal-quality impact of reducing this limit from 100 to 50 has not been tested. No backtest, OOS run, sweep, or production mutation was performed as part of this change.

## 3. LIVE canonical decision thresholds

| Threshold | Current value | Location | Pipeline |
| --- | ---: | --- | --- |
| canonical confidence | `78` | `backend/app/main.py:263` | Historical/canonical quality gate. |
| canonical breakout quality | `50` | `backend/app/main.py:263` | Historical/canonical quality gate. |
| canonical trap score | `<= 35` | `backend/app/main.py:335` | Historical/canonical quality gate. |
| higher-timeframe confirmation | 1h and 4h direction must match 15m | `backend/app/main.py:164-178` | Multi-timeframe permission gate. |
| SHORT alignment filter | alignment must be `< 80` | `backend/app/main.py:90`, `backend/app/main.py:220-225` | Additional SHORT-only filter. |

V25 calls `canonical_historical_decision()` with 15m, 1h, and 4h candles in `backend/app/v25_execution.py:1563-1583`. A candidate must produce `BUY` or `SELL` and `entry_eligible=True` before it can enter the top-candidate list.

The canonical confidence threshold is not the final LIVE execution threshold. It is the first analytical quality gate.

## 4. LIVE execution gate threshold

| Threshold | Current value | Location | Effect |
| --- | ---: | --- | --- |
| V25 `min_confidence` | `86` by default | `backend/app/execution_core.py:45`, `backend/app/execution_core.py:108` | Final account/risk entry gate in `evaluate_entry_gates()`. |
| V25 `max_trap_score` | `35` by default | `backend/app/execution_core.py:46`, `backend/app/execution_core.py:109` | Final trap gate. |
| maximum spread | `8` bps by default | `backend/app/execution_core.py:47`, `backend/app/execution_core.py:110` | Final market microstructure gate. |

The final V25 entry gate also checks direction permissions, One-way mode, position count, duplicate symbols/orders, active plans, total exposure, daily trade count, daily loss, open loss, PnL verification, and consecutive losses. It is implemented in `backend/app/execution_core.py:197-255`.

Therefore, a signal can pass the canonical `78` confidence gate and still be rejected by the final LIVE `86` confidence gate.

## 5. Demo `analysis_score >= 75`

| Threshold | Current value | Location | Pipeline |
| --- | ---: | --- | --- |
| Demo analysis score | `75` | `backend/app/v21_demo.py:967-978` | V21 Demo candidate tradeability gate. |

This threshold belongs to the V21 Demo automation path. It is not used by the V25 LIVE `automatic_cycle()` path. The V25 LIVE path uses canonical confidence/breakout/trap checks plus the V25 execution gates described above.

## 6. `allowed_symbols` connection finding

`rank_market_tickers()` supports an `allowed_symbols` argument at `backend/app/v25_execution.py:657` and applies it in its filter at `backend/app/v25_execution.py:672`.

However, `scan_market_candidates()` accepts `allowed_symbols` at `backend/app/v25_execution.py:701` but calls:

```python
candidates = rank_market_tickers(exchange_info, tickers, excluded_symbols=occupied)
```

The policy list is not passed there. The V25 automatic cycle also calls `scan_market_candidates(client, snapshot)` without the policy list at `backend/app/v25_execution.py:1778`.

This appears to be a wiring gap rather than an intentional independent design, because:

- the function signature exposes `allowed_symbols`,
- the ranking helper implements the filter, and
- the policy stores and sanitizes `allowed_symbols`.

Activating this filter would reduce the LIVE scan universe, but it changes which symbols can be considered. It should therefore be reviewed and approved separately before patching.

## Conclusion

The three named thresholds are not one shared threshold system:

1. `78/50` are canonical analytical quality thresholds for LIVE decision formation.
2. `86` is the final V25 LIVE confidence gate, after canonical analysis.
3. `75` belongs to the separate V21 Demo scanner and is not a V25 LIVE threshold.

The difference is currently an architectural separation between Demo and LIVE plus a two-stage LIVE quality/execution gate. Whether the `78` versus `86` split is intentional policy or needs consolidation is not established by code alone and should not be changed as part of a traffic-reduction patch.
