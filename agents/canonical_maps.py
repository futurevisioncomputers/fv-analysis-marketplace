"""Canonicalization maps for the institute's real free-text vocabulary.

Built from a study of the actual sheets (student-data, fees-recpit,
certificate-data, admission/enquiry forms):

- Faculty appears as `Yash` / `Yash Sir` / `Yash k` / `Yash Kanodia Sir` —
  two different humans plus honorific noise. `canonicalize_faculty` strips
  honorifics and applies an explicit alias table so distinct people never merge.
- Course is ~400 distinct strings for ~40 real programs (`Advance Excel` vs
  `Advanced Excel (M-1 & M-2)` vs `Advance excel 1&2`, typos like `developmnet`,
  `Graohic`, `illustartor`). `canonicalize_course` fixes typos, extracts the
  module suffix into its own value, and maps the rest onto a bounded set of
  course families so categorical profiling / cross-tabs work again
  (course otherwise blows past MAX_CATEGORICAL_CARDINALITY and drops out of EDA).

Inputs are expected lowercased + whitespace-collapsed (the Data Engineer's
`_normalize_categoricals` runs first). Both functions are pure and total:
non-string input is returned unchanged / (None, None).
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

# ----------------------------------------------------------------- branch

# The institute has exactly three branches. This is a closed set, confirmed by
# the institute — which makes it useful in a way an open dimension is not: a
# value outside it is a data-entry error, not a fourth branch. In the real
# sheets "adajan" appears in a branch column, but Adajan is a *locality*
# (it belongs in Residential Area), so grouping by branch silently invents a
# site that does not exist.
BRANCHES = ("citylight", "vesu", "pal")

# Localities that turn up in branch columns, and the branch they are served by.
# Confirmed by the institute: Adajan is an area a short way from the Pal centre,
# so a branch cell reading "adajan" is someone writing the neighbourhood instead
# of the site, and the site is Pal.
#
# This is an inference and is kept separate from `canonicalize_branch` on
# purpose: the closed-set check stays honest about what the cell literally said,
# and the caller decides whether to resolve. When it does resolve, the original
# locality is preserved so the move is auditable and reversible.
LOCALITY_BRANCH = {
    "adajan": "pal",
}

# Spelling drift seen in the sheets. Kept explicit rather than fuzzy-matched:
# a near-miss on a three-item set is far more likely to be a different concept
# (a locality, a course code) than a typo worth absorbing.
_BRANCH_ALIASES = {
    "city light": "citylight", "city-light": "citylight", "clt": "citylight",
    "vesu branch": "vesu", "pal branch": "pal", "palanpur patiya": "pal",
}

# Values that mean "not recorded" rather than a place.
_BRANCH_BLANKS = {"", "na", "n/a", "none", "-", "nil"}


def canonicalize_branch(value: Optional[str]) -> Optional[str]:
    """One of the three branches, None when blank, or the value unchanged.

    An unrecognized value is returned as-is rather than dropped or forced into
    the nearest branch: it is real data that belongs somewhere, and quietly
    reassigning it would move a student between sites. `is_known_branch` is how
    a caller flags it.
    """
    if not isinstance(value, str):
        return value
    text = re.sub(r"\s+", " ", value).strip().lower()
    if text in _BRANCH_BLANKS:
        return None
    text = _BRANCH_ALIASES.get(text, text)
    return text


def is_known_branch(value: Optional[str]) -> bool:
    """True when the value is one of the institute's three branches."""
    return canonicalize_branch(value) in BRANCHES


def branch_from_locality(value: Optional[str]) -> Optional[str]:
    """The branch serving a locality written in a branch column, else None.

    None means "not a locality we recognize" — including for values that are
    already branches. Callers use a non-None result to rewrite the cell while
    keeping the original.
    """
    return LOCALITY_BRANCH.get(canonicalize_branch(value) or "")


# ---------------------------------------------------------------- faculty

_HONORIFIC_RE = re.compile(r"\b(?:sir|mam|ma'am|madam|miss)\b\.?", re.IGNORECASE)

# Applied AFTER honorific stripping. Explicit so "yash" (Yash) and
# "yash k"/"yash kanodia" (Yash Kanodia) stay two people.
FACULTY_ALIASES = {
    "yash k": "yash kanodia",
    "yash kanodia": "yash kanodia",
}


