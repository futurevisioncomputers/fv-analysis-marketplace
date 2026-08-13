"""Tests for the corrected churn rule.

Every case here is hand-computable from the rule the institute stated:

    course_end := Date of Joining + Course Duration (IN DAYS)
    churn      := membership in Not_Coming / NOT TO ENTERTRAIN
                  AND as_of >= course_end + 6 months

Run: python -m tests.test_lifecycle   (plain asserts, no pytest dep)
"""

from __future__ import annotations

import sys

import pandas as pd

from agents import lifecycle as lc


ROLES = {
    "joining_date": "Date of Joining",
    "course_duration": "Course Duration (IN DAYS)",
}


def _frame(rows) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    df["Date of Joining"] = pd.to_datetime(df["Date of Joining"])
    return df


def test_churn_counts_from_course_end_not_admission() -> None:
    """The whole point of the correction.

    A twelve-month course begun on 2025-01-01 is due to finish on 2026-01-01.
    A student who stopped attending is NOT churned in July 2025 — their course
    still has half a year to run. The old admission-date clock said otherwise,
    and mislabelled every course longer than six months.
    """
    df = _frame([{
        "completion_status": "not_coming",
        "Date of Joining": "2025-01-01",
        "Course Duration (IN DAYS)": 360,
    }])

    # Six months after ADMISSION — the old rule's churn date.
    early = lc.churn_labels(df, ROLES, as_of="2025-07-02")
    assert early["columns"]["churn_status"].iloc[0] == "paused"
    assert early["columns"]["is_churn"].iloc[0] is False

    # Six months after the course was due to END.
    late = lc.churn_labels(df, ROLES, as_of="2026-07-02")
    assert late["columns"]["churn_status"].iloc[0] == "churned"
    assert late["columns"]["is_churn"].iloc[0] is True


def test_grace_boundary_is_inclusive() -> None:
    """as_of == course_end + 6 months is churn; one day earlier is not."""
    df = _frame([{
        "completion_status": "not_coming",
        "Date of Joining": "2025-01-01",
        "Course Duration (IN DAYS)": 90,      # ends 2025-04-01
    }])
    assert lc.churn_labels(df, ROLES, as_of="2025-10-01")[
        "columns"]["churn_status"].iloc[0] == "churned"
    assert lc.churn_labels(df, ROLES, as_of="2025-09-30")[
        "columns"]["churn_status"].iloc[0] == "paused"


def test_not_to_entertain_follows_the_same_clock_by_default() -> None:
    """As last stated by the institute: both sheets take the 6-month test.

    The switch back to unconditional churn is one flag, because the earlier
    instruction said NOT TO ENTERTRAIN means the student is not allowed back
    regardless of the calendar.
    """
    df = _frame([{
        "completion_status": "not_to_entertain",
        "Date of Joining": "2025-01-01",
        "Course Duration (IN DAYS)": 360,
    }])
    assert lc.churn_labels(df, ROLES, as_of="2025-07-02")[
        "columns"]["churn_status"].iloc[0] == "paused"

    forced = lc.churn_labels(df, ROLES, as_of="2025-07-02",
                             entertain_unconditional=True)
    assert forced["columns"]["churn_status"].iloc[0] == "churned"
    assert forced["columns"]["churn_basis"].iloc[0] == "sheet_membership"
    assert any("unconditionally" in n for n in forced["summary"]["notes"])


def test_missing_duration_is_unlabelled_not_retained() -> None:
    """No course end, no verdict — and no place in either side of the rate.

    Counting an unlabelled row as retained understates churn; counting it as
    churn overstates it. It is excluded, and the exclusion is reported.
    """
    df = _frame([
        {"completion_status": "not_coming", "Date of Joining": "2024-01-01",
         "Course Duration (IN DAYS)": 90},
        {"completion_status": "not_coming", "Date of Joining": "2024-01-01",
         "Course Duration (IN DAYS)": None},
        # A zero is a blank someone typed as 0, not a same-day course.
        {"completion_status": "not_coming", "Date of Joining": "2024-01-01",
         "Course Duration (IN DAYS)": 0},
    ])
    res = lc.churn_labels(df, ROLES, as_of="2026-01-01")
    statuses = list(res["columns"]["churn_status"])
    assert statuses == ["churned", "unlabelled", "unlabelled"]
    assert res["columns"]["is_churn"].iloc[1] is None

    summary = res["summary"]
    assert summary["at_risk_rows"] == 3
    assert summary["labelled_rows"] == 1
    assert summary["unlabelled_rows"] == 2
    # 1 churned of 1 labelled — the two unknowns are not in the denominator.
    assert summary["churn_rate_of_at_risk"] == 1.0
    assert any("unlabelled" in n for n in summary["notes"])


