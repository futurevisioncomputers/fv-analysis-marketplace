"""The CLI contract the skills read.

Every skill is a markdown file that shells out to `scripts/run_stage.py --json`
and renders whatever comes back. Markdown cannot be type-checked, so a renamed
key or a changed exit code breaks the skill silently — the model improvises
over the missing field and the operator gets a confident, wrong answer.

These tests pin the parts the skills actually index into: the JSON envelope
shape, the checkpoint fields, exit codes, prerequisite back-fill, and the
invalidation that happens when a session is pointed at different data.

Run: python -m tests.test_run_stage_cli   (plain asserts, no pytest dep)
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from scripts import run_stage                                   # noqa: E402

SOURCE = os.path.join(ROOT, "samples", "admission_form__form_responses_1.csv")
OTHER = os.path.join(ROOT, "samples", "student_data_sheet__fees_data.csv")
QUESTION = "How is admission conversion performing by branch?"

# What a skill reads off the envelope and off one checkpoint. Named here so a
# rename fails loudly rather than in a skill nobody is testing.
ENVELOPE_KEYS = {"session", "ran", "checkpoint", "blocked", "progress"}
CHECKPOINT_KEYS = {"stage", "n", "label", "status", "optional", "summary",
                   "details", "artifacts", "offers", "next_stage", "next_label"}


def _cli(*argv) -> tuple:
    """Run the CLI in-process. Returns (exit_code, parsed_json_or_None, text)."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        code = run_stage.main(list(argv))
    text = buffer.getvalue()
    try:
        return code, json.loads(text), text
    except json.JSONDecodeError:
        return code, None, text


def test_json_envelope_is_stable_whatever_ran() -> None:
    """One shape whether the call ran one stage or back-filled three."""
    work = tempfile.mkdtemp(prefix="fv-cli-")
    session = os.path.join(work, "s")
    try:
        code, payload, text = _cli("--session", session, "--csv", SOURCE,
                                   "--question", QUESTION, "--max-questions", "2",
                                   "--stage", "clean", "--json")
        assert code == 0, text
        assert ENVELOPE_KEYS <= set(payload), sorted(payload)

        # Called on a bare CSV, cleaning cannot run alone.
        assert [p["stage"] for p in payload["ran"]] == ["problem", "schema", "clean"]
        assert payload["blocked"] is False
        point = payload["checkpoint"]
        assert point["stage"] == "clean", "checkpoint must be the stage asked for"
        assert CHECKPOINT_KEYS <= set(point), sorted(point)
        assert point["status"] == "done"
        assert point["summary"] and point["details"]

        # A download is only offered for a file that exists on disk.
        for offer in point["offers"]:
            if offer.get("artifact"):
                assert offer.get("path") and os.path.exists(offer["path"]), offer
        assert "cleaned.csv" in point["artifacts"]

        # A second call runs exactly one stage, and the envelope looks the same.
        code, payload, text = _cli("--session", session, "--stage", "eda", "--json")
        assert code == 0, text
        assert ENVELOPE_KEYS <= set(payload)
        assert [p["stage"] for p in payload["ran"]] == ["eda"]
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_blocked_stage_exits_2_and_says_why() -> None:
    """The skill relays this reason verbatim, so it has to be in the payload."""
    work = tempfile.mkdtemp(prefix="fv-cli-blocked-")
    try:
        unusable = os.path.join(work, "unusable.csv")
        with open(unusable, "w", encoding="utf-8", newline="") as fh:
            fh.write("Name,Course\n,\n,\n")

        code, payload, text = _cli("--session", os.path.join(work, "s"),
                                   "--csv", unusable, "--question", QUESTION,
                                   "--stage", "clean", "--json")
        assert code == run_stage.EXIT_BLOCKED, f"exit {code}: {text}"
        assert payload["blocked"] is True
        assert payload["reason"], "a refusal with no reason is unusable"
        assert payload["checkpoint"]["status"] == "blocked"
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_changing_the_source_discards_the_old_results() -> None:
    """Two sheets in one session must never end up in one report."""
    work = tempfile.mkdtemp(prefix="fv-cli-source-")
    session = os.path.join(work, "s")
    try:
        code, payload, text = _cli("--session", session, "--csv", SOURCE,
                                   "--question", QUESTION, "--stage", "clean",
                                   "--json")
        assert code == 0, text
        first = payload["checkpoint"]["summary"]

        code, payload, text = _cli("--session", session, "--csv", OTHER,
                                   "--stage", "clean", "--json")
        assert code == 0, text
        # Everything computed from the first sheet was discarded and rerun.
        assert [p["stage"] for p in payload["ran"]] == ["problem", "schema", "clean"]
        assert payload["checkpoint"]["summary"] != first
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_status_and_show_do_not_run_anything() -> None:
    """Looking at a session must be free of side effects."""
    work = tempfile.mkdtemp(prefix="fv-cli-status-")
    session = os.path.join(work, "s")
    try:
        _cli("--session", session, "--csv", SOURCE, "--question", QUESTION,
             "--stage", "problem", "--json")

        code, payload, text = _cli("--session", session, "--status", "--json")
        assert code == 0, text
        assert payload["next_stage"] == "schema"
        done = [row for row in payload["progress"] if row["status"] == "done"]
        assert [row["key"] for row in done] == ["problem"]

        code, payload, text = _cli("--session", session, "--show", "problem", "--json")
        assert code == 0, text
        assert payload["checkpoint"]["stage"] == "problem"

        # Still exactly one stage done — neither call executed anything.
        code, payload, _ = _cli("--session", session, "--status", "--json")
        assert len([r for r in payload["progress"] if r["status"] == "done"]) == 1
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
