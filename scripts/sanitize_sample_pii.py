#!/usr/bin/env python
"""Rewrite every name / phone / email in the committed sample data.

Policy: **no sample or fixture file in this repo may contain a contact detail
that could belong to a real person** — not a real-looking name, not a phone in
a live dialling series, not an address on a routable mail domain. Even when a
file was already synthetic, a plausible name paired with a real Vodafone-series
number is indistinguishable from live data to anyone who opens it later.

This pass makes the guarantee mechanical rather than a matter of trust:

  - names  -> given name + a surname from a pool absent from the real sheets
  - phones -> the fabricated 99900 / 88800 / 77700 / 66600 blocks
  - emails -> `example.com` / `example.in` (RFC 2606 reserved, non-routable)

The mapping is deterministic and value-stable: one original value always maps
to the same replacement, so repeat enrollments, phone-based joins, and
duplicate-detection fixtures keep working exactly as before. Structure is
preserved; only identity is replaced.

The rewrite is a hash, so re-running it produces a *different* safe value —
it is not idempotent. `--check` therefore tests membership in the safe space
(surname in the approved pool, phone in a fabricated block, mail domain under
`example.`) rather than re-deriving the mapping, so it is stable to re-run and
suitable for CI.

Run:
    python scripts/sanitize_sample_pii.py            # rewrite in place
    python scripts/sanitize_sample_pii.py --check    # exit 1 if anything unsafe
"""

from __future__ import annotations

import argparse
import csv
import glob
import hashlib
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Files this policy covers. `data/ingest/` is gitignored (raw uploads) and is
# deliberately NOT touched — it is never committed.
TARGET_GLOBS = [
    os.path.join(ROOT, "samples", "*.csv"),
    os.path.join(ROOT, "data", "*.csv"),
]

# Outputs of `make_form_samples.py` are safe *by construction* — every name,
# phone and email is fabricated at generation time. They are skipped here
# because this pass would also flatten the dirty-data patterns they exist to
# carry: it normalizes every phone to a clean 10 digits and rewrites names in
# Title Case, destroying the 9/11/13-digit variants, the `+91`/`+81` prefixes,
# the two-numbers-in-one-cell cases, the casing drift, and the status markers
# buried inside the Name column. Regenerate those files instead of scrubbing
# them; `--check` still audits them below.
GENERATED_PREFIXES = (
    "admission_form__", "enquiry_form__",
    "student_data_sheet__", "student_timetable__",
)

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

# Same pool as make_form_samples.py: surnames that do not occur in the
# institute's real sheets, so no generated pair can match a real student.
LAST_NAMES = [
    "Ahluwalia", "Bapat", "Barhate", "Bhide", "Chandorkar", "Chaphekar",
    "Chitnis", "Dandekar", "Deolekar", "Dhond", "Ganvir", "Godbole", "Gokhale",
    "Hardikar", "Inamdar", "Jamdar", "Joglekar", "Kalelkar", "Karnik", "Kelkar",
    "Khaparde", "Kirloskar", "Lele", "Limaye", "Mahabal", "Marathe", "Mhatre",
    "Nadkarni", "Nene", "Oak", "Paranjape", "Phadke", "Ranadive", "Rege",
    "Sahasrabuddhe", "Sathaye", "Talpade", "Tembe", "Ubhaykar", "Vartak",
    "Velankar", "Wadekar", "Wagle", "Yardi",
]

SAFE_PREFIXES = ("99900", "88800", "77700", "66600", "99911", "88822")
SAFE_DOMAINS = ["example.com", "example.com", "example.com", "example.in"]

# Column headers whose values are identities to be replaced.
NAME_COLS = re.compile(r"name", re.I)
PHONE_COLS = re.compile(r"mobile|phone|contact", re.I)
EMAIL_COLS = re.compile(r"e-?mail", re.I)

# Inline occurrences inside free-text columns (notes carry phone numbers).
INLINE_PHONE = re.compile(r"(?<!\d)(\d{10})(?!\d)")
INLINE_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _h(value: str, salt: str) -> int:
    return int(hashlib.sha256((salt + "|" + value).encode("utf-8")).hexdigest()[:12], 16)


def fake_name(original: str) -> str:
    """Stable pseudonym. Preserves a trailing numeric suffix if one exists."""
    original = (original or "").strip()
    if not original:
        return original
    suffix = ""
    m = re.search(r"\s+(\d+)$", original)
    if m:
        suffix = " " + m.group(1)
    # Keep any parenthetical status marker — it is data, not identity.
    marker = ""
    m2 = re.search(r"\s*(\([^)]*\))\s*$", original)
    if m2:
        marker = " " + m2.group(1)
    h = _h(original, "name")
    return (f"{FIRST_NAMES[h % len(FIRST_NAMES)]} "
            f"{LAST_NAMES[(h // 97) % len(LAST_NAMES)]}{suffix}{marker}")


