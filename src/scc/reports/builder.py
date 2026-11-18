"""Build one report from a spec and save it under data/reports/<id>/.

Files written:
    report.html    the report page: title, summary, charts, data tables, sources.
                   "Save as PDF" uses the browser's print dialog (print stylesheet included).
    chart-N.json   each Plotly figure, so the chat can show the charts inline
    data.xlsx      a Summary sheet plus one sheet with the data of each chart
    report.json    the spec and the data rows (an audit trail of exactly what was shown)

Safety: every piece of text the agent wrote (title, summary, chart titles, SQL column names)
is HTML-escaped. The summary is shown as plain paragraphs, never rendered as Markdown or
HTML, because it can echo catalogue text (untrusted data).
"""

from __future__ import annotations

import asyncio
import html
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.graph_objects as go

from scc.reports.charts import make_figure
from scc.reports.sources import Dataset, fetch_dataset
from scc.store.app_db import AppStore
from scc.tools._common import ToolContext, ToolInputError, run_query

MAX_CHARTS = 6
ARABIC = re.compile(r"[؀-ۿݐ-ݿ]")


@dataclass(frozen=True)
class ReportInfo:
    id: str
    title: str
    chart_titles: list[str]
    folder: Path

    @property
    def url(self) -> str:
        return f"/reports/{self.id}"

    @property
    def excel_url(self) -> str:
        return f"/reports/{self.id}/data.xlsx"

    def to_result(self) -> dict[str, Any]:
        """What the create_report tool returns to the agent (and the UI reads)."""
        return {
            "report_id": self.id,
            "title": self.title,
            "url": self.url,
            "excel_url": self.excel_url,
            "charts": self.chart_titles,
        }


def text_direction(spec: dict) -> str:
    """'rtl' for Arabic reports: requested explicitly, or detected in the title or summary."""
    if spec.get("language") == "ar":
        return "rtl"
    return "rtl" if ARABIC.search(f"{spec.get('title', '')} {spec.get('summary', '')}") else "ltr"


def validate_spec(spec: dict) -> list[dict]:
    if not str(spec.get("title") or "").strip():
        raise ToolInputError("The report needs a title.")
    charts = spec.get("charts") or []
    if not 1 <= len(charts) <= MAX_CHARTS:
        raise ToolInputError(f"A report has 1 to {MAX_CHARTS} charts (got {len(charts)}).")
    for i, chart in enumerate(charts, 1):
        if not str(chart.get("title") or "").strip():
            raise ToolInputError(f"Chart {i} needs a title.")
    return charts


async def build_report(
    spec: dict, ctx: ToolContext, store: AppStore, *, session_id: str | None, reports_dir: Path
) -> ReportInfo:
    charts = validate_spec(spec)

    def fetch_all(con) -> list[Dataset]:
        out = []
        for i, chart in enumerate(charts, 1):
            try:
                out.append(fetch_dataset(con, ctx.as_of, chart.get("data") or {}))
            except ToolInputError as err:
                raise ToolInputError(f"Chart {i} ({chart.get('title')}): {err}") from err
        return out

    datasets = await run_query(ctx, fetch_all)  # database work in a worker thread
    info = await asyncio.to_thread(_write_report, spec, charts, datasets, ctx.as_of, reports_dir)
    store.insert_report(info.id, session_id=session_id, title=info.title,
                        chart_count=len(charts))  # fmt: skip
    return info


def _write_report(
    spec: dict, charts: list[dict], datasets: list[Dataset], as_of: date, reports_dir: Path
) -> ReportInfo:
    # Build every figure first: an invalid chart fails before anything is written to disk.
    figures: list[go.Figure] = []
    for i, (chart, ds) in enumerate(zip(charts, datasets, strict=True), 1):
        try:
            figures.append(make_figure(ds, chart.get("type", "column")))
        except ToolInputError as err:
            raise ToolInputError(f"Chart {i} ({chart.get('title')}): {err}") from err

    report_id = uuid.uuid4().hex
    folder = reports_dir / report_id
    folder.mkdir(parents=True, exist_ok=True)
    for i, fig in enumerate(figures, 1):
        (folder / f"chart-{i}.json").write_text(fig.to_json())

    page = render_html(spec, charts, datasets, figures, as_of, report_id=report_id)
    (folder / "report.html").write_text(page, encoding="utf-8")
    write_excel(folder / "data.xlsx", spec, charts, datasets, as_of)
    record = {
        "id": report_id,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "as_of": as_of.isoformat(),
        "spec": spec,
        "data": [ds.df.to_dict(orient="records") for ds in datasets],
    }
    (folder / "report.json").write_text(json.dumps(record, default=str, ensure_ascii=False))
    return ReportInfo(report_id, str(spec["title"]).strip(), [c["title"] for c in charts], folder)


# --------------------------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------------------------

