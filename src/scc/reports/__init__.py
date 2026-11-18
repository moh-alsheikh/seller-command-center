"""On-the-fly reports: charts, a printable HTML page (PDF via the browser) and an Excel file.

How a report is made:

    report_designer (a spoke) --create_report(spec)--> builder.build_report
        for each chart in the spec:
            sources.fetch_dataset   numbers come from code: a curated analytics function,
                                    or guarded read-only SQL written for this request
            charts.make_figure      a Plotly figure of the requested type
        write data/reports/<id>/    report.html, chart-N.json, data.xlsx, report.json

The agent decides WHAT to show (which data, which chart, the title and the summary). It never
types a number into a chart (design rule 4).
"""
