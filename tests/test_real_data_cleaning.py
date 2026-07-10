"""Tests for real-data cleaning: canonical maps, date bounds, lifecycle status.

Every fixture value below is copied verbatim from the institute's real sheets
(student-data / fees-recpit / certificate-data), so passing these means the
cleaner handles the actual chaos, not an idealized version of it.

Run: python -m tests.test_real_data_cleaning   (plain asserts, no pytest dep)
"""

from __future__ import annotations

import sys

import pandas as pd

from agents import canonical_maps as cm
from agents.data_engineer_agent import DataEngineerAgent


# ------------------------------------------------------------------ faculty

def test_faculty_canonical() -> None:
    cases = {
        # honorific stripping
        "siddharth sir": "siddharth",
        "siddharth": "siddharth",
        "mansi mam": "mansi",
        "vansh sir": "vansh",
        "subin sir": "subin",
        "trusha mam": "trusha",
        "trusha": "trusha",
        # two different Yash humans must NOT merge
        "yash sir": "yash",
        "yash": "yash",
        "yash k": "yash kanodia",
        "yash kanodia sir": "yash kanodia",
        "yash kanodia": "yash kanodia",
        # passthrough of unknowns (already lowercased upstream)
        "jinson": "jinson",
        "jay": "jay",
        "khyati": "khyati",
    }
    for raw, want in cases.items():
        got = cm.canonicalize_faculty(raw)
        assert got == want, f"faculty {raw!r}: got {got!r}, want {want!r}"
    assert cm.canonicalize_faculty(None) is None


# ------------------------------------------------------------------- course

def test_course_family() -> None:
    # (raw from real sheets, expected family)
    cases = [
        ("advance excel", "advanced excel"),
        ("advanced excel", "advanced excel"),
        ("advance excel 1&2", "advanced excel"),
        ("advanced excel (m-1 & m-2)", "advanced excel"),
        ("advanced excel & power bi", "advanced excel"),
        ("core python programming", "python programming"),
        ("python [ module 2 & 3]", "python programming"),
        ("python foundation program", "python programming"),
        ("advanced python programming (5 modules)", "python programming"),
        ("diploma in web developmnet, dm & seo", "web development"),
        ("web designing (1 & 2)", "web designing"),
        ("wordpress", "wordpress"),
        ("diploma in digitalmarketing & seo", "digital marketing & seo"),
        ("social media marketing (dm-2)", "social media marketing"),
        ("graohic designing & package designing", "graphic designing"),
        ("photoshop, coreldraw & illustartor", "graphic designing"),
        ("adobe premier pro", "graphic designing"),
        ("cinematic reels & e-invite masterclass", "graphic designing"),
        ("advance computer accounting (with zero module)", "accounting"),
        ("tally prime with gst", "accounting"),
        ("basic computer accounting (2 modules)", "accounting"),
        ("computer basics & generative ai foundation", "computer basics"),
        ("professional office & generative ai essentials", "office & generative ai"),
        ("12th ip", "school course"),
        ("12 ip", "school course"),
        ("11th cs", "school course"),
        ("10th std cbse", "school course"),
        ("data analysis (module3,5,6,8,9)", "data analysis"),
        ("advanced data analytics", "data analysis"),
        ("business analytics", "business analytics"),
        ("power bi & sql", "power bi"),
        ("c programming", "c programming"),
        ("c,c++", "c & c++ programming"),
        ("c & c++ programming", "c & c++ programming"),
        ("core java & c++", "java programming"),
        ("scratch programming", "scratch programming"),
        ("advanced certificate in python development & generative ai",
         "adv certificate: python & generative ai"),
        ("adv. cert. in data analytics & data science",
         "adv certificate: data analytics & data science"),
        ("advanced certificate in digital designing & marketing",
         "adv certificate: digital designing & marketing"),
        ("agentic ai & automation specialist", "agentic ai & automation"),
        ("full stack development (python)", "full stack development"),
        ("diploma in front end development", "front end development"),
        ("ui & ux development", "ui ux designing"),
        ("kids course", "computer basics"),
        ("typing master & ms paint", "computer basics"),
    ]
    for raw, want in cases:
        fam, _module = cm.canonicalize_course(raw)
        assert fam == want, f"course {raw!r}: got {fam!r}, want {want!r}"

    # cardinality collapse: whole real vocabulary lands in <= 45 families
    fams = {cm.canonicalize_course(raw)[0] for raw, _ in cases}
    assert len(fams) <= 45


