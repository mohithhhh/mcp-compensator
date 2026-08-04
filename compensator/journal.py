"""SQLite-backed append-only log of checkpoints and changes.

The journal is the append-only source of truth for everything the proxy
has done. It never rewrites history: undo doesn't delete rows or edit
them, it only flips a `compensated` flag once a change has been undone.
Every call into the journal is dispatched through asyncio.to_thread so a
slow disk never blocks the event loop the proxy shares with every other
in-flight tool call.

See README.md for why this is compensation, not rollback: the journal
records what happened and what would reverse it, but it has no
write-ahead log of the downstream servers' actual storage -- it can only
ever be as good as the compensators configured in the registry.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    checkpoint_id INTEGER NOT NULL REFERENCES checkpoints(id),
    server TEXT NOT NULL,
    tool TEXT NOT NULL,
    arguments TEXT NOT NULL,
    result TEXT,
    snapshot TEXT,
    classification TEXT NOT NULL,
    compensated INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
"""


@dataclass(frozen=True)
class Checkpoint:
    id: int
    label: str | None
    created_at: float


@dataclass(frozen=True)
class Change:
    id: int
    checkpoint_id: int
    server: str
    tool: str
    arguments: dict
    result: Any
    snapshot: Any
    classification: str
    compensated: bool
    created_at: float


class Journal:
    """Owns one SQLite connection. Not safe to share across event loops,
    but fine to share across coroutines within one -- sqlite3 connections
    serialize automatically and every call is routed through the same
    dedicated thread pool via asyncio.to_thread."""

    def __init__(self, path: str | Path = "compensator.db"):
        self.path = str(path)
        self._conn: sqlite3.Connection | None = None

    async def init(self) -> None:
        await asyncio.to_thread(self._connect)

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = sqlite3.connect(self.path)
            conn.row_factory = sqlite3.Row
            conn.executescript(SCHEMA)
            conn.commit()
            self._conn = conn
        return self._conn

    # -- checkpoints ------------------------------------------------------

    async def new_checkpoint(self, label: str | None = None) -> int:
        return await asyncio.to_thread(self._new_checkpoint, label)

    def _new_checkpoint(self, label: str | None) -> int:
        conn = self._connect()
        cur = conn.execute(
            "INSERT INTO checkpoints (label, created_at) VALUES (?, ?)",
            (label, time.time()),
        )
        conn.commit()
        return cur.lastrowid

    async def current_checkpoint(self) -> int:
        """The most recently created checkpoint id, auto-creating one
        labeled "auto" if none exists yet."""
        return await asyncio.to_thread(self._current_checkpoint)

    def _current_checkpoint(self) -> int:
        conn = self._connect()
        row = conn.execute("SELECT id FROM checkpoints ORDER BY id DESC LIMIT 1").fetchone()
        if row is not None:
            return row["id"]
        return self._new_checkpoint("auto")

    async def get_checkpoint(self, checkpoint_id: int) -> Checkpoint | None:
        return await asyncio.to_thread(self._get_checkpoint, checkpoint_id)

    def _get_checkpoint(self, checkpoint_id: int) -> Checkpoint | None:
        conn = self._connect()
        row = conn.execute("SELECT * FROM checkpoints WHERE id = ?", (checkpoint_id,)).fetchone()
        if row is None:
            return None
        return Checkpoint(id=row["id"], label=row["label"], created_at=row["created_at"])

    # -- changes ------------------------------------------------------------

    async def record_change(
        self,
        checkpoint_id: int,
        server: str,
        tool: str,
        arguments: dict,
        result: Any,
        snapshot: Any,
        classification: str,
    ) -> int:
        return await asyncio.to_thread(
            self._record_change, checkpoint_id, server, tool, arguments, result, snapshot, classification
        )

    def _record_change(
        self,
        checkpoint_id: int,
        server: str,
        tool: str,
        arguments: dict,
        result: Any,
        snapshot: Any,
        classification: str,
    ) -> int:
        conn = self._connect()
        cur = conn.execute(
            """INSERT INTO changes
               (checkpoint_id, server, tool, arguments, result, snapshot, classification, compensated, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)""",
            (
                checkpoint_id,
                server,
                tool,
                json.dumps(arguments),
                json.dumps(result) if result is not None else None,
                json.dumps(snapshot) if snapshot is not None else None,
                classification,
                time.time(),
            ),
        )
        conn.commit()
        return cur.lastrowid

    async def changes_since(self, checkpoint_id: int | None = None) -> list[Change]:
        """Uncompensated changes with checkpoint_id >= the given id (or all
        uncompensated changes, if checkpoint_id is None), ordered newest
        first. That ordering is what makes undo LIFO: the caller just walks
        the list in order and replays inverses as it goes."""
        return await asyncio.to_thread(self._changes_since, checkpoint_id)

    def _changes_since(self, checkpoint_id: int | None) -> list[Change]:
        conn = self._connect()
        if checkpoint_id is None:
            rows = conn.execute(
                "SELECT * FROM changes WHERE compensated = 0 ORDER BY id DESC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM changes WHERE checkpoint_id >= ? AND compensated = 0 ORDER BY id DESC",
                (checkpoint_id,),
            ).fetchall()
        return [self._row_to_change(row) for row in rows]

    async def mark_compensated(self, change_id: int) -> None:
        await asyncio.to_thread(self._mark_compensated, change_id)

    def _mark_compensated(self, change_id: int) -> None:
        conn = self._connect()
        conn.execute("UPDATE changes SET compensated = 1 WHERE id = ?", (change_id,))
        conn.commit()

    @staticmethod
    def _row_to_change(row: sqlite3.Row) -> Change:
        return Change(
            id=row["id"],
            checkpoint_id=row["checkpoint_id"],
            server=row["server"],
            tool=row["tool"],
            arguments=json.loads(row["arguments"]),
            result=json.loads(row["result"]) if row["result"] is not None else None,
            snapshot=json.loads(row["snapshot"]) if row["snapshot"] is not None else None,
            classification=row["classification"],
            compensated=bool(row["compensated"]),
            created_at=row["created_at"],
        )

    async def close(self) -> None:
        await asyncio.to_thread(self._close)

    def _close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
