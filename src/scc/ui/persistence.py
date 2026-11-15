"""Chat history for the web UI: Chainlit's SQLAlchemy data layer on a SQLite file.

Without a data layer, Chainlit keeps a chat only in the browser tab, so a reload starts from
scratch. With one, every thread, message and step is saved, the sidebar lists past chats, and
reopening a chat shows it exactly as it was. Chainlit only shows that history to logged-in
users (each thread belongs to a user), so history is turned on together with the login.

There are three stores, each with one job:

    data/chainlit.sqlite   what the UI shows: threads, messages, steps   (Chainlit owns it)
    data/app.sqlite        our records: sessions, costs, approvals, audit (scc.store)
    SDK session files      the model's own conversation memory            (the Claude CLI)

``sessions.chainlit_thread_id`` links a Chainlit thread to our session. Our session holds
the SDK session id, so reopening a chat both shows the old messages (Chainlit) and lets the
agent remember them (``resume=``). See chainlit_app.on_chat_resume.

Why SQLite and not Postgres? One file in the data volume, nothing extra to run, and the
same choice as the rest of version 1. Chainlit's data layer only issues portable SQL
(checked in its source), so moving to Postgres is a change of connection string plus the
Postgres version of this schema.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
from pathlib import Path

# Chainlit 2.12 does not create its tables. Columns follow what its SQLAlchemy data layer
# writes: steps get every non-empty key of chainlit.step.StepDict, threads/users/elements/
# feedbacks the keys used in chainlit/data/sql_alchemy.py. JSON values are stored as TEXT.
CHAINLIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    "id"          TEXT PRIMARY KEY,
    "identifier"  TEXT NOT NULL UNIQUE,
    "metadata"    TEXT NOT NULL,
    "createdAt"   TEXT
);
CREATE TABLE IF NOT EXISTS threads (
    "id"              TEXT PRIMARY KEY,
    "createdAt"       TEXT,
    "name"            TEXT,
    "userId"          TEXT,
    "userIdentifier"  TEXT,
    "tags"            TEXT,
    "metadata"        TEXT,
    FOREIGN KEY ("userId") REFERENCES users("id") ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS steps (
    "id"             TEXT PRIMARY KEY,
    "name"           TEXT NOT NULL,
    "type"           TEXT NOT NULL,
    "threadId"       TEXT NOT NULL,
    "parentId"       TEXT,
    "command"        TEXT,
    "modes"          TEXT,
    "streaming"      BOOLEAN NOT NULL,
    "waitForAnswer"  BOOLEAN,
    "isError"        BOOLEAN,
    "metadata"       TEXT,
    "tags"           TEXT,
    "input"          TEXT,
    "output"         TEXT,
    "createdAt"      TEXT,
    "start"          TEXT,
    "end"            TEXT,
    "generation"     TEXT,
    "showInput"      TEXT,
    "defaultOpen"    BOOLEAN,
    "autoCollapse"   BOOLEAN,
    "language"       TEXT,
    "icon"           TEXT,
    "feedback"       TEXT,
    "indent"         INTEGER,
    FOREIGN KEY ("threadId") REFERENCES threads("id") ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS elements (
    "id"            TEXT PRIMARY KEY,
    "threadId"      TEXT,
    "type"          TEXT,
    "url"           TEXT,
    "chainlitKey"   TEXT,
    "name"          TEXT NOT NULL,
    "display"       TEXT,
    "objectKey"     TEXT,
    "size"          TEXT,
    "page"          INTEGER,
    "language"      TEXT,
    "forId"         TEXT,
    "mime"          TEXT,
    "props"         TEXT,
    "autoPlay"      BOOLEAN,
    "playerConfig"  TEXT,
    FOREIGN KEY ("threadId") REFERENCES threads("id") ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS feedbacks (
    "id"        TEXT PRIMARY KEY,
    "forId"     TEXT NOT NULL,
    "threadId"  TEXT NOT NULL,
    "value"     INTEGER NOT NULL,
    "comment"   TEXT,
    FOREIGN KEY ("threadId") REFERENCES threads("id") ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS steps_thread ON steps("threadId");
CREATE INDEX IF NOT EXISTS threads_user ON threads("userId", "createdAt");
"""


def ensure_schema(db_path: Path) -> None:
    """Create Chainlit's tables if they do not exist yet (safe to call on every start)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    try:
        con.executescript(CHAINLIT_SCHEMA)
    finally:
        con.close()


def conninfo(db_path: Path) -> str:
    """SQLAlchemy async URL for the SQLite file (aiosqlite driver)."""
    return f"sqlite+aiosqlite:///{db_path.resolve()}"


def ensure_auth_secret(data_dir: Path) -> str:
    """Make sure CHAINLIT_AUTH_SECRET is set; generate and keep one if it is not.

    Chainlit signs login tokens with this secret and refuses to start a login-enabled app
    without it. Rather than make every user run `chainlit create-secret`, we create a random
    secret once, store it in the data volume (git-ignored, mode 600) and reuse it, so logins
    survive restarts. An explicit CHAINLIT_AUTH_SECRET in the environment always wins.
    """
    if os.environ.get("CHAINLIT_AUTH_SECRET"):
        return "env"
    path = data_dir / ".chainlit_auth_secret"
    if path.exists():
        secret = path.read_text().strip()
    else:
        data_dir.mkdir(parents=True, exist_ok=True)
        secret = secrets.token_urlsafe(48)
        path.write_text(secret)
        path.chmod(0o600)
    os.environ["CHAINLIT_AUTH_SECRET"] = secret
    return str(path)
