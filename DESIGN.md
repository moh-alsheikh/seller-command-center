# Design notes

This file explains **why** the project is built the way it is. It grows with each milestone.
The full build plan is in [docs/PLAN.md](docs/PLAN.md).

## The six design rules

These are the core ideas of the project. Each section below links back to them.

1. **The hub has no data tools.** It must delegate, so the hub-and-spoke pattern is enforced by
   the tool list, not just suggested in the prompt.
2. **Spokes read; the hub acts.** Every write action sits on the hub and needs human approval
   through `can_use_tool`.
3. **Least privilege.** Built-in Claude Code tools are switched off with `tools=[...]`. Each
   sub-agent gets only its own MCP tools.
4. **Metrics come from deterministic code.** RFM, cohorts and forecasts are computed in Python
   or SQL, so the numbers are exact and testable. The model interprets them; it does not
   calculate them.
5. **SQL is read-only, enforced twice:** a read-only DuckDB connection, plus a `PreToolUse`
   hook that only allows a single `SELECT`.
6. **The reference date is fixed at 2011-12-09**, the last day in the dataset, so relative
   questions ("last 30 days") give reproducible answers.

---

## Milestone 1: scaffold

### Tooling: `uv` with a lock file
`uv` manages Python 3.13, the virtual environment and dependencies. `uv.lock` pins every
package version (including transitive ones), so the Docker build and a reviewer's machine get
exactly the same versions with `uv sync --frozen`.

### `src/` layout, package named `scc`
The code lives in `src/scc/` rather than at the top level. With a `src/` layout, tests import
the installed package instead of whatever happens to be in the current directory, which
catches packaging mistakes early. The distribution is called `seller-command-center`, while
the import name is the short `scc`.

### One settings object (`src/scc/config.py`)
All tunable values are fields on one `pydantic-settings` class:

- **Typed and validated.** `SCC_MAX_TURNS=0` fails at startup with a clear error, not halfway
  through a conversation.
- **One place to look.** No other module reads `os.environ`.
- **`SCC_` prefix** on our own variables to avoid clashes. `ANTHROPIC_API_KEY` keeps its
  standard name because the SDK reads it directly.
- **`SecretStr`** for the API key and password, so they never show up in logs or `repr()`.
- **Per-request guard rails** (`max_turns`, `max_budget_usd`) are settings from day one,
  because an agent loop without limits is the most common production mistake.
- **Model choice:** Sonnet for the hub and the SQL-writing analyst (the hardest reasoning),
  Haiku for the spokes that call deterministic tools and summarise results (cheaper and
  faster). Each model can be changed with an environment variable.

### Offline tests from the start
`pytest` is configured with `asyncio_mode = "auto"` because the tool handlers, hooks and the
approval broker added later are all `async`. Unit tests must never call Anthropic or the
network; HTTP is mocked with `respx`.

---

## Milestone 2: data layer

### Pipeline: `src/scc/data/ingest.py`
`download → extract_workbook → read_sheets → normalize → remove_sheet_overlap → add_flags →
build_database`. Each step is a small function with its own test.

**What the raw data actually looks like** (checked on the real file before writing any code):

| Fact | Consequence |
|---|---|
| 1,067,371 rows in two sheets. "Year 2009-2010" ends on 2010-12-09 and "Year 2010-2011" starts on 2010-12-01. The 22,523 rows in that overlap are **identical** in both sheets. | Drop the earlier sheet's copy, which leaves **1,044,848 rows**. Ingest refuses to continue if the two copies ever differ in size, instead of guessing. |
| About 12,000 exact duplicate lines *within* each sheet. | **Kept.** We can't tell a double scan from two genuine identical lines, so we don't "clean" what we can't verify. |
| Stock codes differ only by case (`85039b` / `85039B`) or a trailing space. | Upper-case and trim: 5,305 codes become 5,131. |
| `Customer ID` arrives as a float (`12346.0`) because about 23% are blank (guest checkouts). | Stored as a nullable `BIGINT`. Customer analyses skip guests. |
| Some stock codes are not products: `POST`, `DOT`, `C2`, `M`, `D`, `S`, `B`, `BANK CHARGES`, `AMAZONFEE`, `CRUK`, `ADJUST*`, `TEST*`, `GIFT_*` vouchers. | An explicit `NON_PRODUCT_CODES` list. It is not a regex like "starts with 5 digits", because real products such as `DCGSSBOY` (party bags) and `SP1002` would fail that rule. |
| 6,116 product lines with price 0, described as "damaged", "check", "?" and so on. | Stock write-offs, not sales. Excluded by `price > 0`. |
| Invoices starting with `C` are cancellations (negative quantity). `A...` invoices are bad-debt adjustments on stock code `B`. | `is_cancellation` flag; `B` is a non-product code. |

**Atomic build.** The database is written to `retail.tmp` and renamed only when complete. The
Docker entrypoint decides whether to run ingest by checking whether the file exists, so it must
never find a half-written database. Downloads use the same `.part`-then-rename pattern.

**Archive safety.** Only the single `.xlsx` member is extracted, to a file name we choose, so an
entry like `../../x` in a malicious zip can never be written outside `data/raw/`.

**Provenance.** A `dataset_info` table records the source URL, the zip's SHA-256, row counts,
the overlap removed and the ingest time.

