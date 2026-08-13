---
description: Walk a sheet through the full analysis one stage at a time, pausing at each checkpoint so you stay in control
argument-hint: <csv-or-xlsx-path-or-sheet-url> [business question]
allowed-tools: Bash, Read
---

Start a guided run. Follow the `fv-run` skill for the walk and each stage's own
skill for what to show; this command only resolves the arguments and starts it.

User input: `$ARGUMENTS`

1. **Parse the input.** Leading tokens that look like paths or URLs are
   sources; the rest is the business question. Ask for the source if none is
   given. Ask for the question too if it is missing — stage 1 scopes the entire
   run from it.

   - one `.csv` → `--csv <path>`
   - one `.xlsx` → `--excel <path>` (every non-empty sheet becomes a source)
   - `http…` → `--sheet-url <url>`, repeatable
   - **several files** → one `--source name=path` each. The `name=` prefix is
     required; derive the name from the file stem and keep the institute's own
     tab name where you can, since every join and quality note refers to it.

2. **Start at stage 1:**

   ```bash
   python "${CLAUDE_PLUGIN_ROOT}/scripts/run_stage.py" --new \
     --csv "<source>" --question "<question>" --stage problem --json
   ```

   or, for several sheets:

   ```bash
   python "${CLAUDE_PLUGIN_ROOT}/scripts/run_stage.py" --new \
     --source student_data=<path> --source fees_data=<path> \
     --question "<question>" --stage problem --json
   ```

3. **Present the checkpoint and wait.** Pay particular attention to
   `data_needs`: it says which questions this sheet cannot answer and which
   file would unlock them. Then advance one stage at a time with
   `--stage next`, pausing after each.

Use `--auto` only if the user asks for the whole thing unattended.
