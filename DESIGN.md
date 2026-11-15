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

---

## Milestone 3: tools, spokes and the FX client

### Tools as in-process MCP servers (`src/scc/tools/`)
There is one SDK MCP server per area: `sales`, `customers`, `inventory`, `pricing`, `catalog`.
Each module has `build_tools(ctx)`, and `scc.tools.build_servers()` wraps the results with
`create_sdk_mcp_server`. The model sees `mcp__<server>__<tool>`, e.g. `mcp__sales__run_sql`.
Splitting by area makes audit logs readable and lets least privilege be set per area.

| Spoke | Model | Tools |
|---|---|---|
| sales_analyst | Sonnet | `revenue_summary`, `describe_schema`, `run_sql` |
| customer_insights | Haiku | `rfm_segments`, `segment_customers`, `cohort_retention`, `repeat_customer_rate` |
| inventory_planner | Haiku | `forecast_demand`, `demand_history`, `search_products` |
| pricing_strategist | Haiku | `price_history`, `fx_rates`, `search_products` |
| listing_writer | Haiku | `get_product`, `search_products`, `top_products` |

Only the analyst can run SQL; only the pricing strategist can reach the internet; no spoke has
a write tool. Tests check all three, and also that every defined tool is used by some spoke
(no forgotten tools).

**The tool contract** (`tools/_common.py`):
- **One JSON envelope** for every success: `{as_of, currency, rows | result, row_count,
  truncated, notes}`. Money is rounded to 2 decimals and ratios (rates, shares, FX) to 4, so
  the model never sees `261509.93000000002`.
