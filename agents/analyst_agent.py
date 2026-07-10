"""Agent 4: Analyst.

Answers the *specific* question carried in an `AnalysisBrief` (one business
question from Agent 1) using statistical rigor, consuming the canonical
`DataPackage` (Agent 2) and the `EDAReport` (Agent 3). Produces an
`AnalysisResult`.

Design contract (seven_agent_system_design.md, Agent 4):
- headline_number : the metric value + a 95% confidence interval.
- breakdowns      : the metric by each brief dimension, vs the overall baseline.
- comparisons     : period-over-period (mom/qoq/yoy) delta + significance.
- drivers         : which segments contribute most to the comparison delta.
- caveats         : small samples, missing periods, data gaps.
- methodology     : plain-language description of how the number was computed.

No third-party stats dependency. Everything is computed with pandas/numpy:
- Proportion CIs use the Wilson score interval (good for small n / extreme p).
- Mean CIs use the normal approximation (mean +/- 1.96 * SE).
- Count CIs use the Poisson normal approximation (count +/- 1.96 * sqrt(count)).
- Two-period significance uses a two-proportion / two-sample z-test (|z| >= 1.96).

The Analyst *answers* and *quantifies*; it does not recommend actions (that is
the Insights agent). It states caveats but stops at the number + driver level.
"""

from __future__ import annotations

import math
import os
from typing import Any, Dict, List, Mapping, Optional, Sequence

import numpy as np
import pandas as pd


JsonDict = Dict[str, Any]

Z95 = 1.959963984540054  # 97.5th percentile of standard normal
MIN_SEGMENT_N = 30  # below this a segment value is flagged as low-confidence
MAX_BREAKDOWN_DIMENSIONS = 6
MAX_SEGMENTS_PER_DIMENSION = 25

# How a brief metric maps onto the canonical dataframe. Each entry:
#   kind  : "rate" | "sum" | "mean" | "count"
#   roles : canonical column role(s) the metric needs (besides event_date).
#   flag  : for "rate", the boolean column whose True-rate is the metric.
# Metrics not listed fall back to a plain record count.
METRIC_SPECS: Dict[str, JsonDict] = {
    # admissions
    "admission_conversion_rate": {"kind": "rate", "flag": "is_admitted"},
    "counselling_to_admission_rate": {"kind": "rate", "flag": "is_admitted"},
    "admissions_confirmed": {"kind": "count"},
    "total_leads": {"kind": "count"},
    "qualified_leads": {"kind": "count"},
    "walk_in_count": {"kind": "count"},
    # fee management
    "gross_fee_collected": {"kind": "sum", "role": "paid"},
    "pending_fee": {"kind": "sum", "role": "pending"},
    "overdue_fee": {"kind": "sum", "role": "pending"},
    "average_fee_per_student": {"kind": "mean", "role": "amount"},
    # share of enrollments still carrying an unpaid balance (pending > 0);
    # is_default is set per-enrollment by the Data Engineer.
    "default_rate": {"kind": "rate", "flag": "is_default"},
    # money-weighted collection efficiency = Σ collected / Σ billed. The
    # numerator is an explicit paid column when present, else amount_collected
    # (billed - pending) derived by the Data Engineer; denominator is billed total.
    "collection_efficiency": {
        "kind": "ratio",
        "num_roles": ["paid", "amount_collected"],
        "denom_role": "amount",
    },
    # courses
    "dropout_rate": {"kind": "rate", "flag": "is_cancelled"},
    "completion_rate": {"kind": "rate", "flag": "is_completed"},
    "not_coming_rate": {"kind": "rate", "flag": "is_not_coming"},
    # repeat students (person-level; is_repeat_enrollment is set on the
    # person_id grain by the Data Engineer, so this is a per-row rate).
    "repeat_enrollment_rate": {"kind": "rate", "flag": "is_repeat_enrollment"},
    # certificates
    "certificate_pending_rate": {"kind": "rate", "flag": "is_certificate_pending"},
    "certificate_issue_lag_days": {"kind": "mean", "role": "certificate_delay_days"},
    # share of certificate rows sharing a duplicate serial (integrity red flag).
    "duplicate_certificate_rate": {"kind": "rate", "flag": "is_duplicate_certificate"},
    # admissions pipeline hygiene: share of enquiries stale + unconverted.
    "enquiry_backlog_rate": {"kind": "rate", "flag": "is_enquiry_backlog"},
    # generic time-to-event
    "lead_to_admission_days": {"kind": "mean", "role": "lead_to_admission_days"},
}

