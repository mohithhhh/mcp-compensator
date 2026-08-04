"""Tests for the registry: YAML loading and the argument-template resolver
(nested path lookup, literal passthrough, type preservation)."""

from __future__ import annotations

from pathlib import Path

import pytest

from compensator.registry import Registry, resolve_args, resolve_template

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_CONFIG = REPO_ROOT / "examples" / "compensators.yaml"


# -- resolve_template ------------------------------------------------------


def test_resolve_template_literal_string_passthrough():
    context = {"args": {"id": 1}, "result": None, "snapshot": None}
    assert resolve_template("just a string", context) == "just a string"


def test_resolve_template_literal_number_and_bool_passthrough():
    context = {"args": {}, "result": None, "snapshot": None}
    assert resolve_template(42, context) == 42
    assert resolve_template(True, context) is True
    assert resolve_template(None, context) is None


def test_resolve_template_top_level_path():
    context = {"args": {"id": 5}, "result": {"id": 5, "title": "hi"}, "snapshot": None}
    assert resolve_template("{args.id}", context) == 5
    assert resolve_template("{result.title}", context) == "hi"


def test_resolve_template_nested_path():
    context = {"args": {}, "result": None, "snapshot": {"id": 7, "title": "task", "status": "open"}}
    assert resolve_template("{snapshot.id}", context) == 7
    assert resolve_template("{snapshot.status}", context) == "open"


def test_resolve_template_preserves_native_type():
    # An int in the context stays an int, not str(int).
    context = {"args": {"id": 3}, "result": None, "snapshot": None}
    value = resolve_template("{args.id}", context)
    assert value == 3
    assert isinstance(value, int)


def test_resolve_template_string_containing_braces_is_literal():
    # Only an *exact* "{path}" match resolves; embedded braces are literal.
    context = {"args": {"id": 3}, "result": None, "snapshot": None}
    assert resolve_template("id is {args.id}", context) == "id is {args.id}"


def test_resolve_template_missing_path_raises_keyerror():
    context = {"args": {}, "result": None, "snapshot": None}
    with pytest.raises(KeyError):
        resolve_template("{args.missing}", context)


def test_resolve_template_recurses_into_nested_structures():
    context = {"args": {"id": 9}, "result": None, "snapshot": None}
    template = {"id": "{args.id}", "nested": {"literal": "x", "also": "{args.id}"}, "list": ["{args.id}", "y"]}
    resolved = resolve_template(template, context)
    assert resolved == {"id": 9, "nested": {"literal": "x", "also": 9}, "list": [9, "y"]}


def test_resolve_args():
    context = {"args": {"id": 2}, "result": {"id": 2, "title": "t"}, "snapshot": None}
    template = {"id": "{result.id}", "title": "{result.title}", "note": "static"}
    assert resolve_args(template, context) == {"id": 2, "title": "t", "note": "static"}


def test_resolve_args_empty_template():
    assert resolve_args({}, {"args": {}, "result": None, "snapshot": None}) == {}
    assert resolve_args(None, {"args": {}, "result": None, "snapshot": None}) == {}


# -- Registry.load ----------------------------------------------------------


def test_registry_loads_example_config():
    registry = Registry.load(EXAMPLE_CONFIG)
    assert "tasks" in registry.servers
    assert registry.servers["tasks"].args == ["examples/tasks_server.py"]


def test_registry_policy_for_known_tool():
    registry = Registry.load(EXAMPLE_CONFIG)
    policy = registry.policy_for("tasks__delete_task")
    assert policy.classification == "compensable"
    assert policy.snapshot_tool == "tasks__get_task"
    assert policy.snapshot_args == {"id": "{args.id}"}
    assert policy.inverse_tool == "tasks__restore_task"


def test_registry_policy_for_unknown_tool_defaults_unknown():
    registry = Registry.load(EXAMPLE_CONFIG)
    policy = registry.policy_for("some_server__some_tool")
    assert policy.classification == "unknown"
    assert policy.inverse_tool is None
    assert policy.snapshot_tool is None


def test_registry_rejects_invalid_classification(tmp_path):
    bad_config = tmp_path / "bad.yaml"
    bad_config.write_text(
        "servers: {}\n"
        "tools:\n"
        "  x__y:\n"
        "    classification: not_a_real_classification\n"
    )
    with pytest.raises(ValueError):
        Registry.load(bad_config)


def test_registry_load_with_no_tools_section(tmp_path):
    minimal_config = tmp_path / "minimal.yaml"
    minimal_config.write_text("servers:\n  srv:\n    command: python3\n")
    registry = Registry.load(minimal_config)
    assert registry.tools == {}
    assert registry.policy_for("srv__anything").classification == "unknown"
