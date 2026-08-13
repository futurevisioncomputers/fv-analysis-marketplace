#!/usr/bin/env python
"""Generate synthetic look-alikes of the five institute sheets.

The real sheets (Admission Form responses, Enquiry Form responses 1 & 2,
fees-recpit, fees-data) carry live PII, so they can never live in the repo.
This script reproduces their *shape* — exact headers (including the duplicate
`Email Address`, the trailing space in `Preferred Branch `, the literal
`Column 2` / `Column 1` / `enq discuusin` names, and the six unnamed trailing
columns on the fee ledger), the era-split course catalogue, and every
dirty-data pattern the cleaner has to survive — using fabricated names, phones,
emails, and addresses.

Everything hangs off a single **enrollment spine**, so the five files describe
the same synthetic humans and are joinable exactly as the real ones are:

    Enquiry 1/2 ──phone──► Admission Form ──Receipt ID──► fees-recpit
                                                   │
                           fees-data ◄──student-id─┘

Two structural facts the spine reproduces:
  - The fee ledger starts Apr 2022 but the admission form only Apr 2024, so
    ~45% of enrollments exist ONLY in the fee sheets — no form row, no phone,
    therefore not phone-linkable to any enquiry.
  - One person holds several enrollments (re-enrollment into a new course),
    each with its own student-id. `student-id` is an ENROLLMENT key, never a
    person key.

Deterministic: a fixed seed means regenerating produces byte-identical files,
so tests can pin exact values.

Run:
    python scripts/make_form_samples.py            # writes into samples/
    python scripts/make_form_samples.py --out DIR  # writes elsewhere
    python scripts/make_form_samples.py --dates mixed   # live date mismatch

Chaos reproduced (see docs/form_schema_notes.md for the full catalogue):
  - impossible years (0026), decade-old typos, future DOBs, 2-digit years, and
    non-date text (`hand written`, `Given`) in date columns. Date ORDER is
    day-first everywhere by default; `--dates mixed` restores the live sheets'
    month-first enquiry and ledger timestamps.
  - phone lengths 9/10/11/13, `+91`/`+1`/`+81` prefixes, two numbers per cell
  - course free-text: legacy names, the Mar-2026 GenAI rename, typos, module
    suffixes, multi-course cells
  - faculty honorific drift (Yash / Yash Sir / Yash Kanodia Sir)
  - branch case drift + `NA` + blanks
  - status and trial notes buried inside the Name column
  - anonymized `ENQ-####` rows carrying only a phone
  - receipt ids that collide across branches, are ranges, zero-padded, or text
  - the enquiry-2 follow-up date/outcome pair migrating across three columns
  - fee ledger: booking-token payments (100/500), one receipt split across two
    payment modes, `Full Paid` rows that do not reconcile, negative pending,
    `Default` used as a flag inside a free-text Description, ops notes landing
    in an UNNAMED column, and a stray `#N/A` in the last column
"""

from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import os
import random

SEED = 20260812

# --------------------------------------------------------------- exact headers

ADMISSION_HEADER = [
    "Timestamp", "Name for Google Contacts", "Which Course do you want to Learn ?",
    "Student Name", "Mobile No (Student)", "Mobile No (Father / Guardian)",
    "Student Address", "Education Level", "From Where Do You Know About Us ?",
    "Presently What Are you Doing ?", "Preferred Branch ", "Preferred Days",
    "Preferred Batch Time", "Faculty", "Receipt ID", "Email Address",
    "Date of Birth", "Date of Admission", "Mobile No (Mother / Guardian)",
    "Any Other Notes", "Education Details", "Student Photo",
    "Admission Form Photo", "Residential Area", "Pincode", "Coupons Given?",
]

# Note: "Email Address" appears TWICE (cols 2 and 16) in the real export; col 2
# is always empty. The unnamed "" column sits between Counsellor and Mode.
ENQUIRY1_HEADER = [
    "Timestamp", "Email Address", "Which Course do you want to Learn ?", "Name",
    "Mobile No (Student)", "Address", "Education",
    "Presently What Are you Doing ? (Student, Business, Job, Housewife, etc...)",
    "From Where Do You Know About Us ? (Google, Social Media, Friends, Old students, etc...)",
    "Preferred Batch Time", "Mobile No (Parent / guardian 1)", "Preferred Branch ",
    "Preferred Days", "Your Photograph", "Faculty", "Email Address",
    "Mobile No (Parent / guardian 2)", "Any Other Notes", "Counsellor Name ", "",
    "Mode of Enquiry", "Date of Enquiry", "Date of Birth", "Receipt ID",
    "Process Status",
]

ENQUIRY2_HEADER = [
    "Timestamp", "Date of Enquiry", "Column 2", "Which Course do you want to Learn ?",
    "Mobile No (Student)", "Mobile No (Parent / guardian 1)",
    "Mobile No (Parent / guardian 2)", "Email Address", "Address", "Education",
    "Presently What Are you Doing ? (Student, Business, Job, Housewife, etc...)",
    "From Where Do You Know About Us ? (Google, Social Media, Friends, Old students, etc...)",
    "Preferred Branch ", "Counsellor Name ", "Mode of Enquiry", "Any Other Notes",
    "enq discuusin", "Column 1", "",
]

# The ledger has 11 named columns then SIX unnamed ones. Column 12 (the first
# unnamed) is not junk — ops notes get typed there while column 11
# (`Description`) holds the payment channel. Column 17 occasionally holds a
# spreadsheet error value.
FEES_RECEIPT_HEADER = [
    "Name", "student-id", "course category", "Course", "Total Fees", "paid amt",
    "Branch", "date of receipt (mm/dd/yy)", "Receipt-id", "Mode of Payment",
    "Description", "", "", "", "", "", "",
]

FEES_DATA_HEADER = [
    "student-id", "Name", "course category", "Course", "Total Fees", "Status",
    "Amt Pending", "Branch", "Description", "Date of Joining (MM/DD/YY)",
]

# The timetable workbook splits students across tabs BY LIFECYCLE — the tab a
# row sits in is the label. It links back on TWO keys, `Timestamp` and the
# student's mobile number, which is worth exploiting: they are independent, so
# a row where they disagree is a data-quality finding rather than a silent
# mismatch. It carries no `student-id`. Not_Coming adds a free-text
# `Status & reason`. The blank interleaved column and the `zzzzz` dropdown
# placeholder rows are structural junk the real sheets carry.
# `Course Duration (IN DAYS)` is on the MAIN sheet only, confirmed by the
# institute. That placement is the whole difficulty of the churn rule: the rows
# that need a course end date — the ones that stopped attending, in Not_Coming
# and NOT TO ENTERTRAIN — are precisely the rows that do not carry it. The
# lifecycle module fills them from the median duration recorded for the same
# course on the main sheet, so the number is still the institute's own.
TIMETABLE_HEADER = [
    "Timestamp", "Mobile No (Student)", "Student Name", "Which Course",
    "Faculty", "Batch Timing", "Course Duration (IN DAYS)", "",
]
COMPLETED_HEADER = [c for c in TIMETABLE_HEADER if c != "Course Duration (IN DAYS)"]
NOT_COMING_HEADER = COMPLETED_HEADER[:-1] + ["Status & reason", ""]

CERTIFICATE_HEADER = [
    "Student-ID", "Student Name", "Certificate Number",
    "Certificate Issue Date", "Date of Joining", "Which Course", "Remark",
]

# The hub tab. It is the ONLY sheet holding both `Timestamp` (the key back to
# the admission form) and `student-id` (the key out to certificate-data /
# fees-data / fees-recpit), and the only one in that workbook carrying phone
# and DOB — which is what makes person resolution possible. The real export
# carries a stray row-count token in the header and trailing empty columns.
STUDENT_DATA_HEADER = [
    "Timestamp", "student-id", "Student Name", "Mobile No (Student)",
    "Secondary Contact", "Email Address", "Date of Birth",
    "Date of Joining", "Which Course", "course category", "Branch", "Faculty",
    "Mode", "From Where Do You Know About Us ?", "1508", "", "",
]

# ------------------------------------------------------------------- vocabulary

