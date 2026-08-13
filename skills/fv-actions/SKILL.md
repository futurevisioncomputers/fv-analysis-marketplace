---
name: fv-actions
description: Propose prioritized, owner-tagged actions from the insights. Runs Recommendation (stage 6.5) of the FV Institute pipeline and stops at a checkpoint, back-filling any prerequisite stages. Use when the user asks what to do about the findings, wants an action plan, or asks what to fix first. Triggers on "what should I do", "give me actions", "what's the priority", "how do I fix this".
---

# Recommendation (stage 6.5)

Runs one stage and stops. The operator decides what happens next.

## Show them

Actions by priority bucket, each with its owner and the finding it came from.
This is the only stage that proposes actions — if an earlier stage's output
sounded like an instruction, it was a finding being over-read.

## Match the action to who owns the sheet

The workbook tells you the audience: admission and enquiry sheets belong to
**counsellors**, the student-data workbook to **admin**, the timetable workbook
to **faculty**. An action a counsellor cannot take does not belong in a
counsellor's report.

## Then ask

- Show the actions with their owners and priorities.
- Continue to monitoring.

If the operator disputes an action, that is worth recording:
`--decision "recommend:rejected:<reason>"`. The report can then explain itself.

## How to run it

Follow `skills/CHECKPOINT_PROTOCOL.md` — read it if it is not already in
context. In short:

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/run_stage.py" --stage recommend --json
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
