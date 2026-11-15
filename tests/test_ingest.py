"""Tests for scc.data.ingest: cleaning rules, sheet overlap, archive safety, atomic build."""

import zipfile
from datetime import datetime

import httpx
import pandas as pd
import pytest
import respx

from scc.data.ingest import (
    IngestError,
    add_flags,
    build_database,
    download,
    extract_workbook,
    is_product_code,
    normalize,
    remove_sheet_overlap,
    summarize,
)
from tests.conftest import raw_frame


def test_normalize_renames_and_cleans():
    df = normalize(raw_frame())
    assert list(df.columns) == [
        "invoice", "stock_code", "description", "quantity", "invoice_date", "price",
        "customer_id", "country",
    ]  # fmt: skip
    assert "30003A" in set(df["stock_code"])  # upper-cased
    assert df.loc[3, "description"] == "RED MUG"  # trailing space stripped
    assert str(df["customer_id"].dtype) == "Int64"
    assert df["customer_id"].isna().sum() == 2  # guests stay missing, not 0 or NaN floats


def test_normalize_turns_blank_description_into_missing():
    raw = raw_frame()
    raw.loc[0, "Description"] = "   "
    assert pd.isna(normalize(raw).loc[0, "description"])


@pytest.mark.parametrize(
    "code,expected",
    [("85123A", True), ("DCGS0058", True), ("PADS", True), ("POST", False),
     ("AMAZONFEE", False), ("BANK CHARGES", False), ("GIFT_0001_20", False), ("M", False)],
)  # fmt: skip
def test_is_product_code(code, expected):
    assert is_product_code(code) is expected


def test_add_flags():
    df = add_flags(normalize(raw_frame()))
    assert df.loc[df["invoice"] == "C500005", "is_cancellation"].all()
    assert not df.loc[df["stock_code"] == "POST", "is_product"].any()
    assert df["is_cancellation"].sum() == 1


def _sheet(dates: list[datetime]) -> pd.DataFrame:
    return pd.DataFrame({"invoice_date": pd.to_datetime(dates), "x": range(len(dates))})


def test_remove_sheet_overlap_drops_repeated_days():
    first = _sheet([datetime(2010, 11, 30), datetime(2010, 12, 1), datetime(2010, 12, 2)])
    second = _sheet([datetime(2010, 12, 1), datetime(2010, 12, 2), datetime(2010, 12, 5)])
    combined, removed = remove_sheet_overlap([second, first])  # order is fixed internally
    assert removed == 2
    assert len(combined) == 4
    assert combined["invoice_date"].is_monotonic_increasing


def test_remove_sheet_overlap_refuses_mismatched_overlap():
    first = _sheet([datetime(2010, 12, 1), datetime(2010, 12, 2)])
    second = _sheet([datetime(2010, 12, 1), datetime(2010, 12, 3)])
    with pytest.raises(IngestError, match="refusing to guess"):
        remove_sheet_overlap([first, second])


def test_extract_workbook_ignores_other_members(tmp_path):
    zip_path = tmp_path / "data.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("../../evil.txt", "should never be written")
        zf.writestr("nested/online_retail_II.xlsx", b"fake-xlsx-bytes")
    out = extract_workbook(zip_path, tmp_path / "raw")
    assert out == tmp_path / "raw" / "online_retail_II.xlsx"
    assert out.read_bytes() == b"fake-xlsx-bytes"
    assert not (tmp_path.parent / "evil.txt").exists()


def test_extract_workbook_requires_exactly_one_xlsx(tmp_path):
    zip_path = tmp_path / "data.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("readme.txt", "no workbook here")
    with pytest.raises(IngestError):
        extract_workbook(zip_path, tmp_path / "raw")


@respx.mock
def test_download_writes_file_and_uses_cache(tmp_path):
    route = respx.get("https://example.test/data.zip").mock(
        return_value=httpx.Response(200, content=b"zip-bytes")
    )
    dest = tmp_path / "raw" / "data.zip"
    download("https://example.test/data.zip", dest)
    download("https://example.test/data.zip", dest)  # second call: cached, no HTTP
    assert dest.read_bytes() == b"zip-bytes"
    assert route.call_count == 1
    assert not dest.with_suffix(".zip.part").exists()


@respx.mock
def test_download_failure_leaves_no_file(tmp_path):
    respx.get("https://example.test/data.zip").mock(return_value=httpx.Response(500))
    dest = tmp_path / "data.zip"
    with pytest.raises(httpx.HTTPStatusError):
        download("https://example.test/data.zip", dest)
    assert not dest.exists()


def test_build_database_is_atomic_and_summarizes(tmp_path):
    db_path = tmp_path / "retail.duckdb"
    build_database(add_flags(normalize(raw_frame())), db_path, {"source": "t", "rows": 9})
    assert db_path.exists()
    assert not db_path.with_suffix(".tmp").exists()

    s = summarize(db_path)
    assert s["transactions"] == 9
    assert s["sales_lines"] == 6
    assert s["sales_revenue_gbp"] == 100.0
    assert s["products"] == 3
    assert s["customers"] == 3
