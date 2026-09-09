"""Deterministic inline-SVG chart renderer.

The report used to hand Chart.js configs to a CDN script and let the browser
paint them onto `<canvas>`. That failed in the only format the institute
actually reads the report in: a canvas on a `display:none` page sizes to 0x0,
so every chart outside the active tab printed as an empty frame. It also meant
no charts at all without internet.

This module renders the same VisualPackage chart dicts to static SVG on the
server. SVG is laid out by the print engine like any other markup, so what is
in the HTML is what lands in the PDF, on any machine, offline.

Only the shapes the Visualization Agent actually emits are supported:
horizontal bar (`options.indexAxis == "y"`), vertical bar, and line. Anything
else returns None and the caller falls back to the chart's `table_fallback`,
which is never invented data.

Coordinates are rounded to one decimal. That keeps the document small and —
because the report's PII guard treats a long unbroken digit run as a possible
phone number — keeps generated geometry well clear of that rule.
"""

from __future__ import annotations

import html
from typing import Any, Dict, List, Mapping, Optional, Sequence

JsonDict = Dict[str, Any]

# Matches the report's own tokens; passed in by the caller so a restyled
# report restyles its charts too.
DEFAULT_COLORS = {
    "primary": "#1E40AF",
    "secondary": "#3B82F6",
    "neutral": "#64748B",
    "grid": "#E9EEF6",
    "text": "#334155",
    "muted": "#64748B",
}

_BAR_H = 22          # bar thickness, horizontal charts
_BAR_GAP = 12        # gap between bars
_LABEL_W = 190       # left gutter for category labels
_VALUE_W = 96        # right gutter for the value label
_PAD_T = 14
_PAD_B = 30
_MAX_BARS = 14       # beyond this the chart stops being readable; rest -> table


def _n(value: float) -> str:
    """Round a coordinate for output ('12.0' -> '12')."""
    r = round(float(value), 1)
    return str(int(r)) if r == int(r) else str(r)


def _esc(text: Any) -> str:
    return html.escape(str(text), quote=True)


def _fmt_value(value: Any, value_format: str) -> str:
    """Format a data value for the label beside its bar.

    `value_format` is the metric kind the Visualization Agent already resolved
    ("rate" | "money" | "count"), so the renderer never has to guess whether
    0.549 is 55% or half a rupee.
    """
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return _esc(value)
    if value_format == "rate":
        return f"{v:.1%}"
    if value_format == "money":
        return f"{v:,.0f}"
    if v == int(v):
        return f"{int(v):,}"
    return f"{v:,.2f}"


def _series(chart: Mapping[str, Any]) -> tuple:
    """Pull (labels, values, colors) out of the embedded Chart.js config."""
    cfg = chart.get("chartjs") or {}
    data = cfg.get("data") or {}
    labels = [str(x) for x in (data.get("labels") or [])]
    datasets = data.get("datasets") or []
    if not datasets:
        return [], [], []
    ds = datasets[0]
    values = list(ds.get("data") or [])
    bg = ds.get("backgroundColor") or ds.get("borderColor")
    if isinstance(bg, list):
        colors = [str(c) for c in bg]
    else:
        colors = [str(bg or DEFAULT_COLORS["primary"])] * len(values)
    # Chart.js tolerates a labels/data length mismatch; the renderer must not.
    size = min(len(labels), len(values))
    return labels[:size], values[:size], (colors + colors)[:size]


def _axis_bounds(values: Sequence[Any]) -> tuple:
    """(low, high) for the value axis, always including zero."""
    nums = [float(v) for v in values if isinstance(v, (int, float))]
    if not nums:
        return 0.0, 1.0
    low, high = min(nums + [0.0]), max(nums + [0.0])
    if low == high:
        # A flat series (every segment identical, or a single zero) would
        # divide by zero below. Give it a nominal span so bars still draw.
        high = low + (abs(low) or 1.0)
    return low, high


def render(
    chart: Mapping[str, Any],
    *,
    width: int = 660,
    colors: Optional[Mapping[str, str]] = None,
) -> Optional[str]:
    """Render one VisualPackage chart to an `<svg>` string, or None.

    None means "this chart cannot be drawn" — the caller should print the
    chart's `table_fallback` instead of showing an empty frame.
    """
    pal = dict(DEFAULT_COLORS)
    if colors:
        pal.update({k: v for k, v in colors.items() if v})

    labels, values, series_colors = _series(chart)
    if not labels or not values:
        return None

    cfg = chart.get("chartjs") or {}
    ctype = str(cfg.get("type") or "bar")
    horizontal = ((cfg.get("options") or {}).get("indexAxis") == "y")
    vfmt = str(chart.get("value_format") or "count")

    if ctype == "line":
        return _render_line(labels, values, width, pal, vfmt)
    if horizontal:
        return _render_hbar(labels, values, series_colors, width, pal, vfmt,
                            chart.get("annotations") or {})
    return _render_vbar(labels, values, series_colors, width, pal, vfmt)


