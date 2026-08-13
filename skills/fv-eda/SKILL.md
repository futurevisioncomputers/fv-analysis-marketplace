---
name: fv-eda
description: Profile distributions, time trends, cross-tabs and anomalies. Runs Exploratory Data Analysis (stage 3) of the FV Institute pipeline and stops at a checkpoint, back-filling any prerequisite stages. Use when the user wants to explore or understand a sheet before analysis: distributions, spikes and dips, what is unusual, how two columns relate. Triggers on "explore this", "what's odd in this data", "show me the trends", "profile these columns".
---

# Exploratory Data Analysis (stage 3)

Runs one stage and stops. The operator decides what happens next.

## Show them

- How many dimensions and numeric fields were profiled, and the trend
  direction.
- The **anomalies**, rendered from `details`: type, metric, period, value, and
  the change against the trailing average.

An anomaly is a shape in the data, not an explanation. A dip in July is a dip
in July; the institute's holiday calendar is a hypothesis, and this stage has
no way to test it. Say what was observed, and leave the cause to the operator
unless a later stage evidences it.

## Then ask

- Show the full profile: per-dimension distributions and per-numeric summaries.
- Continue to the Analyst.

EDA is context, not a gate. If it blocks, the run continues without it and the
report says so — that is deliberate.

## How to run it

Follow `skills/CHECKPOINT_PROTOCOL.md` — read it if it is not already in
context. In short:

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/run_stage.py" --stage eda --json
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
