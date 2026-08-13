"""Feature Engineering: proposals, banding rules, and the approval gate.

Two properties matter more than the individual bands.

**Nothing materializes unapproved.** A feature the operator has not seen must
not appear in the parquet, because every one of these is a policy choice —
"short course" is a boundary someone picked, not a fact — and an unreviewed
boundary in a report is a number nobody can defend.

**A proposal must be honest about what it needs.** Offering `age_band` on a
sheet with no date of birth, or on one where DOB is 10% populated, produces a
column that is mostly blank and looks like signal.

Run: python -m tests.test_feature_engineering   (plain asserts, no pytest dep)
"""

from __future__ import annotations

import datetime as dt
import os
import shutil
import sys
import tempfile

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agents import stages                                          # noqa: E402
from agents.feature_engineering_agent import (FeatureEngineeringAgent,  # noqa: E402
                                              MIN_COVERAGE)
from agents.session import Session                                 # noqa: E402
from scripts.run_pipeline import wrap_goal                         # noqa: E402

AS_OF = dt.date(2026, 8, 12)
SOURCE = os.path.join(ROOT, "samples", "student_data_sheet__fees_data.csv")


def _package(df: pd.DataFrame, roles: dict) -> dict:
    return {"status": "ready", "canonical_columns": roles, "row_count": len(df)}


def test_bands_are_inclusive_and_ordered() -> None:
    """A duration of exactly 90 or 91 days must land where the rule says."""
    agent = FeatureEngineeringAgent()
    df = pd.DataFrame({"Course Duration (IN DAYS)": [30, 90, 91, 180, 181, 400,
                                                     None]})
    proposals = agent.propose(
        _package(df, {"course_duration": "Course Duration (IN DAYS)"}),
        df=df, as_of=AS_OF)
    group = next(f for f in proposals["features"] if f["id"] == "duration_group")

    built = list(group["build"](df))
    assert built[:6] == ["Short", "Short", "Medium", "Medium", "Long", "Long"]
    assert pd.isna(built[6]), "a missing duration must stay missing, not band"


def test_overpayment_is_not_a_debt_band() -> None:
    """A negative balance is money returned, not the smallest amount owed."""
    agent = FeatureEngineeringAgent()
    df = pd.DataFrame({"Amt Pending": [-500, 0, 3000, 9000, 40000]})
    proposals = agent.propose(_package(df, {"pending": "Amt Pending"}),
                              df=df, as_of=AS_OF)
    bucket = next(f for f in proposals["features"] if f["id"] == "outstanding_bucket")
    assert list(bucket["build"](df)) == [
        "Overpaid", "Nil", "1-5000", "5001-15000", "15001+"]


def test_batch_times_read_as_the_institute_writes_them() -> None:
    """"02:00 To 03:00" is an afternoon class — nobody runs a 2am batch."""
    agent = FeatureEngineeringAgent()
    df = pd.DataFrame({"Batch Timing": ["08:00 To 09:00", "02:00 To 03:00",
                                        "06:00 To 07:00", "11:00 To 12:00"]})
    proposals = agent.propose(_package(df, {"batch_time": "Batch Timing"}),
                              df=df, as_of=AS_OF)
    slot = next(f for f in proposals["features"] if f["id"] == "batch_slot")
    assert list(slot["build"](df)) == ["Morning", "Afternoon", "Evening", "Morning"]


def test_a_sparse_column_is_refused_with_its_coverage() -> None:
    """A feature that is mostly blank is noise wearing the shape of signal."""
    agent = FeatureEngineeringAgent()
    df = pd.DataFrame({"Date of Birth": ["01/01/2000"] + [None] * 9,
                       "Date of Joining": ["01/03/2025"] * 10})
    proposals = agent.propose(
        _package(df, {"dob": "Date of Birth", "joining_date": "Date of Joining"}),
        df=df, as_of=AS_OF)

    ids = [f["id"] for f in proposals["features"]]
    assert "age_band" not in ids
    refusal = next(s for s in proposals["skipped"] if s["id"] == "age_band")
    assert "10%" in refusal["reason"], refusal
    assert f"{MIN_COVERAGE:.0%}" in refusal["reason"], refusal