# ------------------------------------------------------------------ h-bar

def _render_hbar(labels, values, colors, width, pal, vfmt, annotations) -> Optional[str]:
    labels, values, colors = labels[:_MAX_BARS], values[:_MAX_BARS], colors[:_MAX_BARS]
    rows = len(labels)
    plot_w = width - _LABEL_W - _VALUE_W
    if plot_w <= 40:
        return None
    height = _PAD_T + rows * (_BAR_H + _BAR_GAP) + _PAD_B

    low, high = _axis_bounds(values)
    span = high - low

    def x_of(v: float) -> float:
        return _LABEL_W + (float(v) - low) / span * plot_w

    zero_x = x_of(0.0)
    parts: List[str] = [_open(width, height)]

    # Baseline marker: the report's own headline value, so a reader can see at
    # a glance which segments beat the institute average.
    baseline = annotations.get("baseline")
    if isinstance(baseline, (int, float)) and low <= float(baseline) <= high:
        bx = x_of(baseline)
        parts.append(
            f"<line x1='{_n(bx)}' y1='{_n(_PAD_T - 4)}' x2='{_n(bx)}' "
            f"y2='{_n(height - _PAD_B + 4)}' stroke='{pal['neutral']}' "
            "stroke-width='1' stroke-dasharray='4 3' />"
        )

    for i, (label, value, color) in enumerate(zip(labels, values, colors)):
        y = _PAD_T + i * (_BAR_H + _BAR_GAP)
        num = float(value) if isinstance(value, (int, float)) else 0.0
        x = x_of(min(num, 0.0))
        w = abs(x_of(num) - zero_x)
        parts.append(
            f"<text x='{_n(_LABEL_W - 10)}' y='{_n(y + _BAR_H * 0.7)}' "
            f"text-anchor='end' class='fv-lbl'>{_esc(_clip(label))}</text>"
        )
        parts.append(
            f"<rect x='{_n(x)}' y='{_n(y)}' width='{_n(max(w, 1))}' "
            f"height='{_n(_BAR_H)}' rx='3' fill='{_esc(color)}' />"
        )
        parts.append(
            f"<text x='{_n(_LABEL_W + plot_w + 8)}' y='{_n(y + _BAR_H * 0.7)}' "
            f"class='fv-val'>{_esc(_fmt_value(value, vfmt))}</text>"
        )

    parts.append(
        f"<line x1='{_n(zero_x)}' y1='{_n(_PAD_T - 4)}' x2='{_n(zero_x)}' "
        f"y2='{_n(height - _PAD_B + 4)}' stroke='{pal['neutral']}' stroke-width='1' />"
    )
    parts.append("</svg>")
    return "".join(parts)


# ------------------------------------------------------------------ v-bar

def _render_vbar(labels, values, colors, width, pal, vfmt) -> Optional[str]:
    labels, values, colors = labels[:_MAX_BARS], values[:_MAX_BARS], colors[:_MAX_BARS]
    cols = len(labels)
    height = 240
    left, right, top, bottom = 64, 16, 16, 46
    plot_w = width - left - right
    plot_h = height - top - bottom
    if plot_w <= 40 or cols == 0:
        return None

    low, high = _axis_bounds(values)
    span = high - low
    slot = plot_w / cols
    bar_w = min(slot * 0.62, 68)

    def y_of(v: float) -> float:
        return top + (high - float(v)) / span * plot_h

    zero_y = y_of(0.0)
    parts: List[str] = [_open(width, height)]
    parts.append(
        f"<line x1='{_n(left)}' y1='{_n(zero_y)}' x2='{_n(left + plot_w)}' "
        f"y2='{_n(zero_y)}' stroke='{pal['neutral']}' stroke-width='1' />"
    )

    for i, (label, value, color) in enumerate(zip(labels, values, colors)):
        num = float(value) if isinstance(value, (int, float)) else 0.0
        cx = left + slot * i + slot / 2
        y = min(y_of(num), zero_y)
        h = abs(y_of(num) - zero_y)
        parts.append(
            f"<rect x='{_n(cx - bar_w / 2)}' y='{_n(y)}' width='{_n(bar_w)}' "
            f"height='{_n(max(h, 1))}' rx='3' fill='{_esc(color)}' />"
        )
        parts.append(
            f"<text x='{_n(cx)}' y='{_n(y - 6)}' text-anchor='middle' "
            f"class='fv-val'>{_esc(_fmt_value(value, vfmt))}</text>"
        )
        parts.append(
            f"<text x='{_n(cx)}' y='{_n(height - bottom + 18)}' text-anchor='middle' "
            f"class='fv-lbl'>{_esc(_clip(label, 16))}</text>"
        )
    parts.append("</svg>")
    return "".join(parts)


