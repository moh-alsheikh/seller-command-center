"""SQLite store for everything the app writes. The analytical DuckDB stays read-only.

Why a second database? The retail data is read-only by design (rule 5). The app still has
state to keep: chat sessions, the seller's saved preferences, approval decisions, the
listing drafts and purchase orders those approvals allowed, an audit log of every tool call,
and the cost of every request. SQLite is in the standard library, handles this volume
easily, and is one file in the data volume.

Tables:
    sessions         one row per chat; maps our id to the SDK session id (for resume=)
    requests         one row per question: turns, cost, duration, outcome (from ResultMessage)
    memory           seller preferences ("focus on UK and Germany"), injected every turn
    approvals        every human approval request and its decision
    listing_drafts   written by save_listing_draft, only with a valid approval
    purchase_orders  written by create_purchase_order, only with a valid approval
    audit_log        tool calls and sub-agent starts/stops from the hooks

Concurrency: one connection per store, shared across async tasks and worker threads, and
guarded by a lock. Every operation is a short single-row write or a small read (well under a
millisecond), so calling it directly from async code is acceptable at this scale. A
multi-instance deployment would move this to Postgres (see DESIGN.md).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id              TEXT PRIMARY KEY,
    sdk_session_id  TEXT,
    chainlit_thread_id TEXT,           -- the web UI thread this chat belongs to, if any
    title           TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS requests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL,
    sdk_session_id  TEXT,
    prompt          TEXT NOT NULL,
    subtype         TEXT,
    is_error        INTEGER,
    num_turns       INTEGER,
    duration_ms     INTEGER,
    cost_usd        REAL,
    usage_json      TEXT,
    created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    text            TEXT NOT NULL,
    session_id      TEXT,
    created_at      TEXT NOT NULL,
    deleted_at      TEXT
);
CREATE TABLE IF NOT EXISTS approvals (
    id              TEXT PRIMARY KEY,
    session_id      TEXT,
    tool_name       TEXT NOT NULL,
    input_json      TEXT NOT NULL,
    input_hash      TEXT NOT NULL,
    summary         TEXT NOT NULL,
    status          TEXT NOT NULL,     -- pending | approved | denied
    decided_by      TEXT,              -- seller | timeout | policy:<name>
    reason          TEXT,
    used_at         TEXT,              -- set when the write tool consumed the approval
    requested_at    TEXT NOT NULL,
    decided_at      TEXT
);
CREATE TABLE IF NOT EXISTS listing_drafts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT,
    approval_id     TEXT NOT NULL REFERENCES approvals(id),
    stock_code      TEXT NOT NULL,
    title           TEXT NOT NULL,
    bullets_json    TEXT NOT NULL,
    description     TEXT NOT NULL,
    tags_json       TEXT NOT NULL,
    created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS purchase_orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT,
    approval_id     TEXT NOT NULL REFERENCES approvals(id),
    supplier        TEXT,
    lines_json      TEXT NOT NULL,
    total_units     INTEGER NOT NULL,
    reason          TEXT,
    status          TEXT NOT NULL,     -- draft (this app never sends orders anywhere)
    created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT,
    event           TEXT NOT NULL,     -- tool_ok | tool_error | tool_denied | subagent_start | ...
    agent           TEXT,              -- 'hub' or the sub-agent type
    agent_id        TEXT,
    tool_name       TEXT,
    tool_use_id     TEXT,
    duration_ms     INTEGER,
    detail          TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_session ON audit_log(session_id, id);
CREATE INDEX IF NOT EXISTS sessions_thread ON sessions(chainlit_thread_id);
CREATE INDEX IF NOT EXISTS requests_session ON requests(session_id, id);
"""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def input_hash(tool_input: dict[str, Any]) -> str:
    """Stable hash of a tool input, ignoring the approval_id we add ourselves.

    Used to check that the input a write tool receives is exactly the input the seller saw
    and approved.
    """
    clean = {k: v for k, v in tool_input.items() if k != "approval_id"}
    blob = json.dumps(clean, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


class ApprovalError(PermissionError):
    """A write tool was called without a valid, unused approval for exactly this input."""


class AppStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._con = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._con.row_factory = sqlite3.Row
        self._con.execute("PRAGMA journal_mode=WAL")  # readers don't block the writer
        self._con.execute("PRAGMA foreign_keys=ON")
        self._migrate()
        self._con.executescript(SCHEMA)

    def _migrate(self) -> None:
        """Small, additive migrations for databases created by earlier versions."""
        cols = {r[1] for r in self._con.execute("PRAGMA table_info(sessions)").fetchall()}
        if cols and "chainlit_thread_id" not in cols:  # added in milestone 5
            self._con.execute("ALTER TABLE sessions ADD COLUMN chainlit_thread_id TEXT")

    def close(self) -> None:
        self._con.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._con.execute("BEGIN IMMEDIATE")
            try:
                yield self._con
                self._con.execute("COMMIT")
            except BaseException:
                self._con.execute("ROLLBACK")
                raise

    def _all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._con.execute(sql, params).fetchall()]

    def _one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        rows = self._all(sql, params)
        return rows[0] if rows else None

    # --- sessions and requests ------------------------------------------------------------

    def create_session(self, title: str | None = None) -> str:
        sid = uuid.uuid4().hex[:12]
        ts = now()
        with self._tx() as c:
            c.execute(
                "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (sid, title, ts, ts),
            )
        return sid

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM sessions WHERE id = ?", (session_id,))

    def find_session(self, any_id: str) -> dict[str, Any] | None:
        """Look up by our id or by the SDK's session id (both are accepted by --resume)."""
        return self._one(
            "SELECT * FROM sessions WHERE id = ? OR sdk_session_id = ?", (any_id, any_id)
        )

    def link_thread(self, session_id: str, thread_id: str) -> None:
        """Remember which web UI thread a session belongs to (for resuming it later)."""
        with self._tx() as c:
            c.execute(
                "UPDATE sessions SET chainlit_thread_id = ? WHERE id = ?", (thread_id, session_id)
            )

    def session_for_thread(self, thread_id: str) -> dict[str, Any] | None:
        return self._one(
            "SELECT * FROM sessions WHERE chainlit_thread_id = ? ORDER BY updated_at DESC LIMIT 1",
            (thread_id,),
        )

    def list_sessions(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._all(
            """
            SELECT s.*, COUNT(r.id) AS requests, COALESCE(SUM(r.cost_usd), 0) AS cost_usd
            FROM sessions s LEFT JOIN requests r ON r.session_id = s.id
            GROUP BY s.id ORDER BY s.updated_at DESC LIMIT ?
            """,
            (limit,),
        )

    def record_request(
        self,
        session_id: str,
        prompt: str,
        *,
        sdk_session_id: str | None,
        subtype: str | None,
        is_error: bool,
        num_turns: int | None,
        duration_ms: int | None,
        cost_usd: float | None,
        usage: dict | None,
    ) -> None:
        ts = now()
        with self._tx() as c:
            c.execute(
                """
                INSERT INTO requests (session_id, sdk_session_id, prompt, subtype, is_error,
                    num_turns, duration_ms, cost_usd, usage_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (session_id, sdk_session_id, prompt, subtype, int(is_error), num_turns,
                 duration_ms, cost_usd, json.dumps(usage) if usage else None, ts),
            )  # fmt: skip
            c.execute(
                """
                UPDATE sessions
                SET sdk_session_id = COALESCE(?, sdk_session_id), updated_at = ?,
                    title = COALESCE(title, ?)
                WHERE id = ?
                """,
                (sdk_session_id, ts, prompt[:80], session_id),
            )

    def list_requests(self, session_id: str) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM requests WHERE session_id = ? ORDER BY id", (session_id,))

    # --- memory ---------------------------------------------------------------------------

    def add_memory(self, text: str, session_id: str | None = None) -> int:
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO memory (text, session_id, created_at) VALUES (?, ?, ?)",
                (text.strip(), session_id, now()),
            )
            return int(cur.lastrowid)

    def list_memory(self) -> list[dict[str, Any]]:
        return self._all(
            "SELECT id, text, created_at FROM memory WHERE deleted_at IS NULL ORDER BY id"
        )

    def delete_memory(self, memory_id: int) -> bool:
        with self._tx() as c:
            cur = c.execute(
                "UPDATE memory SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL",
                (now(), memory_id),
            )
            return cur.rowcount == 1

    # --- approvals ------------------------------------------------------------------------

    def create_approval(
        self, session_id: str | None, tool_name: str, tool_input: dict, summary: str
    ) -> str:
        aid = uuid.uuid4().hex
        with self._tx() as c:
            c.execute(
                """
                INSERT INTO approvals (id, session_id, tool_name, input_json, input_hash,
                    summary, status, requested_at)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (aid, session_id, tool_name, json.dumps(tool_input), input_hash(tool_input),
                 summary, now()),
            )  # fmt: skip
        return aid

    def decide_approval(
        self, approval_id: str, approved: bool, decided_by: str, reason: str | None = None
    ) -> None:
        with self._tx() as c:
            c.execute(
                """
                UPDATE approvals SET status = ?, decided_by = ?, reason = ?, decided_at = ?
                WHERE id = ? AND status = 'pending'
                """,
                ("approved" if approved else "denied", decided_by, reason, now(), approval_id),
            )

    def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM approvals WHERE id = ?", (approval_id,))

    def list_approvals(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM approvals ORDER BY requested_at DESC LIMIT ?", (limit,))

    def consume_approval(self, approval_id: str | None, tool_name: str, tool_input: dict) -> dict:
        """Check an approval token and mark it used, atomically. Raises ApprovalError.

        This is the write tools' own check, independent of can_use_tool. A write only
        happens if the seller approved exactly this tool with exactly this input, and each
        approval can be used once.
        """
        if not approval_id:
            raise ApprovalError("This action needs the seller's approval, and none was given.")
        with self._tx() as c:
            row = c.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
            if row is None:
                raise ApprovalError("Unknown approval id.")
            if row["status"] != "approved":
                raise ApprovalError(f"The seller has not approved this action ({row['status']}).")
            if row["used_at"]:
                raise ApprovalError("This approval was already used.")
            if row["tool_name"] != tool_name:
                raise ApprovalError("This approval was given for a different action.")
            if row["input_hash"] != input_hash(tool_input):
                raise ApprovalError("The action changed after the seller approved it.")
            c.execute("UPDATE approvals SET used_at = ? WHERE id = ?", (now(), approval_id))
            return dict(row)

    # --- approved writes ------------------------------------------------------------------

    def insert_listing_draft(
        self,
        *,
        session_id: str | None,
        approval_id: str,
        stock_code: str,
        title: str,
        bullets: list[str],
        description: str,
        tags: list[str],
    ) -> int:
        with self._tx() as c:
            cur = c.execute(
                """
                INSERT INTO listing_drafts (session_id, approval_id, stock_code, title,
                    bullets_json, description, tags_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (session_id, approval_id, stock_code, title, json.dumps(bullets), description,
                 json.dumps(tags), now()),
            )  # fmt: skip
            return int(cur.lastrowid)

    def insert_purchase_order(
        self,
        *,
        session_id: str | None,
        approval_id: str,
        supplier: str | None,
        lines: list[dict],
        reason: str | None,
    ) -> int:
        total = sum(int(line["quantity"]) for line in lines)
        with self._tx() as c:
            cur = c.execute(
                """
                INSERT INTO purchase_orders (session_id, approval_id, supplier, lines_json,
                    total_units, reason, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'draft', ?)
                """,
                (session_id, approval_id, supplier, json.dumps(lines), total, reason, now()),
            )
            return int(cur.lastrowid)

    def list_listing_drafts(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._all("SELECT * FROM listing_drafts ORDER BY id DESC LIMIT ?", (limit,))
        for r in rows:
            r["bullets"] = json.loads(r.pop("bullets_json"))
            r["tags"] = json.loads(r.pop("tags_json"))
        return rows

    def list_purchase_orders(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._all("SELECT * FROM purchase_orders ORDER BY id DESC LIMIT ?", (limit,))
        for r in rows:
            r["lines"] = json.loads(r.pop("lines_json"))
        return rows

    # --- audit ----------------------------------------------------------------------------

    def audit(
        self,
        event: str,
        *,
        session_id: str | None = None,
        agent: str | None = None,
        agent_id: str | None = None,
        tool_name: str | None = None,
        tool_use_id: str | None = None,
        duration_ms: int | None = None,
        detail: str | None = None,
    ) -> None:
        with self._tx() as c:
            c.execute(
                """
                INSERT INTO audit_log (session_id, event, agent, agent_id, tool_name,
                    tool_use_id, duration_ms, detail, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (session_id, event, agent, agent_id, tool_name, tool_use_id, duration_ms,
                 (detail or "")[:2000] or None, now()),
            )  # fmt: skip

    def list_audit(self, session_id: str | None = None, limit: int = 500) -> list[dict]:
        if session_id:
            return self._all(
                "SELECT * FROM audit_log WHERE session_id = ? ORDER BY id LIMIT ?",
                (session_id, limit),
            )
        return self._all("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,))
