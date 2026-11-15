# Seller Command Center

A "chief of staff" for an online shop owner, built with the **Claude Agent SDK (Python)**.

You ask a business question ("What should I restock for December?"). A **hub** agent breaks it
down and delegates to five **specialist sub-agents** that analyse real e-commerce data. The hub
then writes an action brief. Anything that changes state (saving a listing draft, creating a
purchase order) needs your **approval** through Approve/Deny buttons in the UI.

> **Status:** work in progress. Milestones 1-4 of 7 are done: the agent system works from the terminal. The web UI is next.
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
uv run python -m scc.data.ingest   # download the dataset once and build data/retail.duckdb
```

Ingest downloads about 45 MB and takes 1-2 minutes. Expected output:

```
      transactions: 1044848
     first_invoice: 2009-12-01 07:45:00
      last_invoice: 2011-12-09 12:50:00
       sales_lines: 1015088
 sales_revenue_gbp: 19700954.46
          products: 4725
         customers: 5852
```

### Terminal client (live: uses your API credit)

```bash
uv run scc ask "Why did revenue in Germany change in Nov 2011 vs Oct, and what should I restock?"
uv run scc ask -v "..."        # also show every specialist tool call
uv run scc chat                # multi-turn chat; /exit to leave
uv run scc chat --resume <id>  # continue an earlier chat
uv run scc sessions            # recent chats and their cost
```

A typical question takes 20-40 seconds and costs about $0.03-0.10. Actions (draft purchase
orders, listing drafts) ask for approval with a y/N prompt.

More commands (UI, evals, Docker) will be added as the milestones land.

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
