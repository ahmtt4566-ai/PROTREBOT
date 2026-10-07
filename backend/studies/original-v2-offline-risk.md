# Original v2 offline risk profile

`original-fixed-cap6-lev3-v1` / `kais_original_v2_offline_risk` is a single
immutable offline experiment, not a LIVE/Demo strategy or policy option.
Original v1 closed-candle decisions, stops and targets are unchanged.

The separate profile accepts an initial stop distance at most 6 percent
(native sizing tolerance: 1e-9 percentage points), without an ATR-based
admission cap. It uses a 3 USDT risk budget, leverage 3, maximum margin
25 USDT, minimum entry margin 5 USDT, liquidation buffer 0.5 percent,
and exposure 350 USDT. All remaining native gates remain mandatory.

The production sanitizer still limits its policy stop cap to 5 percent.
No production defaults, request models, routes, or LIVE/Demo flows change.
The native minimum-margin check stays before spec construction. Tick and
quantity rounding may reject a candidate; minimum orders are never padded
by increasing risk. In particular, a 50 USDT boundary order can fail after
quantity flooring when the symbol minimum notional is 50 USDT.

The local spec and entry orchestration deliberately mirror the native
algorithms without replacing their global bindings. Existing pure helpers,
account gates, liquidation checks, Position exits, funding and finalization
are reused. Synthetic tests verify the mirror where both caps accept.
The profile is checked in prefilter, sizing and rounded local spec.

## Count-only measurement

Run only after synthetic/full-suite validation, commit, output-directory
absence and explicit sleep-mode confirmation:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
.\.venv\Scripts\python.exe -B backend\measure_original_v2_counts.py --metadata "C:\Users\ahmtt\kaistrade-data\donchian-holdout-evidence\current-metadata.json"
```

Only `load_measurement_dataset` reads measurement-view, covering the same
TRAIN and VALIDATION phases. Each phase has a fresh Engine and Dataset cache.
No TEST/holdout data, registry, network, signed request or real order is used.
The native network guard permits only Python's internal event-loop socket pair.
An audit guard also rejects forbidden research-path operations before they
occur; synthetic tests call captured guard callbacks without filesystem access.

Results go exclusively to the new external `original-v2-measurement-results`
directory; an existing directory is an error. The positive report schema
permits counts, stop/duration quantiles, source/provenance hashes and the
requested stop-rejection ratio only. Gate-required monetary accounting
stays private. The runner never calls Position.row, R division, performance
metrics, bootstrap or native Engine.replay.

`risk_stage_arrivals` counts ranked, unoccupied, history-complete,
canonical-quality-approved candidates before the profile prefilter.
The stop-rejection numerator is profile-cap rejections (prefilter, sizing
or rounded spec), stop-risk rejections, plus minimum-margin rejections.
The denominator is risk_stage_arrivals, not all-grid quality observations
or top-three entry attempts. Rejection counters may otherwise overlap.

Minimum notional/quantity rejections are combined, exactly as the native
minimum-order check, and counted separately by symbol. Spec quantity ceilings
remain native-spec rejections. Profile hash, unchanged Original parameter
hash, source commit and source-file hashes are recorded separately.
