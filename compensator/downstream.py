"""Manages one subprocess connection to a downstream MCP server.

Each entry under `servers:` in the compensator registry becomes one
DownstreamServer: a long-lived stdio subprocess plus the ClientSession
talking to it. The proxy holds one of these per configured server and
forwards calls to whichever one owns the requested tool.
"""

from __future__ import annotations

from contextlib import AsyncExitStack

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult, Tool


class DownstreamServer:
    def __init__(self, name: str, command: str, args: list[str] | None = None, env: dict[str, str] | None = None):
        self.name = name
        self.command = command
        self.args = args or []
        self.env = env or {}
        self._stack: AsyncExitStack | None = None
        self.session: ClientSession | None = None
        self.tools: list[Tool] = []

    async def start(self) -> None:
        """Launch the subprocess, initialize the MCP session, and cache its
        tool list (used by the proxy to build the aggregated, namespaced
        tool listing)."""
        self._stack = AsyncExitStack()
        # NB: MCP subprocess environments are NOT inherited from the
        # parent -- get_default_environment() only forwards a small safe
        # allowlist. `command` must be a literal, absolute path to an
        # interpreter that already has this server's dependencies
        # installed; env vars needed by the server must be listed
        # explicitly here so they get passed through.
        params = StdioServerParameters(command=self.command, args=self.args, env=self.env or None)
        read, write = await self._stack.enter_async_context(stdio_client(params))
        self.session = await self._stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()
        result = await self.session.list_tools()
        self.tools = result.tools

    async def call_tool(self, tool_name: str, arguments: dict) -> CallToolResult:
        assert self.session is not None, f"downstream server {self.name!r} not started"
        return await self.session.call_tool(tool_name, arguments)

    async def stop(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None
            self.session = None
