"""Tool permissions: one policy function for every tool call, plus human approval of writes.

How the SDK decides whether a tool call may run (verified with scripts/check_sdk.py):

    PreToolUse hooks  ->  allowed_tools (auto-approve)  ->  can_use_tool callback

We leave ``allowed_tools`` EMPTY on purpose. Any tool listed there is auto-approved and never
reaches ``can_use_tool``; the SDK even warns about it (CanUseToolShadowedWarning). With it
empty, every call (hub or sub-agent) goes through ``ToolPolicy.__call__`` below, so the whole
permission model is one short, testable function:

    tool                                  hub (agent_id None)       a spoke (agent_id set)
    -----------------------------------   -----------------------   ----------------------
    Agent / Task (start a sub-agent)      allow (forced foreground) deny (no nesting)
    read-only data tools (mcp__sales__..) DENY: rule 1, delegate    allow
    memory tools                          allow                     deny
    write tools (save_listing_draft, ..)  ASK THE SELLER            deny (rule 2)
    anything else                         deny                      deny

The live check showed that ``can_use_tool`` receives ``context.agent_id`` for calls made inside
a sub-agent, and None for the hub's own calls. That is what lets one function tell them apart.

Human approval (``ApprovalBroker``): a write tool call creates an approval row and an
``asyncio.Future``. The UI (Chainlit buttons, a terminal prompt, or an eval policy) is told
about the request and later calls ``resolve()``. If nobody answers within the timeout, the
request is denied. On approval, the tool input gets an ``approval_id`` token that the write
tool checks again in the database (see AppStore.consume_approval).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, ToolPermissionContext

from scc.store.app_db import AppStore

log = logging.getLogger(__name__)

LAUNCHER_TOOLS = frozenset({"Agent", "Task"})  # the CLI reports "Agent"; "Task" is the old name


@dataclass(frozen=True)
class ApprovalRequest:
    id: str
    session_id: str | None
    tool_name: str
    tool_input: dict[str, Any]
    summary: str  # one human-readable paragraph shown next to the buttons


@dataclass(frozen=True)
class ApprovalDecision:
    approved: bool
    decided_by: str  # "seller" | "timeout" | "policy:<name>"
    reason: str | None = None


# Called when a new request needs a human. It must return quickly: show the buttons (or
# start a task that does) and later call broker.resolve(...).
OnRequest = Callable[[ApprovalRequest], Awaitable[None]]


class ApprovalBroker:
    """Connects can_use_tool (which must wait) with a UI (which answers later)."""

    def __init__(self, store: AppStore, *, timeout_s: float, on_request: OnRequest | None = None):
        self.store = store
        self.timeout_s = timeout_s
        self.on_request = on_request
        self._pending: dict[str, asyncio.Future[ApprovalDecision]] = {}

    async def request(
        self, session_id: str | None, tool_name: str, tool_input: dict, summary: str
    ) -> tuple[str, ApprovalDecision]:
        approval_id = self.store.create_approval(session_id, tool_name, tool_input, summary)
        req = ApprovalRequest(approval_id, session_id, tool_name, tool_input, summary)
        future: asyncio.Future[ApprovalDecision] = asyncio.get_running_loop().create_future()
        self._pending[approval_id] = future
        try:
            if self.on_request is None:
                # Nobody can answer (e.g. a misconfigured runtime): fail closed.
                self.resolve(approval_id, approved=False, decided_by="policy:no_ui")
            else:
                await self.on_request(req)
            decision = await asyncio.wait_for(future, timeout=self.timeout_s)
        except TimeoutError:
            decision = ApprovalDecision(False, "timeout", f"No answer in {self.timeout_s:.0f}s")
            self.store.decide_approval(approval_id, False, decision.decided_by, decision.reason)
        finally:
            self._pending.pop(approval_id, None)
        return approval_id, decision

    def resolve(
        self,
        approval_id: str,
        *,
        approved: bool,
        decided_by: str = "seller",
        reason: str | None = None,
    ) -> bool:
        """Record the decision and wake up the waiting can_use_tool. False if not pending."""
        future = self._pending.get(approval_id)
        if future is None or future.done():
            return False
        self.store.decide_approval(approval_id, approved, decided_by, reason)
        future.set_result(ApprovalDecision(approved, decided_by, reason))
        return True

    @property
    def pending_ids(self) -> list[str]:
        return list(self._pending)


def auto_policy(approve: bool, name: str) -> Callable[[ApprovalBroker], OnRequest]:
    """An on_request that answers immediately, e.g. auto-deny for evals."""

    def factory(broker: ApprovalBroker) -> OnRequest:
        async def on_request(req: ApprovalRequest) -> None:
            broker.resolve(req.id, approved=approve, decided_by=f"policy:{name}")

        return on_request

    return factory


@dataclass
class ToolPolicy:
    """The ``can_use_tool`` callback. Build one per chat session."""

    session_id: str | None
    broker: ApprovalBroker
    spoke_tools: frozenset[str]  # read-only data tools (spokes only)
    hub_tools: frozenset[str]  # hub-only tools that need no approval (memory)
    gated_tools: frozenset[str]  # hub-only write tools that need the seller's approval
    describe: Callable[[str, dict], str]  # human summary of a gated action
    on_decision: Callable[[str, str, str], None] | None = None  # (tool, verdict, reason) for audit
    denials: list[str] = field(default_factory=list)

    @classmethod
    def build(
        cls,
        *,
        session_id: str | None,
        broker: ApprovalBroker,
        spoke_tools: Iterable[str],
        hub_tools: Iterable[str],
        gated_tools: Iterable[str],
        describe: Callable[[str, dict], str],
        on_decision: Callable[[str, str, str], None] | None = None,
    ) -> ToolPolicy:
        return cls(session_id, broker, frozenset(spoke_tools), frozenset(hub_tools),
                   frozenset(gated_tools), describe, on_decision)  # fmt: skip

    def _deny(self, tool_name: str, message: str) -> PermissionResultDeny:
        self.denials.append(tool_name)
        if self.on_decision:
            self.on_decision(tool_name, "denied", message)
        return PermissionResultDeny(message=message)

    async def __call__(
        self, tool_name: str, tool_input: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResultAllow | PermissionResultDeny:
        in_spoke = bool(getattr(context, "agent_id", None))

        if tool_name in LAUNCHER_TOOLS:
            if in_spoke:
                return self._deny(tool_name, "Specialists cannot start other agents.")
            # Force foreground runs. The Agent tool accepts run_in_background; the live smoke
            # test showed the hub using it, ending its turn with "still running" before any
            # report arrived. A request must finish with the specialists' answers in it.
            if tool_input.get("run_in_background"):
                return PermissionResultAllow(
                    updated_input={**tool_input, "run_in_background": False}
                )
            return PermissionResultAllow()

        if tool_name in self.spoke_tools:
            if in_spoke:
                return PermissionResultAllow()
            return self._deny(
                tool_name,
                "You have no data tools. Delegate to the right specialist with the Agent tool.",
            )

        if tool_name in self.hub_tools:
            if in_spoke:
                return self._deny(tool_name, "Only the chief of staff can use this tool.")
            return PermissionResultAllow()

        if tool_name in self.gated_tools:
            if in_spoke:
                return self._deny(tool_name, "Specialists advise; only the chief of staff acts.")
            return await self._ask_seller(tool_name, tool_input)

        return self._deny(tool_name, f"The tool {tool_name} is not permitted in this app.")

    async def _ask_seller(
        self, tool_name: str, tool_input: dict
    ) -> PermissionResultAllow | PermissionResultDeny:
        summary = self.describe(tool_name, tool_input)
        approval_id, decision = await self.broker.request(
            self.session_id, tool_name, tool_input, summary
        )
        if self.on_decision:
            self.on_decision(tool_name, "approved" if decision.approved else "denied",
                             decision.decided_by)  # fmt: skip
        if decision.approved:
            # The token travels inside the tool input; the write tool verifies it.
            return PermissionResultAllow(updated_input={**tool_input, "approval_id": approval_id})
        self.denials.append(tool_name)
        why = {
            "timeout": "The seller did not answer in time, so the action was not taken.",
        }.get(decision.decided_by, "The seller declined this action, so it was not taken.")
        if decision.reason and decision.decided_by == "seller":
            why += f" Their reason: {decision.reason}"
        return PermissionResultDeny(
            message=why + " Do not retry it unless the seller asks again.", interrupt=False
        )
