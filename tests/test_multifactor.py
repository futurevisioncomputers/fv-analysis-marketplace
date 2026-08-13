"""Tests for the multi-factor engine (spec §21/§23).

The fixtures are built so the right answer is computable by hand: a planted
interaction that only exists in one cell, margins that explain everything, and
grids too thin to test.

Run: python -m tests.test_multifactor   (plain asserts, no pytest dep)
"""

from __future__ import annotations

import sys

import numpy as np
import pandas as pd

from agents import multifactor as mf


def _planted(n_per_cell: int = 200, base: float = 0.05,
             hot: float = 0.30, seed: int = 7) -> pd.DataFrame:
    """2x2 grid, one hot cell. Everything else defaults at the base rate."""
    rng = np.random.default_rng(seed)
    rows = []
    for branch in ("citylight", "vesu"):
        for course in ("excel", "python"):
            rate = hot if (branch, course) == ("citylight", "excel") else base
            for _ in range(n_per_cell):
                rows.append({"Branch": branch, "Course": course,
                             "is_default": bool(rng.random() < rate),
                             "Amt Pending": float(rng.integers(0, 5000))})
    return pd.DataFrame(rows)


def test_planted_interaction_is_found_and_named() -> None:
    """The cell that beats both margins is the one that was planted."""
    df = _planted()
    res = mf.crosstab(df, rows="Branch", cols="Course",
                      value="is_default", kind="rate")

    assert res["rows_analysed"] == 800
    assert res["independence"]["valid"] is True
    assert res["independence"]["p_value"] < 0.001

    assert len(res["interactions"]) >= 1
    top = res["interactions"][0]
    assert (top["row"], top["col"]) == ("citylight", "excel")
    assert top["beats_row_margin"] is True and top["beats_col_margin"] is True
    # The additive expectation is what the margins predict. Note the dilution:
    # the hot cell inflates its own row and column margins, so the residual
    # (0.305 observed vs 0.2475 expected) understates the planted effect. The
    # verdict comes from the tests against the margins, not from the residual's
    # size — the residual only ranks.
    assert top["value"] > 0.25
    assert top["residual"] > 0.04


def test_a_pure_margin_effect_is_not_called_an_interaction() -> None:
    """One branch uniformly worse across every course: a branch effect.

    Each of its cells is high for its column, but NOT high for its row —
    within the bad branch, every course looks the same. Crossing added
    nothing, and the engine must say so rather than dress the branch effect
    up as four cell-level findings.
    """
    rng = np.random.default_rng(11)
    rows = []
    for branch, rate in (("citylight", 0.25), ("vesu", 0.05)):
        for course in ("excel", "python"):
            for _ in range(300):
                rows.append({"Branch": branch, "Course": course,
                             "is_default": bool(rng.random() < rate)})
    res = mf.crosstab(pd.DataFrame(rows), rows="Branch", cols="Course",
                      value="is_default", kind="rate")

    assert res["interactions"] == []
    assert any("explained by one dimension" in n for n in res["notes"])


def test_small_cells_are_suppressed_not_shown() -> None:
    """A 12-row cell moves 8 points when one student changes. Suppress it."""
    df = _planted(n_per_cell=200)
    thin = pd.DataFrame([{"Branch": "pal", "Course": "excel",
                          "is_default": True, "Amt Pending": 100.0}] * 12)
    res = mf.crosstab(pd.concat([df, thin], ignore_index=True),
                      rows="Branch", cols="Course",
                      value="is_default", kind="rate")

    shown = {(c["row"], c["col"]) for c in res["cells"]}
    assert ("pal", "excel") not in shown
    hidden = next(c for c in res["suppressed"] if c["row"] == "pal")
    assert hidden["n"] == 12
    assert any("suppressed" in n for n in res["notes"])


def test_filters_are_applied_and_reported() -> None:
    df = _planted()
    spec = mf.parse_filter("Branch = citylight")
    res = mf.crosstab(df, rows="Branch", cols="Course",
                      value="is_default", kind="rate", filters=[spec])

    assert res["rows_analysed"] == 400
    report = res["filters"][0]
    assert report["rows_before"] == 800 and report["rows_after"] == 400
    assert not report["skipped"]

    # Numeric and membership forms.
    ge = mf.parse_filter("Amt Pending >= 2500")
    assert ge == {"field": "Amt Pending", "op": ">=", "value": "2500"}
    inn = mf.parse_filter("Course in excel, python")
    assert inn["op"] == "in" and inn["values"] == ["excel", "python"]

    # Garbage does not silently pass.
    try:
        mf.parse_filter("just some words")
    except ValueError as exc:
        assert "cannot parse" in str(exc)
    else:
        raise AssertionError("an unparseable filter must raise, not no-op")