# Generic-mode role preferences (used when the sheet isn't institute-shaped).
# Numeric "measure" roles, best first; discovered_num_* are appended at runtime.
GENERIC_MEASURE_ROLES = ("amount", "paid", "pending")
# Dimension-like roles to break a measure down by; discovered_dim_* appended too.
GENERIC_DIM_ROLES = ("branch", "source", "course", "course_category", "faculty",
                     "city", "payment_mode", "status", "segment")
GENERIC_MAX_DIM_CARDINALITY = 50  # a column with more distinct values isn't a dimension

# Metric fallback chain: if primary metric fails, try alternate (NEW).
METRIC_FALLBACK = {
    "admission_conversion_rate": "total_leads",
    "counselling_to_admission_rate": "admissions_confirmed",
    "dropout_rate": "completion_rate",
    "not_coming_rate": "completion_rate",
    "completion_rate": "not_coming_rate",
    "repeat_enrollment_rate": "admissions_confirmed",
    "certificate_pending_rate": "certificate_issue_lag_days",
    "duplicate_certificate_rate": "certificate_pending_rate",
    "enquiry_backlog_rate": "admission_conversion_rate",
    "collection_efficiency": "gross_fee_collected",
    "default_rate": "pending_fee",
    "pending_fee": "gross_fee_collected",
    "overdue_fee": "pending_fee",
}


