"""Agent 2.5: Dynamic Data Processor.

Detects, validates, and adapts to schema variations and source diversity at
runtime. Sits between Problem Definition (Agent 1) and Data Engineer (Agent 2).

Design goal: Ensure no surprises downstream. Schema mismatches, multi-source
joins, and capability downgrades are all resolved upfront, with transparent
logging for audit trails.

Responsibilities:
1. Source discovery & validation: accept CSV/JSON, detect schema, validate
   against a registry (required/optional/deprecated columns).
2. Schema adaptation: detect new columns, downgrade affected analysis if
   columns are removed, maintain version log.
3. Multi-source integration: detect joins, propose join strategies, build
   logical data model.
4. Data quality pre-flight: scan for encoding, delimiter, null, duplicate issues.
5. Backward compatibility: rewrite briefs to use only available columns.

Outputs:
- DataSourcePlan: which sources to load, join order, resulting row count.
- CapabilityReport: which questions fully answerable, partially answerable,
  must be skipped.
- adapted_brief: copy of problem_definition_brief with unavailable columns
  removed and alternate metrics substituted.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence

import pandas as pd


JsonDict = Dict[str, Any]

# Schema registry: per dataset, which columns are required/optional/deprecated.
# Expandable; this is a default baseline for the admissions use case.
DEFAULT_SCHEMA_REGISTRY: Dict[str, JsonDict] = {
    "admissions": {
        "required_columns": ["student_id", "admission_date", "course"],
        "optional_columns": [
            "counsellor",
            "lead_source",
            "city",
            "branch",
            "status",
            "enquiry_date",
        ],
        "deprecated_columns": [],
    },
    "fee_receipts": {
        "required_columns": ["student_id", "amount", "receipt_date"],
        "optional_columns": ["paid", "pending", "payment_mode", "status"],
        "deprecated_columns": [],
    },
    "students": {
        "required_columns": ["student_id"],
        "optional_columns": ["course", "batch", "enrollment_date", "status"],
        "deprecated_columns": [],
    },
    "counselling": {
        "required_columns": ["counsellor", "student_id"],
        "optional_columns": ["follow_up_count", "demo_attended", "lead_response_time"],
        "deprecated_columns": [],
    },
}

# Common join keys by entity.
JOIN_KEYS_BY_ENTITY = {
    "student": ["student_id"],
    "admission": ["student_id", "admission_date"],
    "receipt": ["student_id", "receipt_date"],
}

# Dimension roles that should be breakdowned (from AnalystAgent + EDAAgent).
DIMENSION_ROLES = (
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
    "counsellor",
)


class DynamicDataProcessorAgent:
    """Detects schema variations, validates sources, proposes joins."""

    def __init__(
        self,
        schema_registry: Optional[Dict[str, JsonDict]] = None,
    ):
        """Initialize with an optional custom schema registry."""
        self.schema_registry = schema_registry or DEFAULT_SCHEMA_REGISTRY
        self.execution_log: List[str] = []

    def run(
        self,
        problem_definition_brief: Mapping[str, Any],
        data_sources: Sequence[Mapping[str, Any]],
    ) -> JsonDict:
        """Produce a DataSourcePlan and adapted_brief.

        Args:
            problem_definition_brief: From ProblemDefinitionAgent.
            data_sources: List of dicts with 'name', 'type', 'path_or_query', ...

        Returns:
            DataSourcePlan JSON with status, sources_validated, join_plan,
            capability_report, adapted_brief, schema_changes.
        """
        self.execution_log = []
        self._log(f"Starting Dynamic Data Processor run at {datetime.utcnow()}")

        # Step 1: Validate each source
        sources_validated = []
        source_map = {}  # name -> validated source info
        for source in data_sources:
            validated = self._validate_source(source, problem_definition_brief)
            sources_validated.append(validated)
            source_map[source.get("name")] = validated
            if validated.get("status") == "blocked":
                return self._blocked(
                    f"Source '{source.get('name')}' validation failed: "
                    f"{validated.get('error')}"
                )
        self._log(f"Validated {len(sources_validated)} source(s)")

        # Step 2: Detect joins
        join_plan = self._detect_joins(
            sources_validated, problem_definition_brief
        )
        self._log(f"Detected {len(join_plan)} join(s)")

        # Step 3: Check capability against available columns
        capability_report = self._build_capability_report(
            sources_validated, problem_definition_brief
        )
        self._log(f"Capability assessment: {len(capability_report)} question(s)")

        # Step 3.5: Generic-mode detection (graceful degrade).
        # If NO question's requested dimensions match any real column, the brief's
        # framing (institute) does not fit this sheet. Rather than hard-block, mark
        # the run "generic": downstream agents will analyze the columns that DO
        # exist (numeric measures by low-card dimensions over any date) instead of
        # demanding institute columns. Only do this when the sheet is actually
        # analyzable (>=1 numeric/amount column and >=1 categorical column).
        dataset_mode = "institute"
        any_dim_matched = any(q.get("available_dimensions") for q in capability_report)
        cat_cols, num_cols, date_cols = self._analyzable_columns(sources_validated)
        if (not any_dim_matched and capability_report
                and num_cols and (cat_cols or date_cols)):
            dataset_mode = "generic"
            for q in capability_report:
                q["answerable"] = "partial"
                q["dataset_mode"] = "generic"
                q["generic_dimensions"] = cat_cols[:6]
                q["generic_measures"] = num_cols[:6]
                q["suggested_fallback"] = (
                    "Sheet does not match the institute schema; analyzing "
                    f"{', '.join(num_cols[:3])} by {', '.join(cat_cols[:3]) or 'time'} "
                    "instead."
                )
            self._log(f"Generic mode: {len(num_cols)} measure(s), "
                      f"{len(cat_cols)} dimension(s), {len(date_cols)} date(s)")

        # Step 4: Adapt brief to available columns
        adapted_brief = self._adapt_brief(
            problem_definition_brief, sources_validated, capability_report
        )
        if dataset_mode == "generic":
            adapted_brief["dataset_mode"] = "generic"
            self._generalize_questions(
                adapted_brief, problem_definition_brief, num_cols, cat_cols, date_cols
            )
        self._log("Brief adapted")

        # Step 5: Detect schema changes
        schema_changes = self._detect_schema_changes(sources_validated)

        status = (
            "ready"
            if all(q["answerable"] in ["full", "partial"]
                   for q in capability_report)
            else (
                "partial"
                if any(q["answerable"] == "partial" for q in capability_report)
                else "blocked"
            )
        )

        return {
            "status": status,
            "dataset_mode": dataset_mode,
            "timestamp": datetime.utcnow().isoformat(),
            "sources_validated": sources_validated,
            "join_plan": join_plan,
            "capability_report": capability_report,
            "adapted_brief": adapted_brief,
            "schema_changes": schema_changes,
            "handoff_to_data_engineer": status != "blocked",
            "execution_log": self.execution_log,
        }

    def _analyzable_columns(self, sources: Sequence[JsonDict]):
        """Across all sources, the column names by inferred role: categoricals,
        numeric measures (amount), and dates. Used for generic-mode planning."""
        cats, nums, dates = [], [], []
        for src in sources:
            for col, role in (src.get("detected_roles") or {}).items():
                if role == "categorical":
                    cats.append(col)
                elif role == "amount":
                    nums.append(col)
                elif role == "date":
                    dates.append(col)
        return cats, nums, dates

    # --- Private: Source Validation ---

    def _validate_source(
        self, source: Mapping[str, Any], brief: Mapping[str, Any]
    ) -> JsonDict:
        """Validate one source: schema, data quality, etc."""
        name = source.get("name", "unnamed")
        source_type = source.get("type", "csv").lower()

        # Parse based on type
        if source_type == "csv":
            return self._validate_csv(name, source)
        elif source_type == "excel_sheet":
            return self._validate_excel_sheet(name, source)
        elif source_type == "json":
            return self._validate_json(name, source)
        else:
            return {
                "name": name,
                "status": "blocked",
                "error": f"Unsupported source type: {source_type}",
            }

    def _validate_csv(self, name: str, source: Mapping[str, Any]) -> JsonDict:
        """Validate a CSV source."""
        path = source.get("path_or_query")
        if not path:
            return {"name": name, "status": "blocked", "error": "No path provided"}

        try:
            df = pd.read_csv(path, nrows=100)  # Sample first 100 rows
            schema = self._infer_schema(df)
            return {
                "name": name,
                "type": "csv",
                "domain": source.get("domain") or self._infer_domain(name, list(df.columns)),
                "path_or_query": path,
                "status": "valid",
                "row_count": len(df),
                "column_count": len(df.columns),
                "columns": list(df.columns),
                "detected_roles": schema,
                "missing_columns": self._check_missing_columns(
                    name, list(df.columns)
                ),
                "extra_columns": self._check_extra_columns(
                    name, list(df.columns)
                ),
                "data_quality": {
                    "encoding": source.get("encoding", "utf-8"),
                    "delimiter": ",",
                    "null_pattern": (df.isnull().sum() / len(df)).to_dict(),
                    "duplicate_keys": self._scan_duplicates(df, name),
                },
            }
        except Exception as e:
            return {
                "name": name,
                "status": "blocked",
                "error": f"CSV read error: {str(e)}",
            }

    def _validate_excel_sheet(self, name: str, source: Mapping[str, Any]) -> JsonDict:
        """Validate one Excel worksheet source."""
        path = source.get("path_or_query")
        sheet = source.get("sheet_name") or name
        if not path:
            return {"name": name, "status": "blocked", "error": "No workbook path provided"}
        try:
            df = pd.read_excel(path, sheet_name=sheet, nrows=100, engine="openpyxl")
            if df.empty:
                return {"name": name, "status": "blocked", "error": "Sheet is empty"}
            schema = self._infer_schema(df)
            return {
                "name": name,
                "type": "excel_sheet",
                "domain": source.get("domain") or self._infer_domain(name, list(df.columns)),
                "path_or_query": path,
                "sheet_name": sheet,
                "status": "valid",
                "row_count": len(df),
                "column_count": len(df.columns),
                "columns": list(df.columns),
                "detected_roles": schema,
                "missing_columns": self._check_missing_columns(name, list(df.columns)),
                "extra_columns": self._check_extra_columns(name, list(df.columns)),
                "data_quality": {
                    "null_pattern": (df.isnull().sum() / max(len(df), 1)).to_dict(),
                    "duplicate_keys": self._scan_duplicates(df, name),
                },
            }
        except Exception as e:
            return {
                "name": name,
                "status": "blocked",
                "error": f"Excel sheet read error: {str(e)}",
            }

    def _validate_json(self, name: str, source: Mapping[str, Any]) -> JsonDict:
        """Validate a JSON source (stub for MVP)."""
        path = source.get("path_or_query")
        if not path:
            return {"name": name, "status": "blocked", "error": "No path provided"}

        try:
            with open(path) as f:
                data = json.load(f)
            # Normalize list of objects to DataFrame
            if isinstance(data, list):
                df = pd.DataFrame(data)
            else:
                df = pd.DataFrame([data])

            schema = self._infer_schema(df)
            return {
                "name": name,
                "type": "json",
                "domain": source.get("domain") or self._infer_domain(name, list(df.columns)),
                "path_or_query": path,
                "status": "valid",
                "row_count": len(df),
                "column_count": len(df.columns),
                "columns": list(df.columns),
                "detected_roles": schema,
                "missing_columns": self._check_missing_columns(
                    name, list(df.columns)
                ),
                "extra_columns": self._check_extra_columns(
                    name, list(df.columns)
                ),
                "data_quality": {
                    "null_pattern": (df.isnull().sum() / len(df)).to_dict(),
                    "duplicate_keys": self._scan_duplicates(df, name),
                },
            }
        except Exception as e:
            return {
                "name": name,
                "status": "blocked",
                "error": f"JSON read error: {str(e)}",
            }

    def _infer_schema(self, df: pd.DataFrame) -> Dict[str, str]:
        """Infer a generic role for each column from its NAME and its VALUES.

        Roles: date | id | amount (numeric) | categorical | unknown. Value-based
        (dtype + cardinality) so it works on arbitrary, non-institute sheets — not
        just keyword-recognizable headers."""
        schema = {}
        n = max(len(df), 1)
        for col in df.columns:
            col_lower = str(col).lower()
            s = df[col]

            # 1. Date — by name, or a parseable object/datetime column.
            if any(x in col_lower for x in
                   ["date", "timestamp", "admission", "enquiry", "joined", "birth"]):
                schema[col] = "date"
                continue
            if pd.api.types.is_datetime64_any_dtype(s):
                schema[col] = "date"
                continue

            # 2. Numeric dtype -> id (unique integer key) or amount (measure).
            if pd.api.types.is_numeric_dtype(s):
                nun = int(s.nunique(dropna=True))
                looks_id = ("id" in col_lower or "code" in col_lower or "number" in col_lower)
                all_int = bool(pd.api.types.is_integer_dtype(s)) or (
                    s.dropna().mod(1).eq(0).all() if len(s.dropna()) else False)
                if looks_id or (all_int and nun >= max(20, int(0.95 * n))):
                    schema[col] = "id"
                else:
                    schema[col] = "amount"
                continue

            # 3. Object/category -> low-cardinality is a dimension, else a free-text id.
            nun = int(s.nunique(dropna=True))
            if nun <= 50 and (nun / n) < 0.5 and nun >= 1:
                schema[col] = "categorical"
            elif any(x in col_lower for x in
                     ["branch", "course", "faculty", "source", "status", "category",
                      "region", "channel", "segment", "type", "group"]):
                schema[col] = "categorical"
            else:
                schema[col] = "id"  # high-cardinality text (names, free text)
        return schema

    def _check_missing_columns(
        self, source_name: str, actual_columns: List[str]
    ) -> List[str]:
        """Check for required columns that are missing."""
        registry_entry = self.schema_registry.get(source_name)
        if not registry_entry:
            return []

        required = registry_entry.get("required_columns", [])
        actual_lower = [c.lower() for c in actual_columns]
        missing = [
            col for col in required if col.lower() not in actual_lower
        ]
        return missing

    def _check_extra_columns(
        self, source_name: str, actual_columns: List[str]
    ) -> List[str]:
        """Check for new columns not in registry (potential schema drift)."""
        registry_entry = self.schema_registry.get(source_name)
        if not registry_entry:
            return actual_columns

        required = registry_entry.get("required_columns", [])
        optional = registry_entry.get("optional_columns", [])
        registered = set(
            [c.lower() for c in required + optional]
        )
        actual_lower = [c.lower() for c in actual_columns]
        extra = [
            col
            for col in actual_columns
            if col.lower() not in registered
        ]
        return extra

    def _scan_duplicates(self, df: pd.DataFrame, source_name: str) -> Dict:
        """Scan for duplicate keys."""
        duplicates = {}
        for col in ["student_id", "id", "receipt_id"]:
            if col in df.columns:
                dup_count = df[col].duplicated().sum()
                if dup_count > 0:
                    duplicates[col] = dup_count
        return duplicates

    # --- Private: Join Detection ---

    def _detect_joins(
        self,
        sources: Sequence[JsonDict],
        brief: Mapping[str, Any],
    ) -> List[JsonDict]:
        """Propose conservative join strategies across multiple sources."""
        if len(sources) <= 1:
            return []

        joins = []
        master = self._choose_master_source(sources)
        for src in sources:
            if src.get("name") == master.get("name"):
                continue
            candidate = self._relationship_candidate(master, src)
            if candidate:
                joins.append(candidate)

        return joins

    def _find_common_keys(self, src1: JsonDict, src2: JsonDict) -> List[str]:
        """Find common keys between two sources."""
        common = []
        left = {self._norm_col(c): c for c in src1.get("columns", [])}
        right = {self._norm_col(c): c for c in src2.get("columns", [])}
        for alias in ("student_id", "studentid", "student"):
            if alias in left and alias in right:
                common.append("student_id")
                break
        return common

    def _choose_master_source(self, sources: Sequence[JsonDict]) -> JsonDict:
        def score(src):
            cols = {self._norm_col(c) for c in src.get("columns", [])}
            domain = str(src.get("domain") or "")
            value = 0
            if "student_id" in cols or "studentid" in cols:
                value += 100
            if domain in ("student", "master", "student_master"):
                value += 50
            if domain in ("finance", "certificate"):
                value -= 20
            if domain.startswith("admission"):
                value -= 10
            return value
        return max(sources, key=score)

    def _relationship_candidate(self, left: JsonDict, right: JsonDict) -> Optional[JsonDict]:
        left_cols = {self._norm_col(c): c for c in left.get("columns", [])}
        right_cols = {self._norm_col(c): c for c in right.get("columns", [])}
        left_name, right_name = left.get("name"), right.get("name")
        for key in ("student_id", "studentid"):
            if key in left_cols and key in right_cols:
                return {
                    "left_source": left_name,
                    "right_source": right_name,
                    "join_type": "left",
                    "keys": ["student_id"],
                    "left_key_column": left_cols[key],
                    "right_key_column": right_cols[key],
                    "confidence": "high",
                    "risk": "low",
                    "reason": "shared student-id key",
                    "expected_row_count": left.get("row_count"),
                }

        identity_keys = []
        for role, aliases in {
            "student_mobile": ("phone", "mobile", "mobilenostudent", "studentmobile"),
            "email": ("email", "emailaddress"),
            "name": ("name", "studentname"),
        }.items():
            if any(a in left_cols for a in aliases) and any(a in right_cols for a in aliases):
                identity_keys.append(role)
        if identity_keys and str(right.get("domain", "")).startswith("admission"):
            return {
                "left_source": left_name,
                "right_source": right_name,
                "join_type": "left",
                "keys": identity_keys,
                "confidence": "medium",
                "risk": "medium",
                "reason": "admission identity match candidate",
                "expected_row_count": left.get("row_count"),
            }
        return {
            "left_source": left_name,
            "right_source": right_name,
            "join_type": "none",
            "keys": [],
            "confidence": "low",
            "risk": "high",
            "reason": "no shared high-confidence key",
            "expected_row_count": left.get("row_count"),
        }

    # --- Private: Capability Assessment ---

    def _build_capability_report(
        self, sources: Sequence[JsonDict], brief: Mapping[str, Any]
    ) -> List[JsonDict]:
        """Assess which questions are answerable with available columns."""
        report = []
        all_available_columns = set()
        for src in sources:
            all_available_columns.update(
                src.get("detected_roles", {}).keys()
            )

        for question in brief.get("business_questions", []):
            q_id = question.get("question_id")
            q_text = question.get("question")
            dimensions = question.get("dimensions", [])
            metrics = question.get("metrics", [])

            # Check if dimensions are available
            available_dims = [
                d for d in dimensions if d.lower() in
                [c.lower() for c in all_available_columns]
            ]
            missing_dims = [d for d in dimensions if d not in available_dims]

            answerable = (
                "full"
                if not missing_dims
                else "partial" if available_dims else "no"
            )

            report.append(
                {
                    "question_id": q_id,
                    "question": q_text,
                    "answerable": answerable,
                    "available_dimensions": available_dims,
                    "missing_dimensions": missing_dims,
                    "suggested_fallback": (
                        None
                        if not missing_dims
                        else self._suggest_fallback(q_text, available_dims)
                    ),
                }
            )

        return report

    def _suggest_fallback(self, question: str, available_dims: List[str]) -> Optional[str]:
        """Suggest an alternate question if primary dimensions are missing."""
        if not available_dims:
            return None
        return f"Show by {available_dims[0]} instead (other dimensions unavailable)"

    # --- Private: Brief Adaptation ---

    def _adapt_brief(
        self,
        brief: Mapping[str, Any],
        sources: Sequence[JsonDict],
        capability_report: List[JsonDict],
    ) -> JsonDict:
        """Rewrite brief to use only available columns."""
        adapted = dict(brief)

        # Remove questions that are not answerable
        adapted["business_questions"] = [
            q
            for q, cap in zip(
                brief.get("business_questions", []), capability_report
            )
            if cap["answerable"] != "no"
        ]

        # Remove unavailable dimensions from each question
        for q in adapted["business_questions"]:
            dims = q.get("dimensions", [])
            all_available = set()
            for src in sources:
                all_available.update(
                    src.get("detected_roles", {}).keys()
                )
            q["dimensions"] = [
                d for d in dims
                if d.lower() in [c.lower() for c in all_available]
            ]

        return adapted

    def _generalize_questions(
        self,
        adapted: JsonDict,
        brief: Mapping[str, Any],
        num_cols: List[str],
        cat_cols: List[str],
        date_cols: List[str],
    ) -> None:
        """In generic mode the canned institute question texts don't describe
        this sheet. Rewrite them from the measures/dimensions that actually
        exist; the user's own free-text question is kept verbatim."""
        user_text = str(
            (brief.get("problem_statement") or {}).get("raw_business_problem") or ""
        ).strip().lower()

        def label(col: str) -> str:
            return str(col).replace("_", " ").strip()

        measures = [label(c) for c in num_cols[:2]] or ["record volume"]
        dims = [label(c) for c in cat_cols[:2]]

        templates = [
            f"What is the overall level and trend of {measures[0]}?"
            if date_cols else f"What is the overall level of {measures[0]}?"
        ]
        if dims:
            templates.append(
                f"Which {dims[0]} segments have the highest and lowest {measures[0]}?"
            )
        if len(dims) > 1:
            templates.append(
                f"How does {measures[-1]} vary across {dims[1]}?"
            )

        i = 0
        for q in adapted.get("business_questions") or []:
            text = str(q.get("question") or "").strip()
            if user_text and text.lower() == user_text:
                continue
            q["question"] = templates[i % len(templates)]
            q["decision_supported"] = (
                f"Understand {measures[0]} performance across the available segments"
            )
            i += 1
        self._log(f"Generic mode: rewrote {i} canned question(s) from sheet columns")

    # --- Private: Schema Change Detection ---

    def _detect_schema_changes(self, sources: Sequence[JsonDict]) -> JsonDict:
        """Track schema changes: new columns, deprecated columns."""
        new_columns = []
        deprecated_present = []

        for src in sources:
            name = src.get("name")
            extra = src.get("extra_columns", [])
            new_columns.extend(extra)

            registry_entry = self.schema_registry.get(name, {})
            deprecated = registry_entry.get("deprecated_columns", [])
            for col in deprecated:
                if col in src.get("detected_roles", {}):
                    deprecated_present.append(col)

        return {
            "new_columns": new_columns,
            "deprecated_columns_present": deprecated_present,
            "schema_version": "2.1",
        }

    # --- Private: Helpers ---

    def _infer_domain(self, name: str, columns: Sequence[str]) -> str:
        text = " ".join([str(name)] + [str(c) for c in columns]).lower()
        rules = [
            ("finance", ("fee", "fees", "payment", "paid", "pending", "amount",
                         "invoice", "revenue", "collection")),
            ("certificate", ("certificate", "issue date", "certificate number")),
            ("student", ("student-id", "student id", "phone", "secondary contact",
                         "date of joining", "mode")),
            ("admission", ("admission", "preferred branch", "receipt id",
                           "from where", "which course")),
            ("marketing", ("campaign", "lead source", "channel", "source")),
            ("operations", ("faculty", "batch", "branch", "status")),
        ]
        for domain, words in rules:
            if any(word in text for word in words):
                return domain
        return "unknown"

    @staticmethod
    def _norm_col(col: Any) -> str:
        return re.sub(r"[^a-z0-9]+", "", str(col).strip().lower().replace("-", "_"))

    def _log(self, message: str):
        """Log a message."""
        self.execution_log.append(
            f"[{datetime.utcnow().isoformat()}] {message}"
        )

    def _blocked(self, message: str) -> JsonDict:
        """Return a blocked DataSourcePlan."""
        return {
            "status": "blocked",
            "error": message,
            "timestamp": datetime.utcnow().isoformat(),
            "execution_log": self.execution_log,
        }
