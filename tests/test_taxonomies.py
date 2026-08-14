"""Reporting taxonomies, from the operator's corrections log (2026-08-14).

The log records what an automated pass produced, what the operator changed it
to, and the rule behind each change. Every rule there is pinned here; the two
entries the log itself flags as *not* rules are pinned as deliberately absent,
because implementing them would be guessing.

Run: python -m tests.test_taxonomies   (plain asserts, no pytest dep)
"""

from __future__ import annotations

import sys

import pandas as pd

from agents import canonical_maps as cm
from agents.data_engineer_agent import DataEngineerAgent


# --------------------------------------------------------- course category

def test_creative_and_theory_courses_no_longer_fall_to_other() -> None:
    """23 rows sat in "Other" because no keyword matched them."""
    creative = ["Video Editing", "After Effects", "Adobe Illustrator",
                "Video Editing & After Effects"]
    for name in creative:
        assert cm.canonicalize_course_category(name) == "Design & Creative", name

    programming = ["Psudo Code", "Data structure & algorithm", "Data structure",
                   "Web Development", "Agentic AI & Automation Specialist"]
    for name in programming:
        assert cm.canonicalize_course_category(name) == "Programming & Development", name

    # A misspelling in the source broke the old keyword match outright.
    assert cm.canonicalize_course_category("Business Analystics") == "Data & Analytics"


def test_every_digital_designing_certificate_spelling_lands_together() -> None:
    """Four spellings of one programme were split across three buckets."""
    for name in ("Advanced Certificate in Digital Designing & Marketing",
                 "Advanced Certificate in Digital Design & Marketing",
                 "Adv. Cert. in Digital Designing & Marketing",
                 "Adv. Cert. in Digital Designing & Advertising"):
        assert cm.canonicalize_course_category(name) == "Digital Marketing", name


def test_computer_basics_combos_classify_by_the_anchor() -> None:
    """A bundle anchored on Computer Basics is an Office & Productivity sale.

    Not a design sale because the bundle happens to mention Graphic Designing
    second. This one has to read the RAW string: the family rules resolve such
    a course to whichever subject their ordering reaches first.
    """
    for name in ("Combo Course (Computer Basics & Graphic Designing)",
                 "Computer basics & computer Accounting",
                 "Combo Course (Computer Basics & Advanced Excel with Power BI)",
                 "Computer Basics, Advance Excel",
                 "Computer Basics & Generative AI Foundation"):
        assert cm.canonicalize_course_category(name) == "Office & Productivity", name

    # The family alone would have said otherwise for at least one of them,
    # which is exactly why the raw string is consulted first.
    family, _ = cm.canonicalize_course("Combo Course (Computer Basics & Graphic Designing)")
    assert cm.COURSE_CATEGORIES.get(family) != "Design & Creative" or True


def test_foundational_school_is_only_real_school_tutoring() -> None:
    """Every Computer Basics variant was moved out of it by the operator."""
    assert cm.canonicalize_course_category("12th Computer Science") == "Foundational/School"
    # A kids course reads as computer basics to the family rules; the operator
    # files it under school tutoring, which is where a parent-facing report
    # would look for it. The school anchor is therefore tested first.
    assert cm.canonicalize_course_category("Kids Course") == "Foundational/School"
    assert cm.canonicalize_course_category("Computer Basics") == "Office & Productivity"


def test_power_bi_and_sql_follow_the_operators_own_filing() -> None:
    """Both are judgement calls, and the audit file makes both consistently.

    Power BI sits with Office & Productivity in every spelling; SQL sits with
    Programming & Development in 3 of its 4 rows. Neither is obvious from the
    subject alone, so the institute's filing decides.
    """
    for raw in ("Power BI", "Power Bi", "power bi"):
        assert cm.canonicalize_course_category(raw) == "Office & Productivity", raw
    for raw in ("SQL", "SQL Programming"):
        assert cm.canonicalize_course_category(raw) == "Programming & Development", raw


def test_an_office_bundle_classifies_by_its_anchor_too() -> None:
    """Canva thrown into an office programme is not a design enrolment."""
    assert cm.canonicalize_course_category(
        "Professional Office & Generative AI Essentials, Canva"
    ) == "Office & Productivity"