def test_course_module_extraction() -> None:
    fam, module = cm.canonicalize_course("advanced excel (m-1 & m-2)")
    assert fam == "advanced excel" and module  # module captured
    fam, module = cm.canonicalize_course("python [ module 2 & 3]")
    assert fam == "python programming" and module
    fam, module = cm.canonicalize_course("core python programming")
    assert module is None


# ------------------------------------------------------------- date bounds

def test_date_bounds() -> None:
    # NB: literal garbage like "4/23/0026" (real sheet value) is already NaT'd by
    # pandas (outside datetime64[ns] range); bounds exist for plausible-looking
    # but impossible business dates: pre-2000 joins and far-future typos.
    parsed = pd.to_datetime(pd.Series([
        "2026-04-23", "2126-04-23", "1998-08-01", "2023-12-18", None,
    ]), errors="coerce")
    bounded, n_bad = DataEngineerAgent._enforce_date_bounds(parsed, "joining_date")
    assert n_bad == 2  # 2126 (future typo) and 1998 both out of enrollment range
    assert bounded.isna().sum() == 3  # the two bad + the original None
    assert bounded.notna().sum() == 2

    # DOB keeps 1998 (people are old) but still kills year 2126
    bounded, n_bad = DataEngineerAgent._enforce_date_bounds(parsed, "dob")
    assert n_bad == 1
    assert bounded.notna().sum() == 3


# ------------------------------------------------- lifecycle status flags

def test_enrollment_status_from_name() -> None:
    agent = DataEngineerAgent(output_dir="output")
    df = pd.DataFrame({
        "Name": [
            "Ritik Shah (admission cancelled all refunded)",  # real row id 435
            "Shweta Maheswari (Admission Cancelled)",          # real row id 1039
            "Riya Desai (not coming)",                         # real row id 35
            "Geetisha banthia (cancelled)",                    # real row id 33
            "Prithviraj Banerjee",                             # clean
        ]
    })
    issues: list = []
    agent._derive_status_flags(df, {"name": "Name"}, issues)

    assert list(df["enrollment_status"]) == [
        "refunded", "cancelled", "not_coming", "cancelled", "active",
    ]
    # markers stripped so the later hash is clean
    assert list(df["Name"]) == [
        "Ritik Shah", "Shweta Maheswari", "Riya Desai",
        "Geetisha banthia", "Prithviraj Banerjee",
    ]
    # legacy flag preserved for downstream metrics
    assert list(df["is_cancelled"]) == [True, True, False, True, False]


def test_note_markers_and_fast_track() -> None:
    # Real Course_Completed / Main_data name notes: fast-track + schedule notes
    # must strip (stable hash) and fast track must flag; statuses stay active.
    agent = DataEngineerAgent(output_dir="output")
    df = pd.DataFrame({
        "Name": [
            "Advay Tibrewal ( fast track)",           # real Course_Completed row
            "Avyukt bansal (ft) 30/6",                # real Course_Completed row
            "Anshul Agrawal (FT till july end)",      # real Main_data row
            "Harsh rajendrabhai soni (only till 30 may)",
            "Prithviraj Banerjee",
        ]
    })
    issues: list = []
    agent._derive_status_flags(df, {"name": "Name"}, issues)

    assert list(df["Name"]) == [
        "Advay Tibrewal", "Avyukt bansal", "Anshul Agrawal",
        "Harsh rajendrabhai soni", "Prithviraj Banerjee",
    ]
    assert list(df["is_fast_track"]) == [True, True, True, False, False]
    assert list(df["enrollment_status"]) == ["active"] * 5


def test_placeholder_rows_and_empty_columns() -> None:
    # Real timetable structure: "zzzzz (Don't Delete)" dropdown rows and
    # interleaved blank columns.
    agent = DataEngineerAgent(output_dir="output")
    df = pd.DataFrame({
        "Name of Student": ["zzzzz (Don't Delete)", "Vansh Soni", "Lipi chindaliya"],
        "": [None, None, None],                       # blank interleave column
        "Course": [None, "Core python ", "Advance Excel"],
    })
    issues: list = []
    df = agent._drop_empty_columns(df, issues)
    assert "" not in df.columns
    df, purged = agent._drop_placeholder_rows(df, issues)
    assert purged == 1
    assert list(df["Name of Student"]) == ["Vansh Soni", "Lipi chindaliya"]