def canonicalize_faculty(value: Optional[str]) -> Optional[str]:
    """Strip honorifics and resolve known aliases. Non-strings pass through."""
    if not isinstance(value, str):
        return value
    stripped = _HONORIFIC_RE.sub("", value)
    stripped = re.sub(r"\s+", " ", stripped).strip(" .,-")
    if not stripped:
        return value.strip()
    return FACULTY_ALIASES.get(stripped, stripped)


# ----------------------------------------------------------------- course

# Typo / spelling-variant fixes observed verbatim in the sheets.
_TYPO_FIXES: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"developmnet|devlopemnet|develpment"), "development"),
    (re.compile(r"dipolma|dimploma"), "diploma"),
    (re.compile(r"graohic|grapic\b"), "graphic"),
    (re.compile(r"illustartor|illustrat[ao]r"), "illustrator"),
    (re.compile(r"advancced|advanc?e\b"), "advanced"),
    (re.compile(r"digitalmarketing"), "digital marketing"),
    (re.compile(r"digtal"), "digital"),
    (re.compile(r"analystics"), "analytics"),
    (re.compile(r"compter"), "computer"),
    (re.compile(r"premiere?\b"), "premier"),
    (re.compile(r"psudo|pseudo"), "pseudo"),
]

# Module-suffix extraction. Ordered; first hit wins and is removed from the
# string so family matching sees the base course name.
_MODULE_RES: List[re.Pattern] = [
    # "(m-1 & m-2)", "(module 2 & 3)", "[ module 2 & 3]", "(module3,5,6,8,9)"
    re.compile(r"[\(\[]\s*(m(?:odules?)?\s*[-_ ]?\s*\d[^\)\]]*)\s*[\)\]]"),
    # "(5 modules)", "(2 modules)", "(all 3 modules)", "(all modules)"
    re.compile(r"[\(\[]\s*((?:all\s*)?\d*\s*modules?[^\)\]]*)\s*[\)\]]"),
    # "(1 & 2)", "(1 to 4)", "(module-less numeric ranges)"
    re.compile(r"[\(\[]\s*(\d\s*(?:&|,|to|and)\s*[^\)\]]*)\s*[\)\]]"),
    # bare "excel 1&2"
    re.compile(r"\b(\d\s*&\s*\d)\b"),
    # bare "m1", "m-1" (never matches the m in "dm-2": \b requires non-word before)
    re.compile(r"\b(m[-_ ]?\d(?:\s*&\s*m?[-_ ]?\d)*)\b"),
]

