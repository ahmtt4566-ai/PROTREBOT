# Stage 6b: preregistered archival-gap amendment

## Previous failed attempt (preserved)

Stage 6 stopped at source-quality validation, before native signals, entries,
exits or strategy outcomes. Each of eight symbols lacked the archival
2023-11-10 03:45-04:00 UTC 15m mark-price candle. Registered November ZIP
checksums matched and raw CSVs contained 2,879 instead of 2,880 rows, with both
neighbour timestamps present. The previous external and shared Git receipts
remain `FAILED`, `rerun_allowed=false`; nothing is removed or reset.

## Locked rule, before outcomes

[stage6b-plan.json](stage6b-plan.json) has canonical SHA256
`fe0353656e86363e8d85cf68641b96c1200b9439a3f7d6cb51881b8be2a9dd00`.
It inherits **all** settings of [stage6-plan.json](stage6-plan.json), canonical
SHA256 `853dfa4714e8f9bf348350c3616666bafa172da660b383c15f67b86dd63fcc66`:
three B candidates, strict mean R >0 / monetary PF >1.2 / complete trades >=60,
four slip/spread cost pairs, STOP_FIRST/TP_FIRST, 20,000 bootstrap draws,
seed2026 and Bonferroni3. No strategy or parameter is retuned.

Only the already-known exact mark-price gap is permitted. No mark candle is
inserted, interpolated or synthesized; raw mark rows, times and closed history
remain unchanged. A read-only adapter resolves protective evaluation of that
one absent bar directly to the existing contract candle.

- No native signal/new entry at 03:45 UTC, or at 04:00 UTC from that gap's
  decision-closing bar. The worker writes explicit data-gap WAIT records
  without invoking the native signal function at those boundaries.
- Existing Stop/TP evaluates the real contract open/high/low/close, with
  unchanged native priority, quantity, fees and slippage. This is protection,
  not a new discretionary exit decision.
- BE updates are deferred at gap open and at the next boundary whose previous
  closed mark is missing. They resume from the next actual closed mark bar.
  No trailing, time stop or new exit strategy is introduced.
- Snapshot valuation, and a real historical funding event if one occurred
  in that bar, use contract open; no funding rate is invented.
- Every trade exposed during the gap is `GAP_AFFECTED`, even if protection
  does not trigger. Its gap timestamp, protective exits and deferred BE
  updates are exported.
- All-rows and excluding-all-`GAP_AFFECTED` reports derive from the **same**
  outcome rows, with unchanged bootstrap settings. Exclusion does not refill
  capacity, rescan entries or rerun TEST.

## Commands and one-shot boundary

Use fixed external sibling directories; all commands block external network:

```powershell
$stage5 = 'C:\research\protrebot\stage5'
$output = 'C:\research\protrebot\stage6b'
.\.venv\Scripts\python.exe backend\holdout_gap_cli.py register --stage5-root $stage5 --output $output
.\.venv\Scripts\python.exe backend\holdout_gap_cli.py dry-verify --stage5-root $stage5 --output $output
# Only after successful dry verification and synthetic regression gates:
.\.venv\Scripts\python.exe backend\holdout_gap_cli.py run --stage5-root $stage5 --output $output --workers 4
```

Dry verification validates checksums, raw contract/mark coverage and native
warmup windows, without signals/replay/bootstrap and without claiming TEST.
Run verifies raw data again **before** exclusively creating new external and
shared Git receipts. Failed preflight does not consume the right; any attempt
after claim does. There is no reset, resume, relocation or rerun override.
Old Stage 6 receipts are read-only provenance, never reused as Stage 6b claims.

## Files and validation

Only new Stage 6b files are added:
[registration/quality](../app/holdout_gap_study.py),
[gap adapter/replay](../app/holdout_gap_model.py),
[CLI](../holdout_gap_cli.py), [tests](../tests/test_holdout_gap_study.py),
the new plan and this report. LIVE and Stage 1-6 sources/tests/README remain
unchanged and their protected digests are verified.

Synthetic tests cover native parity of unaffected B trades, no raw mark
interpolation, exact-gap-only coverage, embargoed/closed signal calls,
Windows spawn caching, LONG/SHORT protective priority, hand-calculated
TP1/Stop fees and net R, immutable initial risk, BE deferral, actual funding,
gap-exclusion subsets, signal-free dry checks, preclaim failures, irreversible
failure/success receipts and all 24 registered nonempty cells without LIVE
order calls.

Before the authorized outcome run, real archive `dry-verify` passed with
zero signals and zero replays. Final related offline gates passed **842 tests
and 247 subtests**: 21 new Stage 6b tests, one optional PostgreSQL skip and
one previously proven stale source-text baseline deselection. Pylance
reported no diagnostics for the three new runtime modules; the test file
has only two unused variadic fixture-argument hints.

## Measured one-shot TEST outcome