def test_completion_status_from_sheet_name() -> None:
    agent = DataEngineerAgent(output_dir="output")
    cases = {
        "Student_Time_Table2023 - Course_Completed": "completed",
        "Student_Time_Table2023 - Not_Coming": "not_coming",
        "Student_Time_Table2023 - Main_data": "active",
    }
    for source_name, want in cases.items():
        df = pd.DataFrame({"Name": ["A", "B"]})
        agent._derive_completion_status(df, source_name, [])
        assert list(df["completion_status"]) == [want] * 2, source_name
    # unlabeled source: no column invented
    df = pd.DataFrame({"Name": ["A"]})
    agent._derive_completion_status(df, "fees-recpit", [])
    assert "completion_status" not in df.columns


def test_completion_flags_from_sheet_name() -> None:
    # completion_status also emits boolean churn labels the Analyst reads:
    # is_completed backs completion_rate, is_not_coming backs not_coming_rate.
    agent = DataEngineerAgent(output_dir="output")
    cases = {
        "Student_Time_Table2023 - Course_Completed": (True, False),
        "Student_Time_Table2023 - Not_Coming": (False, True),
        "Student_Time_Table2023 - Main_data": (False, False),
    }
    for source_name, (want_done, want_gone) in cases.items():
        df = pd.DataFrame({"Name": ["A", "B"]})
        agent._derive_completion_status(df, source_name, [])
        assert list(df["is_completed"]) == [want_done] * 2, source_name
        assert list(df["is_not_coming"]) == [want_gone] * 2, source_name
    # unlabeled source: no flags invented
    df = pd.DataFrame({"Name": ["A"]})
    agent._derive_completion_status(df, "fees-recpit", [])
    assert "is_completed" not in df.columns
    assert "is_not_coming" not in df.columns


def test_status_reason_role_detected() -> None:
    # Not_Coming sheet: "Status & reason" must NOT be eaten by generic `status`.
    agent = DataEngineerAgent(output_dir="output")
    df = pd.DataFrame({
        "Student Name": ["Radhi Paras Mehta"],
        "Status & reason": ["word completed, powerpoint started"],
        "Branch": ["Pal"],
    })
    roles = agent._detect_roles(df)
    assert roles.get("status_reason") == "Status & reason"
    assert roles.get("status") != "Status & reason"


# ------------------------------------------------- payment reconciliation

def test_payment_channel_from_description() -> None:
    # Real fees-recpit Description prose.
    agent = DataEngineerAgent(output_dir="output")
    df = pd.DataFrame({
        "Description": [
            "paid to ICICI",
            "razorpay emi",
            "2400 refunded",
            "cheque no 445566",
            "paid to sc/shaurya creation",
            "cash",
            "paid by gpay",
        ]
    })
    issues: list = []
    agent._derive_payment_channel(df, {"description": "Description"}, issues)

    got = [None if pd.isna(x) else x for x in df["payment_channel"]]
    assert got == [
        "bank_transfer", "emi", None, "cheque", None, "cash", "upi",
    ]
    assert list(df["is_refund_entry"]) == [
        False, False, True, False, False, False, False,
    ]


def test_negative_pending_survives() -> None:
    # Tanish Kalra real row: Amt Pending = -7200 (overpayment). Must NOT be
    # clipped to 0; amount/paid negatives still clip (garbage there).
    agent = DataEngineerAgent(output_dir="output")
    df = pd.DataFrame({
        "Amt Pending": ["-7200", "0", "1,500"],
        "Total Fees": ["-100", "12,000", "8000"],
    })
    issues: list = []
    agent._normalize_money(
        df, {"pending": "Amt Pending", "amount": "Total Fees"}, issues
    )
    assert list(df["Amt Pending"]) == [-7200.0, 0.0, 1500.0]
    assert list(df["Total Fees"]) == [0.0, 12000.0, 8000.0]


