---
name: fv-metrics
description: Compute the headline metric for every question, with a 95% confidence interval and breakdowns. Runs Analyst (stage 4) of the FV Institute pipeline and stops at a checkpoint, back-filling any prerequisite stages. Use when the user wants the actual numbers: conversion rate, collection efficiency, pending fee, completion rate, churn rate, certificate lag, by branch or course or faculty. Triggers on "what is my conversion rate", "how much is pending", "compare branches", "give me the numbers".
---

# Analyst (stage 4)

Runs one stage and stops. The operator decides what happens next.

## Show them

Per question: the metric, its value, `n`, and the **95% confidence interval**.
Report the interval every time. A 4-point gap between two branches with 22
students each is not a gap, and the interval is what makes that visible.

Then the **skipped** questions with their reasons. These are not failures to
gloss over — a skipped question is the pipeline refusing to invent a number.
Two reasons are worth expanding:

- *"this source has no phone, date-of-birth or email…"* — the sheet cannot tell
  namesakes apart, so person-level metrics are withheld. Name the sheet to add.
- *"required column missing"* — the sheet does not carry the concept at all.

## Segments

A breakdown segment under 30 rows is flagged low-confidence. Relay that flag;
it is the difference between a finding and a coincidence.

## Then ask

- Show every metric with its interval and breakdowns.
- Show what could not be computed, and why.
- Continue to visualization.

## How to run it

Follow `skills/CHECKPOINT_PROTOCOL.md` — read it if it is not already in
context. In short:

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/run_stage.py" --stage analyst --json
```

Add `--csv <path>` (or `--excel` / `--sheet-url` / repeated `--source`) and
`--question "…"` on the first call of a run; neither is needed afterwards.
Prerequisites back-fill automatically, so this works on a bare CSV — say so
when several stages had to run.

Read the `checkpoint` object from the JSON envelope. Relay its `summary` and
`details`, name the files in `artifacts`, offer what `offers` lists plus
continuing to `next_stage`, then **stop and wait**.

Exit code 2 means the stage refused: relay `reason` verbatim and stop. Nothing
downstream ran and nothing was invented.
