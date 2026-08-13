# Plugin restructure — plan of record

Why the plugin is being rebuilt, what the target shape is, and the order the
work lands in. Written before the refactor so the reasoning survives it.

Companion documents:
- `docs/form_schema_notes.md` — the five sheets, their defects, and the join map
- `docs/replan_real_data.md` — the earlier data study (partly superseded; see §6)

---

## 1. Why

The pipeline is one Python process that runs stages 1→8 and returns. Two
problems follow from that shape.

**It cannot pause.** The request was a checkpoint after every stage — show what
the stage produced, offer to export it, ask whether to continue. There is
nowhere in a single function call to stop and ask. `input()` is not available
either: Claude Code's shell is non-interactive.

**It cannot run one stage.** Every metric needs the whole pipeline, so
"just clean this sheet" or "just run EDA" means recomputing everything.

There is also a structural mismatch. The orchestrator runs **question-major** —
for each business question, Analyst → Visualization → Insights →
Recommendation. A checkpoint after "Visualization" would fire once per
question, mid-run. That is not a boundary an operator can act on.

## 2. Target shape

**State moves to disk.** A `Session` directory holds accumulated state; each
stage is its own CLI invocation that loads it, runs one stage, persists, exits.
The caller — a skill, the orchestrator in `--auto`, or the web service —
decides what happens between stages.

**Execution becomes stage-major.** Each stage runs to completion across every
question. Exactly one boundary per stage.

```
        skill / CLI / orchestrator --auto
                     │
                     ▼
        scripts/run_stage.py  --stage <key> --session <dir>
                     │
        ┌────────────┴────────────┐
        │                         │
   agents/session.py        agents/stages.py
   (state, artifacts,       (one function per stage,
    prereqs, decisions)      shared summaries)
                                  │
                     ┌────────────┴────────────┐
                     │   the eleven agents     │
                     └─────────────────────────┘
```

`orchestrator_agent` imports `stages`; never the reverse.

### Session directory

```
<session_dir>/
    session.json              accumulated state — the only authoritative file
    artifacts/
        canonical.parquet     masked frame from the Data Engineer
        cleaned.csv           operator-downloadable copy (masked)
        features.parquet      after Feature Engineering
        report.html
    monitoring_registry.json  KPI hooks, persisted across runs
```

Artifacts are reproducible from `session.json` plus the source, so deleting
`artifacts/` costs time, never correctness.

## 3. Stages and their checkpoints

| # | Stage | Key | Checkpoint offers |
|---|---|---|---|
| 1 | Problem Definition | `problem` | show questions · add a question · **what data each question needs** · next |
| 2.5 | Schema / Source Plan | `schema` | show capability report · column mapping · next |
| 2 | Data Engineer | `clean` | show quality report · **download cleaned CSV** · next |
| 2.7 | Feature Engineering | `features` | **review proposed features** · approve/drop · download · next |
| 3 | EDA | `eda` | show profile and anomalies · next |
| 4 | Analyst | `analyst` | show metrics with CIs · next |
| 4.5 | Prediction *(optional)* | `predict` | show model and lift · next |
| 5 | Visualization | `visualize` | preview charts · next |
| 6 | Insights | `insights` | show findings · next |
| 6.5 | Recommendation | `recommend` | show actions · next |
| 7 | Monitoring | `monitor` | show alerts · next |
| 8 | Report Writer | `report` | open HTML |

One skill per stage, plus `/fv-run` for the guided walk and `--auto` for the
non-interactive path the CLI and web service already depend on.

### Design decisions taken

| Decision | Choice | Reason |
|---|---|---|
| Mid-pipeline skill on a bare CSV | **Auto-run prerequisites** | `Session.missing_prereqs` is transitive, so `/fv-eda` back-fills problem→schema→clean and reports what it did |
| Feature engineering scope | **Auto-derive, operator approves** | Agent proposes with reasons; nothing materializes unapproved |
| Prediction agent | **Optional stage + skill** | Honesty-gated already; off in `--auto` so it does not slow every run |
| Build order | **Usability before multi-sheet correctness** | Most runs are a single sheet, where the lifecycle/churn work never fires |

## 4. Why usability comes before the churn fixes

