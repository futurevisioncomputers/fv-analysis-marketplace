---
name: fv-charts
description: Build chart configurations and KPI cards for the answered questions. Runs Visualization (stage 5) of the FV Institute pipeline and stops at a checkpoint, back-filling any prerequisite stages. Use when the user wants charts, graphs, a dashboard view, or KPI cards from their data. Triggers on "chart this", "show me a graph", "build a dashboard", "visualize the trend".
---

# Visualization (stage 5)

Runs one stage and stops. The operator decides what happens next.

## Show them

How many charts and KPI cards were built, per question, and what each chart
shows. Charts are Chart.js configurations rendered into the HTML report — they
are not images, and there is nothing to open until stage 8.

Every chart is built from a computed metric. There is no chart for a skipped
question, and that absence is correct: a chart of nothing is worse than no
chart.

## Then ask

- List the charts and cards.
- Continue to insights.

## How to run it

Follow `skills/CHECKPOINT_PROTOCOL.md` — read it if it is not already in
context. In short:

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/run_stage.py" --stage visualize --json
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
