# Plan: Seller Command Center — hub-and-spoke multi-agent portfolio project

## Context
The user is studying for CCAR-F and finished the `agent-sdk-workshop`. They want a first **portfolio project** that proves production-level skill with `claude-agent-sdk`, in **e-commerce**, using a **hub-and-spoke multi-agent** design, **with no API keys apart from Anthropic**. They chose:
- **Use case:** *Seller Command Center*, a "chief of staff" for an online shop owner, over **real** transaction data (UCI Online Retail II, about 1.07M rows, 2009-12-01 to 2011-12-09, CC BY 4.0, keyless download).
- **Location:** `/Users/mohammed/Desktop/ClaudeCerts/portfolio_projects/seller-command-center` (new, separate git repo; the parent folder exists and is empty).
- **Scope:** a **Chainlit** seller UI (pure Python) mounted in a small FastAPI app, plus Docker Compose.
- **Build mode:** I build all of it in milestones; the user studies it afterwards. The code must be very readable, and there must be a `DESIGN.md` explaining every decision.

Environment checked: Python 3.13 (pyproject-managed via `uv`), Docker 25 and Node 22 present. The installed `claude-agent-sdk` (0.2.180) **bundles the Claude Code CLI** (`claude_agent_sdk/_bundled/claude`), so the Docker image needs no Node.

## Architecture

```
seller ─▶ HUB "chief_of_staff" (Sonnet) ── has NO data tools: must delegate ──▶ action brief
            │  tools: Task/Agent, remember_preference, save_listing_draft*, create_purchase_order*
            │  (* = human approval via can_use_tool → Chainlit Approve/Deny buttons)
   ┌────────┼──────────────┬───────────────┬──────────────┐
   ▼        ▼              ▼               ▼              ▼
sales_    customer_     inventory_      pricing_       listing_
analyst   insights      planner         strategist     writer
(Sonnet)  (Haiku)       (Haiku)         (Haiku)        (Haiku)
run_sql,  rfm_segments, demand_history, price_history, get_product,
schema    cohort_ret.   forecast_demand fx_rates(live  top_products
                                        Frankfurter)
```

Design rules (each one written up in DESIGN.md as an interview talking point):
1. **The hub has no data tools**, so it must delegate. That enforces the hub-and-spoke pattern rather than just suggesting it.
2. **Spokes only read and advise; the hub acts.** Every write action sits on the hub and is approval-gated through `can_use_tool`.
3. **Least privilege:** `tools=["Task"]` (built-ins are otherwise off; this fixes the gotcha found in Stage 0 of the workshop). Each `AgentDefinition` lists only its own MCP tools.
4. **Metrics come from deterministic code** (RFM, cohorts, forecast), so numbers are exact and testable. The LLM interprets them; it doesn't calculate them.
5. **Read-only data access is enforced twice:** DuckDB `read_only=True` connection, plus a `PreToolUse` hook using `sqlglot` that allows only a single SELECT.
6. **Fixed reference date:** the dataset's "today" is fixed per database build (the last day in the data) and stated in every prompt. *(Updated 2026-11-18: the demo data is re-dated by whole weeks to end on the most recent Friday; originally `2011-12-09`.)*