Most real runs use **one sheet**: the admission form for an admission report,
an enquiry sheet for lead analysis, `fees-data` for a financial report. Nearly
all the correctness work queued in §5(E2) — the churn label, latest-admission
selection, cross-workbook person resolution — only fires when several sheets
are supplied together. Fixing it first would be correcting a path rarely taken.

One exception is pulled forward as **E1**, because it breaks the common case.

**E1a — two silent row-loss bugs, both fixed.** Found by the parity test.

- *Ragged export.* When a sheet's data rows are wider than its header (a
  trailing delimiter on every line), pandas promotes the first field to the
  index and shifts every column one place left. Dates land in the name column,
  no row has a parseable date, and the cleaner blocks having dropped 100% of
  rows. Now detected on read, re-read with `index_col=False`, and reported as
  a known issue. The sample generator also refuses to emit a ragged file.
- *Repeated ids treated as row keys.* Receipt books are per-branch and restart,
  so receipt numbers repeat; on the admission sheet `Receipt ID` is a foreign
  key to the ledger, not a row key at all. Deduplicating on it deleted **71 of
  222 admissions (32%)**, **411 of 748 ledger rows (55%, hiding ₹37.6 lakh of
  ₹71.3 lakh collected)** and **49 of 275 certificates (18%)** — all reported
  as `drop_count: 0`. A candidate key is now measured before it is trusted and
  widened with date/amount/person columns when it fails; if nothing identifies
  a row, every row is kept and the report says so.

**E1b — person grain, fixed.** `person_id` was `hash(name + phone)`. `fees-data`
and `fees-recpit` have no phone column, so the key degraded to name-only and
merged every namesake: **219 real people became 166**, and repeat-enrollment
rows were overstated by 20% — reported as a fact with a confidence interval.

Identity is now built from whatever discriminates (phone, DOB, email), each
used only if populated on ≥ 80% of rows, and the basis is recorded as
`quality_report.person_id_basis`. On a name-only source `is_repeat_enrollment`
is withheld and the Analyst blocks the metric with an actionable reason —
*"this source has no phone, date-of-birth or email … supply student-data or the
admission form"* — rather than the old `required column missing`.

## 5. Build order

| Step | Work | Status |
|---|---|---|
| **A** | `agents/session.py`, `agents/stages.py`, `scripts/run_stage.py`, orchestrator as a thin driver | **done** |
| **A2** | Parity gate — stage-major must equal question-major | **done**, committed as `tests/test_stage_parity.py` |
| **E1a** | Single-sheet correctness: ragged-export shift, repeated-id dedupe | **done** |
| **E1b** | Person-grain guard when phone/DOB absent | **done** |
| **B** | Pilot skill `/fv-clean` with the full checkpoint UX | **done** |
| **B2** | Stage-1 capability checkpoint: what this sheet can and cannot answer | **done** — `agents/capability.py` |
| **A3** | Canonical alias layer + the roles §22 needs | **done** — `CANONICAL_FIELD_NAMES` + `_specialize_roles` |
| **D** | `feature_engineering_agent.py` (2.7); wire prediction as 4.5 | **done** |
| **H** | Statistics module + `/fv-stats`: significance testing across segments, survival/retention with censoring, robust trend | **done** — `agents/statistics.py` |
| **C** | Remaining stage skills + `/fv-run` | **done** — 14 skills, 14 commands |
| **E2** | Churn label, tab union, latest admission, mutable attributes | **done** — `agents/lifecycle.py` |
| **G** | Multi-factor engine (§21/§23) + `/fv-cross` | **done** — `agents/multifactor.py` |
| **F** | `plugin.json`, `marketplace.json`, README, commands | pending |

**E2 — the lifecycle correctness set.** Five defects, all measured on the
samples before and after:

| Defect | Before | After |
|---|---|---|
| The four timetable tabs were **joined**, not unioned | 16 rows of 406, all `active` | 365 rows across four labels |
| Membership rows with no `Timestamp` were dropped as dateless | 57% of completions deleted, then the sheet blocked | kept; the date rule is skipped on a membership tab |
| The roster could not reach the student master (no `student-id`) | churn uncomputable | joined on person + course, with a course-upgrade fallback |
| `NOT TO ENTERTRAIN` matched no completion needle | the most certain churn cases carried no label at all | `not_to_entertain` |
| Churn counted six months from **admission** | any course longer than six months mislabelled | six months from `course_end` |