FIRST_NAMES = [
    "Aarav", "Aditi", "Advait", "Ananya", "Anish", "Arnav", "Avni", "Bhavya",
    "Charvi", "Darsh", "Devansh", "Dhruv", "Disha", "Divya", "Eshan", "Garv",
    "Harshil", "Hetvi", "Isha", "Ishan", "Jainam", "Janvi", "Jiya", "Kavya",
    "Keya", "Khushi", "Krish", "Krupa", "Lakshya", "Mahek", "Manan", "Meera",
    "Mihir", "Naisha", "Namra", "Neel", "Nidhi", "Niyati", "Parth", "Pooja",
    "Pranav", "Priya", "Rachit", "Radhika", "Raghav", "Rhea", "Riddhi", "Rudra",
    "Sachi", "Samarth", "Sanya", "Shaurya", "Shreya", "Siddhi", "Tanish",
    "Tanvi", "Tirth", "Urvi", "Vansh", "Vedant", "Vihaan", "Yashvi", "Zeel",
]

# Surnames are drawn from a pool that does NOT occur in the institute's real
# sheets, so no generated `first + last` pair can collide with a real student.
# Given names stay common (they carry no identifying power on their own) and
# the phone/address/email are fabricated independently.
LAST_NAMES = [
    "Ahluwalia", "Bapat", "Barhate", "Bhide", "Chandorkar", "Chaphekar",
    "Chitnis", "Dandekar", "Deolekar", "Dhond", "Ganvir", "Godbole", "Gokhale",
    "Hardikar", "Inamdar", "Jamdar", "Joglekar", "Kalelkar", "Karnik", "Kelkar",
    "Khaparde", "Kirloskar", "Lele", "Limaye", "Mahabal", "Marathe", "Mhatre",
    "Nadkarni", "Nene", "Oak", "Paranjape", "Phadke", "Ranadive", "Rege",
    "Sahasrabuddhe", "Sathaye", "Talpade", "Tembe", "Ubhaykar", "Vartak",
    "Velankar", "Wadekar", "Wagle", "Yardi",
]

# ------------------------------------------------------------- course catalogue
#
# (course category, course name, base fee at Vesu in 2025). Categories are
# copied verbatim from the real sheet including its inconsistent casing
# (`school course`, `advanced certificate course` lowercase; the rest Title).
# `Data Science & AI` really does appear exactly once for a course that is
# elsewhere filed under `advanced certificate course` — that mis-categorization
# is reproduced below.

LEGACY_CATALOGUE = [
    ("Basic", "Computer Basics", 4800),
    ("Basic", "Kids Course", 5400),
    ("Basic", "Computer Basics & Scratch Programming", 8500),
    ("Programming", "Core Python Programming", 7200),
    ("Programming", "Python Programming", 9000),
    ("Programming", "Python OOP", 9000),
    ("Programming", "Scratch Programming", 9000),
    ("Programming", "C Programming", 7200),
    ("Programming", "C & C++", 18000),
    ("Programming", "Core Java Programming", 9000),
    ("Programming", "Advanced Python Programming (5 Modules)", 33500),
    ("Advanced Excel", "Advance Excel", 7200),
    ("Advanced Excel", "Advanced Excel (M-1 & M-2)", 13600),
    ("Accounting", "Computer Accounting", 6000),
    ("Accounting", "Basic Computer Accounting", 6000),
    ("Accounting", "Advanced Computer Accounting", 13600),
    ("Graphic Designing", "Graphic Designing", 13600),
    ("Graphic Designing", "Basic Graphic Designing", 13600),
    ("Graphic Designing", "Adobe Photoshop", 9000),
    ("Graphic Designing", "Canva", 6000),
    ("Graphic Designing", "Video Editing", 9000),
    ("Graphic Designing", "Advanced Graphic Designing (6 mths)", 46150),
    ("Digital Marketing", "Digital Marketing", 18000),
    ("Digital Marketing", "Digital Marketing & SEO", 52000),
    ("Digital Marketing", "Social Media Marketing (DM-2)", 16200),
    ("Data Analysis", "Data Analysis", 52000),
    ("Data Analysis", "Business Analytics", 24500),
    ("Data Analysis", "Power BI", 6000),
    ("Web Designing & Development", "Web Designing", 19800),
    ("Web Designing & Development", "Diploma in Web Development", 88500),
    ("Web Designing & Development", "Full Stack Development", 52000),
    ("school course", "12th Computer Science", 15000),
    ("school course", "11th IP", 12000),
    ("school course", "10th std school course", 11000),
    ("Front-end Development", "Diploma in Front End Development", 46000),
    ("UI UX Designing", "UI & UX development", 44500),
    ("advanced certificate course",
     "Advanced Certificate in Data Analytics & Data Science", 66150),
    ("advanced certificate course",
     "Advanced Certificate in Python Development & Generative AI", 66150),
    ("Combo Course", "Combo Course (Computer Basics & Advanced Excel with Power BI)", 10700),
]

GENAI_CATALOGUE = [
    ("Basic", "Computer Basics & Generative AI Foundation", 6000),
    ("Basic", "Professional Office & Generative AI Essentials", 7200),
    ("Programming", "Python Foundation Program", 7200),
    ("Programming", "Professional Python Developer", 28000),
    ("Programming", "Python OOP Programming (Module 2)", 9000),
    ("Programming", "C Programming", 7200),
    ("Advanced Excel", "Advanced Excel", 7200),
    ("Advanced Excel", "Advanced Excel & Power BI", 14400),
    ("Accounting", "Tally Prime with GST", 6000),
    ("Accounting", "Advanced Computer Accounting with GST & Zoho Books", 14400),
    ("Graphic Designing", "Social Media Content & Ad Architect", 14400),
    ("Graphic Designing", "Basic Graphic Designing", 14400),
    ("Graphic Designing", "Advanced Graphic Designing", 52000),
    ("Graphic Designing", "Canva", 7200),
    ("Digital Marketing", "Digital Marketing & SEO", 52000),
    ("Digital Marketing", "Performance Advertising & Marketing Analytics", 13600),
    ("Digital Marketing", "Ecommerce Developer & SEO Specialist Program", 14400),
    ("Data Analysis", "Advanced Data Analytics", 52000),
    ("Data Analysis", "Business Analytics", 25600),
    ("Data Science & AI", "Advanced Certificate in Data Analytics & Data Science", 66150),
    ("advanced certificate course", "Adv. Cert. in Data Analytics & Data Science", 77000),
    ("advanced certificate course", "Adv. Cert. in Python Development & Generative AI", 77000),
    ("advanced certificate course", "Agentic AI & Automation Specialist", 52000),
    ("advanced certificate course", "AI & Data Science Professional", 77000),
    ("Combo Course", "Professional Business Finance & Accounting", 28000),
    ("school course", "12th computer science", 15000),
]

# Pal is the budget branch (Computer Basics 3500 there vs 4800 at Vesu);
# Citylight and Vesu price the same. Fees also rose ~year on year.
BRANCH_FEE_FACTOR = {"Vesu": 1.00, "Citylight": 1.00, "Pal": 0.73}
YEAR_FEE_FACTOR = {2022: 0.83, 2023: 0.85, 2024: 0.94, 2025: 1.00, 2026: 1.05}

MODULE_SUFFIXES = [
    " (M-1)", " (m-1)", " (Module 2)", " (module 2 & 3)", " (M-2 & M-3)",
    " module 2", " (6 months)", " (with zero module)", " (Online)",
]

COURSE_TYPOS = {
    "Advance Excel": ["Advance excel", "Advance Excel 1&2", "Advanced Excel (M-1 & M-2)"],
    "Python Programming": ["Python Programmig", "Whole Python Programming", "Python oop"],
    "Graphic Designing": ["Graohic Designing", "Graphics Designing (Photoshop & Illustrator)"],
    "Computer Basics": ["Computer basics", "computer basics ", "Computer Basic"],
    "Data Analysis": ["DATA ANALYSIS", "Data analysis "],
}

BRANCHES = ["Vesu", "Pal", "Citylight"]
BRANCH_VARIANTS = ["Vesu", "vesu", "Pal", "pal", "Citylight", "citylight", "NA", ""]

FACULTY_CANON = ["Yash", "Mansi", "Siddharth", "Vansh", "Subin", "Trusha"]
FACULTY_VARIANTS = {
    "Yash": ["Yash Sir", "Yash Kanodia Sir", "Yash sir", "Yash k"],
    "Mansi": ["Mansi Mam", "Mansi mam"],
    "Siddharth": ["Siddharth Sir", "Siddharth sir"],
    "Vansh": ["Vansh Sir", "vansh sir", "Vansh sir"],
    "Subin": ["Subin Sir", "Subin sir", "subin sir", "SUBIN SIR"],
    "Trusha": ["Trusha Mam", "Trusha", "Trusha mam"],
}

