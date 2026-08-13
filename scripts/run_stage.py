#!/usr/bin/env python
"""Run one pipeline stage against a session, then stop.

This is the entry point every skill calls. The pipeline used to be a single
process that ran stages 1->8 and returned, which left nowhere to pause and ask
the operator anything. Here each stage is its own invocation: load the session
from disk, run one stage, persist, print the checkpoint, exit. What happens
between stages is the caller's decision.

    # start a session and run the first stage
    python scripts/run_stage.py --session runs/aug --csv data.csv \
        --question "How is admission conversion by branch?" --stage problem

    # continue, one stage at a time
    python scripts/run_stage.py --session runs/aug --stage next

    # jump straight to a stage; prerequisites are back-filled automatically
    python scripts/run_stage.py --session runs/aug --csv data.csv --stage eda

    # non-interactive: everything, no pauses (what the web service uses)
    python scripts/run_stage.py --session runs/aug --csv data.csv --auto

    # look without running
    python scripts/run_stage.py --session runs/aug --status
    python scripts/run_stage.py --session runs/aug --show clean --json

Exit codes: 0 ok · 1 usage or unexpected error · 2 a stage blocked (an honest
refusal, with the agent's own reason) · 3 nothing left to run.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Stage summaries contain "→", "·" and curly quotes. Windows consoles default
# to cp1252, where printing those raises UnicodeEncodeError and kills the run
# after the work is already done. Reconfigure in place rather than wrapping:
# replacing sys.stdout lets the original object be collected, and that closes
# the buffer the replacement is writing to.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from agents import stages                                      # noqa: E402
from agents.session import (OPTIONAL_STAGES, STAGE_BY_KEY,      # noqa: E402
                            STAGE_KEYS, Session, SessionError)
from scripts.run_pipeline import _build_data_sources, wrap_goal  # noqa: E402

EXIT_OK, EXIT_ERROR, EXIT_BLOCKED, EXIT_NOTHING = 0, 1, 2, 3

# Where a session lives when the caller does not say. Skills rely on this: each
# one is invoked separately, so "the current run" has to be a place on disk both
# of them can find rather than something passed between them.
DEFAULT_SESSION = os.environ.get("FV_SESSION") or os.path.join(".fv", "session")

RULE = "─" * 66


# ----------------------------------------------------------------- rendering

def _print_rail(session: Session) -> None:
    marks = {"done": "✓", "blocked": "✗", "skipped": "–", "pending": " "}
    print(f"\n{RULE}")
    for row in session.progress():
        mark = marks.get(row["status"], "?")
        optional = " (optional)" if row["optional"] else ""
        print(f" {mark} {row['n']:>3}  {row['label']}{optional}")
        if row["summary"]:
            print(f"       {row['summary']}")
    print(RULE)


def _print_checkpoint(point: dict) -> None:
    print(f"\n{RULE}")
    print(f" Stage {point['n']} · {point['label']} — {point['status']}")
    print(RULE)
    if point["summary"]:
        print(f" {point['summary']}")
    if point["details"]:
        print()
        for line in point["details"]:
            print(f"   {line}")

    needs = point.get("data_needs")
    if needs:
        print(f"\n Can this data answer them?  "
              f"{needs['answerable']} yes · {needs['blocked']} no")
        for line in stages.capability_lines(needs):
            print(f"   {line}")
    if point["artifacts"]:
        print("\n Files written:")
        for name, path in point["artifacts"].items():
            print(f"   {name:22s} {path}")

    print("\n What next?")
    for offer in point["offers"]:
        note = f"  ({offer['note']})" if offer.get("note") else ""
        print(f"   · {offer['label']}{note}")
        if offer.get("path"):
            print(f"       {offer['path']}")
    if point["next_stage"]:
        print(f"   · Continue to stage {STAGE_BY_KEY[point['next_stage']]['n']}"
              f" — {point['next_label']}")
        print(f"\n   next: --stage {point['next_stage']}")
    else:
        print("   · Nothing left — every stage has run.")
    print()


def _json_envelope(session: Session, points: list, blocked=None) -> str:
    """One shape for every --json run, however many stages it took.

    Callers are skills, which have to read this without knowing whether
    prerequisites were back-filled. A bare checkpoint would be a dict on a
    one-stage run and a list on a three-stage one, so the envelope is always an
    object: `checkpoint` is the stage that was asked for, `ran` is everything
    that had to happen to get there.
    """
    payload = {
        "session": session.path,
        "ran": points,
        "checkpoint": points[-1] if points else None,
        "blocked": bool(blocked),
        "progress": session.progress(),
    }
    if blocked is not None:
        payload["reason"] = str(blocked)
        payload["detail"] = blocked.detail
    return json.dumps(payload, indent=2, default=str)


# -------------------------------------------------------------------- actions

def _resolve_targets(session: Session, requested: str, backfill: bool) -> list:
    """Which stages to run, in order, for a `--stage` argument."""
    if requested == "all":
        return [k for k in STAGE_KEYS
                if k not in OPTIONAL_STAGES and not session.is_done(k)]
    if requested == "next":
        following = session.next_stage()
        return [following] if following else []
    if requested not in STAGE_BY_KEY:
        raise SessionError(
            f"Unknown stage {requested!r}. Known: {', '.join(STAGE_KEYS)}, "
            f"plus 'next' and 'all'.")
    # Back-fill is what lets a mid-pipeline skill run against a bare CSV:
    # /fv-eda on a fresh session runs problem -> schema -> clean first.
    prereqs = session.missing_prereqs(requested) if backfill else []
    return prereqs + [requested]


def _run(session: Session, targets: list, *, quiet: bool, as_json: bool) -> int:
    points = []
    for key in targets:
        missing = session.missing_prereqs(key)
        if missing:
            raise SessionError(
                f"Stage '{key}' needs {', '.join(missing)} first. "
                f"Re-run without --no-backfill, or run them explicitly.")
        if not quiet:
            print(f"\n▶ {STAGE_BY_KEY[key]['label']} …", flush=True)
        try:
            stages.run_stage(session, key)
        except stages.StageBlocked as blocked:
            point = stages.checkpoint(session, key)
            if as_json:
                points.append(point)
                print(_json_envelope(session, points, blocked=blocked))
            else:
                print(f"\n✗ {STAGE_BY_KEY[key]['label']} blocked: {blocked}",
                      file=sys.stderr)
                for line in (blocked.detail or [])[:8] if isinstance(
                        blocked.detail, list) else []:
                    print(f"    · {line}", file=sys.stderr)
                print("\n  Nothing downstream was run, and nothing was "
                      "invented. Fix the source or adjust the question.",
                      file=sys.stderr)
            return EXIT_BLOCKED
        points.append(stages.checkpoint(session, key))

    if as_json:
        print(_json_envelope(session, points))
    elif points:
        # In a multi-stage run only the last checkpoint is a decision point;
        # the ones before it already have their summary on the rail.
        if len(points) > 1:
            _print_rail(session)
        _print_checkpoint(points[-1])
    return EXIT_OK


def _resolve_approvals(session: Session, argument: str) -> list:
    """Turn `all` / `none` / an id list into the ids that were actually offered.

    An id the proposal never contained is a typo, and silently accepting it
    would leave the operator believing a feature was approved when nothing will
    be built.
    """
    offered = [f["id"] for f in
               (session.result("features") or {}).get("proposed", [])]
    choice = (argument or "").strip().lower()
    if choice == "all":
        if not offered:
            raise SessionError(
                "No features have been proposed yet — run `--stage features` "
                "first to see what this sheet supports.")
        return offered
    if choice in ("none", ""):
        return []
    wanted = [part.strip() for part in argument.split(",") if part.strip()]
    unknown = [w for w in wanted if w not in offered]
    if unknown:
        raise SessionError(
            f"Not proposed for this sheet: {', '.join(unknown)}. "
            f"Available: {', '.join(offered) or 'none'}")
    return wanted


def _show(session: Session, key: str, as_json: bool) -> int:
    if key not in STAGE_BY_KEY:
        print(f"error: unknown stage {key!r}", file=sys.stderr)
        return EXIT_ERROR
    if as_json:
        print(json.dumps({"stage": key,
                          "entry": session.state["stages"].get(key),
                          "checkpoint": stages.checkpoint(session, key)},
                         indent=2, default=str))
        return EXIT_OK
    if not session.state["stages"].get(key):
        print(f"Stage '{key}' has not run yet.")
        return EXIT_OK
    _print_checkpoint(stages.checkpoint(session, key))
    return EXIT_OK


# ----------------------------------------------------------------------- main

def _apply_source_args(session: Session, args) -> list:
    """Attach or update the session's sources. Returns stages invalidated."""
    before = (session.state.get("csv_path"), _source_key(session.state))
    sources = _build_data_sources(args)
    if sources:
        session.state["data_sources"] = sources
    if args.csv:
        session.state["csv_path"] = args.csv
    if args.question:
        session.state["question"] = args.question
        session.state["goal"] = wrap_goal(args.question)
    if args.max_questions is not None:
        session.state["max_questions"] = args.max_questions
    if args.date_format:
        session.state["date_format"] = args.date_format
    session.save()

    # Pointing an existing session at different data must discard what was
    # computed from the old data. Leaving it would mix results from two files
    # in one report, which is worse than recomputing.
    after = (session.state.get("csv_path"), _source_key(session.state))
    if before != after and any(session.state["stages"].values()):
        return session.reset_from("problem")
    return []


