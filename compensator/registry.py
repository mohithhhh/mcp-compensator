"""Loads the YAML compensator registry: which downstream MCP servers to
launch, and the tool policy table that tells the proxy how -- or whether
-- each mutating call can be compensated.

The registry file has two top-level keys:

    servers:
      tasks:
        command: /path/to/python
        args: ["examples/tasks_server.py"]

    tools:
      tasks__delete_task:
        classification: compensable
        snapshot_tool: tasks__get_task
        snapshot_args:
          id: "{args.id}"
        inverse_tool: tasks__restore_task
        inverse_args:
          id: "{snapshot.id}"
          title: "{snapshot.title}"
          status: "{snapshot.status}"

Tools are keyed by their namespaced name, "{server}__{tool}" (double
underscore separator -- see proxy.py). Anything not listed here defaults
to the "unknown" classification: still journaled for visibility, but
undo_to refuses to touch it rather than guess at an inverse.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

CLASSIFICATIONS = {"read", "reversible", "compensable", "irreversible", "unknown"}

# Matches a template value that is *exactly* "{dotted.path}" -- nothing
# before or after the braces. Only these resolve with native type
# preservation; anything else (including a string that merely contains
# "{...}" somewhere) is a literal.
_PATH_RE = re.compile(r"^\{([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z0-9_\-]+)*)\}$")


@dataclass(frozen=True)
class ServerConfig:
    name: str
    command: str
    args: list = field(default_factory=list)
    env: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ToolPolicy:
    name: str  # "{server}__{tool}"
    classification: str = "unknown"
    snapshot_tool: str | None = None
    snapshot_args: dict = field(default_factory=dict)
    inverse_tool: str | None = None
    inverse_args: dict = field(default_factory=dict)
    description: str | None = None


class Registry:
    def __init__(self, servers: list[ServerConfig], tools: dict[str, ToolPolicy]):
        self.servers: dict[str, ServerConfig] = {s.name: s for s in servers}
        self.tools = tools

    @classmethod
    def load(cls, path: str | Path) -> "Registry":
        raw = yaml.safe_load(Path(path).read_text()) or {}

        servers = [
            ServerConfig(
                name=name,
                command=cfg["command"],
                args=list(cfg.get("args", [])),
                env=dict(cfg.get("env", {})),
            )
            for name, cfg in (raw.get("servers") or {}).items()
        ]

        tools: dict[str, ToolPolicy] = {}
        for name, cfg in (raw.get("tools") or {}).items():
            cfg = cfg or {}
            classification = cfg.get("classification", "unknown")
            if classification not in CLASSIFICATIONS:
                raise ValueError(
                    f"tool {name!r}: unknown classification {classification!r}; "
                    f"must be one of {sorted(CLASSIFICATIONS)}"
                )
            tools[name] = ToolPolicy(
                name=name,
                classification=classification,
                snapshot_tool=cfg.get("snapshot_tool"),
                snapshot_args=dict(cfg.get("snapshot_args") or {}),
                inverse_tool=cfg.get("inverse_tool"),
                inverse_args=dict(cfg.get("inverse_args") or {}),
                description=cfg.get("description"),
            )
        return cls(servers, tools)

    def policy_for(self, namespaced_tool: str) -> ToolPolicy:
        """Look up a tool's policy by its namespaced name. Tools absent
        from the registry get a synthetic "unknown" policy rather than
        raising, so callers never need a separate not-found branch."""
        return self.tools.get(namespaced_tool) or ToolPolicy(name=namespaced_tool, classification="unknown")


def resolve_template(value: Any, context: dict) -> Any:
    """Resolve a single argument-template value against a context of
    {args, result, snapshot}.

    A value that is exactly "{dotted.path}" resolves against the context
    with its native type preserved (an int stays an int, a nested dict
    stays a dict). Anything else -- a plain string, a number, a nested
    dict/list of its own -- is a literal, recursed into so templates can
    appear nested inside larger structures.
    """
    if isinstance(value, str):
        match = _PATH_RE.match(value)
        if match:
            return _lookup_path(match.group(1), context)
        return value
    if isinstance(value, dict):
        return {k: resolve_template(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_template(v, context) for v in value]
    return value


def resolve_args(template: dict, context: dict) -> dict:
    """Resolve an entire arguments template (e.g. a policy's snapshot_args
    or inverse_args) against {args, result, snapshot}."""
    return {k: resolve_template(v, context) for k, v in (template or {}).items()}


def _lookup_path(path: str, context: dict) -> Any:
    parts = path.split(".")
    current: Any = context
    for i, part in enumerate(parts):
        if isinstance(current, dict):
            if part not in current:
                raise KeyError(
                    f"template path {path!r}: no key {part!r} at "
                    f"'{'.'.join(parts[:i]) or '<root>'}' (available: {sorted(current.keys())})"
                )
            current = current[part]
        else:
            raise KeyError(
                f"template path {path!r}: '{'.'.join(parts[:i]) or '<root>'}' is "
                f"{type(current).__name__}, not a mapping -- cannot look up {part!r}"
            )
    return current
