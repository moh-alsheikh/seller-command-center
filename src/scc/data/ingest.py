"""Download the UCI Online Retail II dataset and build ``data/retail.duckdb``.

Run it with::

    uv run python -m scc.data.ingest            # skips the work if the database already exists
    uv run python -m scc.data.ingest --force    # rebuilds from the downloaded file

Pipeline (each step is a small function, so each can be unit-tested on its own):

    download()             zip from UCI, about 45 MB, cached in data/raw/
    extract_workbook()     the single .xlsx inside the zip
    read_sheets()          two sheets, "Year 2009-2010" and "Year 2010-2011" (slow: 1-2 min)
    normalize()            snake_case columns, clean types, upper-case stock codes
    remove_sheet_overlap() drop the 9 days that appear in both sheets
    add_flags()            is_cancellation, is_product
    build_database()       write table + views + metadata into a temp file, then rename

Facts about the raw data that shaped these steps (checked on the real file, 2026-11-15):
  * 1,067,371 raw rows. The sheets overlap on 2010-12-01..2010-12-09 with 22,523 rows that are
    identical in both, so the clean table has 1,044,848 rows.
  * Stock codes differ only by case in some rows ("85039b" vs "85039B"): 5,305 raw codes,
    5,131 after upper-casing and trimming.
  * "Customer ID" is read as a float (12346.0) because of blanks; we store a nullable integer.
  * Some stock codes are not products: postage, fees, manual adjustments, test items, vouchers.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import httpx
import pandas as pd

from scc.config import Settings, get_settings
from scc.data.db import create_views

log = logging.getLogger(__name__)

# Raw Excel column -> our column name.
COLUMN_MAP: dict[str, str] = {
    "Invoice": "invoice",
    "StockCode": "stock_code",
    "Description": "description",
    "Quantity": "quantity",
    "InvoiceDate": "invoice_date",
    "Price": "price",
    "Customer ID": "customer_id",
    "Country": "country",
}

# Stock codes that are not products (after upper-casing). Found by listing every code that
# does not start with five digits and reading its description. Real products with unusual
# codes (DCGS... party bags and pet items, SP1002, PADS) are deliberately NOT in this list.
NON_PRODUCT_CODES: frozenset[str] = frozenset(
    {
        "POST",  # postage
        "DOT",  # dotcom postage
        "C2",  # carriage
        "C3",  # carriage
        "M",  # manual adjustment
        "D",  # discount
        "S",  # samples
        "B",  # adjust bad debt
        "BANK CHARGES",
        "AMAZONFEE",
        "CRUK",  # charity commission
        "ADJUST",
        "ADJUST2",
        "TEST001",
        "TEST002",
        "GIFT",
    }
)
# Gift vouchers: GIFT_0001_10, GIFT_0001_20, ...
NON_PRODUCT_PREFIXES: tuple[str, ...] = ("GIFT_",)

WORKBOOK_SUFFIX = ".xlsx"


class IngestError(RuntimeError):
    """The downloaded data does not look like we expect."""


# --------------------------------------------------------------------------------------------
# Step 1-2: download and extract
# --------------------------------------------------------------------------------------------


def download(url: str, dest: Path, *, timeout_s: float = 120.0) -> Path:
    """Download ``url`` to ``dest`` unless it is already there.

    Streams into ``dest.part`` and renames at the end, so an interrupted download never leaves
    a truncated file that a later run would mistake for a complete one.
    """
    if dest.exists():
        log.info("Using cached download %s", dest)
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    log.info("Downloading %s", url)
    with httpx.stream("GET", url, follow_redirects=True, timeout=timeout_s) as resp:
        resp.raise_for_status()
        with tmp.open("wb") as fh:
            for chunk in resp.iter_bytes(chunk_size=1 << 20):
                fh.write(chunk)
    tmp.replace(dest)
    return dest


def extract_workbook(zip_path: Path, out_dir: Path) -> Path:
    """Extract the single .xlsx file from the zip and return its path.

    We extract only that one member, under a name we choose. A malicious archive entry such as
    ``../../etc/x`` can therefore never be written outside ``out_dir``.
    """
    with zipfile.ZipFile(zip_path) as zf:
        members = [m for m in zf.infolist() if m.filename.lower().endswith(WORKBOOK_SUFFIX)]
        if len(members) != 1:
            raise IngestError(f"Expected one .xlsx file in {zip_path.name}, found {len(members)}")
        target = out_dir / "online_retail_II.xlsx"
        if not target.exists():
            out_dir.mkdir(parents=True, exist_ok=True)
            with zf.open(members[0]) as src, target.open("wb") as dst:
                dst.write(src.read())
    return target


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# --------------------------------------------------------------------------------------------
# Step 3-6: read and clean
# --------------------------------------------------------------------------------------------


def read_sheets(xlsx_path: Path) -> dict[str, pd.DataFrame]:
    """Read every sheet. Text columns are read as strings so codes like '85123A' stay intact."""
    log.info("Reading %s (this takes 1-2 minutes)", xlsx_path.name)
    sheets = pd.read_excel(
        xlsx_path,
        sheet_name=None,
        dtype={"Invoice": str, "StockCode": str, "Description": str, "Country": str},
    )
    for name, df in sheets.items():
        missing = set(COLUMN_MAP) - set(df.columns)
        if missing:
            raise IngestError(f"Sheet {name!r} is missing columns {sorted(missing)}")
    return sheets


def normalize(raw: pd.DataFrame) -> pd.DataFrame:
    """Rename columns to snake_case and clean each one. Returns a new DataFrame."""
    df = raw.rename(columns=COLUMN_MAP)[list(COLUMN_MAP.values())].copy()

    df["invoice"] = df["invoice"].astype("string").str.strip()
    # Same product, different case or stray spaces: "85039b " -> "85039B".
    df["stock_code"] = df["stock_code"].astype("string").str.strip().str.upper()
    desc = df["description"].astype("string").str.strip()
    df["description"] = desc.mask(desc == "")  # empty text -> missing
    df["quantity"] = df["quantity"].astype("int64")
    df["invoice_date"] = pd.to_datetime(df["invoice_date"])
    df["price"] = df["price"].astype("float64")
    # 12346.0 -> 12346; blanks stay missing (pandas nullable integer).
    df["customer_id"] = df["customer_id"].astype("Int64")
    df["country"] = df["country"].astype("string").str.strip()
    return df


def remove_sheet_overlap(sheets: list[pd.DataFrame]) -> tuple[pd.DataFrame, int]:
    """Combine sheets in date order, dropping rows that the next sheet repeats.

    The UCI file repeats the first days of each new sheet at the end of the previous one.
    For each pair of neighbouring sheets we drop the earlier sheet's rows from the later
    sheet's first timestamp onwards, after checking both sides hold the same number of rows.

    Returns the combined frame and the number of rows removed.
    """
    ordered = sorted(sheets, key=lambda d: d["invoice_date"].min())
    kept: list[pd.DataFrame] = []
    removed = 0
    for current, nxt in zip(ordered, [*ordered[1:], None], strict=True):
        if nxt is None:
            kept.append(current)
            break
        cutoff = nxt["invoice_date"].min()
        tail = current["invoice_date"] >= cutoff
        head = nxt["invoice_date"] <= current["invoice_date"].max()
        if tail.sum() != head.sum():
            raise IngestError(
                f"Sheets overlap from {cutoff} but hold {tail.sum()} vs {head.sum()} rows; "
                "refusing to guess which copy is right."
            )
        removed += int(tail.sum())
        kept.append(current[~tail])
    combined = pd.concat(kept, ignore_index=True)
    return combined, removed


def is_product_code(code: str) -> bool:
    return code not in NON_PRODUCT_CODES and not code.startswith(NON_PRODUCT_PREFIXES)


def add_flags(df: pd.DataFrame) -> pd.DataFrame:
    """Add the two flags the views rely on."""
    df = df.copy()
    df["is_cancellation"] = df["invoice"].str.startswith("C").astype(bool)
    df["is_product"] = df["stock_code"].map(is_product_code).astype(bool)
    return df


# --------------------------------------------------------------------------------------------
# Step 7: write the database
# --------------------------------------------------------------------------------------------


def build_database(df: pd.DataFrame, db_path: Path, info: dict[str, object]) -> None:
    """Write ``transactions``, ``dataset_info`` and the views to ``db_path``.

    The database is built in ``<name>.tmp`` and renamed into place only when complete. The
    Docker entrypoint decides whether to run ingest by checking whether the file exists, so it
    must never see a half-written database.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = db_path.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)

    con = duckdb.connect(str(tmp))
    try:
        con.register("clean_df", df)
        con.execute(
            """
            CREATE TABLE transactions AS
            SELECT
                invoice, stock_code, description, quantity, invoice_date, price,
                CAST(customer_id AS BIGINT) AS customer_id, country,
                quantity * price AS line_total,
                is_cancellation, is_product
            FROM clean_df
            ORDER BY invoice_date, invoice, stock_code
            """
        )
        con.unregister("clean_df")
        create_views(con)

        con.register("info_df", pd.DataFrame([info]))
        con.execute("CREATE TABLE dataset_info AS SELECT * FROM info_df")
        con.unregister("info_df")
        con.execute("CHECKPOINT")
    finally:
        con.close()
    tmp.replace(db_path)


