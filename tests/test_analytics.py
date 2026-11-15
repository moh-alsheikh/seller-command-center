"""Exact-number tests for the analytics functions, against the fixture in conftest.py.

Customers in the fixture (as of 2011-12-09):
  1: invoices 500001 (2010-11-05) and 500003 (2011-10-15); spend 20 + 10 = 30; recency 55 days
  2: invoices 500002 (2010-12-03) and 500004 (2011-11-20); spend 12 + 30 = 42; recency 19 days
  3: invoice 500008 (2011-12-09); spend 10; recency 0 days
"""

from datetime import date, datetime

import pandas as pd
import pytest

from scc.analytics import catalog, cohorts, forecast, pricing, rfm
from scc.data.db import connect_readonly
from scc.data.ingest import add_flags, build_database, normalize
from tests.conftest import RAW_ROWS, raw_frame

# --- RFM ----------------------------------------------------------------------------------


def test_customer_rfm_values_and_scores(con, as_of):
    df = rfm.customer_rfm(con, as_of).set_index("customer_id")
    assert df.loc[1, ["recency_days", "frequency", "monetary"]].tolist() == [55, 2, 30.0]
    assert df.loc[2, ["recency_days", "frequency", "monetary"]].tolist() == [19, 2, 42.0]
    assert df.loc[3, ["recency_days", "frequency", "monetary"]].tolist() == [0, 1, 10.0]
    # CUME_DIST scores: ties (customers 1 and 2 both have 2 orders) share the same score.
    assert df["r_score"].to_dict() == {1: 2, 2: 4, 3: 5}
    assert df["f_score"].to_dict() == {1: 5, 2: 5, 3: 2}
    assert df["m_score"].to_dict() == {1: 4, 2: 5, 3: 2}
    assert df["segment"].to_dict() == {
        1: "Can't lose them",
        2: "Loyal customers",
        3: "Potential loyalists",
    }


def test_customer_rfm_ignores_guests(con, as_of):
    assert rfm.customer_rfm(con, as_of)["customer_id"].notna().all()


@pytest.mark.parametrize(
    "r,f,segment",
    [(5, 5, "Champions"), (4, 4, "Loyal customers"), (3, 5, "Loyal customers"),
     (5, 1, "New customers"), (4, 1, "Promising"), (4, 3, "Potential loyalists"),
     (3, 3, "Need attention"), (3, 2, "About to sleep"), (2, 4, "At risk"),
     (1, 5, "Can't lose them"), (1, 1, "Hibernating")],
)  # fmt: skip
def test_segment_grid(r, f, segment):
    assert rfm.segment_for(r, f) == segment


def test_every_score_pair_has_a_known_segment():
    for r in range(1, 6):
        for f in range(1, 6):
            assert rfm.segment_for(r, f) in rfm.SEGMENTS


def test_segment_summary_shares_add_up(con, as_of):
    summary = rfm.segment_summary(rfm.customer_rfm(con, as_of))
    assert summary["customers"].sum() == 3
    assert summary["customer_share"].sum() == pytest.approx(1.0)
    assert summary["revenue_share"].sum() == pytest.approx(1.0)
    # Display order follows SEGMENTS (best first).
    assert summary["segment"].tolist() == [
        "Loyal customers", "Potential loyalists", "Can't lose them",
    ]  # fmt: skip


# --- Cohorts ------------------------------------------------------------------------------


def test_cohort_retention(con, as_of):
    long = cohorts.cohort_retention(con, as_of)
    got = {
        (str(r.cohort_month), r.months_since_first): (r.active_customers, r.retention_rate)
        for r in long.itertuples()
    }
    assert got == {
        ("2010-11-01", 0): (1, 1.0),
        ("2010-11-01", 11): (1, 1.0),  # customer 1 came back in Oct 2011
        ("2010-12-01", 0): (1, 1.0),
        ("2010-12-01", 11): (1, 1.0),  # customer 2 came back in Nov 2011
        ("2011-12-01", 0): (1, 1.0),
    }


def test_cohort_retention_respects_max_months(con, as_of):
    long = cohorts.cohort_retention(con, as_of, max_months=6)
    assert long["months_since_first"].max() == 0


def test_retention_matrix_shape(con, as_of):
    matrix = cohorts.retention_matrix(cohorts.cohort_retention(con, as_of))
    assert list(matrix.columns) == ["cohort_month", "cohort_size", "m0", "m11"]
    assert len(matrix) == 3


def test_repeat_customer_rate(con, as_of):
    assert cohorts.repeat_customer_rate(con, as_of) == {
        "customers": 3,
        "repeat_customers": 2,
        "repeat_rate": pytest.approx(2 / 3),
    }
    # From 2011 onwards each customer has only one invoice.
    in_2011 = cohorts.repeat_customer_rate(con, as_of, start=date(2011, 1, 1))
    assert in_2011["repeat_customers"] == 0


