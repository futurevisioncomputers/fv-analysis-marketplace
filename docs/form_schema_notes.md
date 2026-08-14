# Google-Form export schemas — observed structure and dirty-data catalogue

Source of truth for the three form exports the pipeline ingests. Written from a
read of the institute's **real** sheets (Admission Form responses; Enquiry Form
responses 1 and 2). The real sheets carry live PII and are never committed —
`scripts/make_form_samples.py` regenerates synthetic look-alikes into
`samples/` that reproduce every structural quirk listed here.

Regenerate:

```bash
python scripts/make_form_samples.py                 # all sheets day-first
python scripts/make_form_samples.py --dates mixed   # reproduce the live mismatch
```

Deterministic (seed `20260812`), so tests can pin exact values.

> **Date format — read this first.** The **live** enquiry sheets are entirely
> `M/D/YYYY` (month-first); the live admission sheet is `DD/MM/YYYY`
> (day-first). The generated samples default to **day-first everywhere**,
> matching the standardized forms. Pass `--dates mixed` to reproduce the live
> discrepancy as a regression fixture. See §6.

## 0. Who owns each workbook

Each workbook is maintained by a different role, which is why the same person
appears under three spellings and why the sheets disagree about dates. It also
tells the report writer who the audience is for a single-sheet run.

| Workbook | Owner | Reports it should be written for |
|---|---|---|
| Admission Form (Responses) | **Counsellor** | admission volume, source, conversion |
| Enquiry Form (Responses) 1 & 2 | **Counsellor** | lead quality, follow-up backlog |
| student-data-sheet-from-1-4-22 (student-data, fees-data, fees-recpit, certificate-data) | **Admin** | collections, pending fees, certificates |
| Student_Time_Table2023 (Main_data, Course_Completed, Not_Coming, NOT TO ENTERTRAIN) | **Faculty** | batch load, completion, churn |

**The faculty studied here first.** Staff are former students, so the same
human is a student row in one column and the tutor in another, on the same
sheet. The roster is fixed — `Yash`, `Yash Kanodia`, `Mansi`, `Siddharth`,
`Vansh`, `Subin`, `Trusha` — and the collision is a *staff member holding an
old student record*, never a new name appearing in the Faculty column. The
synthetic samples model it that way round for the same reason: inventing
faculty would pollute every faculty breakdown with people who do not exist.

Three consequences, and each is wrong if the collision goes unnoticed:

- It is **not a duplicate**. One name across a student row and a faculty cell
  is one person in two roles, not two records to merge.
- It is **not necessarily churn**. A student who joins the staff stops
  attending as a student; if they land in a not-coming tab they look like a
  loss when they are the opposite. Bears directly on the churn rule in §10.
- They sit in **two populations**. Counted under both "students per tutor" and
  "students", the same human is counted twice.

The cleaner flags these as `is_staff_alumni` and deliberately does not act on
them: a match may equally be a namesake, the same ambiguity `person_id` refuses
to resolve on a name alone. Note also that a staff alumnus appears **masked**
in the student column and **in clear** in the faculty column — faculty is a
reporting dimension, not PII.

Two further consequences of the ownership split:

- **Data entry is per-role, so drift is per-role.** Names, faculty spellings
  and branch labels diverge across workbooks because different people typed
  them (§5). Never assume a spelling carries across a workbook boundary.
- **A single-sheet run has a known audience.** An admission-form run is a
  counsellor's report; a fees-data run is an admin's. Recommendations should
  address actions that role can actually take.

---

## 1. Admission Form (Responses) — 26 columns

The enrollment record. One row per admission, **not** per person.

| # | Column | Notes |
|---|---|---|
| 1 | `Timestamp` | `DD/MM/YYYY HH:MM:SS` |
| 2 | `Name for Google Contacts` | `Name-Branch-Course` concatenation; **stops being filled ~Jun 2026** |
| 3 | `Which Course do you want to Learn ?` | free text, see §4 |
| 4 | `Student Name` | status markers hidden inside, see §5 |
| 5 | `Mobile No (Student)` | primary join key |
| 6 | `Mobile No (Father / Guardian)` | ~60% filled |
| 7 | `Student Address` | free text |
| 8 | `Education Level` | vocabulary changes over time, see §4 |
| 9 | `From Where Do You Know About Us ?` | lead source; vocabulary changes |
| 10 | `Presently What Are you Doing ?` | occupation |
| 11 | `Preferred Branch ` | **trailing space in the header** |
| 12 | `Preferred Days` | `Monday, Wednesday, Friday` / `All Days` |
| 13 | `Preferred Batch Time` | occasionally two slots in one cell |
| 14 | `Faculty` | honorific drift, see §3 |
| 15 | `Receipt ID` | not reliably numeric or unique, see §6 |
| 16 | `Email Address` | ~60% blank, many malformed |
| 17 | `Date of Birth` | ~30% blank; some equal the admission date |
| 18 | `Date of Admission` | the real event date; differs from Timestamp |
| 19 | `Mobile No (Mother / Guardian)` | ~28% filled |
| 20 | `Any Other Notes` | rare (~5%) but load-bearing (fees, timing, age) |
| 21 | `Education Details` | only collected from ~Feb 2025 |
| 22 | `Student Photo` | Google Drive URL, two link formats |
| 23 | `Admission Form Photo` | Google Drive URL |
| 24 | `Residential Area` | locality, coarser than `Student Address` |
| 25 | `Pincode` | some 5- and 7-digit values |
| 26 | `Coupons Given?` | **only exists from ~14 May 2026**; `Yes`/`No` |

## 2. Enquiry Form Responses 1 — 25 columns