def test_no_duration_source_at_all_blocks_with_an_actionable_reason() -> None:
    """A timetable-only run cannot answer churn. It must say what is missing."""
    df = pd.DataFrame({"completion_status": ["not_coming", "not_coming"]})
    res = lc.churn_labels(df, {}, as_of="2026-01-01")
    assert set(res["columns"]["churn_status"]) == {"unlabelled"}
    note = " ".join(res["summary"]["notes"])
    assert "Course Duration (IN DAYS)" in note and "Date of Joining" in note


def test_staff_alumni_are_not_churn() -> None:
    """They left the roster for the payroll. Counting them as churn is wrong.

    It also blames the institute for its own hiring: the better a tutor the
    student became, the worse the retention number looks.
    """
    df = _frame([
        {"completion_status": "not_coming", "Date of Joining": "2024-01-01",
         "Course Duration (IN DAYS)": 90, "is_staff_alumni": True},
        {"completion_status": "not_coming", "Date of Joining": "2024-01-01",
         "Course Duration (IN DAYS)": 90, "is_staff_alumni": False},
    ])
    res = lc.churn_labels(df, ROLES, as_of="2026-01-01")
    assert list(res["columns"]["churn_status"]) == ["staff_alumni", "churned"]
    assert list(res["columns"]["is_churn"]) == [False, True]
    assert res["summary"]["labelled_rows"] == 1


def test_as_of_defaults_to_the_data_not_the_clock() -> None:
    """These are historical exports. Wall-clock churns the entire roster."""
    df = _frame([{
        "completion_status": "not_coming",
        "Date of Joining": "2019-01-01",
        "Course Duration (IN DAYS)": 90,
    }])
    res = lc.churn_labels(df, ROLES)
    assert res["summary"]["as_of"].startswith("2019-01-01")
    assert res["summary"]["as_of_source"] == "latest date in the data"
    # And the date used is always reported, because the answer depends on it.
    assert "as_of" in res["summary"]


def test_the_same_row_changes_label_with_the_as_of_date() -> None:
    """Churn is a function of time, so it cannot be a stored column."""
    df = _frame([{
        "completion_status": "not_coming",
        "Date of Joining": "2025-01-01",
        "Course Duration (IN DAYS)": 90,
    }])
    before = lc.churn_labels(df, ROLES, as_of="2025-05-01")
    after = lc.churn_labels(df, ROLES, as_of="2026-05-01")
    assert before["columns"]["churn_status"].iloc[0] == "paused"
    assert after["columns"]["churn_status"].iloc[0] == "churned"
    # Nothing in the source changed between the two runs.
    assert len(df) == 1


def test_tenure_for_a_churned_row_ends_at_the_deadline() -> None:
    """Not at as_of — that is when the report ran, not when they left.

    Using as_of stretches every churn duration by however old the export is,
    which bends the whole survival curve.
    """
    df = _frame([{
        "completion_status": "not_coming",
        "Date of Joining": "2025-01-01",
        "Course Duration (IN DAYS)": 90,      # ends 2025-04-01, deadline 2025-10-01
    }])
    res = lc.churn_labels(df, ROLES, as_of="2026-06-01")
    expected = (pd.Timestamp("2025-10-01") - pd.Timestamp("2025-01-01")).days
    assert int(res["columns"]["tenure_days"].iloc[0]) == expected == 273

    # A censored row is measured to as_of, because that is all we know.
    still = _frame([{
        "completion_status": "active",
        "Date of Joining": "2025-01-01",
        "Course Duration (IN DAYS)": 90,
    }])
    res2 = lc.churn_labels(still, ROLES, as_of="2026-06-01")
    assert int(res2["columns"]["tenure_days"].iloc[0]) == (
        pd.Timestamp("2026-06-01") - pd.Timestamp("2025-01-01")).days


