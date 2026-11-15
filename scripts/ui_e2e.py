"""Headless end-to-end check of the web UI, over Chainlit's real websocket protocol (LIVE).

Start the server first, then run this (costs about $0.03):

    uv run uvicorn scc.api.app:create_app --factory --port 8765
    uv run python scripts/ui_e2e.py --url http://127.0.0.1:8765

It behaves like a browser: it connects to /ws/socket.io, starts a chat, sends two requests
for draft purchase orders, clicks Approve on the first and Deny on the second, then checks
through the JSON API that exactly one new purchase order exists.

Why it exists: the Approve/Deny buttons are shown from the SDK's can_use_tool callback, which
runs in the SDK's own task, not in Chainlit's message handler. Whether that works can only be
shown with a real chat session, and this script shows it without a browser.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import UTC, datetime

import httpx
import socketio

REQUESTS = [
    ("approve", "Create a draft purchase order for 12 units of 85123A, reason 'UI e2e approve "
                "test'. I am explicitly asking for it; no analysis needed."),
    ("deny", "Create a draft purchase order for 7 units of 22423, reason 'UI e2e deny test'. "
             "I am explicitly asking for it; no analysis needed."),
]  # fmt: skip


async def run(url: str, timeout_s: float) -> int:
    with httpx.Client(base_url=url, timeout=10) as api:
        pos_before = len(api.get("/api/purchase-orders").json())

    client = socketio.AsyncClient()
    state = {"asks": [], "steps": set(), "messages": {}, "done": asyncio.Event(), "answer": None}

    actions: dict[str, dict] = {}  # Chainlit sends each button as an "action" event first

    @client.on("action")
    async def on_action(action):
        actions[action["id"]] = action

    @client.on("ask")
    async def on_ask(data):  # the Approve/Deny prompt; our return value is the "click"
        buttons = [actions[k] for k in data["spec"].get("keys", []) if k in actions]
        state["asks"].append(data["msg"].get("output", "")[:80])
        wanted = state["answer"]
        chosen = next((a for a in buttons if a["name"] == wanted), None)
        print(f"  [ask] buttons={[a['name'] for a in buttons]} -> clicking {wanted}")
        return chosen

    @client.on("new_message")
    async def on_new(msg):  # Chainlit sends both messages and steps with this event
        if msg.get("type") in ("tool", "run"):
            state["steps"].add(msg.get("name"))
        else:
            state["messages"][msg["id"]] = msg.get("output", "")

    @client.on("update_message")
    async def on_update(msg):
        state["messages"][msg["id"]] = msg.get("output", "")
        if "turns ·" in msg.get("output", ""):  # our footer: the request has finished
            state["done"].set()

    @client.on("stream_start")
    async def on_stream_start(msg):
        state["messages"][msg["id"]] = msg.get("output", "")

    @client.on("send_step")
    async def on_step(step):
        if step.get("type") in ("tool", "run"):
            state["steps"].add(step.get("name"))

    @client.on("update_step")
    async def on_step_update(step):
        if step.get("type") in ("tool", "run"):
            state["steps"].add(step.get("name"))

    await client.connect(
        url,
        socketio_path="/ws/socket.io",
        transports=["websocket"],
        auth={"sessionId": str(uuid.uuid4()), "clientType": "webapp", "threadId": None},
    )
    await client.emit("connection_successful")  # triggers on_chat_start

    for answer, text in REQUESTS:
        state["answer"], state["done"] = answer, asyncio.Event()
        print(f"\n> {text[:70]}...")
        await client.emit(
            "client_message",
            {
                "message": {
                    "id": str(uuid.uuid4()),
                    "threadId": "",
                    "name": "User",
                    "type": "user_message",
                    "output": text,
                    "createdAt": datetime.now(UTC).isoformat(),
                },
                "fileReferences": None,
            },
        )
        await asyncio.wait_for(state["done"].wait(), timeout=timeout_s)
        last = list(state["messages"].values())[-3:]
        for m in last:
            print("  [msg]", " ".join(m.split())[:160])

    await client.disconnect()
    with httpx.Client(base_url=url, timeout=10) as api:
        pos_after = api.get("/api/purchase-orders").json()
    new = pos_after[: len(pos_after) - pos_before]
    print(f"\nsteps seen: {sorted(s for s in state['steps'] if s)}")
    print(f"approval prompts: {len(state['asks'])}; new purchase orders: {len(new)}")
    for po in new:
        print(f"  PO #{po['id']}: {po['lines']} (approval {po['approval_id'][:8]}...)")

    ok = len(state["asks"]) == 2 and len(new) == 1 and new[0]["lines"][0]["stock_code"] == "85123A"
    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=float, default=240)
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args.url, args.timeout)))


if __name__ == "__main__":
    main()