# Family rules: ordered (kw_regex, family). First match wins, so specific
# programs (combo, advanced certificates) precede generic keywords (python).
_FAMILY_RULES: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"\bcombo\b"), "combo course"),
    # advanced certificates (long programs; keep them distinct from base course)
    (re.compile(r"cert.*python|python.*cert"),
     "adv certificate: python & generative ai"),
    (re.compile(r"cert.*(data analy|data science)|(data analy|data science).*cert"),
     "adv certificate: data analytics & data science"),
    (re.compile(r"cert.*digital|digital.*cert"),
     "adv certificate: digital designing & marketing"),
    (re.compile(r"agentic\s*ai"), "agentic ai & automation"),
    # school courses: "12th ip", "12 ip", "11th cs", "10th std cbse", "(ip)"
    (re.compile(r"\bschool\b|\bcbse\b|\b\d{1,2}\s*th\b|\b\d{1,2}\s+(?:ip|cs|ib)\b|\((?:ip|cs)\)"),
     "school course"),
    # web / app stacks
    (re.compile(r"full\s*stack"), "full stack development"),
    (re.compile(r"front[\s-]*end"), "front end development"),
    (re.compile(r"web\s*develop"), "web development"),
    (re.compile(r"web\s*design"), "web designing"),
    (re.compile(r"wordpress"), "wordpress"),
    (re.compile(r"ui\s*[&/]?\s*ux|ux\s*[&/]?\s*ui"), "ui ux designing"),
    # analytics / office (excel MUST precede power bi: "adv excel & power bi")
    (re.compile(r"business analy"), "business analytics"),
    (re.compile(r"financial model"), "financial modelling"),
    (re.compile(r"excel"), "advanced excel"),
    (re.compile(r"power\s*bi"), "power bi"),
    (re.compile(r"data analy"), "data analysis"),
    (re.compile(r"\bsql\b"), "sql"),
    # marketing (social media before generic digital marketing)
    (re.compile(r"social media"), "social media marketing"),
    (re.compile(r"ecommerce|e-commerce"), "ecommerce & seo"),
    (re.compile(r"performance advertising|digital marketing|\bdm\b|\bseo\b|digital advert"),
     "digital marketing & seo"),
    # graphics / media tools
    (re.compile(
        r"graphic|photoshop|illustrator|corel|canva|premier|lightroom|"
        r"after effects|video edit|package design|reels|e-invite|cinematic|"
        r"cgi|3d packag|vfx"
    ), "graphic designing"),
    # accounting
    (re.compile(r"tally|accounting|\bgst\b|zoho"), "accounting"),
    # programming (java before c++: "core java & c++")
    (re.compile(r"\bjava\b"), "java programming"),
    (re.compile(r"data structure"), "data structures & algorithms"),
    (re.compile(r"pseudo"), "programming logic"),
    (re.compile(r"scratch"), "scratch programming"),
    (re.compile(r"python"), "python programming"),
    (re.compile(r"c\+\+.*(?<![\w+])c(?![\w+])|(?<![\w+])c(?![\w+]).*c\+\+"),
     "c & c++ programming"),
    (re.compile(r"c\+\+"), "c++ programming"),
    (re.compile(r"(?<![\w+])c(?![\w+]).*program"), "c programming"),
    (re.compile(r"\br\s+programming\b"), "r programming"),
    # data science (after cert + python rules; NOT bare "ai" — "generative ai
    # foundation" is a basics course)
    (re.compile(r"data science|machine learning|deep learning"), "data science & ai"),
    # office/basics last (so "basic graphic designing" hit graphics above)
    (re.compile(r"professional office|\boffice\b"), "office & generative ai"),
    (re.compile(
        r"computer basic|\bbasics\b|\bkids\b|typing|ms paint|"
        r"generative ai foundation"
    ), "computer basics"),
]


# --------------------------------------------------- reporting taxonomies
#
# Three coarse groupings the institute reports on, taken from an operator's
# corrections log (2026-08-14) after they hand-fixed a generated audit.
#
# All three are POLICY, not fact: they encode which distinctions the institute
# finds useful, and a different operator could want them back. So each one is
# applied as the reporting default while the finer original value is preserved
# in a `<col>_raw` column — the grouping is reversible, and no run has to
# re-derive it from a spreadsheet by hand again.

# The institute's closed set of reporting categories, fixed by the operator
# (2026-08-14). Closed means a value outside it is a bug in these rules, not a
# new category: every course resolves to one of these eight, with `Other` as
# the named landing place for anything the rules do not cover. That is a
# deliberate change from the earlier design, which left unmatched courses
# blank — the operator wants one bounded column they can group by, so the
# unmatched count is reported in the quality notes instead of by a NaN.
COURSE_CATEGORY_BUCKETS = (
    "Foundational/School",
    "Programming & Development",
    "Office & Productivity",
    "Digital Marketing",
    "Data & Analytics",
    "Accounting & Finance",
    "Design & Creative",
    "Other",
)

