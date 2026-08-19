# FV-Analysis — Institute Analytics 

Turns a raw institute sheet (admissions, enquiries, fees, receipts,
certificates, timetable tabs) into a decision-grade **PII-safe HTML report** —
as twelve resumable stages that pause for a decision after each one.

> **Numbers are computed in Python and never by the model.** The LLM phrases
> prose; it does not compute, estimate, or fill a gap. A metric that cannot be
> computed on the data becomes a *skipped question with a reason*. Raw PII
> (names, mobiles, email, DOB) never leaves the Data Engineer unmasked.

## Why it is built this way

Two properties drive everything else.

**It can stop.** State lives on disk, so each stage is its own process: run one,
look at what it produced, export it, decide whether to continue. `/fv-clean`
today and `/fv-metrics` tomorrow continue the same run.

**It can refuse.** Every stage would rather block with an actionable reason than
produce a number it cannot stand behind — and the first stage refuses *before*
any work is spent, by saying what the sheet can and cannot answer.

```
asked_for: churn_rate | will_answer_with: completion_rate | substituted: True
missing_data: course_duration → "a sheet carrying 'Course Duration (IN DAYS)'"
```

## Install

```text
/plugin marketplace add futurevisioncomputers/fv-analysis-marketplace
/plugin install fv-analysis@fv-analysis-marketplace
```

Then once, so the CLI has its dependencies:

```bash
pip install -r requirements.txt      # pandas, pyarrow, openpyxl
```

**Python 3.11+** on PATH. An `ANTHROPIC_API_KEY` in a project `.env` sharpens
the prose; without it the pipeline runs fully and **every number is identical**.

## Start here

| You want | Use |
|---|---|
| The whole analysis, seeing each step | `/fv-run <sheet>` |
| One stage — just clean it, just the metrics | `/fv-clean`, `/fv-metrics`, … |
| The whole thing, unattended | `/fv-analyze <sheet> <question>` |
| Not sure | `/fv-analysis` — it routes |

```text
/fv-run samples/student_data_sheet__fees_data.csv How do fee defaults differ by branch?
```

## The stages

Each has its own skill, and each back-fills what it needs — `/fv-eda` on a bare
CSV runs problem → schema → clean first and says that it did.

| # | Stage | Skill | Produces |
|---|---|---|---|
| 1 | Problem Definition | `fv-questions` | scoped questions + **what data each one needs** |
| 2.5 | Schema / Source Plan | `fv-schema` | column→role mapping, join plan |
| 2 | Data Engineer | `fv-clean` | masked canonical frame, quality report, `cleaned.csv` |
| 2.7 | Feature Engineering | `fv-features` | proposed derived columns *(nothing materializes unapproved)* |
| 3 | EDA | `fv-eda` | distributions, trends, anomalies |
| 4 | Analyst | `fv-metrics` | metrics with 95% CIs, breakdowns, factor analysis |
| 4.5 | Prediction | `fv-predict` | churn model *(optional; refuses on thin labels)* |
| 5 | Visualization | `fv-charts` | Chart.js configs, KPI cards |
| 6 | Insights | `fv-insights` | findings, root causes, risks |
| 6.5 | Recommendation | `fv-actions` | prioritized, owner-tagged actions |
| 7 | Monitoring | `fv-monitor` | KPI hooks and alerts |
| 8 | Report Writer | `fv-report` | the shareable HTML |

### Two skills that are not stages

They read a run that has already been cleaned and write nothing back. Reach for
them when a number is in hand and the question is what it means.

**`/fv-stats`** — is that difference real?

```
 is_default by Branch — overall 8.3% of 1,440
 citylight   11.7%  [9.1%, 14.8%]  n=480   +5.0% vs rest, q=0.004  SIGNIFICANT
 pal          7.5%  [5.5%, 10.2%]  n=480   -1.2% vs rest, q=0.418
 vesu         5.8%  [4.1%,  8.3%]  n=480   -3.8% vs rest, q=0.023  SIGNIFICANT
```

Corrected for having looked three times, so a "worst branch" is not manufactured
by the act of ranking. Also: Kaplan–Meier survival that honours censoring,
cohort comparison, and Mann–Kendall + Theil–Sen trend.

**`/fv-cross`** — which *combination* is it?

Same data, one dimension further:

```
               advanced e  graphic de  python pro      margin
 citylight         4.4%       26.9%*       3.8%         11.7%
 pal               7.5%        8.8%        6.2%          7.5%
 vesu              7.5%        5.6%        4.4%          5.8%
 margin            6.5%       13.8%        4.8%
```

