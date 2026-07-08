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

from . import canonical_maps


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
    "mode": {"include": ["mode"], "exclude": ["payment"]},
    # --- contact / PII ---
    "email": {"include": ["email", "e-mail"], "exclude": []},
    "parent_mobile": {
        "include": [
            "mobile no (parent",
            "mobile no (father",
            "mobile no (mother",
            "parent",
            "guardian",
            "secondary contact",
        ],
        "exclude": [],
    },
    "student_mobile": {
        "include": ["mobile no (student", "mobile", "phone", "contact no", "whatsapp"],
        "exclude": [],
    },
    "address": {
        "include": ["address", "residential", "street", "addr"],
        "exclude": [],
    },
    "pincode": {"include": ["pincode", "pin code", "zip", "postal"], "exclude": []},
    "photo": {"include": ["photo", "image"], "exclude": []},
    # --- name (exclude Google Contacts helper column + course/tutor names) ---
    "name": {
        "include": ["student name", "name of student", "name"],
        "exclude": ["google", "course", "tutor", "faculty", "branch", "category"],
    },
    # --- categoricals ---
    "branch": {
        "include": ["branch", "centre", "center", "preferred branch", "region", "office"],
        "exclude": [],
    },
    "source": {
        "include": ["from where", "source", "channel", "referral", "utm", "how did"],
        "exclude": [],
    },
    "faculty": {
        "include": ["faculty", "tutor", "trainer", "counsellor", "assigned to", "agent"],
        "exclude": [],
    },
    "education": {"include": ["education level", "education", "qualification"], "exclude": ["details"]},
    "occupation": {"include": ["presently what", "occupation", "currently doing"], "exclude": []},
    "batch_time": {"include": ["batch timing", "batch time", "slot", "shift"], "exclude": []},
    "preferred_days": {"include": ["preferred days", "days"], "exclude": ["remain"]},
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
# Main_data -> active. Matched against the source name (substring, lowered).
COMPLETION_BY_SOURCE = (
    ("complete", "completed"),
    ("not_coming", "not_coming"),
    ("not coming", "not_coming"),
    ("main_data", "active"),
    ("time_table", "active"),
    ("timetable", "active"),
)

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


