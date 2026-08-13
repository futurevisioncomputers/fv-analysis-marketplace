---
description: Clean and PII-mask one institute sheet (stage 2), then stop at a checkpoint — quality report, cleaned CSV, and your choice of what runs next
argument-hint: <csv-or-xlsx-path-or-google-sheet-url> [business question]
allowed-tools: Bash, Read
---

Run the Data Engineer stage for the user and stop there. Follow the `fv-clean`
skill for what to show and what to offer — this command only resolves the
arguments and invokes it.

User input: `$ARGUMENTS`

1. **Parse the input.** The first token is the source; anything after it is the
   business question. Pick the flag by type: `http…` → `--sheet-url`, `.xlsx` →
   `--excel`, `.csv` → `--csv`. If no source is given, ask for one.

2. **Run the stage.** Prerequisites back-fill automatically, so this works on a
   bare CSV:

   ```bash
   python "${CLAUDE_PLUGIN_ROOT}/scripts/run_stage.py" \
     --csv "<source>" --question "<question>" --stage clean --json
   ```

3. **Present the checkpoint and wait.** Summary, rows kept, person-identity
   basis, quality notes, and the path to `cleaned.csv`. Then offer: download the
   CSV · show the full quality report · continue to the next stage · clean a
   different sheet. Do not continue until they choose.

The session lives at `.fv/session`, so `/fv-eda` and `/fv-analyze` pick up
exactly where this left off. Use `--session <dir>` to keep runs apart and
`--new` to start fresh.

Exit code 2 means the stage refused; relay its reason verbatim and stop.
