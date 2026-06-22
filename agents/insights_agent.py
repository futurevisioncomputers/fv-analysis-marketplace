"""Agent 6: Insights.

Transforms analytical findings into evidence-based business insights. Answers
*what happened / why / where / how-important* — never *what to do*. Actions,
plans, budgets, and forecasts are explicitly out of scope and belong to the
separate Recommendation Agent (Agent 6.5).

Inputs:  AnalysisBrief + DataPackage + EDAReport + AnalysisResult + VisualPackage
Output:  InsightReport
Handoff: Recommendation Agent

Design rules (project Insights-Agent spec, revised plan):
- Insights only. No recommendation/action/plan/budget/forecast keys.
- Every insight cites `evidence_refs` into real upstream paths
  (breakdowns[i], comparisons[i], drivers[i], chart ids, eda.anomalies[i]).
  Never invent a cause not backed by driver/segment/funnel/chi-square evidence.
- 11 typed sections, but each emits ONLY when its data exists (conditional):
  missing dimensions (counsellor/city/trainer) or stages simply don't appear.
- Confidence is inherited from Agent 4's CI width + significance, not guessed.

Pure and deterministic: works off the JSON contracts, never reads the dataframe.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

from . import llm_client


JsonDict = Dict[str, Any]

# Dimension role -> business area label (spec's business-area taxonomy).
BUSINESS_AREA = {
    "branch": "Branch Performance",
    "source": "Marketing",
    "faculty": "Operations",
    "course": "Courses",
    "course_category": "Courses",
    "paid": "Revenue",
    "amount": "Revenue",
    "pending": "Fees",
    "certificate": "Certificates",
    "review": "Reviews",
}

# Metrics where a *lower* value / negative delta is the good direction.
LOWER_IS_BETTER = ("dropout_rate", "pending_fee", "overdue_fee", "certificate_delay_days")

# Materiality floor for calling a breakdown gap a "finding".
RATE_GAP_FLOOR = 0.05         # 5 percentage points for proportions
OPPORTUNITY_RATIO = 1.2       # segment >= 1.2x baseline = an opportunity


class InsightsAgent:
    """Builds an InsightReport from the upstream analysis contracts."""

    def run(
        self,
        analysis_brief: Mapping[str, Any],
        analysis_result: Mapping[str, Any],
        visual_package: Optional[Mapping[str, Any]] = None,
        eda_report: Optional[Mapping[str, Any]] = None,
        data_package: Optional[Mapping[str, Any]] = None,
    ) -> JsonDict:
        """Produce an InsightReport.

        EDA/Visual/DataPackage are optional — sections that need them degrade to
        empty lists rather than raising.
        """

        if analysis_result.get("status") == "blocked":
            return self._blocked("Upstream AnalysisResult is blocked; nothing to interpret.")

        result = analysis_result
        vp = visual_package or {}
        eda = eda_report or {}
        metric = (result.get("headline_number") or {}).get("metric", "")
        kind = self._kind(metric)

        findings = self._key_findings(result, vp, metric, kind)
        root_causes = self._root_causes(result, eda, metric)
        opportunities = self._opportunities(result, metric, kind)
        risks = self._risks(result, metric, kind)
        funnel = self._funnel_insights(vp)
        segments = self._segment_insights(result, kind)
        trends = self._trend_insights(result, eda, metric)
        anomalies = self._anomalies(eda)
        hooks = self._monitoring_hooks(result, metric, kind)
        open_q = self._open_questions(result, findings)

        dq = self._data_quality_score(data_package)
        score = self._confidence_score(result, eda, dq)
        health = self._business_health(result, risks, opportunities, metric, dq)

        # Deterministic summary always computed; the LLM (when configured) rewrites
        # it into tighter prose GROUNDED in these same facts, never adding numbers.
        deterministic_summary = self._executive_summary(
            result, metric, kind, segments, risks, opportunities
        )
        executive_summary = self._llm_or_default_summary(
            deterministic_summary, analysis_brief, metric, kind, result,
            findings, root_causes, risks, opportunities, health,
        )

        return {
            "status": "success",
            "question_id": result.get("question_id", ""),
            "executive_summary": executive_summary,
            "business_health": health,
            "key_findings": findings,
            "root_causes": root_causes,
            "opportunities": opportunities,
            "risks": risks,
            "funnel_insights": funnel,
            "segment_insights": segments,
            "trend_insights": trends,
            "anomalies": anomalies,
            "monitoring_hooks": hooks,
            "open_questions": open_q,
            "confidence_score": score,
            "handoff_agent": "Recommendation Agent",
        }

    # ------------------------------------------------------------- summary

    def _llm_or_default_summary(
        self, deterministic: str, analysis_brief, metric, kind, result,
        findings, root_causes, risks, opportunities, health,
    ) -> str:
        """Ask the LLM for a sharper executive summary, grounded ONLY in the facts
        this agent already computed. Any failure (no key, API error, empty reply)
        falls back to the deterministic summary — the run never depends on it.

        The LLM is given the structured findings/risks/opportunities and the
        headline number; it phrases, it does not compute. We never feed it the raw
        dataframe, so it cannot introduce a number that the pipeline didn't derive.
        """
        if not llm_client.available():
            return deterministic
        try:
            h = result.get("headline_number") or {}
            facts = {
                "business_question": (analysis_brief or {}).get("question")
                                     or (analysis_brief or {}).get("metric") or metric,
                "headline_metric": self._pretty(metric),
                "headline_value": self._fmt(kind, h.get("value")),
                "sample_size": h.get("n"),
                "business_health": health,
                "key_findings": [f.get("finding") for f in findings][:5],
                "root_causes": [c.get("cause") for c in root_causes][:3],
                "risks": [r.get("risk") for r in risks][:3],
                "opportunities": [o.get("opportunity") for o in opportunities][:3],
            }
            import json as _json
            prompt = (
                "You are a business analyst writing the executive summary of an "
                "analytics report for an education institute's leadership.\n\n"
                "Use ONLY the facts in this JSON. Do NOT invent any number, "
                "percentage, segment, or cause that is not present here. If a list "
                "is empty, simply omit that angle.\n\n"
                f"FACTS:\n{_json.dumps(facts, ensure_ascii=False, default=str)}\n\n"
                "Write 2-4 sentences, plain executive prose, no bullet points, no "
                "preamble. Lead with the headline metric and its level, then the "
                "most important finding, then the top risk or opportunity. Return "
                "only the summary text."
            )
            text = llm_client.complete_text(prompt, max_tokens=400, temperature=0.3)
            cleaned = text.strip().strip('"').strip()
            # Guard: a too-short or empty reply is not an improvement.
            return cleaned if len(cleaned) >= 20 else deterministic
        except llm_client.LLMUnavailable:
            return deterministic
        except Exception:  # noqa: BLE001 - never let summary phrasing break the run
            return deterministic

    def _executive_summary(
        self, result, metric, kind, segments, risks, opportunities
    ) -> str:
        h = result.get("headline_number") or {}
        parts: List[str] = []
        if "value" in h:
            parts.append(
                f"{self._pretty(metric)} is {self._fmt(kind, h['value'])} "
                f"(n={h.get('n', 0)})."
            )
        comps = result.get("comparisons") or []
        if comps and comps[0].get("delta_pct") is not None:
            c = comps[0]
            word = "rose" if c["delta_pct"] > 0 else "fell"
            sig = "significant" if c.get("significant") else "not significant"
            parts.append(
                f"It {word} {abs(c['delta_pct']):.0%} {c.get('type','').upper()} ({sig})."
            )
        if opportunities:
            parts.append(opportunities[0]["opportunity"] + ".")
        if risks:
            parts.append("Key risk: " + risks[0]["risk"].lower() + ".")
        return " ".join(parts) if parts else "No headline metric available."

    # ------------------------------------------------------------ findings

    def _key_findings(self, result, vp, metric, kind) -> List[JsonDict]:
        findings: List[JsonDict] = []
        baseline = (result.get("headline_number") or {}).get("value", 0)
        breakdowns = result.get("breakdowns") or []

        # Material breakdown gaps (biggest deviation per direction).
        ranked = sorted(
            breakdowns, key=lambda b: abs(b.get("value", 0) - baseline), reverse=True
        )
        for b in ranked[:3]:
            gap = b.get("value", 0) - baseline
            if not self._material(kind, gap):
                continue
            dim = b.get("dimension", "segment")
            seg = str(b.get("segment", "")).split("=", 1)[-1]
            findings.append({
                "finding": (
                    f"{seg} shows {self._pretty(metric)} of "
                    f"{self._fmt(kind, b.get('value'))} vs {self._fmt(kind, baseline)} "
                    f"overall ({self._signed(kind, gap)})."
                ),
                "business_area": self._area(dim),
                "confidence": self._confidence_from_n(b.get("n", 0)),
                "evidence_refs": self._refs(
                    [self._breakdown_ref(breakdowns, b)],
                    self._chart_ref(vp, "breakdowns"),
                ),
                "direction": self._direction(metric, gap),
                "segment": seg,  # used by open_questions; not a comparison finding
            })

        # Significant comparison.
        for i, c in enumerate(result.get("comparisons") or []):
            if c.get("significant") and c.get("delta_pct") is not None:
                findings.append({
                    "finding": (
                        f"{self._pretty(metric)} moved {c['delta_pct']:+.0%} "
                        f"{c.get('type','').upper()} (statistically significant)."
                    ),
                    "business_area": self._area_for_metric(metric),
                    "confidence": "high",
                    "evidence_refs": [f"comparisons[{i}]"],
                    "direction": self._direction(metric, c["delta_pct"]),
                })
        return findings

    # --------------------------------------------------------- root causes

    def _root_causes(self, result, eda, metric) -> List[JsonDict]:
        causes: List[JsonDict] = []
        drivers = result.get("drivers") or []
        ranked = sorted(drivers, key=lambda d: abs(d.get("contribution", 0)), reverse=True)
        for i, d in enumerate(ranked[:2]):
            factor = str(d.get("factor", "")).split("=", 1)[-1]
            contrib = d.get("contribution", 0)
            causes.append({
                "cause": (
                    f"{factor} contributes {contrib:+.4f} to "
                    f"{self._pretty(metric)} (largest signed share)."
                ),
                "affected_metric": metric,
                "impact": self._impact_label(abs(contrib)),
                # original index in drivers, for evidence integrity
                "evidence_refs": [f"drivers[{drivers.index(d)}]"],
            })
        # Significant EDA cross-tabs are evidenced associations (not actions).
        for i, ct in enumerate(eda.get("cross_tabs") or []):
            if ct.get("significant"):
                causes.append({
                    "cause": (
                        f"{ct.get('dimension_a')} and {ct.get('dimension_b')} are "
                        f"associated (Cramer's V {ct.get('cramers_v')})."
                    ),
                    "affected_metric": metric,
                    "impact": self._impact_label(float(ct.get("cramers_v", 0))),
                    "evidence_refs": [f"eda.cross_tabs[{i}]"],
                })
                break  # one association cause is enough
        return causes

    # -------------------------------------------------------- opportunities

    def _opportunities(self, result, metric, kind) -> List[JsonDict]:
        opps: List[JsonDict] = []
        baseline = (result.get("headline_number") or {}).get("value", 0)
        if not baseline:
            return opps
        for b in result.get("breakdowns") or []:
            val = b.get("value", 0)
            ratio = val / baseline if baseline else 0
            better = (ratio <= (1 / OPPORTUNITY_RATIO)) if metric in LOWER_IS_BETTER \
                else (ratio >= OPPORTUNITY_RATIO)
            if not better:
                continue
            seg = str(b.get("segment", "")).split("=", 1)[-1]
            opps.append({
                "opportunity": (
                    f"{seg} performs {ratio:.1f}x the institute average on "
                    f"{self._pretty(metric)}"
                ),
                "potential_gain": self._gain_band(ratio),
                "confidence": self._confidence_from_n(b.get("n", 0)),
                "evidence_refs": [self._breakdown_ref(result.get("breakdowns") or [], b)],
            })
        return opps[:5]

    # --------------------------------------------------------------- risks

    def _risks(self, result, metric, kind) -> List[JsonDict]:
        risks: List[JsonDict] = []
        # Adverse significant comparison.
        for i, c in enumerate(result.get("comparisons") or []):
            dp = c.get("delta_pct")
            if dp is None:
                continue
            if c.get("significant") and self._direction(metric, dp) == "negative":
                risks.append({
                    "risk": (
                        f"{self._pretty(metric)} is declining {dp:+.0%} "
                        f"{c.get('type','').upper()} and the move is significant"
                    ),
                    "severity": self._severity(abs(dp)),
                    "affected_area": self._area_for_metric(metric),
                    "evidence_refs": [f"comparisons[{i}]"],
                })
        # Underperforming significant segment.
        baseline = (result.get("headline_number") or {}).get("value", 0)
        breakdowns = result.get("breakdowns") or []
        worst = None
        for b in breakdowns:
            gap = b.get("value", 0) - baseline
            if self._direction(metric, gap) == "negative" and self._material(kind, gap):
                if worst is None or abs(gap) > abs(worst.get("value", 0) - baseline):
                    worst = b
        if worst is not None:
            seg = str(worst.get("segment", "")).split("=", 1)[-1]
            risks.append({
                "risk": f"{seg} materially underperforms on {self._pretty(metric)}",
                "severity": self._severity(abs(worst.get("value", 0) - baseline)),
                "affected_area": self._area(worst.get("dimension", "")),
                "evidence_refs": [self._breakdown_ref(breakdowns, worst)],
            })
        return risks

    # ------------------------------------------------------- funnel insights

    def _funnel_insights(self, vp) -> List[JsonDict]:
        """Read drop-offs straight from the Visual funnel chart (no re-derive)."""
        out: List[JsonDict] = []
        for chart in vp.get("charts") or []:
            if chart.get("type") != "funnel":
                continue
            labels = chart.get("chartjs", {}).get("data", {}).get("labels", [])
            dropoffs = (chart.get("annotations") or {}).get("dropoffs", [])
            for i, drop in enumerate(dropoffs):
                if i + 1 >= len(labels):
                    break
                out.append({
                    "stage": f"{labels[i]} -> {labels[i+1]}",
                    "dropoff_rate": round(float(drop), 4),
                    "severity": self._severity(float(drop)),
                    "evidence_refs": [chart.get("id", "")],
                })
        return out

    # ------------------------------------------------------ segment insights

    def _segment_insights(self, result, kind) -> List[JsonDict]:
        out: List[JsonDict] = []
        breakdowns = result.get("breakdowns") or []
        by_dim: Dict[str, List[JsonDict]] = {}
        for b in breakdowns:
            by_dim.setdefault(b.get("dimension", "segment"), []).append(b)
        for dim, rows in by_dim.items():
            best = max(rows, key=lambda r: r.get("value", 0))
            seg = str(best.get("segment", "")).split("=", 1)[-1]
            out.append({
                "segment": best.get("segment", ""),
                "insight": (
                    f"Highest {dim} on this metric at "
                    f"{self._fmt(kind, best.get('value'))} (n={best.get('n', 0)})."
                ),
                "confidence": self._confidence_from_n(best.get("n", 0)),
                "evidence_refs": [self._breakdown_ref(breakdowns, best)],
            })
        return out

    # -------------------------------------------------------- trend insights

    def _trend_insights(self, result, eda, metric) -> List[JsonDict]:
        out: List[JsonDict] = []
        for i, c in enumerate(result.get("comparisons") or []):
            dp = c.get("delta_pct")
            if dp is None:
                continue
            trend = "growing" if dp > 0.02 else "declining" if dp < -0.02 else "stable"
            out.append({
                "metric": metric,
                "trend": trend,
                "strength": "significant" if c.get("significant") else "weak",
                "evidence_refs": [f"comparisons[{i}]"],
            })
        # EDA's own monthly trend direction (independent of the comparison).
        direction = (eda.get("time_trends") or {}).get("trend_direction")
        if direction in ("rising", "declining"):
            out.append({
                "metric": "record_volume",
                "trend": "growing" if direction == "rising" else "declining",
                "strength": "observed",
                "evidence_refs": ["eda.time_trends.trend_direction"],
            })
        return out

    # ------------------------------------------------------------ anomalies

    def _anomalies(self, eda) -> List[JsonDict]:
        out: List[JsonDict] = []
        for i, a in enumerate(eda.get("anomalies") or []):
            if a.get("type") in ("spike", "dip"):
                desc = (
                    f"{a['type'].title()} at {a.get('period')}: "
                    f"{a.get('magnitude_pct', 0):+.0%} vs trailing average"
                )
                impact = "Revenue/capacity risk" if a["type"] == "dip" else "Demand surge"
            elif a.get("type") == "outliers":
                desc = (
                    f"{a.get('count')} outlier value(s) in {a.get('metric')} "
                    f"outside {a.get('bounds')}"
                )
                impact = "Data quality / exceptional cases"
            else:
                continue
            out.append({
                "description": desc,
                "impact": impact,
                "confidence": "high",
                "evidence_refs": [f"eda.anomalies[{i}]"],
            })
        return out[:8]

    # ----------------------------------------------------- monitoring hooks

    def _monitoring_hooks(self, result, metric, kind) -> List[JsonDict]:
        hooks: List[JsonDict] = []
        h = result.get("headline_number") or {}
        ci = h.get("ci_95")
        if metric and ci:
            hooks.append({
                "metric": metric,
                "scope": "overall",
                "threshold": f"below {self._fmt(kind, ci[0])}",
                "severity": "high",
            })
        # Scoped hook per adverse significant segment.
        baseline = h.get("value", 0)
        for b in result.get("breakdowns") or []:
            gap = b.get("value", 0) - baseline
            if self._direction(metric, gap) == "negative" and self._material(kind, gap):
                hooks.append({
                    "metric": metric,
                    "scope": b.get("segment", ""),
                    "threshold": f"below {self._fmt(kind, b.get('value'))}",
                    "severity": "medium",
                })
        return hooks[:6]

    # ------------------------------------------------------- open questions

    def _open_questions(self, result, findings) -> List[JsonDict]:
        q: List[str] = []
        # Negative finding without an evidenced driver -> a why question.
        driver_factors = {
            str(d.get("factor", "")).split("=", 1)[-1]
            for d in result.get("drivers") or []
        }
        for f in findings:
            if f.get("direction") != "negative":
                continue
            seg = f.get("segment")  # only segment findings carry this
            if seg and seg not in driver_factors:
                q.append(f"Why does {seg} underperform — faculty, timing, or demand?")
        # Low-confidence segments are open questions, not conclusions.
        for b in result.get("breakdowns") or []:
            if b.get("low_confidence"):
                seg = str(b.get("segment", "")).split("=", 1)[-1]
                q.append(f"Is the {seg} result reliable, given its small sample?")
        # De-dup, cap.
        seen, uniq = set(), []
        for item in q:
            if item not in seen:
                seen.add(item)
                uniq.append(item)
        return uniq[:6]

    # ----------------------------------------------- health & confidence

    def _business_health(self, result, risks, opportunities, metric, dq) -> str:
        score = 70
        for c in result.get("comparisons") or []:
            dp = c.get("delta_pct")
            if dp is None or not c.get("significant"):
                continue
            if self._direction(metric, dp) == "negative":
                score -= 20
            else:
                score += 15
        for r in risks:
            if r.get("severity") in ("high", "critical"):
                score -= 10
        score += 5 * len([o for o in opportunities
                          if o.get("potential_gain") == "high"])
        # Untrustworthy data caps the rating.
        capped = dq is not None and dq < 50
        if score >= 85 and not capped:
            return "excellent"
        if score >= 70 and not capped:
            return "good"
        if score >= 50:
            return "stable"
        if score >= 30:
            return "concerning"
        return "critical"

    def _confidence_score(self, result, eda, dq) -> int:
        # Average finding-ish confidence proxy: tight CI on headline + sig comps.
        comp_conf = 0.5
        comps = result.get("comparisons") or []
        if comps:
            comp_conf = 0.9 if any(c.get("significant") for c in comps) else 0.4
        eda_conf = 0.5
        cts = eda.get("cross_tabs") or []
        if cts:
            eda_conf = 0.9 if any(c.get("significant") for c in cts) else 0.4
        data_conf = (dq / 100.0) if dq is not None else 0.6
        blended = 0.40 * data_conf + 0.40 * comp_conf + 0.20 * eda_conf
        return int(round(blended * 100))

    def _data_quality_score(self, data_package) -> Optional[float]:
        if not data_package:
            return None
        qr = data_package.get("quality_report") or {}
        orig = qr.get("original_row_count") or 0
        drop = qr.get("drop_count") or 0
        if not orig:
            return None
        kept_frac = 1 - (drop / orig)
        nulls = qr.get("null_rates") or {}
        avg_null = sum(nulls.values()) / len(nulls) if nulls else 0.0
        return round(100 * (0.6 * kept_frac + 0.4 * (1 - avg_null)), 1)

    # ---------------------------------------------------------------- utils

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

    def _material(self, kind: str, gap: float) -> bool:
        if kind == "rate":
            return abs(gap) >= RATE_GAP_FLOOR
        return abs(gap) > 0  # any nonzero gap for counts/money

    def _direction(self, metric: str, delta: float) -> str:
        if abs(delta) < 1e-9:
            return "neutral"
        good = delta < 0 if metric in LOWER_IS_BETTER else delta > 0
        return "positive" if good else "negative"

    def _severity(self, magnitude: float) -> str:
        if magnitude >= 0.5:
            return "critical"
        if magnitude >= 0.3:
            return "high"
        if magnitude >= 0.15:
            return "medium"
        return "low"

    def _impact_label(self, magnitude: float) -> str:
        if magnitude >= 0.3:
            return "high"
        if magnitude >= 0.1:
            return "medium"
        return "low"

    def _gain_band(self, ratio: float) -> str:
        if ratio >= 1.5 or ratio <= 0.5:
            return "high"
        if ratio >= 1.3 or ratio <= 0.7:
            return "medium"
        return "low"

    def _confidence_from_n(self, n: int) -> str:
        if n >= 100:
            return "high"
        if n >= 30:
            return "medium"
        return "low"

    def _area(self, dim: str) -> str:
        return BUSINESS_AREA.get(dim, "Operations")

    def _area_for_metric(self, metric: str) -> str:
        if "admission" in metric or "conversion" in metric or "lead" in metric:
            return "Admissions"
        if "fee" in metric or "revenue" in metric:
            return "Revenue"
        if "certificate" in metric:
            return "Certificates"
        if "dropout" in metric or "completion" in metric:
            return "Courses"
        return "Operations"

    def _breakdown_ref(self, breakdowns: Sequence[JsonDict], b: JsonDict) -> str:
        try:
            return f"breakdowns[{breakdowns.index(b)}]"
        except ValueError:
            return "breakdowns"

    def _chart_ref(self, vp, claim: str) -> Optional[str]:
        for c in vp.get("charts") or []:
            if c.get("supports_claim") == claim:
                return c.get("id")
        return None

    def _refs(self, base: List[str], chart: Optional[str]) -> List[str]:
        refs = [r for r in base if r]
        if chart:
            refs.append(chart)
        return refs

    def _fmt(self, kind: str, value: Any) -> str:
        if value is None:
            return "n/a"
        try:
            v = float(value)
        except (TypeError, ValueError):
            return str(value)
        if kind == "rate":
            return f"{v:.1%}"
        if kind == "money":
            return f"INR {v:,.0f}"
        if v == int(v):
            return f"{int(v):,}"
        return f"{v:,.2f}"

    def _signed(self, kind: str, gap: float) -> str:
        if kind == "rate":
            return f"{gap:+.1%}"
        return f"{gap:+,.0f}"

    def _pretty(self, name: str) -> str:
        return name.replace("_", " ").strip().title() if name else "Metric"

    # ------------------------------------------------------------ escalation

    def _blocked(self, reason: str) -> JsonDict:
        return {
            "status": "blocked",
            "reason": reason,
            "executive_summary": "",
            "business_health": "critical",
            "key_findings": [],
            "root_causes": [],
            "opportunities": [],
            "risks": [],
            "funnel_insights": [],
            "segment_insights": [],
            "trend_insights": [],
            "anomalies": [],
            "monitoring_hooks": [],
            "open_questions": [],
            "confidence_score": 0,
            "handoff_agent": "Recommendation Agent",
        }


if __name__ == "__main__":
    import json
    import sys

    from data_engineer_agent import DataEngineerAgent
    from eda_agent import EDAAgent
    from analyst_agent import AnalystAgent
    from visualization_agent import VisualizationAgent

    if len(sys.argv) < 2:
        print("usage: python insights_agent.py <source.csv> [metric]", file=sys.stderr)
        raise SystemExit(2)

    metric = sys.argv[2] if len(sys.argv) > 2 else "admissions_confirmed"
    pkg = DataEngineerAgent(output_dir=".").run(brief={}, csv_path=sys.argv[1])
    eda = EDAAgent().run(pkg)
    brief = {"metric": metric, "dimensions": ["branch", "course", "source"],
             "comparison": {"type": "yoy"}, "time_window": {}}
    res = AnalystAgent().run(brief, pkg, eda)
    vp = VisualizationAgent().run(res, pkg)
    report = InsightsAgent().run(brief, res, vp, eda, pkg)
    print(json.dumps(report, indent=2, default=str))
