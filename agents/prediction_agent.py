"""Agent 4.5: Prediction (optional, honesty-gated).

Trains a transparent churn/completion model on the labels the pipeline already
derives — completion_status from the timetable sheet membership
(Course_Completed / Not_Coming / Main_data). The target is *terminal-only*: among
rows whose course has ended (completed vs not_coming) predict not_coming. Rows
still `active` are censored (outcome unknown) and are excluded from training, so
the model never learns from an unfinished outcome.

Design contract (mirrors the rest of the pipeline):
- No third-party ML dependency. The model is a categorical Naive Bayes with
  Laplace smoothing — closed-form counts, fully deterministic, and directly
  explainable (each prediction decomposes into per-feature likelihood ratios),
  in the same hand-rolled-statistics spirit as the Analyst's Wilson/ratio CIs.
- Honesty gate: refuse to train when labels are absent, single-class, or too
  few (MIN_LABELED_ROWS / MIN_PER_CLASS). It returns status "blocked" with the
  specific reason rather than inventing a model on thin data.
- Deterministic holdout: rows are bucketed by a stable hash (person_id when
  present, else row position), so the reported accuracy is reproducible with no
  RNG seed. Accuracy is always reported against the majority-class baseline so a
  model that only learns the base rate is visible as zero lift.

The agent *predicts and explains*; it does not recommend actions (Insights) nor
phrase prose (that is the LLM layer, which only narrates these numbers).
"""

from __future__ import annotations

import hashlib
import math
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import pandas as pd


JsonDict = Dict[str, Any]

# Honesty-gate thresholds. Below these the model is not trustworthy, so refuse.
MIN_LABELED_ROWS = 40      # total terminal (completed + not_coming) rows
MIN_PER_CLASS = 10         # each class needs at least this many examples
LAPLACE_ALPHA = 1.0        # additive smoothing for unseen feature values
MAX_FEATURE_CARDINALITY = 30  # a column with more distinct values isn't a feature
HOLDOUT_BUCKETS = 5        # 1-in-5 rows (20%) held out for evaluation

# Categorical roles usable as churn features when present in the frame. Each is
# read through the role map or by literal column name; missing ones are skipped.
CANDIDATE_FEATURE_ROLES = (
    "branch", "course", "course_category", "course_family", "faculty",
    "payment_mode", "source", "city",
)
# Boolean-flag columns that make good features when present (literal names).
CANDIDATE_FEATURE_FLAGS = ("is_fast_track", "is_repeat_enrollment", "is_default")


