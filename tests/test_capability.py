"""The stage-1 capability check must not lie in either direction.

At the first checkpoint the operator is told which of their questions this
sheet can answer. That claim is made before any cleaning happens, from column
headers and a small sample — so it can be wrong two ways, and both are bad:

  - **Over-promise.** "Yes, answerable" and then the Analyst blocks at stage 4.
    Worse than saying nothing, because the operator waited for it and the
    checkpoint is the moment they could have supplied the missing sheet.
  - **Under-promise.** "You need fees-data" when the metric was computable all
    along, so they go hunting for a file they did not need.

These tests run the real pipeline over every sample sheet and compare what the
checkpoint promised against what the Analyst actually produced.

Run: python -m tests.test_capability   (plain asserts, no pytest dep)
"""

from __future__ import annotations

import glob
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agents import capability                                   # noqa: E402
from agents.analyst_agent import AnalystAgent                   # noqa: E402
from agents.data_engineer_agent import DataEngineerAgent        # noqa: E402

SAMPLES = sorted(glob.glob(os.path.join(ROOT, "samples", "*__*.csv")))

# One question per metric family, phrased the way the checkpoint would see it.
PROBE_METRICS = ("admission_conversion_rate", "gross_fee_collected",
                 "pending_fee", "collection_efficiency", "dropout_rate",
                 "certificate_pending_rate", "duplicate_certificate_rate",
                 "certificate_issue_lag_days", "repeat_enrollment_rate",
                 "lead_to_admission_days", "enquiry_backlog_rate")


def _source(path: str) -> dict:
    return {"name": os.path.basename(path), "type": "csv",
            "path": path, "path_or_query": path, "domain": "single"}


def test_promise_matches_what_the_analyst_does() -> None:
    """Every "answerable" must compute, and every "not answerable" must not."""
    mismatches = []
    for path in SAMPLES:
        source = _source(path)
        roles = capability.available_roles([source])
        package = DataEngineerAgent().run({"question": "capability probe"}, path)
        if package.get("status") != "ready":
            continue                       # a blocked sheet promises nothing

        analyst = AnalystAgent()
        for metric in PROBE_METRICS:
            promised = capability.metric_capability(metric, roles)["available"]
            question = {"question_id": "BQ-001", "question": metric,
                        "metrics": [metric], "dimensions": []}
            actual = analyst.run(question, package).get("status") == "ready"
            if promised != actual:
                mismatches.append(
                    f"{os.path.basename(path)} · {metric}: "
                    f"promised={promised} actual={actual}")

    assert not mismatches, "capability check disagrees with the Analyst:\n  " + \
        "\n  ".join(mismatches)


def test_missing_data_names_the_sheet_to_add() -> None:
    """"Not answerable" is a dead end; naming the sheet is an instruction."""
    admission = _source(os.path.join(ROOT, "samples",
                                     "admission_form__form_responses_1.csv"))
    brief = {"business_questions": [
        {"question_id": "BQ-001", "question": "How much fee is pending?",
         "metrics": ["pending_fee"]},
        {"question_id": "BQ-002", "question": "Are certificate numbers duplicated?",
         "metrics": ["duplicate_certificate_rate"]},
        {"question_id": "BQ-003", "question": "How is conversion by branch?",
         "metrics": ["admission_conversion_rate"]},
    ]}
    needs = capability.data_needs(brief, [admission])

    assert needs["answerable"] == 1 and needs["blocked"] == 2, needs
    gaps = {gap["role"]: gap for gap in needs["missing_data"]}
    assert "pending" in gaps and "fees-data" in gaps["pending"]["found_in"]
    assert "certificate_number" in gaps
    assert "certificate-data" in gaps["certificate_number"]["found_in"]
    # Each gap says which questions it unlocks, so the operator can judge
    # whether fetching the file is worth it.
    assert gaps["pending"]["unlocks"] == ["BQ-001"], gaps["pending"]


def test_substitution_is_disclosed() -> None:
    """Answering with a fallback metric is a different answer — say so.

    `fees-data` has no paid column, so a question asking for gross collection
    is answered with pending fee instead. Silently swapping would put a number
    under a heading it does not match.
    """
    fees = _source(os.path.join(ROOT, "samples",
                                "student_data_sheet__fees_data.csv"))
    brief = {"business_questions": [
        {"question_id": "BQ-001", "question": "How much fee was collected?",
         "metrics": ["gross_fee_collected", "pending_fee"]},
    ]}
    entry = capability.data_needs(brief, [fees])["questions"][0]

    assert entry["answerable"]
    assert entry["asked_for"] == "gross_fee_collected"
    assert entry["will_answer_with"] == "pending_fee"
    assert entry["substituted"] is True


def test_cancellation_availability_is_measured_not_assumed() -> None:
    """Dropout rate depends on typed-in text, so it needs a value probe.

    Every sheet has a name column; only some have "(cancelled)" written into
    it. Promising the metric on the column alone would pass the checkpoint and
    block at stage 4.
    """
    with_markers = capability.available_roles([_source(os.path.join(
        ROOT, "samples", "student_data_sheet__student_data.csv"))])
    without = capability.available_roles([_source(os.path.join(
        ROOT, "samples", "student_timetable__main_data.csv"))])

    assert "cancel_marker" in with_markers
    assert "name" in without and "cancel_marker" not in without, (
        "a name column alone must not promise a dropout rate")


def test_a_silent_substitution_still_asks_for_the_missing_sheet() -> None:
    """Asking for churn and being handed completion_rate is not an answer.

    The question counts as answerable because a substitute exists, so it never
    reached the blocked list — and the one file that would have answered what
    was actually asked went unrequested.
    """
    import os

    from agents.problem_definition_agent import ProblemDefinitionAgent

    brief = ProblemDefinitionAgent().run("what is my churn rate by branch?")

    # Only the leaver tabs plus the student sheet: lifecycle membership and a
    # start date are present, but `Course Duration (IN DAYS)` lives on the MAIN
    # timetable sheet, which is not supplied here.
    sources = [
        _source(os.path.join(ROOT, "samples", f"student_timetable__{tab}.csv"))
        for tab in ("not_coming", "not_to_entertain", "course_completed")
    ]
    sources.append(_source(os.path.join(
        ROOT, "samples", "student_data_sheet__student_data.csv")))

    needs = capability.data_needs(brief, sources)
    assert needs["blocked"] == 0, "a substitute exists, so nothing is blocked"

    question = needs["questions"][0]
    assert question["asked_for"] == "churn_rate"
    assert question["substituted"] is True

    gap = next(m for m in needs["missing_data"] if m["role"] == "course_duration")
    assert any(u.endswith(":churn_rate") for u in gap["unlocks"]), gap
    assert "Course Duration" in gap["found_in"]

    # And the churn metric itself carries the time-dependence warning.
    check = next(m for m in question["metrics"] if m["metric"] == "churn_rate")
    assert check["available"] is False
    assert "as-of date" in check["caveat"]

    # Add the main sheet — the one that carries the column — and the same
    # question is answerable as asked, with no substitution.
    sources.append(_source(os.path.join(
        ROOT, "samples", "student_timetable__main_data.csv")))
    with_main = capability.data_needs(brief, sources)
    answered = with_main["questions"][0]
    assert answered["will_answer_with"] == "churn_rate", answered
    assert answered["substituted"] is False
    assert not [m for m in with_main["missing_data"]
                if m["role"] == "course_duration"]


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