## Repo layout (new project)
```
seller-command-center/
  pyproject.toml  uv.lock  .env.example  .gitignore  LICENSE(MIT)  Makefile
  README.md  DESIGN.md  Dockerfile  docker-compose.yml
  src/scc/
    config.py                 # pydantic-settings: models, budgets, AS_OF_DATE, paths, APP_TOKEN
    data/ingest.py            # download UCI zip → read both sheets → normalize → DuckDB
    data/db.py                # read-only DuckDB connection + views: sales, returns, products
    data/sql_guard.py         # sqlglot: single SELECT only, row LIMIT injection
    analytics/{rfm,cohorts,forecast,pricing}.py   # pure, deterministic pandas/DuckDB functions
    integrations/fx.py        # Frankfurter client (httpx, TTL cache, timeout+retry)
    tools/                    # @tool + create_sdk_mcp_server, one server per spoke
      sales.py customers.py inventory.py pricing.py catalog.py actions.py memory.py
    agents/spokes.py          # 5 AgentDefinitions (description, prompt, tools, model)
    agents/hub.py             # hub prompt + build_options(session) → ClaudeAgentOptions
    agents/prompts/*.md       # prompts kept as files for easy reading
    hooks/                    # sql_guard (PreToolUse), audit (Post/PostFailure),
                              # tracing (SubagentStart/Stop), memory_inject (UserPromptSubmit)
    permissions.py            # can_use_tool + ApprovalBroker (asyncio.Future per request)
    store/app_db.py           # SQLite: sessions(map→sdk session_id), memory, approvals,
                              #   listing_drafts, purchase_orders, audit_log, costs
    runtime/agent_session.py  # wraps one ClaudeSDKClient: connect, ask(), interrupt, close,
                              #   concurrency cap; UI-agnostic (used by Chainlit, CLI, evals)
    runtime/events.py         # SDK messages → neutral events (text, spoke_start/stop, tool, result)
    ui/chainlit_app.py        # Chainlit handlers: on_chat_start, on_message, starters, steps
    ui/approvals.py           # can_use_tool → cl.AskActionMessage approve/deny buttons
    api/app.py                # FastAPI: /health + read-only JSON routes; mounts Chainlit at "/"
    cli.py                    # `scc chat` / `scc ask` terminal client using same runtime
  .chainlit/config.toml  chainlit.md   # UI title, theme, welcome page
  evals/cases.yaml  evals/run_evals.py  evals/README.md
  tests/                      # pytest, no API calls
  scripts/check_sdk.py        # live checks of SDK behaviour assumptions (see below)
```

## Key SDK wiring (reuse workshop patterns)
- The tool, MCP server, sub-agent, hook and memory shapes follow `agent-sdk-workshop/01-guided-demo/{tools,subagents,memory,agent.py}` and `docs/AGENTIC_LOOPS.md`. They are upgraded to: `tools=[...]` least privilege, `can_use_tool` instead of `bypassPermissions` (`permission_mode="default"`), and `max_turns` + `max_budget_usd` per request.
- **The Chainlit UI and CLI use `ClaudeSDKClient`** (multi-turn, interrupt, approvals need streaming mode). **Evals use `query()`** with `output_format` JSON schema, run concurrently. This deliberately demonstrates both, as covered in the workshop notes.
- **Sessions:** one `ClaudeSDKClient` per open chat (follow-up questions keep context). The SDK `session_id` from `ResultMessage` is logged to SQLite so the CLI can continue a chat with `scc chat --resume <id>`.
- **Memory:** a `remember_preference` tool writes to SQLite (for example "focus on UK and Germany", "target margin 40%"); a `UserPromptSubmit` hook injects it.
- **Observability:** the audit hook records tool, agent, duration, success and error; `ResultMessage` records cost, turns and tokens per request. Both are exposed in the UI's activity panel and at `GET /api/sessions/{id}/trace`.

## Seller UI (Chainlit) and API
- **Chainlit app** (`ui/chainlit_app.py`), pure Python:
  - `on_chat_start`: create an `AgentSession` (one `ClaudeSDKClient`) and store it in `cl.user_session`. `on_chat_end`: disconnect.
  - `on_message`: stream the hub's answer into a `cl.Message` token by token.
  - **Agent steps:** each spoke run is shown as a collapsible `cl.Step` ("Used inventory_planner · 3.2s") with its tool calls nested inside (input and output). Spoke messages are grouped using `parent_tool_use_id`.
  - **Approvals:** `can_use_tool` for `save_listing_draft` and `create_purchase_order` shows `cl.AskActionMessage` with **Approve / Deny** buttons and the action details, waits for the click (with a timeout, after which it denies), then returns `PermissionResultAllow` or `PermissionResultDeny`.
  - **Starters** (example buttons): "Revenue trend by country", "What should I restock for December?", "Who are my best customers?", "Write a listing for my top product".
  - Footer on each answer: cost, turns and duration from `ResultMessage`.
  - Optional login: `@cl.password_auth_callback` using `APP_USER`/`APP_PASSWORD` from `.env`.
- **FastAPI** (`api/app.py`): `GET /health`, `GET /api/memory`, `GET /api/drafts`, `GET /api/purchase-orders`, `GET /api/audit` (read-only JSON for inspection), with Chainlit mounted at `/` through `chainlit.utils.mount_chainlit`. One process, one port (8000).
- ~~A conversation lasts while the browser tab is open.~~ Changed after the M5 review: chat history is kept (login + Chainlit data layer on SQLite + `on_chat_resume` → `resume=`). See DESIGN.md.

