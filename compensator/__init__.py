"""mcp-compensator: an MCP proxy that adds checkpoint/undo to any set of
downstream MCP servers via the saga/compensation pattern.

See README.md for the full picture. In short: this is NOT a transactional
rollback system. There is no write-ahead log at the storage layer and no
way to truly revert a downstream server's state. Instead, every mutating
call either has a configured inverse (create -> delete) or a snapshot
taken just before it runs so a compensating call can approximate the old
state afterward. Undo replays those compensations in LIFO order.
"""

__version__ = "0.1.0"