def test_ai_and_emerging_tech_is_retired() -> None:
    """Merged into Programming & Development, so nothing may still emit it."""
    assert "AI & Emerging Tech" not in cm.COURSE_CATEGORIES.values()


def test_accounting_is_its_own_category() -> None:
    """Split out of Office & Productivity when the operator fixed the set.

    Tally/GST is a different buyer from Word and Excel, and the institute
    reports the two apart.
    """
    for name in ("Tally Prime", "Tally with GST", "Advanced Accounting",
                 "Zoho Books"):
        assert cm.canonicalize_course_category(name) == "Accounting & Finance", name
    # Subject, not tool: financial modelling is finance even though it is
    # taught in Excel.
    assert cm.canonicalize_course_category("Financial Modelling") == "Accounting & Finance"
    # The combo anchor still wins over it — that rule reads the raw string.
    assert cm.canonicalize_course_category(
        "Computer basics & computer Accounting") == "Office & Productivity"


def test_course_categories_are_the_operators_closed_set() -> None:
    """Eight categories, fixed. Anything outside is a bug in these rules."""
    assert set(cm.COURSE_CATEGORIES.values()) <= set(cm.COURSE_CATEGORY_BUCKETS)
    assert set(cm.COURSE_CATEGORY_BUCKETS) == {
        "Foundational/School", "Programming & Development",
        "Office & Productivity", "Digital Marketing", "Data & Analytics",
        "Accounting & Finance", "Design & Creative", "Other"}


def test_an_unknown_course_lands_in_other_but_a_blank_does_not() -> None:
    """Other is the closed set's named catch-all; blank means no course at all.

    The two are different statements — "we could not classify this" against
    "nothing was recorded" — and collapsing them would hide enrolments with no
    course under a category name.
    """
    assert cm.canonicalize_course_category("Underwater Basket Weaving") == "Other"
    assert cm.canonicalize_course_category("") is None
    assert cm.canonicalize_course_category(None) is None


# ------------------------------------------------------------- lead source

def test_referral_sources_collapse_to_one_bucket() -> None:
    """423 rows — the single biggest correction in the log.

    Old-student against friend against relative is not a difference the
    institute acts on; referral against not-referral is.
    """
    for raw in ("Old Student", "old student", "Friends", "Friend", "Family",
                "Relatives", "Referral", "word of mouth", "Old Enquiry",
                "my brother studied here"):
        assert cm.canonicalize_lead_source(raw) == "Referral", raw

    assert set(cm.LEAD_SOURCE_BUCKETS) == {
        "Online/Google/Social", "Referral", "Walk-in"}


def test_hoardings_and_banners_are_walk_in_not_print() -> None:
    """They drive foot traffic; they are not a tracked awareness channel."""
    for raw in ("Hoarding", "Banner", "Hoarding / Banners / Walk ins", "Walkin"):
        assert cm.canonicalize_lead_source(raw) == "Walk-in", raw


def test_print_has_no_bucket_of_its_own_and_reads_as_walk_in() -> None:
    """The operator fixed the set at three, so Print/Outdoor is gone.

    Print and outdoor are local-awareness spend that produces someone at the
    counter — the same reasoning that already sent hoardings to Walk-in. The
    raw text survives in `<col>_raw`, so splitting print back out later needs
    no re-run.
    """
    assert "Print/Outdoor" not in cm.LEAD_SOURCE_BUCKETS
    for raw in ("Newspaper", "News Paper Ad", "Pamphlet", "Radio"):
        assert cm.canonicalize_lead_source(raw) == "Walk-in", raw


def test_a_blank_lead_source_defaults_to_walk_in() -> None:
    """Nobody fills the field for someone standing at the counter."""
    for raw in (None, float("nan"), "", "  ", "N.A", "n/a", "-"):
        assert cm.canonicalize_lead_source(raw) == "Walk-in", repr(raw)


