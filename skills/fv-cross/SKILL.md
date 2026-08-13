---
name: fv-cross
description: Cross one metric by two dimensions at once — which course underperforms at which branch, which tutor struggles with which batch slot, which lead source converts at which site. Use when a single breakdown has not explained something, when the user asks about a combination rather than one factor, or when they want to filter to a slice before comparing. Runs over a session that has already been cleaned. Triggers on "by branch and course", "which combination", "where exactly is the problem", "break it down further", "drill into", "cross-tab", "for programming courses only", "is it a branch problem or a course problem".
---

# Multi-factor analysis

One metric across **two** dimensions, with a filter layer. Every other stage
answers one metric by one dimension; this answers the question that needs two.

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/run_cross.py" --value <col> --rows <dim> --cols <dim>
```

Reads the session's cleaned frame, so `--stage clean` must have run. Writes
nothing back. Add `--json` for the raw result; `--session <dir>` for another run.

## Why two dimensions

```
Citylight defaults at 7.9%.  Graphic Designing defaults at 8.1%.
Citylight × Graphic Designing defaults at 22%.
```

Neither single breakdown shows it. Both look mildly above average, and the real
problem — one course at one branch — is averaged away in each. That is the only
reason to reach for this tool; if a one-dimension breakdown already answers the
question, use `/fv-metrics` or `/fv-stats segments` instead.

## Start with what the data supports

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/run_cross.py" --suggest
```

Lists only pairs whose two roles both resolve **and** have two or more levels
each. Do not invent a pair the source cannot carry — crossing by a column that
holds one value everywhere produces a single row that reads like an analysis.

## The kinds

| `--kind` | Measures | Needs |
|---|---|---|
| `rate` *(default)* | share of a 0/1 flag | `--value is_default` |
| `mean` | average of a number | `--value paid` |
| `sum` | total | `--value paid` |
| `count` | rows per cell | nothing |
| `ratio` | Σnum / Σdenom | `--value paid --denom amount` |

## Filters

Repeatable, applied before anything is computed, and reported with rows in and
out so a thin answer can be traced to the filter that caused it.

```bash
--filter "course category = programming"
--filter "Total Fees >= 5000"
--filter "branch in vesu,pal"
--filter "Branch != citylight"
```

An unparseable filter is an error, not a no-op. A filter naming a column that
does not exist is **reported as skipped** — never silently dropped, because a
number labelled as filtered that is not is worse than no number.

## Reading the output

The grid prints rows down, columns across, margins on both edges.

```
  ·  suppressed — the cell is below the row floor
  —  no rows at all
  *  an interaction: this cell beats BOTH its margins
```

Four things guard against reading noise as a finding. **Relay all four, not
just the grid.**

1. **A chi-square runs over the whole grid first.** For a rate it tests the
   metric; for anything else it tests only whether the two dimensions are
   evenly crossed, and the CLI words those differently. Do not upgrade the
   second into a finding about the measure.
2. **A cell must beat both margins to count.** High for its branch alone is a
   branch effect. High for its course alone is a course effect. Only high
   against both, tested against both, needed two dimensions to see.
3. **Every cell test is BH-corrected** across the grid. Eighty-one cells
   produce four "significant" ones at the 5% level by construction.
4. **Cells under the floor are suppressed and counted**, not shown small. Raise
   or lower it with `--min-cell`, and say which floor produced the answer.

## Exit codes

`0` an interaction was found · `1` usage or missing data · `2` the crossing
refused, with its reason · `3` the grid computed cleanly and **nothing beat
both margins**.

Exit 3 is a real answer and the most common one on institute-sized data. Report
it as *"one dimension explains everything here — crossing them added nothing"*,
not as a failure and not by picking the largest cell anyway.

## What to say back

- **Interaction found** → name the cell, its n, and what each dimension alone
  predicted. The gap between the cell and the additive expectation is the whole
  finding.
- **Nothing found** → say so plainly. Then say whether it is because the factors
  really are independent or because the grid was too thin — the notes
  distinguish these and the difference decides whether more data would help.
- **Most cells suppressed** → lead with that. A 3×27 grid on 400 rows cannot
  support a crossing, and the honest answer is to cross by course *category*
  instead, or to filter to one branch and compare courses within it.

Never quote a cell that the tool suppressed, and never lower `--min-cell` to
make a finding appear.
