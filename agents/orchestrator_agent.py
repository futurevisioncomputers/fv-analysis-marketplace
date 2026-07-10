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
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

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
    ) -> JsonDict:
        """Execute the pipeline for one request. Returns a session_state report.

        Args:
            payload: user_question string or /goal JSON (Agent 1 input).
            csv_path: legacy single source CSV for Agent 2.
            data_sources: optional multi-source list for CSV / Excel-sheet runs.
            registry_path: JSON hook registry for Agent 7. Defaults to a path
                beside the CSV; pass an explicit path to persist across runs.
            date_format: optional date hint forwarded to Agent 2.
            max_questions: cap on business questions analyzed (None = all).
            on_stage: optional callback fired as each agent finishes, with
                (stage_n, name, status, summary). `stage_n` is the rail key the
                UI uses ("1".."7"); `status` is "done"/"skipped"/"blocked";
                `summary` is a one-line note of the work just done. Used to stream
                live progress to the UI; None for a plain blocking run.
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

        # --- Stage 1: Problem Definition -------------------------------------
        brief = self.problem_definition.run(payload)
        state["brief"] = brief
        if brief.get("status") == "blocked":
            emit("1", "Problem Definition", "blocked", "Request blocked at scoping.")
            return self._halt(state, "blocked",
                              "Problem Definition blocked the request.",
                              brief.get("clarifying_questions"))
        if brief.get("status") == "needs_clarification":
            emit("1", "Problem Definition", "blocked", "Needs clarification before analysis.")
            return self._halt(state, "needs_clarification",
                              "Problem Definition needs clarification before analysis.",
                              brief.get("clarifying_questions"))

        # --- Stage 2.5: Dynamic Data Processor (NEW) -----------------------
        state["stage_reached"] = "dynamic_data_processor"
        if data_sources is None:
            data_sources = [
                {"name": "primary", "type": "csv", "path": csv_path,
                 "path_or_query": csv_path, "domain": "single"}
            ]
        data_source_plan = self.dynamic_data_processor.run(brief, data_sources)
        if data_source_plan.get("status") == "blocked":
            emit("1", "Problem Definition", "done", self._stage_summary("1", state))
            emit("2.5", "Dynamic Data Processor", "blocked", "Schema validation failed.")
            capability = data_source_plan.get("capability_report") or []
            reasons = [q.get("reason") for q in capability if q.get("reason")]
            return self._halt(state, "blocked",
                              "Dynamic Data Processor blocked: schema unavailable.",
                              reasons)
        emit("1", "Problem Definition", "done", self._stage_summary("1", state))
        emit("2.5", "Dynamic Data Processor", "done", "Schema validated; join plan ready.")
        # Use adapted brief if available; fall back to original
        adapted_brief = data_source_plan.get("adapted_brief") or brief
        # "institute" (default) or "generic" — when the sheet doesn't match the
        # institute schema, the Analyst analyzes the columns that DO exist.
        dataset_mode = data_source_plan.get("dataset_mode", "institute")

        # --- Stage 2: Data Engineer -----------------------------------------
        state["stage_reached"] = "data_engineer"
        if len(data_sources) > 1 or any(s.get("type") == "excel_sheet" for s in data_sources):
            data_package = self.data_engineer.run_sources(
                adapted_brief, data_sources,
                join_plan=data_source_plan.get("join_plan") or [],
                date_format=date_format,
            )
        else:
            data_package = self.data_engineer.run(adapted_brief, csv_path, date_format=date_format)
        state["data_package"] = self._slim_package(data_package)
        if data_package.get("status") == "blocked":
            reason = (data_package.get("quality_report") or {}).get("known_issues")
            emit("1", "Problem Definition", "done", self._stage_summary("1", state))
            emit("2", "Data Engineer", "blocked", "Canonical data unavailable.")
            return self._halt(state, "blocked",
                              "Data Engineer blocked: canonical data unavailable.",
                              reason)

        # Load the masked canonical frame ONCE; reuse for every downstream agent.
        df = self._load_canonical(data_package, state)
        # Agents 1, 2.5, and 2 are now settled; stream their summaries.
        emit("2", "Data Engineer", "done", self._stage_summary("2", state))

        # --- Stage 3: EDA ----------------------------------------------------
        state["stage_reached"] = "eda"
        eda_report = self.eda.run(data_package, df=df)
        state["eda_report"] = eda_report
        if eda_report.get("status") == "blocked":
            # EDA is context, not a hard dependency — continue without it.
            state["errors"].append("EDA blocked; continuing without exploration context.")
            eda_report = {}
        emit("3", "EDA", "done", self._stage_summary("3", state))

        # --- Stages 4-6.5: per business question ----------------------------
        state["stage_reached"] = "analysis"
        # Generic sheets use the rewritten (data-driven) question texts from the
        # adapted brief; institute sheets keep the original brief's questions.
        if dataset_mode == "generic":
            questions = (adapted_brief.get("business_questions")
                         or brief.get("business_questions") or [])
        else:
            questions = brief.get("business_questions") or []
        # Data-aware pruning: swap each question to a metric the actual columns
        # support, and drop questions that collapse to a metric+dimension already
        # covered — so the report is not padded with identical record counts.
        questions = self._select_answerable_questions(
            questions, df, data_package.get("canonical_columns") or {}
        )
        if max_questions is not None:
            questions = questions[:max_questions]

        collected_hooks: List[JsonDict] = []
        for question in questions:
            qstate = self._run_question(question, data_package, eda_report, df,
                                        dataset_mode=dataset_mode)
            state["question_results"].append(qstate)
            collected_hooks.extend(qstate.get("monitoring_hooks") or [])

        # The per-question chain (Analyst -> Viz -> Insights -> Recommendation)
        # is complete; stream agents 4..6.5. EDA's anomalies surface via
        # Monitoring, so its summary is emitted after Monitoring evaluates.
        any_ok = any(q["status"] == "ok" for q in state["question_results"])
        all_skipped = bool(state["question_results"]) and not any_ok
        per_q_status = "skipped" if all_skipped else "done"
        for n, name in (("3", "EDA"), ("4", "Analyst"), ("5", "Visualization"),
                        ("6", "Insights"), ("6.5", "Recommendation")):
            if n == "3":
                emit(n, name, "done", self._stage_summary(n, state))
            else:
                emit(n, name, per_q_status, self._stage_summary(n, state))

        # --- Stage 7: Monitoring --------------------------------------------
        state["stage_reached"] = "monitoring"
        registry_path = registry_path or self._default_registry_path(
            csv_path or self._first_source_path(data_sources)
        )
        hooks = collected_hooks + self._alert_hooks(brief)
        self.monitoring.register(hooks, registry_path)
        monitoring = self.monitoring.evaluate(
            data_package, registry_path, eda_report=eda_report or None,
            df=df, problem_brief=brief,
        )
        state["monitoring"] = monitoring
        emit("7", "Monitoring", "done", self._stage_summary("7", state))

        state["stage_reached"] = "complete"
        state["status"] = "complete"
        state["final_report"] = self._assemble_report(state)

        # --- Stage 8: Report Writer -----------------------------------------
        # Compose the shareable HTML report from the contracts just assembled.
        # Deterministic-safe (LLM only phrases); never breaks the run.
        try:
            report = self.report.run(
                state["final_report"], state["question_results"],
                brief=state.get("brief"),
            )
            state["report"] = {"html": report.get("html"),
                               "narrative": report.get("narrative"),
                               "generated_at": report.get("generated_at")}
            emit("8", "Report Writer", "done", "Assembled the HTML report.")
        except Exception as exc:  # noqa: BLE001 - report must never sink the run
            state["errors"].append(f"Report generation failed: {exc}")
            state["report"] = None
        return state

    # ================================================= data-aware question prune

    def _select_answerable_questions(
        self, questions: Sequence[Mapping[str, Any]], df, roles: Mapping[str, str]
    ) -> List[JsonDict]:
        """Salvage + dedupe questions against the columns that actually exist.

        For each question, reorder its metric list so the FIRST metric is one the
        Analyst can compute on this frame (a question asking for a metric the data
        lacks is answered with the next best metric it carries, instead of being
        skipped). Then drop any question that collapses to a metric+dimension pair
        already covered — those would render identical numbers. The user's own
        question (BQ-001) is always kept, never deduped away.

        Falls back to the original list if df is missing (nothing to check against).
        """
        if df is None or not questions:
            return list(questions)

        analyst = self.analyst
        seen: set = set()
        kept: List[JsonDict] = []
        for q in questions:
            metrics = list(q.get("metrics") or [])
            chosen = next(
                (m for m in metrics if analyst.metric_computable(m, df, roles)), None
            )
            if chosen is None:
                # Nothing computable — keep as-is; the Analyst blocks it honestly.
                kept.append(dict(q))
                continue
            q = dict(q)
            if metrics and chosen != metrics[0]:
                q["metrics"] = [chosen] + [m for m in metrics if m != chosen]
            dim = (q.get("dimensions") or ["overall"])[0]
            key = (chosen, dim)
            if key in seen and q.get("question_id") != "BQ-001":
                continue  # redundant: same metric+dimension already answered
            seen.add(key)
            kept.append(q)
        return kept or [dict(q) for q in questions]

    # ====================================================== per-question chain

    def _run_question(self, question, data_package, eda_report, df,
                      dataset_mode="institute") -> JsonDict:
        """Run Analyst -> Viz -> Insights -> Recommendation for one question."""
        qstate: JsonDict = {
            "question_id": question.get("question_id"),
            "question": question.get("question"),
            "module": question.get("module"),
            "status": "ok",
            "analysis": None,
            "visual": None,
            "insight": None,
            "recommendation": None,
            "monitoring_hooks": [],
        }

        result = self.analyst.run(question, data_package, eda_report or None, df=df,
                                  dataset_mode=dataset_mode)
        qstate["analysis"] = result
        if result.get("status") == "blocked":
            # Metric not computable on this data -> record and skip; no fabrication
            # fed downstream.
            qstate["status"] = "skipped_not_computable"
            qstate["skip_reason"] = result.get("reason")
            return qstate

        visual = self.visualization.run(result, data_package, df=df)
        qstate["visual"] = visual

        insight = self.insights.run(
            question, result, visual_package=visual,
            eda_report=eda_report or None, data_package=data_package,
        )
        qstate["insight"] = insight

        recommendation = self.recommendation.run(insight, result, question)
        qstate["recommendation"] = recommendation

        # Hooks for Monitoring come from Insights (and pass-through Recommendation).
        qstate["monitoring_hooks"] = (
            (insight.get("monitoring_hooks") or [])
            + (recommendation.get("monitoring_hooks") or [])
        )
        return qstate

    # ========================================================= hook assembly

    def _alert_hooks(self, brief: Mapping[str, Any]) -> List[JsonDict]:
        """Candidate hooks from the brief's enabled alert flags."""
        alerts = ((brief.get("kpi_framework") or {}).get("alerts")) or {}
        out: List[JsonDict] = []
        for flag, enabled in alerts.items():
            if enabled and flag in ALERT_HOOK_TEMPLATES:
                out.append(dict(ALERT_HOOK_TEMPLATES[flag]))
        return out

    # ============================================================= reporting

    def _assemble_report(self, state: JsonDict) -> JsonDict:
        """Compact, user-facing roll-up of the run (not the full state dump)."""
        answered = [q for q in state["question_results"] if q["status"] == "ok"]
        skipped = [q for q in state["question_results"]
                   if q["status"] != "ok"]

        recs: List[JsonDict] = []
        for q in answered:
            rec = q.get("recommendation") or {}
            recs.extend(rec.get("recommendations") or [])
        recs.sort(key=lambda r: r.get("priority", 999))

        monitoring = state.get("monitoring") or {}
        return {
            "decision_supported": (state["brief"] or {}).get(
                "problem_statement", {}).get("decision_to_support"),
            "questions_answered": len(answered),
            "questions_skipped": len(skipped),
            "skipped": [{"question_id": q["question_id"],
                         "reason": q.get("skip_reason")} for q in skipped],
            "headline_findings": [
                {
                    "question_id": q["question_id"],
                    "metric": (q["analysis"].get("headline_number") or {}).get("metric"),
                    "value": (q["analysis"].get("headline_number") or {}).get("value"),
                    "executive_summary": (q["insight"] or {}).get("executive_summary"),
                }
                for q in answered
            ],
            "top_recommendations": recs[:10],
            "monitoring": {
                "status": monitoring.get("status"),
                "active_alerts": monitoring.get("active_alerts", 0),
                "health": (monitoring.get("health_report") or {}).get("overall_health"),
                "events": monitoring.get("events", []),
            },
            "data_quality": {
                "row_count": (state["data_package"] or {}).get("row_count"),
                "known_issues": (state["data_package"] or {}).get("known_issues", []),
            },
            "multi_source_summary": (state["data_package"] or {}).get(
                "multi_source_summary", {}
            ),
            "sources": (state["data_package"] or {}).get("source_summary", []),
            "relationships": (state["data_package"] or {}).get(
                "relationship_summary", {}
            ),
            "domain_metrics": (state["data_package"] or {}).get("domain_metrics", {}),
            "unjoined_sources": ((state["data_package"] or {}).get(
                "relationship_summary", {}
            ) or {}).get("unjoined_sources", []),
            "agent_summaries": self._summarize_agents(state),
        }

    # Rail stage number -> agent display name (matches the UI's RAIL_STAGES).
    _STAGE_NAMES = {
        "1": "Problem Definition", "2": "Data Engineer", "3": "EDA",
        "4": "Analyst", "5": "Visualization", "6": "Insights",
        "6.5": "Recommendation", "7": "Monitoring",
    }

    def _summarize_agents(self, state) -> JsonDict:
        """All per-agent summaries keyed by rail stage number, computed from the
        final state. Thin wrapper over _stage_summary so the batch report and the
        live SSE stream produce identical text."""
        return {n: self._stage_summary(n, state) for n in self._STAGE_NAMES}

    def _stage_summary(self, n, state) -> Optional[str]:
        """One-line summary of the work agent `n` did this run, derived from the
        real returns the orchestrator holds. Returns None when the stage has no
        data yet (UI then falls back to a generic role line). Safe to call
        mid-run — reads only state already populated by that point."""
        qresults = state.get("question_results") or []
        answered = [q for q in qresults if q.get("status") == "ok"]
        pkg = state.get("data_package") or {}
        eda = state.get("eda_report") or {}
        mon = state.get("monitoring") or {}
        first = answered[0] if answered else None

        def plural(k, word):
            return f"{k} {word}" + ("" if k == 1 else "s")

        if n == "1":
            brief = state.get("brief") or {}
            if not brief:
                return None
            scope = brief.get("scope") or {}
            kpi = brief.get("kpi_framework") or {}
            modules = scope.get("enabled_modules") or []
            n_q = len(brief.get("business_questions") or [])
            n_targets = len(kpi.get("targets") or {})
            tw = scope.get("time_window") or {}
            start, end = tw.get("start_date"), tw.get("end_date")

            parts = []
            if modules:
                parts.append("Scoped " + ", ".join(modules)
                             + (" module" if len(modules) == 1 else " modules"))
            else:
                parts.append("Scoped the request")
            if n_q:
                parts.append(f"framed {plural(n_q, 'business question')}")
            if n_targets:
                parts.append(f"set {plural(n_targets, 'KPI target')}")
            note = " · ".join(parts) + "."
            if start and end:
                note += f" Window {start} → {end}."
            return note

        if n == "2":
            # Loads the raw CSV, cleans it, masks PII, runs data-quality checks.
            rows = pkg.get("row_count")
            if rows is None:
                return None
            cols = len(pkg.get("canonical_columns") or [])
            issues = pkg.get("known_issues") or []
            masked = sum(1 for s in issues if "Masked PII" in str(s))
            parts = [f"Cleaned {rows:,} rows" + (f" × {cols} columns" if cols else "")]
            if masked:
                parts.append(f"masked {plural(masked, 'PII field')}")
            if issues:
                parts.append(f"logged {plural(len(issues), 'quality note')}")
            return " · ".join(parts) + "."

        if n == "3":
            # Profiles distributions/trends and flags statistical anomalies.
            if not eda:
                return None
            dims = len(eda.get("profiled_dimensions") or [])
            nums = len(eda.get("profiled_numerics") or [])
            trend = (eda.get("time_trends") or {}).get("trend_direction")
            anomalies = eda.get("anomalies") or []
            parts = []
            if dims or nums:
                seg = []
                if dims:
                    seg.append(plural(dims, "dimension"))
                if nums:
                    seg.append(plural(nums, "numeric field"))
                parts.append("Profiled " + " and ".join(seg))
            else:
                parts.append("Profiled the data")
            if trend:
                parts.append(f"trend {trend}")
            if anomalies:
                a = anomalies[0]
                metric = str(a.get("metric", "")).replace("_", " ")
                extra = f" (+{len(anomalies) - 1} more)" if len(anomalies) > 1 else ""
                parts.append(f"flagged anomaly on {metric}{extra}")
            else:
                parts.append("no anomalies")
            return " · ".join(parts) + "."

        if n == "4":
            # Computes the headline metric with a CI, breakdowns, and drivers.
            if not first:
                return None
            a = first.get("analysis") or {}
            h = a.get("headline_number") or {}
            conf = (first.get("insight") or {}).get("confidence_score")
            if h.get("metric") is not None and h.get("value") is not None:
                metric = str(h["metric"]).replace("_", " ")
                note = f"Computed {metric} = {self._fmt_num(h['value'])}"
                if conf is not None:
                    note += f" ({conf}% confidence)"
                extras = []
                nb = len(a.get("breakdowns") or [])
                nd = len(a.get("drivers") or [])
                if nb:
                    extras.append(plural(nb, "breakdown"))
                if nd:
                    extras.append(f"top {plural(nd, 'driver')}")
                tail = (" · " + ", ".join(extras)) if extras else ""
                return note + tail + f". {plural(len(answered), 'question')} analyzed."
            return f"Ran analysis on {plural(len(answered), 'question')}."

        if n == "5":
            # Builds dashboard charts, KPI cards, and sections from the analysis.
            if not answered:
                return None
            charts = sum(len((q.get("visual") or {}).get("charts") or []) for q in answered)
            cards = sum(len((q.get("visual") or {}).get("kpi_cards") or []) for q in answered)
            sections = sum(len((q.get("visual") or {}).get("dashboard_sections") or [])
                           for q in answered)
            note = f"Built {plural(charts, 'chart')}, {plural(cards, 'KPI card')}"
            if sections:
                note += f", {plural(sections, 'dashboard section')}"
            return note + "."

        if n == "6":
            # Turns the numbers into findings, root causes, risks, opportunities.
            if not first:
                return None
            ins = first.get("insight") or {}
            health = ins.get("business_health")
            counts = []
            nf = len(ins.get("key_findings") or [])
            nr = len(ins.get("root_causes") or [])
            nk = len(ins.get("risks") or [])
            if nf:
                counts.append(plural(nf, "finding"))
            if nr:
                counts.append(plural(nr, "root cause"))
            if nk:
                counts.append(plural(nk, "risk"))
            top = None
            for f in ins.get("key_findings") or []:
                top = f if isinstance(f, str) else (f or {}).get("finding")
                if top:
                    break
            head = f"Health: {health}. " if health else ""
            body = (f"Wrote {', '.join(counts)}." if counts else "Summarized the results.")
            quote = f" Top: “{self._truncate(top, 80)}”" if top else ""
            return head + body + quote

        if n == "6.5":
            # Proposes prioritized, owner-tagged actions from the insights.
            if not answered:
                return None
            all_recs = []
            for q in answered:
                all_recs.extend((q.get("recommendation") or {}).get("recommendations") or [])
            all_recs.sort(key=lambda r: r.get("priority", 999))
            if not all_recs:
                return "No recommendations generated."
            buckets = {}
            for r in all_recs:
                b = r.get("priority_bucket", "P?")
                buckets[b] = buckets.get(b, 0) + 1
            spread = ", ".join(f"{k}:{v}" for k, v in sorted(buckets.items()))
            top = all_recs[0]
            return (f"Proposed {plural(len(all_recs), 'action')} ({spread}); "
                    f"top: " + self._truncate(top.get("action", ""), 70))

        if n == "7":
            # Registers KPI hooks and evaluates them, raising health alerts.
            hr = mon.get("health_report") or {}
            health = hr.get("overall_health")
            if not health:
                return None
            alerts = mon.get("active_alerts", 0)
            ev = mon.get("events") or []
            note = f"Registered KPI hooks · health {health} · {plural(alerts, 'active alert')}"
            if ev:
                m = str(ev[0].get("metric", "")).replace("_", " ")
                note += f". Latest: {m} {ev[0].get('event_type')}"
            return note + "."

        return None

    @staticmethod
    def _fmt_num(v):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return str(v)
        if float(v).is_integer():
            return f"{int(v):,}"
        return f"{round(float(v), 2):,}"

    @staticmethod
    def _truncate(s, max_len):
        s = str(s)
        return s if len(s) <= max_len else s[: max_len - 1].rstrip() + "…"

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
