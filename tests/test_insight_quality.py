"""Findings must be comparable and big enough to carry a claim.

Both rules come from one real run: the full pipeline on the restructured
institute workbook produced this executive summary —

    "Key risk: other materially underperforms on gross fee collected."
    "Other shows Gross Fee Collected of INR 0 vs INR 18,839,132 overall
     (-18,839,132)."   n=1

Two separate defects in one sentence:

1. `Other` held ONE row — the single course the taxonomy could not classify.
2. The comparison was a segment SUM against the TOTAL sum, so the gap is
   negative for every segment that is not the whole institute. The second
   finding was the top counsellor "underperforming" by 18.8M.

Run: python -m tests.test_insight_quality   (plain asserts, no pytest dep)
"""

from __future__ import annotations

import sys

from agents.insights_agent import InsightsAgent, MIN_FINDING_N


# The real shape the Analyst emits, with the run's real numbers.
def _result() -> dict:
    return {
        "headline_number": {"metric": "gross_fee_collected",
                            "value": 18839132.22, "n": 1464},
        "breakdowns": [
            # The offender: one row, zero rupees.
            {"dimension": "course", "dimension_label": "course_category_derived",
             "segment": "course=Other", "value": 0.0, "n": 1},
            # A small faculty — real, but too small to headline.
            {"dimension": "faculty", "segment": "faculty=trusha",
             "value": 28000.0, "n": 6},
            # Big segments, genuinely above and below the institute average
            # of 12,868 per student.
            {"dimension": "faculty", "segment": "faculty=subin",
             "value": 353800.0, "n": 53},          # 6,675/student — below
            {"dimension": "branch", "segment": "branch=vesu",
             "value": 9000000.0, "n": 500},        # 18,000/student — above
        ],
        "comparisons": [],
    }


def test_a_one_row_segment_never_becomes_a_finding() -> None:
    """`Other` with n=1 was the headline of a real report."""
    agent = InsightsAgent()
    findings = agent._key_findings(_result(), {}, "gross_fee_collected", "value")
    named = [f["segment"] for f in findings]
    assert "Other" not in named, named
    assert "trusha" not in named, "n=6 is still too small to name"
    assert all(int(f.get("segment") is not None) for f in findings)


def test_the_minimum_matches_the_rest_of_the_pipeline() -> None:
    """One floor, or the report contradicts the Analyst's own suppression."""
    from agents import statistics as st
    assert MIN_FINDING_N == st.MIN_SEGMENT_N == 30


def test_a_sum_is_compared_per_row_not_against_the_total() -> None:
    """The bug that made every money segment underperform.

    A part is always smaller than the whole, so segment-sum vs total-sum is
    arithmetic, not analysis.
    """
    agent = InsightsAgent()
    res = _result()
    seg, base = agent._comparable(res["breakdowns"][3], res, "value")   # vesu
    assert round(seg) == 18000, seg          # 9,000,000 / 500
    assert round(base) == 12868, base        # 18,839,132 / 1,464
    assert seg > base, "vesu is ABOVE the institute average and must read so"

    findings = agent._key_findings(res, {}, "gross_fee_collected", "value")
    vesu = next(f for f in findings if f["segment"] == "vesu")
    assert vesu["direction"] == "positive", vesu["finding"]
    assert "per student" in vesu["finding"], vesu["finding"]
    assert "1,464" not in vesu["finding"], "the total must not appear as a baseline"


def test_a_rate_is_still_compared_directly() -> None:
    """Rates were never broken; the fix must not change them."""
    agent = InsightsAgent()
    res = {"headline_number": {"value": 0.40, "n": 1000},
           "breakdowns": [{"dimension": "branch", "segment": "branch=pal",
                           "value": 0.25, "n": 200}],
           "comparisons": []}
    seg, base = agent._comparable(res["breakdowns"][0], res, "rate")
    assert (seg, base) == (0.25, 0.40)


def test_a_money_segment_can_now_be_an_opportunity() -> None:
    """Under the old ratio it could not: segment-sum/total-sum is always < 1.

    So a money metric produced zero opportunities however good a branch was.
    """
    agent = InsightsAgent()
    opps = agent._opportunities(_result(), "gross_fee_collected", "value")
    assert opps, "vesu at 1.4x the average is an opportunity"
    assert "vesu" in opps[0]["opportunity"]
    assert "1.4x" in opps[0]["opportunity"], opps[0]["opportunity"]


def test_the_risk_is_a_real_underperformer_not_the_smallest_segment() -> None:
    agent = InsightsAgent()
    risks = agent._risks(_result(), "gross_fee_collected", "value")
    assert risks
    assert "subin" in risks[0]["risk"], risks[0]["risk"]      # 6,675 vs 12,868
    assert "Other" not in risks[0]["risk"]
    assert "53" in risks[0]["risk"], "state the n behind the claim"


def test_nothing_is_promoted_when_every_segment_is_tiny() -> None:
    """Silence is the correct output, not the least-bad guess."""
    agent = InsightsAgent()
    res = {"headline_number": {"value": 1000.0, "n": 10},
           "breakdowns": [{"dimension": "branch", "segment": "branch=pal",
                           "value": 100.0, "n": 3}],
           "comparisons": []}
    assert agent._key_findings(res, {}, "gross_fee_collected", "value") == []
    assert agent._risks(res, "gross_fee_collected", "value") == []
    assert agent._opportunities(res, "gross_fee_collected", "value") == []


def _run() -> int:
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run())
