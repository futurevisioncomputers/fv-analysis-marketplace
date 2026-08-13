# Checkpoint protocol — shared by every stage skill

Every `fv-*` stage skill runs one stage and stops. This file is the part they
all share; each skill adds only what is specific to its stage.

## The call

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/run_stage.py" --stage <key> --json
```

Add a source on the first call of a run: `--csv`, `--excel`, `--sheet-url`, or
repeated `--source name=path.csv`. Add `--question "…"` so stage 1 can scope
the brief. Neither is needed once the session exists.

The session defaults to `.fv/session` in the working directory, which is how
one skill continues from another. `--session <dir>` keeps runs apart; `--new`
starts over.

**Prerequisites back-fill automatically.** Calling any stage on a bare CSV runs
everything it depends on first. Say so when it happens — the operator asked for
one stage and got four.

## The envelope

```jsonc
{
  "session":    "…/.fv/session",
  "ran":        [ /* one checkpoint per stage that executed */ ],
  "checkpoint": { /* the stage that was asked for — read this one */ },
  "blocked":    false,
  "progress":   [ /* the whole rail: what is done, what is left */ ]
}
```

`checkpoint` carries `summary`, `details`, `artifacts`, `offers`,
`next_stage`, `next_label`. When `blocked` is true it also carries `reason` and
`detail`.

## What to do with it

1. **Relay the summary and details.** They are computed from the stage's own
   result. Do not restate them as your own estimate, and never add a number
   that is not in the payload.
2. **Name the files.** `artifacts` has full paths.
3. **Offer what `offers` lists**, plus continuing to `next_stage`. Some offers
   carry a `command` — that is the exact flag to run.
4. **Then stop and wait.** Do not run the next stage until asked. If the
   operator says "keep going" or "do the whole thing", run `--auto` and stop
   offering checkpoints.

Record a choice when it changes the run:

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/run_stage.py" --decision "<stage>:<choice>"
```

## Exit codes

| Code | Meaning | What to do |
|---|---|---|
| 0 | ran | present the checkpoint |
| 2 | the stage refused | relay `reason` verbatim, stop |
| 3 | nothing left to run | say so |
| 1 | usage or read error | relay the message |

A refusal is not a crash. The agent declined and said why; nothing downstream
ran and nothing was invented. Guessing past it wastes the operator's time.

## Never

- Never echo raw PII. The frame is masked from stage 2 onward; a name in a
  quality note is already a hash.
- Never call a masked file "anonymous". Hashes are stable so records still
  join — that is the point, and the limit.
- Never fill in a number the CLI did not print.
- Never describe a stage as done when its status is `blocked`.
