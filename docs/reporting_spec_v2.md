# Reporting specification v2 — coverage map

The institute's master specification for what the plugin must report, and an
honest account of where the code stands against it. The spec text is the
institute's; the **Status** column is measured against the current codebase,
not aspirational.

Companions: `docs/form_schema_notes.md` (the sheets and their defects) ·
`docs/restructure_plan.md` (the rebuild in flight).

Legend: **✅ built** · **◐ partial** — works but incomplete or single-source
only · **○ missing** · **⚠ conflict** — the spec and the measured data disagree,
see §C.

---

## A. Reports (spec §§1–20)

| § | Report | Status | Where it stands |
|---|---|---|---|
| 1 | Executive / management dashboard | ◐ | KPI scorecard, trend and health exist; no single executive view, and ~half the named KPIs have no metric (§B) |
| 2 | Branch performance | ◐ | `branch` is a first-class dimension; scorecard is per metric, not a branch-shaped card |
| 3 | Course category performance | ◐ | `course_category` dimension exists; no category→course→branch→tutor drill path |
| 4 | Course performance (full lifecycle) | ◐ | Demand/enrollment/finance yes; active population and certification timing partial |
| 5 | Short / medium / long course analysis | ○ | `course_duration` role exists; no duration **grouping**, no configurable thresholds |
| 6 | Course duration analysis | ○ | Same — buckets not implemented |
| 7 | Faculty / tutor report | ◐ | `faculty` dimension and canonical spellings exist; no allocation/load metrics, no `tutor` vs `faculty` split |
| 8 | Counsellor performance | ○ | No `counsellor` role at all. Attribution needs the enquiry→admission match, which is the estate's weakest join (78–81%) |
| 9 | Enquiry source performance | ✅ | `source` dimension + conversion metrics. Correctly *not* framed as marketing ROI |
| 10 | Enquiry mode performance | ○ | No `enquiry_mode` role; a bare `Mode` column currently has no safe home |
| 11 | Student demographic / profile | ◐ | `education`, `occupation`, `pincode`, `dob` are captured and masked; no age grouping, no area rollup |
| 12 | Batch & timing performance | ◐ | `batch_time` and `preferred_days` exist as roles; preferred-vs-actual is not distinguished, so demand cannot be compared with supply |
| 13 | Student lifecycle report | ◐ | Stages exist as flags; the funnel is not assembled as one report, and the churn label is still wrong (see `restructure_plan` E2) |
| 14 | Churn / not-coming analysis | ◐ | `not_coming_rate` + `churn_reason` text; the 6-month rule and the `NOT TO ENTERTRAIN` tab are not yet honoured |
| 15 | Course completion analysis | ◐⚠ | `completion_rate` yes; **average completion time cannot be computed** — no completion date exists in the sheets. The spec agrees; do not invent one |
| 16 | Certificate analysis | ✅ | Issued counts, `certificate_issue_lag_days`, `certificate_pending_rate`, duplicate-serial detection, `student-id` as the key |
| 17 | Finance dashboard | ✅ | Fee-data snapshot and receipt ledger are already separate concepts with a reconciliation engine between them |
| 18 | Finance by multiple factors | ✅ | Any fee metric crosses two dimensions with a filter layer (§21) |
| 19 | Payment mode analysis | ✅ | `payment_mode` role + `payment_channel` parsed from free text |
| 20 | Financial reconciliation | ◐ | Per-student gap, mismatch flag, negative-pending flag, aging buckets, installment counts. Missing the named exception classes: missing-receipt, missing-fee-record, duplicate-receipt |

## B. Engine and governance (spec §§21–30)

| § | Requirement | Status | Notes |
|---|---|---|---|
| 21 | Multi-factor analysis engine | ✅ | `agents/multifactor.py` + `/fv-cross`. One metric × two dimensions, filter layer, chi-square over the grid, cells tested against **both** margins with BH correction, small cells suppressed and counted |
| 22 | Dimension registry | ◐ | 48 roles. Added: `counsellor`, `staff_role`, `student_category`, `enquiry_mode`, `admission_mode`, `learning_mode`, `fee_status`, `churn_status`, `residential_area`, `education_details`, `preferred_branch`/`preferred_batch_time`/`class_days` (split from their actuals), `parent_mobile_2`, `record_timestamp`, `coupon_given`, `notes`. Still missing: `age_group`, `outstanding_bucket`, `duration_group` — all three are derived buckets, so they belong to the multi-factor engine (§21) |
| 23 | Cross-factor reports (15 pairs) | ✅◐ | The 15 pairs are registered; `--suggest` offers only those the source supports, and the Analyst stage crosses the top 3 unprompted. Derived buckets (`age_group`, `outstanding_bucket`, `duration_group`) still need approving at stage 2.7 before they can be crossed |
| 24 | Management insight engine | ✅ | Insights already cite metric, filter, baseline and n, and the agents refuse to answer rather than invent |
| 25 | Report generation modes | ○ | One mode today. `--max-questions` is the only lever |
| 26 | Standard report structure | ◐ | Executive summary, KPI scorecard, trend, recommendations, monitoring, data quality exist. Missing: comparison, factor analysis, drill-down, exceptions, methodology/formula |
| 27 | Implementation priority P0–P4 | — | Mapped into the build order below |
| 28 | Data governance & guardrails | ✅◐ | Never-invent, profile-unknown-fields, ignore-blank-columns, PII masking, traceable transforms all hold. Name-only matching is now **refused** (E1b). Confidence levels for cross-dataset matches: partial |
| 29 | Financial rules | ✅⚠ | Now enforced — see §C.1. Fee-data vs receipts are not double-counted; receipt date drives collection trend |
| 30 | Core architecture | ◐ | Question → analyzer → metric/dimension → data → KPI → insight → report exists as a stage pipeline. Filter engine and multi-factor selection do not |