def summarize(db_path: Path) -> dict[str, object]:
    """Row counts and date range, printed after ingest and used as a sanity check."""
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        (rows, first, last) = con.execute(
            "SELECT COUNT(*), MIN(invoice_date), MAX(invoice_date) FROM transactions"
        ).fetchone()
        (sales_lines, revenue) = con.execute("SELECT COUNT(*), SUM(revenue) FROM sales").fetchone()
        (n_products,) = con.execute("SELECT COUNT(*) FROM products").fetchone()
        (n_customers,) = con.execute(
            "SELECT COUNT(DISTINCT customer_id) FROM sales WHERE customer_id IS NOT NULL"
        ).fetchone()
    finally:
        con.close()
    return {
        "transactions": rows,
        "first_invoice": str(first),
        "last_invoice": str(last),
        "sales_lines": sales_lines,
        "sales_revenue_gbp": round(revenue or 0.0, 2),
        "products": n_products,
        "customers": n_customers,
    }


# --------------------------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------------------------


def run(settings: Settings | None = None, *, force: bool = False) -> Path:
    """Run the whole pipeline. Returns the path of the database."""
    settings = settings or get_settings()
    db_path = settings.duckdb_path
    if db_path.exists() and not force:
        log.info("%s already exists; use --force to rebuild", db_path)
        return db_path

    zip_path = download(
        settings.uci_url, settings.raw_dir / "online_retail_ii.zip", timeout_s=300
    )
    xlsx_path = extract_workbook(zip_path, settings.raw_dir)
    sheets = read_sheets(xlsx_path)
    raw_rows = sum(len(df) for df in sheets.values())

    cleaned = [normalize(df) for df in sheets.values()]
    combined, overlap = remove_sheet_overlap(cleaned)
    final = add_flags(combined)

    info = {
        "source": "UCI Online Retail II (CC BY 4.0)",
        "source_url": settings.uci_url,
        "source_sha256": sha256_of(zip_path),
        "currency": "GBP",
        "raw_rows": raw_rows,
        "overlap_rows_removed": overlap,
        "rows": len(final),
        "first_invoice": final["invoice_date"].min(),
        "last_invoice": final["invoice_date"].max(),
        "as_of_date": settings.as_of_date,
        "ingested_at": datetime.now(UTC).replace(tzinfo=None),
    }
    build_database(final, db_path, info)
    return db_path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--force", action="store_true", help="rebuild even if the DB exists")
    args = parser.parse_args(argv)

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(message)s")
    db_path = run(force=args.force)
    for key, value in summarize(db_path).items():
        print(f"{key:>18}: {value}")


if __name__ == "__main__":
    main()