def test_a_filter_naming_a_missing_column_is_reported_not_ignored() -> None:
    """Skipping silently would label an unfiltered number as filtered."""
    df = _planted()
    res = mf.crosstab(df, rows="Branch", cols="Course", value="is_default",
                      kind="rate",
                      filters=[{"field": "Nope", "op": "=", "value": "x"}])
    report = res["filters"][0]
    assert report["skipped"] is True
    assert "Nope" in report["reason"]
    # And the data is untouched.
    assert res["rows_analysed"] == 800


def test_crossing_a_dimension_with_itself_is_refused() -> None:
    df = _planted()
    res = mf.crosstab(df, rows="Branch", cols="Branch",
                      value="is_default", kind="rate")
    assert res["blocked"]
    assert "itself" in res["blocked"]


def test_suggest_pairs_offers_only_what_the_source_supports() -> None:
    df = pd.DataFrame({
        "Branch": ["vesu", "pal", "vesu", "pal"],
        "Course": ["a", "b", "a", "b"],
        "Mode of Payment": ["cash", "cash", "cash", "cash"],  # one level
    })
    roles = {"branch": "Branch", "course": "Course",
             "payment_mode": "Mode of Payment"}
    pairs = mf.suggest_pairs(roles, df)
    keys = {(p["rows"], p["cols"]) for p in pairs}
    assert ("branch", "course") in keys
    # payment_mode has a single level; crossing by it is a one-row table.
    assert not any("payment_mode" in k for pair in keys for k in pair)
    # And a role the source lacks is never offered.
    assert not any("faculty" in k for pair in keys for k in pair)


def test_mean_kind_uses_welch_and_finds_the_expensive_cell() -> None:
    rng = np.random.default_rng(3)
    rows = []
    for branch in ("citylight", "vesu"):
        for course in ("excel", "python"):
            centre = 9000 if (branch, course) == ("vesu", "python") else 4000
            for _ in range(120):
                rows.append({"Branch": branch, "Course": course,
                             "Total Fees": float(rng.normal(centre, 800))})
    res = mf.crosstab(pd.DataFrame(rows), rows="Branch", cols="Course",
                      value="Total Fees", kind="mean")
    top = res["interactions"][0]
    assert (top["row"], top["col"]) == ("vesu", "python")


def test_two_mean_test_known_answer() -> None:
    """Hand-checked Welch z: means 10 vs 12, sd 2, n 50 each.

    se = sqrt(4/50 + 4/50) = 0.4; z = -5; p ~ 5.7e-7.
    """
    from agents import statistics as st
    res = st.two_mean_test(10.0, 2.0, 50, 12.0, 2.0, 50)
    assert res["z"] == -5.0
    assert res["p_value"] <= 1e-6  # true value 5.7e-7; reported at 6 decimals
    assert res["difference_ci_95"] == (-2.784, -1.216)


def test_chi_square_independence_known_answer() -> None:
    """Classic 2x2: [[20,30],[30,20]] -> chi2 = 4.0, dof 1, p ~ 0.0455."""
    from agents import statistics as st
    res = st.chi_square_independence([[20, 30], [30, 20]])
    assert res["valid"] is True
    assert abs(res["statistic"] - 4.0) < 1e-9
    assert abs(res["p_value"] - 0.0455) < 0.001

    # Thin table: statistic reported, p-value withheld.
    thin = st.chi_square_independence([[2, 3], [3, 2]])
    assert thin["valid"] is False and thin["p_value"] is None
    assert "fewer than 5" in thin["reason"]


def test_grid_wider_than_max_levels_is_trimmed_and_says_so() -> None:
    rng = np.random.default_rng(5)
    df = pd.DataFrame({
        "Branch": rng.choice(["a", "b", "c"], 3000),
        "Course": rng.choice([f"c{i}" for i in range(30)], 3000),
        "is_default": rng.random(3000) < 0.05,
    })
    res = mf.crosstab(df, rows="Branch", cols="Course",
                      value="is_default", kind="rate")
    seen_cols = {c["col"] for c in res["cells"]} | {c["col"] for c in res["suppressed"]}
    assert len(seen_cols) <= mf.MAX_LEVELS
    assert any("largest levels" in n for n in res["notes"])


def _run() -> int:
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run())