The single Stage 6b claim started 2026-10-06T14:28:25 UTC and completed
successfully. Both new receipts are `COMPLETED`, `rerun_allowed=false`.
Result SHA256:
`84b9588e9b0cb0cee8539fbc018643d281c473bb35bcca4dd0dbf60c9a8b3730`.
The earlier Stage 6 failure receipt retains its exact original SHA256:
`8e0c5b5180102190cdf5bbcaf1c9b4d29b5987c5203c4cbe2ef6e40f398c5a33`.
No rerun, source change, threshold adjustment or Stage 7 followed the results.

Eight complete caches contain 210,432 chronological decision slots:
210,416 native decisions and 16 explicit gap WAIT placeholders. All four
native-A controls matched their frozen A reconstructions. All 24 registered
candidate/scenario cells completed.

### Primary: STOP_FIRST, slip3 bp / spread2 bp

All trades have verified complete net-R and funding accounting. Every candidate
fails **all three** point criteria: mean R is negative, monetary PF is below
1.2, and complete trade count is below 60. Machine labels are `BA\u015eARISIZ`
(Turkish spelling); these are measured failures, unlike the earlier data error.
The minimum sample criterion must not be weakened because B has 59 trades.

| Candidate | Trades | Mean net R | USDT PF | Win rate | Max closed DD USDT | Net PnL USDT | Verdict |
|---|---:|---:|---:|---:|---:|---:|---|
| B | 59 | -0.231986 | 0.628010 | 47.458% | 47.814862 | -39.452507 | FAILED |
| B + 4h ADX >=20 | 33 | -0.338319 | 0.511763 | 42.424% | 37.287590 | -31.734570 | FAILED |
| B, SHORT removed | 42 | -0.225473 | 0.646428 | 47.619% | 33.271963 | -26.473142 | FAILED |

### Gap exclusion and clean-only sensitivity

`GAP_AFFECTED=0` for all native A controls and all B candidates/scenarios:
no selected position was open in the absent bar. Excluding all affected rows
therefore retains 59 / 33 / 42 trades and reproduces the exact all-rows point
statistics and bootstrap intervals, not merely rounded equality.
Raw mark coverage still has the declared gap; no candle was inserted.
The gap's two decision boundaries remain embargoed even though no position
was exposed. This is not a comparison against an imaginary gap-free baseline.

TEST is 2023-10-01 through 2024-07-01 exclusive. Previously seen inputs/replay
start 2025-02-01; overlap is zero. Full TEST and clean-only reports are exactly
equal; there is no partly-seen subgroup to invent.

### Cost and intrabar sensitivity

Both STOP_FIRST and TP_FIRST were executed within the single bundle. Their
stored candidate outcomes, point metrics and intervals match exactly for each
cost pair; ambiguous bars are zero. Spread2 and spread5 retain identical entry
IDs and outcomes for a fixed slippage value. Higher slippage retains all 59
native B entry IDs (0 added, 0 removed), but changes all 59 financial outcomes.
Filters retain the same 33 / 42 entry subsets. Spread is the unchanged native
gate input, not an additional fabricated cash charge.

| Slip bp | Spread bp | Candidate | Trades | Mean net R | PF | Win rate | Max DD USDT | Net PnL USDT |
|---:|---:|---|---:|---:|---:|---:|---:|---:|
| 3 | 2 or 5 | B | 59 | -0.231986 | 0.628010 | 47.458% | 47.814862 | -39.452507 |
| 3 | 2 or 5 | B + ADX4h20 | 33 | -0.338319 | 0.511763 | 42.424% | 37.287590 | -31.734570 |
| 3 | 2 or 5 | B, SHORT removed | 42 | -0.225473 | 0.646428 | 47.619% | 33.271963 | -26.473142 |
| 6 | 2 or 5 | B | 59 | -0.287377 | 0.562017 | 47.458% | 56.691884 | -48.983742 |
| 6 | 2 or 5 | B + ADX4h20 | 33 | -0.395091 | 0.457385 | 42.424% | 42.305703 | -37.198745 |
| 6 | 2 or 5 | B, SHORT removed | 42 | -0.280781 | 0.579058 | 47.619% | 38.912261 | -33.223488 |

All 24 cells fail the registered point criteria. The compact table groups
only empirically identical spread/ordering cases; all eight scenario files
and both orderings are retained outside Git.

### Primary bootstrap intervals

20,000 valid mean-R and PF draws per candidate/method; zero empty or undefined
PF resamples. TRADE clusters are 59 / 33 / 42; active entry-day clusters are
57 / 32 / 41 out of the same 274-day calendar. Nominal95 is unadjusted;
family95 applies Bonferroni3. All expectancy intervals include zero.
No confidence interval is an extra point success criterion.

