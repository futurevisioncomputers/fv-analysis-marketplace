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
        pages = self._dashboard_pages()
        routed = self._route_questions(qresults)
        chart_scripts: List[str] = []
        body: List[str] = [self._header(title, fr, generated_at, live)]
        body.append(self._nav(pages, routed))
        body.append(
            "<section class='page active' id='page-overview' data-page='overview'>"
            + self._page_head("Executive overview",
                              "Board-level summary across all available business areas.")
            + self._exec_section(narrative["executive"])
            + self._kpi_strip(qresults, fr)
            + self._multi_source_section(fr)
            + self._business_tiles(pages, routed)
            + self._recs_section(fr, narrative["recommendations"])
            + self._monitoring_section(fr, narrative["monitoring"])
            + self._data_quality_footer(fr)
            + "</section>"
        )

        for page_id, label, desc in pages[1:]:
            blocks: List[str] = []
            for i, q in enumerate(routed.get(page_id, []), start=1):
                block, scripts = self._question_block(q, f"{page_id}_{i}")
                blocks.append(block)
                chart_scripts.extend(scripts)
            if not blocks:
                blocks.append(
                    "<article class='qblock empty-state'>"
                    "<h3>No direct analysis block for this page</h3>"
                    f"<p>{html.escape(self._empty_page_message(fr, page_id))}</p>"
                    "</article>"
                )
            body.append(
                f"<section class='page' id='page-{page_id}' data-page='{page_id}'>"
                + self._page_head(label, desc)
                + "".join(blocks)
                + "</section>"
            )

        css = self._css(st, pal)
        script = ""
        if chart_scripts:
            script = (
                f'<script src="{CHARTJS_CDN}"></script>\n'
                "<script>document.addEventListener('DOMContentLoaded',function(){\n"
                "window.fvCharts=[];\n"
                + "\n".join(chart_scripts) + "\n"
                + self._dashboard_script() + "\n});</script>"
            )
        else:
            script = (
                "<script>document.addEventListener('DOMContentLoaded',function(){\n"
                + self._dashboard_script() + "\n});</script>"
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

    def _dashboard_pages(self) -> List[tuple]:
        return [
            ("overview", "Overview", "Executive cockpit and decision summary."),
            ("financial", "Financial Report",
             "Fee, revenue, collections, pending amount, and cost indicators."),
            ("operational", "Operational Report",
             "Leads, admissions, conversion funnel, trend, and delivery health."),
            ("product", "Product Performance",
             "Course, product, program, package, and service-line performance."),
            ("branch", "Branch / Store Report",
             "Branch, store, location, region, and city performance."),
            ("team", "Sales / Faculty Report",
             "Counsellor, sales person, trainer, faculty, and staff performance."),
        ]

    def _nav(self, pages, routed) -> str:
        buttons = []
        total = sum(len(v) for k, v in routed.items() if k != "overview")
        for page_id, label, _desc in pages:
            count = total if page_id == "overview" else len(routed.get(page_id, []))
            active = " active" if page_id == "overview" else ""
            buttons.append(
                f"<button class='tab{active}' type='button' data-target='{page_id}'>"
                f"<span>{html.escape(label)}</span><small>{count}</small></button>"
            )
        return "<nav class='tabs' aria-label='Dashboard pages'>" + "".join(buttons) + "</nav>"

    def _page_head(self, title, desc) -> str:
        return (
            "<div class='page-head'>"
            f"<div><h2>{html.escape(title)}</h2><p>{html.escape(desc)}</p></div>"
            "<div class='page-actions'><button type='button' onclick='window.print()'>Print</button></div>"
            "</div>"
        )

    def _exec_section(self, text) -> str:
        return ("<section class='exec panel'><h3>Executive summary</h3>"
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

    def _multi_source_section(self, fr) -> str:
        summary = fr.get("multi_source_summary") or {}
        sources = fr.get("sources") or []
        relationships = fr.get("relationships") or {}
        domain_metrics = fr.get("domain_metrics") or {}
        if not summary and not sources:
            return ""

        cards = []
        for label, value in (
            ("Sources", summary.get("source_count", len(sources))),
            ("Joined", summary.get("joined_count", 0)),
            ("Unjoined", summary.get("unjoined_count", 0)),
            ("Accepted joins", summary.get("accepted_join_count", 0)),
        ):
            cards.append(
                "<div class='kpi mini'>"
                f"<div class='kpi-metric'>{html.escape(str(label))}</div>"
                f"<div class='kpi-value num'>{html.escape(str(value))}</div>"
                "</div>"
            )
        rows = []
        for src in sources:
            rows.append(
                "<tr>"
                f"<td>{html.escape(str(src.get('name','')))}</td>"
                f"<td>{html.escape(str(src.get('domain','')))}</td>"
                f"<td class='num'>{html.escape(str(src.get('row_count','')))}</td>"
                f"<td>{html.escape(str(src.get('join_status','')))}</td>"
                "</tr>"
            )
        table = ""
        if rows:
            table = (
                "<table class='rectable compact'><tr><th>Source</th><th>Domain</th>"
                "<th>Rows</th><th>Status</th></tr>" + "".join(rows) + "</table>"
            )
        metric_rows = []
        for domain, payload in domain_metrics.items():
            metrics = payload.get("metrics") or {}
            metric_rows.append(
                "<tr>"
                f"<td>{html.escape(str(domain))}</td>"
                f"<td>{html.escape(', '.join(f'{k}: {self._fmt_num(v)}' for k, v in metrics.items()))}</td>"
                "</tr>"
            )
        metric_table = ""
        if metric_rows:
            metric_table = (
                "<table class='rectable compact'><tr><th>Domain</th><th>Summary metrics</th></tr>"
                + "".join(metric_rows) + "</table>"
            )
        rejected = relationships.get("rejected") or []
        warnings = "".join(
            f"<li>{html.escape(str(r.get('right_source')))}: "
            f"{html.escape(str(r.get('reason','not joined')))}</li>"
            for r in rejected[:8]
        )
        warn_block = f"<ul class='issues'>{warnings}</ul>" if warnings else ""
        return (
            "<section class='panel source-health'><h3>Multi-source model</h3>"
            f"<div class='kpis source-kpis'>{''.join(cards)}</div>"
            f"{table}{metric_table}{warn_block}</section>"
        )

    def _question_block(self, q, namespace: str = "") -> (str, List[str]):
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
            canvas_id = re.sub(r"[^A-Za-z0-9_]", "_", f"{namespace}_{qid}_{cid}")
            ctitle = html.escape(str(c.get("title", "")))
            sub = html.escape(str(c.get("subtitle", "")))
            alt = html.escape(str(c.get("alt_text", "")))
            parts.append(
                "<figure class='chart'>"
                f"<figcaption><strong>{ctitle}</strong><span>{sub}</span></figcaption>"
                f"<canvas id='{canvas_id}' role='img' aria-label='{alt}'></canvas>"
                "</figure>"
            )
            scripts.append(
                "window.fvCharts.push(new Chart("
                f"document.getElementById('{canvas_id}'),"
                f"{json.dumps(self._chart_config(cfg), default=str)}));"
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

    def _business_tiles(self, pages, routed) -> str:
        tiles = []
        for page_id, label, desc in pages[1:]:
            count = len(routed.get(page_id, []))
            tiles.append(
                f"<button class='tile' type='button' data-target='{page_id}'>"
                f"<strong>{html.escape(label)}</strong>"
                f"<span>{html.escape(desc)}</span>"
                f"<em>{count} analysis block(s)</em>"
                "</button>"
            )
        return "<section class='tiles'>" + "".join(tiles) + "</section>"

    def _empty_page_message(self, fr, page_id: str) -> str:
        sources = fr.get("sources") or []
        domain_aliases = {
            "financial": ("finance",),
            "operational": ("operations", "admission", "student"),
            "product": ("product", "course", "certificate", "student"),
            "branch": ("branch", "operations", "student", "admission"),
            "team": ("team", "faculty", "student", "admission"),
        }
        domains = domain_aliases.get(page_id, (page_id,))
        matching = [s for s in sources if str(s.get("domain")) in domains]
        if matching:
            names = ", ".join(str(s.get("name")) for s in matching[:4])
            return (
                f"Source data exists for this area ({names}), but no matching analysis "
                "block was generated in this run."
            )
        if sources:
            return "No source was provided for this dashboard area."
        return (
            "The saved run did not contain matching metrics or dimensions. "
            "Run a question that mentions this area to populate it."
        )

    def _route_questions(self, qresults) -> Dict[str, List[Mapping[str, Any]]]:
        routed: Dict[str, List[Mapping[str, Any]]] = {
            "financial": [], "operational": [], "product": [], "branch": [], "team": [],
        }
        for q in qresults:
            text = self._question_text(q)
            pages = self._domains_for_text(text)
            if not pages:
                pages = ["operational"]
            for page in pages:
                routed.setdefault(page, []).append(q)
        return routed

    def _question_text(self, q) -> str:
        chunks = [str(q.get("question", "")), str(q.get("question_id", ""))]
        analysis = q.get("analysis") or {}
        headline = analysis.get("headline_number") or {}
        chunks.append(str(headline.get("metric", "")))
        for b in analysis.get("breakdowns") or []:
            chunks.extend([str(b.get("dimension", "")), str(b.get("dimension_label", "")),
                           str(b.get("segment", ""))])
        visual = q.get("visual") or {}
        for c in visual.get("kpi_cards") or []:
            chunks.append(str(c.get("metric", "")))
        for c in visual.get("charts") or []:
            chunks.extend([str(c.get("title", "")), str(c.get("subtitle", "")),
                           str(c.get("alt_text", ""))])
        return " ".join(chunks).lower()

    def _domains_for_text(self, text: str) -> List[str]:
        rules = [
            ("financial", ("fee", "fees", "revenue", "payment", "collection", "pending",
                           "overdue", "installment", "cost", "profit", "sales amount",
                           "invoice", "refund", "discount")),
            ("product", ("course", "product", "program", "programme", "package",
                         "service", "sku", "category")),
            ("branch", ("branch", "store", "location", "city", "region", "center",
                        "centre", "campus")),
            ("team", ("counsellor", "counselor", "sales person", "salesperson",
                      "faculty", "trainer", "teacher", "staff", "employee",
                      "advisor", "consultant")),
            ("operational", ("lead", "admission", "application", "conversion",
                             "funnel", "enquiry", "inquiry", "dropout", "completion",
                             "attendance", "batch", "records", "trend")),
        ]
        return [name for name, words in rules if any(word in text for word in words)]

    def _chart_config(self, cfg: Mapping[str, Any]) -> JsonDict:
        copied = json.loads(json.dumps(cfg, default=str))
        opts = copied.setdefault("options", {})
        opts.setdefault("responsive", True)
        opts.setdefault("maintainAspectRatio", False)
        plugins = opts.setdefault("plugins", {})
        plugins.setdefault("tooltip", {"enabled": True, "intersect": False, "mode": "index"})
        plugins.setdefault("legend", {"display": False})
        scales = opts.setdefault("scales", {})
        for axis in ("x", "y"):
            axis_opts = scales.setdefault(axis, {})
            axis_opts.setdefault("grid", {"color": "rgba(148,163,184,.2)"})
        return copied

    def _dashboard_script(self) -> str:
        return """
function showPage(page){
  document.querySelectorAll('.page').forEach(function(el){
    el.classList.toggle('active', el.dataset.page === page);
  });
  document.querySelectorAll('[data-target]').forEach(function(el){
    el.classList.toggle('active', el.dataset.target === page);
  });
  if (window.fvCharts) {
    window.setTimeout(function(){ window.fvCharts.forEach(function(c){ c.resize(); }); }, 60);
  }
}
document.querySelectorAll('[data-target]').forEach(function(el){
  el.addEventListener('click', function(){ showPage(el.dataset.target); });
});
""".strip()

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
.report{{max-width:1240px;margin:0 auto;padding:24px 20px 64px;}}
h1{{font-size:1.9rem;margin:0;}}
h2{{font-size:1.3rem;color:var(--primary);border-bottom:2px solid var(--grid);
  padding-bottom:6px;margin:36px 0 14px;}}
h3{{font-size:1.05rem;margin:18px 0 6px;}}
h4{{font-size:.9rem;text-transform:uppercase;letter-spacing:.04em;color:var(--neutral);
  margin:14px 0 4px;}}
.hero{{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:20px;align-items:end;
  padding:22px 24px;background:#fff;border:1px solid #D7DEE8;
  border-radius:8px;box-shadow:0 12px 30px rgba(15,23,42,.08);}}
.hero-top{{display:flex;justify-content:space-between;align-items:center;gap:12px;}}
.eyebrow{{margin:0;color:var(--secondary);font-size:.75rem;font-weight:700;
  text-transform:uppercase;}}
.hero-meta{{display:grid;gap:2px;text-align:right;color:var(--neutral);font-size:.76rem;}}
.hero-meta strong{{color:#0F172A;font-weight:600;max-width:260px;word-break:break-word;}}
.decision{{color:var(--neutral);margin:6px 0 0;font-style:italic;}}
.meta{{color:var(--neutral);font-size:.8rem;margin:6px 0 0;}}
.badge{{font-size:.75rem;padding:3px 10px;border-radius:999px;white-space:nowrap;}}
.badge.live{{background:#DCFCE7;color:#166534;}}
.badge.demo{{background:var(--grid);color:var(--neutral);}}
.tabs{{position:sticky;top:0;z-index:10;display:grid;
  grid-template-columns:repeat(6,minmax(0,1fr));gap:8px;margin:18px 0;padding:8px;
  background:rgba(248,250,252,.94);backdrop-filter:blur(10px);border:1px solid #D7DEE8;
  border-radius:8px;}}
.tab,.tile,.page-actions button{{font:inherit;border:1px solid #D7DEE8;background:#fff;color:#334155;
  border-radius:7px;cursor:pointer;transition:border-color .15s,box-shadow .15s,transform .15s;}}
.tab{{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:9px 10px;
  min-height:44px;text-align:left;}}
.tab span{{font-weight:650;font-size:.85rem;white-space:normal;}}
.tab small{{font-family:'{nf}',ui-monospace,monospace;color:var(--neutral);}}
.tab.active,.tile.active{{border-color:var(--primary);box-shadow:0 0 0 2px rgba(30,64,175,.12);
  color:var(--primary);}}
.page{{display:none;}}
.page.active{{display:block;}}
.page-head{{display:flex;justify-content:space-between;gap:20px;align-items:flex-start;margin:22px 0 14px;}}
.page-head h2{{border:0;margin:0;padding:0;color:#0F172A;font-size:1.45rem;}}
.page-head p{{margin:4px 0 0;color:var(--neutral);}}
.page-actions button{{padding:8px 12px;font-size:.85rem;}}
.panel{{background:#fff;border:1px solid #D7DEE8;border-radius:8px;padding:16px 18px;
  box-shadow:0 8px 22px rgba(15,23,42,.06);margin:14px 0;}}
.panel h3{{margin-top:0;}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px;margin:16px 0;}}
.tile{{display:grid;gap:6px;text-align:left;padding:14px;min-height:120px;}}
.tile strong{{font-size:.98rem;color:#0F172A;}}
.tile span{{color:var(--neutral);font-size:.84rem;}}
.tile em{{font-style:normal;color:var(--primary);font-size:.78rem;font-weight:700;}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:14px;
  margin:20px 0;}}
.kpi{{background:#fff;border-radius:10px;padding:14px 16px;
  box-shadow:0 8px 20px rgba(15,23,42,.06);border:1px solid #D7DEE8;border-top:3px solid var(--primary);}}
.kpi-metric{{font-size:.8rem;color:var(--neutral);}}
.kpi-value{{font-size:1.6rem;font-weight:600;color:var(--primary);margin:4px 0;}}
.kpi-conf{{font-size:.7rem;color:var(--neutral);}}
.kpi.mini .kpi-value{{font-size:1.25rem;}}
.source-kpis{{margin:10px 0 14px;}}
.source-health .rectable.compact{{font-size:.82rem;}}
.qblock{{background:#fff;border-radius:8px;padding:16px 18px;margin:14px 0;
  box-shadow:0 8px 22px rgba(15,23,42,.06);border:1px solid #D7DEE8;}}
.qtitle h3{{margin-top:0;}}
.qblock.skipped{{border-left:4px solid var(--neutral);}}
.empty-state{{border-style:dashed;background:#FBFDFF;}}
.empty{{color:var(--neutral);font-style:italic;}}
.qsummary{{font-weight:500;}}
ul{{margin:4px 0 8px;padding-left:20px;}}
li{{margin:3px 0;}}
.sev{{display:inline-block;background:var(--danger);color:#fff;border-radius:4px;
  font-size:.68rem;padding:1px 7px;text-transform:uppercase;margin-right:6px;}}
.chart{{margin:16px 0;background:#fff;border:1px solid var(--grid);border-radius:8px;
  padding:12px;height:390px;}}
.chart figcaption{{display:flex;justify-content:space-between;gap:12px;font-size:.85rem;
  color:var(--primary);margin-bottom:8px;}}
.chart figcaption span{{color:var(--neutral);font-weight:400;text-align:right;}}
canvas{{width:100%!important;height:320px!important;}}
table.rectable{{width:100%;border-collapse:collapse;margin-top:12px;font-size:.9rem;}}
.rectable th{{text-align:left;background:var(--primary);color:#fff;padding:8px 10px;}}
.rectable td{{padding:8px 10px;border-bottom:1px solid var(--grid);vertical-align:top;}}
.rectable tr:nth-child(even) td{{background:#F8FAFC;}}
.events li,.issues li{{margin:5px 0;}}
.dq{{margin-top:20px;color:var(--neutral);font-size:.85rem;}}
@media (max-width:900px){{
  .hero{{grid-template-columns:1fr;}}
  .hero-meta{{text-align:left;}}
  .tabs{{grid-template-columns:repeat(2,minmax(0,1fr));}}
  .page-head{{display:block;}}
  .page-actions{{margin-top:10px;}}
}}
@media (max-width:560px){{
  .report{{padding:14px 12px 48px;}}
  .tabs{{grid-template-columns:1fr;position:static;}}
  .hero{{padding:16px;}}
  h1{{font-size:1.45rem;}}
  .chart{{height:340px;}}
  canvas{{height:270px!important;}}
}}
@media print{{body{{background:#fff;}}.tabs,.page-actions{{display:none;}}
  .page{{display:block;page-break-after:always;}}.qblock,.kpi,.hero,.panel{{box-shadow:none;}}}}
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
