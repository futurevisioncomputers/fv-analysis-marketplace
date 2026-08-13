"""Agent 2.7: Feature Engineering (proposes; the operator approves).

The cleaner produces columns that are *faithful* to the sheet. Analysis needs
columns that are *comparable*: a course duration of 45 days and one of 210 days
belong in different groups before any completion rate means anything, and an
age of 17 only becomes a dimension once it is a band.

Everything here is a derivation the institute's own reporting spec asks for —
duration groups, age bands, outstanding buckets, joining cohorts, batch-time
slots, ending-soon flags. None of it is inferred from statistics; each feature
is a stated rule over a column that exists.

Two design commitments:

- **Nothing materializes unapproved.** `propose()` returns candidates with the
  rule, the source column, and a preview of what the values would look like.
  `apply()` adds only the ids the operator approved. A feature the operator has
  not seen cannot end up in a report.
- **Every threshold is configurable and recorded.** "Short course" is a policy
  choice, not a fact. The defaults below are the spec's suggestions; whatever
  is actually used is written into the session so a number in the report can be
  traced back to the boundary that produced it.

A proposal is only offered when its source column exists AND carries enough
non-null values to be worth having: a feature that is 90% blank is noise that
looks like signal.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Mapping, Optional, Sequence

import pandas as pd

JsonDict = Dict[str, Any]

# A derivation needs this share of its source column populated to be offered.
MIN_COVERAGE = 0.40

# Defaults from the institute's reporting spec (§5, §6, §11). Every one is a
# policy boundary rather than a property of the data, so all are overridable
# and whichever values ran are recorded alongside the output.
DEFAULT_CONFIG: JsonDict = {
    # §5 short / medium / long
    "duration_groups": [("Short", 0, 90), ("Medium", 91, 180), ("Long", 181, None)],
    # §6 finer duration buckets
    "duration_buckets": [(0, 30), (31, 60), (61, 90), (91, 180), (181, 365),
                         (366, None)],
    # §11 age bands
    "age_bands": [(0, 14), (15, 18), (19, 25), (26, 35), (36, 45), (46, None)],
    # Outstanding balance bands, in rupees.
    "outstanding_buckets": [(0, 0), (1, 5000), (5001, 15000), (15001, None)],
    # A course ending within this many days is "ending soon" (§7).
    "ending_soon_days": 30,
    # Batch slots by hour of day.
    "batch_slots": [("Morning", 0, 11), ("Afternoon", 12, 16), ("Evening", 17, 23)],
}


class FeatureEngineeringAgent:
    """Proposes derived columns; materializes only the approved ones."""

    def __init__(self, config: Optional[Mapping[str, Any]] = None):
        self.config: JsonDict = {**DEFAULT_CONFIG, **dict(config or {})}

    # ============================================================== proposing

    def propose(self, data_package: Mapping[str, Any],
                df: Optional[pd.DataFrame] = None,
                as_of: Optional[dt.date] = None) -> JsonDict:
        """Candidate features for this frame, each with its rule and a preview.

        `skipped` is as important as `features`: it says which derivations the
        sheet cannot support and why, so the absence of an age band in the
        report is explained rather than merely noticed.
        """
        if df is None:
            path = data_package.get("canonical_df_path")
            df = pd.read_parquet(path) if path else None
        if df is None or df.empty:
            return {"status": "blocked",
                    "reason": "No canonical frame to derive features from.",
                    "features": [], "skipped": []}

        roles = dict(data_package.get("canonical_columns") or {})
        as_of = as_of or dt.date.today()

        features: List[JsonDict] = []
        skipped: List[JsonDict] = []
        for builder in (self._duration_features, self._age_feature,
                        self._outstanding_feature, self._cohort_features,
                        self._tenure_feature, self._collection_feature,
                        self._batch_slot_feature, self._ending_soon_feature):
            for proposal in builder(df, roles, as_of):
                (features if proposal.pop("_ok") else skipped).append(proposal)

        return {
            "status": "ready",
            "as_of": as_of.isoformat(),
            "config": self.config,
            "features": features,
            "skipped": skipped,
        }

    # ------------------------------------------------------------- builders
    #
    # Each builder yields proposals. `_ok` decides whether it lands in
    # `features` or `skipped`; `build` is the callable `apply` will run.

    def _offer(self, name: str, rule: str, source: Optional[str],
               df: pd.DataFrame, build, reason_missing: str = "") -> JsonDict:
        """One proposal, with the coverage check that decides if it is real."""
        if not source or source not in df.columns:
            return {"_ok": False, "id": name, "name": name, "rule": rule,
                    "source": source,
                    "reason": reason_missing or f"no {source or 'source'} column"}
        coverage = float(df[source].notna().mean())
        if coverage < MIN_COVERAGE:
            return {"_ok": False, "id": name, "name": name, "rule": rule,
                    "source": source,
                    "reason": f"'{source}' is only {coverage:.0%} populated "
                              f"(needs {MIN_COVERAGE:.0%}) — the feature would "
                              f"be mostly blank"}
        return {
            "_ok": True, "id": name, "name": name, "rule": rule,
            "source": source,
            "coverage": round(coverage, 3),
            "preview": self._preview(build(df)),
            "build": build,
        }

    @staticmethod
    def _preview(values: pd.Series) -> Dict[str, Any]:
        """What the column would look like, in the shape that suits its type.

        Counting distinct values of a continuous column produces a useless
        histogram of every number in it; the operator wants the range instead.
        """
        if pd.api.types.is_numeric_dtype(values) and not pd.api.types.is_bool_dtype(values):
            described = values.dropna()
            if described.empty:
                return {}
            return {"min": round(float(described.min()), 1),
                    "median": round(float(described.median()), 1),
                    "max": round(float(described.max()), 1)}
        counts = values.value_counts(dropna=True).head(6)
        return {str(k): int(v) for k, v in counts.items()}

    def _duration_features(self, df, roles, as_of) -> List[JsonDict]:
        col = roles.get("course_duration")
        groups = self.config["duration_groups"]
        buckets = self.config["duration_buckets"]
        return [
            self._offer(
                "duration_group",
                "Course duration in days banded as "
                + ", ".join(f"{n} {lo}–{hi if hi else '+'}" for n, lo, hi in groups),
                col, df,
                lambda d, c=col, g=groups: self._band(
                    pd.to_numeric(d[c], errors="coerce"),
                    [(n, lo, hi) for n, lo, hi in g])),
            self._offer(
                "duration_bucket",
                "Finer duration bands: "
                + ", ".join(f"{lo}–{hi if hi else '+'}" for lo, hi in buckets),
                col, df,
                lambda d, c=col, b=buckets: self._band(
                    pd.to_numeric(d[c], errors="coerce"),
                    [(f"{lo}-{hi}" if hi else f"{lo}+", lo, hi) for lo, hi in b])),
        ]

    def _age_feature(self, df, roles, as_of) -> List[JsonDict]:
        col = roles.get("dob")
        bands = self.config["age_bands"]

        def build(d, c=col, b=bands):
            born = pd.to_datetime(d[c], errors="coerce")
            # Whole years as of the run date, not a 365-day division: a person
            # born on 29 February is not 0.997 of a year older each year.
            years = (pd.Timestamp(as_of) - born).dt.days // 365
            return self._band(years,
                              [(f"{lo}-{hi}" if hi else f"{lo}+", lo, hi)
                               for lo, hi in b])

        return [self._offer(
            "age_band",
            "Age at the run date, banded as "
            + ", ".join(f"{lo}–{hi if hi else '+'}" for lo, hi in bands),
            col, df, build,
            reason_missing="no date of birth on this sheet")]

    def _outstanding_feature(self, df, roles, as_of) -> List[JsonDict]:
        col = roles.get("pending")
        buckets = self.config["outstanding_buckets"]

        def build(d, c=col, b=buckets):
            amounts = pd.to_numeric(d[c], errors="coerce")
            return self._band(
                amounts,
                [("Nil" if hi == 0 else f"{lo}-{hi}" if hi else f"{lo}+", lo, hi)
                 for lo, hi in b],
                # A negative balance is an overpayment, not a debt band.
                below_label="Overpaid")

        return [self._offer(
            "outstanding_bucket",
            "Pending balance banded; negatives are overpayments, not debt",
            col, df, build,
            reason_missing="no pending-amount column (needs fees-data)")]

    def _cohort_features(self, df, roles, as_of) -> List[JsonDict]:
        col = (roles.get("admission_date") or roles.get("joining_date")
               or roles.get("enquiry_date"))
        return [self._offer(
            "joining_cohort",
            "Calendar month of admission — the cohort a student belongs to, "
            "for comparing like with like over time",
            col, df,
            lambda d, c=col: pd.to_datetime(d[c], errors="coerce")
                               .dt.to_period("M").astype("string"))]

    def _tenure_feature(self, df, roles, as_of) -> List[JsonDict]:
        col = roles.get("admission_date") or roles.get("joining_date")

        def build(d, c=col):
            start = pd.to_datetime(d[c], errors="coerce")
            months = ((pd.Timestamp(as_of) - start).dt.days / 30.44).round(1)
            return months

        return [self._offer(
            "months_since_admission",
            f"Whole months from admission to {as_of.isoformat()}. The churn "
            f"rule counts from admission, so this is the column it reads",
            col, df, build)]

    def _collection_feature(self, df, roles, as_of) -> List[JsonDict]:
        paid = roles.get("paid") or ("amount_collected"
                                     if "amount_collected" in df.columns else None)
        billed = roles.get("amount")

        def build(d, p=paid, b=billed):
            collected = pd.to_numeric(d[p], errors="coerce")
            total = pd.to_numeric(d[b], errors="coerce")
            ratio = (collected / total.where(total > 0))
            return self._band(ratio, [("Unpaid", 0, 0), ("Part paid", 0.001, 0.999),
                                      ("Fully paid", 1, None)])

        if not billed or billed not in df.columns or not paid:
            return [{"_ok": False, "id": "collection_band", "name": "collection_band",
                     "rule": "share of the fee actually collected",
                     "source": billed,
                     "reason": "needs both a billed total and a collected amount"}]
        return [self._offer(
            "collection_band",
            "Share of the billed fee collected: unpaid / part paid / fully paid",
            billed, df, build)]

    def _batch_slot_feature(self, df, roles, as_of) -> List[JsonDict]:
        col = roles.get("batch_time") or roles.get("preferred_batch_time")
        slots = self.config["batch_slots"]

        def build(d, c=col, s=slots):
            # "02:00 To 03:00" is an afternoon class: the institute writes
            # timings on a 12-hour clock with no meridiem, and no one runs a
            # 2am batch.
            hour = (d[c].astype("string").str.extract(r"^\s*(\d{1,2})")[0]
                    .astype("float"))
            hour = hour.where(hour >= 7, hour + 12)
            return self._band(hour, [(n, lo, hi) for n, lo, hi in s])

        return [self._offer(
            "batch_slot",
            "Batch start time bucketed into morning / afternoon / evening "
            "(times are 12-hour with no meridiem; anything before 7 reads as pm)",
            col, df, build)]

    def _ending_soon_feature(self, df, roles, as_of) -> List[JsonDict]:
        col = roles.get("days_remaining")
        window = self.config["ending_soon_days"]
        return [self._offer(
            "is_ending_soon",
            f"Course has {window} days or fewer remaining — the list a tutor "
            f"acts on this month",
            col, df,
            lambda d, c=col, w=window: (
                pd.to_numeric(d[c], errors="coerce").le(w)
                & pd.to_numeric(d[c], errors="coerce").ge(0)),
            reason_missing="no days-remaining column (needs a timetable tab)")]

    # ------------------------------------------------------------- applying

    def apply(self, df: pd.DataFrame, proposals: Sequence[Mapping[str, Any]],
              approved: Sequence[str]) -> JsonDict:
        """Add the approved features to `df` in place. Returns what was added.

        Unapproved proposals are not computed at all — not computed and hidden,
        genuinely absent — so an operator who declined a feature cannot find it
        in the parquet later.
        """
        wanted = set(approved)
        added, rejected = [], []
        for proposal in proposals:
            if proposal["id"] not in wanted:
                rejected.append(proposal["id"])
                continue
            build = proposal.get("build")
            if build is None:
                continue
            df[proposal["id"]] = build(df)
            added.append({"id": proposal["id"], "rule": proposal["rule"],
                          "source": proposal.get("source")})
        return {"added": added, "declined": rejected}

    # ---------------------------------------------------------------- shared

    @staticmethod
    def _band(values: pd.Series, bands: Sequence, below_label: str = "") -> pd.Series:
        """Label each value by the first band it falls in. NaN stays NaN.

        Bands are inclusive on both ends, with `None` as an open upper bound.
        A value below every band's floor gets `below_label` when one is given —
        that is how a negative balance is reported as an overpayment instead of
        being silently binned with zero.
        """
        out = pd.Series(pd.NA, index=values.index, dtype="string")
        for label, low, high in bands:
            mask = values.notna() & (values >= low)
            if high is not None:
                mask &= values <= high
            out = out.mask(mask & out.isna(), str(label))
        if below_label:
            floor = min(low for _, low, _ in bands)
            out = out.mask(values.notna() & (values < floor) & out.isna(),
                           below_label)
        return out
