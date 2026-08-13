---
description: Profile distributions, time trends, cross-tabs and anomalies (Exploratory Data Analysis (stage 3)), then stop at a checkpoint
argument-hint: [csv-or-xlsx-path-or-sheet-url] [business question]
allowed-tools: Bash, Read
---

Run Exploratory Data Analysis (stage 3) for the user and stop there. Follow the `fv-eda` skill for
what to show and what to offer; this command only resolves the arguments.

User input: `$ARGUMENTS`

1. If a source is given, pick its flag: `http…` → `--sheet-url`, `.xlsx` →
   `--excel`, `.csv` → `--csv`. Anything after it is the business question.
   With no source, continue the existing session at `.fv/session`.
2. Run it — prerequisites back-fill automatically:

   ```bash
   python "${CLAUDE_PLUGIN_ROOT}/scripts/run_stage.py" \
     [--csv "<source>"] [--question "<question>"] --stage eda --json
   ```

3. Present the checkpoint and wait. Exit code 2 means the stage refused —
   relay its reason verbatim and stop.
