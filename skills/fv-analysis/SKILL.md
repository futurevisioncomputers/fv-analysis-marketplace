---
name: fv-analysis
description: FV Institute analytics — the router. Use when the user points at an institute data sheet (admissions, enquiries, fees, receipts, certificates, timetable tabs) and wants analysis, KPIs, insights, recommendations, monitoring or a report, but has not named a particular stage. Picks between an unattended run, a guided stage-by-stage walk, and the single-stage skills. Triggers on "analyze this sheet", "what does this data say", "build me a report", "why did admissions drop", "compare branches".
---

# FV Institute analysis

End-to-end analytics for Future Vision Computers Institute (Surat; branches
Vesu, Pal, Citylight). A raw sheet becomes a decision-grade HTML report.

This skill routes. Pick one of three shapes, then follow that skill.

## Which shape

| The user wants | Use | Why |
|---|---|---|
| One thing — clean it, get the numbers, chart it | the **stage skill** (`fv-clean`, `fv-metrics`, `fv-charts`, …) | prerequisites back-fill, so a single stage works on a bare CSV |
| The whole analysis, but wants to see each step | **`fv-run`** | twelve stages, a checkpoint after each |
| The whole thing, unattended | `run_pipeline.py` or `run_stage.py --auto` | no pauses; what the web service uses |

When in doubt, prefer `fv-run`. Most operators here supply one sheet and do not
yet know what it can answer — the stage-1 checkpoint tells them before the
pipeline spends any time.

## The stages and their skills

| # | Stage | Skill | Produces |
|---|---|---|---|
| 1 | Problem Definition | `fv-questions` | scoped questions + what data they need |
| 2.5 | Schema / Source Plan | `fv-schema` | column→role mapping, join plan |
| 2 | Data Engineer | `fv-clean` | masked canonical frame, quality report, `cleaned.csv` |
| 2.7 | Feature Engineering | `fv-features` | proposed derived columns *(approval-gated)* |
| 3 | EDA | `fv-eda` | distributions, trends, anomalies |
| 4 | Analyst | `fv-metrics` | metrics with 95% CIs and breakdowns |
| 4.5 | Prediction | `fv-predict` | churn model *(optional, refuses on thin labels)* |
| 5 | Visualization | `fv-charts` | Chart.js configs, KPI cards |
| 6 | Insights | `fv-insights` | findings, root causes, risks |
| 6.5 | Recommendation | `fv-actions` | prioritized, owner-tagged actions |
| 7 | Monitoring | `fv-monitor` | KPI hooks and alerts |
| 8 | Report Writer | `fv-report` | the shareable HTML |

State lives in `.fv/session`, so the skills compose: `/fv-clean` today and
`/fv-metrics` tomorrow continue one run.

## Two skills that are not stages

They read a session that has already been cleaned and write nothing back. Reach
for them when a number is in hand and the question is what it means.

| Skill | Answers |
|---|---|
| `fv-stats` | Is that difference real? When do students leave? Are newer cohorts worse? Is that a trend? |
| `fv-cross` | Which *combination* is the problem — this course at that branch, this tutor in that slot |

`fv-cross` exists because one metric by one dimension cannot see an
interaction: a branch and a course can each look mildly above average while one
cell of the two is three times the rate. If a single breakdown already answers
the question, do not reach for it.

## Boundaries — these hold in every skill

- **Numbers are deterministic.** The LLM phrases prose; it never computes or
  invents a metric. A metric that cannot be computed produces a *skipped
  question with a reason*, never an estimate.
- **PII is masked at stage 2 and never unmasked.** Only the Data Engineer sees
  raw values. Never echo them.
- **Refusals are results.** A blocked stage said why; relay it and stop.
- **Relay, do not embellish.** Every number shown comes from the payload.

## What the sheets are

Four workbooks, each owned by a different role — which is why the same person
is spelled three ways across them, and why a single-sheet run has a known
audience:

| Workbook | Owner | Typical report |
|---|---|---|
| Admission Form (Responses) | counsellor | admissions, source, conversion |
| Enquiry Form (Responses) 1 & 2 | counsellor | lead quality, follow-up backlog |
| student-data-sheet (student-data, fees-data, fees-recpit, certificate-data) | admin | collections, pending, certificates |
| Student_Time_Table2023 (Main_data, Course_Completed, Not_Coming, NOT TO ENTERTRAIN) | faculty | batch load, completion, churn |

`docs/form_schema_notes.md` has the exact headers, the join map, and the
defects worth knowing before promising an answer. `docs/reporting_spec_v2.md`
records what the institute wants reported and what is not built yet.

One sheet at a time is the normal case. Ask for a second only when the question
genuinely needs the join.
