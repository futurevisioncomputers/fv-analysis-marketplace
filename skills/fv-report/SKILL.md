---
name: fv-report
description: Compose the shareable HTML report from the run, or re-render an earlier one from its saved JSON. Runs Report Writer (stage 8) of the FV Institute pipeline and stops at a checkpoint, back-filling any prerequisite stages. Use when the user wants the final report, a shareable document, an HTML summary, or wants to regenerate a past report without re-analyzing. Triggers on "build the report", "give me something to share", "export this", "write it up", "re-render that report".
---

# Report Writer (stage 8)

Runs one stage and stops. The operator decides what happens next.

## Show them

The path to the HTML file, and what is in it: executive summary, KPI scorecard,
trend, recommendations, monitoring, and the data-quality footer.

The report asserts before it is written that no 10-digit mobile number survives
in it. Say the file is safe to share, and say why — the PII was masked at stage
2, not stripped at the end.

## What is in it is what ran

Skipped questions appear as skipped, with reasons. Do not describe the report
as covering questions it declined to answer.

## Then ask

- Open it.
- Start a new analysis on another sheet: `--new --csv <path>`.

## Re-rendering an earlier report

If the operator has a saved run JSON and wants the HTML rebuilt — restyled, or
just lost — there is no need to re-analyze anything:

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/run_pipeline.py" \
  --from-json "<run.json>" --out "<report.html>"
```

The JSON holds `final_report` and `question_results`, so the numbers are the
ones from that run, not recomputed. Say which run it came from.

## How to run it

Follow `skills/CHECKPOINT_PROTOCOL.md` — read it if it is not already in
context. In short:

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/run_stage.py" --stage report --json
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
