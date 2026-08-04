# mcp-compensator

Ctrl+Z for AI agents — an MCP proxy that journals every mutating tool call
and can undo them.

`mcp-compensator` sits in front of the MCP servers you already use. Point
your agent at the proxy instead of at those servers directly, and every
tool call still gets forwarded through unchanged — same tools, same
schemas, same results — but now the agent also has four extra abilities:
start a **checkpoint**, **list** what's changed since one, **undo**
everything reversible since one, and ask **whether** a given tool call
would even be undoable before running it.

## The idea, in plain language

Picture a concierge who's about to rearrange your apartment. Before they
touch anything destructive — say, before they empty a drawer — they take a
photo of it. Everything else they do, they jot down a note of the opposite
action: "I moved the lamp from the desk to the shelf" becomes a note that
says "move it back." When you ask them to undo their work, they don't
have a magic rewind button. They walk backwards through their notes, most
recent first, and either follow the "opposite action" note or use the
photo to put the drawer back the way it was.

That's the whole mechanism. There's no write-ahead log, no transaction,
no actual rollback of the downstream server's storage — the proxy can't
see inside it. For every mutating call, one of two things happens:

- The response already contains enough to build an inverse. A `create`
  returns an id you can `delete`. A `complete` has an obvious `uncomplete`.
  This is the **reversible** case — a true inverse exists.
- The response *doesn't* contain enough (a `delete` returns nothing
  useful once the row is gone). So the proxy snapshots state *before* the
  call runs, then uses that snapshot afterward to reconstruct it. This is
  the **compensable** case — a new, independent write that approximates
  the old state.

Undo replays these compensations in LIFO order: most recent change undone
first, like unwinding a stack.

