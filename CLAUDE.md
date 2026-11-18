# Seller Command Center: project context for Claude

This file gives a new chat everything it needs. **The full approved plan is in [docs/PLAN.md](docs/PLAN.md). Read it before doing anything.**

## Current status
- The plan was approved on 2026-11-15. Milestones 1-5 done on 2026-11-15. **M6: harness done; all 27 cases PENDING (data re-dated on 2026-11-18; earlier 2-case smoke was on 2011 dates). ASK before running the full eval (~$1.50-4).** M7 (Docker + docs) done 2026-11-15: `docker compose up -d --build`; all state in ./data (CLAUDE_CONFIG_DIR=/data/claude). Docker Desktop's disk was 100% full during testing; /tmp is tmpfs in compose. **All milestones built; remaining: the full eval run (ask first).** Run the app with `uv run uvicorn scc.api.app:create_app --factory --port 8000` (login `seller` / `seller-demo` from `.env`; chat history on); headless UI check incl. login + reopen/resume: `scripts/ui_e2e.py`. Tests clear SCC_*/ANTHROPIC/CHAINLIT env vars (Chainlit's load_dotenv leaks .env). Progress table: PLAN.md → "Progress tracker".
- Installed: `claude-agent-sdk` 0.2.182 (bundled CLI confirmed), project `.venv` on Python 3.13 via `uv`. Import package is `scc` (`src/scc/`); settings in `src/scc/config.py` use the `SCC_` env prefix (except `ANTHROPIC_API_KEY`).
- Update the milestone checklist below as work progresses.

### Milestone checklist
- [x] 1. Scaffold: `uv init`, pyproject, config, .gitignore, README skeleton, `git init`
- [x] 2. Data layer: ingest UCI → DuckDB, views, SQL guard, analytics functions, unit tests
- [x] 3. Tools and spokes: MCP servers, 5 AgentDefinitions, Frankfurter FX client
- [x] 4. Hub, hooks, permissions, memory, `scripts/check_sdk.py`, CLI smoke test
- [x] 5. Seller UI (Chainlit) and FastAPI with Chainlit mounted
- [x] 6. Evals (harness; 25 of 27 cases pending a live run) (`query()` + `output_format`), report
- [x] 7. Docker, compose, README, DESIGN.md

## What this project is
A **portfolio project** demonstrating production-level use of the **Claude Agent SDK (Python)** for job applications. The user is studying for the **CCAR-F (Claude Certified Architect – Foundations)** certification and has just finished an Agent SDK workshop.

**Seller Command Center** is a "chief of staff" for an online shop owner. It is a **hub-and-spoke multi-agent system**: a hub agent (Sonnet) has **no data tools** and must delegate to 5 specialist spoke sub-agents (sales_analyst, customer_insights, inventory_planner, pricing_strategist, listing_writer). It runs over **real** e-commerce data. Write actions (listing drafts, purchase orders) sit on the hub only and need **human approval** via `can_use_tool`, shown as Approve/Deny buttons in a **Chainlit** UI.

## Decisions already made with the user (don't reopen them)
| Topic | Decision | Why |
|---|---|---|
| Domain | E-commerce, seller-facing | The user asked for e-commerce in the style of AliExpress |
| API keys | **None except `ANTHROPIC_API_KEY`** | The user's requirement. The AliExpress Open Platform API needs approval and an app key, so it was rejected. |
| Data | UCI Online Retail II (about 1,067,371 rows, 2009-12-01 → 2011-12-09, CC BY 4.0), downloaded without a key from `https://archive.ics.uci.edu/static/public/502/online+retail+ii.zip` | Real data, so the figures can be tested exactly |
| Live API | Frankfurter exchange rates (no key) | A real HTTP integration |
| Architecture | Hub-and-spoke multi-agent | Explicitly requested by the user |
| UI | **Chainlit** (pure Python), mounted in FastAPI on one port | The user wanted a simple seller UI with no complicated frontend |
| Deployment | Docker Compose (local); cloud deploy is out of scope for version 1 | |
| Location | This folder, as its own git repo | |
| Build mode | **Claude builds everything; the user studies it afterwards.** Code must be very readable, well commented and explained in `DESIGN.md`. | The user's choice |
| Git | One **local commit per milestone**. **Never push** unless the user asks. | |