def test_payment_reconciliation_table() -> None:
    import tempfile

    agent = DataEngineerAgent(output_dir=tempfile.mkdtemp())

    # Ledger (fees-recpit shape): several receipt rows per enrollment.
    ledger = pd.DataFrame({
        "student-id": ["500", "500", "501", "501", "502", "503", "503"],
        "Receipt-Id": ["R1", "R2", "R3", "R4", "R5", "R6", "R7"],
        "Date of Receipt": pd.to_datetime([
            "2025-01-10", "2025-03-10", "2025-02-01", "2025-02-15",
            "2025-04-01", "2025-05-01", "2025-05-20",
        ]),
        "Amount": [5000.0, 5000.0, 10000.0, 7200.0, 5000.0, 2400.0, 2400.0],
        "Description": [
            "paid to ICICI", "paid to ICICI", "cash", "cash",
            "razorpay emi", "cash", "2400 refunded",
        ],
    })
    agent._derive_payment_channel(ledger, {"description": "Description"}, [])
    ledger_roles = {
        "student_id": "student-id", "receipt_id": "Receipt-Id",
        "receipt_date": "Date of Receipt", "amount": "Amount",
        "description": "Description",
    }

    # Rollup (fees-data shape): one row per enrollment.
    rollup = pd.DataFrame({
        "student-id": ["500", "501", "502", "503"],
        "Total Fees": [12000.0, 10000.0, 8000.0, 0.0],
        "Amt Pending": [2000.0, -7200.0, 0.0, 0.0],
    })
    rollup_roles = {
        "student_id": "student-id", "amount": "Total Fees",
        "pending": "Amt Pending",
    }

    packages = [
        {"source_name": "fees-data", "source_domain": "finance",
         "canonical_columns": rollup_roles},
        {"source_name": "fees-recpit", "source_domain": "finance",
         "canonical_columns": ledger_roles},
    ]
    frames = {"fees-data": rollup, "fees-recpit": ledger}

    summary = agent._build_payment_reconciliation(packages, frames)
    assert summary is not None
    recon = pd.read_parquet(summary["table_path"]).set_index("student_id")

    # 500: 2 installments, 59-day span, bank channel, books balance.
    row = recon.loc["500"]
    assert row["paid_sum"] == 10000.0 and row["n_installments"] == 2
    assert row["payment_span_days"] == 59
    assert row["payment_channel"] == "bank_transfer"
    assert not row["recon_flag"]

    # 501: overpaid — negative pending kept and flagged, books still balance.
    row = recon.loc["501"]
    assert row["negative_pending_flag"] and not row["recon_flag"]

    # 502: Full-Paid-style mismatch — total 8000, paid 5000, pending 0.
    row = recon.loc["502"]
    assert row["recon_flag"] and row["recon_gap"] == 3000.0

    # 503: cancelled + refunded — refund excluded from net paid.
    row = recon.loc["503"]
    assert row["refund_sum"] == 2400.0 and row["net_paid"] == 0.0
    assert not row["recon_flag"]

    assert summary["recon_mismatch_count"] == 1
    assert summary["negative_pending_count"] == 1
    assert summary["channel_counts"].get("emi") == 1


def test_default_aging_and_collection_efficiency() -> None:
    # Aging buckets debtors by days since last payment (as-of = ledger's own
    # latest receipt); collection_efficiency = Σ collected / Σ billed.
    import tempfile

    agent = DataEngineerAgent(output_dir=tempfile.mkdtemp())
    ledger = pd.DataFrame({
        "student-id": ["600", "601", "602"],
        "Receipt-Id": ["R1", "R2", "R3"],
        "Date of Receipt": pd.to_datetime([
            "2025-01-01",  # stale -> 90+ days behind as-of
            "2025-06-01",  # recent -> 0-30
            "2025-06-01",
        ]),
        "Amount": [7000.0, 4000.0, 8000.0],
    })
    ledger_roles = {
        "student_id": "student-id", "receipt_id": "Receipt-Id",
        "receipt_date": "Date of Receipt", "amount": "Amount",
    }
    rollup = pd.DataFrame({
        "student-id": ["600", "601", "602"],
        "Total Fees": [10000.0, 5000.0, 8000.0],
        "Amt Pending": [3000.0, 1000.0, 0.0],  # 602 fully paid -> not a debtor
    })
    rollup_roles = {
        "student_id": "student-id", "amount": "Total Fees",
        "pending": "Amt Pending",
    }
    packages = [
        {"source_name": "fees-data", "source_domain": "finance",
         "canonical_columns": rollup_roles},
        {"source_name": "fees-recpit", "source_domain": "finance",
         "canonical_columns": ledger_roles},
    ]
    frames = {"fees-data": rollup, "fees-recpit": ledger}

    summary = agent._build_payment_reconciliation(packages, frames)
    recon = pd.read_parquet(summary["table_path"]).set_index("student_id")

    assert recon.loc["600", "default_aging"] == "90+"
    assert recon.loc["601", "default_aging"] == "0-30"
    assert pd.isna(recon.loc["602", "default_aging"])  # not a debtor

    # collected = 7000+4000+8000 = 19000; billed = 23000
    assert summary["collection_efficiency"] == round(19000 / 23000, 4)
    assert summary["total_billed"] == 23000.0
    assert summary["default_aging_counts"] == {"90+": 1, "0-30": 1}
    assert summary["overdue_90plus_amount"] == 3000.0


