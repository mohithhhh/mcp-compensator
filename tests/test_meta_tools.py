"""Integration coverage for meta tools not already exercised by the main
demo flow in test_demo.py -- spins up the real proxy + tasks server
subprocesses and drives them through a live MCP ClientSession, same as
demo.py does."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

from demo import _extract, _write_runtime_config  # noqa: E402


async def _session(tmp_path: Path):
    tasks_db_path = tmp_path / "tasks.db"
    journal_db_path = tmp_path / "journal.db"
    runtime_config_path = _write_runtime_config(tasks_db_path)
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "compensator.proxy", "--config", str(runtime_config_path), "--db", str(journal_db_path)],
        cwd=str(REPO_ROOT),
    )
    return stdio_client(params)


async def test_list_checkpoints_reflects_created_checkpoints():
    with tempfile.TemporaryDirectory(prefix="mcp-compensator-test-") as tmp:
        async with await _session(Path(tmp)) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                # No checkpoints yet.
                listing = _extract(await session.call_tool("list_checkpoints", {}))
                assert listing == {"checkpoints": []}

                first = _extract(await session.call_tool("checkpoint", {"label": "first"}))
                second = _extract(await session.call_tool("checkpoint", {"label": "second"}))

                listing = _extract(await session.call_tool("list_checkpoints", {}))
                ids = [c["id"] for c in listing["checkpoints"]]
                labels = [c["label"] for c in listing["checkpoints"]]

                # Newest first.
                assert ids == [second["checkpoint_id"], first["checkpoint_id"]]
                assert labels == ["second", "first"]


async def test_explain_blast_radius_for_unregistered_tool():
    with tempfile.TemporaryDirectory(prefix="mcp-compensator-test-") as tmp:
        async with await _session(Path(tmp)) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                explanation = _extract(
                    await session.call_tool("explain_blast_radius", {"tool": "tasks__not_a_real_tool"})
                )
                assert explanation["classification"] == "unknown"
                assert explanation["has_inverse"] is False


async def test_read_only_calls_are_not_journaled():
    with tempfile.TemporaryDirectory(prefix="mcp-compensator-test-") as tmp:
        async with await _session(Path(tmp)) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                checkpoint = _extract(await session.call_tool("checkpoint", {}))
                await session.call_tool("tasks__list_tasks", {})  # classification: read

                changes = _extract(
                    await session.call_tool("list_changes", {"checkpoint_id": checkpoint["checkpoint_id"]})
                )
                assert changes == {"changes": []}