STYLE = """
:root { --ink:#0f172a; --muted:#64748b; --line:#e2e8f0; --accent:#2563eb; }
* { box-sizing: border-box; }
body { margin: 0; color: var(--ink); background: #f8fafc;
       font-family: Inter, "Segoe UI", "Noto Naskh Arabic", "Geeza Pro", Tahoma, sans-serif; }
main { max-width: 960px; margin: 0 auto; padding: 32px 24px 48px; background: #fff; }
header { border-bottom: 3px solid var(--accent); padding-bottom: 12px; margin-bottom: 20px; }
h1 { margin: 0 0 6px; font-size: 26px; }
.meta { color: var(--muted); font-size: 13px; }
.actions { margin: 14px 0 0; display: flex; gap: 10px; flex-wrap: wrap; }
.actions a, .actions button { font: inherit; font-size: 14px; padding: 7px 14px; border-radius: 8px;
       border: 1px solid var(--accent); background: var(--accent); color: #fff; cursor: pointer;
       text-decoration: none; }
.actions a.secondary { background: #fff; color: var(--accent); }
.summary p { line-height: 1.6; margin: 0 0 10px; }
section.chart { margin-top: 28px; }
section.chart h2 { font-size: 18px; margin: 0 0 8px; }
table.data { border-collapse: collapse; width: 100%; font-size: 12.5px; margin-top: 8px; }
table.data th, table.data td { border-bottom: 1px solid var(--line); padding: 4px 8px;
                              text-align: start; }
table.data th { color: var(--muted); font-weight: 600; }
details { margin-top: 6px; }
summary { cursor: pointer; color: var(--muted); font-size: 13px; }
footer { margin-top: 36px; border-top: 1px solid var(--line); padding-top: 12px;
         color: var(--muted); font-size: 12px; }
footer code { white-space: pre-wrap; font-size: 11px; }
@media print {
  @page { size: A4; margin: 14mm; }
  body { background: #fff; } main { padding: 0; max-width: none; }
  .actions, details > summary { display: none; }
  section.chart { break-inside: avoid; page-break-inside: avoid; }
}
"""


def _paragraphs(text: str) -> str:
    """Plain text -> escaped <p> blocks (blank line = new paragraph, newline = <br>)."""
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text or "") if b.strip()]
    return "\n".join(f"<p>{html.escape(b).replace(chr(10), '<br>')}</p>" for b in blocks)


def _table(ds: Dataset, limit: int = 30) -> str:
    df = ds.df.head(limit)
    return df.to_html(index=False, escape=True, border=0, classes="data", na_rep="",
                      float_format=lambda v: f"{v:,.2f}")  # fmt: skip


def render_html(
    spec: dict,
    charts: list[dict],
    datasets: list[Dataset],
    figures: list[go.Figure],
    as_of: date,
    *,
    report_id: str,
) -> str:
    direction = text_direction(spec)
    lang = "ar" if direction == "rtl" else "en"
    title = html.escape(str(spec["title"]).strip())
    sections = []
    for i, (chart, ds, fig) in enumerate(zip(charts, datasets, figures, strict=True), 1):
        plot = fig.to_html(full_html=False, include_plotlyjs=False, div_id=f"chart-{i}",
                           config={"displaylogo": False, "responsive": True})  # fmt: skip
        rows = len(ds.df)
        # Charts are always left-to-right (numbers, dates, product codes). Plotly does not
        # support an RTL container: inside dir="rtl" its axis labels get clipped.
        sections.append(
            f'<section class="chart"><h2>{html.escape(str(chart["title"]))}</h2>'
            f'<div class="plot" dir="ltr">{plot}</div>'
            f"<details><summary>Data ({rows} rows)</summary>{_table(ds)}</details></section>"
        )
    sources = "".join(
        f"<li>{html.escape(chart['title'])}: <code>{html.escape(ds.origin)}</code></li>"
        for chart, ds in zip(charts, datasets, strict=True)
    )
    return f"""<!doctype html>
<html lang="{lang}" dir="{direction}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<script src="/reports/assets/plotly.min.js"></script>
<style>{STYLE}</style>
<script>
  // The PDF should include the data tables, which are collapsed on screen.
  window.addEventListener("beforeprint", () =>
    document.querySelectorAll("details").forEach((d) => (d.open = true)));
</script>
</head>
<body><main>
<header>
  <h1>{title}</h1>
  <div class="meta">Seller Command Center · data up to {as_of.isoformat()} · all figures in GBP
  unless stated</div>
  <div class="actions">
    <button onclick="window.print()">Save as PDF</button>
    <a class="secondary" href="/reports/{report_id}/data.xlsx" download>Download Excel</a>
  </div>
</header>
<div class="summary">{_paragraphs(str(spec.get("summary") or ""))}</div>
{"".join(sections)}
<footer>
  <div>Sources (every number is computed from the shop's database, not written by the AI):</div>
  <ul>{sources}</ul>
  <div>Data: UCI Online Retail II (CC BY 4.0), real transactions re-dated to the present.</div>
</footer>
</main></body>
</html>
"""


# --------------------------------------------------------------------------------------------
# Excel
# --------------------------------------------------------------------------------------------

_BAD_SHEET_CHARS = re.compile(r"[\[\]:*?/\\]")


def sheet_name(index: int, title: str) -> str:
    """Excel sheet names: at most 31 characters, none of []:*?/\\ and unique (index prefix)."""
    return _BAD_SHEET_CHARS.sub(" ", f"{index} {title}")[:31].strip()


def write_excel(
    path: Path, spec: dict, charts: list[dict], datasets: list[Dataset], as_of: date
) -> None:
    summary = pd.DataFrame(
        [
            ("Report", spec["title"]),
            ("Data up to", as_of.isoformat()),
            ("Summary", spec.get("summary") or ""),
            *((f"Chart {i}", c["title"]) for i, c in enumerate(charts, 1)),
        ],
        columns=["Field", "Value"],
    )
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="Summary", index=False)
        for i, (chart, ds) in enumerate(zip(charts, datasets, strict=True), 1):
            ds.df.to_excel(writer, sheet_name=sheet_name(i, chart["title"]), index=False)