def test_no_ledger_no_reconciliation() -> None:
    # Honesty gate: no finance ledger among sources -> None, nothing invented.
    agent = DataEngineerAgent(output_dir="output")
    df = pd.DataFrame({"Name": ["A"]})
    packages = [{"source_name": "students", "source_domain": "student",
                 "canonical_columns": {"name": "Name"}}]
    assert agent._build_payment_reconciliation(packages, {"students": df}) is None


def test_enquiry_conversion_cross_source() -> None:
    # Enquiry on one sheet, admission on ANOTHER, linked by person_id (name+phone
    # hash). A converts (cross-source), C converts (cross-source), B never admits.
    agent = DataEngineerAgent(output_dir="output")
    enquiries = pd.DataFrame({
        "person_id": ["A", "B", "C"],
        "is_enquiry": [True, True, True],
    })
    admissions = pd.DataFrame({
        "person_id": ["A", "C", "D"],
        "is_admitted": [True, True, True],
    })
    packages = [
        {"source_name": "enquiries", "source_domain": "admission",
         "canonical_columns": {}},
        {"source_name": "admissions", "source_domain": "student",
         "canonical_columns": {}},
    ]
    frames = {"enquiries": enquiries, "admissions": admissions}
    summary = agent._build_enquiry_conversion(packages, frames)
    assert summary is not None
    assert summary["enquired_persons"] == 3
    assert summary["converted_persons"] == 2          # A, C
    assert summary["conversion_rate"] == round(2 / 3, 4)
    assert summary["cross_source_conversions"] == 2   # both admitted elsewhere

    tbl = pd.read_parquet(summary["table_path"]).set_index("person_id")
    assert bool(tbl.loc["A", "cross_source"]) and not bool(tbl.loc["B", "converted"])


def test_enquiry_conversion_no_enquiry_frame() -> None:
    # Honesty gate: no is_enquiry flag anywhere -> None, nothing invented.
    agent = DataEngineerAgent(output_dir="output")
    df = pd.DataFrame({"person_id": ["A"], "is_admitted": [True]})
    packages = [{"source_name": "students", "source_domain": "student",
                 "canonical_columns": {}}]
    assert agent._build_enquiry_conversion(packages, {"students": df}) is None


# ------------------------------------------------------ person identity

def test_person_id_repeat_enrollment() -> None:
    # Khiren Jain re-enrolls under new student-ids (real: 3/244/609/1070).
    # Case/spacing/phone-format noise must hash to ONE person.
    agent = DataEngineerAgent(output_dir="output")
    df = pd.DataFrame({
        "student-id": [3, 244, 609, 74],
        "Name": ["Khiren Jain", "khiren  jain ", "KHIREN JAIN", "Rupesh Yadav"],
        "Mobile": ["9998877665", "+91 99988 77665", "9998877665.0", "9876543210"],
    })
    issues: list = []
    agent._derive_person_id(
        df, {"name": "Name", "student_mobile": "Mobile"}, issues
    )

    ids = list(df["person_id"])
    assert ids[0] == ids[1] == ids[2] != ids[3]
    # emitted id is a salted hash, never the raw name/phone
    assert all(len(i) == 16 and "khiren" not in i.lower() for i in ids)
    assert list(df["person_enrollment_count"]) == [3, 3, 3, 1]
    assert list(df["is_repeat_enrollment"]) == [True, True, True, False]


def test_person_id_conditional() -> None:
    agent = DataEngineerAgent(output_dir="output")

    # No name role -> no person columns invented.
    df = pd.DataFrame({"Amount": [100]})
    agent._derive_person_id(df, {"amount": "Amount"}, [])
    assert "person_id" not in df.columns

    # No phone role -> name-only key still works; no repeats -> no flag.
    df = pd.DataFrame({"Name": ["Khiren Jain", "Rupesh Yadav", None]})
    agent._derive_person_id(df, {"name": "Name"}, [])
    assert df["person_id"].notna().tolist() == [True, True, False]
    assert "is_repeat_enrollment" not in df.columns


