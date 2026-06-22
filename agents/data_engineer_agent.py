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
from typing import Any, Dict, List, Mapping, Optional

import numpy as np
import pandas as pd


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

        original_rows = len(raw)
        if original_rows == 0:
            return self._blocked("Source CSV has zero rows.", row_count=0)

        known_issues: List[str] = []
        df = raw.copy()

        df = self._drop_ref_columns(df, known_issues)
        df = self._prefer_cleaned_columns(df, known_issues)

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
        df = self._normalize_pincode(df, roles, known_issues)
        df = self._compute_lead_to_admission_days(df, roles, known_issues)

        multivalue_columns: Dict[str, str] = {}
        if split_multivalue:
            multivalue_columns = self._split_multivalue(df, roles, known_issues)

        # Coalesce all date roles into one per-row event_date, then derive the
        # report time columns and drop only rows with no date at all.
        event_date = self._build_event_date(df, roles)
        if event_date is not None:
            df["event_date"] = event_date
        time_columns = self._derive_time_dimensions(df)

        df, dropped_reasons = self._drop_invalid_rows(df, roles)
        drop_count = original_rows - len(df)

        if original_rows and drop_count / original_rows > MAX_DROP_FRACTION:
            return self._blocked(
                f"Row count dropped {drop_count}/{original_rows} "
                f"(> {int(MAX_DROP_FRACTION * 100)}%) during cleaning.",
                row_count=len(df),
                quality_extra={"dropped_reasons": dropped_reasons},
            )

        # Derive status flags from the raw name BEFORE PII masking, otherwise
        # markers like "(cancelled)" are lost once the name is hashed.
        self._derive_status_flags(df, roles, known_issues)

        canonical_columns = self._mask_pii(df, roles, known_issues)
        dedup_keys = self._dedupe(df, roles, known_issues)

        canonical_path = self._write_parquet(df, csv_path)

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
            bad = int(parsed.isna().sum())
            df[col] = parsed
            if mixed:
                issues.append(
                    f"{role} '{col}': mixed M/D/Y and D/M/Y formats detected; "
                    "parsed each value by its unambiguous field, ambiguous values "
                    f"used the column-majority order ({mixed})"
                )
            if bad:
                issues.append(f"{role} '{col}': {bad} unparseable date(s) set to NaT")
        return df

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
            df[col] = numeric.clip(lower=0)  # refunds/negatives -> 0
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

    def _derive_status_flags(
        self, df: pd.DataFrame, roles: Mapping[str, str], issues: List[str]
    ) -> None:
        """Derive boolean funnel/lifecycle flags the downstream agents need.

        Runs before PII masking so the raw name is still readable for the
        cancellation marker. All flags are *conditional*: each is added only when
        its backing column exists, so no data is invented (Agent 5 visualizes
        only what is present). Flags added when data supports them:

          is_cancelled  - "(cancelled)"/"left"/"dropped" marker in the name.
          is_enquiry    - True for every retained row (each row is >= an enquiry).
          is_admitted   - admission/joining date present.
          is_fee_paid   - paid/amount > 0.
          certificate_delay_days / is_certificate_pending - from issue_date.
        """
        self._derive_cancellation_flag(df, roles, issues)
        self._derive_funnel_flags(df, roles, issues)
        self._derive_certificate_flags(df, roles, issues)

    def _derive_cancellation_flag(
        self, df: pd.DataFrame, roles: Mapping[str, str], issues: List[str]
    ) -> None:
        """Extract `is_cancelled` from the raw name, then strip the marker.

        Real admission/student sheets tag cancellations inline, e.g.
        "Patel Sai Ashokbhai (cancelled)". The name column is cleaned of the
        marker so its later hash is not polluted by it.
        """
        col = roles.get("name")
        if col is None or col not in df.columns:
            return

        as_str = df[col].astype("string")
        flag = as_str.str.contains(CANCEL_MARKER_RE, na=False)
        if not bool(flag.any()):
            return

        df["is_cancelled"] = flag.fillna(False).astype(bool)
        # Strip marker + collapse leftover whitespace so the hashed name is clean.
        cleaned = as_str.str.replace(CANCEL_MARKER_RE, "", regex=True)
        cleaned = cleaned.str.replace(r"\s+", " ", regex=True).str.strip()
        df[col] = cleaned
        issues.append(
            f"Derived is_cancelled for {int(flag.sum())} row(s) from name markers"
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
