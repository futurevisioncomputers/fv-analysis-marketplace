"""Agent 1: Problem Definition & Business Question.

This agent is deterministic by default. It accepts an institute goal payload,
normalizes it, and returns a structured ProblemDefinitionBrief for downstream
data preparation and analysis agents.

When an ANTHROPIC_API_KEY is configured it additionally consults the LLM at two
scoping decisions — mapping a free-text request to the fixed module set, and
phrasing the decision-to-support — but the LLM output is always validated against
the deterministic constraints (module enum) and any failure silently falls back
to the keyword logic. The pipeline never depends on the LLM.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from . import llm_client


JsonDict = Dict[str, Any]


MODULE_DEFINITIONS: Dict[str, JsonDict] = {
    "admissions": {
        "metrics": [
            "total_leads",
            "qualified_leads",
            "applications_received",
            "admissions_confirmed",
            "admission_conversion_rate",
            "walk_in_count",
            "counselling_to_admission_rate",
        ],
        "dimensions": ["branch", "course", "counsellor", "lead_source", "city", "campaign"],
        "datasets": ["leads", "enquiries", "admissions"],
        "questions": [
            "How are total leads, qualified leads, applications, and confirmed admissions trending over the selected period?",
            "Which branch, course, counsellor, lead source, city, or campaign is driving admission growth or decline?",
            "Where is the funnel dropping from lead to counselling to admission?",
        ],
    },
    "counselling": {
        "metrics": [
            "counselling_sessions",
            "follow_up_count",
            "demo_attended",
            "lead_response_time",
            "conversion_rate_per_counsellor",
            "lost_leads",
        ],
        "dimensions": ["counsellor", "branch", "course", "lead_source"],
        "datasets": ["leads", "enquiries", "counselling"],
        "questions": [
            "Which counsellors have the highest and lowest counselling-to-admission conversion?",
            "Are follow-ups, demo attendance, or lead response time affecting conversion?",
            "Which lead sources or courses create the most lost leads?",
        ],
    },
    "fee_management": {
        "metrics": [
            "gross_fee_collected",
            "pending_fee",
            "overdue_fee",
            "installment_collection_rate",
            "average_fee_per_student",
            "discount_amount",
            "refund_amount",
        ],
        "dimensions": ["branch", "course", "payment_mode", "batch"],
        "datasets": ["students", "admissions", "fee_receipts"],
        "questions": [
            "How much fee is collected, pending, overdue, discounted, or refunded by branch, course, payment mode, and batch?",
            "Which segments are causing pending or overdue fee risk?",
            "Is installment collection on track against target?",
        ],
    },
    "courses": {
        "metrics": [
            "course_enrollment",
            "active_students",
            "completed_students",
            "dropouts",
            "course_completion_rate",
        ],
        "dimensions": ["course", "trainer", "batch", "branch"],
        "datasets": ["students", "courses", "batches"],
        "questions": [
            "Which courses, trainers, batches, or branches have high enrollment, completion, or dropout issues?",
            "How are active, completed, and dropout students trending?",
            "Where is course completion below target?",
        ],
    },
    "certificates": {
        "metrics": [
            "certificates_issued",
            "certificate_pending",
            "course_completion_count",
            "certificate_issue_time",
        ],
        "dimensions": ["course", "batch", "branch"],
        "datasets": ["students", "courses", "batches", "certificates"],
        "questions": [
            "How many certificates are issued or pending by course, batch, and branch?",
            "Where is certificate issue time increasing?",
            "Are completed students receiving certificates on time?",
        ],
    },
    "student_reviews": {
        "metrics": [
            "average_rating",
            "review_count",
            "positive_reviews",
            "negative_reviews",
            "nps_score",
        ],
        "dimensions": ["course", "trainer", "branch"],
        "datasets": ["students", "courses", "batches", "reviews"],
        "questions": [
            "How are ratings, review volume, negative reviews, and NPS trending?",
            "Which course, trainer, or branch has satisfaction risk?",
            "Do negative reviews correlate with dropouts, low completion, or poor admissions?",
        ],
    },
}


# Keyword -> metric, to steer a free-text question onto a specific metric. The
# matched metric is moved to position 0 so the Analyst (which picks metrics[0])
# computes what the user actually asked about. Order matters: more specific
# phrases first (checked top-down).
METRIC_KEYWORDS: List[tuple] = [
    ("admission_conversion_rate", ("conversion", "convert", "conversion rate")),
    ("counselling_to_admission_rate", ("counselling to admission", "counsel")),
    ("admissions_confirmed", ("admission", "admitted", "enrol", "enroll")),
    ("dropout_rate", ("dropout", "drop out", "drop-out", "churn")),
    ("completion_rate", ("completion", "completed", "finish")),
    ("pending_fee", ("pending fee", "overdue", "unpaid", "outstanding")),
    ("gross_fee_collected", ("fee collected", "revenue", "collection", "collected")),
    ("qualified_leads", ("qualified lead", "qualified")),
    ("total_leads", ("lead", "enquir", "inquir", "interest")),
]

# Keyword -> dimension, to set the breakdown the question asks for (moved to
# dimensions[0]).
DIMENSION_KEYWORDS: List[tuple] = [
    ("branch", ("branch", "vesu", "pal", "citylight", "location", "centre", "center")),
    ("course", ("course", "program", "class", "subject")),
    ("counsellor", ("counsellor", "counselor", "staff", "faculty")),
    ("lead_source", ("source", "channel", "google", "social", "referral", "campaign")),
    ("city", ("city", "area", "region")),
]


DEFAULT_ALERTS = {
    "low_admission_alert": True,
    "pending_fee_alert": True,
    "certificate_pending_alert": True,
    "negative_review_alert": True,
    "dropout_alert": True,
}

DEFAULT_KPI_TARGETS = {
    "admission_growth_percent": "",
    "fee_collection_percent": "",
    "course_completion_percent": "",
    "certificate_completion_percent": "",
    "student_satisfaction_score": "",
}

DEFAULT_DELIVERABLES = [
    "dashboard",
    "excel_report",
    "executive_summary",
    "automated_email_report",
]

DEFAULT_ANALYSIS_TYPES = [
    "descriptive",
    "diagnostic",
    "predictive",
    "prescriptive",
    "forecasting",
    "monitoring",
]

DEFAULT_RISKS = [
    "duplicate_leads",
    "missing_fee_records",
    "missing_certificate_status",
    "incomplete_review_data",
]

MINIMUM_COMMON_FIELDS = [
    "student_id",
    "lead_id",
    "course_id",
    "batch_id",
    "branch",
    "created_date",
    "status",
]

MODULE_KEYWORDS = {
    "admissions": ["admission", "lead", "application", "walk", "campaign"],
    "counselling": ["counsell", "follow", "demo", "response", "lost lead"],
    "fee_management": ["fee", "payment", "receipt", "pending", "overdue", "refund", "discount"],
    "courses": ["course", "trainer", "batch", "dropout", "completion", "active student"],
    "certificates": ["certificate", "issued", "pending certificate"],
    "student_reviews": ["review", "rating", "nps", "satisfaction", "negative"],
}

RISK_HYPOTHESES = {
    "duplicate_leads": {
        "hypothesis": "Duplicate leads may inflate lead volume and reduce apparent conversion rate",
        "test_plan": "Deduplicate leads by phone, email, name, and created date, then compare conversion rates before and after deduplication",
    },
    "missing_fee_records": {
        "hypothesis": "Missing fee records may understate collections and overstate pending fee",
        "test_plan": "Reconcile admissions and students against fee_receipts to identify students without expected fee records",
    },
    "missing_certificate_status": {
        "hypothesis": "Missing certificate status may understate certificate pending workload",
        "test_plan": "Compare completed students against certificate records by course, batch, and branch",
    },
    "incomplete_review_data": {
        "hypothesis": "Incomplete review data may bias satisfaction and NPS metrics",
        "test_plan": "Compare review coverage across course, trainer, branch, and completed-student counts",
    },
}

ALERT_HYPOTHESES = {
    "low_admission_alert": {
        "hypothesis": "Admissions may be below expected trend for one or more branches, courses, or lead sources",
        "test_plan": "Compare admissions_confirmed and admission_conversion_rate against prior periods and available targets",
    },
    "pending_fee_alert": {
        "hypothesis": "Pending or overdue fee may be concentrated in specific branches, courses, payment modes, or batches",
        "test_plan": "Break down pending_fee and overdue_fee by branch, course, payment_mode, and batch",
    },
    "certificate_pending_alert": {
        "hypothesis": "Certificate pending count may be driven by completed students without issued certificates",
        "test_plan": "Compare course_completion_count against certificates_issued and certificate_pending by course, batch, and branch",
    },
    "negative_review_alert": {
        "hypothesis": "Negative reviews may identify course, trainer, or branch satisfaction risk",
        "test_plan": "Analyze negative_reviews, average_rating, and nps_score by course, trainer, and branch",
    },
    "dropout_alert": {
        "hypothesis": "Dropouts may be concentrated in specific courses, trainers, batches, or branches",
        "test_plan": "Compare dropouts and course_completion_rate across course, trainer, batch, and branch",
    },
}


class ProblemDefinitionAgent:
    """Builds a ProblemDefinitionBrief for institute analytics."""

    def __init__(self, catalog: Optional[Any] = None) -> None:
        """Create the agent.

        Args:
            catalog: Optional read-only catalog connector. If supplied, it may
                expose either `list_datasets()` or `has_dataset(name)`.
        """

        self.catalog = catalog

    def run(self, payload: Any) -> JsonDict:
        """Normalize an incoming user request into a ProblemDefinitionBrief."""

        request = self._coerce_payload(payload)
        user_question = str(request.get("user_question") or "").strip()
        goal = request.get("goal") or {}
        if not isinstance(goal, Mapping):
            goal = {}

        # Hard clarifications block a `ready` status (analysis impossible without them).
        # Soft clarifications are documented but do not block `ready` (per spec
        # clarification policy: blank targets/success criteria are advisory).
        clarifying_questions: List[str] = []
        soft_clarifications: List[str] = []
        handoff_notes: List[str] = []

        conversation_history = request.get("conversation_history")
        if not isinstance(conversation_history, list):
            conversation_history = []

        project = self._build_project(goal)
        stakeholder = self._build_stakeholder(goal, clarifying_questions)
        analysis_request = self._build_analysis_request(goal, user_question, clarifying_questions)
        time_window = self._build_time_window(goal, clarifying_questions)
        comparison = self._build_comparison(goal)
        modules = self._build_modules(goal, user_question, clarifying_questions)
        enabled_modules = [name for name, config in modules.items() if config.get("enabled")]

        required_datasets = self._required_datasets(goal, enabled_modules)
        catalog_validation = self._validate_catalog(required_datasets)

        risks = self._clean_string_list(goal.get("risks")) or list(DEFAULT_RISKS)
        alerts = self._build_alerts(goal)
        targets = self._build_targets(goal, soft_clarifications)
        deliverables = self._clean_string_list(goal.get("deliverables")) or list(DEFAULT_DELIVERABLES)
        reporting_frequency = self._as_nonblank_string(goal.get("reporting_frequency")) or "monthly"
        out_of_scope = self._clean_string_list(goal.get("out_of_scope"))

        business_questions = self._build_business_questions(
            goal=goal,
            enabled_modules=enabled_modules,
            modules=modules,
            analysis_types=analysis_request["analysis_types"],
            priority=stakeholder["priority"],
            time_window=time_window,
            comparison=comparison,
            user_question=user_question,
        )

        hypotheses = self._build_hypotheses(goal, risks, alerts)

        success_criteria = self._clean_string_list(goal.get("success_criteria"))
        if not success_criteria:
            success_criteria = [
                "Prioritize the top issues by impact, risk, and decision urgency for every enabled module"
            ]
            soft_clarifications.append(
                "Confirm the success criteria for this analysis, such as target impact, top issue count, or reporting acceptance criteria."
            )

        if catalog_validation["status"] == "not_checked":
            handoff_notes.append(
                "No catalog connector was provided; Data Engineer should validate dataset availability."
            )
        elif catalog_validation["missing_datasets"]:
            handoff_notes.append(
                "Catalog check found missing datasets: "
                + ", ".join(catalog_validation["missing_datasets"])
            )
        else:
            handoff_notes.append("Catalog check found all required datasets available.")

        handoff_notes.extend(
            [
                "Data Engineer should validate dataset availability and minimum fields for every enabled module.",
                "Blank KPI targets should remain unresolved until the stakeholder provides target values.",
                "Monitoring Agent should use enabled alerts as candidate hooks after analysis confirms metric definitions.",
            ]
        )

        status = self._status(
            analysis_request=analysis_request,
            time_window=time_window,
            enabled_modules=enabled_modules,
            required_datasets=required_datasets,
            catalog_validation=catalog_validation,
            clarifying_questions=clarifying_questions,
        )

        return {
            "status": status,
            "project": project,
            "stakeholder": stakeholder,
            "problem_statement": {
                "raw_business_problem": analysis_request["raw_business_problem"],
                "normalized_problem": analysis_request["normalized_problem"],
                "decision_to_support": analysis_request["decision_to_support"],
                "analysis_types": analysis_request["analysis_types"],
            },
            "scope": {
                "time_window": time_window,
                "comparison": comparison,
                "enabled_modules": enabled_modules,
                "out_of_scope": out_of_scope,
            },
            "business_questions": business_questions,
            "kpi_framework": {
                "module_metrics": {
                    name: modules[name]["metrics"] for name in enabled_modules
                },
                "targets": targets,
                "alerts": alerts,
            },
            "data_requirements": {
                "required_datasets": required_datasets,
                "module_dataset_map": {
                    name: MODULE_DEFINITIONS[name]["datasets"] for name in enabled_modules
                },
                "minimum_common_fields": list(MINIMUM_COMMON_FIELDS),
                "catalog_validation": catalog_validation,
                "known_risks": risks,
            },
            "hypotheses": hypotheses,
            "success_criteria": success_criteria,
            "deliverables": deliverables,
            "reporting_frequency": reporting_frequency,
            "clarifying_questions": self._unique(clarifying_questions + soft_clarifications),
            "soft_clarifications": self._unique(soft_clarifications),
            "handoff_notes": self._unique(handoff_notes),
            "conversation_history": conversation_history,
        }

    def _coerce_payload(self, payload: Any) -> JsonDict:
        if isinstance(payload, str):
            text = payload.strip()
            if text.startswith("/goal"):
                first_brace = text.find("{")
                text = text[first_brace:] if first_brace >= 0 else "{}"
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return {"user_question": payload, "goal": {}}
            if isinstance(parsed, Mapping) and "goal" in parsed:
                return dict(parsed)
            if isinstance(parsed, Mapping):
                return {"user_question": "", "goal": dict(parsed)}
            return {"user_question": payload, "goal": {}}

        if isinstance(payload, Mapping):
            if "goal" in payload:
                return dict(payload)
            return {"user_question": str(payload.get("user_question") or ""), "goal": dict(payload)}

        return {"user_question": str(payload or ""), "goal": {}}

    def _build_project(self, goal: Mapping[str, Any]) -> JsonDict:
        project_id = self._as_nonblank_string(goal.get("project_id")) or "INST-2026-001"
        return {
            "project_id": project_id,
            "project_name": self._as_nonblank_string(goal.get("project_name")) or "",
            "business_type": "institute_management",
        }

    def _build_stakeholder(
        self, goal: Mapping[str, Any], clarifying_questions: List[str]
    ) -> JsonDict:
        stakeholder = goal.get("stakeholder") or {}
        if not isinstance(stakeholder, Mapping):
            stakeholder = {}

        department = self._as_nonblank_string(stakeholder.get("department")) or "leadership"
        requested_by = self._as_nonblank_string(stakeholder.get("requested_by")) or ""
        priority = self._as_nonblank_string(stakeholder.get("priority")) or "medium"

        if not requested_by:
            clarifying_questions.append("Who requested this analysis?")

        return {
            "department": self._normalize_choice(
                department,
                ["leadership", "admissions", "counselling", "accounts", "academics", "operations", "other"],
                "leadership",
            ),
            "requested_by": requested_by,
            "priority": self._normalize_choice(priority, ["low", "medium", "high", "critical"], "medium"),
        }

    def _build_analysis_request(
        self,
        goal: Mapping[str, Any],
        user_question: str,
        clarifying_questions: List[str],
    ) -> JsonDict:
        request = goal.get("analysis_request") or {}
        if not isinstance(request, Mapping):
            request = {}

        raw_problem = self._as_nonblank_string(request.get("business_problem"))
        if not raw_problem and user_question:
            raw_problem = user_question
        if not raw_problem:
            clarifying_questions.append("What business problem should this analysis solve?")

        analysis_types = self._clean_string_list(request.get("analysis_type"))
        if not analysis_types:
            analysis_types = self._infer_analysis_types(raw_problem or user_question)

        normalized_problem = (
            raw_problem
            or "Institute performance needs to be monitored and improved across enabled modules"
        )
        deterministic_decision = self._decision_from_problem(normalized_problem, analysis_types)
        decision = self._llm_or_default_decision(
            deterministic_decision, normalized_problem, analysis_types
        )

        return {
            "raw_business_problem": raw_problem or "",
            "normalized_problem": normalized_problem,
            "decision_to_support": decision,
            "analysis_types": analysis_types,
        }

    def _llm_or_default_decision(
        self, deterministic: str, normalized_problem: str, analysis_types: Sequence[str]
    ) -> str:
        """LLM-phrased one-line decision the analysis should support, grounded in
        the normalized problem and inferred analysis types. Falls back to the
        deterministic template on any failure."""
        if not normalized_problem or not llm_client.available():
            return deterministic
        try:
            prompt = (
                "An education institute's analytics request normalizes to:\n"
                f'"{normalized_problem}"\n'
                f"Analysis types inferred: {', '.join(analysis_types) or 'descriptive'}.\n\n"
                "In ONE sentence, state the leadership decision this analysis should "
                "support. Be concrete and specific to the request; do not invent "
                "metrics or numbers. Return only the sentence."
            )
            text = llm_client.complete_text(prompt, max_tokens=120, temperature=0.2)
            cleaned = text.strip().strip('"').strip()
            return cleaned if 10 <= len(cleaned) <= 240 else deterministic
        except llm_client.LLMUnavailable:
            return deterministic
        except Exception:  # noqa: BLE001 - phrasing must never break scoping
            return deterministic

    def _build_time_window(
        self, goal: Mapping[str, Any], clarifying_questions: List[str]
    ) -> JsonDict:
        window = goal.get("time_window") or {}
        if not isinstance(window, Mapping):
            window = {}

        start = self._as_nonblank_string(window.get("start_date"))
        end = self._as_nonblank_string(window.get("end_date"))
        granularity = self._as_nonblank_string(window.get("granularity")) or "month"

        if not start:
            clarifying_questions.append("What is the analysis start date?")
        elif not self._valid_date(start):
            clarifying_questions.append("Use YYYY-MM-DD format for time_window.start_date.")

        if not end:
            clarifying_questions.append("What is the analysis end date?")
        elif not self._valid_date(end):
            clarifying_questions.append("Use YYYY-MM-DD format for time_window.end_date.")

        return {
            "start_date": start or "",
            "end_date": end or "",
            "granularity": self._normalize_choice(
                granularity, ["day", "week", "month", "quarter", "year"], "month"
            ),
        }

    def _build_comparison(self, goal: Mapping[str, Any]) -> JsonDict:
        comparison = goal.get("comparison") or {}
        if not isinstance(comparison, Mapping):
            comparison = {}

        comparison_type = self._as_nonblank_string(comparison.get("type")) or "none"
        return {
            "type": self._normalize_choice(comparison_type, ["none", "mom", "qoq", "yoy"], "none"),
            "baseline_period": self._as_nonblank_string(comparison.get("baseline_period")) or "",
        }

    def _build_modules(
        self,
        goal: Mapping[str, Any],
        user_question: str,
        clarifying_questions: List[str],
    ) -> Dict[str, JsonDict]:
        modules_input = goal.get("modules") or {}
        if not isinstance(modules_input, Mapping):
            modules_input = {}

        inferred_modules = self._infer_modules_from_text(user_question)
        modules: Dict[str, JsonDict] = {}

        for name, defaults in MODULE_DEFINITIONS.items():
            raw_config = modules_input.get(name)
            if isinstance(raw_config, Mapping):
                enabled = bool(raw_config.get("enabled", False))
                metrics = self._clean_string_list(raw_config.get("metrics")) or list(defaults["metrics"])
                dimensions = self._clean_string_list(raw_config.get("dimensions")) or list(
                    defaults["dimensions"]
                )
            elif modules_input:
                enabled = False
                metrics = list(defaults["metrics"])
                dimensions = list(defaults["dimensions"])
            elif inferred_modules:
                enabled = name in inferred_modules
                metrics = list(defaults["metrics"])
                dimensions = list(defaults["dimensions"])
            else:
                enabled = True
                metrics = list(defaults["metrics"])
                dimensions = list(defaults["dimensions"])

            modules[name] = {
                "enabled": enabled,
                "metrics": metrics,
                "dimensions": dimensions,
            }

        if not modules_input and not inferred_modules:
            clarifying_questions.append(
                "Confirm whether all institute modules should be in scope or only selected modules."
            )

        if not any(module["enabled"] for module in modules.values()):
            clarifying_questions.append("At least one module must be enabled for analysis.")

        return modules

    def _required_datasets(self, goal: Mapping[str, Any], enabled_modules: Sequence[str]) -> List[str]:
        provided = self._clean_string_list(goal.get("required_datasets"))
        if provided:
            return provided

        datasets: List[str] = []
        for module_name in enabled_modules:
            datasets.extend(MODULE_DEFINITIONS[module_name]["datasets"])
        return self._unique(datasets)

    def _build_alerts(self, goal: Mapping[str, Any]) -> JsonDict:
        raw = goal.get("alerts") or {}
        if not isinstance(raw, Mapping):
            raw = {}
        return {name: bool(raw.get(name, default)) for name, default in DEFAULT_ALERTS.items()}

    def _build_targets(
        self, goal: Mapping[str, Any], clarifying_questions: List[str]
    ) -> JsonDict:
        raw = goal.get("kpi_targets") or {}
        if not isinstance(raw, Mapping):
            raw = {}

        targets = {}
        for name, default in DEFAULT_KPI_TARGETS.items():
            value = raw.get(name, default)
            targets[name] = value if value is not None else ""
            if self._is_blank(value):
                clarifying_questions.append(f"Confirm KPI target value for {name}.")

        return targets

    def _build_business_questions(
        self,
        goal: Mapping[str, Any],
        enabled_modules: Sequence[str],
        modules: Mapping[str, JsonDict],
        analysis_types: Sequence[str],
        priority: str,
        time_window: Mapping[str, str],
        comparison: Mapping[str, str],
        user_question: str = "",
    ) -> List[JsonDict]:
        provided_questions = self._clean_string_list(goal.get("business_questions"))
        questions: List[JsonDict] = []
        default_analysis_type = analysis_types[0] if analysis_types else "descriptive"

        if provided_questions:
            for question in provided_questions:
                module_name = self._infer_module_for_question(question, enabled_modules)
                questions.append(
                    self._question_record(
                        question_id=f"BQ-{len(questions) + 1:03d}",
                        module_name=module_name,
                        question=question,
                        modules=modules,
                        analysis_type=default_analysis_type,
                        priority=priority,
                        time_window=time_window,
                        comparison=comparison,
                        steer_text=question,
                    )
                )
            return questions

        # A free-text user question becomes BQ-001, with its metric/dimension
        # inferred from keywords so the analysis answers what was actually asked
        # (not the generic module default). The module template questions follow,
        # so other angles still surface.
        if user_question and enabled_modules:
            module_name = self._infer_module_for_question(user_question, enabled_modules)
            questions.append(
                self._question_record(
                    question_id="BQ-001",
                    module_name=module_name,
                    question=user_question,
                    modules=modules,
                    analysis_type=default_analysis_type,
                    priority=priority,
                    time_window=time_window,
                    comparison=comparison,
                    steer_text=user_question,
                )
            )

        for module_name in enabled_modules:
            for question in MODULE_DEFINITIONS[module_name]["questions"]:
                questions.append(
                    self._question_record(
                        question_id=f"BQ-{len(questions) + 1:03d}",
                        module_name=module_name,
                        question=question,
                        modules=modules,
                        analysis_type=default_analysis_type,
                        priority=priority,
                        time_window=time_window,
                        comparison=comparison,
                    )
                )

        return questions

    def _question_record(
        self,
        question_id: str,
        module_name: str,
        question: str,
        modules: Mapping[str, JsonDict],
        analysis_type: str,
        priority: str,
        time_window: Mapping[str, str],
        comparison: Mapping[str, str],
        steer_text: str = "",
    ) -> JsonDict:
        module_config = modules[module_name]
        metrics = list(module_config["metrics"])
        dimensions = list(module_config["dimensions"])
        if steer_text:
            metrics = self._steer_metrics(steer_text, metrics)
            dimensions = self._steer_dimensions(steer_text, dimensions)
        return {
            "question_id": question_id,
            "module": module_name,
            "question": question,
            "decision_supported": self._decision_for_module(module_name),
            "metrics": metrics,
            "dimensions": dimensions,
            "analysis_type": analysis_type,
            "priority": priority,
            "time_window": dict(time_window),
            "comparison": dict(comparison),
            "expected_datasets": list(MODULE_DEFINITIONS[module_name]["datasets"]),
        }

    def _steer_metrics(self, text: str, metrics: List[str]) -> List[str]:
        """Move the metric whose keywords match `text` to position 0 (the Analyst
        picks metrics[0]). Only reorders within the module's own metric list; if
        nothing matches, the order is unchanged."""
        low = text.lower()
        for metric, keywords in METRIC_KEYWORDS:
            if metric in metrics and any(k in low for k in keywords):
                return [metric] + [m for m in metrics if m != metric]
        return metrics

    def _steer_dimensions(self, text: str, dimensions: List[str]) -> List[str]:
        """Move the dimension the question asks to break down by to position 0."""
        low = text.lower()
        for dim, keywords in DIMENSION_KEYWORDS:
            if dim in dimensions and any(k in low for k in keywords):
                return [dim] + [d for d in dimensions if d != dim]
        return dimensions

    def _build_hypotheses(
        self, goal: Mapping[str, Any], risks: Sequence[str], alerts: Mapping[str, bool]
    ) -> List[JsonDict]:
        provided = self._clean_string_list(goal.get("hypotheses"))
        if provided:
            return [
                {
                    "hypothesis": hypothesis,
                    "test_plan": self._generic_test_plan(hypothesis),
                }
                for hypothesis in provided
            ]

        hypotheses: List[JsonDict] = []
        for risk in risks:
            record = RISK_HYPOTHESES.get(risk)
            if record:
                hypotheses.append(dict(record))

        for alert_name, enabled in alerts.items():
            if enabled and alert_name in ALERT_HYPOTHESES:
                hypotheses.append(dict(ALERT_HYPOTHESES[alert_name]))

        return self._unique_records(hypotheses, "hypothesis")

    def _validate_catalog(self, required_datasets: Sequence[str]) -> JsonDict:
        if self.catalog is None:
            return {
                "status": "not_checked",
                "available_datasets": [],
                "missing_datasets": [],
            }

        available: Optional[List[str]] = None
        if hasattr(self.catalog, "list_datasets"):
            available = list(getattr(self.catalog, "list_datasets")())
            missing = [name for name in required_datasets if name not in available]
        elif hasattr(self.catalog, "has_dataset"):
            missing = [
                name
                for name in required_datasets
                if not bool(getattr(self.catalog, "has_dataset")(name))
            ]
            available = [name for name in required_datasets if name not in missing]
        else:
            return {
                "status": "not_checked",
                "available_datasets": [],
                "missing_datasets": [],
            }

        return {
            "status": "checked",
            "available_datasets": available,
            "missing_datasets": missing,
        }

    def _status(
        self,
        analysis_request: Mapping[str, Any],
        time_window: Mapping[str, str],
        enabled_modules: Sequence[str],
        required_datasets: Sequence[str],
        catalog_validation: Mapping[str, Any],
        clarifying_questions: Sequence[str],
    ) -> str:
        if catalog_validation.get("status") == "checked" and catalog_validation.get("missing_datasets"):
            return "blocked"

        if (
            not analysis_request.get("raw_business_problem")
            or not time_window.get("start_date")
            or not time_window.get("end_date")
            or not enabled_modules
            or not required_datasets
            or clarifying_questions
        ):
            return "needs_clarification"

        return "ready"

    def _infer_modules_from_text(self, text: str) -> List[str]:
        """Map a free-text request to the fixed module set. Tries the LLM first
        (robust to phrasing the keyword list misses, e.g. "students keep leaving"
        -> courses/dropout); validates its answer against the module enum and
        falls back to keyword matching on any failure."""
        keyword_matches = self._keyword_modules_from_text(text)
        llm_matches = self._llm_modules_from_text(text)
        # Use LLM modules only when they are a non-empty, valid subset; union with
        # keyword hits so a clear keyword signal is never dropped.
        if llm_matches:
            merged = [m for m in MODULE_DEFINITIONS if m in set(llm_matches) | set(keyword_matches)]
            return merged
        return keyword_matches

    def _keyword_modules_from_text(self, text: str) -> List[str]:
        text_lower = text.lower()
        matches = []
        for module_name, keywords in MODULE_KEYWORDS.items():
            if any(keyword in text_lower for keyword in keywords):
                matches.append(module_name)
        return matches

    def _llm_modules_from_text(self, text: str) -> List[str]:
        """LLM module inference, constrained to the valid module names. Returns []
        on no key / API error / nothing valid — caller then uses keywords only."""
        text = (text or "").strip()
        if not text or not llm_client.available():
            return []
        valid = list(MODULE_DEFINITIONS)
        try:
            descriptions = {
                name: ", ".join(MODULE_DEFINITIONS[name].get("metrics", [])[:4])
                for name in valid
            }
            prompt = (
                "An education institute analyst wrote this request:\n"
                f'"{text}"\n\n'
                "Pick which of these analytics modules the request is about. "
                "Choose only from this exact list (use the keys):\n"
                f"{json.dumps(descriptions, ensure_ascii=False, default=str)}\n\n"
                'Return STRICT JSON only: {"modules": ["<key>", ...]}. Include a '
                "module only if the request clearly concerns it. If unsure, return "
                "an empty list."
            )
            obj = llm_client.complete_json(prompt, max_tokens=200, temperature=0.0)
            raw = obj.get("modules") if isinstance(obj, Mapping) else None
            if not isinstance(raw, list):
                return []
            # Keep only valid module keys, preserve canonical order, de-dup.
            chosen = {str(m).strip() for m in raw}
            return [m for m in valid if m in chosen]
        except llm_client.LLMUnavailable:
            return []
        except Exception:  # noqa: BLE001 - inference must never break scoping
            return []

    def _infer_module_for_question(self, question: str, enabled_modules: Sequence[str]) -> str:
        text = question.lower()
        for module_name in enabled_modules:
            keywords = MODULE_KEYWORDS[module_name]
            if any(keyword in text for keyword in keywords):
                return module_name
        return enabled_modules[0] if enabled_modules else "admissions"

    def _infer_analysis_types(self, text: str) -> List[str]:
        text_lower = text.lower()
        types = []
        if any(word in text_lower for word in ["why", "cause", "drop", "decline", "driver"]):
            types.append("diagnostic")
        if any(word in text_lower for word in ["forecast", "predict", "next"]):
            types.append("forecasting")
        if any(word in text_lower for word in ["monitor", "alert", "daily", "weekly", "monthly"]):
            types.append("monitoring")
        if any(word in text_lower for word in ["recommend", "improve", "action"]):
            types.append("prescriptive")
        if not types:
            types.append("descriptive")
        return types

    def _decision_from_problem(self, problem: str, analysis_types: Sequence[str]) -> str:
        if not problem:
            return "Identify module-level issues and actions for leadership"
        if "monitoring" in analysis_types:
            return "Monitor institute KPIs and trigger action when performance crosses thresholds"
        if "diagnostic" in analysis_types:
            return "Identify drivers of the stated institute performance problem"
        if "prescriptive" in analysis_types:
            return "Recommend actions to improve institute performance"
        return "Summarize institute performance for decision making"

    def _decision_for_module(self, module_name: str) -> str:
        decisions = {
            "admissions": "Improve admissions conversion for the admissions module",
            "counselling": "Improve counselling productivity and lead follow-up outcomes",
            "fee_management": "Improve fee collection and reduce pending or overdue fees",
            "courses": "Improve course enrollment, completion, and dropout performance",
            "certificates": "Reduce certificate pending workload and issue certificates on time",
            "student_reviews": "Improve student satisfaction and identify review-driven quality risks",
        }
        return decisions[module_name]

    def _generic_test_plan(self, hypothesis: str) -> str:
        module_name = self._infer_module_for_question(hypothesis, list(MODULE_DEFINITIONS))
        metrics = ", ".join(MODULE_DEFINITIONS[module_name]["metrics"][:3])
        dimensions = ", ".join(MODULE_DEFINITIONS[module_name]["dimensions"][:3])
        return f"Test using {metrics} by {dimensions} over the selected time window."

    def _clean_string_list(self, value: Any) -> List[str]:
        if value is None:
            return []
        if isinstance(value, str):
            values: Iterable[Any] = [value]
        elif isinstance(value, Iterable):
            values = value
        else:
            values = [value]
        return [str(item).strip() for item in values if not self._is_blank(item)]

    def _as_nonblank_string(self, value: Any) -> str:
        if self._is_blank(value):
            return ""
        return str(value).strip()

    def _is_blank(self, value: Any) -> bool:
        if value is None:
            return True
        if isinstance(value, str):
            return not value.strip()
        return False

    def _normalize_choice(self, value: str, choices: Sequence[str], default: str) -> str:
        value_lower = str(value or "").strip().lower()
        return value_lower if value_lower in choices else default

    def _valid_date(self, value: str) -> bool:
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            return False
        return True

    def _unique(self, values: Sequence[str]) -> List[str]:
        seen = set()
        result = []
        for value in values:
            if value not in seen:
                seen.add(value)
                result.append(value)
        return result

    def _unique_records(self, records: Sequence[JsonDict], key: str) -> List[JsonDict]:
        seen = set()
        result = []
        for record in records:
            value = record.get(key)
            if value not in seen:
                seen.add(value)
                result.append(record)
        return result


if __name__ == "__main__":
    import sys

    payload = sys.stdin.read()
    brief = ProblemDefinitionAgent().run(payload)
    print(json.dumps(brief, indent=2))