def test_analyst_new_metrics() -> None:
    # The step-4 metrics must compute off the flags/columns the Data Engineer
    # now emits, not fall back to a record count.
    from agents.analyst_agent import AnalystAgent

    analyst = AnalystAgent()
    df = pd.DataFrame({
        "is_completed": [True, True, False, False, True],
        "is_not_coming": [False, False, True, False, False],
        "is_repeat_enrollment": [True, False, False, False, True],
        "is_certificate_pending": [False, True, True, False, True],
        "certificate_delay_days": [10.0, 45.0, None, 20.0, None],
    })
    package = {"canonical_columns": {}}

    for metric, want in [
        ("completion_rate", 0.6),
        ("not_coming_rate", 0.2),
        ("repeat_enrollment_rate", 0.4),
        ("certificate_pending_rate", 0.6),
    ]:
        res = analyst.run({"metric": metric}, package, df=df)
        head = res["headline_number"]
        assert head["metric"] == metric, f"{metric} fell back"
        assert round(head["value"], 4) == want, f"{metric}={head['value']}"

    # mean metric on the delay column (only the 3 issued rows count)
    res = analyst.run({"metric": "certificate_issue_lag_days"}, package, df=df)
    head = res["headline_number"]
    assert head["metric"] == "certificate_issue_lag_days"
    assert round(head["value"], 2) == 25.0  # (10+45+20)/3
    assert head["n"] == 3


def test_free_text_pii_scrub() -> None:
    # Contact details typed inside retained free-text notes must be redacted;
    # dates / pincodes / amounts / cheque numbers in the same text must survive.
    agent = DataEngineerAgent(output_dir="output")
    df = pd.DataFrame({
        "Status & reason": [
            "gone to egypt, call 9825012345",         # bare 10-digit phone
            "parent contact +91 98765 43210",         # formatted international
            "shifted on 10/05/2025, pincode 395007",  # date + pincode: keep
        ],
        "Description": [
            "paid via cheque no 445566",              # 6-digit cheque: keep
            "refund, mail a.b@icici.com",             # email redact
            "installment 12000 paid",                 # amount: keep
        ],
        "Total Fees": [12000.0, 5000.0, 8000.0],      # numeric col untouched
    })
    issues: list = []
    agent._scrub_free_text_pii(
        df, {"status_reason": "Status & reason", "description": "Description"}, issues
    )
    assert list(df["Status & reason"]) == [
        "gone to egypt, call [mobile]",
        "parent contact [mobile]",
        "shifted on 10/05/2025, pincode 395007",
    ]
    assert list(df["Description"]) == [
        "paid via cheque no 445566",
        "refund, mail [email]",
        "installment 12000 paid",
    ]
    assert list(df["Total Fees"]) == [12000.0, 5000.0, 8000.0]


def test_amount_collected_derived() -> None:
    # collected = billed total - pending (clipped >= 0). Overpayment (negative
    # pending) means collected exceeds billed. Skipped when an explicit paid role
    # already carries the figure.
    agent = DataEngineerAgent(output_dir="output")
    df = pd.DataFrame({
        "Total Fees": [10000.0, 5000.0],
        "Amt Pending": [2000.0, -500.0],  # row 2 overpaid
    })
    agent._derive_amount_collected(
        df, {"amount": "Total Fees", "pending": "Amt Pending"}, []
    )
    assert list(df["amount_collected"]) == [8000.0, 5500.0]

    df2 = pd.DataFrame({
        "Total Fees": [10000.0], "Amt Pending": [2000.0], "Paid": [8000.0],
    })
    agent._derive_amount_collected(
        df2, {"amount": "Total Fees", "pending": "Amt Pending", "paid": "Paid"}, []
    )
    assert "amount_collected" not in df2.columns  # explicit paid wins


def test_default_flag_derived() -> None:
    # is_default = pending > 0 per enrollment. Negative pending (overpayment)
    # and zero are NOT defaults; needs a numeric pending role.
    agent = DataEngineerAgent(output_dir="output")
    df = pd.DataFrame({"Amt Pending": [2000.0, 0.0, -500.0, 1.0]})
    agent._derive_default_flag(df, {"pending": "Amt Pending"}, [])
    assert list(df["is_default"]) == [True, False, False, True]

    # No pending role -> no flag (conditional emission).
    df2 = pd.DataFrame({"Total Fees": [10000.0]})
    agent._derive_default_flag(df2, {"amount": "Total Fees"}, [])
    assert "is_default" not in df2.columns


def test_analyst_default_rate() -> None:
    # default_rate is a per-enrollment rate off is_default, broken down by branch.
    from agents.analyst_agent import AnalystAgent

    analyst = AnalystAgent()
    df = pd.DataFrame({
        "is_default": [True, False, True, True, False],
        "Branch": ["Pal", "Pal", "Adajan", "Adajan", "Adajan"],
    })
    package = {"canonical_columns": {"branch": "Branch"}}
    res = analyst.run(
        {"metric": "default_rate", "dimensions": ["branch"]}, package, df=df,
    )
    head = res["headline_number"]
    assert head["metric"] == "default_rate", "default_rate fell back"
    assert round(head["value"], 4) == 0.6  # 3 of 5
    segs = {b["segment"]: b["value"] for b in res["breakdowns"]}
    assert segs["branch=Pal"] == 0.5           # 1/2
    assert round(segs["branch=Adajan"], 4) == round(2 / 3, 4)


