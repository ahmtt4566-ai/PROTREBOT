"""Joint selection contrasts on frozen cohorts, not paired exit deltas."""

from __future__ import annotations

import random
import math
from collections import Counter
from statistics import fmean

from .backtest_baseline import Config, metrics, percentile
from .backtest_diagnostics import exit_group, monthly_distribution, utc


def combination_comparisons(baseline: list[dict], be: list[dict], config: Config, plan: dict) -> dict:
    from dataclasses import replace
    from .backtest_ablation import paired_difference

    if {row["signal_id"] for row in baseline} != {row["signal_id"] for row in be}:
        raise ValueError("A/B controls must have the same frozen entry cohort")
    for row in [*baseline, *be]:
        complete(row)
    settings = plan["bootstrap"]
    paired_config = replace(config, bootstrap_samples=settings["samples"], seed=settings["seed"])
    long_baseline = [row for row in baseline if row["direction"] == "LONG"]
    long_be = [row for row in be if row["direction"] == "LONG"]
    return {
        "B_vs_A": paired_difference(be, baseline, paired_config),
        "B_NO_SHORT_vs_A_LONG": paired_difference(long_be, long_baseline, paired_config),
        "removed_short_count": len(be) - len(long_be),
        "definition": "EXIT_DELTA_ON_IDENTICAL_ENTRIES; removed SHORTs are not fictitious paired outcomes",
        "selection_effect": "B_NO_SHORT vs all B is separately reported by the filter selection contrast",
        "ci_scope": "DESCRIPTIVE_PREREGISTERED_A_B_CONTROL; nominal95, not the 11-filter selection family",
        "parameters_changed_after_results": False,
    }


def complete(row: dict) -> bool:
    known = row["status"] == "CLOSED" and row["net_r"] is not None and row["net_pnl"] is not None
    if known and not all(math.isfinite(row[key]) for key in ("net_r", "net_pnl")):
        raise ValueError("Non-finite research accounting")
    return known


def summary(rows: list[dict], config: Config, minimum: int) -> dict:
    result = metrics(rows, config.initial_equity)
    result["sample_status"] = "YETERSIZ_ORNEKLEM" if result["r_eligible_count"] < minimum else "SUFFICIENT_DESCRIPTIVE_SAMPLE"
    result["interpretation_allowed"] = result["r_eligible_count"] >= minimum
    result["long_short"] = {direction: metrics([row for row in rows if row["direction"] == direction],
                                              config.initial_equity) for direction in ("LONG", "SHORT")}
    result["monthly"] = monthly_distribution(rows, config)
    result["exit_groups"] = dict(Counter(exit_group(row) for row in rows))
    return result


