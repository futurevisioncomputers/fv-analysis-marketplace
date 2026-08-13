---
name: fv-stats
description: Test whether a difference is real, when students actually leave, whether newer cohorts are worse, and whether a trend is genuine. Use when the user asks if a gap between branches or courses is significant, wants survival or time-to-churn analysis, cohort comparison, or a trend that is not thrown off by spikes. Runs statistical tests over a session that has already been cleaned. Triggers on "is that difference significant", "is this real or noise", "when do students drop out", "are newer batches worse", "is admissions actually declining", "compare branches properly".
---

# Statistical tests

Not a pipeline stage. A tool pointed at a run that has already cleaned its
data: the stages say what the number is, these say whether to believe it.

```bash
python "${CLAUDE_PLUGIN_ROOT}/scripts/run_stats.py" --analysis <kind> [options]
```

Reads the session's cleaned frame, so `--stage clean` must have run. Writes
nothing back. Add `--json` for the raw result; `--session <dir>` for a run
other than `.fv/session`.

## Pick the analysis from the question

| They ask | `--analysis` | Needs |
|---|---|---|
| "Is Citylight really worse?" | `segments` | `--flag is_default --by Branch` |
| "When do students leave?" | `survival` | `--duration months_since_admission [--event is_not_coming]` |
| "Are newer batches worse?" | `cohorts` | `--flag is_completed [--cohort joining_cohort]` |
| "Is admissions declining?" | `trend` | `--metric monthly_records` or a column |

`survival` and `cohorts` need columns built at stage 2.7 — if they are missing
the CLI says exactly which `--approve-features` call produces them.

## segments — is the gap real

Each segment is tested against everyone else, then corrected with
Benjamini–Hochberg. **Report the correction, not just the rates.** Comparing
fifteen branches at the 5% level finds roughly one "worst" branch every time by
construction; the q-value is what separates a finding from an artefact of
having looked fifteen times.

Read the output honestly:

- `SIGNIFICANT` — worth acting on.
- No marker, overlapping intervals — say so plainly. *"Citylight is 7.9% and
  Vesu 4.9%, but with these numbers the difference is not distinguishable from
  chance"* is a complete answer, not a failure.
- `(too few rows to test)` — reported with its rate, deliberately not tested. A
  verdict on n=6 is noise wearing a p-value.

## survival — when, not just how many

A churn *rate* treats someone who left in week three and someone still enrolled
as the same row. This does not: rows where the outcome has not happened yet are
**censored**, and contribute to the risk set until then.

That distinction is the institute's own: active and paused students are
censored; `NOT TO ENTERTRAIN` and not-coming past six months are events. With
no `--event` column the tool reads `completion_status` for exactly that split.

`median_survival: not reached` is a real answer — over half have not had the
outcome — and must not be reported as zero or as "no churn".

## cohorts — are newer intakes worse

One rate per joining cohort with intervals, plus a Mann–Kendall across them.

**A "flat" verdict on few cohorts is not evidence of stability.** Four cohorts
cannot reach significance even when perfectly monotone — the maximum
attainable p is 0.089. The CLI prints this caveat; keep it.

## trend — a slope spikes cannot move

Mann–Kendall for whether a monotonic trend exists, Theil–Sen for how steep it
is. Both are rank/median based, so one data-entry spike or a festival month
does not create a trend that is not there.

## What to say back

Relay the numbers, the intervals, and the verdict. Then say what it licenses:

- Significant, decent n → a finding worth an action.
- Not significant → the difference is not established. Do not soften this into
  "slightly worse" — that is the same claim with a hedge.
- Underpowered → say how many rows would be needed rather than implying the
  answer is unknowable.

Never re-run a test with a different threshold because the first was not
significant, and never report a p-value without the n beside it.
