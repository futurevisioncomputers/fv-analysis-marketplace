"""What a sheet can and cannot answer — decided before anything is computed.

Stage 1 frames business questions from the operator's words alone; it never
looks at the data. So it will happily frame "how fast are certificates issued?"
against an admission form that has no certificate column anywhere. Today that
only surfaces at stage 4, as a skipped question in the finished report — after
the operator has waited through cleaning, EDA and analysis for an answer that
was never possible.

This module answers it at the first checkpoint instead: for each question, what
the metric needs, whether this source has it, and — when it does not — which of
the institute's other sheets carries the missing piece. That last part is the
whole point. "Not answerable" is a dead end; "not answerable from this sheet,
add fees-data" is an instruction.

Nothing here computes a metric or reads a full frame. It reads column headers
and a small sample, maps them to roles with the Data Engineer's own matcher,
and compares that set against a requirements table. Cheap enough to run at
stage 1, before the operator has committed to anything.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Mapping, Optional, Sequence

JsonDict = Dict[str, Any]

# Rows sampled per source. Enough for the Data Engineer's value-profiling to
# recognize a column whose header is unhelpful ("Column 1", "More Info"),
# cheap enough that this stays a header-speed operation on a 26k-row sheet.
SAMPLE_ROWS = 200

# What each metric needs, as alternative role-sets: satisfied when EVERY role
# in ANY ONE alternative is present. Derived from METRIC_SPECS plus the Data
# Engineer's derivations — a metric keyed on `is_admitted` really needs the
# admission date that flag is built from, and saying "needs is_admitted" would
# be useless to an operator looking at their spreadsheet.
#
# `date_any` is the pseudo-role satisfied by any event date, because the
# cleaner drops rows that have none and a count over zero rows is not a count.
METRIC_REQUIREMENTS: Dict[str, JsonDict] = {
    # admissions funnel
    "admission_conversion_rate": {"any_of": [["admission_date"], ["joining_date"]],
                                  "derives": "is_admitted"},
    "counselling_to_admission_rate": {"any_of": [["admission_date"], ["joining_date"]],
                                      "derives": "is_admitted"},
    "admissions_confirmed": {"any_of": [["date_any"]]},
    "total_leads": {"any_of": [["date_any"]]},
    "qualified_leads": {"any_of": [["date_any"]]},
    "walk_in_count": {"any_of": [["date_any"]]},
    "enquiry_backlog_rate": {"any_of": [["stale_enquiry"]],
                             "derives": "is_enquiry_backlog",
                             "caveat": "an enquiry counts as backlog once it is "
                                       "30 days old and still unconverted"},
    "lead_to_admission_days": {"any_of": [["enquiry_date", "admission_date"],
                                          ["enquiry_date", "joining_date"]]},
    # money
    "gross_fee_collected": {"any_of": [["paid"]]},
    "pending_fee": {"any_of": [["pending"]]},
    "overdue_fee": {"any_of": [["pending"]]},
    "average_fee_per_student": {"any_of": [["amount"]]},
    "default_rate": {"any_of": [["pending"]], "derives": "is_default"},
    "collection_efficiency": {"any_of": [["paid", "amount"], ["amount", "pending"]],
                              "derives": "amount_collected"},
    # lifecycle
    "dropout_rate": {"any_of": [["lifecycle_column"], ["completion_source"],
                                ["cancel_marker"]],
                     "derives": "is_dropped",
                     "caveat": "a dropout is anyone who left before completing "
                               "— a cancellation typed into a name, or a "
                               "lifecycle column reading Not Coming"},
    "completion_rate": {"any_of": [["lifecycle_column"], ["completion_source"]],
                        "derives": "is_completed"},
    "not_coming_rate": {"any_of": [["lifecycle_column"], ["completion_source"]],
                        "derives": "is_not_coming"},
    # Churn needs three things at once — which tab the student sits in, when
    # the course started, and how long it runs — and they live on different
    # sheets. Listing all three means stage 1 asks for the missing file instead
    # of the run producing an all-unlabelled column at stage 4.
    "churn_rate": {
        "any_of": [["completion_source", "course_duration", "joining_date"],
                   ["completion_source", "course_duration", "admission_date"]],
        "derives": "is_churn",
        "caveat": "the label is time-dependent — it is computed against an "
                  "as-of date recorded with the answer, and a row flips from "
                  "paused to churned with no edit to any sheet",
    },
    "repeat_enrollment_rate": {"any_of": [["repeat_person"]],
                               "derives": "is_repeat_enrollment",
                               "caveat": "identity needs a phone, DOB or email "
                                         "as well as a name — a name alone "
                                         "cannot separate namesakes"},
    # certificates
    "certificate_pending_rate": {"any_of": [["issue_date"]],
                                 "derives": "is_certificate_pending"},
    "duplicate_certificate_rate": {"any_of": [["certificate_number"]],
                                   "derives": "is_duplicate_certificate"},
    "certificate_issue_lag_days": {"any_of": [["issue_date", "joining_date"],
                                              ["issue_date", "admission_date"]]},
}

# Roles that count as an event date for `date_any`. DOB is excluded: it is a
# property of a person, not of anything that happened.
DATE_ROLES_FOR_ANY = ("admission_date", "joining_date", "enquiry_date",
                      "receipt_date", "issue_date")

# Which of the institute's sheets carries a role that is missing here. Turns
# "cannot answer" into "add this file". Ordered best source first.
ROLE_SOURCES: Dict[str, str] = {
    "admission_date": "the admission form, or student-data",
    "joining_date": "student-data, fees-data or certificate-data",
    "enquiry_date": "an enquiry form sheet (Form Responses 1 or 2)",
    "receipt_date": "fees-recpit (the receipt ledger)",
    "issue_date": "certificate-data",
    "certificate_number": "certificate-data",
    "paid": "fees-recpit (the receipt ledger)",
    "pending": "fees-data (the per-enrollment rollup)",
    "amount": "fees-data or fees-recpit",
    "student_mobile": "the admission form, or student-data",
    "dob": "the admission form",
    "email": "the admission form, or student-data",
    "name": "any student-level sheet",
    "status": "fees-data (fee status) or a timetable tab",
    "status_reason": "the Not_Coming timetable tab",
    "cancel_marker": "a sheet where cancellations are written into the name or "
                     "status cell — no sheet records them as their own column",
    "stale_enquiry": "an enquiry form sheet — this one has no unconverted "
                     "enquiry older than 30 days",
    "repeat_person": "a sheet with a name AND a phone, DOB or email, holding "
                     "someone who enrolled more than once",
    "completion_source": "the Student_Time_Table tabs "
                         "(Main_data / Course_Completed / Not_Coming / "
                         "NOT TO ENTERTRAIN) — membership is the label",
    "lifecycle_column": "a column holding the lifecycle per student "
                        "(Course Completed / Currently Learning / Not Coming). "
                        "One consolidated sheet with this column replaces the "
                        "four timetable tabs",
    "course_duration": "a sheet carrying 'Course Duration (IN DAYS)'. If no "
                       "sheet records it, pass a duration-per-course table "
                       "instead — churn cannot be dated without one",
    "date_any": "any sheet carrying a date",
}

# Roles that only exist once several sheets are supplied together, so a
# single-sheet run cannot produce them however good the sheet is.
CROSS_SHEET_ROLES = {"completion_source"}


def _sample(path: str, sheet_name: Optional[str] = None):
    """Read a few rows. Returns None rather than raising on an unreadable file."""
    import pandas as pd

    try:
        if sheet_name:
            return pd.read_excel(path, sheet_name=sheet_name, nrows=SAMPLE_ROWS,
                                 engine="openpyxl")
        # index_col=False for the same reason the cleaner uses it: a trailing
        # delimiter would otherwise shift every column one place left.
        return pd.read_csv(path, nrows=SAMPLE_ROWS, index_col=False)
    except Exception:  # noqa: BLE001 - an unreadable source is "no roles", not a crash
        return None


def _columns(path: str, sheet_name: Optional[str], wanted: Sequence[str]):
    """Read just these columns, in full. None when they cannot be read.

    Full rather than sampled: the flags these probes test for are rare (~4% of
    rows carry a cancellation marker), so a 200-row window would miss them on a
    large sheet and under-promise. One or two columns is cheap even at 26k rows.
    """
    import pandas as pd

    columns = [c for c in wanted if c]
    if not columns:
        return None
    try:
        if sheet_name:
            return pd.read_excel(path, sheet_name=sheet_name, usecols=columns,
                                 dtype=str, engine="openpyxl")
        return pd.read_csv(path, usecols=columns, dtype=str, index_col=False)
    except Exception:  # noqa: BLE001 - probe failure means "cannot promise it"
        return None


# Three flags are emitted only when at least one row qualifies, so their
# availability is a property of the VALUES, not the schema. Promising them from
# the columns alone puts a question through the checkpoint that then blocks in
# the Analyst — exactly the late failure this module exists to prevent. Each
# probe below answers "would this flag actually be emitted?".

def _probe_cancel_marker(path, sheet_name, roles) -> bool:
    """Is a cancellation or refund typed into a name / status cell anywhere?"""
    from .data_engineer_agent import STATUS_MARKERS

    frame = _columns(path, sheet_name,
                     [roles.get(r) for r in ("name", "status", "status_reason")])
    if frame is None:
        return False
    # is_cancelled covers refunds too — money already returned is still a
    # cancelled enrollment — so both markers count.
    patterns = [pattern for label, pattern in STATUS_MARKERS
                if label in ("cancelled", "refunded")]
    for col in frame.columns:
        values = frame[col].dropna().astype(str)
        if any(values.str.contains(pattern, regex=True).any()
               for pattern in patterns):
            return True
    return False


def _probe_stale_enquiry(path, sheet_name, roles) -> bool:
    """Is there an enquiry old enough and unconverted enough to be a backlog?

    A roster of admitted students carries enquiry dates but has no backlog at
    all — every row converted — so the flag is never emitted.
    """
    import pandas as pd

    from .data_engineer_agent import ENQUIRY_BACKLOG_DAYS

    enquiry = roles.get("enquiry_date")
    admission = roles.get("admission_date") or roles.get("joining_date")
    frame = _columns(path, sheet_name, [enquiry, admission])
    if frame is None or enquiry not in frame.columns:
        return False

    dates = pd.to_datetime(frame[enquiry], errors="coerce", dayfirst=True,
                           format="mixed")
    if dates.notna().sum() == 0:
        return False
    admitted = (frame[admission].notna() if admission in frame.columns
                else pd.Series(False, index=frame.index))
    age = (dates.max() - dates).dt.days
    return bool((dates.notna() & ~admitted & (age > ENQUIRY_BACKLOG_DAYS)).any())


def _probe_repeat_person(path, sheet_name, roles) -> bool:
    """Does anyone actually appear twice under a key that separates namesakes?"""
    from .data_engineer_agent import DataEngineerAgent

    name = roles.get("name")
    discriminator = next((roles[r] for r in ("student_mobile", "dob", "email")
                          if r in roles), None)
    frame = _columns(path, sheet_name, [name, discriminator])
    if frame is None or name not in frame.columns or discriminator is None:
        return False

    # Normalize exactly as the Data Engineer will. Raw values disagree on
    # spacing, case and phone formatting, so comparing them literally finds no
    # repeats where the cleaner finds several.
    engineer = DataEngineerAgent
    normalize = (engineer._normalize_phone_digits
                 if discriminator == roles.get("student_mobile")
                 else engineer._normalize_identity_date
                 if discriminator == roles.get("dob")
                 else engineer._normalize_identity_email)
    names = frame[name].map(engineer._normalize_person_name)
    keys = names + "|" + frame[discriminator].map(normalize)
    return bool(keys[names != ""].duplicated().any())


def _probe_lifecycle_column(
    path: str, sheet: Optional[str], roles: Mapping[str, str]
) -> bool:
    """Whether a column on this sheet holds the student lifecycle.

    Runs the Data Engineer's own detector on a sample, so stage 1 promises
    exactly what stage 2 will find — the alternative is a checkpoint saying
    completion rate is available and an Analyst that then cannot compute it.
    """
    from .data_engineer_agent import DataEngineerAgent

    frame = _sample(path, sheet)
    if frame is None or frame.empty:
        return False
    return DataEngineerAgent._derive_completion_from_column(frame.copy(), [])


VALUE_PROBES = {
    "cancel_marker": _probe_cancel_marker,
    "stale_enquiry": _probe_stale_enquiry,
    "repeat_person": _probe_repeat_person,
    "lifecycle_column": _probe_lifecycle_column,
}


def available_roles(sources: Sequence[Mapping[str, Any]]) -> Dict[str, List[str]]:
    """Role -> the source names that provide it, across every supplied sheet.

    Uses the Data Engineer's own detector so this agrees with what cleaning
    will actually find, including columns recognized by their values rather
    than their headers.
    """
    from .data_engineer_agent import DataEngineerAgent

    engineer = DataEngineerAgent()
    found: Dict[str, List[str]] = {}
    for source in sources:
        path = source.get("path_or_query") or source.get("path") or ""
        if not path or not os.path.exists(str(path)):
            continue
        frame = _sample(str(path), source.get("sheet_name"))
        if frame is None or frame.empty:
            continue
        name = str(source.get("name") or os.path.basename(str(path)))
        roles = engineer._detect_roles(frame)
        # Apply the same context resolution cleaning will: on student-data the
        # `Timestamp` column is the join key, not an enquiry date, so without
        # this the checkpoint promises a lead-to-admission time the Analyst
        # cannot produce.
        engineer._specialize_roles(frame, roles, [])
        for role in roles:
            found.setdefault(role, []).append(name)
        for probe_role, probe in VALUE_PROBES.items():
            if probe(str(path), source.get("sheet_name"), roles):
                found.setdefault(probe_role, []).append(name)

    if any(role in found for role in DATE_ROLES_FOR_ANY):
        found["date_any"] = sorted({
            src for role in DATE_ROLES_FOR_ANY for src in found.get(role, [])})

    # Completion labels come from which timetable tab a student sits in, so the
    # signal is the set of sources, not a column inside one.
    tabs = [str(s.get("sheet_name") or s.get("name") or "").lower()
            for s in sources]
    if any("not_coming" in t or "course_completed" in t or "entertain" in t
           for t in tabs):
        found["completion_source"] = [t for t in tabs if t]
    return found


def metric_capability(metric: str, roles: Mapping[str, List[str]]) -> JsonDict:
    """Can this metric be computed from these roles? If not, what is missing."""
    spec = METRIC_REQUIREMENTS.get(metric)
    if spec is None:
        # Unlisted metrics fall back to a record count in the Analyst, which
        # needs a date like every other count.
        spec = {"any_of": [["date_any"]]}

    best: List[str] = []
    for alternative in spec["any_of"]:
        missing = [role for role in alternative if role not in roles]
        if not missing:
            return {"metric": metric, "available": True,
                    "using": list(alternative),
                    "caveat": spec.get("caveat")}
        if not best or len(missing) < len(best):
            best = missing

    return {
        "metric": metric,
        "available": False,
        "missing": best,
        "needs": [{"role": role,
                   "found_in": ROLE_SOURCES.get(role, "an unknown sheet"),
                   "cross_sheet": role in CROSS_SHEET_ROLES}
                  for role in best],
        "caveat": spec.get("caveat"),
    }


def question_capability(question: Mapping[str, Any],
                        roles: Mapping[str, List[str]]) -> JsonDict:
    """Whether a question is answerable, and on which of its metrics.

    A question carries a metric list: the first is what it asks for, the rest
    are fallbacks the Analyst will try. So the question is answerable if ANY of
    them is, and it matters which — answering by the third fallback is a
    different answer from the one asked for.
    """
    metrics = list(question.get("metrics") or [])
    checked = [metric_capability(metric, roles) for metric in metrics]
    usable = [c for c in checked if c["available"]]

    result: JsonDict = {
        "question_id": question.get("question_id"),
        "question": question.get("question"),
        "answerable": bool(usable),
        "asked_for": metrics[0] if metrics else None,
        "metrics": checked,
    }
    if usable:
        result["will_answer_with"] = usable[0]["metric"]
        result["substituted"] = bool(metrics) and usable[0]["metric"] != metrics[0]
        result["caveats"] = [c["caveat"] for c in usable[:1] if c.get("caveat")]
    else:
        # Report the shortest path to an answer, not every missing role.
        gaps: Dict[str, JsonDict] = {}
        for check in checked:
            for need in check.get("needs") or []:
                gaps.setdefault(need["role"], need)
        result["needs"] = list(gaps.values())
    return result


def data_needs(brief: Mapping[str, Any],
               sources: Sequence[Mapping[str, Any]]) -> JsonDict:
    """The stage-1 capability answer: what these questions need from this data.

    Returned at the first checkpoint so the operator can add a sheet, drop a
    question, or accept the substitution *before* the pipeline runs.
    """
    roles = available_roles(sources)
    questions = [question_capability(q, roles)
                 for q in (brief.get("business_questions") or [])]

    answerable = [q for q in questions if q["answerable"]]
    blocked = [q for q in questions if not q["answerable"]]

    # One deduplicated shopping list: the roles that would unlock the most.
    #
    # Built from every unavailable metric, not only from wholly blocked
    # questions. A question can be "answerable" on a substitute while the
    # metric that was actually asked for is missing — ask for churn, get
    # completion_rate — and the substitution is silent unless the missing role
    # reaches this list. `substituted` on the question says it happened;
    # this says what to send to fix it.
    wanted: Dict[str, JsonDict] = {}
    for question in questions:
        if not question["answerable"]:
            # Nothing in the question works: the question-level need already
            # names the shortest path, so per-metric detail would only repeat.
            pairs = [(question["question_id"], need)
                     for need in question.get("needs") or []]
        else:
            pairs = [(f"{question['question_id']}:{check['metric']}", need)
                     for check in question.get("metrics") or []
                     if not check.get("available")
                     for need in check.get("needs") or []]
        for unlocks, need in pairs:
            entry = wanted.setdefault(need["role"], {**need, "unlocks": []})
            if unlocks not in entry["unlocks"]:
                entry["unlocks"].append(unlocks)

    return {
        "roles_present": sorted(roles),
        "sources": [str(s.get("name") or s.get("path") or "") for s in sources],
        "answerable": len(answerable),
        "blocked": len(blocked),
        "questions": questions,
        "missing_data": sorted(wanted.values(),
                               key=lambda w: (-len(w["unlocks"]), w["role"])),
    }
