"""End-to-end proof that mcp-compensator works: spawns the proxy as a real
subprocess, connects a real MCP ClientSession to it (exactly as an agent
would), and drives a mistake-and-undo scenario:

    checkpoint -> add_task x2 -> complete_task on one -> delete_task on the
    other (the "mistake") -> list_changes -> undo_to(checkpoint) ->
    list_tasks

...then asserts the task list after undo_to exactly matches the task list
taken right after the checkpoint call -- i.e. every mutating call made
since the checkpoint (both add_tasks, the complete, and the "mistaken"
delete) was fully compensated, back to a clean slate.

Run directly:

    python demo.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

import yaml
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO_ROOT = Path(__file__).resolve().parent
EXAMPLE_CONFIG = REPO_ROOT / "examples" / "compensators.yaml"
TASKS_SERVER = REPO_ROOT / "examples" / "tasks_server.py"


def _step(label: str) -> None:
    print(f"\n--- {label} ---")


def _extract(result) -> object:
    """Pull a plain value out of a CallToolResult, mirroring what
    compensator.results.result_to_value does inside the proxy, so this
    script can print/assert on plain data."""
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return structured
    for block in result.content or []:
        text = getattr(block, "text", None)
        if text is not None:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
    return None


def _print_result(label: str, value: object) -> None:
    print(f"{label}: {json.dumps(value, indent=2, default=str)}")


def _write_runtime_config(tasks_db_path: Path) -> Path:
    """examples/compensators.yaml is written for human readability
    (`command: python3`, relying on PATH). For a deterministic, portable
    run we rewrite it to launch the tasks server with whichever
    interpreter is running this script, and to point it at an isolated
    temp database -- see README's "known gotchas" on why MCP subprocess
    environments need a literal absolute interpreter path rather than
    relying on PATH or inherited env vars."""
    config = yaml.safe_load(EXAMPLE_CONFIG.read_text())
    tasks_cfg = config["servers"]["tasks"]
    tasks_cfg["command"] = sys.executable
    tasks_cfg["args"] = [str(TASKS_SERVER)]
    tasks_cfg.setdefault("env", {})["TASKS_DB_PATH"] = str(tasks_db_path)

    runtime_config_path = tasks_db_path.parent / "compensators.runtime.yaml"
    runtime_config_path.write_text(yaml.safe_dump(config))
    return runtime_config_path


async def run_demo() -> dict:
    """Runs the full checkpoint/undo scenario against a freshly spawned
    proxy + tasks server, both in isolated temp files. Returns
    {"before": [...], "after": [...], "undo_outcome": {...}} -- the task
    list right after the checkpoint, the task list after undo_to, and
    undo_to's own return value. Used both by `python demo.py` and by
    tests/test_demo.py."""
    with tempfile.TemporaryDirectory(prefix="mcp-compensator-demo-") as tmp:
        tmp_path = Path(tmp)
        tasks_db_path = tmp_path / "tasks.db"
        journal_db_path = tmp_path / "journal.db"
        runtime_config_path = _write_runtime_config(tasks_db_path)

        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "compensator.proxy", "--config", str(runtime_config_path), "--db", str(journal_db_path)],
            cwd=str(REPO_ROOT),
        )

        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                _step("checkpoint")
                checkpoint = _extract(await session.call_tool("checkpoint", {"label": "before demo mistakes"}))
                _print_result("checkpoint", checkpoint)
                checkpoint_id = checkpoint["checkpoint_id"]

                before = _extract(await session.call_tool("tasks__list_tasks", {}))
                _print_result("list_tasks (right after checkpoint)", before)

                _step("add_task x2")
                task_a = _extract(await session.call_tool("tasks__add_task", {"title": "Write the README"}))
                _print_result("add_task (a)", task_a)
                task_b = _extract(await session.call_tool("tasks__add_task", {"title": "Ship the demo"}))
                _print_result("add_task (b)", task_b)

                _step("complete_task on task a")
                completed = _extract(await session.call_tool("tasks__complete_task", {"id": task_a["id"]}))
                _print_result("complete_task", completed)

                _step("delete_task on task b (the mistake)")
                deleted = _extract(await session.call_tool("tasks__delete_task", {"id": task_b["id"]}))
                _print_result("delete_task", deleted)

                _step("list_changes since checkpoint")
                changes = _extract(await session.call_tool("list_changes", {"checkpoint_id": checkpoint_id}))
                _print_result("list_changes", changes)

                _step(f"undo_to(checkpoint_id={checkpoint_id})")
                undo_outcome = _extract(await session.call_tool("undo_to", {"checkpoint_id": checkpoint_id}))
                _print_result("undo_to", undo_outcome)

                _step("list_tasks (after undo)")
                after = _extract(await session.call_tool("tasks__list_tasks", {}))
                _print_result("list_tasks (after undo)", after)

        return {"before": before, "after": after, "undo_outcome": undo_outcome}


async def main() -> None:
    outcome = await run_demo()
    before, after = outcome["before"], outcome["after"]
    assert before == after, f"undo_to did not restore state:\nbefore={before}\nafter={after}"
    print("\n✅ state after undo_to exactly matches state right after checkpoint")


if __name__ == "__main__":
    asyncio.run(main())
