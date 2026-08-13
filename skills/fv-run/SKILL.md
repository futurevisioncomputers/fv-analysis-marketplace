---
name: fv-run
description: Walk an institute sheet through the whole analysis one stage at a time, pausing after each so the operator can see the output, export it, change an answer, or stop. Use when the user wants a full analysis but wants to stay in control of it, or does not yet know what they want and needs to see each step. Triggers on "walk me through this", "analyze this step by step", "run the pipeline but let me check each stage", "start an analysis". For a single stage use the stage skill instead (fv-clean, fv-metrics, …); for an unattended run use /fv-analyze.
---

# Guided run

Twelve stages, a checkpoint after each. This skill is the walk; each stage's
own skill has the detail for that stage, and `skills/CHECKPOINT_PROTOCOL.md`
has the mechanics they share.

## Start

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/run_stage.py" --new \
  --csv "<sheet>" --question "<what they want to know>" --stage problem --json
```

Ask for the question if they have not given one — stage 1 scopes the whole run
from it, and "analyze this sheet" produces a generic brief that answers nothing
in particular.

### More than one sheet

Repeat `--source name=path.csv`, one per sheet. **The `name=` prefix is
required** — a bare path is rejected — and the name is what every join note and
quality message calls that sheet, so use the institute's own tab name.

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/run_stage.py" --new \
  --source student_data=student-data.csv \
  --source fees_data=fees-data.csv \
  --source fees_recpit=fees-recpit.csv \
  --question "..." --stage problem --json
```

A whole workbook at once: `--excel book.xlsx` makes every non-empty sheet a
source, named after the tab. Google Sheets: `--sheet-url` per published tab.

Two things to say at the stage-2 checkpoint, because both read wrong otherwise:

- **The row count is the master frame, not a total.** Four sheets totalling
  1,912 rows produce 420 — 772 receipt rows fold into the enrollments they
  belong to. That is a join, not loss, and the summary now says so.
- **An unjoined source is not an error.** It is listed with the reason no safe
  key was found; its numbers are still computed, just not merged.

Ask for a second sheet only when the question needs it. `--suggest` on
`/fv-cross` and the stage-1 `data_needs` block both name the missing file when
one is genuinely required.

## The rail

| # | Stage | `--stage` | The decision at this checkpoint |
|---|---|---|---|
| 1 | Problem Definition | `problem` | are these the right questions, and can this sheet answer them |
| 2.5 | Schema / Source Plan | `schema` | did the columns map correctly |
| 2 | Data Engineer | `clean` | is the cleaned data trustworthy · export it |
| 2.7 | Feature Engineering | `features` | which derived columns to build *(optional)* |
| 3 | EDA | `eda` | what is unusual before we measure |
| 4 | Analyst | `analyst` | the numbers, with intervals |
| 4.5 | Prediction | `predict` | a churn model, if the labels support one *(optional)* |
| 5 | Visualization | `visualize` | which charts |
| 6 | Insights | `insights` | what the numbers mean |
| 6.5 | Recommendation | `recommend` | what to do |
| 7 | Monitoring | `monitor` | what to watch from here |
| 8 | Report Writer | `report` | the shareable HTML |

Advance with `--stage next`. The two optional stages are skipped by `next` and
run only when named.

## At every checkpoint

Relay `summary` and `details`, name the files, offer what `offers` lists plus
the next stage, then **wait**. Never run two stages because the first went
well — the pause is the feature.

Three checkpoints carry a decision worth pressing on:

- **Stage 1** — `data_needs` says which questions this sheet cannot answer and
  which file would fix that. If most are blocked, fetching one sheet usually
  beats accepting a third of an answer.
- **Stage 2** — the quality report. A name-only person key, a malformed export,
  or repeated ids that are not row keys all change what the later numbers mean.
- **Stage 2.7** — nothing is built until they approve it, and approving clears
  every stage after it.

## Changing an answer mid-run

Adding a question, approving features, or pointing at a different sheet all
invalidate what was computed before them. The CLI does the clearing and says
what it cleared; relay that rather than quietly re-running.

```bash
--reset-from analyst          # discard a stage and everything after it
--approve-features all        # clears from 2.7 onward
--csv <different sheet>       # clears the run
```

## Shortcuts they may ask for

- **"Just do the whole thing"** → `--auto`, and stop offering checkpoints.
- **"Where are we?"** → `--status`.
- **"Show me stage N again"** → `--show <key>`.
- **"Start over"** → `--new`.

## When a stage refuses

Exit code 2. Relay the reason verbatim, say that nothing downstream ran, and
offer the fix it implies — usually another sheet, a `--date-format`, or a
narrower question. Do not skip past a refusal to keep the walk moving.
