#!/usr/bin/env python
"""Write the per-stage skill and command files.

Eleven skills that differ only in their stage-specific half would rot apart if
hand-maintained: the shared protocol would drift in some and not others. Held
here as data instead, so a change to the common half lands everywhere at once.

Run: python scripts/_make_skills.py
"""

from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PROTOCOL = """
## How to run it

Follow `skills/CHECKPOINT_PROTOCOL.md` — read it if it is not already in
context. In short:

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/run_stage.py" --stage %(key)s --json
```

Add `--csv <path>` (or `--excel` / `--sheet-url` / repeated `--source`) and
`--question "…"` on the first call of a run; neither is needed afterwards.
Prerequisites back-fill automatically, so this works on a bare CSV — say so
when several stages had to run.

Read the `checkpoint` object from the JSON envelope. Relay its `summary` and
`details`, name the files in `artifacts`, offer what `offers` lists plus
continuing to `next_stage`, then **stop and wait**.

Exit code 2 means the stage refused: relay `reason` verbatim and stop. Nothing
downstream ran and nothing was invented.
""".rstrip()

# key, skill name, one-line command description, trigger phrases, and the
# stage-specific guidance that is the whole reason each file exists.
STAGES = [
    dict(
        key="problem", skill="fv-questions", label="Problem Definition (stage 1)",
        desc="Turn a business question into a scoped brief, and check whether "
             "this sheet can actually answer it",
        triggers="Use when the user asks what a sheet can tell them, wants to "
                 "frame or add business questions, or asks whether they have "
                 "enough data for a question. Triggers on \"what can I ask of "
                 "this file\", \"add a question\", \"do I need more data\", "
                 "\"scope this analysis\".",
        body="""
## What this stage decides

It reads the operator's words, not the data: modules, business questions, KPI
targets and the time window. So it will happily frame a certificate question
against an admission form — which is why the checkpoint carries a capability
check.

## Show them

**The questions**, from `details`: id, text, and the metric each will be
answered with.

**`data_needs`** — the part that matters. It is on the checkpoint object:

- `answerable` / `blocked` counts.
- Per question: ✓ with the metric it will use, or ✗ with the roles it lacks.
- `substituted: true` means the question will be answered with a **fallback
  metric**, not the one asked for. Say which, both times — answering "how much
  was collected?" with pending fee is a different answer.
- `missing_data`: each gap names the sheet that carries it and which questions
  it unlocks. That is the actionable part — "add fees-data, unlocks 4 of 7".

## Then ask

- Show the full brief.
- **Add a question** — re-run with a `--question` that includes it. This
  re-runs stage 1 and clears everything downstream, so say that first.
- **Fetch a missing sheet** and re-run with an extra `--source`.
- Continue to the schema check.

If most questions are blocked, do not just continue. Adding one sheet is
usually cheaper than accepting a report that answers a third of what was asked.
""",
    ),
    dict(
        key="schema", skill="fv-schema", label="Schema / Source Plan (stage 2.5)",
        desc="Validate the sheets against the questions and plan any joins",
        triggers="Use when the user asks how their columns map, whether two "
                 "sheets can be joined, or why a column was interpreted a "
                 "certain way. Triggers on \"what did you map this to\", "
                 "\"can you join these sheets\", \"check my columns\".",
        body="""
## What this stage decides

Which questions the supplied sources can support, how the columns map to
roles, and — with several sheets — which keys join them.

## Show them

- The per-question verdict from `details`.
- The **column → role mapping**, and with it the institute's canonical name for
  each column (`field_names` on the clean package once stage 2 has run).
- The **join plan** when there is more than one source: which key, and how many
  rows match. The estate has one weak edge — enquiry → admission by phone,
  which recovers roughly 78–81% — so a join here is a measured coverage, not a
  promise.
- `dataset_mode`: `institute` when the sheet matches the known shape, `generic`
  when the Analyst is falling back to whatever columns exist.

## Then ask

- Show the mapping in full.
- **Correct a mapping** — if a column was read wrongly, that is a role-matcher
  fix, not something to work around here. Report it precisely: the header, what
  it was mapped to, what it should be.
- Continue to cleaning.
""",
    ),
    dict(
        key="features", skill="fv-features",
        label="Feature Engineering (stage 2.7)",
        desc="Propose derived columns — duration groups, age bands, cohorts, "
             "outstanding buckets — and build only the ones approved",
        triggers="Use when the user wants grouped or banded columns: short vs "
                 "long courses, age groups, fee buckets, joining cohorts, "
                 "batch slots, students ending soon. Triggers on \"group by "
                 "course length\", \"band the ages\", \"bucket the outstanding "
                 "amounts\", \"add derived columns\".",
        body="""
## Two passes, on purpose

The first call **proposes and builds nothing**. Every feature here is a policy
boundary — "short course" is a line someone drew, not a fact about the data —
so the operator owns it. The second call materializes exactly what they picked.

## Show them

For each proposal: the id, the rule in words, and the preview (value counts, or
min/median/max for a numeric one). Then the refusals — each says why, and a
refusal for coverage carries the number: *"only 10% populated (needs 40%)"*.

Do not present a proposal as a finding. It is an offer.

## Then ask

```bash
--approve-features all
--approve-features duration_group,age_band
--approve-features none
```

Two things to state before they choose:

- Approving **clears every stage after this one**. The frame changes, so
  results computed from the old frame cannot stand.
- Approved columns are written into the frame every later stage reads, and
  registered as roles, so they can be used as dimensions afterwards.

Thresholds are configurable. If the institute's idea of "short" is not 90 days,
that is a config change, not a caveat to write in the report.
""",
    ),
    dict(
        key="eda", skill="fv-eda", label="Exploratory Data Analysis (stage 3)",
        desc="Profile distributions, time trends, cross-tabs and anomalies",
        triggers="Use when the user wants to explore or understand a sheet "
                 "before analysis: distributions, spikes and dips, what is "
                 "unusual, how two columns relate. Triggers on \"explore "
                 "this\", \"what's odd in this data\", \"show me the trends\", "
                 "\"profile these columns\".",
        body="""
## Show them

- How many dimensions and numeric fields were profiled, and the trend
  direction.
- The **anomalies**, rendered from `details`: type, metric, period, value, and
  the change against the trailing average.

An anomaly is a shape in the data, not an explanation. A dip in July is a dip
in July; the institute's holiday calendar is a hypothesis, and this stage has
no way to test it. Say what was observed, and leave the cause to the operator
unless a later stage evidences it.

## Then ask

- Show the full profile: per-dimension distributions and per-numeric summaries.
- Continue to the Analyst.

EDA is context, not a gate. If it blocks, the run continues without it and the
report says so — that is deliberate.
""",
    ),
    dict(
        key="analyst", skill="fv-metrics", label="Analyst (stage 4)",
        desc="Compute the headline metric for every question, with a 95% "
             "confidence interval and breakdowns",
        triggers="Use when the user wants the actual numbers: conversion rate, "
                 "collection efficiency, pending fee, completion rate, churn "
                 "rate, certificate lag, by branch or course or faculty. "
                 "Triggers on \"what is my conversion rate\", \"how much is "
                 "pending\", \"compare branches\", \"give me the numbers\".",
        body="""
## Show them

Per question: the metric, its value, `n`, and the **95% confidence interval**.
Report the interval every time. A 4-point gap between two branches with 22
students each is not a gap, and the interval is what makes that visible.

Then the **skipped** questions with their reasons. These are not failures to
gloss over — a skipped question is the pipeline refusing to invent a number.
Two reasons are worth expanding:

- *"this source has no phone, date-of-birth or email…"* — the sheet cannot tell
  namesakes apart, so person-level metrics are withheld. Name the sheet to add.
- *"required column missing"* — the sheet does not carry the concept at all.

## Segments

A breakdown segment under 30 rows is flagged low-confidence. Relay that flag;
it is the difference between a finding and a coincidence.

## Then ask

- Show every metric with its interval and breakdowns.
- Show what could not be computed, and why.
- Continue to visualization.
""",
    ),
    dict(
        key="predict", skill="fv-predict", label="Prediction (stage 4.5, optional)",
        desc="Train an explainable churn model on the completion labels, or "
             "refuse and say why",
        triggers="Use when the user asks to predict or model churn, who is "
                 "likely to drop out, or which factors drive not-coming. "
                 "Triggers on \"predict churn\", \"who is at risk\", \"build a "
                 "model\", \"what drives dropout\".",
        body="""
## This stage is allowed to refuse

It trains only on **terminal** outcomes — completed vs not-coming — because a
student still attending has no outcome yet. Those rows are censored, not
negative examples. It refuses outright when labels are missing, single-class,
or too few (40 labelled rows, 10 per class), and says which.

A refusal here is the correct result on most single sheets. The labels come
from timetable tab membership, so a fee sheet alone cannot produce them.
Relay the reason and name what would supply it.

## Show them

- Accuracy **and the majority-class baseline**, always together. A model that
  only learned the base rate must be visibly worthless, not quietly reported as
  "83% accurate".
- Train/test sizes. The holdout is a stable hash of `person_id`, so all rows
  for one person land on the same side and the number is reproducible.
- The per-feature likelihood ratios — this model is readable, so read it.

## Never

Never turn a prediction into a recommendation here. "Likely to churn" is not
"call them"; that is the Recommendation stage's job, and it needs the insight
in between.
""",
    ),
    dict(
        key="visualize", skill="fv-charts", label="Visualization (stage 5)",
        desc="Build chart configurations and KPI cards for the answered questions",
        triggers="Use when the user wants charts, graphs, a dashboard view, or "
                 "KPI cards from their data. Triggers on \"chart this\", \"show "
                 "me a graph\", \"build a dashboard\", \"visualize the trend\".",
        body="""
## Show them

How many charts and KPI cards were built, per question, and what each chart
shows. Charts are Chart.js configurations rendered into the HTML report — they
are not images, and there is nothing to open until stage 8.

Every chart is built from a computed metric. There is no chart for a skipped
question, and that absence is correct: a chart of nothing is worse than no
chart.

## Then ask

- List the charts and cards.
- Continue to insights.
""",
    ),
    dict(
        key="insights", skill="fv-insights", label="Insights (stage 6)",
        desc="Turn the metrics into findings, root causes and risks — with "
             "the evidence attached",
        triggers="Use when the user asks what the numbers mean, why something "
                 "changed, what is going wrong or well. Triggers on \"what "
                 "does this mean\", \"why did it drop\", \"what should I "
                 "worry about\", \"summarize the findings\".",
        body="""
## Show them

The executive summary, then findings, root causes and risks. Each carries the
metric, the filter, the comparison baseline and the supporting count — relay
those with the claim, not separately. A finding without its baseline is an
assertion.

## The line this stage does not cross

**Correlation is not causation, and a root cause here is a hypothesis with
evidence, not a proven mechanism.** If the payload hedges, keep the hedge. Do
not upgrade "associated with" into "caused by" because it reads better.

Watch for these institute-specific traps when relaying:

- A branch difference on small numbers — check `n` and the interval first.
- A faculty comparison. This is **allocation and outcome**, never teaching
  quality: there is no attendance, assessment or feedback data, so a tutor with
  worse completion may simply have been given the harder cohort.
- A staff alumnus flagged in the data is one human in two roles, not a duplicate.

## Then ask

- Show findings, causes and risks in full.
- Continue to recommendations.
""",
    ),
    dict(
        key="recommend", skill="fv-actions", label="Recommendation (stage 6.5)",
        desc="Propose prioritized, owner-tagged actions from the insights",
        triggers="Use when the user asks what to do about the findings, wants "
                 "an action plan, or asks what to fix first. Triggers on "
                 "\"what should I do\", \"give me actions\", \"what's the "
                 "priority\", \"how do I fix this\".",
        body="""
## Show them

Actions by priority bucket, each with its owner and the finding it came from.
This is the only stage that proposes actions — if an earlier stage's output
sounded like an instruction, it was a finding being over-read.

## Match the action to who owns the sheet

The workbook tells you the audience: admission and enquiry sheets belong to
**counsellors**, the student-data workbook to **admin**, the timetable workbook
to **faculty**. An action a counsellor cannot take does not belong in a
counsellor's report.

## Then ask

- Show the actions with their owners and priorities.
- Continue to monitoring.

If the operator disputes an action, that is worth recording:
`--decision "recommend:rejected:<reason>"`. The report can then explain itself.
""",
    ),
    dict(
        key="monitor", skill="fv-monitor", label="Monitoring (stage 7)",
        desc="Register KPI hooks and evaluate them, raising threshold alerts",
        triggers="Use when the user wants ongoing alerts, KPI thresholds, or "
                 "to know what to watch. Triggers on \"set up alerts\", "
                 "\"monitor this KPI\", \"warn me when\", \"what should I "
                 "keep an eye on\".",
        body="""
## Show them

Overall health, the count of active alerts, and each event: metric, what fired,
and against which threshold.

Only hooks whose metric the Analyst can actually compute become active. The
rest register **inactive** — visible, but not pretending to watch something
that cannot be measured on this data.

## The registry persists

Thresholds are meant to survive between runs, so the registry lives beside the
session (or wherever `--registry` points). One consequence to watch: repeated
runs against the same source accumulate hooks, and the alert count climbs. If
the number looks inflated, check whether it is the same alert registered
several times.

## Then ask

- Show the hooks and alerts.
- Continue to the report.
""",
    ),
    dict(
        key="report", skill="fv-report", label="Report Writer (stage 8)",
        desc="Compose the shareable HTML report from the run, or re-render an "
             "earlier one from its saved JSON",
        triggers="Use when the user wants the final report, a shareable "
                 "document, an HTML summary, or wants to regenerate a past "
                 "report without re-analyzing. Triggers on \"build the "
                 "report\", \"give me something to share\", \"export this\", "
                 "\"write it up\", \"re-render that report\".",
        body="""
## Show them

The path to the HTML file, and what is in it: executive summary, KPI scorecard,
trend, recommendations, monitoring, and the data-quality footer.

The report asserts before it is written that no 10-digit mobile number survives
in it. Say the file is safe to share, and say why — the PII was masked at stage
2, not stripped at the end.

## What is in it is what ran

Skipped questions appear as skipped, with reasons. Do not describe the report
as covering questions it declined to answer.

## Then ask

- Open it.
- Start a new analysis on another sheet: `--new --csv <path>`.

## Re-rendering an earlier report

If the operator has a saved run JSON and wants the HTML rebuilt — restyled, or
just lost — there is no need to re-analyze anything:

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/run_pipeline.py" \\
  --from-json "<run.json>" --out "<report.html>"
```

The JSON holds `final_report` and `question_results`, so the numbers are the
ones from that run, not recomputed. Say which run it came from.
""",
    ),
]