# --- Forecast (pure function) ---------------------------------------------------------------


def series(points: dict[str, float]) -> pd.Series:
    return pd.Series({date.fromisoformat(k): v for k, v in points.items()})


AS_OF = date(2011, 12, 9)


def test_last_complete_month():
    assert forecast.last_complete_month(AS_OF) == date(2011, 11, 1)
    assert forecast.last_complete_month(date(2011, 1, 31)) == date(2010, 12, 1)


def test_seasonal_naive_with_trend():
    s = series({
        "2010-09-01": 10, "2010-10-01": 10, "2010-11-01": 20, "2010-12-01": 50,
        "2011-09-01": 15, "2011-10-01": 15, "2011-11-01": 30, "2011-12-01": 5,
    })  # fmt: skip
    [dec] = forecast.forecast_series(s, AS_OF)
    # trend = (15 + 15 + 30) / (10 + 10 + 20) = 1.5 ; forecast = 50 * 1.5
    assert (dec.month, dec.units, dec.method, dec.trend) == (
        date(2011, 12, 1), 75.0, "seasonal_naive_x_trend", 1.5,
    )  # fmt: skip
    assert dec.last_year_units == 50
    assert dec.sold_so_far == 5


def test_trend_is_clamped():
    s = series({"2010-11-01": 10, "2010-12-01": 10, "2011-11-01": 100})
    [dec] = forecast.forecast_series(s, AS_OF)
    assert dec.trend == forecast.TREND_MAX
    assert dec.units == 20.0


def test_new_product_falls_back_to_trailing_average():
    s = series({"2011-10-01": 30, "2011-11-01": 30})
    [dec] = forecast.forecast_series(s, AS_OF)
    assert (dec.method, dec.units, dec.trend) == ("trailing_average", 20.0, None)


def test_seasonal_naive_without_trend():
    s = series({"2010-12-01": 4, "2011-12-01": 6})
    [dec] = forecast.forecast_series(s, AS_OF)
    assert (dec.method, dec.units, dec.sold_so_far) == ("seasonal_naive", 4.0, 6.0)


def test_no_history():
    [dec] = forecast.forecast_series(pd.Series(dtype=float), AS_OF)
    assert (dec.method, dec.units) == ("no_history", 0.0)


def test_horizon_produces_consecutive_months():
    s = series({"2011-10-01": 30, "2011-11-01": 30})
    months = [f.month for f in forecast.forecast_series(s, AS_OF, horizon=3)]
    assert months == [date(2011, 12, 1), date(2012, 1, 1), date(2012, 2, 1)]


# --- Forecast (database wrappers) -----------------------------------------------------------


def test_demand_history_monthly_and_filtered(con, as_of):
    df = forecast.demand_history(con, as_of, stock_code="10001")
    assert [str(p) for p in df["period"]] == ["2010-11-01", "2011-10-01", "2011-11-01"]
    assert df["units"].tolist() == [10, 5, 20]
    assert df["units_returned"].tolist() == [0, 0, 2]  # C500005 on 2011-11-21
    assert df["net_units"].tolist() == [10, 5, 18]
    assert df["refund_value"].tolist() == [0.0, 0.0, 4.0]
    germany = forecast.demand_history(con, as_of, country="Germany")
    assert germany["revenue"].sum() == pytest.approx(42.0)


def test_demand_history_rejects_unknown_freq(con, as_of):
    with pytest.raises(ValueError):
        forecast.demand_history(con, as_of, freq="day")


def test_forecast_product_from_database(con, as_of):
    [dec] = forecast.forecast_product(con, as_of, "10001")
    # No sales of 10001 in Dec 2010, so: trailing average of NET units Sep-Nov 2011
    # = (0 + 5 + (20 - 2 returned)) / 3 = 7.67.
    assert (dec.method, dec.units) == ("trailing_average", 7.7)


def test_forecast_top_products(con, as_of):
    df = forecast.forecast_top_products(con, as_of, top_n=5)
    # Only 10001 sold in the last 3 complete months (Sep-Nov 2011).
    assert df["stock_code"].tolist() == ["10001"]
    assert df.iloc[0]["description"] == "RED MUG"
    assert df.iloc[0]["recent_units"] == 23  # 5 + 18 net


# --- Pricing --------------------------------------------------------------------------------


def test_price_history(con, as_of):
    df = pricing.price_history(con, as_of, "10001")
    assert df["avg_price"].tolist() == [2.0, 2.0, 1.5]
    assert df["units"].tolist() == [10, 5, 20]


