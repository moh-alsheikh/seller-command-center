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
