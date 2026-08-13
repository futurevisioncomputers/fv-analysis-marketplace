"""Known-answer tests for the statistics module.

Every function here is hand-rolled — no scipy — so "it returned a number" is
not evidence of anything. Each test checks against a value computed
independently: a textbook worked example, an analytically known result, or a
degenerate case whose answer is forced.

Run: python -m tests.test_statistics   (plain asserts, no pytest dep)
"""

from __future__ import annotations

import math
import os
import sys

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agents import statistics as st                              # noqa: E402


def close(a, b, tol=1e-4) -> bool:
    return a is not None and abs(a - b) <= tol


# ------------------------------------------------------------ distributions

def test_normal_and_chi2_match_published_values() -> None:
    # Standard normal tail probabilities.
    assert close(st.normal_sf(0.0), 0.5)
    assert close(st.normal_sf(1.959963984540054), 0.025)
    assert close(st.normal_sf(2.575829303548901), 0.005)

    # Chi-square critical values at alpha = 0.05, from any table.
    assert close(st.chi2_sf(3.841459, 1), 0.05, tol=1e-5)
    assert close(st.chi2_sf(5.991465, 2), 0.05, tol=1e-5)
    assert close(st.chi2_sf(16.918978, 9), 0.05, tol=1e-5)
    # And the ends behave.
    assert st.chi2_sf(0.0, 3) == 1.0
    assert st.chi2_sf(100.0, 1) < 1e-20


def test_wilson_interval_is_bounded_and_asymmetric() -> None:
    low, high = st.wilson_interval(95, 100)
    assert 0.0 <= low <= 0.95 <= high <= 1.0
    # Near the boundary the interval must not run past 1, which is exactly
    # where the normal approximation fails.
    assert high < 1.0
    # 0 of 20 has a real upper bound, not a point estimate of zero.
    low, high = st.wilson_interval(0, 20)
    assert low == 0.0 and 0.10 < high < 0.20


# --------------------------------------------------------------- proportions

def test_two_proportion_test_against_a_worked_example() -> None:
    """30/100 vs 20/100: pooled p = 0.25, se = 0.06124, z = 1.63299."""
    result = st.two_proportion_test(30, 100, 20, 100)
    assert close(result["difference"], 0.10)
    assert close(result["z"], 1.6330, tol=1e-3)
    assert close(result["p_value"], 0.1025, tol=1e-3)

    # Identical rates -> no difference, p = 1.
    same = st.two_proportion_test(25, 100, 50, 200)
    assert close(same["difference"], 0.0)
    assert close(same["p_value"], 1.0)

    # The CI on the difference is unpooled and must straddle zero here.
    low, high = same["difference_ci_95"]
    assert low < 0 < high


def test_benjamini_hochberg_matches_the_hand_calculation() -> None:
    """m=4, p = .01 .02 .03 .04 -> q = .04 .04 .04 .04, all significant."""
    verdicts = st.benjamini_hochberg([0.01, 0.02, 0.03, 0.04], alpha=0.05)
    assert [v["q_value"] for v in verdicts] == [0.04, 0.04, 0.04, 0.04]
    assert all(v["significant"] for v in verdicts)

    # Order is preserved regardless of input order.
    shuffled = st.benjamini_hochberg([0.04, 0.01, 0.03, 0.02])
    assert [v["p_value"] for v in shuffled] == [0.04, 0.01, 0.03, 0.02]

    # One real effect among many nulls: the small p survives, the rest do not.
    verdicts = st.benjamini_hochberg([0.001] + [0.4, 0.5, 0.6, 0.7, 0.8])
    assert verdicts[0]["significant"]
    assert not any(v["significant"] for v in verdicts[1:])

    # Adjusted values must be monotone — a larger p can never get a smaller q.
    q = [v["q_value"] for v in st.benjamini_hochberg([0.01, 0.9, 0.02, 0.03])]
    ordered = sorted(zip([0.01, 0.9, 0.02, 0.03], q))
    assert all(ordered[i][1] <= ordered[i + 1][1] + 1e-12
               for i in range(len(ordered) - 1))


def test_segment_comparison_corrects_for_how_many_were_tested() -> None:
    """Fifteen identical branches must not produce a "worst" one."""
    rows = []
    for branch in range(15):
        for row in range(100):
            rows.append({"branch": f"B{branch}", "is_default": row < 20})
    frame = pd.DataFrame(rows)

    result = st.compare_segments(frame, "is_default", "branch")
    assert result["status"] == "ready"
    assert result["tested"] == 15
    assert result["significant"] == [], (
        "identical segments produced a false discovery")

    # Now make one genuinely different and check it is found.
    frame.loc[frame["branch"] == "B7", "is_default"] = [
        i < 60 for i in range(100)]
    result = st.compare_segments(frame, "is_default", "branch")
    assert result["significant"] == ["B7"], result["significant"]


def test_small_segments_are_reported_but_not_tested() -> None:
    """A verdict on n=6 is noise wearing a p-value."""
    frame = pd.DataFrame(
        [{"branch": "big", "is_default": i < 20} for i in range(200)]
        + [{"branch": "tiny", "is_default": i < 4} for i in range(6)])

    result = st.compare_segments(frame, "is_default", "branch")
    tiny = next(s for s in result["segments"] if s["segment"] == "tiny")
    assert tiny["underpowered"] is True
    assert "vs_rest" not in tiny, "an underpowered segment was tested anyway"
    assert tiny["rate"] == 0.6667, tiny

    # `big` is not tested either, and that is right: "the rest" here is the
    # 6-row segment, so the comparison would be just as underpowered from the
    # other side. Both are reported with their intervals; neither gets a verdict.
    big = next(s for s in result["segments"] if s["segment"] == "big")
    assert "vs_rest" not in big
    assert result["tested"] == 0 and result["not_tested"] == 2


