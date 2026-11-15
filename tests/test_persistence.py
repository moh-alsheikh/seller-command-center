"""Chat history: our SQLite schema works with Chainlit's real SQLAlchemy data layer (offline)."""

import os
import sqlite3
import stat
import uuid

import pytest
from chainlit.data.sql_alchemy import SQLAlchemyDataLayer
from chainlit.types import Pagination, ThreadFilter
from chainlit.user import User

from scc.store.app_db import AppStore
from scc.ui import persistence


@pytest.fixture
async def data_layer(tmp_path):
    db = tmp_path / "chainlit.sqlite"
    persistence.ensure_schema(db)
    persistence.ensure_schema(db)  # idempotent: safe on every start
    layer = SQLAlchemyDataLayer(conninfo=persistence.conninfo(db))
    yield layer
    await layer.engine.dispose()


def step(thread_id, **extra):
    """A step shaped like the ones our UI sends (see ChainlitView)."""
    base = {
        "id": str(uuid.uuid4()),
        "threadId": thread_id,
        "name": "User",
        "type": "user_message",
        "streaming": False,
        "input": "",
        "output": "What should I restock?",
        "createdAt": "2026-11-15T10:00:00Z",
        "metadata": {},
        "showInput": "json",
        "isError": False,
        "defaultOpen": False,
        "autoCollapse": False,
    }
    return base | extra


async def create_step(layer, step_dict):
    # create_step is wrapped to queue until the first user message of a live websocket
    # session; outside a session we call the undecorated method directly.
    await SQLAlchemyDataLayer.create_step.__wrapped__(layer, step_dict)


async def test_round_trip_user_thread_steps(data_layer):
    user = await data_layer.create_user(User(identifier="seller"))
    thread_id = str(uuid.uuid4())
    await data_layer.update_thread(thread_id, name="Restock December", user_id=user.id,
                                   metadata={"scc": "x"})  # fmt: skip

    question = step(thread_id)
    spoke = step(thread_id, name="Used Inventory planner · 4.1s", type="run", output="Report",
                 start="2026-11-15T10:00:01Z", end="2026-11-15T10:00:05Z")  # fmt: skip
    tool = step(thread_id, name="forecast_demand", type="tool", parentId=spoke["id"],
                input='{"top_n": 3}', output='{"rows": []}', language="json")  # fmt: skip
    for s in (question, spoke, tool):
        await create_step(data_layer, s)
    await create_step(data_layer, tool | {"isError": True})  # update = upsert

    thread = await data_layer.get_thread(thread_id)
    assert thread["name"] == "Restock December"
    by_id = {s["id"]: s for s in thread["steps"]}
    assert by_id[tool["id"]]["parentId"] == spoke["id"]  # nesting survives a reload
    assert by_id[tool["id"]]["isError"]  # SQLite returns 1, which the frontend treats as true
    assert by_id[spoke["id"]]["name"].startswith("Used Inventory planner")

    page = await data_layer.list_threads(Pagination(first=10), ThreadFilter(userId=user.id))
    assert [t["id"] for t in page.data] == [thread_id]

    await data_layer.delete_thread(thread_id)
    assert await data_layer.get_thread(thread_id) is None


def test_auth_secret_generated_once_and_reused(tmp_path, monkeypatch):
    monkeypatch.delenv("CHAINLIT_AUTH_SECRET", raising=False)
    where = persistence.ensure_auth_secret(tmp_path)
    secret = os.environ["CHAINLIT_AUTH_SECRET"]
    assert len(secret) >= 48
    assert stat.S_IMODE((tmp_path / ".chainlit_auth_secret").stat().st_mode) == 0o600

    monkeypatch.delenv("CHAINLIT_AUTH_SECRET")
    assert persistence.ensure_auth_secret(tmp_path) == where
    assert os.environ["CHAINLIT_AUTH_SECRET"] == secret  # same secret after a restart


def test_explicit_auth_secret_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAINLIT_AUTH_SECRET", "from-env")
    assert persistence.ensure_auth_secret(tmp_path) == "env"
    assert not (tmp_path / ".chainlit_auth_secret").exists()


def test_session_thread_link(tmp_path):
    store = AppStore(tmp_path / "app.sqlite")
    sid = store.create_session()
    assert store.session_for_thread("thread-1") is None
    store.link_thread(sid, "thread-1")
    assert store.session_for_thread("thread-1")["id"] == sid


def test_old_app_db_is_migrated(tmp_path):
    path = tmp_path / "app.sqlite"
    con = sqlite3.connect(path)  # a sessions table from milestone 4, without the new column
    con.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, sdk_session_id TEXT, title TEXT, "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    con.execute("INSERT INTO sessions VALUES ('old', 'sdk-1', 't', 'x', 'x')")
    con.commit()
    con.close()

    store = AppStore(path)
    store.link_thread("old", "thread-9")
    assert store.session_for_thread("thread-9")["sdk_session_id"] == "sdk-1"
