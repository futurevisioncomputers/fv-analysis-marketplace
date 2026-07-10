---
description: Clean and PII-mask an institute sheet only (Data Engineer), output a canonical CSV + quality report — no analysis
argument-hint: <csv-or-xlsx-path-or-google-sheet-url> [output.csv]
allowed-tools: Bash, Read
---

You are running ONLY the Data Engineer stage of the FV Institute pipeline for the
user: clean the sheet, canonicalize columns, parse dates, derive funnel flags, and
mask PII (names / mobiles / email / DOB to salted hashes). No analysis, no report.

User input: `$ARGUMENTS`

Steps:

1. Parse the input. The first token is the source; the second (optional) is the
   output CSV path (default `cleaned.csv`). The source is one of:
   - a **Google Sheet URL** (starts with `http`) → `--sheet-url`
   - a path ending in `.xlsx` → `--excel`
   - a path ending in `.csv` → `--csv`
   If no source is given, ask for one.

2. Run the clean-only mode of the plugin CLI:

   For a CSV:

   ```bash
   python "${CLAUDE_PLUGIN_ROOT}/scripts/run_pipeline.py" \
     --clean-only --csv "<sheet path>" --clean-out "<output.csv>"
   ```

   For a Google Sheet URL:

   ```bash
   python "${CLAUDE_PLUGIN_ROOT}/scripts/run_pipeline.py" \
     --clean-only --sheet-url "<google sheet url>" --clean-out "<output.csv>"
   ```

   For an Excel workbook (each sheet becomes a source, joined by Student-ID):

   ```bash
   python "${CLAUDE_PLUGIN_ROOT}/scripts/run_pipeline.py" \
     --clean-only --excel "<workbook.xlsx>" --clean-out "<output.csv>"
   ```

3. Relay what the CLI printed: the output CSV path, row/column count, how many
   roles were mapped, how many PII fields were masked, and the quality notes.
   Tell the user the CSV is safe to share — all PII columns are hashed.

4. If the CLI exits non-zero, relay the error verbatim. A blocked status usually
   means the columns could not be mapped — point the user to
   `docs/sheet_schema_guide.md` for the expected headers.

Notes:
- The output CSV is the masked canonical frame: names/mobiles/email are salted
  hashes, dates are normalized, and derived columns (is_admitted, person_id,
  completion_status, etc.) are added where the data supports them.
- Never echo raw PII. Only relay what the CLI produced.
- Use `/fv-analyze` instead when the user wants the full report + recommendations.
