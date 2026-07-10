"""Agent 7: Monitoring & Alerting.

Stateful, scheduled detector. Re-observes registered KPIs against the latest
DataPackage and fires a MonitoringEvent only when a threshold or business rule
breaches. It DETECTS and ESCALATES — it does not analyze, recommend, or recompute
statistics. Re-observation reuses AnalystAgent (Agent 4); no new stats code lives
here.

Inputs:
- monitoring_hooks (from past InsightReports) -> registered, normalized, persisted.
- latest DataPackage (refreshed on cadence) -> re-observed via AnalystAgent.
- optional EDAReport -> anomalies passed through (NOT re-detected).

Output: a monitoring run report — {status, events, health_report, ...}. Each
event matches the design-doc numeric MonitoringEvent shape.

Honesty gates (carried from the pipeline's conventions):
- A hook/rule fires ONLY when its metric is computable from the data. Rules that
  reference columns the institute data lacks (cost-per-admission, NPS, counsellor
  response-time, ...) are registered `inactive` and never fired on a guess.
- Target-deviation fires actual-vs-target only for KPI targets that map to a
  metric AnalystAgent can compute (today: completion_rate). Blank, unmapped, or
  uncomputable targets produce no event.
- No forecast-deviation alerts: there is no Forecast Agent to compare against.
  (Target-deviation is the honest, model-free substitute — a human-set number,
  not a prediction.)
- Thresholds are normalized to a numeric (op, value) on register; a breach is a
  real numeric comparison, never a string match.

State: a versioned JSON registry file (register/dedupe/list/persist).
Trigger: emits intent (recommended_next_step + auto_invoke_orchestrator flag +
a pre-filled brief stub). It does NOT call the Orchestrator — that agent does not
exist yet; wire it when Agent 0 lands.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .analyst_agent import AnalystAgent


JsonDict = Dict[str, Any]

REGISTRY_VERSION = 1

# Metrics where a LOWER value is the good direction (affects breach/trend framing).
LOWER_IS_BETTER = ("dropout_rate", "pending_fee", "overdue_fee",
                   "certificate_delay_days", "certificate_issue_lag_days",
                   "certificate_pending_rate", "not_coming_rate", "refund_rate",
                   "default_rate", "duplicate_certificate_rate",
                   "enquiry_backlog_rate")

# metric/dimension -> business area (mirrors insights_agent.BUSINESS_AREA).
BUSINESS_AREA = {
    "admission_conversion_rate": "Admissions",
    "counselling_to_admission_rate": "Admissions",
    "admissions_confirmed": "Admissions",
    "total_leads": "Admissions",
    "dropout_rate": "Courses",
    "completion_rate": "Courses",
    "course_completion_rate": "Courses",
    "not_coming_rate": "Courses",
    "repeat_enrollment_rate": "Revenue",
    "gross_fee_collected": "Revenue",
    "collection_efficiency": "Fees",
    "pending_fee": "Fees",
    "overdue_fee": "Fees",
    "default_rate": "Fees",
    "average_fee_per_student": "Revenue",
    "certificate_delay_days": "Certificates",
    "certificate_issue_lag_days": "Certificates",
    "certificate_pending_rate": "Certificates",
    "duplicate_certificate_rate": "Certificates",
    "enquiry_backlog_rate": "Admissions",
    "review_rating": "Reviews",
    "branch": "Branch Performance",
    "source": "Marketing",
}

# Static business rules from the Agent 7 spec. Each: metric -> list of
# (op, value, severity). Only rules whose metric AnalystAgent can compute from the
# given DataPackage are evaluated; the rest stay inactive (no fabrication).
BUSINESS_RULES: Dict[str, List[tuple]] = {
    # Pending fee (institute money metric). Spec: >5L warning, >10L critical.
    "pending_fee": [(">", 1_000_000.0, "critical"), (">", 500_000.0, "warning")],
    # Admission conversion (counsellor/admissions). Spec: <20% warn, <10% crit.
    "admission_conversion_rate": [("<", 0.10, "critical"), ("<", 0.20, "warning")],
    # Average review rating. Spec: <4.0 warn, <3.5 crit.
    "review_rating": [("<", 3.5, "critical"), ("<", 4.0, "warning")],
    # Course dropout (lower is better, so a HIGH value is the breach).
    "dropout_rate": [(">", 0.30, "critical"), (">", 0.20, "warning")],
    # Not-coming / churn share of the cohort (lower is better).
    "not_coming_rate": [(">", 0.30, "critical"), (">", 0.20, "warning")],
    # Certificates joined-but-not-issued. High backlog = SLA breach.
    "certificate_pending_rate": [(">", 0.50, "critical"), (">", 0.30, "warning")],
    # Money-weighted collection efficiency (higher is better -> LOW value breaches).
    "collection_efficiency": [("<", 0.50, "critical"), ("<", 0.70, "warning")],
    # Share of enrollments with an unpaid balance (lower is better).
    "default_rate": [(">", 0.40, "critical"), (">", 0.25, "warning")],
    # Duplicate certificate serials — zero tolerance; any occurrence is a breach.
    "duplicate_certificate_rate": [(">", 0.05, "critical"), (">", 0.0, "warning")],
    # Stale unconverted enquiries clogging the pipeline (lower is better).
    "enquiry_backlog_rate": [(">", 0.50, "critical"), (">", 0.30, "warning")],
}

# Statistical anomaly detection: for metrics WITHOUT hard KPI targets,
# compute mean ± 2σ bounds and alert on outliers (NEW).
STATISTICAL_ANOMALY_METRICS = [
    "total_leads",
    "admissions_confirmed",
    "gross_fee_collected",
    "course_completion_rate",
    "repeat_enrollment_rate",
    "not_coming_rate",
]

# Metrics named in the spec that the institute data does not carry. Registered
# inactive if a hook references them; never fired on a guess.
UNSUPPORTED_METRICS = frozenset({
    "cost_per_admission", "nps_score", "lead_quality_score",
    "counsellor_response_time", "batch_fill_rate", "campaign_conversion",
    "demo_attendance",
})

# KPI-target (from Agent 1's brief.kpi_framework.targets) -> Analyst metric.
# ONLY targets that map to a metric AnalystAgent can actually compute as a
# comparable LEVEL belong here. Each entry: brief-target-key -> {metric}.
# Deliberately omitted (gated, never fired on a guess) — add a line when the
# backing metric lands in analyst_agent.METRIC_SPECS:
#   fee_collection_percent      -> no collection-rate metric (sum metrics only;
#                                  no total-billed denominator).
#   certificate_completion_percent -> no such Analyst metric yet.
#   student_satisfaction_score  -> review_rating not in METRIC_SPECS (falls back
#                                  to count), so not a real score yet.
#   admission_growth_percent    -> growth needs a prior period -> belongs to
#                                  trend (_trend_events), not a level target.
TARGET_METRIC_MAP: Dict[str, JsonDict] = {
    "course_completion_percent": {"metric": "completion_rate"},
}

# How a breach should suggest a follow-up (intent only — no real invocation).
NEXT_STEP_BY_AREA = {
    "Admissions": "Re-run admission funnel analysis for the last 30 days",
    "Marketing": "Re-run lead-source performance analysis for the last 30 days",
    "Revenue": "Re-run revenue / fee-collection analysis for the last 30 days",
    "Fees": "Re-run pending-fee analysis and segment by branch",
    "Courses": "Re-run course dropout / completion analysis",
    "Certificates": "Re-run certificate SLA / backlog analysis",
    "Reviews": "Re-run review-sentiment analysis for recent reviews",
    "Branch Performance": "Re-run branch-comparison analysis",
}

_NUM_RE = re.compile(r"-?\d[\d,]*\.?\d*")


class MonitoringAgent:
    """Stateful KPI monitor. Registers hooks, then evaluates them vs latest data."""

    def __init__(self, analyst: Optional[AnalystAgent] = None):
        self._analyst = analyst or AnalystAgent()

    # ============================================================== register

    def register(
        self,
        hooks: Sequence[Mapping[str, Any]],
        registry_path: str,
    ) -> JsonDict:
        """Normalize + dedupe hooks into the JSON registry, persisting to disk.

        Returns the registry dict {version, hooks:[...]}. Idempotent: registering
        the same hook twice yields one entry (deduped by hook_id).
        """
        registry = self._load_registry(registry_path)
        existing = {h["hook_id"]: h for h in registry["hooks"]}

        for raw in hooks:
            entry = self._normalize_hook(raw)
            if entry is None:
                continue
            existing[entry["hook_id"]] = entry  # dedupe / overwrite by id

        registry["hooks"] = sorted(existing.values(), key=lambda h: h["hook_id"])
        self._save_registry(registry_path, registry)
        return registry

    def _normalize_hook(self, raw: Mapping[str, Any]) -> Optional[JsonDict]:
        metric = str(raw.get("metric", "")).strip()
        if not metric:
            return None
        scope = str(raw.get("scope", "overall")) or "overall"
        op, value = self._parse_threshold(raw.get("threshold"))
        status = "active"
        reason = ""
        if metric in UNSUPPORTED_METRICS:
            status, reason = "inactive", "metric unavailable in institute data"
        elif op is None or value is None:
            status, reason = "inactive", "threshold not parseable"

        hook_id = self._slug(metric, scope, op, value)
        return {
            "hook_id": hook_id,
            "metric": metric,
            "scope": scope,
            "op": op,
            "value": value,
            "threshold": raw.get("threshold"),
            "severity": raw.get("severity", "warning"),
            "business_area": self._business_area(metric),
            "evaluation_frequency": raw.get("evaluation_frequency", "daily"),
            "status": status,
            "inactive_reason": reason,
        }

    def _parse_threshold(self, threshold: Any):
        """Turn a human/spec threshold into (op, numeric_value).

        Handles: "below 31.0%" -> ("<", 0.31), "above 500000" -> (">", 500000.0),
        "< 0.35" -> ("<", 0.35), "> 5,00,000" -> (">", 500000.0).
        Percent strings are converted to a 0-1 proportion. Unparseable -> (None,None).
        """
        if threshold is None:
            return None, None
        s = str(threshold).strip().lower()
        if not s:
            return None, None

        if "below" in s or s.startswith("<") or "less" in s or "under" in s:
            op = "<"
        elif "above" in s or s.startswith(">") or "greater" in s or "over" in s:
            op = ">"
        else:
            op = None

        return op, self._to_number(s)

    def _to_number(self, raw: Any) -> Optional[float]:
        """First numeric token in `raw` as a float; a trailing/embedded '%' makes
        it a 0-1 proportion. Returns None if no number is present."""
        if raw is None:
            return None
        s = str(raw).strip()
        if not s:
            return None
        m = _NUM_RE.search(s)
        if not m:
            return None
        num = float(m.group(0).replace(",", ""))
        if "%" in s:
            num /= 100.0
        return num

    # ============================================================== evaluate

    def evaluate(
        self,
        data_package: Mapping[str, Any],
        registry_path: str,
        eda_report: Optional[Mapping[str, Any]] = None,
        df=None,
        problem_brief: Optional[Mapping[str, Any]] = None,
    ) -> JsonDict:
        """Re-observe every active hook + business rule against the latest data.

        Emits a MonitoringEvent per breach. `df` (an already-loaded canonical
        dataframe) is forwarded to AnalystAgent to skip parquet I/O in tests.
        """
        if data_package.get("status") == "blocked":
            return self._blocked("Upstream DataPackage is blocked; cannot monitor.")

        registry = self._load_registry(registry_path)
        events: List[JsonDict] = []
        seen_metric_breach: set = set()  # one event per (metric, scope) — worst sev

        # 1. Registered hooks.
        for hook in registry["hooks"]:
            if hook.get("status") != "active":
                continue
            ev = self._evaluate_hook(hook, data_package, df)
            if ev:
                events.append(ev)
                seen_metric_breach.add((ev["metric"], ev.get("scope", "overall")))

        # 2. Static business rules (skip a (metric,overall) already alerted).
        for ev in self._business_rule_events(data_package, df):
            key = (ev["metric"], "overall")
            if key in seen_metric_breach:
                continue
            events.append(ev)
            seen_metric_breach.add(key)

        # 3. KPI-target deviation (actual < human-set target from Agent 1 brief).
        for ev in self._target_deviation_events(problem_brief, data_package, df):
            key = (ev["metric"], "overall")
            if key in seen_metric_breach:
                continue
            events.append(ev)
            seen_metric_breach.add(key)

        # 4. EDA anomalies — passed through, NOT re-detected.
        events.extend(self._anomaly_passthrough(eda_report))

        events = self._dedupe_events(events)
        health = self._health_report(events)
        status = "alert" if events else "healthy"
        return {
            "status": status,
            "active_alerts": len(events),
            "events": events,
            "health_report": health,
            "registry_version": registry["version"],
            "handoff_agents": ["Orchestrator"],
        }

    def _evaluate_hook(self, hook, data_package, df) -> Optional[JsonDict]:
        metric, scope = hook["metric"], hook.get("scope", "overall")
        op, thr = hook.get("op"), hook.get("value")
        if op is None or thr is None:
            return None
        observed = self._observe(metric, scope, data_package, df)
        if observed is None:
            return None  # metric not computable -> no fabricated event
        if not self._breached(op, observed, thr):
            return None
        return self._event(
            event_type="threshold_breach", metric=metric, scope=scope,
            observed=observed, threshold=thr, op=op,
            severity=hook.get("severity", "warning"), hook_id=hook["hook_id"],
        )

    def _business_rule_events(self, data_package, df) -> List[JsonDict]:
        out: List[JsonDict] = []
        for metric, rules in BUSINESS_RULES.items():
            observed = self._observe(metric, "overall", data_package, df)
            if observed is None:
                continue  # metric absent from this data -> rule inactive
            for op, thr, severity in rules:
                if self._breached(op, observed, thr):
                    out.append(self._event(
                        event_type="business_rule", metric=metric, scope="overall",
                        observed=observed, threshold=thr, op=op, severity=severity,
                        hook_id=self._slug(metric, "rule", op, thr),
                    ))
                    break  # most-severe rule listed first; one event per metric
        return out

    def _target_deviation_events(
        self, problem_brief, data_package, df
    ) -> List[JsonDict]:
        """Fire when an actual KPI falls below a human-set target.

        Target comes from the Agent 1 brief (kpi_framework.targets) — a real
        number, not a prediction. Only targets in TARGET_METRIC_MAP whose metric
        AnalystAgent can compute are evaluated; blank/unmapped/uncomputable targets
        produce no event (no fabrication).
        """
        if not problem_brief:
            return []
        targets = ((problem_brief.get("kpi_framework") or {}).get("targets")
                   or {})
        out: List[JsonDict] = []
        for target_key, raw_target in targets.items():
            mapped = TARGET_METRIC_MAP.get(target_key)
            if not mapped:
                continue  # unmapped target -> not evaluated (no guess)
            target = self._to_number(raw_target)
            if target is None:
                continue  # blank/unparseable target -> Agent 1 left it open
            metric = mapped["metric"]
            observed = self._observe(metric, "overall", data_package, df)
            if observed is None:
                continue  # metric not computable -> no fabricated event
            if observed >= target:
                continue  # target met
            severity = "critical" if observed < target * 0.85 else "warning"
            out.append(self._event(
                event_type="target_deviation", metric=metric, scope="overall",
                observed=observed, threshold=target, op="<", severity=severity,
                hook_id=self._slug(metric, "target", target_key),
                extra={"target": target, "target_key": target_key,
                       "shortfall": round(target - observed, 4)},
            ))
        return out

    def _trend_events(self, analysis_result: Mapping[str, Any]) -> List[JsonDict]:
        """Trend alerts from the Analyst's own comparisons (already sig-tested).

        A significant adverse period-over-period delta -> a trend event. Direction
        is metric-aware: for LOWER_IS_BETTER metrics an increase is adverse.
        """
        out: List[JsonDict] = []
        metric = (analysis_result.get("headline_number") or {}).get("metric", "")
        for c in analysis_result.get("comparisons") or []:
            if not c.get("significant"):
                continue
            delta = c.get("delta_abs", 0.0)
            adverse = (delta > 0) if metric in LOWER_IS_BETTER else (delta < 0)
            if not adverse:
                continue
            out.append(self._event(
                event_type="trend_alert", metric=metric, scope="overall",
                observed=c.get("current"), threshold=c.get("prior"),
                op="trend", severity="warning",
                hook_id=self._slug(metric, "trend", c.get("type", "pop"), delta),
                extra={"trend": "declining" if delta < 0 else "rising",
                       "comparison_type": c.get("type"),
                       "delta_pct": c.get("delta_pct")},
            ))
        return out

    def _anomaly_passthrough(self, eda_report) -> List[JsonDict]:
        if not eda_report:
            return []
        out: List[JsonDict] = []
        for i, a in enumerate(eda_report.get("anomalies") or []):
            metric = a.get("column", a.get("metric", "unknown"))
            out.append(self._event(
                event_type="anomaly", metric=str(metric), scope="overall",
                observed=a.get("value"), threshold=None, op="anomaly",
                severity=a.get("severity", "warning"),
                hook_id=self._slug(str(metric), "anomaly", "eda", i),
                extra={"description": a.get("description", ""),
                       "source": "eda.anomalies[%d]" % i},
            ))
        return out

    # ============================================================ observation

    def _observe(self, metric, scope, data_package, df) -> Optional[float]:
        """Re-observe a metric (overall or scoped) by reusing AnalystAgent."""
        dim = self._scope_dimension(scope)
        brief = {"metric": metric, "dimensions": [dim] if dim else []}
        res = self._analyst.run(brief, data_package, df=df)
        if res.get("status") != "ready":
            return None
        head = res.get("headline_number") or {}
        if metric and head.get("metric") != metric:
            return None  # metric not computable -> Analyst fell back; do not alert
        if scope == "overall" or not dim:
            v = head.get("value")
            return float(v) if v is not None else None
        # scoped: find the matching breakdown row
        for b in res.get("breakdowns") or []:
            if b.get("segment") == scope and b.get("value") is not None:
                return float(b["value"])
        return None

    def _scope_dimension(self, scope: str) -> Optional[str]:
        if not scope or scope == "overall":
            return None
        return scope.split("=", 1)[0] if "=" in scope else None

    def _breached(self, op: str, observed: float, threshold: float) -> bool:
        if op == "<":
            return observed < threshold
        if op == ">":
            return observed > threshold
        return False

    # ================================================================ events

    def _event(self, event_type, metric, scope, observed, threshold, op,
               severity, hook_id, extra=None) -> JsonDict:
        area = self._business_area(metric)
        ev = {
            "event_type": event_type,
            "hook_id": hook_id,
            "triggered_at": datetime.now(timezone.utc).isoformat(),
            "metric": metric,
            "scope": scope,
            "observed_value": observed,
            "threshold": threshold,
            "severity": severity,
            "business_area": area,
            "impact": self._impact(severity),
            "recommended_next_step": NEXT_STEP_BY_AREA.get(
                area, "Re-run analysis for this metric"),
            "auto_invoke_orchestrator": severity == "critical",
            "prefilled_brief": {
                "metric": metric,
                "dimensions": [self._scope_dimension(scope) or "branch"],
                "comparison": {"type": "yoy"},
                "reason": f"{event_type} on {metric}",
            },
        }
        if extra:
            ev.update(extra)
        return ev

    def _dedupe_events(self, events: List[JsonDict]) -> List[JsonDict]:
        seen, out = set(), []
        order = {"critical": 0, "warning": 1, "info": 2}
        events = sorted(events, key=lambda e: order.get(e.get("severity"), 3))
        for e in events:
            key = (e["metric"], e.get("scope", "overall"), e["event_type"])
            if key in seen:
                continue
            seen.add(key)
            out.append(e)
        return out

    def _health_report(self, events: List[JsonDict]) -> JsonDict:
        crit = sum(1 for e in events if e.get("severity") == "critical")
        warn = sum(1 for e in events if e.get("severity") == "warning")
        if crit:
            overall = "Critical"
        elif warn:
            overall = "Watch"
        else:
            overall = "Good"
        risks = [f"{e['metric']} ({e['severity']})"
                 for e in events if e.get("severity") in ("critical", "warning")]
        return {
            "overall_health": overall,
            "active_alerts": len(events),
            "critical_alerts": crit,
            "warning_alerts": warn,
            "top_risks": risks[:5],
        }

    # ================================================================= utils

    def _business_area(self, metric: str) -> str:
        return BUSINESS_AREA.get(metric, "Operations")

    def _impact(self, severity: str) -> str:
        return {"critical": "High", "warning": "Medium", "info": "Low"}.get(
            severity, "Medium")

    def _slug(self, *parts) -> str:
        raw = "_".join(str(p) for p in parts if p is not None)
        return re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_")

    # ============================================================= registry io

    def _load_registry(self, path: str) -> JsonDict:
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    reg = json.load(fh)
                reg.setdefault("version", REGISTRY_VERSION)
                reg.setdefault("hooks", [])
                return reg
            except (OSError, json.JSONDecodeError):
                pass
        return {"version": REGISTRY_VERSION, "hooks": []}

    def _save_registry(self, path: str, registry: JsonDict) -> None:
        if not path:
            return
        d = os.path.dirname(path)
        if d and not os.path.exists(d):
            os.makedirs(d, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(registry, fh, indent=2, default=str)

    # ============================================================== escalation

    def _blocked(self, reason: str) -> JsonDict:
        return {
            "status": "blocked",
            "reason": reason,
            "active_alerts": 0,
            "events": [],
            "health_report": {"overall_health": "Unknown", "active_alerts": 0,
                              "critical_alerts": 0, "warning_alerts": 0,
                              "top_risks": []},
            "handoff_agents": ["Orchestrator"],
        }