class DataEngineerAgent:
    """Cleans a source CSV into a canonical dataframe + quality report."""

    def __init__(self, output_dir: Optional[str] = None, salt: str = "fv-institute") -> None:
        """Create the agent.

        Args:
            output_dir: Directory for the canonical parquet. Defaults to the
                system temp dir.
            salt: Salt mixed into PII hashing so masked IDs are not trivially
                reversible across datasets.
        """

        self.output_dir = output_dir or os.path.join(os.path.sep, "tmp")
        self.salt = salt

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

        merged, relationships, join_issues = self._build_joined_frame(
            frames, ready, join_plan or []
        )
        issues.extend(join_issues)
        stem = "multi_source"
        if data_sources:
            first_path = data_sources[0].get("path_or_query") or data_sources[0].get("name") or stem
            stem = os.path.splitext(os.path.basename(str(first_path)))[0] or stem
        canonical_path = self._write_parquet(merged, f"{stem}_joined.csv")

        source_summary = self._source_summary(ready, frames, relationships)
        domain_metrics = self._domain_metrics(ready, frames)
        payment_reconciliation = self._build_payment_reconciliation(ready, frames)
        canonical_columns = self._merged_roles(ready, merged)
        return {
            "status": "ready",
            "canonical_df_path": canonical_path,
            "row_count": len(merged),
            "schema": {col: str(dtype) for col, dtype in merged.dtypes.items()},
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

        try:
            raw = pd.read_csv(csv_path)
        except Exception as exc:  # noqa: BLE001 - surface any parse failure to orchestrator
            return self._blocked(f"Failed to read CSV: {exc}", row_count=0)

        return self._clean_raw_frame(
            brief, raw, csv_path, date_format=date_format,
            split_multivalue=split_multivalue,
            source_name="primary", source_domain="single",
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
    ) -> JsonDict:
        """Clean an already-loaded dataframe using the legacy single-source flow."""

        original_rows = len(raw)
        if original_rows == 0:
            return self._blocked("Source CSV has zero rows.", row_count=0)

        known_issues: List[str] = []
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
        if not roles:
            return self._blocked(
                "Canonical column mapping failed: no recognizable roles in CSV.",
                row_count=original_rows,
            )

        df = self._parse_dates(df, roles, known_issues, date_format)
        df = self._normalize_money(df, roles, known_issues)
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

        canonical_columns = self._mask_pii(df, roles, known_issues)
        dedup_keys = self._dedupe(df, roles, known_issues)

        canonical_path = self._write_parquet(df, source_path)

        return {
            "status": "ready",
            "canonical_df_path": canonical_path,
            "row_count": len(df),
            "schema": {col: str(dtype) for col, dtype in df.dtypes.items()},
            "quality_report": {
                "original_row_count": original_rows,
                "drop_count": drop_count,
                "dropped_reasons": dropped_reasons,
                "null_rates": self._null_rates(df),
                "deduplication_keys": dedup_keys,
                "known_issues": known_issues,
            },
            "canonical_columns": canonical_columns,
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
                return

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
                if any(bad in header for bad in exclude):
                    continue
                if any(kw in header for kw in include):
                    roles[role] = col
                    taken.add(col)
                    break
        return roles

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
        """
        reasons: Dict[str, int] = {}
        if "event_date" in df.columns:
            before = len(df)
            df = df[df["event_date"].notna()]
            removed = before - len(df)
            if removed:
                reasons["missing_primary_date"] = removed
        return df, reasons

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

    # --------------------------------------------------------------- dedupe

    def _dedupe(
        self, df: pd.DataFrame, roles: Mapping[str, str], issues: List[str]
    ) -> List[str]:
        """Drop exact duplicates on a natural key when one is inferable.

        Prefer a unique id (receipt_id / student_id). Otherwise fall back to a
        composite of person + contact + primary date.
        """
        if "receipt_id" in roles:
            key_cols = [roles["receipt_id"]]
        elif "certificate_number" in roles:
            key_cols = [roles["certificate_number"]]
        elif (
            "student_id" in roles
            and "source_domain" in df.columns
            and set(df["source_domain"].dropna().astype(str)).issubset({"finance"})
        ):
            return []
        else:
            key_cols = [
                roles[r]
                for r in ("student_id", "name", "student_mobile", "enquiry_date", "joining_date")
                if r in roles
            ]
        if not key_cols:
            return []
        before = len(df)
        df.drop_duplicates(subset=key_cols, keep="first", inplace=True)
        removed = before - len(df)
        if removed:
            issues.append(f"Removed {removed} duplicate row(s) on {key_cols}")
        return key_cols

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
            else:
                rejected.append(detail)
                issues.append(
                    f"Source '{name}' not joined: {detail.get('reason', 'unsafe join')}"
                )

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

        composite_roles = ("name", "course", "branch")
        if all(left_roles.get(r) and right_roles.get(r) for r in composite_roles):
            left_key = "__admission_identity_key"
            right_key = "__admission_identity_key"
            left = left.copy()
            right = right.copy()
            left[left_key] = self._composite_key(left, [left_roles[r] for r in composite_roles])
            right[right_key] = self._composite_key(right, [right_roles[r] for r in composite_roles])
            if not left[left_key].dropna().duplicated().any() and not right[right_key].dropna().duplicated().any():
                merged, detail = self._left_join_source(
                    left, right, left_key, right_key, right_name, "admission",
                    relation or {"confidence": "high", "keys": list(composite_roles)},
                )
                detail["match_method"] = "name_course_branch"
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
            money_col = roles.get("amount") or roles.get("paid")
            has_receipt = "receipt_id" in roles or "receipt_date" in roles
            if ledger is None and has_receipt and money_col in frame.columns:
                ledger = (pkg, frame, roles)
            elif rollup is None and roles.get("pending") in frame.columns:
                rollup = (pkg, frame, roles)
        if ledger is None:
            return None

        _, ldf, lroles = ledger
        sid_col = lroles["student_id"]
        money_col = lroles.get("amount") or lroles.get("paid")
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
        return summary

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
