---
name: fv-analysis
description: FV Institute end-to-end data analysis. Use when the user has an institute data sheet (CSV/XLSX of admissions, fees, certificates, leads, courses, branches) and wants analysis, KPIs, insights, recommendations, monitoring, or a shareable report. Runs the 8-agent pipeline (problem definition -> data cleaning + PII masking -> EDA -> analysis -> visualization -> insights -> recommendations -> monitoring -> HTML report). Triggers on requests to analyze a sheet, build a dashboard report, find why a metric dropped, or compare branches/courses/sources.
---

# FV Institute Analysis

End-to-end analytics for Future Vision Computers Institute (Surat; branches Vesu,
Pal, Citylight). Turns a raw data sheet into a decision-grade HTML report.

## When to use
- The user points at a CSV/XLSX of institute data and asks a business question.
- They want a report, dashboard, KPI summary, root-cause, or branch/course comparison.
- They want recommendations or KPI monitoring/alerts from their data.

## How to run

The pipeline is a Python engine; drive it through the CLI (works on any PC):

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/run_pipeline.py" \
  --csv "<sheet path>" \
  --question "<business question>" \
  --out "fv_report.html" \
  --json "fv_run.json"
```

- `--out` writes the standalone HTML report (open in a browser).
- `--json` saves the run contracts so the report can be re-rendered later with
  `--from-json` (no re-analysis).
- Or use the slash commands: `/fv-analyze` (run + report) and `/fv-report`
  (re-render from saved JSON).

## The 8 agents (what they produce)
1. **Problem Definition** — scopes the question into modules + business questions + KPIs.
2. **Data Engineer** — cleans the sheet, parses dates, derives funnel flags, and is the
   ONLY agent that sees raw PII (masks names/mobiles/email/DOB to salted hashes).
3. **EDA** — distributions, time trends, cross-tabs (chi²/Cramér's V), anomalies.
4. **Analyst** — headline metric with a 95% CI, breakdowns, comparisons, drivers.
5. **Visualization** — Chart.js-ready configs, KPI cards, alerts.
6. **Insights** — findings, root causes, risks, opportunities (no actions).
7. **Recommendation** — prioritized, owner-tagged actions (the only action-proposing agent).
8. **Report Writer** — composes the shareable HTML report from all of the above.
   (Monitoring runs alongside, registering KPI hooks and raising threshold alerts.)

## Boundaries (do not violate)
- **Numbers are deterministic.** The LLM only phrases narrative prose; it never
  computes or invents a metric. If a metric is not computable on the data, the
  question is skipped honestly — never fabricate a chart or number.
- **PII never leaves masked.** Only Agent 2 sees raw PII. The report asserts no
  10-digit mobile survives before it is written. Never echo raw PII to the user.
- **Relay, don't invent.** Report only what the CLI/engine produced.

## Expected data
Sheets with columns describing students/admissions/fees/certificates work best.
Unknown/renamed headers are handled by value-based role discovery, but a fully
cryptic sheet may block — then tell the user which columns are missing.
