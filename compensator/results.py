"""Helpers for converting between MCP's CallToolResult objects and plain
Python values, so the rest of the proxy (the journal, registry template
resolution) can work with ordinary JSON-able data instead of MCP content
blocks.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.types import CallToolResult, TextContent


def result_to_value(result: CallToolResult) -> Any:
    """Best-effort extraction of a plain value from a CallToolResult.

    Preference order:
    1. structuredContent, if the tool populated it (FastMCP does this
       automatically for tools that return a dict)
    2. the first TextContent block, parsed as JSON
    3. the first TextContent block's raw text, if it isn't JSON
    4. None, if there's no content at all

    This is what gets journaled as a change's `result`/`snapshot` and what
    registry templates like "{result.id}" or "{snapshot.title}" resolve
    against -- so it needs to be a plain dict/list/scalar, not an MCP
    content-block wrapper.
    """
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return structured
    for block in result.content or []:
        if isinstance(block, TextContent):
            try:
                return json.loads(block.text)
            except (json.JSONDecodeError, TypeError):
                return block.text
    return None
