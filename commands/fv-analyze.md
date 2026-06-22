---
description: Run the FV Institute analytics pipeline on a sheet and produce an HTML report
argument-hint: <csv-path> [business question]
allowed-tools: Bash, Read
---

You are running the FV Institute analytics pipeline for the user.

User input: `$ARGUMENTS`

Steps:

1. Parse the input. The first token is the path to a CSV/XLSX sheet; the rest (if
   any) is the business question. If no sheet path is given, ask the user for one.
   If no question is given, use a sensible default such as
   "How is admission conversion performing by branch?".

2. Run the pipeline via the plugin's CLI (this masks PII, analyzes, and writes the
   report). Use the plugin root so it works on any PC:

   ```bash
   python "${CLAUDE_PLUGIN_ROOT}/scripts/run_pipeline.py" \
     --csv "<sheet path>" \
     --question "<question>" \
     --out "fv_report.html"
   ```

3. Report the result to the user: the absolute path of `fv_report.html`, the number
   of questions answered/skipped, and the monitoring health line the CLI prints.
   Tell them to open the HTML file in a browser. Do NOT paste the raw HTML into the
   chat.

4. If the CLI exits non-zero, relay the error message verbatim. A "blocked" status
   usually means the sheet's columns could not be mapped — suggest the user check
   `docs/DATA_FORMAT.md` (or the README) for the expected columns.

Notes:
- The pipeline runs fully without an API key (deterministic narrative). With a real
  `ANTHROPIC_API_KEY` in the project `.env`, the report's prose is sharper but every
  number is identical — the LLM phrases, it never computes.
- Never invent metrics or fill in numbers yourself; only relay what the CLI produced.