def test_duplicate_certificate_flag() -> None:
    # Same certificate serial on two rows is a duplicate; blanks are NOT (a
    # missing number is a pending certificate, handled elsewhere).
    agent = DataEngineerAgent(output_dir="output")
    df = pd.DataFrame({"Cert No": ["FV-1", "FV-2", "FV-1", None, ""]})
    agent._derive_duplicate_certificate(df, {"certificate_number": "Cert No"}, [])
    assert list(df["is_duplicate_certificate"]) == [True, False, True, False, False]

    # No duplicates -> no flag column (conditional emission).
    df2 = pd.DataFrame({"Cert No": ["FV-1", "FV-2", None]})
    agent._derive_duplicate_certificate(df2, {"certificate_number": "Cert No"}, [])
    assert "is_duplicate_certificate" not in df2.columns


def test_enquiry_backlog_flag() -> None:
    # Stale (> 30d as-of latest enquiry) unconverted enquiry = backlog. Admitted
    # or recent enquiries are not backlog. As-of is the frame's own latest date.
    agent = DataEngineerAgent(output_dir="output")
    df = pd.DataFrame({
        "Timestamp": pd.to_datetime(
            ["2025-01-01", "2025-02-01", "2025-06-01", "2025-06-10"]
        ),
        "is_admitted": [False, True, False, False],
    })
    # latest enquiry = 2025-06-10. Row0 (161d, not admitted) backlog; row1
    # admitted -> no; row2 (9d) too recent -> no; row3 (0d) -> no.
    agent._derive_enquiry_backlog(df, {"enquiry_date": "Timestamp"}, [])
    assert list(df["is_enquiry_backlog"]) == [True, False, False, False]

    # All recent -> no flag column.
    df2 = pd.DataFrame({
        "Timestamp": pd.to_datetime(["2025-06-01", "2025-06-10"]),
        "is_admitted": [False, False],
    })
    agent._derive_enquiry_backlog(df2, {"enquiry_date": "Timestamp"}, [])
    assert "is_enquiry_backlog" not in df2.columns


def test_analyst_collection_efficiency() -> None:
    # Money-weighted ratio of totals, overall + by branch.
    from agents.analyst_agent import AnalystAgent

    analyst = AnalystAgent()
    df = pd.DataFrame({
        "amount_collected": [8000.0, 4000.0, 9000.0, 3000.0],
        "Total": [10000.0, 5000.0, 10000.0, 10000.0],
        "Branch": ["Pal", "Pal", "Adajan", "Adajan"],
    })
    package = {"canonical_columns": {
        "amount_collected": "amount_collected", "amount": "Total", "branch": "Branch",
    }}
    res = analyst.run(
        {"metric": "collection_efficiency", "dimensions": ["branch"]},
        package, df=df,
    )
    head = res["headline_number"]
    assert head["metric"] == "collection_efficiency"
    assert round(head["value"], 4) == round(24000 / 35000, 4)  # Σcol/Σtotal
    assert head["n"] == 4
    segs = {b["segment"]: b["value"] for b in res["breakdowns"]}
    assert segs["branch=Pal"] == 0.8          # 12000/15000
    assert segs["branch=Adajan"] == 0.6       # 12000/20000
    assert "ratio of totals" in res["methodology"]


def test_report_phone_leak_detection() -> None:
    # Report defence-in-depth must catch bare AND formatted phones, but never
    # trip on Chart.js numeric literals (epoch ms, large values have no separators).
    from agents.report_agent import _contains_mobile

    assert _contains_mobile("<td>9825012345</td>")          # bare 10-digit
    assert _contains_mobile("call +91 98765 43210 now")     # formatted intl
    assert _contains_mobile("ph: 98765-43210")              # hyphenated
    assert not _contains_mobile('{"data":[1704067200000, 1706745600000]}')
    assert not _contains_mobile("total 12000 pincode 395007 on 2025-01-10")


def test_prediction_blocks_thin_labels() -> None:
    # Honesty gate: too few terminal labels -> blocked, no model invented.
    from agents.prediction_agent import PredictionAgent
    agent = PredictionAgent()
    df = pd.DataFrame({
        "completion_status": ["completed", "not_coming", "active", "active"],
        "Branch": ["Pal", "Adajan", "Pal", "Adajan"],
    })
    res = agent.run({"canonical_columns": {"branch": "Branch"}}, df=df)
    assert res["status"] == "blocked"
    assert "labeled" in res["reason"].lower() or "terminal" in res["reason"].lower()


