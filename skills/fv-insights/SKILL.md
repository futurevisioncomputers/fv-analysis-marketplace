---
name: fv-insights
description: Turn the metrics into findings, root causes and risks — with the evidence attached. Runs Insights (stage 6) of the FV Institute pipeline and stops at a checkpoint, back-filling any prerequisite stages. Use when the user asks what the numbers mean, why something changed, what is going wrong or well. Triggers on "what does this mean", "why did it drop", "what should I worry about", "summarize the findings".
---

# Insights (stage 6)

Runs one stage and stops. The operator decides what happens next.

## Show them

The executive summary, then findings, root causes and risks. Each carries the
metric, the filter, the comparison baseline and the supporting count — relay
those with the claim, not separately. A finding without its baseline is an
assertion.

## The line this stage does not cross

**Correlation is not causation, and a root cause here is a hypothesis with
evidence, not a proven mechanism.** If the payload hedges, keep the hedge. Do
not upgrade "associated with" into "caused by" because it reads better.

Watch for these institute-specific traps when relaying:

- A branch difference on small numbers — check `n` and the interval first.
- A faculty comparison. This is **allocation and outcome**, never teaching
  quality: there is no attendance, assessment or feedback data, so a tutor with
  worse completion may simply have been given the harder cohort.
- A staff alumnus flagged in the data is one human in two roles, not a duplicate.

## Then ask

- Show findings, causes and risks in full.
- Continue to recommendations.

## How to run it

Follow `skills/CHECKPOINT_PROTOCOL.md` — read it if it is not already in
context. In short:

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/run_stage.py" --stage insights --json
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