# ------------------------------------------------------------------- line

def _render_line(labels, values, width, pal, vfmt) -> Optional[str]:
    nums = [v for v in values if isinstance(v, (int, float))]
    if len(nums) < 2:
        return None
    height = 240
    left, right, top, bottom = 64, 16, 16, 46
    plot_w = width - left - right
    plot_h = height - top - bottom

    low, high = _axis_bounds(values)
    span = high - low
    step = plot_w / max(len(values) - 1, 1)

    def y_of(v: float) -> float:
        return top + (high - float(v)) / span * plot_h

    points = [
        (left + step * i, y_of(v if isinstance(v, (int, float)) else 0.0))
        for i, v in enumerate(values)
    ]
    path = " ".join(
        ("M" if i == 0 else "L") + f"{_n(x)},{_n(y)}"
        for i, (x, y) in enumerate(points)
    )

    parts: List[str] = [_open(width, height)]
    for frac in (0.0, 0.5, 1.0):
        gy = top + plot_h * frac
        parts.append(
            f"<line x1='{_n(left)}' y1='{_n(gy)}' x2='{_n(left + plot_w)}' "
            f"y2='{_n(gy)}' stroke='{pal['grid']}' stroke-width='1' />"
        )
    parts.append(
        f"<path d='{path}' fill='none' stroke='{pal['primary']}' "
        "stroke-width='2' stroke-linejoin='round' />"
    )
    for x, y in points:
        parts.append(
            f"<circle cx='{_n(x)}' cy='{_n(y)}' r='2.5' fill='{pal['primary']}' />"
        )

    # Only the axis extremes are labelled: a monthly series over four years has
    # far more points than a printed axis can carry legibly.
    parts.append(
        f"<text x='{_n(left - 8)}' y='{_n(top + 4)}' text-anchor='end' "
        f"class='fv-val'>{_esc(_fmt_value(high, vfmt))}</text>"
    )
    parts.append(
        f"<text x='{_n(left - 8)}' y='{_n(top + plot_h)}' text-anchor='end' "
        f"class='fv-val'>{_esc(_fmt_value(low, vfmt))}</text>"
    )
    parts.append(
        f"<text x='{_n(left)}' y='{_n(height - bottom + 20)}' "
        f"class='fv-lbl'>{_esc(_clip(labels[0], 16))}</text>"
    )
    parts.append(
        f"<text x='{_n(left + plot_w)}' y='{_n(height - bottom + 20)}' "
        f"text-anchor='end' class='fv-lbl'>{_esc(_clip(labels[-1], 16))}</text>"
    )
    parts.append("</svg>")
    return "".join(parts)


# ------------------------------------------------------------------ shared

def _open(width: int, height: float) -> str:
    return (
        f"<svg class='fv-svg' viewBox='0 0 {_n(width)} {_n(height)}' "
        f"width='100%' height='{_n(height)}' role='img' "
        "preserveAspectRatio='xMinYMin meet' xmlns='http://www.w3.org/2000/svg'>"
    )


def _clip(text: Any, limit: int = 26) -> str:
    s = str(text)
    return s if len(s) <= limit else s[: limit - 1] + "…"


def truncated_rows(chart: Mapping[str, Any]) -> int:
    """How many series rows the SVG had to drop (0 when none)."""
    labels, values, _ = _series(chart)
    return max(min(len(labels), len(values)) - _MAX_BARS, 0)


CSS = """
.fv-svg{display:block;width:100%;height:auto;overflow:visible;}
.fv-svg .fv-lbl{font-size:11px;fill:#475569;}
.fv-svg .fv-val{font-size:11px;fill:#0f172a;font-weight:600;}
@media print{.fv-svg{page-break-inside:avoid;}}
"""