Named enquiries, Apr 2024 → Mar 2026. In the **live** sheet, timestamps are
`M/D/YYYY` (month-first) — the opposite of the admission sheet. Samples are
day-first unless `--dates mixed`. See §6 for why this matters.

Structural traps:

- **`Email Address` appears twice** (columns 2 and 16). Column 2 is always
  empty; column 16 holds the data. A `DictReader` silently keeps only one.
- **One unnamed column** (`""`) sits between `Counsellor Name ` and
  `Mode of Enquiry`.
- **Seven columns are present but never filled**: `Preferred Batch Time`,
  `Preferred Days`, `Your Photograph`, `Faculty`, `Date of Birth`,
  `Receipt ID`, `Process Status`. Drop the column, keep the row.
- `Mode of Enquiry` (`On Call` / `On Branch` / `On Bot`) only starts being
  recorded ~Sept 2025. Blank before that means "not captured", not "unknown".
- A bulk import shares **one identical timestamp across a block of rows**.
- ~14% of later rows are anonymized call logs: the `Name` column holds
  `ENQ-8462` / `ENQ - 8544` / `Enq 8483` and only a phone number is present.

## 3. Enquiry Form Responses 2 — 19 columns

The call-log tab, Apr 2026 →. Thinnest sheet, most damaged headers.

- Columns are literally named **`Column 2`**, **`Column 1`**, and
  **`enq discuusin`** (typo preserved). Plus a trailing unnamed column.
- `Column 2` holds the enquiry id, sometimes with a suffix:
  `ENQ - 8715 (cancelled)`, `ENQ - 8795 (Shreya)`, `enq test`.
- ⚠ **This sheet has no student name column at all.** The only name on it is
  `Counsellor Name`, and with no `counsellor` role defined it won the `name`
  role by default — so person identity was built from *the counsellor* plus the
  student's phone, and "4 repeat enrollments" really meant one counsellor
  handling four enquiries. Found by the capability test. **Fixed**: a
  `counsellor` role now matches before `name`, so this sheet has no name role
  and person-grain metrics are correctly withheld.
