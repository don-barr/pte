"""The rate distribution chart: an inline SVG histogram with the Q1-Q3 band, the median, a density curve and
Tukey's outlier cut-offs (dashed, when inside the axis), with the bars wholly beyond a fence set apart.

Built server-side from rate_stats so the page needs no charting library. Colors come from base.html's CSS
variables (--accent, --text, --muted, --border), so light and dark themes both work. One series, so no
legend box: the caption under the chart names the band, the median line and the curve. Each bin has a
full-height hover target with a native tooltip (count and price range).
"""

import math
from html import escape

from markupsafe import Markup

WIDTH, HEIGHT = 760, 250
LEFT, RIGHT, TOP, BOTTOM = 40, 14, 22, 40  # plot margins: y labels left, x labels and title below


def nice_step(span: float, target: int) -> float:
    """A 1/2/5 x 10^k step that cuts span into about target pieces."""
    raw = span / max(target, 1)
    if raw <= 0:
        return 1.0
    power = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 5, 10):
        if raw <= m * power:
            return m * power
    return 10 * power


def ticks(low: float, high: float, target: int) -> list[float]:
    step = nice_step(high - low, target)
    first = math.ceil(low / step - 1e-9) * step
    out, v = [], first
    while v <= high + 1e-9 * max(1.0, abs(high)):
        out.append(round(v, 10))
        v += step
    return out


def axis_money(value: float, step: float) -> str:
    """Tick label: whole dollars when the ticks are whole dollars, cents otherwise."""
    if step >= 1 and float(value).is_integer():
        return f"${value:,.0f}"
    return f"${value:,.2f}"


def money(value: float) -> str:
    return f"${value:,.2f}"


