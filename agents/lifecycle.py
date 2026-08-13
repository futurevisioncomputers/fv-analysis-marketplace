"""Churn labelling for the institute's student lifecycle.

The rule, as corrected by the institute (August 2026):

```
course_end := Date of Joining + Course Duration (IN DAYS)

Not_Coming        AND  as_of <  course_end + 6 months  ->  paused    (censored)
Not_Coming        AND  as_of >= course_end + 6 months  ->  churned
NOT TO ENTERTRAIN AND  as_of <  course_end + 6 months  ->  paused    (censored)
NOT TO ENTERTRAIN AND  as_of >= course_end + 6 months  ->  churned
Main_data                                              ->  active    (censored)
Course_Completed                                       ->  completed
```

Three properties of this rule drive the whole module.

**It counts from the expected END of the course, not from admission.** The
earlier version counted six months from the admission date, which marks a
student on a twelve-month programme as churned in month six — while their
course still has half a year left to run. Counting from `course_end` makes the
label mean what it was always meant to mean: *the window closed and they never
came back*.

**It is time-dependent.** A row flips from censored to churned with no edit to
any sheet, purely because the calendar moved. Churn therefore cannot be a
stored column; it is recomputed per run against an `as_of` date that is
recorded alongside the answer. Two reports with different as-of dates that
disagree are both right.

**It needs a duration the sheets may not carry.** Where `course_end` cannot be
computed the row is `unlabelled` — never silently defaulted back to the
admission-date rule, and never counted in either the numerator or the
denominator of a churn rate.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

import pandas as pd

# Terminal and censored states. `is_churn` is True only for `churned`; False
# for every other resolved state; None (unknown) for `unlabelled`, so a rate
# computed as mean(is_churn) drops the unknowns instead of counting them as
# retained.
CHURN_STATUSES = (
    "churned",       # window closed, never came back
    "paused",        # stopped attending, still inside the grace window
    "active",        # currently on the roster
    "completed",     # finished the course
    "staff_alumni",  # stopped attending because they joined the staff
    "unlabelled",    # in a churn-risk tab, but no computable course_end
    "no_membership", # in no lifecycle tab at all — a different kind of unknown
)

# Sheet memberships that put a student in scope for the churn test at all.
AT_RISK_STATUSES = ("not_coming", "not_to_entertain")

# The institute's grace window, in calendar months rather than days: "six
# months" from a course ending on 31 January means 31 July, not 31 July minus
# a leap-year rounding error.
DEFAULT_GRACE_MONTHS = 6

# Columns this module writes. Re-running over a frame it has already labelled
# must not treat its own output as evidence.
DERIVED_DATE_COLUMNS = ("course_end_date",)
CHURN_COLUMNS = (
    "churn_status", "is_churn", "course_end_date", "churn_basis",
    "days_past_course_end", "tenure_days",
)


def churn_labels(
    df: pd.DataFrame,
    roles: Mapping[str, str],
    *,
    as_of: Optional[Any] = None,
    grace_months: int = DEFAULT_GRACE_MONTHS,
    duration_days_by_category: Optional[Mapping[str, float]] = None,
    entertain_unconditional: bool = False,
) -> Dict[str, Any]:
    """Label every row's lifecycle state as of a given date.

    Args:
        df: a cleaned frame carrying `completion_status` (from the timetable
            tab membership) — without it there is no lifecycle ground truth and
            everything comes back `unlabelled`.
        roles: canonical role -> column name, as produced by the Data Engineer.
        as_of: the date the labels are true for. Defaults to the frame's own
            latest date, because these exports are historical and wall-clock
            would age every row past the end of the data. Always reported.
        grace_months: months after `course_end` before absence becomes churn.
        duration_days_by_category: fallback course length per course/category
            value, for sources with no duration column. Supplied by the
            operator — nothing is assumed about how long a course runs.
        entertain_unconditional: treat `NOT TO ENTERTRAIN` as churn regardless
            of the calendar (the institute has decided the student is not
            coming back). Off by default, matching the rule as last stated.

    Returns:
        {"columns": DataFrame aligned to df.index, "summary": {...}}
    """
    index = df.index
    out = pd.DataFrame(index=index)

    membership = (
        df["completion_status"] if "completion_status" in df.columns
        else pd.Series(pd.NA, index=index, dtype="object")
    )

    as_of_ts, as_of_source = _resolve_as_of(df, as_of)
    start, start_col = _start_dates(df, roles)
    duration, duration_basis = _durations(df, roles, duration_days_by_category)

    course_end = start + pd.to_timedelta(duration, unit="D")
    deadline = course_end + pd.DateOffset(months=grace_months)

    # Two different unknowns, kept apart. `no_membership` is a row that appears
    # in no lifecycle tab — after a join, most of the student master. Folding it
    # into `unlabelled` makes a missing duration column look ten times worse
    # than it is and buries the rows that a duration table would actually fix.
    status = pd.Series("no_membership", index=index, dtype="object")
    status[membership.notna()] = "unlabelled"
    basis = pd.Series(pd.NA, index=index, dtype="object")

    status[membership == "completed"] = "completed"
    status[membership == "active"] = "active"

    at_risk = membership.isin(AT_RISK_STATUSES)
    computable = at_risk & deadline.notna()
    status[at_risk & ~computable] = "unlabelled"
    status[computable & (as_of_ts >= deadline)] = "churned"
    status[computable & (as_of_ts < deadline)] = "paused"
    basis[computable] = duration_basis[computable]

    if entertain_unconditional:
        forced = membership == "not_to_entertain"
        status[forced] = "churned"
        basis[forced] = "sheet_membership"

    # Staff are hired out of the student body. They stopped attending because
    # they now teach — counting them as churn both overstates churn and blames
    # the institute for its own hiring.
    staff = _staff_alumni(df) & status.isin(("churned", "paused", "unlabelled"))
    status[staff] = "staff_alumni"

    out["churn_status"] = status
    out["is_churn"] = pd.Series(
        [_is_churn(s) for s in status], index=index, dtype="object"
    )
    out["course_end_date"] = course_end
    out["churn_basis"] = basis
    out["days_past_course_end"] = (as_of_ts - course_end).dt.days
    out["tenure_days"] = _tenure_days(start, deadline, status, as_of_ts)

    return {
        "columns": out,
        "summary": _summary(
            out, status, membership, as_of_ts, as_of_source, grace_months,
            start_col, duration_basis, entertain_unconditional,
        ),
    }


# ------------------------------------------------------------------ internals

def _is_churn(status: str) -> Optional[bool]:
    if status == "churned":
        return True
    if status in ("unlabelled", "no_membership"):
        return None
    return False


def _resolve_as_of(df: pd.DataFrame, as_of: Optional[Any]) -> tuple[pd.Timestamp, str]:
    """The date the labels are true for, and where it came from.

    Defaults to the frame's latest date rather than today: these sheets are
    historical exports, and judging a 2019 export against 2026 marks the entire
    roster as churned.
    """
    if as_of is not None:
        return pd.Timestamp(as_of), "supplied"
    latest = pd.NaT
    for col in df.columns:
        # `course_end_date` is this module's own output and lies in the FUTURE
        # for anyone still studying. Letting it set as_of would move the
        # reference date past the end of the data and churn the roster.
        if col in DERIVED_DATE_COLUMNS:
            continue
        if pd.api.types.is_datetime64_any_dtype(df[col]):
            col_max = df[col].max()
            if pd.notna(col_max) and (pd.isna(latest) or col_max > latest):
                latest = col_max
    if pd.isna(latest):
        return pd.Timestamp.today().normalize(), "today (frame carries no date)"
    return pd.Timestamp(latest), "latest date in the data"


def _start_dates(
    df: pd.DataFrame, roles: Mapping[str, str]
) -> tuple[pd.Series, Optional[str]]:
    """The course start. Joining date first, admission date only as a fallback.

    They are not the same thing — a student admitted in March may start with the
    April batch — but admission is the better of the two available answers when
    joining is absent, and which one was used is reported.
    """
    for role in ("joining_date", "admission_date"):
        col = roles.get(role)
        if col and col in df.columns:
            series = pd.to_datetime(df[col], errors="coerce")
            if series.notna().any():
                return series, col
    return pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns]"), None


def _durations(
    df: pd.DataFrame,
    roles: Mapping[str, str],
    by_category: Optional[Mapping[str, float]],
) -> tuple[pd.Series, pd.Series]:
    """Course length in days per row, plus where each row's number came from.

    The duration column is authoritative. A per-category lookup fills gaps only
    when the operator supplies one — the institute knows how long its courses
    run and this module does not guess.
    """
    index = df.index
    days = pd.Series(float("nan"), index=index, dtype="float64")
    basis = pd.Series(pd.NA, index=index, dtype="object")

    col = roles.get("course_duration")
    course_col = roles.get("course")
    if col and col in df.columns:
        parsed = pd.to_numeric(df[col], errors="coerce")
        # A duration of zero or less is a blank typed as 0, not a course that
        # ends the day it starts.
        parsed = parsed.where(parsed > 0)
        days = days.where(parsed.isna(), parsed)
        basis = basis.where(parsed.isna(), "duration_column")

        # `Course Duration (IN DAYS)` lives on the timetable's MAIN sheet, so
        # the rows that need it most — the ones that left, in Not_Coming and
        # NOT TO ENTERTRAIN — are exactly the rows that lack it. Fill from the
        # institute's own recorded length for that course, taken as the median
        # over the rows that do carry it. This is measured from the data, not
        # assumed, so it ranks above any supplied table.
        if course_col and course_col in df.columns and parsed.isna().any():
            lookup = parsed.groupby(df[course_col]).median()
            lookup = lookup[lookup > 0]
            if not lookup.empty:
                filled = df[course_col].map(lookup)
                gap = days.isna() & filled.notna()
                days = days.where(~gap, filled)
                basis = basis.where(~gap, "course_median")

    if by_category:
        lookup = {str(k).strip().lower(): float(v) for k, v in by_category.items()}
        for role in ("student_category", "course"):
            cat_col = roles.get(role)
            if not cat_col or cat_col not in df.columns:
                continue
            mapped = (
                df[cat_col].astype("string").str.strip().str.lower().map(lookup)
            )
            fill = days.isna() & mapped.notna()
            days = days.where(~fill, mapped)
            basis = basis.where(~fill, f"category_default:{role}")

    return days, basis


def _staff_alumni(df: pd.DataFrame) -> pd.Series:
    if "is_staff_alumni" not in df.columns:
        return pd.Series(False, index=df.index)
    # Element-wise rather than .fillna().astype(): the column arrives as object
    # dtype after a multi-source concat, where fillna's downcast is deprecated.
    return df["is_staff_alumni"].map(
        lambda v: bool(v) if pd.notna(v) else False
    ).astype(bool)


def _tenure_days(
    start: pd.Series, deadline: pd.Series, status: pd.Series, as_of: pd.Timestamp
) -> pd.Series:
    """Days from course start to the event, or to the censoring date.

    For a churned row the event happened at `deadline` — the moment the window
    closed — not at `as_of`, which is merely when the report was run. Getting
    this wrong stretches every churn duration by however long ago the export
    was taken, and a Kaplan-Meier curve built on it is wrong everywhere.
    """
    endpoint = pd.Series(as_of, index=start.index)
    churned = status == "churned"
    endpoint = endpoint.where(~churned, deadline)
    return (endpoint - start).dt.days


def _summary(
    out: pd.DataFrame,
    status: pd.Series,
    membership: pd.Series,
    as_of: pd.Timestamp,
    as_of_source: str,
    grace_months: int,
    start_col: Optional[str],
    duration_basis: pd.Series,
    entertain_unconditional: bool,
) -> Dict[str, Any]:
    counts = {k: int(v) for k, v in status.value_counts().items()}
    at_risk = int(membership.isin(AT_RISK_STATUSES).sum())
    resolved = counts.get("churned", 0) + counts.get("paused", 0)
    unlabelled = counts.get("unlabelled", 0)

    notes: List[str] = []
    if unlabelled:
        notes.append(
            f"{unlabelled} of the {at_risk} row(s) that stopped attending have "
            f"no computable course end "
            f"({'no start date' if not start_col else 'no course duration'}), "
            f"so they are unlabelled — excluded from the churn rate rather than "
            f"counted as retained."
        )
    if counts.get("no_membership"):
        notes.append(
            f"{counts['no_membership']} row(s) appear in no lifecycle tab, so "
            f"they have no churn state to compute. That is a coverage gap in "
            f"the timetable workbook, not a missing duration."
        )
    if at_risk and not resolved:
        notes.append(
            "No churn label could be produced. The rule needs a course start "
            "AND a course length: supply the sheet carrying "
            "'Course Duration (IN DAYS)', or pass a duration-per-category "
            "table, and join a source with 'Date of Joining'."
        )
    if counts.get("staff_alumni"):
        notes.append(
            f"{counts['staff_alumni']} row(s) are staff hired from the student "
            f"body; they left the roster for the payroll, not the competition, "
            f"and are excluded from churn."
        )
    if entertain_unconditional:
        notes.append(
            "NOT TO ENTERTRAIN treated as churn unconditionally (the institute "
            "has ruled the student out), bypassing the grace window."
        )

    rate = None
    if resolved:
        rate = round(counts.get("churned", 0) / resolved, 4)

    return {
        "as_of": as_of.isoformat(),
        "as_of_source": as_of_source,
        "grace_months": grace_months,
        "start_column": start_col,
        "duration_basis": {
            str(k): int(v) for k, v in duration_basis.value_counts().items()
        },
        "counts": counts,
        "at_risk_rows": at_risk,
        "labelled_rows": resolved,
        "unlabelled_rows": unlabelled,
        # Share of the rows that STOPPED attending which have passed the
        # window. Not a roster-wide churn rate: active and completed students
        # were never at risk under this rule and are not in the denominator.
        "churn_rate_of_at_risk": rate,
        "notes": notes,
    }


def duration_table_from_months(months_by_category: Mapping[str, float]) -> Dict[str, float]:
    """Convert an operator's "this course runs N months" table into days.

    A convenience for the common case: the institute thinks in months, the rule
    is written in days. 30-day months, matching how the durations are quoted.
    """
    return {str(k): float(v) * 30 for k, v in months_by_category.items()}
