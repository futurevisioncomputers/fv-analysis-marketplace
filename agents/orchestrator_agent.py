"""Agent 0: Orchestrator.

Single entry point. Drives the pipeline 1 -> 7, maintains a `session_state` that
accumulates each agent's output, and assembles the final user-facing report. It
never analyzes, cleans, or recommends itself — it delegates and wires.

Flow:
  1. ProblemDefinition.run(payload)            -> ProblemDefinitionBrief
     (halt on `blocked`; surface `clarifying_questions` on `needs_clarification`)
  2. DataEngineer.run(brief, csv_path)         -> DataPackage  (halt on `blocked`)
     -> load the canonical parquet ONCE here; pass `df=` to every downstream agent
        so nobody re-reads it (PII already masked by Agent 2 — df carries hashes).
  3. EDA.run(data_package, df)                 -> EDAReport
  For each business_question in the brief:
  4. Analyst.run(question, data_package, eda, df)   -> AnalysisResult (skip if blocked)
  5. Visualization.run(result, data_package, df)    -> VisualPackage
  6. Insights.run(question, result, visual, eda, dp)-> InsightReport
  6.5 Recommendation.run(insight, result, question) -> RecommendationReport
      -> collect each report's monitoring_hooks.
  7. Monitoring.register(all_hooks, registry) then
     Monitoring.evaluate(dp, registry, eda, df, problem_brief=brief) -> events.

Honesty / boundary notes:
- PII boundary holds: only Agent 2 sees raw PII; the df handed downstream is the
  masked canonical frame, so reusing it does NOT leak PII.
- The orchestrator fabricates nothing. A question whose AnalysisResult is `blocked`
  (metric not computable on this data) is recorded and skipped — no downstream
  agent is fed a fabricated result.
- Trigger is intent-only: Monitoring emits `auto_invoke_orchestrator` flags; this
  agent does NOT recursively re-invoke itself on them (no real auto-loop yet).
"""

from __future__ import annotations

import os
import tempfile
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from . import stages
from .session import (OPTIONAL_STAGES, STAGE_BY_KEY, STAGE_KEYS,  # noqa: F401
                      Session)
from .problem_definition_agent import ProblemDefinitionAgent
from .dynamic_data_processor_agent import DynamicDataProcessorAgent
from .data_engineer_agent import DataEngineerAgent
from .eda_agent import EDAAgent
from .analyst_agent import AnalystAgent
from .visualization_agent import VisualizationAgent
from .insights_agent import InsightsAgent
from .recommendation_agent import RecommendationAgent
from .monitoring_agent import MonitoringAgent
from .report_agent import ReportAgent


JsonDict = Dict[str, Any]

# Brief alert flags -> a candidate monitoring hook (metric + human threshold).
# Only alerts whose metric the Analyst can actually compute become active hooks;
# the rest register inactive inside MonitoringAgent (no fabrication).
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
    # certificate_pending_alert has no computable metric yet -> intentionally
    # omitted (would register inactive). Add when a cert metric lands.
}