### Business rules live in views: `src/scc/data/db.py`
| View | Definition |
|---|---|
| `sales` | Product lines, not cancelled, `quantity > 0`, `price > 0`. `revenue = quantity × price` (GBP). **This is what "revenue" means everywhere.** |
| `returns` | Product lines on `C` invoices, with signs flipped so `quantity_returned` and `refund_value` are positive. |
| `products` | One row per stock code: most common description, lifetime units and revenue, median price. |

The analytics functions, the agents' SQL and the eval reference queries all read these views,
so a figure such as "revenue" is defined in exactly one place. Check after ingest: an
independent query on the raw sheets gives the same **£19,700,954.46 across 1,015,088 sales
lines**.

**Gross vs net.** The raw data contains huge orders that were cancelled the same day. For example,
80,995 units of `23843` "Paper Craft, Little Birdie" (£168,469.60) were cancelled on
2011-12-09. Ranked by gross revenue, that cancelled order is the "top product of 2011".
Therefore:
- `top_products` ranks by **net revenue** (sales minus refunds in the same window) by
  default. Gross revenue is still available with `by="revenue"`.
- The forecast works on **net units**, floored at 0 per month.
- The `sales` view stays gross. Net = `sales` − `returns`, written out in the schema notes the
  agents read.

### Read-only, layer 1: the connection
`connect_readonly()` opens DuckDB with `read_only=True` **and**
`enable_external_access=False`. Tested on DuckDB 2.0: read-only alone still allows
`SELECT * FROM read_csv('/etc/hosts')`. Turning off external access blocks file reads, `ATTACH`,
`COPY ... TO`, `INSTALL`, and switching itself back on. One connection is opened per request
(it takes milliseconds), so concurrent requests never share connection state.

### Read-only, layer 2: the SQL guard (`src/scc/data/sql_guard.py`)
It parses the SQL with `sqlglot`, a real parser, because a regex like "starts with SELECT"
lets through `SELECT 1; DROP TABLE x`. The rules:
1. Exactly one statement, and it must be a query (`SELECT`, `WITH`, `UNION`; DuckDB's
   `FROM x SELECT y` also works).
2. No write, DDL, `SET`, `PRAGMA`, `ATTACH` or `COPY` nodes anywhere in the tree.
3. Only allow-listed tables, plus the query's own CTE names. No schema prefixes
   (`information_schema.tables`), no table functions (`read_csv(...)`), no file paths.
4. No functions that read settings or files (`current_setting`, `getenv`, `read_*`,
   `duckdb_*`).
5. A `LIMIT` is added, or a larger one is lowered (default 200 rows), so one query can't flood
   the model's context.

Why have both layers? The connection is the hard guarantee. The guard gives the agent a clear,
fixable error ("Unknown table 'orders'. Query one of: products, returns, sales, ..."),
narrows what can be *read*, and caps result size. If either layer has a bug, the other still
holds. In Milestone 4 the guard runs from a `PreToolUse` hook.

### Analytics (`src/scc/analytics/`): design rule 4
Every function takes `(con, as_of, ...)`, reads only the views, and returns DataFrames or dicts
with no randomness.

- **RFM** (`rfm.py`): scores use `CEIL(5 × CUME_DIST())` rather than `NTILE(5)`, so tied
  customers always get the same score. With NTILE, two customers with exactly one order could
  land in different groups because of sort order alone. Segment names come from the standard
  R×F grid (Champions, Loyal, At risk, ...). Every one of the 25 score pairs maps to a named
  segment, and a test checks that.
- **Cohorts** (`cohorts.py`): cohort = month of first purchase; retention = share of the cohort
  active k months later. *Caveat:* the 2009-12 cohort also contains everyone who was buying
  before the data starts (left-censoring), so its retention is inflated.
- **Forecast** (`forecast.py`): seasonal naive × trend:
  `units(same month last year) × units(last 3 full months) / units(same 3 months a year
  earlier)`, with the trend clamped to [0.5, 2.0]. There are explicit fallbacks:
  `seasonal_naive`, then `trailing_average`, then `no_history`. Each forecast returns its
  method and inputs so the agent can explain the number in one sentence. We didn't use
  ARIMA or Prophet: with two years of strongly seasonal data, seasonal naive is a standard,
  hard-to-beat baseline that a seller can follow. *Known limitation:* it can't see a product
  taking off. Small Popcorn Holder had already sold 6,198 units in the first 9 days of
  December 2011 against a 3,538 forecast for the month. The forecast returns `sold_so_far`
  so the agent can spot and report this.
- **Pricing** (`pricing.py`): this is a wholesaler, so one product sells at several price
  tiers at once. We report the **volume-weighted** monthly price (revenue ÷ units) and a
  per-price-point table. A plain average would over-weight small orders.
- **Catalog** (`catalog.py`): top products (net revenue by default), product profile with
  return rate and top countries, and all-words text search.

### Testing approach
The tests build a 9-row database **through the real ingest functions**, so cleaning, flags,
views and analytics are all exercised together. Every expected number is small enough to work
out by hand; `tests/conftest.py` shows the arithmetic. A separate test reproduces the
same-day bulk cancellation and checks that it doesn't top the net ranking or inflate the
forecast.

### DuckDB 2.0 details we hit
- Lambdas use `lambda x: ...`; the old `x -> ...` arrow is rejected.
- `DATE` columns come back as pandas timestamps unless `.df(date_as_object=True)` is used.
  Without it, the forecast's month lookups silently found nothing.
- `SUM(BIGINT)` returns `HUGEINT`, which pandas turns into floats, so unit sums are cast back
  to `BIGINT`.