Citylight is not a bad branch. Its Excel and Python are **better** than average;
one course at one site carries the whole difference. Acting on the branch number
would have fixed the wrong thing.

A cell counts only when it beats **both** of its margins after correction, and
cells under 30 rows are suppressed and counted, never shown small.

## Direct CLI

Everything the skills do is a command you can run yourself.

```bash
# one stage at a time
python scripts/run_stage.py --stage clean --csv sheet.csv --question "..."
python scripts/run_stage.py --stage next          # continue where you stopped
python scripts/run_stage.py --stage all           # no pauses

# several sheets together
python scripts/run_stage.py --stage clean \
    --source student_timetable__main_data.csv \
    --source student_timetable__not_coming.csv \
    --source student_data.csv

# questions about the numbers
python scripts/run_stats.py --analysis segments --flag is_default --by branch
python scripts/run_cross.py --value is_default --rows branch --cols course

# the unattended path the web service uses
python scripts/run_pipeline.py --csv sheet.csv --question "..." \
    --out report.html --json run.json
```

Sessions live in `.fv/session` (override with `--session` or `$FV_SESSION`).
Artifacts are reproducible from `session.json` plus the source, so deleting
`artifacts/` costs time, never correctness.

## What it knows about the institute's sheets

Four workbooks, each maintained by a different role — which is why the same
person is spelled three ways across them:

| Workbook | Owner | Typical report |
|---|---|---|
| Admission Form (Responses) | counsellor | admissions, source, conversion |
| Enquiry Form (Responses) 1 & 2 | counsellor | lead quality, follow-up backlog |
| student-data-sheet (student-data, fees-data, fees-recpit, certificate-data) | admin | collections, pending, certificates |
| Student_Time_Table2023 (Main_data, Course_Completed, Not_Coming, NOT TO ENTERTRAIN) | faculty | batch load, completion, churn |

Some of what that costs, and what the cleaner does about it:

- **Receipt ids repeat.** Books are per-branch and restart; a part-cash
  part-online receipt is two rows sharing one id. A candidate key is *measured*
  before it is trusted.
- **`student-id` is an admission id, not a person id.** One student holds
  several. Identity is built from whatever discriminates (phone, DOB, email),
  and person-grain metrics are **withheld** rather than guessed when nothing
  does.
- **The four timetable tabs are one roster**, unioned rather than joined, and
  most of their rows carry no date at all — membership is the fact.
- **Branch, faculty and batch change mid-course** and the sheet keeps only the
  latest value, so they are barred from join keys and marked
  `attribute_currency`.
- **Churn is time-dependent** — `course_end + 6 months`, recomputed per run
  against an as-of date recorded with the answer. `Course Duration (IN DAYS)`
  sits on the timetable's main sheet only, so leavers borrow the median length
  recorded for their own course rather than an assumed one.

`docs/form_schema_notes.md` has the exact headers, the join map and the
defects; `docs/reporting_spec_v2.md` records what is built against the
institute's specification and what is not.

## Layout

```
.claude-plugin/   plugin.json + marketplace.json
commands/         16 slash commands
skills/           16 skills — one per stage, plus fv-run, fv-stats, fv-cross,
                  and the fv-analysis router
agents/           the engine: one module per agent, plus session/stages,
                  statistics, multifactor, lifecycle, canonical_maps
scripts/          run_stage.py (the CLI every skill calls), run_stats.py,
                  run_cross.py, run_pipeline.py, sample generators
samples/          synthetic sheets that join exactly as the real ones do
data/             a bundled synthetic admissions sheet to try immediately
tests/            125 tests, no pytest dependency — `python -m tests.<name>`
```

## Testing

```bash
python -m tests.test_real_data_cleaning     # cleaner, roles, joins, lifecycle
python -m tests.test_stage_parity           # stage-major == question-major
python -m tests.test_statistics             # known-answer statistical tests
python -m tests.test_multifactor            # planted interactions
python -m tests.test_lifecycle              # the churn rule
python -m tests.test_skill_manifest         # skills match the CLI they drive
```

Plain asserts, no test dependency. `scripts/sanitize_sample_pii.py --check`
gates the sample data: every name, phone and email must sit in a fabricated or
reserved range, and it exits 1 otherwise.

## License

MIT — see [LICENSE](LICENSE).
