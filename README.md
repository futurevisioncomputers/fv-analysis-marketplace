# fv-analysis — FV Institute Analytics Plugin for Claude Code

An 8-agent analytics pipeline that turns a raw institute data sheet (CSV/XLSX of
admissions, fees, certificates, leads, courses, branches) into a decision-grade,
**PII-safe HTML report** — driven from Claude Code with a slash command.

> **Numbers are deterministic. The LLM only phrases narrative prose — it never
> computes or invents a metric.** If a metric is not computable on the data, the
> question is skipped honestly. Raw PII (names, mobiles, email, DOB) never leaves
> the data-engineer stage unmasked.

## The 8 agents
1. **Problem Definition** — scopes the question into modules + business questions + KPIs.
2. **Data Engineer** — cleans the sheet, parses dates, derives funnel flags; the ONLY
   agent that sees raw PII (masks names/mobiles/email/DOB to salted hashes).
3. **EDA** — distributions, time trends, cross-tabs (chi²/Cramér's V), anomalies.
4. **Analyst** — headline metric with a 95% CI, breakdowns, comparisons, drivers.
5. **Visualization** — Chart.js-ready configs, KPI cards, alerts.
6. **Insights** — findings, root causes, risks, opportunities.
7. **Recommendation** — prioritized, owner-tagged actions.
8. **Report Writer** — composes the shareable HTML report.
   (Monitoring runs alongside, registering KPI hooks and raising threshold alerts.)

## Prerequisites
- **Python 3.11+** on PATH.
- Python deps: `pip install -r requirements.txt` (pandas, pyarrow, openpyxl).
- Optional: an `ANTHROPIC_API_KEY` in a project `.env` sharpens the prose. Without
  it the pipeline still runs fully (deterministic narrative); every number is
  identical either way.

## Install in Claude Code

```text
/plugin marketplace add futurevisioncomputers/fv-analysis-plugin
/plugin install fv-analysis@fv-analysis-marketplace
```

`fv-analysis-marketplace` is the marketplace name; `fv-analysis` is the plugin
name. If the repo is private, the account installing needs git/`gh` read access.
After install, run `pip install -r requirements.txt` once so the CLI has its deps.

## Usage

### Slash commands
- `/fv-analyze <csv-path> [business question]` — run the full pipeline + write the report.
- `/fv-report <run-json-path> [output.html]` — re-render a report from a saved run JSON (no re-analysis).

Example:
```text
/fv-analyze data/15_Year_Admissions_Realistic.csv How is admission conversion performing by branch?
```

### Direct CLI
```bash
python scripts/run_pipeline.py \
  --csv data/15_Year_Admissions_Realistic.csv \
  --question "How is admission conversion performing by branch?" \
  --out fv_report.html --json fv_run.json
```
- `--out` writes the standalone HTML report (open in a browser).
- `--json` saves the run contracts for cheap re-rendering with `--from-json`.

Re-render only:
```bash
python scripts/run_pipeline.py --from-json fv_run.json --out fv_report2.html
```

## Sample data
`data/15_Year_Admissions_Realistic.csv` is a bundled **synthetic** admissions
dataset for trying the pipeline immediately. Replace it with your own sheet for
real analysis. Sheets describing students/admissions/fees/certificates work best;
unknown/renamed headers are handled by value-based role discovery, but a fully
cryptic sheet may block — the CLI then tells you which columns are missing.

## Layout
```
.claude-plugin/   plugin.json + marketplace.json (Claude Code metadata)
commands/         /fv-analyze, /fv-report slash commands
skills/           fv-analysis skill (auto-triggers on data-analysis requests)
agents/           the 8-agent Python engine (+ orchestrator, llm_client)
scripts/          run_pipeline.py — the single CLI entry the commands shell into
data/             bundled sample sheet
```

## License
MIT — see [LICENSE](LICENSE).
