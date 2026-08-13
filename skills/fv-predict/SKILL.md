---
name: fv-predict
description: Train an explainable churn model on the completion labels, or refuse and say why. Runs Prediction (stage 4.5, optional) of the FV Institute pipeline and stops at a checkpoint, back-filling any prerequisite stages. Use when the user asks to predict or model churn, who is likely to drop out, or which factors drive not-coming. Triggers on "predict churn", "who is at risk", "build a model", "what drives dropout".
---

# Prediction (stage 4.5, optional)

Runs one stage and stops. The operator decides what happens next.

## This stage is allowed to refuse

It trains only on **terminal** outcomes — completed vs not-coming — because a
student still attending has no outcome yet. Those rows are censored, not
negative examples. It refuses outright when labels are missing, single-class,
or too few (40 labelled rows, 10 per class), and says which.

A refusal here is the correct result on most single sheets. The labels come
from timetable tab membership, so a fee sheet alone cannot produce them.
Relay the reason and name what would supply it.

## Show them

- Accuracy **and the majority-class baseline**, always together. A model that
  only learned the base rate must be visibly worthless, not quietly reported as
  "83% accurate".
- Train/test sizes. The holdout is a stable hash of `person_id`, so all rows
  for one person land on the same side and the number is reproducible.
- The per-feature likelihood ratios — this model is readable, so read it.

## Never

Never turn a prediction into a recommendation here. "Likely to churn" is not
"call them"; that is the Recommendation stage's job, and it needs the insight
in between.

## How to run it

Follow `skills/CHECKPOINT_PROTOCOL.md` — read it if it is not already in
context. In short:

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/run_stage.py" --stage predict --json
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