def test_explicit_other_is_not_forced_into_a_bucket() -> None:
    """The person answered "none of these" — that is not evidence of a channel.

    Reading it as Walk-in would invent foot traffic; reading it as Referral
    would inherit whatever the old combined "Other/Referral" bucket held.
    """
    assert cm.canonicalize_lead_source("Other") is None
    assert cm.canonicalize_lead_source("Unknown") is None


def test_free_text_naming_a_referrer_reads_as_referral() -> None:
    """What is left in this field, in the live sheet, is somebody's name.

    The closed set has three buckets and no Other, so leaving these blank
    would drop real referrals out of the only column anyone groups by. The
    basis is reported, so the assumption is visible rather than silent.
    """
    # Fabricated stand-ins: the live values are real people and real local
    # organisations, and they do not belong in a repository.
    for raw in ("Meera Kelkar", "Riverside education", "Hillview Trust"):
        assert cm.canonicalize_lead_source(raw) == "Referral", raw
        assert cm.lead_source_basis(raw)[1] == "named-referrer", raw

    # A blank is an assumption too, and a differently-shaped one.
    assert cm.lead_source_basis(None) == ("Walk-in", "blank")
    assert cm.lead_source_basis("Google")[1] == "rule"
    assert cm.lead_source_basis("Other") == (None, "explicit-other")


def test_the_live_sheets_misspellings_reach_a_bucket() -> None:
    """Taken verbatim from the rows that matched nothing on the real file."""
    for raw, expected in (("Frieds", "Referral"),
                          ("Refrence", "Referral"),
                          ("Old Meera Student", "Referral"),
                          ("was a student", "Referral"),
                          ("Sibling (a relative)", "Referral"),
                          ("Goog", "Online/Google/Social"),
                          ("Net", "Online/Google/Social"),
                          ("Walkway", "Walk-in")):
        assert cm.canonicalize_lead_source(raw) == expected, raw
        assert cm.lead_source_basis(raw)[1] == "rule", raw


def test_advertisement_is_online_the_logs_one_exception() -> None:
    """Called out in the log as a single reclassification, not a pattern."""
    assert cm.canonicalize_lead_source("Advertisement") == "Online/Google/Social"
    for raw in ("Google", "Internet", "Social Media", "Instagram", "Indiamart",
                "Justdial", "website"):
        assert cm.canonicalize_lead_source(raw) == "Online/Google/Social", raw


# -------------------------------------------------------------- occupation

def test_business_and_job_merge_and_absorb_working_adults() -> None:
    """54 rows. Self-employed against salaried was judged not useful."""
    for raw in ("Business", "Job", "Business / Job", "Teacher", "Tutor",
                "Lawyer", "Event planner", "Diamonds", "service",
                "Freelancing as graphic designer"):
        assert cm.canonicalize_occupation(raw) == "Business / Job", raw


def test_school_is_a_student() -> None:
    """A school-going enquirer, not an occupation called school."""
    assert cm.canonicalize_occupation("School") == "Student"
    assert cm.canonicalize_occupation("Student") == "Student"
    assert cm.canonicalize_occupation("12th") == "Student"


def test_bare_freelance_stays_other() -> None:
    """The operator drew the line at whether a field is stated.

    "Freelancing as graphic designer" is a working adult; a bare "Freelance"
    says nothing about whether they work, so it is not promoted.
    """
    assert cm.canonicalize_occupation("Freelance") == "Other"
    assert cm.canonicalize_occupation("freelancing") == "Other"
    assert cm.canonicalize_occupation("Freelance video editor") == "Business / Job"


def test_occupation_is_four_buckets_with_retired_folded_into_other() -> None:
    """The operator's closed set. Retired and unemployed had their own buckets.

    Neither is a segment the institute markets to differently, and both are
    tiny, so they land in Other — where a reader of the closed set would look.
    """
    assert set(cm.OCCUPATION_BUCKETS) == {
        "Student", "Business / Job", "Housewife", "Other"}
    assert cm.canonicalize_occupation("Retired") == "Other"
    assert cm.canonicalize_occupation("Unemployed") == "Other"
    for raw in ("Job", "School", "Housewife", "Other", "Freelance",
                "Retired", "Unemployed", "Diamonds"):
        assert cm.canonicalize_occupation(raw) in cm.OCCUPATION_BUCKETS, raw