def test_missing_source_says_which_sheet_supplies_it() -> None:
    agent = FeatureEngineeringAgent()
    df = pd.DataFrame({"Student Name": ["asha"], "Date of Joining": ["01/03/2025"]})
    proposals = agent.propose(_package(df, {"name": "Student Name",
                                            "joining_date": "Date of Joining"}),
                              df=df, as_of=AS_OF)
    reasons = {s["id"]: s["reason"] for s in proposals["skipped"]}
    assert "timetable" in reasons["is_ending_soon"]
    assert "fees-data" in reasons["outstanding_bucket"]


def test_thresholds_are_configurable_and_recorded() -> None:
    """"Short course" is a policy, so the boundary that ran must be traceable."""
    agent = FeatureEngineeringAgent(
        {"duration_groups": [("Short", 0, 30), ("Long", 31, None)]})
    df = pd.DataFrame({"Course Duration (IN DAYS)": [20, 60]})
    proposals = agent.propose(
        _package(df, {"course_duration": "Course Duration (IN DAYS)"}),
        df=df, as_of=AS_OF)

    group = next(f for f in proposals["features"] if f["id"] == "duration_group")
    assert list(group["build"](df)) == ["Short", "Long"]
    assert proposals["config"]["duration_groups"][0] == ("Short", 0, 30)
    assert proposals["as_of"] == AS_OF.isoformat()


def test_nothing_is_built_until_it_is_approved() -> None:
    """The approval gate, end to end through the stage runner."""
    work = tempfile.mkdtemp(prefix="fv-features-")
    try:
        session = Session.create(os.path.join(work, "s"), csv_path=SOURCE,
                                 question="How much fee is pending by branch?")
        session.state["goal"] = wrap_goal("How much fee is pending by branch?")
        session.save()
        for key in ("problem", "schema", "clean"):
            stages.run_stage(session, key)

        # First pass: proposals only.
        stages.run_stage(session, "features")
        result = session.result("features")
        assert result["awaiting_approval"] is True
        assert result["proposed"], "nothing proposed on a fee sheet"
        assert result["added"] == []
        assert session.get_artifact("features.parquet") is None

        frame = pd.read_parquet(session.get_artifact("canonical.parquet"))
        assert "outstanding_bucket" not in frame.columns, (
            "an unapproved feature reached the frame")

        # Second pass: approve one of them.
        session.state["approved_features"] = ["outstanding_bucket"]
        session.save()
        stages.run_stage(session, "features")
        result = session.result("features")

        assert [f["id"] for f in result["added"]] == ["outstanding_bucket"]
        assert "collection_band" in result["declined"]

        frame = pd.read_parquet(session.get_artifact("canonical.parquet"))
        assert "outstanding_bucket" in frame.columns
        # A declined feature is genuinely absent, not hidden.
        assert "collection_band" not in frame.columns

        # And it is registered as a role, or no later stage could use it.
        roles = session.result("clean")["canonical_columns"]
        assert roles.get("outstanding_bucket") == "outstanding_bucket"
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_approving_invalidates_everything_downstream() -> None:
    """The frame changed, so results computed from the old one must not stand."""
    work = tempfile.mkdtemp(prefix="fv-features-cascade-")
    try:
        session = Session.create(os.path.join(work, "s"), csv_path=SOURCE,
                                 question="How much fee is pending by branch?")
        session.state["goal"] = wrap_goal("How much fee is pending by branch?")
        session.state["max_questions"] = 1
        session.save()
        for key in ("problem", "schema", "clean", "features", "eda", "analyst"):
            stages.run_stage(session, key)
        assert session.is_done("analyst")

        cleared = session.reset_from("features")
        assert "eda" in cleared and "analyst" in cleared
        assert not session.is_done("analyst")
    finally:
        shutil.rmtree(work, ignore_errors=True)


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
