"""Agent 8: Report Writer.

Turns one completed pipeline run into a single, shareable, standalone **HTML
report**. It is a *composition* agent: it reads ONLY the JSON contracts the
Orchestrator already produced (`final_report` + `question_results`) — never the
dataframe — and assembles a styled document. Charts are the exact Chart.js configs
the Visualization Agent emitted, rendered in-browser via the Chart.js CDN.

LLM boundary (identical to Insights/Recommendation):
- The LLM only *phrases* prose narratives, grounded ONLY in facts already computed
  upstream. Every number in the HTML comes from the structured input, not the model.
- Every narrative is wrapped `if not available: deterministic` /
  `try ... except LLMUnavailable: deterministic` / `except Exception: deterministic`,
  so with no key (or any API/parse failure) the report is produced unchanged.

PII boundary: the upstream df is already masked, but as defence in depth this agent
asserts no 10-digit mobile pattern survives in the rendered HTML before returning,
mirroring the server's `_MOBILE_RE` rule.
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence

from . import llm_client


JsonDict = Dict[str, Any]

# Mirrors ui/src/mock/style_guide.json — the project's captured design tokens.
DEFAULT_STYLE: JsonDict = {
    "palette": {
        "primary": "#1E40AF", "secondary": "#3B82F6", "accent": "#D97706",
        "success": "#16A34A", "warning": "#F59E0B", "danger": "#DC2626",
        "neutral": "#64748B", "grid": "#E9EEF6",
    },
    "font_family": "Fira Sans",
    "number_font": "Fira Code",
}

CHARTJS_CDN = "https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"
_MOBILE_RE = re.compile(r"\b\d{10}\b")


class PIILeakError(RuntimeError):
    """Raised if a 10-digit mobile pattern survives into the rendered HTML."""


class ReportAgent:
    """Composes a standalone HTML report from a completed pipeline run."""

    def run(
        self,
        final_report: Mapping[str, Any],
        question_results: Optional[Sequence[Mapping[str, Any]]] = None,
        *,
        brief: Optional[Mapping[str, Any]] = None,
        style: Optional[Mapping[str, Any]] = None,
        title: Optional[str] = None,
        live: bool = False,
    ) -> JsonDict:
        """Build the report. Returns {status, html, narrative, generated_at}.

        Args:
            final_report: the Orchestrator's assembled roll-up (headline_findings,
                top_recommendations, monitoring, data_quality, decision_supported).
            question_results: per-question contracts (analysis/visual/insight). Used
                for the richer per-question body and for chart embedding. Optional;
                without it the body falls back to final_report.headline_findings.
            brief: optional ProblemDefinitionBrief, used only to title the report.
            style: design tokens; defaults to DEFAULT_STYLE.
            title: report title; defaults from the brief or a generic line.
            live: True if this is a live run (vs. captured demo) — shown in header.
        """
        fr = final_report or {}
        qresults = list(question_results or [])
        st = dict(DEFAULT_STYLE)
        if style:
            st.update(style)
        generated_at = datetime.now(timezone.utc).isoformat()
        rtitle = title or self._title(brief)

        narrative = {
            "executive": self._exec_narrative(fr),
            "recommendations": self._recs_narrative(fr),
            "monitoring": self._monitoring_narrative(fr),
        }

        html_doc = self._render(fr, qresults, narrative, st, rtitle, generated_at, live)

        # Defence in depth: never let a raw mobile number escape in the document.
        if _MOBILE_RE.search(html_doc):
            raise PIILeakError("PII leak detected in report HTML; output withheld.")

        return {
            "status": "success",
            "html": html_doc,
            "narrative": narrative,
            "generated_at": generated_at,
        }

    # =============================================================== narratives

    def _exec_narrative(self, fr: Mapping[str, Any]) -> str:
        """Executive summary paragraph. Deterministic always; LLM rewrites it
        grounded only in the same facts."""
        findings = fr.get("headline_findings") or []
        mon = fr.get("monitoring") or {}
        decision = fr.get("decision_supported")
        answered = fr.get("questions_answered", len(findings))
        skipped = fr.get("questions_skipped", 0)

        bits: List[str] = []
        if decision:
            bits.append(f"This analysis supports the decision to {decision.lower()}.")
        bits.append(
            f"{answered} business question(s) were answered"
            + (f", {skipped} skipped as not computable" if skipped else "") + "."
        )
        for f in findings[:3]:
            metric = self._pretty(f.get("metric"))
            val = f.get("value")
            if metric and val is not None:
                bits.append(f"{metric} stands at {self._fmt_num(val)}.")
        if mon.get("health"):
            bits.append(
                f"Monitoring health is {mon.get('health')} "
                f"with {mon.get('active_alerts', 0)} active alert(s)."
            )
        deterministic = " ".join(bits) if bits else "No headline findings available."

        facts = {
            "decision_supported": decision,
            "questions_answered": answered,
            "questions_skipped": skipped,
            "headline_findings": [
                {"metric": self._pretty(f.get("metric")),
                 "value": self._fmt_num(f.get("value")),
                 "summary": f.get("executive_summary")}
                for f in findings[:5]
            ],
            "monitoring_health": mon.get("health"),
            "active_alerts": mon.get("active_alerts", 0),
        }
        return self._llm_or_default(
            deterministic, facts,
            "You are a business analyst writing the executive summary of an "
            "analytics report for an education institute's leadership.",
            "Write 3-5 sentences of plain executive prose, no bullet points, no "
            "preamble. Lead with the decision being supported, then the headline "
            "metrics, then the monitoring posture.",
        )

    def _recs_narrative(self, fr: Mapping[str, Any]) -> str:
        recs = fr.get("top_recommendations") or []
        if not recs:
            return "No recommendations were generated for this run."
        buckets: Dict[str, int] = {}
        for r in recs:
            b = r.get("priority_bucket", "P?")
            buckets[b] = buckets.get(b, 0) + 1
        spread = ", ".join(f"{k}: {v}" for k, v in sorted(buckets.items()))
        top = recs[0].get("action", "")
        deterministic = (
            f"{len(recs)} recommendation(s) were proposed (by priority {spread}). "
            f"The top action is: {top}"
        )
        facts = {
            "count": len(recs),
            "priority_spread": buckets,
            "actions": [
                {"action": r.get("action"), "owner": r.get("owner_role"),
                 "priority_bucket": r.get("priority_bucket"),
                 "timeline": r.get("timeline")}
                for r in recs[:8]
            ],
        }
        return self._llm_or_default(
            deterministic, facts,
            "You are summarizing prioritized recommendations for institute leadership.",
            "Write 2-3 sentences framing what the recommendations collectively call "
            "for and the highest-priority action. No bullet points, no new actions.",
        )

    def _monitoring_narrative(self, fr: Mapping[str, Any]) -> str:
        mon = fr.get("monitoring") or {}
        health = mon.get("health")
        events = mon.get("events") or []
        if not health and not events:
            return "Monitoring produced no events this run."
        deterministic = (
            f"Monitoring health is {health or 'unknown'} with "
            f"{mon.get('active_alerts', 0)} active alert(s) and "
            f"{len(events)} event(s)."
        )
        facts = {
            "health": health,
            "active_alerts": mon.get("active_alerts", 0),
            "events": [
                {"metric": self._pretty(e.get("metric")),
                 "type": e.get("event_type"), "severity": e.get("severity"),
                 "impact": e.get("impact"),
                 "next_step": e.get("recommended_next_step")}
                for e in events[:6]
            ],
        }
        return self._llm_or_default(
            deterministic, facts,
            "You are summarizing KPI monitoring results for institute leadership.",
            "Write 1-3 sentences on the overall health and what the events warrant. "
            "No bullet points, no invented metrics.",
        )

    def _llm_or_default(
        self, deterministic: str, facts: JsonDict, system: str, instruction: str,
    ) -> str:
        """Shared LLM-phrasing wrapper. Falls back to `deterministic` on any failure.
        The model is fed ONLY `facts` (already-computed numbers) — it phrases, it does
        not compute."""
        if not llm_client.available():
            return deterministic
        try:
            prompt = (
                f"{system}\n\n"
                "Use ONLY the facts in this JSON. Do NOT invent any number, "
                "percentage, segment, name, or cause that is not present here. If a "
                "list is empty, omit that angle.\n\n"
                f"FACTS:\n{json.dumps(facts, ensure_ascii=False, default=str)}\n\n"
                f"{instruction} Return only the prose."
            )
            text = llm_client.complete_text(prompt, max_tokens=500, temperature=0.3)
            cleaned = text.strip().strip('"').strip()
            return cleaned if len(cleaned) >= 20 else deterministic
        except llm_client.LLMUnavailable:
            return deterministic
        except Exception:  # noqa: BLE001 - phrasing must never break the report
            return deterministic

    # ================================================================== render

    def _render(self, fr, qresults, narrative, st, title, generated_at, live) -> str:
        pal = st.get("palette", {})
        body: List[str] = []
        body.append(self._header(title, fr, generated_at, live))
        body.append(self._exec_section(narrative["executive"]))
        body.append(self._kpi_strip(qresults, fr))

        chart_scripts: List[str] = []
        question_blocks: List[str] = []
        for q in qresults:
            block, scripts = self._question_block(q)
            question_blocks.append(block)
            chart_scripts.extend(scripts)
        if question_blocks:
            body.append("<section class='qsection'><h2>Findings by question</h2>"
                        + "".join(question_blocks) + "</section>")

        body.append(self._recs_section(fr, narrative["recommendations"]))
        body.append(self._monitoring_section(fr, narrative["monitoring"]))
        body.append(self._data_quality_footer(fr))

        css = self._css(st, pal)
        script = ""
        if chart_scripts:
            script = (
                f'<script src="{CHARTJS_CDN}"></script>\n'
                "<script>document.addEventListener('DOMContentLoaded',function(){\n"
                + "\n".join(chart_scripts) + "\n});</script>"
            )
        return (
            "<!DOCTYPE html>\n<html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            f"<title>{html.escape(title)}</title>\n{css}\n</head>\n<body>"
            f"<main class='report'>{''.join(body)}</main>\n{script}\n</body></html>"
        )

    def _header(self, title, fr, generated_at, live) -> str:
        decision = fr.get("decision_supported") or ""
        badge = ("<span class='badge live'>● live run</span>" if live
                 else "<span class='badge demo'>○ demo data</span>")
        sub = (f"<p class='decision'>{html.escape(str(decision))}</p>"
               if decision else "")
        return (
            "<header class='hero'>"
            f"<div class='hero-top'><h1>{html.escape(title)}</h1>{badge}</div>"
            f"{sub}"
            f"<p class='meta'>Generated {html.escape(generated_at)}</p>"
            "</header>"
        )

    def _exec_section(self, text) -> str:
        return ("<section class='exec'><h2>Executive summary</h2>"
                f"<p>{html.escape(text)}</p></section>")

    def _kpi_strip(self, qresults, fr) -> str:
        cards: List[str] = []
        seen = set()
        for q in qresults:
            for c in ((q.get("visual") or {}).get("kpi_cards") or []):
                key = (c.get("metric"), c.get("value"))
                if key in seen:
                    continue
                seen.add(key)
                cards.append(
                    "<div class='kpi'>"
                    f"<div class='kpi-metric'>{html.escape(str(c.get('metric','')))}</div>"
                    f"<div class='kpi-value num'>{html.escape(str(c.get('value','')))}</div>"
                    f"<div class='kpi-conf'>{html.escape(str(c.get('confidence','')))} confidence</div>"
                    "</div>"
                )
        if not cards:
            # Fall back to final_report headline metrics.
            for f in (fr.get("headline_findings") or [])[:4]:
                cards.append(
                    "<div class='kpi'>"
                    f"<div class='kpi-metric'>{html.escape(self._pretty(f.get('metric')))}</div>"
                    f"<div class='kpi-value num'>{html.escape(self._fmt_num(f.get('value')))}</div>"
                    "</div>"
                )
        if not cards:
            return ""
        return f"<section class='kpis'>{''.join(cards)}</section>"

    def _question_block(self, q) -> (str, List[str]):
        qid = html.escape(str(q.get("question_id", "")))
        qtext = html.escape(str(q.get("question", "")))
        status = q.get("status")
        if status != "ok":
            reason = html.escape(str(q.get("skip_reason") or "not computable on this data"))
            return (
                f"<article class='qblock skipped'><h3>{qid}: {qtext}</h3>"
                f"<p class='empty'>Skipped — {reason}. No chart is shown rather than "
                "fabricate one.</p></article>", []
            )

        insight = q.get("insight") or {}
        parts = [f"<article class='qblock'><h3>{qid}: {qtext}</h3>"]
        summ = insight.get("executive_summary")
        if summ:
            parts.append(f"<p class='qsummary'>{html.escape(str(summ))}</p>")

        findings = insight.get("key_findings") or []
        if findings:
            items = "".join(
                f"<li>{html.escape(str(self._text_of(f, 'finding')))}</li>"
                for f in findings[:5]
            )
            parts.append(f"<h4>Key findings</h4><ul>{items}</ul>")

        risks = insight.get("risks") or []
        if risks:
            items = "".join(
                f"<li><span class='sev'>{html.escape(str(r.get('severity','')))}</span> "
                f"{html.escape(str(self._text_of(r, 'risk')))}</li>"
                for r in risks[:4]
            )
            parts.append(f"<h4>Risks</h4><ul class='risks'>{items}</ul>")

        opps = insight.get("opportunities") or []
        if opps:
            items = "".join(
                f"<li>{html.escape(str(self._text_of(o, 'opportunity')))}</li>"
                for o in opps[:4]
            )
            parts.append(f"<h4>Opportunities</h4><ul>{items}</ul>")

        scripts: List[str] = []
        charts = (q.get("visual") or {}).get("charts") or []
        for c in charts:
            cfg = c.get("chartjs")
            cid = c.get("id")
            if not cfg or not cid:
                continue
            canvas_id = re.sub(r"[^A-Za-z0-9_]", "_", f"{qid}_{cid}")
            ctitle = html.escape(str(c.get("title", "")))
            alt = html.escape(str(c.get("alt_text", "")))
            parts.append(
                f"<figure class='chart'><figcaption>{ctitle}</figcaption>"
                f"<canvas id='{canvas_id}' role='img' aria-label='{alt}'></canvas>"
                "</figure>"
            )
            scripts.append(
                f"new Chart(document.getElementById('{canvas_id}'),"
                f"{json.dumps(cfg, default=str)});"
            )
        parts.append("</article>")
        return "".join(parts), scripts

    def _recs_section(self, fr, narrative) -> str:
        recs = fr.get("top_recommendations") or []
        head = ("<section class='recs'><h2>Recommendations</h2>"
                f"<p>{html.escape(narrative)}</p>")
        if not recs:
            return head + "</section>"
        rows = ["<tr><th>Priority</th><th>Action</th><th>Owner</th>"
                "<th>Timeline</th><th>Effort</th></tr>"]
        for r in recs:
            rows.append(
                "<tr>"
                f"<td class='num'>{html.escape(str(r.get('priority_bucket','')))}</td>"
                f"<td>{html.escape(str(r.get('action','')))}</td>"
                f"<td>{html.escape(str(r.get('owner_role','')))}</td>"
                f"<td>{html.escape(str(r.get('timeline','')))}</td>"
                f"<td>{html.escape(str(r.get('effort','')))}</td>"
                "</tr>"
            )
        return head + f"<table class='rectable'>{''.join(rows)}</table></section>"

    def _monitoring_section(self, fr, narrative) -> str:
        mon = fr.get("monitoring") or {}
        head = ("<section class='monitoring'><h2>Monitoring</h2>"
                f"<p>{html.escape(narrative)}</p>")
        events = mon.get("events") or []
        if not events:
            return head + "</section>"
        items = []
        for e in events:
            metric = html.escape(self._pretty(e.get("metric")))
            etype = html.escape(str(e.get("event_type", "")))
            sev = html.escape(str(e.get("severity", "")))
            step = html.escape(str(e.get("recommended_next_step", "")))
            items.append(
                f"<li><span class='sev'>{sev}</span> <strong>{metric}</strong> "
                f"{etype} — {step}</li>"
            )
        return head + f"<ul class='events'>{''.join(items)}</ul></section>"

    def _data_quality_footer(self, fr) -> str:
        dq = fr.get("data_quality") or {}
        rows = dq.get("row_count")
        issues = dq.get("known_issues") or []
        parts = ["<footer class='dq'><h2>Data quality</h2>"]
        if rows is not None:
            parts.append(f"<p class='num'>{html.escape(self._fmt_num(rows))} rows analyzed.</p>")
        if issues:
            items = "".join(f"<li>{html.escape(str(i))}</li>" for i in issues)
            parts.append(f"<ul class='issues'>{items}</ul>")
        parts.append("</footer>")
        return "".join(parts)

    # ===================================================================== css

    def _css(self, st, pal) -> str:
        ff = st.get("font_family", "Fira Sans")
        nf = st.get("number_font", "Fira Code")
        p = pal.get("primary", "#1E40AF")
        sec = pal.get("secondary", "#3B82F6")
        danger = pal.get("danger", "#DC2626")
        neutral = pal.get("neutral", "#64748B")
        grid = pal.get("grid", "#E9EEF6")
        return f"""<style>
