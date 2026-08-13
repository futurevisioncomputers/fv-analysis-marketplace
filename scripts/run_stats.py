#!/usr/bin/env python
"""Run a statistical test over a session's cleaned frame.

Not a pipeline stage — a tool you point at a run that has already cleaned its
data. The stages answer "what is the number"; these answer "is the difference
real", "when do they leave", "are newer cohorts worse", and "is that a trend".

    python scripts/run_stats.py --analysis segments --flag is_default --by branch
    python scripts/run_stats.py --analysis survival --duration months_since_admission
    python scripts/run_stats.py --analysis cohorts  --flag is_completed
    python scripts/run_stats.py --analysis trend    --metric monthly_records

Reads the session's canonical parquet, so `--stage clean` must have run.
Writes nothing back: this is a question, not a stage.

Exit codes: 0 ok · 1 usage or missing data · 2 the analysis refused (too few
rows, no events, everything censored) with its reason.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Windows consoles default to cp1252 and these renderings contain "─" and "…".
# Reconfigure in place rather than wrapping: replacing sys.stdout lets the
# original object be collected, which closes the buffer underneath the new one.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from agents import statistics as st                              # noqa: E402
from agents.session import Session, SessionError                 # noqa: E402
from agents.stages import CANONICAL_PARQUET                      # noqa: E402
from scripts.run_stage import DEFAULT_SESSION                    # noqa: E402

RULE = "─" * 66


def _frame(session: Session):
    import pandas as pd

    path = session.get_artifact(CANONICAL_PARQUET)
    if not path:
        raise SessionError(
            "This session has no cleaned data yet. Run `--stage clean` first.")
    return pd.read_parquet(path)


def _resolve(frame, name: str, kind: str) -> str:
    """Accept a column name or a role name, and fail with the real options."""
    if name in frame.columns:
        return name
    candidates = [c for c in frame.columns if c.lower() == name.lower()]
    if candidates:
        return candidates[0]
    hint = ", ".join(sorted(c for c in frame.columns
                            if c.startswith("is_"))[:12]) if kind == "flag" else \
        ", ".join(sorted(frame.columns)[:15])
    raise SessionError(f"No {kind} column {name!r} in the cleaned data. "
                       f"Available: {hint}")


def _render_segments(result: dict) -> None:
    print(f"\n{RULE}\n {result['flag']} by {result['dimension']} — "
          f"overall {result['overall_rate']:.1%} of {result['n']:,}\n{RULE}")
    for row in result["segments"]:
        low, high = row["ci_95"]
        verdict = ""
        if "vs_rest" in row:
            test = row["vs_rest"]
            verdict = (f"  {test['difference']:+.1%} vs rest, "
                       f"q={test['q_value']:.3f}"
                       f"{'  SIGNIFICANT' if test['significant'] else ''}")
        else:
            verdict = "  (too few rows to test)"
        print(f" {row['segment'][:24]:24s} {row['rate']:>7.1%}  "
              f"[{low:.1%}, {high:.1%}]  n={row['n']:<6}{verdict}")
    print(f"\n {result['method']}")
    if result["significant"]:
        print(f" Stands up to correction: {', '.join(result['significant'])}")
    else:
        print(" Nothing survives correction — no segment is distinguishable "
              "from the rest.")


def _render_survival(result: dict) -> None:
    print(f"\n{RULE}\n Survival — {result['events']} event(s), "
          f"{result['censored']} censored, n={result['n']}\n{RULE}")
    for step in result["steps"][:20]:
        print(f" t={step['time']:>7}  at risk {step['at_risk']:<5} "
              f"events {step['events']:<3} survival {step['survival']:.1%}")
    if len(result["steps"]) > 20:
        print(f" … {len(result['steps']) - 20} more steps")
    median = result["median_survival"]
    print(f"\n Median survival: "
          f"{median if median is not None else 'not reached — over half are '
             'still without the outcome'}")
    print(f" {result['method']}")


def _render_cohorts(result: dict) -> None:
    print(f"\n{RULE}\n {result['flag']} by {result['cohort_column']}\n{RULE}")
    for row in result["cohorts"]:
        low, high = row["ci_95"]
        flag = "  (thin)" if row["underpowered"] else ""
        print(f" {row['cohort']:12s} {row['rate']:>7.1%}  "
              f"[{low:.1%}, {high:.1%}]  n={row['n']:<5}{flag}")
    trend = result["trend"]
    if trend.get("status") == "ready":
        print(f"\n Trend across cohorts: {trend['trend']} "
              f"(p={trend['p_value']:.4f})")
        if trend["trend"] == "flat":
            print(" Not evidence of stability — with few cohorts the test "
                  "cannot detect a trend either way.")
    else:
        print(f"\n Trend: {trend.get('reason')}")


def _render_trend(result: dict) -> None:
    trend, slope = result["mann_kendall"], result["theil_sen"]
    print(f"\n{RULE}\n Trend in {result['series']} over "
          f"{trend.get('n', 0)} periods\n{RULE}")
    print(f" Direction: {trend.get('trend')}  (p={trend.get('p_value')})")
    if slope.get("status") == "ready":
        print(f" Slope: {slope['slope_per_period']:+} per period "
              f"(Theil-Sen, robust to spikes)")
    for point in result["points"][-12:]:
        print(f"   {point['period']:12s} {point['value']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--session", default=DEFAULT_SESSION)
    ap.add_argument("--analysis", required=True,
                    choices=("segments", "survival", "cohorts", "trend"))
    ap.add_argument("--flag", help="Boolean column: is_default, is_completed, …")
    ap.add_argument("--by", help="Dimension to split on (segments).")
    ap.add_argument("--duration", help="Numeric column of elapsed time (survival).")
    ap.add_argument("--event", help="Boolean column: True = the outcome "
                                    "happened, False = censored (survival).")
    ap.add_argument("--cohort", default="joining_cohort",
                    help="Cohort column (cohorts). Build it at stage 2.7.")
    ap.add_argument("--metric", default="monthly_records",
                    help="Series to test for trend: a column, or "
                         "'monthly_records' for the row count per month.")
    ap.add_argument("--alpha", type=float, default=0.05)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    try:
        session = Session.load(args.session)
        frame = _frame(session)

        if args.analysis == "segments":
            if not (args.flag and args.by):
                raise SessionError("segments needs --flag and --by")
            result = st.compare_segments(
                frame, _resolve(frame, args.flag, "flag"),
                _resolve(frame, args.by, "dimension"), alpha=args.alpha)
            render = _render_segments

        elif args.analysis == "survival":
            if not args.duration:
                raise SessionError("survival needs --duration")
            duration = _resolve(frame, args.duration, "duration")
            import pandas as pd
            if not pd.api.types.is_numeric_dtype(frame[duration]):
                raise SessionError(
                    f"{duration!r} is not an elapsed time — survival needs a "
                    f"number of days or months, not a date. Build one at "
                    f"stage 2.7 (`--approve-features months_since_admission`) "
                    f"and pass that.")
            if args.event:
                events = frame[_resolve(frame, args.event, "event")].fillna(False)
            elif "completion_status" in frame.columns:
                # The institute's own censoring: only terminal outcomes are
                # events. Active and paused students have not churned — their
                # outcome is simply not known yet.
                events = frame["completion_status"].isin(["not_coming", "churned"])
            else:
                raise SessionError(
                    "survival needs --event, or a completion_status column to "
                    "read the censoring from")
            result = st.kaplan_meier(frame[duration].tolist(), events.tolist())
            render = _render_survival

        elif args.analysis == "cohorts":
            if not args.flag:
                raise SessionError("cohorts needs --flag")
            cohort = args.cohort
            if cohort not in frame.columns:
                raise SessionError(
                    f"No {cohort!r} column. Build it at stage 2.7: "
                    f"`run_stage.py --stage features` then "
                    f"`--approve-features joining_cohort`.")
            result = st.cohort_rates(frame, cohort,
                                     _resolve(frame, args.flag, "flag"))
            render = _render_cohorts

        else:                                    # trend
            import pandas as pd
            if args.metric == "monthly_records" and "event_date" in frame.columns:
                series = (pd.to_datetime(frame["event_date"], errors="coerce")
                          .dt.to_period("M").value_counts().sort_index())
            else:
                column = _resolve(frame, args.metric, "metric")
                if "event_date" not in frame.columns:
                    raise SessionError("trend needs an event_date column")
                series = (frame.assign(_p=pd.to_datetime(frame["event_date"],
                                                         errors="coerce")
                                       .dt.to_period("M"))
                          .groupby("_p")[column].mean().sort_index())
            values = [float(v) for v in series.tolist()]
            result = {
                "series": args.metric,
                "points": [{"period": str(p), "value": round(float(v), 2)}
                           for p, v in series.items()],
                "mann_kendall": st.mann_kendall(values),
                "theil_sen": st.theil_sen(values),
            }
            render = _render_trend

        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            render(result)

        blocked = (result.get("status") == "blocked"
                   or result.get("mann_kendall", {}).get("status") == "blocked")
        if result.get("status") == "blocked":
            print(f"\n Cannot run this: {result['reason']}", file=sys.stderr)
        return 2 if blocked else 0

    except SessionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