def test_price_points(con, as_of):
    df = pricing.price_points(con, as_of, "10001")
    assert df[["price", "units", "lines", "revenue"]].values.tolist() == [
        [1.5, 20, 1, 30.0],
        [2.0, 15, 2, 30.0],
    ]


# --- Catalog --------------------------------------------------------------------------------


def test_top_products_by_revenue_and_units(con, as_of):
    by_rev = catalog.top_products(con, as_of)
    assert by_rev["stock_code"].tolist() == ["10001", "20002", "30003A"]
    assert by_rev["net_revenue"].tolist() == [56.0, 30.0, 10.0]  # 10001: 60 - 4 refunded
    assert by_rev["revenue"].tolist() == [60.0, 30.0, 10.0]
    assert by_rev["units_returned"].tolist() == [2, 0, 0]
    assert by_rev["rank"].tolist() == [1, 2, 3]

    by_units = catalog.top_products(con, as_of, by="units", limit=1)
    assert by_units["stock_code"].tolist() == ["10001"]


def test_top_products_filters(con, as_of):
    germany = catalog.top_products(con, as_of, country="Germany")
    assert germany["net_revenue"].tolist() == [26.0, 12.0]
    in_2011 = catalog.top_products(con, as_of, start=date(2011, 1, 1))
    assert in_2011["net_revenue"].tolist() == [36.0, 18.0, 10.0]


def test_cancelled_bulk_order_does_not_top_the_net_ranking(tmp_path, as_of):
    # Mirrors the real data: a huge order cancelled the same day (like 23843 on 2011-12-09).
    extra = [
        ("600001", "20002", "CANDLE", 1000, datetime(2011, 12, 9, 9), 3.0, 9.0, "United Kingdom"),
        ("C600002", "20002", "CANDLE", -1000, datetime(2011, 12, 9, 10), 3.0, 9.0,
         "United Kingdom"),
    ]  # fmt: skip
    path = tmp_path / "retail.duckdb"
    build_database(add_flags(normalize(raw_frame(RAW_ROWS + extra))), path, {"source": "t"})
    with connect_readonly(path) as c:
        gross = catalog.top_products(c, as_of, by="revenue")
        net = catalog.top_products(c, as_of)
        [dec] = forecast.forecast_product(c, as_of, "20002")
    assert gross["stock_code"].iloc[0] == "20002"  # 3,030 gross
    assert net["stock_code"].iloc[0] == "10001"  # 20002 nets back to 30
    assert dec.sold_so_far == 6  # the cancelled 1,000 units are not demand


def test_top_products_rejects_unknown_ranking(con, as_of):
    with pytest.raises(ValueError):
        catalog.top_products(con, as_of, by="margin")


def test_get_product(con):
    p = catalog.get_product(con, " 10001 ")
    assert p["description"] == "RED MUG"
    assert p["units_sold"] == 35
    assert p["units_returned"] == 2
    assert p["return_rate"] == pytest.approx(2 / 35)
    # Germany and the UK tie at 30.0; ties are broken alphabetically.
    assert [c["country"] for c in p["top_countries"]] == ["Germany", "United Kingdom"]


def test_get_product_unknown(con):
    assert catalog.get_product(con, "NOPE") is None


@pytest.mark.parametrize(
    "text,codes",
    [("mug", ["10001"]), ("red MUG", ["10001"]), ("30003a", ["30003A"]), ("mug candle", []),
     ("", [])],
)  # fmt: skip
def test_search_products(con, text, codes):
    assert catalog.search_products(con, text)["stock_code"].tolist() == codes


# --- Revenue summary ------------------------------------------------------------------------


def test_revenue_summary_month_grouping(con, as_of):
    from scc.analytics.sales import revenue_summary

    df = revenue_summary(
        con, as_of, start=date(2011, 10, 1), end=date(2011, 11, 30), group_by="month"
    )
    assert [str(d) for d in df["period_or_group"]] == ["2011-10-01", "2011-11-01"]
    assert df["revenue"].tolist() == [10.0, 30.0]
    assert df["refunds"].tolist() == [0.0, 4.0]
    assert df["net_revenue"].tolist() == [10.0, 26.0]


def test_revenue_summary_end_is_capped_at_as_of(con, as_of):
    from scc.analytics.sales import revenue_summary

    df = revenue_summary(con, as_of, end=date(2030, 1, 1))
    assert df["revenue"].tolist() == [100.0]


def test_revenue_summary_rejects_unknown_grouping(con, as_of):
    from scc.analytics.sales import revenue_summary

    with pytest.raises(ValueError):
        revenue_summary(con, as_of, group_by="product")