:root{{--primary:{p};--secondary:{sec};--danger:{danger};--neutral:{neutral};--grid:{grid};}}
*{{box-sizing:border-box;}}
body{{margin:0;background:#F8FAFC;color:#0F172A;font-family:'{ff}',system-ui,sans-serif;
  line-height:1.55;-webkit-font-smoothing:antialiased;}}
.num,.kpi-value{{font-family:'{nf}',ui-monospace,monospace;font-variant-numeric:tabular-nums;}}
.report{{max-width:960px;margin:0 auto;padding:32px 24px 64px;}}
h1{{font-size:1.9rem;margin:0;}}
h2{{font-size:1.3rem;color:var(--primary);border-bottom:2px solid var(--grid);
  padding-bottom:6px;margin:36px 0 14px;}}
h3{{font-size:1.05rem;margin:18px 0 6px;}}
h4{{font-size:.9rem;text-transform:uppercase;letter-spacing:.04em;color:var(--neutral);
  margin:14px 0 4px;}}
.hero{{border-left:5px solid var(--primary);padding:12px 18px;background:#fff;
  border-radius:8px;box-shadow:0 1px 3px rgba(15,23,42,.08);}}
.hero-top{{display:flex;justify-content:space-between;align-items:center;gap:12px;}}
.decision{{color:var(--neutral);margin:6px 0 0;font-style:italic;}}
.meta{{color:var(--neutral);font-size:.8rem;margin:6px 0 0;}}
.badge{{font-size:.75rem;padding:3px 10px;border-radius:999px;white-space:nowrap;}}
.badge.live{{background:#DCFCE7;color:#166534;}}
.badge.demo{{background:var(--grid);color:var(--neutral);}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:14px;
  margin:20px 0;}}
.kpi{{background:#fff;border-radius:10px;padding:14px 16px;
  box-shadow:0 1px 3px rgba(15,23,42,.08);border-top:3px solid var(--primary);}}
.kpi-metric{{font-size:.8rem;color:var(--neutral);}}
.kpi-value{{font-size:1.6rem;font-weight:600;color:var(--primary);margin:4px 0;}}
.kpi-conf{{font-size:.7rem;color:var(--neutral);}}
.qblock{{background:#fff;border-radius:10px;padding:16px 18px;margin:14px 0;
  box-shadow:0 1px 3px rgba(15,23,42,.08);}}
.qblock.skipped{{border-left:4px solid var(--neutral);}}
.empty{{color:var(--neutral);font-style:italic;}}
.qsummary{{font-weight:500;}}
ul{{margin:4px 0 8px;padding-left:20px;}}
li{{margin:3px 0;}}
.sev{{display:inline-block;background:var(--danger);color:#fff;border-radius:4px;
  font-size:.68rem;padding:1px 7px;text-transform:uppercase;margin-right:6px;}}
.chart{{margin:16px 0;background:#fff;border:1px solid var(--grid);border-radius:8px;
  padding:12px;}}
.chart figcaption{{font-size:.85rem;font-weight:600;color:var(--primary);margin-bottom:8px;}}
canvas{{max-height:340px;}}
table.rectable{{width:100%;border-collapse:collapse;margin-top:12px;font-size:.9rem;}}
.rectable th{{text-align:left;background:var(--primary);color:#fff;padding:8px 10px;}}
.rectable td{{padding:8px 10px;border-bottom:1px solid var(--grid);vertical-align:top;}}
.rectable tr:nth-child(even) td{{background:#F8FAFC;}}
.events li,.issues li{{margin:5px 0;}}
.dq{{margin-top:40px;color:var(--neutral);font-size:.85rem;}}
@media print{{body{{background:#fff;}}.qblock,.kpi,.hero{{box-shadow:none;}}}}
</style>"""

    # ================================================================== utils

    def _title(self, brief: Optional[Mapping[str, Any]]) -> str:
        if brief:
            ps = brief.get("problem_statement") or {}
            name = (brief.get("project_name") or ps.get("project_name"))
            if name:
                return f"{name} — Analytics Report"
        return "FV Institute — Analytics Report"

    @staticmethod
    def _text_of(item: Any, key: str) -> str:
        if isinstance(item, str):
            return item
        if isinstance(item, Mapping):
            return str(item.get(key) or item.get("text") or item)
        return str(item)

    @staticmethod
    def _pretty(name: Any) -> str:
        s = "" if name is None else str(name)
        return s.replace("_", " ").strip().title() if s else "Metric"

    @staticmethod
    def _fmt_num(v: Any) -> str:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return "n/a" if v is None else str(v)
        if float(v).is_integer():
            return f"{int(v):,}"
        return f"{round(float(v), 2):,}"


if __name__ == "__main__":
    import os
    import sys

    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.dirname(here))
    mock = os.path.join(os.path.dirname(here), "ui", "src", "mock")
    with open(os.path.join(mock, "final_report.json"), encoding="utf-8") as fh:
        fr = json.load(fh)
    with open(os.path.join(mock, "question_results.json"), encoding="utf-8") as fh:
        qr = json.load(fh)
    out = ReportAgent().run(fr, qr)
    dest = sys.argv[1] if len(sys.argv) > 1 else "report_preview.html"
    with open(dest, "w", encoding="utf-8") as fh:
        fh.write(out["html"])
    print(f"wrote {dest} ({len(out['html'])} bytes)")