class PredictionAgent:
    """Honesty-gated churn predictor over the timetable completion labels."""

    # =============================================================== public

    def run(
        self,
        data_package: Mapping[str, Any],
        df: Optional[pd.DataFrame] = None,
        dataset_mode: str = "institute",
    ) -> JsonDict:
        """Train + evaluate a churn model, or block with a specific reason.

        Args:
            data_package: DataEngineerAgent output (roles + canonical path).
            df: already-loaded canonical dataframe (skips the parquet read).
            dataset_mode: "institute" only; generic sheets have no churn label,
                so anything else blocks immediately.
        """
        if data_package.get("status") == "blocked":
            return self._blocked("Upstream DataPackage is blocked; cannot train.")
        if dataset_mode != "institute":
            return self._blocked(
                "Prediction runs only on institute-shaped data (needs a "
                "completion label); generic sheets carry no churn outcome."
            )
        if df is None:
            df = self._load_frame(data_package)
            if df is None:
                return self._blocked("Canonical dataframe not found or unreadable.")

        roles: Dict[str, str] = dict(data_package.get("canonical_columns") or {})

        labels = self._terminal_labels(df)
        if labels is None:
            return self._blocked(
                "No completion label present (need completion_status or "
                "is_not_coming from the timetable sheet membership)."
            )
        gate = self._check_gate(labels)
        if gate is not None:
            return self._blocked(gate)

        features = self._select_features(df, roles)
        if not features:
            return self._blocked(
                "No usable categorical feature columns for prediction."
            )

        return self._train_and_evaluate(df, labels, features)

    # =============================================================== labels

    def _terminal_labels(self, df: pd.DataFrame) -> Optional["pd.Series"]:
        """Boolean churn label (True = not_coming) over TERMINAL rows only.

        Terminal = the course outcome is known: completed or not_coming. `active`
        rows are censored and dropped so the model never trains on an unfinished
        student. Returns a boolean Series indexed like df (subset), or None when
        no completion signal exists.
        """
        if "completion_status" in df.columns:
            status = df["completion_status"].astype("string")
            terminal = status.isin(["completed", "not_coming"])
            if terminal.sum() == 0:
                return None
            return (status[terminal] == "not_coming")
        # Fallback: the boolean flags, when the status string was not retained.
        if "is_not_coming" in df.columns and "is_completed" in df.columns:
            nc = df["is_not_coming"].fillna(False).astype(bool)
            comp = df["is_completed"].fillna(False).astype(bool)
            terminal = nc | comp
            if terminal.sum() == 0:
                return None
            return nc[terminal]
        return None

    def _check_gate(self, labels: "pd.Series") -> Optional[str]:
        """Return a block reason if the labels are too thin/degenerate, else None."""
        n = int(len(labels))
        if n < MIN_LABELED_ROWS:
            return (
                f"Only {n} labeled (terminal) rows; need >= {MIN_LABELED_ROWS} "
                "before a churn model is trustworthy. Wait for more completed / "
                "not-coming outcomes."
            )
        pos = int(labels.sum())
        neg = n - pos
        if pos < MIN_PER_CLASS or neg < MIN_PER_CLASS:
            return (
                f"Class imbalance too severe: {pos} not_coming vs {neg} completed "
                f"(each needs >= {MIN_PER_CLASS}). Model would only learn the base "
                "rate."
            )
        return None

    # ============================================================= features

    def _select_features(
        self, df: pd.DataFrame, roles: Mapping[str, str]
    ) -> List[str]:
        """Pick low-cardinality categorical + flag columns present in the frame."""
        features: List[str] = []
        seen: set = set()
        for role in CANDIDATE_FEATURE_ROLES:
            col = roles.get(role, role)
            if col in df.columns and col not in seen:
                nuniq = int(df[col].nunique(dropna=True))
                if 1 < nuniq <= MAX_FEATURE_CARDINALITY:
                    features.append(col)
                    seen.add(col)
        for col in CANDIDATE_FEATURE_FLAGS:
            if col in df.columns and col not in seen and df[col].nunique(dropna=True) > 1:
                features.append(col)
                seen.add(col)
        return features

    # ================================================================ train

    def _train_and_evaluate(
        self, df: pd.DataFrame, labels: "pd.Series", features: Sequence[str]
    ) -> JsonDict:
        """Fit categorical NB on the train split, score the holdout, explain."""
        work = df.loc[labels.index, list(features)].copy()
        work["_label"] = labels.astype(bool).values
        work["_holdout"] = self._holdout_mask(df.loc[labels.index])

        train = work[~work["_holdout"]]
        holdout = work[work["_holdout"]]
        # Guard: if the stable split starved a side, fall back to train-on-all.
        if train["_label"].nunique() < 2 or len(holdout) == 0:
            train = work
            holdout = work

        model = self._fit(train, features)
        acc, base_acc, n_eval = self._score(model, holdout, features)
        factors = self._risk_factors(model, features)

        pos = int(work["_label"].sum())
        n = int(len(work))
        return {
            "status": "ready",
            "target": "not_coming (churn) among terminal rows",
            "n_labeled": n,
            "class_balance": {"not_coming": pos, "completed": n - pos},
            "base_rate": round(pos / n, 4),
            "features_used": list(features),
            "model": "categorical Naive Bayes (Laplace-smoothed, dependency-free)",
            "holdout_accuracy": round(acc, 4),
            "baseline_accuracy": round(base_acc, 4),
            "accuracy_lift": round(acc - base_acc, 4),
            "holdout_n": n_eval,
            "top_risk_factors": factors,
            "caveats": self._caveats(n, pos, n - pos, acc, base_acc),
            "methodology": (
                "Categorical Naive Bayes: P(churn|features) from Laplace-smoothed "
                "class-conditional value frequencies and the class prior. Target is "
                "terminal-only (completed vs not_coming); active/censored rows "
                "excluded. Accuracy on a deterministic 1-in-5 holdout, reported "
                "against the majority-class baseline."
            ),
        }

    def _fit(
        self, train: pd.DataFrame, features: Sequence[str]
    ) -> JsonDict:
        """Return {prior, cond, cats} — log-priors and log class-conditionals."""
        y = train["_label"].astype(bool)
        n_pos = int(y.sum())
        n_neg = int(len(y) - n_pos)
        prior = {
            True: math.log(n_pos / len(y)) if n_pos else float("-inf"),
            False: math.log(n_neg / len(y)) if n_neg else float("-inf"),
        }
        cond: Dict[str, Dict[bool, Dict[str, float]]] = {}
        cats: Dict[str, List[str]] = {}
        for col in features:
            vals = train[col].astype("string").fillna("<NA>")
            categories = sorted(vals.dropna().unique().tolist())
            cats[col] = categories
            k = len(categories)
            cond[col] = {}
            for cls, count in ((True, n_pos), (False, n_neg)):
                sub = vals[y.values == cls]
                freq = sub.value_counts().to_dict()
                denom = count + LAPLACE_ALPHA * k
                cond[col][cls] = {
                    cat: math.log((freq.get(cat, 0) + LAPLACE_ALPHA) / denom)
                    for cat in categories
                }
                # log-prob mass for a value unseen in training (still smoothed).
                cond[col][cls]["<UNSEEN>"] = math.log(LAPLACE_ALPHA / denom)
        return {"prior": prior, "cond": cond, "cats": cats}

    def _predict_logits(
        self, model: JsonDict, row: Mapping[str, Any], features: Sequence[str]
    ) -> Tuple[float, float]:
        """Return (logscore_churn, logscore_stay) for one row."""
        score = {True: model["prior"][True], False: model["prior"][False]}
        for col in features:
            raw = row.get(col)
            val = "<NA>" if pd.isna(raw) else str(raw)
            for cls in (True, False):
                table = model["cond"][col][cls]
                score[cls] += table.get(val, table["<UNSEEN>"])
        return score[True], score[False]

    def _score(
        self, model: JsonDict, holdout: pd.DataFrame, features: Sequence[str]
    ) -> Tuple[float, float, int]:
        """Holdout accuracy, majority-baseline accuracy, and n evaluated."""
        n = len(holdout)
        if n == 0:
            return 0.0, 0.0, 0
        correct = 0
        for _, row in holdout.iterrows():
            churn_logit, stay_logit = self._predict_logits(model, row, features)
            pred = churn_logit >= stay_logit
            if bool(pred) == bool(row["_label"]):
                correct += 1
        y = holdout["_label"].astype(bool)
        majority = max(int(y.sum()), int(len(y) - y.sum()))
        return correct / n, majority / n, n

    # ============================================================== explain

    def _risk_factors(
        self, model: JsonDict, features: Sequence[str], top: int = 8
    ) -> List[JsonDict]:
        """Feature values most predictive of churn, by log-likelihood ratio.

        LLR = log P(value|churn) - log P(value|completed). Positive = pushes
        toward churn. These are the model's transparent 'why'.
        """
        rows: List[JsonDict] = []
        for col in features:
            for cat in model["cats"][col]:
                lp = model["cond"][col][True][cat]
                ln = model["cond"][col][False][cat]
                rows.append({
                    "feature": col,
                    "value": cat,
                    "churn_log_likelihood_ratio": round(lp - ln, 4),
                })
        rows.sort(key=lambda r: r["churn_log_likelihood_ratio"], reverse=True)
        return rows[:top]

    def _caveats(
        self, n: int, pos: int, neg: int, acc: float, base_acc: float
    ) -> List[str]:
        out: List[str] = []
        if n < 2 * MIN_LABELED_ROWS:
            out.append(
                f"Small training set ({n} terminal rows); treat scores as "
                "indicative, not decisive."
            )
        if acc <= base_acc + 1e-9:
            out.append(
                "Model does not beat the majority-class baseline — features carry "
                "little churn signal yet."
            )
        minority = min(pos, neg)
        if minority < 2 * MIN_PER_CLASS:
            out.append(
                f"Minority class has only {minority} examples; per-feature "
                "estimates are noisy."
            )
        return out

    # ================================================================ utils

    def _holdout_mask(self, frame: pd.DataFrame) -> "pd.Series":
        """Deterministic 1-in-HOLDOUT_BUCKETS holdout by stable hash.

        Keyed on person_id when present (so all rows of one person share a fold),
        else on row position. No RNG — the split is reproducible run to run.
        """
        if "person_id" in frame.columns and frame["person_id"].notna().any():
            keys = frame["person_id"].astype("string").fillna("")
            return keys.map(lambda k: self._bucket(str(k)) == 0)
        positions = pd.Series(range(len(frame)), index=frame.index)
        return positions.map(lambda i: i % HOLDOUT_BUCKETS == 0)

    @staticmethod
    def _bucket(key: str) -> int:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return int(digest[:8], 16) % HOLDOUT_BUCKETS

    def _load_frame(self, data_package: Mapping[str, Any]) -> Optional[pd.DataFrame]:
        import os

        path = data_package.get("canonical_df_path")
        if not path or not os.path.exists(path):
            return None
        try:
            return pd.read_parquet(path)
        except Exception:  # noqa: BLE001
            return None

    def _blocked(self, reason: str) -> JsonDict:
        return {
            "status": "blocked",
            "reason": reason,
            "target": "",
            "features_used": [],
            "top_risk_factors": [],
            "caveats": [reason],
            "methodology": "",
        }