SOURCES_OLD = [
    "Google", "google", "Social media", "Social Media ", "Friends", "Friend ",
    "Old student ", "Old students", "old student", "Hoarding ", "Walk in",
    "Walkin", "Relatives ", "Family", "Indiamart", "Reference ", "Old Enquiry",
    "Banner", "Internet ", "NA", "",
]
SOURCES_NEW = [
    "Online (Google / Social Media)", "Old Students", "Friends",
    "Hoarding / Banners / Walk ins", "Google/Social Media", "Others", "NA",
]

OCCUPATIONS = [
    "Student", "Student ", "student", "Housewife", "Housewife ", "Job",
    "Business", "Business / Job", "Teacher", "Freelance", "Other ", "NA", "",
]

EDUCATION_OLD = [
    "12", "12th", "12th completed ", "11", "11th ", "10", "10th completed",
    "9", "9th std", "Graduate", "Graduation ", "Bcom", "BBA", "B.C.A", "MBA",
    "Post Graduation", "Diploma", "NA", "Na", "",
]
EDUCATION_NEW = [
    "12th Standard", "11th Grade", "10th Standard", "In School", "In college",
    "Graduation", "Post Graduation", "Diploma", "5th", "8th Grade", "NA",
]

DAYS = [
    "Monday, Wednesday, Friday", "Tuesday, Thursday, Saturday", "All Days",
    "Monday, Wednesday, Friday, Tuesday, Thursday, Saturday",
]

BATCH_TIMES = [
    "10:00 To 11:00", "11:00 To 12:00", "12:00 To 01:00", "01:00 To 02:00",
    "02:00 To 03:00", "03:00 To 04:00", "04:00 To 05:00", "05:00 To 06:00",
    "06:00 To 07:00", "07:00 To 08:00",
]

AREAS = [
    "Vesu", "Pal", "Citylight", "Althan", "Adajan", "Piplod", "Bhatar", "Umra",
    "Magdalla", "Parle point", "Palanpur", "Udhna", "Pandesara", "Varachha",
    "Rander", "Jahangirpura", "Ghodhod road", "New citylight", "Katargam", "",
]

PINCODES = ["395007", "395009", "395017", "395001", "394210", "395005",
            "394510", "395006", "0194520", "39500", "305007", ""]

# Invented building names — deliberately NOT real Surat societies, so no
# address in these samples can be traced to a real household. Locality names
# (Vesu / Pal / Althan ...) are kept because the cleaner canonicalizes them.
SOCIETIES = [
    "Sample Heights", "Testvan Residency", "Demo Enclave", "Placeholder Palace",
    "Mocklight Apartments", "Fixture Villa", "Synthetic Square", "Dummy Darshan",
    "Example Estate", "Sandbox Sadan", "Notional Nivas", "Stub Sankul",
    "Faux Fortune", "Trial Towers", "Proxy Paradise", "Specimen Skyline",
]

# Free-text status markers the institute writes INSIDE the name column.
NAME_MARKERS = [
    " (cancelled)", " (not coming)", " (Admission Cancelled)",
    " (admission cancelled all refunded)", " (fast track)",
    " (refunded trial amount, no admission)", " (register for trial 3 & 5 june)",
    " (Registered for trial, 4 to 5)", " (old student sister)",
]
# Markers that mean the enrollment really was cancelled (fee goes to ~0).
CANCEL_MARKERS = [
    " (cancelled)", " (Cancelled)", " (Admission Cancelled)",
    " (admission cancelled all refunded)", " (admission Cenceled)",
    " (not coming)",
]

ENQ_OUTCOMES = [
    "call not picked up", "call busy", "switch off", "out of service",
    "call cutted by them", "timepass", "will join in june", "not right now",
    "already discussed", "brochure sended", "will visit our branch in 1 or 2 days",
    "they joined somewhere else, our fees is high", "wrong number",
    "taken admission on other classes", "out of town", "will call us back",
    "not intrested", "will let us know once they take any decisions",
]

MODES = ["On Call", "On Branch", "On Bot", ""]

DRIVE_URL_FORMS = [
    "https://drive.google.com/file/d/1{tok}/view?usp=drivesdk",
    "https://drive.google.com/open?id=1{tok}",
    "https://drive.google.com/file/d/1{tok}/view?usp=drive_link",
]

# ------------------------------------------------------- fee-ledger vocabulary

PAYMENT_MODES = ["Cash", "Cash", "Cash", "Online", "Online", "Cheque"]

# `Description` on the ledger is the payment CHANNEL, and its vocabulary drifts
# every few months as the institute switches acquiring bank.
CHANNEL_BY_ERA = [
    (dt.date(2022, 1, 1), ["", "", "", "Gpay", "neft", "bank transer"]),
    (dt.date(2023, 3, 1), ["paid to shaurya creation", "paid to sc", "paid to ICICI",
                           "Gpay", "cash + gpay", ""]),
    (dt.date(2024, 4, 1), ["paid to ICICI", "paid to HDFC", "paid to fv", "paid to sc",
                           "Razorpay emi", ""]),
    (dt.date(2025, 1, 1), ["UPI- ICICI", "UPI -HDFC", "razorpay emi / icici",
                           "Paid to ICICI", ""]),
    (dt.date(2026, 1, 1), ["ICICI - UPI", "UPI -HDFC", "HDFC - UPI",
                           "razorpay emi/icici", ""]),
]

CHEQUE_BANKS = ["ICICI", "HDFC", "SBI", "BOB", "PNB", "KOTAK", "CUB", "AXIS",
                "UNION BANK", "canara bank", "sutex", "prime co operative bank"]

# Ops notes. In the real ledger these land in the UNNAMED column 12, while
# column 11 (`Description`) keeps the payment channel — a two-field split the
# header does not describe.
OPS_NOTES = [
    "COMPLETLY SHIFTED IN ICICI ZOHO", "COMPLETLY SHIFTED IN HDFC ZOHO",
    "admission at citylight", "receipt made at citylight",
    "shifted from citylight to Pal", "2400 refunded", "refund from icici",
    "300 more final disc at last payment", "1000 disc at last payment",
    "(Fees for module 2 has been refunded)", "admission cancel",
    "hdfc- future vision computer institute",
]

RNG = random.Random(SEED)


# --------------------------------------------------------------------- helpers

def _tok(n: int = 32) -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
    return "".join(RNG.choice(alphabet) for _ in range(n))


def drive_url() -> str:
    return RNG.choice(DRIVE_URL_FORMS).format(tok=_tok())


# Synthetic dialling blocks. Format-valid Indian mobiles (10 digits, leading
# 6-9) so the cleaner's phone regex and person-hash behave exactly as they do on
# real data — but every number sits in an obviously fabricated 99900/88800-style
# block, so none of these can ring a real handset.
FAKE_PREFIXES = ["99900", "88800", "77700", "66600", "99911", "88822"]


def clean_phone() -> str:
    """A well-formed but unmistakably synthetic 10-digit mobile."""
    return RNG.choice(FAKE_PREFIXES) + "".join(
        RNG.choice("0123456789") for _ in range(5))


def mangle_phone(phone: str) -> str:
    """Reproduce the real sheet's phone damage (~18% of cells)."""
    roll = RNG.random()
    if roll < 0.82:
        return phone
    if roll < 0.86:
        return phone[:-1]                       # 9 digits (dropped a char)
    if roll < 0.89:
        return phone + RNG.choice("0123456789")  # 11 digits (fat finger)
    if roll < 0.91:
        return "0" + phone                       # leading zero
    if roll < 0.93:
        return "+91" + phone
    if roll < 0.945:
        # NRI students appear with foreign numbers. Use the North American
        # 555-01xx block, formally reserved for fiction, so these cannot dial
        # a real handset either.
        return f"+1555010{RNG.randint(0, 99):02d}"
    if roll < 0.955:
        # No reserved fictional range exists for +81, so keep the length but
        # build it from the same fabricated digits.
        return "+81" + phone + RNG.choice("0123456789")
    if roll < 0.965:
        return f"{phone}, {clean_phone()}"       # two numbers, one cell
    if roll < 0.975:
        return f"{phone}---{clean_phone()}"
    if roll < 0.985:
        return f"{phone[:2]}-{phone[2:]}"
    return ""                                    # missing