# Family -> reporting category. `AI & Emerging Tech` is deliberately absent:
# the operator merged it into Programming & Development, leaving it empty.
COURSE_CATEGORIES = {
    # Programming & Development — including the theory courses that used to
    # fall through to "Other" because no keyword matched them.
    "python programming": "Programming & Development",
    "java programming": "Programming & Development",
    "c programming": "Programming & Development",
    "c++ programming": "Programming & Development",
    "c & c++ programming": "Programming & Development",
    "r programming": "Programming & Development",
    "scratch programming": "Programming & Development",
    "programming logic": "Programming & Development",          # "Psudo Code"
    "data structures & algorithms": "Programming & Development",
    "web development": "Programming & Development",
    "full stack development": "Programming & Development",
    "front end development": "Programming & Development",
    "wordpress": "Programming & Development",
    "agentic ai & automation": "Programming & Development",
    "adv certificate: python & generative ai": "Programming & Development",
    # Design & Creative — video/motion work was landing in "Other".
    "graphic designing": "Design & Creative",
    "ui ux designing": "Design & Creative",
    "web designing": "Design & Creative",
    # Digital Marketing — every certificate spelling of it, which the old
    # keyword list missed one way or another.
    "digital marketing & seo": "Digital Marketing",
    "social media marketing": "Digital Marketing",
    "ecommerce & seo": "Digital Marketing",
    "adv certificate: digital designing & marketing": "Digital Marketing",
    # Data & Analytics
    "data analysis": "Data & Analytics",
    "data science & ai": "Data & Analytics",
    "business analytics": "Data & Analytics",
    "adv certificate: data analytics & data science": "Data & Analytics",
    # SQL is sold as a programming skill here, and the operator's audit files
    # classify it that way (3 of the 4 SQL rows). Power BI is NOT here: they
    # put it with Office & Productivity, consistently, across every spelling.
    "sql": "Programming & Development",
    # Accounting & Finance — split out of Office & Productivity by the
    # operator. Tally / GST / Zoho Books is a distinct buyer from someone
    # buying Excel and Word, and the institute reports on them separately.
    # Financial modelling comes here rather than staying in Data & Analytics:
    # its subject is finance, and the split is by subject, not by the tool.
    "accounting": "Accounting & Finance",
    "financial modelling": "Accounting & Finance",
    # Office & Productivity
    "advanced excel": "Office & Productivity",
    "power bi": "Office & Productivity",
    "office & generative ai": "Office & Productivity",
    "computer basics": "Office & Productivity",
    "combo course": "Office & Productivity",
    # Foundational/School — school tutoring ONLY. Any Computer Basics variant
    # was moved out of here by the operator.
    "school course": "Foundational/School",
}

# A bundle is classified by its anchor, not by whichever subject happens to
# come second: "Computer Basics & Graphic Designing" is an Office &
# Productivity sale, and "Professional Office & Generative AI Essentials,
# Canva" is one too — Canva is a tool thrown into an office programme, not a
# design enrolment. Checked against the RAW course string, before family
# matching, because the family rules resolve such a string to its second
# subject.
#
# Ordered: the school anchor is tested first. "Kids Course" reads as computer
# basics to the family rules, but the operator files it under school tutoring,
# which is where a parent-facing report would look for it.
_CATEGORY_ANCHORS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"\bkids?\b", re.IGNORECASE), "Foundational/School"),
    (re.compile(r"computer\s*basic|professional\s*office", re.IGNORECASE),
     "Office & Productivity"),
]

# Lead source, collapsed to the three buckets the institute acts on. The
# operator's judgement: old-student vs friend vs family vs relative is not an
# operational difference — referral or not is.
#
# There is no Print/Outdoor bucket. The operator fixed the set at three, and
# print/outdoor placements are local-awareness spend that produces someone at
# the counter — the same reasoning that already put hoardings and banners in
# Walk-in — so newspaper, pamphlet and radio land there too. Their raw text
# survives in `<col>_raw`, so pulling print back out later costs nothing.
_LEAD_SOURCE_RULES: List[Tuple[re.Pattern, str]] = [
    # Hoarding and banners drive people through the door rather than being a
    # separately tracked awareness channel, so they read as Walk-in.
    (re.compile(r"hoarding|banner|pamphlet|poster|\bwalk\s*\w*|walkin|"
                r"\bvisit|passing by|near\s*by|nearby|news\s*paper|newspaper|"
                r"\bprint\b|magazine|radio|\btv\b"), "Walk-in"),
    # Misspellings taken from the live sheet: "Frieds", "Refrence", "Old <name>
    # Student", "was a student", "Sibling (…)". A referrer's own name usually
    # sits in the cell, which the fallback below handles.
    (re.compile(r"old\s*\w*\s*student|ex\s*student|was a student|frie?n?ds?\b|"
                r"family|relative|referr?al|refer|refre|word of mouth|known|"
                r"neighbou?r|brother|sister|sibling|cousin|parent|colleague|"
                r"old\s*enquiry"), "Referral"),
    (re.compile(r"goog\w*|internet|online|social|facebook|instagram|insta\b|"
                r"youtube|whatsapp|website|\bweb\b|\bnet\b|indiamart|justdial|"
                r"just dial|\bsms\b|\bad(?:vertisement|vert)?\b|\bads\b|"
                r"linkedin|\bseo\b"), "Online/Google/Social"),
]

