---
name: fv-questions
description: Turn a business question into a scoped brief, and check whether this sheet can actually answer it. Runs Problem Definition (stage 1) of the FV Institute pipeline and stops at a checkpoint, back-filling any prerequisite stages. Use when the user asks what a sheet can tell them, wants to frame or add business questions, or asks whether they have enough data for a question. Triggers on "what can I ask of this file", "add a question", "do I need more data", "scope this analysis".
---

# Problem Definition (stage 1)

Runs one stage and stops. The operator decides what happens next.

## What this stage decides

It reads the operator's words, not the data: modules, business questions, KPI
targets and the time window. So it will happily frame a certificate question
against an admission form — which is why the checkpoint carries a capability
check.

## Show them

**The questions**, from `details`: id, text, and the metric each will be
answered with.

**`data_needs`** — the part that matters. It is on the checkpoint object:

- `answerable` / `blocked` counts.
- Per question: ✓ with the metric it will use, or ✗ with the roles it lacks.
- `substituted: true` means the question will be answered with a **fallback
  metric**, not the one asked for. Say which, both times — answering "how much
  was collected?" with pending fee is a different answer.
- `missing_data`: each gap names the sheet that carries it and which questions
  it unlocks. That is the actionable part — "add fees-data, unlocks 4 of 7".

## Then ask

- Show the full brief.
- **Add a question** — re-run with a `--question` that includes it. This
  re-runs stage 1 and clears everything downstream, so say that first.
- **Fetch a missing sheet** and re-run with an extra `--source`.
- Continue to the schema check.

If most questions are blocked, do not just continue. Adding one sheet is
usually cheaper than accepting a report that answers a third of what was asked.

## How to run it

Follow `skills/CHECKPOINT_PROTOCOL.md` — read it if it is not already in
context. In short:

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/run_stage.py" --stage problem --json
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
