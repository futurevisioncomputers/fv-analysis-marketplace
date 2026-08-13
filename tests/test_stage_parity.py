"""Parity gate: `OrchestratorAgent.run` must equal a stage-by-stage walk.

`agents/stages.py` re-expresses the pipeline as independent stages so it can
pause for an operator between them. The transposition was *supposed* to be
behaviour-preserving — no agent carries state between questions, so running
Analyst for every question and then Visualization for every question produces
the same numbers as interleaving them per question. This test pinned that while
the two implementations still existed side by side, and it passed field for
field against the 26k-row dataset before the orchestrator was rewritten.

The orchestrator is now a thin driver over the same registry, so what this
guards has shifted: it is the **contract between the two entry points**. One
walks the stages itself; the other calls `OrchestratorAgent.run`. They must
still produce the same report, and the orchestrator must still assemble the
full legacy state shape the CLI and web service read. A change that satisfies
the stage registry but breaks the assembled state — a renamed key, a stage
result that never reaches `question_results` — fails here rather than in a
caller.

Uses a small synthetic sample rather than the 26k-row dataset so it runs in
seconds and can stay in the normal test loop.

Run: python -m tests.test_stage_parity   (plain asserts, no pytest dep)
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agents import stages                                    # noqa: E402
from agents.orchestrator_agent import OrchestratorAgent      # noqa: E402
from agents.session import OPTIONAL_STAGES, STAGE_KEYS, Session  # noqa: E402
from scripts.run_pipeline import wrap_goal                   # noqa: E402

# The single-sheet admission export — the shape most runs actually use.
SOURCE = os.path.join(ROOT, "samples", "admission_form__form_responses_1.csv")
QUESTION = "How is admission conversion performing by branch?"
MAX_QUESTIONS = 3

# Fields that must agree exactly between the two execution shapes.
SCALAR_FIELDS = ("questions_answered", "questions_skipped")
STRUCTURED_FIELDS = ("headline_findings", "top_recommendations", "skipped")


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _run_question_major(work_dir: str) -> dict:
    # Monitoring thresholds persist across runs by design, and the default
    # registry lives beside the source CSV. Left shared, every previous run on
    # this sample adds hooks and the alert count drifts upward, so the run gets
    # its own registry inside the temp directory.
    state = OrchestratorAgent().run(
        wrap_goal(QUESTION), SOURCE, max_questions=MAX_QUESTIONS,
        registry_path=os.path.join(work_dir, "registry-legacy.json"))
    assert state.get("status") == "complete", (
        f"question-major run did not complete: {state.get('message')}")
    return state["final_report"]


def _run_stage_major(work_dir: str) -> dict:
    session = Session.create(os.path.join(work_dir, "parity"),
                             question=QUESTION, csv_path=SOURCE)
    session.state["goal"] = wrap_goal(QUESTION)
    session.state["max_questions"] = MAX_QUESTIONS
    session.save()
    for key in STAGE_KEYS:
        if key in OPTIONAL_STAGES:
            continue
        stages.run_stage(session, key)
    return session.state["final_report"]


def test_orchestrator_matches_a_stage_by_stage_walk() -> None:
    work_dir = tempfile.mkdtemp(prefix="fv-parity-")
    try:
        old = _run_question_major(work_dir)
        new = _run_stage_major(work_dir)

        for field in SCALAR_FIELDS:
            assert old[field] == new[field], (
                f"{field}: question-major={old[field]} stage-major={new[field]}")

        for field in STRUCTURED_FIELDS:
            assert _canonical(old.get(field)) == _canonical(new.get(field)), (
                f"{field} differs between execution shapes")

        assert old["data_quality"]["row_count"] == new["data_quality"]["row_count"]

        old_mon, new_mon = old.get("monitoring", {}), new.get("monitoring", {})
        for field in ("health", "active_alerts"):
            assert old_mon.get(field) == new_mon.get(field), (
                f"monitoring.{field}: {old_mon.get(field)} != {new_mon.get(field)}")
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def test_orchestrator_assembles_the_full_legacy_state() -> None:
    """The driver must rebuild every field its callers read, not just the report.

    `run_pipeline.py` and the web service index into `brief`, `data_package`,
    `eda_report`, `question_results`, `monitoring` and `report` directly. A
    stage result that never gets copied out of the session would leave one of
    them silently None.
    """
    work_dir = tempfile.mkdtemp(prefix="fv-state-")
    try:
        seen = []
        state = OrchestratorAgent().run(
            wrap_goal(QUESTION), SOURCE, max_questions=MAX_QUESTIONS,
            registry_path=os.path.join(work_dir, "registry.json"),
            session_dir=os.path.join(work_dir, "session"),
            on_stage=lambda n, name, status, summary: seen.append((n, status)))

        assert state["status"] == "complete", state.get("message")
        for field in ("brief", "data_package", "eda_report", "question_results",
                      "monitoring", "final_report", "report"):
            assert state.get(field), f"{field} was not assembled from the session"
        assert state["report"]["html"], "report HTML did not reach the state"

        # Progress must stream as the run goes, one emission per stage, in
        # rail order — the UI draws from these and cannot reorder them.
        assert [n for n, _ in seen] == [
            "1", "2.5", "2", "3", "4", "5", "6", "6.5", "7", "8"], seen
        assert all(status in ("done", "skipped") for _, status in seen), seen

        # The session it ran in is resumable: state survived on disk.
        resumed = Session.load(state["session_dir"])
        assert resumed.next_stage() is None, "a stage was left unrecorded"
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def test_every_registry_stage_is_declared() -> None:
    """A runner with no session entry (or vice versa) would silently never run."""
    undeclared = sorted(set(stages.RUNNERS) - set(STAGE_KEYS))
    assert not undeclared, f"runners missing from STAGES: {undeclared}"

    # `features` is allowed to lag: it is declared optional until its agent
    # lands, so `next_stage()` skips it instead of stalling the run.
    unrunnable = [k for k in STAGE_KEYS
                  if k not in stages.RUNNERS and k not in OPTIONAL_STAGES]
    assert not unrunnable, (
        f"stages with no runner that are not optional: {unrunnable}")


def test_session_resume_and_cascade_reset() -> None:
    """Resuming must not recompute, and changing an answer must invalidate."""
    work_dir = tempfile.mkdtemp(prefix="fv-session-")
    try:
        session = Session.create(os.path.join(work_dir, "s"),
                                 question=QUESTION, csv_path=SOURCE)
        session.state["goal"] = wrap_goal(QUESTION)
        session.save()

        assert session.missing_prereqs("eda") == ["problem", "schema", "clean"]
        stages.run_stage(session, "problem")
        assert session.missing_prereqs("eda") == ["schema", "clean"]

        # State survives the process boundary — each stage is its own run.
        reloaded = Session.load(session.path)
        assert reloaded.is_done("problem")
        assert reloaded.next_stage() == "schema"

        # Changing an upstream answer must discard everything downstream,
        # rather than leaving results computed against different inputs.
        cleared = reloaded.reset_from("problem")
        assert "problem" in cleared
        assert reloaded.next_stage() == "problem"
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


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