def test_prediction_blocks_no_label() -> None:
    # No completion signal at all -> blocked.
    from agents.prediction_agent import PredictionAgent
    agent = PredictionAgent()
    df = pd.DataFrame({"Branch": ["Pal", "Adajan"] * 30})
    res = agent.run({"canonical_columns": {"branch": "Branch"}}, df=df)
    assert res["status"] == "blocked"
    assert "completion label" in res["reason"].lower()


def test_prediction_trains_and_beats_baseline() -> None:
    # A feature that perfectly separates churn: NB must beat the 50% baseline and
    # surface the churn-linked value as a top risk factor. Censored (active) rows
    # are excluded from the label.
    from agents.prediction_agent import PredictionAgent
    agent = PredictionAgent()
    n = 30
    df = pd.DataFrame({
        "completion_status": (["completed"] * n + ["not_coming"] * n + ["active"] * 5),
        "Branch": (["Pal"] * n + ["Adajan"] * n + ["Pal"] * 5),
    })
    res = agent.run({"canonical_columns": {"branch": "Branch"}}, df=df)
    assert res["status"] == "ready", res.get("reason")
    assert res["n_labeled"] == 2 * n            # active rows excluded
    assert res["class_balance"] == {"not_coming": n, "completed": n}
    assert res["accuracy_lift"] > 0             # beats majority baseline
    top = res["top_risk_factors"][0]
    assert top["feature"] == "Branch" and top["value"] == "Adajan"
    assert top["churn_log_likelihood_ratio"] > 0


def test_gsheet_url_normalization() -> None:
    from agents.ingestion import normalize_gsheet_url

    # /edit URL with a gid -> single-tab CSV export.
    edit = "https://docs.google.com/spreadsheets/d/1AbC-dEf_123/edit#gid=456"
    got = normalize_gsheet_url(edit)
    assert got == (
        "https://docs.google.com/spreadsheets/d/1AbC-dEf_123/export?format=csv&gid=456"
    )
    # Already-published CSV URL is left alone.
    pub = "https://docs.google.com/spreadsheets/d/e/2PACX/pub?output=csv"
    assert normalize_gsheet_url(pub) == pub
    # Publish-to-web HTML embed (pubhtml + &amp; entities) -> CSV variant.
    embed = ("https://docs.google.com/spreadsheets/d/e/2PACX/pubhtml"
             "?gid=1312780133&amp;single=true&amp;widget=true")
    out = normalize_gsheet_url(embed)
    assert "/pub?" in out and "output=csv" in out
    assert "gid=1312780133" in out and "single=true" in out
    assert "pubhtml" not in out and "&amp;" not in out
    # Non-Google http(s) endpoint passes through (raw CSV endpoint).
    raw = "https://example.com/data/report.csv"
    assert normalize_gsheet_url(raw) == raw
    # Non-web scheme is rejected (blocks file:// and friends).
    try:
        normalize_gsheet_url("file:///etc/passwd")
        assert False, "file:// should be rejected"
    except ValueError:
        pass


def test_ingestion_snapshot_and_fetch() -> None:
    import datetime
    import os
    import pathlib
    import tempfile

    from agents.ingestion import fetch_csv, snapshot_local, snapshot_path

    now = datetime.datetime(2026, 7, 10, 9, 30, 0)
    with tempfile.TemporaryDirectory() as tmp:
        snap_dir = os.path.join(tmp, "ingest")
        # Deterministic dated snapshot path.
        p = snapshot_path("Main Data!", snap_dir, ".csv", now=now)
        assert os.path.basename(p) == "20260710_093000_Main_Data.csv"

        # fetch_csv works against a file:// URL (no network in tests).
        src = os.path.join(tmp, "src.csv")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write("a,b\n1,2\n")
        url = pathlib.Path(src).as_uri()
        out = fetch_csv(url, p)
        assert pd.read_csv(out).iloc[0].tolist() == [1, 2]

        # snapshot_local keeps the extension and copies the bytes.
        xlsx_src = os.path.join(tmp, "book.xlsx")
        with open(xlsx_src, "wb") as fh:
            fh.write(b"PK\x03\x04stub")
        copied = snapshot_local(xlsx_src, snap_dir, now=now)
        assert copied.endswith(".xlsx")
        with open(copied, "rb") as fh:
            assert fh.read() == b"PK\x03\x04stub"


def main() -> int:
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
