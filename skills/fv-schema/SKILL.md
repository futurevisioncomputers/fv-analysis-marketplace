---
name: fv-schema
description: Validate the sheets against the questions and plan any joins. Runs Schema / Source Plan (stage 2.5) of the FV Institute pipeline and stops at a checkpoint, back-filling any prerequisite stages. Use when the user asks how their columns map, whether two sheets can be joined, or why a column was interpreted a certain way. Triggers on "what did you map this to", "can you join these sheets", "check my columns".
---

# Schema / Source Plan (stage 2.5)

Runs one stage and stops. The operator decides what happens next.

## What this stage decides

Which questions the supplied sources can support, how the columns map to
roles, and — with several sheets — which keys join them.

## Show them

- The per-question verdict from `details`.
- The **column → role mapping**, and with it the institute's canonical name for
  each column (`field_names` on the clean package once stage 2 has run).
- The **join plan** when there is more than one source: which key, and how many
  rows match. The estate has one weak edge — enquiry → admission by phone,
  which recovers roughly 78–81% — so a join here is a measured coverage, not a
  promise.
- `dataset_mode`: `institute` when the sheet matches the known shape, `generic`
  when the Analyst is falling back to whatever columns exist.

## Then ask

- Show the mapping in full.
- **Correct a mapping** — if a column was read wrongly, that is a role-matcher
  fix, not something to work around here. Report it precisely: the header, what
  it was mapped to, what it should be.
- Continue to cleaning.

## How to run it

Follow `skills/CHECKPOINT_PROTOCOL.md` — read it if it is not already in
context. In short:

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/run_stage.py" --stage schema --json
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