def distribution_chart(stats: dict, units: tuple[str, str] = ("rate", "rates")) -> Markup | None:
    """SVG for a rate_stats result; None when every rate is the same (nothing to draw). units names what is
    counted (("provider offer", "provider offers") with Count by Provider)."""
    if not stats or stats["min"] == stats["max"]:
        return None
    low, high = stats["min"], stats["max"]
    bins, curve = stats["histogram"], stats["density"]
    plot_w, plot_h = WIDTH - LEFT - RIGHT, HEIGHT - TOP - BOTTOM
    peak = max([b["count"] for b in bins] + [p["y"] for p in curve])
    y_ticks = ticks(0, peak, 4)
    y_top = max(y_ticks[-1], peak) if y_ticks else peak
    x = lambda v: LEFT + (v - low) / (high - low) * plot_w
    y = lambda c: TOP + plot_h - c / y_top * plot_h

    parts = [f'<svg class="dist-chart" viewBox="0 0 {WIDTH} {HEIGHT}" role="img" '
             f'aria-labelledby="dist-title" xmlns="http://www.w3.org/2000/svg">',
             f'<title id="dist-title">Distribution of {stats["count"]:,} {units[1]} from {money(low)} to {money(high)}; '
             f'median {money(stats["median"])}, middle half {money(stats["q1"])} to {money(stats["q3"])}</title>']
    # Recessive horizontal gridlines with count labels.
    for t in y_ticks:
        parts.append(f'<line class="grid" x1="{LEFT}" x2="{LEFT + plot_w}" y1="{y(t):.1f}" y2="{y(t):.1f}"/>')
        parts.append(f'<text class="tick" x="{LEFT - 6}" y="{y(t) + 4:.1f}" text-anchor="end">{t:,.0f}</text>')
    # Middle half (Q1-Q3) as a wash behind the bars.
    parts.append(f'<rect class="iqr" x="{x(stats["q1"]):.1f}" y="{TOP}" '
                 f'width="{max(x(stats["q3"]) - x(stats["q1"]), 1):.1f}" height="{plot_h}"/>')
    # Bars: 2px surface gap between neighbours, 4px rounded top, square at the baseline.
    base = y(0)
    for b in bins:
        if not b["count"]:
            continue
        x0, x1 = x(b["low"]) + 1, x(b["high"]) - 1
        if x1 - x0 < 1:
            x0, x1 = x0 - 0.5, x1 + 0.5
        top = y(b["count"])
        r = min(4, (x1 - x0) / 2, base - top)
        beyond = b["high"] < stats.get("low_fence", -math.inf) or b["low"] > stats.get("high_fence", math.inf)
        parts.append(
            f'<path class="bar{" outlier" if beyond else ""}" d="M{x0:.1f},{base:.1f} V{top + r:.1f} Q{x0:.1f},{top:.1f} {x0 + r:.1f},{top:.1f} '
            f'H{x1 - r:.1f} Q{x1:.1f},{top:.1f} {x1:.1f},{top + r:.1f} V{base:.1f} Z"/>')
    # Density curve over the bars.
    if curve:
        d = " ".join(f'{"M" if i == 0 else "L"}{x(p["x"]):.1f},{y(p["y"]):.1f}' for i, p in enumerate(curve))
        parts.append(f'<path class="density" d="{d}"/>')
    # Outlier fences (Tukey), dashed, only where they fall inside the axis.
    for key, label in (("low_fence", "low outlier cut-off"), ("high_fence", "high outlier cut-off")):
        fence = stats.get(key)
        if fence is not None and low <= fence <= high:
            parts.append(f'<line class="fence" x1="{x(fence):.1f}" x2="{x(fence):.1f}" y1="{TOP}" y2="{base:.1f}">'
                         f'<title>{label} {money(fence)}</title></line>')
    # Median line, labelled at the top.
    mx = x(stats["median"])
    anchor = "start" if mx < LEFT + plot_w * 0.75 else "end"
    dx = 4 if anchor == "start" else -4
    parts.append(f'<line class="median" x1="{mx:.1f}" x2="{mx:.1f}" y1="{TOP - 4}" y2="{base:.1f}"/>')
    parts.append(f'<text class="median-label" x="{mx + dx:.1f}" y="{TOP - 8}" text-anchor="{anchor}">'
                 f'median {money(stats["median"])}</text>')
    # Baseline and currency ticks.
    parts.append(f'<line class="axis" x1="{LEFT}" x2="{LEFT + plot_w}" y1="{base:.1f}" y2="{base:.1f}"/>')
    x_ticks = ticks(low, high, 5)
    step = x_ticks[1] - x_ticks[0] if len(x_ticks) > 1 else high - low
    for t in x_ticks:
        parts.append(f'<line class="axis" x1="{x(t):.1f}" x2="{x(t):.1f}" y1="{base:.1f}" y2="{base + 4:.1f}"/>')
        parts.append(f'<text class="tick" x="{x(t):.1f}" y="{base + 17:.1f}" text-anchor="middle">'
                     f'{axis_money(t, step)}</text>')
    # P99 / P99.9: short marks hanging under the baseline, labelled when they don't crowd each other.
    marks = [(label, stats.get(key)) for key, label in (("p99", "p99"), ("p99_9", "p99.9")) if stats.get(key) is not None]
    last_x = None
    for label, value in marks:
        px = x(value)
        parts.append(f'<line class="pmark" x1="{px:.1f}" x2="{px:.1f}" y1="{base:.1f}" y2="{base + 7:.1f}">'
                     f'<title>{label} {money(value)}</title></line>')
        if last_x is None or abs(px - last_x) > 34:
            parts.append(f'<text class="pmark-label" x="{px:.1f}" y="{base - 4:.1f}" text-anchor="end">{label}</text>')
        last_x = px
    parts.append(f'<text class="axis-title" x="{LEFT + plot_w / 2:.1f}" y="{HEIGHT - 4}" text-anchor="middle">'
                 'Negotiated rate</text>')
    parts.append(f'<text class="axis-title" x="4" y="{TOP - 8}">{units[1][:1].upper() + units[1][1:]}</text>')
    # Hover targets: the full height of each bin, so short bars are easy to point at.
    for b in bins:
        x0, x1 = x(b["low"]), x(b["high"])
        label = f'{b["count"]:,} {units[0] if b["count"] == 1 else units[1]} from {money(b["low"])} to {money(b["high"])}'
        parts.append(f'<rect class="hit" x="{x0:.1f}" y="{TOP}" width="{max(x1 - x0, 1):.1f}" height="{plot_h}">'
                     f'<title>{escape(label)}</title></rect>')
    parts.append("</svg>")
    return Markup("".join(parts))