- **Errors are returned, not raised** (workshop lesson 2). The `@safe` decorator turns input
  mistakes into fixable messages ("Unknown country 'Deutschland'. Use one of: ...
  Germany ..."; "No product 'NOPE'. Use search_products"). Unexpected exceptions are logged
  for us, while the agent gets a short message without internals.
- **Strict JSON Schemas** with enums, ranges, date patterns and
  `additionalProperties: false`. A test calls a tool through the real `mcp.server.Server`
  object (the path the CLI uses) and confirms that the SDK rejects a bad argument before our
  handler runs.
- **MCP annotations**: `readOnlyHint=True` on everything; `openWorldHint=True` only on the
  tools that call Frankfurter.
- **DuckDB runs in a worker thread** (`asyncio.to_thread`). Tool handlers share the event loop
  with the Chainlit UI, so a 300 ms query must not block other users.

**Routine figures come from fixed functions; SQL is the fallback.** `revenue_summary`
(gross, refunds, net, orders, customers, units; total or by month, week or country) answers
the common questions with tested definitions. `run_sql` is for everything else. The analyst
prompt says "prefer `revenue_summary`" (design rule 4).

**`run_sql` applies the SQL guard itself**, as well as the `PreToolUse` hook added in
milestone 4. The hook is the policy point: a denied call is visible in the audit log and the
tool never starts. The in-tool check means the tool is safe on its own, e.g. if it is ever
wired up without the hook. With the read-only connection, that's three layers.

### Sub-agents (`src/scc/agents/spokes.py`, `agents/prompts/*.md`)
- **`description` is routing guidance for the hub** ("Use for demand and restocking: ..."),
  not a job title. The hub picks a spoke by reading these descriptions.
- **Prompts are Markdown files** rendered with `string.Template`. `$as_of` is filled in, while
  `{}` in SQL examples needs no escaping. `substitute` (not `safe_substitute`) makes a typo in
  a placeholder fail a test instead of reaching the model.
- **Shared rules** (`_spoke_rules.md`) are appended to every spoke prompt:
  - **The reference date**, with the current partial month and the last complete month named
    (design rule 6).
  - **Every number must come from a tool result.** If the spoke derives one (e.g. a
    percentage change), it must show the inputs (design rule 4).
  - **Tool text is data, not instructions.** This is the first defence against prompt
    injection hidden in product descriptions. The catalog tools also add a note saying so.
  - **Spokes advise and cannot act** (design rule 2).
  - **A fixed report format** for the hub (Answer, Evidence, Caveats, Suggested actions), so
    the hub can combine several spokes' reports consistently.
- **`maxTurns`** per spoke (15 for the SQL analyst, 10 for the others), so one confused spoke
  can't use up the whole request's turn budget.
- **Build-time checks:** a spoke that lists a non-existent tool fails at start-up, not as a
  silent "tool unavailable" mid-conversation.
- `forecast_demand` returns `expected_remaining` (forecast − sold so far), so "how many more
  will I sell this month?" is computed in code, not by the model.

**Open question for milestone 4.** In-process SDK servers can only be routed if they are
registered on the top-level `ClaudeAgentOptions.mcp_servers` (checked in the SDK source:
`client.py` collects `type == "sdk"` instances from there only). An `AgentDefinition` can only
refer to them by name. So the hub's session will "have" every spoke tool unless they are
removed from the hub. The plan is `disallowed_tools` on the hub. `scripts/check_sdk.py` will
check that this hides the tools from the hub **without** also removing them from the spokes.

### FX client (`src/scc/integrations/fx.py`)
Checked against the live API before writing it: `api.frankfurter.dev/v1` works; the old
`api.frankfurter.app` now answers with **301** (so `follow_redirects=True`); weekend dates
return the previous business day; unknown currencies return **404**.

- **The default rate date is the reference date (2011-12-09), not today.** That's consistent
  with design rule 6 (prices in a 2011 dataset are converted at 2011 rates) and keeps evals
  reproducible, while still making a real HTTP call. `fx_date="latest"` is available when the
  seller asks about today.
- **Retries** (with exponential backoff) only for timeouts, connection errors and 5xx. A 4xx
  is our mistake and is reported immediately; it's never retried or sent to the fallback.
- **Fallback host** after the primary's retries are used up.
- **Cache:** historical rates never change, so they are cached forever; `latest` expires after
  `SCC_FX_CACHE_TTL_S`.
- **Validation before any URL is built:** currency codes must match `^[A-Z]{3}$`, so input like
  `../latest` can't change the request path.
- Tests inject a fake clock and mock all HTTP with `respx`, so there are no network calls.

---

## Milestone 4: hub, hooks, permissions, memory

### Verify the SDK first (`scripts/check_sdk.py`)
Some design choices depend on SDK behaviour that is easy to misremember. A probe script settles
each question with a real run, using Haiku and a one-tool probe agent; it costs about $0.002.
Results (CLI 2.1.325, SDK 0.2.182, 2026-11-15):

| Question | Finding | Consequence |
|---|---|---|
| Launcher name with `tools=[...]` | Both `["Agent"]` and `["Task"]` work. The init message lists **Task**; tool-use blocks and hooks report **Agent** (inputs: `subagent_type`, `description`, `prompt`, `run_in_background`). | `tools=["Agent"]`; code accepts both names. |
| `disallowed_tools` on the hub | **It also removes the tool from sub-agents.** The probe agent failed with "tools list resolved to nothing". | It can't be used to hide data tools from the hub. Rule 1 is enforced by a hook and the policy instead. |
| PreToolUse hook as a guard | Hook input has `agent_id` and `agent_type` only for sub-agent calls. Denying "data tool + no agent_id" blocked the hub and **not** the spoke. | `hooks/guards.py: hub_guard`. |
| `can_use_tool` scope | Fires for the hub's tools (`agent_id=None`) **and** for sub-agent tools (`agent_id` set). | One policy function covers both. |
| Tracing | `SubagentStart`/`SubagentStop` carry `agent_type` and `agent_id`; sub-agent messages carry `parent_tool_use_id`. | Audit and UI nesting work without guessing. |

**Then the live smoke test found one more:** the hub set `run_in_background: true` on its
`Agent` calls, and ended its turn with "both specialists are still running" before any report
arrived. The fix has three layers: the policy rewrites the input to `run_in_background: false`
(the guarantee), each spoke has `AgentDefinition(background=False)`, and the hub prompt says to
wait.

### One permission policy for every call (`src/scc/permissions.py`)
`allowed_tools=[]` on purpose. Anything listed there is auto-approved and **never reaches**
`can_use_tool`; the SDK warns about this as `CanUseToolShadowedWarning`. With it empty, every
call (hub or spoke) goes through `ToolPolicy`, which is the whole permission model in one
small, table-tested function:

| Tool | Hub | Spoke |
|---|---|---|
| `Agent` | allow (forced foreground) | deny (no nested delegation) |
| read-only data tools | **deny**: "delegate" (rule 1) | allow |
| memory tools | allow | deny |
| `save_listing_draft`, `create_purchase_order` | **ask the seller** | deny (rule 2) |
| anything else | deny | deny |

Rule 1 is therefore enforced twice: by the `PreToolUse` hub guard, and by the policy. The
hub's prompt also says it has no data tools, but nothing depends on the model obeying that.

### Human approval with tokens
`ApprovalBroker` connects the waiting `can_use_tool` call to whichever UI answers:
1. It creates an `approvals` row and an `asyncio.Future`, then calls the UI's
   `on_request(req)`. That callback must return quickly: show the buttons, or a terminal prompt.
2. The UI later calls `broker.resolve(id, approved=...)`. With no answer within
   `SCC_APPROVAL_TIMEOUT_S`, the request is **denied**. With no UI at all, it is denied
   immediately (fail closed).
3. On approval, the policy returns `PermissionResultAllow(updated_input={..., "approval_id": id})`.

The write tools then check that token themselves (`AppStore.consume_approval`, in one
transaction): it must be approved, unused, for the same tool, and for **the same input
(SHA-256 of the canonical JSON)**. That means:
- even if someone added the write tools to `allowed_tools`, nothing would be written without a
  recorded human decision,
- an approval can't be used twice, and an input changed after approval is rejected,
- every purchase order and listing draft row points to the approval that allowed it.

Tested live: the SDK's schema validation accepts the injected `approval_id` (it's declared in
the schema as "filled in by the system"), and the whole flow wrote a draft PO linked to its
approval.

