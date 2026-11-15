"""Headless end-to-end check of the web UI, over Chainlit's real protocol (LIVE, ~$0.05).

Start the server first, then run this:

    uv run uvicorn scc.api.app:create_app --factory --port 8000
    uv run python scripts/ui_e2e.py --url http://127.0.0.1:8000

It behaves like a browser:
  1. logs in (POST /login -> auth cookie), if login is enabled,
  2. opens a chat over /ws/socket.io and asks for two draft purchase orders; it clicks
     Approve on the first and Deny on the second,
  3. checks through the JSON API that exactly one new purchase order exists,
  4. (with login + history) disconnects, REOPENS the same chat as a fresh connection, checks
     that Chainlit restored the history, and asks a follow-up that can only be answered if
     the agent remembers the earlier conversation (SDK resume=).

Why it exists: approvals are shown from the SDK's can_use_tool callback (outside Chainlit's
handler), and resuming spans three stores (Chainlit thread, our session, the SDK session).
Neither can be shown by unit tests alone; this script shows both without a browser.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from datetime import UTC, datetime

import httpx
import socketio

sys.path.insert(0, "src")
from scc.config import get_settings  # noqa: E402

APPROVE = (
    "Create a draft purchase order for 12 units of 85123A, reason 'UI e2e approve test'. "
    "I am explicitly asking for it; no analysis needed."
)
DENY = (
    "Create a draft purchase order for 7 units of 22423, reason 'UI e2e deny test'. "
    "I am explicitly asking for it; no analysis needed."
)
FOLLOW_UP = (
    "Earlier in this chat I approved one purchase order. How many units were in it? "
    "Reply with just the number."
)


class Chat:
    """One browser tab: a socket.io connection plus what it has received."""

    def __init__(self, url: str, cookie: str | None, thread_id: str | None = None):
        self.url, self.cookie, self.thread_id = url, cookie, thread_id
        self.client = socketio.AsyncClient()
        self.actions: dict[str, dict] = {}
        self.messages: dict[str, str] = {}
        self.steps: set[str] = set()
        self.asks = 0
        self.answer = "approve"
        self.resumed_steps: list | None = None
        self.done = asyncio.Event()
        c = self.client

        @c.on("action")  # each button arrives before its "ask"
        async def on_action(action):
            self.actions[action["id"]] = action

        @c.on("ask")  # the Approve/Deny prompt; our return value is the "click"
        async def on_ask(data):
            buttons = [self.actions[k] for k in data["spec"].get("keys", []) if k in self.actions]
            self.asks += 1
            chosen = next((a for a in buttons if a["name"] == self.answer), None)
            print(f"  [ask] buttons={[a['name'] for a in buttons]} -> clicking {self.answer}")
            return chosen

        @c.on("new_message")  # messages and steps both use this event
        async def on_new(msg):
            if msg.get("type") in ("tool", "run"):
                self.steps.add(msg.get("name"))
            else:
                self.messages[msg["id"]] = msg.get("output", "")

        @c.on("stream_start")
        async def on_stream(msg):
            self.messages[msg["id"]] = msg.get("output", "")

        @c.on("update_message")
        async def on_update(msg):
            self.messages[msg["id"]] = msg.get("output", "")
            if "turns ·" in msg.get("output", ""):  # our footer: the request has finished
                self.done.set()

        @c.on("first_interaction")
        async def on_first(data):
            self.thread_id = data.get("thread_id") or self.thread_id

        @c.on("resume_thread")
        async def on_resume(thread):
            self.resumed_steps = thread.get("steps", [])

    async def open(self) -> None:
        headers = {"Cookie": self.cookie} if self.cookie else {}
        await self.client.connect(
            self.url,
            socketio_path="/ws/socket.io",
            transports=["websocket"],
            headers=headers,
            auth={"sessionId": str(uuid.uuid4()), "clientType": "webapp",
                  "threadId": self.thread_id},
        )  # fmt: skip
        await self.client.emit("connection_successful")  # on_chat_start or on_chat_resume

    async def ask(self, text: str, answer: str = "approve", timeout_s: float = 240) -> str:
        self.answer, self.done = answer, asyncio.Event()
        print(f"\n> {text[:75]}...")
        await self.client.emit(
            "client_message",
            {
                "message": {
                    "id": str(uuid.uuid4()),
                    "threadId": self.thread_id or "",
                    "name": "User",
                    "type": "user_message",
                    "output": text,
                    "createdAt": datetime.now(UTC).isoformat(),
                },
                "fileReferences": None,
            },
        )
        await asyncio.wait_for(self.done.wait(), timeout=timeout_s)
        last = list(self.messages.values())[-1]
        print("  [answer]", " ".join(last.split())[:160])
        return last

    async def close(self) -> None:
        await self.client.disconnect()


def login(url: str, user: str, password: str) -> str:
    """Log in like the login form does; return the Cookie header for the websocket."""
    r = httpx.post(f"{url}/login", data={"username": user, "password": password}, timeout=10)
    r.raise_for_status()
    return "; ".join(f"{k}={v}" for k, v in r.cookies.items())


def purchase_orders(url: str, token: str | None) -> list[dict]:
    """Newest first (the API orders by id descending)."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with httpx.Client(base_url=url, timeout=10, headers=headers) as api:
        return api.get("/api/purchase-orders").json()


async def run(url: str) -> int:
    s = get_settings()
    token = s.api_token.get_secret_value() if s.api_token else None
    cookie = None
    if s.login_enabled:
        cookie = login(url, s.app_user, s.app_password.get_secret_value())
        print("logged in as", s.app_user)
    before = len(purchase_orders(url, token))
    checks: dict[str, bool] = {}

    # --- 1-3: approve one action, deny another ---------------------------------------------
    chat = Chat(url, cookie)
    await chat.open()
    await chat.ask(APPROVE, "approve")
    await chat.ask(DENY, "deny")
    thread_id = chat.thread_id
    await chat.close()

    after = purchase_orders(url, token)
    new = after[: len(after) - before]
    checks["two approval prompts"] = chat.asks == 2
    checks["exactly one new PO (85123A x 12)"] = len(new) == 1 and [
        (ln["stock_code"], ln["quantity"]) for ln in new[0]["lines"]
    ] == [("85123A", 12)]

    # --- 4: reopen the same chat ----------------------------------------------------------
    if s.login_enabled and thread_id:
        reopened = Chat(url, cookie, thread_id=thread_id)
        await reopened.open()
        await asyncio.sleep(1)
        restored = reopened.resumed_steps or []
        print(f"\nreopened thread {thread_id[:8]}...: {len(restored)} steps restored")
        checks["history restored on reopen"] = len(restored) >= 4
        answer = await reopened.ask(FOLLOW_UP)
        checks["agent remembers after reopen (answer 12)"] = "12" in answer
        await reopened.close()
    else:
        print("\n(login/history off: skipping the reopen check)")

    print()
    for name, ok in checks.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    ok = all(checks.values())
    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args.url.rstrip("/"))))


if __name__ == "__main__":
    main()
