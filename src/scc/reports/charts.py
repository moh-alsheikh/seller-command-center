"""Turn a Dataset into a Plotly figure. One function, eight chart types, one style.

Which chart for which question (the report designer's prompt uses the same table):

    trend over time        line, area
    comparison             column (vertical bars), grouped (several series side by side)
    ranking                bar (horizontal, biggest at the top)
    parts of a whole       pie (donut), stacked (stacked columns)
    distribution           histogram

Chart titles are NOT drawn inside the figure: the report page shows them as HTML headings,
so the browser lays out Arabic (right-to-left) titles correctly.
"""

from __future__ import annotations

import plotly.graph_objects as go

from scc.reports.sources import Dataset
from scc.tools._common import ToolInputError

CHART_TYPES = ("line", "area", "column", "grouped", "stacked", "bar", "pie", "histogram")
PALETTE = ["#2563eb", "#f59e0b", "#10b981", "#ef4444", "#8b5cf6", "#06b6d4", "#84cc16", "#f97316"]


def _series(ds: Dataset) -> list[tuple[str, list, list]]:
    """(name, x values, y values) for each line/bar group.

    Several y columns -> one series each (e.g. "2026" vs "2025").
    One y column plus a series column -> one series per value of that column.
    """
    df = ds.df
    if ds.series:
        y = ds.y[0]
        return [
            (str(name), list(part[ds.x]), list(part[y]))
            for name, part in df.groupby(ds.series, sort=False)
        ]
    return [(col, list(df[ds.x]), list(df[col])) for col in ds.y]


def make_figure(ds: Dataset, chart_type: str) -> go.Figure:
    if chart_type not in CHART_TYPES:
        raise ToolInputError(f"Chart type must be one of {list(CHART_TYPES)}.")
    if chart_type != "histogram" and not ds.y:
        raise ToolInputError(f"A {chart_type} chart needs at least one y column.")

    fig = go.Figure()
    if chart_type == "histogram":
        fig.add_trace(go.Histogram(x=list(ds.df[ds.x]), nbinsx=30, marker_color=PALETTE[0]))
        fig.update_layout(xaxis_title=ds.x, yaxis_title="count", bargap=0.05)

    elif chart_type == "pie":
        if len(ds.y) != 1 or ds.series:
            raise ToolInputError("A pie chart needs exactly one y column and no series.")
        fig.add_trace(go.Pie(labels=list(ds.df[ds.x]), values=list(ds.df[ds.y[0]]), hole=0.45,
                             sort=False, marker={"colors": PALETTE}))  # fmt: skip

    elif chart_type == "bar":  # horizontal ranking: biggest at the top
        for i, (name, x, y) in enumerate(_series(ds)):
            fig.add_trace(go.Bar(y=x[::-1], x=y[::-1], name=name, orientation="h",
                                 marker_color=PALETTE[i % len(PALETTE)]))  # fmt: skip
        fig.update_layout(barmode="group")

    elif chart_type in ("column", "grouped", "stacked"):
        for i, (name, x, y) in enumerate(_series(ds)):
            fig.add_trace(go.Bar(x=x, y=y, name=name, marker_color=PALETTE[i % len(PALETTE)]))
        fig.update_layout(barmode="stack" if chart_type == "stacked" else "group")

    else:  # line, area
        for i, (name, x, y) in enumerate(_series(ds)):
            fig.add_trace(go.Scatter(
                x=x, y=y, name=name, mode="lines+markers",
                fill="tozeroy" if chart_type == "area" else None,
                line={"color": PALETTE[i % len(PALETTE)], "width": 2.5},
            ))  # fmt: skip

    # Room for axis labels: a base margin, and automargin so long labels (product names on
    # a ranking) push the plot over instead of being clipped (seen in the first renders).
    fig.update_xaxes(automargin=True)
    fig.update_yaxes(automargin=True)
    # A ranking's category labels (product names) sit left of the bars: size the margin from
    # the longest label (about 7 px per character at this font size).
    left = 60
    if chart_type == "bar":
        longest = max((len(str(v)) for v in ds.df[ds.x]), default=10)
        left = min(320, 7 * longest + 24)
    fig.update_layout(
        template="plotly_white",
        margin={"l": left, "r": 20, "t": 10, "b": 40},
        separators=".,",
        height=380,
        showlegend=len(fig.data) > 1 or chart_type == "pie",
        legend={"orientation": "h", "y": -0.15},
        font={"family": "Inter, 'Segoe UI', 'Noto Naskh Arabic', Tahoma, sans-serif"},
    )
    return fig