def make_email(first: str, last: str) -> str:
    """Fabricated address on an RFC-2606 reserved domain — cannot reach anyone.

    The malformed variants mirror the real sheet's damage (typo TLD, missing
    dot, stray comma, no domain, mojibake) so the masker is exercised properly.
    """
    stem = f"{first.lower()}{last.lower()}{RNG.randint(1, 9999)}"
    roll = RNG.random()
    if roll < 0.40:
        return ""                                # most rows have no email
    if roll < 0.88:
        return f"{stem}@example.com"
    if roll < 0.91:
        return f"{stem}@example.con"             # typo TLD
    if roll < 0.93:
        return f"{stem}@example com"             # missing dot
    if roll < 0.945:
        return f"{stem}@example ,com"
    if roll < 0.955:
        return stem                              # no domain at all
    if roll < 0.965:
        return f"{stem}@example.in"
    if roll < 0.975:
        return f"{stem.upper()}@EXAMPLE.COM"
    if roll < 0.985:
        return "NA"
    return f"itâ€™s_{stem}@example.in"  # mojibake from a smart quote


def fmt_ddmmyyyy(d: dt.date) -> str:
    return d.strftime("%d/%m/%Y")


def fmt_mdyyyy(d: dt.date) -> str:
    return f"{d.month}/{d.day}/{d.year}"


def fmt_dd_mm_yyyy(d: dt.date) -> str:
    return d.strftime("%d-%m-%Y")


# The admission sheet is day-first; both live enquiry sheets and the fee ledger
# are month-first. `--dates ddmmyyyy` (the default) emits day-first everywhere,
# matching the standardized forms. `--dates mixed` reproduces the live
# discrepancy so the cleaner's per-column format vote has a regression fixture.
MIXED_DATE_FORMATS = False


def enquiry_fmt():
    """Date formatter for the two enquiry sheets."""
    return fmt_mdyyyy if MIXED_DATE_FORMATS else fmt_ddmmyyyy


def ledger_fmt():
    """Date formatter for the two fee sheets."""
    return fmt_mdyyyy if MIXED_DATE_FORMATS else fmt_ddmmyyyy


def mangle_date(d: dt.date, fmt) -> str:
    """~6% of dates in the real sheets are unusable."""
    roll = RNG.random()
    if roll < 0.94:
        return fmt(d)
    if roll < 0.960:                             # year typed as 00YY
        return fmt(d).replace(str(d.year), f"00{str(d.year)[-2:]}")
    if roll < 0.972:                             # separator drift
        return d.strftime("%d-%m-%Y") if fmt is not fmt_dd_mm_yyyy else d.strftime("%d/%m/%Y")
    if roll < 0.982:                             # decade-old typo
        return fmt(d.replace(year=d.year - 20))
    if roll < 0.992:
        return ""
    return RNG.choice(["hand written", "Given", "-", "NA"])


def short_year(text: str) -> str:
    """Drop the century: `04/08/2022` -> `04/08/22`. The ledger does this for
    roughly one row in twelve, which breaks naive `%Y` parsing."""
    parts = text.replace("-", "/").split("/")
    if len(parts) == 3 and len(parts[2]) == 4:
        return f"{parts[0]}/{parts[1]}/{parts[2][2:]}"
    return text


def course_variant(name: str) -> str:
    """Type the course the way an operator would: typos, suffixes, casing."""
    roll = RNG.random()
    if roll < 0.08 and name in COURSE_TYPOS:
        return RNG.choice(COURSE_TYPOS[name])
    if roll < 0.20:
        return name + RNG.choice(MODULE_SUFFIXES)
    if roll < 0.24:
        return name.upper()
    if roll < 0.30:
        return name + " "                        # trailing space
    if roll < 0.34:
        return name.lower()
    return name


def faculty() -> str:
    canon = RNG.choice(FACULTY_CANON)
    return RNG.choice(FACULTY_VARIANTS[canon])


def enroll_staff_as_students(people: list, n: int = 3) -> list:
    """Give some existing staff a student record, as the institute's staff have.

    Faculty here were students first, so their name appears on both sides of a
    sheet: a student row from when they studied, and the tutor cell now. This
    reproduces the collision **without inventing anyone** — the roster stays
    exactly the institute's six (`FACULTY_CANON`), and a few of the synthetic
    student records are renamed to match a real staff member.

    Doing it the other way round — promoting a random synthetic student onto
    the roster — would fabricate faculty who do not exist and pollute every
    faculty breakdown with them.
    """
    chosen = RNG.sample(FACULTY_CANON, n)
    enrolled = []
    for person, staff_name in zip(RNG.sample(people, n), chosen):
        person.name_override = staff_name
        enrolled.append(person)
    return enrolled


def dirty_branch(branch: str) -> str:
    """The same branch, typed inconsistently (~12% of cells)."""
    if RNG.random() < 0.12:
        return RNG.choice(BRANCH_VARIANTS)
    return branch


def address() -> str:
    if RNG.random() < 0.18:
        return ""
    return (f"{RNG.choice('ABCDEFG')}-{RNG.randint(101, 1204)}, "
            f"{RNG.choice(SOCIETIES)}, {RNG.choice([a for a in AREAS if a])}")


def channel_for(d: dt.date) -> str:
    pool = CHANNEL_BY_ERA[0][1]
    for start, options in CHANNEL_BY_ERA:
        if d >= start:
            pool = options
    return RNG.choice(pool)


# --------------------------------------------------------------------- people

class Person:
    """A synthetic human. Repeat enrollers reuse one Person across enrollments."""

    def __init__(self):
        self.first = RNG.choice(FIRST_NAMES)
        self.last = RNG.choice(LAST_NAMES)
        self.phone = clean_phone()
        self.father = clean_phone() if RNG.random() < 0.62 else ""
        self.mother = clean_phone() if RNG.random() < 0.28 else ""
        self.email = make_email(self.first, self.last)
        self.area = RNG.choice(AREAS)
        self.address = address()
        self.pincode = RNG.choice(PINCODES)
        self.dob = dt.date(RNG.randint(1972, 2017), RNG.randint(1, 12),
                           RNG.randint(1, 28))
        # Set for the few students who are also on the teaching staff, so
        # their student record carries the name the faculty column uses.
        self.name_override = ""

    @property
    def name(self) -> str:
        return self.name_override or f"{self.first} {self.last}"

    def dirty_name(self, marker: str = "") -> str:
        """Name as typed: casing drift, stray spaces, embedded status markers."""
        base = self.name
        roll = RNG.random()
        if roll < 0.06:
            base = base.lower()
        elif roll < 0.09:
            base = base.upper()
        elif roll < 0.13:
            base = f"{self.last} {self.first}"   # surname-first entry
        base += marker
        if RNG.random() < 0.15:
            base += " "
        return base


def build_people(n: int) -> list:
    return [Person() for _ in range(n)]


# ---------------------------------------------------------------- enrollments

# The GenAI rename landed mid-March 2026; enrollments before use the legacy
# catalogue. The admission Google Form only went live in April 2024.
RENAME_DATE = dt.date(2026, 3, 15)
FORM_LIVE_DATE = dt.date(2024, 4, 1)


class Receipt:
    __slots__ = ("date", "amount", "receipt_id", "mode", "channel", "note")

    def __init__(self, date, amount, receipt_id, mode, channel, note=""):
        self.date = date
        self.amount = amount
        self.receipt_id = receipt_id
        self.mode = mode
        self.channel = channel
        self.note = note


class Enrollment:
    """One admission = one `student-id`. A person can hold several."""

    def __init__(self, student_id: int, person: Person, join: dt.date):
        self.student_id = student_id
        self.person = person
        self.join = join
        self.branch = RNG.choice(BRANCHES)
        era_new = join >= RENAME_DATE
        self.category, base_course, base_fee = RNG.choice(
            GENAI_CATALOGUE if era_new else LEGACY_CATALOGUE)
        self.course = course_variant(base_course)
        factor = (BRANCH_FEE_FACTOR[self.branch]
                  * YEAR_FEE_FACTOR.get(join.year, 1.0))
        self.total_fees = int(round(base_fee * factor / 100.0)) * 100

        # ~4% of enrollments are cancelled. The institute records that inside
        # the Name column and zeroes the fee rather than deleting the row.
        self.marker = ""
        if RNG.random() < 0.04:
            self.marker = RNG.choice(CANCEL_MARKERS)
            self.total_fees = RNG.choice([0, 0, 500, 1000, 1700])

        # A handful of high-value razorpay enrollments carry a decimal fee.
        if self.total_fees > 10000 and RNG.random() < 0.01:
            self.total_fees = round(self.total_fees * 0.9973, 2)

        self.receipts: list = []
        self.pending = self.total_fees
        self.recon_note = ""

        # The Google Form Timestamp doubles as the student key: the SAME value
        # appears in the Admission Form sheet and in the student Main_data
        # (currently-learning) sheet, and the office uses it to tie the two
        # together. So it is generated once, here, and every sheet that needs
        # it reads this field — never re-rolls its own.
        self.form_timestamp = ""
        if join >= FORM_LIVE_DATE:
            stamp_date = join + dt.timedelta(days=RNG.choice([0, 0, 0, 1, 2]))
            self.form_timestamp = (
                f"{fmt_ddmmyyyy(stamp_date)} {RNG.randint(9, 20)}:"
                f"{RNG.randint(0, 59):02d}:{RNG.randint(0, 59):02d}")

    @property
    def paid(self) -> float:
        return sum(r.amount for r in self.receipts)

    @property
    def status(self) -> str:
        return "Full Paid" if self.pending == 0 else "Pending"