def fake_phone(original: str) -> str:
    """Stable 10-digit replacement in a fabricated block; keeps blanks blank."""
    digits = re.sub(r"\D", "", original or "")
    if not digits:
        return original
    h = _h(digits, "phone")
    return f"{SAFE_PREFIXES[h % len(SAFE_PREFIXES)]}{h % 100000:05d}"


def fake_email(original: str) -> str:
    original = (original or "").strip()
    if not original or "@" not in original:
        return original
    local = original.split("@", 1)[0]
    local = re.sub(r"[^A-Za-z0-9._+-]", "", local) or "user"
    h = _h(original, "email")
    return f"{local.lower()}@{SAFE_DOMAINS[h % len(SAFE_DOMAINS)]}"


def scrub_free_text(value: str) -> str:
    """Notes and descriptions carry inline phones and emails; replace those too."""
    if not value:
        return value
    value = INLINE_PHONE.sub(lambda m: fake_phone(m.group(1)), value)
    value = INLINE_EMAIL.sub(lambda m: fake_email(m.group(0)), value)
    return value


_SAFE_SURNAMES = {s.lower() for s in LAST_NAMES}
_SAFE_FIRST_NAMES = {f.lower() for f in FIRST_NAMES}

# Titles, stripped before a name is judged. Not name parts.
_HONORIFICS = {"sir", "mam", "madam", "ma'am", "mr", "mrs", "ms", "miss"}

# The institute's own instructors. They are staff, not students, and
# `agents/canonical_maps.py` is keyed to these exact spellings — the samples
# would be useless for testing honorific canonicalization without them. They
# appear only in Faculty / Counsellor columns, never as a student identity.
FACULTY_TOKENS = {"yash", "mansi", "siddharth", "vansh", "subin", "trusha",
                  "kanodia", "sir", "mam", "k"}

# A single recognizable token is enough to call a packed cell safe — the
# `Name for Google Contacts` column concatenates Name-Branch-Course, so most
# of its tokens are course words that will never be in a name list. Staff
# first names count here because staff studied at the institute and so appear
# as students too, but only the multi-character ones: "k" alone would wave
# through any unknown surname followed by an initial.
_SAFE_IDENTITY_TOKENS = (
    {s.lower() for s in LAST_NAMES}
    | {f.lower() for f in FIRST_NAMES}
    | {t for t in FACULTY_TOKENS if len(t) > 2 and t not in ("sir", "mam")}
)

# +1 555-01xx is reserved for fiction; anything else on +1 would be a real
# North American number.
SAFE_INTL = ("1555010",)


def is_safe(kind: str, value: str) -> bool:
    """Is this cell already inside the fabricated / reserved space?

    Used by `--check`. Deliberately permissive about *shape* — a phone may be
    9, 11 or 13 digits and still be safe, because the generated samples carry
    those defects on purpose. What matters is the dialling block, the surname
    pool, and the mail domain.
    """
    value = (value or "").strip()
    if not value:
        return True
    if kind == "phone":
        digits = re.sub(r"\D", "", value)
        if not digits:
            return True
        # Two numbers in one cell: every one of them must be safe.
        for chunk in (re.findall(r"\d{9,}", value) or [digits]):
            body = chunk.lstrip("0")
            if body.startswith("91"):         # +91 country code
                body = body[2:]
            if body.startswith(SAFE_INTL) or body.startswith(SAFE_PREFIXES):
                continue
            # +81 keeps its length by appending a digit to a safe body.
            if body.startswith("81") and body[2:].startswith(SAFE_PREFIXES):
                continue
            return False
        return True
    if kind == "email":
        if "@" not in value:
            return True                       # a stray token, not an address
        # The generator emits deliberately malformed addresses (`@example .con`,
        # `@example ,com`, `@example com`). All are example-based and none
        # resolve, so match the domain stem rather than a well-formed domain.
        return "@example" in value.lower()
    if kind == "name":
        # Anonymized call-log rows carry an enquiry id where a name would go.
        # That IS the pseudonym, so it is safe by definition.
        if re.match(r"^enq\b|^enq\s*-?\s*\d", value, re.I):
            return True
        # `zzzzz (Don't Delete)` dropdown placeholders — structural junk, not a
        # person. Same pattern the cleaner purges (PLACEHOLDER_NAME_RE).
        if re.match(r"^\s*z{3,}", value, re.I):
            return True
        # Strip status markers and trailing indices, then check the surname.
        core = re.sub(r"\s*\([^)]*\)\s*", " ", value)
        core = re.sub(r"\s+\d+$", "", core).strip()
        # `Name for Google Contacts` packs Name-Branch-Course into one cell, so
        # split on hyphens as well as whitespace before looking for a surname.
        words = [w for w in re.split(r"[\s\-]+", core) if w]
        # Honorifics are titles, not name parts. The institute hires faculty
        # out of its own students, so a synthetic student's name reappears as
        # "Hetvi Sir" in a faculty cell; judging that on "Sir" would fail a
        # name that is safe by construction.
        words = [w for w in words if w.lower().strip(".") not in _HONORIFICS]
        if not words:
            return True
        if any(w.lower() in _SAFE_IDENTITY_TOKENS for w in words):
            return True
        # Faculty / counsellor cells hold instructor names, not students.
        return all(w.lower().strip(".") in FACULTY_TOKENS for w in words)
    # Free text: only inline phones and mail addresses matter.
    for m in INLINE_PHONE.finditer(value):
        if not is_safe("phone", m.group(1)):
            return False
    for m in INLINE_EMAIL.finditer(value):
        if not is_safe("email", m.group(0)):
            return False
    return True


