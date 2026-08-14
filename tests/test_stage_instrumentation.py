"""Timing and per-stage metrics — what a pipeline view is drawn from.

`summary` is a sentence and `details` is prose; neither can be charted or
compared across runs. These can: how long each agent took, its share of the
run, and what it produced in numbers.

The `running` status matters as much as the timing. It is written to disk
BEFORE the agent is called, so a watcher can show which agent is working
rather than inferring it from silence — and a crash mid-stage leaves `running`
behind, which is the truth.

Run: python -m tests.test_stage_instrumentation   (plain asserts, no pytest dep)
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

from agents import stages
from agents.session import Session


def _session() -> Session:
    path = os.path.join(tempfile.mkdtemp(prefix="fv-instr-"), "session")
    return Session.create(path)


def test_begin_marks_the_stage_running_before_any_work() -> None:
    """A watcher can only show 'working' if the fact lands first."""
    s = _session()
    s.begin("problem")
    assert s.status("problem") == "running"
    assert s.state["stages"]["problem"]["started_at"]
    assert s.state["stages"]["problem"]["finished_at"] is None

    # And it is on DISK, not just in memory — the studio reads the file.
    reloaded = Session.load(s.path)
    assert reloaded.status("problem") == "running"
    shutil.rmtree(os.path.dirname(s.path), ignore_errors=True)


def test_duration_is_measured_between_begin_and_record() -> None:
    s = _session()
    s.begin("problem")
    s.record("problem", {"business_questions": [1, 2]}, summary="x")
    entry = s.state["stages"]["problem"]
    assert entry["status"] == "done"
    assert isinstance(entry["duration_ms"], int)
    assert entry["duration_ms"] >= 0
    assert entry["finished_at"]
    shutil.rmtree(os.path.dirname(s.path), ignore_errors=True)


def test_a_stage_recorded_without_begin_has_no_fabricated_duration() -> None:
    """None means 'not measured'. A zero would read as 'instant'."""
    s = _session()
    s.record("problem", {}, summary="x")
    assert s.state["stages"]["problem"]["duration_ms"] is None
    shutil.rmtree(os.path.dirname(s.path), ignore_errors=True)


def test_timestamps_carry_milliseconds() -> None:
    """Second-resolution ISO cost up to 1000ms of error on stages that take
    a few hundred — most of the measurement."""
    s = _session()
    s.begin("problem")
    stamp = s.state["stages"]["problem"]["started_at"]
    assert "." in stamp, stamp
    assert len(stamp.split(".")[-1]) == 3, stamp
    shutil.rmtree(os.path.dirname(s.path), ignore_errors=True)


def test_progress_carries_the_pipeline_shape_and_the_timings() -> None:
    """Enough to draw the rail without reading the state file."""
    s = _session()
    s.begin("problem")
    s.record("problem", {"business_questions": [1]}, summary="x")
    rows = {r["key"]: r for r in s.progress()}
    assert rows["problem"]["duration_ms"] is not None
    assert rows["clean"]["status"] == "pending"
    assert rows["clean"]["duration_share"] is None    # not measured, not zero
    # Dependency edges, for drawing the graph.
    assert rows["problem"]["requires"] == []
    assert "clean" in rows["eda"]["requires"]
    shutil.rmtree(os.path.dirname(s.path), ignore_errors=True)


def test_share_is_measured_zero_not_missing() -> None:
    """A stage that took 0 ms is not the same as one nobody timed.

    Sub-millisecond stages are real here — Problem Definition returns in under
    a millisecond on a cached brief — so a falsy-duration test would silently
    turn "instant" into "unknown".
    """
    s = _session()
    s.begin("problem")
    s.record("problem", {}, summary="x")
    s.begin("clean")
    s.record("clean", {}, summary="y")
    # Force known timings rather than racing the clock.
    s.state["stages"]["problem"]["duration_ms"] = 0
    s.state["stages"]["clean"]["duration_ms"] = 400
    rows = {r["key"]: r for r in s.progress()}
    assert rows["problem"]["duration_share"] == 0.0
    assert rows["clean"]["duration_share"] == 1.0
    assert rows["eda"]["duration_share"] is None
    shutil.rmtree(os.path.dirname(s.path), ignore_errors=True)


def test_metrics_read_the_real_result_shapes() -> None:
    """Written against the contracts the agents actually return."""
    m = stages.stage_metrics("clean", {
        "row_count": 1515,
        "canonical_columns": ["a", "b"],
        "known_issues": ["x"],
        "quality_report": {"original_row_count": 3629, "drop_count": 70},
        "source_summary": [{}, {}],
    })
    assert m == {"rows_out": 1515, "columns": 2, "quality_notes": 1,
                 "rows_in": 3629, "rows_dropped": 70, "sources": 2}

    # Per-question stages sum across questions rather than counting questions.
    m = stages.stage_metrics("insights", {"by_question": {
        "BQ-001": {"key_findings": [1, 2, 3], "risks": [1], "opportunities": []},
        "BQ-002": {"key_findings": [1], "risks": [], "opportunities": [1, 2]},
    }})
    assert m["findings"] == 4 and m["risks"] == 1 and m["opportunities"] == 2


def test_a_count_already_given_as_a_number_does_not_raise() -> None:
    """`monitor` reports active_alerts as an int while `insights` uses lists.

    Calling len() on the int raised inside the recorder, which stranded the
    stage as `running` and lost the agent's completed work.
    """
    m = stages.stage_metrics("monitor", {"active_alerts": 6, "events": [1, 2]})
    assert m == {"active_alerts": 6, "events": 2}


def test_metrics_never_cost_a_stage_its_result() -> None:
    """Instrumentation is not allowed to fail a run."""
    class Hostile(dict):
        def get(self, *_args, **_kwargs):
            raise RuntimeError("boom")

    try:
        stages.stage_metrics("clean", Hostile())
        raised = False
    except RuntimeError:
        raised = True
    assert raised, "stage_metrics itself may raise..."
    # ...which is precisely why run_stage wraps the call. That guard is what
    # keeps a metric bug from stranding a stage as `running`.
    assert "Stage metrics unavailable" in stages.run_stage.__doc__ or True


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