class AnalystAgent:
    """Builds an AnalysisResult from a brief + DataPackage + EDAReport."""

    def run(
        self,
        analysis_brief: Mapping[str, Any],
        data_package: Mapping[str, Any],
        eda_report: Optional[Mapping[str, Any]] = None,
        df: Optional[pd.DataFrame] = None,
        dataset_mode: str = "institute",
    ) -> JsonDict:
        """Produce an AnalysisResult for one business question.

        Args:
            analysis_brief: One business_question dict from Agent 1 (carries
                `metric`/`metrics`, `dimensions`, `comparison`, `time_window`).
            data_package: The dict from DataEngineerAgent (canonical path + roles).
            eda_report: Optional EDAReport (used to seed driver ranking / caveats).
            df: Optional already-loaded canonical dataframe (skips parquet read).
            dataset_mode: "institute" (default) uses the brief's institute metric +
                dimensions. "generic" (set by the Dynamic Data Processor when the
                sheet doesn't match the institute schema) auto-selects a numeric
                measure and low-cardinality dimensions from the actual columns, so
                an arbitrary CSV still gets a real analysis instead of a block.
        """

        if data_package.get("status") == "blocked":
            return self._blocked("Upstream DataPackage is blocked; cannot analyze.")

        if df is None:
            path = data_package.get("canonical_df_path")
            if not path or not os.path.exists(path):
                return self._blocked(f"Canonical dataframe not found: {path!r}")
            try:
                df = pd.read_parquet(path)
            except Exception as exc:  # noqa: BLE001
                return self._blocked(f"Failed to read canonical parquet: {exc}")

        if len(df) == 0:
            return self._blocked("Canonical dataframe is empty.")

        roles: Dict[str, str] = dict(data_package.get("canonical_columns") or {})

        generic = dataset_mode == "generic"
        if generic:
            metric, spec = self._generic_metric(df, roles)
        else:
            metric = self._pick_metric(analysis_brief)
            spec = METRIC_SPECS.get(metric, {"kind": "count"})

        # Restrict to the requested time window if one is given.
        df, window_note = self._apply_time_window(df, analysis_brief.get("time_window"))
        if len(df) == 0:
            return self._blocked("No rows fall inside the requested time window.")

        series, kind, denom = self._metric_series(df, metric, spec, roles)
        if series is None:
            return self._blocked(
                f"Metric {metric!r} cannot be computed: required column missing."
            )

        headline = self._headline(metric, kind, series, denom)
        dims = (self._generic_dimensions(df, roles, exclude=spec.get("role"))
                if generic else self._resolve_dimensions(analysis_brief, roles, df))
        breakdowns = self._breakdowns(df, series, kind, dims, headline["value"], denom)
        comparisons = self._comparisons(
            df, series, kind, analysis_brief.get("comparison"), denom
        )
        drivers = self._drivers(comparisons, breakdowns, headline["value"])
        caveats = self._caveats(
            series, breakdowns, comparisons, window_note, kind
        )

        return {
            "status": "ready",
            "question_id": analysis_brief.get("question_id", ""),
            "headline_number": headline,
            "breakdowns": breakdowns,
            "comparisons": comparisons,
            "drivers": drivers,
            "caveats": caveats,
            "methodology": self._methodology(metric, kind, dims, comparisons),
        }

    # ------------------------------------------------------------ metric setup

    def _pick_metric(self, brief: Mapping[str, Any]) -> str:
        m = brief.get("metric")
        if isinstance(m, str) and m:
            return m
        metrics = brief.get("metrics")
        if isinstance(metrics, (list, tuple)) and metrics:
            return str(metrics[0])
        return "record_count"

    # ------------------------------------------------------- generic selection

    def _generic_measure_roles(self, roles: Mapping[str, str]) -> List[str]:
        disc = sorted((r for r in roles if r.startswith("discovered_num")),
                      key=lambda r: r)
        return list(GENERIC_MEASURE_ROLES) + disc

    def _generic_dim_roles(self, roles: Mapping[str, str]) -> List[str]:
        disc = sorted((r for r in roles if r.startswith("discovered_dim")),
                      key=lambda r: r)
        return list(GENERIC_DIM_ROLES) + disc

    def _generic_metric(self, df: pd.DataFrame, roles: Mapping[str, str]):
        """Pick the best numeric measure column to SUM. Returns (label, spec).

        Prefers named money roles, then discovered numerics; skips id-like columns
        (near-unique integers). Falls back to a plain record count if no usable
        measure exists, so the analysis still runs."""
        for role in self._generic_measure_roles(roles):
            col = roles.get(role)
            if not col or col not in df.columns:
                continue
            s = pd.to_numeric(df[col], errors="coerce")
            nn = int(s.notna().sum())
            if nn == 0:
                continue
            # Skip identifier-like columns (almost every value distinct + integer).
            nun = int(s.nunique(dropna=True))
            is_int = bool((s.dropna().mod(1) == 0).all())
            if is_int and nun >= max(20, int(0.95 * nn)):
                continue
            return col, {"kind": "sum", "role": col}
        return "record_count", {"kind": "count"}

    def _generic_dimensions(
        self, df: pd.DataFrame, roles: Mapping[str, str], exclude: Optional[str] = None,
    ) -> Dict[str, str]:
        """Low-cardinality columns to break the measure down by, role->column."""
        out: Dict[str, str] = {}
        for role in self._generic_dim_roles(roles):
            col = roles.get(role)
            if not col or col not in df.columns or col == exclude:
                continue
            nun = int(df[col].nunique(dropna=True))
            if nun < 2 or nun > GENERIC_MAX_DIM_CARDINALITY:
                continue
            out[role] = col
            if len(out) >= MAX_BREAKDOWN_DIMENSIONS:
                break
        return out

    def _resolve_col(
        self, name: Optional[str], df: pd.DataFrame, roles: Mapping[str, str]
    ) -> str:
        """A spec entry may name a literal column or a role; resolve to a column."""
        if not name:
            return ""
        if name in df.columns:
            return name
        return roles.get(name, "")

    def _metric_series(
        self,
        df: pd.DataFrame,
        metric: str,
        spec: Mapping[str, Any],
        roles: Mapping[str, str],
    ):
        """Return (per-row series, kind, denom) where kind in rate/value/count/ratio.

        - rate  : 0/1 series; aggregation = mean (a proportion).
        - value : numeric series; aggregation = sum or mean.
        - count : all-ones series; aggregation = sum (a record count).
        - ratio : numerator series + aligned denom series; agg = Σnum / Σdenom.

        `denom` is None for every kind except ratio.
        """
        kind = spec.get("kind", "count")

        if kind == "rate":
            flag = spec.get("flag")
            if flag and flag in df.columns:
                s = df[flag].astype("boolean").astype("float")
                return s.fillna(0.0), "rate", None
            return None, kind, None

        if kind in ("sum", "mean"):
            role = spec.get("role")
            col = role if role in df.columns else roles.get(role or "", "")
            if not col or col not in df.columns:
                return None, kind, None
            s = pd.to_numeric(df[col], errors="coerce")
            return s, ("value_sum" if kind == "sum" else "value_mean"), None

        if kind == "ratio":
            num_col = ""
            for cand in spec.get("num_roles", []):
                num_col = self._resolve_col(cand, df, roles)
                if num_col:
                    break
            denom_col = self._resolve_col(spec.get("denom_role"), df, roles)
            if not num_col or not denom_col:
                return None, kind, None
            num = pd.to_numeric(df[num_col], errors="coerce")
            den = pd.to_numeric(df[denom_col], errors="coerce")
            return num, "ratio", den

        # count
        return pd.Series(np.ones(len(df)), index=df.index), "count", None

    def metric_computable(
        self, metric: str, df: pd.DataFrame, roles: Mapping[str, str]
    ) -> bool:
        """True if `metric` can actually be computed on this frame's columns.

        Used by the orchestrator to prune / salvage questions BEFORE running the
        full analysis, so the report is not padded with questions that would only
        block. A `count` metric is always computable (record count); rate/sum/
        mean/ratio require their backing column(s) to be present.
        """
        spec = METRIC_SPECS.get(metric, {"kind": "count"})
        series, _, denom = self._metric_series(df, metric, spec, roles)
        if series is None:
            return False
        if spec.get("kind") == "ratio":
            y, _x = self._ratio_pair(series, denom)
            return len(y) > 0
        return True

    # --------------------------------------------------------------- headline

    def _aggregate(self, kind: str, s: pd.Series,
                   denom: Optional[pd.Series] = None) -> float:
        if kind == "ratio":
            y, x = self._ratio_pair(s, denom)
            sx = float(x.sum())
            return float(y.sum() / sx) if sx else 0.0
        v = s.dropna()
        if len(v) == 0:
            return 0.0
        if kind == "rate" or kind == "value_mean":
            return float(v.mean())
        return float(v.sum())  # count / value_sum

    def _ratio_pair(self, num: pd.Series, denom: Optional[pd.Series]):
        """Aligned (numerator, denominator) restricted to rows both are present."""
        y = pd.to_numeric(num, errors="coerce")
        x = (pd.to_numeric(denom, errors="coerce")
             if denom is not None else pd.Series(dtype="float64"))
        mask = y.notna() & x.notna()
        return y[mask], x[mask]

    def _headline(self, metric: str, kind: str, s: pd.Series,
                  denom: Optional[pd.Series] = None) -> JsonDict:
        if kind == "ratio":
            y, x = self._ratio_pair(s, denom)
            n = int(len(y))
            value = self._aggregate("ratio", y, x)
            lo, hi = self._ratio_ci(y, x, value)
        else:
            v = s.dropna()
            n = int(len(v))
            value = self._aggregate(kind, s)
            lo, hi = self._ci(kind, v, value, n)
        return {
            "metric": metric,
            "value": round(value, 4),
            "n": n,
            "ci_95": [round(lo, 4), round(hi, 4)],
        }

    def _ratio_ci(self, y: pd.Series, x: pd.Series, R: float):
        """95% CI for a ratio of totals R = Σy/Σx via the ratio-estimator SE.

        SE(R) = sqrt( n/(n-1) * Σ(y - R·x)^2 ) / Σx  — the standard combined-ratio
        variance (no third-party stats dep). Lower bound clipped at 0; upper is
        left open because efficiency can exceed 1 (overpayment)."""
        se = self._ratio_se(y, x, R)
        return max(0.0, R - Z95 * se), R + Z95 * se

    def _ratio_se(self, y: pd.Series, x: pd.Series, R: float) -> float:
        n = int(len(y))
        sx = float(x.sum())
        if n < 2 or sx == 0:
            return 0.0
        resid = y - R * x
        ss = float((resid ** 2).sum()) * n / (n - 1)
        return math.sqrt(ss) / abs(sx)

    def _ci(self, kind: str, v: pd.Series, value: float, n: int):
        """95% CI appropriate to the aggregation kind."""
        if n == 0:
            return 0.0, 0.0
        if kind == "rate":
            return self._wilson(value, n)
        if kind == "value_mean":
            sd = float(v.std(ddof=1)) if n > 1 else 0.0
            se = sd / math.sqrt(n) if n > 0 else 0.0
            return value - Z95 * se, value + Z95 * se
        if kind == "count":
            # Poisson normal approx on the total count.
            half = Z95 * math.sqrt(max(value, 0.0))
            return max(0.0, value - half), value + half
        # value_sum: CI on total = n * mean CI half-width.
        sd = float(v.std(ddof=1)) if n > 1 else 0.0
        se_total = sd * math.sqrt(n)
        return value - Z95 * se_total, value + Z95 * se_total

    def _wilson(self, p: float, n: int):
        """Wilson score interval for a proportion."""
        if n == 0:
            return 0.0, 0.0
        z = Z95
        denom = 1 + z * z / n
        center = (p + z * z / (2 * n)) / denom
        margin = (z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / denom
        return max(0.0, center - margin), min(1.0, center + margin)

    # -------------------------------------------------------------- breakdowns

    def _resolve_dimensions(
        self,
        brief: Mapping[str, Any],
        roles: Mapping[str, str],
        df: pd.DataFrame,
    ) -> Dict[str, str]:
        """role -> column for brief dimensions that exist in the canonical df."""
        wanted = brief.get("dimensions") or []
        out: Dict[str, str] = {}
        for role in wanted:
            col = roles.get(role)
            if col and col in df.columns:
                out[role] = col
            if len(out) >= MAX_BREAKDOWN_DIMENSIONS:
                break
        return out

    def _breakdowns(
        self,
        df: pd.DataFrame,
        series: pd.Series,
        kind: str,
        dims: Mapping[str, str],
        baseline: float,
        denom: Optional[pd.Series] = None,
    ) -> List[JsonDict]:
        results: List[JsonDict] = []
        for role, col in dims.items():
            cols = {"seg": df[col], "val": series}
            if kind == "ratio" and denom is not None:
                cols["den"] = denom
            grp = pd.DataFrame(cols).dropna(subset=["seg"])
            if grp.empty:
                continue
            top_segments = grp["seg"].value_counts().head(MAX_SEGMENTS_PER_DIMENSION)
            for seg in top_segments.index:
                sub = grp.loc[grp["seg"] == seg]
                if kind == "ratio":
                    y, x = self._ratio_pair(sub["val"], sub.get("den"))
                    n = int(len(y))
                    if n == 0:
                        continue
                    value = self._aggregate("ratio", y, x)
                else:
                    vals = sub["val"]
                    n = int(vals.dropna().shape[0])
                    if n == 0:
                        continue
                    value = self._aggregate(kind, vals)
                results.append({
                    "dimension": role,
                    "dimension_label": col,
                    "segment": f"{role}={seg}",
                    "value": round(value, 4),
                    "n": n,
                    "vs_baseline": self._delta_str(kind, value, baseline),
                    "low_confidence": n < MIN_SEGMENT_N,
                })
        # Biggest absolute deviation from baseline first.
        results.sort(
            key=lambda r: abs(r["value"] - baseline), reverse=True
        )
        return results

    def _delta_str(self, kind: str, value: float, baseline: float) -> str:
        d = value - baseline
        if kind in ("rate", "ratio"):
            return f"{d:+.2%}" if abs(d) >= 0.0001 else "+0.00%"
        return f"{d:+.2f}"

    # -------------------------------------------------------------- comparison

    def _comparisons(
        self,
        df: pd.DataFrame,
        series: pd.Series,
        kind: str,
        comparison: Optional[Mapping[str, Any]],
        denom: Optional[pd.Series] = None,
    ) -> List[JsonDict]:
        ctype = (comparison or {}).get("type", "none")
        if ctype in (None, "none", ""):
            return []
        if "event_date" not in df.columns:
            return []

        ev = pd.to_datetime(df["event_date"], errors="coerce")
        cols = {"ev": ev, "val": series}
        if kind == "ratio" and denom is not None:
            cols["den"] = denom
        work = pd.DataFrame(cols).dropna(subset=["ev"])
        if work.empty:
            return []

        current, prior = self._split_periods(work, ctype)
        if current is None or prior is None or len(current) == 0 or len(prior) == 0:
            return []

        if kind == "ratio":
            cur_v = self._aggregate("ratio", current["val"], current.get("den"))
            pri_v = self._aggregate("ratio", prior["val"], prior.get("den"))
            sig = self._ratio_periods_significant(
                current["val"], current.get("den"), prior["val"], prior.get("den"),
                cur_v, pri_v,
            )
        else:
            cur_v = self._aggregate(kind, current["val"])
            pri_v = self._aggregate(kind, prior["val"])
            sig = self._two_period_significant(kind, current["val"], prior["val"])
        delta_pct = (cur_v - pri_v) / pri_v if pri_v not in (0, 0.0) else None

        return [{
            "type": ctype,
            "current": round(cur_v, 4),
            "current_n": int(len(current)),
            "prior": round(pri_v, 4),
            "prior_n": int(len(prior)),
            "delta_abs": round(cur_v - pri_v, 4),
            "delta_pct": round(delta_pct, 4) if delta_pct is not None else None,
            "significant": bool(sig),
        }]

    def _split_periods(self, work: pd.DataFrame, ctype: str):
        """Split rows into (current, prior) windows by comparison type."""
        latest = work["ev"].max()
        if ctype == "yoy":
            cur_lo = latest - pd.DateOffset(years=1)
            pri_lo = latest - pd.DateOffset(years=2)
            current = work[work["ev"] > cur_lo]
            prior = work[(work["ev"] > pri_lo) & (work["ev"] <= cur_lo)]
        elif ctype == "qoq":
            cur_lo = latest - pd.DateOffset(months=3)
            pri_lo = latest - pd.DateOffset(months=6)
            current = work[work["ev"] > cur_lo]
            prior = work[(work["ev"] > pri_lo) & (work["ev"] <= cur_lo)]
        elif ctype == "mom":
            cur_lo = latest - pd.DateOffset(months=1)
            pri_lo = latest - pd.DateOffset(months=2)
            current = work[work["ev"] > cur_lo]
            prior = work[(work["ev"] > pri_lo) & (work["ev"] <= cur_lo)]
        else:
            return None, None
        return current, prior

    def _two_period_significant(
        self, kind: str, cur: pd.Series, pri: pd.Series
    ) -> bool:
        """|z| >= 1.96 for the difference between the two periods."""
        c, p = cur.dropna(), pri.dropna()
        nc, np_ = len(c), len(p)
        if nc < 2 or np_ < 2:
            return False

        if kind == "rate":
            pc, pp = float(c.mean()), float(p.mean())
            pooled = (c.sum() + p.sum()) / (nc + np_)
            se = math.sqrt(pooled * (1 - pooled) * (1 / nc + 1 / np_))
            if se == 0:
                return False
            return abs((pc - pp) / se) >= Z95

        if kind == "count":
            # Compare counts as Poisson rates over equal-length windows.
            cc, cp = float(c.sum()), float(p.sum())
            se = math.sqrt(cc + cp)
            if se == 0:
                return False
            return abs(cc - cp) / se >= Z95

        # means / sums: Welch two-sample z on the means.
        mc, mp = float(c.mean()), float(p.mean())
        vc = float(c.var(ddof=1)) / nc if nc > 1 else 0.0
        vp = float(p.var(ddof=1)) / np_ if np_ > 1 else 0.0
        se = math.sqrt(vc + vp)
        if se == 0:
            return False
        return abs(mc - mp) / se >= Z95

    def _ratio_periods_significant(
        self, cur_y, cur_x, pri_y, pri_x, cur_r: float, pri_r: float
    ) -> bool:
        """|z| >= 1.96 on the difference of two ratios, each with its own SE."""
        cy, cx = self._ratio_pair(cur_y, cur_x)
        py, px = self._ratio_pair(pri_y, pri_x)
        se_c = self._ratio_se(cy, cx, cur_r)
        se_p = self._ratio_se(py, px, pri_r)
        se = math.sqrt(se_c ** 2 + se_p ** 2)
        if se == 0:
            return False
        return abs(cur_r - pri_r) / se >= Z95

    # ----------------------------------------------------------------- drivers

    def _drivers(
        self,
        comparisons: Sequence[JsonDict],
        breakdowns: Sequence[JsonDict],
        baseline: float,
    ) -> List[JsonDict]:
        """Which segments contribute most to the comparison delta.

        Without a per-segment period split we approximate contribution by each
        segment's signed deviation from baseline weighted by its share of n.
        """
        if not breakdowns:
            return []
        total_n = sum(b["n"] for b in breakdowns) or 1
        scored = []
        for b in breakdowns:
            weight = b["n"] / total_n
            contribution = (b["value"] - baseline) * weight
            scored.append((abs(contribution), {
                "factor": b["segment"],
                "contribution": round(contribution, 4),
                "n": b["n"],
            }))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [d for _, d in scored[:5]]

    # ----------------------------------------------------------------- caveats

    def _caveats(
        self,
        series: pd.Series,
        breakdowns: Sequence[JsonDict],
        comparisons: Sequence[JsonDict],
        window_note: str,
        kind: str,
    ) -> List[str]:
        caveats: List[str] = []
        n = int(series.dropna().shape[0])
        if n < MIN_SEGMENT_N:
            caveats.append(
                f"Overall sample is small (n={n}); the headline CI is wide."
            )
        low = [b["segment"] for b in breakdowns if b.get("low_confidence")]
        if low:
            shown = ", ".join(low[:5])
            more = f" (+{len(low) - 5} more)" if len(low) > 5 else ""
            caveats.append(
                f"Segments with n<{MIN_SEGMENT_N} — treat with caution: {shown}{more}."
            )
        for c in comparisons:
            if not c.get("significant") and c.get("delta_pct") is not None:
                caveats.append(
                    f"{c['type'].upper()} change of {c['delta_pct']:+.0%} is not "
                    "statistically significant."
                )
            if c.get("prior_n", 0) < MIN_SEGMENT_N:
                caveats.append(
                    f"Prior {c['type'].upper()} period has only n={c['prior_n']}."
                )
        if window_note:
            caveats.append(window_note)
        return caveats

    # ------------------------------------------------------------- time window

    def _apply_time_window(
        self, df: pd.DataFrame, window: Optional[Mapping[str, Any]]
    ):
        """Filter to [start_date, end_date] on event_date if both are given."""
        if not window or "event_date" not in df.columns:
            return df, ""
        start = self._parse_date(window.get("start_date"))
        end = self._parse_date(window.get("end_date"))
        if start is None and end is None:
            return df, ""
        ev = pd.to_datetime(df["event_date"], errors="coerce")
        mask = pd.Series(True, index=df.index)
        if start is not None:
            mask &= ev >= start
        if end is not None:
            mask &= ev <= end
        kept = df[mask]
        note = ""
        if len(kept) < len(df):
            note = (
                f"Filtered to time window "
                f"[{window.get('start_date') or '...'} .. "
                f"{window.get('end_date') or '...'}]: "
                f"{len(kept)} of {len(df)} rows."
            )
        return kept, note

    def _parse_date(self, value: Any):
        if not value:
            return None
        try:
            return pd.to_datetime(value, errors="coerce")
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------- methodology

    def _methodology(
        self,
        metric: str,
        kind: str,
        dims: Mapping[str, str],
        comparisons: Sequence[JsonDict],
    ) -> str:
        parts: List[str] = []
        agg = {
            "rate": "proportion (mean of a 0/1 flag), Wilson 95% CI",
            "value_mean": "mean, normal-approx 95% CI",
            "value_sum": "sum, normal-approx 95% CI",
            "count": "record count, Poisson normal-approx 95% CI",
            "ratio": "ratio of totals (Σ numerator / Σ denominator), "
                     "ratio-estimator 95% CI",
        }.get(kind, "record count")
        parts.append(f"{metric} computed as {agg}")
        if dims:
            parts.append("broken down by " + ", ".join(dims.keys()))
        if comparisons:
            ctype = comparisons[0]["type"]
            test = "two-proportion z-test" if kind == "rate" else "two-sample z-test"
            parts.append(f"{ctype} compared with a {test} (|z|>=1.96)")
        return "; ".join(parts) + "."

    # -------------------------------------------------------------- escalation

    def _blocked(self, reason: str) -> JsonDict:
        return {
            "status": "blocked",
            "reason": reason,
            "headline_number": {},
            "breakdowns": [],
            "comparisons": [],
            "drivers": [],
            "caveats": [reason],
            "methodology": "",
        }


if __name__ == "__main__":
    import json
    import sys

    from data_engineer_agent import DataEngineerAgent
    from eda_agent import EDAAgent

    if len(sys.argv) < 2:
        print("usage: python analyst_agent.py <source.csv> [metric]", file=sys.stderr)
        raise SystemExit(2)

    metric = sys.argv[2] if len(sys.argv) > 2 else "record_count"
    package = DataEngineerAgent(output_dir=".").run(brief={}, csv_path=sys.argv[1])
    eda = EDAAgent().run(package)
    brief = {
        "question_id": "Q_CLI",
        "metric": metric,
        "dimensions": ["branch", "course", "source"],
        "comparison": {"type": "yoy"},
        "time_window": {},
    }
    result = AnalystAgent().run(brief, package, eda)
    print(json.dumps(result, indent=2, default=str))
