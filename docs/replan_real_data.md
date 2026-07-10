# Replan After Full Real-Data Study

Studied all 9 real sheets: `student-data` (old + new, ~1508 enrollments), `fees-recpit`
(transaction ledger), `fees-data` (per-enrollment status), `certificate-data`,
`Admission Form (Responses)`, `Enquiry Form (Responses)` 1 & 2, `Student_Time_Table`.
This document (A) lists what the real data actually looks like, (B) replans each agent,
(C) proposes sheet changes so future data supports prediction models.

---

## A. What the real data actually is (findings)

### A1. It is relational, not one flat sheet
- `student-id` is an **enrollment id**, not a person id. Same person re-enrolls with new
  ids (Khiren Jain: 3, 244, 609, 1070; Aadit Joshina: 373, 417, 444, 478; Rupesh Yadav: 74, 85).
- `fees-recpit` has **multiple rows per enrollment** (installments). `fees-data` is the
  per-enrollment rollup (Total Fees / Status / Amt Pending).
- Enquiry and Admission forms are separate lead-stage tables with **no linking id** —
  only phone numbers overlap.

### A2. Categorical chaos (breaks role profiling and cross-tabs)
- Faculty variants: `Siddharth` / `Siddharth Sir`, `Yash` / `Yash Sir` / `Yash k` /
  `Yash Kanodia Sir`, `Mansi` / `Mansi Mam`, `vansh` / `Vansh Sir`, `Subin sir`, `Trusha` / `Trusha Mam`.
- Branch case variants: `Vesu` / `citylight` / `Citylight` / `Pal` / `NA`.
- Mode: `Offline` / `offline` / `online`.
- Course is free text with 400+ distinct strings for ~40 real courses:
  `Advance Excel` vs `Advanced Excel` vs `Advance excel 1&2` vs `Advanced Excel (M-1 & M-2)`,
  typos (`Web developmnet`, `Dipolma`, `Graohic`), module suffixes `(m-1)`, `(Module 2 & 3)`.
  This blows past `MAX_CATEGORICAL_CARDINALITY = 30` — course currently drops out of EDA.

### A3. Date chaos
- `student-data` joining dates are MM/DD/YY(YY); Admission Form uses DD/MM/YYYY;
  certificate issue dates mix both plus text (`hand written`, `Given`, `-`, `NA`).
- 2-digit years (`12/18/23`), impossible years (`4/23/0026`, `4/22/0026`), future-typo
  receipts (`8/23/2026` inside a 2025 block), `1/8/1998` admission for a 2025 student.

### A4. Status buried in text
- Cancellations/refunds live inside the **Name** column: `(cancelled)`, `(not coming)`,
  `(admission cancelled all refunded)`, `(Admission Cancelled)`, plus trial notes
  `(Register for trial ...)` in enquiry names.
- Refund facts live in free-text Description (`2400 refunded`, `refund from icici`).

### A5. Fee ledger inconsistencies (also: signal!)
- Negative pending: Tanish Kalra `-7200`. Zero-fee rows for cancelled (id 98, 435, 681).
- `Full Paid` in fees-data while receipts sum ≠ Total Fees in several cases; receipts
  sometimes recorded at other branch (`admission at citylight`, `receipt made at citylight`).
- Receipt-id sequences restart per branch/book and collide (same id reused).
- Payment channel is text in Description: `paid to ICICI`, `paid to HDFC`, `razorpay emi`,
  `paid to sc/shaurya creation`, cheque numbers. This is a real feature (EMI users behave
  differently) but currently unparseable.

### A6. Certificates
- Duplicate certificate numbers (V121222182 twice, C250823004 twice, C220606198 twice).
- Multi-certificate cells (newline/comma separated). Many blanks = certificate never issued
  or never recorded — indistinguishable today.

### A7. PII edge cases current masking misses
- `+1(414) 526-5885`, `+818035074667`, `+13068074262`, 11–13 digit strings,
  malformed emails (`tannaa@123@gmail.com`, `Mehtakrishang230908` no domain).
  `_MOBILE_RE = \b\d{10}\b` misses the international ones.

### A8. Structural junk
- Timetable sheet: `zzzzz (Don't Delete)` placeholder rows, blank interleaved columns.
- `student-data` new sheet: trailing empty columns, stray `1508` token in header row,
  `Timestamp` column only populated post-Apr-2024.
- Enquiry sheet ~150 anonymized `ENQ-####` rows with only a phone + counsellor.

---

## B. Agent replan (concrete changes per agent)

### Agent 0 / Orchestrator — multi-file runs
- Accept a **bundle** (directory / list of CSVs), not one CSV. Build a dataset registry:
  classify each file as `enrollments | payments | fee_status | certificates | admissions_form |
  enquiries | timetable` via header-role voting (reuse role keywords).
- Sequence joins before analysis: payments ⨝ enrollments on student-id; forms/enquiries
  linked by normalized phone.