SKILL_TEMPLATE = """---
name: %(skill)s
description: %(desc)s. Runs %(label)s of the FV Institute pipeline and stops at a checkpoint, back-filling any prerequisite stages. %(triggers)s
---

# %(label)s

Runs one stage and stops. The operator decides what happens next.
%(body)s%(protocol)s
"""

COMMAND_TEMPLATE = """---
description: %(desc)s (%(label)s), then stop at a checkpoint
argument-hint: [csv-or-xlsx-path-or-sheet-url] [business question]
allowed-tools: Bash, Read
---

Run %(label)s for the user and stop there. Follow the `%(skill)s` skill for
what to show and what to offer; this command only resolves the arguments.

User input: `$ARGUMENTS`

1. If a source is given, pick its flag: `http…` → `--sheet-url`, `.xlsx` →
   `--excel`, `.csv` → `--csv`. Anything after it is the business question.
   With no source, continue the existing session at `.fv/session`.
2. Run it — prerequisites back-fill automatically:

   ```bash
   python "${CLAUDE_PLUGIN_ROOT}/scripts/run_stage.py" \\
     [--csv "<source>"] [--question "<question>"] --stage %(key)s --json
   ```

3. Present the checkpoint and wait. Exit code 2 means the stage refused —
   relay its reason verbatim and stop.
"""


def write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    print(f"  {os.path.relpath(path, ROOT)}")


def main() -> int:
    print("Writing stage skills:")
    for stage in STAGES:
        fields = {**stage, "protocol": PROTOCOL % {"key": stage["key"]}}
        write(os.path.join(ROOT, "skills", stage["skill"], "SKILL.md"),
              SKILL_TEMPLATE % fields)
    print("Writing stage commands:")
    for stage in STAGES:
        write(os.path.join(ROOT, "commands", f"{stage['skill']}.md"),
              COMMAND_TEMPLATE % stage)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
