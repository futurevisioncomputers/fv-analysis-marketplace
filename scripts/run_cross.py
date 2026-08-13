#!/usr/bin/env python
"""Cross one metric by two dimensions over a session's cleaned frame.

Everything else in the pipeline answers *one metric by one dimension*. This
answers the question that needs two: where does a combination behave unlike
what either factor predicts on its own?

    python scripts/run_cross.py --value is_default --rows branch --cols course
    python scripts/run_cross.py --value is_default --rows branch --cols faculty \\
        --filter "course category = programming" --filter "Total Fees >= 5000"
    python scripts/run_cross.py --suggest

Reads the session's canonical parquet, so `--stage clean` must have run.
Writes nothing back: this is a question, not a stage.

Exit codes: 0 ok · 1 usage or missing data · 2 the crossing refused (no usable
grid, everything filtered away) with its reason · 3 no interaction found — the
grid computed cleanly and one dimension explains everything in it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Windows consoles default to cp1252 and these renderings contain "─" and "×".
# Reconfigure in place rather than wrapping: replacing sys.stdout lets the
# original object be collected, which closes the buffer underneath the new one.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from agents import multifactor as mf                             # noqa: E402
from agents.session import Session, SessionError                 # noqa: E402
from agents.stages import CANONICAL_PARQUET                      # noqa: E402
from scripts.run_stage import DEFAULT_SESSION                    # noqa: E402

RULE = "─" * 74


def _frame(session: Session):
    import pandas as pd

    path = session.get_artifact(CANONICAL_PARQUET)
    if not path:
        raise SessionError(
            "This session has no cleaned data yet. Run `--stage clean` first.")
    return pd.read_parquet(path)


def _roles(session: Session) -> dict:
    package = session.result("clean") or {}
    return dict(package.get("canonical_columns") or {})


def _fmt(value, kind: str) -> str:
    if value is None:
        return "     —"
    if kind == "rate":
        return f"{value:>6.1%}"
    if kind in ("sum", "count"):
        return f"{value:>10,.0f}"
    return f"{value:>10,.1f}"


def _render(result: dict) -> None:
    if result.get("blocked"):
        print(f"\n Cannot cross these: {result['blocked']}")
        return

    kind = result["kind"]
    overall = result["overall"]
    print(f"\n{RULE}")
    print(f" {result['metric']} — {result['rows']} × {result['cols']}")
    print(f" overall {_fmt(overall['value'], kind).strip()} of "
          f"{overall['n']:,} row(s)")
    print(RULE)

    for report in result.get("filters") or []:
        if report.get("skipped"):
            print(f" ! filter {report['field']} ignored: {report['reason']}")
        else:
            print(f" filter {report['field']} {report['op']} "
                  f"{report.get('value') or ','.join(report.get('values', []))}"
                  f"  →  {report['rows_before']:,} to {report['rows_after']:,} rows")

    # The grid, rows down and columns across, margins on the edges.
    col_keys = sorted(result["col_margins"], key=lambda c: -result["col_margins"][c]["n"])
    row_keys = sorted(result["row_margins"], key=lambda r: -result["row_margins"][r]["n"])
    cells = {(c["row"], c["col"]): c for c in result["cells"]}
    thin = {(c["row"], c["col"]) for c in result["suppressed"]}

    # One column width for the header, every cell, and both margins, so the
    # grid still lines up when a cell is suppressed or a star is appended.
    cw = 12
    width = max([12] + [len(str(r)) for r in row_keys])
    header = " " * (width + 1) + "".join(f"{str(c)[:cw - 2]:>{cw}s}"
                                         for c in col_keys)
    print(f"\n{header}{'margin':>{cw}s}")
    for r in row_keys:
        line = f" {str(r)[:width]:<{width}s}"
        for c in col_keys:
            cell = cells.get((r, c))
            if cell is None:
                mark = "·" if (r, c) in thin else "—"
                line += f"{mark:>{cw - 2}s}  "
            else:
                star = "*" if (cell.get("beats_row_margin")
                               and cell.get("beats_col_margin")) else " "
                line += f"{_fmt(cell['value'], kind).strip():>{cw - 2}s}{star} "
        line += f"{_fmt(result['row_margins'][r]['value'], kind).strip():>{cw}s}"
        print(line)
    margin_line = f" {'margin':<{width}s}"
    for c in col_keys:
        margin_line += f"{_fmt(result['col_margins'][c]['value'], kind).strip():>{cw - 2}s}  "
    print(margin_line)
    print("\n  · = suppressed (too few rows)   — = no rows   * = interaction")

    if result["interactions"]:
        print(f"\n{RULE}\n Cells that beat BOTH margins\n{RULE}")
        for cell in result["interactions"]:
            print(f" {cell['row']} × {cell['col']}: "
                  f"{_fmt(cell['value'], kind).strip()} on n={cell['n']}")
            print(f"    {result['rows']} alone says "
                  f"{_fmt(cell['row_margin'], kind).strip()}, "
                  f"{result['cols']} alone says "
                  f"{_fmt(cell['col_margin'], kind).strip()}, "
                  f"together they predict "
                  f"{_fmt(cell.get('expected_additive'), kind).strip()}")
            print(f"    q={cell.get('q_vs_row')} vs its row, "
                  f"q={cell.get('q_vs_col')} vs its column")

    for note in result["notes"]:
        print(f"\n {note}")


def _render_suggestions(pairs: list) -> None:
    print(f"\n{RULE}\n Pairs this data can support\n{RULE}")
    if not pairs:
        print(" None. Crossing needs two dimensions with at least two levels "
              "each;\n this source has fewer than two such columns.")
        return
    for pair in pairs:
        print(f" --rows {pair['rows']:<18s} --cols {pair['cols']:<18s} "
              f"{pair['why']}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--session", default=DEFAULT_SESSION)
    ap.add_argument("--rows", help="First dimension (role or column name).")
    ap.add_argument("--cols", help="Second dimension (role or column name).")
    ap.add_argument("--value", help="Column being measured. Not needed for "
                                    "--kind count.")
    ap.add_argument("--kind", default="rate",
                    choices=("rate", "mean", "sum", "count", "ratio"))
    ap.add_argument("--denom", help="Denominator column, for --kind ratio.")
    ap.add_argument("--filter", action="append", default=[],
                    metavar="EXPR",
                    help="Repeatable. 'branch=vesu', 'amount>=5000', "
                         "'course in a,b'. Applied before anything is computed.")
    ap.add_argument("--min-cell", type=int, default=mf.MIN_CELL_N,
                    help=f"Suppress cells below this many rows "
                         f"(default {mf.MIN_CELL_N}).")
    ap.add_argument("--max-levels", type=int, default=mf.MAX_LEVELS)
    ap.add_argument("--alpha", type=float, default=0.05)
    ap.add_argument("--suggest", action="store_true",
                    help="List the pairs this source supports, and stop.")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    try:
        session = Session.load(args.session)
        frame = _frame(session)
        roles = _roles(session)

        if args.suggest:
            pairs = mf.suggest_pairs(roles, frame)
            if args.json:
                print(json.dumps(pairs, indent=2))
            else:
                _render_suggestions(pairs)
            return 0

        if not (args.rows and args.cols):
            raise SessionError(
                "crossing needs --rows and --cols. Run --suggest to see the "
                "pairs this data supports.")
        if args.kind != "count" and not args.value:
            raise SessionError(f"--kind {args.kind} needs --value")

        try:
            filters = [mf.parse_filter(f) for f in args.filter]
        except ValueError as exc:
            raise SessionError(str(exc)) from exc

        result = mf.crosstab(
            frame, rows=args.rows, cols=args.cols, value=args.value,
            kind=args.kind, denom=args.denom, roles=roles, filters=filters,
            min_cell=args.min_cell, max_levels=args.max_levels,
            alpha=args.alpha,
        )

        if args.json:
            print(json.dumps(result, indent=2, default=str))
        else:
            _render(result)

        if result.get("blocked"):
            print(f"\nerror: {result['blocked']}", file=sys.stderr)
            return 2
        # A clean grid with nothing in it is a real answer, and a different one
        # from "it worked" — the caller can branch on it without parsing prose.
        return 0 if result["interactions"] else 3

    except SessionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
