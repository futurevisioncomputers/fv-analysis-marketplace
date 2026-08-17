"""Reading session.json while another thread writes it.

The CLI runs stages front to back in one thread and never exercises this. The
web service does: it answers a status poll on an HTTP thread while a worker
thread is part-way through `save()`.

On Windows a file caught mid-`os.replace` is refused with PermissionError —
ERROR_ACCESS_DENIED, not anything that reads like "try again". That surfaced
twice as a bug that looked nothing like a race:

  * a 500 on `GET /api/runs/<id>` for a run that was working perfectly, and
  * a stage left `pending` on disk, because the PermissionError was raised
    inside `save()` and aborted the record the agent had just produced.

The write was already atomic. The loser of the race only has to wait for the
swap to land, which is what these tests pin down.

Run: python -m tests.test_session_concurrency   (plain asserts, no pytest dep)
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import time

from agents.session import Session

# Enough passes to lose the race on any machine. The window is sub-millisecond,
# so a handful of iterations proves nothing.
ROUNDS = 400


def _session() -> Session:
    path = os.path.join(tempfile.mkdtemp(prefix="fv-conc-"), "session")
    return Session.create(path)


def test_a_reader_never_sees_a_half_replaced_file() -> None:
    """The exact failure: PermissionError on open, mid-swap."""
    writer_session = _session()
    errors: list = []
    stop = threading.Event()

    def write() -> None:
        try:
            for i in range(ROUNDS):
                writer_session.state["question"] = f"round {i}"
                writer_session.save()
        except Exception as exc:  # noqa: BLE001
            errors.append(("writer", exc))
        finally:
            stop.set()

    def read() -> None:
        try:
            while not stop.is_set():
                loaded = Session.load(writer_session.path)
                # Never a truncated mix: whatever is read, it parsed, and it
                # carries the keys a caller reads without checking.
                assert "stages" in loaded.state
                assert "question" in loaded.state
        except Exception as exc:  # noqa: BLE001
            errors.append(("reader", exc))

    readers = [threading.Thread(target=read) for _ in range(3)]
    writer = threading.Thread(target=write)
    for t in readers:
        t.start()
    writer.start()
    writer.join(timeout=60)
    for t in readers:
        t.join(timeout=10)

    assert not errors, errors[:3]


def test_a_stage_record_survives_a_concurrent_reader() -> None:
    """The costlier half: a refused swap loses the agent's result.

    A reader holding the target open can make `os.replace` fail. If that
    escapes `save()`, the stage that just finished is never recorded and the
    run reports it as `pending` — work done, nothing to show for it.
    """
    session = _session()
    stop = threading.Event()
    errors: list = []

    def read() -> None:
        while not stop.is_set():
            try:
                Session.load(session.path)
            except Exception as exc:  # noqa: BLE001
                errors.append(("reader", exc))
                return

    readers = [threading.Thread(target=read) for _ in range(3)]
    for t in readers:
        t.start()
    try:
        for i in range(ROUNDS):
            session.begin("problem")
            session.record("problem", {"summary": f"round {i}"},
                           summary=f"round {i}")
            assert session.status("problem") == "done"
    finally:
        stop.set()
        for t in readers:
            t.join(timeout=10)

    assert not errors, errors[:3]
    # And the last record is on disk, not merely in memory.
    reloaded = Session.load(session.path)
    assert reloaded.status("problem") == "done"
    assert reloaded.state["stages"]["problem"]["summary"] == f"round {ROUNDS - 1}"


def test_a_corrupt_file_is_reported_rather_than_retried() -> None:
    """Retrying a JSON error just delays the report by the retry budget."""
    session = _session()
    target = os.path.join(session.path, "session.json")
    with open(target, "w", encoding="utf-8") as fh:
        fh.write("{ not json")

    started = time.time()
    raised = ""
    try:
        Session.load(session.path)
    except Exception as exc:  # noqa: BLE001
        raised = str(exc)
    assert "Corrupt session file" in raised, raised
    # Straight through, not through the retry ladder.
    assert time.time() - started < 0.2


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
