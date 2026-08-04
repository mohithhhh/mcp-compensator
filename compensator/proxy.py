"""The aggregator: a low-level MCP server that forwards every downstream
tool call transparently and adds four meta tools -- checkpoint,
list_changes, undo_to, explain_blast_radius -- that give the calling agent
checkpoint/undo capability over everything that happens through it.

Deliberately built on `mcp.server.lowlevel.Server` rather than FastMCP:
this proxy needs to forward arbitrary downstream tool schemas verbatim,
which the low-level list_tools/call_tool handler pattern supports
directly (call_tool receives raw (name, arguments) and may return a raw
CallToolResult), where FastMCP's static @mcp.tool() decorator doesn't fit
a pass-through proxy.

IMPORTANT: this is compensation, not rollback. See README.md for the full
explanation and its limits. In short: undo_to doesn't revert downstream
storage -- it replays configured inverse calls (or calls compensating
tools reconstructed from a pre-call snapshot) in LIFO order. It is only
ever as trustworthy as the registry entries backing it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from typing import Any

import mcp.server.stdio
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.types import CallToolResult, TextContent, Tool

from .downstream import DownstreamServer
from .journal import Change, Journal
from .registry import Registry, ToolPolicy, resolve_args
from .results import result_to_value

logger = logging.getLogger("mcp_compensator")

SERVER_NAME = "mcp-compensator"
SERVER_VERSION = "0.1.0"

META_TOOLS: dict[str, Tool] = {
    "checkpoint": Tool(
        name="checkpoint",
        description=(
            "Start a new checkpoint. Every mutating call made after this point is "
            "journaled against it, so it can later be listed with list_changes or "
            "undone with undo_to. Returns the new checkpoint's id."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "label": {"type": "string", "description": "Optional human-readable label for this checkpoint."}
            },
        },
    ),
    "list_changes": Tool(
        name="list_changes",
        description=(
            "List not-yet-undone journaled changes, newest first. With no arguments, "
            "lists every outstanding change. Pass checkpoint_id to list only changes "
            "at or after that checkpoint."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "checkpoint_id": {"type": "integer", "description": "Optional checkpoint id to start listing from."}
            },
        },
    ),
    "undo_to": Tool(
        name="undo_to",
        description=(
            "Undo every reversible/compensable change made at or after the given "
            "checkpoint, most recent first (LIFO). This is compensation, not "
            "rollback: each undo is a new call to a configured inverse tool, not a "
            "true storage-level revert -- see README.md. Irreversible and "
            "unregistered ('unknown') changes are left alone and reported in "
            "`skipped` along with the reason. Returns {undone: [...], skipped: [...]}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "checkpoint_id": {"type": "integer", "description": "Undo changes at or after this checkpoint."}
            },
            "required": ["checkpoint_id"],
        },
    ),
    "explain_blast_radius": Tool(
        name="explain_blast_radius",
        description=(
            "Look up a namespaced downstream tool (e.g. 'tasks__delete_task') and "
            "explain -- before calling it -- whether and how it could be undone: "
            "its classification plus a plain-language description."
        ),
        inputSchema={
            "type": "object",
            "properties": {"tool": {"type": "string", "description": "Namespaced tool name, '{server}__{tool}'."}},
            "required": ["tool"],
        },
    ),
}

# Plain-language explanations shown by explain_blast_radius, keyed by
# classification. See registry.py / README.md for what each tier means.
EXPLANATIONS: dict[str, str] = {
    "read": "This tool has no side effects. There is nothing to journal or undo.",
    "reversible": (
        "This tool's effect has a true inverse configured (e.g. a create paired "
        "with a matching delete, or complete paired with uncomplete). undo_to will "
        "call that inverse tool directly."
    ),
    "compensable": (
        "This tool destroys or overwrites state. Before it runs, the proxy takes a "
        "snapshot of the prior state so undo_to can reconstruct it afterward via a "
        "compensating call -- a new write that approximates the old state, not a "
        "true revert. Anything the original call did beyond what was snapshotted "
        "(side effects like a webhook or a counter decremented elsewhere) will not "
        "be undone."
    ),
    "irreversible": (
        "This tool has no configured inverse and cannot be undone by this proxy. "
        "Any side effects are permanent from this proxy's point of view."
    ),
    "unknown": (
        "This tool is not listed in the compensator registry, so its safety is "
        "unknown. It will still be journaled for visibility if called, but undo_to "
        "refuses to touch it rather than guess at an inverse."
    ),
}


def _split_namespaced(name: str) -> tuple[str, str]:
    if "__" not in name:
        raise ValueError(f"tool name {name!r} is not namespaced as 'server__tool'")
    server, tool = name.split("__", 1)
    return server, tool


def _json_result(value: Any) -> CallToolResult:
    structured = value if isinstance(value, dict) else None
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(value, default=str, indent=2))],
        structuredContent=structured,
    )


def _change_to_dict(change: Change) -> dict:
    return {
        "id": change.id,
        "checkpoint_id": change.checkpoint_id,
        "server": change.server,
        "tool": change.tool,
        "arguments": change.arguments,
        "result": change.result,
        "snapshot": change.snapshot,
        "classification": change.classification,
        "compensated": change.compensated,
        "created_at": change.created_at,
    }


def _skip_reason(change: Change, policy: ToolPolicy) -> str:
    if change.classification == "irreversible":
        return "classified irreversible: no inverse exists for this tool."
    if change.classification == "unknown":
        return "tool is not in the compensator registry; refusing to guess at an inverse."
    if change.classification not in ("reversible", "compensable"):
        return f"classification {change.classification!r} is not eligible for automatic undo."
    if not policy.inverse_tool:
        return f"classified {change.classification!r} but no inverse_tool is configured in the registry."
    return "not eligible for automatic undo."  # pragma: no cover - defensive fallback


class Proxy:
    """Owns the downstream server connections and the journal, and
    implements both the meta tools and the pass-through/journaling logic
    for every downstream tool call."""

    def __init__(self, registry: Registry, journal: Journal):
        self.registry = registry
        self.journal = journal
        self.downstreams: dict[str, DownstreamServer] = {}

    async def start(self) -> None:
        await self.journal.init()
        for name, cfg in self.registry.servers.items():
            ds = DownstreamServer(cfg.name, cfg.command, cfg.args, cfg.env)
            await ds.start()
            self.downstreams[name] = ds
            logger.info("started downstream server %r (%d tools)", name, len(ds.tools))

    async def stop(self) -> None:
        for ds in self.downstreams.values():
            await ds.stop()
        await self.journal.close()

    def list_all_tools(self) -> list[Tool]:
        tools = list(META_TOOLS.values())
        for server_name, ds in self.downstreams.items():
            for tool in ds.tools:
                tools.append(
                    Tool(
                        name=f"{server_name}__{tool.name}",
                        description=tool.description,
                        inputSchema=tool.inputSchema,
                        outputSchema=tool.outputSchema,
                    )
                )
        return tools

    async def dispatch(self, name: str, arguments: dict) -> CallToolResult:
        if name in META_TOOLS:
            return await self._handle_meta(name, arguments or {})
        return await self._handle_downstream(name, arguments or {})

    # -- meta tools -----------------------------------------------------

    async def _handle_meta(self, name: str, arguments: dict) -> CallToolResult:
        if name == "checkpoint":
            checkpoint_id = await self.journal.new_checkpoint(arguments.get("label"))
            return _json_result({"checkpoint_id": checkpoint_id})

        if name == "list_changes":
            changes = await self.journal.changes_since(arguments.get("checkpoint_id"))
            return _json_result({"changes": [_change_to_dict(c) for c in changes]})

        if name == "undo_to":
            outcome = await self._undo_to(arguments["checkpoint_id"])
            return _json_result(outcome)

        if name == "explain_blast_radius":
            policy = self.registry.policy_for(arguments["tool"])
            return _json_result(
                {
                    "tool": policy.name,
                    "classification": policy.classification,
                    "explanation": EXPLANATIONS[policy.classification],
                    "has_inverse": bool(policy.inverse_tool),
                }
            )

        raise ValueError(f"unknown meta tool {name!r}")  # pragma: no cover - unreachable, guarded by dispatch

    async def _undo_to(self, checkpoint_id: int) -> dict:
        """Fetch uncompensated changes at/after checkpoint_id, newest
        first, and replay each one's inverse in that order (LIFO). A
        compensator call that itself raises is caught and reported in
        `skipped` rather than aborting the rest of the undo."""
        changes = await self.journal.changes_since(checkpoint_id)
        undone: list[dict] = []
        skipped: list[dict] = []

        for change in changes:
            policy = self.registry.policy_for(f"{change.server}__{change.tool}")
            eligible = change.classification in ("reversible", "compensable") and policy.inverse_tool
            if not eligible:
                skipped.append(
                    {
                        "change_id": change.id,
                        "server": change.server,
                        "tool": change.tool,
                        "classification": change.classification,
                        "reason": _skip_reason(change, policy),
                    }
                )
                continue

            context = {"args": change.arguments, "result": change.result, "snapshot": change.snapshot}
            try:
                inverse_server, inverse_tool = _split_namespaced(policy.inverse_tool)
                ds = self.downstreams.get(inverse_server)
                if ds is None:
                    raise ValueError(f"unknown downstream server {inverse_server!r}")
                inverse_args = resolve_args(policy.inverse_args, context)
                await ds.call_tool(inverse_tool, inverse_args)
                await self.journal.mark_compensated(change.id)
                undone.append(
                    {
                        "change_id": change.id,
                        "server": change.server,
                        "tool": change.tool,
                        "inverse_tool": policy.inverse_tool,
                        "inverse_args": inverse_args,
                    }
                )
            except Exception as exc:
                skipped.append(
                    {
                        "change_id": change.id,
                        "server": change.server,
                        "tool": change.tool,
                        "classification": change.classification,
                        "reason": f"compensator call failed: {exc}",
                    }
                )

        return {"undone": undone, "skipped": skipped}

    # -- downstream pass-through -----------------------------------------

    async def _handle_downstream(self, namespaced: str, arguments: dict) -> CallToolResult:
        server_name, tool_name = _split_namespaced(namespaced)
        ds = self.downstreams.get(server_name)
        if ds is None:
            raise ValueError(f"unknown downstream server {server_name!r} (from tool {namespaced!r})")

        policy = self.registry.policy_for(namespaced)

        snapshot = None
        if policy.snapshot_tool:
            snap_server, snap_tool = _split_namespaced(policy.snapshot_tool)
            snap_ds = self.downstreams.get(snap_server)
            if snap_ds is None:
                raise ValueError(f"unknown downstream server {snap_server!r} (snapshot_tool for {namespaced!r})")
            snap_context = {"args": arguments, "result": None, "snapshot": None}
            snap_args = resolve_args(policy.snapshot_args, snap_context)
            snap_result = await snap_ds.call_tool(snap_tool, snap_args)
            snapshot = result_to_value(snap_result)

        result = await ds.call_tool(tool_name, arguments)

        # Journal every mutating call for visibility, even ones the
        # registry can't compensate for (irreversible, unknown) -- so
        # undo_to has something to report in `skipped` with a reason,
        # instead of the call vanishing without a trace. Only genuinely
        # side-effect-free "read" calls are skipped entirely.
        if policy.classification != "read":
            checkpoint_id = await self.journal.current_checkpoint()
            await self.journal.record_change(
                checkpoint_id=checkpoint_id,
                server=server_name,
                tool=tool_name,
                arguments=arguments,
                result=result_to_value(result),
                snapshot=snapshot,
                classification=policy.classification,
            )

        return result


def build_server(proxy: Proxy) -> Server:
    server = Server(SERVER_NAME)

    @server.list_tools()
    async def handle_list_tools() -> list[Tool]:
        return proxy.list_all_tools()

    @server.call_tool()
    async def handle_call_tool(name: str, arguments: dict | None) -> CallToolResult:
        return await proxy.dispatch(name, arguments or {})

    return server


async def run(config_path: str, db_path: str = "compensator.db") -> None:
    registry = Registry.load(config_path)
    journal = Journal(db_path)
    proxy = Proxy(registry, journal)
    await proxy.start()
    server = build_server(proxy)
    try:
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name=SERVER_NAME,
                    server_version=SERVER_VERSION,
                    capabilities=server.get_capabilities(
                        notification_options=NotificationOptions(),
                        experimental_capabilities={},
                    ),
                ),
            )
    finally:
        await proxy.stop()


def main() -> None:
    parser = argparse.ArgumentParser(prog="mcp-compensator")
    parser.add_argument(
        "--config", default="compensators.yaml", help="Path to the compensator registry YAML file."
    )
    parser.add_argument("--db", default="compensator.db", help="Path to the SQLite journal database.")
    args = parser.parse_args()
    asyncio.run(run(args.config, args.db))


if __name__ == "__main__":
    main()