def build_enrollments(people: list, count: int, today: dt.date) -> list:
    """Assign sequential student-ids over Apr 2022 -> today, with re-enrollment.

    Real quirks reproduced: ids are assigned in rough date order but a few
    arrive out of sequence, and a small number of ids are skipped entirely
    (rows deleted from the sheet).
    """
    start = dt.date(2022, 4, 1)
    span = (today - start).days
    dates = sorted(start + dt.timedelta(days=RNG.randint(0, span))
                   for _ in range(count))

    enrollments, roster, next_id = [], [], 1
    held = collections.Counter()
    for i, join in enumerate(dates):
        # ~26% of enrollments are a repeat by someone already on the roster.
        # Capped at 5 per person, matching the busiest real re-enrollers.
        eligible = [p for p in roster if held[id(p)] < 5]
        if eligible and RNG.random() < 0.26:
            person = RNG.choice(eligible)
        else:
            person = people[i % len(people)]
            roster.append(person)
        held[id(person)] += 1
        if RNG.random() < 0.006:
            next_id += 1                          # a deleted row leaves a gap
        enrollments.append(Enrollment(next_id, person, join))
        next_id += 1
    return enrollments


def plan_payments(enr: Enrollment, today: dt.date) -> None:
    """Turn a total fee into a realistic receipt history.

    Mirrors the four patterns in the real ledger: paid in full on day one; a
    100/500 booking token then the balance; two roughly equal installments; and
    a long EMI tail on the 45k-77k certificate courses.
    """
    total = enr.total_fees
    if total <= 0:                                # cancelled, refunded to zero
        enr.receipts.append(_receipt(enr, enr.join, 0))
        enr.pending = 0
        return

    # Enrollments in the last ~7 weeks may not have been billed yet at all.
    if (today - enr.join).days < 50 and RNG.random() < 0.35:
        enr.pending = total
        return

    roll = RNG.random()
    if total >= 40000:
        parts = _split_emi(total, RNG.randint(2, 5))
    elif roll < 0.55:
        parts = [total]
    elif roll < 0.75:
        token = RNG.choice([100, 100, 500, 1000, 2000])
        parts = [token, total - token]            # booking token then balance
    elif roll < 0.92:
        half = int(total * RNG.uniform(0.35, 0.65) / 100) * 100
        parts = [half, total - half]
    else:
        parts = _split_emi(total, 3)

    # ~14% of multi-installment enrollments stall: the student stops paying
    # partway through. This — not date truncation — is what produces most of
    # the non-zero `Amt Pending` in the rollup, and every `Default` flag.
    stalls = len(parts) > 1 and RNG.random() < 0.14

    date = enr.join
    for idx, amount in enumerate(parts):
        if idx:
            gap = RNG.randint(1, 6) if amount == parts[-1] and len(parts) == 2 \
                else RNG.randint(25, 75)
            date = date + dt.timedelta(days=gap)
        if date > today:                          # future installment not yet paid
            break
        if stalls and idx == len(parts) - 1:
            break                                 # final installment never came
        enr.receipts.append(_receipt(enr, date, amount))

    enr.pending = total - enr.paid

    # ~22% of the time the rollup says Full Paid while money is still owed —
    # the single most dangerous defect in the real sheet, because nothing in
    # `fees-data` alone reveals it. Only summing the ledger exposes the gap.
    if enr.pending > 0 and RNG.random() < 0.22:
        enr.pending = 0
        enr.recon_note = "unreconciled"
    # A duplicated receipt entry produces a negative balance.
    elif enr.pending == 0 and RNG.random() < 0.004:
        dupe = enr.receipts[-1]
        enr.receipts.append(_receipt(enr, dupe.date, dupe.amount))
        enr.pending = -dupe.amount


def _split_emi(total: float, n: int) -> list:
    """Split into n installments; the real ones are round with a ragged tail."""
    step = int(total / n / 100) * 100
    parts = [step] * (n - 1)
    parts.append(round(total - sum(parts), 2))
    return parts


# Receipt books are per-branch and restart every few hundred entries, which is
# why the same Receipt-id turns up on different students in different years.
_RECEIPT_BOOKS: dict = {}


def _next_receipt_id(branch: str) -> str:
    book = _RECEIPT_BOOKS.setdefault(branch, RNG.randint(1, 300))
    book += 1
    if book > RNG.randint(700, 999):              # book exhausted, start a new one
        book = RNG.choice([1, 100, 200, 500, 601, 701, 901])
    _RECEIPT_BOOKS[branch] = book
    roll = RNG.random()
    if roll < 0.90:
        return str(book)
    if roll < 0.94:
        return f"{book:03d}"                      # zero padded
    if roll < 0.96:
        return f"0{book}"
    if roll < 0.975:
        return f"{book} & {book + 1}"             # two receipts, one cell
    if roll < 0.985:
        return f"{book},{book + 1}"
    if roll < 0.995:
        return f"{book}-1"
    return "no number"


def _receipt(enr: Enrollment, date: dt.date, amount) -> Receipt:
    mode = RNG.choice(PAYMENT_MODES)
    channel = channel_for(date)
    if mode == "Cheque":
        channel = f"{RNG.choice(CHEQUE_BANKS)}-{RNG.randint(1, 999999):06d}"
    return Receipt(date, amount, _next_receipt_id(enr.branch), mode, channel)


# ------------------------------------------------------------------ admission

def date_stamp(d: dt.date, fmt) -> str:
    return f"{fmt(d)} {RNG.randint(9, 20)}:{RNG.randint(0, 59):02d}:{RNG.randint(0, 59):02d}"


def gen_admission(enrollments: list) -> list:
    """Admission Form responses — only enrollments from Apr 2024 onward.

    `Receipt ID` carries the enrollment's FIRST receipt id, which is the exact
    join back to the fee ledger.
    """
    out = [ADMISSION_HEADER]
    for enr in enrollments:
        if enr.join < FORM_LIVE_DATE:
            continue                              # predates the form
        person = enr.person
        era_new = enr.join >= RENAME_DATE
        br = dirty_branch(enr.branch)
        # "Name for Google Contacts" is a Name-Branch-Course concatenation the
        # office stopped filling in around June 2026.
        contact = "" if enr.join >= dt.date(2026, 6, 11) \
            else f"{person.name}-{br}-{enr.course}"
        receipt_id = enr.receipts[0].receipt_id if enr.receipts else ""

        out.append([
            enr.form_timestamp,
            contact,
            enr.course,
            person.dirty_name(enr.marker),
            mangle_phone(person.phone),
            mangle_phone(person.father) if person.father else "",
            person.address,
            RNG.choice(EDUCATION_NEW if era_new else EDUCATION_OLD),
            RNG.choice(SOURCES_NEW if era_new else SOURCES_OLD),
            RNG.choice(OCCUPATIONS),
            br,
            RNG.choice(DAYS),
            RNG.choice(BATCH_TIMES) if RNG.random() > 0.03
            else f"{RNG.choice(BATCH_TIMES)}, {RNG.choice(BATCH_TIMES)}",
            faculty(),
            receipt_id,
            person.email,
            mangle_date(person.dob, fmt_ddmmyyyy) if RNG.random() > 0.30 else "",
            mangle_date(enr.join, fmt_ddmmyyyy),
            mangle_phone(person.mother) if person.mother else "",
            # Notes are rare in the real sheet (~5% of rows) but load-bearing
            # when present - they carry fees, age, and timing constraints.
            (RNG.choice(["60+", "total fees: 27000/-", "Trial",
                         "They want someone in morning 8 o clock",
                         "DOB: 11/11/2015", "Fastrack",
                         "Will join from 16th March", "home tuition"])
             if RNG.random() < 0.05 else ""),
            # "Education Details" only started being collected in early 2025.
            (RNG.choice(["12th commerce", "BBA", "10th completed ", "Bcom",
                         "Graduation ", "8th std", ""])
             if enr.join >= dt.date(2025, 2, 1) else ""),
            drive_url() if RNG.random() < 0.45 else "",
            drive_url() if RNG.random() < 0.62 else "",
            person.area,
            person.pincode,
            # The coupon column only exists from mid-May 2026 onward.
            (RNG.choice(["Yes", "No"]) if enr.join >= dt.date(2026, 5, 14) else ""),
        ])
    return out