### Agent 2 Data Engineer — biggest rework
1. **Entity resolution:** derive `person_id = hash(normalized_name + best_phone)` with
   fuzzy name match fallback; keep `enrollment_id` (= student-id). Emit repeat-enrollment
   count per person.
2. **Canonicalization dictionaries** (config-driven, shipped in repo):
   - faculty map (strip `Sir/Mam`, `Yash k` → `Yash Kanodia`),
   - branch title-case map + `NA` → null,
   - mode lower-case map,
   - **course canonicalizer:** normalize spelling/typos, strip module suffix into a new
     `course_module` column, fuzzy-map to a course catalog (`course_family`, `category`).
     Target: course_family cardinality ≤ 40 so EDA/cross-tabs work again.
3. **Date parsing v2:** per-column format vote (day-first vs month-first), 2-digit year
   expansion, reject rows outside [2021, today+1y] into quality report (don't silently keep
   year-0026), treat non-date tokens (`Given`, `hand written`) as issue flags not dates.
4. **Status extraction:** regex parenthetical from Name → `enrollment_status`
   {active, cancelled, refunded, not_coming, trial}; strip from name BEFORE hashing so the
   same person hashes identically.
5. **Payment reconciliation table:** per enrollment — `paid_sum`, `n_installments`,
   `first/last_payment_date`, `payment_span_days`, `channel` (parsed from Description:
   icici/hdfc/razorpay-emi/cash/cheque), `recon_flag` when `paid_sum + pending ≠ total`
   or Status says Full Paid but receipts disagree, negative-pending flag.
6. **PII mask v2:** extend regex to `+?\d[\d\s().-]{8,14}\d`, mask emails, mask parent
   phones; assert on report output as today.
7. **Junk row purge:** drop `zzzzz` placeholders, all-empty trailing columns, duplicate
   header rows.

### Agent 3 EDA
- Profile on canonical columns (`course_family`, canonical faculty/branch), not raw.
- Add old-vs-new sheet drift check (the two student-data exports disagree on faculty
  spellings and some phones — report it, pick newest by Timestamp).

### Agent 4 Analyst — new computable metrics (data now supports)
- `repeat_enrollment_rate` (person-level) and upsell revenue share.
- `collection_efficiency = Σpaid / Σtotal` by branch/course/month.
- `default_rate`: pending > 0 AND last payment older than N days (aging buckets 30/60/90).
- `avg_installments`, `payment_span_days` distribution.
- `enquiry_to_admission_conversion` via phone linkage, by source / counsellor / branch.
- `certificate_issue_lag_days` (joining→issue) + `%never_issued`.
- `effective_discount` = catalog price − charged fee (needs catalog sheet, see C).

### Agent 6 Insights / Agent 6.5 Recommendation
- New conditional sections: fee-aging & default risk, repeat-student value, lead-source ROI
  (conversion by `From Where Do You Know About Us`), certificate SLA.

### Agent 7 Monitoring — new hooks
- pending-fee aging breach, negative-pending anomaly, receipt-vs-status reconciliation
  mismatch count, duplicate certificate number detector, enquiry backlog (leads with no
  outcome after X days).

### NEW Agent 4.5 (optional, later): Prediction
- Only after C-changes land. Candidate models: fee-default risk (features: installment
  count, channel=EMI, course price band, branch, days since last payment), lead-conversion
  propensity (source, counsellor, course asked, days-to-decision), upsell propensity
  (person history). Keep it behind the same honesty gate: refuse to train when labels
  are absent/dirty.

---

## C. Sheet changes to request from the institute (for future prediction)

Priority-ordered. 1–5 are cheap (dropdowns + one column) and unblock the most.

1. **Dropdowns everywhere** (Google Sheets data-validation): Course (from a Catalog tab),
   Branch, Faculty, Mode, Payment Mode. Kills 90% of the cleaning burden permanently.
2. **Course Catalog tab:** `course_id, course_name, category, standard_fee, duration_days,
   modules`. Every enrollment references `course_id`. Enables discount detection, price
   realization, duration-based completion prediction.
3. **Status column** on the student sheet: `Active / Completed / Dropped / Cancelled /
   Refunded` — never write status inside the Name again. Completion label = the single
   most valuable ML target the data currently lacks.
4. **One date format** (set columns to Date type, display YYYY-MM-DD). Includes certificate
   issue date — no `Given` / `hand written` text; add a separate `delivery_note` column.
5. **`enquiry_id` carried into the Admission Form** (and receipt sheet keeps `student-id`
   as it does). This makes lead→admission conversion exact instead of phone-fuzzy.
6. **Stable `person_id`:** when an old student re-enrolls, reuse their person id and only
   issue a new enrollment id. Even a simple "old student? previous id:" form field works.
7. **Payment plan columns** on fees sheet: `due_date` per installment (or `emi_plan`,
   `next_due_date`). Without a due date, "default" is undefined — with it, default
   prediction becomes trainable.