## Docker
`python:3.13-slim` image, `uv sync --frozen`, non-root user. The entrypoint runs ingest if `/data/retail.duckdb` is missing (about 45 MB download and 1–2 min to parse, once), then uvicorn. Compose mounts `./data:/data` and reads `ANTHROPIC_API_KEY` from `.env`. Health check on `/health`.

## Evals (`evals/`)
About 25 cases, run with `query()` + structured output `{answer, value, unit, spokes_used}`:
- **Accuracy (~15):** exact figures with a tolerance check against ground truth computed by reference SQL at eval time (for example, top-5 products by 2011 revenue, Germany revenue in Nov 2011, repeat-customer rate, return rate).
- **Routing (~5):** the expected spokes were called (checked from the tracing hook log).
- **Safety (~5):** an attempt to modify data via SQL is blocked; "create a PO without asking" still triggers approval (auto-denied in eval mode); a prompt-injection string planted in a test product description is ignored.
- Output: a `evals/report.md` table with pass rate, cost and latency per case. Cost estimate: about $3–8 per full run on Sonnet + Haiku.

## Milestones (each one is a local git commit; nothing is pushed)
1. **Scaffold:** `uv init`, pyproject (claude-agent-sdk, chainlit, fastapi, uvicorn, duckdb, pandas, openpyxl, sqlglot, httpx, pydantic-settings, pytest, respx), config, .gitignore, README skeleton.
2. **Data layer:** ingest, DuckDB views, SQL guard, analytics functions, plus unit tests.
3. **Tools and spokes:** the MCP servers, 5 AgentDefinitions, FX client (tests mock HTTP with respx).
4. **Hub, hooks, permissions, memory:** build_options, hooks, ApprovalBroker, app DB, plus `scripts/check_sdk.py` and the CLI for a live smoke test.
5. **Seller UI and API:** AgentSession runtime, Chainlit app (streaming, steps, approval buttons, starters), FastAPI with Chainlit mounted.
6. **Evals:** cases, runner, report.
7. **Docker and docs:** Dockerfile, compose, README (quick start, demo script, screenshots placeholder), DESIGN.md (architecture, the 6 rules, trade-offs, what I'd do next in production).

### Progress tracker
Update this table when a milestone starts or finishes. A commit can't contain its own hash, so each row's hash is added when the next milestone starts. Status: ⬜ not started · 🟡 in progress · ✅ done.

| # | Milestone | Status | Finished | Commit | Tests | Notes |
|---|---|---|---|---|---|---|
| 1 | Scaffold | ✅ | 2026-11-15 | `90419d6` | 8 pass | SDK 0.2.182 (bundled CLI confirmed); `SCC_` env prefix; package `scc` |
| 2 | Data layer | ✅ | 2026-11-15 | `40e3d6b` | 113 pass | 1,044,848 rows after removing the 22,523-row sheet overlap; revenue cross-checked against raw data (£19,700,954.46); rankings and forecasts use net values (same-day bulk cancellations) |
| 3 | Tools and spokes | ✅ | 2026-11-15 | `e37c178` | 171 pass | 5 MCP servers / 14 tools (JSON envelope, errors returned, strict schemas); 5 AgentDefinitions with least privilege; FX defaults to as-of date; added `revenue_summary`; hub tool-visibility check moved to M4 |
| 4 | Hub, hooks, permissions, memory | ✅ | 2026-11-15 | `54fee25` | 216 pass | check_sdk: launcher=`Agent`; `disallowed_tools` propagates to sub-agents, so rule 1 is enforced by a PreToolUse hook + `can_use_tool` (allowed_tools empty); forced foreground sub-agents; approval tokens; live smoke $0.072 |
| 5 | Seller UI and API | ✅ | 2026-11-15 | `b91d799`, `0242796`, `d29a7a2` | 233 pass | Chainlit mounted at `/` in FastAPI; UI-agnostic ChatController; approvals via `init_ws_context`; read-only API; README rewritten. **Added after review: chat history** (login + Chainlit SQLAlchemy data layer on SQLite + `on_chat_resume` → SDK `resume=`); e2e PASS incl. reopen + memory |
| 6 | Evals | ✅ harness · ⏳ all 27 pending | 2026-11-15 | `591fad7`, `4e7fe60` | – | 27 cases (16 accuracy / 5 routing / 6 safety); references computed live; graded on evidence + invariants on every case; isolated workspace with planted injection. Smoke 2/2 PASS on 2011 dates (stale after re-dating). **All 27 pending** a live run; cases are now date-relative (`render_case`) |
| 7 | Docker and docs | ✅ | 2026-11-15 | `M7:` commit | 257 pass | `python:3.13-slim` + uv (frozen), non-root, healthcheck; all state in `./data` incl. CLI sessions (`CLAUDE_CONFIG_DIR`) so resume survives restarts; `/tmp` as tmpfs after a full Docker disk broke the CLI; verified in container: health, login, CLI call, restart persistence |
| + | Fresh demo dates | ✅ | 2026-11-18 | `cfb9459` | 269 pass | Real data shifted by whole weeks to end on the most recent Friday (built 2026-11-18: 2024-11-05 → 2026-11-13, shift 5453 days; totals unchanged); "today" read from `dataset_info`; prompts, UI and evals date-relative; 2011 chats archived to `data/archive-2011/`; eval report reset to 27 pending |
| + | On-the-fly reports | ✅ | 2026-11-18 | `a881353`, `971d25a` | 302 pass | 6th spoke `report_designer`; `create_report` spec: curated sources or guarded SQL (numbers from code); Plotly charts in chat; HTML report with browser Save-as-PDF; Excel; Arabic RTL; checked with headless Chrome screenshots + PDF print (3 layout bugs fixed). **Live UI check PASS** ("Show me 2026 revenue." $0.057, 2 charts; "نتائج مبيعات 2026" $0.100, 3 charts, Arabic report) |
| + | Fix: background specialists | ✅ | 2026-11-18 | `fix:` commit | 304 pass | Seller's chat ended with "still running": the CLI auto-backgrounded sub-agents ("Async agent launched"); the M4 input rewrite never stopped it. Now `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` + policy layer. Replayed live: 0 async launches, full answer + report in one turn |

## Assumptions to verify early (`scripts/check_sdk.py`, milestone 4)
- The name of the sub-agent launcher tool when `tools=[...]` restricts built-ins (`"Task"` vs `"Agent"`). The script checks which one starts a spoke.
- Whether `can_use_tool` fires for tool calls made inside sub-agents. The design doesn't depend on it, because all gated actions sit on the hub, but the result is documented.
- That the `SubagentStart`/`SubagentStop` hook inputs carry the agent name for tracing. Fallback: trace from `ToolUseBlock` input `subagent_type`.
- The Frankfurter base URL (`api.frankfurter.dev/v1` vs the older `.app`), with a fallback.
- That `can_use_tool` and hook callbacks run inside Chainlit's context, so `cl.AskActionMessage` works from them. Fallback: pass the Chainlit session through an `asyncio.Queue` bridge.
- That spoke messages carry `parent_tool_use_id`, so steps can be nested under the right spoke.
- The real UCI column names (`Invoice`, `Price`, `Customer ID`) and normalization to snake_case.

## Verification
1. `uv run pytest` — all unit tests pass offline (SQL guard, analytics numbers, FX mocked, approval broker, memory store, API routes with a fake runtime).
2. `uv run python -m scc.data.ingest` → print row counts (expect about 1,067,371 raw rows) and date range.
3. `uv run python scripts/check_sdk.py` → reports the tool name, hook payloads and whether `can_use_tool` fires in sub-agents.
4. `uv run scc ask "Why did revenue in Germany change in Nov 2011 vs Oct, and what should I restock?"` → several spokes start; the answer cites exact figures.
5. `uv run python evals/run_evals.py` → `evals/report.md`, target ≥ 85% pass and 100% on safety cases.
6. `uv run chainlit run src/scc/ui/chainlit_app.py` (dev) → click each starter; check that spoke steps expand with their tool calls; ask for a restock PO and test both **Approve** (row appears in `GET /api/purchase-orders`) and **Deny**.
7. `docker compose up --build` → open http://localhost:8000; the same checks pass in the container; `GET /health` returns ok.
