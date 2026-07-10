#!/usr/bin/env python
"""CLI glue for the FV-Institute analysis plugin."""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agents.orchestrator_agent import OrchestratorAgent  # noqa: E402
from agents.report_agent import ReportAgent  # noqa: E402


def wrap_goal(question: str) -> dict:
    """Wrap a free-text question in a minimal ready /goal."""
    return {
        "user_question": question,
        "goal": {
            "project_id": "INST-FV-UI",
            "project_name": "FV Institute Operations",
            "stakeholder": {
                "department": "admissions",
                "requested_by": "Operator",
                "priority": "high",
            },
            "analysis_request": {
                "business_problem": question,
                "analysis_type": ["diagnostic", "monitoring"],
            },
            "time_window": {
                "start_date": "2010-01-01",
                # End a year out so current + near-future dated rows are in-window
                # (a hardcoded past year silently filters out this-year data).
                "end_date": f"{datetime.date.today().year + 1}-12-31",
                "granularity": "month",
            },
            # No hardcoded module — let Problem Definition infer enabled modules
            # from the question (so "fees / completion / certificates" questions
            # actually enable those modules instead of admissions-only).
            "kpi_targets": {
                "admission_growth_percent": 10,
                "fee_collection_percent": 90,
                "course_completion_percent": 80,
                "certificate_completion_percent": 85,
                "student_satisfaction_score": 4.2,
            },
            "success_criteria": ["Surface the widest performance gaps"],
            "alerts": {
                "low_admission_alert": True,
                "pending_fee_alert": True,
                "dropout_alert": True,
                "negative_review_alert": True,
            },
        },
    }


def _render_from_json(path: str, out: str) -> int:
    """Render-only: rebuild the HTML report from a saved run JSON."""
    with open(path, encoding="utf-8") as fh:
        saved = json.load(fh)
    report = ReportAgent().run(
        saved.get("final_report") or {}, saved.get("question_results") or [],
    )
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(report["html"])
    print(f"Report written: {os.path.abspath(out)} (from {path})")
    return 0


def _run_clean_only(csv_path: str, data_sources, out: str) -> int:
    """Data Engineer only: clean + PII-mask, write a canonical CSV + quality."""
    import pandas as pd

    from agents.data_engineer_agent import DataEngineerAgent

    engine = DataEngineerAgent()
    if data_sources:
        pkg = engine.run_sources({}, data_sources)
    else:
        pkg = engine.run({}, csv_path)

    if pkg.get("status") == "blocked":
        reason = (pkg.get("quality_report") or {}).get("known_issues") or [
            pkg.get("reason", "cleaning blocked")
        ]
        print("error: cleaning blocked", file=sys.stderr)
        print(json.dumps(reason, indent=2, default=str), file=sys.stderr)
        return 1

    # The canonical frame is already PII-masked (names/mobiles/email hashed).
    df = pd.read_parquet(pkg["canonical_df_path"])
    df.to_csv(out, encoding="utf-8", index=False)

    qr = pkg.get("quality_report") or {}
    issues = list(qr.get("known_issues") or [])
    # Multi-source keeps per-sheet notes (incl. PII masking) in source_packages.
    for sp in pkg.get("source_packages") or []:
        issues.extend((sp.get("quality_report") or {}).get("known_issues") or [])
    masked = sum(1 for s in issues if "mask" in str(s).lower() or "hash" in str(s).lower())
    print(f"Cleaned CSV written: {os.path.abspath(out)}")
    print(f"  rows: {pkg.get('row_count')} - columns: {len(df.columns)}")
    print(f"  roles mapped: {len(pkg.get('canonical_columns') or {})}"
          f" - PII fields masked: {masked}")
    if issues:
        print(f"  quality notes ({len(issues)}):")
        for note in issues[:15]:
            print(f"    - {note}")
        if len(issues) > 15:
            print(f"    ... (+{len(issues) - 15} more)")
    return 0


def _build_data_sources(args) -> list:
    """Build normalized source descriptors for Excel sheets and named CSVs."""
    sources = []
    if args.excel:
        if not os.path.exists(args.excel):
            raise FileNotFoundError(f"Excel workbook not found: {args.excel}")
        import pandas as pd

        workbook = pd.ExcelFile(args.excel, engine="openpyxl")
        for sheet in workbook.sheet_names:
            sample = pd.read_excel(args.excel, sheet_name=sheet, nrows=1, engine="openpyxl")
            if sample.empty and len(sample.columns) == 0:
                continue
            sources.append({
                "name": _safe_source_name(sheet),
                "type": "excel_sheet",
                "path": args.excel,
                "path_or_query": args.excel,
                "sheet_name": sheet,
                "domain": _infer_domain(sheet, list(sample.columns)),
            })
    for item in args.source or []:
        if "=" not in item:
            raise ValueError("--source must be in name=path.csv format")
        name, path = item.split("=", 1)
        if not os.path.exists(path):
            raise FileNotFoundError(f"Source CSV not found: {path}")
        sources.append({
            "name": _safe_source_name(name),
            "type": "csv",
            "path": path,
            "path_or_query": path,
            "domain": _infer_domain(name + " " + os.path.basename(path), []),
        })
    for item in args.sheet_url or []:
        sources.append(_ingest_sheet_url(item))
    return sources


