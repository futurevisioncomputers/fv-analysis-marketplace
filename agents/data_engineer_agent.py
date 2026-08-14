"""Agent 2: Data Engineer.

Consumes a ProblemDefinitionBrief (or AnalysisBrief) plus a path to a source CSV
and produces a single trusted canonical dataframe + a data-quality report
(`DataPackage`).

Design goals (from seven_agent_system_design.md, Agent 2):
- One canonical dataframe per query, written to parquet.
- Quality report: drop count, null rates, dedup keys, known issues.
- PII boundary: this is the only agent that sees raw PII. Name / mobile / address
  are masked to hashed IDs before output.
- Escalate `status: blocked` if canonical column mapping fails or row count drops
  more than 10%.

This implementation is column-role generic: it auto-detects roles from column
names + content (like the csv-*-report skills) instead of hardcoding the
admissions schema, then applies the doc's cleaning rules wherever the matching
role is present.
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from . import canonical_maps, lifecycle


JsonDict = Dict[str, Any]

# Fraction of rows we may drop before declaring the clean untrustworthy.
MAX_DROP_FRACTION = 0.10

# Column-role detection keywords (substring match on lowercased header).
# Role detection. Order matters: roles are matched top-to-bottom and each source
# column is claimed by at most one role, so specific roles (course_category,
# parent_mobile, receipt_id) must precede their generic cousins (course, mobile,
# student_id). Each role carries:
#   include: substrings; the header must contain at least one.
#   exclude: substrings; the header must contain none (blacklist).
ROLE_SPECS: Dict[str, Dict[str, List[str]]] = {
    # --- dates (specific first; DOB explicitly excluded everywhere) ---
    "dob": {"include": ["date of birth", "dob", "birth date"], "exclude": []},
    "admission_date": {
        "include": ["date of admission", "admission_date"],
        "exclude": ["birth"],
    },
    "joining_date": {
        "include": ["date of joining", "joining", "doj", "enrolled"],
        "exclude": ["birth"],
    },
    "receipt_date": {"include": ["date of receipt", "receipt date"], "exclude": []},
    "issue_date": {
        "include": ["certificate issue", "issue date", "issued"],
        "exclude": [],
    },
    "enquiry_date": {
        "include": ["timestamp", "enquiry", "lead_date", "created"],
        "exclude": ["birth"],
    },
    # --- identifiers (specific id types before generic) ---
    "receipt_id": {"include": ["receipt id", "receipt-id"], "exclude": []},
    "certificate_number": {
        "include": ["certificate number", "certificate no", "certificate-id"],
        "exclude": [],
    },
    "student_id": {"include": ["student-id", "student id", "student_id"], "exclude": []},
    # --- course vs course category (category MUST win for its column) ---
    "course_category": {"include": ["course category", "category"], "exclude": []},
    "course": {
        "include": ["course", "which course", "program", "service", "product"],
        "exclude": ["category", "duration", "remain"],
    },
    "course_duration": {"include": ["course duration", "duration"], "exclude": []},
    "days_remaining": {"include": ["days remaning", "days remaining", "remain"], "exclude": []},
    # --- money (fee sheets) ---
    "amount": {
        "include": ["total fees", "total fee", "amount", "revenue", "price"],
        "exclude": ["pending", "paid"],
    },
    "paid": {"include": ["paid amt", "paid", "collected", "received"], "exclude": []},
    "pending": {
        "include": ["amt pending", "pending", "due", "outstanding", "balance"],
        "exclude": [],
    },
    "payment_mode": {"include": ["mode of payment", "payment mode"], "exclude": []},
    # Fee-ledger free text ("paid to ICICI", "razorpay emi", "2400 refunded").
    # Kept as text; payment_channel / is_refund_entry are parsed from it.
    "description": {"include": ["description", "narration", "particular"], "exclude": []},
    # Timetable churn sheets: "Status & reason" / "reason for not coming" hold
    # free-text progress + churn reasons. Must precede generic `status`.
    "status_reason": {
        "include": ["status & reason", "reason for not coming"],
        "exclude": [],
    },
    "status": {"include": ["status", "stage"], "exclude": []},
    # Mode is written three different ways across the estate. The specific
    # headers win here; a bare "Mode" stays generic and is resolved from its
    # values by `_specialize_roles`, because the institute uses it for the
    # online/offline delivery mode on student-data and for something else
    # entirely on the enquiry sheets.
    "enquiry_mode": {"include": ["mode of enquiry", "enquiry mode"], "exclude": []},
    "admission_mode": {"include": ["mode of admission", "admission mode"],
                       "exclude": []},
    "mode": {"include": ["mode"], "exclude": ["payment", "enquiry", "admission"]},
    # --- contact / PII ---
    "email": {"include": ["email", "e-mail"], "exclude": []},
    # Two guardian numbers exist on the admission form (father and mother) and
    # only one role used to claim them, so the second was masked as an
    # anonymous `discovered_pii_N` and could never be used as a fallback
    # contact. Father / "guardian 1" / a bare "parent" is the first; mother /
    # "guardian 2" / "secondary contact" is the second.
    "parent_mobile": {
        "include": [
            "mobile no (parent",
            "mobile no (father",
            "guardian 1",
            "parent",
            "guardian",
        ],
        "exclude": ["mother", "guardian 2"],
    },
    "parent_mobile_2": {
        "include": ["mobile no (mother", "mother", "guardian 2",
                    "secondary contact"],
        "exclude": [],
    },
    "student_mobile": {
        "include": ["mobile no (student", "mobile", "phone", "contact no", "whatsapp"],
        "exclude": [],
    },
    # Residential area is a locality ("Adajan", "Vesu") — a usable dimension.
    # The full address is PII. Claim the area first so it is not masked away.
    "residential_area": {"include": ["residential area", "area"],
                         "exclude": ["address"]},
    "address": {
        "include": ["address", "residential", "street", "addr"],
        "exclude": [],
    },
    "pincode": {"include": ["pincode", "pin code", "zip", "postal"], "exclude": []},
    "photo": {"include": ["photo", "image"], "exclude": []},
    # --- name -------------------------------------------------------------
    # `counsellor` MUST precede `name`. Enquiry Form Responses 2 has no student
    # name column at all, so "Counsellor Name" won the `name` role and person
    # identity was built from the counsellor: "4 repeat enrollments" meant one
    # counsellor handling four enquiries. With the role split, that sheet has
    # no name role and person-grain metrics are correctly withheld.
    "counsellor": {"include": ["counsellor", "counselor"], "exclude": []},
    "name": {
        "include": ["student name", "name of student", "name"],
        "exclude": ["google", "course", "tutor", "faculty", "branch", "category",
                    "counsellor", "counselor"],
    },
    # --- categoricals ---
    # A preference stated on a form is not where the student ended up. Keeping
    # them apart is what lets demand be compared against actual supply; merged,
    # a branch report silently mixes the two.
    "preferred_branch": {"include": ["preferred branch"], "exclude": []},
    "branch": {
        "include": ["branch", "centre", "center", "region", "office"],
        "exclude": ["preferred"],
    },
    "source": {
        "include": ["from where", "source", "channel", "referral", "utm", "how did"],
        "exclude": [],
    },
    # Tutor and Faculty are the same person in different sheets' vocabulary, so
    # they share one role; which word the sheet used is kept as `staff_role`.
    "faculty": {
        "include": ["faculty", "tutor", "trainer", "assigned to", "agent"],
        "exclude": [],
    },
    "student_category": {"include": ["student category", "category"],
                         "exclude": ["course"]},
    "education": {"include": ["education level", "education", "qualification"], "exclude": ["details"]},
    "occupation": {"include": ["presently what", "occupation", "currently doing"], "exclude": []},
    "education_details": {"include": ["education details"], "exclude": []},
    "preferred_batch_time": {"include": ["preferred batch time", "preferred batch"],
                             "exclude": []},
    "batch_time": {"include": ["batch timing", "batch time", "slot", "shift"],
                   "exclude": ["preferred"]},
    "preferred_days": {"include": ["preferred days"], "exclude": ["remain"]},
    # A bare "Days" is the timetable's actual class days, not a preference.
    "class_days": {"include": ["days"], "exclude": ["remain", "preferred", "duration"]},
    "coupon_given": {"include": ["coupon"], "exclude": []},
    "notes": {"include": ["any other notes", "notes", "more info"], "exclude": []},
}

# The institute's own vocabulary for each internal role. Internal keys stay as
# they are — METRIC_SPECS and every derivation are keyed on them — but anything
# an operator reads should use the name they gave it. Emitted on the data
# package as `field_names` so reports and skills can render their words.
CANONICAL_FIELD_NAMES: Dict[str, str] = {
    "name": "student_name",
    "student_id": "student_id",
    "student_mobile": "student_phone",
    "parent_mobile": "guardian_phone_1",
    "parent_mobile_2": "guardian_phone_2",
    "email": "student_email",
    "address": "student_address",
    "residential_area": "residential_area",
    "pincode": "pincode",
    "education": "education_level",
    "education_details": "education_details",
    "occupation": "current_occupation",
    "course": "course",
    "course_category": "course_category",
    "course_duration": "course_duration_days",
    "days_remaining": "days_remaining",
    "preferred_branch": "preferred_branch",
    "branch": "branch",
    "counsellor": "counsellor_name",
    "faculty": "faculty_name",
    "preferred_days": "preferred_days",
    "class_days": "class_days",
    "preferred_batch_time": "preferred_batch_time",
    "batch_time": "batch_timing",
    "enquiry_date": "enquiry_date",
    "record_timestamp": "record_timestamp",
    "admission_date": "admission_date",
    "joining_date": "joining_date",
    "dob": "date_of_birth",
    "issue_date": "certificate_issue_date",
    "certificate_number": "certificate_number",
    "receipt_id": "receipt_id",
    "receipt_date": "receipt_date",
    "amount": "total_fee",
    "paid": "paid_amount",
    "pending": "amount_pending",
    "status": "fee_status",
    "payment_mode": "payment_mode",
    "source": "lead_source",
    "enquiry_mode": "enquiry_mode",
    "admission_mode": "admission_mode",
    "mode": "learning_mode",
    "notes": "notes",
    "description": "description",
    "status_reason": "churn_status_reason",
    "student_category": "student_category",
    "coupon_given": "coupon_given",
    "photo": "student_photo",
}

# Roles whose raw values are PII and must be hashed before output.
PII_ROLES = (
    "name",
    "student_mobile",
    "parent_mobile",
    "email",
    "address",
    "dob",
    "photo",
)

# Safety-net PII detection by header keyword, for columns that never won a role
# (e.g. a second guardian mobile or a second photo). Note: deliberately narrow —
# must not catch analytical columns like branch/course names.
PII_HEADER_KEYWORDS = (
    "name for google",
    "mobile no",
    "phone",
    "contact no",
    "whatsapp",
    "email",
    "e-mail",
    "address",
    "residential",
    "date of birth",
    "photo",
    "aadhaar",
    "aadhar",
)

# Roles treated as categorical for lowercase+strip normalization.
CATEGORICAL_ROLES = (
    "branch",
    "source",
    "status",
    "course",
    "course_category",
    "faculty",
    "education",
    "occupation",
    "mode",
    "payment_mode",
    "batch_time",
    "preferred_days",
)

# Date roles in priority order for picking the canonical "primary" date.
DATE_ROLES = (
    "admission_date",
    "joining_date",
    "receipt_date",
    "issue_date",
    "enquiry_date",
    "dob",
)

# Numeric/money roles to coerce (strip currency, commas) before analysis.
MONEY_ROLES = ("amount", "paid", "pending")

# Roles whose cells may hold several comma-separated values (e.g. Faculty =
# "Mansi Mam, Yash Sir"). For each, a non-destructive `<role>_list` column of
# parsed lists is added so downstream can explode the dimension it needs without
# changing the record grain here. Split on comma only; these roles never contain
# an in-value comma (unlike batch_time slots or receipt-id pairs).
MULTIVALUE_ROLES = ("faculty", "course", "preferred_days")

# --- Phase 1: value-based discovery of columns no header keyword claimed -----
# When a header matches no ROLE_SPEC, infer the column's NATURE from its values
# so renamed / cryptic / foreign-language headers are still cleaned, masked, and
# profiled instead of being silently dropped. Discovered columns get generic
# role keys (discovered_dim_N / discovered_num_N / discovered_date_N) that EDA
# profiles. A value that looks like contact PII (phone/email) is masked and
# never exposed as an analysable metric (no-PII-leak boundary preserved).
DISCOVERY_SAMPLE_ROWS = 500          # cap rows sniffed per column (speed)
DISCOVERY_PII_MIN_FRAC = 0.6         # >= this share looks like phone/email -> PII
DISCOVERY_DATE_MIN_FRAC = 0.6        # >= this share parses as a date -> date
DISCOVERY_NUM_MIN_FRAC = 0.8         # >= this share is numeric -> numeric
DISCOVERY_DIM_MAX_CARDINALITY = 50   # categorical only if distinct values <= this
DISCOVERY_ID_UNIQUE_FRAC = 0.95      # mostly-unique numeric -> identifier, skip

# A value is "date-ish" only if it carries a separator or a month name; this
# stops bare integers (years, counts, ids) from being mis-read as dates.
# pandas' placeholder for a column with no header. Never a keyword match.
_UNNAMED_HEADER_RE = re.compile(r"^unnamed:\s*\d+$")

_DATEISH_RE = r"[/\-]|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"
_EMAIL_VALUE_RE = r"[^@\s]+@[^@\s]+\.[^@\s]+"

# Status markers sometimes embedded in the student name, e.g.
# "Patel Sai Ashokbhai (cancelled)". Captured into an `is_cancelled` flag before
# the name is hashed, and stripped from the name so the hash is clean.
CANCEL_MARKER_RE = re.compile(
    r"\(?\s*(?:cancel+ed|cancell?ed|cancel|canceled|cancled|left|dropped|discontinued)\s*\)?",
    re.IGNORECASE,
)

# Lifecycle markers embedded in name parentheticals across the real sheets:
# "(admission cancelled all refunded)", "(not coming)", "(Register for trial ...)".
# Ordered by priority: within one marker text, the first matching label wins,
# so "cancelled all refunded" resolves to refunded (money already returned).
STATUS_MARKERS: List[Tuple[str, re.Pattern]] = [
    ("refunded", re.compile(r"refund", re.IGNORECASE)),
    ("cancelled", CANCEL_MARKER_RE),
    ("not_coming", re.compile(r"not\s+coming", re.IGNORECASE)),
    ("trial", re.compile(r"register(?:ed)?\s+for\s+trial|\btrial\b", re.IGNORECASE)),
]

# Parenthetical chunk in a name cell; inspected for lifecycle markers.
_NAME_PAREN_RE = re.compile(r"\(([^)]*)\)")

# Operational notes (NOT lifecycle) embedded in name parentheticals across the
# timetable sheets: "(fast track)", "(FT till july end)", "(only till 30 may)",
# "(ft) 30/6". Fast-track markers set `is_fast_track`; schedule notes are just
# stripped so the name hash stays stable for the same person.
FAST_TRACK_MARKER_RE = re.compile(r"fast\s*track|\bft\b", re.IGNORECASE)
NOTE_MARKER_RE = re.compile(
    r"only\s+till|till\s+\w+|don'?t\s+delete|register(?:ed)?\s+for", re.IGNORECASE
)
# Residue after stripping "(ft) 30/6"-style parens: a trailing bare date chunk.
_TRAILING_DATE_FRAGMENT_RE = re.compile(r"\s+\d{1,2}[/-]\d{1,2}\s*$")

# Placeholder rows the institute keeps for sheet dropdowns:
# "zzzzz (Don't Delete)". Pure structural junk — purged before cleaning and
# excluded from the drop-fraction escalation math.
PLACEHOLDER_NAME_RE = re.compile(r"^\s*z{3,}", re.IGNORECASE)

# Lifecycle ground truth carried by the timetable workbook's SHEET, not any
# column: Course_Completed -> completed, Not_Coming -> not_coming,
# NOT TO ENTERTRAIN -> not_to_entertain, Main_data -> active. Matched against
# the source name (substring, lowered).
#
# ORDER MATTERS: every tab name also contains "timetable" when exported as
# `student_timetable__<tab>.csv`, so the specific tab needles must all precede
# the generic workbook ones or every tab reads as 'active'.
COMPLETION_BY_SOURCE = (
    ("complete", "completed"),
    ("not_coming", "not_coming"),
    ("not coming", "not_coming"),
    ("entertain", "not_to_entertain"),
    ("main_data", "active"),
    ("time_table", "active"),
    ("timetable", "active"),
)

# The same lifecycle, written as a COLUMN instead of as sheet membership.
#
# Sheet membership was the only label the institute's original timetable
# workbook carried, so `_derive_completion_status` read the source name. Once
# the sheets are consolidated onto a student master the label becomes an
# ordinary column, and reading it is strictly better: it is per-row, it
# survives a join, and one sheet can then hold every lifecycle state.
#
# `unknown` maps to None deliberately. A student whose status nobody recorded
# is not active — treating "we don't know" as "still learning" would quietly
# inflate the completion denominator with people who left years ago.
LIFECYCLE_VALUES = {
    "course completed": "completed",
    "completed": "completed",
    "complete": "completed",
    "finished": "completed",
    "currently learning": "active",
    "learning": "active",
    "ongoing": "active",
    "active": "active",
    "running": "active",
    "not coming": "not_coming",
    "not_coming": "not_coming",
    "discontinued": "not_coming",
    "dropped": "not_coming",
    "left": "not_coming",
    "not to entertain": "not_to_entertain",
    "not to entertrain": "not_to_entertain",   # the institute's own spelling
    "unknown": None,
}

# How much of a column must speak this vocabulary before it is read as the
# lifecycle column, and how many distinct states it must show. A column that
# is 100% one value labels nothing and is more likely a coincidence.
LIFECYCLE_MATCH_FLOOR = 0.60
LIFECYCLE_MIN_STATES = 2

# Attributes the institute changes mid-enrollment: a student moves branch,
# switches batch timing, or is handed to a different tutor, and the sheet is
# overwritten in place. There is no history column, so each of these holds the
# LATEST value, not the one that was true when the student joined.
#
# Two consequences, and both are reported rather than silently accepted:
# - they must never be part of a join key (the join then fails precisely on the
#   students who moved, who are the ones a retention report cares about), and
# - a breakdown by them is an as-of snapshot. Revenue "by branch" credits the
#   branch the student sits in today for fees paid at the one they left.
MUTABLE_ATTRIBUTE_ROLES = ("branch", "faculty", "batch_time", "class_days")

# The four timetable tabs are ONE roster split by membership, not four related
# tables. Joining them keeps only the master's rows (a real run kept 16 of 406);
# they have to be unioned. Precedence resolves a student left in two tabs — a
# terminal state beats a staging one, because Main_data is where rows sit until
# somebody moves them.
LIFECYCLE_PRECEDENCE = ("completed", "not_to_entertain", "not_coming", "active")

# Payment channel buried in receipt Description prose ("paid to ICICI",
# "razorpay emi", "cheque no 123", "paid to sc"). Ordered: first match wins,
# so "razorpay emi to icici" classifies as emi, not bank_transfer.
PAYMENT_CHANNEL_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("emi", re.compile(r"razorpay|\bemi\b|bajaj", re.IGNORECASE)),
    ("cheque", re.compile(r"cheque|\bchq\b|check\s*no", re.IGNORECASE)),
    ("upi", re.compile(r"\bupi\b|gpay|google\s*pay|phonepe|paytm", re.IGNORECASE)),
    ("bank_transfer", re.compile(r"icici|hdfc|\bsbi\b|axis|kotak|neft|imps|rtgs|\bbank\b", re.IGNORECASE)),
    ("cash", re.compile(r"\bcash\b", re.IGNORECASE)),
]

# Refund facts also live in Description ("2400 refunded", "refund from icici").
REFUND_ENTRY_RE = re.compile(r"refund", re.IGNORECASE)

# Rupee tolerance when checking total = paid + pending (rounding in sheets).
RECON_TOLERANCE = 1.0

# Default-aging buckets (days since a debtor's last payment). An enrollment with
# pending > 0 falls into the first bucket whose upper bound it is under; >90 is
# the tail. Reference "as of" date is the ledger's own latest receipt, not wall
# clock, because the export is historical.
AGING_BUCKETS = ((30, "0-30"), (60, "31-60"), (90, "61-90"))
AGING_OVERDUE_BUCKET = "90+"

# An enquiry with no admission that is older than this many days (relative to the
# frame's own latest enquiry, since the export is historical) is a stale backlog
# lead. 30 days = roughly one intake cycle without follow-through.
ENQUIRY_BACKLOG_DAYS = 30

# Derived columns the downstream agents (Analyst rate/mean metrics, Prediction,
# Monitoring) look up by their BARE name. A left join prefixes right-source
# columns to `<source>__<col>`, so after a multi-sheet merge these only exist
# prefixed and the metric silently skips. They are coalesced back to the bare
# name on the merged master (first non-null across sources, since a student
# belongs to one source per flag — e.g. exactly one timetable tab).
COALESCE_DERIVED_COLUMNS = (
    "completion_status", "is_completed", "is_not_coming", "is_default",
    "is_certificate_pending", "certificate_delay_days", "is_duplicate_certificate",
    "is_enquiry_backlog", "is_admitted", "is_enquiry", "is_repeat_enrollment",
    "person_enrollment_count", "is_cancelled", "is_fee_paid", "is_fast_track",
    "amount_collected", "enrollment_status",
)

# person_id key normalization: names keep letters/digits/spaces only; phones
# keep the last 10 digits (drops +91 / spaces / hyphens / float ".0" artifacts).
_PERSON_NAME_NORM_RE = re.compile(r"[^a-z0-9 ]+")
_NON_DIGIT_RE = re.compile(r"\D")

# PII v2 — inline scrubbing of RETAINED free-text columns. Column-level hashing
# (_mask_pii) masks whole PII columns; this catches a phone or email typed inside
# an analytical note we keep (a Not_Coming "Status & reason", a receipt
# Description) so no raw contact detail survives into the parquet or the report.
# The pattern is deliberately permissive (spaces / +91 / brackets / hyphens); a
# digit-count guard (>= _MIN_PHONE_DIGITS) then rejects dates, pincodes, ids and
# fee amounts that share the same cell, so only real phone runs are redacted.
_INLINE_PHONE_RE = re.compile(r"\+?\d[\d\s().\-]{8,14}\d")
_INLINE_EMAIL_RE = re.compile(r"[\w.\-+]+@[\w\-]+\.[\w.\-]+")
_MIN_PHONE_DIGITS = 10
# Only columns whose header signals free text are scrubbed, so id / amount / date
# columns are never mangled by a coincidental long digit run.
_FREE_TEXT_HEADER_RE = re.compile(
    r"remark|note|comment|reason|narration|particular|feedback|message|query|description",
    re.IGNORECASE,
)


class DataEngineerAgent:
    """Cleans a source CSV into a canonical dataframe + quality report."""

    def __init__(
        self,
        output_dir: Optional[str] = None,
        salt: str = "fv-institute",
        churn_config: Optional[Mapping[str, Any]] = None,
    ) -> None:
        """Create the agent.

        Args:
            output_dir: Directory for the canonical parquet. Defaults to the
                system temp dir.
            salt: Salt mixed into PII hashing so masked IDs are not trivially
                reversible across datasets.
            churn_config: Options for the time-dependent churn label —
                `as_of` (defaults to the latest date in the data, because these
                are historical exports), `grace_months`,
                `duration_days_by_category`, `entertain_unconditional`.
                See `agents/lifecycle.py`.
        """

        self.output_dir = output_dir or os.path.join(os.path.sep, "tmp")
        self.salt = salt
        self.churn_config = dict(churn_config or {})

    # ------------------------------------------------------------------ run

    def run_sources(
        self,
        brief: Any,
        data_sources: Sequence[Mapping[str, Any]],
        join_plan: Optional[Sequence[Mapping[str, Any]]] = None,
        date_format: Optional[str] = None,
        split_multivalue: bool = True,
    ) -> JsonDict:
        """Produce a DataPackage from multiple CSV / Excel-sheet sources.

        Each source is cleaned independently with the same PII masking rules as
        the legacy single-CSV path. Safe relationship joins are then applied to a
        student-id-centered master frame; unjoined sources remain available in
        `source_packages` and are summarized for the dashboard.
        """
        if not data_sources:
            return self._blocked("No data sources provided.", row_count=0)

        packages: List[JsonDict] = []
        frames: Dict[str, pd.DataFrame] = {}
        issues: List[str] = []
        for source in data_sources:
            name = str(source.get("name") or "source")
            domain = str(source.get("domain") or self._infer_source_domain(source))
            try:
                raw = self._read_source_frame(source)
            except Exception as exc:  # noqa: BLE001 - surface source read failure
                issues.append(f"Skipped source '{name}': {exc}")
                continue
            if raw.empty:
                issues.append(f"Skipped empty source '{name}'")
                continue

            package = self._clean_raw_frame(
                brief, raw, self._source_output_name(source, name),
                date_format=date_format,
                split_multivalue=split_multivalue,
                source_name=name,
                source_domain=domain,
                # The churn rule reads the lifecycle tab and the course length
                # from different sheets; it runs once, on the merged frame.
                derive_churn=False,
            )
            package["source_name"] = name
            package["source_domain"] = domain
            packages.append(package)
            if package.get("status") == "ready":
                frames[name] = pd.read_parquet(package["canonical_df_path"])
            else:
                issues.extend((package.get("quality_report") or {}).get("known_issues", []))

        ready = [p for p in packages if p.get("status") == "ready"]
        if not ready:
            return self._blocked(
                "No usable source remained after multi-source cleaning.",
                row_count=0,
                quality_extra={"known_issues": issues or ["All sources were blocked."]},
            )

        # Stack membership tabs of one roster BEFORE any join is considered —
        # they are not related tables and a join keeps only one tab's rows.
        frames, ready = self._union_lifecycle_partitions(frames, ready, issues)

        merged, relationships, join_issues = self._build_joined_frame(
            frames, ready, join_plan or []
        )
        issues.extend(join_issues)

        # Now — and only now — is the whole rule visible in one frame.
        canonical_columns = self._merged_roles(ready, merged)
        churn_summary = self._derive_churn_labels(merged, canonical_columns, issues)
        # Same reason: identity comes from the student sheet and the outstanding
        # balance from the fee sheet, so "how much receivable sits on superseded
        # admissions" is only answerable after the join.
        if "is_repeat_enrollment" in merged.columns:
            self._derive_current_admission(merged, canonical_columns, issues)

        stem = "multi_source"
        if data_sources:
            first_path = data_sources[0].get("path_or_query") or data_sources[0].get("name") or stem
            stem = os.path.splitext(os.path.basename(str(first_path)))[0] or stem
        canonical_path = self._write_parquet(merged, f"{stem}_joined.csv")

        source_summary = self._source_summary(ready, frames, relationships)
        domain_metrics = self._domain_metrics(ready, frames)
        payment_reconciliation = self._build_payment_reconciliation(ready, frames)
        enquiry_conversion = self._build_enquiry_conversion(ready, frames)
        return {
            "status": "ready",
            "canonical_df_path": canonical_path,
            "row_count": len(merged),
            "schema": {col: str(dtype) for col, dtype in merged.dtypes.items()},
            "churn_summary": churn_summary,
            "quality_report": {
                "original_row_count": sum(
                    (p.get("quality_report") or {}).get("original_row_count", 0)
                    for p in ready
                ),
                "drop_count": sum(
                    (p.get("quality_report") or {}).get("drop_count", 0)
                    for p in ready
                ),
                "dropped_reasons": {},
                "null_rates": self._null_rates(merged),
                "deduplication_keys": [],
                "known_issues": issues,
            },
            "canonical_columns": canonical_columns,
            # The institute's own name for each mapped column, so operator-
            # facing output uses their vocabulary rather than the internal
            # role keys the metrics are wired to.
            "field_names": self._field_names(canonical_columns),
            "time_dimensions": {
                "event_date_sources": [
                    c for c in ("event_date", "admission_date", "joining_date", "issue_date")
                    if c in merged.columns
                ],
                "derived_columns": [
                    c for c in (
                        "event_date", "period_year", "period_month", "period_month_name",
                        "period_quarter", "period_week", "period_day_name",
                        "period_is_weekend",
                    ) if c in merged.columns
                ],
            },
            "multivalue_columns": {},
            "source_packages": ready,
            "source_summary": source_summary,
            "relationship_summary": relationships,
            "multi_source_summary": self._multi_source_summary(source_summary, relationships),
            "domain_metrics": domain_metrics,
            # None when no finance ledger among sources — key present but empty,
            # so downstream agents can gate on it without inventing fee data.
            "payment_reconciliation": payment_reconciliation,
            # None when no enquiry frame carries person_id — enquiry->admission
            # conversion linked across sheets by the person_id (name+phone) hash.
            "enquiry_conversion": enquiry_conversion,
        }

    def run(
        self,
        brief: Any,
        csv_path: str,
        date_format: Optional[str] = None,
        split_multivalue: bool = True,
    ) -> JsonDict:
        """Produce a DataPackage from a brief + source CSV.

        Args:
            date_format: Optional hint for ambiguous numeric dates, since each
                source file is internally one format. One of "mdy", "dmy",
                "iso" (case-insensitive). When omitted, the parser adapts per
                value and falls back to the column majority for ambiguous ones.
            split_multivalue: When True (default), add non-destructive
                `<role>_list` columns for comma-separated multi-value roles
                (faculty, course, preferred_days), leaving the record grain
                unchanged. Set False to skip.
        """

        if not csv_path or not os.path.exists(csv_path):
            return self._blocked(f"Source CSV not found: {csv_path!r}", row_count=0)

        date_format = (date_format or "").strip().lower() or None
        if date_format and date_format not in ("mdy", "dmy", "iso"):
            return self._blocked(
                f"Unsupported date_format {date_format!r}; use 'mdy', 'dmy', or 'iso'.",
                row_count=0,
            )

        load_issues: List[str] = []
        try:
            raw = pd.read_csv(csv_path)
            # A sheet export whose data rows are wider than its header (a
            # trailing comma on every line is the usual cause) does not raise:
            # pandas quietly promotes the first field to the index and shifts
            # every column one place left. Dates land in the name column, the
            # cleaner finds no parseable date anywhere and drops 100% of rows.
            # A non-RangeIndex from a header-ed read is the tell.
            if not isinstance(raw.index, pd.RangeIndex):
                raw = pd.read_csv(csv_path, index_col=False)
                load_issues.append(
                    "Data rows are wider than the header; re-read with "
                    "index_col=False and ignored the extra trailing field(s). "
                    "Check the source export for a trailing delimiter.")
        except Exception as exc:  # noqa: BLE001 - surface any parse failure to orchestrator
            return self._blocked(f"Failed to read CSV: {exc}", row_count=0)

        return self._clean_raw_frame(
            brief, raw, csv_path, date_format=date_format,
            split_multivalue=split_multivalue,
            source_name="primary", source_domain="single",
            load_issues=load_issues,
        )

    def _clean_raw_frame(
        self,
        brief: Any,
        raw: pd.DataFrame,
        source_path: str,
        date_format: Optional[str] = None,
        split_multivalue: bool = True,
        source_name: Optional[str] = None,
        source_domain: Optional[str] = None,
        load_issues: Optional[List[str]] = None,
        derive_churn: bool = True,
    ) -> JsonDict:
        """Clean an already-loaded dataframe using the legacy single-source flow.

        `derive_churn` is False when this is one source of many: the churn rule
        spans sheets, so it is applied once to the merged frame instead.
        """

        original_rows = len(raw)
        if original_rows == 0:
            return self._blocked("Source CSV has zero rows.", row_count=0)

        known_issues: List[str] = list(load_issues or [])
        df = raw.copy()

        df = self._drop_ref_columns(df, known_issues)
        df = self._drop_empty_columns(df, known_issues)
        df = self._prefer_cleaned_columns(df, known_issues)
        # Purge "zzzzz (Don't Delete)" dropdown-placeholder rows. Structural
        # junk, so it lowers the baseline for the drop-fraction escalation.
        df, placeholder_count = self._drop_placeholder_rows(df, known_issues)
        effective_rows = original_rows - placeholder_count
        if effective_rows == 0:
            return self._blocked(
                "Source contained only placeholder rows.", row_count=0
            )

        roles = self._detect_roles(df)
        # Value-based fallback: infer roles for columns no header keyword claimed,
        # so a sheet with renamed/cryptic headers is still analysable rather than
        # blocked. Runs before the empty-check so a zero-keyword sheet survives.
        roles = self._discover_unclaimed_roles(df, roles, known_issues)
        # Resolve the columns whose meaning depends on the sheet, not the
        # header: a bare `Timestamp`, `Mode` or `Status` means something
        # different on each one.
        self._specialize_roles(df, roles, known_issues)
        if not roles:
            return self._blocked(
                "Canonical column mapping failed: no recognizable roles in CSV.",
                row_count=original_rows,
            )

        df = self._parse_dates(df, roles, known_issues, date_format)
        df = self._normalize_money(df, roles, known_issues)
        self._derive_amount_collected(df, roles, known_issues)
        self._derive_default_flag(df, roles, known_issues)
        df = self._normalize_discovered_numeric(df, roles, known_issues)
        df = self._normalize_categoricals(df, roles)
        df = self._apply_canonical_maps(df, roles, known_issues)
        df = self._normalize_pincode(df, roles, known_issues)
        df = self._compute_lead_to_admission_days(df, roles, known_issues)
        self._derive_payment_channel(df, roles, known_issues)

        multivalue_columns: Dict[str, str] = {}
        if split_multivalue:
            multivalue_columns = self._split_multivalue(df, roles, known_issues)

        if source_name:
            df["source_name"] = source_name
        if source_domain:
            df["source_domain"] = source_domain
        # Timetable workbook sheets carry lifecycle ground truth in their NAME
        # (Course_Completed / Not_Coming / Main_data) — the churn/completion
        # label the row data itself lacks.
        self._derive_completion_status(df, source_name, known_issues)
        # Needs completion_status: how stale branch/faculty/batch are depends on
        # which tab the row sits in.
        self._flag_mutable_attributes(df, roles, known_issues)

        # Coalesce all date roles into one per-row event_date, then derive the
        # report time columns and drop only rows with no date at all.
        event_date = self._build_event_date(df, roles)
        if event_date is not None:
            df["event_date"] = event_date
        time_columns = self._derive_time_dimensions(df)

        df, dropped_reasons = self._drop_invalid_rows(df, roles)
        if placeholder_count:
            dropped_reasons["placeholder_rows"] = placeholder_count
        drop_count = original_rows - len(df)
        # Escalation baseline excludes structural placeholder junk, which is
        # not real data loss.
        real_drop = effective_rows - len(df)

        if effective_rows and real_drop / effective_rows > MAX_DROP_FRACTION:
            return self._blocked(
                f"Row count dropped {real_drop}/{effective_rows} "
                f"(> {int(MAX_DROP_FRACTION * 100)}%) during cleaning.",
                row_count=len(df),
                quality_extra={"dropped_reasons": dropped_reasons},
            )

        # Derive status flags from the raw name BEFORE PII masking, otherwise
        # markers like "(cancelled)" are lost once the name is hashed.
        self._derive_status_flags(df, roles, known_issues)
        # Person identity also needs the raw (marker-stripped) name + phone;
        # the emitted person_id is already a salted hash.
        person_id_basis = self._derive_person_id(df, roles, known_issues)
        # Staff are hired out of the student body, so a name can appear on both
        # sides of the same sheet. Detected before masking, for the same reason.
        self._derive_staff_alumni(df, roles, known_issues)

        canonical_columns = self._mask_pii(df, roles, known_issues)
        # Whole PII columns are now hashed; scrub any contact detail typed inside
        # a retained free-text note so no raw phone/email reaches the report.
        self._scrub_free_text_pii(df, roles, known_issues)
        dedup_keys = self._dedupe(df, roles, known_issues)

        churn_summary = (
            self._derive_churn_labels(df, roles, known_issues)
            if derive_churn else None
        )

        canonical_path = self._write_parquet(df, source_path)

        return {
            "status": "ready",
            "canonical_df_path": canonical_path,
            "row_count": len(df),
            "schema": {col: str(dtype) for col, dtype in df.dtypes.items()},
            # None when the source carries no lifecycle membership. Present, it
            # records the as-of date the labels are true for — the same rows
            # answer differently on a different date.
            "churn_summary": churn_summary,
            "quality_report": {
                "original_row_count": original_rows,
                "drop_count": drop_count,
                "dropped_reasons": dropped_reasons,
                "null_rates": self._null_rates(df),
                "deduplication_keys": dedup_keys,
                # What person identity was actually built from. ["name"] alone
                # means person-grain metrics were withheld as unreliable.
                "person_id_basis": person_id_basis,
                "known_issues": known_issues,
            },
            "canonical_columns": canonical_columns,
            # The institute's own name for each mapped column, so operator-
            # facing output uses their vocabulary rather than the internal
            # role keys the metrics are wired to.
            "field_names": self._field_names(canonical_columns),
            "time_dimensions": {
                "event_date_sources": [
                    roles[r] for r in DATE_ROLES if r != "dob" and r in roles
                ],
                "derived_columns": time_columns,
            },
            "multivalue_columns": multivalue_columns,
        }

    # -------------------------------------------------------------- cleaning

    def _drop_ref_columns(self, df: pd.DataFrame, issues: List[str]) -> pd.DataFrame:
        ref_cols = [c for c in df.columns if "#ref!" in str(c).strip().lower()]
        if ref_cols:
            df = df.drop(columns=ref_cols)
            issues.append(f"Dropped {len(ref_cols)} '#REF!' column(s): {ref_cols}")
        return df

    def _drop_empty_columns(self, df: pd.DataFrame, issues: List[str]) -> pd.DataFrame:
        """Drop all-NaN columns (the timetable sheets interleave blank columns).

        Headers like '' / 'Unnamed: 3' with no values carry zero signal and
        would otherwise pollute role discovery and null-rate reporting.
        """
        empty_cols = [c for c in df.columns if df[c].isna().all()]
        if empty_cols:
            df = df.drop(columns=empty_cols)
            issues.append(f"Dropped {len(empty_cols)} all-empty column(s)")
        return df

    def _drop_placeholder_rows(
        self, df: pd.DataFrame, issues: List[str]
    ) -> Tuple[pd.DataFrame, int]:
        """Purge dropdown-placeholder rows ("zzzzz (Don't Delete)").

        A row is a placeholder when ANY text cell starts with 'zzz...'. Returns
        (frame, purged_count); the count is excluded from real-drop math.
        """
        mask = pd.Series(False, index=df.index)
        for col in df.columns:
            if pd.api.types.is_numeric_dtype(df[col]):
                continue
            vals = df[col].astype("string")
            mask |= vals.str.match(PLACEHOLDER_NAME_RE).fillna(False)
        count = int(mask.sum())
        if count:
            df = df.loc[~mask]
            issues.append(f"Purged {count} placeholder row(s) ('zzzz…/Don't Delete')")
        return df, count

    def _derive_completion_status(
        self, df: pd.DataFrame, source_name: Optional[str], issues: List[str]
    ) -> None:
        """Label lifecycle from the source sheet's name when it encodes it.

        The institute's timetable workbook separates students by sheet:
        Course_Completed (finished), Not_Coming (churned/paused with reason),
        Main_data (active). That sheet membership is the ONLY completion label
        in the data, so it is captured as `completion_status`. No-op when the
        source name matches nothing (no label invented).
        """
        # A lifecycle COLUMN beats sheet membership when both exist: it is
        # per-row rather than per-sheet, so one consolidated master can carry
        # every state at once.
        if self._derive_completion_from_column(df, issues):
            return
        if not source_name:
            return
        lowered = source_name.lower()
        for needle, label in COMPLETION_BY_SOURCE:
            if needle in lowered:
                df["completion_status"] = label
                issues.append(
                    f"Derived completion_status='{label}' from source sheet "
                    f"name '{source_name}'"
                )
                # Boolean churn labels the Analyst/Monitoring read directly.
                # `completion_rate` (Analyst METRIC_SPECS) needs is_completed;
                # not_coming_rate needs is_not_coming. Both are added only on the
                # sheet whose name resolved a label, so no row is guessed.
                df["is_completed"] = label == "completed"
                df["is_not_coming"] = label == "not_coming"
                self._derive_dropped(df)
                return

    @staticmethod
    def _derive_completion_from_column(
        df: pd.DataFrame, issues: List[str]
    ) -> bool:
        """Read the lifecycle from a column that speaks it. True when found.

        Detection is by VALUES, not by header. The institute's own sheets use
        `status` for the fee state on one tab and the lifecycle on another, so
        a header match would pick the wrong column about half the time; a
        column holding "Course Completed" and "Not Coming" can only be one
        thing.
        """
        best: Optional[Tuple[str, pd.Series, float]] = None
        for col in df.columns:
            if pd.api.types.is_numeric_dtype(df[col]):
                continue
            values = df[col].astype("string").str.strip().str.lower()
            present = values.dropna()
            if present.empty:
                continue
            mapped = present.map(
                lambda v: LIFECYCLE_VALUES[v] if v in LIFECYCLE_VALUES else "")
            known = mapped != ""
            # `unknown` counts as vocabulary (it is a recognized answer) but
            # labels nothing, so it supports detection and not the label.
            recognized = present.map(lambda v: v in LIFECYCLE_VALUES)
            share = float(recognized.mean())
            states = mapped[known].nunique()
            if share < LIFECYCLE_MATCH_FLOOR or states < LIFECYCLE_MIN_STATES:
                continue
            if best is None or share > best[2]:
                best = (col, values, share)

        if best is None:
            return False
        col, values, share = best
        labels = values.map(
            lambda v: LIFECYCLE_VALUES.get(v) if isinstance(v, str) else None)
        df["completion_status"] = labels
        df["is_completed"] = labels.eq("completed")
        df["is_not_coming"] = labels.eq("not_coming")
        DataEngineerAgent._derive_dropped(df)
        counts = labels.value_counts(dropna=False).to_dict()
        issues.append(
            f"Derived completion_status per row from column '{col}' "
            f"({share:.0%} of values recognized): "
            + ", ".join(f"{k or 'unlabelled'}={v}" for k, v in sorted(
                counts.items(), key=lambda kv: str(kv[0])))
        )
        return True

    @staticmethod
    def _derive_dropped(df: pd.DataFrame) -> None:
        """`is_dropped` — left before finishing, by EITHER available signal.

        `is_cancelled` only ever sees a cancellation someone typed into a name
        ("Ritik Shah (admission cancelled)"), so on a sheet where leaving is
        recorded in a status column instead it reports almost nobody. Reading
        both together is what makes a dropout rate mean "left before
        completing" rather than "was annotated a particular way".
        """
        parts = [df[c] for c in ("is_cancelled", "is_not_coming")
                 if c in df.columns]
        if not parts:
            return
        dropped = parts[0].fillna(False).astype(bool)
        for extra in parts[1:]:
            dropped = dropped | extra.fillna(False).astype(bool)
        df["is_dropped"] = dropped

    @staticmethod
    def _flag_mutable_attributes(
        df: pd.DataFrame, roles: Mapping[str, str], issues: List[str]
    ) -> None:
        """Record how current branch / faculty / batch actually are.

        Confirmed by the institute: students change batch timing, tutor and
        even branch during a course, the cell is overwritten, and **the edits
        land almost entirely on Main_data** — that is the live roster. Once a
        student is moved to Course_Completed or Not_Coming, their row stops
        being maintained and freezes at whatever was true on the way out.

        So these columns carry two different as-of dates in one frame, and a
        breakdown that mixes them is comparing today's Main_data against a
        2023 exit snapshot. `attribute_currency` marks which is which so the
        two can be told apart or filtered; nothing is corrected, because there
        is no history column to correct it from.
        """
        present = [r for r in MUTABLE_ATTRIBUTE_ROLES if roles.get(r)]
        if not present:
            return
        names = ", ".join(f"{r} ('{roles[r]}')" for r in present)
        note = (
            f"{names} change during an enrollment and the sheet keeps only the "
            f"latest value — there is no history column. Breakdowns by them are "
            f"an as-of snapshot, not where the student actually spent the "
            f"course, and they are excluded from join keys for the same reason."
        )

        if "completion_status" in df.columns:
            currency = df["completion_status"].map(
                lambda s: "current" if s == "active" else (
                    "at_exit" if isinstance(s, str) else None
                )
            )
            df["attribute_currency"] = currency
            counts = currency.value_counts().to_dict()
            if counts.get("at_exit"):
                note += (
                    f" Worse in this frame: only the active roster is still "
                    f"maintained, so {counts.get('current', 0)} row(s) are "
                    f"current and {counts['at_exit']} are frozen at the point "
                    f"the student left. Marked in attribute_currency."
                )
        issues.append(note)

    def _apply_reporting_taxonomies(
        self, df: pd.DataFrame, roles: Mapping[str, str], issues: List[str]
    ) -> None:
        """Group course, lead source and occupation the way the institute reports.

        From an operator's corrections log: they exported a generated audit,
        hand-fixed the groupings, and recorded the rules behind the fixes. Three
        groupings resulted, and each collapses distinctions the institute does
        not act on — referral-by-friend against referral-by-old-student,
        self-employed against salaried.

        **The original value is never destroyed.** Each grouped column keeps its
        raw text in `<col>_raw`, because these are the institute's current
        reporting policy rather than facts about the world, and the next
        operator may want the finer split back. Reversible by construction.

        An unmatched value becomes NaN and is COUNTED, not swept into an
        "Other" bucket. A catch-all bucket makes a taxonomy look complete while
        hiding exactly the vocabulary the rules have not learned yet.
        """
        course_col = roles.get("course")
        if course_col and course_col in df.columns:
            family = df[course_col].map(
                lambda v: canonical_maps.canonicalize_course(v)[0])
            category = [
                canonical_maps.canonicalize_course_category(raw, fam)
                for raw, fam in zip(df[course_col], family)
            ]
            category = pd.Series(category, index=df.index, dtype="object")
            if category.notna().any():
                existing = roles.get("course_category")
                # The sheet's own category column, when present, is what the
                # institute typed; keep it and add the derived one beside it
                # rather than overwriting a human's classification.
                target = ("course_category_derived"
                          if existing and existing in df.columns
                          else "course_category_derived")
                df[target] = category
                roles.setdefault("course_category_derived", target)
                self._report_taxonomy(
                    "course category", course_col, target, category, issues,
                    raw=df[course_col])

        for role, fn, label in (
            ("source", canonical_maps.canonicalize_lead_source, "lead source"),
            ("occupation", canonical_maps.canonicalize_occupation, "occupation"),
        ):
            col = roles.get(role)
            if not col or col not in df.columns:
                continue
            if pd.api.types.is_numeric_dtype(df[col]):
                continue
            bucketed = df[col].map(fn)
            if not bucketed.notna().any():
                continue
            raw_values = df[col]
            df[f"{col}_raw"] = raw_values
            df[col] = bucketed
            self._report_taxonomy(label, col, col, bucketed, issues,
                                  raw_kept=f"{col}_raw", raw=raw_values)
            if role == "source":
                self._report_lead_source_assumptions(raw_values, issues)

    @staticmethod
    def _report_lead_source_assumptions(
        raw: pd.Series, issues: List[str]
    ) -> None:
        """Say how much of the lead-source column rests on an assumption.

        Two of the three buckets can be reached without the cell saying so: a
        blank read as Walk-in, and free text read as a referrer's name. Both
        are defensible and both are guesses, so the share is stated and the
        free-text values are listed — that list is also how the rules learn a
        channel they do not yet know.
        """
        bases = raw.map(lambda v: canonical_maps.lead_source_basis(v)[1])
        blank = int((bases == "blank").sum())
        named = int((bases == "named-referrer").sum())
        if not (blank or named):
            return
        parts = []
        if blank:
            parts.append(f"{blank} blank cell(s) read as Walk-in")
        if named:
            # Counted, never quoted: these cells hold the names of real people
            # who referred someone, and a quality note travels into reports.
            distinct = len({str(v).strip().lower() for v, b in zip(raw, bases)
                            if b == "named-referrer"})
            parts.append(
                f"{named} row(s) ({distinct} distinct value(s)) naming a "
                f"person or organisation read as Referral")
        issues.append(
            "lead source: " + "; ".join(parts)
            + ". Both are assumptions, not what the cell said. Read the "
              "distinct values in '<col>_raw' if a channel looks missing."
        )

    @staticmethod
    def _report_taxonomy(label: str, source_col: str, target_col: str,
                         values: pd.Series, issues: List[str],
                         raw_kept: Optional[str] = None,
                         raw: Optional[pd.Series] = None) -> None:
        counts = values.value_counts().to_dict()
        unmatched = int(values.isna().sum())
        note = (
            f"{label} grouped into {len(counts)} reporting bucket(s) from "
            f"'{source_col}' → '{target_col}': "
            + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        )
        if raw_kept:
            note += f". Original kept in '{raw_kept}'"
        if unmatched:
            # An empty input and a value no rule covers both come out blank,
            # and they say different things: one is a field nobody filled, the
            # other is vocabulary these rules have not learned. Only the second
            # is a reason to add a rule, so they are never reported as one
            # number.
            empty = 0
            if raw is not None:
                blank_in = raw.isna() | (
                    raw.astype(str).str.strip().str.lower().isin(["", "nan"]))
                empty = int((values.isna() & blank_in).sum())
            unlearned = unmatched - empty
            if empty:
                note += f". {empty} row(s) had nothing recorded to group"
            if unlearned:
                note += (f". {unlearned} row(s) matched no rule and are left "
                         f"blank rather than bucketed — they are the "
                         f"vocabulary the rules have not learned")
        # `Other` is a real bucket in the institute's closed sets, so it cannot
        # be reported as blank. It is still the place unrecognized values land,
        # so say how big it is: a growing Other is a gap in these rules, not a
        # segment worth acting on.
        other = int(counts.get("Other", 0))
        if other:
            note += (f". 'Other' holds {other} row(s) — the closed set's "
                     f"catch-all; check it before reading it as a segment")
        issues.append(note + ".")

    def _derive_churn_labels(
        self, df: pd.DataFrame, roles: Mapping[str, str], issues: List[str]
    ) -> Optional[JsonDict]:
        """Attach the time-dependent churn label. Returns its summary, or None.

        Computed on the FINAL frame only — after any union and join — because
        the rule spans sheets: the lifecycle label comes from the timetable tab
        the row sat in, while the course start and length come from the student
        or fees sheet. Run per-source it would find one half of the rule and
        report the whole roster unlabelled.

        No-op without `completion_status`: no membership, no lifecycle ground
        truth, and nothing here is worth guessing.
        """
        if "completion_status" not in df.columns:
            return None
        cfg = self.churn_config
        result = lifecycle.churn_labels(
            df, roles,
            as_of=cfg.get("as_of"),
            grace_months=int(cfg.get("grace_months", lifecycle.DEFAULT_GRACE_MONTHS)),
            duration_days_by_category=cfg.get("duration_days_by_category"),
            entertain_unconditional=bool(cfg.get("entertain_unconditional")),
        )
        for col in result["columns"].columns:
            df[col] = result["columns"][col]
        summary = result["summary"]
        issues.append(
            f"Churn labelled as of {summary['as_of'][:10]} "
            f"({summary['as_of_source']}), {summary['grace_months']} month "
            f"grace after course end: "
            f"{', '.join(f'{k}={v}' for k, v in sorted(summary['counts'].items()))}"
        )
        issues.extend(summary["notes"])
        return summary

    def _prefer_cleaned_columns(self, df: pd.DataFrame, issues: List[str]) -> pd.DataFrame:
        """When both `Cleaned X` and `X` exist, keep the cleaned one as `X`."""
        lower_map = {str(c).strip().lower(): c for c in df.columns}
        dropped = []
        for lower, original in list(lower_map.items()):
            if lower.startswith("cleaned "):
                base = lower[len("cleaned "):].strip()
                if base in lower_map:
                    raw_col = lower_map[base]
                    df = df.drop(columns=[raw_col])
                    df = df.rename(columns={original: raw_col})
                    dropped.append(raw_col)
        if dropped:
            issues.append(f"Preferred 'Cleaned *' columns over raw for: {dropped}")
        return df

    def _detect_roles(self, df: pd.DataFrame) -> Dict[str, str]:
        """Map role -> actual column name using ROLE_SPECS include/exclude.

        Roles are evaluated in ROLE_SPECS order (specific before generic) and a
        source column is claimed by at most one role, so `course category` is
        taken by `course_category` before `course` can see it.
        """
        roles: Dict[str, str] = {}
        taken: set = set()
        for role, spec in ROLE_SPECS.items():
            include = spec["include"]
            exclude = spec.get("exclude", [])
            for col in df.columns:
                if col in taken:
                    continue
                header = str(col).strip().lower()
                # pandas names headerless columns "Unnamed: 18", which contains
                # the substring "name" and so claimed the `name` role on any
                # sheet that had no real name column. A placeholder header
                # carries no information at all; value profiling handles these.
                if _UNNAMED_HEADER_RE.match(header):
                    continue
                if any(bad in header for bad in exclude):
                    continue
                if any(kw in header for kw in include):
                    roles[role] = col
                    taken.add(col)
                    break
        return roles

    # Values that identify a bare `Mode` column as the delivery mode rather
    # than a payment or enquiry mode.
    _LEARNING_MODES = {"online", "offline", "hybrid", "both"}

    def _specialize_roles(
        self, df: pd.DataFrame, roles: Dict[str, str], issues: List[str]
    ) -> None:
        """Give context-dependent columns their real name, without renaming keys.

        Three headers in this estate mean different things on different sheets:

        - `Timestamp` is the form-submission time. On the enquiry sheets that
          *is* the enquiry date; on the admission form and student-data it is
          the **join key between them** and there is nothing enquiry-ish about
          it. Calling it `enquiry_date` everywhere invents a lead date for
          every admission.
        - `Mode` is online/offline delivery on student-data, and mode of
          enquiry on a lead sheet.
        - `Status` is fee status next to a pending column, and lifecycle status
          next to a churn reason.

        Contextual aliases are ADDED alongside the generic role rather than
        replacing it: every derivation and metric is keyed on the generic name,
        so renaming would break them, while the alias is what an operator sees
        and what a question can name as a dimension.
        """
        added: List[str] = []

        # --- Timestamp: submission record vs. the enquiry itself
        enquiry_col = roles.get("enquiry_date")
        if enquiry_col and str(enquiry_col).strip().lower() == "timestamp":
            roles["record_timestamp"] = enquiry_col
            explicit = next((c for c in df.columns
                             if "date of enquiry" in str(c).strip().lower()), None)
            if explicit:
                # A real enquiry date exists, so the timestamp is only the
                # submission record and must not stand in for a lead date.
                roles["enquiry_date"] = explicit
                added.append(f"'{enquiry_col}' is the submission timestamp "
                             f"(join key), not the enquiry date — using "
                             f"'{explicit}' for enquiry timing")
            elif "admission_date" in roles or "joining_date" in roles:
                # An admission-side sheet: the timestamp is when the form was
                # filled, which is the admission event, not a prior enquiry.
                roles.pop("enquiry_date", None)
                added.append(f"'{enquiry_col}' treated as the record timestamp "
                             f"(the admission/student-data join key), not an "
                             f"enquiry date")

        # --- Mode: resolved from its values
        mode_col = roles.get("mode")
        if mode_col is not None and mode_col in df.columns:
            values = {str(v).strip().lower()
                      for v in df[mode_col].dropna().unique()[:50]}
            if values and values <= self._LEARNING_MODES:
                roles["learning_mode"] = mode_col
                added.append(f"'{mode_col}' resolved to learning mode "
                             f"(online/offline delivery)")
            elif "enquiry_date" in roles and "admission_date" not in roles:
                roles["enquiry_mode"] = mode_col
                added.append(f"'{mode_col}' resolved to enquiry mode")

        # --- Status: fee vs lifecycle
        status_col = roles.get("status")
        if status_col is not None:
            if "pending" in roles or "amount" in roles or "paid" in roles:
                roles["fee_status"] = status_col
                added.append(f"'{status_col}' resolved to fee status")
            elif "status_reason" in roles or "completion_status" in df.columns:
                roles["churn_status"] = status_col
                added.append(f"'{status_col}' resolved to churn status")

        # --- Tutor and Faculty are one person under two words
        faculty_col = roles.get("faculty")
        if faculty_col is not None:
            header = str(faculty_col).strip().lower()
            word = "tutor" if "tutor" in header else "faculty"
            df["staff_role"] = word
            added.append(f"'{faculty_col}' mapped to faculty; staff_role="
                         f"'{word}' records which word the sheet used")

        for note in added:
            issues.append(f"Field naming: {note}")

    def _discover_unclaimed_roles(
        self, df: pd.DataFrame, roles: Dict[str, str], issues: List[str]
    ) -> Dict[str, str]:
        """Profile columns no header keyword claimed and assign generic roles.

        Mutates and returns `roles`, adding discovered_dim_N / discovered_num_N /
        discovered_date_N / discovered_pii_N keys so the rest of the pipeline
        (normalize, mask, EDA profiling) treats a cryptically-named column by its
        inferred NATURE. PII-looking columns are routed to a discovered_pii role
        so they get hashed, never surfaced as a metric or dimension.
        """
        claimed = set(roles.values())
        counters = {"dimension": 0, "numeric": 0, "date": 0, "pii": 0}
        prefix = {
            "dimension": "discovered_dim",
            "numeric": "discovered_num",
            "date": "discovered_date",
            "pii": "discovered_pii",
        }
        for col in df.columns:
            if col in claimed:
                continue
            kind = self._profile_unclaimed_column(df[col])
            if not kind:
                continue
            counters[kind] += 1
            role = f"{prefix[kind]}_{counters[kind]}"
            roles[role] = col
            issues.append(
                f"Discovered column '{col}' by value profiling -> {role} ({kind})"
            )
        return roles

    def _profile_unclaimed_column(self, series: pd.Series) -> Optional[str]:
        """Classify a column by its values: pii | date | numeric | dimension | None.

        PII is checked first (safety): a phone/email column must be masked, not
        analysed. Numeric before date so '2024' style years are not read as dates;
        date requires a separator/month token AND a successful parse. Free-text or
        mostly-unique identifier columns return None (left untouched / unmasked but
        never promoted to a metric).
        """
        s = series.dropna().astype(str).str.strip()
        s = s[s.str.lower().ne("nan") & s.ne("")]
        if s.empty:
            return None
        if len(s) > DISCOVERY_SAMPLE_ROWS:
            s = s.sample(DISCOVERY_SAMPLE_ROWS, random_state=0)
        n = len(s)

        # 1) PII first: 10-12 digit phone numbers or email addresses.
        digits = s.str.replace(r"\D", "", regex=True)
        residue = s.str.replace(r"[\d\s\-+()]", "", regex=True)
        phoneish = digits.str.len().between(10, 12) & residue.str.len().eq(0)
        emailish = s.str.contains(_EMAIL_VALUE_RE, regex=True, na=False)
        if phoneish.mean() >= DISCOVERY_PII_MIN_FRAC or emailish.mean() >= DISCOVERY_PII_MIN_FRAC:
            return "pii"

        # 2) numeric (strip currency/commas); guard against id-like columns.
        cleaned = s.str.replace(r"[₹$£€,\s]", "", regex=True)
        numeric = pd.to_numeric(cleaned, errors="coerce")
        if numeric.notna().mean() >= DISCOVERY_NUM_MIN_FRAC:
            # Only skip as an identifier when it really looks like one: long,
            # integer, almost-all-unique, over a meaningful row count. This keeps
            # genuine measures (even all-distinct ones) as metrics.
            vals = numeric.dropna()
            is_intlike = bool((vals % 1 == 0).all())
            longish = float(cleaned.str.len().median()) >= 6
            if (
                n >= 20
                and is_intlike
                and longish
                and s.nunique() / n >= DISCOVERY_ID_UNIQUE_FRAC
            ):
                return None  # integer identifier, not a metric
            return "numeric"

        # 3) date: needs a date-ish token AND to actually parse.
        if s.str.contains(_DATEISH_RE, case=False, regex=True, na=False).mean() >= DISCOVERY_DATE_MIN_FRAC:
            parsed = pd.to_datetime(s, errors="coerce", format="mixed")
            if parsed.notna().mean() >= DISCOVERY_DATE_MIN_FRAC:
                return "date"

        # 4) low-cardinality string -> categorical dimension.
        nunique = s.nunique()
        if nunique <= DISCOVERY_DIM_MAX_CARDINALITY and nunique / n < 0.5:
            return "dimension"

        return None  # free text / high-card -> leave unknown

    def _discovered_roles(self, roles: Mapping[str, str], prefix: str) -> List[str]:
        """Role keys with the given discovered_* prefix, in stable numeric order."""
        return sorted(r for r in roles if r.startswith(prefix))

    def _parse_dates(
        self,
        df: pd.DataFrame,
        roles: Mapping[str, str],
        issues: List[str],
        date_format: Optional[str] = None,
    ) -> pd.DataFrame:
        date_roles = list(DATE_ROLES) + self._discovered_roles(roles, "discovered_date")
        for role in date_roles:
            col = roles.get(role)
            if not col:
                continue
            parsed, mixed = self._smart_parse_date_series(df[col], date_format)
            parsed, out_of_range = self._enforce_date_bounds(parsed, role)
            bad = int(parsed.isna().sum()) - out_of_range
            df[col] = parsed
            if mixed:
                issues.append(
                    f"{role} '{col}': mixed M/D/Y and D/M/Y formats detected; "
                    "parsed each value by its unambiguous field, ambiguous values "
                    f"used the column-majority order ({mixed})"
                )
            if bad:
                issues.append(f"{role} '{col}': {bad} unparseable date(s) set to NaT")
            if out_of_range:
                issues.append(
                    f"{role} '{col}': {out_of_range} date(s) outside plausible "
                    "bounds (data-entry typos like year 0026/2126 or pre-2000 "
                    "business dates) set to NaT"
                )
        return df

    @staticmethod
    def _enforce_date_bounds(parsed: pd.Series, role: str) -> Tuple[pd.Series, int]:
        """NaT-out plausible-looking but impossible business dates.

        The real sheets contain entry typos (`4/23/0026`, receipts dated a year
        ahead) — pandas silently keeps any value inside datetime64 range, so an
        explicit business-bounds check is required. Bounds by role:
          dob        : [1900-01-01, today]           (students are born, not scheduled)
          everything : [2000-01-01, today + 2 years] (institute opened this century;
                       courses are booked at most a term ahead)
        Returns (bounded_series, n_removed).
        """
        if parsed.empty or not pd.api.types.is_datetime64_any_dtype(parsed):
            return parsed, 0
        today = pd.Timestamp.today().normalize()
        if role == "dob":
            lo, hi = pd.Timestamp("1900-01-01"), today
        else:
            lo, hi = pd.Timestamp("2000-01-01"), today + pd.DateOffset(years=2)
        bad = parsed.notna() & ((parsed < lo) | (parsed > hi))
        n_bad = int(bad.sum())
        if n_bad:
            parsed = parsed.mask(bad)
        return parsed, n_bad

    # Matches D?/M?/Y or D?-M?-Y style numeric dates (the only ambiguous case).
    _NUMERIC_DATE_RE = re.compile(r"^\s*(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\s*$")

    def _smart_parse_date_series(self, series: pd.Series, date_format: Optional[str] = None):
        """Parse a date column that may MIX M/D/Y and D/M/Y in the same field.

        When `date_format` is supplied ("mdy"/"dmy"/"iso"), the order is forced
        and no inference happens — preferred, since each source file is one
        format and this removes ambiguity on values like 5/2/2022.

        Real institute sheets contain both `4/24/2026` (month-first) and
        `30/03/2024` (day-first). A single dayfirst flag silently mis-parses or
        nulls half the column, so instead:

        1. For each `a/b/y` value, if a>12 it must be day-first; if b>12 it must
           be month-first. These unambiguous values vote on the column order.
        2. Ambiguous values (both <=12) are parsed using the winning order.
        3. Non-numeric / ISO / textual dates fall through to pandas directly.

        Returns (parsed_series, mixed_label) where mixed_label is "" when the
        column is not mixed, else the chosen majority order string.
        """
        raw = series.astype("string")

        # Forced format: each source file is internally consistent, so when the
        # caller knows the convention we skip inference entirely. `format="mixed"`
        # lets pandas parse each value on its own, so a column mixing 4-digit and
        # 2-digit years (e.g. 06/08/2024 alongside 06/08/24) doesn't collapse.
        if date_format == "iso":
            return self._to_datetime_mixed(raw), ""
        if date_format in ("mdy", "dmy"):
            return self._to_datetime_mixed(raw, dayfirst=date_format == "dmy"), ""

        first_votes = 0   # day-first evidence
        month_votes = 0   # month-first evidence
        numeric_mask = []
        for val in raw:
            # `astype("string")` yields pd.NA (not None) for missing values, so
            # guard with pd.isna before the regex (NAType is not str/bytes).
            is_str = isinstance(val, str)
            m = self._NUMERIC_DATE_RE.match(val) if is_str else None
            numeric_mask.append(m is not None)
            if not m:
                continue
            a, b = int(m.group(1)), int(m.group(2))
            if a > 12 and b <= 12:
                first_votes += 1
            elif b > 12 and a <= 12:
                month_votes += 1

        # No numeric ambiguous-style dates at all: let pandas handle it.
        if not any(numeric_mask):
            parsed = self._to_datetime_mixed(raw)
            if parsed.isna().mean() > 0.3:
                parsed = self._to_datetime_mixed(raw, dayfirst=True)
            return parsed, ""

        majority_dayfirst = first_votes >= month_votes
        mixed = first_votes > 0 and month_votes > 0

        out = []
        for val, is_num in zip(raw, numeric_mask):
            if not isinstance(val, str):  # None / pd.NA / NaN -> not parseable
                out.append(pd.NaT)
                continue
            if not is_num:
                out.append(pd.to_datetime(val, errors="coerce"))
                continue
            m = self._NUMERIC_DATE_RE.match(val)
            a, b = int(m.group(1)), int(m.group(2))
            if a > 12 and b <= 12:
                dayfirst = True
            elif b > 12 and a <= 12:
                dayfirst = False
            else:
                dayfirst = majority_dayfirst
            out.append(
                pd.to_datetime(val, errors="coerce", dayfirst=dayfirst, format="mixed")
            )

        parsed = pd.Series(out, index=series.index, dtype="datetime64[ns]")
        label = "dayfirst" if majority_dayfirst else "monthfirst"
        return parsed, (label if mixed else "")

    @staticmethod
    def _to_datetime_mixed(values: pd.Series, dayfirst: bool = False) -> pd.Series:
        """pd.to_datetime with per-value format inference.

        `format="mixed"` parses each element independently, so a column mixing
        4-digit and 2-digit years (06/08/2024 vs 06/08/24) or odd separators does
        not get force-fit to one inferred format and silently NaT the rest.
        """
        return pd.to_datetime(
            values, errors="coerce", dayfirst=dayfirst, format="mixed"
        )

    def _normalize_money(
        self, df: pd.DataFrame, roles: Mapping[str, str], issues: List[str]
    ) -> pd.DataFrame:
        for role in MONEY_ROLES:
            col = roles.get(role)
            if not col:
                continue
            cleaned = (
                df[col]
                .astype(str)
                .str.replace(r"[₹$£€,\s]", "", regex=True)
            )
            numeric = pd.to_numeric(cleaned, errors="coerce")
            bad = int(numeric.isna().sum() - df[col].isna().sum())
            if role == "pending":
                # Negative pending = overpayment / refund-due. Real anomaly
                # signal (reconciliation flags it) — must NOT be clipped away.
                df[col] = numeric
                n_neg = int((numeric < 0).sum())
                if n_neg:
                    issues.append(
                        f"pending '{col}': {n_neg} negative value(s) kept (overpayment signal)"
                    )
            else:
                df[col] = numeric.clip(lower=0)  # negative fee/paid is garbage
            if bad > 0:
                issues.append(f"{role} '{col}': {bad} non-numeric value(s) set to NaN")
        return df

    def _derive_amount_collected(
        self, df: pd.DataFrame, roles: Mapping[str, str], issues: List[str]
    ) -> None:
        """Per-enrollment rupees collected = billed total - pending.

        On a rollup frame (total_fees + pending) the collected amount is not a
        stored column but drives collection_efficiency by branch/course. Only
        added when both an amount (total) and a pending role are numeric, so
        ledger/enquiry frames without a billed total get nothing. A pre-existing
        explicit `paid` role is left as the truth and no derived column is made.
        """
        if roles.get("paid"):
            return  # explicit collected figure already present
        amount_col = roles.get("amount")
        pending_col = roles.get("pending")
        if not amount_col or not pending_col:
            return
        if not (pd.api.types.is_numeric_dtype(df[amount_col])
                and pd.api.types.is_numeric_dtype(df[pending_col])):
            return
        collected = (df[amount_col] - df[pending_col].fillna(0)).clip(lower=0)
        df["amount_collected"] = collected
        issues.append(
            f"Derived amount_collected = '{amount_col}' - '{pending_col}' "
            f"(collection_efficiency numerator)"
        )

    def _derive_default_flag(
        self, df: pd.DataFrame, roles: Mapping[str, str], issues: List[str]
    ) -> None:
        """Flag each enrollment carrying an unpaid balance (pending > 0).

        Enrollment-grain default: the share of students still owing money. This
        is the per-row rate the Analyst breaks down by branch/course; the recon
        table's default_aging is the complementary money-weighted / overdue view.
        Only added when a numeric pending role exists. Negative pending is an
        overpayment (not a default), so the strict `> 0` test excludes it.
        """
        pending_col = roles.get("pending")
        if not pending_col or not pd.api.types.is_numeric_dtype(df[pending_col]):
            return
        df["is_default"] = df[pending_col].fillna(0) > 0
        n_default = int(df["is_default"].sum())
        if n_default:
            issues.append(
                f"Flagged {n_default} enrollment(s) with unpaid balance "
                f"(is_default from '{pending_col}')"
            )

    def _normalize_discovered_numeric(
        self, df: pd.DataFrame, roles: Mapping[str, str], issues: List[str]
    ) -> pd.DataFrame:
        """Coerce value-discovered numeric columns. Unlike money, values are NOT
        clipped to 0 (a discovered metric may legitimately be negative)."""
        for role in self._discovered_roles(roles, "discovered_num"):
            col = roles[role]
            cleaned = df[col].astype(str).str.replace(r"[₹$£€,\s]", "", regex=True)
            numeric = pd.to_numeric(cleaned, errors="coerce")
            bad = int(numeric.isna().sum() - df[col].isna().sum())
            df[col] = numeric
            if bad > 0:
                issues.append(f"{role} '{col}': {bad} non-numeric value(s) set to NaN")
        return df

    def _derive_payment_channel(
        self, df: pd.DataFrame, roles: Mapping[str, str], issues: List[str]
    ) -> None:
        """Parse payment channel + refund marker from ledger Description prose.

        Conditional emission: `payment_channel` / `is_refund_entry` are added
        only when at least one row matches, so non-ledger sheets that happen to
        have a description column gain nothing.
        """
        col = roles.get("description")
        if not col or col not in df.columns:
            return
        text = df[col].fillna("").astype(str)

        def classify(value: str) -> Optional[str]:
            for label, pattern in PAYMENT_CHANNEL_PATTERNS:
                if pattern.search(value):
                    return label
            return None

        channels = text.map(classify)
        n_found = int(channels.notna().sum())
        if n_found:
            df["payment_channel"] = channels
            issues.append(
                f"payment_channel parsed from '{col}' for {n_found} row(s)"
            )
        refunds = text.str.contains(REFUND_ENTRY_RE)
        n_refunds = int(refunds.sum())
        if n_refunds:
            df["is_refund_entry"] = refunds
            issues.append(
                f"{n_refunds} refund entr(ies) detected in '{col}'"
            )

    def _derive_status_flags(
        self, df: pd.DataFrame, roles: Mapping[str, str], issues: List[str]
    ) -> None:
        """Derive boolean funnel/lifecycle flags the downstream agents need.

        Runs before PII masking so the raw name is still readable for the
        cancellation marker. All flags are *conditional*: each is added only when
        its backing column exists, so no data is invented (Agent 5 visualizes
        only what is present). Flags added when data supports them:

          enrollment_status - lifecycle from name markers:
                              active/cancelled/refunded/not_coming/trial.
          is_cancelled  - "(cancelled)"/"refunded"/"left"/"dropped" in the name.
          is_enquiry    - True for every retained row (each row is >= an enquiry).
          is_admitted   - admission/joining date present.
          is_fee_paid   - paid/amount > 0.
          certificate_delay_days / is_certificate_pending - from issue_date.
        """
        self._derive_lifecycle_status(df, roles, issues)
        self._derive_funnel_flags(df, roles, issues)
        self._derive_certificate_flags(df, roles, issues)
        self._derive_duplicate_certificate(df, roles, issues)
        self._derive_enquiry_backlog(df, roles, issues)

    def _derive_lifecycle_status(
        self, df: pd.DataFrame, roles: Mapping[str, str], issues: List[str]
    ) -> None:
        """Extract `enrollment_status` (+ legacy `is_cancelled`) from raw names.

        The real sheets tag lifecycle inline in the Name column:
        "Ritik Shah (admission cancelled all refunded)", "Riya Desai (not coming)",
        "(Register for trial ...)". Each marker parenthetical is classified via
        STATUS_MARKERS (refunded > cancelled > not_coming > trial) and stripped,
        so the later name hash is identical for the same person with/without a
        marker. Bare markers ("left", "dropped") outside parens count too.

        `enrollment_status` is added whenever a name column exists (default
        "active"); `is_cancelled` only when at least one marker was found —
        preserving the previous conditional-flag behaviour for clean sheets.
        """
        col = roles.get("name")
        if col is None or col not in df.columns:
            return

        statuses: List[str] = []
        cleaned_vals: List[Any] = []
        fast_flags: List[bool] = []
        n_marked = 0
        n_fast = 0
        for val in df[col].astype("string"):
            if not isinstance(val, str):
                statuses.append("active")
                cleaned_vals.append(val)
                fast_flags.append(False)
                continue

            status = "active"
            fast = False

            def classify_paren(match: "re.Match[str]") -> str:
                nonlocal status, fast
                inner = match.group(1)
                for label, rx in STATUS_MARKERS:
                    if rx.search(inner):
                        if status == "active":
                            status = label
                        return ""  # strip the whole marker parenthetical
                # Operational notes: "(fast track)", "(FT till july end)",
                # "(only till 30 may)" — not lifecycle, but must be stripped so
                # the same person hashes identically across sheets.
                if FAST_TRACK_MARKER_RE.search(inner):
                    fast = True
                    return ""
                if NOTE_MARKER_RE.search(inner):
                    return ""
                return match.group(0)  # legit parenthetical, keep

            new = _NAME_PAREN_RE.sub(classify_paren, val)
            # Bare markers without parentheses ("left", "dropped", "cancelled").
            if status == "active" and CANCEL_MARKER_RE.search(new):
                status = "cancelled"
                new = CANCEL_MARKER_RE.sub("", new)
            # Residue like "Avyukt bansal (ft) 30/6" -> "Avyukt bansal 30/6".
            new = _TRAILING_DATE_FRAGMENT_RE.sub("", new)
            new = re.sub(r"\s+", " ", new).strip()

            if status != "active":
                n_marked += 1
            if fast:
                n_fast += 1
            statuses.append(status)
            cleaned_vals.append(new)
            fast_flags.append(fast)

        df["enrollment_status"] = statuses
        df[col] = pd.Series(cleaned_vals, index=df.index, dtype="string")
        if n_marked:
            df["is_cancelled"] = [s in ("cancelled", "refunded") for s in statuses]
            # Build the union now as well: a sheet may carry typed
            # cancellations and no lifecycle column at all, and the capability
            # check promises `dropout_rate` on that basis alone. The completion
            # path rebuilds it afterwards once `is_not_coming` also exists.
            self._derive_dropped(df)
            issues.append(
                f"Derived enrollment_status from name markers for {n_marked} "
                f"row(s); markers stripped before hashing"
            )
        if n_fast:
            df["is_fast_track"] = fast_flags
            issues.append(
                f"Derived is_fast_track for {n_fast} row(s) from name notes"
            )

    def _derive_funnel_flags(
        self, df: pd.DataFrame, roles: Mapping[str, str], issues: List[str]
    ) -> None:
        """Add the 3 funnel-stage flags supportable by real sheets.

        Real data only carries enquiry -> admission -> fee-paid. Contacted /
        counselling / demo / completed stages do not exist as columns, so they
        are deliberately not invented. Each flag is added only when its source
        column is present.
        """
        # is_enquiry: a retained row has at least an enquiry. Only meaningful on
        # admission/enquiry sheets (where an enquiry_date or admission funnel
        # exists), so gate on the presence of an enquiry/admission date role.
        if roles.get("enquiry_date") or roles.get("admission_date"):
            df["is_enquiry"] = True

        adm_col = roles.get("admission_date") or roles.get("joining_date")
        if adm_col and pd.api.types.is_datetime64_any_dtype(df[adm_col]):
            df["is_admitted"] = df[adm_col].notna()
            issues.append(
                f"Derived is_admitted from '{adm_col}' "
                f"({int(df['is_admitted'].sum())} admitted)"
            )

        money_col = roles.get("paid") or roles.get("amount")
        if money_col and pd.api.types.is_numeric_dtype(df[money_col]):
            df["is_fee_paid"] = (pd.to_numeric(df[money_col], errors="coerce") > 0)
            issues.append(
                f"Derived is_fee_paid from '{money_col}' "
                f"({int(df['is_fee_paid'].sum())} paid)"
            )

    def _derive_certificate_flags(
        self, df: pd.DataFrame, roles: Mapping[str, str], issues: List[str]
    ) -> None:
        """From issue_date, derive certificate delay days + pending flag.

        delay = issue_date - (joining/admission date). Pending = joined but no
        issue_date. Only runs when an issue_date column is present (certificate
        sheet); skipped silently otherwise.
        """
        issue_col = roles.get("issue_date")
        if not issue_col or not pd.api.types.is_datetime64_any_dtype(df[issue_col]):
            return

        start_col = roles.get("joining_date") or roles.get("admission_date")
        if start_col and pd.api.types.is_datetime64_any_dtype(df[start_col]):
            delay = (df[issue_col].dt.normalize() - df[start_col].dt.normalize()).dt.days
            df["certificate_delay_days"] = delay
            issued = int(df[issue_col].notna().sum())
            issues.append(
                f"Derived certificate_delay_days from '{issue_col}' - '{start_col}' "
                f"({issued} issued)"
            )
            # Joined but not yet issued = pending certificate.
            df["is_certificate_pending"] = df[start_col].notna() & df[issue_col].isna()
        else:
            df["is_certificate_pending"] = df[issue_col].isna()
        pending = int(df["is_certificate_pending"].sum())
        if pending:
            issues.append(f"Flagged {pending} pending certificate(s)")

    def _derive_duplicate_certificate(
        self, df: pd.DataFrame, roles: Mapping[str, str], issues: List[str]
    ) -> None:
        """Flag certificate numbers issued more than once (integrity red flag).

        A repeated certificate_number means the same serial was assigned to two
        rows — a clerical error or worse. Blanks are ignored (a missing number is
        `is_certificate_pending`, not a duplicate). Only runs when the sheet
        carries a certificate_number role.
        """
        cert_col = roles.get("certificate_number")
        if not cert_col or cert_col not in df.columns:
            return
        key = df[cert_col].astype("string").str.strip()
        key = key.where(key.notna() & (key != ""), other=pd.NA)
        dup = key.notna() & key.duplicated(keep=False)
        if not dup.any():
            return
        df["is_duplicate_certificate"] = dup
        n_rows = int(dup.sum())
        n_numbers = int(key[dup].nunique())
        issues.append(
            f"Flagged {n_rows} row(s) sharing {n_numbers} duplicate "
            f"certificate number(s) in '{cert_col}'"
        )

    def _derive_enquiry_backlog(
        self, df: pd.DataFrame, roles: Mapping[str, str], issues: List[str]
    ) -> None:
        """Flag stale, unconverted enquiries (is_enquiry_backlog).

        Backlog = an enquiry with no admission whose enquiry date is older than
        ENQUIRY_BACKLOG_DAYS relative to the frame's own latest enquiry (the
        export is historical, so wall-clock would overstate every age). Needs an
        enquiry_date role; admission is read from is_admitted when present, else
        from an admission/joining date column.
        """
        enq_col = roles.get("enquiry_date")
        if not enq_col or not pd.api.types.is_datetime64_any_dtype(df[enq_col]):
            return
        enq = df[enq_col]
        as_of = enq.max()
        if pd.isna(as_of):
            return
        age = (as_of - enq).dt.days

        if "is_admitted" in df.columns:
            admitted = df["is_admitted"].fillna(False)
        else:
            adm_col = roles.get("admission_date") or roles.get("joining_date")
            admitted = (
                df[adm_col].notna()
                if adm_col and pd.api.types.is_datetime64_any_dtype(df[adm_col])
                else pd.Series(False, index=df.index)
            )

        backlog = enq.notna() & ~admitted & (age > ENQUIRY_BACKLOG_DAYS)
        if not backlog.any():
            return
        df["is_enquiry_backlog"] = backlog
        issues.append(
            f"Flagged {int(backlog.sum())} stale enquir(ies) unconverted "
            f">{ENQUIRY_BACKLOG_DAYS}d (is_enquiry_backlog)"
        )

    def _split_multivalue(
        self, df: pd.DataFrame, roles: Mapping[str, str], issues: List[str]
    ) -> Dict[str, str]:
        """Add `<col>_list` columns of comma-split values for multi-value roles.

        Non-destructive: the original column is kept intact (still the raw
        joined string), and a parallel list column is added so downstream can
        `explode` the dimension it needs. Returns {role: new_list_column}.
        """
        added: Dict[str, str] = {}
        for role in MULTIVALUE_ROLES:
            col = roles.get(role)
            if col is None:
                continue
            list_col = f"{col}_list"
            parsed = df[col].map(self._split_cell)
            df[list_col] = parsed
            multi = int(parsed.map(lambda v: isinstance(v, list) and len(v) > 1).sum())
            added[role] = list_col
            if multi:
                issues.append(
                    f"{role} '{col}': {multi} row(s) had multiple values; added "
                    f"list column '{list_col}'"
                )
        return added

    @staticmethod
    def _split_cell(value: Any) -> Any:
        """Split a single cell on commas into a trimmed list; NaN stays NaN."""
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return np.nan
        parts = [p.strip() for p in str(value).split(",")]
        parts = [p for p in parts if p and p.lower() not in ("nan", "none")]
        return parts or np.nan

    def _normalize_categoricals(
        self, df: pd.DataFrame, roles: Mapping[str, str]
    ) -> pd.DataFrame:
        cat_roles = list(CATEGORICAL_ROLES) + self._discovered_roles(roles, "discovered_dim")
        for role in cat_roles:
            col = roles.get(role)
            if col is None:
                continue
            # Only normalize non-numeric columns. pandas >=3 reports string
            # columns as the `str` dtype rather than `object`, so check against
            # numeric kinds instead of testing for object dtype.
            if pd.api.types.is_numeric_dtype(df[col]):
                continue
            normalized = (
                df[col].astype(str).str.strip().str.lower().str.replace(
                    r"\s+", " ", regex=True
                )
            )
            df[col] = normalized.replace({"nan": np.nan, "none": np.nan, "": np.nan})
        return df

    def _apply_canonical_maps(
        self, df: pd.DataFrame, roles: Mapping[str, str], issues: List[str]
    ) -> pd.DataFrame:
        """Collapse real-world vocabulary chaos onto canonical values.

        Runs AFTER `_normalize_categoricals` (values are lowercased) and BEFORE
        `_split_multivalue` (so list columns inherit canonical values).

        - faculty: honorifics stripped + alias table ("yash kanodia sir" and
          "yash k" merge; "yash" stays a different person).
        - course: typo fixes + module-suffix extraction + family mapping. The
          role column is OVERWRITTEN with the bounded `course family` (so EDA /
          cross-tabs stop exploding past cardinality limits); the raw string is
          preserved in `<col>_raw` and the module suffix in `course_module`.
        """
        fac_col = roles.get("faculty")
        if fac_col and fac_col in df.columns and not pd.api.types.is_numeric_dtype(df[fac_col]):
            canon = df[fac_col].map(canonical_maps.canonicalize_faculty)
            changed = int((canon != df[fac_col]).fillna(False).sum())
            if changed:
                df[f"{fac_col}_raw"] = df[fac_col]
                df[fac_col] = canon
                issues.append(
                    f"faculty '{fac_col}': canonicalized {changed} value(s) "
                    f"(honorifics/aliases); raw kept in '{fac_col}_raw'"
                )

        # Branch is a CLOSED set of three. A value outside it is a data-entry
        # error, not a fourth site — a branch breakdown that accepts one invents
        # a centre the institute does not have.
        #
        # Two kinds of error, handled differently. A known locality ("adajan",
        # which is served by Pal) is resolved to its branch, because leaving it
        # alone splits Pal's numbers across two rows of every breakdown; the
        # original is kept in '<col>_locality' so the move can be audited or
        # undone. Anything else is left exactly as written and flagged — quietly
        # guessing a site would move a student between centres.
        for role in ("branch", "preferred_branch"):
            branch_col = roles.get(role)
            if not branch_col or branch_col not in df.columns:
                continue
            if pd.api.types.is_numeric_dtype(df[branch_col]):
                continue
            df[branch_col] = df[branch_col].map(canonical_maps.canonicalize_branch)

            served_by = df[branch_col].map(canonical_maps.branch_from_locality)
            is_locality = served_by.notna()
            if is_locality.any():
                localities = sorted(set(df.loc[is_locality, branch_col].astype(str)))
                df[f"{branch_col}_locality"] = df[branch_col].where(is_locality)
                df.loc[is_locality, branch_col] = served_by[is_locality]
                issues.append(
                    f"{role} '{branch_col}': {int(is_locality.sum())} row(s) named "
                    f"a locality instead of a site "
                    f"({', '.join(localities[:5])}); resolved to the branch that "
                    f"serves it. Original kept in '{branch_col}_locality'.")

            known = df[branch_col].map(canonical_maps.is_known_branch)
            unknown = df[branch_col].notna() & ~known
            if unknown.any():
                names = sorted(set(df.loc[unknown, branch_col].astype(str)))
                df["is_known_branch"] = known | df[branch_col].isna()
                issues.append(
                    f"{role} '{branch_col}': {int(unknown.sum())} row(s) name "
                    f"something outside the three branches "
                    f"({', '.join(canonical_maps.BRANCHES)}): "
                    f"{', '.join(names[:5])}. Kept as-is and flagged in "
                    f"is_known_branch — these are not sites, and no locality "
                    f"mapping is known for them.")

        self._apply_reporting_taxonomies(df, roles, issues)

        course_col = roles.get("course")
        if course_col and course_col in df.columns and not pd.api.types.is_numeric_dtype(df[course_col]):
            pairs = df[course_col].map(canonical_maps.canonicalize_course)
            families = pairs.map(lambda p: p[0])
            modules = pairs.map(lambda p: p[1])
            changed = int((families != df[course_col]).fillna(False).sum())
            n_modules = int(modules.notna().sum())
            if changed or n_modules:
                df[f"{course_col}_raw"] = df[course_col]
                df[course_col] = families
                if n_modules:
                    df["course_module"] = modules
                before = int(df[f"{course_col}_raw"].nunique(dropna=True))
                after = int(df[course_col].nunique(dropna=True))
                issues.append(
                    f"course '{course_col}': mapped to canonical families "
                    f"({before} -> {after} distinct); raw kept in "
                    f"'{course_col}_raw'"
                    + (f"; module suffix extracted for {n_modules} row(s) "
                       f"into 'course_module'" if n_modules else "")
                )
        return df

    def _normalize_pincode(
        self, df: pd.DataFrame, roles: Mapping[str, str], issues: List[str]
    ) -> pd.DataFrame:
        col = roles.get("pincode")
        if not col:
            return df
        digits = df[col].astype(str).str.replace(r"\D", "", regex=True)
        valid = digits.str.fullmatch(r"\d{6}")
        invalid_count = int((~valid & df[col].notna()).sum())
        df[col] = digits.where(valid, np.nan)
        if invalid_count:
            issues.append(f"pincode '{col}': {invalid_count} invalid value(s) set to null")
        return df

    def _compute_lead_to_admission_days(
        self, df: pd.DataFrame, roles: Mapping[str, str], issues: List[str]
    ) -> pd.DataFrame:
        enq = roles.get("enquiry_date")
        adm = roles.get("admission_date") or roles.get("joining_date")
        if not (enq and adm) or enq == adm:
            return df
        # Compare calendar days only: enquiry timestamps carry a time component
        # (e.g. 16:40) while admission is date-only (00:00), so a raw subtraction
        # would report a spurious -1 day for same-day or next-day admissions.
        delta = (df[adm].dt.normalize() - df[enq].dt.normalize()).dt.days
        df["lead_to_admission_days"] = delta
        negative = int((delta < 0).sum())
        if negative:
            df["admission_before_enquiry_flag"] = delta < 0
            issues.append(
                f"{negative} row(s) have admission earlier than enquiry "
                "(flagged in admission_before_enquiry_flag)"
            )
        return df

    def _primary_date_col(self, roles: Mapping[str, str]) -> Optional[str]:
        """First available non-DOB date column, in DATE_ROLES priority order."""
        return next((roles[r] for r in DATE_ROLES if r != "dob" and r in roles), None)

    def _build_event_date(
        self, df: pd.DataFrame, roles: Mapping[str, str]
    ) -> Optional[pd.Series]:
        """Coalesce all non-DOB date columns into one per-row `event_date`.

        Real admission sheets often have a blank `Date of Admission` for
        enquiry-only / not-yet-admitted rows while still carrying a `Timestamp`
        (enquiry date). Picking one column and dropping blanks would discard
        valid leads, so instead each row falls back through the date roles in
        priority order. Returns None if no usable date role exists.
        """
        date_cols = [roles[r] for r in DATE_ROLES if r != "dob" and r in roles]
        # No canonical date role? Fall back to a value-discovered date so a sheet
        # with only a cryptically-named date column still gets a timeline.
        if not date_cols:
            date_cols = [roles[r] for r in self._discovered_roles(roles, "discovered_date")]
        date_cols = [
            c for c in date_cols if pd.api.types.is_datetime64_any_dtype(df[c])
        ]
        if not date_cols:
            return None
        event = pd.Series(pd.NaT, index=df.index, dtype="datetime64[ns]")
        for col in date_cols:  # already in priority order
            event = event.fillna(df[col])
        return event

    def _derive_time_dimensions(self, df: pd.DataFrame) -> List[str]:
        """Add report-friendly time columns from the coalesced event_date.

        Creates: period_year, period_month (1-12), period_month_name,
        period_quarter (e.g. 'Q2-2026'), period_week (ISO week), period_day_name,
        period_is_weekend. These let downstream EDA/Analyst/Viz group by month,
        quarter, weekday/weekend without re-deriving dates.

        Returns the list of column names added (empty if no usable date).
        """
        if "event_date" not in df.columns or not pd.api.types.is_datetime64_any_dtype(
            df["event_date"]
        ):
            return []

        d = df["event_date"]
        df["period_year"] = d.dt.year.astype("Int64")
        df["period_month"] = d.dt.month.astype("Int64")
        df["period_month_name"] = d.dt.strftime("%b")
        # Quarter as a sortable label tied to the year, e.g. 'Q2-2026'.
        df["period_quarter"] = (
            "Q" + d.dt.quarter.astype("Int64").astype("string") + "-" +
            d.dt.year.astype("Int64").astype("string")
        )
        df["period_week"] = d.dt.isocalendar().week.astype("Int64")
        df["period_day_name"] = d.dt.day_name()
        df["period_is_weekend"] = d.dt.dayofweek >= 5  # Sat=5, Sun=6

        return [
            "event_date",
            "period_year",
            "period_month",
            "period_month_name",
            "period_quarter",
            "period_week",
            "period_day_name",
            "period_is_weekend",
        ]

    def _drop_invalid_rows(
        self, df: pd.DataFrame, roles: Mapping[str, str]
    ) -> tuple[pd.DataFrame, Dict[str, int]]:
        """Drop rows with no usable date at all. Returns (df, reasons).

        Uses the coalesced `event_date`, so a row is kept as long as *any* of
        its date roles is present (e.g. enquiry-only admission rows survive).

        **A membership tab is exempt.** On the timetable sheets the fact is the
        row's presence, not when it was typed: `Timestamp` is only filled for
        rows created through the form, so 158 of 277 completions carry no date
        at all. Dropping them deletes 57% of the completions and then trips the
        drop-fraction guard, blocking the sheet outright. A labelled row with no
        date is still a labelled row.
        """
        reasons: Dict[str, int] = {}
        if "event_date" in df.columns and not self._single_completion_label(df):
            before = len(df)
            df = df[df["event_date"].notna()]
            removed = before - len(df)
            if removed:
                reasons["missing_primary_date"] = removed
        return df, reasons

    # ------------------------------------------------------------ person id

    # A discriminator populated on less than this share of rows splits people
    # instead of separating them, so it is refused.
    MIN_DISCRIMINATOR_COVERAGE = 0.80

    def _derive_person_id(
        self, df: pd.DataFrame, roles: Mapping[str, str], issues: List[str]
    ) -> List[str]:
        """Stable cross-source person identity (student-id is NOT a person id).

        The same person re-enrolls under new student-ids (real data: Khiren
        Jain holds ids 3, 244, 609, 1070), so person-level metrics need their
        own key: person_id = salted hash of normalized name + last-10-digit
        phone. Runs AFTER lifecycle/note markers are stripped from the name
        (so "(cancelled)" variants hash identically) and BEFORE PII masking
        (needs the raw values); only the hash is emitted, nothing raw leaks.

        Conditional emission: requires a name role, plus at least one
        discriminator (phone, DOB or email) before any *person-grain* metric is
        emitted. Names alone are not an identity here — on `fees-data`, which
        carries no phone column, a name-only key merged 219 real people into
        166 and inflated the repeat-enrollment count by 20%, silently. When
        nothing discriminates, `person_id` is still emitted (it is the right
        join key to a richer source) but `is_repeat_enrollment` is withheld and
        the Analyst falls back rather than reporting a number built on
        namesakes.

        A discriminator is only used when it is populated on most rows: a
        sparse phone column splits one person into "with phone" and "without
        phone", which is the opposite failure and just as wrong.

        Returns the basis actually used, for the quality report.
        """
        name_col = roles.get("name")
        if not name_col or name_col not in df.columns:
            return []
        names = df[name_col].map(self._normalize_person_name)

        parts = [names]
        basis = ["name"]
        for role, normalize in (
            ("student_mobile", self._normalize_phone_digits),
            ("dob", self._normalize_identity_date),
            ("email", self._normalize_identity_email),
        ):
            col = roles.get(role)
            if not col or col not in df.columns:
                continue
            values = df[col].map(normalize)
            coverage = float((values != "").mean()) if len(values) else 0.0
            if coverage < self.MIN_DISCRIMINATOR_COVERAGE:
                if coverage:
                    issues.append(
                        f"person_id: ignored {col!r} as an identity "
                        f"discriminator - populated on only {coverage:.0%} of "
                        f"rows, so keying on it would split one person in two.")
                continue
            parts.append(values)
            basis.append(role)

        keys = parts[0]
        for extra in parts[1:]:
            keys = keys + "|" + extra
        df["person_id"] = keys.where(names != "").map(self._hash_value)

        counts = df.groupby("person_id")["person_id"].transform("size")
        df["person_enrollment_count"] = counts.where(df["person_id"].notna())

        if len(basis) == 1:
            distinct = int(names[names != ""].nunique())
            merged = int((counts > 1).sum())
            issues.append(
                f"person_id: no phone, date-of-birth or email in this source, "
                f"so identity is name-only. {distinct} distinct name(s) cover "
                f"{merged} row(s); namesakes and re-admissions are "
                f"indistinguishable. Person-grain metrics are withheld - join "
                f"a source with contact details to compute them.")
            return basis

        n_repeat_rows = int((counts > 1).sum())
        if n_repeat_rows:
            df["is_repeat_enrollment"] = (counts > 1).fillna(False)
            issues.append(
                f"person_id: {n_repeat_rows} row(s) belong to repeat-enrollment "
                f"person(s) (identity = {' + '.join(basis)})"
            )
            self._derive_current_admission(df, roles, issues)
        return basis

    def _derive_current_admission(
        self, df: pd.DataFrame, roles: Mapping[str, str], issues: List[str]
    ) -> None:
        """Mark each person's latest admission, per the institute's own rule.

            current(person) := max Date of Joining, tie-break max admission_id

        `student-id` is an ADMISSION id: a student who re-enrols is issued a new
        one (Khiren Jain holds 3, 244, 609, 1070). Their **current fee position
        is the latest admission only** — earlier ids are closed history. Summing
        every admission overstates both revenue and receivable.

        Only emitted when repeat enrollments exist AND identity was confirmed by
        a real discriminator: on a name-only key this would supersede one
        namesake's live admission with another's, which is worse than not having
        the flag. Deliberately advisory — no money metric is silently rewritten
        to use it. The report says how much receivable sits on superseded rows
        and the operator decides.
        """
        date_col = roles.get("joining_date") or roles.get("admission_date")
        id_col = roles.get("student_id")
        if not date_col and not id_col:
            return

        order = pd.DataFrame(index=df.index)
        if date_col and date_col in df.columns:
            order["_d"] = pd.to_datetime(df[date_col], errors="coerce")
        if id_col and id_col in df.columns:
            order["_i"] = pd.to_numeric(df[id_col], errors="coerce")
        if order.empty:
            return

        # Rank descending within a person: 1 is the current admission. NaT/NaN
        # sort last, so a dated row always beats an undated one.
        order["_p"] = df["person_id"]
        seq = (
            order.sort_values(list(order.columns[:-1]), ascending=False,
                              na_position="last", kind="stable")
            .groupby("_p", sort=False)
            .cumcount() + 1
        ).reindex(df.index)

        df["person_admission_seq"] = seq.where(df["person_id"].notna())
        df["is_current_admission"] = (seq == 1).where(df["person_id"].notna())

        superseded = int((seq > 1).sum())
        if not superseded:
            return
        note = (
            f"{superseded} row(s) are superseded admissions — the same person "
            f"re-enrolled later. Flagged as is_current_admission=False "
            f"(ordering: {'latest ' + date_col if date_col else ''}"
            f"{', tie-break highest ' + id_col if id_col else ''})."
        )
        pending_col = roles.get("pending")
        if pending_col and pending_col in df.columns:
            stale = pd.to_numeric(
                df.loc[seq > 1, pending_col], errors="coerce"
            ).sum()
            total = pd.to_numeric(df[pending_col], errors="coerce").sum()
            if total:
                note += (
                    f" They carry {stale:,.0f} of {total:,.0f} outstanding "
                    f"({stale / total:.0%}); by the institute's rule that is "
                    f"closed history, not receivable."
                )
        issues.append(note)

    def _derive_staff_alumni(
        self, df: pd.DataFrame, roles: Mapping[str, str], issues: List[str]
    ) -> None:
        """Flag student rows whose name also appears as faculty or counsellor.

        The institute hires its staff out of its own students, so the same
        human is a student in one column and the tutor in another. Three things
        follow, and all of them are wrong if the collision goes unnoticed:

        - **It is not a duplicate.** A student row and a faculty cell bearing
          one name are the same person in two roles, not two records to merge.
        - **It is not necessarily churn.** A student who joins the staff stops
          attending as a student; landing in a not-coming tab makes them look
          like a loss when they are the opposite.
        - **Staff appear in two populations.** Counting them under both
          "students per tutor" and "students" double-counts the human.

        The flag is advisory and deliberately not acted on: a match may equally
        be a namesake, which is the same ambiguity `person_id` refuses to
        resolve on a name alone. Surfacing it lets the operator decide; merging
        it silently would be a guess.
        """
        from . import canonical_maps as cm

        name_col = roles.get("name")
        staff_cols = [roles[r] for r in ("faculty", "counsellor")
                      if r in roles and roles[r] in df.columns]
        if not name_col or name_col not in df.columns or not staff_cols:
            return

        staff: set = set()
        for col in staff_cols:
            for value in df[col].dropna().unique():
                # Faculty values carry honorifics ("Yash Kanodia Sir") that a
                # student row never does; canonicalize before comparing.
                normalized = cm.canonicalize_faculty(
                    self._normalize_person_name(value))
                if normalized:
                    staff.add(normalized)
        staff.discard("")
        if not staff:
            return

        students = df[name_col].map(self._normalize_person_name)
        hits = students.isin(staff) & (students != "")
        if not hits.any():
            return

        df["is_staff_alumni"] = hits
        issues.append(
            f"{int(hits.sum())} student row(s) carry a name that also appears "
            f"as faculty/counsellor on this sheet (staff are hired from the "
            f"student body). Flagged as is_staff_alumni, NOT merged — the "
            f"match may equally be a namesake.")

    @staticmethod
    def _normalize_person_name(value: Any) -> str:
        if pd.isna(value):
            return ""
        text = _PERSON_NAME_NORM_RE.sub(" ", str(value).strip().lower())
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _normalize_phone_digits(value: Any) -> str:
        if pd.isna(value):
            return ""
        text = str(value).strip()
        if text.endswith(".0"):  # float artifact from numeric CSV columns
            text = text[:-2]
        digits = _NON_DIGIT_RE.sub("", text)
        return digits[-10:] if len(digits) >= 10 else ""

    @staticmethod
    def _normalize_identity_date(value: Any) -> str:
        """Date reduced to its digits, so 5/4/24 and 05/04/2024 hash alike.

        Deliberately format-agnostic rather than parsed: a DOB used as an
        identity discriminator only has to be *consistent* across the rows of
        one person, and the sheets disagree about day-first vs month-first.
        """
        if pd.isna(value):
            return ""
        digits = _NON_DIGIT_RE.sub("", str(value).strip())
        return digits if len(digits) >= 6 else ""

    @staticmethod
    def _normalize_identity_email(value: Any) -> str:
        if pd.isna(value):
            return ""
        # Typed-in addresses carry stray spaces, and one sheet writes the dot
        # before "com" as a comma.
        text = re.sub(r"\s+", "", str(value).strip().lower()).replace(",", ".")
        return text if "@" in text and "." in text.split("@")[-1] else ""

    # ------------------------------------------------------------------- PII

    def _mask_pii(
        self, df: pd.DataFrame, roles: Mapping[str, str], issues: List[str]
    ) -> Dict[str, str]:
        """Hash PII columns in place; return role -> canonical column mapping.

        Masks two sets of columns:
        1. Columns mapped to a PII role.
        2. Any *unmapped* column whose header matches a PII keyword (e.g. a
           second 'Mobile No (Mother / Guardian)' or 'Admission Form Photo'
           that lost the single-column-per-role race). This is a safety net so
           no raw PII leaks downstream just because a role was already filled.
        """
        canonical: Dict[str, str] = dict(roles)

        to_mask: Dict[str, str] = {}
        pii_roles = list(PII_ROLES) + self._discovered_roles(roles, "discovered_pii")
        for role in pii_roles:
            col = roles.get(role)
            if col is not None:
                to_mask[col] = role

        for col in df.columns:
            if col in to_mask:
                continue
            header = str(col).strip().lower()
            if any(kw in header for kw in PII_HEADER_KEYWORDS):
                to_mask[col] = "pii_keyword"

        for col, role in to_mask.items():
            df[col] = df[col].map(self._hash_value)
            issues.append(f"Masked PII column '{col}' (role={role}) to hashed IDs")
        return canonical

    def _hash_value(self, value: Any) -> Any:
        if pd.isna(value):
            return np.nan
        digest = hashlib.sha256(f"{self.salt}:{value}".encode("utf-8")).hexdigest()
        return digest[:16]

    def _scrub_free_text_pii(
        self, df: pd.DataFrame, roles: Mapping[str, str], issues: List[str]
    ) -> None:
        """Redact inline phone/email PII from retained free-text columns.

        Whole PII columns are already hashed by `_mask_pii`; this handles contact
        details typed INSIDE an analytical note we keep (a Not_Coming
        "Status & reason", a receipt Description). Only free-text columns are
        touched — a status_reason/description role or a header keyword match — so
        id / amount / date columns are never mangled. Runs after masking, so a
        hashed column (16-hex) is scanned harmlessly (no 10+ digit run to hit).
        """
        targets: set = set()
        for role in ("status_reason", "description"):
            col = roles.get(role)
            if col and col in df.columns:
                targets.add(col)
        for col in df.columns:
            if _FREE_TEXT_HEADER_RE.search(str(col)):
                targets.add(col)

        n_cells = 0
        for col in targets:
            series = df[col]
            if not (series.dtype == object or pd.api.types.is_string_dtype(series)):
                continue
            scrubbed = series.map(self._scrub_inline_pii)
            changed = int(
                (scrubbed.astype("string").fillna("")
                 != series.astype("string").fillna("")).sum()
            )
            if changed:
                df[col] = scrubbed
                n_cells += changed
        if n_cells:
            issues.append(
                f"Scrubbed inline phone/email from {n_cells} free-text cell(s)"
            )

    @staticmethod
    def _scrub_inline_pii(value: Any) -> Any:
        """Replace phone runs (>= _MIN_PHONE_DIGITS digits) and emails with tokens."""
        if not isinstance(value, str):
            return value

        def redact_phone(match: "re.Match[str]") -> str:
            run = match.group(0)
            if sum(ch.isdigit() for ch in run) >= _MIN_PHONE_DIGITS:
                return "[mobile]"
            return run

        out = _INLINE_PHONE_RE.sub(redact_phone, value)
        return _INLINE_EMAIL_RE.sub("[email]", out)

    # --------------------------------------------------------------- dedupe

    def _dedupe(
        self, df: pd.DataFrame, roles: Mapping[str, str], issues: List[str]
    ) -> List[str]:
        """Drop exact duplicates on a natural key when one is inferable.

        Prefer a unique id (receipt_id / student_id). Otherwise fall back to a
        composite of person + contact + primary date.

        An id column is only a natural key if it is actually unique *in this
        frame*, which the institute's ids frequently are not:

        - Receipt books are per-branch and restart every few hundred entries,
          so the same receipt number is reused across branches and years. On
          the admission sheet `Receipt ID` is a foreign key to the fee ledger,
          not a row key at all — keying on it deleted 71 of 222 admissions.
        - A receipt paid part-cash part-online is entered as two ledger rows
          sharing one receipt id. Keying on it deletes a real payment and
          understates collections.

        So a candidate key is measured before it is trusted, and widened with
        whatever date / amount / person columns exist when it fails. Rows are
        only removed when the widened key still identifies a row.
        """
        if (
            "student_id" in roles
            and "source_domain" in df.columns
            and set(df["source_domain"].dropna().astype(str)).issubset({"finance"})
        ):
            return []

        composite = [
            roles[r]
            for r in ("student_id", "name", "student_mobile", "enquiry_date", "joining_date")
            if r in roles
        ]
        id_col = roles.get("receipt_id") or roles.get("certificate_number")
        if id_col:
            key_cols = [id_col]
            if not self._is_row_key(df, key_cols):
                # Widen with everything that distinguishes two rows sharing an
                # id: when and how much, then who.
                widened = key_cols + [
                    roles[r] for r in ("receipt_date", "amount", "paid", "payment_mode")
                    if r in roles and roles[r] not in key_cols
                ]
                widened += [c for c in composite if c not in widened]
                if not self._is_row_key(df, widened):
                    issues.append(
                        f"{id_col!r} repeats and no composite key identifies a "
                        f"row; kept every row rather than guess at duplicates.")
                    return []
                issues.append(
                    f"{id_col!r} repeats in this source, so it is not a row "
                    f"key here; deduplicated on {widened} instead.")
                key_cols = widened
        else:
            key_cols = composite

        if not key_cols:
            return []
        before = len(df)
        df.drop_duplicates(subset=key_cols, keep="first", inplace=True)
        removed = before - len(df)
        if removed:
            issues.append(f"Removed {removed} duplicate row(s) on {key_cols}")
        return key_cols

    # Share of rows a candidate key may collapse before it stops being a key.
    # Non-zero because genuine re-entered duplicates exist in every sheet; a
    # key that folds away more than this is measuring something else.
    MAX_KEY_COLLAPSE = 0.02

    def _is_row_key(self, df: pd.DataFrame, key_cols: List[str]) -> bool:
        """True when `key_cols` identifies a row rather than a group of them."""
        cols = [c for c in key_cols if c in df.columns]
        if not cols or df.empty:
            return False
        collapsed = int(df.duplicated(subset=cols, keep="first").sum())
        return collapsed / len(df) <= self.MAX_KEY_COLLAPSE

    # ---------------------------------------------------------- multi-source

    def _read_source_frame(self, source: Mapping[str, Any]) -> pd.DataFrame:
        typ = str(source.get("type") or "csv").lower()
        path = source.get("path_or_query") or source.get("path")
        if typ == "csv":
            if not path or not os.path.exists(str(path)):
                raise FileNotFoundError(f"CSV not found: {path!r}")
            return pd.read_csv(path)
        if typ == "excel_sheet":
            if not path or not os.path.exists(str(path)):
                raise FileNotFoundError(f"Excel workbook not found: {path!r}")
            sheet = source.get("sheet_name") or source.get("name")
            return pd.read_excel(path, sheet_name=sheet, engine="openpyxl")
        raise ValueError(f"Unsupported source type: {typ}")

    def _source_output_name(self, source: Mapping[str, Any], name: str) -> str:
        base = source.get("path_or_query") or source.get("path") or name
        root, ext = os.path.splitext(str(base))
        safe = re.sub(r"\W+", "_", str(name)).strip("_") or "source"
        return f"{root}_{safe}{ext or '.csv'}"

    def _union_lifecycle_partitions(
        self,
        frames: Dict[str, pd.DataFrame],
        packages: List[JsonDict],
        issues: List[str],
    ) -> Tuple[Dict[str, pd.DataFrame], List[JsonDict]]:
        """Stack the timetable tabs into one roster instead of joining them.

        Course_Completed / Not_Coming / NOT TO ENTERTRAIN / Main_data are the
        same entity — one student roster partitioned by lifecycle membership.
        The join path treats extra sources as related tables and left-joins them
        onto a master, which keeps only the master's rows: supplying all four
        tabs of the real workbook produced **16 rows out of 406**, every one of
        them `active`, silently deleting every completion and every churn.

        A source qualifies as a partition when its sheet name resolved a single
        `completion_status` (see COMPLETION_BY_SOURCE). Fewer than two such
        sources is not a partitioned roster, so nothing happens.

        A student present in two tabs is kept once, at the most advanced label
        (LIFECYCLE_PRECEDENCE), and marked `lifecycle_conflict` so the operator
        can see the sheets disagree rather than inherit a silent choice.
        """
        members = [
            p for p in packages
            if p.get("source_name") in frames
            and self._single_completion_label(frames[str(p.get("source_name"))])
        ]
        if len(members) < 2:
            return frames, packages

        names = [str(p.get("source_name")) for p in members]
        parts = [frames[n] for n in names]
        stacked = pd.concat(parts, ignore_index=True, sort=False)

        rank = {label: i for i, label in enumerate(LIFECYCLE_PRECEDENCE)}
        stacked["_lifecycle_rank"] = (
            stacked["completion_status"].map(rank).fillna(len(rank)).astype(int)
        )

        key = [c for c in ("person_id",) if c in stacked.columns]
        course_col = (members[0].get("canonical_columns") or {}).get("course")
        if key and course_col and course_col in stacked.columns:
            key.append(course_col)

        # Only CONTRADICTIONS are collapsed. Two rows for one student in the
        # same tab, or in two tabs at the same label, are left alone: students
        # re-enroll (111 of 154 in the sample, up to 5 times), so same-label
        # repeats are real enrollments, not copies. Dropping them here cost 41
        # of 406 rows. A row that is both `completed` and `active`, though,
        # cannot be two enrollments — that is a copy someone forgot to delete.
        conflicts = 0
        removed_by_union = 0
        if key:
            best = stacked.groupby(key, dropna=True)["_lifecycle_rank"].transform("min")
            contested = stacked.groupby(key, dropna=True)["_lifecycle_rank"].transform(
                "nunique"
            ) > 1
            contested = contested.fillna(False)
            stacked["lifecycle_conflict"] = contested & (stacked["_lifecycle_rank"] == best)
            conflicts = int(stacked.loc[contested, key[0]].nunique())
            losers = contested & (stacked["_lifecycle_rank"] > best)
            removed_by_union = int(losers.sum())
            stacked = stacked[~losers]
        stacked = stacked.drop(columns=["_lifecycle_rank"])

        counts = stacked["completion_status"].value_counts().to_dict()
        union_notes = [
            f"Unioned {len(names)} lifecycle partition(s) of one roster "
            f"({', '.join(names)}) into {len(stacked)} row(s): "
            f"{', '.join(f'{k}={v}' for k, v in sorted(counts.items()))}. "
            f"Joining them instead would have kept only one tab's rows."
        ]
        if conflicts:
            union_notes.append(
                f"{conflicts} student(s) carry contradicting lifecycle labels "
                f"across tabs; kept at the most advanced one "
                f"({' > '.join(LIFECYCLE_PRECEDENCE)}) and flagged in "
                f"lifecycle_conflict. The sheets disagree — someone was copied "
                f"rather than moved. Same-label repeats are NOT collapsed: "
                f"students really do re-enroll."
            )
        issues.extend(union_notes)

        union_name = self._partition_union_name(names)
        stacked["source_name"] = union_name
        merged_roles: Dict[str, str] = {}
        for pkg in members:
            merged_roles.update(pkg.get("canonical_columns") or {})

        survivor = dict(members[0])
        survivor["source_name"] = union_name
        survivor["row_count"] = len(stacked)
        survivor["canonical_columns"] = merged_roles
        survivor["field_names"] = self._field_names(merged_roles)
        survivor["canonical_df_path"] = self._write_parquet(
            stacked, f"{union_name}_union.csv"
        )
        survivor["partition_members"] = names
        # Roll the members' accounting up, or the run reports one tab's original
        # row count against the whole union's output and the drop maths is a lie.
        survivor["quality_report"] = self._merge_quality_reports(
            members, len(stacked), union_notes, removed_by_union
        )
        survivor["schema"] = {c: str(t) for c, t in stacked.dtypes.items()}

        frames = {n: f for n, f in frames.items() if n not in names}
        frames[union_name] = stacked
        packages = [p for p in packages if p.get("source_name") not in names]
        packages.insert(0, survivor)
        return frames, packages

    @staticmethod
    def _merge_quality_reports(
        members: Sequence[JsonDict],
        row_count: int,
        notes: Sequence[str],
        removed_by_union: int = 0,
    ) -> JsonDict:
        """Sum the member tabs' accounting so the union's drop maths is real.

        `drop_count` is recomputed from (total rows in, rows out) rather than
        summed, because the union itself removes the contradicting copy of a
        student who sat in two tabs — a drop none of the members knows about.

        The union's own removals are counted, not inferred by subtraction: each
        member's per-source dedupe runs after its drop accounting is taken, so
        subtracting would silently charge those rows to the union.
        """
        reports = [dict(p.get("quality_report") or {}) for p in members]
        original = sum(int(r.get("original_row_count") or 0) for r in reports)
        reasons: Dict[str, int] = {}
        for r in reports:
            for key, value in (r.get("dropped_reasons") or {}).items():
                reasons[key] = reasons.get(key, 0) + int(value)
        if removed_by_union:
            reasons["contradicting_lifecycle_tabs"] = removed_by_union
        residual = original - sum(reasons.values()) - row_count
        if residual > 0:
            reasons["deduplicated_within_source"] = residual

        known: List[str] = []
        for r in reports:
            for issue in r.get("known_issues") or []:
                if issue not in known:
                    known.append(issue)
        known.extend(notes)

        merged = dict(reports[0])
        merged.update({
            "original_row_count": original,
            "drop_count": original - row_count,
            "dropped_reasons": reasons,
            "known_issues": known,
        })
        return merged

    @staticmethod
    def _single_completion_label(frame: pd.DataFrame) -> bool:
        """True when the whole frame carries exactly one lifecycle label.

        That is the signature of a membership tab. A frame that mixes labels is
        already a roster and must not be stacked onto anything.
        """
        if "completion_status" not in frame.columns:
            return False
        return frame["completion_status"].dropna().nunique() == 1

    @staticmethod
    def _partition_union_name(names: Sequence[str]) -> str:
        """Name the union after the workbook the tabs came from, when they say.

        `student_timetable__not_coming` -> `student_timetable`. Falls back to a
        generic label so the name is never empty or misleadingly one tab's.
        """
        prefixes = {n.split("__")[0] for n in names if "__" in n}
        if len(prefixes) == 1:
            return f"{prefixes.pop()}_all_tabs"
        return "lifecycle_roster"

    def _build_joined_frame(
        self,
        frames: Mapping[str, pd.DataFrame],
        packages: Sequence[JsonDict],
        join_plan: Sequence[Mapping[str, Any]],
    ) -> Tuple[pd.DataFrame, JsonDict, List[str]]:
        package_by_name = {p.get("source_name"): p for p in packages}
        issues: List[str] = []
        accepted: List[JsonDict] = []
        rejected: List[JsonDict] = []
        master_name = self._choose_master_source(packages)
        master = frames[master_name].copy()
        joined_sources = {master_name}

        for package in packages:
            name = package.get("source_name")
            if not name or name == master_name or name not in frames:
                continue
            right = frames[name].copy()
            relation = self._join_relation_for(master_name, name, join_plan)
            domain = package.get("source_domain")
            master_roles = package_by_name[master_name].get("canonical_columns") or {}
            right_roles = package.get("canonical_columns") or {}

            joined = False
            if "student_id" in master_roles and "student_id" in right_roles:
                master, detail = self._left_join_source(
                    master, right, master_roles["student_id"], right_roles["student_id"],
                    name, domain, relation or {"confidence": "high", "keys": ["student_id"]},
                )
                joined = detail["status"] == "accepted"
            elif "completion_status" in right.columns:
                master, detail = self._join_lifecycle_roster(
                    master, right, master_roles, right_roles, name, relation
                )
                joined = detail["status"] == "accepted"
            elif str(domain).startswith("admission"):
                master, detail = self._join_admission_identity(
                    master, right, master_roles, right_roles, name, relation
                )
                joined = detail["status"] == "accepted"
            else:
                detail = {
                    "status": "rejected", "left_source": master_name,
                    "right_source": name, "reason": "no high-confidence key",
                }

            if joined:
                accepted.append(detail)
                joined_sources.add(name)
                if detail.get("course_upgrades_matched"):
                    issues.append(
                        f"{detail['course_upgrades_matched']} row(s) matched "
                        f"'{name}' on person alone: one enrollment recorded "
                        f"under two course names, which is what a course "
                        f"upgrade entered on only one sheet looks like. Only "
                        f"done where the person had exactly one unmatched row "
                        f"on each side, so there was nothing else it could be."
                    )
                if detail.get("unlabelled_master_rows"):
                    issues.append(
                        f"{detail['unlabelled_master_rows']} row(s) got no "
                        f"lifecycle label from '{name}': "
                        f"{detail['person_in_roster_other_course']} are people "
                        f"the timetable knows but on a different course (their "
                        f"label belongs to that enrollment, not this one), and "
                        f"{detail['person_absent_from_roster']} are "
                        f"{detail['people_absent_from_roster']} people the "
                        f"timetable does not cover at all."
                    )
            else:
                rejected.append(detail)
                issues.append(
                    f"Source '{name}' not joined: {detail.get('reason', 'unsafe join')}"
                )

        # Bring join-prefixed derived flags/columns back to their bare names so
        # the Analyst / Prediction / Monitoring metrics compute on the merged frame.
        self._coalesce_derived_columns(master)

        if "source_name" not in master.columns:
            master["source_name"] = master_name
        if "source_domain" not in master.columns:
            master["source_domain"] = package_by_name[master_name].get("source_domain")

        relationships = {
            "master_source": master_name,
            "accepted": accepted,
            "rejected": rejected,
            "joined_sources": sorted(joined_sources),
            "unjoined_sources": [
                p.get("source_name") for p in packages
                if p.get("source_name") not in joined_sources
            ],
        }
        return master, relationships, issues

    def _coalesce_derived_columns(self, master: pd.DataFrame) -> None:
        """Coalesce join-prefixed derived columns back to their bare names.

        For each known derived column, if it only exists as `<source>__<col>`
        after the merge, build the bare `<col>` by taking the first non-null value
        across the master's own column (if any) and every prefixed variant. A
        student belongs to one source per flag (e.g. exactly one timetable tab),
        so first-non-null is unambiguous. Prefixed columns are left in place for
        provenance; nothing is dropped.
        """
        for canonical in COALESCE_DERIVED_COLUMNS:
            prefixed = [
                c for c in master.columns
                if c != canonical and c.endswith(f"__{canonical}")
            ]
            if not prefixed:
                continue
            candidates = ([canonical] if canonical in master.columns else []) + prefixed
            coalesced = master[candidates[0]]
            for col in candidates[1:]:
                coalesced = coalesced.where(coalesced.notna(), master[col])
            master[canonical] = coalesced

    def _choose_master_source(self, packages: Sequence[JsonDict]) -> str:
        def score(pkg):
            roles = pkg.get("canonical_columns") or {}
            domain = str(pkg.get("source_domain") or "")
            s = 0
            if "student_id" in roles:
                s += 100
            if domain in ("student", "master", "student_master"):
                s += 50
            if domain in ("finance", "certificate"):
                s -= 20
            if domain.startswith("admission"):
                s -= 10
            return s

        return str(max(packages, key=score).get("source_name"))

    def _join_relation_for(
        self, left: str, right: str, join_plan: Sequence[Mapping[str, Any]]
    ) -> Optional[Mapping[str, Any]]:
        for rel in join_plan:
            if rel.get("left_source") == left and rel.get("right_source") == right:
                return rel
            if rel.get("right_source") == left and rel.get("left_source") == right:
                return rel
        return None

    def _left_join_source(
        self,
        left: pd.DataFrame,
        right: pd.DataFrame,
        left_key: str,
        right_key: str,
        right_name: str,
        right_domain: Optional[str],
        relation: Mapping[str, Any],
    ) -> Tuple[pd.DataFrame, JsonDict]:
        detail = {
            "status": "rejected", "left_source": "master", "right_source": right_name,
            "keys": [left_key], "confidence": relation.get("confidence", "high"),
        }
        if left_key not in left.columns or right_key not in right.columns:
            detail["reason"] = "join key missing after cleaning"
            return left, detail

        left_keys = left[left_key].dropna()
        right_keys = right[right_key].dropna()
        right_key_values = set(right_keys.astype(str))
        left_key_values = set(left_keys.astype(str))
        overlap = left_key_values & right_key_values
        if not overlap:
            detail["reason"] = "no overlapping join key values"
            detail["cardinality"] = "no_overlap"
            return left, detail
        left_dup = bool(left_keys.duplicated().any())
        right_dup = bool(right_keys.duplicated().any())
        if left_dup and right_dup:
            detail["reason"] = "many-to-many join rejected"
            detail["cardinality"] = "many_to_many"
            return left, detail

        before = len(left)
        prepared, aggregated = self._prepare_right_for_join(right, right_key, right_name)
        rename = {right_key: left_key}
        prepared = prepared.rename(columns=rename)
        merged = left.merge(prepared, on=left_key, how="left")
        if len(merged) > before:
            detail["reason"] = "row multiplication rejected"
            detail["expected_row_count"] = before
            detail["actual_row_count"] = len(merged)
            return left, detail

        detail.update({
            "status": "accepted",
            "right_domain": right_domain,
            "cardinality": "many_to_one" if aggregated else "one_to_one",
            "left_unmatched": int((~left[left_key].astype(str).isin(right_key_values)).sum()),
            "right_unmatched": int((~right[right_key].astype(str).isin(left_key_values)).sum()),
            "aggregated_right_duplicates": aggregated,
        })
        return merged, detail

    def _prepare_right_for_join(
        self, right: pd.DataFrame, key: str, source_name: str
    ) -> Tuple[pd.DataFrame, bool]:
        safe = re.sub(r"\W+", "_", str(source_name)).strip("_").lower() or "source"
        renamed = right.copy()
        renamed = renamed.rename(
            columns={c: (c if c == key else f"{safe}__{c}") for c in renamed.columns}
        )
        if not renamed[key].dropna().duplicated().any():
            return renamed, False

        aggregations = {}
        for col in renamed.columns:
            if col == key:
                continue
            if pd.api.types.is_bool_dtype(renamed[col]):
                aggregations[col] = "max"
            elif pd.api.types.is_numeric_dtype(renamed[col]):
                aggregations[col] = "sum"
            elif pd.api.types.is_datetime64_any_dtype(renamed[col]):
                aggregations[col] = "max"
            else:
                aggregations[col] = "first"
        return renamed.groupby(key, as_index=False).agg(aggregations), True

    def _join_lifecycle_roster(
        self,
        left: pd.DataFrame,
        right: pd.DataFrame,
        left_roles: Mapping[str, str],
        right_roles: Mapping[str, str],
        right_name: str,
        relation: Optional[Mapping[str, Any]],
    ) -> Tuple[pd.DataFrame, JsonDict]:
        """Attach timetable lifecycle labels to the student master.

        The timetable tabs carry no `student-id`, so the only link to the
        student sheet is identity: the salted `person_id` (name + phone) plus
        the canonicalized course. Without this the roster is left unjoined and
        churn cannot be computed at all — the labels live on one sheet and the
        course start and length on another.

        The roster is collapsed to one row per (person, course) before the join,
        by lifecycle precedence. It has no enrollment id, so a person who took
        the same course twice cannot have their two attempts told apart; the
        approximation is reported rather than hidden. Every other guard in
        `_left_join_source` (overlap, cardinality, row multiplication) still
        applies.
        """
        detail: JsonDict = {
            "status": "rejected", "left_source": "master",
            "right_source": right_name, "confidence": "medium",
        }
        lc_course, rc_course = left_roles.get("course"), right_roles.get("course")
        if "person_id" not in left.columns or "person_id" not in right.columns:
            detail["reason"] = (
                "lifecycle roster needs person identity on both sides; one of "
                "them has no phone, date-of-birth or email to resolve people by"
            )
            return left, detail
        if not (lc_course and rc_course
                and lc_course in left.columns and rc_course in right.columns):
            detail["reason"] = "lifecycle roster needs a course column on both sides"
            return left, detail

        key = "__lifecycle_identity_key"
        left = left.copy()
        right = right.copy()
        left[key] = self._composite_key(left, ["person_id", lc_course])
        right[key] = self._composite_key(right, ["person_id", rc_course])

        upgraded = self._rekey_course_upgrades(left, right, key)

        rank = {label: i for i, label in enumerate(LIFECYCLE_PRECEDENCE)}
        right["_lifecycle_rank"] = (
            right["completion_status"].map(rank).fillna(len(rank)).astype(int)
        )
        before = len(right)
        right = (
            right.sort_values("_lifecycle_rank", kind="stable")
            .drop_duplicates(subset=[key], keep="first")
            .drop(columns=["_lifecycle_rank"])
            .sort_index()
        )
        collapsed = before - len(right)

        merged, join_detail = self._left_join_source(
            left, right, key, key, right_name, "timetable",
            relation or {"confidence": "medium", "keys": ["person_id", "course"]},
        )
        join_detail["match_method"] = "person_course"
        if upgraded:
            join_detail["course_upgrades_matched"] = upgraded
        if join_detail.get("status") == "accepted":
            join_detail.update(
                self._lifecycle_coverage(merged, left, right, key)
            )
        if collapsed:
            join_detail["collapsed_repeat_enrollments"] = collapsed
            join_detail["note"] = (
                f"{collapsed} roster row(s) were repeat enrollments on the same "
                f"course; the timetable tabs carry no enrollment id, so they "
                f"share one lifecycle label"
            )
        return merged.drop(columns=[key], errors="ignore"), join_detail

    @staticmethod
    def _rekey_course_upgrades(
        left: pd.DataFrame, right: pd.DataFrame, key: str
    ) -> int:
        """Match one enrollment recorded under two different course names.

        Confirmed by the institute: a student upgrades their course and the
        change is entered on one sheet but not the other. It is then one
        enrollment wearing two names, and a person+course join misses it —
        losing a lifecycle label that genuinely belongs to that row.

        Only the unambiguous case is repaired: a person with **exactly one**
        unmatched row on each side. There is nothing else the roster row could
        refer to, so its key is rewritten to the master's. Where a person has
        several unmatched rows on either side the pairing is a guess — two real
        enrollments look identical to one renamed enrollment — and they are left
        unmatched rather than labelled on a coin flip.

        Mutates `right` in place. Returns how many rows were re-keyed.
        """
        left_keys = set(left[key].dropna().astype(str))
        right_keys = set(right[key].dropna().astype(str))
        l_un = left[~left[key].astype(str).isin(right_keys)]
        r_un = right[~right[key].astype(str).isin(left_keys)]
        if l_un.empty or r_un.empty:
            return 0

        l_people = l_un["person_id"].astype(str)
        r_people = r_un["person_id"].astype(str)
        l_counts = l_people.value_counts()
        r_counts = r_people.value_counts()
        singles = set(l_counts[l_counts == 1].index) & set(r_counts[r_counts == 1].index)
        singles.discard("nan")
        if not singles:
            return 0

        target = dict(zip(l_people[l_people.isin(singles)],
                          l_un.loc[l_people.isin(singles), key]))
        rows = r_un.index[r_people.isin(singles)]
        right.loc[rows, key] = r_people.loc[rows].map(target).values
        return len(rows)

    @staticmethod
    def _lifecycle_coverage(
        merged: pd.DataFrame, left: pd.DataFrame, right: pd.DataFrame, key: str
    ) -> JsonDict:
        """Explain the master rows the roster could not label.

        "150 rows have no lifecycle state" is not an answer an operator can act
        on. There are two very different causes and they need different fixes:

        - the person is in the timetable, but under a DIFFERENT course. Their
          label belongs to that other enrollment. It is deliberately not copied
          across: a student can be `completed` in Excel and `not_coming` in
          Python on the same day, and sharing the label would invent churn.
        - the person is in no tab at all — the timetable workbook simply does
          not cover them. That is a data-entry backlog, not a join failure.
        """
        matched = set(right[key].dropna().astype(str))
        unmatched = left[~left[key].astype(str).isin(matched)]
        if unmatched.empty:
            return {"unlabelled_master_rows": 0}
        people = set(right["person_id"].dropna().astype(str))
        known_person = unmatched["person_id"].astype(str).isin(people)
        return {
            "unlabelled_master_rows": int(len(unmatched)),
            "person_in_roster_other_course": int(known_person.sum()),
            "person_absent_from_roster": int((~known_person).sum()),
            "people_absent_from_roster": int(
                unmatched.loc[~known_person, "person_id"].nunique()
            ),
        }

    def _join_admission_identity(
        self,
        left: pd.DataFrame,
        right: pd.DataFrame,
        left_roles: Mapping[str, str],
        right_roles: Mapping[str, str],
        right_name: str,
        relation: Optional[Mapping[str, Any]],
    ) -> Tuple[pd.DataFrame, JsonDict]:
        for role in ("student_mobile", "email", "name"):
            lcol, rcol = left_roles.get(role), right_roles.get(role)
            if lcol and rcol and lcol in left.columns and rcol in right.columns:
                if not left[lcol].dropna().duplicated().any() and not right[rcol].dropna().duplicated().any():
                    merged, detail = self._left_join_source(
                        left, right, lcol, rcol, right_name, "admission",
                        relation or {"confidence": "high", "keys": [role]},
                    )
                    detail["match_method"] = role
                    return merged, detail

        # Third field of the composite key. Branch used to sit here and must
        # not: the institute moves students between branches (and changes their
        # batch timing and faculty) mid-course. A mutable attribute in a join
        # key fails exactly on the students who moved, which is the population
        # a retention report most needs. An admission date is immutable, so it
        # is tried first; branch is the last resort and downgrades confidence.
        for third, method, confidence in (
            ("admission_date", "name_course_admission_date", "high"),
            ("joining_date", "name_course_joining_date", "high"),
            ("branch", "name_course_branch", "medium"),
        ):
            composite_roles = ("name", "course", third)
            if not all(left_roles.get(r) and right_roles.get(r) for r in composite_roles):
                continue
            left_key = right_key = "__admission_identity_key"
            left_c = left.copy()
            right_c = right.copy()
            left_c[left_key] = self._composite_key(left_c, [left_roles[r] for r in composite_roles])
            right_c[right_key] = self._composite_key(right_c, [right_roles[r] for r in composite_roles])
            if left_c[left_key].dropna().duplicated().any() or right_c[right_key].dropna().duplicated().any():
                continue
            merged, detail = self._left_join_source(
                left_c, right_c, left_key, right_key, right_name, "admission",
                relation or {"confidence": confidence, "keys": list(composite_roles)},
            )
            detail["match_method"] = method
            if third == "branch":
                detail["caveat"] = (
                    "matched on branch, which students change mid-course; any "
                    "row for a student who moved branch will have missed"
                )
            return merged.drop(columns=[left_key], errors="ignore"), detail

        return left, {
            "status": "rejected", "left_source": "master", "right_source": right_name,
            "reason": "no unique high-confidence admission identity match",
            "confidence": "low",
        }

    @staticmethod
    def _composite_key(df: pd.DataFrame, cols: Sequence[str]) -> pd.Series:
        parts = []
        for col in cols:
            parts.append(df[col].astype(str).str.strip().str.lower().fillna(""))
        out = parts[0]
        for part in parts[1:]:
            out = out + "|" + part
        return out.replace({"||": np.nan, "nan|nan|nan": np.nan})

    def _source_summary(
        self,
        packages: Sequence[JsonDict],
        frames: Mapping[str, pd.DataFrame],
        relationships: Mapping[str, Any],
    ) -> List[JsonDict]:
        joined = set(relationships.get("joined_sources") or [])
        summary = []
        for pkg in packages:
            name = pkg.get("source_name")
            frame = frames.get(name)
            summary.append({
                "name": name,
                "domain": pkg.get("source_domain"),
                "row_count": pkg.get("row_count"),
                "column_count": len(frame.columns) if frame is not None else 0,
                "join_status": "joined" if name in joined else "standalone",
                "canonical_df_path": pkg.get("canonical_df_path"),
            })
        return summary

    def _multi_source_summary(
        self, source_summary: Sequence[Mapping[str, Any]], relationships: Mapping[str, Any]
    ) -> JsonDict:
        domains = sorted({str(s.get("domain")) for s in source_summary if s.get("domain")})
        return {
            "source_count": len(source_summary),
            "domains": domains,
            "joined_count": len(relationships.get("joined_sources") or []),
            "unjoined_count": len(relationships.get("unjoined_sources") or []),
            "accepted_join_count": len(relationships.get("accepted") or []),
            "rejected_join_count": len(relationships.get("rejected") or []),
        }

    def _domain_metrics(
        self, packages: Sequence[JsonDict], frames: Mapping[str, pd.DataFrame]
    ) -> JsonDict:
        metrics: JsonDict = {}
        for pkg in packages:
            name = pkg.get("source_name")
            domain = str(pkg.get("source_domain") or "unknown")
            frame = frames.get(name)
            roles = pkg.get("canonical_columns") or {}
            if frame is None:
                continue
            bucket = metrics.setdefault(domain, {"sources": [], "metrics": {}})
            bucket["sources"].append(name)
            vals = bucket["metrics"]
            if domain == "finance":
                amount = roles.get("amount")
                pending = roles.get("pending")
                status = roles.get("status")
                if amount in frame:
                    vals["total_fees"] = vals.get("total_fees", 0) + float(frame[amount].sum(skipna=True))
                if pending in frame:
                    vals["pending_fees"] = vals.get("pending_fees", 0) + float(frame[pending].sum(skipna=True))
                    vals["full_paid_count"] = vals.get("full_paid_count", 0) + int((frame[pending].fillna(0) == 0).sum())
                elif status in frame:
                    vals["full_paid_count"] = vals.get("full_paid_count", 0) + int(
                        frame[status].astype(str).str.contains("full paid", case=False, na=False).sum()
                    )
            elif domain == "certificate":
                pending_col = "is_certificate_pending"
                if pending_col in frame:
                    pending_count = int(frame[pending_col].fillna(False).sum())
                    vals["certificate_pending"] = vals.get("certificate_pending", 0) + pending_count
                    vals["certificates_issued"] = vals.get("certificates_issued", 0) + int(len(frame) - pending_count)
            elif domain in ("student", "product"):
                vals["enrollment_count"] = vals.get("enrollment_count", 0) + int(len(frame))
            elif domain in ("admission", "marketing"):
                vals["lead_count"] = vals.get("lead_count", 0) + int(len(frame))
        return metrics

    @staticmethod
    def _field_names(roles: Mapping[str, str]) -> Dict[str, str]:
        """Source column -> the institute's canonical name for it.

        Only mapped roles appear; a discovered column keeps its own header,
        because inventing a canonical name for something we merely profiled
        would claim more understanding than we have.
        """
        return {column: CANONICAL_FIELD_NAMES[role]
                for role, column in roles.items()
                if role in CANONICAL_FIELD_NAMES and column}

    @staticmethod
    def _ledger_money_column(roles: Mapping[str, str]) -> Optional[str]:
        """What a receipt row actually paid — never the fee it was paid against.

        The receipt ledger carries BOTH `paid amt` (this transaction) and
        `Total Fees` (the enrollment's whole obligation, repeated on every one
        of that student's receipt rows). Summing the latter across rows
        overstates collections by the number of installments: on the sample,
        ₹1.73 crore against ₹71.3 lakh actually paid — 2.4x. `paid` wins, and
        `amount` is only a fallback for a ledger that has no paid column.
        """
        return roles.get("paid") or roles.get("amount")

    def _build_payment_reconciliation(
        self, packages: Sequence[JsonDict], frames: Mapping[str, pd.DataFrame]
    ) -> Optional[JsonDict]:
        """Per-enrollment payment reconciliation from finance sources.

        Detects sources by role shape, not name: a finance frame carrying a
        receipt role is the transaction LEDGER (many rows per enrollment); one
        carrying a pending role is the per-enrollment ROLLUP (total/pending).
        Emits nothing (None) when no ledger exists — no table is invented.

        Output parquet columns per enrollment (student-id grain):
          paid_sum, refund_sum, net_paid, n_installments,
          first/last_payment_date, payment_span_days, payment_channel,
          total_fees, pending  (when rollup present),
          recon_gap = total - net_paid - pending, recon_flag (|gap| > tol),
          negative_pending_flag.
        """
        ledger = rollup = None
        for pkg in packages:
            if str(pkg.get("source_domain")) != "finance":
                continue
            roles = pkg.get("canonical_columns") or {}
            frame = frames.get(pkg.get("source_name"))
            if frame is None or "student_id" not in roles:
                continue
            money_col = self._ledger_money_column(roles)
            has_receipt = "receipt_id" in roles or "receipt_date" in roles
            if ledger is None and has_receipt and money_col in frame.columns:
                ledger = (pkg, frame, roles)
            elif rollup is None and roles.get("pending") in frame.columns:
                rollup = (pkg, frame, roles)
        if ledger is None:
            return None

        _, ldf, lroles = ledger
        sid_col = lroles["student_id"]
        money_col = self._ledger_money_column(lroles)
        work = ldf[ldf[sid_col].notna()].copy()
        work["_sid"] = work[sid_col].astype(str).str.strip()
        work = work[work["_sid"] != ""]
        if work.empty:
            return None

        amounts = pd.to_numeric(work[money_col], errors="coerce").fillna(0.0)
        refund_mask = (
            work["is_refund_entry"].fillna(False).astype(bool)
            if "is_refund_entry" in work.columns
            else pd.Series(False, index=work.index)
        )
        work["_paid"] = amounts.where(~refund_mask, 0.0)
        work["_refund"] = amounts.where(refund_mask, 0.0)

        recon = work.groupby("_sid").agg(
            paid_sum=("_paid", "sum"),
            refund_sum=("_refund", "sum"),
            n_installments=("_paid", lambda s: int((s > 0).sum())),
        )
        recon["net_paid"] = recon["paid_sum"] - recon["refund_sum"]

        date_col = lroles.get("receipt_date")
        if date_col and date_col in work.columns and pd.api.types.is_datetime64_any_dtype(
            work[date_col]
        ):
            spans = work.groupby("_sid")[date_col].agg(["min", "max"])
            recon["first_payment_date"] = spans["min"]
            recon["last_payment_date"] = spans["max"]
            recon["payment_span_days"] = (spans["max"] - spans["min"]).dt.days

        if "payment_channel" in work.columns:
            recon["payment_channel"] = work.groupby("_sid")["payment_channel"].agg(
                lambda s: s.dropna().mode().iloc[0] if s.notna().any() else None
            )

        if rollup is not None:
            _, rdf, rroles = rollup
            rsid = rroles["student_id"]
            side = rdf[rdf[rsid].notna()].copy()
            side["_sid"] = side[rsid].astype(str).str.strip()
            side = side[side["_sid"] != ""].drop_duplicates("_sid")
            keep: Dict[str, str] = {}
            if rroles.get("amount") in side.columns:
                keep[rroles["amount"]] = "total_fees"
            if rroles.get("pending") in side.columns:
                keep[rroles["pending"]] = "pending"
            side = side.set_index("_sid")[list(keep)].rename(columns=keep)
            recon = recon.join(side, how="outer")
            if "pending" in recon.columns:
                recon["negative_pending_flag"] = recon["pending"] < 0
            if {"total_fees", "pending"} <= set(recon.columns):
                recon["recon_gap"] = (
                    recon["total_fees"].fillna(0)
                    - recon["net_paid"].fillna(0)
                    - recon["pending"].fillna(0)
                )
                recon["recon_flag"] = recon["recon_gap"].abs() > RECON_TOLERANCE

        # Default aging: bucket debtors (pending > 0) by days since last payment.
        # Only when both a pending balance and a last-payment date are available.
        if {"pending", "last_payment_date"} <= set(recon.columns):
            self._derive_default_aging(recon)

        recon = recon.reset_index().rename(columns={"_sid": "student_id"})
        path = self._write_parquet(recon, "payment_reconciliation.csv")

        summary: JsonDict = {
            "table_path": path,
            "enrollments": int(len(recon)),
            "paid_sum_total": float(recon["paid_sum"].sum(skipna=True)),
            "refund_sum_total": float(recon["refund_sum"].sum(skipna=True)),
            "avg_installments": float(recon["n_installments"].mean(skipna=True))
            if recon["n_installments"].notna().any()
            else 0.0,
        }
        if "payment_channel" in recon.columns:
            summary["channel_counts"] = (
                recon["payment_channel"].value_counts(dropna=True).to_dict()
            )
        if "recon_flag" in recon.columns:
            summary["recon_mismatch_count"] = int(recon["recon_flag"].fillna(False).sum())
        if "negative_pending_flag" in recon.columns:
            summary["negative_pending_count"] = int(
                recon["negative_pending_flag"].fillna(False).sum()
            )
        # Collection efficiency = rupees actually collected / rupees billed,
        # money-weighted across enrollments (not an average of per-student ratios).
        # Needs a rollup with total_fees; absent it, there is no billed denominator.
        if "total_fees" in recon.columns:
            billed = float(recon["total_fees"].sum(skipna=True))
            if billed > 0:
                collected = float(recon["net_paid"].fillna(0).clip(lower=0).sum())
                summary["collection_efficiency"] = round(collected / billed, 4)
                summary["total_billed"] = billed
        if "default_aging" in recon.columns:
            summary["default_aging_counts"] = (
                recon["default_aging"].value_counts(dropna=True).to_dict()
            )
            overdue = recon.loc[
                recon["default_aging"] == AGING_OVERDUE_BUCKET, "pending"
            ]
            summary["overdue_90plus_amount"] = float(overdue.sum(skipna=True))
        return summary

    def _build_enquiry_conversion(
        self, packages: Sequence[JsonDict], frames: Mapping[str, pd.DataFrame]
    ) -> Optional[JsonDict]:
        """Person-grain enquiry->admission conversion linked across sources.

        The link key is `person_id` (a salted hash of normalized name + last-10
        phone digits emitted per frame), so an enquiry recorded on one sheet and
        the admission recorded on another are matched by the same person even
        with no shared row id. A person is `enquired` if is_enquiry is set on any
        frame, `converted` if is_admitted is set on any frame. `cross_source`
        counts conversions whose admission came from a DIFFERENT source than the
        enquiry — the payoff of the phone link. Emits None when no frame carries
        both person_id and an is_enquiry flag (nothing to convert).
        """
        enquiry_source: Dict[str, str] = {}
        admitted_sources: Dict[str, set] = {}
        have_enquiry = False
        for pkg in packages:
            frame = frames.get(pkg.get("source_name"))
            if frame is None or "person_id" not in frame.columns:
                continue
            src = str(pkg.get("source_name"))
            pid = frame["person_id"].astype("string")
            valid = pid.notna() & (pid != "")
            if "is_enquiry" in frame.columns:
                have_enquiry = True
                mask = valid & frame["is_enquiry"].fillna(False).astype(bool)
                for p in pid[mask].dropna().unique():
                    enquiry_source.setdefault(str(p), src)
            if "is_admitted" in frame.columns:
                mask = valid & frame["is_admitted"].fillna(False).astype(bool)
                for p in pid[mask].dropna().unique():
                    admitted_sources.setdefault(str(p), set()).add(src)
        if not have_enquiry or not enquiry_source:
            return None

        rows = []
        cross = 0
        for pid, src in enquiry_source.items():
            adm = admitted_sources.get(pid, set())
            converted = bool(adm)
            cross_source = bool(adm - {src})
            cross += int(cross_source)
            rows.append({
                "person_id": pid,
                "enquiry_source": src,
                "converted": converted,
                "cross_source": cross_source,
            })
        conv = pd.DataFrame(rows)
        path = self._write_parquet(conv, "enquiry_conversion.csv")

        n_enq = int(len(conv))
        n_conv = int(conv["converted"].sum())
        return {
            "table_path": path,
            "enquired_persons": n_enq,
            "converted_persons": n_conv,
            "conversion_rate": round(n_conv / n_enq, 4) if n_enq else 0.0,
            "cross_source_conversions": cross,
        }

    def _derive_default_aging(self, recon: pd.DataFrame) -> None:
        """Tag each debtor (pending > 0) with a days-since-last-payment bucket.

        Reference date is the ledger's own latest payment (historical export, so
        wall-clock 'today' would overstate every age). Non-debtors (pending <= 0
        or no payment date) get no bucket — the column stays null for them.
        """
        pending = pd.to_numeric(recon["pending"], errors="coerce")
        last = recon["last_payment_date"]
        if not pd.api.types.is_datetime64_any_dtype(last) or last.notna().sum() == 0:
            return
        as_of = last.max()
        days = (as_of - last).dt.days
        debtor = (pending > 0) & days.notna()

        bucket = pd.Series(pd.NA, index=recon.index, dtype="object")
        prev = -1
        for upper, label in AGING_BUCKETS:
            in_band = debtor & (days > prev) & (days <= upper)
            bucket[in_band] = label
            prev = upper
        bucket[debtor & (days > prev)] = AGING_OVERDUE_BUCKET
        recon["default_aging"] = bucket

    def _merged_roles(self, packages: Sequence[JsonDict], merged: pd.DataFrame) -> Dict[str, str]:
        roles: Dict[str, str] = {}
        for pkg in packages:
            source = re.sub(r"\W+", "_", str(pkg.get("source_name"))).strip("_").lower()
            for role, col in (pkg.get("canonical_columns") or {}).items():
                candidates = [col, f"{source}__{col}"]
                for candidate in candidates:
                    if candidate in merged.columns and role not in roles:
                        roles[role] = candidate
        return roles

    def _infer_source_domain(self, source: Mapping[str, Any]) -> str:
        text = " ".join(
            str(source.get(k) or "") for k in ("name", "sheet_name", "path_or_query", "path")
        ).lower()
        if any(w in text for w in ("fee", "payment", "finance", "invoice")):
            return "finance"
        if "certificate" in text:
            return "certificate"
        if any(w in text for w in ("student", "master")):
            return "student"
        if any(w in text for w in ("admission", "lead", "enquiry", "marketing")):
            return "admission"
        return "unknown"

    # ---------------------------------------------------------------- output

    def _write_parquet(self, df: pd.DataFrame, csv_path: str) -> str:
        os.makedirs(self.output_dir, exist_ok=True)
        stem = re.sub(r"\W+", "_", os.path.splitext(os.path.basename(csv_path))[0])
        out_path = os.path.join(self.output_dir, f"{stem}_canonical.parquet")
        df.to_parquet(out_path, index=False)
        return out_path

    def _null_rates(self, df: pd.DataFrame) -> Dict[str, float]:
        if len(df) == 0:
            return {}
        return {col: round(float(df[col].isna().mean()), 4) for col in df.columns}

    # ------------------------------------------------------------- escalation

    def _blocked(
        self,
        reason: str,
        row_count: int,
        quality_extra: Optional[JsonDict] = None,
    ) -> JsonDict:
        quality: JsonDict = {"known_issues": [reason]}
        if quality_extra:
            quality.update(quality_extra)
        return {
            "status": "blocked",
            "canonical_df_path": "",
            "row_count": row_count,
            "schema": {},
            "quality_report": quality,
            "canonical_columns": {},
        }


if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) < 2:
        print("usage: python data_engineer_agent.py <source.csv>", file=sys.stderr)
        raise SystemExit(2)

    package = DataEngineerAgent().run(brief={}, csv_path=sys.argv[1])
    # Avoid dumping the dataframe; just the package metadata.
    print(json.dumps(package, indent=2, default=str))
