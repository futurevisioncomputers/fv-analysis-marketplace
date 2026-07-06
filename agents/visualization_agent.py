"""Agent 5: Visualization.

Turns an `AnalysisResult` (Agent 4) into visual assets: KPI cards, alerts, chart
specifications, and dashboard sections. Consumes the `DataPackage` (Agent 2) for
any extra slicing (trend series, funnel stages). Produces a `VisualPackage`.

Design contract (seven_agent_system_design.md, Agent 5 + the project's expanded
Visualization spec):
- Every visual maps to a specific claim in AnalysisResult (`supports_claim`).
  No decorative charts.
- Agent 5 NEVER analyzes, recommends, forecasts, or invents data. It presents
  only what AnalysisResult / DataPackage already established.
- Style guide (palette / fonts / number format) is enforced here.

Key decision: charts carry **Chart.js-ready config JSON** (`chartjs`), not
pre-rendered PNG paths. The web Ops Center renders them live. `png_path` /
`dashboard_html_path` stay null until a headless export is added.

Conditional emission: KPI cards, alerts, and sections appear only when the data
backs them, so missing dimensions (counsellor, sentiment, cost-per-admission)
simply don't show — never faked.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Mapping, Optional

import pandas as pd


JsonDict = Dict[str, Any]

# Locked style guide (from the UI/UX design-system pass: Fira + WCAG-AA palette).
STYLE_GUIDE: JsonDict = {
    "theme": "fv-institute-ai",
    "palette": {
        "primary": "#1E40AF",
        "secondary": "#3B82F6",
        "accent": "#D97706",
        "success": "#16A34A",
        "warning": "#F59E0B",
        "danger": "#DC2626",
        "neutral": "#64748B",
        "grid": "#E9EEF6",
    },
    "font_family": "Fira Sans",
    "number_font": "Fira Code",
    "value_format": {"rate": "0.0%", "count": "#,##0", "money": "INR #,##0"},
}

# Metric -> how to format its value in cards/labels.
RATE_METRICS = (
    "admission_conversion_rate",
    "counselling_to_admission_rate",
    "dropout_rate",
    "completion_rate",
    "installment_collection_rate",
)
MONEY_METRICS = (
    "gross_fee_collected",
    "pending_fee",
    "overdue_fee",
    "average_fee_per_student",
)


class VisualizationAgent:
    """Builds a VisualPackage from an AnalysisResult + DataPackage."""

    def run(
        self,
        analysis_result: Mapping[str, Any],
        data_package: Optional[Mapping[str, Any]] = None,
        df: Optional[pd.DataFrame] = None,
    ) -> JsonDict:
        """Produce a VisualPackage.

        Args:
            analysis_result: The dict from AnalystAgent (headline, breakdowns,
                comparisons, drivers, caveats).
            data_package: Optional DataPackage (for trend/funnel slicing).
            df: Optional canonical dataframe (skips parquet read).
        """

        if analysis_result.get("status") == "blocked":
            return self._blocked("Upstream AnalysisResult is blocked; nothing to show.")

        df = self._load_df(data_package, df)
        result = analysis_result

        metric = (result.get("headline_number") or {}).get("metric", "")
        kind = self._metric_kind(metric)

        charts: List[JsonDict] = []
        self._add(charts, self._headline_chart(result, metric, kind))
        charts.extend(self._breakdown_charts(result, metric, kind))
        self._add(charts, self._comparison_chart(result, metric, kind))
        self._add(charts, self._trend_chart(df))
        self._add(charts, self._funnel_chart(df))
        self._add(charts, self._driver_chart(result, kind))
        self._assign_ids(charts)

        kpi_cards = self._kpi_cards(result, metric, kind)
        alerts = self._alerts(result, metric, kind)
        sections = self._dashboard_sections(charts, kpi_cards, alerts)

        return {
            "status": "success",
            "question_id": result.get("question_id", ""),
            "theme": STYLE_GUIDE["theme"],
            "style_guide": STYLE_GUIDE,
            "kpi_cards": kpi_cards,
            "alerts": alerts,
            "charts": charts,
            "dashboard_sections": sections,
            "dashboard_html_path": None,
            "dashboard_json_path": None,
            "handoff_agent": "Insights Agent",
        }

    # --------------------------------------------------------------- loading

    def _load_df(self, data_package, df) -> Optional[pd.DataFrame]:
        if df is not None:
            return df
        if not data_package:
            return None
        path = data_package.get("canonical_df_path")
        if path and os.path.exists(path):
            try:
                return pd.read_parquet(path)
            except Exception:  # noqa: BLE001 - charts that need df just get skipped
                return None
        return None

    def _metric_kind(self, metric: str) -> str:
        if metric in RATE_METRICS:
            return "rate"
        if metric in MONEY_METRICS:
            return "money"
        return "count"

    # ----------------------------------------------------------------- charts

    def _headline_chart(self, result, metric, kind) -> Optional[JsonDict]:
        """Bullet-style chart: headline value within its CI band."""
        h = result.get("headline_number") or {}
        if "value" not in h:
            return None
        value = h["value"]
        ci = h.get("ci_95") or [value, value]
        return {
            "type": "bullet",
            "title": self._pretty(metric),
            "subtitle": f"n={h.get('n', 0)}, 95% CI {self._fmt(kind, ci[0])}–{self._fmt(kind, ci[1])}",
            "supports_claim": "headline_number",
            "alt_text": (
                f"{self._pretty(metric)} is {self._fmt(kind, value)} "
                f"(95% confidence {self._fmt(kind, ci[0])} to {self._fmt(kind, ci[1])})."
            ),
            "chartjs": {
                "type": "bar",
                "data": {
                    "labels": [self._pretty(metric)],
                    "datasets": [{"label": "value", "data": [value],
                                  "backgroundColor": STYLE_GUIDE["palette"]["primary"]}],
                },
                "options": {"indexAxis": "y",
                            "plugins": {"legend": {"display": False}}},
            },
            "annotations": {"ci_low": ci[0], "ci_high": ci[1]},
            "table_fallback": [{"metric": metric, "value": value, "ci_95": ci}],
        }

    def _breakdown_charts(self, result, metric, kind) -> List[JsonDict]:
        """One sorted horizontal-bar chart per breakdown dimension."""
        breakdowns = result.get("breakdowns") or []
        if not breakdowns:
            return []
        baseline = (result.get("headline_number") or {}).get("value", 0)
        by_dim: Dict[str, List[JsonDict]] = {}
        for b in breakdowns:
            by_dim.setdefault(b.get("dimension", "segment"), []).append(b)

        charts: List[JsonDict] = []
        for dim, rows in by_dim.items():
            rows = sorted(rows, key=lambda r: r.get("value", 0), reverse=True)
            # Display the source column's real name; `dim` stays the canonical
            # role (recommendation/insights key owner lookups off it).
            dim_label = self._pretty(rows[0].get("dimension_label") or dim)
            labels = [str(r["segment"]).split("=", 1)[-1] for r in rows]
            values = [r.get("value", 0) for r in rows]
            charts.append({
                "type": "bar",
                "title": f"{self._pretty(metric)} by {dim_label}",
                "subtitle": f"vs baseline {self._fmt(kind, baseline)}",
                "supports_claim": "breakdowns",
                "alt_text": self._breakdown_alt(metric, kind, dim_label, rows),
                "chartjs": {
                    "type": "bar",
                    "data": {"labels": labels,
                             "datasets": [{"label": self._pretty(metric), "data": values,
                                           "backgroundColor": STYLE_GUIDE["palette"]["secondary"]}]},
                    "options": {"indexAxis": "y",
                                "plugins": {"legend": {"display": False}}},
                },
                "annotations": {"baseline": baseline},
                "table_fallback": [
                    {"segment": r["segment"], "value": r.get("value"),
                     "n": r.get("n"), "vs_baseline": r.get("vs_baseline")}
                    for r in rows
                ],
            })
        return charts

    def _comparison_chart(self, result, metric, kind) -> Optional[JsonDict]:
        comps = result.get("comparisons") or []
        if not comps:
            return None
        c = comps[0]
        ctype = c.get("type", "comparison").upper()
        return {
            "type": "bar",
            "title": f"{self._pretty(metric)}: {ctype}",
            "subtitle": self._delta_subtitle(c),
            "supports_claim": "comparisons[0]",
            "alt_text": (
                f"{ctype}: current {self._fmt(kind, c.get('current'))} vs prior "
                f"{self._fmt(kind, c.get('prior'))}"
                + (" (significant)." if c.get("significant") else " (not significant).")
            ),
            "chartjs": {
                "type": "bar",
                "data": {"labels": ["Prior", "Current"],
                         "datasets": [{"label": self._pretty(metric),
                                       "data": [c.get("prior"), c.get("current")],
                                       "backgroundColor": [
                                           STYLE_GUIDE["palette"]["neutral"],
                                           STYLE_GUIDE["palette"]["primary"]]}]},
                "options": {"plugins": {"legend": {"display": False}}},
            },
            "annotations": {"delta_pct": c.get("delta_pct"),
                            "significant": c.get("significant")},
            "table_fallback": [c],
        }

    def _trend_chart(self, df) -> Optional[JsonDict]:
        """Line chart of monthly record volume, if a time series exists."""
        if df is None or "event_date" not in getattr(df, "columns", []):
            return None
        if "period_year" not in df.columns or "period_month" not in df.columns:
            return None
        monthly = (
            df.dropna(subset=["period_year", "period_month"])
            .groupby(["period_year", "period_month"])
            .size()
            .reset_index(name="records")
            .sort_values(["period_year", "period_month"])
        )
        if len(monthly) < 2:
            return None
        labels = [f"{int(r.period_year)}-{int(r.period_month):02d}"
                  for r in monthly.itertuples()]
        values = [int(r.records) for r in monthly.itertuples()]
        return {
            "type": "line",
            "title": "Records by month",
            "subtitle": f"{labels[0]} to {labels[-1]}",
            "supports_claim": "headline_number",
            "alt_text": (
                f"Monthly record volume from {labels[0]} to {labels[-1]}; "
                f"ranges {min(values)} to {max(values)} records."
            ),
            "chartjs": {
                "type": "line",
                "data": {"labels": labels,
                         "datasets": [{"label": "records", "data": values,
                                       "borderColor": STYLE_GUIDE["palette"]["primary"],
                                       "fill": False}]},
                "options": {"plugins": {"legend": {"display": False}}},
            },
            "table_fallback": [{"period": p, "records": v}
                               for p, v in zip(labels, values)],
        }

    def _funnel_chart(self, df) -> Optional[JsonDict]:
        """3-stage funnel (enquiry -> admitted -> fee paid), drop-offs as %.

        Only the stages whose flag columns exist are included; never invents the
        missing 8-stage funnel.
        """
        if df is None:
            return None
        cols = getattr(df, "columns", [])
        stages: List[tuple] = []
        if "is_enquiry" in cols:
            stages.append(("Enquiry", int(df["is_enquiry"].fillna(False).sum())))
        else:
            stages.append(("Records", int(len(df))))
        if "is_admitted" in cols:
            stages.append(("Admitted", int(df["is_admitted"].fillna(False).sum())))
        if "is_fee_paid" in cols:
            stages.append(("Fee Paid", int(df["is_fee_paid"].fillna(False).sum())))
        if len(stages) < 2:
            return None

        labels = [s[0] for s in stages]
        values = [s[1] for s in stages]
        dropoffs = []
        for i in range(1, len(values)):
            prev = values[i - 1] or 1
            dropoffs.append(round(1 - values[i] / prev, 4))
        return {
            "type": "funnel",
            "title": ("Admission funnel" if any(l == "Admitted" for l in labels)
                      else "Conversion funnel"),
            "subtitle": " -> ".join(labels),
            "supports_claim": "breakdowns",
            "alt_text": "Funnel " + ", ".join(
                f"{l}={v}" for l, v in zip(labels, values)
            ) + ".",
            "chartjs": {
                "type": "bar",
                "data": {"labels": labels,
                         "datasets": [{"label": "count", "data": values,
                                       "backgroundColor": STYLE_GUIDE["palette"]["primary"]}]},
                "options": {"indexAxis": "y",
                            "plugins": {"legend": {"display": False}}},
            },
            "annotations": {"dropoffs": dropoffs},
            "table_fallback": [{"stage": l, "count": v}
                               for l, v in zip(labels, values)],
        }

    def _driver_chart(self, result, kind) -> Optional[JsonDict]:
        drivers = result.get("drivers") or []
        if not drivers:
            return None
        labels = [str(d.get("factor", "")).split("=", 1)[-1] for d in drivers]
        values = [d.get("contribution", 0) for d in drivers]
        colors = [STYLE_GUIDE["palette"]["success"] if v >= 0
                  else STYLE_GUIDE["palette"]["danger"] for v in values]
        return {
            "type": "bar",
            "title": "Top drivers of the result",
            "subtitle": "signed contribution to the metric",
            "supports_claim": "drivers",
            "alt_text": "Drivers: " + ", ".join(
                f"{l} {v:+.4f}" for l, v in zip(labels, values)
            ) + ".",
            "chartjs": {
                "type": "bar",
                "data": {"labels": labels,
                         "datasets": [{"label": "contribution", "data": values,
                                       "backgroundColor": colors}]},
                "options": {"indexAxis": "y",
                            "plugins": {"legend": {"display": False}}},
            },
            "table_fallback": [{"factor": d.get("factor"),
                                "contribution": d.get("contribution"),
                                "n": d.get("n")} for d in drivers],
        }

    # -------------------------------------------------------------- kpi cards

    def _kpi_cards(self, result, metric, kind) -> List[JsonDict]:
        cards: List[JsonDict] = []
        h = result.get("headline_number") or {}
        if "value" in h:
            comps = result.get("comparisons") or []
            trend, comparison = "", ""
            status = "neutral"
            if comps:
                c = comps[0]
                dp = c.get("delta_pct")
                if dp is not None:
                    trend = f"{dp:+.0%}"
                    comparison = c.get("type", "").upper()
                    status = self._delta_status(metric, dp)
            cards.append({
                "metric": self._pretty(metric),
                "value": self._fmt(kind, h["value"]),
                "raw_value": h["value"],
                "trend": trend,
                "comparison": comparison,
                "confidence": self._confidence(h),
                "status": status,
                "supports_claim": "headline_number",
            })
        return cards

    def _confidence(self, headline) -> str:
        """Qualitative confidence from the CI width relative to the value."""
        ci = headline.get("ci_95")
        val = headline.get("value")
        if not ci or val in (None, 0):
            return "low"
        width = abs(ci[1] - ci[0])
        rel = width / abs(val) if val else 1.0
        if rel <= 0.2:
            return "high"
        if rel <= 0.5:
            return "medium"
        return "low"

    def _delta_status(self, metric: str, delta_pct: float) -> str:
        """Up is good — except for metrics where lower is better."""
        lower_is_better = metric in ("dropout_rate", "pending_fee", "overdue_fee")
        good = delta_pct < 0 if lower_is_better else delta_pct > 0
        if abs(delta_pct) < 0.01:
            return "neutral"
        return "positive" if good else "negative"

    # ----------------------------------------------------------------- alerts

    def _alerts(self, result, metric, kind) -> List[JsonDict]:
        """Visual alerts from facts already in AnalysisResult (no new analysis)."""
        alerts: List[JsonDict] = []
        # Significant adverse comparison.
        for c in result.get("comparisons") or []:
            dp = c.get("delta_pct")
            if dp is None or not c.get("significant"):
                continue
            if self._delta_status(metric, dp) == "negative":
                alerts.append({
                    "severity": "high",
                    "message": (
                        f"{self._pretty(metric)} moved {dp:+.0%} {c.get('type','').upper()} "
                        f"(significant): {self._fmt(kind, c.get('prior'))} -> "
                        f"{self._fmt(kind, c.get('current'))}."
                    ),
                    "supports_claim": "comparisons[0]",
                })
        # Caveats surfaced as low-severity advisories.
        for cav in result.get("caveats") or []:
            alerts.append({
                "severity": "low",
                "message": cav,
                "supports_claim": "caveats",
            })
        return alerts

    # ------------------------------------------------------------- sections

    def _dashboard_sections(self, charts, kpi_cards, alerts) -> List[JsonDict]:
        """Ordered sections (insight-before-chart). Empty sections omitted."""
        sections: List[JsonDict] = []
        if kpi_cards:
            sections.append({"id": "executive_summary", "title": "Executive Summary",
                             "kpi_card_ids": list(range(len(kpi_cards)))})
        if alerts:
            sections.append({"id": "alerts", "title": "Alerts",
                             "alert_count": len(alerts)})

        def chart_ids(claim_prefix):
            return [c["id"] for c in charts
                    if c.get("supports_claim", "").startswith(claim_prefix)
                    or c.get("type") == claim_prefix]

        funnel_ids = [c["id"] for c in charts if c["type"] == "funnel"]
        if funnel_ids:
            sections.append({"id": "funnel", "title": "Funnel Intelligence",
                             "chart_ids": funnel_ids})
        trend_ids = [c["id"] for c in charts if c["type"] == "line"]
        if trend_ids:
            sections.append({"id": "trends", "title": "Trends",
                             "chart_ids": trend_ids})
        breakdown_ids = [c["id"] for c in charts
                         if c.get("supports_claim") == "breakdowns" and c["type"] == "bar"]
        if breakdown_ids:
            sections.append({"id": "breakdowns", "title": "Breakdowns",
                             "chart_ids": breakdown_ids})
        driver_ids = [c["id"] for c in charts if c.get("supports_claim") == "drivers"]
        if driver_ids:
            sections.append({"id": "drivers", "title": "Deep Dive: Drivers",
                             "chart_ids": driver_ids})
        return sections

    # ----------------------------------------------------------------- format

    def _fmt(self, kind: str, value: Any) -> str:
        if value is None:
            return "—"
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

    def _pretty(self, name: str) -> str:
        return name.replace("_", " ").strip().title() if name else "Metric"

    def _delta_subtitle(self, c) -> str:
        dp = c.get("delta_pct")
        sig = " · significant" if c.get("significant") else ""
        return (f"{dp:+.0%}{sig}" if dp is not None else "no prior baseline")

    def _breakdown_alt(self, metric, kind, dim, rows) -> str:
        parts = [f"{str(r['segment']).split('=',1)[-1]} {self._fmt(kind, r.get('value'))}"
                 for r in rows[:5]]
        return f"{self._pretty(metric)} by {dim}: " + ", ".join(parts) + "."

    # ------------------------------------------------------------------ util

    def _add(self, charts: List[JsonDict], chart: Optional[JsonDict]) -> None:
        if chart is not None:
            charts.append(chart)

    def _assign_ids(self, charts: List[JsonDict]) -> None:
        for i, c in enumerate(charts, start=1):
            c["id"] = f"chart_{i}"
            c.setdefault("png_path", None)

    def _blocked(self, reason: str) -> JsonDict:
        return {
            "status": "blocked",
            "reason": reason,
            "theme": STYLE_GUIDE["theme"],
            "style_guide": STYLE_GUIDE,
            "kpi_cards": [],
            "alerts": [],
            "charts": [],
            "dashboard_sections": [],
            "dashboard_html_path": None,
            "dashboard_json_path": None,
            "handoff_agent": "Insights Agent",
        }


if __name__ == "__main__":
    import json
    import sys

    from data_engineer_agent import DataEngineerAgent
    from eda_agent import EDAAgent
    from analyst_agent import AnalystAgent

    if len(sys.argv) < 2:
        print("usage: python visualization_agent.py <source.csv> [metric]", file=sys.stderr)
        raise SystemExit(2)

    metric = sys.argv[2] if len(sys.argv) > 2 else "admissions_confirmed"
    package = DataEngineerAgent(output_dir=".").run(brief={}, csv_path=sys.argv[1])
    EDAAgent().run(package)
    brief = {"metric": metric, "dimensions": ["branch", "course", "source"],
             "comparison": {"type": "yoy"}, "time_window": {}}
    result = AnalystAgent().run(brief, package)
    vis = VisualizationAgent().run(result, package)
    print(json.dumps(vis, indent=2, default=str))