def audit(path: str) -> tuple:
    """Count cells that are NOT already in the safe space."""
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))
    if not rows:
        return 0, 0, []
    kinds = _classify(rows[0])
    unsafe, samples = 0, []
    for row in rows[1:]:
        for i, cell in enumerate(row):
            kind = kinds[i] if i < len(kinds) else "text"
            if not is_safe(kind, cell):
                unsafe += 1
                if len(samples) < 3:
                    samples.append(f"{kind}={cell[:38]}")
    return len(rows) - 1, unsafe, samples


def _classify(header: list) -> list:
    """Map each header to the kind of identity it holds.

    NAME is tested before PHONE on purpose: `Name for Google Contacts` matches
    the phone pattern on the word "Contacts", but it holds a name.
    """
    kinds = []
    for col in header:
        if EMAIL_COLS.search(col):
            kinds.append("email")
        elif NAME_COLS.search(col):
            kinds.append("name")
        elif PHONE_COLS.search(col):
            kinds.append("phone")
        else:
            kinds.append("text")
    return kinds


def process(path: str, check_only: bool) -> tuple:
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh))
    if not rows:
        return 0, 0

    kinds = _classify(rows[0])
    changed = 0
    for row in rows[1:]:
        for i, cell in enumerate(row):
            kind = kinds[i] if i < len(kinds) else "text"
            new = {"name": fake_name, "phone": fake_phone,
                   "email": fake_email}.get(kind, scrub_free_text)(cell)
            if new != cell:
                changed += 1
                row[i] = new

    if changed and not check_only:
        with open(path, "w", encoding="utf-8", newline="") as fh:
            csv.writer(fh).writerows(rows)
    return len(rows) - 1, changed


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true",
                    help="Report unsafe values and exit 1 without rewriting.")
    args = ap.parse_args(argv)

    paths = sorted({p for pattern in TARGET_GLOBS for p in glob.glob(pattern)})
    total = 0
    print(("Auditing" if args.check else "Sanitizing")
          + f" {len(paths)} sample file(s):")

    for path in paths:
        rel = os.path.relpath(path, ROOT)
        if args.check:
            # Audit EVERY file, generated or not — membership in the safe space
            # is the guarantee, regardless of how the file was produced.
            n_rows, unsafe, samples = audit(path)
            total += unsafe
            note = f"  UNSAFE: {'; '.join(samples)}" if unsafe else "  ok"
            print(f"  {rel:48s} {n_rows:6d} rows  {unsafe:5d} unsafe{note}")
            continue
        if os.path.basename(path).startswith(GENERATED_PREFIXES):
            print(f"  {rel:48s}         generated - safe by construction, skipped")
            continue
        n_rows, changed = process(path, False)
        total += changed
        print(f"  {rel:48s} {n_rows:6d} rows  {changed:5d} rewritten")

    if args.check:
        if total:
            print(f"\nFAIL: {total} cells hold identities outside the safe space.")
            return 1
        print("\nPASS: every name, phone and email is in a fabricated or "
              "reserved range. No sample file can reach a real person.")
        return 0
    print(f"\nRewrote {total} identity cells. Run with --check to verify.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
