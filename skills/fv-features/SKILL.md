---
name: fv-features
description: Propose derived columns — duration groups, age bands, cohorts, outstanding buckets — and build only the ones approved. Runs Feature Engineering (stage 2.7) of the FV Institute pipeline and stops at a checkpoint, back-filling any prerequisite stages. Use when the user wants grouped or banded columns: short vs long courses, age groups, fee buckets, joining cohorts, batch slots, students ending soon. Triggers on "group by course length", "band the ages", "bucket the outstanding amounts", "add derived columns".
---

# Feature Engineering (stage 2.7)

Runs one stage and stops. The operator decides what happens next.

## Two passes, on purpose

The first call **proposes and builds nothing**. Every feature here is a policy
boundary — "short course" is a line someone drew, not a fact about the data —
so the operator owns it. The second call materializes exactly what they picked.

## Show them

For each proposal: the id, the rule in words, and the preview (value counts, or
min/median/max for a numeric one). Then the refusals — each says why, and a
refusal for coverage carries the number: *"only 10% populated (needs 40%)"*.

Do not present a proposal as a finding. It is an offer.

## Then ask

```bash
--approve-features all
--approve-features duration_group,age_band
--approve-features none
```

Two things to state before they choose:

- Approving **clears every stage after this one**. The frame changes, so
  results computed from the old frame cannot stand.
- Approved columns are written into the frame every later stage reads, and
  registered as roles, so they can be used as dimensions afterwards.

Thresholds are configurable. If the institute's idea of "short" is not 90 days,
that is a config change, not a caveat to write in the report.

## How to run it

Follow `skills/CHECKPOINT_PROTOCOL.md` — read it if it is not already in
context. In short:

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/run_stage.py" --stage features --json
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
