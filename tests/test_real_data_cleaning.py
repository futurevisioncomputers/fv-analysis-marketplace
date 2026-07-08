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