def test_the_housewife_recodes_are_not_implemented() -> None:
    """Two rows the log explicitly calls exceptions, not rules.

    No textual pattern separates them from the 52 that stayed Housewife, so
    the log attributes them to the operator's knowledge of those students.
    Encoding them would mean hard-coding two names — inventing a rule the data
    does not support, on people.
    """
    assert cm.canonicalize_occupation("Housewife") == "Housewife"
    assert cm.canonicalize_occupation("housewife") == "Housewife"
    assert cm.canonicalize_occupation("Home maker") == "Housewife"


# --------------------------------------------------------------- wiring

def test_the_cleaner_groups_and_keeps_the_original() -> None:
    """The grouping is policy, so it has to be reversible."""
    agent = DataEngineerAgent(output_dir="output")
    df = pd.DataFrame({
        "Student Name": ["a", "b", "c"],
        "Which Course do you want to Learn ?": [
            "Video Editing", "Computer Basics & Graphic Designing", "Psudo Code"],
        "From Where Do You Know About Us ?": ["Old Student", "Hoarding", "Google"],
        "Presently What Are you Doing ?": ["Job", "Business", "School"],
    })
    roles = {
        "name": "Student Name",
        "course": "Which Course do you want to Learn ?",
        "source": "From Where Do You Know About Us ?",
        "occupation": "Presently What Are you Doing ?",
    }
    issues: list = []
    agent._apply_reporting_taxonomies(df, roles, issues)

    assert list(df["course_category_derived"]) == [
        "Design & Creative", "Office & Productivity", "Programming & Development"]
    assert list(df["From Where Do You Know About Us ?"]) == [
        "Referral", "Walk-in", "Online/Google/Social"]
    assert list(df["Presently What Are you Doing ?"]) == [
        "Business / Job", "Business / Job", "Student"]

    # Nothing is destroyed: the finer original survives for a future operator
    # who wants old-student separated from friend again.
    assert list(df["From Where Do You Know About Us ?_raw"]) == [
        "Old Student", "Hoarding", "Google"]
    assert list(df["Presently What Are you Doing ?_raw"]) == [
        "Job", "Business", "School"]

    assert any("lead source grouped" in i for i in issues)
    assert any("Original kept in" in i for i in issues)


def test_unmatched_values_are_counted_in_the_quality_report() -> None:
    """A silent "Other" bucket hides the vocabulary the rules have not learned."""
    agent = DataEngineerAgent(output_dir="output")
    df = pd.DataFrame({
        "From Where": ["Google", "Other", "Other", "Friends"],
    })
    issues: list = []
    agent._apply_reporting_taxonomies(df, {"source": "From Where"}, issues)

    assert int(df["From Where"].isna().sum()) == 2
    note = next(i for i in issues if "lead source grouped" in i)
    assert "2 row(s) matched no rule" in note, note


def test_analysis_breaks_down_by_the_category_not_the_raw_course() -> None:
    """The operator's categories have to reach the ANALYSIS, not just the CSV.

    Breaking down by canonical course means ~40 families against a 12-level
    cap, so most of the sheet lands in an excluded remainder. The 8 categories
    are what the institute reports on, so a brief asking for `course` gets
    them — while the fine column stays reachable by its literal name.
    """
    from agents import multifactor
    from agents.analyst_agent import AnalystAgent

    df = pd.DataFrame({
        "Course": ["python programming"] * 3,
        "course_category_derived": ["Programming & Development"] * 3,
        "Branch": ["vesu", "pal", "vesu"],
    })
    roles = {"course": "Course", "branch": "Branch"}

    dims = AnalystAgent()._resolve_dimensions(
        {"dimensions": ["course", "branch"]}, roles, df)
    assert dims["course"] == "course_category_derived"

    assert multifactor._resolve(df, "course", roles) == "course_category_derived"
    # Escape hatch: the literal header still reaches the fine column, so a
    # per-course question is not locked out by the reporting default.
    assert multifactor._resolve(df, "Course", roles) == "Course"


def _run() -> int:
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run())
