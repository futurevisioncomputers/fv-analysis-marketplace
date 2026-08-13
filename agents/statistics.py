"""Statistical tests the pipeline needs and does not have.

The Analyst already reports a Wilson interval on every metric, which answers
"how precise is this number". Four questions it cannot answer come up
constantly in this institute's reporting, and each has a specific failure mode
when answered by eye:

- **"Which branch is really different?"** Comparing fifteen branches and
  reporting the worst guarantees finding one that looks bad. Needs a test per
  segment and a correction for having run fifteen of them.
- **"When do students leave?"** A churn *rate* treats a student who left after
  three weeks and one who is still enrolled as the same kind of row. The
  institute's own lifecycle already distinguishes them — paused and active are
  **censored**, not "did not churn" — and nothing currently uses that.
- **"Are newer cohorts worse?"** A single completion rate mixes cohorts that
  have had three years to finish with ones that have had three months.
- **"Is this trend real?"** A least-squares slope through a series with two
  spikes reports the spikes.

Everything here is closed-form and dependency-free, matching the hand-rolled
statistics already in `analyst_agent` and `eda_agent`: no scipy, deterministic,
and readable enough to check by hand. Where a distribution function is needed
it is implemented from its series expansion rather than approximated.

Nothing in this module decides anything. It returns numbers with their
uncertainty; the Insight agent and the operator decide what they mean.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

JsonDict = Dict[str, Any]

Z95 = 1.959963984540054

# Below this a segment is reported but not tested: the interval is so wide the
# test has no power, and a "not significant" verdict on n=6 says nothing.
MIN_SEGMENT_N = 30

_MAX_ITER = 300
_EPS = 3.0e-12


# ------------------------------------------------------------ distributions

def normal_sf(z: float) -> float:
    """P(Z > z) for a standard normal. Exact to double precision via erfc."""
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def _gamma_series(a: float, x: float) -> float:
    """Lower regularized incomplete gamma P(a, x), by series. Good for x < a+1."""
    total = term = 1.0 / a
    ap = a
    for _ in range(_MAX_ITER):
        ap += 1.0
        term *= x / ap
        total += term
        if abs(term) < abs(total) * _EPS:
            break
    return total * math.exp(-x + a * math.log(x) - math.lgamma(a))


def _gamma_cf(a: float, x: float) -> float:
    """Upper regularized incomplete gamma Q(a, x), by continued fraction.

    Lentz's method. Used for x >= a+1, where the series above converges slowly.
    """
    tiny = 1.0e-300
    b = x + 1.0 - a
    c = 1.0 / tiny
    d = 1.0 / b
    h = d
    for i in range(1, _MAX_ITER):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < tiny:
            d = tiny
        c = b + an / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < _EPS:
            break
    return math.exp(-x + a * math.log(x) - math.lgamma(a)) * h


def chi2_sf(statistic: float, dof: int) -> float:
    """P(X² > statistic) with `dof` degrees of freedom.

    The EDA agent reports chi² and Cramér's V but judges significance from the
    effect size alone, which on a large table with few rows calls noise a
    weak association. This supplies the missing p-value.
    """
    if dof <= 0 or statistic <= 0:
        return 1.0
    a, x = dof / 2.0, statistic / 2.0
    return 1.0 - _gamma_series(a, x) if x < a + 1.0 else _gamma_cf(a, x)


def wilson_interval(successes: int, n: int, z: float = Z95) -> Tuple[float, float]:
    """Wilson score interval — the same one the Analyst reports.

    Chosen over the normal approximation because rates here are routinely near
    0 or 1 (95% collection, 2% duplicates), where the naive interval runs past
    the ends of the scale.
    """
    if n <= 0:
        return (0.0, 0.0)
    p = successes / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - spread), min(1.0, centre + spread))


# ------------------------------------------------------- comparing segments

def two_proportion_test(x1: int, n1: int, x2: int, n2: int) -> JsonDict:
    """Is the rate in group 1 different from group 2?

    Two-sided z-test on the pooled proportion. Returns the difference, its
    confidence interval (unpooled — the pooled standard error belongs to the
    test, not to the estimate) and the p-value.
    """
    if n1 <= 0 or n2 <= 0:
        return {"difference": None, "z": None, "p_value": None, "n1": n1, "n2": n2}
    p1, p2 = x1 / n1, x2 / n2
    pooled = (x1 + x2) / (n1 + n2)
    se_pooled = math.sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n2))
    z = (p1 - p2) / se_pooled if se_pooled > 0 else 0.0
    se_diff = math.sqrt(p1 * (1 - p1) / n1 + p2 * (1 - p2) / n2)
    return {
        "rate_1": round(p1, 4), "rate_2": round(p2, 4),
        "difference": round(p1 - p2, 4),
        "difference_ci_95": (round(p1 - p2 - Z95 * se_diff, 4),
                             round(p1 - p2 + Z95 * se_diff, 4)),
        "z": round(z, 4),
        "p_value": round(2 * normal_sf(abs(z)), 6),
        "n1": n1, "n2": n2,
    }


def two_mean_test(mean1: float, sd1: float, n1: int,
                  mean2: float, sd2: float, n2: int) -> JsonDict:
    """Is the mean in group 1 different from group 2? Welch, normal approx.

    Welch rather than pooled because the groups here have wildly different
    spreads — average fee at a school course and at an advanced certificate are
    not the same distribution — and pooling their variances would report a
    difference that is really a variance mismatch.

    The normal approximation stands in for Student's t, which needs a
    distribution this module deliberately does not carry. It is close enough
    above n≈30 per group, which is also the floor a segment must clear to be
    tested at all, and conservative nowhere — so the caller must not lower that
    floor to use this.
    """
    if n1 < 2 or n2 < 2:
        return {"difference": None, "z": None, "p_value": None,
                "n1": n1, "n2": n2}
    se = math.sqrt(sd1 * sd1 / n1 + sd2 * sd2 / n2)
    diff = mean1 - mean2
    z = diff / se if se > 0 else 0.0
    return {
        "mean_1": round(mean1, 4), "mean_2": round(mean2, 4),
        "difference": round(diff, 4),
        "difference_ci_95": (round(diff - Z95 * se, 4), round(diff + Z95 * se, 4)),
        "z": round(z, 4),
        "p_value": round(2 * normal_sf(abs(z)), 6),
        "n1": n1, "n2": n2,
    }


def chi_square_independence(table: Sequence[Sequence[float]]) -> JsonDict:
    """Are the two factors of a contingency table independent?

    One headline test for a whole grid, run before its cells are picked over.
    Without it, scanning a 3x27 crosstab for the largest deviation finds one
    every time — the maximum of eighty-one noisy numbers is always large.

    Returns `valid: False` when any expected count falls below 5, where the
    chi-square approximation stops holding; the statistic is still reported so
    the caller can see how far off it was, but it must not be read as a p-value.
    """
    rows = [list(map(float, row)) for row in table if len(row)]
    if len(rows) < 2 or len(rows[0]) < 2:
        return {"statistic": None, "p_value": None, "dof": 0, "valid": False,
                "reason": "a test of independence needs at least a 2x2 table"}

    row_sums = [sum(r) for r in rows]
    col_sums = [sum(col) for col in zip(*rows)]
    total = sum(row_sums)
    if total <= 0:
        return {"statistic": None, "p_value": None, "dof": 0, "valid": False,
                "reason": "the table is empty"}

    statistic = 0.0
    low_expected = 0
    for i, row in enumerate(rows):
        for j, observed in enumerate(row):
            expected = row_sums[i] * col_sums[j] / total
            if expected < 5:
                low_expected += 1
            if expected > 0:
                statistic += (observed - expected) ** 2 / expected
    dof = (len(rows) - 1) * (len(rows[0]) - 1)
    valid = low_expected == 0
    return {
        "statistic": round(statistic, 4),
        "p_value": round(chi2_sf(statistic, dof), 6) if valid else None,
        "dof": dof,
        "valid": valid,
        "cells_with_expected_below_5": low_expected,
        "reason": None if valid else (
            f"{low_expected} cell(s) expect fewer than 5 observations; the "
            f"chi-square approximation does not hold, so no p-value is given"
        ),
    }


def benjamini_hochberg(p_values: Sequence[float],
                       alpha: float = 0.05) -> List[JsonDict]:
    """Control the false-discovery rate across a family of tests.

    Testing fifteen branches at the 5% level and reporting whichever look
    different finds roughly one spurious result every time, by construction.
    BH answers the question that actually matters — "of the ones I am about to
    call real, what share are likely noise?" — and is the right correction here
    because these are screening comparisons, not a single pre-registered
    hypothesis where Bonferroni's stricter control would be warranted.

    Returns one entry per input, in the input's order, each with its adjusted
    q-value and whether it survives.
    """
    indexed = sorted(enumerate(p_values), key=lambda pair: pair[1])
    m = len(indexed)
    out: List[Optional[JsonDict]] = [None] * m
    running = 1.0
    # Walk from the largest p-value down, keeping the running minimum, so the
    # adjusted values come out monotone.
    for rank in range(m, 0, -1):
        position, p = indexed[rank - 1]
        running = min(running, p * m / rank)
        out[position] = {"p_value": round(p, 6), "q_value": round(running, 6),
                         "significant": running <= alpha}
    return [entry for entry in out if entry is not None]


def compare_segments(frame, flag_column: str, dimension: str,
                     alpha: float = 0.05,
                     min_n: int = MIN_SEGMENT_N) -> JsonDict:
    """Each segment against everyone else, corrected for multiple testing.

    This is the "which branch is really different" answer. Segments below
    `min_n` are reported with their rate but excluded from the testing family:
    including them costs power for every other segment and returns a verdict
    nobody should act on anyway.
    """
    import pandas as pd

    if flag_column not in frame.columns or dimension not in frame.columns:
        return {"status": "blocked",
                "reason": f"need both {flag_column!r} and {dimension!r} in the frame"}

    work = frame[[dimension, flag_column]].dropna(subset=[dimension])
    flags = work[flag_column].fillna(False).astype(bool)
    total_n = len(work)
    total_x = int(flags.sum())
    if total_n == 0:
        return {"status": "blocked", "reason": "no rows carry both columns"}

    rows, tested = [], []
    for value, index in work.groupby(dimension).groups.items():
        n = len(index)
        x = int(flags.loc[index].sum())
        low, high = wilson_interval(x, n)
        entry = {
            "segment": str(value), "n": n, "count": x,
            "rate": round(x / n, 4) if n else None,
            "ci_95": (round(low, 4), round(high, 4)),
            "underpowered": n < min_n,
        }
        if n >= min_n and (total_n - n) >= min_n:
            entry["vs_rest"] = two_proportion_test(x, n, total_x - x, total_n - n)
            tested.append(entry)
        rows.append(entry)

    for entry, verdict in zip(tested, benjamini_hochberg(
            [e["vs_rest"]["p_value"] for e in tested], alpha)):
        entry["vs_rest"].update(verdict)

    rows.sort(key=lambda r: (r["rate"] is None, r["rate"]))
    return {
        "status": "ready",
        "dimension": dimension,
        "flag": flag_column,
        "overall_rate": round(total_x / total_n, 4),
        "n": total_n,
        "segments": rows,
        "tested": len(tested),
        "not_tested": len(rows) - len(tested),
        "significant": [e["segment"] for e in tested
                        if e["vs_rest"].get("significant")],
        "method": (f"two-proportion z-test of each segment against the rest, "
                   f"Benjamini-Hochberg corrected at alpha={alpha}; segments "
                   f"under n={min_n} are reported but not tested"),
    }


# ------------------------------------------------------------- time to event

def kaplan_meier(durations: Sequence[float],
                 events: Sequence[bool]) -> JsonDict:
    """Survival curve honouring censored rows.

    `events[i]` is True when the outcome happened (the student churned) and
    False when the row is **censored** — still enrolled, or paused, so their
    outcome is not yet known. That distinction is the whole point: counting a
    censored student as "did not churn" understates churn, and dropping them
    overstates it. The institute's lifecycle already draws this line — active
    and paused are censored, `NOT TO ENTERTRAIN` and 6-month not-coming are
    events — and nothing else in the pipeline uses it.

    Returns the step function, the median survival time when the curve reaches
    0.5, and the number still at risk at each step.
    """
    pairs = sorted((float(d), bool(e)) for d, e in zip(durations, events)
                   if d is not None and not (isinstance(d, float) and math.isnan(d)))
    if not pairs:
        return {"status": "blocked", "reason": "no usable durations"}

    n_total = len(pairs)
    n_events = sum(1 for _, e in pairs if e)
    if n_events == 0:
        return {"status": "blocked",
                "reason": f"all {n_total} rows are censored — no outcome has "
                          f"occurred yet, so there is no curve to estimate"}

    steps: List[JsonDict] = []
    survival = 1.0
    at_risk = n_total
    index = 0
    while index < len(pairs):
        time = pairs[index][0]
        # Every row sharing this time is handled together, deaths before
        # censorings, which is the standard convention.
        same = [p for p in pairs if p[0] == time]
        deaths = sum(1 for _, e in same if e)
        if deaths:
            survival *= (1 - deaths / at_risk)
            steps.append({"time": round(time, 2), "at_risk": at_risk,
                          "events": deaths, "survival": round(survival, 4)})
        at_risk -= len(same)
        index += len(same)

    median = next((s["time"] for s in steps if s["survival"] <= 0.5), None)
    return {
        "status": "ready",
        "n": n_total,
        "events": n_events,
        "censored": n_total - n_events,
        "median_survival": median,
        "steps": steps,
        "method": ("Kaplan-Meier product-limit estimator; censored rows "
                   "contribute to the risk set until they are censored, so an "
                   "unfinished course neither counts as churn nor as survival"),
    }


def survival_at(curve: Mapping[str, Any], time: float) -> Optional[float]:
    """Survival probability at `time` — the last step at or before it."""
    value = 1.0
    for step in curve.get("steps") or []:
        if step["time"] > time:
            break
        value = step["survival"]
    return value


# ------------------------------------------------------------------- trends

def mann_kendall(values: Sequence[float]) -> JsonDict:
    """Is there a monotonic trend? Rank-based, so spikes do not drive it.

    Preferred over a regression slope's t-test because these series are short,
    seasonal and contain data-entry spikes: Mann-Kendall asks only whether
    later values tend to exceed earlier ones.
    """
    series = [float(v) for v in values
              if v is not None and not (isinstance(v, float) and math.isnan(v))]
    n = len(series)
    if n < 4:
        return {"status": "blocked",
                "reason": f"need at least 4 periods, got {n}"}

    s = 0
    for i in range(n - 1):
        for j in range(i + 1, n):
            s += (series[j] > series[i]) - (series[j] < series[i])

    # Variance with the tie correction; without it, a series of repeated counts
    # (common in small monthly tallies) looks more significant than it is.
    counts: Dict[float, int] = {}
    for value in series:
        counts[value] = counts.get(value, 0) + 1
    ties = sum(c * (c - 1) * (2 * c + 5) for c in counts.values() if c > 1)
    variance = (n * (n - 1) * (2 * n + 5) - ties) / 18.0

    if variance <= 0:
        z = 0.0
    elif s > 0:
        z = (s - 1) / math.sqrt(variance)
    elif s < 0:
        z = (s + 1) / math.sqrt(variance)
    else:
        z = 0.0

    p = 2 * normal_sf(abs(z))
    direction = "flat"
    if p <= 0.05:
        direction = "rising" if s > 0 else "falling"
    return {"status": "ready", "n": n, "s": s, "z": round(z, 4),
            "p_value": round(p, 6), "trend": direction,
            "method": "Mann-Kendall test for monotonic trend, tie-corrected"}


def theil_sen(values: Sequence[float]) -> JsonDict:
    """Median of all pairwise slopes — a trend line two outliers cannot move.

    Reported alongside Mann-Kendall: the test says whether a trend exists, this
    says how steep it is, in units per period.
    """
    series = [(i, float(v)) for i, v in enumerate(values)
              if v is not None and not (isinstance(v, float) and math.isnan(v))]
    if len(series) < 2:
        return {"status": "blocked", "reason": "need at least 2 periods"}

    slopes = sorted((y2 - y1) / (x2 - x1)
                    for i, (x1, y1) in enumerate(series)
                    for x2, y2 in series[i + 1:] if x2 != x1)
    if not slopes:
        return {"status": "blocked", "reason": "no distinct periods"}

    slope = _median(slopes)
    intercept = _median([y - slope * x for x, y in series])
    return {"status": "ready", "slope_per_period": round(slope, 4),
            "intercept": round(intercept, 4), "pairs": len(slopes),
            "method": "Theil-Sen estimator (median of pairwise slopes)"}


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


# ------------------------------------------------------------------ cohorts

def cohort_rates(frame, cohort_column: str, flag_column: str,
                 min_n: int = MIN_SEGMENT_N) -> JsonDict:
    """One rate per joining cohort, with intervals and a trend across them.

    A single completion rate mixes a cohort with three years to finish and one
    with three months. Splitting by cohort is what makes "are we getting worse"
    answerable; the Mann-Kendall over the cohort sequence answers it.
    """
    if cohort_column not in frame.columns or flag_column not in frame.columns:
        return {"status": "blocked",
                "reason": f"need both {cohort_column!r} and {flag_column!r}"}

    work = frame[[cohort_column, flag_column]].dropna(subset=[cohort_column])
    flags = work[flag_column].fillna(False).astype(bool)

    rows = []
    for value, index in sorted(work.groupby(cohort_column).groups.items(),
                               key=lambda pair: str(pair[0])):
        n = len(index)
        x = int(flags.loc[index].sum())
        low, high = wilson_interval(x, n)
        rows.append({"cohort": str(value), "n": n, "count": x,
                     "rate": round(x / n, 4) if n else None,
                     "ci_95": (round(low, 4), round(high, 4)),
                     "underpowered": n < min_n})
    if not rows:
        return {"status": "blocked", "reason": "no cohorts found"}

    trend = mann_kendall([r["rate"] for r in rows])
    return {"status": "ready", "cohort_column": cohort_column,
            "flag": flag_column, "cohorts": rows, "trend": trend}
