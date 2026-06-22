#!/usr/bin/env python
"""CLI glue for the FV-Institute analysis plugin.

One command runs the full 8-agent pipeline on a sheet and writes the standalone
HTML report. This is the single entry the Claude Code plugin shells into, so the
same flow works on any PC after `pip install -r requirements.txt`.

Usage:
    python scripts/run_pipeline.py --csv data/15_Year_Admissions_Realistic.csv \
        --question "How is admission conversion by branch?" --out report.html

Exit codes: 0 success, 1 pipeline did not complete, 2 bad arguments.

The orchestrator masks PII (only Agent 2 sees raw PII); ReportAgent additionally
asserts no 10-digit mobile survives in the HTML before it is written.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agents.orchestrator_agent import OrchestratorAgent  # noqa: E402
from agents.report_agent import ReportAgent  # noqa: E402


def wrap_goal(question: str) -> dict:
    """Wrap a free-text question in a minimal ready /goal so Agent 1 doesn't block
    the run on clarifications. The question itself is preserved verbatim."""
    return {
        "user_question": question,
        "goal": {
            "project_id": "INST-FV-UI",
            "project_name": "FV Institute Operations",
            "stakeholder": {"department": "admissions", "requested_by": "Operator",
                            "priority": "high"},
            "analysis_request": {"business_problem": question,
                                 "analysis_type": ["diagnostic", "monitoring"]},
            "time_window": {"start_date": "2010-01-01", "end_date": "2025-12-31",
                            "granularity": "month"},
            "modules": {"admissions": {"enabled": True, "metrics": [], "dimensions": []}},
            "kpi_targets": {"admission_growth_percent": 10, "fee_collection_percent": 90,
                            "course_completion_percent": 80,
                            "certificate_completion_percent": 85,
                            "student_satisfaction_score": 4.2},
            "success_criteria": ["Surface the widest performance gaps"],
            "alerts": {"low_admission_alert": True, "pending_fee_alert": True,
                       "dropout_alert": True, "negative_review_alert": True},
        },
    }


def _render_from_json(path: str, out: str) -> int:
    """Render-only: rebuild the HTML report from a saved run JSON (no pipeline)."""
    with open(path, encoding="utf-8") as fh:
        saved = json.load(fh)
    report = ReportAgent().run(
        saved.get("final_report") or {}, saved.get("question_results") or [],
    )
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(report["html"])
    print(f"Report written: {os.path.abspath(out)} (from {path})")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run the FV analysis pipeline -> HTML report.")
    ap.add_argument("--csv", help="Source CSV/sheet path (omit with --from-json).")
    ap.add_argument("--question", default="How is admission conversion performing by branch?",
                    help="Business question (free text or a /goal JSON string).")
    ap.add_argument("--out", default="report.html", help="Output HTML path.")
    ap.add_argument("--registry", default=None, help="Monitoring registry JSON path.")
    ap.add_argument("--max-questions", type=int, default=3,
                    help="Cap on business questions analyzed.")
    ap.add_argument("--json", default=None,
                    help="Path to also dump the run JSON (final_report + question_results).")
    ap.add_argument("--from-json", default=None,
                    help="Render the report from a saved run JSON; skips the pipeline.")
    args = ap.parse_args(argv)

    if args.from_json:
        if not os.path.exists(args.from_json):
            print(f"error: JSON not found: {args.from_json}", file=sys.stderr)
            return 2
        return _render_from_json(args.from_json, args.out)

    if not args.csv:
        print("error: --csv is required (or use --from-json)", file=sys.stderr)
        return 2
    if not os.path.exists(args.csv):
        print(f"error: CSV not found: {args.csv}", file=sys.stderr)
        return 2

    goal = wrap_goal(args.question.strip())
    state = OrchestratorAgent().run(
        goal, args.csv, registry_path=args.registry, max_questions=args.max_questions,
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
            json.dump({"final_report": state["final_report"],
                       "question_results": state["question_results"]},
                      fh, indent=2, default=str)

    fr = state["final_report"]
    print(f"Report written: {os.path.abspath(args.out)}")
    print(f"  questions answered: {fr.get('questions_answered')}"
          f" · skipped: {fr.get('questions_skipped')}")
    mon = fr.get("monitoring") or {}
    print(f"  monitoring health: {mon.get('health')}"
          f" · active alerts: {mon.get('active_alerts')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