# ------------------------------------------------------------------- fee sheets

def gen_fees_receipts(enrollments: list) -> list:
    """The receipt ledger — one row per payment, several rows per enrollment.

    Rows are written grouped by branch-ish blocks rather than strict date order,
    matching the real sheet where each branch's book was pasted in separately.
    """
    rows = []
    for enr in enrollments:
        for rec in enr.receipts:
            split = (rec.amount >= 2000 and RNG.random() < 0.07)
            # One receipt paid part-cash part-online is entered as TWO rows
            # sharing a receipt id and date.
            amounts = ([int(rec.amount * 0.4), rec.amount - int(rec.amount * 0.4)]
                       if split else [rec.amount])
            for part_i, amount in enumerate(amounts):
                mode = rec.mode if part_i == 0 else (
                    "Online" if rec.mode == "Cash" else "Cash")
                date_text = ledger_fmt()(rec.date)
                if RNG.random() < 0.08:
                    date_text = short_year(date_text)   # 2-digit year
                elif RNG.random() < 0.02:
                    date_text = mangle_date(rec.date, ledger_fmt())

                note = ""
                if RNG.random() < 0.05:
                    note = RNG.choice(OPS_NOTES)
                tail = ["", "", "", "", "", ""]
                if note:
                    tail[0] = note                # the UNNAMED column 12
                if RNG.random() < 0.004:
                    tail[5] = "#N/A"              # stray spreadsheet error

                rows.append([
                    enr.person.dirty_name(enr.marker),
                    enr.student_id,
                    enr.category,
                    enr.course,
                    enr.total_fees,
                    amount,
                    dirty_branch(enr.branch),
                    date_text,
                    rec.receipt_id,
                    mode,
                    rec.channel,
                ] + tail)

    # Blocked by branch, roughly date-ordered inside each block.
    rows.sort(key=lambda r: (str(r[6]).lower(), str(r[1])))
    return [FEES_RECEIPT_HEADER] + rows


def gen_fees_data(enrollments: list) -> list:
    """The per-enrollment rollup: one row per student-id, with Amt Pending."""
    rows = []
    for enr in enrollments:
        description = ""
        if enr.pending > 0 and RNG.random() < 0.55:
            description = "Default"               # a flag, typed into free text
        elif enr.marker and RNG.random() < 0.5:
            description = RNG.choice([
                "admission cancelled only 7000 fees received",
                "admisison cancelled fees refunded on 18/9/23",
                "2400 refunded",
            ])
        elif RNG.random() < 0.01:
            description = RNG.choice(["300 more final disc at last payment",
                                      "1000 disc at last payment"])

        join_text = ledger_fmt()(enr.join)
        if RNG.random() < 0.03:
            join_text = mangle_date(enr.join, ledger_fmt())

        rows.append([
            enr.student_id,
            enr.person.dirty_name(enr.marker),
            enr.category,
            enr.course,
            enr.total_fees,
            enr.status,
            enr.pending,
            dirty_branch(enr.branch),
            description,
            join_text,
        ])
    return [FEES_DATA_HEADER] + rows


# ------------------------------------------------------- lifecycle / timetable

# Nominal course length in months, by category. Used to decide whether a
# student has had time to finish — and to expose the edge case where a course
# runs LONGER than the 6-month churn cutoff.
DURATION_MONTHS = {
    "Basic": 2, "Advanced Excel": 2, "Programming": 3, "Accounting": 3,
    "Graphic Designing": 4, "Digital Marketing": 4, "Combo Course": 4,
    "Data Analysis": 6, "Web Designing & Development": 6, "school course": 6,
    "Front-end Development": 6, "UI UX Designing": 6,
    "advanced certificate course": 9, "Data Science & AI": 9,
}

CHURN_CUTOFF_MONTHS = 6

NOT_COMING_REASONS = [
    "gone to out of station for one month", "medical, will resume later",
    "exam going on", "word completed, powerpoint started",
    "shifted to another city", "job timing clash",
    "will resume after diwali", "not picking calls",
    "family function", "", "",
]

BARRED_REASONS = [
    "misbehaviour with faculty", "repeated absence, do not allot batch",
    "fees dispute", "not to entertain", "batch disturbance",
]

PLACEHOLDER_ROW_NAME = "zzzzz (Don't Delete)"


def months_between(start: dt.date, end: dt.date) -> int:
    return (end.year - start.year) * 12 + (end.month - start.month)


def assign_lifecycle(enr: "Enrollment", today: dt.date) -> str:
    """Which timetable tab this enrollment sits in.

    Returns one of: completed | active | not_coming | barred | absent.
    Cancelled admissions never reach a batch, so they appear in no tab.
    """
    if enr.marker:
        return "absent"
    # Nobody finishes exactly on the nominal duration — students carry over,
    # repeat modules, or take a break. Without this slack every cohort older
    # than its course length would be 100% completed, leaving no current
    # roster and almost no `paused` rows to test the 6-month rule against.
    duration = DURATION_MONTHS.get(enr.category, 4) + RNG.randint(0, 5)
    elapsed = months_between(enr.join, today)
    if elapsed >= duration:
        roll = RNG.random()
        if roll < 0.74:
            return "completed"
        if roll < 0.94:
            return "not_coming"
        return "barred"
    # Still inside the course window. A recent absence is usually unresolved —
    # it sits in Not_Coming until someone decides whether the student returns —
    # whereas older absences have long since been reclassified as completed or
    # barred. That is why `not_coming` is weighted higher here than above, and
    # it is what puts rows on both sides of the 6-month churn boundary.
    roll = RNG.random()
    if roll < 0.58:
        return "active"
    if roll < 0.96:
        return "not_coming"
    return "barred"


def churn_label(enr: "Enrollment", lifecycle: str, as_of: dt.date) -> str:
    """Terminal vs censored, applying the institute's 6-month rule.

    The clock runs from the DATE OF ADMISSION, not from the last date attended:
    a `Not_Coming` student is `paused` (censored) until six months after they
    enrolled, and `churned` from then on. `barred` is unconditional churn.
    """
    if lifecycle == "barred":
        return "churned"
    if lifecycle == "completed":
        return "completed"
    if lifecycle == "not_coming":
        return ("churned"
                if months_between(enr.join, as_of) >= CHURN_CUTOFF_MONTHS
                else "paused")
    return "active"


def _timetable_row(enr: "Enrollment", reason: str = None,
                   with_duration: bool = False) -> list:
    row = [
        enr.form_timestamp,                       # blank before Apr 2024
        mangle_phone(enr.person.phone),
        enr.person.dirty_name(),
        enr.course,
        faculty(),
        RNG.choice(BATCH_TIMES),
    ]
    if with_duration:
        # Main sheet only. Left blank on ~8% of rows, as in the real export —
        # the column exists but is not maintained on every line.
        months = DURATION_MONTHS.get(enr.category, 4)
        row.append("" if RNG.random() < 0.08 else months * 30)
    if reason is not None:
        row.append(reason)
    row.append("")                                # the blank interleaved column
    return row


def gen_timetables(enrollments: list, today: dt.date) -> dict:
    """Split the roster across the four lifecycle tabs."""
    tabs = {"active": [], "completed": [], "not_coming": [], "barred": []}
    for enr in enrollments:
        life = assign_lifecycle(enr, today)
        if life == "absent":
            continue
        enr.lifecycle = life
        if life == "not_coming":
            tabs[life].append(_timetable_row(enr, RNG.choice(NOT_COMING_REASONS)))
        elif life == "barred":
            tabs[life].append(_timetable_row(enr, RNG.choice(BARRED_REASONS)))
        elif life == "active":
            tabs[life].append(_timetable_row(enr, with_duration=True))
        else:
            tabs[life].append(_timetable_row(enr))

    # Dropdown placeholder rows the institute keeps in the real sheets.
    for name in ("active", "completed"):
        width = len(TIMETABLE_HEADER if name == "active" else COMPLETED_HEADER)
        tabs[name].insert(RNG.randint(0, max(0, len(tabs[name]) - 1)),
                          ["", "", PLACEHOLDER_ROW_NAME] + [""] * (width - 3))

    return {
        "main_data": [TIMETABLE_HEADER] + tabs["active"],
        "course_completed": [COMPLETED_HEADER] + tabs["completed"],
        "not_coming": [NOT_COMING_HEADER] + tabs["not_coming"],
        "not_to_entertain": [NOT_COMING_HEADER] + tabs["barred"],
    }


