"""Agent 6.5: Recommendation.

Converts the InsightReport (Agent 6) into actionable recommendations — the
"now what" that Insights deliberately withholds. This is the ONLY agent that
proposes actions, owners, effort/risk, and priority.

Inputs:  InsightReport + AnalysisResult (for impact arithmetic) + AnalysisBrief
Output:  RecommendationReport
Handoff: Orchestrator (final response) + Monitoring Agent (registers hooks)

Honesty rules (carried over from the original Agent 6 plan):
- Every recommendation traces to a real insight via `evidence_refs`.
- `expected_impact` is COMPUTED from the result's own numbers (gap-to-baseline x
  segment n) and labelled an estimate with the arithmetic shown. Never a
  free-floating promise, never a forecast.
- A recommendation is emitted ONLY when it has (a) a closable gap or scalable
  lever, (b) a mappable owner, and (c) quantifiable impact. Otherwise the insight
  stays an open question — no fabricated action.

Pure and deterministic: works off JSON contracts, never reads the dataframe.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

from . import llm_client


JsonDict = Dict[str, Any]

# Dimension/area -> owner role accountable for acting on it.
OWNER_BY_DIMENSION = {
    "branch": "branch_manager",
    "source": "marketing_lead",
    "faculty": "academic_head",
    "course": "academics",
    "course_category": "academics",
    "pending": "accounts",
    "amount": "accounts",
    "paid": "accounts",
    "certificate": "operations",
}
OWNER_BY_AREA = {
    "Branch Performance": "branch_manager",
    "Marketing": "marketing_lead",
    "Operations": "operations",
    "Courses": "academics",
    "Revenue": "accounts",
    "Fees": "accounts",
    "Certificates": "operations",
    "Admissions": "admissions_head",
}
DEFAULT_OWNER = "leadership"

# Action category, derived from the segment's dimension or the insight area.
# Matches the institute's functional buckets (spec Step 2).
CATEGORY_BY_DIMENSION = {
    "branch": "Branch Management",
    "source": "Marketing",
    "faculty": "Operations",
    "course": "Courses",
    "course_category": "Courses",
    "pending": "Finance",
    "amount": "Finance",
    "paid": "Revenue",
    "certificate": "Certificates",
    "review": "Student Experience",
}
DEFAULT_CATEGORY = "Operations"

# Implementation horizon, derived from effort (no invented week-by-week roadmap).
TIMELINE_BY_EFFORT = {
    "low": "Short-Term (8-30 Days)",
    "medium": "Medium-Term (1-3 Months)",
    "high": "Long-Term (3+ Months)",
}

# Static effort/risk per action template (no ML, transparent heuristics).
EFFORT_RISK = {
    "close_gap": ("medium", "low"),     # bring a laggard up to baseline
    "scale_lever": ("low", "low"),      # do more of what already works
    "investigate": ("low", "low"),      # diagnose before committing spend
    "reduce_backlog": ("medium", "medium"),
}

# Cost estimates per effort level (hours; used in ROI calculation - NEW).
EFFORT_HOURS = {
    "low": 20,
    "medium": 60,
    "high": 150,
}

# Hourly cost estimate (INR) for resource allocation. Adjustable per institute (NEW).
HOURLY_COST_INR = 1000  # ~USD 12/hour equivalent

# Metrics where lower is the good direction (affects how a gap is framed).
LOWER_IS_BETTER = ("dropout_rate", "pending_fee", "overdue_fee", "certificate_delay_days")


class RecommendationAgent:
    """Builds a RecommendationReport from an InsightReport + AnalysisResult."""

    def run(
        self,
        insight_report: Mapping[str, Any],
        analysis_result: Optional[Mapping[str, Any]] = None,
        analysis_brief: Optional[Mapping[str, Any]] = None,
    ) -> JsonDict:
        """Produce a RecommendationReport.

        AnalysisResult is needed for impact arithmetic (segment n + baseline);
        without it, recommendations are still emitted but impact is qualitative.
        """

        if insight_report.get("status") == "blocked":
            return self._blocked("Upstream InsightReport is blocked; nothing to act on.")

        result = analysis_result or {}
        metric = (result.get("headline_number") or {}).get("metric", "")
        baseline = (result.get("headline_number") or {}).get("value", 0)
        kind = self._kind(metric)
        breakdowns = result.get("breakdowns") or []

        recommendations: List[JsonDict] = []

        # 1. Close gaps on underperforming segments (from risks).
        for risk in insight_report.get("risks") or []:
            rec = self._from_risk(risk, breakdowns, metric, baseline, kind)
            if rec:
                recommendations.append(rec)

        # 2. Scale levers from high-gain opportunities.
        for opp in insight_report.get("opportunities") or []:
            rec = self._from_opportunity(opp, breakdowns, metric, baseline, kind)
            if rec:
                recommendations.append(rec)

        # 3. Reduce certificate backlog if flagged in anomalies/insights.
        rec = self._from_certificate(insight_report, metric)
        if rec:
            recommendations.append(rec)

        # Open questions with no actionable lever become "investigate" actions —
        # the action is to diagnose, not to commit resources.
        investigations = self._investigations(insight_report)

        recommendations = self._dedupe_and_prioritize(recommendations + investigations)

        return {
            "status": "success",
            "question_id": insight_report.get("question_id", ""),
            "executive_action_summary": self._llm_or_default_action_summary(
                self._exec_summary(recommendations), recommendations,
                analysis_brief, metric,
            ),
            "recommendations": recommendations,
            "priority_summary": self._priority_summary(recommendations),
            "monitoring_hooks": list(insight_report.get("monitoring_hooks") or []),
            "handoff_agents": ["Orchestrator", "Monitoring Agent"],
        }

    # ----------------------------------------------------------- generators

    def _from_risk(self, risk, breakdowns, metric, baseline, kind) -> Optional[JsonDict]:
        refs = risk.get("evidence_refs") or []
        b = self._breakdown_for_refs(refs, breakdowns)
        if b is None:
            return None  # risk not tied to a segment we can act on -> skip
        owner = self._owner(b.get("dimension"), risk.get("affected_area"))
        if owner == DEFAULT_OWNER:
            return None
        impact = self._impact(b, baseline, kind)
        if impact is None:
            return None
        seg = self._seg(b)
        effort, rk = EFFORT_RISK["close_gap"]
        return {
            "action": (
                f"Review {seg}'s {self._pretty(metric)} delivery; it trails the "
                f"institute baseline by {self._signed(kind, abs(b.get('value',0)-baseline))}."
            ),
            "expected_impact": impact["text"],
            "impact_basis": impact["basis"],
            "owner_role": owner,
            "effort": effort,
            "risk": rk,
            "rationale_ref": "risks",
            "evidence_refs": refs,
            "_score": impact["basis"]["estimated_delta"],
            "_dim": b.get("dimension"),
            "_area": risk.get("affected_area"),
            "_metric": metric,
            "_kind": kind,
            "_seg_value": b.get("value"),
            "_baseline": baseline,
        }

    def _from_opportunity(self, opp, breakdowns, metric, baseline, kind) -> Optional[JsonDict]:
        if opp.get("potential_gain") not in ("high", "medium"):
            return None
        refs = opp.get("evidence_refs") or []
        b = self._breakdown_for_refs(refs, breakdowns)
        if b is None:
            return None
        owner = self._owner(b.get("dimension"), None)
        if owner == DEFAULT_OWNER:
            return None
        seg = self._seg(b)
        effort, rk = EFFORT_RISK["scale_lever"]
        # Impact of scaling a lever = its positive gap over baseline x its n.
        impact = self._impact(b, baseline, kind, scaling=True)
        return {
            "action": (
                f"Scale what works in {seg}: it outperforms the average on "
                f"{self._pretty(metric)} — replicate its approach in weaker segments."
            ),
            "expected_impact": (impact["text"] if impact
                                else f"{seg} runs above baseline; replicating its "
                                     "approach could lift weaker segments."),
            "impact_basis": (impact["basis"] if impact else {}),
            "owner_role": owner,
            "effort": effort,
            "risk": rk,
            "rationale_ref": "opportunities",
            "evidence_refs": refs,
            "_score": (impact["basis"]["estimated_delta"] if impact else 0.0),
            "_dim": b.get("dimension"),
            "_area": None,
            "_metric": metric,
            "_kind": kind,
            "_seg_value": b.get("value"),
            "_baseline": baseline,
        }

    def _from_certificate(self, insight_report, metric) -> Optional[JsonDict]:
        # Certificate backlog surfaces as a pending/delay anomaly or risk.
        text = " ".join(
            (a.get("description", "") + " " + a.get("impact", ""))
            for a in insight_report.get("anomalies") or []
        ).lower()
        risks_text = " ".join(r.get("risk", "").lower()
                              for r in insight_report.get("risks") or [])
        if "certificate" not in (text + risks_text) and "delay" not in (text + risks_text):
            return None
        effort, rk = EFFORT_RISK["reduce_backlog"]
        return {
            "action": "Clear the certificate backlog; prioritise the oldest pending issues.",
            "expected_impact": "Reduces certificate delay days toward target (qualitative).",
            "impact_basis": {},
            "owner_role": "operations",
            "effort": effort,
            "risk": rk,
            "rationale_ref": "anomalies",
            "evidence_refs": ["anomalies"],
            "_score": 0.0,
            "_dim": "certificate",
            "_area": "Certificates",
            "_metric": metric,
            "_kind": self._kind(metric),
            "_seg_value": None,
            "_baseline": None,
        }

    def _investigations(self, insight_report) -> List[JsonDict]:
        out: List[JsonDict] = []
        effort, rk = EFFORT_RISK["investigate"]
        for q in insight_report.get("open_questions") or []:
            out.append({
                "action": f"Investigate: {q}",
                "expected_impact": "Diagnostic — informs a future targeted action.",
                "impact_basis": {},
                "owner_role": "analytics_team",
                "effort": effort,
                "risk": rk,
                "rationale_ref": "open_questions",
                "evidence_refs": ["open_questions"],
                "_score": -1.0,  # always rank below quantified actions
                "_dim": None,
                "_area": None,
                "_metric": "",
                "_kind": "count",
                "_seg_value": None,
                "_baseline": None,
            })
        return out

    # -------------------------------------------------------------- impact

    def _impact(self, b, baseline, kind, scaling: bool = False) -> Optional[JsonDict]:
        """Estimate impact = closable gap x segment n, with arithmetic shown.

        For a laggard: gap = baseline - value (bring it up to baseline).
        For scaling: gap = value - baseline (the surplus it already delivers).
        Only defined for rate/count metrics where n makes the product meaningful.
        """
        val = b.get("value")
        n = b.get("n")
        if val is None or not n:
            return None
        gap = (val - baseline) if scaling else (baseline - val)
        gap = abs(gap)
        if gap <= 0:
            return None

        if kind == "rate":
            delta = gap * n
            text = (
                f"Closing the {gap:.0%} gap on {self._seg(b)}'s {n} records "
                f"≈ {delta:.0f} more (estimate: {gap:.2f} × {n})."
            )
            basis = {"gap": round(gap, 4), "segment_n": int(n),
                     "estimated_delta": round(delta, 1)}
            return {"text": text, "basis": basis}

        # count / money: a precise figure would require unit economics or a
        # per-segment baseline we don't have (the headline baseline is the grand
        # total, so gap x n is meaningless). Stay qualitative — no fabrication.
        return None

    # ----------------------------------------------------------- prioritize

    def _dedupe_and_prioritize(self, recs: List[JsonDict]) -> List[JsonDict]:
        seen, uniq = set(), []
        for r in recs:
            key = r["action"]
            if key in seen:
                continue
            seen.add(key)
            uniq.append(r)
        uniq.sort(key=lambda r: r.get("_score", 0.0), reverse=True)
        for i, r in enumerate(uniq, start=1):
            r["priority"] = i
            self._decorate(r)
        return uniq

    def _decorate(self, r: JsonDict) -> None:
        """Attach derived fields (category, timeline, quick_win, priority_bucket,
        success_metric) and strip the private `_*` scratch keys. All fields are
        derived from data already on the rec — nothing invented."""
        effort = r.get("effort", "medium")
        risk = r.get("risk", "medium")
        quantified = bool(r.get("impact_basis"))

        r["category"] = self._category(r.get("_dim"), r.get("_area"))
        r["timeline"] = TIMELINE_BY_EFFORT.get(effort, "Medium-Term (1-3 Months)")
        # Quick win: cheap, safe, AND it actually moves a measured number.
        r["quick_win"] = (effort == "low" and risk == "low" and quantified)
        r["priority_bucket"] = self._bucket(quantified, effort, risk)

        sm = self._success_metric(r)
        if sm is not None:
            r["success_metric"] = sm

        for k in ("_score", "_dim", "_area", "_metric", "_kind",
                  "_seg_value", "_baseline"):
            r.pop(k, None)

    def _category(self, dim: Optional[str], area: Optional[str]) -> str:
        if dim and dim in CATEGORY_BY_DIMENSION:
            return CATEGORY_BY_DIMENSION[dim]
        # fall back via the insight's business area
        area_to_cat = {"Branch Performance": "Branch Management",
                       "Marketing": "Marketing", "Revenue": "Revenue",
                       "Fees": "Finance", "Courses": "Courses",
                       "Certificates": "Certificates", "Admissions": "Admissions"}
        if area and area in area_to_cat:
            return area_to_cat[area]
        return DEFAULT_CATEGORY

    def _bucket(self, quantified: bool, effort: str, risk: str) -> str:
        """P1 high-impact/low-risk/low-effort; P2 high-impact/medium-risk;
        P3 medium; P4 everything else (incl. unquantified investigations)."""
        if not quantified:
            return "P4"
        if effort == "low" and risk == "low":
            return "P1"
        if risk in ("low", "medium") and effort in ("low", "medium"):
            return "P2"
        return "P3"

    def _success_metric(self, r: JsonDict) -> Optional[JsonDict]:
        """current = segment's value, target = institute baseline (close the gap).
        Only for rate metrics where the figures are comparable and meaningful."""
        if r.get("_kind") != "rate":
            return None
        cur, base = r.get("_seg_value"), r.get("_baseline")
        if cur is None or base is None:
            return None
        return {
            "metric": self._pretty(r.get("_metric", "")),
            "current": f"{cur:.1%}",
            "target": f"{base:.1%}",
            "measurement_frequency": "Monthly",
        }

    def _llm_or_default_action_summary(
        self, deterministic: str, recs: List[JsonDict], analysis_brief, metric,
    ) -> str:
        """LLM-written leadership action summary, grounded ONLY in the already-
        prioritized recommendations (their actions, owners, impact, priority).
        The recommendation list itself stays deterministic; the LLM only narrates
        it. Any failure falls back to the deterministic one-liner."""
        if not recs or not llm_client.available():
            return deterministic
        try:
            facts = {
                "business_question": (analysis_brief or {}).get("question")
                                     or (analysis_brief or {}).get("metric") or metric,
                "actions": [
                    {
                        "action": r.get("action"),
                        "owner": r.get("owner_role"),
                        "priority_bucket": r.get("priority_bucket"),
                        "expected_impact": r.get("expected_impact"),
                        "quick_win": r.get("quick_win"),
                    }
                    for r in recs[:8]
                ],
            }
            import json as _json
            prompt = (
                "You are an operations advisor briefing an education institute's "
                "leadership on what to do next.\n\n"
                "Use ONLY the recommendations in this JSON. Do NOT invent actions, "
                "owners, numbers, or impact claims not present here.\n\n"
                f"RECOMMENDATIONS:\n{_json.dumps(facts, ensure_ascii=False, default=str)}\n\n"
                "Write 2-3 sentences of plain executive prose: what to prioritise "
                "first and why, who owns it, and what quick wins exist. No bullet "
                "points, no preamble. Return only the summary text."
            )
            text = llm_client.complete_text(prompt, max_tokens=400, temperature=0.3)
            cleaned = text.strip().strip('"').strip()
            return cleaned if len(cleaned) >= 20 else deterministic
        except llm_client.LLMUnavailable:
            return deterministic
        except Exception:  # noqa: BLE001 - phrasing must never break the run
            return deterministic

    def _exec_summary(self, recs: List[JsonDict]) -> str:
        if not recs:
            return "No evidence-backed actions surfaced; insights remain diagnostic."
        quant = [r for r in recs if r.get("impact_basis")]
        qw = [r for r in recs if r.get("quick_win")]
        parts = [f"{len(recs)} recommended action(s)"]
        if quant:
            parts.append(f"{len(quant)} with quantified impact")
        if qw:
            parts.append(f"{len(qw)} quick win(s)")
        top = recs[0]["action"]
        return "; ".join(parts) + f". Top priority: {top}"

    def _priority_summary(self, recs: List[JsonDict]) -> JsonDict:
        out = {"P1": 0, "P2": 0, "P3": 0, "P4": 0}
        for r in recs:
            out[r.get("priority_bucket", "P4")] += 1
        return out

    # ---------------------------------------------------------------- ROI estimation (NEW)

    def _roi_estimate(self, recommendation: JsonDict, breakdowns: List[JsonDict],
                      metric: str, baseline: float, kind: str) -> JsonDict:
        """Estimate ROI for a recommendation: (expected_benefit / cost) as a ratio.
        
        Impact is derived from the recommendation's impact_basis (e.g., segment n + gap).
        Cost is estimated from effort hours and hourly_cost_inr.
        ROI = (annual_impact / cost_inr).
        """
        effort = recommendation.get("effort", "medium")
        hours = EFFORT_HOURS.get(effort, 60)
        cost_inr = hours * HOURLY_COST_INR
        
        # Extract impact basis (e.g., segment delta + n)
        basis = recommendation.get("impact_basis", {})
        estimated_delta = basis.get("estimated_delta", 0.0)
        segment_n = basis.get("segment_n", 1)
        
        # Rough annualized impact (not sophisticated, but transparent)
        if kind == "rate":
            # For rates: (delta % ) × segment_n = estimated headcount/item impact
            annual_impact_units = estimated_delta * segment_n
            annual_impact_inr = annual_impact_units * 50_000  # rough avg fee/student
        elif kind == "money":
            # Already in INR; scale by segment ratio
            annual_impact_inr = estimated_delta * 12  # monthly delta × 12 months
        else:  # count
            annual_impact_inr = estimated_delta * segment_n * 50_000
        
        roi_ratio = annual_impact_inr / cost_inr if cost_inr > 0 else 0
        
        return {
            "effort": effort,
            "effort_hours": hours,
            "cost_inr": cost_inr,
            "annual_impact_inr": annual_impact_inr,
            "roi_ratio": round(roi_ratio, 2),
            "roi_label": (
                f"~{roi_ratio:.0f}:1 ROI (INR {annual_impact_inr:,.0f} benefit / INR {cost_inr:,.0f} cost)"
                if roi_ratio > 0 else "Diagnostic (no quantified ROI)"
            ),
        }

    def _rank_by_roi(self, recommendations: List[JsonDict]) -> List[Dict]:
        """Rank recommendations by ROI ratio (highest first)."""
        ranked = []
        for i, rec in enumerate(recommendations):
            roi = rec.get("roi_estimate", {})
            ranked.append({
                "rank": i + 1,
                "action": rec.get("action", "")[:60] + "...",
                "roi_ratio": roi.get("roi_ratio", 0),
                "priority": rec.get("priority_bucket", "P4"),
            })
        # Sort by ROI desc, then by priority
        ranked.sort(key=lambda x: (-x["roi_ratio"], x["priority"]))
        return ranked[:5]  # Top 5 by ROI

    # ---------------------------------------------------------------- utils

    def _owner(self, dimension: Optional[str], area: Optional[str]) -> str:
        if dimension and dimension in OWNER_BY_DIMENSION:
            return OWNER_BY_DIMENSION[dimension]
        if area and area in OWNER_BY_AREA:
            return OWNER_BY_AREA[area]
        return DEFAULT_OWNER

    def _breakdown_for_refs(
        self, refs: Sequence[str], breakdowns: Sequence[JsonDict]
    ) -> Optional[JsonDict]:
        """Resolve a `breakdowns[i]` evidence ref back to its breakdown row."""
        for ref in refs:
            if ref.startswith("breakdowns[") and ref.endswith("]"):
                try:
                    idx = int(ref[len("breakdowns["):-1])
                except ValueError:
                    continue
                if 0 <= idx < len(breakdowns):
                    return breakdowns[idx]
        return None

    def _seg(self, b: JsonDict) -> str:
        return str(b.get("segment", "")).split("=", 1)[-1] or "this segment"

    def _kind(self, metric: str) -> str:
        rate = ("admission_conversion_rate", "counselling_to_admission_rate",
                "dropout_rate", "completion_rate", "installment_collection_rate")
        money = ("gross_fee_collected", "pending_fee", "overdue_fee",
                 "average_fee_per_student")
        if metric in rate:
            return "rate"
        if metric in money:
            return "money"
        return "count"

    def _signed(self, kind: str, gap: float) -> str:
        if kind == "rate":
            return f"{gap:.1%}"
        return f"{gap:,.0f}"

    def _pretty(self, name: str) -> str:
        return name.replace("_", " ").strip().title() if name else "the metric"

    # ------------------------------------------------------------ escalation

    def _blocked(self, reason: str) -> JsonDict:
        return {
            "status": "blocked",
            "reason": reason,
            "recommendations": [],
            "monitoring_hooks": [],
            "handoff_agents": ["Orchestrator", "Monitoring Agent"],
        }


if __name__ == "__main__":
    import json
    import sys

    from data_engineer_agent import DataEngineerAgent
    from eda_agent import EDAAgent
    from analyst_agent import AnalystAgent
    from visualization_agent import VisualizationAgent
    from insights_agent import InsightsAgent

    if len(sys.argv) < 2:
        print("usage: python recommendation_agent.py <source.csv> [metric]", file=sys.stderr)
        raise SystemExit(2)

    metric = sys.argv[2] if len(sys.argv) > 2 else "admission_conversion_rate"
    pkg = DataEngineerAgent(output_dir=".").run(brief={}, csv_path=sys.argv[1])
    eda = EDAAgent().run(pkg)
    brief = {"metric": metric, "dimensions": ["branch", "course", "source"],
             "comparison": {"type": "yoy"}, "time_window": {}}
    res = AnalystAgent().run(brief, pkg, eda)
    vp = VisualizationAgent().run(res, pkg)
    insights = InsightsAgent().run(brief, res, vp, eda, pkg)
    recs = RecommendationAgent().run(insights, res, brief)
    print(json.dumps(recs, indent=2, default=str))