# Nothing was recorded at all.
_BLANK_VALUES = {"", "na", "n.a", "n/a", "none", "-", "nil", "nan"}

# The form's own catch-all answer. Distinct from blank: the person answered,
# and the answer was "none of these". It is NOT read as a walk-in — that would
# assert a channel on no evidence — and not as Referral either, whatever the
# old combined "Other/Referral" bucket happened to contain.
_EXPLICIT_OTHER = {"other", "others", "unknown"}
LEAD_SOURCE_BUCKETS = ("Online/Google/Social", "Referral", "Walk-in")

# Occupation. Self-employed vs salaried was judged not useful, so both are one
# working-adult bucket; the free-text trades that used to fall to "Other" join
# it, since a lawyer and a teacher are working adults by any reading.
#
# Four buckets, closed by the operator. Retired and unemployed had their own
# buckets here and no longer do: neither is a segment the institute markets to
# differently, and both are tiny. They fall to Other, which is where a reader
# of the closed set would look for them.
OCCUPATION_BUCKETS = ("Student", "Business / Job", "Housewife", "Other")

_OCCUPATION_RULES: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"house\s*wife|home\s*maker|housewife"), "Housewife"),
    # "School" is a school-going enquirer, not an occupation called school.
    (re.compile(r"\bstudent\b|\bstudying\b|\bschool\b|\bcollege\b|"
                r"\b\d{1,2}\s*th\b|\bpursuing\b"), "Student"),
    (re.compile(r"\bretired\b"), "Other"),
    (re.compile(r"\bunemployed\b|not working|no\s*job"), "Other"),
    (re.compile(r"business|\bjob\b|service|\bwork(?:ing)?\b|teacher|tutor|"
                r"lawyer|advocate|doctor|engineer|accountant|\bca\b|event "
                r"planner|diamond|shop|trader|manager|employee|self\s*employ"),
     "Business / Job"),
]

# A bare "Freelance" with no stated field stays Other. A qualified one
# ("Freelancing as graphic designer") is a working adult. The operator drew
# that line explicitly, so the qualifier is what decides.
_BARE_FREELANCE_RE = re.compile(r"^\s*free\s*lanc\w*\s*$", re.IGNORECASE)
_FREELANCE_RE = re.compile(r"free\s*lanc", re.IGNORECASE)


# Roles whose reporting column is a coarser derived one. When a breakdown or a
# crossing asks for the role on the left, it gets the column on the right if
# the cleaner produced it.
#
# This is what makes the taxonomies show up in ANALYSIS and not only in the
# cleaned CSV. Crossing by raw course means ~40 families against a 12-level
# cap, so most of the sheet lands in an "other levels" remainder and the grid
# says little; crossing by the 8 categories is the breakdown the institute
# actually reports. The fine column is still there and still reachable — pass
# its literal header instead of the role name.
REPORTING_ROLE_PREFERENCE = {
    "course": "course_category_derived",
    "course_category": "course_category_derived",
}


def preferred_reporting_column(
    role: str, roles, columns
) -> Optional[str]:
    """The column a role should break down by, honouring the coarse default.

    Falls back to the role's own column when the derived one is absent, so a
    source the taxonomies never ran on behaves exactly as before.
    """
    preferred = REPORTING_ROLE_PREFERENCE.get(role)
    if preferred:
        if preferred in columns:
            return preferred
        mapped = roles.get(preferred)
        if mapped and mapped in columns:
            return mapped
    col = roles.get(role)
    return col if col and col in columns else None