## Environment facts (verified 2026-11-15)
- macOS. Python 3.13 available (`python3.13`); default `python3` is 3.14. Use **`uv`** with Python 3.13 and a project `.venv`.
- Docker 25 and Node 22 are installed.
- `claude-agent-sdk` 0.2.180 (latest at the time) **bundles the Claude Code CLI** in `claude_agent_sdk/_bundled/claude`, so the Docker image does not need Node. Use a glibc base image (`python:3.13-slim`), not Alpine.
- **Demo dates (2026-11-18):** ingest shifts the real data by whole weeks to end on the most recent Friday before the build day (`--dates recent`, default; `--dates original` = 2009-2011). Built 2026-11-18: 2024-11-05 → 2026-11-13, shift 5453 days. "Today" = `dataset_info.as_of_date` via `scc.data.db.resolve_as_of` (Settings.as_of_date defaults to None). Prompts/UI/evals use relative dates. Old 2011 chats are in `data/archive-2011/`.
- **Reports (2026-11-18):** 6th spoke `report_designer` (Sonnet) with `describe_schema` + `mcp__reports__create_report` (per-session server built in `build_options`, classed as a SPOKE tool so the hub is denied). Spec charts use curated sources (`scc/reports/sources.py`) or guarded SQL. Output in `data/reports/<id>/` (report.html, chart-N.json, data.xlsx, report.json); served at `/reports/{id}`; PDF = browser print. UI shows reports after the answer (`ChatController` -> `show_report`). Live UI check PASSED 2026-11-18 (EN $0.057, AR $0.100).
- Data facts (verified on the real file): 1,044,848 clean rows after removing the 22,523-row sheet overlap; sales revenue £19,700,954.46 over 1,015,088 lines; 4,725 products; 5,852 customers. Revenue = `sales` view (gross); rankings and forecasts use NET (minus `returns`) because of same-day bulk cancellations (e.g. 23843). Build with `uv run python -m scc.data.ingest` (~1.5 min).
- Verified live by `scripts/check_sdk.py` (CLI 2.1.325): launcher tool is `Agent` (init lists `Task`; both accepted). `disallowed_tools` on the main session ALSO strips the tool from sub-agents, so don't use it; rule 1 = PreToolUse `hub_guard` (no `agent_id` ⇒ hub) + `ToolPolicy`. `can_use_tool` fires for hub AND sub-agent calls (`context.agent_id`). `allowed_tools=[]` so every call hits `can_use_tool`. Sub-agents must not run in the background: the CLI can auto-background them even when the hub omits `run_in_background` (seen live 2026-11-18), and rewriting the Agent input in `can_use_tool` does NOT stop it. Guarantee: `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` in `build_options` env (policy also writes `run_in_background=False`). `.env` exists with the API key (git-ignored).
- Frankfurter (verified live): `api.frankfurter.dev/v1` OK; `.app` returns 301 (follow redirects); weekend → previous business day; unknown currency → 404. FX defaults to the as-of date's rate.
- DuckDB 2.0 gotchas: lambdas are `lambda x: ...` (not `->`); use `.df(date_as_object=True)` for DATE columns; relations have no `fetchone()` (use `con.execute(...)`); `SUM(BIGINT)` is HUGEINT so cast to BIGINT.
- Default model IDs: `claude-sonnet-5-5` (hub, sales_analyst), `claude-haiku-5-5` (other spokes). In `AgentDefinition`, `model="haiku"` / `"sonnet"` aliases also work.

## SDK lessons learned in the workshop (apply them here)
1. **`allowed_tools` does NOT restrict tools.** It only auto-approves them. Use **`tools=[...]`** to set which built-in tools exist (`[]` = none) and `disallowed_tools` to remove some. `permission_mode="bypassPermissions"` approves everything, so **don't use it**; use `"default"` with `can_use_tool`.
2. Custom tool names are `mcp__<server_key>__<tool_name>`. Handlers are async, take `args: dict`, and return `{"content": [{"type": "text", "text": ...}]}`. Return errors as text; don't raise.
3. Hook callback signature: `async def hook(input_data, tool_use_id, context) -> dict`. `PreToolUse` deny output is `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "..."}}`. `UserPromptSubmit` uses `additionalContext`. Hook events: PreToolUse, PostToolUse, PostToolUseFailure, UserPromptSubmit, Stop, SubagentStart, SubagentStop, PreCompact, Notification, PermissionRequest.
4. `can_use_tool(tool_name, tool_input, context)` returns `PermissionResultAllow(updated_input=...)` or `PermissionResultDeny(message=..., interrupt=...)`. It needs streaming mode, which `ClaudeSDKClient` always uses.
5. `query()` = one independent task (stateless across calls, many turns inside). `ClaudeSDKClient` = a multi-turn session with `interrupt()`, `set_model()`, `set_permission_mode()` and `get_context_usage()`. The project deliberately uses **`ClaudeSDKClient` for the UI and CLI** and **`query()` for evals**.
6. `ResultMessage` has `session_id`, `total_cost_usd`, `num_turns`, `structured_output`, `is_error`. `output_format={"type": "json_schema", "schema": {...}}`.
7. The workshop passes `"Task"` in `allowed_tools` to start sub-agents, but newer SDK docs call it the **"Agent"** tool. **Verify which name works with `tools=[...]`** (`scripts/check_sdk.py`).

Reference material on this machine:
- Workshop repo: `/Users/mohammed/Desktop/ClaudeCerts/agent-sdk-workshop` (patterns in `01-guided-demo/{agent,tools,subagents,memory}.py`)
- The user's SDK notes: `/Users/mohammed/Desktop/ClaudeCerts/agent-sdk-workshop/docs/AGENTIC_LOOPS.md`
- Installed SDK source, to check APIs: `/Users/mohammed/Desktop/ClaudeCerts/agent-sdk-workshop/.venv/lib/python3.13/site-packages/claude_agent_sdk/` (`types.py`, `client.py`, `query.py`)

External references for production patterns:
- Anthropic cookbook "07 Hosting the agent" (FastAPI, Docker, sessions via `resume=`, narrowed tools): https://platform.claude.com/cookbook/claude-agent-sdk-07-hosting-the-agent
- Agent SDK hosting docs: https://code.claude.com/docs/en/agent-sdk/hosting
- A real project to compare with: https://github.com/ClickHouse/nerve

## Working conventions
- Check the SDK API against the installed source (or the new `.venv` once created), not from memory.
- Keep the 6 design rules from PLAN.md, because they are the interview talking points:
  - The hub has no data tools.
  - Spokes read; the hub acts with approval.
  - Least-privilege tools.
  - Metrics come from deterministic code.
  - SQL is read-only, enforced twice.
  - The reference date is fixed per database build (the last day in the data).
- Unit tests must run offline (no Anthropic or HTTP calls; use `respx` for HTTP).
- Live runs (smoke tests and evals) cost the user's API credit. Say so before running them; a full eval run is about $3–8.
- Explain the design choices in `DESIGN.md` as you go. The user will study the code afterwards.