class OrchestratorAgent:
    """Wires Agents 1->7 for one user request and assembles the final report."""

    def __init__(
        self,
        problem_definition: Optional[ProblemDefinitionAgent] = None,
        dynamic_data_processor: Optional[DynamicDataProcessorAgent] = None,
        data_engineer: Optional[DataEngineerAgent] = None,
        eda: Optional[EDAAgent] = None,
        analyst: Optional[AnalystAgent] = None,
        visualization: Optional[VisualizationAgent] = None,
        insights: Optional[InsightsAgent] = None,
        recommendation: Optional[RecommendationAgent] = None,
        monitoring: Optional[MonitoringAgent] = None,
        report: Optional[ReportAgent] = None,
    ):
        self.problem_definition = problem_definition or ProblemDefinitionAgent()
        self.dynamic_data_processor = dynamic_data_processor or DynamicDataProcessorAgent()
        self.data_engineer = data_engineer or DataEngineerAgent()
        self.eda = eda or EDAAgent()
        self.analyst = analyst or AnalystAgent()
        self.visualization = visualization or VisualizationAgent()
        self.insights = insights or InsightsAgent()
        self.recommendation = recommendation or RecommendationAgent()
        self.monitoring = monitoring or MonitoringAgent(analyst=self.analyst)
        self.report = report or ReportAgent()

    # =================================================================== run

    def run(
        self,
        payload: Any,
        csv_path: str = "",
        data_sources: Optional[Sequence[Mapping[str, Any]]] = None,
        registry_path: Optional[str] = None,
        date_format: Optional[str] = None,
        max_questions: Optional[int] = None,
        on_stage: Optional[Callable[[str, str, str, Optional[str]], None]] = None,
        session_dir: Optional[str] = None,
    ) -> JsonDict:
        """Execute the whole pipeline for one request. Returns a session_state.

        This is the non-interactive path — the one the CLI and the web service
        use when nobody is there to answer a checkpoint. It drives
        `agents.stages`, the same registry `scripts/run_stage.py` drives one
        stage at a time, so there is a single implementation of what each stage
        does and the two paths cannot drift apart. The legacy `state` shape is
        assembled from the session at the end, unchanged.

        Args:
            payload: user_question string or /goal JSON (Agent 1 input).
            csv_path: legacy single source CSV for Agent 2.
            data_sources: optional multi-source list for CSV / Excel-sheet runs.
            registry_path: JSON hook registry for Agent 7. Defaults to a path
                beside the CSV; pass an explicit path to persist across runs.
            date_format: optional date hint forwarded to Agent 2.
            max_questions: cap on business questions analyzed (None = all).
            on_stage: optional callback fired as each stage finishes, with
                (stage_n, name, status, summary). Used to stream live progress;
                None for a plain blocking run.
            session_dir: where to keep the run state. Defaults to a temporary
                directory that is left in place — an interrupted run can be
                resumed from it with `scripts/run_stage.py --session`.
        """
        def emit(n, name, status, summary=None):
            if on_stage:
                try:
                    on_stage(n, name, status, summary)
                except Exception:  # noqa: BLE001 - a broken sink must not kill the run
                    pass

        state: JsonDict = {
            "status": "running",
            "stage_reached": "problem_definition",
            "brief": None,
            "data_package": None,
            "eda_report": None,
            "question_results": [],
            "monitoring": None,
            "errors": [],
        }

        if session_dir is None:
            session_dir = tempfile.mkdtemp(prefix="fv-run-")
        session = Session.create(session_dir, csv_path=csv_path,
                                 data_sources=list(data_sources or []))
        session.state["goal"] = payload
        session.state["max_questions"] = max_questions
        session.state["date_format"] = date_format
        session.state["registry_path"] = registry_path or self._default_registry_path(
            csv_path or self._first_source_path(data_sources))
        session.save()
        state["session_dir"] = session.path

        for key in STAGE_KEYS:
            if key in OPTIONAL_STAGES:
                continue          # prediction is opt-in; features has no agent yet
            stage = STAGE_BY_KEY[key]
            state["stage_reached"] = key
            try:
                entry = stages.run_stage(session, key)
            except stages.StageBlocked as blocked:
                emit(stage["n"], stage["label"], "blocked", str(blocked))
                return self._halt_from(state, session, blocked)
            except Exception as exc:  # noqa: BLE001
                # The report is the one stage allowed to fail without sinking
                # the run: every number is already computed and stored.
                if key != "report":
                    raise
                state["errors"].append(f"Report generation failed: {exc}")
                state["report"] = None
                emit(stage["n"], stage["label"], "blocked", "Report generation failed.")
                break
            self._absorb(state, session, key)
            emit(stage["n"], stage["label"],
                 self._emit_status(key, session), entry.get("summary"))

        state["errors"].extend(session.state.get("errors") or [])
        state["stage_reached"] = "complete"
        state["status"] = "complete"
        state["final_report"] = session.state.get("final_report") or             stages.assemble_report(session)
        state["question_results"] = stages.question_results(session)
        return state

    # ------------------------------------------------- session -> legacy state

    def _absorb(self, state: JsonDict, session: "Session", key: str) -> None:
        """Copy one finished stage's result into the legacy state shape."""
        result = session.result(key)
        if key == "problem":
            state["brief"] = result
        elif key == "clean":
            state["data_package"] = result
        elif key == "eda":
            state["eda_report"] = result
        elif key == "monitor":
            state["monitoring"] = result
        elif key == "report":
            html_path = (result or {}).get("html_path")
            html = ""
            if html_path and os.path.exists(html_path):
                with open(html_path, encoding="utf-8") as fh:
                    html = fh.read()
            state["report"] = {"html": html,
                               "narrative": (result or {}).get("narrative"),
                               "generated_at": (result or {}).get("generated_at")}

    def _emit_status(self, key: str, session: "Session") -> str:
        """"skipped" when a per-question stage had nothing computable to do.

        Read from the session rather than the legacy state, which is only
        assembled once the run finishes — checking it mid-run would report
        every stage as done regardless.
        """
        if key not in ("analyst", "visualize", "insights", "recommend"):
            return "done"
        analysis = session.result("analyst") or {}
        answered, skipped = analysis.get("answered") or [], analysis.get("skipped") or []
        return "skipped" if skipped and not answered else "done"

    def _halt_from(self, state: JsonDict, session: "Session",
                   blocked: "stages.StageBlocked") -> JsonDict:
        """Translate a stage refusal into the legacy halt payload."""
        messages = {
            "problem": ("Problem Definition blocked the request."
                        if blocked.status == "blocked"
                        else "Problem Definition needs clarification before analysis."),
            "schema": "Dynamic Data Processor blocked: schema unavailable.",
            "clean": "Data Engineer blocked: canonical data unavailable.",
        }
        if blocked.stage == "problem":
            state["brief"] = blocked.payload
        elif blocked.stage == "clean":
            state["data_package"] = blocked.payload
        state["errors"].extend(session.state.get("errors") or [])
        return self._halt(
            state, blocked.status,
            messages.get(blocked.stage, f"{blocked.stage} blocked: {blocked}"),
            blocked.detail)

    # ================================================= data-aware question prune

    def _select_answerable_questions(
        self, questions: Sequence[Mapping[str, Any]], df, roles: Mapping[str, str]
    ) -> List[JsonDict]:
        """Salvage + dedupe questions against the columns that actually exist.

        Delegates to the stage registry so the guided walk and this
        non-interactive path prune identically. Kept as a method because it is
        the pruning behaviour callers and tests reach for by name.
        """
        return stages.select_answerable_questions(
            self.analyst, questions, df, roles)

    # ========================================================= hook assembly

    def _alert_hooks(self, brief: Mapping[str, Any]) -> List[JsonDict]:
        """Candidate hooks from the brief's enabled alert flags."""
        alerts = ((brief.get("kpi_framework") or {}).get("alerts")) or {}
        return [dict(stages.ALERT_HOOK_TEMPLATES[flag])
                for flag, enabled in alerts.items()
                if enabled and flag in stages.ALERT_HOOK_TEMPLATES]

    # ================================================================= utils

    def _load_canonical(self, data_package: Mapping[str, Any], state: JsonDict):
        """Read the masked canonical parquet once. None if unavailable (agents
        then fall back to their own path read, or block)."""
        path = data_package.get("canonical_df_path")
        if not path or not os.path.exists(path):
            return None
        try:
            import pandas as pd
            return pd.read_parquet(path)
        except Exception as exc:  # noqa: BLE001 - downstream agents handle a None df
            state["errors"].append(f"Could not pre-load canonical frame: {exc}")
            return None

    def _slim_package(self, data_package: Mapping[str, Any]) -> JsonDict:
        """A compact view of the DataPackage for the session report (drops nothing
        the pipeline needs — the full dict is still used internally)."""
        qr = data_package.get("quality_report") or {}
        return {
            "status": data_package.get("status"),
            "row_count": data_package.get("row_count"),
            "canonical_df_path": data_package.get("canonical_df_path"),
            "canonical_columns": data_package.get("canonical_columns"),
            "known_issues": qr.get("known_issues", []),
            "source_summary": data_package.get("source_summary", []),
            "relationship_summary": data_package.get("relationship_summary", {}),
            "multi_source_summary": data_package.get("multi_source_summary", {}),
            "domain_metrics": data_package.get("domain_metrics", {}),
        }

    def _default_registry_path(self, csv_path: str) -> str:
        base = os.path.dirname(os.path.abspath(csv_path)) if csv_path else os.getcwd()
        return os.path.join(base, "monitoring_registry.json")

    @staticmethod
    def _first_source_path(data_sources: Optional[Sequence[Mapping[str, Any]]]) -> str:
        if not data_sources:
            return ""
        first = data_sources[0]
        return str(first.get("path_or_query") or first.get("path") or "")

    def _halt(self, state: JsonDict, status: str, message: str,
              detail: Any = None) -> JsonDict:
        state["status"] = status
        state["message"] = message
        if detail:
            state["detail"] = detail
        return state
