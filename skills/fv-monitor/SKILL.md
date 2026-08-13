---
name: fv-monitor
description: Register KPI hooks and evaluate them, raising threshold alerts. Runs Monitoring (stage 7) of the FV Institute pipeline and stops at a checkpoint, back-filling any prerequisite stages. Use when the user wants ongoing alerts, KPI thresholds, or to know what to watch. Triggers on "set up alerts", "monitor this KPI", "warn me when", "what should I keep an eye on".
---

# Monitoring (stage 7)

Runs one stage and stops. The operator decides what happens next.

## Show them

Overall health, the count of active alerts, and each event: metric, what fired,
and against which threshold.

Only hooks whose metric the Analyst can actually compute become active. The
rest register **inactive** — visible, but not pretending to watch something
that cannot be measured on this data.

## The registry persists

Thresholds are meant to survive between runs, so the registry lives beside the
session (or wherever `--registry` points). One consequence to watch: repeated
runs against the same source accumulate hooks, and the alert count climbs. If
the number looks inflated, check whether it is the same alert registered
several times.

## Then ask

- Show the hooks and alerts.
- Continue to the report.

## How to run it

Follow `skills/CHECKPOINT_PROTOCOL.md` — read it if it is not already in
context. In short:

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/run_stage.py" --stage monitor --json
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