def _source_key(state: dict) -> tuple:
    return tuple(sorted(str(s.get("path_or_query") or s.get("path") or "")
                        for s in (state.get("data_sources") or [])))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"Stages: {', '.join(STAGE_KEYS)}")
    ap.add_argument("--session", default=DEFAULT_SESSION,
                    help=f"Session directory (default {DEFAULT_SESSION}, or "
                         f"$FV_SESSION). Created if it does not exist.")
    ap.add_argument("--new", action="store_true",
                    help="Discard any existing session at that path and start over.")
    ap.add_argument("--stage", default=None,
                    help="Stage key, or 'next' / 'all'.")
    ap.add_argument("--auto", action="store_true",
                    help="Run every remaining stage without pausing.")

    ap.add_argument("--csv", help="Source CSV path.")
    ap.add_argument("--excel", help="Excel workbook; each non-empty sheet is a source.")
    ap.add_argument("--source", action="append", default=[],
                    help="Extra source as name=path.csv (repeatable).")
    ap.add_argument("--sheet-url", action="append", default=[], dest="sheet_url",
                    help="Published Google Sheet URL, snapshotted before use.")
    ap.add_argument("--question", help="The business question driving the run.")
    ap.add_argument("--max-questions", type=int, default=None,
                    help="Cap how many business questions are analyzed.")
    ap.add_argument("--date-format", choices=("dmy", "mdy", "iso"),
                    help="Force a date interpretation instead of inferring it.")

    ap.add_argument("--status", action="store_true", help="Show the stage rail and exit.")
    ap.add_argument("--show", metavar="STAGE", help="Show a completed stage and exit.")
    ap.add_argument("--reset-from", metavar="STAGE",
                    help="Discard a stage and everything downstream of it.")
    ap.add_argument("--decision", metavar="STAGE:CHOICE[:DETAIL]",
                    help="Record an operator decision on the audit trail.")
    ap.add_argument("--approve-features", metavar="all|id,id|none",
                    help="Approve derived features by id (stage 2.7). Nothing "
                         "is materialized until you do. Re-runs the stage and "
                         "clears everything downstream of it.")
    ap.add_argument("--no-backfill", action="store_true",
                    help="Fail instead of running missing prerequisites.")
    ap.add_argument("--json", action="store_true",
                    help="Emit the checkpoint as JSON (what the skills read).")
    ap.add_argument("--quiet", action="store_true", help="Suppress progress lines.")
    args = ap.parse_args(argv)

    try:
        if args.new and os.path.isdir(args.session):
            shutil.rmtree(args.session, ignore_errors=True)
        session = Session.open_or_create(args.session, question=args.question or "",
                                         csv_path=args.csv or "")
        invalidated = _apply_source_args(session, args)
        if invalidated and not args.json:
            print(f"Source changed — discarded {len(invalidated)} stage(s) "
                  f"computed from the previous data: {', '.join(invalidated)}")

        if args.decision:
            parts = args.decision.split(":", 2)
            session.add_decision(parts[0], parts[1] if len(parts) > 1 else "",
                                 parts[2] if len(parts) > 2 else "")
            print(f"Recorded decision on '{parts[0]}'.")

        if args.approve_features is not None:
            approved = _resolve_approvals(session, args.approve_features)
            session.state["approved_features"] = approved
            # The approved set changes the frame every later stage reads, so
            # anything computed from the old frame has to go.
            session.reset_from("features")
            session.add_decision("features", "approved", ", ".join(approved) or "none")
            if not args.json:
                print(f"Approved {len(approved)} feature(s): "
                      f"{', '.join(approved) or 'none'}")
            args.stage = args.stage or "features"

        if args.reset_from:
            cleared = session.reset_from(args.reset_from)
            print(f"Cleared {len(cleared)} stage(s): {', '.join(cleared) or 'none'}")
            if not (args.stage or args.auto):
                _print_rail(session)
                return EXIT_OK

        if args.show:
            return _show(session, args.show, args.json)

        if args.status or not (args.stage or args.auto):
            if args.json:
                print(json.dumps({"path": session.path,
                                  "progress": session.progress(),
                                  "next_stage": session.next_stage(),
                                  "artifacts": session.state["artifacts"]},
                                 indent=2, default=str))
            else:
                _print_rail(session)
                following = session.next_stage()
                print(f" next: --stage {following}" if following
                      else " every stage has run.")
            return EXIT_OK

        if not session.sources():
            print("error: this session has no data source. Pass --csv, "
                  "--excel, --source or --sheet-url.", file=sys.stderr)
            return EXIT_ERROR

        requested = "all" if args.auto else (args.stage or "next")
        targets = _resolve_targets(session, requested, not args.no_backfill)
        if not targets:
            print("Nothing to run — every requested stage is already done.")
            return EXIT_NOTHING

        return _run(session, targets, quiet=args.quiet or args.json,
                    as_json=args.json)

    except SessionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