- **A mid-sheet block is date-only** — one pasted batch drops the time
  component entirely. In the live sheet that block also flips separator and
  order (`DD-MM-YYYY` against the tab's usual `M/D/YYYY H:MM:SS`).
- **The follow-up date / outcome pair migrates across three columns** over
  time: early rows put them in `Any Other Notes` + `enq discuusin`, later rows
  in `Column 1` + the trailing unnamed column, later still in
  `enq discuusin` + `Column 1`. Any fixed column mapping loses data.
- `Any Other Notes` is misused for grade and locality: `AREA - PARLEPOINT`,
  `9TH GRADE`, `DPS SCHOOL`, `NO DATA ON THIS NUMBER`.
- Enquiry ids repeat (`8759`, `8776`, `8816`, `8837`, `8889`, `8890`, `8932`).
- `Course` sometimes holds a non-course: `NA`, `SENDED CATALOGUE`,
  `LINK TO EXPLORE COURSE CATALOGUE`.

---

## 4. The March-2026 course rename

The catalogue was rebranded around **15 Mar 2026**. Both naming systems live in
the same column, so any course-level trend crossing that date is comparing two
different vocabularies unless they are mapped to a shared `course_family`.

| Legacy name (2024 → Mar 2026) | Current name (Mar 2026 →) |
|---|---|
| Computer Basics | Computer Basics & Generative AI Foundation |
| Advance Excel / Advanced Excel | Advanced Excel & Power BI |
| Python Programming / Core Python | Python Foundation Program |
| — | Professional Python Developer |
| Data Analysis / Data Analytics | Advanced Data Analytics |
| Computer Accounting | Advanced Computer Accounting with GST & Zoho Books |
| — | Tally Prime with GST |
| Graphic Designing | Basic / Advanced Graphic Designing |
| — | Social Media Content & Ad Architect |
| — | Agentic AI & Automation Specialist |
| — | AI & Data Science Professional |

On top of the rename, the free-text field carries: spelling variants
(`Advance excel`, `Advance Excel 1&2`, `Advanced Excel (M-1 & M-2)`), typos
(`Web developmnet`, `Graohic`, `Python Programmig`), module suffixes
(`(M-1)`, `(module 2 & 3)`, `(with zero module)`, `(6 months)`), ALL-CAPS
blocks, trailing spaces, and multi-course cells (`Graphic Designing, Advance
Excel`). Roughly **400 distinct strings for ~40 real courses** — well past the
`MAX_CATEGORICAL_CARDINALITY = 30` gate, which is why course drops out of EDA
until it is canonicalized.

Education and lead-source vocabularies changed at the same time:
`12` / `12th` / `12th completed` → `12th Standard`; `Google` / `Social media`
→ `Online (Google / Social Media)`; `Hoarding` / `Walk in` →
`Hoarding / Banners / Walk ins`.

## 5. Faculty, branch, and status drift

**Faculty** — one person, many spellings. Strip honorifics before grouping:
`Mansi` / `Mansi Mam`; `Subin Sir` / `subin sir` / `SUBIN SIR`;
`Vansh Sir` / `vansh sir`; `Trusha` / `Trusha Mam`; `Siddharth Sir`.

⚠ **There are two different Yash.** Confirmed by the institute:

```
Yash Kanodia Sir · Yash k        -> yash kanodia   (one human)
Yash Sir         · Yash · yash   -> yash           (a different human)
```

So a bare first name must **never** be folded into a full name that starts with
it. Any "shorten to the first token" rule merges two staff members into one and
misattributes every student between them. Pinned by
`test_two_different_yash_never_merge`.

**Branch** — a **closed set of exactly three** sites, confirmed by the
institute: `Citylight`, `Vesu`, `Pal`. Appearing as `vesu` / `pal` /
`citylight` / `NA` / blank, plus spelling drift (`City Light`, `clt`,
`Palanpur Patiya`).

Because the set is closed, an unrecognized value is a data-entry error rather
than a fourth site — and a breakdown that accepts it invents a centre and
splits a real branch's numbers across two rows. Two error kinds, handled
differently:

| Cell says | Treatment | Why |
|---|---|---|
| `adajan` | rewritten to `pal`, original kept in `<col>_locality` | Adajan is an **area near the Pal branch**, confirmed by the institute — someone typed the neighbourhood instead of the site. Leaving it alone fragments Pal. |
| anything else unknown | kept verbatim, `is_known_branch = False` | No mapping is known, and guessing would move a student between centres on nothing but a name. |

`canonicalize_branch` reports what the cell literally said;
`branch_from_locality` is the separate, opt-in resolution step. Keeping them
apart is what makes the rewrite auditable and reversible. Pinned by
`test_branch_is_a_closed_set_of_three`,
`test_locality_in_a_branch_column_resolves_to_the_serving_branch`, and
`test_unknown_branches_are_flagged_not_dropped`.

New localities go in `LOCALITY_BRANCH` in `agents/canonical_maps.py` — one line
each, and only on the institute's word about which site serves them.

**Status lives inside the Name column** — never in a status field:
`(cancelled)`, `(not coming)`, `(Admission Cancelled)`,
`(admission cancelled all refunded)`, `(refunded trial amount, no admission)`,
plus operational notes that are *not* statuses: `(fast track)`,
`(register for trial 3 & 5 june)`, `(parent)`, `(indiamart)`.
Strip these **before** hashing the name, or the same person hashes differently
across rows.

## 5a. Reporting taxonomies — the operator's own groupings

Recorded from a corrections log (2026-08-14) and then **fixed as closed sets by
the operator**. All of them are **policy, not fact** — they encode which
distinctions the institute acts on — so the cleaner applies them as the default
and keeps the original value in `<col>_raw`. Reversible by construction.

Closed means a value outside the set is a bug in the rules, not a new level:

| Field | The set |
|---|---|
| `course_category_derived` | Foundational/School · Programming & Development · Office & Productivity · Digital Marketing · Data & Analytics · Accounting & Finance · Design & Creative · Other |
| lead source | Online/Google/Social · Referral · Walk-in |
| occupation | Student · Business / Job · Housewife · Other |
| branch | Vesu · Pal · Citylight (§5) |

These are also what the **analysis** breaks down by: a brief or a crossing that
asks for the `course` role resolves to `course_category_derived`
(`canonical_maps.REPORTING_ROLE_PREFERENCE`). Eight levels are a report; ~40
canonical families against a 12-level cap are a truncated grid. The fine column
stays reachable by its literal header.

**Course → category.** Eight buckets. The rules the keyword matcher had missed:

| Rule | Was |
|---|---|
| Video/motion work (`Video Editing`, `After Effects`, `Adobe Illustrator`) → Design & Creative | Other |
| Theory courses (`Psudo Code`, `Data structure & algorithm`, `Web Development`) → Programming & Development | Other |
| Every spelling of the digital-designing certificate → Digital Marketing | Other, or wrongly Design & Creative |
| `Business Analystics` (misspelled at source) → Data & Analytics | Other — the typo broke the match |
| **A bundle anchored on Computer Basics → Office & Productivity**, whatever its second subject | classified by the second subject |
| `Agentic AI & Automation Specialist` → Programming & Development | AI & Emerging Tech, now retired |

Foundational/School is now real school tutoring only (12th Computer Science,
Kids Course, School Course) — every Computer Basics variant moved out of it.
The **anchor** rules read the raw course string, because the family rules
resolve a bundle to whichever subject their ordering reaches first. Two anchors,
tested in order: `kids` → Foundational/School, then `computer basic` /
`professional office` → Office & Productivity.

Two calls that follow the operator's own filing rather than the subject:
**Power BI → Office & Productivity** (every spelling, in their audit) and
**SQL → Programming & Development** (3 of its 4 rows). Accounting & Finance was
split out of Office & Productivity — Tally/GST is a different buyer from Word
and Excel — and financial modelling goes with it, by subject rather than tool.

**Lead source → three buckets.** The largest correction in the log, 423 rows.
Old Student, Friends, Family and Relatives all collapse into one **Referral**:
who referred them is not a decision the institute takes, referral-or-not is.

There is **no Print/Outdoor bucket**. Hoardings, banners, newspaper, pamphlets
and radio are local-awareness spend that puts someone at the counter, which is
the same reasoning the log already applied to hoardings; all read **Walk-in**.

Two paths reach a bucket without the cell saying so, and both are counted in
the quality report (`lead_source_basis` returns which):

- a **blank** source defaults to Walk-in, since nobody fills the field for
  someone standing at the counter;
- free text matching no channel reads **Referral**, because what sits in that
  field is the name of whoever referred them — a person, the school they came
  from, a community trust. The count is reported and the values are **not**
  quoted: they are real people's names, and a quality note travels into
  reports.

**Occupation → four buckets.** Business and Job merge into one working-adult
bucket, absorbing the free-text trades that used to fall through (Teacher,
Lawyer, Event planner, Diamonds). `School` is a **Student**. A bare `Freelance`
stays Other — no field is stated — while `Freelancing as graphic designer` is a
working adult. Retired and unemployed had buckets of their own and no longer do:
neither is a segment the institute markets to differently, and both are tiny.

⚠ **Two entries in the log are explicitly not rules** and are not implemented:
two `Housewife` rows the operator recoded to Other. No textual pattern
separates them from the 52 that stayed, so the log attributes them to outside
knowledge of those students. Encoding them would mean hard-coding two people's
names into the cleaner.

`Other` is a **named member** of the course and occupation sets, so an
unclassifiable course lands there rather than blank — the operator wants one
bounded column to group by. Its size is quoted in the quality report every time
it is non-zero, so a growing Other reads as a gap in these rules rather than as
a segment worth acting on.

Blank still means blank: no course recorded at all is not the same statement as
a course we could not classify, and the two are kept apart. The one lead source
left blank is a literal `Other` answer — the person said none of these, which is
evidence of no channel, and the closed set has nowhere to put it.

## 6. Field-level damage

**Phones** — 10-digit is the norm, but the sheets contain 9-digit (dropped
character), 11-digit (fat finger), leading-zero (`08880054334`), `+91`, `+1`,
`+81` international, two numbers in one cell (`7770019667, 7770062788` and
`9990065869---9990015508`), and blanks. A `\b\d{10}\b` mask misses most of
these.

**Dates** — two separate problems. Keep them apart:

*Order.* In the live exports the admission sheet is day-first (`DD/MM/YYYY`)
and **both enquiry sheets are month-first (`M/D/YYYY`) — 100% of their rows**.
Parsing all three with a single day-first format does not fail loudly: every
date whose day component is ≤ 12 is a valid date under both readings, so it is
silently swapped (`4/9/2026` becomes 4 Sept instead of 9 April). Roughly 40% of
rows land in that ambiguous window; the other ~60% raise a parse error and get
caught. **The silent 40% is the dangerous part** — it shifts enquiry dates by
up to eleven months and corrupts every funnel lag and month-over-month trend.
Fix at the source (set the Google Sheet column type) or vote the format
per-column before parsing; never assume one format for the workbook.

*Value damage* (independent of order, present in every sheet): impossible years
(`24/01/0026`, `23/04/0026`), decade-old typos (`01/08/1998` on a 2026
admission), future DOBs (`04/11/2026`), DOB equal to the admission date,
separator drift (`09-04-2024` inside an otherwise slash-formatted column), and
non-date text (`hand written`, `Given`, `-`, `NA`).

In the generated samples, date **order is day-first in 100% of rows**; the
value-damage classes above are retained, including ~1% separator drift.

**Emails** — typo TLD (`.con`), missing dot (`gmail com`), stray comma
(`gmail,.com`), no domain at all (`Mehtakrishang230908`), ALL CAPS, and
mojibake from smart quotes (`itâ€™s_jaya@example.in`).

**Receipt IDs** — not a key. Blank, zero-padded (`002`, `0000`), ranges
(`741 & 742`, `276,277`), suffixed (`455-1`), free text (`no number`), and
sequences that restart per branch so the same id repeats.

## 7. How the sheets join — `student-data` is the hub

There is no single shared id across the whole estate, but there is a clean
two-hop path, and **`student-data` sits in the middle of it**. Everything
downstream of the admission form reaches everything else through that one tab:

```
  Enquiry Form (Responses) 1  ─┐
  Enquiry Form (Responses) 2  ─┘  normalized phone (fuzzy, no id exists)
                                        │
                                        ▼
  Admission Form (Responses) - Form responses 1
                                        │
                                        │  Timestamp
                                        │  (the form's timestamp is copied
                                        │   into student-data)
                                        ▼
  ┌──────────── student-data-sheet-from-1-4-22 ─────────────┐
  │                                                          │
  │   student-data   ◄── HUB. Holds BOTH Timestamp and       │
  │        │              student-id, so it is the only      │
  │        │              tab that bridges the two keys.     │
  │        │  student-id                                     │
  │        ├──────────►  certificate-data                    │
  │        ├──────────►  fees-data                           │
  │        └──────────►  fees-recpit                         │
  └──────────────────────────────────────────────────────────┘

  Student_Time_Table2023 tabs  ── key UNCONFIRMED (see below)
```

| Key | Links | Reliability |
|---|---|---|
| **Timestamp** | Admission Form → `student-data` | Exact, by construction — the value is carried over from the form |
| **student-id** | `student-data` ↔ `certificate-data` ↔ `fees-data` ↔ `fees-recpit` | Exact, one-to-many. All four are tabs of one workbook |
| **phone** | Enquiry 1/2 → Admission Form | Fuzzy. No id exists; normalize to last 10 digits |
| Receipt ID | Admission Form ↔ `fees-recpit` | Works, but incidental — verified on real rows (`591`, `592`, `1`, `2`, `3`). Ids repeat across branches/years, so qualify with branch + date. Prefer the Timestamp → student-id path |
| ~~name (sorted)~~ | `student-data` → `fees-data.Status` | **Broken.** The office's current manual method; see §9. Do not reproduce it |

**Practical consequence: never join the admission form straight to the fee
sheets.** Route Admission →(Timestamp)→ `student-data` →(student-id)→ fees /
certificates. `student-data` is also the only tab carrying phone and DOB, which
is what makes person resolution possible at all (see §9).

The four `Student_Time_Table2023` tabs (`Main_data`, `Course_Completed`,
`Not_Coming`, `NOT TO ENTERTRAIN`) are splits of one list — same name, same
data, a student simply moves between tabs as their status changes. So they are
internally consistent and a student appears in exactly one of them.

> **Unconfirmed:** which key ties the timetable workbook back to `student-data`
> — `Timestamp`, `student-id`, or name+course. The generated timetable samples
> carry **both** id columns so either can be tested; headers will be corrected
> once the real tabs are supplied.

### 7a. The one weak link — enquiry → admission

Every hop after the admission form is exact. **Phone is the only fuzzy join in
the estate**, and it is the join the entire lead-conversion funnel rests on.
Measured on the generated samples, which carry the real sheets' phone damage
(15% of cells are not a clean 10 digits):

| Matching strategy | Enquiry 1 | Enquiry 2 |
|---|---|---|
| Exact string, student-phone column only | 71.5% | 69.6% |
| + last-10-digit normalize, multi-number cells, **guardian columns indexed** | 77.6% | 77.6% |
| + 9-digit salvage (try each missing digit) | 78.2% | 80.8% |

Normalization buys 7–11 points. Indexing the guardian phone columns is the
single biggest win — a student often enquires from a parent's phone and
enrolls on their own, or the reverse.

**The trap this creates.** In these samples every enquirer *is* also an
admission, so the ~20% that still fail to match are pure phone damage. In the
real sheets a non-match means **either** damaged phone data **or** a lead that
genuinely never converted — and nothing in the data distinguishes them. So the
measured `enquiry_to_admission_conversion` is a *lower bound*: understated by
however much of that 20% were real conversions. Report it as a bound, never as
a point estimate, and publish the match rate alongside it.

**Fixes, in order of payoff:**

1. *Upstream, cheap, permanent:* carry an `enquiry_id` onto the admission form.
   One column turns the estate's only fuzzy join into an exact one and makes
   conversion exactly measurable.
2. Index all three phone columns (student, guardian 1, guardian 2) on both
   sides, not just student→student.
3. Normalize to last 10 digits and split multi-number cells before matching.
4. Confirm borderline matches with name + date, per §9 — never phone alone,
   since a guardian's number is shared between siblings.

Consequences the pipeline has to respect:

- **`student-id` is an enrollment key, not a person key.** One person holds
  several ids (re-enrollment into a new course). Person-grain metrics need
  `person_id = hash(normalized_name + last-10-digits of best phone)`.
- **The fee ledger starts Apr 2022; the admission form only Apr 2024.** Around
  45% of enrollments have no form row at all — no phone, so they cannot be
  linked to any enquiry. Any funnel metric computed over the whole history
  silently under-counts; scope it to Apr 2024+.
- A guardian's phone is shared between siblings, so phone alone over-merges;
  the name component of the hash is what separates them.
- In the synthetic sample, 111 of 154 people re-enroll, up to 5 times.

## 8. fees-recpit — the receipt ledger (17 columns)

A copy of every receipt written. **One row per payment**, several rows per
enrollment. Not a per-student table.

| # | Column | Notes |
|---|---|---|
| 1 | `Name` | casing drifts between rows for the same `student-id` |
| 2 | `student-id` | enrollment key → `fees-data` |
| 3 | `course category` | 13 values; casing inconsistent (`school course`, `advanced certificate course` lowercase) |
| 4 | `Course` | same free-text chaos as the forms |
| 5 | `Total Fees` | repeated on every installment row — do **not** sum it |
| 6 | `paid amt` | the actual payment; this is what you sum |
| 7 | `Branch` | `citylight` lowercase in places |
| 8 | `date of receipt (mm/dd/yy)` | header says `mm/dd/yy`, values are mostly `M/D/YYYY` with ~8% 2-digit years |
| 9 | `Receipt-id` | per-branch book, restarts → heavy collisions |
| 10 | `Mode of Payment` | `Cash` / `Online` / `Cheque` |
| 11 | `Description` | the payment **channel**, not a note |
| 12 | *(unnamed)* | the ops **note** lands here, not in `Description` |
| 13–17 | *(unnamed)* | empty; col 17 occasionally holds a stray `#N/A` |

Traps:

- **Columns 11 and 12 are two different fields and only one is named.** Channel
  (`paid to ICICI`, `UPI -HDFC`, `Razorpay emi`, `Gpay`, cheque numbers) goes in
  `Description`; accounting notes (`COMPLETLY SHIFTED IN ICICI ZOHO`,
  `2400 refunded`, `admission at citylight`) go in the unnamed column after it.
  A reader that stops at the named headers loses every ops note.
- **The channel vocabulary drifts** as the institute switches acquiring bank:
  `Gpay`/`neft` (2022) → `paid to shaurya creation`/`paid to sc` (2023) →
  `paid to ICICI`/`paid to HDFC` (2024) → `UPI- ICICI`/`UPI -HDFC` (2025) →
  `ICICI - UPI`/`razorpay emi/icici` (2026). Same channel, five spellings.
- **One receipt is often two rows** — same `Receipt-id`, same date, split across
  Cash and Online. Counting receipts by row over-counts.
- **Booking tokens**: a ₹100 or ₹500 row followed by the balance days later.
- `Mode of Payment` and `Description` sometimes contradict each other
  (`Cash` + `UPI -HDFC`).
- Zero-value rows exist for cancelled admissions rather than being deleted.

## 9. fees-data — the per-enrollment rollup (10 columns)

One row per `student-id`, carrying the outstanding balance.

`student-id, Name, course category, Course, Total Fees, Status, Amt Pending,
Branch, Description, Date of Joining (MM/DD/YY)`

### Where `Status` actually comes from

`Status` is **not** computed from the receipt ledger. It is maintained by hand:
values are taken from the `student-data` tab of `student-data-sheet-from-1-4-22`
and matched across to the admission sheet **by student name, with both lists
sorted in descending name order**.

The descending sort is deliberate: because one student takes admission several
times, sorting by name groups a person's admissions together and puts the
**latest** one on top — and that latest admission is the one whose fee matters.
The intent is correct; matching on sorted *names* is what makes it fragile.

Three failure modes, each with the institute's own resolution rule:

1. **`student-id` is an ADMISSION id, not a person id.** A student who
   re-enrols is issued a new one: `Khiren Jain` holds 3, 244, 609, 1070;
   `Harvish Nayak` holds 94, 105, 303, 585, 719. Measured on the synthetic
   sample (same repeat ratio as the real sheet), **90% of rows carry a name
   shared with at least one other id**, worst case one name across 7
   admissions. Read the column as `admission_id` everywhere and derive a
   separate `person_id`.
   → **Rule: a person's current fee position is their LATEST admission only**
   (`Khiren Jain` → id 1070). Earlier ids are closed history, not outstanding
   balance. Summing every admission overstates both revenue and receivable.
2. **Sorted-order alignment shifts.** Position-matching two descending name
   lists only holds while both lists are identical. One missing or extra
   student and every row below the gap takes its neighbour's Status — a gap a
   quarter of the way down mis-assigns **75%** of the sheet, near the top ~90%.
   → **Rule: join on `student-id`, which both sheets already carry.** That
   keeps the intent (latest admission wins, via `max`) without depending on two
   lists staying the same length.
3. **Name reliability depends on which workbook you are in.**
   `student-data`, `fees-data` and `fees-recpit` are tabs of the *same*
   workbook and are linked to `student-data`, so a given `student-id` carries
   the same name in all three. Names entered independently elsewhere — the
   admission form, the certificate register — are typed by a different person
   and do drift.
   → **Rule: within `student-data-sheet-from-1-4-22`, name is a valid
   cross-check.** Across workbooks, it is not: confirm identity with the other
   columns and the date of admission instead. The name string never decides on
   its own.

   The in-workbook guarantee holds on content but not on exact bytes — the
   same id appears with different casing (`Rishika Lalani` / `Rishika lalani`
   at id 640, `Hitanshi Jain` / `Hitanshi jain` at id 285), and some cells
   carry a trailing space or an appended ` (cancelled)` marker. Compare
   case-folded and marker-stripped; an exact string compare silently drops
   those rows. A *real* name difference between these three tabs is a
   data-entry error worth surfacing, not expected variation.

**Consequence: `Status` and `Amt Pending` are derived opinion, not measurement.
Recompute both by summing the ledger per `student-id`.** In the synthetic
sample 6 rows say `Full Paid` while ₹62,500 is outstanding.

### Person resolution — the agreed algorithm

```
admission_id  := the sheet's `student-id` column         (one per admission)
person_id     := resolve(name, phone, DOB, date of admission, branch)
current(person) := latest admission — max Date of Joining,
                   tie-break max admission_id
```

`resolve` blocks on the normalized name (case-folded, status markers stripped,
surname-order-insensitive) and then confirms with **any** of: matching phone,
matching DOB, or matching date of admission plus branch.

Name trust by source:

| Source | Name reliable? | Join on |
|---|---|---|
| `student-data` ↔ `fees-data` ↔ `fees-recpit` | Yes — one workbook, linked to `student-data` | `student-id`; name as a cross-check |
| Admission form | No — separate form, different operator | Receipt ID, or Timestamp |
| Certificate register | **No, by design** — carries the government-proof name | `student-id` only |
| Timetable tabs | Unverified | `student-id` / Timestamp |

Two constraints this puts on the pipeline:

- **The fee sheets cannot resolve persons on their own.** Neither `fees-data`
  nor `fees-recpit` carries a phone or a DOB — only a name. Person-grain
  metrics must first join fees → admission / `student-data` on `student-id` to
  pick up the identifying fields, then resolve. Building `person_id` from the
  fee sheets alone silently merges unrelated namesakes.

  *Implemented.* The Data Engineer now measures what identity it can build and
  records it as `quality_report.person_id_basis`. A discriminator (phone, DOB,
  email) is used only when populated on ≥ 80% of rows — a sparse one splits one
  person in two instead of separating two people. When nothing but the name is
  available, `person_id` is still emitted (it remains the correct join key to a
  richer source) but `is_repeat_enrollment` is **withheld**, and the Analyst
  blocks the metric saying which sheet to supply instead. Measured on the
  sample: name-only merged 219 people into 166 and inflated repeat rows by 20%.
- **Certificate names are legitimately different.** Where a student filled the
  admission form in wrong, the certificate carries the corrected name from
  their government proof. A certificate row therefore may not match its
  admission row by name *by design*. **Never match certificates by name — use
  `student-id`.** A name mismatch there is a correction to record, not an
  error to flag.

Other traps:

- **`Status` has only two values** — `Full Paid` / `Pending`. There is no state
  for "cancelled", "refunded", or "not yet billed"; those all get flattened into
  one of the two.
- **`Description` is a flag field in disguise.** The literal string `Default`
  marks a defaulter, mixed in with free-text notes (`2400 refunded`,
  `admission cancelled only 7000 fees received`). It is also applied
  inconsistently — some `Pending` rows carry it, some don't.
- **`Amt Pending` can be negative** (overpayment or a double-entered receipt).
- Some enrollments have **no ledger rows at all** — billed but never paid.
- `student-id` has gaps where rows were deleted.
- Joining dates predate the sheet's own stated start (`from 1-4-22`).

## 10. Lifecycle sheets — semantics (pending receipt of the files)

Six further sheets are described by the institute as follows. Recording the
semantics now because two of them are easy to conflate, and getting it wrong
corrupts the churn label:

| Sheet | Meaning | Label |
|---|---|---|
| Certificate | Issue date present ⇒ issued. **Blank issue date ⇒ still pending.** | `certificate_issued` |
| Admission data (duplicate) | Backup copy of the admission sheet | reconcile, don't double-count |
| `Student_Time_Table2023 - Main_data` | Actively attending. **A student who goes on leave is removed from this sheet**, because they may resume. | active, **censored** |
| `Student_Time_Table2023 - Course_Completed` | Finished the course | `completed` — terminal |
| `Student_Time_Table2023 - NOT TO ENTERTRAIN` | Barred from resuming | **churn = true** — terminal |
| `Student_Time_Table2023 - Not_Coming` | On leave. May resume — **but only within 6 months.** | **conditional**, see below |

### The four tabs are ONE roster, and must be unioned

They are the same entity split by membership, not four related tables. The
pipeline used to left-join them onto a master, which keeps only the master
tab's rows: supplying all four produced **16 rows out of 406**, every one
`active`, deleting every completion and every churn.

Two things had to change to make the union work:

- **Union, don't join.** `_union_lifecycle_partitions` stacks any source whose
  sheet name resolved a single `completion_status`. A student who appears in
  two tabs with *contradicting* labels is kept at the most advanced one
  (`completed > not_to_entertain > not_coming > active`) and flagged in
  `lifecycle_conflict` — someone copied the row instead of moving it. Same-label
  repeats are **not** collapsed: students really do re-enroll.
- **Membership rows have no date and must survive anyway.** `Timestamp` is only
  filled for rows created through the form; 158 of 277 real completions are
  blank. The drop-invalid-rows rule deleted 57% of the completions and then
  tripped the drop-fraction guard, blocking the sheet outright. On a membership
  tab the fact is the row's presence, so the date rule is skipped there.

### Attributes drift, and only Main_data is maintained

Confirmed by the institute: **batch timing, faculty and even branch change
during a course**, the cell is overwritten in place, and there is no history
column. The edits land almost entirely on `Main_data` — once a student is moved
to `Course_Completed` or `Not_Coming`, the row stops being maintained and
freezes at whatever was true on the way out.

Three consequences, all handled:

| Consequence | Handling |
|---|---|
| A mutable attribute in a join key fails exactly on the students who moved | `branch` removed from the admission-identity composite; `admission_date` / `joining_date` tried first, branch only as a last resort with a recorded caveat |
| One frame carries two different as-of dates | `attribute_currency` = `current` (Main_data) / `at_exit` (archive tabs) |
| "Revenue by branch" credits the branch the student sits in *now* | stated as a known issue on every source carrying one of `MUTABLE_ATTRIBUTE_ROLES` |

### A course upgrade is recorded on one sheet, not both

Also confirmed: a student upgrades their course and the change is entered on
one sheet only. That one enrollment then wears two course names, and a
person+course join misses it.

`_rekey_course_upgrades` repairs **only the unambiguous case** — a person with
exactly one unmatched row on each side, where there is nothing else the row
could refer to. Where a person has several unmatched rows the pairing is a
guess (two real enrollments look identical to one renamed enrollment) and they
stay unmatched. On the samples this recovers 16 rows.

What it deliberately does **not** do is carry a label across courses in
general. A student can be `completed` in Excel and `not_coming` in Python on
the same day; sharing the label would invent churn.

### The churn label is time-dependent, not sheet-membership

The label depends on *when you ask*, and the clock starts at the date the
course was **due to finish** — not the admission date, and not the last date
attended:

```
course_end := Date of Joining + Course Duration (IN DAYS)

Not_Coming        AND  as_of <  course_end + 6 months  ->  paused  (censored)
Not_Coming        AND  as_of >= course_end + 6 months  ->  churn = true
NOT TO ENTERTRAIN AND  as_of <  course_end + 6 months  ->  paused  (censored)
NOT TO ENTERTRAIN AND  as_of >= course_end + 6 months  ->  churn = true
Main_data                                              ->  active  (censored)
Course_Completed                                       ->  completed
```

**Corrected by the institute (Aug 2026).** An earlier version of this document
ran the six months from the admission date. That is wrong for any course longer
than six months: a student on a 12-month programme who stopped attending in
month five would be marked churned at month six, while their course still had
half a year to run. Counting from the expected end makes the test what it was
always meant to be — *the course window closed and they never came back*.

Two consequences of the correction:

- **`Course Duration (IN DAYS)` becomes load-bearing**, and it lives on the
  **`Student_Time_Table2023` MAIN sheet only** (confirmed by the institute).
  That placement is backwards for this rule: the rows that need a course end
  are the ones that stopped attending, and those sit in `Not_Coming` and
  `NOT TO ENTERTRAIN`, where the column does not exist.

  So the duration is resolved in three steps, best evidence first, and which
  one was used is recorded per row in `churn_basis`:

  | Basis | Source |
  |---|---|
  | `duration_column` | the row's own recorded length |
  | `course_median` | the median length recorded for that **same course** on the main sheet — the institute's own number, measured, not assumed |
  | `category_default:<role>` | an operator-supplied months-per-course table, only if one is passed |

  A row with none of the three is `unlabelled` — never defaulted back to the
  admission-date rule. On the samples this resolves 145 rows by course median
  against 10 that carry the column directly.
- **`NOT TO ENTERTRAIN` is now conditional too.** ⚠ This differs from the
  earlier instruction that the tab is unconditionally churn because those
  students may not resume. Both readings are defensible — "not allowed back"
  is a decision that has already been taken, regardless of the calendar — so
  the rule above follows the most recent instruction and the switch is one
  line. Worth confirming which is intended before the first churn report goes
  out.

Still no last-attended column is required: the rule is computable from
`Date of Joining` and `Course Duration (IN DAYS)`, both of which already exist.

Three consequences the pipeline must respect:

1. **Never derive churn from sheet membership alone.** A row sitting in
   `Not_Coming` flips from paused to churned with no edit to any sheet — only
   the calendar moves. The label must be recomputed at analysis time against an
   explicit as-of date, and that date must be recorded in the run.
2. **Two censoring classes, not one.** `Main_data` (attending) and recent
   `Not_Coming` (paused) both have unknown final outcomes and must be excluded
   from churn training. Only `Course_Completed`, `NOT TO ENTERTRAIN`, and aged
   `Not_Coming` are terminal.
3. **Backtesting needs the as-of date frozen.** Computing features as of
   enrollment but labels as of today leaks the future into training. Pin one
   as-of date per run and apply the 6-month rule from it.

> Supersedes `docs/replan_real_data.md`, which treats `Not_Coming` as a single
> unconditional churn class. Under the rule above that both inflates the
> positive class (paused students counted as lost) and hides real churn
> (the ≥6-month group that `NOT TO ENTERTRAIN` does not contain).

### Code changes this requires

Both landed, in `agents/data_engineer_agent.py` and the new
[`agents/lifecycle.py`](../agents/lifecycle.py):

1. **`COMPLETION_BY_SOURCE`** had no entry for the `NOT TO ENTERTRAIN` tab, so
   the institute's most certain churn cases carried **no lifecycle label at
   all** — absent from `not_coming_rate` and from the model's training set. An
   `entertain` needle now resolves them to `not_to_entertain`. The tab needles
   must stay ahead of the generic `timetable` one, or every tab reads `active`.
2. **The label is computed, not stored.** `lifecycle.churn_labels` resolves
   membership against `course_end + 6 months` at an explicit `as_of` (defaulting
   to the latest date in the data, never `today()` — these are historical
   exports). It emits `churn_status`, `is_churn`, `course_end_date`,
   `churn_basis`, `days_past_course_end` and `tenure_days`, and reports the
   as-of date with the answer. It runs once on the **merged** frame: the label
   is on the timetable sheet and the course start and length are on the student
   sheet, so per-source it would find one half of the rule.

Two states are kept apart that are easy to merge and shouldn't be:
`unlabelled` (in a churn-risk tab but no computable course end — excluded from
both sides of the rate) and `no_membership` (in no tab at all — a coverage gap
in the timetable workbook, not a missing duration).

Downstream, `not_coming_rate` ([analyst_agent.py:75](../agents/analyst_agent.py#L75))
and its >20% / >30% monitoring thresholds
([monitoring_agent.py:98](../agents/monitoring_agent.py#L98)) still read the raw
`is_not_coming` flag rather than `is_churn`, so they measure "stopped
attending", not "churned". Repointing them is the remaining step.

## 11. Synthetic sample guarantees

All five files under `samples/` are generated from one enrollment spine, so
they join to each other exactly as the real sheets do:

| File | Rows | Covers |
|---|---|---|
| `admission_form_responses.csv` | 222 | Admission Form responses |
| `enquiry_form_responses_1.csv` | 170 | Enquiry sheet 1 (named) |
| `enquiry_form_responses_2.csv` | 130 | Enquiry sheet 2 (call log) |
| `fees_receipts.csv` | 746 | fees-recpit ledger |
| `fees_data.csv` | 420 | fees-data rollup |
| `student_timetable_main_data.csv` | 28 | active roster |
| `student_timetable_course_completed.csv` | 268 | completed |
| `student_timetable_not_coming.csv` | 88 | paused / aged-out |
| `student_timetable_not_to_entertain.csv` | 23 | barred |
| `certificate_register.csv` | 267 | certificates |

Verified on the generated output:

- Receipt ID joins admission → ledger at **100%** coverage; every ledger
  `student-id` exists in `fees-data`; all 222 admission Timestamps distinct.
- Every timetable and certificate `student-id` resolves into `fees-data`
  (100%), and every timetable `Timestamp` that is present resolves into the
  admission sheet. Rows with a blank Timestamp are the pre-Apr-2024
  enrollments that predate the form — correct, not a gap.
- **No student appears in more than one lifecycle tab.**
- The `Not_Coming` tab deliberately straddles the cutoff — 79 rows are ≥6
  months past admission (churn) and 9 are inside it (paused) — so a test can
  prove the 6-month rule is being applied rather than the tab name being read.
- Certificates: 180 issued, **82 pending (blank issue date)**, 5 carrying
  non-date text, 5 duplicate certificate numbers, 3 multi-certificate cells,
  and 15 rows where the certificate name differs from the admission name
  (government-proof correction).

They are safe to commit and share:

- **Names** — common given names paired with a surname pool that does not occur
  in the real sheets, so no generated full name can collide with a real student.
  Verified: zero overlap.
- **Phones** — format-valid 10-digit Indian mobiles confined to obviously
  fabricated `99900` / `88800` / `77700` / `66600` blocks.
- **Emails** — on `example.com` / `example.in` (RFC 2606 reserved, non-routable).
- **Addresses** — invented building names (`Testvan Residency`, `Proxy
  Paradise`). Locality names (Vesu, Pal, Althan, …) are kept because the cleaner
  canonicalizes them.
- **Faculty names are real and kept deliberately** — they are the institute's
  own instructors, not students, and `agents/canonical_maps.py` is keyed to
  those exact spellings. Removing them would make the samples useless for
  testing honorific canonicalization.
