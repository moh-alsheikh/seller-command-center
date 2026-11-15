# Seller Command Center

A "chief of staff" for an online shop owner, built with the **Claude Agent SDK (Python)**.

You ask a business question ("What should I restock for December?"). A **hub** agent breaks it
down and delegates to five **specialist sub-agents** that analyse real e-commerce data. The hub
then writes an action brief. Anything that changes state (saving a listing draft, creating a
purchase order) needs your **approval** through Approve/Deny buttons in the UI.

> **Status:** work in progress. Milestone 1 of 7 (scaffold) is done.
> See [docs/PLAN.md](docs/PLAN.md) for the full plan and [DESIGN.md](DESIGN.md) for the design decisions.

## Architecture (summary)

```
seller ─▶ HUB "chief_of_staff" (Sonnet), with no data tools, so it must delegate
   ┌────────┬──────────────┬───────────────┬──────────────┐
   ▼        ▼              ▼               ▼              ▼
sales_    customer_     inventory_      pricing_       listing_
analyst   insights      planner         strategist     writer
```

- **Data:** [UCI Online Retail II](https://archive.ics.uci.edu/dataset/502/online+retail+ii)
  (about 1.07M transactions, 2009-12-01 to 2011-12-09, CC BY 4.0), loaded into DuckDB.
- **Live API:** [Frankfurter](https://frankfurter.dev) exchange rates (no key needed).
- **UI:** Chainlit, mounted in FastAPI on one port.
- **Only one key is needed:** `ANTHROPIC_API_KEY`.

## Quick start (development)

Requires [uv](https://docs.astral.sh/uv/) and Python 3.13.

```bash
cp .env.example .env          # then put your ANTHROPIC_API_KEY in .env
uv sync                       # create .venv and install pinned dependencies
uv run pytest                 # offline unit tests (no API calls)
```

More commands (data ingest, CLI, UI, evals, Docker) will be added as the milestones land.

## Project layout

```
src/scc/        application package (config, data, analytics, tools, agents, hooks, UI, API)
tests/          offline pytest suite
docs/PLAN.md    the approved build plan
DESIGN.md       why things are built the way they are
```

## Data licence

The UCI Online Retail II dataset is by Daqing Chen, licensed under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). The data is downloaded at ingest time
and is not committed to this repository.

## Licence

Code: MIT. See [LICENSE](LICENSE).
