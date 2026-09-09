"""Agent 8: Report Writer.

Turns one completed pipeline run into a single, shareable, standalone **HTML
report**. It is a *composition* agent: it reads ONLY the JSON contracts the
Orchestrator already produced (`final_report` + `question_results`) — never the
dataframe — and assembles a styled document. Charts are drawn server-side as
inline SVG (`svg_charts`) from the exact series the Visualization Agent emitted.

Three rules govern what reaches the page, all of them learned from a run that
printed ninety-seven sheets and not one visible graph:

1. **A question appears exactly once.** It is routed to the single business area
   it is about — not to every area whose keyword appears somewhere in a chart
   label. See `_route_questions`.
2. **A question the data cannot answer prints nothing.** It is listed once, with
   its reason, in the data-quality footer. See `_unanswered_list`.
3. **A chart is SVG or it is a table.** Never an empty frame: a canvas on a
   hidden page sizes to 0x0 and prints blank. See `_chart_figure`.

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

from . import llm_client, svg_charts


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

# Bare 10-digit run (unbounded on both sides so an 11+ digit Chart.js epoch /
# value never trips it — \b requires exactly ten).
_MOBILE_RE = re.compile(r"\b\d{10}\b")
# Formatted / international phone: digits split by +, spaces, brackets or hyphens.
# JSON chart data has no such separators between digits, so this cannot match a
# serialized Chart.js number — only a phone left in narrative or table text.
_FORMATTED_PHONE_RE = re.compile(r"\+?\d[\d\s().\-]{8,14}\d")
_PHONE_SEPARATORS = set(" +().-")

# A plain decimal number, optionally signed. `-279345.8333` has eleven digits
# and a "." — which the separator rule counts as a formatted phone, blocking a
# whole report over a chart label. A phone number is never a single signed
# decimal literal, so these are excluded before the digit count is applied.
_DECIMAL_LITERAL_RE = re.compile(r"^\d+(?:\.\d+)?$")


def _contains_mobile(text: str) -> bool:
    """True if a bare-10-digit or a formatted phone number survives in `text`."""
    if _MOBILE_RE.search(text):
        return True
    for m in _FORMATTED_PHONE_RE.finditer(text):
        run = m.group(0)
        # Look at the whole literal, not the window the regex happened to
        # capture: a trailing digit of a longer number would otherwise be cut
        # off and the remainder read as a phone.
        start, end = m.start(), m.end()
        while start > 0 and (text[start - 1].isdigit() or text[start - 1] in "-+."):
            start -= 1
        while end < len(text) and (text[end].isdigit() or text[end] == "."):
            end += 1
        # Strip the sign and any sentence-ending dot before deciding. A phone
        # is never a single signed decimal; a chart label routinely is.
        literal = text[start:end].lstrip("+-").strip(".")
        if _DECIMAL_LITERAL_RE.match(literal):
            continue
        n_digits = sum(ch.isdigit() for ch in run)
        # Require a separator so a pure 11-13 digit numeric literal (epoch ms, a
        # large metric value in a chart config) is not mistaken for a phone.
        if 10 <= n_digits <= 13 and any(ch in _PHONE_SEPARATORS for ch in run):
            return True
    return False


class PIILeakError(RuntimeError):
    """Raised if a mobile-number pattern survives into the rendered HTML."""


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
        if _contains_mobile(html_doc):
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
        answered = [q for q in qresults if q.get("status") == "ok"]
        unanswered = [q for q in qresults if q.get("status") != "ok"]
        routed = self._route_questions(answered)

        # Only areas that actually have an answered question get a page. An
        # empty section used to print a placeholder card, so a run that could
        # answer nothing about fees still shipped a Financial Report.
        pages = self._dashboard_pages()
        live_pages = [p for p in pages[1:] if routed.get(p[0])]

        body: List[str] = [self._header(title, fr, generated_at, live)]
        body.append(self._nav([pages[0]] + live_pages, routed))
        body.append(
            "<section class='page active' id='page-overview' data-page='overview'>"
            + self._page_head("Executive overview",
                              "What the uploaded data says, and what to do about it.")
            + self._exec_section(narrative["executive"])
            + self._kpi_strip(answered, fr)
            + self._recs_section(fr, narrative["recommendations"])
            + self._dataset_charts(answered, pal)
            + self._multi_source_section(fr)
            + self._cross_factor_section(fr)
            + self._business_tiles(live_pages, routed)
            + self._monitoring_section(fr, narrative["monitoring"])
            + self._data_quality_footer(fr, unanswered)
            + "</section>"
        )

        for page_id, label, desc in live_pages:
            blocks = [
                block for block in
                (self._question_block(q, pal) for q in routed.get(page_id, []))
                if block
            ]
            if not blocks:
                continue
            body.append(
                f"<section class='page' id='page-{page_id}' data-page='{page_id}'>"
                + self._page_head(label, desc)
                + "".join(blocks)
                + "</section>"
            )

        css = self._css(st, pal)
        # Charts are inline SVG now, so the only script left is tab switching —
        # and the document is fully readable, and fully printable, without it.
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

    def _cross_factor_section(self, fr) -> str:
        """Spec §21/§23 — where two factors combine into something new.

        Rendered even when nothing was found. "Every difference here is
        explained by one dimension on its own" is a real conclusion, and a
        section that vanishes on a null result trains the reader to expect a
        finding whenever it appears.
        """
        cross = fr.get("cross_factor") or {}
        if cross.get("status") != "ready":
            return ""

        pairs = cross.get("pairs_examined") or []
        crossed = ", ".join(f"{p['rows']} × {p['cols']}" for p in pairs)
        findings = cross.get("interactions") or []
        metric = html.escape(str(cross.get("metric", "the metric")))

        if not findings:
            thin = sum(p.get("cells_suppressed", 0) for p in pairs)
            tested = sum(p.get("cells_tested", 0) for p in pairs)
            detail = (
                f"{tested} cell(s) had enough rows to test"
                + (f"; {thin} were below the floor and were not shown"
                   if thin else "")
            )
            return (
                "<section class='panel cross-factor'>"
                "<h3>Factor analysis</h3>"
                f"<p>Crossed {html.escape(crossed)} on {metric}. "
                f"{html.escape(cross.get('verdict', ''))}</p>"
                f"<p class='muted'>{html.escape(detail)}. A cell counts only "
                f"when it differs from both of its margins after correction "
                f"for the number of cells examined.</p></section>"
            )

        rows = "".join(
            "<tr>"
            f"<td>{html.escape(str(f['row']))} × {html.escape(str(f['col']))}</td>"
            f"<td>{self._fmt_rate(f.get('value'))}</td>"
            f"<td>{self._fmt_rate(f.get('row_margin'))}</td>"
            f"<td>{self._fmt_rate(f.get('col_margin'))}</td>"
            f"<td>{self._fmt_rate(f.get('expected_additive'))}</td>"
            f"<td>{int(f.get('n', 0)):,}</td>"
            "</tr>"
            for f in findings[:10]
        )
        return (
            "<section class='panel cross-factor'>"
            "<h3>Factor analysis</h3>"
            f"<p>Crossed {html.escape(crossed)} on {metric}. "
            f"{html.escape(cross.get('verdict', ''))} — each one differs from "
            f"both of its margins, so neither factor alone explains it.</p>"
            "<table class='rectable compact'><tr>"
            "<th>Combination</th><th>Observed</th><th>Row alone</th>"
            "<th>Column alone</th><th>Both predict</th><th>n</th></tr>"
            f"{rows}</table></section>"
        )

    @staticmethod
    def _fmt_rate(value) -> str:
        return "—" if value is None else f"{float(value):.1%}"

    def _question_block(self, q, pal: Mapping[str, Any]) -> Optional[str]:
        """Render one answered question, or None if it was not answered.

        A question the data could not answer produces nothing here. It is
        listed once, with its reason, in the data-quality footer — instead of
        printing a "Skipped" card on every page it was routed to.
        """
        if q.get("status") != "ok":
            return None

        qid = html.escape(str(q.get("question_id", "")))
        qtext = html.escape(str(q.get("question", "")))
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

        # Caveats say why a number looks the way it does — "every row in this
        # file is admitted, so the rate is 100% everywhere". Without them a
        # bare 100% with no breakdown reads as a broken report rather than as
        # a fact about the upload.
        caveats = (q.get("analysis") or {}).get("caveats") or []
        if caveats:
            items = "".join(
                f"<li>{html.escape(str(self._text_of(c, 'caveat')))}</li>"
                for c in caveats[:3]
            )
            parts.append(f"<h4>Read this with</h4><ul class='caveats'>{items}</ul>")

        for c in (q.get("visual") or {}).get("charts") or []:
            # Dataset-scope charts (monthly volume, the funnel) describe the
            # whole upload, not this question. They are identical under every
            # question, so they are drawn once on the overview instead.
            if str(c.get("scope") or "question") != "question":
                continue
            figure = self._chart_figure(c, pal)
            if figure:
                parts.append(figure)

        parts.append("</article>")
        return "".join(parts)

    def _chart_figure(self, c: Mapping[str, Any],
                      pal: Mapping[str, Any]) -> Optional[str]:
        """One chart as inline SVG, falling back to its own data table.

        Charts were Chart.js canvases painted by a CDN script. A canvas on a
        `display:none` page has zero size, so every chart outside the active
        tab printed as an empty frame — which is what the whole PDF was. SVG is
        laid out by the print engine like any other markup, needs no network,
        and shows the same numbers the agent computed.
        """
        title = html.escape(str(c.get("title", "")))
        sub = html.escape(str(c.get("subtitle", "")))
        alt = html.escape(str(c.get("alt_text", "")))
        body = svg_charts.render(c, colors=pal)
        if body is None:
            body = self._chart_table(c)
        if not body:
            return None
        dropped = svg_charts.truncated_rows(c)
        note = (f"<p class='chart-note'>Showing the top rows; {dropped} more "
                "not plotted.</p>") if dropped else ""
        return (
            f"<figure class='chart' role='img' aria-label='{alt}'>"
            f"<figcaption><strong>{title}</strong><span>{sub}</span></figcaption>"
            f"{body}{note}</figure>"
        )

    def _chart_table(self, c: Mapping[str, Any]) -> str:
        """The chart's `table_fallback` as a small table.

        Used when a chart cannot be drawn. The rows are the agent's own
        computed values, so this degrades to fewer pixels — never to invention.
        """
        rows = c.get("table_fallback") or []
        if not rows or not isinstance(rows[0], Mapping):
            return ""
        cols = list(rows[0].keys())
        head = "".join(f"<th>{html.escape(self._pretty(k))}</th>" for k in cols)
        body = []
        for r in rows[:12]:
            cells = "".join(
                f"<td>{html.escape(self._fmt_num(r.get(k)))}</td>" for k in cols
            )
            body.append(f"<tr>{cells}</tr>")
        return (f"<table class='charttable'><tr>{head}</tr>"
                f"{''.join(body)}</table>")

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

    def _data_quality_footer(self, fr, unanswered=()) -> str:
        dq = fr.get("data_quality") or {}
        rows = dq.get("row_count")
        issues = dq.get("known_issues") or []
        parts = ["<footer class='dq'><h2>Data quality</h2>"]
        if rows is not None:
            parts.append(f"<p class='num'>{html.escape(self._fmt_num(rows))} rows analyzed.</p>")
        if issues:
            items = "".join(f"<li>{html.escape(str(i))}</li>" for i in issues)
            parts.append(f"<ul class='issues'>{items}</ul>")
        parts.append(self._unanswered_list(unanswered))
        parts.append("</footer>")
        return "".join(parts)

    def _unanswered_list(self, unanswered) -> str:
        """The questions this upload could not answer, listed once, with why.

        These used to render as a "Skipped" card in the report body — on every
        page the question was routed to. They are a property of the uploaded
        file, not a finding, so they belong here: one line each, telling the
        operator which column to add to the sheet next time.
        """
        rows = []
        for q in unanswered:
            qid = html.escape(str(q.get("question_id", "")))
            qtext = html.escape(str(q.get("question", "")))
            reason = html.escape(
                str(q.get("skip_reason") or "not computable on this data").rstrip(".")
            )
            label = f"{qid}: {qtext}" if qid else qtext
            rows.append(f"<li><strong>{label}</strong><span>{reason}</span></li>")
        if not rows:
            return ""
        # A plain block, not a <details>: a collapsed disclosure prints as its
        # summary line alone, and this list has to survive the PDF.
        return (
            "<section class='unanswered'>"
            f"<h3>{len(rows)} question(s) this file could not answer</h3>"
            "<p>No chart or number is shown for these — the columns they need "
            "are not in the upload. Add the column and re-run to answer them.</p>"
            f"<ul>{''.join(rows)}</ul></section>"
        )

    def _business_tiles(self, live_pages, routed) -> str:
        """Jump tiles for the areas that have content (already filtered)."""
        if not live_pages:
            return ""
        tiles = []
        for page_id, label, desc in live_pages:
            count = len(routed.get(page_id, []))
            tiles.append(
                f"<button class='tile' type='button' data-target='{page_id}'>"
                f"<strong>{html.escape(label)}</strong>"
                f"<span>{html.escape(desc)}</span>"
                f"<em>{count} analysis block(s)</em>"
                "</button>"
            )
        return "<section class='tiles'>" + "".join(tiles) + "</section>"

    def _dataset_charts(self, answered, pal) -> str:
        """Whole-upload charts (monthly volume, the funnel), drawn once.

        The Visualization Agent attaches these to every question because they
        need the dataframe, not the question. Printing them under each question
        repeated the same two pictures a dozen times; they are shown here, once,
        as context for the run.
        """
        seen, figures = set(), []
        for q in answered:
            for c in (q.get("visual") or {}).get("charts") or []:
                if str(c.get("scope") or "question") != "dataset":
                    continue
                title = str(c.get("title", ""))
                if title in seen:
                    continue
                seen.add(title)
                figure = self._chart_figure(c, pal)
                if figure:
                    figures.append(figure)
        if not figures:
            return ""
        return ("<section class='panel context'><h3>This upload at a glance</h3>"
                + "".join(figures) + "</section>")

    # Ordered most-specific first. On a score tie the earlier rule wins, so
    # "which counsellors convert best?" lands on the faculty page instead of
    # being pulled onto operational by the word "admission".
    _DOMAIN_RULES = (
        ("financial", ("fee", "fees", "revenue", "payment", "collection", "collected",
                       "pending", "overdue", "installment", "cost", "profit",
                       "sales amount", "invoice", "refund", "discount")),
        ("team", ("counsellor", "counselor", "sales person", "salesperson",
                  "faculty", "trainer", "teacher", "staff", "employee",
                  "advisor", "consultant")),
        ("branch", ("branch", "store", "location", "city", "region", "center",
                    "centre", "campus")),
        ("product", ("course", "product", "program", "programme", "package",
                     "service", "sku", "category", "certificate")),
        ("operational", ("lead", "admission", "application", "conversion",
                         "funnel", "enquiry", "inquiry", "dropout", "completion",
                         "attendance", "batch", "records", "trend")),
    )

    def _route_questions(self, qresults) -> Dict[str, List[Mapping[str, Any]]]:
        """Place every question on exactly one page.

        Routing used to append a question to *every* page whose keywords it
        matched, and built the match text from the question plus each chart's
        title, subtitle and alt-text. Because every question is charted "by
        Faculty", "by Branch" and "by Course Category", every question matched
        every area: six questions became thirty blocks and a 97-page PDF whose
        five business sections were verbatim copies of one another.

        A question now goes to the single best-scoring area, scored on what the
        question is about — never on the furniture of its charts.
        """
        routed: Dict[str, List[Mapping[str, Any]]] = {
            name: [] for name, _ in self._DOMAIN_RULES
        }
        for q in qresults:
            routed[self._page_for(q)].append(q)
        return routed

    def _page_for(self, q) -> str:
        """The one page a question belongs on."""
        question = " ".join(
            [str(q.get("question", "")), str(q.get("question_id", ""))]
        ).lower()
        best = self._best_domain(question)
        if best:
            return best
        # Nothing in the wording places it. Fall back to the metric it actually
        # computed, which is weaker evidence (a metric named
        # `counselling_to_admission_rate` says "admission" as loudly as it says
        # "counselling") but better than defaulting blind.
        metric = str(
            ((q.get("analysis") or {}).get("headline_number") or {}).get("metric", "")
        ).lower()
        return self._best_domain(metric) or "operational"

    def _best_domain(self, text: str) -> Optional[str]:
        if not text.strip():
            return None
        best_name, best_score = None, 0
        for name, words in self._DOMAIN_RULES:
            score = sum(1 for word in words if word in text)
            if score > best_score:      # strict: earlier rule wins a tie
                best_name, best_score = name, score
        return best_name

    def _dashboard_script(self) -> str:
        return """
