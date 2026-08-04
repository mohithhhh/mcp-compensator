"""Full demo flow, run as an actual assertion rather than just printed
output: add -> complete -> delete -> undo -> assert the task list is
restored to exactly what it was right after the checkpoint."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from demo import run_demo  # noqa: E402


async def test_demo_undo_restores_pre_mistake_state():
    outcome = await run_demo()

    before = outcome["before"]
    after = outcome["after"]

    assert before == after, f"undo_to did not restore state:\nbefore={before}\nafter={after}"


async def test_demo_undo_outcome_has_no_skipped_changes():
    # Every change in this scenario is reversible/compensable and has a
    # configured inverse, so nothing should be skipped.
    outcome = await run_demo()
    undo_outcome = outcome["undo_outcome"]

    assert undo_outcome["skipped"] == []
    assert len(undo_outcome["undone"]) == 4  # add x2, complete, delete