def joint_contrasts(rows: list[dict], masks: dict[str, list[bool]], config: Config,
                   *, samples: int, family_size: int, alpha: float = 0.05,
                   base_mask: list[bool] | None = None,
                   target_values: dict[str, list[float | None]] | None = None) -> dict:
    """Resample common observations once; selection and reference stay correlated."""
    if samples < 1 or family_size < len(masks) or not 0 < alpha < 1:
        raise ValueError("Invalid preregistered bootstrap parameters")
    if any(len(mask) != len(rows) for mask in masks.values()):
        raise ValueError("Mask length differs from cohort")
    if base_mask is None:
        base_mask = [True] * len(rows)
    if len(base_mask) != len(rows):
        raise ValueError("Reference mask length differs from cohort")
    if target_values is not None and (set(target_values) != set(masks)
                                      or any(len(values) != len(rows) for values in target_values.values())):
        raise ValueError("Target values differ from cohort")
    names = list(masks)
    eligible = [(row, index) for index, row in enumerate(rows) if complete(row)]
    units: dict[str, list[list[tuple[float, bool, list[float | None]]]]] = {"TRADE": [], "DAY": []}
    days = {day: [] for day in range(config.start // 86400, (config.end - 1) // 86400 + 1)}
    for row, index in eligible:
        observation = (row["net_r"], base_mask[index],
                       [(target_values[name][index] if target_values is not None else row["net_r"])
                        if masks[name][index] else None for name in names])
        units["TRADE"].append([observation])
        day = int(utc(row["opened_at"]).timestamp()) // 86400
        if day not in days:
            raise ValueError("Bootstrap entry outside development period")
        days[day].append(observation)
    units["DAY"] = list(days.values())
    result = {name: {} for name in names}
    for method, blocks in units.items():
        active = sum(bool(block) for block in blocks)
        # Aggregate all memberships before drawing to avoid rescanning trades.
        aggregates = []
        for block in blocks:
            counts, totals = [0] * (len(names) + 1), [0.0] * (len(names) + 1)
            for value, base, selected in block:
                for group, observed in enumerate([value if base else None, *selected]):
                    if observed is not None:
                        if not math.isfinite(observed):
                            raise ValueError("Non-finite contrast observation")
                        counts[group] += 1
                        totals[group] += observed
            aggregates.append((counts, totals))
        group_active = [sum(counts[group] > 0 for counts, _ in aggregates) for group in range(len(names) + 1)]
        draws = {name: [] for name in names}
        if active >= (2 if method == "DAY" else 1):
            rng = random.Random(config.seed)
            for _ in range(samples):
                counts, totals = [0] * (len(names) + 1), [0.0] * (len(names) + 1)
                weights = Counter(rng.randrange(len(blocks)) for _ in blocks)
                for block, weight in weights.items():
                    bc, bt = aggregates[block]
                    if not any(bc):
                        continue
                    for group in range(len(counts)):
                        counts[group] += bc[group] * weight
                        totals[group] += bt[group] * weight
                if not counts[0]:
                    continue
                base_mean = totals[0] / counts[0]
                for group, name in enumerate(names, 1):
                    if counts[group] and (method != "DAY" or min(group_active[0], group_active[group]) >= 2):
                        draws[name].append(totals[group] / counts[group] - base_mean)
        for group, (name, values) in enumerate(draws.items(), 1):
            result[name][method] = {
                "status": "OK" if values else "INSUFFICIENT_CLUSTERS_OR_COMPLETE_TRADES",
                "valid_resamples": len(values), "undefined_resamples": samples - len(values),
                "active_clusters": active, "calendar_clusters": len(blocks),
                "reference_active_clusters": group_active[0], "selected_active_clusters": group_active[group],
                "nominal_95": [percentile(values, alpha / 2), percentile(values, 1 - alpha / 2)] if values else None,
                "bonferroni_family_95": [
                    percentile(values, alpha / (2 * family_size)),
                    percentile(values, 1 - alpha / (2 * family_size))] if values else None,
            }
    for name, mask in masks.items():
        selected = [(target_values[name][index] if target_values is not None else row["net_r"])
                    for row, index in eligible if mask[index]]
        selected = [value for value in selected if value is not None]
        reference = [row["net_r"] for row, index in eligible if base_mask[index]]
        result[name]["mean_r_difference"] = fmean(selected) - fmean(reference) if selected and reference else None
        result[name]["definition"] = "selected_mean_R_minus_reference_mean_R; NOT_paired_exit_delta"
        result[name]["samples"] = samples
        result[name]["seed"] = config.seed
        result[name]["family_size"] = family_size
    return result


def evaluate_filters(rows: list[dict], masks: dict[str, list[bool]], config: Config, plan: dict) -> dict:
    settings = plan["bootstrap"]
    intervals = joint_contrasts(rows, masks, config, samples=settings["samples"],
                               family_size=settings["family_size"], alpha=settings["alpha"])
    results = {}
    for name, mask in masks.items():
        retained = [row for row, keep in zip(rows, mask) if keep]
        eliminated = [row for row, keep in zip(rows, mask) if not keep]
        known = [row for row in retained if complete(row)]
        days = {utc(row["opened_at"]).date() for row in known}
        results[name] = {
            "retained": summary(retained, config, plan["minimum_complete_trades"]),
            "remaining_ratio": len(retained) / len(rows) if rows else None,
            "eliminated": summary(eliminated, config, plan["minimum_complete_trades"]),
            "selection_contrast": intervals[name], "paired_common_exit_delta_r": 0.0 if known else None,
            "paired_common_exit_delta_trade_95": [0.0, 0.0] if known else None,
            "paired_common_exit_delta_day_95": [0.0, 0.0] if len(days) >= 2 else None,
            "paired_ci_method": "ANALYTICAL_DEGENERATE_ZERO_DELTA_BOOTSTRAP; SAME_ROW_SAME_EXIT",
            "added_entry_count": 0,
        }
    return results


def cohort_periods(rows: list[dict], config: Config, plan: dict) -> dict:
    boundary = plan["splits"]["VALIDATION"]["start"]
    train, validation, crossing = [], [], []
    for row in rows:
        opening = int(utc(row["opened_at"]).timestamp())
        if opening >= boundary:
            validation.append(row)
        elif row["closed_at"] and int(utc(row["closed_at"]).timestamp()) < boundary:
            train.append(row)
        else:
            crossing.append(row["signal_id"])
    return {
        "TRAIN": summary(train, config, plan["minimum_complete_trades"]),
        "VALIDATION": summary(validation, config, plan["minimum_complete_trades"]),
        "train_boundary_crossing_excluded_from_train_only": crossing,
        "combined_includes_boundary_crossing": True,
        "monthly_basis": "UTC_CLOSE_MONTH",
        "entry_date_timezone": "UTC",
    }