function showPage(page){
  document.querySelectorAll('.page').forEach(function(el){
    el.classList.toggle('active', el.dataset.page === page);
  });
  document.querySelectorAll('[data-target]').forEach(function(el){
    el.classList.toggle('active', el.dataset.target === page);
  });
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
  padding:12px;}}
.chart figcaption{{display:flex;justify-content:space-between;gap:12px;font-size:.85rem;
  color:var(--primary);margin-bottom:8px;}}
.chart figcaption span{{color:var(--neutral);font-weight:400;text-align:right;}}
.chart-note{{margin:6px 0 0;font-size:.78rem;color:var(--neutral);}}
.caveats li{{color:var(--neutral);font-size:.86rem;}}
table.charttable{{width:100%;border-collapse:collapse;font-size:.82rem;}}
.charttable th{{text-align:left;color:var(--neutral);font-weight:600;
  border-bottom:1px solid var(--grid);padding:5px 8px;}}
.charttable td{{padding:5px 8px;border-bottom:1px solid var(--grid);}}
.context .chart{{border:none;padding:0;}}
.unanswered{{margin-top:16px;padding-top:12px;border-top:1px solid var(--grid);}}
.unanswered h3{{font-size:.95rem;color:var(--primary);margin:0 0 4px;}}
.unanswered ul{{list-style:none;padding-left:0;}}
.unanswered li{{padding:5px 0;border-bottom:1px solid var(--grid);}}
.unanswered li span{{display:block;color:var(--neutral);font-size:.82rem;}}
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
}}
@media print{{body{{background:#fff;}}.tabs,.page-actions{{display:none;}}
  /* Every section prints, in order, and the SVG inside them prints with them.
     `page-break-after:always` used to force a feed after each one, which on a
     report whose sections were near-duplicates is how six questions became
     ninety-seven sheets of paper. Sections now flow, and only blocks that
     would straddle a fold are kept whole. */
  .page{{display:block;}}
  .page + .page{{page-break-before:always;}}
  /* Keep a chart whole; let a question block flow. A qblock carries five or
     six charts and is taller than a sheet of paper, so "never break it" means
     "start it on a fresh page and leave the rest of this one empty" — the gaps
     that read as blank pages. Only things that fit on a page are kept whole. */
  .chart,.kpi,.unanswered li{{page-break-inside:avoid;}}
  .qblock h3,h2,h3,h4{{page-break-after:avoid;}}
  /* Nothing after the last section: a trailing break, or bottom padding that
     spills past the boundary, prints one empty sheet at the end. */
  .page:last-child{{page-break-after:avoid;}}
  .report{{padding-bottom:0;}}
  body{{margin:0;}}
  .qblock,.kpi,.hero,.panel{{box-shadow:none;}}}}
{svg_charts.CSS}
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