## C. Where the spec and the data disagree

Three places. In each the measurement wins, and the spec text needs amending.

### C.1 "Receipt-id should normally be unique" — it is not ⚠

Measured across the estate: receipt books are **per branch and restart every
few hundred entries**, so the same number recurs across branches and years. A
receipt paid part-cash part-online is entered as **two rows sharing one id**.
And on the admission sheet `Receipt ID` is a *foreign key* to the ledger, not a
row key at all.

Treating it as unique deleted 55% of the ledger, 32% of admissions and 18% of
certificates. Fixed: a candidate key is measured before it is trusted
(`restructure_plan` E1a).

The spec's own rule — **never SUM Total Fees across receipt rows** — was being
violated by the reconciliation engine, which preferred `Total Fees` over
`paid amt` when both were present. Since `Total Fees` repeats on every one of a
student's receipt rows, collections read **₹1,73,47,585 against ₹71,28,426
actually paid — 2.4×**, and collection efficiency came out at **229.9%** with
222 false mismatches. Now fixed: `paid` wins, `amount` is a fallback only for a
ledger with no paid column. Post-fix the same run reads ₹71,28,426, 94.5%, and
6 real mismatches.

### C.2 "Use student-id as the identity key" — it is an *admission* key ⚠

The institute confirmed one person takes several admissions (Khiren Jain holds
ids 3, 244, 609, 1070). So `student-id` identifies an **enrollment**, not a
person. Both are needed and they are not the same key:

```
admission_id := student-id          one per admission — the right join key
person_id    := resolve(name, phone, DOB, …)   one per human — needed for
                                    repeat rate, lifetime value, churn history
```

The spec's rule is right where it matters — *never match by name alone* — and
that is now enforced: without a phone, DOB or email the person-grain metrics
are withheld rather than guessed (E1b).

### C.3 Completion duration has no source field

Spec §15 already says not to compute it. Recording it here as a permanent
constraint: the timetable tabs carry membership, not dates. Anything described
as "average completion time" must come from a completion date the institute
does not currently record. The 6-month churn rule counts from **admission
date** for the same reason.

## D. What this changes in the build order

The spec's P0 is largely the work already in flight; its P1–P2 is mostly new.

| Spec priority | Maps to | Status |
|---|---|---|
| P0 schema profiler, data dictionary | Stage 2.5 + role discovery | ✅ built |
| P0 student identity resolution | E1b person key + basis reporting | ✅ built |
| P0 reconciliation | multi-source finance engine | ◐ (C.1 fixed; exception classes pending) |
| P0 data quality engine | quality report + known issues | ✅ built |
| P0 KPI engine | 21 metrics with CIs | ◐ (see §B/22 for missing metrics) |
| P0 multi-dimensional analysis engine | §21 | ✅ built |
| P1 core reports | one report shape today | ◐ |
| P2 cross-factor intelligence | duration groups, 15 pairs | ◐ — pairs done, duration groups are approval-gated at 2.7 |
| P3 management intelligence | anomalies ✅, alerts ✅, NL questions ✅; export/scheduling ○ | ◐ |
| P4 future data | attendance, tests, feedback, spend, placement | ○ — correctly out of scope until the data exists |

Two items are promoted into the current plan as a result:

- **A3 — canonical alias layer.** The institute's field-name table becomes the
  public vocabulary, mapped onto internal role keys. It also adds the roles
  §22 needs: `counsellor`, `tutor`/`staff_role`, `enquiry_mode`,
  `student_category`, `record_timestamp` (distinct from `enquiry_date`),
  `preferred_*` vs actual, and contextual `fee_status` / `churn_status` in
  place of a bare `status`.
- **G — multi-factor engine (§21/§23).** One metric across two dimensions, with
  a filter layer. Everything in P2 depends on it, and nothing else in the spec
  unlocks as many reports at once.
