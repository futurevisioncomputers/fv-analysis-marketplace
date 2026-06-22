"""Agent 3: EDA.

Consumes a `DataPackage` (the dict Agent 2 / DataEngineerAgent returns) and
surfaces what is *interesting* in the canonical data, independent of any specific
business question. Produces an `EDAReport`.

Design contract (seven_agent_system_design.md, Agent 3):
- distributions: per-column top values + entropy (categorical) and descriptive
  statistics (numeric).
- time_trends: record counts over the derived period columns + a trend label.
- cross_tabs: pairs of categorical dimensions with a chi-square statistic and
  Cramer's V effect size, plus the strongest cell.
- anomalies: spikes/dips in the monthly trend and numeric outliers.
- candidate_hypotheses: plain-language leads for the Analyst to test.

Rule: EDA never makes recommendations — only observations and hypotheses.

No third-party stats dependency: the chi-square statistic and Cramer's V are
computed directly with pandas/numpy. "significant" is judged by Cramer's V
effect-size thresholds rather than a p-value, which is both dependency-free and
more meaningful for ranking interesting associations.
"""

from __future__ import annotations

import math
import os
from typing import Any, Dict, List, Mapping, Optional, Sequence

import numpy as np
import pandas as pd

from . import llm_client


JsonDict = Dict[str, Any]

# Categorical roles worth profiling / crossing. Excludes high-cardinality and
# PII-ish roles (name/mobile/email/address are hashed, id-like columns explode).
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
)

NUMERIC_ROLES = ("amount", "paid", "pending", "course_duration", "days_remaining")

# Derived numeric columns Agent 2 may have added.
DERIVED_NUMERIC = ("lead_to_admission_days",)

MAX_CATEGORICAL_CARDINALITY = 30  # above this, skip distribution/cross-tab
TOP_N = 10
MIN_ROWS_FOR_CROSSTAB = 20

# Cramer's V interpretation (Cohen-style) for the "significant" flag.
CRAMERS_V_WEAK = 0.10
CRAMERS_V_MODERATE = 0.30


