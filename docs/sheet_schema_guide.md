# Sheet schema guide — how to structure your sheets

The pipeline maps a column to a **role** by matching keywords in the header. Use
the headers below and detection is automatic — no config. Headers are
case-insensitive and matched by substring, so `Total Fees` and `total fees (₹)`
both hit the `amount` role.

Rules that make everything else work:

- **One row = one enrollment** (not one person). A repeat student gets a new row
  with a new `Student-ID`.
- **Link across sheets** = every sheet that should join carries the SAME
  `Student-ID`, and ideally `Student Name` + `Mobile No (Student)` (the person
  link is a hash of name + phone, so keep them consistent).
- **Dates**: one format, ideally `YYYY-MM-DD`. Set the column to Date type.
- **Money**: numbers only (`12000`), not `₹12,000/-`. Commas/symbols are
  tolerated but plain numbers are safest.
- **Never write status inside the Name** (`Aarav (cancelled)`). Use a `Status`
  column. (The pipeline still parses name-markers as a fallback, but a column is
  correct.)

---

## 1. Students (master sheet) — one row per enrollment

| Column header | Role | Required? |
|---|---|---|
| `Student-ID` | student_id | **yes** (the join key) |
| `Student Name` | name (hashed) | yes (person link) |
| `Mobile No (Student)` | student_mobile (hashed) | yes (person link) |
| `Mobile No (Parent)` | parent_mobile (hashed) | optional |
| `Email` | email (hashed) | optional |
| `Date of Birth` | dob (hashed) | optional |
| `Date of Admission` | admission_date | yes (funnel + trends) |
| `Date of Joining` | joining_date | optional |
| `Which Course` | course | yes |
| `Course Category` | course_category | optional |
| `Branch` | branch | yes (breakdowns) |
| `Faculty` | faculty | optional |
| `From Where` | source | optional (marketing) |
| `Mode` | mode (online/offline) | optional |
| `Status` | status | optional |
| `Address` | address (hashed) | optional |
| `Pincode` | pincode | optional |

Unlocks: admission counts/trends, breakdowns by branch/course/faculty/source,
repeat-student detection, person linkage.

## 2. Enquiry / Admission form — one row per lead

| Column header | Role | Notes |
|---|---|---|
| `Timestamp` | enquiry_date | when the lead came in |
| `Student Name` | name (hashed) | for the person link |
| `Mobile No (Student)` | student_mobile (hashed) | **the phone link to admissions** |
| `Which Course` | course | course asked |
| `Preferred Branch` | branch | |
| `From Where` | source | lead source |
| `Date of Admission` | admission_date | fill ONLY if this lead converted |
| `Status` | status | `Converted` / `Lost` / `Pending` |

Unlocks: enquiry→admission conversion. A lead with an enquiry but no admission
(and same person never appears admitted elsewhere) counts as **not converted**;
if the same phone shows up admitted on the Students sheet, it counts as a
**cross-sheet conversion**.

## 3. Fees — receipt ledger (one row per payment)

| Column header | Role | Notes |
|---|---|---|
| `Student-ID` | student_id | join key |
| `Receipt ID` | receipt_id | marks this as the LEDGER |
| `Date of Receipt` | receipt_date | installment date |
| `Paid Amt` | paid | amount of THIS receipt |
| `Mode of Payment` | payment_mode | cash/upi/bank |
| `Description` | description | free text; channel + refunds parsed from it |

Unlocks: paid total, installment count, payment span, channel mix, refund
detection. **Put refunds in the Description** (e.g. `2400 refunded`) — they're
excluded from net paid.

## 4. Fees — rollup (one row per enrollment)

| Column header | Role | Notes |
|---|---|---|
| `Student-ID` | student_id | join key |
| `Total Fees` | amount | billed total |
| `Amt Pending` | pending | outstanding (>0 = default; <0 = overpaid) |
| `Mode of Payment` | payment_mode | optional |

Unlocks: **collection efficiency**, **default rate**, **default aging**,
reconciliation gap (ledger paid vs rollup total − pending). Keep the ledger AND
the rollup for the fullest fee picture.

## 5. Certificates — one row per certificate

| Column header | Role | Notes |
|---|---|---|
| `Student-ID` | student_id | join key |
| `Student Name` | name (hashed) | optional |
| `Certificate Number` | certificate_number | **must be unique** — repeats flagged |
| `Certificate Issue Date` | issue_date | blank = pending |
| `Date of Joining` | joining_date | used for issue-lag days |
| `Which Course` | course | optional |

Unlocks: certificate issue-lag, pending-certificate rate, **duplicate-serial
alert**.

## 6. Timetable workbook — the completion / churn LABEL

This is the most valuable sheet for prediction. It is **one workbook with three
tabs**, and the **tab name is the label** (do not put status in a column):

- Tab `Main_data` → students still **active**
- Tab `Course_Completed` → **completed**
- Tab `Not_Coming` → **churned** (not coming)

Columns on each tab:

| Column header | Role | Notes |
|---|---|---|
| `Student-ID` | student_id | join key |
| `Student Name` | name (hashed) | person link |
| `Which Course` | course | |
| `Faculty` | faculty | |
| `Batch Timing` | batch_time | |
| `Days Remaining` | days_remaining | optional |
| `Status & reason` | status_reason | on `Not_Coming`: churn reason (free text; PII scrubbed) |

Unlocks: `completion_status`, completion / not-coming rates, and the
**churn-prediction model** (trains on completed vs not_coming; active rows are
censored/excluded).

Run it with the tab names preserved:

```bash
python scripts/run_pipeline.py --question "..." --excel timetable.xlsx
# each tab (Main_data / Course_Completed / Not_Coming) becomes a source
```

---

## Testing with the bundled sample data

Ready-made sample sheets live in `samples/`. Regenerate them any time with:

```bash
python scripts/make_sample_data.py
```

Run the whole pipeline on them:

```bash
python scripts/run_pipeline.py \
  --question "How is admission conversion and fee collection performing by branch?" \
  --source "students=samples/students.csv" \
  --source "enquiries=samples/enquiries.csv" \
  --source "fees_ledger=samples/fees_ledger.csv" \
  --source "fees_rollup=samples/fees_rollup.csv" \
  --source "certificates=samples/certificates.csv" \
  --source "Course_Completed=samples/timetable_Course_Completed.csv" \
  --source "Not_Coming=samples/timetable_Not_Coming.csv" \
  --source "Main_data=samples/timetable_Main_data.csv" \
  --out sample_report.html
```

The sample data is built to exercise every feature: a repeat student, one
converted + one lost lead, a fee reconciliation mismatch, a defaulter, a
duplicate certificate serial, and all three completion labels.