def canonicalize_course_category(
    raw_course: Optional[str], family: Optional[str] = None
) -> Optional[str]:
    """The reporting category for a course — one of COURSE_CATEGORY_BUCKETS.

    Args:
        raw_course: the course string as typed. Needed as well as the family
            because the Computer Basics rule reads the bundle, and the family
            has already resolved such a string to its second subject.
        family: the output of `canonicalize_course`; derived when omitted.

    A course that no rule covers becomes "Other" — the operator fixed the set
    of categories, and a bounded column is what they group by. It is still
    counted in the quality notes, so an "Other" that grows is visible as a gap
    in these rules rather than passing for a real segment.

    None means no course was recorded at all (blank cell), which is a different
    statement from "a course we could not classify" and is kept separate.
    """
    if not isinstance(raw_course, str) or not raw_course.strip():
        if not isinstance(family, str) or not family.strip():
            return None
    if isinstance(raw_course, str):
        for rx, anchored in _CATEGORY_ANCHORS:
            if rx.search(raw_course):
                return anchored
    if family is None and isinstance(raw_course, str):
        family, _ = canonicalize_course(raw_course)
    if not isinstance(family, str):
        return None
    return COURSE_CATEGORIES.get(family.strip().lower(), "Other")


def lead_source_basis(value: Optional[str]) -> Tuple[Optional[str], str]:
    """(bucket, how it was decided) — so the cleaner can report the weak ones.

    The basis matters because two of the four paths are assumptions rather
    than readings: a blank cell assumed to be a walk-in, and free text assumed
    to be a referrer's name. Both are reported, so an operator sees how much
    of a bucket rests on an assumption.
    """
    if not isinstance(value, str):
        # A blank cell (NaN/None) is the case the operator ruled on: nobody
        # fills the field for someone standing at the counter.
        return "Walk-in", "blank"
    text = re.sub(r"\s+", " ", value).strip().lower()
    if text in _BLANK_VALUES:
        return "Walk-in", "blank"
    if text in _EXPLICIT_OTHER:
        # The person answered "none of these". That is evidence of no channel,
        # and the closed set has no bucket for it, so it stays blank.
        return None, "explicit-other"
    for rx, bucket in _LEAD_SOURCE_RULES:
        if rx.search(text):
            return bucket, "rule"
    # What is left in this field, in the live sheets, is the name of a person
    # or an organisation — a referrer's own name, the school they came from, a
    # community trust. Somebody told them, which is what Referral means. The
    # closed set has three buckets and no Other, so leaving these blank would
    # drop real referrals out of the only column anyone groups by.
    return "Referral", "named-referrer"


def canonicalize_lead_source(value: Optional[str]) -> Optional[str]:
    """One of LEAD_SOURCE_BUCKETS, or None for an explicit "Other" answer."""
    return lead_source_basis(value)[0]


def canonicalize_occupation(value: Optional[str]) -> Optional[str]:
    """Working adults into one bucket; students, housewives kept apart."""
    if not isinstance(value, str):
        return None
    text = re.sub(r"\s+", " ", value).strip()
    if not text or text.lower() in _BLANK_VALUES:
        return None
    # Unlike a lead source, "Other" here is a real answer on the form — the
    # person said none of the listed occupations — so it keeps its bucket.
    if text.lower() in _EXPLICIT_OTHER:
        return "Other"
    if _BARE_FREELANCE_RE.match(text):
        return "Other"
    if _FREELANCE_RE.search(text):
        return "Business / Job"
    # Match case-folded. The cleaner lowercases categoricals before this runs,
    # but the function is also called directly and must not depend on that.
    lowered = text.lower()
    for rx, bucket in _OCCUPATION_RULES:
        if rx.search(lowered):
            return bucket
    return "Other"


def canonicalize_course(value: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Return (course_family, module_label) for a raw course string.

    - family: bounded canonical family (falls back to the cleaned string when
      no rule matches — never None for a string input).
    - module: the extracted module/part suffix ("m-1 & m-2", "1 & 2", ...) or
      None when the string names the whole course.
    """
    if not isinstance(value, str) or not value.strip():
        return (value if isinstance(value, str) else None), None

    text = value.strip().lower()
    for rx, repl in _TYPO_FIXES:
        text = rx.sub(repl, text)

    module: Optional[str] = None
    for rx in _MODULE_RES:
        m = rx.search(text)
        if m:
            module = re.sub(r"\s+", " ", m.group(1)).strip(" -_,")
            text = (text[: m.start()] + " " + text[m.end():])
            break

    text = re.sub(r"\s+", " ", text).strip(" .,&-")

    for rx, family in _FAMILY_RULES:
        if rx.search(text):
            return family, module
    return (text or value.strip().lower()), module