class EDAAgent:
    """Builds an EDAReport from a DataPackage."""

    def run(
        self,
        data_package: Mapping[str, Any],
        df: Optional[pd.DataFrame] = None,
    ) -> JsonDict:
        """Produce an EDAReport.

        Args:
            data_package: The dict returned by DataEngineerAgent. Must carry
                `canonical_df_path` (unless `df` is supplied) and
                `canonical_columns` (role -> column mapping).
            df: Optional already-loaded canonical dataframe (skips the parquet
                read; mainly for testing / orchestrator reuse).
        """

        if data_package.get("status") == "blocked":
            return self._blocked("Upstream DataPackage is blocked; nothing to explore.")

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

        # Include value-discovered roles (Data Engineer Phase 1) so columns whose
        # headers matched no keyword are still profiled by their inferred nature.
        dim_roles = tuple(DIMENSION_ROLES) + tuple(
            sorted(r for r in roles if r.startswith("discovered_dim"))
        )
        dimension_cols = self._available(roles, dim_roles, df)
        numeric_cols = self._numeric_columns(roles, df)

        distributions = self._distributions(df, dimension_cols, numeric_cols)
        time_trends = self._time_trends(df)
        cross_tabs = self._cross_tabs(df, dimension_cols)
        anomalies = self._anomalies(df, time_trends, numeric_cols)
        hypotheses = self._candidate_hypotheses(
            cross_tabs, time_trends, anomalies, dimension_cols
        )

        narrative = self._narrative(
            int(len(df)), dimension_cols, numeric_cols, time_trends,
            cross_tabs, anomalies,
        )

        return {
            "status": "ready",
            "row_count": int(len(df)),
            "profiled_dimensions": list(dimension_cols.keys()),
            "profiled_numerics": list(numeric_cols.keys()),
            "distributions": distributions,
            "time_trends": time_trends,
            "cross_tabs": cross_tabs,
            "anomalies": anomalies,
            "candidate_hypotheses": hypotheses,
            "narrative": narrative,
        }

    # ------------------------------------------------------------- narrative

    def _narrative(
        self, row_count, dimension_cols, numeric_cols, time_trends,
        cross_tabs, anomalies,
    ) -> str:
        """A short plain-language read of what the exploration surfaced. Always
        observation-only (EDA never recommends). The LLM, when configured, phrases
        it from the structured findings below; otherwise a deterministic one-liner
        is used. The LLM is never shown raw data — only these computed facts — so
        it cannot introduce a statistic the agent did not derive."""
        trend = (time_trends or {}).get("trend_direction")
        top_assoc = None
        for ct in cross_tabs or []:
            if ct.get("significant"):
                top_assoc = ct
                break

        # Deterministic fallback narrative.
        parts: List[str] = [
            f"Profiled {len(dimension_cols)} dimension(s) and "
            f"{len(numeric_cols)} numeric field(s) over {row_count:,} records."
        ]
        if trend:
            parts.append(f"Record volume is {trend} over time.")
        if top_assoc:
            parts.append(
                f"Strongest association: {top_assoc.get('dimension_a')} × "
                f"{top_assoc.get('dimension_b')} (Cramer's V "
                f"{top_assoc.get('cramers_v')})."
            )
        if anomalies:
            parts.append(f"{len(anomalies)} anomaly signal(s) flagged.")
        deterministic = " ".join(parts)

        if not llm_client.available():
            return deterministic
        try:
            import json as _json
            facts = {
                "row_count": row_count,
                "dimensions": list(dimension_cols.keys()),
                "numerics": list(numeric_cols.keys()),
                "trend_direction": trend,
                "significant_associations": [
                    {"a": ct.get("dimension_a"), "b": ct.get("dimension_b"),
                     "cramers_v": ct.get("cramers_v")}
                    for ct in (cross_tabs or []) if ct.get("significant")
                ][:3],
                "anomalies": [
                    {"type": a.get("type"), "metric": a.get("metric"),
                     "period": a.get("period")}
                    for a in (anomalies or [])
                ][:4],
            }
            prompt = (
                "You are a data analyst summarizing exploratory analysis of an "
                "education institute's dataset. Use ONLY the facts in this JSON; "
                "do not invent numbers, columns, or associations.\n\n"
                f"FACTS:\n{_json.dumps(facts, ensure_ascii=False, default=str)}\n\n"
                "Write 2-3 sentences describing what stands out in the data: "
                "volume/trend, the strongest associations, and any anomalies. "
                "Observations only — no recommendations or actions. Return only "
                "the narrative text."
            )
            text = llm_client.complete_text(prompt, max_tokens=350, temperature=0.3)
            cleaned = text.strip().strip('"').strip()
            return cleaned if len(cleaned) >= 20 else deterministic
        except llm_client.LLMUnavailable:
            return deterministic
        except Exception:  # noqa: BLE001 - narrative must never break EDA
            return deterministic

    # ----------------------------------------------------------- column setup

    def _available(
        self, roles: Mapping[str, str], wanted: Sequence[str], df: pd.DataFrame
    ) -> Dict[str, str]:
        """role -> column for wanted roles that exist and are low-cardinality."""
        out: Dict[str, str] = {}
        for role in wanted:
            col = roles.get(role)
            if col and col in df.columns:
                if df[col].nunique(dropna=True) <= MAX_CATEGORICAL_CARDINALITY:
                    out[role] = col
        return out

    def _numeric_columns(
        self, roles: Mapping[str, str], df: pd.DataFrame
    ) -> Dict[str, str]:
        out: Dict[str, str] = {}
        num_roles = list(NUMERIC_ROLES) + sorted(
            r for r in roles if r.startswith("discovered_num")
        )
        for role in num_roles:
            col = roles.get(role)
            if col and col in df.columns and pd.api.types.is_numeric_dtype(df[col]):
                out[role] = col
        for col in DERIVED_NUMERIC:
            if col in df.columns and pd.api.types.is_numeric_dtype(df[col]):
                out[col] = col
        return out

    # ------------------------------------------------------------ univariate

    def _distributions(
        self,
        df: pd.DataFrame,
        dimension_cols: Mapping[str, str],
        numeric_cols: Mapping[str, str],
    ) -> JsonDict:
        result: JsonDict = {"categorical": {}, "numeric": {}}

        for role, col in dimension_cols.items():
            counts = df[col].value_counts(dropna=True)
            n = int(counts.sum())
            if n == 0:
                continue
            probs = counts / n
            top = [
                {"value": str(idx), "count": int(c), "pct": round(float(c) / n, 4)}
                for idx, c in counts.head(TOP_N).items()
            ]
            result["categorical"][role] = {
                "column": col,
                "distinct": int(df[col].nunique(dropna=True)),
                "mode": str(counts.index[0]),
                "entropy": round(float(-(probs * np.log2(probs)).sum()), 4),
                "null_rate": round(float(df[col].isna().mean()), 4),
                "top_values": top,
            }

        for role, col in numeric_cols.items():
            s = pd.to_numeric(df[col], errors="coerce").dropna()
            if len(s) == 0:
                continue
            q1, q3 = float(s.quantile(0.25)), float(s.quantile(0.75))
            result["numeric"][role] = {
                "column": col,
                "n": int(len(s)),
                "mean": round(float(s.mean()), 2),
                "median": round(float(s.median()), 2),
                "std": round(float(s.std()), 2) if len(s) > 1 else 0.0,
                "min": round(float(s.min()), 2),
                "max": round(float(s.max()), 2),
                "iqr": round(q3 - q1, 2),
                "skewness": round(float(s.skew()), 2) if len(s) > 2 else 0.0,
                "kurtosis": round(float(s.kurt()), 2) if len(s) > 3 else 0.0,
            }

        return result

    # ------------------------------------------------------------ time trends

    def _time_trends(self, df: pd.DataFrame) -> JsonDict:
        trends: JsonDict = {}
        if "period_year" in df.columns and "period_month" in df.columns:
            monthly = (
                df.dropna(subset=["period_year", "period_month"])
                .groupby(["period_year", "period_month"])
                .size()
                .reset_index(name="records")
                .sort_values(["period_year", "period_month"])
            )
            series = [
                {
                    "period": f"{int(r.period_year)}-{int(r.period_month):02d}",
                    "records": int(r.records),
                }
                for r in monthly.itertuples()
            ]
            trends["monthly_records"] = series
            trends["trend_direction"] = self._trend_direction(
                [p["records"] for p in series]
            )

        if "period_quarter" in df.columns:
            q = df["period_quarter"].dropna().value_counts().sort_index()
            trends["quarterly_records"] = [
                {"period": str(idx), "records": int(c)} for idx, c in q.items()
            ]

        if "period_is_weekend" in df.columns:
            wk = df["period_is_weekend"].dropna()
            if len(wk):
                weekend = int(wk.sum())
                trends["weekend_vs_weekday"] = {
                    "weekend": weekend,
                    "weekday": int(len(wk) - weekend),
                    "weekend_pct": round(weekend / len(wk), 4),
                }

        if "period_day_name" in df.columns:
            dn = df["period_day_name"].dropna().value_counts()
            if len(dn):
                trends["busiest_day"] = str(dn.index[0])
                trends["records_by_day_name"] = {
                    str(k): int(v) for k, v in dn.items()
                }

        return trends

    def _trend_direction(self, counts: Sequence[int]) -> str:
        """Label the trend by comparing first and second half averages."""
        if len(counts) < 4:
            return "insufficient_data"
        mid = len(counts) // 2
        first = np.mean(counts[:mid])
        second = np.mean(counts[mid:])
        if first == 0:
            return "rising" if second > 0 else "flat"
        change = (second - first) / first
        if change > 0.10:
            return "rising"
        if change < -0.10:
            return "declining"
        return "flat"

    # -------------------------------------------------------------- bivariate

    def _cross_tabs(
        self, df: pd.DataFrame, dimension_cols: Mapping[str, str]
    ) -> List[JsonDict]:
        if len(df) < MIN_ROWS_FOR_CROSSTAB:
            return []
        cols = list(dimension_cols.items())
        results: List[JsonDict] = []
        for i in range(len(cols)):
            for j in range(i + 1, len(cols)):
                role_a, col_a = cols[i]
                role_b, col_b = cols[j]
                record = self._chi_square(df, role_a, col_a, role_b, col_b)
                if record:
                    results.append(record)
        # Strongest associations first.
        results.sort(key=lambda r: r["cramers_v"], reverse=True)
        return results

    def _chi_square(
        self,
        df: pd.DataFrame,
        role_a: str,
        col_a: str,
        role_b: str,
        col_b: str,
    ) -> Optional[JsonDict]:
        sub = df[[col_a, col_b]].dropna()
        if len(sub) < MIN_ROWS_FOR_CROSSTAB:
            return None
        observed = pd.crosstab(sub[col_a], sub[col_b])
        if observed.shape[0] < 2 or observed.shape[1] < 2:
            return None

        obs = observed.to_numpy(dtype=float)
        n = obs.sum()
        row_tot = obs.sum(axis=1, keepdims=True)
        col_tot = obs.sum(axis=0, keepdims=True)
        expected = row_tot @ col_tot / n
        with np.errstate(divide="ignore", invalid="ignore"):
            chi2 = float(np.nansum((obs - expected) ** 2 / expected))

        r, k = obs.shape
        min_dim = min(r - 1, k - 1)
        cramers_v = math.sqrt(chi2 / (n * min_dim)) if min_dim > 0 else 0.0

        # Strongest cell = largest positive standardized residual.
        with np.errstate(divide="ignore", invalid="ignore"):
            resid = (obs - expected) / np.sqrt(expected)
        ri, ci = np.unravel_index(np.nanargmax(resid), resid.shape)
        strongest = f"{observed.index[ri]} x {observed.columns[ci]}"

        return {
            "dimension_a": role_a,
            "dimension_b": role_b,
            "chi2": round(chi2, 3),
            "dof": int((r - 1) * (k - 1)),
            "cramers_v": round(cramers_v, 3),
            "association": self._assoc_label(cramers_v),
            "significant": cramers_v >= CRAMERS_V_WEAK,
            "strongest_cell": strongest,
            "n": int(n),
        }

    def _assoc_label(self, v: float) -> str:
        if v >= CRAMERS_V_MODERATE:
            return "strong"
        if v >= CRAMERS_V_WEAK:
            return "moderate"
        return "weak"

    # --------------------------------------------------------------- anomalies

    def _anomalies(
        self,
        df: pd.DataFrame,
        time_trends: Mapping[str, Any],
        numeric_cols: Mapping[str, str],
    ) -> List[JsonDict]:
        anomalies: List[JsonDict] = []

        # 1. Monthly spikes/dips vs trailing average.
        series = time_trends.get("monthly_records") or []
        counts = [p["records"] for p in series]
        if len(counts) >= 4:
            for idx in range(3, len(counts)):
                trailing = np.mean(counts[max(0, idx - 3):idx])
                if trailing <= 0:
                    continue
                change = (counts[idx] - trailing) / trailing
                if abs(change) >= 0.5:
                    anomalies.append({
                        "type": "spike" if change > 0 else "dip",
                        "metric": "monthly_records",
                        "period": series[idx]["period"],
                        "value": counts[idx],
                        "magnitude_pct": round(float(change), 3),
                        "vs": "trailing 3-month average",
                    })

        # 2. Numeric outliers via the 1.5*IQR rule.
        for role, col in numeric_cols.items():
            s = pd.to_numeric(df[col], errors="coerce").dropna()
            if len(s) < 8:
                continue
            q1, q3 = s.quantile(0.25), s.quantile(0.75)
            iqr = q3 - q1
            if iqr <= 0:
                continue
            lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
            outliers = int(((s < lo) | (s > hi)).sum())
            if outliers:
                anomalies.append({
                    "type": "outliers",
                    "metric": role,
                    "count": outliers,
                    "pct": round(outliers / len(s), 4),
                    "bounds": [round(float(lo), 2), round(float(hi), 2)],
                })

        return anomalies

    # ----------------------------------------------------------- hypotheses

    def _candidate_hypotheses(
        self,
        cross_tabs: Sequence[JsonDict],
        time_trends: Mapping[str, Any],
        anomalies: Sequence[JsonDict],
        dimension_cols: Mapping[str, str],
    ) -> List[str]:
        hyps: List[str] = []

        for ct in cross_tabs:
            if ct["association"] in ("strong", "moderate"):
                hyps.append(
                    f"{ct['dimension_a']} and {ct['dimension_b']} are associated "
                    f"(Cramer's V {ct['cramers_v']}); strongest at "
                    f"{ct['strongest_cell']}."
                )

        direction = time_trends.get("trend_direction")
        if direction in ("rising", "declining"):
            hyps.append(f"Record volume is {direction} over the observed months.")

        for an in anomalies:
            if an["type"] in ("spike", "dip"):
                hyps.append(
                    f"A {an['type']} in records at {an['period']} "
                    f"({an['magnitude_pct']:+.0%} vs trailing avg) may have a cause."
                )

        weekend = time_trends.get("weekend_vs_weekday")
        if weekend and weekend.get("weekend_pct", 0) >= 0.5:
            hyps.append(
                "More than half of records fall on weekends; intake may be "
                "weekend-driven."
            )

        return hyps[:12]

    # -------------------------------------------------------------- escalation

    def _blocked(self, reason: str) -> JsonDict:
        return {
            "status": "blocked",
            "reason": reason,
            "distributions": {},
            "time_trends": {},
            "cross_tabs": [],
            "anomalies": [],
            "candidate_hypotheses": [],
        }


if __name__ == "__main__":
    import json
    import sys

    from data_engineer_agent import DataEngineerAgent

    if len(sys.argv) < 2:
        print("usage: python eda_agent.py <source.csv> [date_format]", file=sys.stderr)
        raise SystemExit(2)

    dfmt = sys.argv[2] if len(sys.argv) > 2 else None
    package = DataEngineerAgent(output_dir=".").run(
        brief={}, csv_path=sys.argv[1], date_format=dfmt
    )
    report = EDAAgent().run(package)
    print(json.dumps(report, indent=2, default=str))
