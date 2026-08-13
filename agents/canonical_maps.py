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
