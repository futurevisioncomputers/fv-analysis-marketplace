"""The lifecycle read from a COLUMN, not only from which sheet a row sat in.

The institute's original timetable workbook split students across four tabs,
so `_derive_completion_status` read the source name. Once those tabs are
consolidated onto a student master the label is an ordinary column — and a run
on that master reported:

    Dropout Rate 0.4%   health "excellent"

while 25 students sat in `status = Not Coming`. The 0.4% came from
`is_cancelled`, which only ever sees a cancellation typed INTO A NAME
("Ritik Shah (admission cancelled)"). It was measuring a typing convention.

Run: python -m tests.test_lifecycle_column   (plain asserts, no pytest dep)
"""

from __future__ import annotations

import sys

import pandas as pd

from agents.data_engineer_agent import DataEngineerAgent


def _master() -> pd.DataFrame:
    """The restructured shape: one row per student, lifecycle in a column."""
    return pd.DataFrame({
        "student_id": [1, 2, 3, 4, 5, 6],
        "name": ["a", "b", "c", "d", "e", "f"],
        "status": ["Course Completed", "Course Completed", "Currently Learning",
                   "Not Coming", "Unknown", "Not To Entertrain"],
        "fee_status": ["Full Paid", "Pending", "Pending", "Pending",
                       "Full Paid", "Pending"],
    })


def test_the_lifecycle_is_read_from_the_column() -> None:
    df, issues = _master(), []
    assert DataEngineerAgent._derive_completion_from_column(df, issues) is True
    assert list(df["completion_status"]) == [
        "completed", "completed", "active", "not_coming", None,
        "not_to_entertain"]
    assert list(df["is_completed"]) == [True, True, False, False, False, False]
    assert list(df["is_not_coming"]) == [False, False, False, True, False, False]
    assert any("per row from column 'status'" in i for i in issues), issues


def test_unknown_labels_nothing_rather_than_counting_as_active() -> None:
    """698 of this institute's 1,536 students have no recorded status.

    Reading "we don't know" as "still learning" would put people who left
    years ago into the active denominator and make completion look worse and
    retention look better, both at once.
    """
    df = _master()
    DataEngineerAgent._derive_completion_from_column(df, [])
    assert df["completion_status"].iloc[4] is None
    assert not df["is_completed"].iloc[4]
    assert not df["is_not_coming"].iloc[4]


def test_detection_is_by_values_not_by_header() -> None:
    """`status` means the fee state on one of the institute's sheets and the
    lifecycle on another, so a header match picks wrong about half the time."""
    df = _master().rename(columns={"status": "current_position"})
    assert DataEngineerAgent._derive_completion_from_column(df, []) is True
    assert list(df["is_completed"])[:2] == [True, True]

    # And the fee column must never be mistaken for it.
    fee_only = pd.DataFrame({"status": ["Full Paid", "Pending", "Pending"]})
    assert DataEngineerAgent._derive_completion_from_column(fee_only, []) is False


def test_a_single_state_column_is_not_a_lifecycle() -> None:
    """All-one-value labels nothing and is more likely a coincidence."""
    df = pd.DataFrame({"status": ["Active"] * 20})
    assert DataEngineerAgent._derive_completion_from_column(df, []) is False


def test_dropout_counts_leavers_not_just_typed_cancellations() -> None:
    """The bug: 0.4% dropout while 25 students sat in Not Coming."""
    df = _master()
    DataEngineerAgent._derive_completion_from_column(df, [])
    assert "is_dropped" in df.columns
    # The Not Coming student is a dropout; the Unknown one is not asserted to be.
    assert list(df["is_dropped"]) == [False, False, False, True, False, False]


def test_both_signals_union_when_both_exist() -> None:
    """A sheet can carry a typed cancellation AND a lifecycle column."""
    df = _master()
    df["is_cancelled"] = [False, True, False, False, False, False]
    DataEngineerAgent._derive_completion_from_column(df, [])
    assert list(df["is_dropped"]) == [False, True, False, True, False, False]


def test_the_column_beats_sheet_membership() -> None:
    """A per-row label is strictly better than one label for a whole sheet.

    The old path would have stamped every row of a sheet named
    'course_completed' as completed, including the ones its own column says
    are Not Coming.
    """
    agent = DataEngineerAgent(output_dir="output")
    df, issues = _master(), []
    agent._derive_completion_status(df, "student_timetable__course_completed", issues)
    assert list(df["is_completed"]) == [True, True, False, False, False, False]
    assert not any("from source sheet name" in i for i in issues), issues


def test_sheet_membership_still_works_without_a_column() -> None:
    """The four-tab workbook must keep behaving exactly as before."""
    agent = DataEngineerAgent(output_dir="output")
    df = pd.DataFrame({"name": ["a", "b"], "course": ["python", "excel"]})
    issues: list = []
    agent._derive_completion_status(df, "student_timetable__not_coming", issues)
    assert list(df["is_not_coming"]) == [True, True]
    assert list(df["is_dropped"]) == [True, True]
    assert any("from source sheet name" in i for i in issues), issues


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