def _ingest_sheet_url(item: str) -> dict:
    """Fetch a published Google Sheet URL to a dated CSV snapshot -> csv source.

    Accepts `URL` or `name=URL`. The snapshot pins exactly the bytes analyzed.
    """
    import pandas as pd

    from agents.ingestion import ingest_gsheet

    if "=" in item and not item.split("=", 1)[0].lower().startswith("http"):
        name, url = item.split("=", 1)
    else:
        name, url = "", item
    url = url.strip()
    name = _safe_source_name(name) if name else "google_sheet"
    snapshot = ingest_gsheet(url, name)
    columns = list(pd.read_csv(snapshot, nrows=1).columns)
    return {
        "name": name,
        "type": "csv",
        "path": snapshot,
        "path_or_query": snapshot,
        "domain": _infer_domain(name, columns),
    }


def _safe_source_name(name: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(name)).strip()
    return cleaned or "source"


def _infer_domain(name: str, columns: list) -> str:
    text = " ".join([str(name)] + [str(c) for c in columns]).lower()
    rules = [
        ("finance", ("fee", "fees", "payment", "paid", "pending", "amount",
                     "invoice", "revenue", "collection")),
        ("certificate", ("certificate", "issue date", "certificate number")),
        ("student", ("student-id", "student id", "phone", "secondary contact",
                     "date of joining", "mode")),
        ("admission", ("admission", "preferred branch", "receipt id",
                       "from where", "which course")),
        ("marketing", ("campaign", "lead source", "channel", "source")),
        ("operations", ("faculty", "batch", "branch", "status")),
    ]
    for domain, words in rules:
        if any(word in text for word in words):
            return domain
    return "unknown"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run the FV analysis pipeline -> HTML report.")
    ap.add_argument("--csv", help="Source CSV/sheet path (omit with --from-json).")
    ap.add_argument("--excel", help="Excel workbook path; each non-empty sheet is a source.")
    ap.add_argument("--source", action="append", default=[],
                    help="Additional named CSV source as name=path.csv; repeatable.")
    ap.add_argument("--sheet-url", action="append", default=[], dest="sheet_url",
                    help="Published Google Sheet URL (or name=URL); snapshotted to "
                         "data/ingest as CSV. Repeatable.")
    ap.add_argument("--question", default="How is admission conversion performing by branch?",
                    help="Business question (free text or a /goal JSON string).")
    ap.add_argument("--out", default="report.html", help="Output HTML path.")
    ap.add_argument("--registry", default=None, help="Monitoring registry JSON path.")
    ap.add_argument("--max-questions", type=int, default=3,
                    help="Cap on business questions analyzed.")
    ap.add_argument("--json", default=None,
                    help="Path to also dump the run JSON.")
    ap.add_argument("--from-json", default=None,
                    help="Render the report from a saved run JSON; skips the pipeline.")
    ap.add_argument("--clean-only", action="store_true",
                    help="Run Data Engineer only: clean + PII-mask the sheet(s) and "
                         "write a canonical CSV + quality report. No analysis.")
    ap.add_argument("--clean-out", default="cleaned.csv",
                    help="Output CSV path for --clean-only (default cleaned.csv).")
    args = ap.parse_args(argv)

    if args.from_json:
        if not os.path.exists(args.from_json):
            print(f"error: JSON not found: {args.from_json}", file=sys.stderr)
            return 2
        return _render_from_json(args.from_json, args.out)

    multi_requested = bool(args.excel or args.source or args.sheet_url)
    if args.csv and multi_requested:
        print("error: use either --csv or --excel/--source/--sheet-url, not both",
              file=sys.stderr)
        return 2
    if not args.csv and not multi_requested:
        print("error: --csv, --excel, --source, or --sheet-url is required "
              "(or use --from-json)", file=sys.stderr)
        return 2
    if args.csv and not os.path.exists(args.csv):
        print(f"error: CSV not found: {args.csv}", file=sys.stderr)
        return 2

    data_sources = None
    csv_path = args.csv
    if multi_requested:
        try:
            data_sources = _build_data_sources(args)
        except Exception as exc:  # noqa: BLE001 - invalid CLI input
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if not data_sources:
            print("error: no usable sources found", file=sys.stderr)
            return 2
        csv_path = data_sources[0].get("path_or_query", "")

    if args.clean_only:
        return _run_clean_only(csv_path, data_sources, args.clean_out)

    goal = wrap_goal(args.question.strip())
    state = OrchestratorAgent().run(
        goal, csv_path, data_sources=data_sources,
        registry_path=args.registry, max_questions=args.max_questions,
    )

    if state.get("status") != "complete":
        msg = state.get("message", "pipeline did not complete")
        print(f"error: {msg}", file=sys.stderr)
        detail = state.get("detail")
        if detail:
            print(json.dumps(detail, indent=2, default=str), file=sys.stderr)
        return 1

    report = state.get("report") or {}
    html = report.get("html")
    if not html:
        print("error: pipeline completed but no report was produced", file=sys.stderr)
        return 1

    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(html)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({
                "final_report": state["final_report"],
                "question_results": state["question_results"],
            }, fh, indent=2, default=str)

    fr = state["final_report"]
    print(f"Report written: {os.path.abspath(args.out)}")
    print(f"  questions answered: {fr.get('questions_answered')}"
          f" - skipped: {fr.get('questions_skipped')}")
    mon = fr.get("monitoring") or {}
    print(f"  monitoring health: {mon.get('health')}"
          f" - active alerts: {mon.get('active_alerts')}")
    ms = fr.get("multi_source_summary") or {}
    if ms:
        print(f"  sources: {ms.get('source_count')} - joined: {ms.get('joined_count')}"
              f" - unjoined: {ms.get('unjoined_count')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
