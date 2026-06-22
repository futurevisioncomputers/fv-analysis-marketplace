---
description: Regenerate the HTML report from a saved FV run JSON (no re-analysis)
argument-hint: <run-json-path> [output.html]
allowed-tools: Bash, Read
---

Regenerate the FV Institute HTML report from a previously saved run JSON, without
re-running the pipeline. Use this to restyle/re-export a past analysis cheaply.

User input: `$ARGUMENTS`

Steps:

1. Parse the input. The first token is the path to a saved run JSON (produced by
   `fv-analyze` / the CLI's `--json` flag; it contains `final_report` and
   `question_results`). The optional second token is the output HTML path
   (default `fv_report.html`).

2. Render the report from that JSON:

   ```bash
   python "${CLAUDE_PLUGIN_ROOT}/scripts/run_pipeline.py" \
     --from-json "<run json path>" \
     --out "<output html>"
   ```

3. Tell the user the absolute path of the written HTML and to open it in a browser.
   Do not paste the HTML into the chat.

4. If the JSON is missing or malformed, relay the error. To produce a run JSON,
   run `fv-analyze` (or the CLI with `--json run.json`).
