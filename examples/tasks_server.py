"""Demo downstream MCP server: a tiny SQLite-backed task list.

This is a real, independent MCP server -- mcp-compensator knows nothing
about tasks specifically. It just forwards calls to whatever tools this
server exposes. What makes this particular server a good demo is that it
happens to expose both the "forward" tools an agent would naturally call
(add_task, delete_task, complete_task) *and* the tools needed to compensate
for them (uncomplete_task, restore_task, get_task) -- see
examples/compensators.yaml for how those get wired together.

Real downstream servers usually only expose the forward half. That's
exactly why the `irreversible` classification exists: something with no
compensating tool available just can't be undone, and the registry should
say so honestly rather than pretend.

Run standalone over stdio:

    python examples/tasks_server.py [path/to/tasks.db]

The database path defaults to "tasks.db" in the current directory, or can
be set via the TASKS_DB_PATH environment variable (checked first) so
tests and concurrent demo runs can point at isolated, temporary files.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("tasks")

_DB_PATH = os.environ.get("TASKS_DB_PATH") or (sys.argv[1] if len(sys.argv) > 1 else "tasks.db")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS tasks (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               title TEXT NOT NULL,
               status TEXT NOT NULL DEFAULT 'open'
           )"""
    )
    conn.commit()
    return conn


def _row_to_task(row: sqlite3.Row) -> dict[str, Any]:
    return {"id": row["id"], "title": row["title"], "status": row["status"]}


@mcp.tool()
def add_task(title: str) -> dict[str, Any]:
    """Add a new task with the given title. Status starts as 'open'."""
    conn = _connect()
    try:
        cur = conn.execute("INSERT INTO tasks (title, status) VALUES (?, 'open')", (title,))
        conn.commit()
        return {"id": cur.lastrowid, "title": title, "status": "open"}
    finally:
        conn.close()


@mcp.tool()
def delete_task(id: int) -> dict[str, Any]:
    """Permanently delete a task by id."""
    conn = _connect()
    try:
        conn.execute("DELETE FROM tasks WHERE id = ?", (id,))
        conn.commit()
        return {"id": id, "deleted": True}
    finally:
        conn.close()


@mcp.tool()
def complete_task(id: int) -> dict[str, Any]:
    """Mark a task as completed."""
    conn = _connect()
    try:
        conn.execute("UPDATE tasks SET status = 'completed' WHERE id = ?", (id,))
        conn.commit()
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (id,)).fetchone()
        if row is None:
            raise ValueError(f"no such task: {id}")
        return _row_to_task(row)
    finally:
        conn.close()


@mcp.tool()
def uncomplete_task(id: int) -> dict[str, Any]:
    """Mark a task as open again. Exists purely to serve as the
    compensator for complete_task -- real task-list servers won't
    necessarily have this."""
    conn = _connect()
    try:
        conn.execute("UPDATE tasks SET status = 'open' WHERE id = ?", (id,))
        conn.commit()
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (id,)).fetchone()
        if row is None:
            raise ValueError(f"no such task: {id}")
        return _row_to_task(row)
    finally:
        conn.close()


@mcp.tool()
def restore_task(id: int, title: str, status: str) -> dict[str, Any]:
    """Reinsert a task under a specific id, title, and status. Exists
    purely to serve as the compensator for delete_task, driven from a
    get_task snapshot taken before the delete ran. Only works because
    this is a toy SQLite table under our own control -- see README.md's
    "known limitations" on identity restoration for real systems."""
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO tasks (id, title, status) VALUES (?, ?, ?)",
            (id, title, status),
        )
        conn.commit()
        return {"id": id, "title": title, "status": status}
    finally:
        conn.close()


@mcp.tool()
def get_task(id: int) -> dict[str, Any]:
    """Fetch a single task by id. Exists purely to serve as the
    snapshot_tool for delete_task, capturing state before it's destroyed."""
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (id,)).fetchone()
        if row is None:
            raise ValueError(f"no such task: {id}")
        return _row_to_task(row)
    finally:
        conn.close()


@mcp.tool()
def list_tasks() -> list[dict[str, Any]]:
    """List every task."""
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM tasks ORDER BY id").fetchall()
        return [_row_to_task(r) for r in rows]
    finally:
        conn.close()


if __name__ == "__main__":
    mcp.run(transport="stdio")