# ------------------------------------------------------------- time to event

def test_kaplan_meier_against_a_hand_computed_curve() -> None:
    """Six subjects, events at 2 and 5, censoring at 3.

    t=2: 1 event of 6 at risk        -> S = 5/6      = 0.8333
    t=3: censored, leaves the set    -> S unchanged
    t=5: 1 event of 4 at risk        -> S = 0.8333 * 3/4 = 0.6250
    """
    curve = st.kaplan_meier([2, 3, 5, 6, 7, 8],
                            [True, False, True, False, False, False])
    assert curve["status"] == "ready"
    assert curve["events"] == 2 and curve["censored"] == 4
    assert close(curve["steps"][0]["survival"], 0.8333, tol=1e-4)
    assert close(curve["steps"][1]["survival"], 0.6250, tol=1e-4)
    assert curve["median_survival"] is None, (
        "the curve never reaches 0.5, so there is no median to report")

    # Survival at a time between steps is the last step at or before it.
    assert close(st.survival_at(curve, 4), 0.8333, tol=1e-4)
    assert close(st.survival_at(curve, 1), 1.0)


def test_censoring_changes_the_answer() -> None:
    """The reason this module exists: censored is not "did not churn".

    Ten students, three churned early, seven still enrolled. A naive rate says
    30% churn. The curve says survival is 70% *only so far* — the seven have
    not finished, and treating them as survivors is a claim about the future.
    """
    durations = [1, 2, 3] + [4] * 7
    curve = st.kaplan_meier(durations, [True, True, True] + [False] * 7)
    assert curve["censored"] == 7
    assert close(curve["steps"][-1]["survival"], 0.7, tol=1e-9)

    # With everyone censored there is no curve, and it says so rather than
    # returning a flat line at 1.0.
    none_yet = st.kaplan_meier([1, 2, 3], [False, False, False])
    assert none_yet["status"] == "blocked"
    assert "censored" in none_yet["reason"]


# ------------------------------------------------------------------- trends

def test_mann_kendall_finds_a_trend_and_ignores_a_spike() -> None:
    rising = st.mann_kendall([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    assert rising["trend"] == "rising"
    assert rising["s"] == 45          # every one of the 45 pairs is concordant
    assert rising["p_value"] < 0.001

    falling = st.mann_kendall([10, 9, 8, 7, 6, 5, 4, 3, 2, 1])
    assert falling["trend"] == "falling" and falling["s"] == -45

    # A flat series with one big spike has no trend — this is the case a
    # least-squares slope gets wrong.
    spiky = st.mann_kendall([5, 5, 5, 5, 99, 5, 5, 5, 5, 5])
    assert spiky["trend"] == "flat", spiky

    assert st.mann_kendall([1, 2])["status"] == "blocked"


def test_theil_sen_recovers_an_exact_line_and_resists_an_outlier() -> None:
    exact = st.theil_sen([0, 3, 6, 9, 12])
    assert close(exact["slope_per_period"], 3.0)
    assert close(exact["intercept"], 0.0)

    # One wild point must not move the slope much; ordinary least squares on
    # this series returns roughly 6.9.
    robust = st.theil_sen([0, 3, 6, 9, 120])
    assert close(robust["slope_per_period"], 3.0), robust


def _cohort_frame(rates) -> pd.DataFrame:
    rows = []
    for month, rate in enumerate(rates, start=1):
        rows += [{"joining_cohort": f"2024-{month:02d}",
                  "is_completed": j < rate * 50} for j in range(50)]
    return pd.DataFrame(rows)


def test_cohort_rates_carry_intervals_and_a_trend() -> None:
    result = st.cohort_rates(_cohort_frame([0.9, 0.8, 0.7, 0.6, 0.5, 0.4]),
                             "joining_cohort", "is_completed")

    assert result["status"] == "ready"
    assert [c["rate"] for c in result["cohorts"]] == [0.9, 0.8, 0.7, 0.6, 0.5, 0.4]
    assert result["trend"]["trend"] == "falling"
    assert all(c["ci_95"][0] < c["rate"] < c["ci_95"][1]
               for c in result["cohorts"])


def test_four_cohorts_cannot_evidence_a_trend() -> None:
    """A real limit, not a bug: Mann-Kendall on n=4 cannot reach p < 0.05.

    Even a perfectly monotone four-point series gives p = 0.089. So a report
    must not call four declining cohorts a decline — the honest answer is that
    there is not yet enough history, and that is what "flat" means here.
    """
    result = st.cohort_rates(_cohort_frame([0.8, 0.6, 0.4, 0.2]),
                             "joining_cohort", "is_completed")

    assert [c["rate"] for c in result["cohorts"]] == [0.8, 0.6, 0.4, 0.2]
    assert result["trend"]["trend"] == "flat"
    assert 0.05 < result["trend"]["p_value"] < 0.10, result["trend"]
    # Three cohorts cannot even be tested.
    assert st.cohort_rates(_cohort_frame([0.8, 0.5, 0.2]), "joining_cohort",
                           "is_completed")["trend"]["status"] == "blocked"


def main() -> int:
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"PASS {name}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {name}: {exc}")
        except Exception as exc:  # noqa: BLE001 - report, do not mask
            failures += 1
            print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    print(f"\n{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
