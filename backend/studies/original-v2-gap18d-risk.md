# Final count-only gap18d exception

This is one explicitly user-authorized exception to stopping after VALIDATION
has fewer than 150 fully verified completions. It is the final Original v2 trial.
Run2 recorded 129 fully verified completions, a BTC data gap on 2022-07-31,
1,546 subsequent accounting-gate rejections, and zero August entries.
The authorization is based only on counts; no performance was viewed.
These observations are not a counterfactual proof of how many entries would
have completed without the lock.

Profile: `original-fixed-cap6-lev3-gap18d-v1`, offline only.
All risk settings and Original signals remain those of
`original-fixed-cap6-lev3-v1`; that profile and its hash are unchanged.
The new immutable profile has its own hash. Report provenance includes the
exception, run2 report SHA-256, Original parameter hash, source commit, and
hashes of the existing Donchian inventory implementation and new source.

Blackouts reuse the existing `_series_gaps` and `_funding_gaps` helpers from
[offline_facade.py](../app/strategies/offline_facade.py). There is no fixed date
list. Per symbol, exclude `[gap_start - 1555200, gap_start)` seconds for contract,
mark, and funding gaps. The 432-hour duration rounds above run2's completed
duration p99 of 408.56972222222197 hours. Funding tolerance, left-event gap start,
head/tail inventory, and native lifecycle are unchanged.
There is no added post-gap delay: Original's existing contiguous-history and
warmup checks still apply. Open positions are never force-closed by the blackout;
native UNKNOWN/funding completeness and accounting-lock behavior remain intact.

Gap counts are per exclusion attempt. A symbol excluded by multiple stream
types counts once in the total and once for each distinct type; type counts
therefore overlap. This future-gap exclusion is an offline research operation,
not a causal live signal or an option selectable from LIVE/Demo.

Run exactly once per phase, after confirming sleep is disabled:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
.\.venv\Scripts\python.exe -B backend\measure_original_gap_counts.py --phase VALIDATION --metadata "C:\Users\ahmtt\kaistrade-data\donchian-holdout-evidence\current-metadata.json"
```

Outputs are outside the repository, in
`original-v2-measurement-results-run3\VALIDATION`.
If fully verified completions are below 150, do not run TRAIN and abandon
Original v2. Otherwise run the same command once with `--phase TRAIN`,
writing `original-v2-measurement-results-run3\TRAIN`. The CLI requires a valid
same-provenance run3 VALIDATION report with at least 150 verified completions
before TRAIN. Existing phase output directories cause failure, not overwrite.
No performance fields are admitted to the strict count-only report.
