#!/usr/bin/env python
"""Generate coherent sample sheets under samples/ for testing the pipeline.

All PII is fake. The rows are wired to exercise every feature: a repeat student
(101 re-enrolls as 105), one converted + one lost lead, a fee reconciliation
mismatch (103), a defaulter with pending balance (102/103), a duplicate
certificate serial (FV-1001 on two rows), and all three completion labels
(completed / not_coming / active). See docs/sheet_schema_guide.md.
"""

from __future__ import annotations

import csv
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "samples")


def _write(name: str, header: list, rows: list) -> None:
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, name)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)
    print(f"wrote {path} ({len(rows)} rows)")


def main() -> None:
    # 1. Students (master) — one row per enrollment. 105 is 101 re-enrolling.
    _write(
        "students.csv",
        ["Student-ID", "Student Name", "Mobile No (Student)", "Email",
         "Date of Admission", "Which Course", "Branch", "Faculty", "From Where"],
        [
            [101, "Aarav Shah", "9820011111", "aarav@example.com",
             "2025-01-10", "Tally", "Adajan", "Mansi Mam", "Google"],
            [102, "Isha Patel", "9820022222", "isha@example.com",
             "2025-02-05", "DTP", "Pal", "Yash Sir", "Instagram"],
            [103, "Rohan Mehta", "9820033333", "rohan@example.com",
             "2025-03-12", "Tally", "Adajan", "Mansi Mam", "Walk-in"],
            [105, "Aarav Shah", "9820011111", "aarav@example.com",
             "2025-06-01", "Python", "Adajan", "Kiran Sir", "Referral"],
        ],
    )

    # 2. Enquiries — 101/102/103 convert (admitted on Students); 104 is Lost.
    _write(
        "enquiries.csv",
        ["Timestamp", "Student Name", "Mobile No (Student)", "Which Course",
         "Preferred Branch", "From Where", "Date of Admission", "Status"],
        [
            ["2025-01-02", "Aarav Shah", "9820011111", "Tally",
             "Adajan", "Google", "2025-01-10", "Converted"],
            ["2025-01-28", "Isha Patel", "9820022222", "DTP",
             "Pal", "Instagram", "2025-02-05", "Converted"],
            ["2025-03-01", "Rohan Mehta", "9820033333", "Tally",
             "Adajan", "Walk-in", "2025-03-12", "Converted"],
            ["2025-04-15", "Neha Verma", "9820044444", "DTP",
             "Pal", "Instagram", "", "Lost"],
        ],
    )

    # 3. Fees ledger — receipts (many per student). 103 has a refund row.
    _write(
        "fees_ledger.csv",
        ["Student-ID", "Receipt ID", "Date of Receipt", "Paid Amt",
         "Mode of Payment", "Description"],
        [
            [101, "R1", "2025-01-10", 6000, "cash", "cash at desk"],
            [101, "R2", "2025-02-10", 6000, "upi", "razorpay upi"],
            [102, "R3", "2025-02-05", 4000, "upi", "paid to ICICI"],
            [103, "R4", "2025-03-12", 5000, "cash", "cash at desk"],
            [103, "R6", "2025-04-01", 1000, "upi", "1000 refunded"],
            [105, "R5", "2025-06-01", 8000, "bank transfer", "razorpay emi"],
        ],
    )

    # 4. Fees rollup — one row per enrollment. 102/103 owe; 103 also mismatches
    #    the ledger (net paid 4000 vs total 8000 - pending 3000 => gap 1000).
    _write(
        "fees_rollup.csv",
        ["Student-ID", "Total Fees", "Amt Pending", "Mode of Payment"],
        [
            [101, 12000, 0, "upi"],
            [102, 10000, 6000, "upi"],
            [103, 8000, 3000, "cash"],
            [105, 8000, 0, "bank transfer"],
        ],
    )

    # 5. Certificates — FV-1001 appears twice (duplicate serial -> flagged).
    _write(
        "certificates.csv",
        ["Student-ID", "Student Name", "Certificate Number",
         "Certificate Issue Date", "Date of Joining", "Which Course"],
        [
            [101, "Aarav Shah", "FV-1001", "2025-05-15", "2025-01-10", "Tally"],
            [103, "Rohan Mehta", "FV-1001", "2025-05-20", "2025-03-12", "Tally"],
            [105, "Aarav Shah", "FV-1002", "", "2025-06-01", "Python"],
        ],
    )

    # 6. Timetable workbook tabs — the tab/source NAME is the completion label.
    _write(
        "timetable_Course_Completed.csv",
        ["Student-ID", "Student Name", "Which Course", "Faculty", "Batch Timing"],
        [[101, "Aarav Shah", "Tally", "Mansi Mam", "10-12"]],
    )
    _write(
        "timetable_Not_Coming.csv",
        ["Student-ID", "Student Name", "Which Course", "Faculty",
         "Batch Timing", "Status & reason"],
        [[102, "Isha Patel", "DTP", "Yash Sir", "4-6",
          "shifted to Mumbai, not coming, call 9820022222"]],
    )
    _write(
        "timetable_Main_data.csv",
        ["Student-ID", "Student Name", "Which Course", "Faculty", "Batch Timing"],
        [
            [103, "Rohan Mehta", "Tally", "Mansi Mam", "10-12"],
            [105, "Aarav Shah", "Python", "Kiran Sir", "6-8"],
        ],
    )


if __name__ == "__main__":
    main()
