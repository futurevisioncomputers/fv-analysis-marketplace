"""Stage registry: the pipeline as independent, resumable steps.

The orchestrator used to run the pipeline *question-major* — for each business
question, run Analyst -> Visualization -> Insights -> Recommendation. That
shape has no place to pause: a checkpoint after "Visualization" would fire once
per question, half-way through the run, which is not a stage boundary an
operator can act on.

This module re-expresses the same work *stage-major*. Each stage runs to
completion across every question, records its result in the `Session`, and
returns. That gives exactly one boundary per stage, which is what makes
"pause, show the operator, ask, continue" possible — and what lets a single
stage be invoked on its own from a skill.

The transposition is behaviour-preserving. No agent holds state between
questions, so running Analyst for all questions and then Visualization for all
of them produces the same values as interleaving them. `scripts/run_stage.py`
carries a parity check against the pre-refactor pipeline.

Dependency direction: `orchestrator_agent` imports this module, never the
reverse.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from . import multifactor
from .session import Session, SessionError

from .problem_definition_agent import ProblemDefinitionAgent
from .dynamic_data_processor_agent import DynamicDataProcessorAgent
from .data_engineer_agent import DataEngineerAgent
from .feature_engineering_agent import FeatureEngineeringAgent
from .eda_agent import EDAAgent
from .analyst_agent import AnalystAgent
from .prediction_agent import PredictionAgent
from .visualization_agent import VisualizationAgent
from .insights_agent import InsightsAgent
from .recommendation_agent import RecommendationAgent
from .monitoring_agent import MonitoringAgent
from .report_agent import ReportAgent

JsonDict = Dict[str, Any]

# Artifact names. Kept as constants because skills reference them by name when
# offering the operator a download at a checkpoint.
CANONICAL_PARQUET = "canonical.parquet"
CLEANED_CSV = "cleaned.csv"
FEATURES_PARQUET = "features.parquet"
REPORT_HTML = "report.html"

# Alert flag -> candidate monitoring hook. Only alerts whose metric the Analyst
# can actually compute become active hooks; the rest register inactive inside
# MonitoringAgent, so nothing is fabricated.
ALERT_HOOK_TEMPLATES: Dict[str, JsonDict] = {
    "low_admission_alert": {"metric": "admission_conversion_rate",
                            "scope": "overall", "threshold": "below 20%",
                            "severity": "warning"},
    "pending_fee_alert": {"metric": "pending_fee", "scope": "overall",
                          "threshold": "above 500000", "severity": "warning"},
    "dropout_alert": {"metric": "dropout_rate", "scope": "overall",
                      "threshold": "above 20%", "severity": "warning"},
    "negative_review_alert": {"metric": "review_rating", "scope": "overall",
                              "threshold": "below 4.0", "severity": "warning"},
}


class StageBlocked(RuntimeError):
    """A stage refused to run and said why. Not a crash — an honest halt.

    Carries the agent's own reason so the caller can show it verbatim rather
    than inventing an explanation. `status` distinguishes a refusal from a
    request for more information ("needs_clarification"), and `payload` holds
    whatever the agent did return, so a caller can still show the operator the
    partial brief instead of only an error string.
    """

    def __init__(self, stage: str, message: str, detail: Any = None,
                 status: str = "blocked", payload: Any = None):
        super().__init__(message)
        self.stage = stage
        self.detail = detail
        self.status = status
        self.payload = payload


# --------------------------------------------------------------------- shared

def _load_frame(session: Session):
    """The masked canonical frame, or None when the Data Engineer has not run.

    Read from the session artifact rather than passed between stages: each
    stage is its own process, so there is nothing in memory to hand over.
    """
    path = session.get_artifact(CANONICAL_PARQUET)
    if not path:
        return None
    try:
        import pandas as pd
        return pd.read_parquet(path)
    except Exception as exc:  # noqa: BLE001 - agents degrade without a df
        session.add_error(f"Could not read {CANONICAL_PARQUET}: {exc}")
        return None


def _require(session: Session, key: str, what: str) -> Any:
    result = session.result(key)
    if result is None:
        raise SessionError(
            f"Stage '{key}' has not run, so {what} is unavailable. "
            f"Run it first, or let the runner back-fill prerequisites."
        )
    return result


def _brief(session: Session) -> JsonDict:
    """The adapted brief when the schema stage rewrote it, else the original."""
    plan = session.result("schema") or {}
    return plan.get("adapted_brief") or _require(
        session, "problem", "the business questions")


def _dataset_mode(session: Session) -> str:
    plan = session.result("schema") or {}
    return plan.get("dataset_mode", "institute")


def select_answerable_questions(
    analyst: AnalystAgent,
    questions: Sequence[Mapping[str, Any]],
    df,
    roles: Mapping[str, str],
) -> List[JsonDict]:
    """Salvage + dedupe questions against the columns that actually exist.

    For each question, reorder its metric list so the FIRST metric is one the
    Analyst can compute on this frame — a question asking for a metric the data
    lacks is answered with the next best metric it carries, instead of being
    skipped. Then drop any question that collapses to a metric+dimension pair
    already covered, since those render identical numbers. The operator's own
    question (BQ-001) is always kept.

    Falls back to the original list when df is missing (nothing to check).
    """
    if df is None or not questions:
        return [dict(q) for q in questions]

    seen: set = set()
    kept: List[JsonDict] = []
    for question in questions:
        metrics = list(question.get("metrics") or [])
        chosen = next(
            (m for m in metrics if analyst.metric_computable(m, df, roles)), None)
        if chosen is None:
            kept.append(dict(question))       # Analyst blocks it honestly
            continue
        question = dict(question)
        if metrics and chosen != metrics[0]:
            question["metrics"] = [chosen] + [m for m in metrics if m != chosen]
        dimension = (question.get("dimensions") or ["overall"])[0]
        key = (chosen, dimension)
        if key in seen and question.get("question_id") != "BQ-001":
            continue                          # same metric+dimension already answered
        seen.add(key)
        kept.append(question)
    return kept or [dict(q) for q in questions]


def _slim_package(data_package: Mapping[str, Any]) -> JsonDict:
    """Compact view of the DataPackage for the session file.

    The full dict carries frame-sized structures that would bloat session.json
    on every save; downstream stages re-read what they need from the parquet.
    """
    quality = data_package.get("quality_report") or {}
    return {
        "status": data_package.get("status"),
        "row_count": data_package.get("row_count"),
        "canonical_df_path": data_package.get("canonical_df_path"),
        "canonical_columns": data_package.get("canonical_columns"),
        # Carries the as-of date the churn labels are true for. Without it the
        # session records the labels but not what makes them mean anything —
        # two runs a month apart disagree, correctly, and only this says why.
        "churn_summary": data_package.get("churn_summary"),
        "known_issues": quality.get("known_issues", []),
        "field_names": data_package.get("field_names", {}),
        # The scalar half of the quality report travels with the package: the
        # Analyst reads `person_id_basis` to explain *why* a person-grain
        # metric is unavailable, and dropping it would downgrade that back to
        # "required column missing".
        "quality_report": {
            "original_row_count": quality.get("original_row_count"),
            "drop_count": quality.get("drop_count"),
            "dropped_reasons": quality.get("dropped_reasons", {}),
            "deduplication_keys": quality.get("deduplication_keys", []),
            "person_id_basis": quality.get("person_id_basis", []),
            "known_issues": quality.get("known_issues", []),
        },
        "source_summary": data_package.get("source_summary", []),
        "relationship_summary": data_package.get("relationship_summary", {}),
        "multi_source_summary": data_package.get("multi_source_summary", {}),
        "domain_metrics": data_package.get("domain_metrics", {}),
        "payment_reconciliation": data_package.get("payment_reconciliation", {}),
        "enquiry_conversion": data_package.get("enquiry_conversion", {}),
    }


def _plural(n: int, word: str) -> str:
    """`3 anomalies`, not `3 anomalys` — consonant + y takes -ies."""
    if n == 1:
        return f"{n} {word}"
    if word.endswith("y") and len(word) > 1 and word[-2] not in "aeiou":
        return f"{n} {word[:-1]}ies"
    return f"{n} {word}s"


# --------------------------------------------------------------------- stages

def run_problem(session: Session) -> JsonDict:
    """Stage 1 — scope the request into modules, questions, and KPI targets."""
    payload = session.state.get("goal") or session.state.get("question") or ""
    brief = ProblemDefinitionAgent().run(payload)
    status = brief.get("status")
    if status in ("blocked", "needs_clarification"):
        raise StageBlocked("problem",
                           f"Problem Definition returned '{status}'.",
                           brief.get("clarifying_questions"),
                           status=status, payload=brief)
    return brief


def run_schema(session: Session) -> JsonDict:
    """Stage 2.5 — validate the sources against the brief; plan any joins."""
    brief = _require(session, "problem", "the brief")
    sources = session.sources()
    if not sources:
        raise SessionError("No data source recorded on this session.")
    plan = DynamicDataProcessorAgent().run(brief, sources)
    if plan.get("status") == "blocked":
        reasons = [q.get("reason") for q in (plan.get("capability_report") or [])
                   if q.get("reason")]
        raise StageBlocked("schema", "Schema validation failed.", reasons,
                           payload=plan)
    return plan


def run_clean(session: Session) -> JsonDict:
    """Stage 2 — clean, canonicalize, mask PII, write the artifacts.

    The ONLY stage that sees raw PII. Everything it writes is already masked,
    which is why `cleaned.csv` is safe to hand to an operator at the checkpoint.
    """
    brief = _brief(session)
    plan = session.result("schema") or {}
    sources = session.sources()
    engineer = DataEngineerAgent()

    multi = len(sources) > 1 or any(s.get("type") == "excel_sheet" for s in sources)
    if multi:
        package = engineer.run_sources(
            brief, sources, join_plan=plan.get("join_plan") or [],
            date_format=session.state.get("date_format"))
    else:
        package = engineer.run(brief, sources[0].get("path_or_query") or "",
                               date_format=session.state.get("date_format"))

    if package.get("status") == "blocked":
        reason = (package.get("quality_report") or {}).get("known_issues")
        raise StageBlocked("clean", "Canonical data unavailable.", reason,
                           payload=_slim_package(package))

    # Copy the canonical frame into the session so later stages do not depend
    # on wherever the Data Engineer happened to write it.
    source_parquet = package.get("canonical_df_path")
    if source_parquet and os.path.exists(source_parquet):
        import pandas as pd
        frame = pd.read_parquet(source_parquet)
        target = session.artifact_path(CANONICAL_PARQUET)
        os.makedirs(session.artifact_dir, exist_ok=True)
        frame.to_parquet(target, index=False)
        session.add_artifact(CANONICAL_PARQUET, target, "clean")

        # The operator-facing copy offered at the checkpoint. Same masked
        # frame, so no raw PII leaves this stage.
        cleaned = session.artifact_path(CLEANED_CSV)
        frame.to_csv(cleaned, index=False, encoding="utf-8")
        session.add_artifact(CLEANED_CSV, cleaned, "clean")

    return _slim_package(package)


def run_features(session: Session) -> JsonDict:
    """Stage 2.7 — propose derived features; materialize only approved ones.

    Runs in two passes on purpose. The first proposes and stops, because a
    banding threshold is a policy choice the operator owns: "short course" is
    not a fact about the data. Once `approved_features` is recorded on the
    session, a second run materializes exactly those.

    Approved features are written into the canonical parquet as well as
    `features.parquet`, because that is the frame every later stage reads —
    a feature only in the side file would never reach a metric. Approving
    changes the inputs of everything downstream, which is why `features` sits
    before EDA in the stage order: `reset_from('features')` clears the rest.
    """
    package = _require(session, "clean", "the canonical data")
    frame = _load_frame(session)
    if frame is None:
        raise StageBlocked("features", "No canonical frame to derive from.")

    agent = FeatureEngineeringAgent(session.state.get("feature_config"))
    proposal = agent.propose(package, df=frame)
    if proposal.get("status") == "blocked":
        raise StageBlocked("features", proposal.get("reason", "cannot derive"))

    approved = session.state.get("approved_features")
    listing = [{k: v for k, v in p.items() if k != "build"}
               for p in proposal["features"]]
    result: JsonDict = {
        "as_of": proposal["as_of"],
        "config": proposal["config"],
        "proposed": listing,
        "skipped": proposal["skipped"],
        "approved": list(approved or []),
        "added": [],
        "awaiting_approval": approved is None,
    }
    if approved is None:
        return result

    outcome = agent.apply(frame, proposal["features"], approved)
    result["added"] = outcome["added"]
    result["declined"] = outcome["declined"]

    canonical = session.artifact_path(CANONICAL_PARQUET)
    frame.to_parquet(canonical, index=False)
    session.add_artifact(CANONICAL_PARQUET, canonical, "features")

    columns = [f["id"] for f in outcome["added"]]
    # Register each new column as a role on the data package. Without this a
    # feature exists in the parquet but is invisible to every stage that works
    # from the role map — EDA would not profile it and no question could name
    # it as a dimension.
    if columns:
        roles = dict((package.get("canonical_columns") or {}))
        roles.update({name: name for name in columns})
        package["canonical_columns"] = roles
        session.state["stages"]["clean"]["result"] = package
        session.save()
    if columns:
        keys = [c for c in ("person_id", "student-id", "event_date")
                if c in frame.columns]
        path = session.artifact_path(FEATURES_PARQUET)
        frame[keys + columns].to_parquet(path, index=False)
        session.add_artifact(FEATURES_PARQUET, path, "features")
    return result


def run_eda(session: Session) -> JsonDict:
    """Stage 3 — distributions, trends, cross-tabs, anomalies.

    Context, not a hard dependency: a blocked EDA degrades the run rather than
    stopping it, matching the pre-refactor behaviour.
    """
    package = _require(session, "clean", "the canonical data")
    report = EDAAgent().run(package, df=_load_frame(session))
    if report.get("status") == "blocked":
        session.add_error("EDA blocked; continuing without exploration context.")
        return {}
    return report


def run_analyst(session: Session) -> JsonDict:
    """Stage 4 — the headline metric per question, with a 95% CI.

    Runs across every business question in one pass. Questions whose metric is
    not computable on this data are recorded as skipped, never fabricated.
    """
    package = _require(session, "clean", "the canonical data")
    brief = _brief(session)
    eda = session.result("eda") or None
    frame = _load_frame(session)
    analyst = AnalystAgent()
    mode = _dataset_mode(session)

    questions = brief.get("business_questions") or []
    questions = select_answerable_questions(
        analyst, questions, frame, package.get("canonical_columns") or {})
    limit = session.state.get("max_questions")
    if limit:
        questions = questions[:limit]

    answered, skipped = [], []
    for question in questions:
        result = analyst.run(question, package, eda, df=frame, dataset_mode=mode)
        entry = {
            "question_id": question.get("question_id"),
            "question": question.get("question"),
            "module": question.get("module"),
            "brief": question,
            "analysis": result,
        }
        if result.get("status") == "blocked":
            entry["status"] = "skipped_not_computable"
            entry["skip_reason"] = result.get("reason")
            skipped.append(entry)
        else:
            entry["status"] = "ok"
            answered.append(entry)
    return {
        "answered": answered,
        "skipped": skipped,
        "cross_factor": _cross_factor(
            frame, package.get("canonical_columns") or {}, answered),
    }


# How many dimension pairs a run crosses on its own. Every pair is a family of
# tests; crossing all fifteen unprompted would make a "finding" somewhere
# inevitable. Three is enough to catch the obvious combination and few enough
# that the correction still means something.
MAX_AUTO_CROSSINGS = 3


def _cross_factor(frame, roles: Mapping[str, str],
                  answered: Sequence[Mapping[str, Any]]) -> JsonDict:
    """Spec §21 — cross the run's headline rate metric by two dimensions.

    Runs unprompted because the finding it produces is invisible to every other
    stage: a branch and a course can each sit slightly above average while one
    cell of the two is three times the rate, and no single breakdown can show
    that.

    Deliberately narrow. Only a **rate** is crossed (a share is comparable
    across cells of different sizes; a sum is not), only the first metric the
    run actually answered, and only the first few registry pairs. Nothing but
    cells that beat *both* margins is kept — the grids themselves stay out of
    the session, since a stored grid invites reading its largest number.
    """
    from .analyst_agent import METRIC_SPECS

    flag = metric_name = None
    columns = list(getattr(frame, "columns", []))
    for entry in answered:
        headline = (entry.get("analysis") or {}).get("headline_number") or {}
        spec = METRIC_SPECS.get(headline.get("metric")) or {}
        if spec.get("kind") == "rate" and spec.get("flag") in columns:
            metric_name, flag = headline["metric"], spec["flag"]
            break
    if not flag:
        return {"status": "skipped",
                "reason": "no rate metric was answered, and only a rate is "
                          "comparable across cells of different sizes"}

    pairs = multifactor.suggest_pairs(roles, frame)[:MAX_AUTO_CROSSINGS]
    if not pairs:
        return {"status": "skipped",
                "reason": "this source has no two dimensions with more than "
                          "one level each to cross"}

    findings, examined = [], []
    for pair in pairs:
        result = multifactor.crosstab(
            frame, rows=pair["rows"], cols=pair["cols"],
            value=flag, kind="rate", roles=roles)
        examined.append({
            "rows": pair["rows"], "cols": pair["cols"],
            "cells_tested": len(result.get("cells") or []),
            "cells_suppressed": len(result.get("suppressed") or []),
            "blocked": result.get("blocked"),
        })
        for cell in result.get("interactions") or []:
            findings.append({**cell, "metric": flag,
                             "rows": pair["rows"], "cols": pair["cols"]})

    findings.sort(key=lambda f: -abs(f.get("residual") or 0.0))
    return {
        "status": "ready",
        "metric": metric_name,
        "flag": flag,
        "pairs_examined": examined,
        "interactions": findings,
        # An empty list is a result, not an absence: it says the variation here
        # is explained by one dimension at a time.
        "verdict": (
            f"{len(findings)} combination(s) behave unlike either factor alone"
            if findings else
            "No combination beats both of its margins — every difference here "
            "is explained by one dimension on its own."
        ),
    }


def run_predict(session: Session) -> JsonDict:
    """Stage 4.5 — optional churn/completion model, honesty-gated.

    Refuses to train when labels are absent, single-class, or too few, and says
    which. Off by default: most questions do not need a model.
    """
    package = _require(session, "clean", "the canonical data")
    return PredictionAgent().run(package, df=_load_frame(session),
                                 dataset_mode=_dataset_mode(session))


def run_visualize(session: Session) -> JsonDict:
    """Stage 5 — Chart.js configs and KPI cards for every answered question."""
    package = _require(session, "clean", "the canonical data")
    analysis = _require(session, "analyst", "the analysis results")
    frame = _load_frame(session)
    agent = VisualizationAgent()
    return {
        "by_question": {
            entry["question_id"]: agent.run(entry["analysis"], package, df=frame)
            for entry in analysis["answered"]
        }
    }


def run_insights(session: Session) -> JsonDict:
    """Stage 6 — findings, root causes, risks. No actions (that is stage 6.5)."""
    package = _require(session, "clean", "the canonical data")
    analysis = _require(session, "analyst", "the analysis results")
    visuals = (session.result("visualize") or {}).get("by_question", {})
    eda = session.result("eda") or None
    agent = InsightsAgent()
    return {
        "by_question": {
            entry["question_id"]: agent.run(
                entry["brief"], entry["analysis"],
                visual_package=visuals.get(entry["question_id"]),
                eda_report=eda, data_package=package)
            for entry in analysis["answered"]
        }
    }


def run_recommend(session: Session) -> JsonDict:
    """Stage 6.5 — prioritized, owner-tagged actions. The only action source."""
    analysis = _require(session, "analyst", "the analysis results")
    insights = (_require(session, "insights", "the insight reports")
                or {}).get("by_question", {})
    agent = RecommendationAgent()
    return {
        "by_question": {
            entry["question_id"]: agent.run(
                insights.get(entry["question_id"]) or {},
                entry["analysis"], entry["brief"])
            for entry in analysis["answered"]
        }
    }


def run_monitor(session: Session) -> JsonDict:
    """Stage 7 — register KPI hooks, then evaluate them against the data."""
    package = _require(session, "clean", "the canonical data")
    brief = session.result("problem") or {}
    insights = (session.result("insights") or {}).get("by_question", {})
    recommends = (session.result("recommend") or {}).get("by_question", {})

    hooks: List[JsonDict] = []
    for source in (insights, recommends):
        for report in source.values():
            hooks.extend((report or {}).get("monitoring_hooks") or [])
    alerts = ((brief.get("kpi_framework") or {}).get("alerts")) or {}
    for flag, enabled in alerts.items():
        if enabled and flag in ALERT_HOOK_TEMPLATES:
            hooks.append(dict(ALERT_HOOK_TEMPLATES[flag]))

    # Thresholds are meant to persist across runs on the same data, so a caller
    # may point at a shared registry instead of the session's own copy.
    registry = session.state.get("registry_path") or session.registry_path
    agent = MonitoringAgent(analyst=AnalystAgent())
    agent.register(hooks, registry)
    return agent.evaluate(package, registry,
                          eda_report=session.result("eda") or None,
                          df=_load_frame(session), problem_brief=brief)


def run_report(session: Session) -> JsonDict:
    """Stage 8 — compose the shareable HTML report from the stored contracts."""
    final = assemble_report(session)
    session.state["final_report"] = final
    report = ReportAgent().run(final, question_results(session),
                               brief=session.result("problem"))
    html = report.get("html") or ""
    path = session.artifact_path(REPORT_HTML)
    os.makedirs(session.artifact_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)
    session.add_artifact(REPORT_HTML, path, "report")
    return {"html_path": path,
            "narrative": report.get("narrative"),
            "generated_at": report.get("generated_at")}


# ------------------------------------------------------------------- registry

StageFn = Callable[[Session], Any]

RUNNERS: Dict[str, StageFn] = {
    "problem": run_problem,
    "schema": run_schema,
    "clean": run_clean,
    "features": run_features,
    "eda": run_eda,
    "analyst": run_analyst,
    "predict": run_predict,
    "visualize": run_visualize,
    "insights": run_insights,
    "recommend": run_recommend,
    "monitor": run_monitor,
    "report": run_report,
}


def run_stage(session: Session, key: str) -> JsonDict:
    """Execute one stage and record it. Returns the session entry.

    A `StageBlocked` is recorded as a blocked stage and re-raised, so the
    caller can show the agent's own reason instead of a stack trace.
    """
    runner = RUNNERS.get(key)
    if runner is None:
        raise SessionError(
            f"Stage {key!r} has no runner. Available: {', '.join(RUNNERS)}")
    # Durable before the work starts, so a watcher can see which agent is
    # working rather than inferring it from silence.
    session.begin(key)
    try:
        result = runner(session)
    except StageBlocked as blocked:
        session.record(key, {"reason": str(blocked), "detail": blocked.detail},
                       status="blocked", summary=str(blocked))
        raise
    # Summarize from the result in hand, not from the session: `record` has not
    # stored it yet, so reading it back would summarize nothing.
    try:
        metrics = stage_metrics(key, result)
    except Exception as exc:  # noqa: BLE001
        # Instrumentation must never cost a stage its result. A missed metric
        # is a gap in a chart; a raised metric would strand the stage as
        # `running` and lose work the agent already did.
        session.add_error(f"Stage metrics unavailable for {key}: {exc}")
        metrics = {}
    session.record(key, result, summary=summarize(key, result), metrics=metrics)
    return session.state["stages"][key]


# What each agent actually did, in numbers, on one comparable shape.
#
# `summary` is a sentence for a human and `details` is prose; neither can be
# charted or compared across runs. These can. Every value is read from the
# result the agent already returned — nothing is measured twice, and a stage
# that does not report a given quantity simply omits the key rather than
# reporting a zero that looks like a real measurement.
def stage_metrics(key: str, result: Any) -> JsonDict:
    """Comparable per-stage numbers for a performance view."""
    if not isinstance(result, Mapping):
        return {}
    out: JsonDict = {}

    def put(name: str, value: Any) -> None:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[name] = value

    def count(value: Any) -> Optional[int]:
        """How many, whether the agent returned a list or already a count.

        The contracts are not uniform — `monitor` reports `active_alerts` as a
        number while `insights` reports lists — and guessing wrong used to
        raise inside the recorder and strand the stage as `running`.
        """
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, int):
            return value
        try:
            return len(value)
        except TypeError:
            return None

    def per_question(field: str) -> int:
        return sum(len((q or {}).get(field) or [])
                   for q in (result.get("by_question") or {}).values())

    if key == "problem":
        put("questions", count(result.get("business_questions")))
        put("hypotheses", count(result.get("hypotheses")))
    elif key == "schema":
        put("sources_validated", count(result.get("sources_validated")))
        put("joins_planned", count(result.get("join_plan")))
    elif key == "clean":
        put("rows_out", result.get("row_count"))
        put("columns", count(result.get("canonical_columns")))
        put("quality_notes", count(result.get("known_issues")))
        quality = result.get("quality_report") or {}
        put("rows_in", quality.get("original_row_count"))
        put("rows_dropped", quality.get("drop_count"))
        put("sources", count(result.get("source_summary")))
    elif key == "features":
        put("proposed", count(result.get("proposed")))
        put("materialized", count(result.get("materialized")))
    elif key == "eda":
        put("rows", result.get("row_count"))
        put("dimensions", count(result.get("profiled_dimensions")))
        put("numeric_fields", count(result.get("profiled_numerics")))
        put("anomalies", count(result.get("anomalies")))
        put("hypotheses", count(result.get("candidate_hypotheses")))
    elif key == "analyst":
        put("questions_answered", count(result.get("answered")))
        put("questions_skipped", count(result.get("skipped")))
        put("crossings", count(result.get("cross_factor")))
    elif key == "visualize":
        put("charts", per_question("charts"))
        put("kpi_cards", per_question("kpi_cards"))
    elif key == "insights":
        put("findings", per_question("key_findings"))
        put("risks", per_question("risks"))
        put("opportunities", per_question("opportunities"))
        put("root_causes", per_question("root_causes"))
    elif key == "recommend":
        put("actions", per_question("recommendations"))
        put("monitoring_hooks", per_question("monitoring_hooks"))
    elif key == "monitor":
        put("active_alerts", count(result.get("active_alerts")))
        put("events", count(result.get("events")))
    elif key == "report":
        put("narrative_chars", len(str(result.get("narrative") or "")))
    return out


# ----------------------------------------------------------------- checkpoint

# What the operator may do at each pause, beyond "continue" and "stop". Held
# here rather than in the CLI or the skill text so that every caller — the
# terminal, a skill, the web service — offers the same choices, and adding a
# stage cannot leave one of them behind.
CHECKPOINT_OFFERS: Dict[str, List[JsonDict]] = {
    "problem": [
        {"id": "show", "label": "Show the business questions in full"},
        {"id": "add-question", "label": "Add a question to the brief",
         "note": "re-runs stage 1 and clears everything downstream"},
        {"id": "data-needs", "label": "What data each question needs, and "
                                      "whether this source has it"},
    ],
    "schema": [
        {"id": "show", "label": "Show the capability report per question"},
        {"id": "mapping", "label": "Show the column → role mapping"},
    ],
    "clean": [
        {"id": "download", "label": "Download the cleaned CSV",
         "artifact": CLEANED_CSV},
        {"id": "quality", "label": "Show the full quality report"},
    ],
    "features": [
        {"id": "approve-all", "label": "Approve every proposed feature",
         "command": "--approve-features all"},
        {"id": "approve-some", "label": "Approve only some, by id",
         "command": "--approve-features duration_group,age_band"},
        {"id": "skip", "label": "Build none of them and move on",
         "command": "--approve-features none"},
        {"id": "download", "label": "Download the feature table",
         "artifact": FEATURES_PARQUET},
    ],
    "eda": [
        {"id": "show", "label": "Show distributions, trends and anomalies"},
    ],
    "analyst": [
        {"id": "show", "label": "Show every metric with its confidence interval"},
        {"id": "skipped", "label": "Show what could not be computed, and why"},
    ],
    "predict": [
        {"id": "show", "label": "Show model metrics and lift over baseline"},
    ],
    "visualize": [
        {"id": "show", "label": "List the charts and KPI cards"},
    ],
    "insights": [
        {"id": "show", "label": "Show findings, root causes and risks"},
    ],
    "recommend": [
        {"id": "show", "label": "Show the recommended actions by priority"},
    ],
    "monitor": [
        {"id": "show", "label": "Show KPI hooks and active alerts"},
    ],
    "report": [
        {"id": "open", "label": "Open the HTML report", "artifact": REPORT_HTML},
    ],
}


def capability_lines(needs: Mapping[str, Any]) -> List[str]:
    """The data-needs check as text: one line per question, then the gaps."""
    lines: List[str] = []
    for entry in needs.get("questions") or []:
        qid = entry.get("question_id")
        if entry["answerable"]:
            mark = "✓"
            note = str(entry.get("will_answer_with", "")).replace("_", " ")
            if entry.get("substituted"):
                note += (f"  (asked for "
                         f"{str(entry.get('asked_for')).replace('_', ' ')})")
        else:
            mark = "✗"
            note = "needs " + ", ".join(n["role"].replace("_", " ")
                                        for n in entry.get("needs") or [])
        lines.append(f"{mark} {qid}  {note}")

    for gap in needs.get("missing_data") or []:
        where = gap["found_in"]
        unlocks = ", ".join(gap["unlocks"])
        lines.append(f"→ add {gap['role'].replace('_', ' ')} — {where} "
                     f"(unlocks {unlocks})")
    return lines


def stage_details(key: str, result: Any) -> List[str]:
    """The few lines an operator wants to see without asking for the full dump.

    Distinct from `summarize`, which is one line for the rail. These are the
    contents of the checkpoint itself.
    """
    if not result:
        return []
    lines: List[str] = []

    if key == "problem":
        for question in (result.get("business_questions") or [])[:8]:
            # The first metric is the one the Analyst will actually compute;
            # the rest are fallbacks and would bury the question if listed.
            metrics = question.get("metrics") or []
            head = str(metrics[0]).replace("_", " ") if metrics else "no metric"
            more = f" +{len(metrics) - 1}" if len(metrics) > 1 else ""
            lines.append(f"{question.get('question_id')}  "
                         f"{_truncate(question.get('question'), 88)}"
                         f"  [{head}{more}]")

    elif key == "schema":
        for entry in (result.get("capability_report") or [])[:8]:
            verdict = "answerable" if entry.get("answerable") else "NOT answerable"
            reason = f" — {entry['reason']}" if entry.get("reason") else ""
            lines.append(f"{entry.get('question_id')}  {verdict}{reason}")

    elif key == "clean":
        quality = result.get("quality_report") or {}
        rows, original = result.get("row_count"), quality.get("original_row_count")
        sources = result.get("source_summary") or []
        if original and rows is not None:
            if len(sources) > 1:
                # On a multi-sheet run `original` is the sum across every
                # sheet, and the master frame is one row per enrollment — 772
                # receipt rows fold into 420 students by design. Reporting that
                # as "420 of 1,912 kept" reads as 78% data loss when nothing
                # was lost at all.
                lines.append(
                    f"{rows:,} row(s) on the master frame, joined from "
                    f"{_plural(len(sources), 'sheet')} totalling {original:,} "
                    f"row(s) — a join folds detail rows into their parent, so "
                    f"the difference is not loss")
            else:
                lines.append(f"rows {rows:,} of {original:,} kept"
                             + (f" · dropped {quality['drop_count']:,}"
                                if quality.get("drop_count") else ""))
        basis = quality.get("person_id_basis") or []
        if basis:
            lines.append("person identity: " + " + ".join(basis)
                         + ("  (name alone — person-grain metrics withheld)"
                            if list(basis) == ["name"] else ""))
        named = result.get("field_names") or {}
        if named:
            lines.append(f"mapped {_plural(len(named), 'column')} to canonical "
                         f"names (ask to see the mapping)")
        issues = result.get("known_issues") or []
        # Naming decisions are the ones an operator can contest, so they lead.
        naming = [i for i in issues if str(i).startswith("Field naming:")]
        rest = [i for i in issues if not str(i).startswith("Field naming:")]
        lines.extend(f"· {_truncate(issue, 110)}" for issue in naming[:3])
        lines.extend(f"· {_truncate(issue, 110)}" for issue in rest[:6])
        hidden = len(issues) - len(naming[:3]) - len(rest[:6])
        if hidden > 0:
            lines.append(f"… {hidden} more quality note(s)")

    elif key == "features":
        if result.get("awaiting_approval"):
            lines.append("proposed — nothing is built until you approve it:")
        for feature in result.get("proposed") or []:
            mark = "✓" if feature["id"] in (result.get("approved") or []) else "·"
            preview = ", ".join(f"{k} {v}" for k, v in
                                list((feature.get("preview") or {}).items())[:4])
            lines.append(f"{mark} {feature['id']:24s} {_truncate(feature['rule'], 70)}")
            if preview:
                lines.append(f"    {preview}")
        for skip in result.get("skipped") or []:
            lines.append(f"✗ {skip['id']:24s} {_truncate(skip['reason'], 70)}")
        if result.get("added"):
            lines.append(f"built {_plural(len(result['added']), 'column')} into "
                         f"the frame every later stage reads")

    elif key == "eda":
        for anomaly in (result.get("anomalies") or [])[:6]:
            if isinstance(anomaly, str):
                lines.append("· " + _truncate(anomaly, 110))
                continue
            # EDA emits structured anomalies; render the fields an operator
            # reads rather than the dict repr.
            pct = anomaly.get("magnitude_pct")
            change = f" ({pct:+.0%})" if isinstance(pct, (int, float)) else ""
            lines.append(
                f"· {anomaly.get('type', 'anomaly')} in "
                f"{str(anomaly.get('metric', '')).replace('_', ' ')} at "
                f"{anomaly.get('period', '?')}: {_fmt_num(anomaly.get('value'))}"
                f"{change} vs {anomaly.get('vs', 'trend')}")
        extra = len(result.get("anomalies") or []) - 6
        if extra > 0:
            lines.append(f"… {extra} more anomaly/anomalies")

    elif key == "analyst":
        for entry in (result.get("answered") or [])[:8]:
            head = (entry["analysis"].get("headline_number") or {})
            ci = head.get("ci_95") or []
            span = f"  95% CI [{ci[0]}, {ci[1]}]" if len(ci) == 2 else ""
            lines.append(f"{entry['question_id']}  "
                         f"{str(head.get('metric', '?')).replace('_', ' ')} = "
                         f"{_fmt_num(head.get('value'))} (n={head.get('n')}){span}")
        for entry in (result.get("skipped") or [])[:6]:
            lines.append(f"{entry['question_id']}  skipped — "
                         f"{_truncate(entry.get('skip_reason'), 100)}")

    elif key == "predict":
        if result.get("status") == "blocked":
            lines.append(f"no model — {result.get('reason')}")
        else:
            metrics = result.get("metrics") or {}
            lines.append(f"accuracy {metrics.get('accuracy')} vs baseline "
                         f"{metrics.get('baseline_accuracy')}"
                         f" · rows {metrics.get('n_train')}/{metrics.get('n_test')}")

    elif key in ("visualize", "insights", "recommend"):
        for qid, package in list((result.get("by_question") or {}).items())[:6]:
            if key == "visualize":
                lines.append(f"{qid}  {len(package.get('charts') or [])} chart(s), "
                             f"{len(package.get('kpi_cards') or [])} KPI card(s)")
            elif key == "insights":
                lines.append(f"{qid}  {_truncate(package.get('executive_summary'), 110)}")
            else:
                for action in (package.get("recommendations") or [])[:2]:
                    lines.append(f"{qid}  [{action.get('priority_bucket', 'P?')}] "
                                 f"{_truncate(action.get('action'), 100)}")

    elif key == "monitor":
        for event in (result.get("events") or [])[:6]:
            lines.append(f"· {str(event.get('metric', '')).replace('_', ' ')} "
                         f"{event.get('event_type')} — {_truncate(event.get('detail', ''), 80)}")

    elif key == "report":
        lines.append(str(result.get("html_path")))

    return lines


def checkpoint(session: Session, key: str) -> JsonDict:
    """Everything a caller needs to render one pause. The checkpoint contract.

    Returned to the CLI and, as JSON, to the skills — so the terminal walk and
    a skill invocation show the operator the same thing.
    """
    from .session import STAGE_BY_KEY, OPTIONAL_STAGES

    entry = session.state["stages"].get(key) or {}
    result = entry.get("result")
    stage = STAGE_BY_KEY.get(key, {})

    offers = []
    for offer in CHECKPOINT_OFFERS.get(key, []):
        name = offer.get("artifact")
        # Do not offer a download for a file that was never written.
        if name and not session.get_artifact(name):
            continue
        offers.append({**offer,
                       **({"path": session.get_artifact(name)} if name else {})})

    following = session.next_stage()
    extra: JsonDict = {}
    if entry.get("status") == "blocked" and isinstance(result, Mapping):
        # A refusal is only useful if the caller can see WHAT the agent wants.
        # Problem Definition asks eleven clarifying questions; without these
        # the operator reads "returned 'needs_clarification'" and has no idea
        # which answer would unblock it.
        extra["reason"] = result.get("reason")
        detail = result.get("detail")
        if detail:
            extra["detail"] = detail
    if key == "problem" and result:
        # The operator's decision at this checkpoint is "are these the right
        # questions?", which cannot be answered without knowing which of them
        # this data can actually support. Computed here rather than at stage
        # 2.5 so it arrives before the pipeline has run.
        from .capability import data_needs
        try:
            extra["data_needs"] = data_needs(result, session.sources())
        except Exception as exc:  # noqa: BLE001 - advisory, never blocks a stage
            session.add_error(f"Capability check unavailable: {exc}")

    return {
        "stage": key,
        **extra,
        "n": stage.get("n"),
        "label": stage.get("label", key),
        "status": entry.get("status", "pending"),
        "optional": key in OPTIONAL_STAGES,
        "summary": entry.get("summary") or "",
        # How this agent performed, alongside what it found. Present on every
        # checkpoint so a caller never has to special-case a stage to draw it.
        "started_at": entry.get("started_at"),
        "finished_at": entry.get("finished_at"),
        "duration_ms": entry.get("duration_ms"),
        "metrics": entry.get("metrics") or {},
        "details": stage_details(key, result),
        "artifacts": {name: session.get_artifact(name)
                      for name, record in session.state["artifacts"].items()
                      if record.get("stage") == key},
        "offers": offers,
        "next_stage": following,
        "next_label": STAGE_BY_KEY.get(following, {}).get("label") if following else None,
    }


# ------------------------------------------------------------------ reporting

def question_results(session: Session) -> List[JsonDict]:
    """Re-assemble the question-major view the ReportAgent still expects.

    The pipeline now runs stage-major, but the report contract is per question.
    Stitching here keeps that contract unchanged, so the HTML template and the
    saved run JSON stay byte-compatible with the pre-refactor output.
    """
    analysis = session.result("analyst") or {"answered": [], "skipped": []}
    visuals = (session.result("visualize") or {}).get("by_question", {})
    insights = (session.result("insights") or {}).get("by_question", {})
    recommends = (session.result("recommend") or {}).get("by_question", {})

    rows: List[JsonDict] = []
    for entry in analysis["answered"]:
        qid = entry["question_id"]
        insight = insights.get(qid) or {}
        recommend = recommends.get(qid) or {}
        rows.append({
            "question_id": qid,
            "question": entry["question"],
            "module": entry["module"],
            "status": "ok",
            "analysis": entry["analysis"],
            "visual": visuals.get(qid),
            "insight": insight,
            "recommendation": recommend,
            "monitoring_hooks": ((insight.get("monitoring_hooks") or [])
                                 + (recommend.get("monitoring_hooks") or [])),
        })
    for entry in analysis["skipped"]:
        rows.append({
            "question_id": entry["question_id"],
            "question": entry["question"],
            "module": entry["module"],
            "status": entry["status"],
            "skip_reason": entry.get("skip_reason"),
            "analysis": entry["analysis"],
            "visual": None, "insight": None, "recommendation": None,
            "monitoring_hooks": [],
        })
    return rows


def assemble_report(session: Session) -> JsonDict:
    """Compact, user-facing roll-up of the run."""
    rows = question_results(session)
    answered = [r for r in rows if r["status"] == "ok"]
    skipped = [r for r in rows if r["status"] != "ok"]
    package = session.result("clean") or {}
    monitoring = session.result("monitor") or {}
    brief = session.result("problem") or {}

    recommendations: List[JsonDict] = []
    for row in answered:
        recommendations.extend((row.get("recommendation") or {})
                               .get("recommendations") or [])
    recommendations.sort(key=lambda r: r.get("priority", 999))

    return {
        "decision_supported": (brief.get("problem_statement") or {})
        .get("decision_to_support"),
        "questions_answered": len(answered),
        "questions_skipped": len(skipped),
        "skipped": [{"question_id": r["question_id"],
                     "reason": r.get("skip_reason")} for r in skipped],
        "headline_findings": [
            {
                "question_id": r["question_id"],
                "metric": (r["analysis"].get("headline_number") or {}).get("metric"),
                "value": (r["analysis"].get("headline_number") or {}).get("value"),
                "executive_summary": (r["insight"] or {}).get("executive_summary"),
            }
            for r in answered
        ],
        "top_recommendations": recommendations[:10],
        "monitoring": {
            "status": monitoring.get("status"),
            "active_alerts": monitoring.get("active_alerts", 0),
            "health": (monitoring.get("health_report") or {}).get("overall_health"),
            "events": monitoring.get("events", []),
        },
        "data_quality": {
            "row_count": package.get("row_count"),
            "known_issues": package.get("known_issues", []),
        },
        "multi_source_summary": package.get("multi_source_summary", {}),
        "sources": package.get("source_summary", []),
        "relationships": package.get("relationship_summary", {}),
        "domain_metrics": package.get("domain_metrics", {}),
        # Spec §21/§23. Travels even when empty: "no combination behaves
        # unlike either factor alone" is a finding the report should state,
        # not a section that quietly disappears when nothing was found.
        "cross_factor": (session.result("analyst") or {}).get("cross_factor", {}),
        "unjoined_sources": (package.get("relationship_summary", {})
                             or {}).get("unjoined_sources", []),
        "agent_summaries": {key: summarize(key, session.result(key))
                            for key in RUNNERS},
    }


def _fmt_num(value) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    return f"{int(value):,}" if float(value).is_integer() else f"{round(float(value), 2):,}"


def _truncate(text, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def summarize(key: str, result: Any) -> Optional[str]:
    """One line describing what a stage actually did this run.

    Derived from the stage's own result, never from a template, so the
    checkpoint text and the report's agent summaries cannot drift apart.
    Takes the result directly rather than reading it back from the session,
    because `run_stage` needs the summary before the result is recorded.
    """
    if result is None:
        return None

    if key == "problem":
        scope = result.get("scope") or {}
        kpi = result.get("kpi_framework") or {}
        modules = scope.get("enabled_modules") or []
        parts = ["Scoped " + ", ".join(modules) +
                 (" module" if len(modules) == 1 else " modules")
                 if modules else "Scoped the request"]
        n_q = len(result.get("business_questions") or [])
        if n_q:
            parts.append(f"framed {_plural(n_q, 'business question')}")
        n_t = len(kpi.get("targets") or {})
        if n_t:
            parts.append(f"set {_plural(n_t, 'KPI target')}")
        note = " · ".join(parts) + "."
        window = scope.get("time_window") or {}
        if window.get("start_date") and window.get("end_date"):
            note += f" Window {window['start_date']} → {window['end_date']}."
        return note

    if key == "schema":
        capability = result.get("capability_report") or []
        joins = result.get("join_plan") or []
        return (f"Validated schema · {_plural(len(capability), 'question')} checked"
                + (f" · {_plural(len(joins), 'join')} planned" if joins else "")
                + f" · mode {result.get('dataset_mode', 'institute')}.")

    if key == "clean":
        rows = result.get("row_count")
        if rows is None:
            return None
        cols = len(result.get("canonical_columns") or {})
        issues = result.get("known_issues") or []
        masked = sum(1 for s in issues if "mask" in str(s).lower())
        parts = [f"Cleaned {rows:,} rows" + (f" × {cols} columns" if cols else "")]
        if masked:
            parts.append(f"masked {_plural(masked, 'PII field')}")
        if issues:
            parts.append(f"logged {_plural(len(issues), 'quality note')}")
        return " · ".join(parts) + "."

    if key == "features":
        proposed = result.get("proposed") or []
        if result.get("awaiting_approval"):
            return (f"Proposed {_plural(len(proposed), 'feature')} · none built "
                    f"yet — approve to materialize.")
        added = result.get("added") or []
        declined = len(proposed) - len(added)
        note = f"Built {_plural(len(added), 'feature')}"
        if added:
            note += ": " + ", ".join(f["id"] for f in added[:4])
        return note + (f" · {declined} declined." if declined else ".")

    if key == "eda":
        if not result:
            return "EDA unavailable; continued without exploration context."
        dims = len(result.get("profiled_dimensions") or [])
        nums = len(result.get("profiled_numerics") or [])
        anomalies = result.get("anomalies") or []
        seg = []
        if dims:
            seg.append(_plural(dims, "dimension"))
        if nums:
            seg.append(_plural(nums, "numeric field"))
        parts = ["Profiled " + " and ".join(seg) if seg else "Profiled the data"]
        trend = (result.get("time_trends") or {}).get("trend_direction")
        if trend:
            parts.append(f"trend {trend}")
        parts.append(f"flagged {_plural(len(anomalies), 'anomaly')}"
                     if anomalies else "no anomalies")
        return " · ".join(parts) + "."

    if key == "analyst":
        answered = result.get("answered") or []
        if not answered:
            return f"No question was computable ({len(result.get('skipped') or [])} skipped)."
        head = (answered[0]["analysis"].get("headline_number") or {})
        note = f"Analyzed {_plural(len(answered), 'question')}"
        if head.get("metric") is not None and head.get("value") is not None:
            note += (f" · headline {str(head['metric']).replace('_', ' ')}"
                     f" = {_fmt_num(head['value'])}")
        skipped = result.get("skipped") or []
        if skipped:
            note += f" · {len(skipped)} not computable"
        return note + "."

    if key == "predict":
        if result.get("status") == "blocked":
            # The agent's reason is a sentence already; adding a period gives
            # the operator "…membership)..".
            reason = str(result.get("reason", "insufficient labels")).rstrip(".")
            return f"No model: {reason}."
        metrics = result.get("metrics") or {}
        return (f"Trained churn model · accuracy {metrics.get('accuracy')} "
                f"vs baseline {metrics.get('baseline_accuracy')}.")

    if key == "visualize":
        packages = (result.get("by_question") or {}).values()
        charts = sum(len(p.get("charts") or []) for p in packages)
        cards = sum(len(p.get("kpi_cards") or []) for p in packages)
        return f"Built {_plural(charts, 'chart')} and {_plural(cards, 'KPI card')}."

    if key == "insights":
        reports = list((result.get("by_question") or {}).values())
        if not reports:
            return None
        first = reports[0]
        counts = []
        for field, word in (("key_findings", "finding"),
                            ("root_causes", "root cause"), ("risks", "risk")):
            n = len(first.get(field) or [])
            if n:
                counts.append(_plural(n, word))
        health = first.get("business_health")
        head = f"Health: {health}. " if health else ""
        top = next((f if isinstance(f, str) else (f or {}).get("finding")
                    for f in (first.get("key_findings") or [])), None)
        body = f"Wrote {', '.join(counts)}." if counts else "Summarized the results."
        return head + body + (f" Top: “{_truncate(top, 80)}”" if top else "")

    if key == "recommend":
        actions: List[JsonDict] = []
        for report in (result.get("by_question") or {}).values():
            actions.extend(report.get("recommendations") or [])
        if not actions:
            return "No recommendations generated."
        actions.sort(key=lambda r: r.get("priority", 999))
        buckets: Dict[str, int] = {}
        for action in actions:
            bucket = action.get("priority_bucket", "P?")
            buckets[bucket] = buckets.get(bucket, 0) + 1
        spread = ", ".join(f"{k}:{v}" for k, v in sorted(buckets.items()))
        return (f"Proposed {_plural(len(actions), 'action')} ({spread}); top: "
                + _truncate(actions[0].get("action", ""), 70))

    if key == "monitor":
        health = (result.get("health_report") or {}).get("overall_health")
        if not health:
            return None
        events = result.get("events") or []
        note = (f"Registered KPI hooks · health {health} · "
                f"{_plural(result.get('active_alerts', 0), 'active alert')}")
        if events:
            metric = str(events[0].get("metric", "")).replace("_", " ")
            note += f". Latest: {metric} {events[0].get('event_type')}"
        return note + "."

    if key == "report":
        return f"Assembled the HTML report → {result.get('html_path')}"

    return None
