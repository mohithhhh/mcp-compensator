"""Tests for the journal: checkpoint ordering, changes_since filtering,
and the compensated flag."""

from __future__ import annotations

from pathlib import Path

import pytest

from compensator.journal import Journal


@pytest.fixture
async def journal(tmp_path: Path) -> Journal:
    j = Journal(tmp_path / "journal.db")
    await j.init()
    yield j
    await j.close()


async def test_current_checkpoint_auto_creates(journal: Journal):
    # No checkpoint exists yet -> current_checkpoint() creates one labeled "auto".
    checkpoint_id = await journal.current_checkpoint()
    assert checkpoint_id == 1
    checkpoint = await journal.get_checkpoint(checkpoint_id)
    assert checkpoint is not None
    assert checkpoint.label == "auto"

    # Calling it again returns the same (most recent) checkpoint, not a new one.
    assert await journal.current_checkpoint() == checkpoint_id


async def test_list_checkpoints_newest_first(journal: Journal):
    first = await journal.new_checkpoint("first")
    second = await journal.new_checkpoint("second")
    third = await journal.new_checkpoint("third")

    checkpoints = await journal.list_checkpoints()
    assert [c.id for c in checkpoints] == [third, second, first]
    assert [c.label for c in checkpoints] == ["third", "second", "first"]


async def test_list_checkpoints_empty(journal: Journal):
    assert await journal.list_checkpoints() == []


async def test_new_checkpoint_ordering(journal: Journal):
    first = await journal.new_checkpoint("first")
    second = await journal.new_checkpoint("second")
    third = await journal.new_checkpoint("third")

    assert first < second < third
    # current_checkpoint() is always the most recently created one.
    assert await journal.current_checkpoint() == third


async def test_changes_since_filters_by_checkpoint(journal: Journal):
    cp1 = await journal.new_checkpoint("cp1")
    await journal.record_change(cp1, "srv", "toolA", {"x": 1}, {"id": 1}, None, "reversible")

    cp2 = await journal.new_checkpoint("cp2")
    await journal.record_change(cp2, "srv", "toolB", {"x": 2}, {"id": 2}, None, "reversible")

    # From cp2 onward: only toolB's change.
    since_cp2 = await journal.changes_since(cp2)
    assert [c.tool for c in since_cp2] == ["toolB"]

    # From cp1 onward: both, newest first.
    since_cp1 = await journal.changes_since(cp1)
    assert [c.tool for c in since_cp1] == ["toolB", "toolA"]

    # No argument: every uncompensated change, newest first.
    all_changes = await journal.changes_since(None)
    assert [c.tool for c in all_changes] == ["toolB", "toolA"]


async def test_changes_since_orders_newest_first_lifo(journal: Journal):
    cp = await journal.new_checkpoint("cp")
    await journal.record_change(cp, "srv", "first", {}, None, None, "reversible")
    await journal.record_change(cp, "srv", "second", {}, None, None, "reversible")
    await journal.record_change(cp, "srv", "third", {}, None, None, "reversible")

    changes = await journal.changes_since(cp)
    # Newest first -- this ordering is what makes undo LIFO.
    assert [c.tool for c in changes] == ["third", "second", "first"]


async def test_compensated_changes_excluded_from_changes_since(journal: Journal):
    cp = await journal.new_checkpoint("cp")
    change_id = await journal.record_change(cp, "srv", "toolA", {}, None, None, "reversible")
    await journal.record_change(cp, "srv", "toolB", {}, None, None, "reversible")

    await journal.mark_compensated(change_id)

    remaining = await journal.changes_since(cp)
    assert [c.tool for c in remaining] == ["toolB"]

    all_remaining = await journal.changes_since(None)
    assert [c.tool for c in all_remaining] == ["toolB"]


async def test_record_change_round_trips_json_fields(journal: Journal):
    cp = await journal.new_checkpoint("cp")
    await journal.record_change(
        cp,
        "srv",
        "delete_task",
        arguments={"id": 5},
        result={"id": 5, "deleted": True},
        snapshot={"id": 5, "title": "hi", "status": "open"},
        classification="compensable",
    )

    [change] = await journal.changes_since(cp)
    assert change.arguments == {"id": 5}
    assert change.result == {"id": 5, "deleted": True}
    assert change.snapshot == {"id": 5, "title": "hi", "status": "open"}
    assert change.classification == "compensable"
    assert change.compensated is False


async def test_record_change_with_none_result_and_snapshot(journal: Journal):
    cp = await journal.new_checkpoint("cp")
    await journal.record_change(cp, "srv", "toolA", {}, result=None, snapshot=None, classification="reversible")

    [change] = await journal.changes_since(cp)
    assert change.result is None
    assert change.snapshot is None
