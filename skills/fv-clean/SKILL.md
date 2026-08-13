---
name: fv-clean
description: Clean one institute sheet and stop at a checkpoint. Use when the user wants a sheet cleaned, PII-masked, quality-checked, or exported as a tidy CSV — admission form, enquiry form, student-data, fees-data, fees-recpit, certificate-data, or a timetable tab. Runs the Data Engineer stage (2), back-filling problem definition and schema validation if they have not run, then shows the quality report and offers the cleaned CSV before anything else happens. Triggers on "clean this sheet", "check data quality", "mask the PII", "give me a tidy CSV", "what is wrong with this file".
---

# Clean a sheet (stage 2)

Runs one stage and stops. The operator decides what happens next — that is the
point of this skill, not a limitation of it.

Cleaning is the only stage that sees raw PII. Everything it writes is already
masked, which is why the CSV at the end is safe to hand to someone.

## Run it

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/run_stage.py" \
  --csv "<sheet path>" \
  --question "<what the operator wants to know>" \
  --stage clean --json
```

Source flags, by input type:

| Input | Flag |
|---|---|
| `*.csv` | `--csv "<path>"` |
| `*.xlsx` | `--excel "<path>"` (every non-empty sheet becomes a source) |
| Google Sheet URL | `--sheet-url "<url>"` (snapshotted to `data/ingest/` first) |
| Several sheets together | repeat `--source name=path.csv` |

Notes on the arguments:

- `--question` is optional but worth passing. Stage 1 uses it to decide which
  modules matter, which changes what the cleaner derives. With no question, ask
  the operator what they want from the sheet; if they genuinely do not know,
  omit it.
- The session defaults to `.fv/session` in the working directory, so a later
  `/fv-eda` or `/fv-analyze` continues this exact run. Pass `--session <dir>`
  to keep several analyses apart, `--new` to start over.
- **Prerequisites back-fill automatically.** Calling this on a bare CSV runs
  problem definition and schema validation first. Say so when it happens —
  three stages ran, not one.

Read the JSON it prints. Everything below comes out of that object; do not
compute or estimate anything yourself. The envelope is the same shape however
many stages it took:

```jsonc
{
  "session":   "…/.fv/session",
  "ran":       [ /* one checkpoint per stage that executed */ ],
  "checkpoint":{ /* the stage you asked for — read this one */ },
  "blocked":   false,
  "progress":  [ /* the whole rail, for showing what is left */ ]
}
```

`checkpoint` carries `summary`, `details`, `artifacts`, `offers`, `next_stage`
and `next_label`. When `blocked` is true it also carries `reason` and `detail`.

## Show the operator

**1. What happened** — the `summary` line, then the `details`: rows kept out of
rows read, the person-identity basis, and the first few quality notes.

**2. Anything that changes what they can trust.** Do not bury these in a list:

- `person_id_basis` of `["name"]` alone means the sheet has no phone, DOB or
  email, so namesakes cannot be separated and person-level metrics
  (repeat-enrollment rate) are withheld. Tell them which sheet to add.
- A note about rows being **wider than the header** means the export has a
  trailing delimiter. It was handled, but their export is malformed.
- A note that an id **is not a row key** means receipt or certificate numbers
  repeat. Normal for this institute's per-branch receipt books; worth stating.
- Dropped rows. `dropped_reasons` says why. Rows without any usable date are
  dropped, not silently ignored.

**3. The files.** `artifacts` lists them with full paths. `cleaned.csv` is the
masked frame — hand them the path; it is safe to share.

## Then ask, and wait

Offer exactly what the JSON's `offers` array carries, plus the next stage from
`next_stage` / `next_label`. Typically:

- **Download the cleaned CSV** — give the path, and copy it wherever they ask.
- **Show the full quality report** —
  `run_stage.py --show clean --json`, then relay `known_issues` in full.
- **Continue to the next stage** — `--stage <next_stage>`. Once Feature
  Engineering lands this is stage 2.7; until then it is EDA.
- **Clean a different sheet** — re-run with the new source. Pointing the same
  session at different data discards what was computed from the old data, and
  the CLI says so.

Record what they chose so the report can explain itself later:

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/run_stage.py" --decision "clean:downloaded-csv"
```

Do not run the next stage until they ask. If they say "keep going" or "do
everything", then run `--auto` and stop offering checkpoints.

## When it blocks

Exit code 2 means the Data Engineer refused, and the JSON carries its reason.
Relay the reason verbatim — it is specific, and guessing over it wastes the
operator's time. Common causes:

- *No recognizable roles* — the headers do not resemble any known sheet. Ask
  which columns hold the name, the date and the amount.
- *Row count dropped more than 10%* — most rows have no parseable date. Usually
  a date-format mismatch: retry with `--date-format dmy` (or `mdy`). The live
  enquiry sheets are month-first; the admission sheet is day-first.

Nothing downstream ran and nothing was invented. Say that plainly.

## Never

- Never echo raw PII. Relay only what the CLI produced; the frame is hashed,
  and a name that appears in a quality note is already masked.
- Never describe the cleaned file as "anonymous". It is **masked** — hashes are
  stable so records can still be joined, which is the point and also the limit.
- Never fill in a number the CLI did not print.
