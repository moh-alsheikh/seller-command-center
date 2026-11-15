# Seller Command Center

**A multi-agent "chief of staff" for an online shop owner, built with the Claude Agent SDK
(Python).**

The seller asks a business question in plain English. A **hub** agent decides which
**specialist agents** to involve and gives each a precise task. The specialists run
deterministic analytics over **real e-commerce data** (1M+ transactions) and report back.
The hub combines their findings into a short brief with recommended next steps. Anything that
changes something, such as a draft purchase order or a product listing, happens **only after
the seller clicks Approve**.

> **Status:** all 7 milestones are built. It runs locally or in Docker. Evals: the harness and 27
> cases are done; 2 have been run live (both pass) and **25 are pending** a full run
> ([evals/report.md](evals/report.md)).
> Design decisions: [DESIGN.md](DESIGN.md) · Build plan and progress: [docs/PLAN.md](docs/PLAN.md)

---

## Contents
1. [The use case](#1-the-use-case)
2. [What it looks like in practice](#2-what-it-looks-like-in-practice)
3. [How it works](#3-how-it-works)
4. [Quick start](#4-quick-start)
5. [Using the web UI](#5-using-the-web-ui)
6. [Using the terminal client](#6-using-the-terminal-client)
7. [The JSON API](#7-the-json-api)
8. [Configuration](#8-configuration)
9. [Testing and verification](#9-testing-and-verification)
10. [Costs](#10-costs)
11. [Project layout](#11-project-layout)
12. [Troubleshooting](#12-troubleshooting)
13. [Limitations](#13-limitations)
14. [Data and licences](#14-data-and-licences)

---

## 1. The use case

### Who it is for
The owner of a small online gift shop: a UK business that sells mainly to wholesale buyers
across Europe, with a few thousand products and a few thousand repeat customers. They have
plenty of data and no analyst. Every week they have the same kinds of questions:

| The seller wants to know... | Which specialist answers |
|---|---|
| "Why did revenue in Germany drop last month?" | **Sales analyst**: revenue, orders, trends, drivers of a change (it can write read-only SQL) |
| "Who are my best customers, and who is about to leave?" | **Customer insights**: RFM segments, cohort retention, repeat-purchase rate |
| "What should I restock for December, and how many?" | **Inventory planner**: demand history and seasonal forecasts |
| "What should this cost in euros? Are my bulk prices working?" | **Pricing strategist**: price history, bulk price tiers, live exchange rates |
| "Write a listing for my best seller." | **Listing writer**: listings built only from real sales facts |

A **chief of staff** (the hub agent) sits in front of the specialists. It works out which of
them a question needs, runs independent ones in parallel, and turns their reports into one
brief the seller can act on.

### What it can do for the seller
- **Answer questions with exact figures** from the shop's own data, with periods and caveats
  stated (for example "December is a partial month", "guest checkouts excluded").
- **Explain changes**, not just report them, by breaking a change down by country, product,
  customer and order size.
- **Recommend actions** and, when asked, **prepare them**: a draft purchase order based on
  the forecast, or a listing draft saved for later.
- **Remember standing preferences** ("focus on UK and Germany", "our target margin is 40%")
  across chats.
- **Show its work**: every specialist and every tool call it made can be expanded in the UI,
  with the exact inputs and outputs.

### What makes it trustworthy (the design rules)
1. **The hub has no data tools.** It must delegate, and this is enforced in code, not just
   asked for in the prompt.
2. **Specialists only read; the hub acts, and only with approval.** Write actions need a click
   on **Approve**, and the write tool checks a one-time approval token as well.
3. **Least privilege.** No shell, file or web tools exist. Each specialist sees only its own
   tools.
4. **Numbers come from code, not from the model.** Metrics (RFM, cohorts, forecasts, revenue)
   are deterministic, tested functions. The model chooses and explains them; it doesn't do
   the arithmetic.
5. **SQL is read-only, enforced twice**: a locked-down database connection plus a SQL parser
   check before every query.
6. **"Today" is fixed at 2011-12-09**, the last day in the data, so answers are reproducible.

Each rule is explained, with the alternatives considered, in [DESIGN.md](DESIGN.md).

---

## 2. What it looks like in practice

**Question:** *"Why did revenue in Germany change in Nov 2011 vs Oct, and what should I
restock?"*

1. The hub starts the **Sales analyst** and the **Inventory planner** at the same time.
2. The sales analyst calls `revenue_summary` for both months, then writes three SQL queries
   to find the drivers (products, customers, order sizes).
3. The inventory planner forecasts December demand for the top sellers and checks Germany's
   monthly history for each.
4. The hub replies (shortened):

> **Germany's net revenue fell 16.8% in November**, from £28,016 (1–31 Oct 2011) to £23,304
> (1–30 Nov 2011). Orders rose from 56 to 64, but orders got smaller and refunds more than
> doubled...
>
> **Restock:** WW2 Gliders (~2,900 more units expected in December), Mini Paint Set (~2,900),
> ... Small Popcorn Holder has already sold past its December forecast, so treat that as a
> minimum.
>
> **Recommended next steps:** ... Tell me if you want a draft purchase order for these
> products (it needs your approval). Check stock on hand first; I have no stock data.

It took 29 seconds and cost $0.07. Every figure above appears in the tools' outputs.

**Follow-up (illustrative):** *"Yes, create a draft PO for those four, 2,000 units each."*
The hub calls `create_purchase_order`, and the UI shows:

> **Approval needed**
> Create a DRAFT purchase order: 4 line(s), 8,000 units in total. ...
> [✅ Approve] [❌ Deny]

Nothing is written until the seller clicks. If they deny, the hub says so and doesn't retry.

---

## 3. How it works

```
                         seller (web UI or terminal)
                                     │
                                     ▼
          ┌─────────────────── HUB: chief of staff (Sonnet) ───────────────────┐
          │ tools: Agent (start specialists), remember/forget_preference,      │
          │        save_listing_draft*, create_purchase_order*                 │
          │        (* = seller must click Approve)                             │
          └──────┬────────────┬──────────────┬──────────────┬───────────┬──────┘
                 ▼            ▼              ▼              ▼           ▼
           sales_analyst  customer_     inventory_     pricing_     listing_
           (Sonnet)       insights      planner        strategist   writer
                          (Haiku)       (Haiku)        (Haiku)      (Haiku)
           revenue_summary rfm_segments forecast_demand price_history get_product
           describe_schema segment_...  demand_history  fx_rates ──► Frankfurter
           run_sql         cohort_...   search_products search_...   (live FX)
                           repeat_...                                top_products
                 │            │              │              │           │
                 └────────────┴──── read-only DuckDB (1,044,848 rows) ──┘
```

| Layer | Technology |
|---|---|
| Agents | Claude Agent SDK: `ClaudeSDKClient` for chats, sub-agents via `AgentDefinition` |
| Tools | 18 in-process MCP tools (`@tool` + `create_sdk_mcp_server`): 14 read-only for the specialists, 4 for the hub |
| Permissions | One `can_use_tool` policy for every call, plus `PreToolUse` hooks (hub guard, SQL guard) |
| Analytics data | DuckDB, read-only, built from UCI Online Retail II |
| App state | SQLite: chats, costs, preferences, approvals, drafts, purchase orders, audit log |
| Chat history | Chainlit SQLAlchemy data layer on SQLite (threads, messages, steps), linked to SDK sessions for `resume=` |
| Live API | Frankfurter (European Central Bank exchange rates, no key) |
| UI | Chainlit (pure Python), mounted inside FastAPI on one port |

How a request flows, including where approvals and audit records come from, is covered
milestone by milestone in [DESIGN.md](DESIGN.md).

---

## 4. Quick start

### Prerequisites
- macOS or Linux, **Python 3.13**, and [uv](https://docs.astral.sh/uv/)
- An **Anthropic API key**. That's the only key needed. The Claude Code CLI comes bundled
  with the SDK, so Node isn't needed.
- About 200 MB of disk space for the dataset and database

### Option A: Docker (simplest)

Requires Docker (Desktop) and an Anthropic API key.

```bash
cp .env.example .env              # then put your ANTHROPIC_API_KEY in .env
docker compose up -d --build      # first build ~5-15 min; first start builds the database (1-2 min)
#   open http://localhost:8000 and log in: seller / seller-demo
docker compose logs -f app        # follow the logs
docker compose down               # stop (everything in ./data is kept)
```

Everything the app stores lives in `./data` on your machine: the database, chats, approvals,
purchase orders and the agent's conversation files. It survives restarts and rebuilds. The
port is published on `127.0.0.1` only. `make docker-up` / `make docker-down` do the same.

### Option B: local Python

```bash
# 1. Install (creates .venv with the exact versions from uv.lock)
uv sync

# 2. Configure: add your key to .env (this file is git-ignored)
cp .env.example .env
#    then edit .env:  ANTHROPIC_API_KEY=sk-ant-...

# 3. Build the database: downloads ~45 MB once, takes 1-2 minutes
uv run python -m scc.data.ingest

# 4. Start the web UI and API
uv run uvicorn scc.api.app:create_app --factory --port 8000
#    open http://localhost:8000 and log in with the demo account:
#    user: seller   password: seller-demo   (set in .env; change it before sharing the app)
```

Expected output of step 3:

```
      transactions: 1044848
     first_invoice: 2009-12-01 07:45:00
      last_invoice: 2011-12-09 12:50:00
       sales_lines: 1015088
 sales_revenue_gbp: 19700954.46
          products: 4725
         customers: 5852
```

Check that everything is up:

```bash
curl http://localhost:8000/health
# {"status":"ok","version":"0.1.0","as_of":"2011-12-09","retail_db":true,"app_db":true}
```

Run commands from the project root: Chainlit reads `.chainlit/config.toml` and `chainlit.md`
from the working directory. Docker and local runs can share `./data`, but don't run both at
once (they use the same port and files). The `Makefile` has shortcuts for everything: `make install`,
`make ingest`, `make serve`, `make test`, `make chat`.

---

## 5. Using the web UI

Open **http://localhost:8000** and log in (demo: `seller` / `seller-demo`, from `.env`).

- **Chat history.** Every chat is saved. Past chats are listed in the sidebar; reopen one
  and both the messages and the agent's memory of the conversation come back, so you can
  just continue ("and what about France?"). Reloading the page doesn't lose anything.
- **Starters.** Four example buttons on the welcome screen (revenue by country, December
  restock, best customers, listing for the top product). Click one to try it.
- **Ask anything** about sales, customers, stock, prices or listings. Follow-up questions keep
  the context ("and what about France?").
- **Watch the work.** Each specialist appears as a collapsible step, such as
  *"Used Inventory planner · 4.1s"*. Expand it to see each tool it called, with the exact
  input and output (JSON).
- **Approve or deny actions.** When the hub wants to save a listing draft or create a draft
  purchase order, you see a summary of exactly what will happen, then **✅ Approve /
  ❌ Deny**. With no answer in 5 minutes, the action is denied. The summary stays in the chat
  as a record.
- **Stop.** The stop button interrupts a long answer. The conversation is kept.
- **Preferences.** Say "remember that we focus on Germany and the UK" and it's applied to
  every later question, in every chat. Say "forget that preference" to remove it.
- **Cost.** Under each answer: number of turns, duration and cost, e.g. *3 turns · 29.0s ·
  $0.0719*.

**Login and history go together.** Chainlit keeps history per user, so it needs a login.
Both are on when `SCC_APP_USER` and `SCC_APP_PASSWORD` are set (the defaults in
`.env.example`). Remove both for an anonymous UI without history. The secret that signs login
cookies is generated on first start (`data/.chainlit_auth_secret`); set
`CHAINLIT_AUTH_SECRET` yourself only if several servers must share it.

### Good questions to try
- *How has net revenue developed by month in 2011 for our top 5 countries?*
- *Which customers in the "Can't lose them" segment spent the most, and when did they last order?*
- *What should I restock for the rest of December? Then draft a purchase order for the top 3.*
- *What does the Regency Cakestand cost in EUR and USD, and how do its bulk price tiers work?*
- *Write a listing for our best-selling product of 2011 and save it as a draft.*
- *Remember: our target margin is 40%.*

---

## 6. Using the terminal client

The CLI uses the same runtime as the web UI.

```bash
uv run scc ask "Why did revenue in Germany change in Nov 2011 vs Oct?"
uv run scc ask -v "..."          # verbose: also print every specialist tool call
uv run scc chat                  # multi-turn chat; type /exit to leave
uv run scc chat --resume <id>    # continue an earlier chat (our id or the SDK session id)
uv run scc sessions              # recent chats with request count and total cost
```

Approvals appear as a `y/N` prompt. If no terminal is attached (piped input), they are denied.

---

## 7. The JSON API

Read-only views of the app's state, served next to the UI. Interactive docs:
**http://localhost:8000/docs**.

| Endpoint | Returns |
|---|---|
| `GET /health` | `ok` / `degraded` (HTTP 503) with database checks; used by container health checks |
| `GET /api/sessions` | Recent chats with request count and cost |
| `GET /api/sessions/{id}/trace` | One chat: every request (cost, turns, duration) and every audit record (tool calls, specialists, approvals, denials) |
| `GET /api/purchase-orders` | Draft purchase orders, each linked to the approval that allowed it |
| `GET /api/drafts` | Saved listing drafts |
| `GET /api/approvals` | Every approval request and its decision (seller, timeout, policy) |
| `GET /api/memory` | Saved seller preferences |
| `GET /api/audit?limit=200` | Latest audit records across all chats |

The API is **read-only on purpose**. The only way to change anything is through the agent and
its approval flow, so every change is approved and audited. Set `SCC_API_TOKEN` to require
`Authorization: Bearer <token>` on `/api/...`.

---

## 8. Configuration

Everything is set through environment variables or `.env`. Defaults are in
[src/scc/config.py](src/scc/config.py).

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | (required) | The only API key the project needs |
| `SCC_HUB_MODEL` | `claude-sonnet-5-5` | Hub (chief of staff) |
| `SCC_ANALYST_MODEL` | `claude-sonnet-5-5` | Sales analyst (writes SQL) |
| `SCC_SPOKE_MODEL` | `claude-haiku-5-5` | The other four specialists |
| `SCC_MAX_TURNS` | `30` | Turn limit per request |
| `SCC_MAX_BUDGET_USD` | `1.00` | Hard cost limit per request |
| `SCC_MAX_CONCURRENT_REQUESTS` | `4` | Requests running at the same time, across all chats |
| `SCC_APPROVAL_TIMEOUT_S` | `300` | Seconds before an unanswered approval is denied |
| `SCC_DATA_DIR` | `./data` | DuckDB, SQLite and the downloaded dataset |
| `SCC_FX_BASE_URL` | `https://api.frankfurter.dev/v1` | Exchange-rate service (fallback: `api.frankfurter.app`) |
| `SCC_APP_USER` / `SCC_APP_PASSWORD` | `seller` / `seller-demo` in `.env.example` | UI login **and** chat history (remove both to turn off) |
| `CHAINLIT_AUTH_SECRET` | generated | Signs login cookies; auto-created in `data/` on first start |
| `SCC_API_TOKEN` | unset | Bearer token for `/api/...` |
| `APP_UID` (Docker build arg) | `1000` | Container user id; on Linux set it to `$(id -u)` so files in `./data` stay yours |

---

## 9. Testing and verification

### Offline unit tests (free, no network)

```bash
uv run pytest        # 257 tests, ~10 seconds
make lint            # ruff check + format check
```

They need neither an API key nor network access. They cover: data cleaning rules, the
business-rule views, the SQL guard (with injection attempts), every analytics function with
hand-checked numbers, every tool handler including its error messages, the FX client
(retries, fallback, cache; HTTP mocked), the permission policy table, approval tokens, hooks,
the hub wiring, the event translator, the UI controller, the API routes, and Chainlit's real
data layer running on our chat-history schema. Tests clear the app's environment variables
first, so your `.env` never changes their outcome.

### Live checks (use API credit)

| Command | What it proves | Cost |
|---|---|---|
| `uv run python scripts/check_sdk.py` | The SDK behaviours the design relies on (sub-agent tool name, `disallowed_tools` scope, `can_use_tool` inside sub-agents, hook payloads) | ~$0.01 |
| `uv run scc ask "..."` | The whole agent system from the terminal | ~$0.03-0.10 |
| `uv run python evals/run_evals.py` | The eval suite: 27 cases (accuracy, routing, safety) graded automatically; see [evals/README.md](evals/README.md) | ~$1.50-4 |
| `uv run python scripts/ui_e2e.py --url http://127.0.0.1:8000` | Against a running server, like a browser: log in, approve one purchase order and deny another (check that exactly one was written), then reopen the chat and check that the history is restored **and** the agent still remembers it | ~$0.05 |

---

## 10. Costs

Measured on real runs:

| Request | Cost | Time |
|---|---|---|
| Simple action (draft PO, no analysis) | ~$0.01-0.03 | 3-10 s |
| Two specialists, ~20 tool calls (the Germany example above) | ~$0.07 | ~30 s |
| SDK behaviour checks (`check_sdk.py`) | ~$0.003 | ~30 s |

Cost controls built in: Haiku for four of the five specialists, a hard `max_budget_usd` per
request, a turn limit for each specialist and for the hub, and compact JSON tool results with
row caps. Every request's cost is stored and shown under the answer and in
`GET /api/sessions`.

---

## 11. Project layout

```
src/scc/
  config.py              all settings (pydantic-settings)
  data/                  ingest (UCI -> DuckDB), read-only connections + views, SQL guard
  analytics/             deterministic metrics: sales, RFM, cohorts, forecast, pricing, catalog
  integrations/fx.py     Frankfurter client (timeouts, retries, fallback, cache)
  tools/                 MCP tools: one server per area + hub actions and memory
  agents/                hub + 5 specialist definitions; prompts/*.md
  permissions.py         can_use_tool policy + approval broker
  hooks/                 hub guard, SQL guard, audit/tracing, memory injection
  store/app_db.py        SQLite app state
  runtime/               AgentSession (one ClaudeSDKClient per chat), SDK -> UI events
  ui/                    Chainlit adapter, UI-agnostic chat controller, chat history (persistence)
  api/app.py             FastAPI: /health, /api/*, Chainlit mounted at /
  cli.py                 `scc` terminal client
scripts/                 check_sdk.py, ui_e2e.py (live checks)
evals/                   cases.yaml, run_evals.py, report.md (see evals/README.md)
  (src/scc/evals/)       harness: cases, references, checks, runner, report
tests/                   offline pytest suite
docs/PLAN.md             build plan + progress tracker
DESIGN.md                every design decision, with the reasoning
Dockerfile, docker-compose.yml, docker/entrypoint.sh   container build and start
```

---

## 12. Troubleshooting

| Symptom | Fix |
|---|---|
| Container log says `/tmp ... is full (ENOSPC)`, or builds fail with "no space left" | Docker Desktop's virtual disk is full (it happened during testing). Free space with `docker builder prune` and `docker image prune`, or raise the disk limit in Docker Desktop settings. The compose file already keeps `/tmp` in memory. |
| `address already in use` on port 8000 | A local `uvicorn` (or another app) is using it. Stop it, or change the port mapping in `docker-compose.yml`. |
| Login fails | User and password come from `SCC_APP_USER` / `SCC_APP_PASSWORD` in `.env` (demo: `seller` / `seller-demo`). Restart after changing them. |
| `/health` returns 503 | `retail_db: false` means the database hasn't been built: run `uv run python -m scc.data.ingest` (Docker does this on first start). |
| Questions fail straight away | `ANTHROPIC_API_KEY` is missing or the API balance is used up. Check the server log. |

---

## 13. Limitations

- **Historical data.** The dataset ends on 2011-12-09, and the app treats that day as
  "today". Exchange rates also default to that date (live "latest" rates are available on
  request).
- **No stock levels or costs** in the data. Restock advice is expected demand to compare with
  stock on hand, and margins are only discussed if the seller gives a cost or target.
- **Drafts stay inside the app.** Purchase orders and listings are saved as drafts and
  aren't sent to a supplier or shop platform.
- **Simple forecasting.** Seasonal naive × trend is explainable and a strong baseline for
  two years of strongly seasonal data, but it can't see a product suddenly taking off (it
  reports units already sold so the agent can point this out).
- **Single instance.** SQLite and in-memory approval futures assume one server process. See
  DESIGN.md for what would change for a multi-instance deployment.
- **Demo login.** One user, set in `.env`. Fine for a local demo; a real deployment would use
  OAuth (Chainlit supports it) and per-user data.

---

## 14. Data and licences

- **Data:** [UCI Online Retail II](https://archive.ics.uci.edu/dataset/502/online+retail+ii)
  by Daqing Chen, [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). It's downloaded
  at ingest time and not stored in this repository.
- **Exchange rates:** [Frankfurter](https://frankfurter.dev), European Central Bank reference
  rates.
- **Code:** MIT, see [LICENSE](LICENSE).