def gen_student_data(enrollments: list) -> list:
    """The hub tab: one row per admission, bridging Timestamp and student-id.

    `Timestamp` is blank for anything predating the admission form (Apr 2024),
    exactly as in the real export — which is why ~45% of the estate cannot be
    reached from the form side at all.
    """
    rows = []
    for enr in enrollments:
        p = enr.person
        rows.append([
            enr.form_timestamp,                   # blank before Apr 2024
            enr.student_id,
            p.dirty_name(enr.marker),
            mangle_phone(p.phone),
            mangle_phone(p.father) if p.father else "",
            p.email,
            mangle_date(p.dob, ledger_fmt()) if RNG.random() > 0.30 else "",
            ledger_fmt()(enr.join),
            enr.course,
            enr.category,
            dirty_branch(enr.branch),
            faculty(),
            RNG.choice(["Offline", "offline", "Online", "online", ""]),
            RNG.choice(SOURCES_NEW if enr.join >= RENAME_DATE else SOURCES_OLD),
            "", "", "",                           # stray token column + trailing
        ])
    return [STUDENT_DATA_HEADER] + rows


def gen_certificates(enrollments: list, today: dt.date) -> list:
    """Certificate register. A blank issue date means still pending."""
    rows = []
    serial = 1000
    issued_numbers: list = []
    for enr in enrollments:
        if getattr(enr, "lifecycle", None) != "completed":
            continue                              # only finishers get one
        serial += 1
        number = f"{enr.branch[0].upper()}{enr.join.strftime('%y%m%d')}{serial % 1000:03d}"
        # Duplicate certificate numbers really do occur.
        if issued_numbers and RNG.random() < 0.03:
            number = RNG.choice(issued_numbers)
        issued_numbers.append(number)

        duration = DURATION_MONTHS.get(enr.category, 4)
        eligible = enr.join + dt.timedelta(days=duration * 30)
        issue = eligible + dt.timedelta(days=RNG.randint(5, 120))

        remark = ""
        if issue > today or RNG.random() < 0.28:
            # Blank issue date = created but not yet issued.
            issue_text, number_text = "", (number if RNG.random() < 0.5 else "")
            remark = RNG.choice(["", "", "pending", "to be collected"])
        else:
            issue_text = ledger_fmt()(issue)
            number_text = number
            roll = RNG.random()
            if roll < 0.04:
                issue_text = RNG.choice(["hand written", "Given", "-", "NA"])
            elif roll < 0.07:
                issue_text = short_year(issue_text)
            if RNG.random() < 0.03:               # two certificates in one cell
                number_text = f"{number}\n{number[:-3]}{(serial + 1) % 1000:03d}"

        # Where the admission form was filled in wrong, the certificate carries
        # the corrected name from the student's government proof — so this name
        # legitimately differs from the admission name. Never match on it.
        if RNG.random() < 0.08:
            name = f"{enr.person.first} {RNG.choice(LAST_NAMES)}"
            remark = remark or "name as per government proof"
        else:
            name = enr.person.dirty_name()

        rows.append([
            enr.student_id, name, number_text, issue_text,
            ledger_fmt()(enr.join), enr.course, remark,
        ])
    return [CERTIFICATE_HEADER] + rows


# ------------------------------------------------------------------ enquiry 1

def date_seq(start: dt.date, end: dt.date, count: int) -> list:
    """Ascending-ish dates; ~4% deliberately out of order (real sheets do this)."""
    span = (end - start).days
    days = sorted(RNG.randint(0, span) for _ in range(count))
    dates = [start + dt.timedelta(days=d) for d in days]
    for i in range(len(dates) - 1):
        if RNG.random() < 0.04:
            dates[i], dates[i + 1] = dates[i + 1], dates[i]
    return dates


def enquiry_course(era_new: bool) -> str:
    pool = GENAI_CATALOGUE if era_new else LEGACY_CATALOGUE
    base = RNG.choice(pool)[1]
    if RNG.random() < 0.22:                       # multi-course cell
        second = RNG.choice(pool)[1]
        if second != base:
            return f"{course_variant(base)}, {second}"
    return course_variant(base)


def gen_enquiry1(people: list, rows: int) -> list:
    """Sheet 1: named enquiries (Apr 2024 - Mar 2026).

    Columns 10, 13, 14, 15, 23, 24, 25 are present in the export but never
    filled — the cleaner must drop all-empty columns without dropping the row.
    """
    out = [ENQUIRY1_HEADER]
    dates = date_seq(dt.date(2024, 4, 1), dt.date(2026, 3, 31), rows)
    enq_no = 8460
    bulk_stamp = None
    for i, d in enumerate(dates):
        person = people[(i * 7) % len(people)]
        era_new = d >= RENAME_DATE
        # ~14% of rows are phone-only, anonymized call logs (`ENQ-####`).
        anonymized = RNG.random() < 0.14 and d >= dt.date(2025, 9, 1)
        if anonymized:
            enq_no += 1
            name = RNG.choice([f"ENQ-{enq_no}", f"ENQ - {enq_no}", f"Enq {enq_no}"])
        else:
            name = person.dirty_name()
            if RNG.random() < 0.05:
                name += RNG.choice([
                    " (Register for trial 4 & 6 may)", " (parent)",
                    " (refunded trial amount)", " (indiamart)",
                ])

        # One real bulk import shares a single timestamp across ~9 rows.
        efmt = enquiry_fmt()
        if bulk_stamp is None and 0.30 < i / rows < 0.32:
            bulk_stamp = date_stamp(d, efmt)
        ts = bulk_stamp if (bulk_stamp and RNG.random() < 0.5 and i / rows < 0.36) \
            else date_stamp(d, efmt)

        # Mode of Enquiry only starts being recorded from Sept 2025.
        mode = RNG.choice(MODES) if d >= dt.date(2025, 9, 20) else ""

        out.append([
            ts,
            "",                                          # duplicate header, always blank
            "" if anonymized and RNG.random() < 0.3 else enquiry_course(era_new),
            name,
            mangle_phone(person.phone),
            "" if anonymized else person.address,
            "" if anonymized else RNG.choice(EDUCATION_NEW if era_new else EDUCATION_OLD),
            "" if anonymized else RNG.choice(OCCUPATIONS),
            "" if anonymized else RNG.choice(SOURCES_NEW if era_new else SOURCES_OLD),
            "",                                          # Preferred Batch Time - never filled
            "" if anonymized else (mangle_phone(person.father) if person.father else ""),
            dirty_branch(RNG.choice(BRANCHES)),
            "",                                          # Preferred Days - never filled
            "",                                          # Your Photograph - never filled
            "",                                          # Faculty - never filled
            "" if anonymized else person.email,
            "" if anonymized else (mangle_phone(person.mother) if person.mother else ""),
            (RNG.choice(["60+", "total fees: 27000/-", "Trial",
                         "They want someone in morning 8 o clock",
                         "DOB: 11/11/2015"]) if RNG.random() < 0.05 else ""),
            faculty(),
            "",                                          # the unnamed spacer column
            mode,
            mangle_date(d, efmt) if mode else "",
            "", "", "",                                  # DOB / Receipt / Status - never filled
        ])
    return out


# ------------------------------------------------------------------ enquiry 2