| Candidate | Method | Nominal95 mean R | Nominal95 USDT PF | Family95 mean R | Family95 USDT PF |
|---|---|---|---|---|---|
| B | TRADE | [-0.495260, 0.041700] | [0.345827, 1.093264] | [-0.549134, 0.107937] | [0.298150, 1.244069] |
| B | DAY | [-0.502987, 0.054786] | [0.339477, 1.123682] | [-0.561786, 0.119362] | [0.289422, 1.279655] |
| B + ADX4h20 | TRADE | [-0.680075, 0.028015] | [0.213817, 1.077067] | [-0.748773, 0.106142] | [0.172342, 1.265852] |
| B + ADX4h20 | DAY | [-0.688184, 0.043686] | [0.207745, 1.113214] | [-0.764850, 0.140167] | [0.161533, 1.352934] |
| B, SHORT removed | TRADE | [-0.544821, 0.107791] | [0.311475, 1.258135] | [-0.609428, 0.182974] | [0.263060, 1.465815] |
| B, SHORT removed | DAY | [-0.551445, 0.113198] | [0.308869, 1.283632] | [-0.614013, 0.187156] | [0.257641, 1.483243] |

### Primary direction split

| Candidate | Direction | Trades | Mean net R | PF | Net PnL USDT |
|---|---|---:|---:|---:|---:|
| B | LONG | 42 | -0.225473 | 0.646428 | -26.473142 |
| B | SHORT | 17 | -0.248076 | 0.583789 | -12.979365 |
| B + ADX4h20 | LONG | 24 | -0.349836 | 0.511617 | -23.349835 |
| B + ADX4h20 | SHORT | 9 | -0.307607 | 0.512168 | -8.384734 |
| B, SHORT removed | LONG | 42 | -0.225473 | 0.646428 | -26.473142 |
| B, SHORT removed | SHORT | 0 | -- | -- | -- |

### Primary monthly distribution

UTC **close-month** basis. Cells show trade count / mean net R / net USDT.
An empty month has unknown metrics (`--`), not invented zero returns.
Positive mean R and positive monetary PnL are not interchangeable when
initial trade risk varies, as B October illustrates.

| Month | B | B + ADX4h20 | B, SHORT removed |
|---|---|---|---|
| 2023-10 | 8 / 0.002222 / -0.033699 | 6 / 0.122467 / 2.162915 | 5 / 0.369503 / 5.439308 |
| 2023-11 | 10 / -0.573554 / -16.449641 | 5 / -1.185732 / -17.226324 | 9 / -0.507441 / -12.978987 |
| 2023-12 | 7 / -0.783753 / -15.272616 | 6 / -0.721055 / -11.794184 | 5 / -0.637492 / -8.567876 |
| 2024-01 | 5 / 0.177562 / 2.631161 | 3 / -0.072622 / -0.640865 | 4 / 0.084309 / 1.014457 |
| 2024-02 | 8 / -0.053177 / -1.928208 | 6 / -0.154372 / -2.853516 | 6 / 0.317333 / 5.031187 |
| 2024-03 | 2 / 0.228906 / 1.332729 | 0 / -- / -- | 2 / 0.228906 / 1.332729 |
| 2024-04 | 3 / 0.751472 / 6.607265 | 1 / 1.615129 / 4.702448 | 1 / -1.112610 / -3.320919 |
| 2024-05 | 8 / -0.145307 / -3.260657 | 2 / -0.346166 / -2.074920 | 7 / -0.226730 / -4.325857 |
| 2024-06 | 8 / -0.561913 / -13.078843 | 4 / -0.355757 / -4.010122 | 3 / -1.187430 / -10.097184 |

November, December and June account for large losses in this particular
sample; small monthly/directional subsets do not prove a general regime
effect or justify changing filters after TEST.

### Persisted evidence

Fixed external `stage6b` contains the immutable plan lock, signal-free
`dry-verify.json`, original raw `test-data-quality.json`, new `test-once.json`,
eight complete native caches, four native entry/control cohorts, eight
scenario JSON files, eight B trade CSVs, `comparison.csv` and
`holdout-results.json`. Full direction/month/CI data for every cost, ordering
and gap-exclusion scope is in those stored results.

Post-run validation read stored outcomes/cache headers only: both new receipts
and result digest match; prior failure and protected Stage 1-6 source digests
remain unchanged; corrected intervals contain their nominal counterparts;
all timestamp slots and 16 native-skipped gap records are verified.
No trading simulation or bootstrap was repeated for this audit.

## Limits

Reverse-time historical holdout, not forward walk-forward. Current Stage 5
metadata/universe is held fixed. Contract OHLC is an explicit approximation
of missing mark-price protective triggers, not evidence of actual mark prices.
Per-cost native A entry cohorts, frozen B exits and filters that remove only
remain unchanged. Excluding gap-affected outcomes cannot remove their earlier
portfolio/gate effects. Bonferroni3 covers the primary candidate family, not
all 24 sensitivity cells or the extra gap-exclusion subsets. No Stage 7.
