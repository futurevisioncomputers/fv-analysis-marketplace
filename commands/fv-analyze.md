---
description: Run the FV Institute analytics pipeline on one or more sheets and produce an HTML dashboard
argument-hint: <csv-or-xlsx-path-or-google-sheet-url> [business question]
allowed-tools: Bash, Read
---

You are running the FV Institute analytics pipeline for the user.

User input: `$ARGUMENTS`

Steps:

1. Parse the input. The first token is the source; the rest (if any) is the
   business question. The source is one of:
   - a **Google Sheet URL** (starts with `http`, usually `docs.google.com`) —
     use `--sheet-url`. The sheet must be published or link-shared.
   - a path ending in `.xlsx` — use `--excel` (every tab becomes a source).
   - a path ending in `.csv` — use `--csv`.
   If no source is given, ask the user for one. If no question is given, use a
   sensible default such as "How is admission conversion performing by branch?".

2. Run the pipeline via the plugin's CLI (this masks PII, analyzes, and writes the
   report). Use the plugin root so it works on any PC:

   For CSV:

   ```bash
   python "${CLAUDE_PLUGIN_ROOT}/scripts/run_pipeline.py" \
     --csv "<sheet path>" \
     --question "<question>" \
     --out "fv_report.html"
   ```

   For a published Google Sheet (URL). Repeat `--sheet-url` for multiple tabs;
   optionally name each as `name=URL`:

   ```bash
   python "${CLAUDE_PLUGIN_ROOT}/scripts/run_pipeline.py" \
     --sheet-url "<google sheet url>" \
     --question "<question>" \
     --out "fv_report.html"
   ```

   For Excel workbooks with multiple sheets:

   ```bash
   python "${CLAUDE_PLUGIN_ROOT}/scripts/run_pipeline.py" \
     --excel "<workbook.xlsx>" \
     --question "<question>" \
     --out "fv_report.html"
   ```

   For multiple CSV exports:

   ```bash
   python "${CLAUDE_PLUGIN_ROOT}/scripts/run_pipeline.py" \
     --source "student=<student.csv>" \
     --source "fee=<fee.csv>" \
     --source "certificate=<certificate.csv>" \
     --question "<question>" \
     --out "fv_report.html"
   ```

3. Report the result to the user: the absolute path of `fv_report.html`, the number
   of questions answered/skipped, monitoring health, and any source/join summary
   line the CLI prints.
   Tell them to open the HTML file in a browser. Do NOT paste the raw HTML into the
   chat.

4. If the CLI exits non-zero, relay the error message verbatim. A "blocked" status
   usually means the sheet's columns could not be mapped — point the user to
   `docs/sheet_schema_guide.md` for the expected headers. An HTTP error on a
   `--sheet-url` run means the Google Sheet is not readable by link — tell the
   user to File → Share → "Anyone with the link" (or Publish to web).

Notes:
- The pipeline runs fully without an API key (deterministic narrative). With a real
  `ANTHROPIC_API_KEY` in the project `.env`, the report's prose is sharper but every
  number is identical — the LLM phrases, it never computes.
- Never invent metrics or fill in numbers yourself; only relay what the CLI produced.