Plus three rules the institute supplied while this landed: `student-id` is an
admission id so a person's fee position is their **latest** admission
(`is_current_admission`; on the samples 46% of the outstanding balance sits on
superseded rows); branch / faculty / batch **change mid-course** and so are
barred from join keys and marked `attribute_currency`; and a **course upgrade**
entered on one sheet only is repaired where it is unambiguous.

Two states are deliberately kept apart, because they need different fixes:
`unlabelled` (in a churn tab, no computable course end) and `no_membership`
(in no tab at all).

**`Course Duration (IN DAYS)` is on the `Student_Time_Table2023` MAIN sheet**,
confirmed by the institute — which is backwards for this rule, since the rows
needing a course end are the leavers and the column is not on their tabs. Filled
from the median length recorded for the same course on the main sheet, so the
number stays the institute's own; `churn_basis` records which of
`duration_column` / `course_median` / `category_default` each row used, and a
row with none of them is `unlabelled` rather than defaulted. Measured on the
samples: 145 rows resolved by course median, 10 direct, 23 churned / 9 paused.

**G — the multi-factor engine.** Spec §21 called this the single largest gap.
One metric across two dimensions, plus a filter layer:

```
Citylight defaults at 11.7%.  Graphic Designing defaults at 13.8%.
Together the margins predict 17.1%.  The cell is 26.9% on n=160.
```

No single breakdown shows that, which is the only reason the engine exists. Four
guards stop it from manufacturing findings out of a grid:

1. a **chi-square over the whole grid** runs before any cell is picked over —
   worded differently for a rate (a test of the metric) and for anything else
   (a test only of whether the dimensions are evenly crossed);
2. a cell must beat **both** margins — high for its branch alone is a branch
   effect, and needed no crossing to find;
3. every cell test is **BH-corrected** across the grid;
4. cells under 30 rows are **suppressed and counted**, never shown small.

Exit code 3 means the grid computed cleanly and nothing beat both margins. On
institute-sized data that is the common answer and it is reported as one.

Found while wiring it: the report's PII guard counted `-279345.8333` — eleven
digits and a "." — as a formatted phone, and **withheld an entire report** over
a chart label. Signed decimal literals are now excluded; the bare-10-digit and
separator rules are untouched.

## 6. Safety nets

**Parity gate.** `tests/test_stage_parity.py` runs both execution shapes over
the same source and asserts the reports agree field by field —
`questions_answered`, `questions_skipped`, `headline_findings`,
`top_recommendations`, `skipped`, row count, monitoring health and alert count.
It also asserts every registry stage is declared in `STAGES` (a runner with no
declaration would silently never run) and that resume and cascade-reset behave.

Verified passing against the 26k-row dataset before the refactor started: every
field matched.

**Version control.** The repository lives at
`github.com/futurevisioncomputers/fv-analysis-marketplace`. This working copy
has a `.gitignore` but no `.git`, so local changes are untracked — they need
committing from a proper clone before the refactor is relied on.

**Synthetic fixtures.** Eleven sample files under `samples/`, generated from one
enrollment spine by `scripts/make_form_samples.py`, joinable exactly as the real
sheets are. `scripts/sanitize_sample_pii.py --check` gates them: every name,
phone and email must sit in a fabricated or reserved range, and it exits 1
otherwise.

## 7. Superseded guidance

`docs/replan_real_data.md` treats `Not_Coming` as a single unconditional churn
class. That is wrong under the institute's actual rule:

```
Not_Coming  AND  months_since_ADMISSION_DATE <  6  ->  paused   (censored)
Not_Coming  AND  months_since_ADMISSION_DATE >= 6  ->  churn = true
NOT TO ENTERTRAIN                                  ->  churn = true (unconditional)
Main_data                                          ->  active   (censored)
Course_Completed                                   ->  completed
```

Two consequences: there is a fourth tab (`NOT TO ENTERTRAIN`) the matcher does
not recognize at all, and the label is **time-dependent** — a row flips from
censored to churned with no edit to any sheet. Churn cannot be a stored column;
it must be recomputed per run against an explicit as-of date recorded in the
run. Full detail in `docs/form_schema_notes.md` §10.