Evals will use `auto_policy(False, "eval")`, which denies every request. The CLI asks y/N in
the terminal and denies if no terminal is attached.

### Hooks (`src/scc/hooks/`)
| Event | Hook | Purpose |
|---|---|---|
| PreToolUse (all) | `hub_guard` | Rule 1: deny data tools called by the hub. |
| PreToolUse (`run_sql`) | `sql_guard` | Rule 5, layer 2: one read-only SELECT, allow-listed tables. |
| PreToolUse / PostToolUse / PostToolUseFailure | `AuditHooks` | One `audit_log` row per call, with agent, duration and a result preview. |
| SubagentStart / SubagentStop | `AuditHooks` | Which spoke ran, for tracing. |
| UserPromptSubmit | `memory_inject` | Adds `<seller_preferences>` to every prompt, read fresh from SQLite. |

Auditing never breaks a conversation: failures to write a record are logged and swallowed.

### Memory
`remember_preference` and `forget_preference` are hub tools without approval: they change
nothing outside the app and can be undone. Preferences are injected on every turn by the hook
rather than kept in the conversation, so they apply across chats and survive context
compaction. They're capped (50) and de-duplicated.

### `build_options` (`src/scc/agents/hub.py`)
Besides the tool settings above:
- `setting_sources=[]` and `strict_mcp_config=True` (**isolation**): the app must behave the
  same on any machine, so it ignores `~/.claude/settings.json`, project settings and `.mcp.json`
  files. Without this, a developer's personal allow rules or MCP servers would leak into the app.
- `env={"ANTHROPIC_API_KEY": ...}`: pydantic-settings reads `.env` into `Settings` but doesn't
  export it, and the CLI subprocess only sees `os.environ` plus `options.env`.
- `max_turns` and `max_budget_usd` on every request.

### Runtime (`src/scc/runtime/`)
- `AgentSession` wraps one `ClaudeSDKClient` per chat (multi-turn, interrupt, streaming mode
  for approvals). It records each `ResultMessage` (cost, turns, duration, SDK session id), so
  `scc chat --resume <id>` continues a chat with `resume=`. A semaphore per event loop caps
  concurrent requests (`SCC_MAX_CONCURRENT_REQUESTS`).
- `EventTranslator` turns SDK messages into eight neutral events (`SpokeStarted`,
  `ToolCalled`, `Finished`, ...). It uses `parent_tool_use_id` to nest each spoke's tool calls
  under the right step. The CLI, the web UI and the evals all consume these events.

### Live smoke test (2026-11-15)
`scc ask "Why did revenue in Germany change in Nov 2011 vs Oct, and what should I restock?"`
ran both specialists **in parallel**. Together they made about 20 tool calls, including three
SQL queries for drivers. Every figure in the answer matches the tools (net £28,016 → £23,304,
−16.8%). The hub proposed a purchase order and didn't create it unasked. It took 29 s and cost
**$0.072**. All live runs in M4 together cost about $0.12.