def test_duration_fills_from_the_courses_own_recorded_length() -> None:
    """`Course Duration (IN DAYS)` is on the MAIN timetable sheet only.

    Which is exactly backwards for churn: the rows that need a course end are
    the ones that left, and those sit in Not_Coming and NOT TO ENTERTRAIN,
    where the column does not exist. Filling from the median recorded for the
    same course keeps the number the institute's own rather than an assumption.
    """
    df = _frame([
        # Main sheet rows carry the length.
        {"completion_status": "active", "Date of Joining": "2025-01-01",
         "Which Course": "python programming", "Course Duration (IN DAYS)": 90},
        {"completion_status": "active", "Date of Joining": "2025-01-01",
         "Which Course": "python programming", "Course Duration (IN DAYS)": 90},
        {"completion_status": "active", "Date of Joining": "2025-01-01",
         "Which Course": "advanced excel", "Course Duration (IN DAYS)": 360},
        # Leavers do not — same courses, blank column.
        {"completion_status": "not_coming", "Date of Joining": "2025-01-01",
         "Which Course": "python programming", "Course Duration (IN DAYS)": None},
        {"completion_status": "not_coming", "Date of Joining": "2025-01-01",
         "Which Course": "advanced excel", "Course Duration (IN DAYS)": None},
        # A course nobody on the main sheet takes: nothing to borrow.
        {"completion_status": "not_coming", "Date of Joining": "2025-01-01",
         "Which Course": "tally", "Course Duration (IN DAYS)": None},
    ])
    roles = dict(ROLES, course="Which Course")

    res = lc.churn_labels(df, roles, as_of="2025-10-02")
    statuses = list(res["columns"]["churn_status"])
    # python ends 2025-04-01, grace to 2025-10-01 -> churned by 10-02.
    assert statuses[3] == "churned"
    # excel runs 360 days, so it has not even finished yet.
    assert statuses[4] == "paused"
    # No recorded length anywhere for tally: unlabelled, never defaulted.
    assert statuses[5] == "unlabelled"

    assert list(res["columns"]["churn_basis"])[3] == "course_median"
    assert res["summary"]["duration_basis"]["course_median"] == 2


def test_category_duration_fallback_is_opt_in_and_recorded() -> None:
    """No duration column? Only the operator's own table fills the gap.

    The module never guesses how long a course runs — a wrong default silently
    moves the churn date for every student on that course.
    """
    df = _frame([
        {"completion_status": "not_coming", "Date of Joining": "2024-01-01",
         "course category": "advanced certificate course"},
        {"completion_status": "not_coming", "Date of Joining": "2024-01-01",
         "course category": "short course"},
    ])
    roles = {"joining_date": "Date of Joining", "student_category": "course category"}

    # Without a table: nothing is labelled.
    bare = lc.churn_labels(df, roles, as_of="2024-10-01")
    assert set(bare["columns"]["churn_status"]) == {"unlabelled"}

    # With one: 360-day course still inside its window, 60-day course past it.
    table = lc.duration_table_from_months(
        {"advanced certificate course": 12, "short course": 2}
    )
    assert table["short course"] == 60.0
    res = lc.churn_labels(df, roles, as_of="2024-10-01",
                          duration_days_by_category=table)
    assert list(res["columns"]["churn_status"]) == ["paused", "churned"]
    assert set(res["summary"]["duration_basis"]) == {
        "category_default:student_category"
    }


def test_duration_column_wins_over_the_category_table() -> None:
    """A recorded length beats an assumed one, row by row, not sheet by sheet."""
    df = _frame([
        {"completion_status": "not_coming", "Date of Joining": "2024-01-01",
         "course category": "short course", "Course Duration (IN DAYS)": 360},
        {"completion_status": "not_coming", "Date of Joining": "2024-01-01",
         "course category": "short course", "Course Duration (IN DAYS)": None},
    ])
    roles = dict(ROLES, student_category="course category")
    res = lc.churn_labels(df, roles, as_of="2024-10-01",
                          duration_days_by_category={"short course": 60})
    assert list(res["columns"]["churn_status"]) == ["paused", "churned"]
    assert list(res["columns"]["churn_basis"]) == [
        "duration_column", "category_default:student_category"
    ]


def test_active_and_completed_are_not_in_the_churn_denominator() -> None:
    """They were never at risk under this rule. Including them dilutes it."""
    df = _frame([
        {"completion_status": "not_coming", "Date of Joining": "2024-01-01",
         "Course Duration (IN DAYS)": 90},
        {"completion_status": "active", "Date of Joining": "2024-01-01",
         "Course Duration (IN DAYS)": 90},
        {"completion_status": "completed", "Date of Joining": "2024-01-01",
         "Course Duration (IN DAYS)": 90},
    ])
    res = lc.churn_labels(df, ROLES, as_of="2026-01-01")
    assert res["summary"]["labelled_rows"] == 1
    assert res["summary"]["churn_rate_of_at_risk"] == 1.0
    assert list(res["columns"]["is_churn"]) == [True, False, False]


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
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run())