def gen_enquiry2(people: list, rows: int) -> list:
    """Sheet 2: the call-log tab (Apr 2026+). Mostly phone + course + outcome.

    Two quirks the cleaner must handle: a mid-sheet block is date-only (no time
    component), and the follow-up date/outcome pair migrates across three
    different columns over the life of the sheet.
    """
    out = [ENQUIRY2_HEADER]
    dates = date_seq(dt.date(2026, 4, 1), dt.date(2026, 8, 11), rows)
    enq_no = 8700
    for i, d in enumerate(dates):
        person = people[(i * 11) % len(people)]
        enq_no += 1 if RNG.random() > 0.05 else 0     # ~5% duplicate ENQ ids
        tag = RNG.choice([f"ENQ - {enq_no}", f"ENQ-{enq_no}", f"Enq {enq_no}"])
        if RNG.random() < 0.04:
            tag += RNG.choice([" (cancelled)", " (Shreya)", " (Veer Mistry)"])
        if RNG.random() < 0.02:
            tag = ""

        # A mid-sheet block was pasted date-only, with no time component. In
        # mixed mode it also carries the real sheet's dash separator.
        efmt = enquiry_fmt()
        bare_fmt = fmt_dd_mm_yyyy if MIXED_DATE_FORMATS else fmt_ddmmyyyy
        bare_block = 0.18 < i / rows < 0.24
        ts = bare_fmt(d) if bare_block else date_stamp(d, efmt)
        enq_date = bare_fmt(d) if bare_block else mangle_date(d, efmt)
        if RNG.random() < 0.03:
            ts, enq_date = "", ""

        course = enquiry_course(True)
        if RNG.random() < 0.10:
            course = RNG.choice(["NA", "SENDED CATALOGUE",
                                 "LINK TO EXPLORE COURSE CATALOGUE", "12 TH CS"])
        if bare_block:
            course = course.upper()

        followup = f"{RNG.randint(1, 28)}/{RNG.randint(1, 12)}/26" \
            if RNG.random() < 0.55 else ""
        outcome = RNG.choice(ENQ_OUTCOMES) if followup and RNG.random() < 0.8 else ""

        # The pair drifts: early rows put it in Notes/enq-discuusin, later rows
        # in Column 1/the trailing unnamed column.
        notes = enq_discussion = column1 = trailing = ""
        if i / rows < 0.35:
            notes, enq_discussion = followup, outcome
        elif i / rows < 0.60:
            column1, trailing = followup, outcome
        else:
            enq_discussion, column1 = followup, outcome
        if RNG.random() < 0.06:
            notes = RNG.choice(["AREA - PARLEPOINT", "9TH GRADE", "11 GRADE",
                                "DPS SCHOOL", "NO DATA ON THIS NUMBER",
                                "old Student fast track"])

        out.append([
            ts, enq_date, tag, course,
            mangle_phone(person.phone),
            "", "",                                       # guardian phones - rarely filled
            person.email if RNG.random() < 0.10 else "",
            person.area.upper() if RNG.random() < 0.22 else "",
            RNG.choice(["", "", "", "", "12th", "Bcom", "9TH GRADE", "M.B.A"]),
            RNG.choice(["", "", "", "Student", "STUDENT", "Job"]),
            "",                                           # source - not captured on this tab
            dirty_branch(RNG.choice(BRANCHES)),
            faculty(),
            RNG.choice(["On Call", "On Call", "On Call", "On Branch"]),
            notes, enq_discussion, column1, trailing,
        ])
    return out


# ----------------------------------------------------------------------- main

def write_csv(path: str, rows: list) -> None:
    # A row wider than the header does not raise anywhere downstream: pandas
    # silently promotes the first column to the index, shifting every field one
    # place left, and the whole file cleans to garbage. Caught exactly once,
    # after the admission sample spent a week producing zero usable rows.
    header, width = rows[0], len(rows[0])
    bad = [(i, len(r)) for i, r in enumerate(rows[1:], 1) if len(r) != width]
    if bad:
        i, got = bad[0]
        raise ValueError(
            f"{os.path.basename(path)}: header has {width} columns but row {i} "
            f"has {got} ({len(bad)} row(s) mismatched). Header: {header}")
    with open(path, "w", encoding="utf-8", newline="") as fh:
        csv.writer(fh).writerows(rows)
    print(f"  {os.path.basename(path):34s} {len(rows) - 1:5d} rows x "
          f"{len(rows[0]):2d} cols")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "samples"))
    ap.add_argument("--enrollments", type=int, default=420,
                    help="Rows in fees-data; admission gets the Apr-2024+ subset.")
    ap.add_argument("--enquiries1", type=int, default=170)
    ap.add_argument("--enquiries2", type=int, default=130)
    ap.add_argument(
        "--dates", choices=["ddmmyyyy", "mixed"], default="ddmmyyyy",
        help="ddmmyyyy (default): every sheet is day-first, matching the "
             "standardized forms. mixed: reproduces the live discrepancy "
             "(admission day-first; enquiry and fee sheets month-first) as a "
             "regression fixture for the per-column format vote.")
    args = ap.parse_args(argv)

    global MIXED_DATE_FORMATS
    MIXED_DATE_FORMATS = args.dates == "mixed"

    os.makedirs(args.out, exist_ok=True)
    today = dt.date(2026, 8, 11)

    # One shared roster so the same person appears as an enquiry, an admission,
    # and several enrollments — that linkage is what the funnel metrics need.
    people = build_people(150)
    # The institute's faculty studied here first, so a few student records
    # carry a name that also appears in the Faculty column.
    staff_alumni = enroll_staff_as_students(people)
    enrollments = build_enrollments(people, args.enrollments, today)
    for enr in enrollments:
        plan_payments(enr, today)

    # The real estate is FOUR workbooks, each with several sheets — not eleven
    # independent files. Filenames encode `<workbook>__<sheet>` so the grouping
    # survives the CSV export, because it is what decides the join strategy:
    # within a workbook the sheets are already linked; only the three
    # cross-workbook edges need a key.
    print(f"Writing synthetic institute samples to {args.out} "
          f"(seed {SEED}, dates={args.dates}):")

    print("\n  workbook: Admission Form (Responses)")
    write_csv(os.path.join(args.out, "admission_form__form_responses_1.csv"),
              gen_admission(enrollments))

    print("\n  workbook: Enquiry Form (Responses)")
    write_csv(os.path.join(args.out, "enquiry_form__form_responses_1.csv"),
              gen_enquiry1(people, args.enquiries1))
    write_csv(os.path.join(args.out, "enquiry_form__form_responses_2.csv"),
              gen_enquiry2(people, args.enquiries2))

    print("\n  workbook: student-data-sheet-from-1-4-22")
    write_csv(os.path.join(args.out, "student_data_sheet__student_data.csv"),
              gen_student_data(enrollments))
    write_csv(os.path.join(args.out, "student_data_sheet__fees_data.csv"),
              gen_fees_data(enrollments))
    write_csv(os.path.join(args.out, "student_data_sheet__fees_recpit.csv"),
              gen_fees_receipts(enrollments))

    print("\n  workbook: Student_Time_Table2023")
    # Lifecycle tabs must run before certificate-data, which only issues to
    # students the timetable marked `completed`.
    tabs = gen_timetables(enrollments, today)
    for tab, rows in tabs.items():
        write_csv(os.path.join(args.out, f"student_timetable__{tab}.csv"), rows)

    # certificate-data lives in the student-data workbook, not the timetable
    # one, but has to be generated after the lifecycle split.
    write_csv(os.path.join(args.out, "student_data_sheet__certificate_data.csv"),
              gen_certificates(enrollments, today))

    _report_labels(enrollments, today)
    print(f"\n  staff who also hold a student record: "
          f"{', '.join(p.name for p in staff_alumni)}")
    print("Synthetic only - no real student data. Safe to commit.")
    return 0


def _report_labels(enrollments: list, today: dt.date) -> None:
    """Show the churn split, so the 6-month rule is visibly exercised."""
    counts = collections.Counter()
    for enr in enrollments:
        life = getattr(enr, "lifecycle", "absent")
        counts[churn_label(enr, life, today) if life != "absent" else "cancelled"] += 1
    terminal = counts["churned"] + counts["completed"]
    print("\n  lifecycle labels @ as-of "
          f"{today:%d/%m/%Y} (6-month rule from date of admission):")
    for label in ("completed", "churned", "paused", "active", "cancelled"):
        note = {"paused": "censored", "active": "censored",
                "cancelled": "never started"}.get(label, "TERMINAL")
        print(f"    {label:10s} {counts[label]:4d}   {note}")
    print(f"    -> {terminal} trainable rows, "
          f"{counts['paused'] + counts['active']} censored")
    # The whole point of the fixture: Not_Coming must straddle the boundary,
    # so a test can prove the cutoff is applied and not just the tab name.
    nc = [e for e in enrollments if getattr(e, "lifecycle", None) == "not_coming"]
    split = collections.Counter(churn_label(e, "not_coming", today) for e in nc)
    print(f"    Not_Coming tab holds {len(nc)} rows -> "
          f"{split['churned']} churned (>=6mo) / {split['paused']} paused (<6mo)")


if __name__ == "__main__":
    raise SystemExit(main())