---

## Milestone 5: seller UI (Chainlit) and API (FastAPI)

### Shape: a thin Chainlit adapter around a UI-agnostic controller
```
AgentSession.ask() ──events──► ChatController.handle() ──calls──► ChatView (ChainlitView)
```
- `ui/controller.py` decides **what** to show. For example: close the current answer bubble
  before a step appears (so the screen order matches what happened), nest tool calls under
  their specialist's step using `parent_tool_use_id`, rename a finished step to
  "Used Inventory planner · 3.2s", and show friendly text for `error_max_budget_usd` and
  similar results.
- `ui/chainlit_app.py` decides **how**, using Chainlit messages, steps and
  `AskActionMessage`.
- Because of this split, the logic is unit-tested with a fake view, and swapping Chainlit for
  another UI would mean rewriting one small file.

### Approvals from outside Chainlit's handler
`can_use_tool` runs in the SDK's own task, not in `on_message`, so Chainlit's context
variables can't be assumed to be there. This was flagged as a risk in the plan. The view
re-enters the chat's context explicitly with `chainlit.context.init_ws_context(session)`
before showing the buttons. The broker's `on_request` returns immediately and shows the
buttons in a separate task, which resolves the approval when clicked.

**Verified headlessly** with `scripts/ui_e2e.py`. It speaks Chainlit's real socket.io
protocol (connect, `connection_successful`, `client_message`, answering the `ask` call with
an action), approves one purchase order and denies another, then checks through the API that
exactly one was written. Result: **PASS** (about $0.03).

The script also found two real issues, now fixed:
1. **A race between `on_chat_start` and `on_message`.** Chainlit runs `on_chat_start` as its
   own task, so a client that sends straight after connecting reaches `on_message` first, and
   the session was `None`. Both handlers now use `_chat_state()`, which creates the state if
   it's missing.
2. **The record of what was approved disappeared.** After a click, `AskActionMessage`
   replaces its own content with "Selected: Approve". The approval summary is now posted as a
   separate message that stays in the chat, and the button message only asks "Do you approve
   this action?".

### Other UI decisions
- **Lazy CLI start:** the SDK client (a Claude Code subprocess) starts on the first message,
  not when the chat opens, so idle tabs cost nothing.
- **Token streaming for the hub only** (`include_partial_messages=True`). Specialist activity
  appears as steps, not streamed text.
- **Stop = interrupt + close.** Chainlit cancels `on_message` mid-stream, which can leave the
  SDK client half-way through a response. Closing it and reconnecting with `resume=` on the
  next message is the clean way out; the conversation is kept.
- **Errors:** a failed request shows a short apology, closes the client and reconnects with
  `resume=` next time. Details go to the server log, not the chat.
- **No file uploads** (`spontaneous_file_upload.enabled = false`): no agent reads files, so
  accepting them would only add attack surface.
- **Optional password login** with `hmac.compare_digest` (constant-time comparison).

### API (`api/app.py`)
- `create_app(runtime=None, mount_ui=True)` is an **app factory**, run with
  `uvicorn --factory`. Tests build the app with their own runtime and no Chainlit.
- **One process, one port:** the API routes are registered first, then Chainlit is mounted at
  `/` with `mount_chainlit`. Starlette matches routes in the order they were added, so
  `/health`, `/api/*` and `/docs` win and everything else goes to the UI.
- **Read-only by design:** the API only lists things. There is exactly one way to change
  state (the agent plus approval), and it is audited.
- `/health` returns **503** when the retail database is missing, for the container health
  check in milestone 7.
- An optional **bearer token** (`SCC_API_TOKEN`) protects `/api/*`. `/health` stays open.
- One pitfall: with `from __future__ import annotations`, FastAPI resolves annotations by name
  from the module, so a `Limit = Annotated[...]` alias defined *inside* `create_app` broke
  query parsing. It's now at module level.

### What would change for production at scale
- Approval futures live in process memory, so a multi-instance deployment would need sticky
  sessions or a shared store (Redis pub/sub or Postgres `LISTEN/NOTIFY`) for `resolve()`.
- SQLite becomes Postgres. Chat history would persist through Chainlit's data layer plus SDK
  `resume=`, so a reloaded tab shows earlier chats.