**This is the saga/compensation pattern, not rollback.** Say that out loud
before using this in anything that matters — see [Known
limitations](#known-limitations) below.

## Quickstart

Requires Python 3.10+. `mcp` is pinned to `1.29.0` — see [Known
gotchas](#known-gotchas).

```bash
git clone https://github.com/<you>/mcp-compensator
cd mcp-compensator
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Run the end-to-end demo. It spawns the proxy and a toy downstream task-list
server as real subprocesses, connects a real MCP client to the proxy (the
same way an agent would), and drives a mistake-and-undo scenario:

```bash
python demo.py
```

You should see something like:

```
--- checkpoint ---
checkpoint: {"checkpoint_id": 1}
list_tasks (right after checkpoint): {"result": []}

--- add_task x2 ---
add_task (a): {"id": 1, "title": "Write the README", "status": "open"}
add_task (b): {"id": 2, "title": "Ship the demo", "status": "open"}

--- complete_task on task a ---
complete_task: {"id": 1, "title": "Write the README", "status": "completed"}

--- delete_task on task b (the mistake) ---
delete_task: {"id": 2, "deleted": true}

--- undo_to(checkpoint_id=1) ---
undo_to: {"undone": [...4 entries...], "skipped": []}

--- list_tasks (after undo) ---
list_tasks (after undo): {"result": []}

✅ state after undo_to exactly matches state right after checkpoint
```

`undo_to` walked backwards through everything that happened after the
checkpoint — the "mistaken" delete, the complete, and even the two
`add_task` calls themselves — and compensated all of it, back to the
empty task list that existed right when the checkpoint was taken.

Run the tests:

```bash
pytest
```

### Wiring it up for real

1. Write a `compensators.yaml` listing your downstream servers under
   `servers:` and a compensator policy per mutating tool under `tools:`
   (see [Registry format](#registry-format) below; `examples/compensators.yaml`
   is a full worked example).
2. Point your MCP client at the proxy instead of at your servers directly:

   ```bash
   python -m compensator.proxy --config compensators.yaml --db compensator.db
   ```

3. Your agent now sees every downstream tool renamed to
   `{server}__{tool}`, plus `checkpoint`, `list_changes`, `undo_to`, and
   `explain_blast_radius`.

## Registry format

`compensators.yaml` has two top-level keys.

```yaml
servers:
  tasks:
    command: /path/to/venv/bin/python   # absolute path -- see Known gotchas
    args: ["examples/tasks_server.py"]
    env: {}                              # optional, passed through explicitly

tools:
  tasks__delete_task:
    classification: compensable
    snapshot_tool: tasks__get_task       # called before the real call, to capture prior state
    snapshot_args:
      id: "{args.id}"
    inverse_tool: tasks__restore_task
    inverse_args:
      id: "{snapshot.id}"
      title: "{snapshot.title}"
      status: "{snapshot.status}"
```

Every downstream tool is addressed by its namespaced name,
`"{server}__{tool}"` (double underscore separator). A policy entry can
configure:

| Key              | Meaning                                                                 |
|------------------|--------------------------------------------------------------------------|
| `classification` | One of `read`, `reversible`, `compensable`, `irreversible` (see below). |
| `snapshot_tool`  | Namespaced tool to call *before* the real call, to capture prior state. |
| `snapshot_args`  | Argument template for the snapshot call.                               |
| `inverse_tool`   | Namespaced tool that reverses this one.                                |
| `inverse_args`   | Argument template for the inverse call.                                |

**Argument templates** resolve dotted paths against a context built from
the original call: `{args, result, snapshot}`. `{args.id}` reaches into
the arguments the original call was made with; `{result.id}` reaches into
what it returned; `{snapshot.title}` reaches into whatever the
`snapshot_tool` call returned. A template value that is *exactly*
`"{some.path}"` resolves with its native type preserved — an int stays an
int, it doesn't get stringified. Anything else (a plain string, a nested
dict/list with templates inside it) is treated as a literal or recursed
into.

Any tool called through the proxy that has *no* entry in `tools:` defaults
to the `unknown` classification: it's still journaled so you can see it
happened, but `undo_to` will refuse to touch it and tell you why, rather
than silently pretending it wasn't there.

## The four classifications

In order of how much you can trust the undo:

- **`read`** — no side effects. Nothing is journaled; there's nothing to
  undo.
- **`reversible`** — a true inverse exists (`create` ↔ `delete`, `complete`
  ↔ `uncomplete`). `undo_to` calls the configured `inverse_tool` directly,
  built from the original call's own arguments/result.
- **`compensable`** — no inverse can be built from the response alone
  (a `delete` doesn't return the deleted row). The proxy calls
  `snapshot_tool` *before* the real call, and `undo_to` uses that snapshot
  to drive a compensating call that reconstructs prior state.
- **`irreversible`** — no inverse exists at all. `explain_blast_radius`
  will say so *before* you run it; `undo_to` reports it in `skipped`
  rather than pretending it did something.

(There's a fifth, implicit tier — `unknown`, described above — for
anything the registry hasn't been told about.)

## API reference

Four meta tools, alongside every namespaced downstream tool:

- **`checkpoint(label?)`** → starts a new checkpoint; returns
  `{"checkpoint_id": <int>}`. Every mutating call after this point is
  journaled against it.
- **`list_changes(checkpoint_id?)`** → lists not-yet-undone journaled
  changes, newest first. Omit `checkpoint_id` to see every outstanding
  change; pass one to see only changes at or after it.
- **`undo_to(checkpoint_id)`** → undoes every reversible/compensable
  change at or after that checkpoint, most recent first. Returns
  `{"undone": [...], "skipped": [...]}` — `skipped` entries include why
  (irreversible, unregistered, or the compensator call itself raised; one
  failure doesn't abort the rest of the undo).
- **`explain_blast_radius(tool)`** → looks up a namespaced tool and
  returns its classification plus a plain-language explanation of what
  undoing it would (or wouldn't) do — meant to be called *before* the
  tool itself, so an agent can decide whether it's comfortable proceeding.

## Known limitations

Read this before trusting `mcp-compensator` with anything that matters.

- **This is compensation, not rollback.** A compensator is a new,
  independent write that approximates prior state — it does not undo
  side effects the original action had beyond what was snapshotted. A
  webhook that fired during `delete_task`, an email that got sent, a
  counter decremented somewhere else entirely: none of that unwinds. The
  proxy only ever sees the downstream MCP tool calls that flow through
  it.
- **Identity isn't always restorable.** The demo's `restore_task` can
  reinsert a row under its original id because it's a toy SQLite table
  under our own control. Real systems that assign ids themselves —
  autoincrement you don't control, a ticket number, a commit SHA —
  generally can't be restored under the same identity. Anything that
  referenced the old id in the meantime is now dangling.
- **`undo_to` is itself just another mutating call**, from each downstream
  server's point of view — the inverse/compensating calls it makes are
  real writes with real (if any) side effects of their own. This proxy's
  chosen policy: compensating calls are **not** journaled and **not**
  themselves undoable. `undo_to` is terminal. If you need undo-of-undo,
  you'd need to journal compensations too and decide how deep that stack
  is allowed to go — this project deliberately doesn't, to keep the
  semantics of "undo" unambiguous.
- **Undo is best-effort per change, not transactional across changes.**
  If one compensator call fails partway through an `undo_to`, the rest
  still run; you get a mix of `undone` and `skipped` back, not an
  all-or-nothing guarantee.
- **A snapshot is only as fresh as the moment it was taken.** If something
  else mutates the same downstream state between the snapshot and the
  undo (e.g. another agent, another session), the compensating call will
  happily overwrite that intervening change with the older snapshot.

## Known gotchas

Things that will bite you if you don't pin them down:

- **Pin `mcp==1.29.0`.** `pip install "mcp[cli]"` unpinned currently
  resolves to a `2.0.0` release with a broken import chain
  (`ImportError: cannot import name 'TASK_STATUS_COMPLETED' from
  'mcp.types'`). Use a clean venv and pin the version explicitly, as this
  project's `pyproject.toml` does.
- **MCP subprocess environments are NOT inherited from the parent.**
  `mcp.client.stdio.get_default_environment()` only forwards a small safe
  allowlist (`HOME`, `LOGNAME`, `PATH`, `SHELL`, `TERM`, `USER`). Don't
  rely on env vars to select an interpreter in a server config — use a
  literal absolute path to whatever interpreter has that downstream
  server's dependencies installed, same as any real MCP client config
  does. If a server genuinely needs extra env vars, pass them explicitly
  via its `env:` block in `compensators.yaml`, which *does* get merged
  in on top of the default allowlist.

## Project layout

```
compensator/
  journal.py       # SQLite-backed append-only log of checkpoints + changes
  registry.py      # loads the YAML compensator registry, resolves arg templates
  downstream.py    # manages one subprocess connection to a downstream MCP server
  results.py       # CallToolResult <-> plain dict helpers
  proxy.py         # the aggregator: list_tools/call_tool handlers, meta tools, undo logic
examples/
  tasks_server.py    # demo downstream MCP server (FastMCP, SQLite-backed task list)
  compensators.yaml  # example server list + tool registry for the tasks demo
demo.py              # end-to-end script: drives the proxy as a real MCP client would
tests/               # journal, registry template resolution, full demo flow
```

## License

MIT — see [LICENSE](LICENSE).
