"""Terminal client. Uses the same AgentSession runtime as the web UI.

    uv run scc ask "Why did revenue in Germany change last month vs the month before?"
    uv run scc chat                      # multi-turn chat; type /exit to leave
    uv run scc chat --resume <id>        # continue an earlier chat (our id or the SDK's)
    uv run scc sessions                  # recent chats with their cost

Approvals appear as a y/N prompt in the terminal. The answer defaults to No, and the prompt
times out (as in the web UI).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from scc.agents.hub import HubRuntime
from scc.hooks import TraceEvent
from scc.permissions import ApprovalBroker, ApprovalRequest, OnRequest
from scc.runtime.agent_session import AgentSession
from scc.runtime.events import (
    Event,
    Finished,
    HubText,
    SpokeFinished,
    SpokeStarted,
    ToolCalled,
    ToolReturned,
)

DIM, BOLD, RESET = "\033[2m", "\033[1m", "\033[0m"


def _short(value: object, limit: int = 120) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def render(event: Event, verbose: bool) -> None:
    """Print one event. Hub text in full; specialist activity as dim progress lines."""
    if isinstance(event, HubText):
        print(event.text, flush=True)
    elif isinstance(event, SpokeStarted):
        print(f"{DIM}-> {event.agent}: {_short(event.task)}{RESET}", flush=True)
    elif isinstance(event, SpokeFinished):
        status = "failed" if event.is_error else "done"
        print(f"{DIM}<- {event.agent} {status}{RESET}", flush=True)
    elif isinstance(event, ToolCalled) and verbose:
        print(f"{DIM}   [{event.agent}] {event.name} {_short(event.input)}{RESET}", flush=True)
    elif isinstance(event, ToolReturned) and (verbose or event.is_error):
        mark = "ERROR " if event.is_error else ""
        print(f"{DIM}   [{event.agent}] {mark}{_short(event.output)}{RESET}", flush=True)
    elif isinstance(event, Finished):
        cost = f"${event.cost_usd:.4f}" if event.cost_usd is not None else "n/a"
        status = "" if not event.is_error else f" | {event.subtype}"
        print(
            f"\n{DIM}[{event.num_turns} turns | {event.duration_ms / 1000:.1f}s | {cost}{status} | "
            f"session {event.sdk_session_id}]{RESET}",
            flush=True,
        )


def terminal_approval(broker: ApprovalBroker) -> OnRequest:
    """Ask y/N in the terminal. input() blocks, so it runs in a thread."""

    async def on_request(req: ApprovalRequest) -> None:
        async def ask() -> None:
            print(f"\n{BOLD}Approval needed{RESET}\n{req.summary}\n")
            try:
                answer = await asyncio.to_thread(input, "Approve? [y/N] ")
            except EOFError:  # no terminal attached (e.g. piped input): deny, don't hang
                broker.resolve(req.id, approved=False, decided_by="policy:no_tty")
                return
            approved = answer.strip().lower() in ("y", "yes")
            broker.resolve(req.id, approved=approved, decided_by="seller")

        asyncio.create_task(ask())  # return now; the broker waits for resolve()

    return on_request


def print_trace(ev: TraceEvent) -> None:
    if ev.event == "tool_denied":
        print(f"{DIM}   [{ev.agent}] DENIED {ev.tool_name}: {_short(ev.detail or '')}{RESET}")


async def run_ask(prompt: str, verbose: bool) -> int:
    runtime = HubRuntime.create()
    async with AgentSession(runtime, on_approval=terminal_approval, on_trace=print_trace) as s:
        print(f"{DIM}[session {s.session_id}]{RESET}")
        async for event in s.ask(prompt):
            render(event, verbose)
        return 1 if (s.last_result and s.last_result.is_error) else 0


async def run_chat(resume: str | None, verbose: bool) -> int:
    runtime = HubRuntime.create()
    session_id = sdk_id = None
    if resume:
        found = runtime.store.find_session(resume)
        if found:
            session_id = found["id"]
        else:
            sdk_id = resume  # not ours: try it as an SDK session id
    async with AgentSession(
        runtime, session_id=session_id, resume=sdk_id, on_approval=terminal_approval,
        on_trace=print_trace,
    ) as s:  # fmt: skip
        print(f"{DIM}[session {s.session_id}] Type /exit to leave.{RESET}")
        while True:
            try:
                prompt = (await asyncio.to_thread(input, f"\n{BOLD}you>{RESET} ")).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if prompt in ("/exit", "/quit"):
                break
            if prompt:
                async for event in s.ask(prompt):
                    render(event, verbose)
    return 0


def list_sessions() -> int:
    runtime = HubRuntime.create()
    rows = runtime.store.list_sessions()
    if not rows:
        print("No sessions yet.")
    for r in rows:
        print(f"{r['id']}  {r['updated_at']}  {r['requests']:>3} req  ${r['cost_usd']:.4f}  "
              f"{(r['title'] or '')[:60]}")  # fmt: skip
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="scc", description="Seller Command Center (terminal)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ask = sub.add_parser("ask", help="ask one question")
    p_ask.add_argument("prompt")
    p_ask.add_argument("-v", "--verbose", action="store_true", help="show every tool call")

    p_chat = sub.add_parser("chat", help="multi-turn chat")
    p_chat.add_argument("--resume", help="session id to continue (ours or the SDK's)")
    p_chat.add_argument("-v", "--verbose", action="store_true")

    sub.add_parser("sessions", help="list recent sessions")

    args = parser.parse_args(argv)
    if args.command == "ask":
        code = asyncio.run(run_ask(args.prompt, args.verbose))
    elif args.command == "chat":
        code = asyncio.run(run_chat(args.resume, args.verbose))
    else:
        code = list_sessions()
    sys.exit(code)


if __name__ == "__main__":
    main()