8. **Completion / attendance signal:** `expected_end_date`, `actual_end_date`, and ideally
   `last_attended_date` or monthly attendance %. This is the churn label + early-warning
   feature.
9. **Refund & discount as numbers:** `discount_amount`, `refund_amount` columns instead of
   Description prose.
10. **Phone hygiene:** one 10-digit field + separate country-code field; form validation
    to reject 9/11-digit entries.
11. **Enquiry outcome field:** `Converted / Lost / Pending` + lost-reason dropdown. Today
    lost leads are silent; conversion models need negatives, and the ENQ-#### phone-only
    rows are nearly useless — capture at least course + branch + outcome for every call.
12. **Certificate register:** unique cert number enforced (sheet formula flag on dupes),
    one row per certificate (not newline-multi-cells).

---

## C2. Update — timetable workbook carries the missing labels

Two additional sheets studied (`Course_Completed`, `Not_Coming`):
- **Sheet membership = lifecycle label.** Course_Completed → `completed`,
  Not_Coming → `not_coming`, Main_data → `active`. This is the completion/churn
  ground truth C-item 3 asked for — it already exists, just encoded in sheet
  names. Pipeline now derives `completion_status` from the source sheet name.
- **`Status & reason` column** on Not_Coming holds churn reasons + module-level
  progress ("word completed, powerpoint started", "gone to egypt for one month").
  Mapped to a `status_reason` role; churn-reason categorization (out-of-town /
  medical / timing-conflict / never-attended) is a cheap next analyst metric.
- **Institute convention confirmed:** admission cancelled ⇒ fees refunded, so
  `cancelled` and `refunded` are the same business outcome (`is_cancelled`
  covers both).
- Name notes like "(fast track)", "(FT till july end)", "(only till 30 may)"
  are operational notes, not statuses — now stripped (stable person hash) with
  an `is_fast_track` flag derived.

## D. Suggested implementation order

| Step | Work | Why first |
|------|------|-----------|
| 1 | ✅ Agent 2 canonicalization dicts + date parser v2 + status extraction | Unblocks every downstream metric on TODAY's data |
| 2 | ✅ Multi-file bundle ingest + payment reconciliation (`payment_reconciliation` in run_sources output: per-enrollment paid/refund/installments/span/channel + recon & negative-pending flags) | Enables fee/default analytics |
| 3 | ✅ Person-id entity resolution (`person_id` = salted hash of normalized name + last-10-digit phone, derived post-marker-strip / pre-mask; `person_enrollment_count`, `is_repeat_enrollment`). Phone-linked enquiry→admission conversion still open | Repeat-student + funnel metrics |
| 4 | ✅ New Analyst metrics + Monitoring hooks. Analyst METRIC_SPECS: `not_coming_rate`, `repeat_enrollment_rate`, `certificate_pending_rate`, `certificate_issue_lag_days` (+ `completion_rate` now backed by `is_completed`). Data Engineer emits `is_completed`/`is_not_coming` from completion_status; recon summary adds money-weighted `collection_efficiency = Σnet_paid/Σtotal_fees`, `default_aging` buckets (0-30/31-60/61-90/90+, as-of = ledger's latest receipt) + `overdue_90plus_amount`. Monitoring: business rules for not_coming/certificate_pending, area maps + LOWER_IS_BETTER + statistical-anomaly entries. ✅ collection_efficiency by dimension now lands via a new Analyst `ratio` kind (Σ numerator / Σ denominator, ratio-estimator 95% CI, two-period z on ratio SEs); Data Engineer derives per-enrollment `amount_collected = billed - pending` as the numerator; Monitoring gains a collection_efficiency business rule (<0.70 warn, <0.50 crit). ✅ `default_rate` now surfaced through Analyst (`rate`/`is_default`): Data Engineer flags per-enrollment `is_default = pending > 0` (negative pending is an overpayment, not a default); Monitoring adds a default_rate rule (>0.25 warn, >0.40 crit), area=Fees, LOWER_IS_BETTER. This is the enrollment-grain view; recon `default_aging` remains the money-weighted/overdue complement. Still open: duplicate-certificate + enquiry-backlog hooks | Business value visible in report |
| 5 | ✅ PII mask v2. Data Engineer scrubs inline phone/email from RETAINED free-text columns (status_reason, description, remark/note/reason/… headers) after column-level hashing — permissive phone pattern `\+?\d[\d\s().\-]{8,14}\d` gated by a ≥10-digit count so dates/pincodes/ids/amounts in the same cell survive. Report defence-in-depth widened: `_contains_mobile` catches bare 10-digit AND formatted/international phones, but ignores separator-free Chart.js numeric literals (epoch ms, large values) so valid reports aren't withheld. `llm_client.mask_text` broadened to the same guarded pattern | Correctness/safety |
| 6 | Hand institute the C-checklist; wait one intake cycle | Creates labels |
| 7 | Prediction agent (4.5) | Only meaningful after 6 |
