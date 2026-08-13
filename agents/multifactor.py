"""Multi-factor analysis — one metric across two dimensions, with filters.

Spec §21/§23. Everything the pipeline does today answers *one metric by one
dimension*: default rate by branch, completion by course. That shape cannot see
the finding the institute most needs, which is where two factors combine:

    Citylight defaults at 7.9%.  Graphic Designing defaults at 8.1%.
    Citylight × Graphic Designing defaults at 22%.

Neither single breakdown shows it. Both look mildly above average and the real
problem — one course at one branch — is averaged away in each.

## What this module refuses to do

**It will not hand back a grid and call it analysis.** Crossing three branches
with twenty-seven courses is eighty-one cells; the largest of eighty-one noisy
numbers is always large, and reading the grid for extremes finds a "finding"
every time. So:

1. A **chi-square test of independence** runs over the whole grid first. If the
   two factors are independent, that is the answer, and the cells are reported
   as description rather than discovery.
2. A cell must beat **both** of its margins to be called an interaction. Being
   high for its branch is a branch effect; being high for its course is a course
   effect; being high for both, against both, is the thing that needed two
   dimensions to see.
3. Every cell test is **BH-corrected** across the grid.
4. Cells below `min_cell` observations are **suppressed, and counted**, not
   silently averaged in.

The filter layer is deliberately plain: equality, inequality, membership and
numeric comparison, applied before anything is computed, with the row count
before and after reported. A filtered answer that does not say what it filtered
is not reproducible.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import pandas as pd

from . import statistics as st

JsonDict = Dict[str, Any]

# A cell below this many rows is suppressed. Same floor as the Analyst's
# segments and `statistics.MIN_SEGMENT_N`, and for the same reason: below it a
# rate moves by more than a percentage point when one row changes.
MIN_CELL_N = 30

# How many levels of a dimension to keep, by row count. A crosstab wider than
# this stops being readable and starts being a fishing expedition; the rest are
# grouped out and reported as excluded.
MAX_LEVELS = 12

# The pairs the spec asks for (§23), as role names. Only those whose two roles
# both resolve are offered — `suggest_pairs` does that filtering, so a source
# that cannot support a pair never advertises it.
PAIR_REGISTRY: Tuple[Tuple[str, str, str], ...] = (
    ("branch", "course", "which course underperforms at which branch"),
    ("branch", "faculty", "tutor strength is not evenly spread across sites"),
    ("branch", "source", "which lead source works at which branch"),
    ("branch", "payment_mode", "payment behaviour differs by site"),
    ("course", "faculty", "the same course taught by different people"),
    ("course", "batch_time", "morning vs evening, per course"),
    ("course", "source", "which channel brings which course"),
    ("course", "student_category", "who takes what"),
    ("faculty", "batch_time", "load and slot together"),
    ("faculty", "student_category", "who teaches whom"),
    ("source", "student_category", "channel reaches which audience"),
    ("branch", "student_category", "catchment differs by site"),
    ("branch", "class_days", "weekday vs weekend demand per site"),
    ("course", "class_days", "scheduling fit per course"),
    ("payment_mode", "student_category", "how each audience pays"),
)

_FILTER_RE = re.compile(
    r"^\s*(?P<field>[^!<>=]+?)\s*(?P<op>!=|>=|<=|=|>|<|\bin\b|\bnot in\b)\s*(?P<value>.*)$",
    re.IGNORECASE,
)


# ------------------------------------------------------------------ filters

def parse_filter(text: str) -> JsonDict:
    """Turn `branch=vesu` / `amount>=5000` / `course in a,b` into a spec.

    Raises ValueError on anything unparseable rather than ignoring it — a
    filter that silently does nothing produces a number labelled as filtered
    that is not, which is worse than an error.
    """
    match = _FILTER_RE.match(text or "")
    if not match:
        raise ValueError(
            f"cannot parse filter {text!r}; use field=value, field!=value, "
            f"field>=number, or 'field in a,b,c'"
        )
    field = match.group("field").strip()
    op = match.group("op").strip().lower()
    raw = match.group("value").strip()
    if not field or not raw:
        raise ValueError(f"filter {text!r} is missing a field or a value")
    if op in ("in", "not in"):
        values = [v.strip() for v in raw.split(",") if v.strip()]
        if not values:
            raise ValueError(f"filter {text!r} lists no values")
        return {"field": field, "op": op, "values": values}
    return {"field": field, "op": op, "value": raw}


def apply_filters(
    df: pd.DataFrame,
    filters: Sequence[Mapping[str, Any]],
    roles: Optional[Mapping[str, str]] = None,
) -> Tuple[pd.DataFrame, List[JsonDict]]:
    """Apply parsed filters in order. Returns (frame, one report per filter).

    Each report carries the rows in and out, so a reader can see which filter
    did the damage when an answer comes back thin.
    """
    roles = roles or {}
    applied: List[JsonDict] = []
    work = df
    for spec in filters or []:
        col = _resolve(work, spec["field"], roles)
        before = len(work)
        if col is None:
            applied.append({**spec, "column": None, "rows_before": before,
                            "rows_after": before, "skipped": True,
                            "reason": f"no column or role named {spec['field']!r}"})
            continue
        mask = _mask(work[col], spec)
        work = work[mask]
        applied.append({**spec, "column": col, "rows_before": before,
                        "rows_after": len(work), "skipped": False,
                        "reason": None})
    return work, applied


def _mask(series: pd.Series, spec: Mapping[str, Any]) -> pd.Series:
    op = spec["op"]
    if op in ("in", "not in"):
        wanted = {v.strip().lower() for v in spec["values"]}
        hit = series.astype("string").str.strip().str.lower().isin(wanted)
        return hit.fillna(False) if op == "in" else ~hit.fillna(False)

    raw = spec["value"]
    if op in (">", ">=", "<", "<="):
        left = pd.to_numeric(series, errors="coerce")
        right = pd.to_numeric(pd.Series([raw]), errors="coerce").iloc[0]
        if pd.isna(right):
            # A date comparison written like a number comparison.
            left = pd.to_datetime(series, errors="coerce")
            right = pd.to_datetime(raw, errors="coerce")
        if pd.isna(right):
            return pd.Series(False, index=series.index)
        cmp = {">": left > right, ">=": left >= right,
               "<": left < right, "<=": left <= right}[op]
        return cmp.fillna(False)

    same = series.astype("string").str.strip().str.lower() == raw.strip().lower()
    same = same.fillna(False)
    return same if op == "=" else ~same


def _resolve(df: pd.DataFrame, name: str, roles: Mapping[str, str]) -> Optional[str]:
    """A column name, a role name, or a case-insensitive column match."""
    if name in df.columns:
        return name
    if name in roles and roles[name] in df.columns:
        return roles[name]
    lowered = {str(c).strip().lower(): c for c in df.columns}
    return lowered.get(name.strip().lower())


# ----------------------------------------------------------------- crossing

def crosstab(
    df: pd.DataFrame,
    *,
    rows: str,
    cols: str,
    value: Optional[str] = None,
    kind: str = "rate",
    denom: Optional[str] = None,
    roles: Optional[Mapping[str, str]] = None,
    filters: Sequence[Mapping[str, Any]] = (),
    min_cell: int = MIN_CELL_N,
    max_levels: int = MAX_LEVELS,
    alpha: float = 0.05,
) -> JsonDict:
    """One metric across two dimensions.

    Args:
        rows, cols: the two dimensions, as role or column names.
        value: the column being measured. For `kind="count"` it is ignored.
        kind: rate (mean of a 0/1 flag) · mean · sum · count · ratio.
        denom: denominator column, required for `kind="ratio"`.
        min_cell: cells below this many rows are suppressed, not shown small.

    Returns a dict with `overall`, `row_margins`, `col_margins`, `cells`,
    `interactions`, `independence`, `suppressed` and `notes`.
    """
    roles = roles or {}
    notes: List[str] = []

    work, applied = apply_filters(df, filters, roles)
    if work.empty:
        return _empty(rows, cols, kind, applied,
                      "every row was removed by the filters")

    row_col = _resolve(work, rows, roles)
    col_col = _resolve(work, cols, roles)
    if not row_col or not col_col:
        missing = rows if not row_col else cols
        return _empty(rows, cols, kind, applied,
                      f"no column or role named {missing!r}")
    if row_col == col_col:
        return _empty(rows, cols, kind, applied,
                      "the two dimensions are the same column; crossing it "
                      "with itself only restates the one-dimension breakdown")

    measure, denom_series, err = _measure(work, value, kind, denom, roles)
    if err:
        return _empty(rows, cols, kind, applied, err)

    frame = pd.DataFrame({
        "_row": work[row_col], "_col": work[col_col], "_val": measure,
    })
    if denom_series is not None:
        frame["_den"] = denom_series
    frame = frame.dropna(subset=["_row", "_col"])
    if frame.empty:
        return _empty(rows, cols, kind, applied,
                      "no row has both dimensions populated")

    frame, level_note = _trim_levels(frame, max_levels)
    if level_note:
        notes.append(level_note)

    overall = _cell_stats(frame, kind)
    row_margins = {
        str(k): _cell_stats(g, kind)
        for k, g in frame.groupby("_row", dropna=True, observed=True)
    }
    col_margins = {
        str(k): _cell_stats(g, kind)
        for k, g in frame.groupby("_col", dropna=True, observed=True)
    }

    cells: List[JsonDict] = []
    suppressed: List[JsonDict] = []
    for (r, c), group in frame.groupby(["_row", "_col"], dropna=True, observed=True):
        stats = _cell_stats(group, kind)
        record = {
            "row": str(r), "col": str(c),
            "value": stats["value"], "n": stats["n"],
        }
        if stats["n"] < min_cell:
            suppressed.append(record)
            continue
        record["ci_95"] = stats.get("ci_95")
        record["row_margin"] = row_margins[str(r)]["value"]
        record["col_margin"] = col_margins[str(c)]["value"]
        # Additive expectation: what the two dimensions predict on their own.
        # A residual near zero means the cell is exactly what the margins
        # already told you, and needed no crossing to find.
        if None not in (record["value"], record["row_margin"],
                        record["col_margin"], overall["value"]):
            expected = record["row_margin"] + record["col_margin"] - overall["value"]
            record["expected_additive"] = round(expected, 4)
            record["residual"] = round(record["value"] - expected, 4)
        cells.append(record)

    _test_against_margins(cells, frame, kind, min_cell, alpha)

    independence = _independence(frame, kind)
    interactions = [
        c for c in cells
        if c.get("beats_row_margin") and c.get("beats_col_margin")
    ]
    interactions.sort(key=lambda c: -abs(c.get("residual") or 0.0))
    cells.sort(key=lambda c: -abs(c.get("residual") or 0.0))

    notes.extend(_notes(independence, interactions, suppressed, min_cell, kind))

    return {
        "metric": value or "count",
        "kind": kind,
        "rows": rows, "rows_column": row_col,
        "cols": cols, "cols_column": col_col,
        "filters": applied,
        "rows_analysed": int(len(frame)),
        "overall": overall,
        "row_margins": row_margins,
        "col_margins": col_margins,
        "cells": cells,
        "interactions": interactions,
        "independence": independence,
        "suppressed": suppressed,
        "min_cell": min_cell,
        "notes": notes,
    }


def suggest_pairs(roles: Mapping[str, str],
                  df: Optional[pd.DataFrame] = None) -> List[JsonDict]:
    """The registry pairs this source can actually support.

    A pair is offered only when both roles resolve and each has at least two
    levels: crossing by a column that holds one value everywhere produces a
    single row and reads like an analysis.
    """
    out: List[JsonDict] = []
    for left, right, why in PAIR_REGISTRY:
        if left not in roles or right not in roles:
            continue
        if df is not None:
            lc, rc = roles[left], roles[right]
            if lc not in df.columns or rc not in df.columns:
                continue
            if df[lc].nunique(dropna=True) < 2 or df[rc].nunique(dropna=True) < 2:
                continue
        out.append({"rows": left, "cols": right, "why": why})
    return out


# ---------------------------------------------------------------- internals

def _measure(df, value, kind, denom, roles):
    """(series, denominator, error). The denominator is None except for ratio."""
    if kind == "count":
        return pd.Series(1.0, index=df.index), None, None

    col = _resolve(df, value, roles) if value else None
    if not col:
        return None, None, (
            f"no column or role named {value!r} to measure"
            if value else f"kind={kind!r} needs a value column"
        )

    if kind == "rate":
        return df[col].astype("boolean").astype("float"), None, None
    if kind in ("mean", "sum"):
        return pd.to_numeric(df[col], errors="coerce"), None, None
    if kind == "ratio":
        den_col = _resolve(df, denom, roles) if denom else None
        if not den_col:
            return None, None, "kind='ratio' needs a denominator column"
        return (pd.to_numeric(df[col], errors="coerce"),
                pd.to_numeric(df[den_col], errors="coerce"), None)
    return None, None, f"unknown kind {kind!r}"


def _cell_stats(group: pd.DataFrame, kind: str) -> JsonDict:
    vals = group["_val"]
    if kind == "ratio":
        den = group.get("_den")
        mask = vals.notna() & (den.notna() if den is not None else False)
        y, x = vals[mask], den[mask]
        n = int(len(y))
        total = float(x.sum())
        return {"value": round(float(y.sum()) / total, 4) if total else None,
                "n": n}

    clean = vals.dropna()
    n = int(len(clean))
    if n == 0:
        return {"value": None, "n": 0}
    if kind == "sum" or kind == "count":
        return {"value": round(float(clean.sum()), 4), "n": n}
    value = float(clean.mean())
    out: JsonDict = {"value": round(value, 4), "n": n}
    if kind == "rate":
        low, high = st.wilson_interval(int(round(clean.sum())), n)
        out["ci_95"] = (round(low, 4), round(high, 4))
    else:
        out["sd"] = round(float(clean.std(ddof=1)), 4) if n > 1 else 0.0
    return out


def _test_against_margins(cells, frame, kind, min_cell, alpha) -> None:
    """Test each cell against the rest of its row and the rest of its column.

    Two families of tests, corrected separately: a cell's row comparison and
    its column comparison answer different questions, and pooling them into one
    correction would make each harder to pass for no reason.

    Only `rate` and `mean` are testable here. For a sum or a count "different
    from the rest of the row" is not a statistical question — the total is what
    it is — so those cells carry residuals and no verdict.
    """
    if kind not in ("rate", "mean") or not cells:
        return

    row_p: List[float] = []
    col_p: List[float] = []
    for cell in cells:
        in_row = frame[frame["_row"] == cell["row"]]
        in_col = frame[frame["_col"] == cell["col"]]
        here = in_row[in_row["_col"] == cell["col"]]
        row_p.append(_compare(here, in_row.drop(here.index), kind, min_cell))
        col_p.append(_compare(here, in_col.drop(here.index), kind, min_cell))

    for family, values, tag in (("row", row_p, "beats_row_margin"),
                                ("col", col_p, "beats_col_margin")):
        testable = [i for i, p in enumerate(values) if p is not None]
        if not testable:
            for cell in cells:
                cell[tag] = None
            continue
        adjusted = st.benjamini_hochberg([values[i] for i in testable], alpha)
        lookup = dict(zip(testable, adjusted))
        for i, cell in enumerate(cells):
            entry = lookup.get(i)
            cell[f"q_vs_{family}"] = entry["q_value"] if entry else None
            cell[tag] = bool(entry["significant"]) if entry else None


def _compare(here: pd.DataFrame, rest: pd.DataFrame, kind: str,
             min_cell: int) -> Optional[float]:
    """p-value for this cell against the rest of its row/column, or None."""
    a = here["_val"].dropna()
    b = rest["_val"].dropna()
    if len(a) < min_cell or len(b) < min_cell:
        return None
    if kind == "rate":
        result = st.two_proportion_test(int(round(a.sum())), len(a),
                                        int(round(b.sum())), len(b))
    else:
        result = st.two_mean_test(float(a.mean()), float(a.std(ddof=1)), len(a),
                                  float(b.mean()), float(b.std(ddof=1)), len(b))
    return result.get("p_value")


def _independence(frame: pd.DataFrame, kind: str) -> JsonDict:
    """Chi-square over the grid — one test before any cell is picked over.

    For a rate, the table is (successes, failures) per cell, which asks whether
    the flag depends on the combination. For anything else it is the row counts,
    which asks whether the two dimensions are related at all.
    """
    if kind == "rate":
        grouped = frame.groupby(["_row", "_col"], dropna=True, observed=True)["_val"]
        agg = grouped.agg(["sum", "count"]).reset_index()
        table = [[float(r["sum"]), float(r["count"] - r["sum"])]
                 for _, r in agg.iterrows()]
        return st.chi_square_independence(table)
    counts = pd.crosstab(frame["_row"], frame["_col"])
    return st.chi_square_independence(counts.values.tolist())


def _trim_levels(frame: pd.DataFrame, max_levels: int):
    """Keep the biggest levels of each dimension; say what was dropped."""
    dropped: List[str] = []
    for axis in ("_row", "_col"):
        counts = frame[axis].value_counts()
        if len(counts) <= max_levels:
            continue
        keep = set(counts.head(max_levels).index)
        dropped.append(f"{len(counts) - max_levels} {axis.strip('_')} level(s)")
        frame = frame[frame[axis].isin(keep)]
    if not dropped:
        return frame, None
    return frame, (
        f"Kept the {max_levels} largest levels per dimension; excluded "
        f"{' and '.join(dropped)} with the fewest rows. A wider grid is a "
        f"fishing expedition, not a report."
    )


def _notes(independence, interactions, suppressed, min_cell, kind) -> List[str]:
    notes: List[str] = []
    if independence.get("valid") is False and independence.get("reason"):
        notes.append(independence["reason"])
    elif independence.get("p_value") is not None:
        p = independence["p_value"]
        if kind == "rate":
            # The table is successes vs failures, so this really is a test of
            # the metric across the grid.
            notes.append(
                f"The two factors look independent (chi-square p={p:.3f}). "
                f"Read the grid as description: the cells vary, but not by "
                f"more than chance would produce."
                if p > 0.05 else
                f"The rate is not independent of the combination (chi-square "
                f"p={p:.3f}), so at least one cell behaves differently from "
                f"what the margins predict."
            )
        else:
            # The table is row counts, so this tests whether the two DIMENSIONS
            # are associated — not whether the measured value interacts. Saying
            # otherwise would read as a finding about the metric when it is
            # really a statement about who appears where.
            notes.append(
                f"The two dimensions are not evenly crossed (chi-square on "
                f"cell counts, p={p:.3f}): the mix of {'columns'} differs by "
                f"row. That is a caveat about the grid, not a finding about "
                f"the measure — the margins are computed over different "
                f"populations."
                if p <= 0.05 else
                f"The two dimensions are evenly crossed (chi-square on cell "
                f"counts, p={p:.3f}), so the margins are comparable across "
                f"the grid."
            )
    if kind in ("rate", "mean") and not interactions:
        notes.append(
            "No cell beats both of its margins after correction. Whatever "
            "varies here is explained by one dimension on its own — crossing "
            "them added nothing."
        )
    if suppressed:
        rows = sum(c["n"] for c in suppressed)
        notes.append(
            f"{len(suppressed)} cell(s) holding {rows} row(s) are below the "
            f"{min_cell}-row floor and are suppressed, not shown small. They "
            f"are listed under 'suppressed' with their counts only."
        )
    return notes


def _empty(rows, cols, kind, applied, reason) -> JsonDict:
    return {
        "metric": None, "kind": kind, "rows": rows, "cols": cols,
        "filters": applied, "rows_analysed": 0,
        "overall": {"value": None, "n": 0},
        "row_margins": {}, "col_margins": {}, "cells": [], "interactions": [],
        "independence": {"statistic": None, "p_value": None, "valid": False},
        "suppressed": [], "blocked": reason, "notes": [reason],
    }
